// Wire protocol shared by the exchange, the CPU contestant and (later) the FPGA contestant.
//
// Every message is exactly 32 bytes, little-endian, fixed layout, one message per UDP
// datagram. Fixed offsets keep the hardware parser trivial: no length walking, no varints.
// Prices are integer ticks ($0.01), quantities are shares.
//
//   contestant -> exchange (order entry port)
//     Hello      register: name + the UDP port to receive market data on
//     NewOrder   limit order; tif GTC or IOC; post_only rejects instead of crossing
//     Cancel     cancel by client order id
//   exchange -> contestant
//     Tob        market data: top of book after every change, with a sequence number.
//                Type 0x10 = the change came from market flow, 0x11 = from a contestant. The race
//                rule: contestants trade only on 0x10 events and just track the book on 0x11, so
//                every race is a reaction to the same market event (no feedback loops between
//                contestants). An FPGA checks one byte to apply it.
//     ExecReport ack / reject / fill / cancelled, for that contestant's orders only
//
// trigger_seq on NewOrder/Cancel is the Tob sequence number the contestant reacted to.
// The exchange uses it to measure reaction latency on ITS OWN clock (send time of that Tob
// to this contestant -> arrival of the order) and to judge races: when two contestants react
// to the same update, the earlier arrival wins.
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace proto {

constexpr uint16_t MSG_HELLO = 0x01;
constexpr uint16_t MSG_NEW = 0x02;
constexpr uint16_t MSG_CANCEL = 0x03;
constexpr uint16_t MSG_TOB = 0x10;             // top of book changed by market (historical) flow
constexpr uint16_t MSG_TOB_CONTESTANT = 0x11;  // same layout; changed by a contestant's order or cancel
constexpr uint16_t MSG_ACK = 0x81;
constexpr uint16_t MSG_REJECT = 0x82;
constexpr uint16_t MSG_FILL = 0x83;
constexpr uint16_t MSG_CANCELLED = 0x84;
constexpr uint16_t MSG_HELLO_ACK = 0x85;

constexpr uint8_t SIDE_BUY = 1;
constexpr uint8_t SIDE_SELL = 2;
constexpr uint8_t TIF_GTC = 0;
constexpr uint8_t TIF_IOC = 1;

constexpr uint32_t REJ_POST_ONLY_CROSS = 1;
constexpr uint32_t REJ_UNKNOWN_ORDER = 2;
constexpr uint32_t REJ_BAD_ORDER = 3;
constexpr uint32_t REJ_DUPLICATE_ID = 4;
constexpr uint32_t REJ_NOT_REGISTERED = 5;

constexpr uint32_t NO_PRICE = 0;  // empty side in Tob

#pragma pack(push, 1)
struct Hello {            // 32 bytes
    uint16_t type;        // MSG_HELLO
    uint16_t len;         // 32
    uint16_t md_port;     // UDP port on the sender's address for market data
    char name[26];        // NUL-padded
};

struct NewOrder {         // 32 bytes
    uint16_t type;        // MSG_NEW
    uint16_t len;
    uint32_t client_order_id;
    uint32_t trigger_seq;
    uint32_t price;       // ticks
    uint32_t qty;         // shares
    uint8_t side;
    uint8_t tif;
    uint8_t post_only;
    uint8_t pad;
    uint64_t client_ts;   // contestant's own clock, echoed nowhere; for its own logs
};

struct Cancel {           // 32 bytes
    uint16_t type;        // MSG_CANCEL
    uint16_t len;
    uint32_t client_order_id;
    uint32_t trigger_seq;
    uint8_t pad[12];
    uint64_t client_ts;
};

struct Tob {              // 32 bytes
    uint16_t type;        // MSG_TOB or MSG_TOB_CONTESTANT
    uint16_t len;
    uint32_t seq;         // increments by 1 per update, per exchange run
    uint64_t exch_ts;     // exchange clock, ns
    uint32_t bid;         // ticks, NO_PRICE if empty
    uint32_t bid_qty;
    uint32_t ask;
    uint32_t ask_qty;
};

struct ExecReport {       // 32 bytes
    uint16_t type;        // MSG_ACK / REJECT / FILL / CANCELLED / HELLO_ACK
    uint16_t len;
    uint32_t client_order_id;
    uint32_t price;       // fill price (FILL), order price otherwise
    uint32_t qty;         // fill qty (FILL), else 0
    uint32_t leaves;      // remaining open qty after this event
    uint32_t reason;      // reject reason; for HELLO_ACK the assigned participant id
    uint64_t exch_ts;
};
#pragma pack(pop)

static_assert(sizeof(Hello) == 32, "Hello must be 32 bytes");
static_assert(sizeof(NewOrder) == 32, "NewOrder must be 32 bytes");
static_assert(sizeof(Cancel) == 32, "Cancel must be 32 bytes");
static_assert(sizeof(Tob) == 32, "Tob must be 32 bytes");
static_assert(sizeof(ExecReport) == 32, "ExecReport must be 32 bytes");
constexpr size_t MSG_SIZE = 32;

}  // namespace proto
