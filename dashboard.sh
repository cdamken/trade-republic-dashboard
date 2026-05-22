#!/bin/bash
# =============================================================================
# Trade Republic Dashboard — Single orchestrator
# =============================================================================
# USO:
#   ./dashboard.sh              Arranca server + abre browser
#                               (toda la actualización se hace desde el web UI)
#   ./dashboard.sh start        Igual que el default
#   ./dashboard.sh stop         Detiene el server
#   ./dashboard.sh restart      stop + start
#   ./dashboard.sh status       Inventario, fechas, estado del server
#
# Para actualizar datos, cambiar de cuenta, o meter el código MFA:
#   abre el dashboard y usa los botones en la UI.
#   El terminal NUNCA te pide el código de 4 dígitos — siempre va en el modal.
# =============================================================================

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$PROJECT_DIR/app"
DATA_DIR="$PROJECT_DIR/DATA"
PORT="${TR_DASHBOARD_PORT:-8085}"

# tr-api lives in its own repo. Override with TR_API_PATH if it's somewhere
# unusual; otherwise we look for the sibling checkout, then fall back to PyPI.
TR_API_PATH="${TR_API_PATH:-$PROJECT_DIR/../tr-api}"

VENV_DIR="$PROJECT_DIR/.venv"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

LAST_UPDATE_FILE="$DATA_DIR/last_update.date"
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

    if [ ! -f "$HOME/.pytr/credentials" ]; then
        echo "ℹ️  No credentials yet — the dashboard will pop up the setup wizard."
    fi
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

# ----------------------------------------------------------------------- status
do_status() {
    echo "📊 TRADE REPUBLIC DASHBOARD — STATUS"
    echo "===================================="
    echo "Project:     $PROJECT_DIR"
    echo "Total size:  $(du -sh "$PROJECT_DIR" 2>/dev/null | cut -f1)"
    echo ""
    if [ -f "$LAST_UPDATE_FILE" ]; then
        echo "Last update: $(cat "$LAST_UPDATE_FILE")"
    else
        echo "Last update: never (open the dashboard and click ⟳ Update)"
    fi
    echo ""
    echo "Data files:"
    for f in "$DATA_DIR/portfolio.json" \
             "$DATA_DIR/account_transactions.csv" \
             "$DATA_DIR/analytics.json" \
             "$DATA_DIR/net_worth_history.json"; do
        if [ -f "$f" ]; then
            local mtime size
            mtime=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f")
            size=$(du -h "$f" | cut -f1)
            printf "  %-40s  %6s  %s\n" "$(basename "$f")" "$size" "$mtime"
        else
            printf "  %-40s  (missing)\n" "$(basename "$f")"
        fi
    done
    echo ""
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "Server:      🟢 RUNNING  (http://localhost:$PORT/app/index.html)"
    else
        echo "Server:      ⚪ stopped  (start with: ./dashboard.sh)"
    fi
}

# ----------------------------------------------------------------------- dispatch
case "${1:-}" in
    ""|start)  start_server ;;
    stop)      stop_server ;;
    restart)   stop_server; start_server ;;
    status)    do_status ;;
    *)
        echo "Usage: $0 [start|stop|restart|status]"
        echo ""
        echo "  (no args)  Arranca server + abre browser"
        echo "  start      Igual que el default"
        echo "  stop       Detiene el server"
        echo "  restart    stop + start"
        echo "  status     Estado del server e inventario de datos"
        echo ""
        echo "Para actualizar datos, cambiar de cuenta o meter el código MFA,"
        echo "abre el dashboard y usa la UI (botón ⟳ Update / ⚙️ Switch account)."
        exit 1
        ;;
esac
