// Reference contestant: CPU market maker using the "first in line" rule from the pitch deck.
//
//   trader --name cpu --exchange 127.0.0.1:9000 [--delay-ns 0] [--qty 100] [--max-pos 500]
//          [--duration 70] [--core N] [--keep-ticks 1]
//
// It decides only on market-flow updates (Tob type 0x10) and acts on the newest book it has;
// updates caused by contestants (0x11) just refresh that book.
// Strategy (identical logic is what the FPGA contestant will implement):
//   spread >= 3 ticks: improve both sides by one tick (we are first at the new price)
//   spread == 2      : improve one side only: the one that reduces inventory, else the side the
//                      queue imbalance favours
//   spread == 1      : keep an order already at the touch, never join the back of a queue
// A resting order is kept while it is at the desired price, already at the best price, or at most
// --keep-ticks (default 1) behind a better price someone else just posted; otherwise it is
// cancelled. Inventory beyond +/- max-pos shares stops quoting that side.
//
// --delay-ns adds a busy-wait between reading a quote and sending orders, to emulate a slower
// contestant (and to validate that the exchange's race judging favours the faster one).
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "proto.hpp"
#include "util.hpp"

using namespace proto;

namespace {

volatile std::sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

struct SideState { uint32_t coid = 0, px = 0, left = 0; bool live = false; };

class Trader {
public:
    Trader(std::string name, const std::string& host, uint16_t port, uint64_t delay_ns, uint32_t qty, int64_t max_pos,
           uint32_t keep_ticks)
        : name_(std::move(name)), ex_(util::make_addr(host, port)), delay_ns_(delay_ns), qty_(qty), max_pos_(max_pos),
          keep_ticks_(keep_ticks) {
        oe_ = util::udp_socket("0.0.0.0", 0);
        md_ = util::udp_socket("0.0.0.0", 0);
    }

    bool login(double timeout_s = 60.0) {
        Hello h{};
        h.type = MSG_HELLO;
        h.len = MSG_SIZE;
        h.md_port = util::bound_port(md_);
        std::strncpy(h.name, name_.c_str(), sizeof(h.name));
        const uint64_t until = util::now_ns() + uint64_t(timeout_s * 1e9);
        uint64_t next_send = 0;
        while (util::now_ns() < until && !g_stop) {
            if (util::now_ns() >= next_send) { send(&h); next_send = util::now_ns() + 200000000ull; }
            ExecReport r;
            if (::recv(oe_, &r, sizeof(r), 0) == ssize_t(sizeof(r)) && r.type == MSG_HELLO_ACK) {
                id_ = r.reason;
                std::printf("%s: registered as contestant %u\n", name_.c_str(), id_);
                std::fflush(stdout);
                return true;
            }
        }
        return false;
    }

    void run(double duration_s) {
        const uint64_t end = util::now_ns() + uint64_t(duration_s * 1e9);
        Tob latest{};
        while (!g_stop && util::now_ns() < end) {
            // market data: drain, keep the newest book; act only if a market (0x10) event arrived
            bool have = false;
            Tob t;
            while (::recv(md_, &t, sizeof(t), 0) == ssize_t(sizeof(t))) {
                if ((t.type == MSG_TOB || t.type == MSG_TOB_CONTESTANT) && t.seq >= latest.seq) {
                    latest = t;
                    if (t.type == MSG_TOB) { have = true; md_oneway_.add(util::now_ns() - t.exch_ts); }
                }
            }
            if (have) {
                const uint64_t t0 = util::now_ns();
                on_tob(latest);
                if (sent_this_tob_) decide_.add(util::now_ns() - t0);
                ++md_count_;
            }
            ExecReport r;
            while (::recv(oe_, &r, sizeof(r), 0) == ssize_t(sizeof(r))) on_report(r);
        }
        std::printf("%s: md %llu, orders %llu, cancels %llu, fills %llu, position %+lld, "
                    "decide+send p50 %s p99 %s (own clock), feed one-way p50 %s p99 %s (same host only)\n",
                    name_.c_str(), (unsigned long long)md_count_, (unsigned long long)orders_,
                    (unsigned long long)cancels_, (unsigned long long)fills_, (long long)position_,
                    util::fmt_ns(double(decide_.percentile(50))).c_str(), util::fmt_ns(double(decide_.percentile(99))).c_str(),
                    util::fmt_ns(double(md_oneway_.percentile(50))).c_str(), util::fmt_ns(double(md_oneway_.percentile(99))).c_str());
    }

private:
    void on_tob(const Tob& t) {
        sent_this_tob_ = false;
        if (t.bid == NO_PRICE || t.ask == NO_PRICE || t.ask <= t.bid) return;
        const uint32_t spread = t.ask - t.bid;
        uint32_t db = t.bid, da = t.ask;
        if (spread >= 3) { db = t.bid + 1; da = t.ask - 1; }
        else if (spread == 2) {
            const bool buy_side = position_ < 0 || (position_ == 0 && t.bid_qty >= t.ask_qty);
            if (buy_side) db = t.bid + 1; else da = t.ask - 1;
        }
        if (position_ + int64_t(qty_) > max_pos_) db = 0;
        if (position_ - int64_t(qty_) < -max_pos_) da = 0;
        // decisions first, then the (optionally delayed) sends, so the delay models a slow path
        n_actions_ = 0;
        plan(bid_, SIDE_BUY, db, t.bid);
        plan(ask_, SIDE_SELL, da, t.ask);
        if (!n_actions_) return;
        util::spin_ns(delay_ns_);
        for (int i = 0; i < n_actions_; ++i) execute(actions_[i], t.seq);
        sent_this_tob_ = true;
    }

