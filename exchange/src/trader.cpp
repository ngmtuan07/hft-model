// Reference contestant: CPU market maker using the "first in line" rule from the pitch deck.
//
//   trader --name cpu --exchange 127.0.0.1:9000 [--delay-ns 0] [--qty 100] [--max-pos 500]
//          [--duration 70] [--keep-ticks 1]
//          [--core N] [--rt PRIO] [--mlock] [--busy-poll US] [--kernel-ts]     (latency tuning)
//          [--no-mm]   only race for stale quotes, no market making
//
// Stale-quote race (Flash 0x12): the moment one arrives, an IOC on the opposite side at the stale
// price is sent, before anything else in the same batch is processed.
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
// Hot path (one busy-polling thread, no allocation, no locks):
//   recv_batch(one socket: market data + exec reports) -> newest Tob -> decide -> ONE send_batch
// On Linux recv_batch/send_batch are recvmmsg/sendmmsg (cancel + new order in one syscall) and the
// order socket is connected (no per-send address lookup). Latency tuning switches:
//   --core N       pin to a CPU (pair with isolcpus/nohz_full, see tools/linux_tune.sh)
//   --rt PRIO      SCHED_FIFO real-time priority (needs CAP_SYS_NICE)
//   --mlock        lock memory so the hot path never page-faults
//   --busy-poll US SO_BUSY_POLL on both sockets (kernel spins on the NIC queue)
//   --kernel-ts    use the kernel's receive timestamp of the market data as "tick in", so the
//                  reported tick-to-trade includes the kernel-to-user time
//
// --delay-ns adds a busy-wait between reading a quote and sending orders, to emulate a slower
// contestant (and to validate that the exchange's race judging favours the faster one).
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <algorithm>
#include <cstring>
#include <string>

#include "net.hpp"
#include "proto.hpp"
#include "util.hpp"

using namespace proto;

namespace {

volatile std::sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

struct SideState { uint32_t coid = 0, px = 0, left = 0; bool live = false; };

struct Config {
    std::string name = "cpu", host = "127.0.0.1";
    uint16_t port = 9000;
    uint64_t delay_ns = 0;
    uint32_t qty = 100, keep_ticks = 1;
    int64_t max_pos = 500;
    double duration = 70;
    int core = -1, rt = 0, busy_poll = 0;
    bool mlock = false, kernel_ts = false, no_mm = false;
};

class Trader {
public:
    explicit Trader(const Config& c) : c_(c), ex_(util::make_addr(c.host, c.port)) {
        // ONE socket for market data, order entry and execution reports: one receive call per loop
        // iteration, so a market data packet never waits behind a poll of another socket.
        fd_ = util::udp_socket("0.0.0.0", 0, 4 << 20, c.busy_poll);
        connected_ = net::connect_udp(fd_, ex_);     // everything comes from the exchange's one address
        if (c.kernel_ts) {
            if (!net::enable_rx_timestamps(fd_)) std::fprintf(stderr, "%s: kernel timestamps unavailable\n", c.name.c_str());
        }
    }

    bool login(double timeout_s = 60.0) {
        Hello h{};
        h.type = MSG_HELLO;
        h.len = MSG_SIZE;
        h.md_port = util::bound_port(fd_);
        std::memcpy(h.name, c_.name.data(), std::min(c_.name.size(), sizeof(h.name)));   // NUL-padded, not terminated
        const uint64_t until = util::now_ns() + uint64_t(timeout_s * 1e9);
        uint64_t next_send = 0;
        while (util::now_ns() < until && !g_stop) {
            if (util::now_ns() >= next_send) {
                const void* p = &h;
                net::send_batch(fd_, &p, sizeof(h), 1, connected_ ? nullptr : &ex_);
                next_send = util::now_ns() + 200000000ull;
            }
            ExecReport r;
            if (::recv(fd_, &r, sizeof(r), MSG_DONTWAIT) == ssize_t(sizeof(r)) && r.type == MSG_HELLO_ACK) {
                id_ = r.reason;
                std::printf("%s: registered as contestant %u\n", c_.name.c_str(), id_);
                std::fflush(stdout);
                return true;
            }
        }
        return false;
    }

