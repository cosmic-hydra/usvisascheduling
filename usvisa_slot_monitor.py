"""
US Visa Scheduling slot monitor

Flow:
  1. Opens https://www.usvisascheduling.com/en-US with an undetected Chrome (nodriver)
  2. Handles Cloudflare Turnstile ("Am I Human" checkbox — auto-solves invisible;
     clicks the checkbox for the managed/visible widget)
  3. Detects & survives Cloudflare Waiting Room / queue pages
     (keeps session cookie alive, refreshes, prints queue info)
  4. Logs in with credentials from environment variables
  5. Answers security questions
  6. Navigates to Reschedule Appointment page
  7. Scans EVERY OFC/consulate option in the dropdown and reads ALL calendar
     months for each post to find the earliest available date
  8. Sends a formatted HTML report to Telegram with the nearest slot

Required environment variables:
  USVISA_USERNAME       login e-mail
  USVISA_PASSWORD       login password
  USVISA_Q1             security question answer 1
  USVISA_Q2             security question answer 2
  USVISA_Q3             security question answer 3

Optional environment variables:
  USVISA_POSTS          comma-separated post labels/values to check
                        (default: all options found in the dropdown)
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  AUTO_BOOK             true / false  (default: false)
  CHROME_PATH           path to Chrome executable
  TS_PROFILE_DIR        persistent Chrome profile directory
  MAX_WAIT_MINUTES      max minutes to wait in CF Waiting Room (default: 30)
  CALENDAR_MONTHS       how many months ahead to scan per post  (default: 12)
  RESCHEDULE_LINK_TEXT  link text for the reschedule page
                        (default: "Reschedule Appointment")
  BOOK_DATE_SELECTOR    CSS selector — date input  (required when AUTO_BOOK=true)
  BOOK_TIME_SELECTOR    CSS selector — time input  (required when AUTO_BOOK=true)
  BOOK_SUBMIT_SELECTOR  CSS selector — submit btn  (required when AUTO_BOOK=true)

Install:
  pip install nodriver
  # Linux headless only:
  sudo apt install xvfb

Run:
  python usvisa_slot_monitor.py
"""

import asyncio
import getpass
import json
import os
import platform
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple
from urllib import parse, request

import nodriver as uc

try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False


BASE_URL = "https://www.usvisascheduling.com/en-US"


# ─── .env loader (no extra dependencies) ─────────────────────────────────────

def _load_dotenv() -> None:
    """
    Load a .env file from the same directory as this script into os.environ.
    Lines starting with # are ignored.  Values already set in the environment
    take precedence (so real env vars always win over the file).
    """
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(dotenv_path):
        return
    with open(dotenv_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            key = key.strip()
            value = raw_value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# ─── env helpers ──────────────────────────────────────────────────────────────

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def require(name: str) -> str:
    v = env(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


# ─── chrome / display / platform setup ────────────────────────────────────────

def _is_termux() -> bool:
    """Return True when running inside Termux on Android."""
    return bool(
        os.environ.get("TERMUX_VERSION")
        or os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
        or os.path.isdir("/data/data/com.termux")
    )


def _find_chrome() -> str:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]

    # Termux (Android) — Chromium installed via `pkg install chromium`
    if _is_termux():
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        for name in ("chromium", "chromium-browser", "google-chrome"):
            p = os.path.join(prefix, "bin", name)
            if os.path.isfile(p):
                return p

    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "Chrome not found.\n"
        "  Termux  : pkg install chromium\n"
        "  Linux   : sudo apt install chromium-browser\n"
        "  Override: set CHROME_PATH in .env"
    )


def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if _is_termux():
        home = os.environ.get("HOME", "/data/data/com.termux/files/home")
        return os.path.join(home, ".usvisa_profile")
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or r"C:\Temp"
    else:
        base = "/tmp"
    return os.path.join(base, "usvisa_profile")


def _get_chrome_flags() -> List[str]:
    """Return platform-appropriate extra Chrome flags."""
    flags: List[str] = ["--no-sandbox", "--disable-dev-shm-usage"]
    if _is_termux():
        flags += [
            "--disable-gpu",
            "--window-size=1280,900",
            "--disable-features=VizDisplayCompositor",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ]
    return flags


