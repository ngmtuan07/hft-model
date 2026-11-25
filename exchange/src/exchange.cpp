// Exchange box: matching engine + LOBSTER background flow + UDP feed/order entry + race judge.
//
//   exchange run    --messages <LOBSTER message file> [--start 10:00:00] [--duration 60] [--speed 1]
//                   [--port 9000] [--wait 2] [--stats out.jsonl] [--summary out.json] [--core N]
//   exchange verify --messages <file>          replay with no contestants; compare our top of book
//                                              with LOBSTER's after every message
//   exchange bench  --messages <file> [--reps 5]  matching-engine throughput and per-message latency
//
// Single thread, busy polling. Every order is timestamped on arrival with the exchange clock;
// latency = arrival of the order - time the triggering Tob was sent to that contestant.
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "book.hpp"
#include "lobster.hpp"
#include "proto.hpp"
#include "util.hpp"

using namespace proto;

namespace {

volatile std::sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

struct Args {
    std::string mode, messages, stats, summary, order_log, host = "127.0.0.1";
    std::string start;              // HH:MM:SS of the trading day; default: first message
    double duration = 60.0;         // wall seconds of replay
    double speed = 1.0;             // 1 = real time, 0 = as fast as possible
    int port = 9000, wait = 0, reps = 5, core = -1, max_contestants = 8;
    bool no_reconcile = false;      // replay messages only, without snapping to LOBSTER snapshots
};

Args parse_args(int argc, char** argv) {
    Args a;
    if (argc < 2) { std::fprintf(stderr, "usage: exchange run|verify|bench --messages FILE [options]\n"); std::exit(2); }
    a.mode = argv[1];
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        auto v = [&]() -> std::string { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", k.c_str()); std::exit(2); } return argv[++i]; };
        if (k == "--messages") a.messages = v();
        else if (k == "--start") a.start = v();
        else if (k == "--duration") a.duration = std::atof(v().c_str());
        else if (k == "--speed") a.speed = std::atof(v().c_str());
        else if (k == "--port") a.port = std::atoi(v().c_str());
        else if (k == "--host") a.host = v();
        else if (k == "--wait") a.wait = std::atoi(v().c_str());
        else if (k == "--stats") a.stats = v();
        else if (k == "--summary") a.summary = v();
        else if (k == "--order-log") a.order_log = v();
        else if (k == "--reps") a.reps = std::atoi(v().c_str());
        else if (k == "--core") a.core = std::atoi(v().c_str());
        else if (k == "--no-reconcile") a.no_reconcile = true;
        else { std::fprintf(stderr, "unknown option %s\n", k.c_str()); std::exit(2); }
    }
    if (a.messages.empty()) { std::fprintf(stderr, "--messages is required\n"); std::exit(2); }
    return a;
}

uint64_t parse_hms(const std::string& s) {
    int h = 0, m = 0, sec = 0;
    std::sscanf(s.c_str(), "%d:%d:%d", &h, &m, &sec);
    return (uint64_t(h) * 3600 + uint64_t(m) * 60 + uint64_t(sec)) * 1000000000ull;
}

std::string hms(uint64_t ns) {
    char b[16];
    const uint64_t s = ns / 1000000000ull;
    std::snprintf(b, sizeof(b), "%02llu:%02llu:%02llu", (unsigned long long)(s / 3600),
                  (unsigned long long)(s / 60 % 60), (unsigned long long)(s % 60));
    return b;
}

// Applies historical LOBSTER messages to the book as background participant 0.
template <class BookT>
struct Replayer {
    BookT& book;
    uint64_t seed_n = 0;
    uint64_t skipped = 0;

    void seed(const lob::Row& r) {
        for (const auto& l : r.bids) if (l.px && l.qty) book.submit(me::SEED_BIT | ++seed_n, 0, 0, me::BUY, l.px, l.qty, false, false);
        for (const auto& l : r.asks) if (l.px && l.qty) book.submit(me::SEED_BIT | ++seed_n, 0, 0, me::SELL, l.px, l.qty, false, false);
    }

