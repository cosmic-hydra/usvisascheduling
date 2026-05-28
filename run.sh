#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo "  US Visa Scheduling — Slot Monitor"
echo "============================================================"
echo

# ── Python check ──────────────────────────────────────────────
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "[ERROR] Python 3 is not installed."
    echo "        Ubuntu/Debian:  sudo apt install python3 python3-pip"
    echo "        macOS:          brew install python"
    exit 1
fi

PYTHON=$(command -v python3 2>/dev/null || command -v python)
PYVER=$("$PYTHON" --version 2>&1)
echo "[OK] $PYVER"

# ── Xvfb check (Linux headless only) ─────────────────────────
if [[ "$(uname)" == "Linux" ]] && [[ -z "${DISPLAY:-}" ]]; then
    if ! command -v Xvfb &>/dev/null; then
        echo
        echo "[WARN] Xvfb not found. Installing ..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y xvfb
        else
            echo "[WARN] Could not auto-install Xvfb. Run: sudo apt install xvfb"
        fi
    fi
fi

# ── Dependencies ──────────────────────────────────────────────
echo
echo "[INFO] Checking dependencies ..."
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r requirements.txt
echo "[OK] Dependencies ready"

# ── Chrome check ──────────────────────────────────────────────
CHROME_PATHS=(
    "/usr/bin/google-chrome-stable"
    "/usr/bin/google-chrome"
    "/usr/bin/chromium-browser"
    "/usr/bin/chromium"
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
CHROME_FOUND=0
for p in "${CHROME_PATHS[@]}"; do
    if [[ -f "$p" ]]; then
        CHROME_FOUND=1
        echo "[OK] Chrome found: $p"
        break
    fi
done

if [[ "$CHROME_FOUND" -eq 0 ]]; then
    echo
    echo "[WARN] Google Chrome not found in default locations."
    echo "       Install it or set CHROME_PATH in .env"
    echo
fi

# ── Run ───────────────────────────────────────────────────────
echo
echo "[INFO] Starting monitor ... (Ctrl+C to stop)"
echo
"$PYTHON" usvisa_slot_monitor.py
