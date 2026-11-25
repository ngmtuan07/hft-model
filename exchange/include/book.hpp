// Price-time priority limit order book (one instrument), header-only.
//
// Hot-path design:
//   * prices are integer ticks indexing two flat arrays of levels (bids, asks): O(1) level access
//   * each level is an intrusive doubly linked FIFO of orders living in one preallocated pool
//   * order lookup by 64-bit key uses an open-addressing hash map (linear probing,
//     backward-shift deletion): no std::unordered_map nodes, no allocation per order
//   * events go to a Listener template parameter: calls inline, no virtual dispatch
//
// Order keys: the caller chooses them. The exchange uses
//   LOBSTER order ids as-is (background flow), SEED_BIT|n for the seeded opening book,
//   CLIENT_BIT|owner<<32|client_order_id for contestants.
#pragma once
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <vector>

namespace me {

using Px = uint32_t;   // ticks; 0 means "no price"
using Qty = uint32_t;
constexpr uint32_t NIL = 0xFFFFFFFFu;
constexpr uint8_t BUY = 1;
constexpr uint8_t SELL = 2;
constexpr uint64_t SEED_BIT = 1ull << 62;
constexpr uint64_t CLIENT_BIT = 1ull << 63;

inline uint64_t client_key(uint32_t owner, uint32_t client_order_id) {
    return CLIENT_BIT | (uint64_t(owner) << 32) | client_order_id;
}

struct Order {
    uint64_t key;
    uint32_t prev, next;
    Px px;
    Qty qty;
    uint32_t owner;       // 0 = background (historical flow), 1.. = contestants
    uint32_t client_id;   // contestant's client_order_id
    Qty ahead0;           // visible qty already queued at this price when the order arrived
    uint8_t side;
};

struct Level {
    uint32_t head = NIL, tail = NIL;
    uint64_t qty = 0;
    uint64_t bg = 0;      // part of qty owned by background (owner 0): O(1) reconciliation
    uint32_t count = 0;
};

// u64 -> u32 open-addressing map. Key 0 is reserved as "empty".
class FlatMap {
public:
    explicit FlatMap(size_t capacity_pow2 = 1u << 20) { resize(capacity_pow2); }

    uint32_t find(uint64_t k) const {
        size_t i = hash(k) & mask_;
        while (true) {
            const uint64_t kk = keys_[i];
            if (kk == k) return vals_[i];
            if (kk == 0) return NIL;
            i = (i + 1) & mask_;
        }
    }
    bool insert(uint64_t k, uint32_t v) {          // false if the key already exists
        if ((size_ + 1) * 10 > keys_.size() * 7) grow();
        size_t i = hash(k) & mask_;
        while (keys_[i] != 0) {
            if (keys_[i] == k) return false;
            i = (i + 1) & mask_;
        }
        keys_[i] = k;
        vals_[i] = v;
        ++size_;
        return true;
    }
    void erase(uint64_t k) {
        size_t i = hash(k) & mask_;
        while (keys_[i] != k) {
            if (keys_[i] == 0) return;
            i = (i + 1) & mask_;
        }
        // backward-shift deletion keeps probe chains intact without tombstones
        size_t j = i;
        while (true) {
            j = (j + 1) & mask_;
            if (keys_[j] == 0) break;
            const size_t home = hash(keys_[j]) & mask_;
            const bool between = (i <= j) ? (i < home && home <= j) : (i < home || home <= j);
            if (between) continue;
            keys_[i] = keys_[j];
            vals_[i] = vals_[j];
            i = j;
        }
        keys_[i] = 0;
        --size_;
    }
    size_t size() const { return size_; }

private:
    static size_t hash(uint64_t k) {              // splitmix64 finaliser
        k ^= k >> 30; k *= 0xbf58476d1ce4e5b9ull;
        k ^= k >> 27; k *= 0x94d049bb133111ebull;
        return size_t(k ^ (k >> 31));
    }
    void resize(size_t n) {
        keys_.assign(n, 0);
        vals_.assign(n, NIL);
        mask_ = n - 1;
        size_ = 0;
    }
    void grow() {
        std::vector<uint64_t> ok = std::move(keys_);
        std::vector<uint32_t> ov = std::move(vals_);
        resize(ok.size() * 2);
        for (size_t i = 0; i < ok.size(); ++i)
            if (ok[i]) insert(ok[i], ov[i]);
    }
    std::vector<uint64_t> keys_;
    std::vector<uint32_t> vals_;
    size_t mask_ = 0, size_ = 0;
};

struct NullListener {
    void on_trade(const Order&, uint32_t, uint32_t, Px, Qty, Qty, Qty) {}
};

// Listener must provide:
//   on_trade(const Order& maker, uint32_t taker_owner, uint32_t taker_client_id,
//            Px px, Qty qty, Qty maker_leaves, Qty taker_leaves)
// called before a fully filled maker is removed from the book.
template <class Listener>
class Book {
public:
    enum Result : uint8_t { RESTED, FILLED, IOC_DONE, REJECT_CROSS, REJECT_DUP, REJECT_BAD };

