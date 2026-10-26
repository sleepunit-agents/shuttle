"""The harness-neutral inbox hook (t-1439), run as a real subprocess the way a
harness runs it (`python3 shuttle/hooks/inbox_hook.py`, hook JSON on stdin),
against a temp inbox dir. Claude-shaped and Codex-shaped events both deliver;
SHUTTLE_BODY names the body whenever it is set.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shuttle import inbox
from shuttle.hooks import inbox_hook

HOOK = Path(inbox_hook.__file__).resolve()
KEY = "c0dex0b0d1e5ke11"                          # a runner-picked body key
THREAD = "019a0b1c-2d3e-7f40-8a5b-6c7d8e9f0a1b"   # a Codex thread id (stdin session_id)
CLAUDE_SID = "abcd1234-0000-0000-0000-000000000000"
ENV_KEYS = ("SHUTTLE_INBOX_DIR", "SHUTTLE_INSTANCE", "SHUTTLE_BODY", "CLAUDE_CODE_SESSION_ID")


def item(mid, text="hi"):
    return {"id": str(mid), "text": f"someone: {text}", "msg": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shuttle-inbox-hook-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        self.env["SHUTTLE_INBOX_DIR"] = str(self.dir)
        # the test process's own inbox calls use the same dir
        patcher = mock.patch.dict(os.environ, {"SHUTTLE_INBOX_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, aid, *items):
        inbox.open_inbox(aid, "#general", "C1", "discord")
        if items:
            inbox.put(aid, list(items))

    def run_hook(self, event, session_id, stdin=None, cwd=None, **env_extra):
        h = {"session_id": session_id, "hook_event_name": event}
        if isinstance(stdin, dict):
            h.update(stdin)
            stdin = None
        env = dict(self.env, **env_extra)
        r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True,
                           input=json.dumps(h) if stdin is None else stdin,
                           env=env, cwd=cwd or self.dir, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr


class Delivery(Base):
    def test_codex_shaped_event_with_shuttle_body_delivers(self):
        self.open(KEY, item(1, "and also this"))
        codex = {"turn_id": "t1", "tool_name": "Bash", "tool_input": {}, "tool_response": "",
                 "tool_use_id": "u1", "model": "gpt", "permission_mode": "bypassPermissions"}
        out, err = self.run_hook("PostToolUse", THREAD, codex, SHUTTLE_BODY=KEY)
        self.assertIn("someone: and also this", out["hookSpecificOutput"]["additionalContext"], err)
        self.assertEqual([i["id"] for i in inbox.delivered(KEY)], ["1"])

    def test_claude_shaped_event_delivers_by_session_id(self):
        self.open(CLAUDE_SID, item(1, "and also this"))
        out, err = self.run_hook("PostToolUse", CLAUDE_SID, {"tool_name": "Bash"},
                                 CLAUDE_CODE_SESSION_ID=CLAUDE_SID)
        self.assertIn("someone: and also this", out["hookSpecificOutput"]["additionalContext"], err)

    def test_shuttle_body_beats_stdin_and_claude_env(self):
        # A Codex body launched from a Claude session: the launcher's session id
        # is inherited in env and both it and the thread id have inboxes here.
        self.open(KEY, item(1, "for the codex body"))
        self.open(THREAD, item(2, "not mine (thread id)"))
        self.open(CLAUDE_SID, item(3, "not mine (launcher)"))
        out, _ = self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY,
                               CLAUDE_CODE_SESSION_ID=CLAUDE_SID)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("for the codex body", ctx)
        self.assertNotIn("not mine", ctx)
        self.assertEqual(inbox.delivered(THREAD), [])
        self.assertEqual(inbox.delivered(CLAUDE_SID), [])

    def test_no_inbox_for_the_body_is_silent(self):
        self.open(THREAD, item(1))     # the stdin id has one, but SHUTTLE_BODY names the body
        self.assertEqual(self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY), (None, ""))
        self.assertEqual(self.run_hook("Stop", THREAD, SHUTTLE_BODY=KEY), (None, ""))

    def test_post_tool_use_returns_additional_context_once(self):
        self.open(KEY, item(1, "one"), item(2, "two"))
        out, _ = self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("New messages in #general", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY)[0])

    def test_stop_blocks_while_pending_and_lets_go_when_empty(self):
        self.open(KEY, item(1, "wait"))
        out, _ = self.run_hook("Stop", THREAD, {"stop_hook_active": False}, SHUTTLE_BODY=KEY)
        self.assertEqual(out["decision"], "block")
        self.assertIn("someone: wait", out["reason"])
        self.assertIn("Handle these before you end", out["reason"])
        self.assertIsNone(self.run_hook("Stop", THREAD, {"stop_hook_active": True}, SHUTTLE_BODY=KEY)[0])
        inbox.put(KEY, [item(2)])        # new ones block even on the second stop
        self.assertEqual(self.run_hook("Stop", THREAD, {"stop_hook_active": True},
                                       SHUTTLE_BODY=KEY)[0]["decision"], "block")

    def test_parked_notice_once(self):
        self.open(CLAUDE_SID)
        out, _ = self.run_hook("Stop", CLAUDE_SID, {"background_tasks": [{"kind": "monitor"}]})
        self.assertEqual(out, {"decision": "block", "reason": inbox_hook.PARKED})
        self.assertIsNone(self.run_hook("Stop", CLAUDE_SID, {"stop_hook_active": True,
                                                             "session_crons": [{}]})[0])

    def test_subagent_and_other_events_never_take(self):
        self.open(CLAUDE_SID, item(1))
        self.assertIsNone(self.run_hook("PostToolUse", CLAUDE_SID, {"agent_id": "a1"})[0])
        self.assertIsNone(self.run_hook("UserPromptSubmit", CLAUDE_SID)[0])
        self.assertEqual(inbox.delivered(CLAUDE_SID), [])

    def test_closed_inbox_is_silent(self):
        self.open(KEY, item(1))
        inbox.close(KEY)
        self.assertIsNone(self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY)[0])

    def test_runs_from_anywhere_as_a_file(self):
        # A Codex profile points at the file: no SHUTTLE_HOME, no PYTHONPATH, another cwd.
        self.open(KEY, item(1))
        env = {k: v for k, v in self.env.items() if k not in ("PYTHONPATH", "SHUTTLE_HOME")}
        r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, cwd="/",
                           input=json.dumps({"session_id": THREAD, "hook_event_name": "Stop"}),
                           env=dict(env, SHUTTLE_BODY=KEY), timeout=20)
        self.assertEqual(json.loads(r.stdout)["decision"], "block", r.stderr)


class InboxDir(Base):
    def write_instance(self, inbox_dir):
        root = Path(tempfile.mkdtemp(prefix="shuttle-inst-", dir=self.dir))
        p = root / "shuttle.json"
        p.write_text(json.dumps({"agent": {"slug": "kit"}, "secrets": "s", "inbox_dir": str(inbox_dir)}))
        return p

    def test_shuttle_instance_env(self):
        d = self.dir / "from-instance"
        p = self.write_instance(d)
        with mock.patch.dict(os.environ, {"SHUTTLE_INBOX_DIR": str(d)}):
            self.open(KEY, item(1))
        self.env.pop("SHUTTLE_INBOX_DIR")
        out, err = self.run_hook("Stop", THREAD, SHUTTLE_BODY=KEY, SHUTTLE_INSTANCE=str(p))
        self.assertEqual(out["decision"], "block", err)

    def test_order(self):
        a, b = self.dir / "a", self.dir / "b"
        pa, pb = self.write_instance(a), self.write_instance(b)
        env = {"SHUTTLE_INBOX_DIR": str(self.dir / "env")}
        self.assertEqual(inbox_hook.inbox_dir(pa, "/fb", env), self.dir / "env")
        self.assertEqual(inbox_hook.inbox_dir(pa, "/fb", {"SHUTTLE_INSTANCE": str(pb)}), b)
        self.assertEqual(inbox_hook.inbox_dir(pa, "/fb", {}), a)
        self.assertEqual(inbox_hook.inbox_dir(None, "/fb", {}), Path("/fb"))
        with self.assertRaises(RuntimeError):
            inbox_hook.inbox_dir(None, None, {})
        with self.assertRaises(RuntimeError):
            inbox_hook.inbox_dir(self.dir / "missing.json", None, {})

    def test_body_id(self):
        self.assertEqual(inbox_hook.body_id({"session_id": THREAD}, {"SHUTTLE_BODY": KEY}), KEY)
        self.assertEqual(inbox_hook.body_id({"session_id": CLAUDE_SID},
                                            {"CLAUDE_CODE_SESSION_ID": "x"}), CLAUDE_SID)
        self.assertEqual(inbox_hook.body_id({}, {}), "")


class FailOpen(Base):
    def test_malformed_stdin_is_silent(self):
        self.open(KEY, item(1))
        for bad in ("{nope", "", "[1, 2]", '"str"', "null"):
            self.assertEqual(self.run_hook("PostToolUse", THREAD, stdin=bad, SHUTTLE_BODY=KEY),
                             (None, ""), bad)
        self.assertEqual(len(inbox.take(KEY)), 1)    # nothing was taken

    def test_internal_error_exits_0_and_says_why(self):
        self.env.pop("SHUTTLE_INBOX_DIR")
        out, err = self.run_hook("PostToolUse", THREAD, SHUTTLE_BODY=KEY,
                                 SHUTTLE_INSTANCE=str(self.dir / "missing.json"))
        self.assertIsNone(out)
        self.assertIn("inbox-hook", err)
        self.assertIn("missing.json", err)

    def test_internal_error_in_process(self):
        self.open(KEY, item(1))
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(inbox, "take", side_effect=OSError("disk gone")), \
                mock.patch.dict(os.environ, {"SHUTTLE_BODY": KEY}):
            rc = inbox_hook.run(label="my-hook", stdout=stdout, stderr=stderr,
                                stdin=io.StringIO(json.dumps({"session_id": THREAD,
                                                              "hook_event_name": "Stop"})))
        self.assertEqual((rc, stdout.getvalue()), (0, ""))
        self.assertIn("my-hook: OSError('disk gone')", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
