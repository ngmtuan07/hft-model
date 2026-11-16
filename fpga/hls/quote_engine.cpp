#include "quote_engine.hpp"

static inline int32_t qbin(int32_t q, int32_t q_recip) {
#pragma HLS INLINE
    // x = clip((min(q, QTY_MAX) * Q_RECIP + 2^23) >> 24, 1, NQ); no divider needed
    qprod_t qc = (qprod_t)(uint32_t)(q > QE_QTY_MAX ? QE_QTY_MAX : (q < 0 ? 0 : q));
    qprod_t prod = qc * (qprod_t)(uint32_t)q_recip + ((qprod_t)1 << (QE_RECIP_SHIFT - 1));
    qprod_t x = prod >> QE_RECIP_SHIFT;
    if (x < 1) x = 1;
    if (x > QE_NQ) x = QE_NQ;
    return (int32_t)x;
}

Quote quote_core(int32_t bb, int32_t ba, int32_t bq, int32_t aq, int32_t inv, const QuoteParams& p,
                 const int32_t lut[1 << (2 * QE_LUT_BITS)]) {
#pragma HLS INLINE
    // reservation price r = mid - inv*C_INV + skew   (Q16; mid = (bb+ba)/2)
    acc_t r = ((acc_t)bb + (acc_t)ba) << (QE_FRAC - 1);
    r -= (acc_t)inv * (acc_t)p.c_inv;
    int32_t gate = 3;  // bit0 = bid allowed, bit1 = ask allowed
    if (ba - bb == 1) {
        const int32_t x = qbin(bq, p.q_recip);
        const int32_t y = qbin(aq, p.q_recip);
        const int32_t e = lut[(x << QE_LUT_BITS) | y];  // {skew Q16, gate[1:0]}
        r += (acc_t)(e >> 2);
        gate = e & 3;
    }
    // floor / ceil by arithmetic shift
    acc_t lo = (r - (acc_t)p.c_half) >> QE_FRAC;
    acc_t hi = -((-(r + (acc_t)p.c_half)) >> QE_FRAC);
    int32_t bid = (int32_t)lo;
    int32_t ask = (int32_t)hi;
    if (bid > ba - 1) bid = ba - 1;  // post-only
    if (ask < bb + 1) ask = bb + 1;
    if (!(gate & 1)) bid = -1;       // imbalance gate
    if (!(gate & 2)) ask = -1;
    if (inv >= p.max_pos) bid = -1;  // inventory gating
    if (inv <= -p.max_pos) ask = -1;
    return Quote{bid, ask};
}

void quote_engine(hls::stream<pkt_in_t>& s_in, hls::stream<pkt_out_t>& s_out) {
#pragma HLS INTERFACE mode=axis port=s_in
#pragma HLS INTERFACE mode=axis port=s_out
#pragma HLS INTERFACE mode=ap_ctrl_none port=return
#pragma HLS PIPELINE II=1
    // State lives in the engine: parameter registers and the CST skew LUT (one BRAM).
    static QuoteParams params = {0, 0, 0, 0};
    static int32_t lut[1 << (2 * QE_LUT_BITS)];
#pragma HLS BIND_STORAGE variable=lut type=ram_s2p impl=bram
    // LUT writes happen in the config phase, before book updates; no read-after-write
    // hazard between consecutive packets needs to be honoured.
#pragma HLS DEPENDENCE variable=lut type=inter false

    pkt_in_t in = s_in.read();
    const int32_t hdr = get_w(in.data, 0);
    const int32_t type = hdr & 0xFF;
    const int32_t w1 = get_w(in.data, 1), w2 = get_w(in.data, 2), w3 = get_w(in.data, 3),
                  w4 = get_w(in.data, 4), w5 = get_w(in.data, 5);

    int32_t out_bid = 0, out_ask = 0;
    if (type == QE_TYPE_PARAM) {
        params.c_inv = w1;
        params.c_half = w2;
        params.q_recip = w3;
        params.max_pos = w4;
    } else if (type == QE_TYPE_LUT) {
        lut[w1 & ((1 << (2 * QE_LUT_BITS)) - 1)] = w2;
    } else {
        Quote q = quote_core(w1, w2, w3, w4, w5, params, lut);
        out_bid = q.bid;
        out_ask = q.ask;
    }

    pkt_out_t out;
    set_w(out.data, 0, hdr);
    set_w(out.data, 1, out_bid);
    set_w(out.data, 2, out_ask);
    set_w(out.data, 3, 0);
    out.keep = -1;
    out.strb = -1;
    out.last = in.last;
    s_out.write(out);
}
