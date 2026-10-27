"""Relay (t-1570): a body asks another conversation to say something, through
loom's piece API; the bridge takes its queue and hands each to the DM it names."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shuttle import relay

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_slack_adapter import load_adapter, make_instance  # noqa: E402

PEOPLE = {"people": {
    "loren": {"name": "Loren", "tier": 2, "slack": {"T1": ["U5"]}},
    "nodm": {"name": "Ned", "tier": 2, "discord": ["123"]},
}}


class Queue(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-relay-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.inst = SimpleNamespace(state_root=self.root, agent_slug="kit", agent_name="Kit")
        env = mock.patch.dict(os.environ, {"LOOM_PIECE_HOME": str(self.root / "piece")})
        env.start()
        self.addCleanup(env.stop)

    def api(self, argv, caller=None):
        return relay.api_answer(self.inst, PEOPLE, {"caller": caller or {"kind": "agent", "agent": "kit"}, "path": "run", "body": {"argv": argv}})

    def test_queued_then_taken_once(self):
        r = self.api(["relay", "--via", "slack", "--person", "loren", "--say", "Your S11 marks: undo is in the QC menu.",
                      "--why", "she asked in #general", "--from", "chat:#general"])
        self.assertEqual(r["body"]["exit"], 0, r)
        self.assertIn("queued for Loren on slack", r["body"]["stdout"])
        [e] = json.loads(self.api(["relay-take", "--via", "slack"])["body"]["stdout"])
        self.assertEqual((e["person"], e["name"], e["from"], e["why"]), ("loren", "Loren", "chat:#general", "she asked in #general"))
        self.assertEqual(json.loads(self.api(["relay-take", "--via", "slack"])["body"]["stdout"]), [], "taken once")
        text = relay.brief(e, "Kit")
        self.assertTrue(text.startswith(f"[Relay {e['id']}: Kit's body in chat:#general asked"))
        self.assertIn("It is not from them. Why: she asked in #general]", text)

    def test_the_loop_shape_is_refused_not_a_count(self):
        args = ["relay", "--via", "slack", "--person", "loren", "--say", "same words"]
        self.assertEqual(self.api(args)["body"]["exit"], 0)
        again = self.api(args)["body"]
        self.assertEqual(again["exit"], 1)
        self.assertIn("same words already went to Loren", again["stderr"])
        # Something new to the same person is fine, however many: no cap.
        for i in range(30):
            self.assertEqual(self.api(["relay", "--via", "slack", "--person", "loren", "--say", f"update {i}"])["body"]["exit"], 0)

    def test_refusals(self):
        def err(argv, caller=None):
            b = self.api(argv, caller)
            return b["body"].get("error") or b["body"].get("stderr")
        self.assertIn("no person ghost", err(["relay", "--via", "slack", "--person", "ghost", "--say", "x"]))
        self.assertIn("has no slack id", err(["relay", "--via", "slack", "--person", "nodm", "--say", "x"]))
        self.assertIn("for now, not discord", err(["relay", "--via", "discord", "--person", "loren", "--say", "x"]))
        self.assertIn("--say is what to tell them", err(["relay", "--via", "slack", "--person", "loren", "--say", "  "]))
        self.assertIn("doesn't take --channel", err(["relay", "--via", "slack", "--person", "loren", "--say", "x", "--channel", "C1"]))
        self.assertIn("another agent relays through its own", err(["relay"], {"kind": "agent", "agent": "mark"}))
        self.assertIn("only the owner", err(["relay"], {"kind": "piece"}))
        self.assertIn("relay, relay-take", err(["rm", "-rf"]))
        self.assertEqual(self.api(["relay", "--via", "slack", "--person", "loren", "--say", "from the owner"], {"kind": "owner"})["body"]["exit"], 0)


class SlackDelivery(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-relay-slack-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load_adapter(make_instance(self.root, commands={"piece_call": "/opt/piece-call", "send": {"slack": "/x"}, "alert": "/bin/true"}))
        self.m.api = mock.Mock(return_value={"channel": {"id": "D5"}})
        self.people = {"U5": ("loren", {"name": "Loren", "tier": 2})}

    def test_a_relay_becomes_a_message_in_the_persons_dm(self):
        e = {"id": "r-1", "via": "slack", "person": "loren", "name": "Loren", "say": "hi", "why": "", "from": "chat:#general", "at": 1}
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([e]), ""))
        state = {}
        self.assertEqual(self.m.relay_in(state, self.people, run=run, now=1000.0), 1)
        self.assertEqual(run.call_args[0][0], ["/opt/piece-call", "shuttle", "relay-take", "--via", "slack"])
        self.m.api.assert_called_with("conversations.open", users="U5")
        [p] = state["pending"]["D5"]
        self.assertEqual((p["uid"], p["own"], p["relay"]), ("U5", False, "r-1"))
        self.assertIn("It is not from them.", p["text"])
        self.assertEqual(state["history"]["D5"][-1], p)
        # Not asked again inside RELAY_EVERY_S.
        self.assertEqual(self.m.relay_in(state, self.people, run=run, now=1005.0), 0)
        self.assertEqual(run.call_count, 1)

    def test_someone_not_in_this_workspace_is_paged_not_guessed(self):
        e = {"id": "r-2", "person": "ghost", "say": "x"}
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([e]), ""))
        state = {}
        self.assertEqual(self.m.relay_in(state, self.people, run=run, now=1000.0), 0)
        self.assertIn("ghost has no Slack id", self.m.page.call_args[0][0])
        self.assertNotIn("pending", state)


if __name__ == "__main__":
    unittest.main()
