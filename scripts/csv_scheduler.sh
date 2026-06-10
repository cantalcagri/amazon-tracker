#!/bin/bash
# Daily CSV scheduler — runs daily_pipeline.sh once per day at RUN_HOUR local.
#
# Why this exists: the launchd job (com.amazontracker.daily) is blocked by
# macOS TCC for Desktop paths (exit 126), so the daily CSV import silently
# never ran — June 2026's BSR gap. Like the collector watchdog, this runs as a
# nohup'd loop started from the Login Item (~/start_amazon_tracker.command).
#
# The CSV step needs the dedicated Keepa Chrome profile listening on :9222;
# we launch it if it's not already up (same profile start_dashboard.sh uses).
#
# Env overrides:
#   RUN_HOUR  — local hour (0-23) after which today's run fires (default 8)
#   TICKS     — per-seller ticks inside daily_pipeline (default 0: the two
#               --loop collectors already cover per-seller continuously)

REPO="/Users/cagri/Desktop/amazon-tracker"
STAMP_FILE="$REPO/logs/.csv_last_run"
RUN_HOUR="${RUN_HOUR:-8}"
export TICKS="${TICKS:-0}"

mkdir -p "$REPO/logs"
echo "[$(date)] csv_scheduler started (run hour ${RUN_HOUR}, pid $$)"

ensure_chrome() {
  if ! pgrep -f "chrome_keepa" >/dev/null 2>&1; then
    echo "[$(date)] launching Keepa Chrome profile on :9222"
    nohup "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      --remote-debugging-port=9222 \
      --user-data-dir="$REPO/pipeline/.chrome_keepa" \
      --no-first-run --no-default-browser-check \
      "https://keepa.com" >/dev/null 2>&1 &
    sleep 20   # give Chrome time to restore the logged-in Keepa session
  fi
}

while true; do
  TODAY="$(date +%Y-%m-%d)"
  LAST_RUN="$(cat "$STAMP_FILE" 2>/dev/null)"
  HOUR="$(date +%H)"
  if [ "$LAST_RUN" != "$TODAY" ] && [ "$((10#$HOUR))" -ge "$RUN_HOUR" ]; then
    echo "[$(date)] starting daily pipeline (last run: ${LAST_RUN:-never})"
    ensure_chrome
    if bash "$REPO/scripts/daily_pipeline.sh"; then
      echo "[$(date)] daily pipeline OK"
    else
      echo "[$(date)] daily pipeline reported failure — will retry tomorrow (check logs/run_*.log)"
    fi
    # Stamp even on failure so a broken Chrome can't trigger a retry storm;
    # health_check + the dashboard freshness banner surface a missed day.
    echo "$TODAY" > "$STAMP_FILE"
  fi
  sleep 600
done
