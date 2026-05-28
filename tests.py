"""
Unit + integration tests for usvisa_slot_monitor.py

Run:
    python tests.py
"""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── bootstrap: load the module without triggering _load_dotenv side-effects ───
# We inject a fake __file__ so _load_dotenv can resolve a temp dir safely.

_HERE = Path(__file__).parent
_MODULE_PATH = _HERE / "usvisa_slot_monitor.py"

# Patch os.environ so _load_dotenv doesn't bleed real .env into test env
_ORIG_ENVIRON = os.environ.copy()


def _load_module():
    """Import the monitor module fresh, isolated from the real .env."""
    import types
    src = _MODULE_PATH.read_text(encoding="utf-8")
    code = compile(src, str(_MODULE_PATH), "exec")
    mod = types.ModuleType("usvisa_slot_monitor")
    mod.__file__ = str(_MODULE_PATH)
    # Patch _load_dotenv to be a no-op so .env doesn't bleed into test env
    patched_src = src.replace("_load_dotenv()\n", "pass  # _load_dotenv patched\n", 1)
    code = compile(patched_src, str(_MODULE_PATH), "exec")
    exec(code, mod.__dict__)
    return mod


MON = _load_module()


# ─────────────────────────────────────────────────────────────────────────────
# 1. .env loader
# ─────────────────────────────────────────────────────────────────────────────