    uint64_t synth = 0;
    uint64_t reconciled = 0;

    void apply(const lob::Msg& m, const lob::Target* t = nullptr) {
        const uint8_t side = m.dir == 1 ? me::BUY : me::SELL;
        switch (m.type) {
            case 1:   // new limit order (may now trade with a contestant that improved the price)
                book.submit(m.id, 0, 0, side, m.px, m.size, false, false);
                break;
            case 2:   // partial cancel
                if (!book.reduce(m.id, m.size)) book.reduce_background_at(side, m.px, m.size);
                break;
            case 3:   // full delete
                if (!book.cancel(m.id)) book.reduce_background_at(side, m.px, m.size);
                break;
            case 4: { // visible execution: the historical order trades, unless a contestant had
                      // priority over it in our book (see Book::execute_historical)
                const uint8_t aggressor = side == me::BUY ? me::SELL : me::BUY;
                book.execute_historical(m.id, aggressor, m.px, m.size);
                break;
            }
            default:  // 5 hidden execution, 6 cross, 7 halt: no visible book change
                ++skipped;
        }
        if (t) reconcile(m, side, *t);
    }

    // Snap background liquidity to the historical snapshot at the touched price and at the
    // historical best bid/ask (reserve-order refills and other unreported changes), and clear
    // stale background liquidity priced better than history's best. Contestants untouched.
    void reconcile(const lob::Msg& m, uint8_t side, const lob::Target& t) {
        if (t.msg_qty != lob::UNKNOWN) set(side, m.px, t.msg_qty);
        clear_better(me::BUY, t.bid);
        clear_better(me::SELL, t.ask);
        if (t.bid) set(me::BUY, t.bid, t.bid_qty);
        if (t.ask) set(me::SELL, t.ask, t.ask_qty);
    }
    void set(uint8_t side, me::Px px, uint64_t q) {
        reconciled += book.reconcile_background(side, px, q, me::SEED_BIT | (1ull << 61) | ++synth);
    }
    void clear_better(uint8_t side, me::Px hist_best) {
        if (!hist_best) return;
        int guard = 1000;
        if (side == me::BUY) {
            for (me::Px p = book.best_bid(); p > hist_best && guard-- > 0; --p)
                if (book.qty_at(me::BUY, p)) set(me::BUY, p, 0);
        } else {
            for (me::Px p = book.best_ask(); p && p < hist_best && guard-- > 0; ++p)
                if (book.qty_at(me::SELL, p)) set(me::SELL, p, 0);
        }
    }
};

me::Px max_price(const std::vector<lob::Msg>& msgs) {
    me::Px mx = 0;
    for (const auto& m : msgs) if (m.px > mx) mx = m.px;
    return mx * 2 + 1000;
}

size_t first_index_at(const std::vector<lob::Msg>& msgs, uint64_t t) {
    size_t i = 0;
    while (i < msgs.size() && msgs[i].ts_ns < t) ++i;
    return i;
}

// ------------------------------------------------------------------------------- verify
int run_verify(const Args& a) {
    const auto msgs = lob::load_messages(a.messages);
    const auto tops = lob::load_tops(lob::orderbook_path(a.messages));
    if (tops.size() != msgs.size()) { std::fprintf(stderr, "row count mismatch\n"); return 1; }
    const bool reconcile = !a.no_reconcile;
    const auto targets = reconcile ? lob::load_targets(lob::orderbook_path(a.messages), msgs) : std::vector<lob::Target>();
    me::NullListener nl;
    me::Book<me::NullListener> book(max_price(msgs), nl);
    Replayer<me::Book<me::NullListener>> rp{book};
    rp.seed(lob::read_row(lob::orderbook_path(a.messages), 0));
    uint64_t match = 0, px_match = 0, n = 0;
    size_t first_bad = 0;
    for (size_t i = 1; i < msgs.size(); ++i) {
        if (msgs[i].type == 7) continue;
        rp.apply(msgs[i], reconcile ? &targets[i] : nullptr);
        const auto& t = tops[i];
        const bool pm = book.best_bid() == t.bid && book.best_ask() == t.ask;
        const bool qm = pm && book.bid_qty() == t.bid_qty && book.ask_qty() == t.ask_qty;
        px_match += pm;
        match += qm;
        if (!qm && !first_bad) first_bad = i;
        ++n;
    }
    std::printf("{\"messages\": %llu, \"top_of_book_exact\": %.4f, \"top_prices_match\": %.4f, "
                "\"first_mismatch_row\": %zu, \"skipped_hidden_or_other\": %llu, \"reconciled_levels\": %llu, "
                "\"reconcile\": %s, \"open_orders_end\": %zu}\n",
                (unsigned long long)n, 100.0 * double(match) / double(n), 100.0 * double(px_match) / double(n),
                first_bad, (unsigned long long)rp.skipped, (unsigned long long)rp.reconciled,
                reconcile ? "true" : "false", book.open_orders());
    return 0;
}

// ------------------------------------------------------------------------------- bench
int run_bench(const Args& a) {
    const auto msgs = lob::load_messages(a.messages);
    const lob::Row seed = lob::read_row(lob::orderbook_path(a.messages), 0);
    const auto targets = a.no_reconcile ? std::vector<lob::Target>() : lob::load_targets(lob::orderbook_path(a.messages), msgs);
    const lob::Target* tg = a.no_reconcile ? nullptr : targets.data();
    util::Histogram lat;
    double best_rate = 0.0;
    for (int r = 0; r < a.reps; ++r) {
        me::NullListener nl;
        me::Book<me::NullListener> book(max_price(msgs), nl);
        Replayer<me::Book<me::NullListener>> rp{book};
        rp.seed(seed);
        const bool timed = r == a.reps - 1;      // last rep measures per-message latency
        const uint64_t t0 = util::now_ns();
        uint64_t sink = 0;
        for (size_t i = 1; i < msgs.size(); ++i) {
            if (timed) {
                const uint64_t s = util::now_ns();
                rp.apply(msgs[i], tg ? tg + i : nullptr);
                sink += book.best_bid();
                lat.add(util::now_ns() - s);
            } else {
                rp.apply(msgs[i], tg ? tg + i : nullptr);
                sink += book.best_bid();
            }
        }
        const double dt = double(util::now_ns() - t0) / 1e9;
        if (!timed) {
            const double rate = double(msgs.size() - 1) / dt;
            if (rate > best_rate) best_rate = rate;
        }
        if (sink == 42) std::printf(" ");
    }
    std::printf("{\"messages_per_rep\": %zu, \"throughput_msgs_per_s\": %.0f, \"ns_per_msg\": %.1f, "
                "\"timed_p50_ns\": %llu, \"timed_p99_ns\": %llu, \"timed_p999_ns\": %llu, \"timed_max_ns\": %llu, "
                "\"reconcile\": %s, \"note\": \"timed pass includes ~2 clock reads per message\"}\n",
                msgs.size() - 1, best_rate, best_rate > 0 ? 1e9 / best_rate : 0.0,
                (unsigned long long)lat.percentile(50), (unsigned long long)lat.percentile(99),
                (unsigned long long)lat.percentile(99.9), (unsigned long long)lat.max(), tg ? "true" : "false");
    return 0;
}

// ------------------------------------------------------------------------------- run
constexpr uint32_t RING = 1u << 16;

struct Contestant {
    uint32_t id = 0;
    std::string name;
    sockaddr_in oe{}, md{};
    uint64_t md_sent_ts[RING] = {};      // when Tob seq%RING was sent to this contestant
    uint64_t orders = 0, cancels = 0, rejects = 0, fills = 0, fill_qty = 0, races = 0, wins = 0;
    int64_t position = 0, cash = 0;      // cash in ticks*shares
    uint64_t fills_last = 0;
    uint64_t first_fills = 0;            // passive fills on orders that were first in line at their price
    int64_t markout_x2 = 0;              // sum of side*(mid_1s - px)*qty, in half-ticks (mid can be x.5)
    uint64_t markout_qty = 0;
    util::Histogram lat, lat_window, oe_oneway;   // oe_oneway: same-host only (shared clock)
};

struct PendingMarkout { uint64_t due_hist_ns; uint32_t owner; int8_t sgn; me::Px px; me::Qty qty; };

struct SeqInfo { uint32_t seq = 0; uint32_t mask = 0; uint32_t first = 0; bool raced = false; };

class Exchange {
public:
    explicit Exchange(const Args& a)
        : args_(a), msgs_(lob::load_messages(a.messages)), book_(max_price(msgs_), *this), rp_{book_} {
        if (!a.no_reconcile) targets_ = lob::load_targets(lob::orderbook_path(a.messages), msgs_);
        contestants_.reserve(size_t(a.max_contestants));
        fd_ = util::udp_socket(a.host, uint16_t(a.port));
        start_idx_ = a.start.empty() ? 1 : first_index_at(msgs_, parse_hms(a.start));
        if (start_idx_ == 0) start_idx_ = 1;
        if (start_idx_ >= msgs_.size()) throw std::runtime_error("--start is after the last message");
        rp_.seed(lob::read_row(lob::orderbook_path(a.messages), start_idx_ - 1));
        if (!a.stats.empty()) stats_f_ = std::fopen(a.stats.c_str(), "w");
        if (!a.order_log.empty()) {
            olog_ = std::fopen(a.order_log.c_str(), "w");
            if (olog_) std::fprintf(olog_, "t_ns,contestant,msg,coid,side,price,qty,trigger_seq,result,bid,bid_qty,ask,ask_qty\n");
        }
    }

