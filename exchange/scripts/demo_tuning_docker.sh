#!/usr/bin/env bash
# Runs scripts/demo_tuning.sh inside a Linux container (Docker Desktop on a Mac).
# Scoreboard: http://localhost:8080   Stop with Ctrl+C.
#   LINUX_IMAGE  any image with g++, make and taskset (default gcc:14)
#   NOISE        busy-loop processes simulating a busy server (default 6)   SHARED  their cores (default 0,3,4)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
docker rm -f hft-tuning-demo >/dev/null 2>&1 || true
# always remove the container when this script stops (Ctrl+C, kill, or the demo ending)
cleanup() { docker rm -f hft-tuning-demo >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker run --rm --name hft-tuning-demo --pull=never -p "${UI_PORT:-8080}:8080" \
    --cap-add SYS_NICE --cap-add NET_ADMIN --ulimit memlock=-1 \
    ${NOISE:+-e NOISE=$NOISE} ${SHARED:+-e SHARED=$SHARED} -v "$ROOT":/w -w /w/exchange "${LINUX_IMAGE:-gcc:14}" bash scripts/demo_tuning.sh &
wait $!
