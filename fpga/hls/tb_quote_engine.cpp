// C-simulation testbench: replays stream_in.hex through quote_engine and checks every output
// packet against stream_out.hex (written by scripts/export_fpga_vectors.py).
// Works in Vitis HLS csim/cosim and with a plain C++ compiler (make csim).
#include <chrono>
#include <cstdio>
#include <fstream>
#include <string>
#include <vector>

#include "quote_engine.hpp"

static std::vector<std::vector<int32_t>> read_hex(const std::string& path, int words) {
    std::vector<std::vector<int32_t>> rows;
    std::ifstream f(path);
    std::string line;
    while (std::getline(f, line)) {
        if ((int)line.size() < 8 * words) continue;
        std::vector<int32_t> w(words);
        for (int i = 0; i < words; ++i)  // most significant word first
            w[words - 1 - i] = (int32_t)(uint32_t)std::stoul(line.substr(8 * i, 8), nullptr, 16);
        rows.push_back(w);
    }
    return rows;
}

int main(int argc, char** argv) {
    std::string dir = argc > 1 ? argv[1] : "../../output/fpga_vectors";
    auto in = read_hex(dir + "/stream_in.hex", 8);
    auto exp = read_hex(dir + "/stream_out.hex", 4);
    if (in.empty() || in.size() != exp.size()) {
        std::fprintf(stderr, "bad or missing vectors in %s (%zu in, %zu out)\n", dir.c_str(), in.size(), exp.size());
        return 2;
    }

    hls::stream<pkt_in_t> s_in;
    hls::stream<pkt_out_t> s_out;
    size_t bad = 0, books = 0;
    std::vector<int32_t> lut_copy(1 << (2 * QE_LUT_BITS), 0);
    QuoteParams pcopy{0, 0, 0, 0};
    for (size_t i = 0; i < in.size(); ++i) {
        pkt_in_t p;
        for (int k = 0; k < 8; ++k) set_w(p.data, k, in[i][k]);
        p.last = (i + 1 == in.size());
        s_in.write(p);
        quote_engine(s_in, s_out);  // ap_ctrl_none engine: one call = one packet
        pkt_out_t o = s_out.read();
        int32_t got[4] = {get_w(o.data, 0), get_w(o.data, 1), get_w(o.data, 2), get_w(o.data, 3)};
        bool ok = true;
        for (int k = 0; k < 4; ++k) ok &= got[k] == exp[i][k];
        if (!ok && bad++ < 10)
            std::printf("MISMATCH pkt %zu type %d: got (%d,%d) expected (%d,%d)\n", i, in[i][0] & 0xFF, got[1], got[2],
                        exp[i][1], exp[i][2]);
        const int type = in[i][0] & 0xFF;
        if (type == QE_TYPE_BOOK) ++books;
        if (type == QE_TYPE_PARAM) pcopy = QuoteParams{in[i][1], in[i][2], in[i][3], in[i][4]};
        if (type == QE_TYPE_LUT) lut_copy[in[i][1] & ((1 << (2 * QE_LUT_BITS)) - 1)] = in[i][2];
    }
    std::printf("%zu packets (%zu book updates), %zu mismatches\n", in.size(), books, bad);

#if !QE_HAVE_VITIS
    // CPU baseline for the combinational core (not meaningful inside Vitis csim).
    volatile int64_t sink = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (size_t i = 0; i < in.size(); ++i) {
        if ((in[i][0] & 0xFF) != QE_TYPE_BOOK) continue;
        Quote q = quote_core(in[i][1], in[i][2], in[i][3], in[i][4], in[i][5], pcopy, lut_copy.data());
        sink = sink + q.bid + q.ask;
    }
    auto t1 = std::chrono::steady_clock::now();
    std::printf("CPU quote_core: %.2f ns/update\n",
                std::chrono::duration<double, std::nano>(t1 - t0).count() / (books ? books : 1));
#endif
    return bad ? 1 : 0;
}