    Book(Px max_px, Listener& l, size_t order_capacity = 1u << 20)
        : max_px_(max_px), bids_(max_px + 2), asks_(max_px + 2), map_(1u << 20), l_(l) {
        pool_.reserve(order_capacity);
        free_.reserve(order_capacity);
    }

    // Submit a limit order. Matches against the opposite side in price-time priority,
    // then rests the remainder (GTC) or drops it (IOC). *remaining = unfilled qty.
    Result submit(uint64_t key, uint32_t owner, uint32_t client_id, uint8_t side, Px px, Qty qty,
                  bool ioc, bool post_only, Qty* remaining = nullptr) {
        if (qty == 0 || px == 0 || px > max_px_ || (side != BUY && side != SELL)) return REJECT_BAD;
        if (!ioc && map_.find(key) != NIL) return REJECT_DUP;
        const bool crosses = side == BUY ? (best_ask_ && best_ask_ <= px) : (best_bid_ && best_bid_ >= px);
        if (post_only && crosses) return REJECT_CROSS;
        Qty rem = qty;
        if (crosses) rem = side == BUY ? match<BUY>(owner, client_id, px, rem) : match<SELL>(owner, client_id, px, rem);
        if (remaining) *remaining = rem;
        if (rem == 0) return FILLED;
        if (ioc) return IOC_DONE;
        rest(key, owner, client_id, side, px, rem);
        return RESTED;
    }

    // Replay of a historical execution of resting order `key` for `qty` at `px`, by an aggressor
    // on `taker_side`. History says which order traded; the exchange's real priority rules are
    // richer than pure price-time (reserve orders refresh, etc.), so we honour that choice for
    // background orders. Contestant orders that have priority in OUR book trade first:
    //   1. any contestant order at a better price than px
    //   2. contestant orders ahead of `key` in the queue at px
    //   3. then `key` itself; any remainder (or an unknown key) trades in normal priority order
    // With no contestants this reproduces the historical book exactly.
    Qty execute_historical(uint64_t key, uint8_t taker_side, Px px, Qty qty, uint32_t taker_owner = 0) {
        const uint32_t target = map_.find(key);
        const uint8_t maker_side = taker_side == BUY ? SELL : BUY;
        if (target == NIL || pool_[target].px != px || pool_[target].side != maker_side)
            return qty - (taker_side == BUY ? match<BUY>(taker_owner, 0, px, qty) : match<SELL>(taker_owner, 0, px, qty));
        Qty rem = qty;
        // 1. better prices: only contestants can be there (historical best was px)
        for (Px p = maker_side == SELL ? best_ask_ : best_bid_; rem > 0 && p != 0 && p != px;
             p = maker_side == SELL ? best_ask_ : best_bid_) {
            if (maker_side == SELL ? p > px : p < px) break;
            rem = take_level(level(maker_side, p), p, taker_owner, rem, false, NIL);
            if (level(maker_side, p).count) break;   // level not exhausted (background left): stop
        }
        // 2. contestants ahead of the target at px, 3. the target
        if (rem > 0) rem = take_level(level(maker_side, px), px, taker_owner, rem, true, target);
        // remainder: normal priority
        if (rem > 0) rem = taker_side == BUY ? match<BUY>(taker_owner, 0, px, rem) : match<SELL>(taker_owner, 0, px, rem);
        return qty - rem;
    }

    // Cancel the whole remaining quantity. Returns the cancelled qty (0 if not found).
    Qty cancel(uint64_t key) {
        const uint32_t i = map_.find(key);
        if (i == NIL) return 0;
        const Qty q = pool_[i].qty;
        remove(i);
        return q;
    }

