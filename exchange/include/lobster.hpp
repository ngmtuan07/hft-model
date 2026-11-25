// LOBSTER message / orderbook file loading (see hftmodel/lobster.py for the format).
// Prices in the files are $ * 10000; the exchange works in ticks of $0.01 (= 100 units).
#pragma once
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

namespace lob {

constexpr int64_t DUMMY_ASK = 9999999999LL;
constexpr int64_t DUMMY_BID = -9999999999LL;

struct Msg {
    uint64_t ts_ns;   // since midnight
    uint64_t id;
    uint32_t size;
    uint32_t px;      // ticks
    uint8_t type;     // 1 add, 2 partial cancel, 3 delete, 4 exec visible, 5 exec hidden, 6 cross, 7 halt
    int8_t dir;       // 1 buy order, -1 sell order (for executions: side of the resting order)
};

struct Level { uint32_t px; uint32_t qty; };            // px 0 = empty
struct Row { std::vector<Level> bids, asks; };          // best first

inline std::string slurp(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) throw std::runtime_error("cannot open " + path);
    std::fseek(f, 0, SEEK_END);
    const long n = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    std::string s(size_t(n), '\0');
    if (n > 0 && std::fread(&s[0], 1, size_t(n), f) != size_t(n)) { std::fclose(f); throw std::runtime_error("read " + path); }
    std::fclose(f);
    return s;
}

inline int64_t parse_int(const char*& p) {
    bool neg = false;
    if (*p == '-') { neg = true; ++p; }
    int64_t v = 0;
    while (*p >= '0' && *p <= '9') v = v * 10 + (*p++ - '0');
    return neg ? -v : v;
}

inline uint64_t parse_time_ns(const char*& p) {    // "34200.004241176" -> ns
    uint64_t s = 0;
    while (*p >= '0' && *p <= '9') s = s * 10 + uint64_t(*p++ - '0');
    uint64_t frac = 0;
    int digits = 0;
    if (*p == '.') {
        ++p;
        while (*p >= '0' && *p <= '9') {
            if (digits < 9) { frac = frac * 10 + uint64_t(*p - '0'); ++digits; }
            ++p;
        }
    }
    while (digits < 9) { frac *= 10; ++digits; }
    return s * 1000000000ull + frac;
}

inline void skip_sep(const char*& p) { if (*p == ',') ++p; }
inline void next_line(const char*& p, const char* end) { while (p < end && *p != '\n') ++p; if (p < end) ++p; }

inline std::vector<Msg> load_messages(const std::string& path) {
    const std::string s = slurp(path);
    std::vector<Msg> out;
    out.reserve(s.size() / 40);
    const char* p = s.data();
    const char* end = p + s.size();
    while (p < end) {
        if (*p == '\n' || *p == '\r') { ++p; continue; }
        Msg m{};
        m.ts_ns = parse_time_ns(p); skip_sep(p);
        m.type = uint8_t(parse_int(p)); skip_sep(p);
        m.id = uint64_t(parse_int(p)); skip_sep(p);
        m.size = uint32_t(parse_int(p)); skip_sep(p);
        const int64_t px = parse_int(p); skip_sep(p);
        m.px = px > 0 ? uint32_t(px / 100) : 0;
        m.dir = int8_t(parse_int(p));
        next_line(p, end);
        out.push_back(m);
    }
    return out;
}

inline Row parse_row(const char*& p, const char* end) {
    Row r;
    while (p < end && *p != '\n' && *p != '\r') {
        const int64_t ap = parse_int(p); skip_sep(p);
        const int64_t as = parse_int(p); skip_sep(p);
        const int64_t bp = parse_int(p); skip_sep(p);
        const int64_t bs = parse_int(p); skip_sep(p);
        r.asks.push_back({ap == DUMMY_ASK || ap <= 0 ? 0u : uint32_t(ap / 100), uint32_t(as)});
        r.bids.push_back({bp == DUMMY_BID || bp <= 0 ? 0u : uint32_t(bp / 100), uint32_t(bs)});
    }
    next_line(p, end);
    return r;
}

// Row `index` of an orderbook file (state after message `index`).
inline Row read_row(const std::string& path, size_t index) {
    const std::string s = slurp(path);
    const char* p = s.data();
    const char* end = p + s.size();
    for (size_t i = 0; i < index && p < end; ++i) next_line(p, end);
    if (p >= end) throw std::runtime_error("orderbook row out of range");
    return parse_row(p, end);
}

// Level-1 of every row: {bid px, bid qty, ask px, ask qty}.
struct Top { uint32_t bid, bid_qty, ask, ask_qty; };
inline std::vector<Top> load_tops(const std::string& path) {
    const std::string s = slurp(path);
    std::vector<Top> out;
    out.reserve(s.size() / 300);
    const char* p = s.data();
    const char* end = p + s.size();
    while (p < end) {
        if (*p == '\n' || *p == '\r') { ++p; continue; }
        const int64_t ap = parse_int(p); skip_sep(p);
        const int64_t as = parse_int(p); skip_sep(p);
        const int64_t bp = parse_int(p); skip_sep(p);
        const int64_t bs = parse_int(p);
        out.push_back({bp == DUMMY_BID || bp <= 0 ? 0u : uint32_t(bp / 100), uint32_t(bs),
                       ap == DUMMY_ASK || ap <= 0 ? 0u : uint32_t(ap / 100), uint32_t(as)});
        next_line(p, end);
    }
    return out;
}

// What history says the visible book looked like right after each message, for reconciling
// the replayed background liquidity (reserve/iceberg refills are never reported as messages).
constexpr uint32_t UNKNOWN = 0xFFFFFFFFu;
struct Target {
    uint32_t msg_qty;          // visible qty at the message's own price/side, UNKNOWN if beyond depth
    uint32_t bid, bid_qty, ask, ask_qty;
};

inline std::vector<Target> load_targets(const std::string& ob_path, const std::vector<Msg>& msgs) {
    const std::string s = slurp(ob_path);
    std::vector<Target> out;
    out.reserve(msgs.size());
    const char* p = s.data();
    const char* end = p + s.size();
    size_t i = 0;
    while (p < end && i < msgs.size()) {
        if (*p == '\n' || *p == '\r') { ++p; continue; }
        const Row r = parse_row(p, end);
        const Msg& m = msgs[i];
        const auto& side = m.dir == 1 ? r.bids : r.asks;
        Target t{UNKNOWN, r.bids.empty() ? 0 : r.bids[0].px, r.bids.empty() ? 0 : r.bids[0].qty,
                 r.asks.empty() ? 0 : r.asks[0].px, r.asks.empty() ? 0 : r.asks[0].qty};
        // the price is visible if it lies inside the snapshot's range of levels on that side
        uint32_t worst = 0;
        bool full = !side.empty() && side.back().px != 0;
        for (const auto& l : side) {
            if (l.px == m.px) { t.msg_qty = l.qty; break; }
            if (l.px) worst = l.px;
        }
        if (t.msg_qty == UNKNOWN && m.px) {
            const bool inside = m.dir == 1 ? (!full || m.px > worst) : (!full || m.px < worst);
            if (inside) t.msg_qty = 0;   // inside the visible range but absent: level is empty
        }
        out.push_back(t);
        ++i;
    }
    if (out.size() != msgs.size()) throw std::runtime_error("orderbook rows != messages");
    return out;
}

inline std::string orderbook_path(const std::string& message_path) {
    std::string s = message_path;
    const auto k = s.find("_message_");
    if (k == std::string::npos) throw std::runtime_error("not a LOBSTER message file: " + s);
    return s.replace(k, 9, "_orderbook_");
}

}  // namespace lob
