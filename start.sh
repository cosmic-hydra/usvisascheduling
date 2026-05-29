#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# US Visa Slot Monitor — universal launcher
# Works on: Termux (Android), Linux (desktop/server), macOS
#
# Usage:
#   bash start.sh                 # normal start
#   bash start.sh --no-display    # skip display setup (headless Linux servers)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'
ok()   { echo -e "${G}[start]${N} $*"; }
warn() { echo -e "${Y}[warn ]${N} $*"; }

# ── 0. Load .env ─────────────────────────────────────────────────────────────
if [ -f .env ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source .env
    set +o allexport
    ok ".env loaded"
else
    warn ".env not found — create it from .env.example first"
    exit 1
fi

# ── 1. Detect Termux ─────────────────────────────────────────────────────────
IS_TERMUX=false
if [ -d "/data/data/com.termux" ] || [ -n "${TERMUX_VERSION:-}" ]; then
    IS_TERMUX=true
fi

# ── 2. Set up display ────────────────────────────────────────────────────────
DISPLAY_PROC=""

if [ "${1:-}" != "--no-display" ] && [ -z "${DISPLAY:-}" ]; then
    if $IS_TERMUX; then
        # Try Termux:X11
        if command -v termux-x11 &>/dev/null; then
            ok "Starting Termux:X11 on :0..."
            termux-x11 :0 -xstartup "" &
            DISPLAY_PROC=$!
            export DISPLAY=:0
            sleep 2
            ok "DISPLAY=:0"
        else
            warn "Termux:X11 not found."
            warn "Install it: pkg install x11-repo && pkg install termux-x11-nightly"
            warn "Then open the Termux:X11 companion app."
            warn "Continuing without display — Cloudflare bypass may be less effective."
        fi
    else
        # Linux server — try Xvfb
        if command -v Xvfb &>/dev/null; then
            ok "Starting Xvfb on :99..."
            Xvfb :99 -screen 0 1280x1024x24 &>/dev/null &
            DISPLAY_PROC=$!
            export DISPLAY=:99
            sleep 0.5
            ok "DISPLAY=:99"
        else
            warn "Xvfb not found. Install: sudo apt install xvfb"
        fi
    fi
fi

# ── 3. Optionally start Tor ───────────────────────────────────────────────────
if [ "${IP_ROTATION_METHOD:-}" = "tor" ]; then
    if ! pgrep -x tor &>/dev/null; then
        if command -v tor &>/dev/null; then
            ok "Starting Tor daemon..."
            tor --SocksPort 9050 --ControlPort 9051 \
                --CookieAuthentication 0 \
                --Log "notice stderr" \
                --quiet &
            sleep 6
            ok "Tor started"
        else
            warn "Tor not installed. Install: pkg install tor (Termux) or apt install tor (Linux)"
        fi
    else
        ok "Tor already running"
    fi
fi

# ── 4. Check Python & dependencies ───────────────────────────────────────────
if ! command -v python &>/dev/null && ! command -v python3 &>/dev/null; then
    warn "Python not found. Install: pkg install python"
    exit 1
fi

PYTHON=$(command -v python || command -v python3)

ok "Python: $($PYTHON --version)"

if ! $PYTHON -c "import nodriver" &>/dev/null; then
    warn "nodriver not installed — installing now..."
    $PYTHON -m pip install nodriver --quiet
fi

# ── 5. Run the monitor ───────────────────────────────────────────────────────
ok "Starting US Visa Slot Monitor..."
echo ""

cleanup() {
    [ -n "$DISPLAY_PROC" ] && kill "$DISPLAY_PROC" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

$PYTHON usvisa_slot_monitor.py
