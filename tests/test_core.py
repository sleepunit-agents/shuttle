import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import secrets
import subprocess
import time

from shuttle import core
from shuttle.config import Config
from shuttle.runner import ClaudeRunner


def cfg(name, root):
    runner = ClaudeRunner(claude=Path("/fake/claude"), jobs_dir=root / "jobs", model_resolver=None,
                          preferred_model=None, subprocess=subprocess, time=time, secrets=secrets)
    return Config(name=name, state_dir=root / f"kit-{name}", runner=runner)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-core-"))

    def job(self, aid, **state):
        d = self.root / "jobs" / aid[:8]
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(state))

    def test_save_state_writes_only_its_own_bridge(self):
        d, s = cfg("discord", self.root), cfg("slack", self.root)
        core.save_state(d, {"who": "discord"})
        core.save_state(s, {"who": "slack"})
        self.assertEqual(json.loads(d.state_file.read_text()), {"who": "discord"})
        self.assertEqual(json.loads(s.state_file.read_text()), {"who": "slack"})
        self.assertFalse(d.state_file.with_suffix(".tmp").exists())

    def test_job_intent_and_done(self):
        c = cfg("slack", self.root)
        self.job("abcd1234-rest", intent="(bridge spawn token t1: hi", state="done")
        self.assertEqual(core.job_intent(c, "abcd1234-rest"), "(bridge spawn token t1: hi")
        self.assertTrue(core.job_done(c, "abcd1234-rest"))
        self.job("eeee0000-rest", state="working")
        self.assertEqual(core.job_intent(c, "eeee0000-rest"), "")
        self.assertFalse(core.job_done(c, "eeee0000-rest"))

    def test_missing_or_bad_job_is_unknown_not_an_error(self):
        c = cfg("slack", self.root)
        self.assertEqual(core.job_intent(c, "nope0000"), "")
        self.assertFalse(core.job_done(c, "nope0000"))
        d = self.root / "jobs" / "bad00000"
        d.mkdir(parents=True)
        (d / "state.json").write_text("{not json")
        self.assertFalse(core.job_done(c, "bad00000"))

    def test_kill_agent_uses_the_configured_claude(self):
        with mock.patch.object(core.subprocess, "run") as run:
            core.kill_agent(cfg("discord", self.root), "abc")
        self.assertEqual(run.call_args.args[0], ["/fake/claude", "kill", "abc"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_sent_logs_never_cross(self):
        d, s = cfg("discord", self.root), cfg("slack", self.root)
        s.state_dir.mkdir(parents=True)
        s.sent_log.write_text(f"{1000.5} abcd1234 D1 10\n{999.0} - D2 5\n")
        self.assertEqual(core.last_post_ts(s, "abcd1234-x"), 1000.5)
        self.assertEqual(core.last_post_ts(d, "abcd1234-x"), 0.0)
        self.assertTrue(core.session_replied(s, "abcd1234-x", "D9", 0))
        self.assertFalse(core.session_replied(d, "abcd1234-x", "D9", 0))
        self.assertTrue(core.session_replied(s, "eeee0000-x", "D2", 1000))     # channel fallback, 5s slack
        self.assertFalse(core.session_replied(s, "eeee0000-x", "D2", 1010))

    def test_a_send_tool_logging_the_whole_body_key_counts(self):
        # The send tools log $SHUTTLE_BODY as is (t-1439 task 0): a Codex body's
        # whole 16-hex key, not its first 8. Found by the live smoke (Task 4).
        s = cfg("slack", self.root)
        s.state_dir.mkdir(parents=True)
        s.sent_log.write_text(f"{1000.5} c0dec0dec0dec0de D1 10\n{1001.0} bridge D1 5\n")
        self.assertTrue(core.session_replied(s, "c0dec0dec0dec0de", "D9", 0))
        self.assertEqual(core.last_post_ts(s, "c0dec0dec0dec0de"), 1000.5)
        # the bridge's own posts never count as a body's
        self.assertFalse(core.session_replied(s, "bridge00", "D9", 0))
        self.assertFalse(core.session_replied(s, "aaaa1111bbbb2222", "D9", 0))

    def test_load_state_defaults_and_bad_file(self):
        c = cfg("discord", self.root)
        self.assertEqual(core.load_state(c, ("cursors",)), {"cursors": {}})
        core.save_state(c, {"spawned_msgs": {}})
        self.assertEqual(core.load_state(c, ("cursors",)), {"spawned_msgs": {}, "cursors": {}})
        c.state_file.write_text("{half")
        self.assertEqual(core.load_state(c), {})

    def test_state_locked_excludes_a_second_holder(self):
        import fcntl
        c = cfg("slack", self.root)
        with core.state_locked(c):
            with open(c.state_lock, "a") as f, self.assertRaises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(c.state_lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_parked_and_quiet(self):
        c = cfg("slack", self.root)
        self.job("abcd1234-x", inFlight={"tasks": 1, "kinds": ["monitor"]})
        self.assertTrue(core.job_parked(c, "abcd1234-x"))
        self.job("abcd1234-x", inFlight={"tasks": 1, "kinds": ["monitor"], "queued": 1})
        self.assertFalse(core.job_parked(c, "abcd1234-x"))
        self.assertFalse(core.job_parked(c, "nope0000"))
        self.assertLess(core.job_quiet_s(c, "abcd1234-x"), 5)
        self.assertEqual(core.job_quiet_s(c, "nope0000"), 0.0)

    def test_notices_go_through_the_configured_send_tool(self):
        c = Config(name="slack", state_dir=self.root,
                   send=Path("/bin/slack-send"), send_env={"CLAUDE_JOB_DIR": "bridge"})
        with mock.patch.object(core.subprocess, "run") as run:
            core.notify_hung(c, "D1", 1800)
            core.notify_failure(c, "D1", "rate limited")
        hung, failed = (call.args[0] for call in run.call_args_list)
        self.assertEqual(hung[:2], ["/bin/slack-send", "D1"])
        self.assertTrue(hung[2].startswith("(agent) I went silent for 30 min"))
        self.assertIn("(rate limited)", failed[2])
        self.assertEqual(run.call_args.kwargs["env"], {"CLAUDE_JOB_DIR": "bridge"})

    def test_notices_carry_the_tag(self):
        c = Config(name="slack", state_dir=self.root,
                   send=Path("/bin/slack-send"), tag="kit")
        with mock.patch.object(core.subprocess, "run") as run:
            core.notify_hung(c, "D1", 1800)
            core.notify_failure(c, "D1", "rate limited")
            core.notify_failure(c, "D1", "Login expired", parked=True)
        hung, failed, parked = (call.args[0][2] for call in run.call_args_list)
        self.assertEqual(hung, "(kit) I went silent for 30 min (hung, not thinking) and was "
                               "stopped. Poke me again if you still need it.")
        self.assertEqual(failed, "(kit) my reply died twice before I could send it "
                                 "(rate limited). Poke me again in a minute.")
        self.assertEqual(parked, "(kit) my login lapsed before I could answer (Login expired). "
                                 "I'll pick this up as soon as it's back; no need to resend.")

    def test_page_runs_the_configured_alert(self):
        c = Config(name="slack", state_dir=self.root,
                   alert=Path("/bin/pager"))
        with mock.patch.object(core.subprocess, "run") as run:
            core.page(c, "help")
        self.assertEqual(run.call_args.args[0], ["/bin/pager", "help"])


if __name__ == "__main__":
    unittest.main()


class EndedTurnTest(unittest.TestCase):
    """A body that ends its turn with a status line and a closing message made a
    judgment, whatever state it set; only the harness's own failure entries
    (text == detail, blocked) and a turn cut off mid-flight are deaths. The
    entries below are real last-timeline shapes from ~/.claude/jobs."""

    def b(self, entry, replied=False):
        return mock.Mock(session_replied=lambda aid, chan, ts: replied,
                         _last_timeline=lambda aid: entry)

    def silent(self, entry, replied=False):
        return core.session_died_silent(self.b(entry, replied), "abcd1234", {"chan": "C1"})

    def test_a_chosen_silence_is_not_a_death(self):
        for state in ("working", "blocked", "done"):
            with self.subTest(state=state):
                self.assertIsNone(self.silent({
                    "state": state, "detail": "#142 approved, Quinn merging",
                    "text": "I didn't reply to Quinn. His message only confirmed the plan."}))

    def test_harness_failures_are_deaths(self):
        for text in ("Login expired", "API Error: 529 Overloaded. This is a server-side issue",
                     "You're out of usage credits. Run /usage-credits to keep using Fable 5."):
            with self.subTest(text=text):
                self.assertIsNotNone(self.silent({"state": "blocked", "detail": text, "text": text}))

    def test_a_turn_cut_off_mid_flight_is_a_death(self):
        self.assertIsNotNone(self.silent({"state": "working", "detail": "Running taskstore li…", "text": ""}))
        self.assertEqual(self.silent(None), "no timeline")

    def test_a_reply_is_never_a_death(self):
        self.assertIsNone(self.silent({"state": "blocked", "detail": "Login expired",
                                       "text": "Login expired"}, replied=True))