    // Book listener: a trade happened. Report fills to contestants on either side.
    void on_trade(const me::Order& maker, uint32_t taker_owner, uint32_t taker_cid, me::Px px, me::Qty q,
                  me::Qty maker_leaves, me::Qty taker_leaves) {
        if (maker.owner) fill(maker.owner, maker.client_id, maker.side, px, q, maker_leaves, maker.ahead0 == 0);
        if (taker_owner) fill(taker_owner, taker_cid, maker.side == me::BUY ? me::SELL : me::BUY, px, q, taker_leaves, false);
    }

    int run() {
        std::printf("exchange: %zu messages loaded, replay from %s (row %zu), port %d, speed %.2fx, duration %.0fs\n",
                    msgs_.size(), hms(msgs_[start_idx_].ts_ns).c_str(), start_idx_, args_.port, args_.speed, args_.duration);
        if (args_.wait) std::printf("exchange: waiting for %d contestants...\n", args_.wait);
        std::fflush(stdout);
        if (util::pin_to_core(args_.core)) std::printf("exchange: pinned to core %d\n", args_.core);
        publish_if_changed();
        bool started = args_.wait == 0;
        uint64_t wall0 = util::now_ns(), next_print = 0;
        size_t next = start_idx_;
        const uint64_t lob0 = msgs_[start_idx_].ts_ns;
        alignas(64) unsigned char buf[2048];
        while (!g_stop) {
            // 1. drain order entry
            for (int k = 0; k < 256; ++k) {
                sockaddr_in from{};
                socklen_t fl = sizeof(from);
                const ssize_t n = ::recvfrom(fd_, buf, sizeof(buf), 0, reinterpret_cast<sockaddr*>(&from), &fl);
                if (n < 0) break;
                const uint64_t t_rx = util::now_ns();
                if (n == ssize_t(MSG_SIZE)) handle(buf, from, t_rx);
            }
            const uint64_t now = util::now_ns();
            if (!started) {
                if (int(contestants_.size()) >= args_.wait) { started = true; wall0 = now; std::printf("exchange: start\n"); }
                else continue;
            }
            // 2. replay ONE batch of history if due: the messages sharing a timestamp form one
            //    exchange event and produce at most one Tob. Orders are polled again before the
            //    next batch, so a burst of history never delays order entry by more than one batch.
            if (next < msgs_.size()) {
                const uint64_t due = args_.speed > 0 ? wall0 + uint64_t(double(msgs_[next].ts_ns - lob0) / args_.speed) : 0;
                if (due <= now) {
                    const uint64_t ts = msgs_[next].ts_ns;
                    hist_now_ = ts;
                    int cap = 64;
                    while (next < msgs_.size() && msgs_[next].ts_ns == ts && cap-- > 0) {
                        if (msgs_[next].type != 7) rp_.apply(msgs_[next], targets_.empty() ? nullptr : &targets_[next]);
                        ++next;
                    }
                    publish_if_changed();
                    settle_markouts();
                }
            }
            // 3. scoreboard
            const double el = double(now - wall0) / 1e9;
            if (now >= next_print) {
                scoreboard(el, next < msgs_.size() ? msgs_[next].ts_ns : msgs_.back().ts_ns);
                next_print = now + 1000000000ull;
            }
            if (next >= msgs_.size() || el >= args_.duration) break;
        }
        const double el = double(util::now_ns() - wall0) / 1e9;
        scoreboard(el, next < msgs_.size() ? msgs_[next].ts_ns : msgs_.back().ts_ns);
        summary(el, next - start_idx_);
        if (stats_f_) std::fclose(stats_f_);
        if (olog_) std::fclose(olog_);
        return 0;
    }

private:
    void handle(const unsigned char* buf, const sockaddr_in& from, uint64_t t_rx) {
        uint16_t type;
        std::memcpy(&type, buf, 2);
        if (type == MSG_HELLO) { hello(reinterpret_cast<const Hello*>(buf), from); return; }
        Contestant* c = by_addr(from);
        if (!c) { reply(from, MSG_REJECT, 0, 0, 0, 0, REJ_NOT_REGISTERED); return; }
        if (type == MSG_NEW) {
            NewOrder o;
            std::memcpy(&o, buf, sizeof(o));
            judge(*c, o.trigger_seq, t_rx);
            if (o.client_ts && t_rx > o.client_ts) c->oe_oneway.add(t_rx - o.client_ts);
            ++c->orders;
            me::Qty rem = 0;
            const auto r = book_.submit(me::client_key(c->id, o.client_order_id), c->id, o.client_order_id, o.side,
                                        o.price, o.qty, o.tif == TIF_IOC, o.post_only != 0, &rem);
            if (olog_) std::fprintf(olog_, "%llu,%u,new,%u,%u,%u,%u,%u,%d,%u,%u,%u,%u\n", (unsigned long long)t_rx, c->id,
                                    o.client_order_id, o.side, o.price, o.qty, o.trigger_seq, int(r), last_.bid, last_.bid_qty,
                                    last_.ask, last_.ask_qty);
            switch (r) {
                case decltype(book_)::RESTED: reply(c->oe, MSG_ACK, o.client_order_id, o.price, 0, rem, 0); break;
                case decltype(book_)::FILLED: break;   // fills already reported
                case decltype(book_)::IOC_DONE: reply(c->oe, MSG_CANCELLED, o.client_order_id, o.price, 0, 0, 0); break;
                case decltype(book_)::REJECT_CROSS: ++c->rejects; reply(c->oe, MSG_REJECT, o.client_order_id, o.price, 0, 0, REJ_POST_ONLY_CROSS); break;
                case decltype(book_)::REJECT_DUP: ++c->rejects; reply(c->oe, MSG_REJECT, o.client_order_id, o.price, 0, 0, REJ_DUPLICATE_ID); break;
                default: ++c->rejects; reply(c->oe, MSG_REJECT, o.client_order_id, o.price, 0, 0, REJ_BAD_ORDER);
            }
            publish_if_changed(MSG_TOB_CONTESTANT);
        } else if (type == MSG_CANCEL) {
            Cancel x;
            std::memcpy(&x, buf, sizeof(x));
            judge(*c, x.trigger_seq, t_rx);
            ++c->cancels;
            const me::Qty q = book_.cancel(me::client_key(c->id, x.client_order_id));
            if (olog_) std::fprintf(olog_, "%llu,%u,cancel,%u,0,0,%u,%u,0,%u,%u,%u,%u\n", (unsigned long long)t_rx, c->id,
                                    x.client_order_id, q, x.trigger_seq, last_.bid, last_.bid_qty, last_.ask, last_.ask_qty);
            if (q) reply(c->oe, MSG_CANCELLED, x.client_order_id, 0, 0, 0, 0);
            else reply(c->oe, MSG_REJECT, x.client_order_id, 0, 0, 0, REJ_UNKNOWN_ORDER);
            publish_if_changed(MSG_TOB_CONTESTANT);
        }
    }