    struct Action { bool is_cancel; uint8_t side; uint32_t coid, px; };

    void plan(SideState& s, uint8_t side, uint32_t desired, uint32_t best) {
        const bool more_aggressive = side == SIDE_BUY ? desired > s.px : desired < s.px;
        if (s.live) {
            if (desired && s.px == desired) return;                       // already where we want
            if (desired && s.px == best && more_aggressive) return;       // at the touch: keep priority
            // someone improved just ahead of us: hold our place a level back instead of
            // re-pennying them (stops penny wars between contestants on wide-spread names)
            const uint32_t behind = side == SIDE_BUY ? (best > s.px ? best - s.px : 0) : (s.px > best ? s.px - best : 0);
            if (desired && behind > 0 && behind <= keep_ticks_ && more_aggressive) return;
            actions_[n_actions_++] = {true, side, s.coid, 0};
            s.live = false;
        }
        const bool improves = side == SIDE_BUY ? desired > best : (desired && desired < best);
        if (desired && improves) {                                        // a new price: we can be first
            s = SideState{++next_coid_, desired, qty_, true};
            side_of_[s.coid & 4095] = side;
            actions_[n_actions_++] = {false, side, s.coid, desired};
        }
    }

    void execute(const Action& a, uint32_t seq) {
        if (a.is_cancel) {
            Cancel c{};
            c.type = MSG_CANCEL; c.len = MSG_SIZE; c.client_order_id = a.coid; c.trigger_seq = seq;
            c.client_ts = util::now_ns();
            send(&c);
            ++cancels_;
        } else {
            NewOrder o{};
            o.type = MSG_NEW; o.len = MSG_SIZE; o.client_order_id = a.coid; o.trigger_seq = seq;
            o.price = a.px; o.qty = qty_; o.side = a.side; o.tif = TIF_GTC; o.post_only = 1;
            o.client_ts = util::now_ns();
            send(&o);
            ++orders_;
        }
    }

    void on_report(const ExecReport& r) {
        SideState& s = side_of_[r.client_order_id & 4095] == SIDE_BUY ? bid_ : ask_;
        const bool current = s.live && s.coid == r.client_order_id;
        switch (r.type) {
            case MSG_FILL:
                ++fills_;
                position_ += (side_of_[r.client_order_id & 4095] == SIDE_BUY ? 1 : -1) * int64_t(r.qty);
                if (current) { s.left = r.leaves; if (!r.leaves) s.live = false; }
                break;
            case MSG_REJECT:
            case MSG_CANCELLED:
                if (current) s.live = false;
                break;
            default: break;
        }
    }

    template <class M>
    void send(const M* m) {
        ::sendto(oe_, m, sizeof(M), 0, reinterpret_cast<const sockaddr*>(&ex_), sizeof(ex_));
    }

    std::string name_;
    sockaddr_in ex_;
    uint64_t delay_ns_;
    uint32_t qty_;
    int64_t max_pos_;
    uint32_t keep_ticks_;
    int oe_ = -1, md_ = -1;
    uint32_t id_ = 0, next_coid_ = 0;
    SideState bid_, ask_;
    uint8_t side_of_[4096] = {};
    Action actions_[4];
    int n_actions_ = 0;
    bool sent_this_tob_ = false;
    int64_t position_ = 0;
    uint64_t md_count_ = 0, orders_ = 0, cancels_ = 0, fills_ = 0;
    util::Histogram decide_, md_oneway_;
};

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::string name = "cpu", host = "127.0.0.1";
    uint16_t port = 9000;
    uint64_t delay = 0;
    uint32_t qty = 100;
    int64_t max_pos = 500;
    double duration = 70;
    int core = -1;
    uint32_t keep_ticks = 1;
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        auto v = [&]() -> std::string { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", k.c_str()); std::exit(2); } return argv[++i]; };
        if (k == "--name") name = v();
        else if (k == "--exchange") { const std::string s = v(); const auto c = s.find(':'); host = s.substr(0, c); port = uint16_t(std::atoi(s.c_str() + c + 1)); }
        else if (k == "--delay-ns") delay = std::strtoull(v().c_str(), nullptr, 10);
        else if (k == "--qty") qty = uint32_t(std::atoi(v().c_str()));
        else if (k == "--max-pos") max_pos = std::atoll(v().c_str());
        else if (k == "--duration") duration = std::atof(v().c_str());
        else if (k == "--core") core = std::atoi(v().c_str());
        else if (k == "--keep-ticks") keep_ticks = uint32_t(std::atoi(v().c_str()));
        else { std::fprintf(stderr, "unknown option %s\n", k.c_str()); return 2; }
    }
    try {
        util::pin_to_core(core);
        Trader t(name, host, port, delay, qty, max_pos, keep_ticks);
        if (!t.login()) { std::fprintf(stderr, "%s: no answer from exchange\n", name.c_str()); return 1; }
        t.run(duration);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "trader: %s\n", e.what());
        return 1;
    }
    return 0;
}
