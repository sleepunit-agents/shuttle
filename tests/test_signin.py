"""Signing a harness in from loom's page (shuttle/signin.py, tools/loom-shuttle):
the harness's own login in a pty, the code handed over once, the token into the
secrets store on stdin and never into a state file or onto the page."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shuttle import signin  # noqa: E402

TOKEN = "sk-ant-oat01-" + "Q" * 60

FAKE_CLAUDE = f"""#!/usr/bin/env python3
import sys
print("Opening browser... \\x1b]8;id=x;https://claude.com/cai/oauth/authorize?code=true&state=abc\\x1b\\\\https://claude.com/cai/oauth/authorize?code=true&state=abc\\x1b]8;;\\x1b\\\\", flush=True)
print("Paste code here if prompted >", end="", flush=True)
code = sys.stdin.readline().strip()
if code == "good-code#state":
    print("\\nYour OAuth token (valid for 1 year):\\n\\n{TOKEN}\\n", flush=True)
else:
    print("\\nOAuth error: invalid code", flush=True); sys.exit(1)
"""

FAKE_CLAUDE_LOGIN = f"""#!/usr/bin/env python3
import json, os, sys
home = os.environ.get("CLAUDE_CONFIG_DIR")
mark = os.path.join(home, ".credentials.json") if home else os.path.join(os.environ["SHUTTLE_SIGNIN_DIR"], "..", "logged-in")
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": os.path.exists(mark)}})); sys.exit(0)
if sys.argv[1:3] == ["auth", "login"]:
    print("Visit https://claude.com/cai/oauth/authorize?code=true&state=s", flush=True)
    print("Paste code here if prompted >", end="", flush=True)
    if sys.stdin.readline().strip() == "good-code#state":
        open(mark, "w").write(json.dumps({{"claudeAiOauth": {{"accessToken": "a", "refreshToken": "r"}}}})); print("Login successful.", flush=True)
    else:
        print("OAuth error", flush=True); sys.exit(1)
    sys.exit(0)