    // Latency and race bookkeeping for the first message a contestant sends in reaction to a Tob.
    void judge(Contestant& c, uint32_t s, uint64_t t_rx) {
        if (s == 0 || s > seq_ || seq_ - s >= RING) return;
        SeqInfo& si = seqs_[s % RING];
        if (si.seq != s) return;
        const uint32_t bit = 1u << c.id;
        if (si.mask & bit) return;                 // only the first reaction counts
        const uint64_t sent = c.md_sent_ts[s % RING];
        if (sent && t_rx > sent) { c.lat.add(t_rx - sent); c.lat_window.add(t_rx - sent); }
        if (si.mask == 0) { si.first = c.id; }
        else {
            if (!si.raced) { auto& f = contestants_[si.first - 1]; ++f.races; ++f.wins; si.raced = true; }
            ++c.races;
        }
        si.mask |= bit;
    }

    void hello(const Hello* h, const sockaddr_in& from) {
        Contestant* c = by_addr(from);
        if (!c) {
            if (int(contestants_.size()) >= args_.max_contestants) { reply(from, MSG_REJECT, 0, 0, 0, 0, REJ_NOT_REGISTERED); return; }
            contestants_.emplace_back();
            c = &contestants_.back();
            c->id = uint32_t(contestants_.size());
            c->oe = from;
            c->name.assign(h->name, strnlen(h->name, sizeof(h->name)));
            c->md = from;
            c->md.sin_port = htons(h->md_port);
            std::printf("exchange: contestant %u '%s' registered (md port %u)\n", c->id, c->name.c_str(), h->md_port);
            std::fflush(stdout);
        }
        reply(c->oe, MSG_HELLO_ACK, 0, 0, 0, 0, c->id);
        send_tob(*c, last_, false);             // current book so it can start quoting
    }

