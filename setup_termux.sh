#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# US Visa Slot Monitor — Termux one-shot setup
# Run once: bash setup_termux.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn ]${NC} $*"; }

info "Updating package lists ..."
pkg update -y

info "Installing core packages ..."
pkg install -y python chromium tor

info "Installing Python dependencies ..."
pip install --upgrade pip
pip install nodriver

# Optional: Anthropic SDK (API key required — skip if you don't have one)
read -rp "Install Anthropic SDK for cloud AI question matching? [y/N] " yn
if [[ "${yn,,}" == "y" ]]; then
    pip install anthropic
    info "Anthropic SDK installed."
else
    info "Skipping Anthropic SDK — built-in keyword AI will be used."
fi

# Optional: Termux:API for WiFi-based IP rotation
info ""
info "For WiFi-based IP rotation you need the Termux:API companion app."
info "Install it from F-Droid, then run: pkg install termux-api"
read -rp "Install termux-api package now? [y/N] " yn2
if [[ "${yn2,,}" == "y" ]]; then
    pkg install -y termux-api
    info "termux-api installed. Make sure the Termux:API app is also installed."
fi

# Create .env from example if it doesn't already exist
if [ ! -f .env ]; then
    cp .env.example .env
    info ".env created from template — fill in your credentials before running."
else
    info ".env already exists — skipping copy."
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit .env and fill in your credentials:"
echo "     nano .env"
echo ""
echo "  2. (Optional) Start Tor for IP rotation:"
echo "     tor &"
echo "     # then set IP_ROTATION_METHOD=tor in .env"
echo ""
echo "  3. Run the monitor:"
echo "     python usvisa_slot_monitor.py"
echo "════════════════════════════════════════════════════════"
