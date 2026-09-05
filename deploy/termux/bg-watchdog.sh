#!/data/data/com.termux/files/usr/bin/bash
# bg-watchdog for sealed-inbox — manage the three long-running pieces on
# a Termux phone (or any box without a service manager):
#
#   watcher   — python -m src.watcher   (IMAP IDLE → pipeline)
#   dashboard — python -m src.dashboard (local web UI)
#   tunnel    — cloudflared quick tunnel to the dashboard
#
# Usage: bg-watchdog {start|stop|status|restart|log}  [watcher|dashboard|tunnel]
#        bg-watchdog watchdog                          (URL-change → Telegram loop)
#
# Config: copy deploy/termux/env.example to ~/.config/secure-record/notify.env
# and fill in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / APP_DIR / PORT.
#
# Adapted from the production deployment; all credentials now live in
# env files, never in this script.
set -uo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/sealed-inbox}"
PORT="${PORT:-8086}"
ENV_FILE="${ENV_FILE:-$HOME/.config/secure-record/notify.env}"
RUN_DIR="$APP_DIR/data"
LOG_DIR="$RUN_DIR"

WATCHER_LOG="$LOG_DIR/watcher.log"
DASH_LOG="$LOG_DIR/dashboard.log"
CF_LOG="$LOG_DIR/cloudflared.log"
URL_STATE="$LOG_DIR/tunnel_url_state.txt"
[ -d "$RUN_DIR" ] || mkdir -p "$RUN_DIR"

# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

notify_tg() {
    local msg="$1"
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d text="$msg" > /dev/null 2>&1 || true
}

latest_tunnel_url() {
    grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | tail -1 || true
}

report_tunnel_url_change() {
    local url prev
    url="$(latest_tunnel_url)"
    [ -n "$url" ] || return 0
    if [ ! -f "$URL_STATE" ]; then
        printf '%s\n' "$url" > "$URL_STATE"
        return 0
    fi
    prev="$(cat "$URL_STATE" 2>/dev/null || true)"
    if [ "$url" != "$prev" ]; then
        printf '%s\n' "$url" > "$URL_STATE"
        local link=""
        [ -f "$APP_DIR/deploy/termux/make_v6_link.py" ] && \
            link="$(python3 "$APP_DIR/deploy/termux/make_v6_link.py" \
                    --app-dir "$APP_DIR" --dash "$url" 2>/dev/null || true)"
        if [ -n "$link" ]; then
            notify_tg "🔁 sealed-inbox 面板地址更新: $url
🔗 一键入口(点这个): $link"
        else
            notify_tg "🔁 sealed-inbox 面板地址更新: $url"
        fi
        echo "Tunnel URL changed: $url (Telegram notified)"
    fi
}

start_watcher() {
    if pgrep -f "src\.watcher" > /dev/null 2>&1; then
        echo "watcher: already running"
        return 0
    fi
    termux-wake-lock 2>/dev/null || true
    (cd "$APP_DIR" && nohup python3 -m src.watcher >> "$WATCHER_LOG" 2>&1 &
     echo $! > "$RUN_DIR/watcher.pid")
    sleep 2 && status_proc watcher
}

start_dashboard() {
    if pgrep -f "src\.dashboard" > /dev/null 2>&1; then
        echo "dashboard: already running"
        return 0
    fi
    (cd "$APP_DIR" && nohup python3 -m src.dashboard >> "$DASH_LOG" 2>&1 &
     echo $! > "$RUN_DIR/dashboard.pid")
    sleep 1 && status_proc dashboard
}

start_tunnel() {
    if pgrep -f "cloudflared.*tunnel" > /dev/null 2>&1; then
        echo "tunnel: already running"
        return 0
    fi
    (nohup "$HOME/.local/bin/cloudflared-tunnel" >> "$CF_LOG" 2>&1 &
     echo $! > "$RUN_DIR/cloudflared.pid")
    sleep 6
    report_tunnel_url_change
    status_proc tunnel
}

stop_one() {
    local name="$1" pattern="$2" pidfile="$3"
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        pkill -f "$pattern"
        sleep 1
        echo "$name: stopped"
    else
        echo "$name: not running"
    fi
    rm -f "$pidfile"
}

status_proc() {
    local name="${1:-$2}" pattern="${2:-$1}"
    case "$name" in
        watcher)   pattern="src\.watcher" ;;
        dashboard) pattern="src\.dashboard" ;;
        tunnel)    pattern="cloudflared.*tunnel" ;;
    esac
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        local pid; pid=$(pgrep -f "$pattern" | head -1)
        local up; up=$(ps -o etime= -p "$pid" 2>/dev/null | xargs)
        echo "$name: running (PID $pid, uptime ${up:-?})"
    else
        echo "$name: stopped"
    fi
}

url_watch_loop() {
    echo "URL watch loop started (Telegram: $([ -n "${TELEGRAM_BOT_TOKEN:-}" ] && echo on || echo OFF))"
    while true; do
        report_tunnel_url_change
        sleep 300
    done
}

cmd="${1:-status}"; piece="${2:-all}"
case "$cmd" in
    start)
        [ "$piece" = "all" -o "$piece" = "watcher" ] && start_watcher
        [ "$piece" = "all" -o "$piece" = "dashboard" ] && start_dashboard
        [ "$piece" = "all" -o "$piece" = "tunnel" ] && start_tunnel
        ;;
    stop)
        stop_one watcher "src\.watcher" "$RUN_DIR/watcher.pid"
        stop_one dashboard "src\.dashboard" "$RUN_DIR/dashboard.pid"
        stop_one tunnel "cloudflared.*tunnel" "$RUN_DIR/cloudflared.pid"
        ;;
    restart) "$0" stop; sleep 2; "$0" start ;;
    status)
        status_proc watcher
        status_proc dashboard
        status_proc tunnel
        echo "tunnel URL: $(latest_tunnel_url || echo none)"
        ;;
    log) tail -n 40 "${2:-$WATCHER_LOG}" ;;
    watchdog) url_watch_loop ;;
    *) echo "usage: bg-watchdog {start|stop|status|restart|log|watchdog} [watcher|dashboard|tunnel]" ;;
esac
