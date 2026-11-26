#!/usr/bin/env bash
# Local race: exchange + two CPU contestants on this machine.
#   A_DELAY_NS / B_DELAY_NS  artificial handicap per contestant (ns)
#   SPEED   history replay speed (1 = real time)    DUR  seconds    START  HH:MM:SS of the day
#   MSG     LOBSTER message file                     OUT  output folder
#   UI_PORT serve the live scoreboard on this port (e.g. 8080)
#   BIN     build directory (default build)
#   EX_FLAGS / A_FLAGS / B_FLAGS  extra flags for the exchange / contestant A / contestant B,
#           e.g. A_FLAGS="--core 2 --rt 50 --mlock --busy-poll 50 --kernel-ts" on Linux
#   A_PREFIX / B_PREFIX  command prefix for a contestant, e.g. B_PREFIX="taskset -c 0,3-7"
#   SNIPE_MS  stale-quote race: post a mispriced order every ~SNIPE_MS ms (off if unset)
set -euo pipefail
cd "$(dirname "$0")/.."
MSG=${MSG:-../data/lobster/INTC_2012-06-21_34200000_57600000_message_10.csv}
DUR=${DUR:-30}; SPEED=${SPEED:-10}; START=${START:-10:00:00}
A_NAME=${A_NAME:-fast}; B_NAME=${B_NAME:-slow}
A_DELAY_NS=${A_DELAY_NS:-0}; B_DELAY_NS=${B_DELAY_NS:-20000}
OUT=${OUT:-../output/race}
BIN=${BIN:-build}
mkdir -p "$OUT"
./$BIN/exchange run ${EX_FLAGS:-} --messages "$MSG" --start "$START" --duration "$DUR" --speed "$SPEED" --wait 2 \
    --stats "$OUT/stats.jsonl" --summary "$OUT/summary.json" ${ORDER_LOG:+--order-log "$OUT/orders.csv"} ${UI_PORT:+--http "$UI_PORT"} ${SNIPE_MS:+--snipe-every-ms "$SNIPE_MS"} > "$OUT/exchange.log" 2>&1 &
EX=$!
[ -n "${UI_PORT:-}" ] && echo "scoreboard: http://localhost:$UI_PORT"
trap 'kill $EX 2>/dev/null || true' EXIT
${A_PREFIX:-} ./$BIN/trader ${A_FLAGS:-} --name "$A_NAME" --delay-ns "$A_DELAY_NS" --duration $((DUR + 90)) > "$OUT/$A_NAME.log" 2>&1 &
TA=$!
${B_PREFIX:-} ./$BIN/trader ${B_FLAGS:-} --name "$B_NAME" --delay-ns "$B_DELAY_NS" --duration $((DUR + 90)) > "$OUT/$B_NAME.log" 2>&1 &
TB=$!
wait $EX
kill $TA $TB 2>/dev/null || true
wait $TA $TB 2>/dev/null || true
grep -v '^\[' "$OUT/exchange.log" | grep -v SUMMARY || true
tail -n 2 "$OUT/exchange.log" | grep '^\[' || true
cat "$OUT/$A_NAME.log" "$OUT/$B_NAME.log"
