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


# ─── chrome / display setup ───────────────────────────────────────────────────

def _find_chrome() -> str:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
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
        "Chrome not found in default locations. "
        "Set the CHROME_PATH environment variable to your Chrome executable."
    )


def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or r"C:\Temp"
    else:
        base = "/tmp"
    return os.path.join(base, "usvisa_profile")


def _start_xvfb() -> Optional[subprocess.Popen]:
    """On Linux headless servers, start a virtual display so Chrome can run."""
    if platform.system() != "Linux" or os.environ.get("DISPLAY"):
        return None
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x1024x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] started Xvfb on :99")
    return proc


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
    if not iframe_present:
        return  # no checkbox widget — nothing to click

    _log("Cloudflare Turnstile checkbox widget detected — solving ...")
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
            rect = await _cf_iframe_rect(tab)
            if rect:
                # Cloudflare checkbox sits on the left side of the challenge iframe.
                cx = rect["x"] + min(36, max(20, rect["w"] * 0.22)) + random.uniform(-3, 3)
                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                _log(f"clicking Turnstile checkbox at ({cx:.0f}, {cy:.0f})")
                await tab.mouse_move(cx - 60, cy - 15)
                await asyncio.sleep(random.uniform(0.15, 0.25))
                await tab.mouse_move(cx, cy)
                await asyncio.sleep(random.uniform(0.08, 0.15))
                await tab.mouse_click(cx, cy)
                last_click = asyncio.get_event_loop().time()
                clicks += 1

                # Wait briefly for challenge confirmation after each click.
                confirm_deadline = asyncio.get_event_loop().time() + 12
                while asyncio.get_event_loop().time() < confirm_deadline:
                    if await is_confirmed():
                        _log("Turnstile confirmed after click")
                        return
                    await asyncio.sleep(0.4)
            else:
                # Last-resort click for opaque challenge layouts where iframe rect cannot be read.
                viewport = await tab.evaluate("({w: window.innerWidth || 1280, h: window.innerHeight || 900})")
                vw = float((viewport or {}).get("w", 1280))
                vh = float((viewport or {}).get("h", 900))
                cx = min(max(vw * 0.22, 40), vw - 40) + random.uniform(-2, 2)
                cy = min(max(vh * 0.42, 60), vh - 60) + random.uniform(-2, 2)
                _log(f"iframe rect unavailable — fallback click at ({cx:.0f}, {cy:.0f})")
                await tab.mouse_move(cx - 40, cy - 10)
                await asyncio.sleep(random.uniform(0.12, 0.2))
                await tab.mouse_move(cx, cy)
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

    Handles three kinds:
    1. Cloudflare Waiting Room  — periodic polling
    2. JS Challenge ("Just a moment...")  — Chrome passes this automatically; we wait
    3. Turnstile checkbox widget  — we click it
    """
    await handle_waiting_room(tab)

    # The managed/malicious-check variant can show only the checkbox iframe.
    # Delay first click a bit on verification interstitials to mimic real user behavior.
    click_delay = float(env("CF_CLICK_DELAY_SECONDS", "6"))
    last_solve_attempt = 0.0

    if await _is_security_verification_page(tab) and await _has_turnstile_iframe(tab):
        _log(f"security verification page detected — waiting {click_delay:.0f}s before checkbox click")
        await asyncio.sleep(click_delay)

    await solve_turnstile(tab)
    last_solve_attempt = asyncio.get_event_loop().time()

    # If already past any challenge, return immediately
    if not await _is_cf_challenge(tab):
        _log("No CF challenge detected — proceeding")
        return

    _log("CF challenge active — waiting for Chrome to pass it ...")
    deadline = asyncio.get_event_loop().time() + timeout
    poll = 0
    while asyncio.get_event_loop().time() < deadline:
        # Avoid repeatedly re-solving every poll; retry only on cooldown.
        now = asyncio.get_event_loop().time()
        if await _has_turnstile_iframe(tab) and (now - last_solve_attempt >= 20):
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
            _log(f"still on CF challenge (title: '{title}', elapsed: {int(asyncio.get_event_loop().time()-(deadline-timeout))}s)")

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


async def answer_security_questions(tab) -> None:
    answers = [require("USVISA_Q1"), require("USVISA_Q2"), require("USVISA_Q3")]

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

    _log("answering security questions")
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

async def run() -> None:
    chrome = _find_chrome()
    profile = _get_profile_dir()
    _log(f"=== US Visa Slot Monitor starting ===")
    _log(f"Chrome  : {chrome}")
    _log(f"Profile : {profile}")
    _log(f"Site    : {BASE_URL}")

    browser = await uc.start(
        browser_executable_path=chrome,
        headless=False,
        user_data_dir=profile,
        no_sandbox=True,
    )
    _log("Chrome launched")
    try:
        _log(f"Navigating to {BASE_URL} ...")
        tab = await browser.get(BASE_URL)
        await asyncio.sleep(2.0)
        await _log_page_state(tab, "initial load")

        # ── Phase 1: clear Cloudflare gating ──────────────────────────────────
        _log("--- Phase 1: Cloudflare guard ---")
        await cf_guard(tab)

        # ── Phase 2: login ────────────────────────────────────────────────────
        _log("--- Phase 2: Login ---")
        await wait_for_login_page(tab)
        await perform_login(tab)
        await answer_security_questions(tab)
        await asyncio.sleep(2.0)
        await cf_guard(tab)

        # ── Phase 3: reschedule page ──────────────────────────────────────────
        _log("--- Phase 3: Navigate to Reschedule ---")
        await goto_reschedule(tab)
        await asyncio.sleep(2.0)

        # ── Phase 4: find the OFC/consulate dropdown ──────────────────────────
        _log("--- Phase 4: Find post dropdown ---")
        dropdown_sel = await find_dropdown_selector(tab)
        if not dropdown_sel:
            await _log_page_state(tab, "no dropdown found")
            raise RuntimeError(
                "OFC/consulate dropdown not found on the reschedule page. "
                "The page layout may have changed."
            )

        options = await get_all_options(tab, dropdown_sel)
        if not options:
            raise RuntimeError("Dropdown found but it contains no options.")

        _log(
            f"found {len(options)} posts in dropdown: "
            f"{[lbl for _, lbl in options]}"
        )

        # Apply USVISA_POSTS filter if configured
        configured = env("USVISA_POSTS")
        if configured:
            want = {p.strip().lower() for p in configured.split(",") if p.strip()}
            filtered = [
                (v, lbl) for v, lbl in options
                if lbl.lower() in want or v.lower() in want
            ]
            if filtered:
                options = filtered
                _log(f"USVISA_POSTS filter applied: {[lbl for _, lbl in options]}")
            else:
                _log("USVISA_POSTS filter matched nothing — scanning all posts")

        # ── Phase 5: scan every post ──────────────────────────────────────────
        _log(f"--- Phase 5: Scan {len(options)} post(s) ---")
        results: List[SlotResult] = []
        for i, (value, label) in enumerate(options, 1):
            _log(f"post {i}/{len(options)}: {label!r}")
            results.append(await scan_post(tab, dropdown_sel, value, label))

        # ── Phase 6: report & notify ──────────────────────────────────────────
        _log("--- Phase 6: Build report ---")
        report, best = build_report(results)
        booking_note = await try_auto_book(tab, best)

        plain = re.sub(r"<[^>]+>", "", report)
        print("\n" + "=" * 60)
        print(plain)
        if booking_note:
            print(booking_note)
        print("=" * 60 + "\n")

        telegram_text = report + (f"\n\n{booking_note}" if booking_note else "")
        send_telegram(telegram_text)
        _log("=== Done ===")

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
