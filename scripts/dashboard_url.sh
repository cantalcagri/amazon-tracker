#!/bin/bash
# Print the current public dashboard URL + whether services are up.
REPO="/Users/cagri/Desktop/amazon-tracker"

URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$REPO/logs/tunnel.out.log" 2>/dev/null | tail -1)
DASH=$(pgrep -f "streamlit run.*dashboard.py" >/dev/null && echo "running" || echo "DOWN")
TUN=$(pgrep -f "cloudflared tunnel --url http://localhost:8502" >/dev/null && echo "running" || echo "DOWN")

echo "Dashboard (streamlit :8502): $DASH"
echo "Cloudflare tunnel:           $TUN"
echo "Public URL:                  ${URL:-<none — run scripts/start_dashboard.sh>}"
echo "Local URL:                   http://localhost:8502"
echo ""
echo "Password: set in pipeline/.env (DASHBOARD_PASSWORD)"