    void run() {
        const uint64_t end = util::now_ns() + uint64_t(c_.duration * 1e9);
        Tob latest{};
        while (!g_stop) {
            // one batch from the single socket: execution reports update our order state in arrival
            // order; the newest book is kept; act only if a market (0x10) event arrived
            const int n = rx_.recv_batch(fd_, buf_, net::MAX_BATCH);
            if (n > 0) {
                const uint64_t t_user = util::now_ns();
                uint64_t t_in = 0;
                bool act = false;
                for (int i = 0; i < n; ++i) {
                    if (buf_[i].len != ssize_t(MSG_SIZE)) continue;
                    uint16_t type;
                    std::memcpy(&type, buf_[i].buf, sizeof(type));
                    if (type == MSG_FLASH) {                // stale quote: fire first, think later
                        Flash f;
                        std::memcpy(&f, buf_[i].buf, sizeof(f));
                        snipe(f, buf_[i].kernel_ts ? buf_[i].kernel_ts : t_user);
                        continue;
                    }
                    if (type >= MSG_ACK) {
                        ExecReport r;
                        std::memcpy(&r, buf_[i].buf, sizeof(r));
                        on_report(r);
                        continue;
                    }
                    Tob t;
                    std::memcpy(&t, buf_[i].buf, sizeof(t));
                    if ((t.type != MSG_TOB && t.type != MSG_TOB_CONTESTANT) || t.seq < latest.seq) continue;
                    latest = t;
                    if (t.type == MSG_TOB) {
                        act = true;
                        t_in = buf_[i].kernel_ts ? buf_[i].kernel_ts : t_user;
                        const int64_t ow = int64_t(t_user) - int64_t(t.exch_ts);
                        if (ow > 0 && ow < 10000000000ll) md_oneway_.add(uint64_t(ow));   // only if clocks agree
                    }
                }
                if (act && !c_.no_mm) {
                    ++md_count_;
                    if (on_tob(latest)) {
                        // tick-to-trade inside this process: market data in -> orders handed to the kernel
                        t2t_.add(t_out_ - t_in);
                        if (t_in != t_user) k2u_.add(t_user - t_in);
                        send_cost_.add(t_sent_ - t_out_);
                    }
                }
            }
            if (((++loops_) & 0xFFF) == 0 && util::now_ns() >= end) break;
        }
        report();
    }

private:
    void report() const {
        auto f = [](const util::Histogram& h, double p) { return util::fmt_ns(double(h.percentile(p))); };
        std::printf("%s: md %llu, orders %llu, cancels %llu, fills %llu, position %+lld\n", c_.name.c_str(),
                    (unsigned long long)md_count_, (unsigned long long)orders_, (unsigned long long)cancels_,
                    (unsigned long long)fills_, (long long)position_);
        std::printf("%s: tick-to-trade in process (%s -> orders handed to kernel): p50 %s p99 %s p99.9 %s max %s (n=%llu)\n",
                    c_.name.c_str(), k2u_.count() ? "kernel rx timestamp" : "user rx", f(t2t_, 50).c_str(), f(t2t_, 99).c_str(),
                    f(t2t_, 99.9).c_str(), util::fmt_ns(double(t2t_.max())).c_str(), (unsigned long long)t2t_.count());
        if (k2u_.count())
            std::printf("%s: kernel rx -> user p50 %s p99 %s\n", c_.name.c_str(), f(k2u_, 50).c_str(), f(k2u_, 99).c_str());
        if (snipes_)
            std::printf("%s: stale quotes answered %llu, tick-to-trade p50 %s p99 %s p99.9 %s\n", c_.name.c_str(),
                        (unsigned long long)snipes_, f(snipe_t2t_, 50).c_str(), f(snipe_t2t_, 99).c_str(), f(snipe_t2t_, 99.9).c_str());
        std::printf("%s: send syscall p50 %s p99 %s; market data one-way p50 %s p99 %s (same host, same clock only)\n",
                    c_.name.c_str(), f(send_cost_, 50).c_str(), f(send_cost_, 99).c_str(), f(md_oneway_, 50).c_str(),
                    f(md_oneway_, 99).c_str());
    }