    Contestant* by_addr(const sockaddr_in& a) {
        for (auto& c : contestants_)
            if (c.oe.sin_addr.s_addr == a.sin_addr.s_addr && c.oe.sin_port == a.sin_port) return &c;
        return nullptr;
    }

    void fill(uint32_t owner, uint32_t cid, uint8_t side, me::Px px, me::Qty q, me::Qty leaves, bool first) {
        Contestant& c = contestants_[owner - 1];
        ++c.fills;
        c.first_fills += first;
        pending_.push_back({hist_now_ + MARKOUT_NS, owner, int8_t(side == me::BUY ? 1 : -1), px, q});
        c.fill_qty += q;
        const int64_t sgn = side == me::BUY ? 1 : -1;
        c.position += sgn * int64_t(q);
        c.cash -= sgn * int64_t(q) * int64_t(px);
        reply(c.oe, MSG_FILL, cid, px, q, leaves, 0);
    }

    // Fill quality: where the mid is 1 s of market time after each fill, from the filled side's view.
    void settle_markouts() {
        while (!pending_.empty() && pending_.front().due_hist_ns <= hist_now_) {
            const PendingMarkout& m = pending_.front();
            if (last_.bid && last_.ask) {
                Contestant& c = contestants_[m.owner - 1];
                c.markout_x2 += int64_t(m.sgn) * (int64_t(last_.bid) + int64_t(last_.ask) - 2 * int64_t(m.px)) * int64_t(m.qty);
                c.markout_qty += m.qty;
            }
            pending_.pop_front();
        }
    }