    // Reduce an order by `by` shares (removes it if that is all of it). Returns the reduction.
    Qty reduce(uint64_t key, Qty by) {
        const uint32_t i = map_.find(key);
        if (i == NIL) return 0;
        Order& o = pool_[i];
        if (by >= o.qty) {
            const Qty q = o.qty;
            remove(i);
            return q;
        }
        o.qty -= by;
        level(o.side, o.px).qty -= by;
        if (o.owner == 0) level(o.side, o.px).bg -= by;
        return by;
    }

    // Reduce background liquidity resting at a price, oldest first, preferring seeded
    // orders. Used for historical cancels of orders that existed before the replay started.
    Qty reduce_background_at(uint8_t side, Px px, Qty by) {
        if (px == 0 || px > max_px_) return 0;
        Qty done = 0;
        for (int pass = 0; pass < 2 && done < by; ++pass) {
            uint32_t i = level(side, px).head;
            while (i != NIL && done < by) {
                const uint32_t nxt = pool_[i].next;
                Order& o = pool_[i];
                const bool eligible = o.owner == 0 && (pass == 1 || (o.key & SEED_BIT));
                if (eligible) {
                    const Qty take = (by - done < o.qty) ? by - done : o.qty;
                    done += take;
                    if (take == o.qty) remove(i);
                    else { o.qty -= take; level(side, px).qty -= take; level(side, px).bg -= take; }
                }
                i = nxt;
            }
        }
        return done;
    }

    // Make the background (owner 0) quantity at one price equal `target`: excess is removed from
    // the back of the queue (lowest priority first), a shortfall is added as one synthetic order
    // at the back. Contestant orders are never touched. Returns true if anything changed.
    bool reconcile_background(uint8_t side, Px px, uint64_t target, uint64_t synth_key) {
        if (px == 0 || px > max_px_) return false;
        Level& lv = level(side, px);
        const uint64_t bg = lv.bg;
        if (bg == target) return false;
        if (bg < target) { rest(synth_key, 0, 0, side, px, Qty(target - bg)); return true; }
        uint64_t excess = bg - target;
        uint32_t i = lv.tail;
        while (excess > 0 && i != NIL) {
            const uint32_t prv = pool_[i].prev;
            Order& o = pool_[i];
            if (o.owner == 0) {
                if (o.qty <= excess) { excess -= o.qty; remove(i); }
                else { o.qty -= Qty(excess); level(side, px).qty -= excess; level(side, px).bg -= excess; excess = 0; }
            }
            i = prv;
        }
        return true;
    }

    Px best_bid() const { return best_bid_; }
    Px best_ask() const { return best_ask_; }
    uint64_t bid_qty() const { return best_bid_ ? bids_[best_bid_].qty : 0; }
    uint64_t ask_qty() const { return best_ask_ ? asks_[best_ask_].qty : 0; }
    uint64_t qty_at(uint8_t side, Px px) const {
        if (px == 0 || px > max_px_) return 0;
        return side == BUY ? bids_[px].qty : asks_[px].qty;
    }
    const Order* find(uint64_t key) const {
        const uint32_t i = map_.find(key);
        return i == NIL ? nullptr : &pool_[i];
    }
    size_t open_orders() const { return map_.size(); }
    Px max_px() const { return max_px_; }

    // Walks the book in priority order (for tests): f(side, px, const Order&).
    template <class F>
    void for_each_level(uint8_t side, int max_levels, F f) const {
        int n = 0;
        if (side == BUY) {
            for (Px p = best_bid_; p >= 1 && n < max_levels && best_bid_; --p)
                if (bids_[p].count) { f(p, bids_[p]); ++n; }
        } else {
            for (Px p = best_ask_; p <= max_px_ && n < max_levels && best_ask_; ++p)
                if (asks_[p].count) { f(p, asks_[p]); ++n; }
        }
    }

private:
    Level& level(uint8_t side, Px px) { return side == BUY ? bids_[px] : asks_[px]; }

