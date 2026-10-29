"""What a body did (shuttle/deeds.py): summarized from its own session, sent
as a `did` event when its turn ends. Identifiers and counts, never words."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shuttle import deeds, inbox
from shuttle.hooks import inbox_hook

HOOK = Path(inbox_hook.__file__).resolve()
SID = "abcd1234-0000-0000-0000-000000000000"


def use(uid, name, **inp):
    return {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def result(uid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": [{"type": "text", "text": text}]}]}}


SESSION = [
    {"type": "user", "message": {"content": "Pat: can you fix the widgets and file it? my secret is hunter2"}},
    use("1", "Bash", command="taskstore claim t-1682"), result("1", '{"ok":true}'),
    use("2", "Bash", command='taskstore create "fix widgets" --priority 1'), result("2", '{"ok":true,"id":"t-1700"}'),
    use("3", "Bash", command="taskstore close t-1682 --reason done"), result("3", "{}"),
    use("4", "Bash", command="gh pr create -R acme/widgets --title x"), result("4", "https://github.com/acme/widgets/pull/371\n"),
    use("5", "Bash", command="gh pr merge 371 -R acme/widgets --squash"), result("5", ""),
    use("6", "Bash", command='approvals request --kind run --target loom-machine --what "flip" --why "t-1682"'), result("6", "a-1"),
    use("7", "Bash", command='secret request github-token --why "check CI"'), result("7", "q-1"),
    use("8", "Bash", command="git commit -m x && git push -u origin b"), result("8", ""),
    use("9", "mcp__loom__remember", title="Pat's private thing", content="secret stuff"), result("9", "ok"),
    use("10", "mcp__loom__knowledge_write", slug="ours/site/notes", content="..."), result("10", "ok"),
    use("11", "Edit", file_path="/work/a.md", old_string="a", new_string="b"), result("11", "ok"),
    use("12", "Write", file_path="/work/a.md", content="b"), result("12", "ok"),
    {"type": "cost-state", "totalCostUSD": 0.98770, "totalDuration": 89934, "startTime": 1793300019151, "totalLinesAdded": 3,
     "totalLinesRemoved": 1, "modelUsage": {"claude-opus-5-5": {"costUSD": 0.9}, "claude-haiku-5-5": {"costUSD": 0.0877}}},
]


class Summarize(unittest.TestCase):
    def test_what_it_did_without_what_was_said(self):
        rec = deeds.summarize([json.dumps(r) for r in SESSION])
        self.assertEqual(rec["tasks"], [{"id": "t-1682", "did": ["claim", "close"]}, {"id": "t-1700", "did": ["create"]}])
        self.assertEqual(rec["prs"], [{"ref": "acme/widgets#371", "did": "merged"}])
        self.assertEqual(rec["asks"], [{"kind": "approval", "what": "loom-machine"}, {"kind": "secret", "what": "github-token"}])
        self.assertEqual((rec["memories"], rec["files"], rec["commits"], rec["pushes"]), (1, 1, 1, 1))
        self.assertEqual(rec["knowledge"], [{"slug": "ours/site/notes", "did": "wrote"}])
        self.assertEqual(rec["tools"]["Bash"], 8)
        self.assertEqual(rec["tools"]["loom remember"], 1)
        self.assertEqual(rec["cost"]["usd"], 0.9877)
        self.assertEqual(rec["cost"]["ms"], 89934)
        # Nothing said, and no memory's title, ever leaves the body.
        flat = json.dumps(rec)
        for words in ("hunter2", "Pat", "private thing", "secret stuff", "fix widgets"):
            self.assertNotIn(words, flat)

    def test_garbage_is_skipped(self):
        rec = deeds.summarize(["not json", "[]", json.dumps({"type": "assistant", "message": {"content": "text"}})])
        self.assertEqual(rec["tools"], {})
        self.assertNotIn("cost", rec)


class OnStop(unittest.TestCase):
    """The inbox hook's Stop sends the record, run as a harness runs it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shuttle-deeds-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.inbox, self.now = self.dir / "inbox", self.dir / "now"
        os.environ["SHUTTLE_INBOX_DIR"] = str(self.inbox)
        self.addCleanup(os.environ.pop, "SHUTTLE_INBOX_DIR", None)
        inbox.open_inbox(SID, "slack-dm", "D0C9H93GM7Y", "slack")
        self.transcript = self.dir / "t.jsonl"
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in SESSION))

    def run_hook(self, event, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SHUTTLE_", "LOOM_"))}
        env.update(SHUTTLE_INBOX_DIR=str(self.inbox), SHUTTLE_NOW_DIR=str(self.now), HOUSE_AGENT="robin")
        hook = {"hook_event_name": event, "session_id": SID, "transcript_path": str(self.transcript), **extra}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(hook), capture_output=True, text=True, env=env)

    def events(self):
        return [json.loads(l) for f in self.now.glob("events-*.jsonl") for l in f.read_text().splitlines()]

    def test_a_turn_that_ends_says_what_it_did(self):
        r = self.run_hook("Stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        [ev] = self.events()
        self.assertEqual((ev["kind"], ev["body"], ev["bridge"], ev["agent"], ev["venue"]), ("did", "abcd1234", "slack", "robin", "slack-dm"))
        self.assertEqual(ev["prs"], [{"ref": "acme/widgets#371", "did": "merged"}])

    def test_a_tool_call_or_a_blocked_stop_sends_nothing(self):
        self.run_hook("PostToolUse")
        inbox.put(SID, [{"id": "1", "text": "x: more", "msg": {}}])
        r = self.run_hook("Stop")
        self.assertIn("block", r.stdout)
        self.assertEqual(self.events(), [])

    def test_no_transcript_no_record_and_no_noise(self):
        self.transcript.unlink()
        r = self.run_hook("Stop")
        self.assertEqual((r.returncode, r.stdout, self.events()), (0, "", []))


if __name__ == "__main__":
    unittest.main()