    void reply(const sockaddr_in& to, uint16_t type, uint32_t cid, uint32_t px, uint32_t q, uint32_t leaves, uint32_t reason) {
        ExecReport r{type, uint16_t(MSG_SIZE), cid, px, q, leaves, reason, util::now_ns()};
        ::sendto(fd_, &r, sizeof(r), 0, reinterpret_cast<const sockaddr*>(&to), sizeof(to));
    }

    // type: MSG_TOB when historical flow changed the book, MSG_TOB_CONTESTANT when a contestant did
    void publish_if_changed(uint16_t type = MSG_TOB) {
        Tob t{type, uint16_t(MSG_SIZE), 0, 0, book_.best_bid(), uint32_t(book_.bid_qty()), book_.best_ask(),
              uint32_t(book_.ask_qty())};
        if (seq_ && t.bid == last_.bid && t.bid_qty == last_.bid_qty && t.ask == last_.ask && t.ask_qty == last_.ask_qty) return;
        t.seq = ++seq_;
        t.exch_ts = util::now_ns();
        last_ = t;
        SeqInfo& si = seqs_[t.seq % RING];
        si = SeqInfo{t.seq, 0, 0, false};
        // fairness: rotate which contestant gets each update first
        const size_t n = contestants_.size();
        for (size_t k = 0; k < n; ++k) send_tob(contestants_[(t.seq + k) % n], t, true);
    }

