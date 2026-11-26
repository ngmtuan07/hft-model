#!/usr/bin/env bash
# Tuned vs untuned CPU contestant, live scoreboard. LINUX ONLY (on a Mac use demo_tuning_docker.sh).
#
# Simulates a busy production server: NOISE busy-loop processes load the shared cores.
#   core 1          exchange (pinned, busy polling, kernel timestamps)
#   core 2          "tuned" trader: reserved core the noise never touches, busy polling,
#                   kernel timestamps, locked memory. (What isolcpus gives you on real hardware.)
#   SHARED cores    "untuned" trader: an ordinary process on the shared cores, competing with the noise.
#   remaining cores left idle, so a VM on a laptop doesn't starve its host (a real server has no such layer)
# Both run the identical strategy; only the environment differs.
#   SHARED=0,3,4  cores the noise and the untuned trader share
#   NOISE=6       busy-loop processes on them (0 = idle machine: then both look the same)
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(uname -s)" = "Linux" ] || { echo "Linux only: on a Mac run scripts/demo_tuning_docker.sh"; exit 1; }
NCPU=$(nproc)
[ "$NCPU" -ge 4 ] || { echo "needs at least 4 CPUs"; exit 1; }
SHARED=${SHARED:-0,3,4}
NOISE=${NOISE:-6}
make -s B=build-linux
NOISE_PIDS=()
cleanup() { [ ${#NOISE_PIDS[@]} -gt 0 ] && kill "${NOISE_PIDS[@]}" 2>/dev/null; true; }
trap cleanup EXIT INT TERM
for _ in $(seq 1 "$NOISE"); do taskset -c "$SHARED" sh -c 'while :; do :; done' & NOISE_PIDS+=($!); done
echo "load: $NOISE busy-loop processes on cores $SHARED (the tuned trader's core 2 is reserved)"
export BIN=build-linux
export MSG=${MSG:-../data/lobster/AAPL_2012-06-21_34200000_57600000_message_10.csv}
export UI_PORT=${UI_PORT:-8080} DUR=${DUR:-1800} SPEED=${SPEED:-10} START=${START:-10:00:00}
export SNIPE_MS=${SNIPE_MS:-50}
export A_NAME=tuned B_NAME=untuned A_DELAY_NS=0 B_DELAY_NS=0 OUT=${OUT:-../output/demo_tuning}
export EX_FLAGS="--core 1 --busy-poll 50 --kernel-ts --mlock"
export A_FLAGS="--core 2 --busy-poll 50 --kernel-ts --mlock"
export B_FLAGS="" B_PREFIX="taskset -c $SHARED"
./scripts/race.sh
