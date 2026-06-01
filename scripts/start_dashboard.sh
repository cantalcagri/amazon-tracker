#!/bin/bash
# Launch the Amazon Tracker dashboard (Streamlit) + public Cloudflare tunnel.
#
# - Reads DB_PATH and DASHBOARD_PASSWORD from pipeline/.env
# - Serves Streamlit on :8502 (8501 is the Costco dashboard)
# - Opens a Cloudflare quick-tunnel → public https URL (printed to the log)
#
# Logs: logs/dashboard.out.log (streamlit), logs/tunnel.out.log (public URL)

REPO="/Users/cagri/Desktop/amazon-tracker"
VENV="$REPO/.venv"
PORT=8502
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"

# Load env (DB_PATH, DASHBOARD_PASSWORD)
set -a
[ -f "$REPO/pipeline/.env" ] && . "$REPO/pipeline/.env"
set +a
export DB_PATH="${DB_PATH:-$REPO/pipeline/amazon_tracker.db}"

# Run from repo root so Streamlit picks up .streamlit/config.toml (dark theme)
cd "$REPO"

# Launch the dedicated Keepa Chrome profile (used by the daily CSV export).
# A separate profile from the user's main Chrome — no account crossover.
CHROME_KEEPA_PROFILE="$REPO/pipeline/.chrome_keepa"
if ! pgrep -f "chrome_keepa" >/dev/null 2>&1; then
  nohup /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$CHROME_KEEPA_PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    "https://keepa.com" > /dev/null 2>&1 &
  echo "[$(date)] Keepa Chrome started (PID $!)" >> "$LOG_DIR/dashboard.out.log"
fi

# Kill any previous instances of THIS dashboard / tunnel (not the Costco one on 8501)
pkill -f "streamlit run.*amazon-tracker/dashboard/dashboard.py" 2>/dev/null
pkill -f "streamlit run dashboard/dashboard.py" 2>/dev/null
pkill -f "cloudflared tunnel --url http://localhost:$PORT" 2>/dev/null
sleep 2

# Start Streamlit (headless, listening on all interfaces so the tunnel can reach it)
nohup "$VENV/bin/streamlit" run "$REPO/dashboard/dashboard.py" \
  --server.port "$PORT" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false \
  >> "$LOG_DIR/dashboard.out.log" 2>&1 &
echo "[$(date)] streamlit started (PID $!) on :$PORT" >> "$LOG_DIR/dashboard.out.log"

# Wait for Streamlit to come up
for i in $(seq 1 30); do
  curl -s -o /dev/null "http://localhost:$PORT" && break
  sleep 1
done

# Start the Cloudflare quick tunnel → public URL appears in the tunnel log
nohup cloudflared tunnel --url "http://localhost:$PORT" \
  >> "$LOG_DIR/tunnel.out.log" 2>&1 &
echo "[$(date)] cloudflared started (PID $!)" >> "$LOG_DIR/tunnel.out.log"
