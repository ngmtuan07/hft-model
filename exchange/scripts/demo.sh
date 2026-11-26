#!/usr/bin/env bash
# Live demo: exchange + two identical CPU contestants, scoreboard at http://localhost:8080.
# Use the "Extra latency" sliders on the page to handicap either side.
set -euo pipefail
cd "$(dirname "$0")/.."
make -s
export MSG=${MSG:-../data/lobster/AAPL_2012-06-21_34200000_57600000_message_10.csv}
export UI_PORT=${UI_PORT:-8080} DUR=${DUR:-1800} SPEED=${SPEED:-10} START=${START:-10:00:00}
export A_NAME=${A_NAME:-cpu-a} B_NAME=${B_NAME:-cpu-b} A_DELAY_NS=${A_DELAY_NS:-0} B_DELAY_NS=${B_DELAY_NS:-0}
export OUT=${OUT:-../output/demo}
exec ./scripts/race.sh
