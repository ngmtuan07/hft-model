// Matching engine unit tests (no framework: each check aborts with a message on failure).
#include <cstdio>
#include <cstdlib>
#include <random>
#include <unordered_map>
#include <vector>

#include "book.hpp"
#include "util.hpp"

#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); std::exit(1); } } while (0)

struct Fill { uint32_t maker_owner, maker_cid, taker_owner; me::Px px; me::Qty q, maker_leaves, taker_leaves; };
struct Rec {
    std::vector<Fill> fills;
    void on_trade(const me::Order& m, uint32_t to, uint32_t, me::Px px, me::Qty q, me::Qty ml, me::Qty tl) {
        fills.push_back({m.owner, m.client_id, to, px, q, ml, tl});
    }
};

using B = me::Book<Rec>;

static void price_time_priority() {
    Rec r; B b(1000, r);
    CHECK(b.submit(1, 1, 11, me::SELL, 101, 100, false, false) == B::RESTED);
    CHECK(b.submit(2, 2, 22, me::SELL, 101, 100, false, false) == B::RESTED);
    CHECK(b.submit(3, 3, 33, me::SELL, 100, 50, false, false) == B::RESTED);
    CHECK(b.best_ask() == 100 && b.ask_qty() == 50);
    me::Qty rem = 0;
    CHECK(b.submit(4, 4, 44, me::BUY, 101, 120, true, false, &rem) == B::FILLED && rem == 0);
    CHECK(r.fills.size() == 2);
    CHECK(r.fills[0].maker_owner == 3 && r.fills[0].px == 100 && r.fills[0].q == 50);     // better price first
    CHECK(r.fills[1].maker_owner == 1 && r.fills[1].px == 101 && r.fills[1].q == 70);     // then time priority
    CHECK(r.fills[1].maker_leaves == 30 && r.fills[1].taker_leaves == 0);
    CHECK(b.best_ask() == 101 && b.ask_qty() == 130);
}

static void limit_rests_remainder() {
    Rec r; B b(1000, r);
    b.submit(1, 1, 1, me::SELL, 105, 100, false, false);
    me::Qty rem = 0;
    CHECK(b.submit(2, 2, 2, me::BUY, 106, 300, false, false, &rem) == B::RESTED && rem == 200);
    CHECK(b.best_bid() == 106 && b.bid_qty() == 200 && b.best_ask() == 0);
}

static void post_only_and_ioc() {
    Rec r; B b(1000, r);
    b.submit(1, 1, 1, me::SELL, 105, 100, false, false);
    CHECK(b.submit(2, 2, 2, me::BUY, 105, 100, false, true) == B::REJECT_CROSS);
    CHECK(b.submit(3, 2, 3, me::BUY, 104, 100, false, true) == B::RESTED);
    CHECK(b.submit(4, 2, 4, me::BUY, 103, 100, true, false) == B::IOC_DONE);     // IOC that can't trade
    CHECK(b.find(4) == nullptr && b.best_bid() == 104);
    CHECK(b.submit(3, 2, 3, me::BUY, 104, 100, false, false) == B::REJECT_DUP);
    CHECK(b.submit(5, 2, 5, me::BUY, 0, 100, false, false) == B::REJECT_BAD);
    CHECK(b.submit(6, 2, 6, me::BUY, 2000, 100, false, false) == B::REJECT_BAD);
    CHECK(r.fills.empty());
}

static void cancel_reduce_and_best_price() {
    Rec r; B b(1000, r);
    b.submit(1, 0, 0, me::BUY, 100, 100, false, false);
    b.submit(2, 0, 0, me::BUY, 98, 100, false, false);
    b.submit(3, 0, 0, me::SELL, 103, 100, false, false);
    CHECK(b.reduce(1, 40) == 40 && b.bid_qty() == 60);
    CHECK(b.cancel(1) == 60 && b.best_bid() == 98);        // level emptied: best moves to next level
    CHECK(b.cancel(1) == 0);
    CHECK(b.reduce(2, 500) == 100 && b.best_bid() == 0);    // over-reduce removes it, side empty
    CHECK(b.cancel(3) == 100 && b.best_ask() == 0 && b.open_orders() == 0);
}

static void background_reduce_prefers_seed() {
    Rec r; B b(1000, r);
    b.submit(77, 0, 0, me::SELL, 110, 100, false, false);                   // historical order
    b.submit(me::SEED_BIT | 1, 0, 0, me::SELL, 110, 300, false, false);      // seeded opening book
    b.submit(me::client_key(1, 5), 1, 5, me::SELL, 110, 100, false, false);  // contestant
    CHECK(b.reduce_background_at(me::SELL, 110, 250) == 250);
    CHECK(b.find(me::SEED_BIT | 1)->qty == 50 && b.find(77)->qty == 100 && b.find(me::client_key(1, 5))->qty == 100);
    CHECK(b.reduce_background_at(me::SELL, 110, 1000) == 150);              // never touches contestants
    CHECK(b.ask_qty() == 100 && b.find(me::client_key(1, 5)) != nullptr);
}

