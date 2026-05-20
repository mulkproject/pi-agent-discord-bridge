#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# pi-discord-manager.sh — Manage the Discord ↔ pi Bridge Bot
# ═══════════════════════════════════════════════════════════════
# Usage:
#   ./pi-discord-manager.sh           → Interactive menu (TUI)
#   ./pi-discord-manager.sh start     → Start the bot
#   ./pi-discord-manager.sh stop      → Stop the bot
#   ./pi-discord-manager.sh restart   → Restart the bot
#   ./pi-discord-manager.sh status    → Show status
#   ./pi-discord-manager.sh logs      → Tail live logs
#   ./pi-discord-manager.sh autostart → Install/remove systemd service
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SCRIPT="$BOT_DIR/bot.py"
LOG_FILE="/tmp/pi-discord-bot.log"
PID_FILE="/tmp/pi-discord-bot.pid"
SERVICE_NAME="pi-discord-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON="${PYTHON:-python3}"
DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# ── Helpers ────────────────────────────────────────────────────

_print_banner() {
    clear
    echo -e "${CYAN}${BOLD}"
    echo '  ╔══════════════════════════════════════════╗'
    echo '  ║     Discord ↔ pi Agent Bridge Manager    ║'
    echo '  ╚══════════════════════════════════════════╝'
    echo -e "${NC}"
}

_log() { echo -e "${GREEN}[✓]${NC} $1"; }
_warn() { echo -e "${YELLOW}[!]${NC} $1"; }
_error() { echo -e "${RED}[✗]${NC} $1"; }
_info() { echo -e "${BLUE}[i]${NC} $1"; }

_get_pid() {
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE"
    else
        echo ""
    fi
}

_is_running() {
    local pid
    pid="$(_get_pid)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    # Stale PID file — clean it
    if [[ -f "$PID_FILE" ]]; then
        rm -f "$PID_FILE"
    fi
    return 1
}

_get_uptime() {
    local pid start_time now elapsed
    pid="$(_get_pid)"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        echo "N/A"
        return
    fi
    start_time=$(ps -o lstart= -p "$pid" 2>/dev/null)
    echo "$start_time"
}

