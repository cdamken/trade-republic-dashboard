#!/bin/bash
# =============================================================================
# Trade Republic Dashboard — Single orchestrator
# =============================================================================
# USO:
#   ./dashboard.sh              Smart update + arranca server + abre browser
#   ./dashboard.sh update       Igual que ↑ (alias explícito)
#   ./dashboard.sh full         Force re-download de todo (~3 min)
#   ./dashboard.sh start        Solo arranca el server (no toca datos)
#   ./dashboard.sh stop         Detiene el server
#   ./dashboard.sh restart      stop + start
#   ./dashboard.sh status       Inventario, fechas, estado del server
#
# Smart update:
#   - portfolio_raw.txt: siempre se descarga (precios live, ~5 s)
#   - account_transactions.csv: incremental real
#       * primer uso o gap >365d → full download (~3 min)
#       * caso normal → --last_days N + merge dedupe (~2-5 s)
# =============================================================================

set -e

# Resolve PROJECT_DIR from the script's own location (portable across machines)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$PROJECT_DIR/app"
DATA_DIR="$PROJECT_DIR/DATA"
PORT="${TR_DASHBOARD_PORT:-8085}"

# tr-api lives in its own repo. Override with TR_API_PATH if it's somewhere
# unusual; otherwise we look for the sibling checkout, then fall back to
# installing from PyPI.
TR_API_PATH="${TR_API_PATH:-$PROJECT_DIR/../tr-api}"

VENV_DIR="$PROJECT_DIR/.venv"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

LAST_UPDATE_FILE="$DATA_DIR/last_update.date"
LOG_FILE="$DATA_DIR/last_update.log"
TX_FILE="$DATA_DIR/account_transactions.csv"
PORTFOLIO_FILE="$DATA_DIR/portfolio_raw.txt"
SERVER_LOG="$DATA_DIR/server.log"
SERVER_PID="$DATA_DIR/server.pid"

mkdir -p "$DATA_DIR"
cd "$PROJECT_DIR"

# ----------------------------------------------------------------------- python env
# First-run-only: build .venv and install tr-api (+ playwright chromium for
# the WAF token). Idempotent — subsequent runs just verify the import works.
ensure_python_env() {
    if [ ! -x "$PY" ]; then
        echo "🐍 Creating Python venv at $VENV_DIR …"
        python3 -m venv "$VENV_DIR"
        "$PIP" install --quiet --upgrade pip >/dev/null
    fi
    if ! "$PY" -c "import tr_api" 2>/dev/null; then
        echo "📦 Installing tr-api into the dashboard venv …"
        if [ -d "$TR_API_PATH" ]; then
            "$PIP" install --quiet -e "$TR_API_PATH[browser]"
        else
            "$PIP" install --quiet "tr-api[browser]"
        fi
        echo "🌐 Installing headless Chromium for tr-api (one-time) …"
        "$PY" -m playwright install chromium >/dev/null
    fi
}

# ----------------------------------------------------------------------- banners
mfa_banner() {
    cat <<'BANNER'

┌──────────────────────────────────────────────────────────────────────┐
│  🔐  TRADE REPUBLIC SECURITY CODE                                    │
│                                                                      │
│  If you see "Code:" below, your session expired. TR needs a 4-digit  │
│  security code.                                                      │
│                                                                      │
│  📱 Open the Trade Republic app — push notification with the code.   │
│  💬 No push? Press Enter at the prompt for SMS fallback.             │
│  ⏱  ~60 seconds before the code expires.                             │
└──────────────────────────────────────────────────────────────────────┘

BANNER
}

# ----------------------------------------------------------------------- downloads
download_portfolio() {
    echo "📊 Downloading portfolio snapshot (live prices)..."
    $PYTR_PATH portfolio > "$PORTFOLIO_FILE"
}