    // One IOC at the stale price, opposite side. Inventory limits deliberately do not apply: the race is
    // the point, and a stale quote is worth taking from either side.
    void snipe(const Flash& f, uint64_t t_in) {
        const uint32_t coid = ++next_coid_;
        const uint8_t side = f.side == SIDE_SELL ? SIDE_BUY : SIDE_SELL;
        side_of_[coid & 4095] = side;
        NewOrder o{};
        o.type = MSG_NEW; o.len = MSG_SIZE; o.client_order_id = coid; o.trigger_seq = f.seq;
        o.price = f.price; o.qty = f.qty; o.side = side; o.tif = TIF_IOC; o.post_only = 0;
        util::spin_ns(c_.delay_ns);
        o.client_ts = util::now_ns();
        const void* p = &o;
        net::send_batch(fd_, &p, MSG_SIZE, 1, connected_ ? nullptr : &ex_);
        snipe_t2t_.add(o.client_ts - t_in);
        ++snipes_;
    }

    // Returns true if orders were sent for this update.
    bool on_tob(const Tob& t) {
        if (t.bid == NO_PRICE || t.ask == NO_PRICE || t.ask <= t.bid) return false;
        const uint32_t spread = t.ask - t.bid;
        uint32_t db = t.bid, da = t.ask;
        if (spread >= 3) { db = t.bid + 1; da = t.ask - 1; }
        else if (spread == 2) {
            const bool buy_side = position_ < 0 || (position_ == 0 && t.bid_qty >= t.ask_qty);
            if (buy_side) db = t.bid + 1; else da = t.ask - 1;
        }
        if (position_ + int64_t(c_.qty) > c_.max_pos) db = 0;
        if (position_ - int64_t(c_.qty) < -c_.max_pos) da = 0;
        // decide first, then the (optionally delayed) send, so the delay models a slow path
        n_out_ = 0;
        plan(bid_, SIDE_BUY, db, t.bid, t.seq);
        plan(ask_, SIDE_SELL, da, t.ask, t.seq);
        if (!n_out_) return false;
        util::spin_ns(c_.delay_ns);
        t_out_ = util::now_ns();
        const void* ptrs[4];
        for (int i = 0; i < n_out_; ++i) {
            std::memcpy(out_[i] + 24, &t_out_, sizeof(t_out_));   // client_ts lives at offset 24 in both
            ptrs[i] = out_[i];
        }
        net::send_batch(fd_, ptrs, MSG_SIZE, n_out_, connected_ ? nullptr : &ex_);
        t_sent_ = util::now_ns();
        return true;
    }

