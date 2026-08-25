#!/usr/bin/env bash
# Daily refresh for the raw layer — a stopgap until Phase 4 (proper
# orchestration) replaces it. Keeps Binance current and, more urgently,
# keeps Kraken's ~720-candle rolling window from eroding unrecoverably:
# every day this doesn't run, that window shrinks and the missing candles
# are gone for good. See NOTES.md / ROADMAP.md (Phase 2 & 4).
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate

for source in binance kraken; do
  for interval in 1h 4h; do
    python -m ingestion.load --source "$source" --interval "$interval" >> trading_bot.log 2>&1
  done
done
