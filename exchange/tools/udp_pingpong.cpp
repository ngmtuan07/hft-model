// UDP ping-pong: the network floor under every race. Measures round-trip time of 32-byte datagrams
// between two busy-polling endpoints, with the same socket options the exchange and trader use.
//
//   one machine (fork):   udp_pingpong --local [--n 200000] [--core 2 --peer-core 3]
//   two machines:         udp_pingpong --server 9100                       (on host B)
//                         udp_pingpong --client 10.0.0.2:9100 [--n 200000] (on host A)
//   common options:       --busy-poll US  --rt PRIO  --mlock  --kernel-ts
//
// Reports RTT p50/p99/p99.9/max; one-way latency is roughly RTT/2 on a symmetric path. With
// --kernel-ts also reports kernel-receive -> user-space time on the client.
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "net.hpp"
#include "util.hpp"

namespace {

struct Opt {
    std::string mode = "local", client;
    int port = 9100, n = 200000, warmup = 2000, busy_poll = 0, rt = 0, core = -1, peer_core = -1;
    bool mlock = false, kernel_ts = false;
};

void setup(const Opt& o, int core, const char* who) {
    if (core >= 0 && util::pin_to_core(core)) std::printf("%s: pinned to core %d\n", who, core);
    const std::string r = util::realtime_setup(o.rt, o.mlock);
    if (!r.empty()) std::printf("%s: %s\n", who, r.c_str());
}

// Echo every datagram back to its sender until `count` datagrams were echoed (0 = forever).
void echo(int fd, long count) {
    net::Receiver rx;
    net::Rx b[net::MAX_BATCH];
    long done = 0;
    while (count == 0 || done < count) {
        const int n = rx.recv_batch(fd, b, net::MAX_BATCH);
        for (int i = 0; i < n; ++i) {
            const void* p = b[i].buf;
            net::send_batch(fd, &p, size_t(b[i].len), 1, &b[i].from);
            ++done;
        }
    }
}

void ping(int fd, const sockaddr_in& to, const Opt& o) {
    if (o.kernel_ts) net::enable_rx_timestamps(fd);
    net::Receiver rx;
    net::Rx b[net::MAX_BATCH];
    unsigned char msg[32] = {};
    util::Histogram rtt, k2u;
    for (int i = 0; i < o.warmup + o.n; ++i) {
        const uint64_t t0 = util::now_ns();
        std::memcpy(msg, &i, sizeof(i));
        const void* p = msg;
        net::send_batch(fd, &p, sizeof(msg), 1, &to);
        int got = 0;
        while (!got) got = rx.recv_batch(fd, b, 1);
        const uint64_t t1 = util::now_ns();
        if (i >= o.warmup) {
            rtt.add(t1 - t0);
            if (b[0].kernel_ts && t1 > b[0].kernel_ts) k2u.add(t1 - b[0].kernel_ts);
        }
    }
    auto f = [](const util::Histogram& h, double q) { return util::fmt_ns(double(h.percentile(q))); };
    std::printf("RTT over %d round trips: p50 %s  p99 %s  p99.9 %s  max %s   (one-way ~ p50/2 = %s)\n", o.n,
                f(rtt, 50).c_str(), f(rtt, 99).c_str(), f(rtt, 99.9).c_str(), util::fmt_ns(double(rtt.max())).c_str(),
                util::fmt_ns(double(rtt.percentile(50)) / 2).c_str());
    if (k2u.count()) std::printf("kernel rx -> user: p50 %s  p99 %s\n", f(k2u, 50).c_str(), f(k2u, 99).c_str());
}

}  // namespace

int main(int argc, char** argv) {
    Opt o;
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        auto v = [&]() -> std::string { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", k.c_str()); std::exit(2); } return argv[++i]; };
        if (k == "--local") o.mode = "local";
        else if (k == "--server") { o.mode = "server"; o.port = std::atoi(v().c_str()); }
        else if (k == "--client") { o.mode = "client"; o.client = v(); }
        else if (k == "--n") o.n = std::atoi(v().c_str());
        else if (k == "--busy-poll") o.busy_poll = std::atoi(v().c_str());
        else if (k == "--rt") o.rt = std::atoi(v().c_str());
        else if (k == "--mlock") o.mlock = true;
        else if (k == "--kernel-ts") o.kernel_ts = true;
        else if (k == "--core") o.core = std::atoi(v().c_str());
        else if (k == "--peer-core") o.peer_core = std::atoi(v().c_str());
        else { std::fprintf(stderr, "unknown option %s\n", k.c_str()); return 2; }
    }
    if (o.kernel_ts) util::use_realtime_clock(true);
    try {
        if (o.mode == "server") {
            setup(o, o.core, "server");
            const int fd = util::udp_socket("0.0.0.0", uint16_t(o.port), 4 << 20, o.busy_poll);
            std::printf("server: echoing on port %d\n", o.port);
            std::fflush(stdout);
            echo(fd, 0);
        } else if (o.mode == "client") {
            setup(o, o.core, "client");
            const auto c = o.client.find(':');
            const sockaddr_in to = util::make_addr(o.client.substr(0, c), uint16_t(std::atoi(o.client.c_str() + c + 1)));
            const int fd = util::udp_socket("0.0.0.0", 0, 4 << 20, o.busy_poll);
            ping(fd, to, o);
        } else {
            const int a = util::udp_socket("127.0.0.1", 0, 4 << 20, o.busy_poll);
            const int b = util::udp_socket("127.0.0.1", 0, 4 << 20, o.busy_poll);
            const sockaddr_in bto = util::make_addr("127.0.0.1", util::bound_port(b));
            std::fflush(stdout);
            const pid_t pid = fork();
            if (pid == 0) { setup(o, o.peer_core, "echo"); echo(b, o.warmup + o.n); return 0; }
            setup(o, o.core, "ping");
            ping(a, bto, o);
            waitpid(pid, nullptr, 0);
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "udp_pingpong: %s\n", e.what());
        return 1;
    }
    return 0;
}
