// Batched UDP I/O with Linux fast paths and portable fallbacks.
//
//   recv_batch  Linux: one recvmmsg() returns up to N datagrams (fewer syscalls on bursts).
//               Elsewhere: recvmsg() in a loop.
//   send_batch  Linux: one sendmmsg() sends several datagrams (e.g. cancel + new order in one
//               syscall). Elsewhere: send()/sendto() in a loop.
//   kernel receive timestamps (enable_rx_timestamps): the kernel stamps each datagram when the
//               network stack receives it (Linux SO_TIMESTAMPNS, ns; macOS SO_TIMESTAMP, us), in
//               CLOCK_REALTIME. Turn on util::use_realtime_clock(true) so the rest of the program
//               measures on the same clock. This takes our own polling delay out of "arrival time".
//   connect     a connected UDP socket skips the per-send route/address lookup.
#pragma once
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/uio.h>

#include <cerrno>
#include <cstdint>
#include <cstring>

namespace net {

constexpr int MAX_BATCH = 64;
constexpr size_t MAX_DGRAM = 64;      // protocol messages are 32 bytes; anything bigger is dropped

struct Rx {
    alignas(8) unsigned char buf[MAX_DGRAM];
    ssize_t len;
    sockaddr_in from;
    uint64_t kernel_ts;               // 0 if not available
};

inline bool enable_rx_timestamps(int fd) {
    const int one = 1;
#if defined(SO_TIMESTAMPNS)
    return setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPNS, &one, sizeof(one)) == 0;
#elif defined(SO_TIMESTAMP)
    return setsockopt(fd, SOL_SOCKET, SO_TIMESTAMP, &one, sizeof(one)) == 0;
#else
    (void)fd;
    (void)one;
    return false;
#endif
}

inline uint64_t parse_rx_timestamp(msghdr* mh) {
    for (cmsghdr* c = CMSG_FIRSTHDR(mh); c; c = CMSG_NXTHDR(mh, c)) {
        if (c->cmsg_level != SOL_SOCKET) continue;
#if defined(SO_TIMESTAMPNS)
        if (c->cmsg_type == SO_TIMESTAMPNS) {
            timespec ts;
            std::memcpy(&ts, CMSG_DATA(c), sizeof(ts));
            return uint64_t(ts.tv_sec) * 1000000000ull + uint64_t(ts.tv_nsec);
        }
#endif
#if defined(SCM_TIMESTAMP)
        if (c->cmsg_type == SCM_TIMESTAMP) {
            timeval tv;
            std::memcpy(&tv, CMSG_DATA(c), sizeof(tv));
            return uint64_t(tv.tv_sec) * 1000000000ull + uint64_t(tv.tv_usec) * 1000ull;
        }
#endif
    }
    return 0;
}

// Receives up to `max` datagrams without blocking. Returns how many were received.
class Receiver {
public:
    int recv_batch(int fd, Rx* out, int max) {
        if (max > MAX_BATCH) max = MAX_BATCH;
#if defined(__linux__)
        for (int i = 0; i < max; ++i) {
            iov_[i] = {out[i].buf, sizeof(out[i].buf)};
            mm_[i].msg_hdr = msghdr{};
            mm_[i].msg_hdr.msg_name = &out[i].from;
            mm_[i].msg_hdr.msg_namelen = sizeof(out[i].from);
            mm_[i].msg_hdr.msg_iov = &iov_[i];
            mm_[i].msg_hdr.msg_iovlen = 1;
            mm_[i].msg_hdr.msg_control = ctrl_[i];
            mm_[i].msg_hdr.msg_controllen = sizeof(ctrl_[i]);
        }
        const int n = recvmmsg(fd, mm_, unsigned(max), MSG_DONTWAIT, nullptr);
        if (n <= 0) return 0;
        for (int i = 0; i < n; ++i) {
            out[i].len = ssize_t(mm_[i].msg_len);
            out[i].kernel_ts = parse_rx_timestamp(&mm_[i].msg_hdr);
        }
        return n;
#else
        int n = 0;
        while (n < max) {
            iovec iov{out[n].buf, sizeof(out[n].buf)};
            msghdr mh{};
            mh.msg_name = &out[n].from;
            mh.msg_namelen = sizeof(out[n].from);
            mh.msg_iov = &iov;
            mh.msg_iovlen = 1;
            mh.msg_control = ctrl_[0];
            mh.msg_controllen = sizeof(ctrl_[0]);
            const ssize_t r = recvmsg(fd, &mh, MSG_DONTWAIT);
            if (r < 0) break;
            out[n].len = r;
            out[n].kernel_ts = parse_rx_timestamp(&mh);
            ++n;
        }
        return n;
#endif
    }

private:
    alignas(8) unsigned char ctrl_[MAX_BATCH][64];
#if defined(__linux__)
    mmsghdr mm_[MAX_BATCH];
    iovec iov_[MAX_BATCH];
#endif
};

// Sends `n` datagrams of `len` bytes each, in order. `to` = nullptr on a connected socket.
inline int send_batch(int fd, const void* const* bufs, size_t len, int n, const sockaddr_in* to = nullptr) {
#if defined(__linux__)
    mmsghdr mm[8];
    iovec iov[8];
    if (n > 8) n = 8;
    for (int i = 0; i < n; ++i) {
        iov[i] = {const_cast<void*>(bufs[i]), len};
        mm[i].msg_hdr = msghdr{};
        mm[i].msg_hdr.msg_iov = &iov[i];
        mm[i].msg_hdr.msg_iovlen = 1;
        if (to) {
            mm[i].msg_hdr.msg_name = const_cast<sockaddr_in*>(to);
            mm[i].msg_hdr.msg_namelen = sizeof(*to);
        }
    }
    return sendmmsg(fd, mm, unsigned(n), 0);
#else
    int sent = 0;
    for (int i = 0; i < n; ++i) {
        const ssize_t r = to ? ::sendto(fd, bufs[i], len, 0, reinterpret_cast<const sockaddr*>(to), sizeof(*to))
                             : ::send(fd, bufs[i], len, 0);
        if (r < 0) break;
        ++sent;
    }
    return sent;
#endif
}

inline bool connect_udp(int fd, const sockaddr_in& to) {
    return ::connect(fd, reinterpret_cast<const sockaddr*>(&to), sizeof(to)) == 0;
}

}  // namespace net