""" + FAKE_CLAUDE.split("\n", 1)[1]

FAKE_CODEX = """#!/usr/bin/env python3
import os, time
print("Visit https://auth.openai.com/codex/device and enter ABCD-12345", flush=True)
time.sleep(1)
open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), "w").write('{"tokens": "t"}')
print("Successfully logged in", flush=True)
"""

FAKE_LOOM = """#!/bin/sh
# loom secrets add <name> --force: the value on stdin, kept as <name> in $FAKE_STORE
[ "$1 $2" = "secrets add" ] || { echo "no" >&2; exit 2; }
cat > "$FAKE_STORE/$3"
"""


class SignIn(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-signin-"))
        bin_ = self.root / "bin"
        bin_.mkdir()
        for name, body in (("claude", FAKE_CLAUDE_LOGIN), ("codex", FAKE_CODEX), ("loom", FAKE_LOOM)):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        (self.root / "store").mkdir()
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{bin_}:{os.environ['PATH']}", "SHUTTLE_SIGNIN_DIR": str(self.root / "state"),
            "FAKE_STORE": str(self.root / "store"), "SHUTTLE_SIGNIN_LOOM": str(bin_ / "loom")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def files_have_no_token(self):
        for p in (self.root / "state").iterdir():
            self.assertNotIn(TOKEN, p.read_text(errors="replace"), p.name)

    def test_claude_link_then_code_then_the_token_into_the_store(self):
        sid, st = signin.start("claude")
        self.assertEqual(st["status"], "waiting", st)
        self.assertTrue(st["link"].startswith("https://claude.com/cai/oauth/authorize?"))
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "state" / f"{sid}.json").stat().st_mode), 0o600)
        bad = signin.submit_code(sid, "x y")
        self.assertIn("doesn't look like", bad["error"])
        done = signin.submit_code(sid, "good-code#state")
        self.assertEqual(done["status"], "done", done)
        self.assertEqual((self.root / "store" / "claude-oauth-token").read_text(), TOKEN)
        self.assertFalse((self.root / "state" / f"{sid}.code").exists(), "the code is read once and gone")
        self.files_have_no_token()

    def test_a_wrong_code_fails_and_says_so_without_a_token(self):
        sid, _ = signin.start("claude")
        st = signin.submit_code(sid, "wrong-code-xx")
        self.assertEqual(st["status"], "failed")
        self.assertIn("didn't take the code", st["error"])
        self.assertFalse((self.root / "store" / "claude-oauth-token").exists())

    def test_a_session_login_is_this_machines_and_stores_nothing(self):
        sid, st = signin.start("claude-session")
        self.assertEqual(st["status"], "waiting", st)
        self.assertNotIn("secret", st)
        done = signin.submit_code(sid, "good-code#state", wait_s=20)
        end = __import__("time").time() + 20
        while (signin.read(sid) or {}).get("status") not in ("done", "failed") and __import__("time").time() < end:
            __import__("time").sleep(0.3)
        self.assertEqual(signin.read(sid)["status"], "done", signin.read(sid))
        self.assertEqual(list((self.root / "store").iterdir()), [], "nothing in the secrets store")

    def test_a_login_for_houses_is_signed_in_privately_and_kept_in_the_store(self):
        sid, st = signin.start("claude-broker", secret="claude-session-main.json")
        self.assertEqual(st["status"], "waiting", st)
        signin.submit_code(sid, "good-code#state", wait_s=20)
        end = __import__("time").time() + 20
        while (signin.read(sid) or {}).get("status") not in ("done", "failed") and __import__("time").time() < end:
            __import__("time").sleep(0.3)
        self.assertEqual(signin.read(sid)["status"], "done", signin.read(sid))
        stored = json.loads((self.root / "store" / "claude-session-main.json").read_text())
        self.assertEqual(stored["claudeAiOauth"]["refreshToken"], "r")
        self.assertFalse((self.root / "logged-in").exists(), "this machine's own login wasn't touched")

    def test_device_login_finishes_by_itself(self):
        sid, st = signin.start("codex")
        self.assertEqual(st["status"], "waiting", st)
        self.assertEqual(st["user_code"], "ABCD-12345")
        end = time.time() + 15
        while time.time() < end and (signin.read(sid) or {}).get("status") != "done":
            time.sleep(0.2)
        self.assertEqual(signin.read(sid)["status"], "done")
        self.assertEqual((self.root / "store" / "codex-auth.json").read_text(), '{"tokens": "t"}')

    def test_the_page_and_its_actions(self):
        def tool(*args, stdin="{}"):
            r = subprocess.run([sys.executable, str(ROOT / "tools/loom-shuttle"), "ui", *args, "--json"],
                               input=stdin, capture_output=True, text=True, env=os.environ)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        page = tool("page", stdin=json.dumps({"path": "signin"}))
        self.assertEqual(page["title"], "Sign in")
        start = tool("act", stdin=json.dumps({"action": "signin-start", "path": "signin", "fields": {"harness": "claude"}}))
        self.assertTrue(start["ok"], start)
        sid = start["page"].split("/", 1)[1]
        one = tool("page", stdin=json.dumps({"path": start["page"]}))
        blocks = one["sections"][0]["blocks"]
        self.assertEqual(blocks[0]["href"][:40], "https://claude.com/cai/oauth/authorize?c")
        self.assertEqual(blocks[1]["fields"][0]["kind"], "secret")
        fin = tool("act", stdin=json.dumps({"action": "signin-code", "path": start["page"], "fields": {"id": sid, "code": "good-code#state"}}))
        self.assertTrue(fin["ok"], fin)
        self.assertNotIn(TOKEN, json.dumps(tool("page", stdin=json.dumps({"path": start["page"]}))))
        self.assertFalse(tool("act", stdin=json.dumps({"action": "signin-start", "fields": {"harness": "nope"}}))["ok"])
        self.assertEqual(tool("page", stdin=json.dumps({"path": "signin/../../etc"}))["title"], "Not found")
