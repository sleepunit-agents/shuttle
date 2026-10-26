"""inbox and nowlog: where they write, and what the board's records say."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shuttle import inbox, nowlog

ENV = ("SHUTTLE_INBOX_DIR", "SHUTTLE_NOW_DIR", "SHUTTLE_NOW_IDS")


class Dirs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-boards-"))
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        for k in ENV:
            os.environ.pop(k, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(inbox._configured.update, inbox_dir=None)
        self.addCleanup(nowlog._configured.update, now_dir=None, ids_dir=None, agent=None)

    def test_unconfigured_is_an_explicit_error(self):
        with self.assertRaises(RuntimeError):
            inbox.base()
        with self.assertRaises(RuntimeError):
            nowlog.now_dir()
        with self.assertRaises(RuntimeError):
            nowlog.ids_dir()

    def test_inbox_env_beats_configured(self):
        inbox.configure(inbox_dir=self.root / "conf")
        self.assertEqual(inbox.base(), self.root / "conf")
        os.environ["SHUTTLE_INBOX_DIR"] = str(self.root / "env")
        self.assertEqual(inbox.base(), self.root / "env")
        self.assertEqual(inbox.path_for("abcdef1234"), self.root / "env/abcdef12")

    def test_nowlog_env_beats_configured(self):
        nowlog.configure(now_dir=self.root / "now", ids_dir=self.root / "ids")
        self.assertEqual((nowlog.now_dir(), nowlog.ids_dir()), (self.root / "now", self.root / "ids"))
        os.environ["SHUTTLE_NOW_DIR"] = str(self.root / "e-now")
        os.environ["SHUTTLE_NOW_IDS"] = str(self.root / "e-ids")
        self.assertEqual((nowlog.now_dir(), nowlog.ids_dir()), (self.root / "e-now", self.root / "e-ids"))

    def test_records_and_slots_carry_the_configured_agent(self):
        nowlog.configure(now_dir=self.root / "now", ids_dir=self.root / "ids", agent="kit")
        cid = nowlog.conversation("discord", "C1", "pat", "m1", 1000.0)
        self.assertEqual(cid, "c-0001")
        convs = json.loads((self.root / "now/convs.json").read_text())
        self.assertEqual(list(convs), ["kit|discord|C1"])
        nowlog.touch("discord", "C1", 1100.0)
        self.assertEqual(json.loads((self.root / "now/convs.json").read_text())
                         ["kit|discord|C1"]["last"], 1100.0)
        nowlog.emit("discord", "picked", cid=cid)
        (events,) = (self.root / "now").glob("events-*.jsonl")
        rec = json.loads(events.read_text())
        self.assertEqual((rec["agent"], rec["bridge"], rec["kind"]), ("kit", "discord", "picked"))
        self.assertEqual(nowlog.snapshot("discord", 5, 3, 100, [], [])["agent"], "kit")

    def test_configure_leaves_what_it_is_not_given(self):
        nowlog.configure(now_dir=self.root / "now", ids_dir=self.root / "ids")
        nowlog.configure(agent="kit")
        self.assertEqual((nowlog.now_dir(), nowlog.ids_dir()), (self.root / "now", self.root / "ids"))
        nowlog.configure(now_dir=self.root / "now2")
        self.assertEqual((nowlog.now_dir(), nowlog.ids_dir()), (self.root / "now2", self.root / "ids"))
        self.assertEqual(nowlog.snapshot("slack", 5, 3, 100, [], [])["agent"], "kit")

    def test_unconfigured_agent_is_generic(self):
        self.assertEqual(nowlog.snapshot("slack", 5, 3, 100, [], [])["agent"], "agent")


if __name__ == "__main__":
    unittest.main()


class Remote(unittest.TestCase):
    """A bridge in a house: its board writes also go to loom's now piece."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-now-remote-"))
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(nowlog._configured.update, now_dir=None, ids_dir=None, agent=None)
        tok = self.root / "tok"
        tok.write_text("loom_t\n")
        os.environ.update(SHUTTLE_NOW_DIR=str(self.root / "now"), SHUTTLE_NOW_REMOTE="now",
                          LOOM_URL="http://loom.test:8787/mcp", LOOM_TOKEN_FILE=str(tok))
        self.sent = []

        def fake(req, timeout):
            self.sent.append((req.full_url, req.headers["Authorization"], json.loads(req.data), timeout))
            return mock.MagicMock()
        p = mock.patch("urllib.request.urlopen", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)

    def test_snapshot_and_events_go_to_the_piece_too(self):
        nowlog.write_now("discord", {"v": 1, "bridge": "discord"})
        nowlog.emit("discord", "spawn", chan="c1")
        self.assertEqual([s[0] for s in self.sent], ["http://loom.test:8787/api/now/snapshot", "http://loom.test:8787/api/now/event"])
        self.assertEqual(self.sent[0][1], "Bearer loom_t")
        self.assertEqual(self.sent[0][2], {"bridge": "discord", "snap": {"v": 1, "bridge": "discord"}})
        self.assertEqual(self.sent[1][2]["record"]["kind"], "spawn")
        self.assertLessEqual(self.sent[0][3], 3)
        # Still written locally as well.
        self.assertTrue((self.root / "now" / "discord.json").exists())

    def test_without_the_piece_named_nothing_is_sent(self):
        os.environ.pop("SHUTTLE_NOW_REMOTE")
        nowlog.write_now("discord", {"v": 1})
        self.assertEqual(self.sent, [])

    def test_loom_being_down_never_reaches_the_bridge(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            nowlog._remote_failed["at"] = 0.0
            nowlog.write_now("discord", {"v": 1})
            nowlog.emit("discord", "x")