    void send_tob(Contestant& c, const Tob& t, bool record) {
        ::sendto(fd_, &t, sizeof(t), 0, reinterpret_cast<const sockaddr*>(&c.md), sizeof(c.md));
        if (record) c.md_sent_ts[t.seq % RING] = util::now_ns();
    }

    void scoreboard(double el, uint64_t lob_ts) {
        std::printf("[%6.1fs] replay %s  seq %u  open %zu", el, hms(lob_ts).c_str(), seq_, book_.open_orders());
        for (auto& c : contestants_) {
            const uint64_t fps = c.fills - c.fills_last;
            std::printf(" | %s: won %llu/%llu fills %llu (+%llu/s, first %llu) pos %+lld p50 %s p99 %s", c.name.c_str(),
                        (unsigned long long)c.wins, (unsigned long long)c.races, (unsigned long long)c.fills,
                        (unsigned long long)fps, (unsigned long long)c.first_fills, (long long)c.position, util::fmt_ns(double(c.lat.percentile(50))).c_str(),
                        util::fmt_ns(double(c.lat.percentile(99))).c_str());
        }
        std::printf("\n");
        std::fflush(stdout);
        if (stats_f_) {
            std::fprintf(stats_f_, "{\"t\": %.3f, \"replay_ts_ns\": %llu, \"seq\": %u, \"bid\": %u, \"ask\": %u, \"contestants\": [",
                         el, (unsigned long long)lob_ts, seq_, last_.bid, last_.ask);
            for (size_t i = 0; i < contestants_.size(); ++i) {
                auto& c = contestants_[i];
                std::fprintf(stats_f_, "%s{\"name\": \"%s\", \"races\": %llu, \"wins\": %llu, \"fills\": %llu, "
                             "\"fills_last_s\": %llu, \"first_fills\": %llu, \"fill_qty\": %llu, \"orders\": %llu, \"position\": %lld, "
                             "\"lat_p50_ns\": %llu, \"lat_p99_ns\": %llu, \"lat_window_p50_ns\": %llu, \"lat_window_p99_ns\": %llu}",
                             i ? ", " : "", c.name.c_str(), (unsigned long long)c.races, (unsigned long long)c.wins,
                             (unsigned long long)c.fills, (unsigned long long)(c.fills - c.fills_last),
                             (unsigned long long)c.first_fills, (unsigned long long)c.fill_qty, (unsigned long long)c.orders, (long long)c.position,
                             (unsigned long long)c.lat.percentile(50), (unsigned long long)c.lat.percentile(99),
                             (unsigned long long)c.lat_window.percentile(50), (unsigned long long)c.lat_window.percentile(99));
            }
            std::fprintf(stats_f_, "]}\n");
            std::fflush(stats_f_);
        }
        for (auto& c : contestants_) { c.fills_last = c.fills; c.lat_window.reset(); }
    }

