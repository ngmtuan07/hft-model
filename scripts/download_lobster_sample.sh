#!/usr/bin/env bash
# Downloads free LOBSTER sample files (2012-06-21, NASDAQ) into data/lobster/.
# Usage: scripts/download_lobster_sample.sh [TICKER ...] [LEVEL]
#   TICKER: AAPL AMZN GOOG INTC MSFT (default: INTC MSFT)   LEVEL: 1 5 10 (default 10)
# Each zip is ~7-9 MB (level 10). Source: https://lobsterdata.com/info/DataSamples.php
set -euo pipefail
LEVEL=10
TICKERS=()
for a in "$@"; do
  if [[ "$a" =~ ^[0-9]+$ ]]; then LEVEL="$a"; else TICKERS+=("$a"); fi
done
[ ${#TICKERS[@]} -eq 0 ] && TICKERS=(INTC MSFT)
DEST="$(cd "$(dirname "$0")/.." && pwd)/data/lobster"
mkdir -p "$DEST"
for T in "${TICKERS[@]}"; do
  Z="LOBSTER_SampleFile_${T}_2012-06-21_${LEVEL}.zip"
  if ls "$DEST"/${T}_2012-06-21_*_message_${LEVEL}.csv >/dev/null 2>&1; then echo "$T already present"; continue; fi
  echo "downloading $Z"
  curl -fL --retry 3 -o "$DEST/$Z" "https://php.lobsterdata.com/info/sample/$Z"
  if ! unzip -tq "$DEST/$Z" >/dev/null 2>&1; then echo "not a zip: $Z (site layout changed?)"; rm -f "$DEST/$Z"; exit 1; fi
  unzip -o -q "$DEST/$Z" -d "$DEST" && rm "$DEST/$Z"
done
ls -lh "$DEST"