_check_token() {
    if [[ -z "$DISCORD_BOT_TOKEN" ]]; then
        # Try to read from config
        local token
        token=$(python3 -c "
import json
with open('$BOT_DIR/config.json') as f:
    cfg = json.load(f)
print(cfg.get('token', '') or '')
" 2>/dev/null || echo "")
        if [[ -n "$token" ]]; then
            export DISCORD_BOT_TOKEN="$token"
            return 0
        fi
        _error "Discord bot token not found!"
        echo "  Set DISCORD_BOT_TOKEN env var or add token to config.json"
        return 1
    fi
    return 0
}

# ── Actions ────────────────────────────────────────────────────

cmd_start() {
    _print_banner
    echo -e "${BOLD}Starting bot...${NC}\n"

    # Prevent multiple instances
    if _is_running; then
        local pid
        pid="$(_get_pid)"
        _warn "Bot is already running (PID: $pid)"
        echo "  Use '$0 restart' to restart, or '$0 stop' first."
        return 1
    fi

    # Check token
    _check_token || return 1

    # Check bot script exists
    if [[ ! -f "$BOT_SCRIPT" ]]; then
        _error "Bot script not found: $BOT_SCRIPT"
        return 1
    fi

    # Check dependencies
    if ! command -v "$PYTHON" &>/dev/null; then
        _error "Python not found: $PYTHON"
        return 1
    fi

    # Verify Python dependencies
    if ! python3 -c "import discord" 2>/dev/null; then
        _error "discord.py not installed!"
        echo "  Run: cd $BOT_DIR && pip install -r requirements.txt"
        return 1
    fi

    # Start the bot
    cd "$BOT_DIR"
    nohup "$PYTHON" "$BOT_SCRIPT" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    # Wait and verify
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
        _log "Bot started successfully (PID: $pid)"
        echo "  Logs: tail -f $LOG_FILE"
        echo "  Stop: $0 stop"
    else
        _error "Bot failed to start!"
        echo "  Check logs: cat $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

cmd_stop() {
    _print_banner
    echo -e "${BOLD}Stopping bot...${NC}\n"

    if ! _is_running; then
        _warn "Bot is not running."
        return 0
    fi

    local pid
    pid="$(_get_pid)"
    echo -n "  Stopping PID $pid..."

    # Graceful shutdown
    kill "$pid" 2>/dev/null

    # Wait for it to stop
    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [[ $waited -ge 10 ]]; then
            echo -n " (force kill)"
            kill -9 "$pid" 2>/dev/null
            break
        fi
        echo -n "."
    done
    echo ""

    # Kill any orphaned pi RPC processes
    pkill -f "pi --mode rpc" 2>/dev/null || true

    rm -f "$PID_FILE"
    _log "Bot stopped."
}

cmd_status() {
    _print_banner
    echo -e "${BOLD}Bot Status${NC}\n"

    if _is_running; then
        local pid uptime sessions
        pid="$(_get_pid)"
        uptime="$(_get_uptime)"
        
        # Get session count from bot
        sessions=$(grep -c "Creating new session" "$LOG_FILE" 2>/dev/null || echo "0")
        total_prompts=$(grep -c "Prompt from" "$LOG_FILE" 2>/dev/null || echo "0")

        echo -e "  ${GREEN}●${NC} ${BOLD}Running${NC}"
        echo -e "  PID:       ${CYAN}$pid${NC}"
        echo -e "  Started:   $uptime"
        echo -e "  Directory: ${DIM}$BOT_DIR${NC}"
        echo -e "  Sessions:  ${CYAN}$sessions${NC} total"
        echo -e "  Prompts:   ${CYAN}$total_prompts${NC} total"
        echo ""

        # Check pi availability
        if command -v pi &>/dev/null; then
            echo -e "  ${GREEN}✓${NC} pi CLI available: $(which pi)"
        else
            echo -e "  ${RED}✗${NC} pi CLI not found in PATH"
        fi

        # Check memory usage
        local mem
        mem=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')
        echo -e "  Memory:    ${mem:-N/A}"

        # Check for pi RPC processes
        local rpc_count
        rpc_count=$(ps aux | grep "pi --mode rpc" | grep -v grep | wc -l)
        if [[ "$rpc_count" -gt 0 ]]; then
            echo -e "  Active pi RPC subprocesses: ${CYAN}$rpc_count${NC}"
        fi

        # Show last few log entries
        echo ""
        echo -e "${DIM}Last 3 log entries:${NC}"
        tail -3 "$LOG_FILE" 2>/dev/null | sed 's/^/  /'
    else
        echo -e "  ${RED}●${NC} ${BOLD}Stopped${NC}"
    fi

    # Autostart status
    echo ""
    if [[ -f "$SERVICE_FILE" ]]; then
        echo -e "  Autostart: ${GREEN}enabled${NC} (systemd: $SERVICE_NAME)"
        systemctl is-enabled "$SERVICE_NAME" &>/dev/null && \
            echo -e "  Service:   ${GREEN}active${NC}" || \
            echo -e "  Service:   ${YELLOW}inactive${NC}"
    else
        echo -e "  Autostart: ${DIM}not configured${NC}"
    fi
}

cmd_logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        _warn "No log file yet. Start the bot first."
        return 1
    fi
    echo -e "${CYAN}Tailing logs... (Ctrl+C to stop)${NC}\n"
    tail -f "$LOG_FILE"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_autostart() {
    _print_banner
    echo -e "${BOLD}Autostart Setup${NC}\n"

    if [[ $EUID -ne 0 ]]; then
        _warn "Installing systemd service requires root (sudo)."
        echo "  Run: sudo $0 autostart"
        echo "  Or install manually (see below)."
        echo ""
        echo "  Manual crontab alternative (no root needed):"
        echo "    crontab -e"
        echo "    Add: @reboot cd $BOT_DIR && nohup python3 bot.py > $LOG_FILE 2>&1 &"
        return 1
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        echo -e "${YELLOW}Autostart is already configured.${NC}"
        echo "  Service: $SERVICE_NAME"
        echo "  File:    $SERVICE_FILE"
        echo ""
        echo -n "  Remove it? [y/N] "
        read -r answer
        if [[ "$answer" =~ ^[Yy] ]]; then
            systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            systemctl disable "$SERVICE_NAME" 2>/dev/null || true
            rm -f "$SERVICE_FILE"
            systemctl daemon-reload
            _log "Autostart removed."
        fi
        return 0
    fi

    # Check token
    _check_token || return 1

    # Create systemd service
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Discord ↔ pi Agent Bridge Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=$PYTHON $BOT_SCRIPT
Restart=on-failure
RestartSec=5
Environment=DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"

    _log "Autostart installed and bot started via systemd."
    echo "  Service: $SERVICE_NAME"
    echo "  Status:  systemctl status $SERVICE_NAME"
    echo "  Logs:    journalctl -u $SERVICE_NAME -f"
}

# ── TUI Menu ───────────────────────────────────────────────────

show_menu() {
    while true; do
        _print_banner

        # Status line
        if _is_running; then
            local pid
            pid="$(_get_pid)"
            echo -e "  ${GREEN}● Bot is RUNNING${NC} (PID: $pid)"
        else
            echo -e "  ${RED}● Bot is STOPPED${NC}"
        fi
        echo ""

        # Menu
        echo -e "  ${BOLD}Management:${NC}"
        echo -e "    ${CYAN}1${NC})  Start Bot"
        echo -e "    ${CYAN}2${NC})  Stop Bot"
        echo -e "    ${CYAN}3${NC})  Restart Bot"
        echo -e "    ${CYAN}4${NC})  Show Status"
        echo ""
        echo -e "  ${BOLD}Monitoring:${NC}"
        echo -e "    ${CYAN}5${NC})  View Live Logs"
        echo -e "    ${CYAN}8${NC})  List Active Sessions"
        echo ""
        echo -e "  ${BOLD}Setup:${NC}"
        echo -e "    ${CYAN}6${NC})  Configure Autostart (systemd)"
        echo -e "    ${CYAN}7${NC})  Check Dependencies"
        echo ""
        echo -e "  ${BOLD}Other:${NC}"
        echo -e "    ${CYAN}0${NC})  Exit"
        echo ""
        echo -n "  Select option [0-8]: "
        read -r opt

        case "$opt" in
            1) cmd_start;;
            2) cmd_stop;;
            3) cmd_restart;;
            4) cmd_status;;
            5) cmd_logs;;
            6) cmd_autostart;;
            7) cmd_check_deps;;
            8) cmd_sessions;;
            0) echo ""; _log "Goodbye!"; exit 0;;
            *) _warn "Invalid option: $opt";;
        esac

        echo ""
        echo -n "  Press Enter to continue..."
        read -r
    done
}