    template <uint8_t SIDE>
    Qty match(uint32_t owner, uint32_t client_id, Px limit, Qty rem) {
        // SIDE is the taker's side: a buyer lifts asks from best_ask_ upward
        while (rem > 0) {
            Px p = SIDE == BUY ? best_ask_ : best_bid_;
            if (p == 0) break;
            if (SIDE == BUY ? p > limit : p < limit) break;
            Level& lv = SIDE == BUY ? asks_[p] : bids_[p];
            while (rem > 0 && lv.head != NIL) {
                const uint32_t i = lv.head;
                Order& m = pool_[i];
                const Qty q = rem < m.qty ? rem : m.qty;
                rem -= q;
                m.qty -= q;
                lv.qty -= q;
                if (m.owner == 0) lv.bg -= q;
                l_.on_trade(m, owner, client_id, p, q, m.qty, rem);
                if (m.qty == 0) remove(i);
            }
        }
        return rem;
    }

    // Walk one level in priority order. Contestant orders always trade; background orders trade
    // only if !stop_at_target (better-priced levels: contestants only, so background is skipped)
    // or if they are the target itself. Stops after the target is done.
    Qty take_level(Level& lv, Px p, uint32_t taker_owner, Qty rem, bool to_target, uint32_t target) {
        uint32_t i = lv.head;
        while (rem > 0 && i != NIL) {
            const uint32_t nxt = pool_[i].next;
            Order& m = pool_[i];
            const bool is_target = i == target;
            if (m.owner != 0 || is_target) {
                const Qty q = rem < m.qty ? rem : m.qty;
                rem -= q;
                m.qty -= q;
                lv.qty -= q;
                if (m.owner == 0) lv.bg -= q;
                l_.on_trade(m, taker_owner, 0, p, q, m.qty, rem);
                if (m.qty == 0) remove(i);
            }
            if (is_target && to_target) break;
            i = nxt;
        }
        return rem;
    }

    void rest(uint64_t key, uint32_t owner, uint32_t client_id, uint8_t side, Px px, Qty qty) {
        uint32_t i;
        if (!free_.empty()) { i = free_.back(); free_.pop_back(); }
        else { i = uint32_t(pool_.size()); pool_.emplace_back(); }
        Order& o = pool_[i];
        o.key = key; o.px = px; o.qty = qty; o.owner = owner; o.client_id = client_id; o.side = side;
        Level& lv = level(side, px);
        o.ahead0 = lv.qty > 0xFFFFFFFFull ? 0xFFFFFFFFu : Qty(lv.qty);
        o.prev = lv.tail;
        o.next = NIL;
        if (lv.tail != NIL) pool_[lv.tail].next = i; else lv.head = i;
        lv.tail = i;
        lv.qty += qty;
        if (owner == 0) lv.bg += qty;
        ++lv.count;
        map_.insert(key, i);
        if (side == BUY) { if (px > best_bid_) best_bid_ = px; ++n_bids_; }
        else { if (best_ask_ == 0 || px < best_ask_) best_ask_ = px; ++n_asks_; }
    }

    void remove(uint32_t i) {
        Order& o = pool_[i];
        Level& lv = level(o.side, o.px);
        if (o.prev != NIL) pool_[o.prev].next = o.next; else lv.head = o.next;
        if (o.next != NIL) pool_[o.next].prev = o.prev; else lv.tail = o.prev;
        lv.qty -= o.qty;
        if (o.owner == 0) lv.bg -= o.qty;
        --lv.count;
        map_.erase(o.key);
        free_.push_back(i);
        if (o.side == BUY) {
            --n_bids_;
            if (lv.count == 0 && o.px == best_bid_) {
                if (n_bids_ == 0) best_bid_ = 0;
                else { Px p = best_bid_; while (p > 1 && bids_[p].count == 0) --p; best_bid_ = bids_[p].count ? p : 0; }
            }
        } else {
            --n_asks_;
            if (lv.count == 0 && o.px == best_ask_) {
                if (n_asks_ == 0) best_ask_ = 0;
                else { Px p = best_ask_; while (p < max_px_ && asks_[p].count == 0) ++p; best_ask_ = asks_[p].count ? p : 0; }
            }
        }
    }

    Px max_px_;
    std::vector<Level> bids_, asks_;
    std::vector<Order> pool_;
    std::vector<uint32_t> free_;
    FlatMap map_;
    Listener& l_;
    Px best_bid_ = 0, best_ask_ = 0;
    uint64_t n_bids_ = 0, n_asks_ = 0;
};

}  // namespace me
