#!/bin/bash
#
# daily_pipeline.sh — one orchestrated daily run of the Amazon Tracker.
#
# Steps (each is fault-tolerant; a failure degrades status to partial/failed
# but never aborts the whole run):
#   1. Keepa Viewer CSV export  → fct_keepa_daily      (needs logged-in Chrome)
#   2. Per-seller API ticks ×N  → fct_keepa_seller_history
#   3. Seller-name catch-up     → dim_keepa_seller
#   4. Health check             → logs/health_*.txt
#   5. DB backup                → backups/  (keeps last $BACKUP_KEEP_DAYS)
#   6. Log the run              → pipeline_runs table
#
# Deploy on the Mac mini via launchd (see scripts/com.amazontracker.daily.plist)
# or cron. Override these with env vars if your paths differ:
#   PYTHON        — python interpreter (default: python3 on PATH)
#   SKIP_CSV=1    — skip the Selenium CSV step (e.g. headless box, no Chrome)
#   TICKS         — number of --tick batches to run (default 15)
#   TICK_SLEEP    — seconds between ticks (default 600 = 10 min)
#   BACKUP_KEEP_DAYS — how many daily backups to retain (default 14)
#
set -uo pipefail

# ── Resolve repo paths (script lives in <repo>/scripts) ────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PIPELINE_DIR="$REPO_DIR/pipeline"
DB_PATH="${DB_PATH:-$PIPELINE_DIR/amazon_tracker.db}"
ASINS_FILE="${ASINS_FILE:-$REPO_DIR/data/asins.txt}"
[ -f "$ASINS_FILE" ] || ASINS_FILE="$PIPELINE_DIR/asins.txt"

PYTHON="${PYTHON:-python3}"
TICKS="${TICKS:-15}"
TICK_SLEEP="${TICK_SLEEP:-600}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

LOG_DIR="$REPO_DIR/logs"
BACKUP_DIR="$REPO_DIR/backups"
mkdir -p "$LOG_DIR" "$BACKUP_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="$LOG_DIR/run_$STAMP.log"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STATUS="success"
NOTES=""

export DB_PATH

log()  { echo "$(date -u +%H:%M:%S) $*" | tee -a "$RUN_LOG"; }
fail() { STATUS="failed";  NOTES="$NOTES; $*"; log "FAIL: $*"; }
warn() { [ "$STATUS" = "success" ] && STATUS="partial"; NOTES="$NOTES; $*"; log "WARN: $*"; }

sqlq() { sqlite3 "$DB_PATH" "$1" 2>>"$RUN_LOG"; }

log "=== daily_pipeline START (repo=$REPO_DIR) ==="
SELLER_EVENTS_BEFORE="$(sqlq 'SELECT COUNT(*) FROM fct_keepa_seller_history' || echo 0)"

# ── 1. CSV export (Keepa Viewer via dedicated Chrome profile on :9222) ──────
# Uses the isolated .chrome_keepa profile — NOT the user's main Chrome account.
# Chrome must be running (start_dashboard.sh launches it automatically at login).
if [ "${SKIP_CSV:-0}" = "1" ]; then
  log "Step 1: CSV export SKIPPED (SKIP_CSV=1)"
else
  log "Step 1: Keepa Viewer CSV export (dedicated Chrome profile, port 9222)"
  VENV_PY="$REPO_DIR/.venv/bin/python"
  [ ! -x "$VENV_PY" ] && VENV_PY="$PYTHON"
  if ( cd "$PIPELINE_DIR" && "$VENV_PY" keepa_viewer_export.py \
        --asins-file "$ASINS_FILE" \
        --connect-port 9222 \
        --download-dir "$PIPELINE_DIR/keepa_exports" ) >>"$RUN_LOG" 2>&1; then
    log "  CSV export OK"
    # Import the latest CSV
    LATEST_CSV=$(ls -t "$PIPELINE_DIR/keepa_exports/"*.csv 2>/dev/null | head -1)
    if [ -n "$LATEST_CSV" ]; then
      if ( cd "$PIPELINE_DIR" && "$VENV_PY" keepa_csv_importer.py "$LATEST_CSV" ) >>"$RUN_LOG" 2>&1; then
        log "  CSV import OK → $LATEST_CSV"
      else
        warn "CSV import failed"
      fi
    fi
  else
    warn "CSV export failed — is Keepa Chrome running? Check logs/run_*.log"
  fi