download_transactions() {
    local buffer=3 force_full=${1:-0}

    # No previous CSV → must do full download
    if [ ! -f "$TX_FILE" ] || [ "$force_full" = "1" ]; then
        echo "📋 Downloading FULL transactions CSV (~3 min)..."
        $PYTR_PATH export_transactions "$TX_FILE"
        return
    fi

    # No last_update.date → can't compute window → fall back to full
    if [ ! -f "$LAST_UPDATE_FILE" ]; then
        echo "📋 No last_update.date — full download as baseline (~3 min)..."
        $PYTR_PATH export_transactions "$TX_FILE"
        return
    fi

    # Compute window
    local last_date last_epoch today_epoch days_since days_to_scan
    last_date=$(cat "$LAST_UPDATE_FILE")
    last_epoch=$(date -j -f "%Y-%m-%d" "$last_date" +%s 2>/dev/null) || last_epoch=0
    today_epoch=$(date +%s)
    days_since=$(( (today_epoch - last_epoch) / 86400 ))
    days_to_scan=$(( days_since + buffer ))

    # Too big a gap → fall back to full
    if [ "$days_to_scan" -gt 365 ]; then
        echo "📋 More than a year since last update — falling back to full (~3 min)..."
        $PYTR_PATH export_transactions "$TX_FILE"
        return
    fi

    # Incremental download into tmp, then merge
    echo "📋 Incremental download — last $days_to_scan days (~2 s)..."
    local tmpfile="$DATA_DIR/.tx_incremental.csv"
    $PYTR_PATH export_transactions --last_days "$days_to_scan" "$tmpfile"

    if [ ! -s "$tmpfile" ]; then
        echo "    ℹ No new transactions found."
        rm -f "$tmpfile"
        return
    fi

    echo "🔀 Merging with existing history..."
    local before after merged
    before=$(($(wc -l < "$TX_FILE") - 1))
    merged="$DATA_DIR/.tx_merged.csv"
    {
        head -1 "$TX_FILE"   # header from existing CSV
        { tail -n +2 "$TX_FILE"; tail -n +2 "$tmpfile"; } \
            | awk '!seen[$0]++' \
            | sort -t';' -k1,1
    } > "$merged"
    mv "$merged" "$TX_FILE"
    rm -f "$tmpfile"
    after=$(($(wc -l < "$TX_FILE") - 1))
    echo "    Transactions: $before → $after  (+$((after - before)) new)"
}

# ----------------------------------------------------------------------- processing
process_data() {
    echo "⚙️  Processing portfolio + analytics..."
    # parse_pytr_output.py is obsolete — tr_fetch.py now writes portfolio.json
    # directly. We keep this stub for any old callers; analytics still applies.
    "$PY" "$APP_DIR/analyze_analytics.py"
}

cleanup() {
    local removed
    removed=$(find "$PROJECT_DIR" \( -name '.DS_Store' -o -name '*.tmp' -o -name '*.partial' \) -type f -print -delete 2>/dev/null | wc -l | tr -d ' ')
    if [ "$removed" -gt 0 ]; then
        echo "🧹 Cleaned $removed leftover files."
    fi
}

# ----------------------------------------------------------------------- server
start_server() {
    ensure_python_env
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null; then
        echo "🌐 Server already running at http://localhost:$PORT/app/index.html"
        open "http://localhost:$PORT/app/index.html" 2>/dev/null
        return
    fi
    echo "🚀 Starting local server on port $PORT..."
    "$PY" "$APP_DIR/server.py" > "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID"
    sleep 2
    echo "🌐 Server ready at http://localhost:$PORT/app/index.html"
    open "http://localhost:$PORT/app/index.html" 2>/dev/null
}

stop_server() {
    if [ -f "$SERVER_PID" ]; then
        local pid
        pid=$(cat "$SERVER_PID")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "🛑 Server stopped (PID $pid)."
        fi
        rm -f "$SERVER_PID"
    fi
    local lsof_pid
    lsof_pid=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$lsof_pid" ]; then
        kill -9 "$lsof_pid" 2>/dev/null
    fi
}

# ----------------------------------------------------------------------- actions
# do_update / do_full now delegate to app/tr_fetch.py (the same module used by
# the web UI via POST /update). The CLI flow is the interactive path: if pytr
# needs MFA, it prompts on the terminal directly. The MFA banner appears first
# so the user knows what's happening.
do_update() {
    ensure_python_env
    set -o pipefail
    {
        mfa_banner
        "$PY" "$APP_DIR/tr_fetch.py"
    } 2>&1 | tee "$LOG_FILE"
    local rc=${PIPESTATUS[0]}
    set +o pipefail
    if [ "$rc" -ne 0 ]; then
        echo "❌ Update failed (exit $rc). See log: $LOG_FILE"
        exit "$rc"
    fi
    summarize
    start_server
}

