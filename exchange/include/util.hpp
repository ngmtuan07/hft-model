// Clock, latency histogram, UDP helpers, CPU pinning.
#pragma once
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <stdexcept>
#include <string>

#ifdef __linux__
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#endif

namespace util {

// Clock for all latency bookkeeping. Default: monotonic. With kernel receive timestamps enabled
// (which the kernel stamps in CLOCK_REALTIME) every timestamp must come from the same clock, so
// use_realtime_clock() switches the whole process over.
inline bool& realtime_flag() { static bool f = false; return f; }
inline void use_realtime_clock(bool on) { realtime_flag() = on; }

inline uint64_t now_ns() {
    timespec ts;
    clock_gettime(realtime_flag() ? CLOCK_REALTIME : CLOCK_MONOTONIC, &ts);
    return uint64_t(ts.tv_sec) * 1000000000ull + uint64_t(ts.tv_nsec);
}

inline void spin_ns(uint64_t ns) {
    if (!ns) return;
    const uint64_t until = now_ns() + ns;
    while (now_ns() < until) {}
}

// Pin the calling thread to one core (Linux). On macOS there is no hard affinity API; no-op.
inline bool pin_to_core(int core) {
#ifdef __linux__
    if (core < 0) return false;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
#else
    (void)core;
    return false;
#endif
}

// Linux: SCHED_FIFO at `prio` (needs CAP_SYS_NICE) and lock all memory (needs memlock limit).
inline std::string realtime_setup(int prio, bool lock_memory) {
    std::string out;
#ifdef __linux__
    if (prio > 0) {
        sched_param sp{};
        sp.sched_priority = prio;
        out += sched_setscheduler(0, SCHED_FIFO, &sp) == 0 ? "SCHED_FIFO " + std::to_string(prio) : std::string("SCHED_FIFO failed (") + std::strerror(errno) + ")";
    }
    if (lock_memory) {
        if (!out.empty()) out += ", ";
        out += mlockall(MCL_CURRENT | MCL_FUTURE) == 0 ? "memory locked" : std::string("mlockall failed (") + std::strerror(errno) + ")";
    }
#else
    if (prio > 0 || lock_memory) out = "real-time scheduling / memory locking: Linux only, skipped";
#endif
    return out;
}

// Log-linear histogram (HDR-style): 2^5 sub-buckets per power of two, 1 ns .. ~68 s.
class Histogram {
public:
    static constexpr int SUB = 5;
    static constexpr int NB = (36 + 1) << SUB;
    void add(uint64_t v) {
        ++counts_[bucket(v)];
        ++n_;
        sum_ += v;
        if (v > max_) max_ = v;
        if (n_ == 1 || v < min_) min_ = v;
    }
    uint64_t count() const { return n_; }
    double mean() const { return n_ ? double(sum_) / double(n_) : 0.0; }
    uint64_t max() const { return max_; }
    uint64_t min() const { return min_; }
    uint64_t percentile(double p) const {    // upper edge of the bucket holding the p-th value
        if (!n_) return 0;
        const uint64_t target = uint64_t(std::ceil(p / 100.0 * double(n_)));
        uint64_t acc = 0;
        for (int b = 0; b < NB; ++b) {
            acc += counts_[b];
            if (acc >= target && counts_[b]) {
                const uint64_t edge = upper(b);
                return edge > max_ ? max_ : edge;
            }
        }
        return max_;
    }
    void merge(const Histogram& o) {
        for (int b = 0; b < NB; ++b) counts_[b] += o.counts_[b];
        if (o.n_) {
            if (!n_ || o.min_ < min_) min_ = o.min_;
            if (o.max_ > max_) max_ = o.max_;
        }
        n_ += o.n_;
        sum_ += o.sum_;
    }
    void reset() { *this = Histogram(); }

private:
    static int bucket(uint64_t v) {
        if (v < (1u << SUB)) return int(v);
        const int msb = 63 - __builtin_clzll(v);
        const int shift = msb - SUB;
        const int b = ((shift + 1) << SUB) + int((v >> shift) & ((1u << SUB) - 1));
        return b < NB ? b : NB - 1;
    }
    static uint64_t upper(int b) {
        if (b < (1 << SUB)) return uint64_t(b);
        const int shift = (b >> SUB) - 1;
        const uint64_t base = (uint64_t(1) << SUB) | uint64_t(b & ((1 << SUB) - 1));
        return ((base + 1) << shift) - 1;
    }
    uint64_t counts_[NB] = {};
    uint64_t n_ = 0, sum_ = 0, max_ = 0, min_ = 0;
};

inline sockaddr_in make_addr(const std::string& host, uint16_t port) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    if (inet_pton(AF_INET, host.c_str(), &a.sin_addr) != 1) throw std::runtime_error("bad address " + host);
    return a;
}

// Non-blocking UDP socket bound to host:port (port 0 = any).
inline int udp_socket(const std::string& host, uint16_t port, int rcvbuf = 4 << 20, int busy_poll_us = 50) {
    const int fd = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) throw std::runtime_error("socket()");
    const int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &rcvbuf, sizeof(rcvbuf));
#ifdef SO_BUSY_POLL
    // Linux: the kernel spins on the NIC queue for up to this many us on each receive. Values above
    // net.core.busy_read need CAP_NET_ADMIN; failure is harmless (falls back to interrupts).
    if (busy_poll_us > 0) setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &busy_poll_us, sizeof(busy_poll_us));
#else
    (void)busy_poll_us;
#endif
    sockaddr_in a = make_addr(host, port);
    if (::bind(fd, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0)
        throw std::runtime_error("bind " + host + ":" + std::to_string(port) + ": " + std::strerror(errno));
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    return fd;
}

inline uint16_t bound_port(int fd) {
    sockaddr_in a{};
    socklen_t n = sizeof(a);
    getsockname(fd, reinterpret_cast<sockaddr*>(&a), &n);
    return ntohs(a.sin_port);
}

inline std::string fmt_ns(double ns) {
    char b[32];
    if (ns < 1e3) std::snprintf(b, sizeof(b), "%.0fns", ns);
    else if (ns < 1e6) std::snprintf(b, sizeof(b), "%.1fus", ns / 1e3);
    else std::snprintf(b, sizeof(b), "%.2fms", ns / 1e6);
    return b;
}

}  // namespace util
