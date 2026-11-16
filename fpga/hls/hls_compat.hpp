// Lets the same source build in Vitis HLS (real ap_int / hls::stream / ap_axiu) and with a
// plain C++ compiler for fast local C-simulation (minimal drop-in shims below).
// The shims implement only what quote_engine uses, with identical semantics for the value
// ranges the golden model guarantees (see hftmodel/strategy.py: QTY_MAX, TICK_MAX).
#pragma once
#include <cstdint>

#if defined(__SYNTHESIS__) || defined(__VITIS_HLS__) || __has_include(<ap_int.h>)
#define QE_HAVE_VITIS 1
#include <ap_axi_sdata.h>
#include <ap_int.h>
#include <hls_stream.h>

typedef ap_axiu<256, 0, 0, 0> pkt_in_t;   // 8 x 32-bit words
typedef ap_axiu<128, 0, 0, 0> pkt_out_t;  // 4 x 32-bit words
typedef ap_int<48> acc_t;                 // reservation-price accumulator, Q16 ticks
typedef ap_uint<48> qprod_t;              // qty * Q_RECIP

template <int W>
static inline int32_t get_w(const ap_uint<W>& d, int i) {
#pragma HLS INLINE
    return (int32_t)(uint32_t)d.range(32 * i + 31, 32 * i);
}
template <int W>
static inline void set_w(ap_uint<W>& d, int i, int32_t v) {
#pragma HLS INLINE
    d.range(32 * i + 31, 32 * i) = (uint32_t)v;
}

#else
#define QE_HAVE_VITIS 0
#include <deque>
#include <stdexcept>

namespace hls {
template <typename T>
class stream {
    std::deque<T> q_;
  public:
    void write(const T& v) { q_.push_back(v); }
    T read() {
        if (q_.empty()) throw std::runtime_error("read from empty hls::stream");
        T v = q_.front();
        q_.pop_front();
        return v;
    }
    bool empty() const { return q_.empty(); }
    size_t size() const { return q_.size(); }
};
}  // namespace hls

template <int N>
struct words_t { uint32_t w[N] = {}; };
template <int NW>
struct axis_pkt_t {
    words_t<NW> data;
    uint64_t keep = ~0ull, strb = ~0ull;
    bool last = false;
};
typedef axis_pkt_t<8> pkt_in_t;
typedef axis_pkt_t<4> pkt_out_t;
typedef int64_t acc_t;
typedef uint64_t qprod_t;

template <int N>
static inline int32_t get_w(const words_t<N>& d, int i) { return (int32_t)d.w[i]; }
template <int N>
static inline void set_w(words_t<N>& d, int i, int32_t v) { d.w[i] = (uint32_t)v; }
#endif