do_full() {
    ensure_python_env
    echo "⚠️  FULL update — re-downloads portfolio + FULL transactions (~3 min)."
    set -o pipefail
    {
        mfa_banner
        "$PY" "$APP_DIR/tr_fetch.py" --full
    } 2>&1 | tee "$LOG_FILE"
    local rc=${PIPESTATUS[0]}
    set +o pipefail
    if [ "$rc" -ne 0 ]; then
        echo "❌ Update failed (exit $rc). See log: $LOG_FILE"
        exit "$rc"
    fi
    summarize
    start_server
}

do_status() {
    echo "📊 TRADE REPUBLIC DASHBOARD — STATUS"
    echo "===================================="
    echo "Project:     $PROJECT_DIR"
    echo "Total size:  $(du -sh "$PROJECT_DIR" 2>/dev/null | cut -f1)"
    echo ""
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "Last update: $(cat "$LAST_UPDATE_FILE")"
    else
        echo "Last update: never (run ./dashboard.sh)"
    fi
    echo ""
    echo "Data files:"
    for f in "$PORTFOLIO_FILE" "$TX_FILE" "$DATA_DIR/portfolio.json" "$DATA_DIR/analytics.json" "$DATA_DIR/net_worth_history.json"; do
        if [ -f "$f" ]; then
            local mtime size
            mtime=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f")
            size=$(du -h "$f" | cut -f1)
            printf "  %-45s  %6s  %s\n" "$(basename "$f")" "$size" "$mtime"
        else
            printf "  %-45s  (missing)\n" "$(basename "$f")"
        fi
    done
    echo ""
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "Server:      🟢 RUNNING  (http://localhost:$PORT/app/index.html)"
    else
        echo "Server:      ⚪ stopped  (start with: ./dashboard.sh start)"
    fi
}

summarize() {
    echo ""
    echo "✅ Update complete."
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "    Date saved:  $(cat "$LAST_UPDATE_FILE") → $LAST_UPDATE_FILE"
    fi
    echo "    Log:         $LOG_FILE"
}

# ----------------------------------------------------------------------- dispatch
do_reset() {
    cat <<'EOF'
⚠️  This will ERASE everything related to the current Trade Republic account:
   • ~/.pytr/credentials  (phone + PIN)
   • ~/.pytr/cookies.*    (session)
   • DATA/                (portfolio, transactions, history, analytics)

The next run of ./dashboard.sh will trigger the first-time setup wizard.
EOF
    read -p "Type 'delete' to confirm: " ans
    if [ "$ans" != "delete" ]; then
        echo "Aborted."
        exit 0
    fi

    # pytr credentials + cookies
    rm -f "$HOME/.pytr/credentials"
    rm -f "$HOME/.pytr"/cookies.*

    # Project DATA contents
    if [ -d "$DATA_DIR" ]; then
        find "$DATA_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    fi
    mkdir -p "$DATA_DIR"

    echo "✅ Account erased. Run ./dashboard.sh to configure a new one."
}

case "${1:-}" in
    "")        do_update ;;
    update)    do_update ;;
    full)      do_full ;;
    start)     start_server ;;
    stop)      stop_server ;;
    restart)   stop_server; start_server ;;
    status)    do_status ;;
    reset)     do_reset ;;
    *)
        echo "Usage: $0 [update|full|start|stop|restart|status|reset]"
        echo ""
        echo "  (no args)  Smart update + arranca server (alias de 'update')"
        echo "  update     Smart update (incremental transactions, full portfolio)"
        echo "  full       Force full re-download of everything (~3 min)"
        echo "  start      Just start the local HTTP server"
        echo "  stop       Stop the server"
        echo "  restart    stop + start"
        echo "  status     Show data files, last update, server state"
        echo "  reset      Erase current account (credentials + DATA) to switch"
        exit 1
        ;;
esac