cmd_sessions() {
    _print_banner
    echo -e "${BOLD}Active pi Sessions${NC}
"

    if ! _is_running; then
        _warn "Bot is not running."
        return 1
    fi

    # Find pi RPC processes
    local rpc_count=0
    while IFS= read -r line; do
        if [[ -n "$line" ]]; then
            rpc_count=$((rpc_count + 1))
            # Extract PID and uptime
            local pid=$(echo "$line" | awk '{print $2}')
            local etime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
            local mem=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0f MB", $1/1024}')
            echo -e "  ${CYAN}●${NC} PID: ${CYAN}$pid${NC} | Uptime: ${etime:-?} | Mem: ${mem:-?}"
        fi
    done < <(ps aux | grep "pi --mode rpc" | grep -v grep)

    if [[ "$rpc_count" -eq 0 ]]; then
        echo -e "  ${DIM}No active pi sessions.${NC}"
        echo ""
        echo "  Sessions are created when you send a message in a thread."
        echo "  They auto-cleanup after 30 minutes of inactivity."
    else
        echo ""
        echo -e "  Total: ${CYAN}$rpc_count${NC} active pi process(es)"
        echo ""
        echo -e "  ${DIM}To manage sessions from Discord:${NC}"
        echo -e "    ${BOLD}!sessions${NC}       — List sessions"
        echo -e "    ${BOLD}!session-kill <id>${NC} — Remove a session"
    fi
}

cmd_check_deps() {
    _print_banner
    echo -e "${BOLD}Checking Dependencies${NC}\n"

    # Python
    if command -v python3 &>/dev/null; then
        local ver
        ver=$(python3 --version 2>&1)
        echo -e "  ${GREEN}✓${NC} $ver"
    else
        echo -e "  ${RED}✗${NC} python3 not found"
    fi

    # discord.py
    if python3 -c "import discord; print(discord.__version__)" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} discord.py found"
    else
        echo -e "  ${RED}✗${NC} discord.py not installed"
        echo "     Run: cd $BOT_DIR && pip install -r requirements.txt"
    fi

    # pi
    if command -v pi &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} pi CLI: $(which pi)"
    else
        echo -e "  ${RED}✗${NC} pi CLI not found in PATH"
    fi

    # ImageMagick
    if command -v identify &>/dev/null; then
        echo -e "  ${GREEN}✓${NC} ImageMagick (identify) available"
    else
        echo -e "  ${YELLOW}⚠${NC} ImageMagick not installed (optional, for image metadata)"
    fi

    # Token
    if _check_token 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} Discord bot token configured"
    else
        echo -e "  ${RED}✗${NC} Discord bot token missing"
    fi

    # Config
    if [[ -f "$BOT_DIR/config.json" ]]; then
        echo -e "  ${GREEN}✓${NC} Config file: $BOT_DIR/config.json"
    else
        echo -e "  ${RED}✗${NC} Config file missing"
    fi
}

# ── CLI Routing ────────────────────────────────────────────────

main() {
    case "${1:-}" in
        start)    cmd_start;;
        stop)     cmd_stop;;
        restart)  cmd_restart;;
        status)   cmd_status;;
        logs)     cmd_logs;;
        autostart) cmd_autostart;;
        deps)     cmd_check_deps;;
        sessions) cmd_sessions;;
        help|--help|-h)
            echo "Usage: $0 [command]"
            echo ""
            echo "Commands:"
            echo "  (no args)  → Interactive TUI menu"
            echo "  start      → Start the bot"
            echo "  stop       → Stop the bot"
            echo "  restart    → Restart the bot"
            echo "  status     → Show bot status"
            echo "  logs       → Tail live logs"
            echo "  sessions   → List active pi sessions"
            echo "  autostart  → Install/remove systemd autostart"
            echo "  deps       → Check dependencies"
            ;;
        *)
            show_menu
            ;;
    esac
}

main "$@"
