#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# US Visa Slot Monitor — Termux one-shot setup
# Usage: bash setup_termux.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
ok()   { echo -e "${G}[ok]${N}  $*"; }
warn() { echo -e "${Y}[!]${N}   $*"; }
die()  { echo -e "${R}[err]${N} $*"; exit 1; }

echo ""
echo "════════════════════════════════════════════════════════"
echo "  US Visa Slot Monitor — Termux Setup"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 0. Termux permission ─────────────────────────────────────────────────────
if ! termux-setup-storage 2>/dev/null; then
    warn "termux-setup-storage not available — skipping"
fi

# ── 1. Core packages ─────────────────────────────────────────────────────────
ok "Updating package lists..."
pkg update -y -o Dpkg::Options::="--force-confold" 2>&1 | tail -3

ok "Installing core packages (python, chromium, tor)..."
pkg install -y python chromium tor 2>&1 | tail -5

# ── 2. X11 display for Chromium ──────────────────────────────────────────────
echo ""
echo "Chromium needs a display to run (required for Cloudflare bypass)."
echo "Options:"
echo "  a) Termux:X11  — best; install the companion APK from GitHub or F-Droid"
echo "  b) Xvfb        — virtual display (no GUI, works headless)"
echo ""
read -rp "Install Termux:X11 packages? [Y/n] " yn_x11
if [[ "${yn_x11,,}" != "n" ]]; then
    ok "Adding x11-repo..."
    pkg install -y x11-repo 2>&1 | tail -3
    ok "Installing Termux:X11..."
    pkg install -y termux-x11-nightly 2>&1 | tail -3 || \
        warn "termux-x11-nightly not found — try: pkg install termux-x11"
    ok "Termux:X11 installed. Open the Termux:X11 app on your phone before running."
fi

read -rp "Also install Xvfb as fallback? [y/N] " yn_xvfb
if [[ "${yn_xvfb,,}" == "y" ]]; then
    pkg install -y xorg-server-xvfb 2>&1 | tail -3 || warn "Xvfb not available in current repos"
fi

# ── 3. Python packages ───────────────────────────────────────────────────────
ok "Upgrading pip..."
pip install --upgrade pip --quiet

ok "Installing nodriver..."
pip install nodriver --quiet

read -rp "Install Anthropic SDK for cloud AI (optional)? [y/N] " yn_ai
if [[ "${yn_ai,,}" == "y" ]]; then
    pip install anthropic --quiet
    ok "Anthropic SDK installed."
else
    ok "Skipping — built-in keyword AI will handle everything."
fi

# ── 4. Termux:API for WiFi-based IP rotation ─────────────────────────────────
echo ""
read -rp "Install termux-api (enables WiFi-cycle IP rotation)? [y/N] " yn_api
if [[ "${yn_api,,}" == "y" ]]; then
    pkg install -y termux-api 2>&1 | tail -3
    ok "termux-api installed. Also install the 'Termux:API' companion APK."
fi

# ── 5. Create .env ──────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    ok ".env created from template."
    warn "Fill in your credentials now:  nano .env"
else
    ok ".env already exists — not overwriting."
fi

# ── 6. Make start script executable ─────────────────────────────────────────
chmod +x start.sh 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Quickstart:"
echo "  1. Fill in credentials:   nano .env"
echo "  2. Set your target date:  TARGET_DATE=2025-03-15 in .env"
echo "  3. Open Termux:X11 app on your phone (if installed)"
echo "  4. Launch:"
echo "       bash start.sh"
echo ""
echo "  For IP rotation (anti-rate-limit):"
echo "     Start Tor:  tor &"
echo "     Then set:   IP_ROTATION_METHOD=tor  in .env"
echo "════════════════════════════════════════════════════════"