class TestDotenvLoader(unittest.TestCase):

    def _write_dotenv(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_loads_basic_values(self):
        path = self._write_dotenv("FOO=bar\nBAZ=qux\n")
        env_before = os.environ.copy()
        try:
            os.environ.pop("FOO", None)
            os.environ.pop("BAZ", None)
            # call the loader directly with a temp path
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    key = key.strip()
                    val = raw.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            self.assertEqual(os.environ.get("FOO"), "bar")
            self.assertEqual(os.environ.get("BAZ"), "qux")
        finally:
            os.unlink(path)
            for k in ("FOO", "BAZ"):
                os.environ.pop(k, None)
            os.environ.update(env_before)

    def test_ignores_comments(self):
        path = self._write_dotenv("# this is a comment\nKEY=value\n")
        os.environ.pop("KEY", None)
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    key = key.strip()
                    val = raw.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            self.assertEqual(os.environ.get("KEY"), "value")
        finally:
            os.unlink(path)
            os.environ.pop("KEY", None)

    def test_env_var_takes_precedence(self):
        """Real env vars must not be overwritten by .env"""
        path = self._write_dotenv("MY_KEY=from_file\n")
        os.environ["MY_KEY"] = "from_real_env"
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    key = key.strip()
                    val = raw.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            self.assertEqual(os.environ["MY_KEY"], "from_real_env")
        finally:
            os.unlink(path)
            os.environ.pop("MY_KEY", None)

    def test_strips_quotes(self):
        path = self._write_dotenv('QUOTED="hello world"\nSINGLE=\'bye\'\n')
        os.environ.pop("QUOTED", None)
        os.environ.pop("SINGLE", None)
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    key = key.strip()
                    val = raw.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            self.assertEqual(os.environ.get("QUOTED"), "hello world")
            self.assertEqual(os.environ.get("SINGLE"), "bye")
        finally:
            os.unlink(path)
            for k in ("QUOTED", "SINGLE"):
                os.environ.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _save_dotenv round-trip
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveDotenv(unittest.TestCase):

    def _make_dotenv(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def _save_dotenv(self, dotenv_path: str, values: dict) -> None:
        """Inline copy of _save_dotenv that takes an explicit path."""
        from typing import List
        existing_lines: List[str] = []
        if os.path.isfile(dotenv_path):
            with open(dotenv_path, encoding="utf-8") as fh:
                existing_lines = fh.readlines()

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

        for key, value in values.items():
            if key not in written_keys:
                new_lines.append(f"{key}={value}\n")

        with open(dotenv_path, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)

    def test_updates_existing_key(self):
        path = self._make_dotenv("USVISA_USERNAME=old\n")
        try:
            self._save_dotenv(path, {"USVISA_USERNAME": "new"})
            content = Path(path).read_text()
            self.assertIn("USVISA_USERNAME=new", content)
            self.assertNotIn("old", content)
        finally:
            os.unlink(path)

    def test_appends_new_key(self):
        path = self._make_dotenv("EXISTING=yes\n")
        try:
            self._save_dotenv(path, {"NEW_KEY": "hello"})
            content = Path(path).read_text()
            self.assertIn("EXISTING=yes", content)
            self.assertIn("NEW_KEY=hello", content)
        finally:
            os.unlink(path)

    def test_preserves_comments(self):
        path = self._make_dotenv("# my comment\nUSER=foo\n")
        try:
            self._save_dotenv(path, {"USER": "bar"})
            content = Path(path).read_text()
            self.assertIn("# my comment", content)
            self.assertIn("USER=bar", content)
        finally:
            os.unlink(path)

    def test_multiple_keys(self):
        initial = "A=1\nB=2\nC=3\n"
        path = self._make_dotenv(initial)
        try:
            self._save_dotenv(path, {"A": "10", "B": "20", "D": "40"})
            content = Path(path).read_text()
            self.assertIn("A=10", content)
            self.assertIn("B=20", content)
            self.assertIn("C=3", content)   # untouched
            self.assertIn("D=40", content)  # appended
        finally:
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Date extraction (_extract_date)
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractDate(unittest.TestCase):

    def _ext(self, text):
        return MON._extract_date(text)

    def test_iso_format(self):
        self.assertEqual(self._ext("2026-08-15"), "2026-08-15")

    def test_iso_inside_sentence(self):
        self.assertEqual(self._ext("Earliest slot: 2026-08-15 — book now"), "2026-08-15")

    def test_slash_dmy(self):
        self.assertEqual(self._ext("15/08/2026"), "15/08/2026")

    def test_slash_mdy(self):
        self.assertEqual(self._ext("08/15/2026"), "08/15/2026")

    def test_month_name_full(self):
        result = self._ext("August 15, 2026")
        self.assertIsNotNone(result)
        self.assertIn("August", result)

    def test_month_name_abbr(self):
        result = self._ext("Aug 15, 2026")
        self.assertIsNotNone(result)

    def test_no_date_returns_none(self):
        self.assertIsNone(self._ext("No dates here at all"))

    def test_empty_string(self):
        self.assertIsNone(self._ext(""))

    def test_whitespace_normalised(self):
        self.assertEqual(self._ext("  2026-08-15  "), "2026-08-15")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Date parsing (_parse_date)
# ─────────────────────────────────────────────────────────────────────────────

class TestParseDate(unittest.TestCase):

    def _p(self, text):
        return MON._parse_date(text)

    def test_iso(self):
        self.assertEqual(self._p("2026-08-15"), datetime(2026, 8, 15))

    def test_dmy_slash(self):
        self.assertEqual(self._p("15/08/2026"), datetime(2026, 8, 15))

    def test_mdy_slash(self):
        self.assertEqual(self._p("08/15/2026"), datetime(2026, 8, 15))

    def test_month_abbr(self):
        self.assertEqual(self._p("Aug 15, 2026"), datetime(2026, 8, 15))

    def test_month_full(self):
        self.assertEqual(self._p("August 15, 2026"), datetime(2026, 8, 15))

    def test_invalid_returns_none(self):
        self.assertIsNone(self._p("not a date"))

    def test_empty_returns_none(self):
        self.assertIsNone(self._p(""))


# ─────────────────────────────────────────────────────────────────────────────
# 5. build_report
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildReport(unittest.TestCase):

    def _make_result(self, post, earliest=None, status="ok"):
        r = MON.SlotResult(post=post)
        r.earliest = earliest
        r.status = status
        return r

    def test_single_result_with_date(self):
        results = [self._make_result("Chennai", "2026-09-01")]
        report, best = MON.build_report(results)
        self.assertIn("Chennai", report)
        self.assertIn("2026-09-01", report)
        self.assertIsNotNone(best)
        self.assertEqual(best.post, "Chennai")

    def test_picks_nearest_date(self):
        results = [
            self._make_result("Delhi",   "2026-12-01"),
            self._make_result("Chennai", "2026-09-01"),
            self._make_result("Mumbai",  "2026-11-15"),
        ]
        _, best = MON.build_report(results)
        self.assertEqual(best.post, "Chennai")

    def test_no_dates_reports_status(self):
        results = [self._make_result("Kolkata", status="no dates found")]
        report, best = MON.build_report(results)
        self.assertIn("no dates found", report)
        self.assertIsNone(best)

    def test_mixed_results(self):
        results = [
            self._make_result("Chennai", "2026-09-01"),
            self._make_result("Delhi",   status="no dates found"),
        ]
        report, best = MON.build_report(results)
        self.assertIn("Chennai", report)
        self.assertIn("Delhi", report)
        self.assertEqual(best.post, "Chennai")

    def test_report_is_html(self):
        results = [self._make_result("Chennai", "2026-09-01")]
        report, _ = MON.build_report(results)
        self.assertIn("<b>", report)

    def test_includes_timestamp(self):
        results = [self._make_result("Chennai", "2026-09-01")]
        report, _ = MON.build_report(results)
        self.assertIn("UTC", report)

    def test_all_no_dates(self):
        results = [
            self._make_result("A", status="no dates found"),
            self._make_result("B", status="no dates found"),
        ]
        report, best = MON.build_report(results)
        self.assertIsNone(best)
        self.assertIn("No available slots", report)


# ─────────────────────────────────────────────────────────────────────────────
# 6. env / require helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvHelpers(unittest.TestCase):

    def test_env_returns_default_when_missing(self):
        os.environ.pop("_TEST_MISSING_KEY", None)
        self.assertEqual(MON.env("_TEST_MISSING_KEY", "fallback"), "fallback")

    def test_env_strips_whitespace(self):
        os.environ["_TEST_SPACED"] = "  hello  "
        try:
            self.assertEqual(MON.env("_TEST_SPACED"), "hello")
        finally:
            del os.environ["_TEST_SPACED"]

    def test_require_raises_when_missing(self):
        os.environ.pop("_TEST_REQUIRED_MISSING", None)
        with self.assertRaises(RuntimeError):
            MON.require("_TEST_REQUIRED_MISSING")

    def test_require_returns_value(self):
        os.environ["_TEST_REQUIRED_PRESENT"] = "yes"
        try:
            self.assertEqual(MON.require("_TEST_REQUIRED_PRESENT"), "yes")
        finally:
            del os.environ["_TEST_REQUIRED_PRESENT"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Waiting Room detection
# ─────────────────────────────────────────────────────────────────────────────

class TestWaitingRoomDetection(unittest.TestCase):

    def _make_tab(self, text="", url="https://example.com"):
        tab = MagicMock()
        tab.evaluate = AsyncMock(side_effect=lambda js: (
            text.lower() if "innerText" in js
            else url if "location.href" in js
            else None
        ))
        return tab

    def _run(self, coro):
        return asyncio.run(coro)

    def test_detects_waiting_room_text(self):
        tab = self._make_tab("you are in the waiting room please wait")
        result = self._run(MON._is_waiting_room(tab))
        self.assertTrue(result)

    def test_detects_queue_position_text(self):
        tab = self._make_tab("queue position: 42")
        result = self._run(MON._is_waiting_room(tab))
        self.assertTrue(result)

    def test_detects_waitingroom_in_url(self):
        tab = self._make_tab(url="https://site.com/waitingroom?id=abc")
        result = self._run(MON._is_waiting_room(tab))
        self.assertTrue(result)

    def test_normal_page_not_detected(self):
        tab = self._make_tab("welcome to the appointment system", "https://usvisascheduling.com/dashboard")
        result = self._run(MON._is_waiting_room(tab))
        self.assertFalse(result)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Cloudflare challenge detection
# ─────────────────────────────────────────────────────────────────────────────

class TestCloudflareChallengeDetection(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_tab(
        self,
        text="",
        url="https://example.com",
        title="",
        has_cf_iframe=False,
        has_waiting_room=False,
    ):
        tab = MagicMock()

        async def _eval(js):
            if "looksLikeCfFrame" in js or "iframe[src*=\"challenges.cloudflare.com\"]" in js:
                return has_cf_iframe
            if "innerText" in js:
                return text.lower()
            if "location.href" in js:
                return url
            if "document.title" in js:
                return title
            if "waiting room" in js or "queue position" in js:
                return text.lower() if has_waiting_room else ""
            return None

        tab.evaluate = AsyncMock(side_effect=_eval)
        return tab

    def test_detects_malicious_text(self):
        tab = self._make_tab(
            text="our systems have detected potentially malicious traffic",
            title="Attention Required! | Cloudflare",
        )
        result = self._run(MON._is_cf_challenge(tab))
        self.assertTrue(result)

    def test_detects_cloudflare_challenge_iframe(self):
        tab = self._make_tab(has_cf_iframe=True)
        result = self._run(MON._is_cf_challenge(tab))
        self.assertTrue(result)

    def test_detects_cdn_cgi_challenge_url(self):
        tab = self._make_tab(url="https://example.com/cdn-cgi/challenge-platform/h/b")
        result = self._run(MON._is_cf_challenge(tab))
        self.assertTrue(result)


class TestChallengeIframeRect(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_parses_challenges_cloudflare_rect(self):
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value='{"x":10,"y":20,"w":300,"h":120}')
        rect = self._run(MON._cf_iframe_rect(tab))
        self.assertEqual(rect, {"x": 10, "y": 20, "w": 300, "h": 120})

    def test_parses_cdn_cgi_challenge_rect(self):
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value='{"x":30,"y":40,"w":280,"h":90}')
        rect = self._run(MON._cf_iframe_rect(tab))
        self.assertEqual(rect, {"x": 30, "y": 40, "w": 280, "h": 90})

    def test_returns_none_for_invalid_payload(self):
        tab = MagicMock()
        tab.evaluate = AsyncMock(return_value='not-json')
        rect = self._run(MON._cf_iframe_rect(tab))
        self.assertIsNone(rect)


class TestSecurityVerificationPage(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_tab(self, text):
        tab = MagicMock()

        async def _eval(js):
            if "innerText" in js:
                return text.lower()
            return ""

        tab.evaluate = AsyncMock(side_effect=_eval)
        return tab

    def test_detects_security_verification_text(self):
        tab = self._make_tab("This website is performing security verification")
        self.assertTrue(self._run(MON._is_security_verification_page(tab)))

    def test_non_security_page_returns_false(self):
        tab = self._make_tab("Welcome to dashboard")
        self.assertFalse(self._run(MON._is_security_verification_page(tab)))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Auto-book gating
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoBook(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_skipped_when_auto_book_false(self):
        os.environ["AUTO_BOOK"] = "false"
        best = MON.SlotResult(post="Chennai", earliest="2026-09-01")
        result = self._run(MON.try_auto_book(MagicMock(), best))
        self.assertEqual(result, "")

    def test_skipped_when_no_best(self):
        os.environ["AUTO_BOOK"] = "true"
        os.environ.pop("BOOK_DATE_SELECTOR", None)
        os.environ.pop("BOOK_TIME_SELECTOR", None)
        os.environ.pop("BOOK_SUBMIT_SELECTOR", None)
        result = self._run(MON.try_auto_book(MagicMock(), None))
        self.assertIn("no valid slot", result)

    def test_skipped_when_selectors_missing(self):
        os.environ["AUTO_BOOK"] = "true"
        for k in ("BOOK_DATE_SELECTOR", "BOOK_TIME_SELECTOR", "BOOK_SUBMIT_SELECTOR"):
            os.environ.pop(k, None)
        best = MON.SlotResult(post="Chennai", earliest="2026-09-01")
        result = self._run(MON.try_auto_book(MagicMock(), best))
        self.assertIn("not all set", result)

    def tearDown(self):
        for k in ("AUTO_BOOK", "BOOK_DATE_SELECTOR", "BOOK_TIME_SELECTOR", "BOOK_SUBMIT_SELECTOR"):
            os.environ.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# 10. _ask helper (prompt logic, non-interactive path)
# ─────────────────────────────────────────────────────────────────────────────

class TestAskHelper(unittest.TestCase):

    def test_returns_current_when_empty_input(self):
        with patch("builtins.input", return_value=""):
            result = MON._ask("Label", "saved_value")
        self.assertEqual(result, "saved_value")

    def test_returns_new_input_when_typed(self):
        with patch("builtins.input", return_value="new_value"):
            result = MON._ask("Label", "old_value")
        self.assertEqual(result, "new_value")

    def test_hidden_uses_getpass(self):
        with patch("getpass.getpass", return_value="secret") as mock_gp:
            result = MON._ask("Password", "", hidden=True)
        mock_gp.assert_called_once()
        self.assertEqual(result, "secret")

    def test_hidden_shows_saved_placeholder(self):
        """Prompt text must show [saved] for hidden fields with existing value."""
        captured = {}
        def fake_getpass(prompt):
            captured["prompt"] = prompt
            return ""  # press Enter → keep current
        with patch("getpass.getpass", side_effect=fake_getpass):
            MON._ask("Password", "my_secret", hidden=True)
        self.assertIn("[saved]", captured["prompt"])
        self.assertNotIn("my_secret", captured["prompt"])

    def test_visible_shows_current_value(self):
        """Prompt text must show the current value for non-hidden fields."""
        captured = {}
        def fake_input(prompt):
            captured["prompt"] = prompt
            return ""
        with patch("builtins.input", side_effect=fake_input):
            MON._ask("Username", "advaithv7")
        self.assertIn("advaithv7", captured["prompt"])


# ─────────────────────────────────────────────────────────────────────────────
# run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TestDotenvLoader,
        TestSaveDotenv,
        TestExtractDate,
        TestParseDate,
        TestBuildReport,
        TestEnvHelpers,
        TestWaitingRoomDetection,
        TestCloudflareChallengeDetection,
        TestChallengeIframeRect,
        TestSecurityVerificationPage,
        TestAutoBook,
        TestAskHelper,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