static void contestant_priority_over_later_background() {
    Rec r; B b(1000, r);
    b.submit(me::client_key(2, 1), 2, 1, me::BUY, 50, 100, false, true);   // contestant first at a new price
    b.submit(900, 0, 0, me::BUY, 50, 500, false, false);                    // background joins behind
    b.submit(0, 0, 0, me::SELL, 50, 150, true, false);                       // historical execution replayed as IOC
    CHECK(r.fills.size() == 2 && r.fills[0].maker_owner == 2 && r.fills[0].q == 100 && r.fills[1].q == 50);
}

static void flatmap_matches_reference() {
    me::FlatMap m(16);
    std::unordered_map<uint64_t, uint32_t> ref;
    std::mt19937_64 rng(1);
    for (int i = 0; i < 200000; ++i) {
        const uint64_t k = 1 + rng() % 5000;
        const int op = int(rng() % 3);
        if (op == 0) { const bool ins = m.insert(k, uint32_t(i)); CHECK(ins == (ref.find(k) == ref.end())); if (ins) ref[k] = uint32_t(i); }
        else if (op == 1) { m.erase(k); ref.erase(k); }
        else { auto it = ref.find(k); CHECK(m.find(k) == (it == ref.end() ? me::NIL : it->second)); }
    }
    CHECK(m.size() == ref.size());
}

static void random_book_invariants() {
    // random flow; after every step the best prices must equal a brute-force scan and the book
    // must never be crossed
    Rec r; B b(400, r);
    std::mt19937_64 rng(7);
    std::vector<uint64_t> live;
    uint64_t next = 1;
    for (int i = 0; i < 100000; ++i) {
        const int op = int(rng() % 10);
        if (op < 6) {
            const uint8_t side = rng() % 2 ? me::BUY : me::SELL;
            const me::Px px = me::Px(180 + rng() % 40);
            const bool ioc = rng() % 5 == 0;
            if (b.submit(next, 0, 0, side, px, me::Qty(1 + rng() % 500), ioc, false) == B::RESTED) live.push_back(next);
            ++next;
        } else if (!live.empty()) {
            const size_t j = rng() % live.size();
            if (op < 9) b.cancel(live[j]); else b.reduce(live[j], me::Qty(1 + rng() % 200));
            if (!b.find(live[j])) { live[j] = live.back(); live.pop_back(); }
        }
        me::Px bb = 0, ba = 0;
        for (me::Px p = 400; p >= 1; --p) if (b.qty_at(me::BUY, p)) { bb = p; break; }
        for (me::Px p = 1; p <= 400; ++p) if (b.qty_at(me::SELL, p)) { ba = p; break; }
        CHECK(b.best_bid() == bb && b.best_ask() == ba);
        CHECK(bb == 0 || ba == 0 || bb < ba);
    }
}

static void reconcile_background_counts() {
    Rec r; B b(1000, r);
    b.submit(1, 0, 0, me::BUY, 100, 300, false, false);
    b.submit(me::client_key(1, 1), 1, 1, me::BUY, 100, 100, false, false);
    b.submit(2, 0, 0, me::BUY, 100, 200, false, false);
    CHECK(!b.reconcile_background(me::BUY, 100, 500, 99));                 // already 500 background
    CHECK(b.reconcile_background(me::BUY, 100, 350, 99));                  // trims 150 from the back
    CHECK(b.find(2)->qty == 50 && b.find(1)->qty == 300 && b.bid_qty() == 450);
    b.submit(0, 0, 0, me::SELL, 100, 320, true, false);                     // 300 background + 20 contestant
    CHECK(b.find(me::client_key(1, 1))->qty == 80);
    CHECK(!b.reconcile_background(me::BUY, 100, 50, 99));                  // background is exactly 50
    CHECK(b.reconcile_background(me::BUY, 100, 120, 99));                   // shortfall -> synthetic order
    CHECK(b.find(99)->qty == 70 && b.qty_at(me::BUY, 100) == 200);
    CHECK(b.reconcile_background(me::BUY, 100, 0, 98));                     // clears background only
    CHECK(b.qty_at(me::BUY, 100) == 80 && b.find(me::client_key(1, 1)) != nullptr);
}

static void histogram_percentiles() {
    util::Histogram h;
    for (uint64_t v = 1; v <= 100000; ++v) h.add(v);
    const double p50 = double(h.percentile(50)), p99 = double(h.percentile(99));
    CHECK(p50 >= 50000 && p50 <= 50000 * 1.04);     // 32 sub-buckets: ~3% bucket width
    CHECK(p99 >= 99000 && p99 <= 99000 * 1.04);
    CHECK(h.percentile(100) == 100000 && h.count() == 100000);
}

int main() {
    price_time_priority();
    limit_rests_remainder();
    post_only_and_ioc();
    cancel_reduce_and_best_price();
    background_reduce_prefers_seed();
    contestant_priority_over_later_background();
    flatmap_matches_reference();
    random_book_invariants();
    reconcile_background_counts();
    histogram_percentiles();
    std::printf("all matching-engine tests passed\n");
    return 0;
}