    void plan(SideState& s, uint8_t side, uint32_t desired, uint32_t best, uint32_t seq) {
        const bool more_aggressive = side == SIDE_BUY ? desired > s.px : desired < s.px;
        if (s.live) {
            if (desired && s.px == desired) return;                       // already where we want
            if (desired && s.px == best && more_aggressive) return;       // at the touch: keep priority
            // someone improved just ahead of us: hold our place a level back instead of
            // re-pennying them (stops penny wars between contestants on wide-spread names)
            const uint32_t behind = side == SIDE_BUY ? (best > s.px ? best - s.px : 0) : (s.px > best ? s.px - best : 0);
            if (desired && behind > 0 && behind <= c_.keep_ticks && more_aggressive) return;
            Cancel x{};
            x.type = MSG_CANCEL; x.len = MSG_SIZE; x.client_order_id = s.coid; x.trigger_seq = seq;
            std::memcpy(out_[n_out_++], &x, MSG_SIZE);
            ++cancels_;
            s.live = false;
        }
        const bool improves = side == SIDE_BUY ? desired > best : (desired && desired < best);
        if (desired && improves) {                                        // a new price: we can be first
            s = SideState{++next_coid_, desired, c_.qty, true};
            side_of_[s.coid & 4095] = side;
            NewOrder o{};
            o.type = MSG_NEW; o.len = MSG_SIZE; o.client_order_id = s.coid; o.trigger_seq = seq;
            o.price = desired; o.qty = c_.qty; o.side = side; o.tif = TIF_GTC; o.post_only = 1;
            std::memcpy(out_[n_out_++], &o, MSG_SIZE);
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

    Config c_;
    sockaddr_in ex_;
    int fd_ = -1;
    bool connected_ = false;
    net::Receiver rx_;
    net::Rx buf_[net::MAX_BATCH];
    alignas(64) unsigned char out_[4][MSG_SIZE];
    int n_out_ = 0;
    uint64_t t_out_ = 0, t_sent_ = 0, loops_ = 0;
    uint32_t id_ = 0, next_coid_ = 0;
    SideState bid_, ask_;
    uint8_t side_of_[4096] = {};
    int64_t position_ = 0;
    uint64_t md_count_ = 0, orders_ = 0, cancels_ = 0, fills_ = 0, snipes_ = 0;
    util::Histogram t2t_, k2u_, send_cost_, md_oneway_, snipe_t2t_;
};

static_assert(offsetof(NewOrder, client_ts) == 24 && offsetof(Cancel, client_ts) == 24, "client_ts offset");

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    Config c;
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        auto v = [&]() -> std::string { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", k.c_str()); std::exit(2); } return argv[++i]; };
        if (k == "--name") c.name = v();
        else if (k == "--exchange") { const std::string s = v(); const auto p = s.find(':'); c.host = s.substr(0, p); c.port = uint16_t(std::atoi(s.c_str() + p + 1)); }
        else if (k == "--delay-ns") c.delay_ns = std::strtoull(v().c_str(), nullptr, 10);
        else if (k == "--qty") c.qty = uint32_t(std::atoi(v().c_str()));
        else if (k == "--max-pos") c.max_pos = std::atoll(v().c_str());
        else if (k == "--duration") c.duration = std::atof(v().c_str());
        else if (k == "--core") c.core = std::atoi(v().c_str());
        else if (k == "--keep-ticks") c.keep_ticks = uint32_t(std::atoi(v().c_str()));
        else if (k == "--rt") c.rt = std::atoi(v().c_str());
        else if (k == "--mlock") c.mlock = true;
        else if (k == "--busy-poll") c.busy_poll = std::atoi(v().c_str());
        else if (k == "--kernel-ts") c.kernel_ts = true;
        else if (k == "--no-mm") c.no_mm = true;
        else { std::fprintf(stderr, "unknown option %s\n", k.c_str()); return 2; }
    }
    try {
        if (c.kernel_ts) util::use_realtime_clock(true);
        if (c.core >= 0) std::printf("%s: %s\n", c.name.c_str(), util::pin_to_core(c.core) ? ("pinned to core " + std::to_string(c.core)).c_str() : "core pinning unavailable (Linux only)");
        const std::string rt = util::realtime_setup(c.rt, c.mlock);
        if (!rt.empty()) std::printf("%s: %s\n", c.name.c_str(), rt.c_str());
        Trader t(c);
        if (!t.login()) { std::fprintf(stderr, "%s: no answer from exchange\n", c.name.c_str()); return 1; }
        t.run();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "trader: %s\n", e.what());
        return 1;
    }
    return 0;
}