    void summary(double el, size_t replayed) {
        std::string s = "{\"wall_s\": " + std::to_string(el) + ", \"history_messages\": " + std::to_string(replayed) +
                        ", \"tob_updates\": " + std::to_string(seq_) + ", \"contestants\": [";
        const double mid = (double(last_.bid) + double(last_.ask)) / 2.0;
        for (size_t i = 0; i < contestants_.size(); ++i) {
            auto& c = contestants_[i];
            char b[2048];
            std::snprintf(b, sizeof(b),
                          "%s{\"name\": \"%s\", \"races\": %llu, \"wins\": %llu, \"win_pct\": %.1f, \"fills\": %llu, "
                          "\"fills_per_s\": %.2f, \"fill_qty\": %llu, \"orders\": %llu, \"cancels\": %llu, \"rejects\": %llu, "
                          "\"position\": %lld, \"pnl_mark_to_mid_usd\": %.2f, \"reactions\": %llu, \"lat_p50_ns\": %llu, "
                          "\"lat_p99_ns\": %llu, \"lat_p999_ns\": %llu, \"lat_max_ns\": %llu, \"order_oneway_p50_ns\": %llu, "
                          "\"first_in_line_fills\": %llu, \"markout_1s_ticks_per_share\": %.3f, \"markout_1s_usd\": %.2f}",
                          i ? ", " : "", c.name.c_str(), (unsigned long long)c.races, (unsigned long long)c.wins,
                          c.races ? 100.0 * double(c.wins) / double(c.races) : 0.0, (unsigned long long)c.fills,
                          el > 0 ? double(c.fills) / el : 0.0, (unsigned long long)c.fill_qty, (unsigned long long)c.orders,
                          (unsigned long long)c.cancels, (unsigned long long)c.rejects, (long long)c.position,
                          (double(c.cash) + double(c.position) * mid) / 100.0, (unsigned long long)c.lat.count(),
                          (unsigned long long)c.lat.percentile(50), (unsigned long long)c.lat.percentile(99),
                          (unsigned long long)c.lat.percentile(99.9), (unsigned long long)c.lat.max(),
                          (unsigned long long)c.oe_oneway.percentile(50), (unsigned long long)c.first_fills,
                          c.markout_qty ? double(c.markout_x2) / 2.0 / double(c.markout_qty) : 0.0,
                          double(c.markout_x2) / 2.0 / 100.0);
            s += b;
        }
        s += "]}";
        std::printf("SUMMARY %s\n", s.c_str());
        if (!args_.summary.empty()) {
            FILE* f = std::fopen(args_.summary.c_str(), "w");
            if (f) { std::fprintf(f, "%s\n", s.c_str()); std::fclose(f); }
        }
    }

    Args args_;
    std::vector<lob::Msg> msgs_;
    std::vector<lob::Target> targets_;
    me::Book<Exchange> book_;
    Replayer<me::Book<Exchange>> rp_;
    int fd_ = -1;
    size_t start_idx_ = 1;
    std::vector<Contestant> contestants_;
    std::vector<SeqInfo> seqs_ = std::vector<SeqInfo>(RING);
    uint32_t seq_ = 0;
    Tob last_{};
    FILE* stats_f_ = nullptr;
    FILE* olog_ = nullptr;
    static constexpr uint64_t MARKOUT_NS = 1000000000ull;
    uint64_t hist_now_ = 0;
    std::deque<PendingMarkout> pending_;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    const Args a = parse_args(argc, argv);
    try {
        if (a.mode == "verify") return run_verify(a);
        if (a.mode == "bench") return run_bench(a);
        if (a.mode == "run") {
            auto ex = std::make_unique<Exchange>(a);   // large (per-contestant rings): keep off the stack
            return ex->run();
        }
        std::fprintf(stderr, "unknown mode %s\n", a.mode.c_str());
        return 2;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "exchange: %s\n", e.what());
        return 1;
    }
}