def _setup_display() -> Optional[subprocess.Popen]:
    """
    Ensure a display is available for Chrome:
      • Termux — try Termux:X11 first, then VNC
      • Headless Linux — start Xvfb
      • Windows / already-set DISPLAY — nothing needed
    """
    if os.environ.get("DISPLAY"):
        return None

    if _is_termux():
        # Try Termux:X11 (install: pkg install x11-repo && pkg install termux-x11-nightly)
        try:
            proc = subprocess.Popen(
                ["termux-x11", ":0", "-xstartup", ""],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":0"
            time.sleep(2)
            _log("Termux:X11 started on :0")
            return proc
        except FileNotFoundError:
            pass

        # Fallback: Xvfb if available on Termux
        try:
            proc = subprocess.Popen(
                ["Xvfb", ":1", "-screen", "0", "1280x900x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":1"
            time.sleep(1)
            _log("Xvfb started on :1 (Termux fallback)")
            return proc
        except FileNotFoundError:
            _log(
                "WARNING: No display server found on Termux.\n"
                "  Install Termux:X11:\n"
                "    pkg install x11-repo\n"
                "    pkg install termux-x11-nightly\n"
                "  Then open the Termux:X11 companion app before running."
            )
            return None

    if platform.system() != "Linux":
        return None

    try:
        proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x1024x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        time.sleep(0.5)
        _log("Xvfb started on :99")
        return proc
    except FileNotFoundError:
        _log("Xvfb not found — install with: sudo apt install xvfb")
        return None


# Keep the old name as an alias so __main__ block still works.
_start_xvfb = _setup_display


# ─── IP rotation ─────────────────────────────────────────────────────────────

class IPRotator:
    """
    Changes the public IP to defeat Cloudflare rate-limiting.

    Supported methods (set via IP_ROTATION_METHOD in .env):
      tor       — rotate Tor circuit via NEWNYM (default; install with pkg install tor)
      airplane  — toggle Android airplane mode (requires root)
      wifi      — cycle WiFi adapter
      none      — disable rotation (just wait)

    On rate-limit the rotator tries the configured method, verifies the IP
    actually changed, and logs the result.
    """

    METHOD_TOR      = "tor"
    METHOD_AIRPLANE = "airplane"
    METHOD_WIFI     = "wifi"
    METHOD_NONE     = "none"

    def __init__(self) -> None:
        self.method    = env("IP_ROTATION_METHOD", "tor").lower()
        self._old_ip   = ""
        self._tor_proc: Optional[subprocess.Popen] = None

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background services needed by the chosen method."""
        if self.method == self.METHOD_TOR:
            self._ensure_tor()

    def get_browser_args(self) -> List[str]:
        """Extra Chrome flags required for the chosen method."""
        if self.method == self.METHOD_TOR and self._tor_running():
            return ["--proxy-server=socks5://127.0.0.1:9050"]
        return []

    def rotate(self) -> bool:
        """Execute one IP-rotation cycle. Returns True when IP changed."""
        _log(f"[IPRotator] rotating via method={self.method!r}")
        self._old_ip = self._get_ip()
        _log(f"[IPRotator] current IP: {self._old_ip}")

        ok = False
        if self.method == self.METHOD_TOR:
            ok = self._rotate_tor()
        elif self.method == self.METHOD_AIRPLANE:
            ok = self._rotate_airplane()
        elif self.method == self.METHOD_WIFI:
            ok = self._rotate_wifi()
        else:
            _log("[IPRotator] method=none — skipping rotation")
            return False

        new_ip = self._get_ip()
        changed = new_ip != self._old_ip and new_ip not in ("", "unknown")
        _log(f"[IPRotator] new IP: {new_ip} | changed={changed}")
        return changed

    # ── Tor ───────────────────────────────────────────────────────────────────

    def _ensure_tor(self) -> None:
        if self._tor_running():
            return
        try:
            self._tor_proc = subprocess.Popen(
                ["tor",
                 "--SocksPort",   "9050",
                 "--ControlPort", "9051",
                 "--CookieAuthentication", "0",
                 "--Log", "notice stderr"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _log("[IPRotator] Tor daemon started")
            time.sleep(8)  # allow bootstrap
        except FileNotFoundError:
            _log("[IPRotator] Tor not found — install with: pkg install tor")

    def _tor_running(self) -> bool:
        import socket as _socket
        try:
            with _socket.create_connection(("127.0.0.1", 9050), timeout=1):
                return True
        except OSError:
            return False

    def _rotate_tor(self) -> bool:
        """Request a new Tor exit circuit (NEWNYM)."""
        import socket as _socket
        self._ensure_tor()
        try:
            with _socket.create_connection(("127.0.0.1", 9051), timeout=5) as s:
                s.sendall(b'AUTHENTICATE ""\r\nSIGNAL NEWNYM\r\nQUIT\r\n')
                resp = s.recv(256)
            ok = b"250" in resp
            if ok:
                time.sleep(5)  # new circuit takes a moment to become active
            return ok
        except Exception as exc:
            _log(f"[IPRotator] Tor NEWNYM failed: {exc}")
            return False

    # ── Airplane mode (Android root) ─────────────────────────────────────────

    def _rotate_airplane(self) -> bool:
        try:
            subprocess.run(
                ["su", "-c",
                 "settings put global airplane_mode_on 1; "
                 "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true"],
                timeout=6, check=True, capture_output=True,
            )
            time.sleep(4)
            subprocess.run(
                ["su", "-c",
                 "settings put global airplane_mode_on 0; "
                 "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false"],
                timeout=6, check=True, capture_output=True,
            )
            time.sleep(10)  # wait for cellular reconnection
            return True
        except Exception as exc:
            _log(f"[IPRotator] airplane-mode toggle failed: {exc}")
            return False

    # ── WiFi cycle ────────────────────────────────────────────────────────────

    def _rotate_wifi(self) -> bool:
        # Try Termux:API first, fall back to `svc` (root).
        for cmd_off, cmd_on in [
            (["termux-wifi-enable", "false"], ["termux-wifi-enable", "true"]),
            (["su", "-c", "svc wifi disable"], ["su", "-c", "svc wifi enable"]),
        ]:
            try:
                subprocess.run(cmd_off, timeout=5, capture_output=True)
                time.sleep(3)
                subprocess.run(cmd_on, timeout=5, capture_output=True)
                time.sleep(8)
                return True
            except Exception:
                continue
        _log("[IPRotator] WiFi cycle failed — no suitable command found")
        return False

    # ── IP probe ─────────────────────────────────────────────────────────────

    def _get_ip(self) -> str:
        for url in ["https://api.ipify.org", "https://icanhazip.com"]:
            try:
                with request.urlopen(url, timeout=8) as r:
                    return r.read().decode().strip()
            except Exception:
                continue
        return "unknown"


# Module-level singleton — initialised in run().
_ip_rotator: Optional[IPRotator] = None


# ─── data ─────────────────────────────────────────────────────────────────────

@dataclass
class SlotResult:
    post: str
    earliest: Optional[str] = None
    all_dates: List[str] = field(default_factory=list)
    status: str = "pending"


# ─── Cloudflare: page helpers ─────────────────────────────────────────────────

def _log(msg: str) -> None:
    """Timestamped log line."""
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def _page_text(tab) -> str:
    """Return lowercased body text (safe — returns '' on any error)."""
    try:
        raw = await tab.evaluate(
            "(document.body && document.body.innerText)"
            " ? document.body.innerText.toLowerCase() : ''"
        )
        return raw or ""
    except Exception:
        return ""


async def _page_url(tab) -> str:
    try:
        return await tab.evaluate("window.location.href") or ""
    except Exception:
        return ""


async def _page_title(tab) -> str:
    try:
        return await tab.evaluate("document.title") or ""
    except Exception:
        return ""


async def _log_page_state(tab, label: str = "") -> None:
    """Print current URL, title, and first 120 chars of body text."""
    url   = await _page_url(tab)
    title = await _page_title(tab)
    body  = await _page_text(tab)
    snippet = " ".join(body.split())[:120]
    prefix = f"[page{' — ' + label if label else ''}]"
    _log(f"{prefix} URL   : {url}")
    _log(f"{prefix} Title : {title}")
    _log(f"{prefix} Body  : {snippet}")


async def _apply_stealth(tab) -> None:
    """
    Patch the page JS environment to hide browser-automation signals that
    Cloudflare's fingerprinting checks look for.
    Called once after each navigation to a new page.
    """
    try:
        await tab.evaluate("""
            (() => {
                // 1. Remove the navigator.webdriver flag
                try {
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                } catch(e) {}

                // 2. Add a realistic window.chrome object (headless Chrome lacks it)
                if (!window.chrome) {
                    window.chrome = {
                        app: {isInstalled: false},
                        runtime: {
                            onMessage: {addListener: () => {}},
                            connect: () => ({onMessage: {addListener: () => {}},
                                             onDisconnect: {addListener: () => {}},
                                             postMessage: () => {}})
                        }
                    };
                }

                // 3. Fix navigator.permissions.query (headless throws on 'notifications')
                if (navigator.permissions && navigator.permissions.query) {
                    const _origQuery = navigator.permissions.query.bind(navigator.permissions);
                    navigator.permissions.query = (p) =>
                        p.name === 'notifications'
                            ? Promise.resolve({state: Notification.permission})
                            : _origQuery(p);
                }

                // 4. Realistic plugin list (headless has 0 plugins)
                if (navigator.plugins.length === 0) {
                    try {
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => {
                                const arr = [
                                    {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
                                    {name:'Chrome PDF Viewer',  filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                                    {name:'Native Client',      filename:'internal-nacl-plugin'},
                                ];
                                arr.length = arr.length;  // make it look like a PluginArray
                                return arr;
                            }
                        });
                    } catch(e) {}
                }

                // 5. Consistent language list
                try {
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en']
                    });
                } catch(e) {}

                // 6. Hide headless from User-Agent string
                try {
                    Object.defineProperty(navigator, 'userAgent', {
                        get: () => navigator.userAgent.replace('HeadlessChrome', 'Chrome')
                    });
                } catch(e) {}
            })()
        """)
    except Exception as exc:
        _log(f"stealth patch error (non-fatal): {exc}")


# ─── Cloudflare: Waiting Room ─────────────────────────────────────────────────

async def _is_waiting_room(tab) -> bool:
    text = await _page_text(tab)
    url = (await _page_url(tab)).lower()
    return any([
        "waiting room"         in text,
        "you are in the queue" in text,
        "queue position"       in text,
        "estimated wait"       in text,
        "waitingroom"          in url,
        "waiting-room"         in url,
    ])


async def handle_waiting_room(tab) -> None:
    """
    Block until the Cloudflare Waiting Room releases us (or timeout).

    The browser holds the session cookie so Cloudflare keeps our queue
    position even while we sleep between checks.
    """
    max_minutes = int(env("MAX_WAIT_MINUTES", "30"))
    if not await _is_waiting_room(tab):
        return

    _log(f"Cloudflare Waiting Room detected — "
         f"waiting up to {max_minutes} min (checking every 20 s) ...")
    await _log_page_state(tab, "waiting room")

    deadline = asyncio.get_event_loop().time() + max_minutes * 60

    while asyncio.get_event_loop().time() < deadline:
        # Print any human-readable queue information shown on the page
        try:
            info = await tab.evaluate("""
                (() => {
                    for (const sel of [
                        '#cf-content', '.body-text', '#cf-spinner-allow-1min',
                        '[data-translate]', '.lds-ring + p',
                    ]) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim())
                            return el.innerText.trim().slice(0, 200);
                    }
                    return '';
                })()
            """)
            if info:
                print(f"[monitor] queue: {info}")
        except Exception:
            pass

        await asyncio.sleep(20)

        if not await _is_waiting_room(tab):
            _log("Waiting Room passed — continuing")
            return

    raise TimeoutError(
        f"Still in Cloudflare Waiting Room after {max_minutes} minutes. "
        "Increase MAX_WAIT_MINUTES or try again later."
    )


# ─── Cloudflare: rate-limit handling ─────────────────────────────────────────

async def _is_rate_limited(tab) -> bool:
    """Return True when Cloudflare is rate-limiting (HTTP 429 / CF error 1015)."""
    text = await _page_text(tab)
    title = (await _page_title(tab)).lower()
    return any(phrase in text for phrase in [
        "too many requests",
        "rate limit",
        "error 429",
        "error 1015",
        "you have been temporarily blocked",
        "access denied",
    ]) or "429" in title or "1015" in title


async def _handle_rate_limit(tab) -> None:
    """
    Defeat Cloudflare rate-limiting by rotating the IP and reloading.
    Falls back to exponential back-off (30 → 60 → 120 → 240 → 300 s) if the
    IP rotation method is 'none' or rotation fails.
    """
    global _ip_rotator
    for attempt in range(5):
        # Rotate IP first — most effective mitigation.
        if _ip_rotator and _ip_rotator.method != IPRotator.METHOD_NONE:
            _log(f"Rate limited — rotating IP (attempt {attempt + 1}/5) ...")
            rotated = _ip_rotator.rotate()
            if rotated:
                _log("IP rotated — reloading page ...")
                try:
                    await tab.get(BASE_URL)
                    await asyncio.sleep(3.0)
                except Exception:
                    pass
                if not await _is_rate_limited(tab):
                    _log("Rate limit lifted after IP rotation")
                    return
                await asyncio.sleep(10)
                continue

        # IP rotation unavailable or failed — fall back to timed back-off.
        backoff = min(300, 30 * (2 ** attempt)) + random.uniform(0, 20)
        _log(f"Rate limited — backing off {backoff:.0f}s (attempt {attempt + 1}/5)")
        await asyncio.sleep(backoff)
        try:
            await tab.get(BASE_URL)
            await asyncio.sleep(3.0)
        except Exception:
            pass
        if not await _is_rate_limited(tab):
            _log("Rate limit lifted")
            return

    _log("WARNING: still rate-limited after 5 attempts — continuing anyway")


# ─── Cloudflare: AI challenge classifier ─────────────────────────────────────

def _rule_classify_cf(url: str, title: str, body: str) -> dict:
    """
    Built-in rule-based Cloudflare challenge classifier (no API key needed).
    Returns {"type": str, "action": str}.
    """
    u, t, b = url.lower(), title.lower(), body.lower()

    # Rate-limited
    if any(k in b for k in ["too many requests", "error 1015", "error 429",
                             "rate limit", "slow down"]):
        return {"type": "rate_limit", "action": "backoff"}

    # Outright block
    if ("access denied" in b or "blocked" in b) and "cloudflare" in b:
        return {"type": "block", "action": "abort"}

    # Waiting room
    if any(k in b for k in ["waiting room", "you are in the queue",
                             "queue position", "estimated wait"]):
        return {"type": "waiting_room", "action": "wait_queue"}

    # Turnstile / security verification
    if (any(k in b for k in ["verify you are human", "security verification",
                              "performing security verification", "malicious"])
            or "challenges.cloudflare.com" in u
            or "/cdn-cgi/challenge" in u):
        return {"type": "turnstile", "action": "click_checkbox"}

    # JS check ("Just a moment…")
    if any(k in t for k in ["just a moment", "attention required",
                             "one more step", "please wait"]):
        return {"type": "js_check", "action": "wait_js"}

    return {"type": "none", "action": "proceed"}


async def _ai_classify_cf(tab) -> dict:
    """
    Classify the current Cloudflare challenge.
    Priority: Claude API (if key set) → built-in rule classifier.
    Returns {"type": str, "action": str}.
    """
    url   = await _page_url(tab)
    title = await _page_title(tab)
    body  = (await _page_text(tab))[:1500]

    # Try Claude API.
    if _HAS_ANTHROPIC:
        api_key = env("ANTHROPIC_API_KEY")
        if api_key:
            try:
                client = _anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Classify the Cloudflare protection on this page. "
                            "Reply ONLY with compact JSON.\n\n"
                            f"URL: {url}\nTitle: {title}\nBody: {body}\n\n"
                            "Types: none, turnstile, js_check, waiting_room, "
                            "rate_limit, block\n"
                            "Actions: proceed, click_checkbox, wait_js, "
                            "wait_queue, backoff, abort\n\n"
                            'Format: {"type":"...","action":"..."}'
                        ),
                    }],
                )
                result = json.loads(msg.content[0].text.strip())
                _log(f"Claude CF analysis: {result}")
                return result
            except Exception as exc:
                _log(f"Claude CF classify failed ({exc}) — falling back to rule AI")

    # Built-in rule classifier (no API key needed).
    result = _rule_classify_cf(url, title, body)
    _log(f"Rule-based CF analysis: {result}")
    return result


# ─── Cloudflare: Turnstile ────────────────────────────────────────────────────

async def _cf_iframe_rect(tab) -> Optional[dict]:
    raw = await tab.evaluate("""
        JSON.stringify((() => {
            const looksLikeCfFrame = (f) => {
                const src = (f.src || '').toLowerCase();
                const title = (f.title || '').toLowerCase();
                const id = (f.id || '').toLowerCase();
                const name = (f.name || '').toLowerCase();
                const attrs = [src, title, id, name].join(' ');
                return attrs.includes('challenges.cloudflare.com')
                    || attrs.includes('/cdn-cgi/challenge')
                    || attrs.includes('turnstile')
                    || attrs.includes('cloudflare');
            };

            for (const f of document.querySelectorAll('iframe')) {
                if (!looksLikeCfFrame(f)) continue;
                const r = f.getBoundingClientRect();
                if (r.width > 20 && r.height > 10)
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
            }

            // Some challenge variants use transient/opaque iframe attributes.
            // On security verification pages, fall back to the first visible iframe.
            const body = ((document.body && document.body.innerText) || '').toLowerCase();
            const looksLikeSecurityPage = body.includes('security verification')
                || body.includes('verify you are human')
                || body.includes('checking your browser')
                || body.includes('malicious');

            if (looksLikeSecurityPage) {
                for (const f of document.querySelectorAll('iframe')) {
                    const r = f.getBoundingClientRect();
                    if (r.width > 100 && r.height > 40)
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                }
            }

            return null;
        })())
    """)
    if raw and raw != "null":
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


async def _has_turnstile_iframe(tab) -> bool:
    """Return True when a Cloudflare challenge iframe is present."""
    try:
        return bool(await tab.evaluate(
            """
            (() => {
                const looksLikeCfFrame = (f) => {
                    const src = (f.src || '').toLowerCase();
                    const title = (f.title || '').toLowerCase();
                    const id = (f.id || '').toLowerCase();
                    const name = (f.name || '').toLowerCase();
                    const attrs = [src, title, id, name].join(' ');
                    return attrs.includes('challenges.cloudflare.com')
                        || attrs.includes('/cdn-cgi/challenge')
                        || attrs.includes('turnstile')
                        || attrs.includes('cloudflare');
                };

                const frames = Array.from(document.querySelectorAll('iframe'));
                if (frames.some(looksLikeCfFrame)) return true;

                const body = ((document.body && document.body.innerText) || '').toLowerCase();
                const looksLikeSecurityPage = body.includes('security verification')
                    || body.includes('verify you are human')
                    || body.includes('checking your browser')
                    || body.includes('malicious');

                // Fallback for challenge pages with opaque iframe attributes.
                if (!looksLikeSecurityPage) return false;
                return frames.some(f => {
                    const r = f.getBoundingClientRect();
                    return r.width > 100 && r.height > 40;
                });
            })()
            """
        ))
    except Exception:
        return False


async def _security_checkbox_point(tab) -> tuple:
    """Best-effort click target for security verification pages."""
    point = await tab.evaluate("""
        (() => {
            const pick = (el) => {
                if (!el || !el.getBoundingClientRect) return null;
                const r = el.getBoundingClientRect();
                if (r.width < 18 || r.height < 18) return null;
                const leftBias = Math.min(36, Math.max(20, r.width * 0.22));
                return {x: r.x + leftBias, y: r.y + (r.height / 2)};
            };

            for (const sel of [
                'iframe',
                '[role="checkbox"]',
                'input[type="checkbox"]',
                '[id*="turnstile"]',
                '[class*="turnstile"]',
                '[id*="challenge"]',
                '[class*="challenge"]'
            ]) {
                for (const el of document.querySelectorAll(sel)) {
                    const p = pick(el);
                    if (p) return p;
                }
            }

            const w = window.innerWidth || 1280;
            const h = window.innerHeight || 900;
            return {x: Math.max(40, Math.min(w - 40, w * 0.22)), y: Math.max(60, Math.min(h - 60, h * 0.42))};
        })()
    """)
    if isinstance(point, dict) and "x" in point and "y" in point:
        try:
            return (float(point["x"]), float(point["y"]))
        except (TypeError, ValueError):
            pass
    return (280.0, 380.0)


async def _bezier_mouse_move(tab, tx: float, ty: float, steps: int = 20) -> None:
    """
    Move the mouse to (tx, ty) along a cubic Bezier curve.
    Reads the last tracked position from window._mouseX/Y so consecutive calls
    form a continuous natural path.
    """
    try:
        pos_raw = await tab.evaluate(
            "JSON.stringify({x: window._mouseX || window.innerWidth/2,"
            "                y: window._mouseY || window.innerHeight/2})"
        )
        p = json.loads(pos_raw) if pos_raw and pos_raw != "null" else {}
        sx, sy = float(p.get("x", 640)), float(p.get("y", 450))
    except Exception:
        sx, sy = 640.0, 450.0

    dx, dy = tx - sx, ty - sy
    # Perpendicular offset creates a gentle arc instead of a straight line.
    perp_x = -dy * random.uniform(0.08, 0.22)
    perp_y =  dx * random.uniform(0.08, 0.22)

    cx1 = sx + dx * 0.30 + perp_x + random.uniform(-12, 12)
    cy1 = sy + dy * 0.30 + perp_y + random.uniform(-12, 12)
    cx2 = sx + dx * 0.70 + perp_x + random.uniform(-12, 12)
    cy2 = sy + dy * 0.70 + perp_y + random.uniform(-12, 12)

    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * sx + 3*mt**2*t * cx1 + 3*mt*t**2 * cx2 + t**3 * tx
        y = mt**3 * sy + 3*mt**2*t * cy1 + 3*mt*t**2 * cy2 + t**3 * ty
        await tab.mouse_move(x, y)
        # Ease-in/out: faster in the middle, slower near endpoints.
        speed = max(0.3, 1.0 - abs(2 * t - 1) * 0.6)
        await asyncio.sleep(random.uniform(0.008, 0.022) / speed)

    try:
        await tab.evaluate(f"window._mouseX={tx}; window._mouseY={ty};")
    except Exception:
        pass


async def _simulate_human_behavior(tab) -> None:
    """
    Simulate human browsing with Bezier mouse paths, natural scrolling, and
    micro-pauses so Cloudflare fingerprinting classifies the session as human.
    """
    try:
        w = int(await tab.evaluate("window.innerWidth || 1280") or 1280)
        h = int(await tab.evaluate("window.innerHeight || 900") or 900)

        # Visit 2–4 random spots with natural curved mouse paths.
        for _ in range(random.randint(2, 4)):
            tx = random.randint(int(w * 0.12), int(w * 0.88))
            ty = random.randint(int(h * 0.12), int(h * 0.65))
            await _bezier_mouse_move(tab, tx, ty)
            await asyncio.sleep(random.uniform(0.18, 0.65))

        # Scroll down naturally, then partially back up.
        down = random.randint(55, 160)
        await tab.evaluate(
            f"window.scrollBy({{top: {down}, left: 0, behavior: 'smooth'}})"
        )
        await asyncio.sleep(random.uniform(0.5, 1.0))
        up = random.randint(10, 40)
        await tab.evaluate(
            f"window.scrollBy({{top: -{up}, left: 0, behavior: 'smooth'}})"
        )
        await asyncio.sleep(random.uniform(0.3, 0.6))

        # Final resting position near the centre.
        await _bezier_mouse_move(
            tab,
            random.randint(int(w * 0.3), int(w * 0.7)),
            random.randint(int(h * 0.3), int(h * 0.6)),
        )
        await asyncio.sleep(random.uniform(0.2, 0.45))
    except Exception:
        pass


async def solve_turnstile(tab, timeout: int = 60) -> None:
    """
    Handle a Cloudflare Turnstile CHECKBOX widget on the current page.

    Only activates when a challenges.cloudflare.com iframe is actually present
    (the visible/managed checkbox widget).  Invisible widgets resolve on their
    own and the pure JS-challenge "Just a moment" page is handled by cf_guard.
    """
    # Only trigger on a real Turnstile iframe — NOT on bare window.turnstile
    # which is injected on every CF-protected page regardless of widget type.
    iframe_present = await _has_turnstile_iframe(tab)
    security_page = await _is_security_verification_page(tab)
    if not iframe_present and not security_page:
        return  # no checkbox widget — nothing to click

    if iframe_present:
        _log("Cloudflare Turnstile checkbox widget detected — solving ...")
    else:
        _log("security verification challenge detected without explicit iframe — using fallback click targeting")
    await _log_page_state(tab, "turnstile")

    async def is_confirmed() -> bool:
        # 1) Token present
        token = await tab.evaluate("""
            (() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return (el && el.value) ? el.value : null;
            })()
        """)
        if token:
            return True

        # 2) Challenge iframe removed
        if not await _has_turnstile_iframe(tab):
            return True

        # 3) Challenge page no longer active
        if not await _is_cf_challenge(tab):
            return True

        return False

    deadline = asyncio.get_event_loop().time() + timeout
    clicks = 0
    last_click = 0.0

    while asyncio.get_event_loop().time() < deadline:
        if await is_confirmed():
            _log("Turnstile confirmed")
            return

        # Click the checkbox (retry up to 4 times, every 10 s)
        now = asyncio.get_event_loop().time()
        if clicks == 0 or (now - last_click > 10 and clicks < 4):
            # Scroll the Turnstile iframe into the viewport before reading its rect.
            await tab.evaluate("""
                (() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const s = (f.src||'').toLowerCase();
                        if (s.includes('challenges.cloudflare.com')
                                || s.includes('turnstile')
                                || s.includes('/cdn-cgi/challenge')) {
                            f.scrollIntoView({block:'center', behavior:'smooth'});
                            return;
                        }
                    }
                    // Fallback: scroll any visible iframe into view
                    const iframes = document.querySelectorAll('iframe');
                    if (iframes.length) iframes[0].scrollIntoView({block:'center'});
                })()
            """)
            await asyncio.sleep(0.8)

            rect = await _cf_iframe_rect(tab)
            if rect:
                # Cloudflare checkbox sits on the left side of the challenge iframe.
                cx = rect["x"] + min(36, max(20, rect["w"] * 0.22)) + random.uniform(-3, 3)
                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                _log(f"clicking Turnstile checkbox at ({cx:.0f}, {cy:.0f})")
                # Bezier approach from current position — looks more human.
                await _bezier_mouse_move(tab, cx, cy)
                await asyncio.sleep(random.uniform(0.08, 0.15))
                await tab.mouse_click(cx, cy)
                last_click = asyncio.get_event_loop().time()
                clicks += 1

                confirm_deadline = asyncio.get_event_loop().time() + 12
                while asyncio.get_event_loop().time() < confirm_deadline:
                    if await is_confirmed():
                        _log("Turnstile confirmed after click")
                        return
                    await asyncio.sleep(0.4)
            else:
                # Last-resort click for opaque challenge layouts.
                px, py = await _security_checkbox_point(tab)
                cx = px + random.uniform(-2, 2)
                cy = py + random.uniform(-2, 2)
                _log(f"iframe rect unavailable — fallback click at ({cx:.0f}, {cy:.0f})")
                await _bezier_mouse_move(tab, cx, cy)
                await asyncio.sleep(random.uniform(0.06, 0.12))
                await tab.mouse_click(cx, cy)
                last_click = asyncio.get_event_loop().time()
                clicks += 1

                confirm_deadline = asyncio.get_event_loop().time() + 12
                while asyncio.get_event_loop().time() < confirm_deadline:
                    if await is_confirmed():
                        _log("Turnstile confirmed after click")
                        return
                    await asyncio.sleep(0.4)

            # After 2 failed clicks, try a keyboard fallback (Tab → Space).
            if clicks >= 2 and not await is_confirmed():
                _log("trying keyboard fallback (Tab + Space) for Turnstile ...")
                try:
                    await tab.evaluate("""
                        (() => {
                            // Focus the CF iframe if we can
                            for (const f of document.querySelectorAll('iframe')) {
                                const src = (f.src || '').toLowerCase();
                                if (src.includes('challenges.cloudflare.com')
                                        || src.includes('turnstile')) {
                                    f.focus();
                                    break;
                                }
                            }
                            const tabDown  = new KeyboardEvent('keydown', {key:'Tab',  code:'Tab',  bubbles:true});
                            const tabUp    = new KeyboardEvent('keyup',   {key:'Tab',  code:'Tab',  bubbles:true});
                            const spaceDown= new KeyboardEvent('keydown', {key:' ',    code:'Space',bubbles:true});
                            const spaceUp  = new KeyboardEvent('keyup',   {key:' ',    code:'Space',bubbles:true});
                            document.dispatchEvent(tabDown);
                            document.dispatchEvent(tabUp);
                            setTimeout(() => {
                                document.dispatchEvent(spaceDown);
                                document.dispatchEvent(spaceUp);
                            }, 300);
                        })()
                    """)
                    await asyncio.sleep(2.0)
                except Exception as kb_exc:
                    _log(f"keyboard fallback error: {kb_exc}")

        await asyncio.sleep(0.5)

    _log("Turnstile: timed out — continuing anyway")
    await _log_page_state(tab, "after turnstile timeout")


async def _is_cf_challenge(tab) -> bool:
    """Return True if the page is any Cloudflare challenge (JS check, Turnstile, waiting room)."""
    if await _has_turnstile_iframe(tab):
        return True

    url = (await _page_url(tab)).lower()
    if any(part in url for part in [
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge",
        "/cdn-cgi/challenge-platform",
    ]):
        return True

    title = (await _page_title(tab)).lower()
    if any(phrase in title for phrase in [
        "just a moment",
        "attention required",
        "one more step",
        "please wait",
    ]):
        return True

    text = await _page_text(tab)
    if any(phrase in text for phrase in [
        "performing security verification",
        "security check",
        "verify you are human",
        "checking your browser",
        "enable javascript and cookies",
        "malicious",
    ]):
        return True

    return await _is_waiting_room(tab)


async def _is_security_verification_page(tab) -> bool:
    """Return True on the Cloudflare security-verification interstitial."""
    text = await _page_text(tab)
    return "performing security verification" in text


async def cf_guard(tab, timeout: int = 90) -> None:
    """
    Wait until all Cloudflare challenges clear.

    Handles:
    1. Rate limiting (HTTP 429 / CF error 1015) — exponential back-off
    2. Cloudflare Waiting Room — periodic polling
    3. JS Challenge ("Just a moment") — Chrome resolves automatically; we wait
    4. Turnstile checkbox — human-like Bezier click
    5. Outright block — raises RuntimeError

    When ANTHROPIC_API_KEY is set an AI classifier runs first to choose
    the best strategy for whatever challenge variant is shown.
    """
    # ── Step 0: rate-limit check (fast, no API needed) ───────────────────────
    if await _is_rate_limited(tab):
        await _handle_rate_limit(tab)

    # ── Step 1: AI-assisted classification ───────────────────────────────────
    cf_info = await _ai_classify_cf(tab)
    action = cf_info.get("action", "proceed")

    if action == "abort":
        raise RuntimeError(
            f"Cloudflare outright blocked the request "
            f"(AI type={cf_info.get('type')}). "
            "Try again later or change your IP."
        )
    if action == "backoff":
        _log(f"AI recommends back-off (type={cf_info.get('type')}) — running rate-limit handler")
        await _handle_rate_limit(tab)
    elif action == "wait_queue":
        await handle_waiting_room(tab)
    else:
        await handle_waiting_room(tab)

    # ── Step 2: human behaviour simulation ───────────────────────────────────
    await _simulate_human_behavior(tab)

    # ── Step 3: Turnstile click with pre-delay ────────────────────────────────
    click_delay = float(env("CF_CLICK_DELAY_SECONDS", "8"))
    last_solve_attempt = 0.0

    if await _is_security_verification_page(tab) and await _has_turnstile_iframe(tab):
        _log(f"security verification page — waiting {click_delay:.0f}s before checkbox click")
        await asyncio.sleep(click_delay)

    await solve_turnstile(tab)
    last_solve_attempt = asyncio.get_event_loop().time()

    # ── Step 4: poll until cleared ────────────────────────────────────────────
    if not await _is_cf_challenge(tab):
        _log("No CF challenge detected — proceeding")
        return

    _log("CF challenge active — waiting for Chrome to pass it ...")
    deadline = asyncio.get_event_loop().time() + timeout
    poll = 0
    while asyncio.get_event_loop().time() < deadline:
        # Re-check rate limit mid-wait.
        if await _is_rate_limited(tab):
            await _handle_rate_limit(tab)

        now = asyncio.get_event_loop().time()
        if await _has_turnstile_iframe(tab) and (now - last_solve_attempt >= 20):
            await _simulate_human_behavior(tab)
            await solve_turnstile(tab)
            last_solve_attempt = asyncio.get_event_loop().time()

        if not await _is_cf_challenge(tab):
            _log("CF challenge cleared")
            await _log_page_state(tab, "after cf_guard")
            await asyncio.sleep(1.0)
            return

        poll += 1
        if poll % 5 == 0:
            await _log_page_state(tab, f"cf_guard poll {poll}")
        else:
            title = await _page_title(tab)
            elapsed = int(asyncio.get_event_loop().time() - (deadline - timeout))
            _log(f"still on CF challenge (title: '{title}', elapsed: {elapsed}s)")

        await asyncio.sleep(2.0)

    _log("WARNING: CF challenge still active after timeout — proceeding anyway")
    await _log_page_state(tab, "cf_guard timeout")
    await asyncio.sleep(1.0)


# ─── login helpers ────────────────────────────────────────────────────────────

async def _fill(tab, selectors: List[str], value: str) -> bool:
    for sel in selectors:
        try:
            el = await tab.select(sel)
            if el:
                await el.clear_input()
                await el.send_keys(value)
                return True
        except Exception:
            continue
    return False


async def _click(tab, selectors: List[str]) -> bool:
    for sel in selectors:
        try:
            el = await tab.select(sel)
            if el:
                await el.click()
                return True
        except Exception:
            continue
    return False


async def _click_by_text(tab, js_pattern: str) -> bool:
    """Click the first button/link whose visible text matches js_pattern (regex)."""
    result = await tab.evaluate(f"""
        (() => {{
            for (const el of document.querySelectorAll('button, a, input[type=submit]')) {{
                if (/{js_pattern}/i.test(el.textContent || el.value || '')) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }})()
    """)
    return bool(result)


# ─── login flow ───────────────────────────────────────────────────────────────

async def wait_for_login_page(tab, timeout: int = 180) -> None:
    """Poll until an email / username input is present on the page."""
    checks = [
        "input[type='email']",
        "input[name*='email']",
        "input[id*='email']",
        "input[name*='user']",
    ]
    _log("waiting for login page ...")
    await _log_page_state(tab, "before login wait")
    deadline = asyncio.get_event_loop().time() + timeout
    last_url = ""
    poll = 0
    while asyncio.get_event_loop().time() < deadline:
        # Only solve CF guard when URL changes, not every poll
        url = await _page_url(tab)
        if url != last_url:
            _log(f"URL changed -> {url}")
            last_url = url
            await cf_guard(tab)
        for sel in checks:
            try:
                el = await tab.select(sel)
                if el:
                    _log(f"login page ready (found: {sel})")
                    await _log_page_state(tab, "login page")
                    return
            except Exception:
                pass
        poll += 1
        if poll % 5 == 0:
            _log(f"still waiting for login page ... ({int(asyncio.get_event_loop().time()-(deadline-timeout))}s elapsed)")
            await _log_page_state(tab, "login wait poll")
        await asyncio.sleep(3.0)
    raise RuntimeError("Login page did not appear within 3 minutes")


async def perform_login(tab) -> None:
    username = require("USVISA_USERNAME")
    password = require("USVISA_PASSWORD")
    _log(f"logging in as {username[:3]}***")

    filled_user = await _fill(
        tab,
        ["input[type='email']", "#user_email",
         "input[name='user[email]']", "input[name*='email']"],
        username,
    )
    _log(f"username field filled: {filled_user}")
    if not filled_user:
        raise RuntimeError("Email/username input not found on login page")

    filled_pass = await _fill(
        tab,
        ["input[type='password']", "#user_password",
         "input[name='user[password]']", "input[name*='password']"],
        password,
    )
    _log(f"password field filled: {filled_pass}")
    if not filled_pass:
        raise RuntimeError("Password input not found on login page")

    clicked = await _click(tab, ["button[type='submit']", "input[type='submit']"])
    if not clicked:
        _log("submit button not found via CSS — trying text search")
        clicked = await _click_by_text(tab, r"sign.?in|log.?in")
    _log(f"submit clicked: {clicked}")

    _log("waiting for post-login redirect ...")
    await asyncio.sleep(4.0)
    await _log_page_state(tab, "after login submit")
    await cf_guard(tab)
    _log("login form submitted")


async def _extract_question_labels(tab) -> List[str]:
    """Read the visible label text for every text input on the page."""
    raw = await tab.evaluate("""
        JSON.stringify((() => {
            return Array.from(document.querySelectorAll('input[type=text]')).map(inp => {
                const id = inp.id || '';
                let label = '';
                if (id) {
                    const lbl = document.querySelector('label[for="' + id + '"]');
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    let el = inp.parentElement;
                    for (let i = 0; i < 4 && el; i++, el = el.parentElement) {
                        const lbl = el.querySelector(
                            'label, .label, .question, [class*="question"], p'
                        );
                        if (lbl && lbl.innerText.trim() && !lbl.querySelector('input')) {
                            label = lbl.innerText.trim();
                            break;
                        }
                    }
                }
                return label || inp.placeholder || inp.name || inp.id || '';
            });
        })())
    """)
    if raw and raw != "null":
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


# Security-question keyword patterns: list of (keyword_list, preferred_answer_index).
# The index is used as a *priority hint* when multiple stored answers are present.
_SQ_PATTERNS: List[Tuple[List[str], int]] = [
    (["mother", "maiden", "mom", "mum"], 0),
    (["born", "birth", "city", "hometown", "birthplace"], 1),
    (["pet", "dog", "cat", "animal", "childhood pet"], 2),
    (["school", "elementary", "primary", "first grade", "high school"], 0),
    (["nickname", "nick name", "called", "childhood name"], 1),
    (["father", "dad", "papa", "paternal", "father's"], 2),
    (["street", "road", "address", "grew up", "live"], 0),
    (["teacher", "favorite teacher", "first teacher"], 1),
    (["car", "first car", "vehicle"], 2),
    (["job", "first job", "employer"], 0),
]


def _keyword_match_security_answers(questions: List[str], stored: List[str]) -> List[str]:
    """
    Rule-based AI: maps each security question to the most likely stored answer
    using semantic keyword patterns.  No API key required.
    """
    assigned: List[Optional[str]] = [None] * len(questions)
    used_indices: set = set()

    for qi, question in enumerate(questions):
        q_lower = question.lower()
        for keywords, preferred_idx in _SQ_PATTERNS:
            if any(kw in q_lower for kw in keywords):
                # Find the preferred answer index that hasn't been used yet.
                for offset in range(len(stored)):
                    idx = (preferred_idx + offset) % len(stored)
                    if idx not in used_indices:
                        assigned[qi] = stored[idx]
                        used_indices.add(idx)
                        break
                if assigned[qi] is not None:
                    break

    # Fill any still-unmatched questions sequentially.
    for qi in range(len(questions)):
        if assigned[qi] is None:
            for idx in range(len(stored)):
                if idx not in used_indices:
                    assigned[qi] = stored[idx]
                    used_indices.add(idx)
                    break
            if assigned[qi] is None and stored:
                assigned[qi] = stored[0]

    result = [str(a) for a in assigned]
    _log(f"keyword AI matched: {list(zip(questions, result))}")
    return result


def _ai_match_answers(questions: List[str], stored: List[str]) -> List[str]:
    """
    Match stored security answers to the questions shown on screen.
    Priority: Claude API (if key set) → keyword AI (always available).
    """
    # Try Claude API first.
    if _HAS_ANTHROPIC:
        api_key = env("ANTHROPIC_API_KEY")
        if api_key:
            try:
                client = _anthropic.Anthropic(api_key=api_key)
                q_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
                a_text = "\n".join(f"Answer {i + 1}: {a}" for i, a in enumerate(stored))
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Match each stored answer to the security question it answers. "
                            "Return ONLY a JSON array of answer strings in question order.\n\n"
                            f"Questions:\n{q_text}\n\n"
                            f"Stored answers:\n{a_text}\n\n"
                            'Reply: ["ans1", "ans2", "ans3"]'
                        ),
                    }],
                )
                m = re.search(r"\[.*?\]", msg.content[0].text.strip(), re.DOTALL)
                if m:
                    matched = json.loads(m.group())
                    _log(f"Claude API matched: {list(zip(questions, matched))}")
                    return matched
            except Exception as exc:
                _log(f"Claude API match failed ({exc}) — falling back to keyword AI")

    # Built-in keyword AI (no API key needed).
    return _keyword_match_security_answers(questions, stored)


async def answer_security_questions(tab) -> None:
    stored = [require("USVISA_Q1"), require("USVISA_Q2"), require("USVISA_Q3")]

    _log("checking for security question form ...")
    await _log_page_state(tab, "security questions check")

    # Wait up to 20 s for the form to appear
    found_count = 0
    for attempt in range(4):
        count = await tab.evaluate(
            "document.querySelectorAll('input[type=text]').length"
        )
        found_count = int(count) if count else 0
        _log(f"security form poll {attempt+1}/4: found {found_count} text input(s)")
        if found_count >= 3:
            break
        await asyncio.sleep(5.0)
    else:
        _log("security question form not found — skipping")
        await _log_page_state(tab, "no security form")
        return

    # Read question labels so AI can route the right answer to the right field.
    question_labels = await _extract_question_labels(tab)
    _log(f"detected question labels: {question_labels}")
    answers = _ai_match_answers(question_labels[:found_count], stored) if question_labels else stored[:found_count]
    # Ensure we always have exactly found_count answers
    while len(answers) < found_count:
        answers.append(stored[len(answers)] if len(answers) < len(stored) else "")

    _log(f"filling {found_count} security answers")
    answers_json = json.dumps(answers)
    await tab.evaluate(f"""
        (() => {{
            const answers = {answers_json};
            const inputs = Array.from(document.querySelectorAll('input[type=text]'));
            answers.forEach((ans, i) => {{
                if (!inputs[i]) return;
                inputs[i].value = ans;
                ['input', 'change'].forEach(ev =>
                    inputs[i].dispatchEvent(new Event(ev, {{bubbles: true}}))
                );
            }});
        }})()
    """)
    await asyncio.sleep(0.5)

    clicked_sq = await _click(tab, ["button[type='submit']", "input[type='submit']"])
    if not clicked_sq:
        clicked_sq = await _click_by_text(tab, r"submit|continue|next")
    _log(f"security questions submit clicked: {clicked_sq}")

    await asyncio.sleep(3.0)
    await _log_page_state(tab, "after security questions")
    _log("security questions submitted")


# ─── session helpers ─────────────────────────────────────────────────────────

async def _is_logged_in(tab) -> bool:
    """Return True when the browser is on a post-login page (not login or CF)."""
    if await _is_cf_challenge(tab):
        return False
    url = (await _page_url(tab)).lower()
    if any(k in url for k in ("login", "sign-in", "signin")):
        return False
    text = await _page_text(tab)
    return any(phrase in text for phrase in [
        "reschedule", "appointment", "dashboard", "welcome",
        "my visa", "sign out", "logout", "profile",
    ])


# ─── reschedule navigation ────────────────────────────────────────────────────

async def goto_reschedule(tab) -> None:
    link_text = env("RESCHEDULE_LINK_TEXT", "Reschedule Appointment")
    _log(f"looking for '{link_text}' link ...")
    await _log_page_state(tab, "before reschedule nav")

    # nodriver text search
    try:
        el = await tab.find(link_text, best_match=True)
        if el:
            _log(f"found '{link_text}' element — clicking")
            await el.click()
            await asyncio.sleep(3.0)
            await _log_page_state(tab, "after reschedule click")
            return
    except Exception as exc:
        _log(f"nodriver find failed: {exc}")

    _log("trying JS fallback to find reschedule link")
    # JS fallback
    found = await tab.evaluate("""
        (() => {
            for (const el of document.querySelectorAll('a, button')) {
                if (/reschedule/i.test(el.textContent || '')) {
                    el.click();
                    return true;
                }
            }
            return false;
        })()
    """)
    if not found:
        await _log_page_state(tab, "reschedule link NOT found")
        raise RuntimeError(
            "Reschedule Appointment link not found. "
            "Check that login succeeded or set RESCHEDULE_LINK_TEXT."
        )
    await asyncio.sleep(3.0)
    await _log_page_state(tab, "reschedule page")
    _log("on reschedule page")


# ─── post / consulate dropdown ────────────────────────────────────────────────

_DROPDOWN_SELECTORS = [
    "select[name*='facility']",
    "select[id*='facility']",
    "select[name*='consulate']",
    "select[id*='consulate']",
    "select[name*='post']",
    "select[id*='post']",
    "select[name*='location']",
    "select[id*='location']",
    "select",  # last resort
]


async def find_dropdown_selector(tab) -> Optional[str]:
    _log("probing for OFC/consulate dropdown ...")
    for sel in _DROPDOWN_SELECTORS:
        try:
            el = await tab.select(sel)
            if not el:
                continue
            count = await tab.evaluate(
                f"document.querySelector('{sel}').options.length"
            )
            cnt = int(count) if count else 0
            _log(f"  selector '{sel}': {cnt} option(s)")
            if cnt > 1:
                _log(f"  -> using selector: {sel}")
                return sel
        except Exception:
            continue
    return None


async def get_all_options(tab, sel: str) -> List[Tuple[str, str]]:
    """Return list of (value, label) for every non-empty option."""
    raw = await tab.evaluate(f"""
        JSON.stringify(
            Array.from(document.querySelector('{sel}').options)
                .filter(o => o.value && o.value.trim())
                .map(o => [o.value.trim(), (o.text || o.label || '').trim()])
        )
    """)
    if raw and raw != "null":
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


async def select_post(tab, sel: str, value: str) -> None:
    """Select a dropdown option and fire change/input events."""
    await tab.evaluate(f"""
        (() => {{
            const s = document.querySelector('{sel}');
            if (!s) return;
            s.value = {json.dumps(value)};
            ['change', 'input'].forEach(ev =>
                s.dispatchEvent(new Event(ev, {{bubbles: true}}))
            );
        }})()
    """)
    # Give AJAX time to refresh the calendar
    await asyncio.sleep(2.5)


# ─── calendar scanning ────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"),
    re.compile(r"\b(\d{1,2}-\d{1,2}-\d{4})\b"),
    re.compile(
        r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4})\b",
        re.I,
    ),
    re.compile(
        r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4})\b",
        re.I,
    ),
]

_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
]


def _extract_date(text: str) -> Optional[str]:
    t = " ".join(text.split())
    for pat in _DATE_PATTERNS:
        m = pat.search(t)
        if m:
            return m.group(1)
    return None


def _parse_date(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


# JavaScript: collect all available-date strings from the visible calendar view.
_JS_COLLECT_DATES = """
(() => {
    const SELECTORS = [
        'td.available:not(.disabled):not(.off)',
        'td[data-date]:not(.disabled):not(.unavailable)',
        'td.day:not(.disabled):not(.off)',
        '.ui-datepicker-calendar td:not(.ui-datepicker-unselectable)',
        'td:not(.disabled):not(.off):not([class*="unavail"]):not([class*="past"]) a',
    ];
    const seen = new Set();
    for (const sel of SELECTORS) {
        const cells = document.querySelectorAll(sel);
        if (!cells.length) continue;
        cells.forEach(c => {
            const v = c.getAttribute('data-date')
                   || c.getAttribute('data-value')
                   || c.getAttribute('title')
                   || c.innerText.trim();
            if (v && v.trim()) seen.add(v.trim());
        });
        if (seen.size) break;
    }
    return JSON.stringify([...seen]);
})()
"""

# JavaScript: advance the calendar to the next month.  Returns true if clicked.
_JS_NEXT_MONTH = """
(() => {
    const SELECTORS = [
        '.datepicker-next', 'th.next', '.next', '.ui-datepicker-next',
        '[data-handler="next"]', 'button[aria-label*="next" i]',
        '.fc-next-button', 'a[title*="next" i]', '.arrow-right',
        '.rightNavigation', 'button.right-arrow',
    ];
    for (const sel of SELECTORS) {
        const btn = document.querySelector(sel);
        if (btn) { btn.click(); return true; }
    }
    return false;
})()
"""

# JavaScript: read the calendar's current month/year label.
_JS_MONTH_LABEL = """
(() => {
    const SELECTORS = [
        '.datepicker-switch', '.ui-datepicker-title', '.calendar-title',
        '.month-year-header', 'th.month', '.month-title', '.current-month',
    ];
    for (const sel of SELECTORS) {
        const el = document.querySelector(sel);
        if (el && el.innerText.trim()) return el.innerText.trim();
    }
    return '';
})()
"""

# JavaScript: try to open the date-picker so the calendar widget appears.
_JS_OPEN_DATEPICKER = """
(() => {
    const SELECTORS = [
        '#consulate_appointment_date',
        '#appointments_consulate_appointment_date',
        'input[name*="appointment"][name*="date"]',
        'input[id*="appointment"][id*="date"]',
        'input[class*="datepicker"]',
        '.datepicker-input',
        'input[type="date"]',
    ];
    for (const sel of SELECTORS) {
        const el = document.querySelector(sel);
        if (el) { el.click(); el.focus(); return true; }
    }
    return false;
})()
"""


async def scan_calendar_dates(tab, max_months: int = 12) -> List[str]:
    """
    Scan the calendar widget month by month and collect all available-date strings.

    - Tries to open the date-picker if it isn't visible.
    - Advances up to max_months months forward.
    - Stops early if the calendar stops advancing (stuck month label).
    Returns a de-duplicated list of raw date strings found in available cells.
    """
    # Ensure the calendar is open
    await tab.evaluate(_JS_OPEN_DATEPICKER)
    await asyncio.sleep(0.5)

    all_dates: List[str] = []
    seen_labels: set = set()

    for month_idx in range(max_months):
        # Read month label first (for logging)
        label = (await tab.evaluate(_JS_MONTH_LABEL) or "").strip()
        _log(f"  calendar month {month_idx+1}: '{label}'")

        # Collect available dates on the current calendar view
        raw = await tab.evaluate(_JS_COLLECT_DATES)
        month_new: List[str] = []
        if raw and raw != "null":
            try:
                raw_dates = json.loads(raw)
                for rd in raw_dates:
                    d = _extract_date(rd) or (rd if _parse_date(rd) else None)
                    if d and d not in all_dates:
                        all_dates.append(d)
                        month_new.append(d)
            except (json.JSONDecodeError, ValueError):
                pass
        _log(f"  calendar month {month_idx+1}: {len(month_new)} new date(s) found{': ' + str(month_new[:5]) if month_new else ''}")

        # Detect stalled calendar
        if label:
            if label in seen_labels:
                _log(f"  calendar stalled at '{label}' — stopping")
                break
            seen_labels.add(label)

        # Try to advance to the next month
        advanced = await tab.evaluate(_JS_NEXT_MONTH)
        if not advanced:
            if month_idx == 0:
                _log("  (single-month calendar — no next-month button)")
            break
        await asyncio.sleep(0.8)

    return all_dates


async def scan_post(tab, dropdown_sel: str, value: str, label: str) -> SlotResult:
    """Select one post in the dropdown and return its earliest available slot."""
    result = SlotResult(post=label)
    _log(f"scanning post: {label!r} (value={value!r})")

    await select_post(tab, dropdown_sel, value)

    # Fast path: some sites show an explicit "earliest date" element
    inline = await tab.evaluate("""
        (() => {
            for (const sel of [
                '[id*="earliest"]', '[class*="earliest"]',
                '#consulate_appointment_date',
                '#appointments_consulate_appointment_date',
                'input[name*="appointment"][name*="date"]',
            ]) {
                const el = document.querySelector(sel);
                const v = el && (el.value || el.innerText || el.textContent || '').trim();
                if (v) return v;
            }
            return null;
        })()
    """)
    if inline:
        d = _extract_date(inline)
        if d:
            result.earliest = d
            result.all_dates = [d]
            result.status = "ok"
            _log(f"  {label!r}: earliest (inline) = {d}")
            return result

    # Full calendar scan
    max_months = int(env("CALENDAR_MONTHS", "12"))
    _log(f"  {label!r}: scanning calendar (up to {max_months} months) ...")
    dates = await scan_calendar_dates(tab, max_months)

    _log(f"  {label!r}: raw dates found: {dates}")
    if dates:
        pairs = [(d, _parse_date(d)) for d in dates if _parse_date(d)]
        pairs.sort(key=lambda x: x[1])  # type: ignore[arg-type]
        result.all_dates = [d for d, _ in pairs]
        result.earliest = pairs[0][0]
        result.status = "ok"
        _log(f"  {label!r}: earliest = {result.earliest}")
    else:
        result.status = "no dates found"
        _log(f"  {label!r}: no available dates found")

    return result


# ─── auto-booking helpers ─────────────────────────────────────────────────────

_MONTH_FMTS = ["%B %Y", "%b %Y", "%B, %Y", "%b, %Y", "%m/%Y", "%Y-%m"]


async def _navigate_to_month(tab, target_dt: datetime, max_forward: int = 36) -> bool:
    """
    Click the calendar's 'next month' button until the calendar shows the month
    that contains target_dt.  Returns True on success.
    """
    for _ in range(max_forward):
        label = (await tab.evaluate(_JS_MONTH_LABEL) or "").strip()
        if label:
            for fmt in _MONTH_FMTS:
                try:
                    cur = datetime.strptime(label, fmt)
                    if cur.year == target_dt.year and cur.month == target_dt.month:
                        return True
                    if cur > target_dt:
                        return False  # overshot
                    break
                except ValueError:
                    continue
        # Advance one month
        advanced = await tab.evaluate(_JS_NEXT_MONTH)
        if not advanced:
            return False
        await asyncio.sleep(0.7)
    return False


async def _click_calendar_date(tab, date_str: str) -> bool:
    """
    Click the cell for date_str in the currently-visible calendar.
    Tries data attributes first, then falls back to matching the day number.
    """
    target_dt = _parse_date(date_str)
    day_str = str(target_dt.day) if target_dt else ""

    result = await tab.evaluate(f"""
        (() => {{
            const ds = {json.dumps(date_str)};
            const day = {json.dumps(day_str)};

            // Strategy 1: exact attribute match
            for (const attr of ['data-date','data-value','title','aria-label']) {{
                const sel = `td[${{attr}}="${{ds}}"]:not(.disabled):not(.unavailable)` +
                            `,a[${{attr}}="${{ds}}"]`;
                const el = document.querySelector(sel);
                if (el) {{ el.click(); return 'attr:'+attr; }}
            }}

            // Strategy 2: available cell whose text == day number
            if (day) {{
                const cells = document.querySelectorAll(
                    'td.available:not(.disabled):not(.off),' +
                    'td[data-date]:not(.disabled):not(.unavailable),' +
                    'td.day:not(.disabled):not(.off)'
                );
                for (const c of cells) {{
                    if ((c.innerText||c.textContent||'').trim() === day) {{
                        c.click(); return 'day:'+day;
                    }}
                }}
            }}
            return null;
        }})()
    """)
    return bool(result)


async def book_appointment(
    tab,
    dropdown_sel: str,
    post_value: str,
    post_label: str,
    target_date: str,
) -> bool:
    """
    Book an appointment at the given post on the given date.

    Flow:
      1. Select the post in the dropdown
      2. Open the date-picker
      3. Navigate to the correct month
      4. Click the date cell
      5. Select the first available time slot (if present)
      6. Submit the form
      7. Verify a confirmation message appeared

    Returns True when the booking appears to have been submitted successfully.
    """
    _log(f"Auto-booking: {post_label} on {target_date}")

    # 1. Select the post.
    await select_post(tab, dropdown_sel, post_value)
    await asyncio.sleep(2.5)

    # 2. Open the date-picker.
    opened = await tab.evaluate(_JS_OPEN_DATEPICKER)
    await asyncio.sleep(1.0)
    if not opened:
        _log("Could not open date-picker")

    # 3. Navigate to the correct month.
    target_dt = _parse_date(target_date)
    if target_dt:
        navigated = await _navigate_to_month(tab, target_dt)
        if not navigated:
            _log(f"Could not navigate calendar to {target_date} — trying direct click anyway")

    # 4. Click the date cell.
    clicked = await _click_calendar_date(tab, target_date)
    if not clicked:
        _log(f"Could not click date {target_date} in calendar — aborting booking")
        return False
    _log(f"Clicked date cell: {target_date}")
    await asyncio.sleep(1.5)

    # 5. Select first available time slot (optional).
    time_val = await tab.evaluate("""
        (() => {
            for (const sel of [
                'select[name*="time"]','select[id*="time"]',
                '#appointments_consulate_appointment_time',
            ]) {
                const s = document.querySelector(sel);
                if (!s) continue;
                const opt = s.querySelector('option:not([value=""]):not([disabled])');
                if (opt) {
                    s.value = opt.value;
                    s.dispatchEvent(new Event('change', {bubbles:true}));
                    return opt.value;
                }
            }
            return null;
        })()
    """)
    if time_val:
        _log(f"Selected time slot: {time_val}")
        await asyncio.sleep(1.0)

    # 6. Submit.
    submitted = await _click(tab, [
        "#appointments_submit",
        "input[type='submit']",
        "button[type='submit']",
    ])
    if not submitted:
        submitted = await _click_by_text(tab, r"confirm|book|submit|reschedule|schedule")
    if not submitted:
        _log("Submit button not found — booking may have failed")
        return False

    _log("Booking form submitted")
    await asyncio.sleep(4.0)
    await _log_page_state(tab, "after booking submit")

    # 7. Verify confirmation.
    text = await _page_text(tab)
    confirmed = any(phrase in text for phrase in [
        "confirmed", "booked", "appointment scheduled",
        "reschedule successful", "booking successful",
        "your appointment", "successfully",
    ])
    if confirmed:
        _log(f"Booking CONFIRMED: {post_label} on {target_date}")
    else:
        _log("Booking submitted (confirmation message not detected — check manually)")
    return True


# ─── Telegram notification ────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = request.Request(endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req, timeout=20) as resp:
            json.loads(resp.read())
        _log("Telegram notification sent")
    except Exception as exc:
        _log(f"Telegram error: {exc}")


def build_report(results: List[SlotResult]) -> Tuple[str, Optional[SlotResult]]:
    lines = ["<b>US Visa OFC — Earliest Available Slots</b>"]
    best: Optional[SlotResult] = None
    best_dt: Optional[datetime] = None

    for r in results:
        if r.earliest:
            lines.append(f"  <b>{r.post}</b>: {r.earliest}")
            dt = _parse_date(r.earliest)
            if dt and (best_dt is None or dt < best_dt):
                best_dt, best = dt, r
        else:
            lines.append(f"  <b>{r.post}</b>: {r.status}")

    lines.append("")
    if best:
        lines.append(f"<b>Nearest slot:</b> {best.post} -> {best.earliest}")
    else:
        lines.append("<b>No available slots found across all posts.</b>")

    lines.append(
        f"\n<i>Scanned {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
    )
    return "\n".join(lines), best


# ─── auto-book ────────────────────────────────────────────────────────────────

async def try_auto_book(tab, best: Optional[SlotResult]) -> str:
    if env("AUTO_BOOK", "false").lower() != "true":
        return ""
    if not best or not best.earliest:
        return "AUTO_BOOK: no valid slot found — skipping"

    date_sel   = env("BOOK_DATE_SELECTOR")
    time_sel   = env("BOOK_TIME_SELECTOR")
    submit_sel = env("BOOK_SUBMIT_SELECTOR")

    if not all([date_sel, time_sel, submit_sel]):
        return "AUTO_BOOK: BOOK_DATE/TIME/SUBMIT_SELECTOR not all set — skipping"

    try:
        for sel in [date_sel, time_sel, submit_sel]:
            el = await tab.select(sel)
            if el:
                await el.click()
            await asyncio.sleep(0.5)
        await asyncio.sleep(3.0)
        return f"AUTO_BOOK: submitted for {best.post} on {best.earliest}"
    except Exception as exc:
        return f"AUTO_BOOK: failed — {exc}"


# ─── main flow ────────────────────────────────────────────────────────────────

async def _login_flow(tab) -> None:
    """Run through stealth patch → CF guard → login → security questions."""
    await _apply_stealth(tab)
    await cf_guard(tab)
    await wait_for_login_page(tab)
    await perform_login(tab)
    await answer_security_questions(tab)
    await asyncio.sleep(2.0)
    await _apply_stealth(tab)
    await cf_guard(tab)


async def _scan_cycle(
    tab,
    cycle: int,
    current_booked_dt: Optional[datetime] = None,
):
    """
    One full scan: reschedule page → ALL posts → find global earliest.
    Books the slot only when it is strictly earlier than current_booked_dt
    (or current_booked_dt is None, meaning nothing is booked yet).

    Returns (best, report, booking_note, new_booked_dt).
    new_booked_dt is the datetime of the newly booked slot, or None if
    nothing was booked this cycle.
    """
    _log(f"--- Cycle {cycle}: navigate to reschedule ---")
    await goto_reschedule(tab)
    await asyncio.sleep(2.0)

    _log(f"--- Cycle {cycle}: find post dropdown ---")
    dropdown_sel = await find_dropdown_selector(tab)
    if not dropdown_sel:
        await _log_page_state(tab, "no dropdown found")
        raise RuntimeError("OFC/consulate dropdown not found. Page layout may have changed.")

    options = await get_all_options(tab, dropdown_sel)
    if not options:
        raise RuntimeError("Dropdown found but it contains no options.")

    _log(f"found {len(options)} posts: {[lbl for _, lbl in options]}")

    configured = env("USVISA_POSTS")
    if configured:
        want = {p.strip().lower() for p in configured.split(",") if p.strip()}
        filtered = [(v, lbl) for v, lbl in options if lbl.lower() in want or v.lower() in want]
        if filtered:
            options = filtered
            _log(f"USVISA_POSTS filter: {[lbl for _, lbl in options]}")
        else:
            _log("USVISA_POSTS matched nothing — scanning all posts")

    _log(f"--- Cycle {cycle}: scan {len(options)} post(s) ---")
    results: List[SlotResult] = []
    post_map: dict = {}
    for i, (value, label) in enumerate(options, 1):
        _log(f"post {i}/{len(options)}: {label!r}")
        r = await scan_post(tab, dropdown_sel, value, label)
        results.append(r)
        post_map[label] = (value, label)

    report, best = build_report(results)

    plain = re.sub(r"<[^>]+>", "", report)
    cur_str = current_booked_dt.strftime("%Y-%m-%d") if current_booked_dt else "none"
    print("\n" + "=" * 60)
    print(plain)
    print(f"Current booking : {cur_str}")
    print("=" * 60 + "\n")

    # ── Decide whether to book ────────────────────────────────────────────────
    booking_note = ""
    new_booked_dt: Optional[datetime] = None

    if best and best.earliest:
        new_dt = _parse_date(best.earliest)
        is_improvement = new_dt is not None and (
            current_booked_dt is None or new_dt < current_booked_dt
        )

        if is_improvement:
            post_value, _ = post_map.get(best.post, (None, best.post))
            if post_value:
                _log(f"Improvement found: {best.earliest} @ {best.post} "
                     f"(was: {cur_str}) — booking ...")
                try:
                    booked = await book_appointment(
                        tab, dropdown_sel, post_value, best.post, best.earliest
                    )
                    if booked:
                        new_booked_dt = new_dt
                        booking_note = f"BOOKED: {best.post} on {best.earliest}"
                    else:
                        booking_note = f"BOOKING FAILED: {best.post} on {best.earliest}"
                    send_telegram(report + f"\n\n<b>{booking_note}</b>")
                except Exception as exc:
                    booking_note = f"BOOKING ERROR: {exc}"
                    _log(f"Booking exception: {exc}")
            else:
                booking_note = "Could not resolve post value — skipping booking"
        else:
            booking_note = (
                f"No improvement over current booking ({cur_str}) — "
                f"best found: {best.earliest}"
            )
            _log(booking_note)

    return best, report, booking_note, new_booked_dt


async def run() -> None:
    global _ip_rotator

    chrome   = _find_chrome()
    profile  = _get_profile_dir()
    interval = int(env("MONITOR_INTERVAL_MINUTES", "15"))

    # Parse optional target date — bot stops once it books on or before this date.
    target_dt: Optional[datetime] = None
    target_str = env("TARGET_DATE")
    if target_str:
        target_dt = _parse_date(target_str)
        if not target_dt:
            _log(f"WARNING: could not parse TARGET_DATE={target_str!r} — ignoring")

    # Initialise IP rotator.
    _ip_rotator = IPRotator()
    _ip_rotator.start()
    extra_flags = _get_chrome_flags() + _ip_rotator.get_browser_args()

    _log("=== US Visa Slot Monitor starting ===")
    _log(f"Chrome      : {chrome}")
    _log(f"Profile     : {profile}")
    _log(f"Site        : {BASE_URL}")
    _log(f"Termux      : {_is_termux()}")
    _log(f"IP rotate   : {_ip_rotator.method}")
    _log(f"Interval    : {interval} min between idle scans")
    _log(f"Target date : {target_dt.strftime('%Y-%m-%d') if target_dt else 'not set'}")

    browser = await uc.start(
        browser_executable_path=chrome,
        headless=False,
        user_data_dir=profile,
        no_sandbox=True,
        browser_args=extra_flags if extra_flags else None,
    )
    _log("Chrome launched")

    # Tracks the date of the most recently booked appointment.
    current_booked_dt: Optional[datetime] = None
    cycle = 0

    try:
        _log(f"Navigating to {BASE_URL} ...")
        tab = await browser.get(BASE_URL)
        await asyncio.sleep(2.0)
        await _log_page_state(tab, "initial load")

        # ── Phase 1 & 2: stealth + Cloudflare + login ────────────────────────
        _log("--- Phase 1+2: stealth / Cloudflare / login ---")
        await _login_flow(tab)

        # ── Continuous reschedule loop ────────────────────────────────────────
        # When an improvement is found, re-scan immediately (no delay) to chase
        # even earlier dates.  Only sleep when no improvement was found.
        while True:
            cycle += 1
            improved_this_cycle = False
            _log(f"=== Scan cycle {cycle} ===")

            try:
                best, report, booking_note, new_booked_dt = await _scan_cycle(
                    tab, cycle, current_booked_dt
                )

                if new_booked_dt is not None:
                    current_booked_dt = new_booked_dt
                    improved_this_cycle = True
                    _log(f"Booked: {new_booked_dt.strftime('%Y-%m-%d')}")

                    # ── Check if we hit the target ────────────────────────────
                    if target_dt and current_booked_dt <= target_dt:
                        msg = (
                            f"TARGET DATE REACHED!\n"
                            f"Booked: {best.post} on "
                            f"{current_booked_dt.strftime('%Y-%m-%d')}\n"
                            f"(target was {target_dt.strftime('%Y-%m-%d')})"
                        )
                        _log(msg)
                        send_telegram(f"<b>{msg}</b>")
                        break  # Mission complete — exit the loop.

            except Exception as exc:
                _log(f"Cycle {cycle} error: {exc}")
                import traceback; traceback.print_exc()

            if improved_this_cycle:
                # Scan again immediately — don't waste time when a slot was just booked.
                _log("Improvement booked — re-scanning immediately for an even earlier slot ...")
                # Short pause to let the site process the booking before we hit it again.
                await asyncio.sleep(10)
            else:
                _log(f"No improvement this cycle. Waiting {interval} min ...")
                await asyncio.sleep(interval * 60)

            # Re-navigate to home page and handle any fresh CF challenge.
            _log("Returning to home page ...")
            try:
                await tab.get(BASE_URL)
                await asyncio.sleep(2.0)
                await _apply_stealth(tab)
                await cf_guard(tab)
            except Exception as exc:
                _log(f"Navigation error (will retry): {exc}")
                await asyncio.sleep(15)

            # Re-login if session expired.
            if not await _is_logged_in(tab):
                _log("Session expired — re-logging in ...")
                await _login_flow(tab)

    finally:
        _log("closing browser")
        browser.stop()


# ─── interactive credential prompt ──────────────────────────────────────────

def _ask(prompt: str, current: str, hidden: bool = False) -> str:
    """
    Ask the user for a value in the terminal.
    Shows '[saved]' if a value is already stored; pressing Enter keeps it.
    """
    if current:
        display = "[saved]" if hidden else f"[{current}]"
        label = f"{prompt} {display}: "
    else:
        label = f"{prompt}: "

    try:
        if hidden:
            entered = getpass.getpass(label)
        else:
            entered = input(label).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise

    return entered if entered else current


def _save_dotenv(values: dict) -> None:
    """Overwrite .env with the given key=value pairs (plus existing comments)."""
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    # Read existing lines to preserve comments / optional keys
    existing_lines: List[str] = []
    if os.path.isfile(dotenv_path):
        with open(dotenv_path, encoding="utf-8") as fh:
            existing_lines = fh.readlines()

    # Build updated lines: replace matching key lines, keep everything else
    written_keys: set = set()
    new_lines: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}\n")
            written_keys.add(key)
        else:
            new_lines.append(line)

    # Append any new keys not already present
    for key, value in values.items():
        if key not in written_keys:
            new_lines.append(f"{key}={value}\n")

    with open(dotenv_path, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)


def prompt_credentials() -> None:
    """
    Interactively ask for login credentials in the terminal.
    Existing .env values are shown as defaults — press Enter to keep them.
    Offers to save updated values back to .env.
    """
    print()
    print("=" * 60)
    print("  US Visa Slot Monitor — Login Setup")
    print("=" * 60)
    print("  Press Enter to keep a saved value, or type a new one.")
    print()

    username = _ask("Username / e-mail", env("USVISA_USERNAME"))
    password = _ask("Password",          env("USVISA_PASSWORD"), hidden=True)
    q1       = _ask("Security Answer 1", env("USVISA_Q1"),       hidden=True)
    q2       = _ask("Security Answer 2", env("USVISA_Q2"),       hidden=True)
    q3       = _ask("Security Answer 3", env("USVISA_Q3"),       hidden=True)

    # Validate nothing critical is blank
    missing = [n for n, v in [
        ("Username", username), ("Password", password),
        ("Security Answer 1", q1), ("Security Answer 2", q2), ("Security Answer 3", q3),
    ] if not v]
    if missing:
        print(f"\n[ERROR] These fields cannot be empty: {', '.join(missing)}")
        raise SystemExit(1)

    # Push into environment so the rest of the script picks them up
    os.environ["USVISA_USERNAME"] = username
    os.environ["USVISA_PASSWORD"] = password
    os.environ["USVISA_Q1"]       = q1
    os.environ["USVISA_Q2"]       = q2
    os.environ["USVISA_Q3"]       = q3

    # Offer to save
    print()
    try:
        save = input("Save credentials to .env for next time? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        save = "n"
        print()

    if save in ("", "y", "yes"):
        _save_dotenv({
            "USVISA_USERNAME": username,
            "USVISA_PASSWORD": password,
            "USVISA_Q1":       q1,
            "USVISA_Q2":       q2,
            "USVISA_Q3":       q3,
        })
        print("[OK] Saved to .env")

    print()


if __name__ == "__main__":
    prompt_credentials()
    _xvfb = _start_xvfb()
    try:
        asyncio.run(run())
    finally:
        if _xvfb:
            _xvfb.terminate()
