"""C4 parity (t-1439): the Claude wording moved from core and dispatch into
ClaudeRunner, and the pages and decisions must come out byte-identical.

Each test renders a page through today's code path with a real ClaudeRunner
as the bridge's runner, and compares it with the same page rendered from the
strings main had inline (copied here verbatim from shuttle 9630cae).
"""
import re
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path

from shuttle import core, dispatch
from shuttle.runner import ClaudeRunner
from tests.test_dispatch import DispatchBridge, retry
from tests.test_reap import AID, owned

# main (9630cae), core.py:441-444
MAIN_AUTH_FAILURE_RE = re.compile(
    r"login expired|oauth session expired|could not refresh .{0,20}login|not authenticated",
    re.IGNORECASE,
)


def main_registry_page(agent, name, n, conv):   # main core.py:545-546
    return (f"{agent}-{name}: `claude agents` has failed {n} ticks running; the bridge "
            f"can't reap, so dead bodies keep their {conv}. Check the --bg daemon.")


def main_auth_page(agent, name, label, attempt, why):   # main dispatch.py:97-99
    return (f"{agent}-{name} bridge giving up {label} after {attempt} "
            f"attempts — auth failure ({why}). Check the --bg daemon's token / "
            f"`claude auth status` (t-1320 shape).")


def main_session_failed(d):   # main core.py:189-194
    if d is None:
        return False
    text = (d.get("text") or "") + " " + (d.get("detail") or "")
    return d.get("state") == "blocked" and "API Error" in text


def claude_runner():
    return ClaudeRunner(claude="/x/claude", jobs_dir="/x/jobs", model_resolver=None,
                        preferred_model="m", subprocess=None, time=time,
                        secrets=types.SimpleNamespace(token_hex=lambda n=8: "0" * 16))


class Parity(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-parity-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.b = DispatchBridge(self.root)
        self.b.runner = claude_runner()

    def test_the_registry_failure_page(self):
        state = {"owned": {AID: owned()}}
        self.b.present = None
        for _ in range(self.b.cfg.alert_after):
            core.reap_and_count(self.b, state)
        self.assertEqual(self.b.pages, [main_registry_page("kit", "fake", self.b.cfg.alert_after,
                                                           self.b.CONV_NOUN)])

    def test_the_auth_give_up_page(self):
        why = "ended blocked: Login expired"
        dispatch.run_retries(self.b, {"owned": {}}, [], 0, [retry(attempt=2, why=why)], {})
        self.assertEqual(self.b.pages, [main_auth_page("kit", "fake", "in R1", 2, why)])

    def test_the_auth_decision(self):
        r = claude_runner()
        for text in ("ended blocked: Login expired", "OAuth session expired and could not be refreshed",
                     "Could not refresh your login because another Claude Code process",
                     "Not Authenticated", "ended blocked: API Error: overloaded_error", "no timeline",
                     "", "unexpected status 401 Unauthorized"):
            with self.subTest(text):
                self.assertEqual(r.auth_failure(text), bool(MAIN_AUTH_FAILURE_RE.search(text)))
        self.assertEqual(core.AUTH_FAILURE_RE.pattern, MAIN_AUTH_FAILURE_RE.pattern)
        self.assertIs(dispatch.AUTH_FAILURE_RE, core.AUTH_FAILURE_RE)

    def test_the_api_error_decision(self):
        for d in (None, {}, {"state": "blocked", "text": "API Error: 529"},
                  {"state": "blocked", "detail": "API Error: x", "text": None},
                  {"state": "done", "text": "API Error"}, {"state": "blocked", "text": "Login expired"},
                  {"state": "blocked", "text": "api error"}):
            with self.subTest(d):
                self.b.timeline[AID] = d
                self.assertEqual(core.session_failed(self.b, AID), main_session_failed(d))


if __name__ == "__main__":
    unittest.main()