fi

# ── 2. Per-seller API ticks ─────────────────────────────────────────────────
log "Step 2: per-seller API ticks (x$TICKS, ${TICK_SLEEP}s apart)"
# Guard: BSD seq counts DOWN for `seq 1 0`, so TICKS=0 would run 2 ticks
for i in $(if [ "$TICKS" -ge 1 ]; then seq 1 "$TICKS"; fi); do
  if ( cd "$PIPELINE_DIR" && "$PYTHON" keepa_api_offers.py --tick ) >>"$RUN_LOG" 2>&1; then
    log "  tick $i/$TICKS OK"
  else
    warn "tick $i/$TICKS failed"
  fi
  [ "$i" -lt "$TICKS" ] && sleep "$TICK_SLEEP"
done

# ── 3. Seller-name catch-up (cheap; only fetches unnamed) ───────────────────
log "Step 3: seller-name catch-up"
if ( cd "$PIPELINE_DIR" && "$PYTHON" keepa_api_offers.py --seller-names ) >>"$RUN_LOG" 2>&1; then
  log "  seller-names OK"
else
  warn "seller-names step failed"
fi

# ── 4. Health check ─────────────────────────────────────────────────────────
log "Step 4: health check"
if ( cd "$PIPELINE_DIR" && "$PYTHON" health_check.py ) >>"$RUN_LOG" 2>&1; then
  log "  health: healthy"
else
  warn "health check reported issues (non-fatal; see $RUN_LOG)"
fi

# ── 5. DB backup + prune (local + off-machine mirror) ───────────────────────
# BACKUP_MIRROR defaults to iCloud Drive if present — set it to any synced
# folder (Dropbox, an external drive, a mounted bucket) to override.
DEFAULT_MIRROR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/amazon-tracker-backups"
BACKUP_MIRROR="${BACKUP_MIRROR:-$DEFAULT_MIRROR}"
log "Step 5: DB backup"
if [ -f "$DB_PATH" ]; then
  LOCAL_BAK="$BACKUP_DIR/amazon_tracker_$STAMP.db"
  # sqlite3 .backup is safe against a live DB (handles locks correctly)
  if sqlite3 "$DB_PATH" ".backup '$LOCAL_BAK'" 2>>"$RUN_LOG"; then
    log "  local backup → $LOCAL_BAK"
    find "$BACKUP_DIR" -name 'amazon_tracker_*.db' -mtime +"$BACKUP_KEEP_DAYS" -delete 2>/dev/null
    # Off-machine mirror so a dead disk doesn't take the history with it.
    if [ -n "$BACKUP_MIRROR" ]; then
      if mkdir -p "$BACKUP_MIRROR" 2>>"$RUN_LOG" && cp "$LOCAL_BAK" "$BACKUP_MIRROR/" 2>>"$RUN_LOG"; then
        log "  off-machine mirror → $BACKUP_MIRROR"
        find "$BACKUP_MIRROR" -name 'amazon_tracker_*.db' -mtime +"$BACKUP_KEEP_DAYS" -delete 2>/dev/null
      else
        warn "off-machine mirror to $BACKUP_MIRROR failed"
      fi
    fi
  else
    warn "DB backup failed"
  fi
else
  fail "DB not found at $DB_PATH"
fi

# ── 6. Log the run ──────────────────────────────────────────────────────────
FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CSV_ROWS_TODAY="$(sqlq "SELECT COUNT(*) FROM fct_keepa_daily WHERE snapshot_date = date('now','localtime')" || echo 0)"
SELLER_EVENTS_AFTER="$(sqlq 'SELECT COUNT(*) FROM fct_keepa_seller_history' || echo 0)"
NOTES_ESC="$(printf '%s' "${NOTES#; }" | sed "s/'/''/g")"

sqlq "INSERT INTO pipeline_runs (started_at, finished_at, status, csv_rows_today, seller_events, notes)
      VALUES ('$STARTED_AT', '$FINISHED_AT', '$STATUS', $CSV_ROWS_TODAY, $SELLER_EVENTS_AFTER, '$NOTES_ESC');"

log "=== daily_pipeline END status=$STATUS csv_today=$CSV_ROWS_TODAY seller_events=${SELLER_EVENTS_BEFORE}->${SELLER_EVENTS_AFTER} ==="

[ "$STATUS" = "failed" ] && exit 1 || exit 0
