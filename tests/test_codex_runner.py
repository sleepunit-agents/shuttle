"""CodexRunner (t-1439) against a fake `codex` in a temp dir.

The fake reads its prompt from stdin, records what it was started with
(argv, stdin, env, whether stdout is a file, its session) next to the -o
file, writes canned JSONL events to stdout with small sleeps, and exits. The
env decides the run (FAKE_MODE): ok, fail (the measured 401 turn.failed),
error (a fatal top-level error, no turn end), silent (no events), hang (never
ends its turn), done_hang (turn.completed, then never exits).

Real processes, real files, a real runs dir: the bridge-restart and zombie
cases need them.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from shuttle import codex_profile, core, dispatch, runner
from shuttle.config import Config
from shuttle.runners import codex as codex_mod
from shuttle.runners.codex import CodexRunner
from tests.test_dispatch import DispatchBridge

KEY = "c0dec0dec0dec0de"
WARN = {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message":
        "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review "
        "for this invocation."}}
AUTH_MSG = ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
            "url: https://api.openai.com/v1/responses, cf-ray: a4ea5dbe6821f032-DFW, "
            "request id: req_a72c5d472133491287437da47476f3fb")

FAKE = r'''
import json, os, stat, sys, time
argv = sys.argv[1:]
prompt = sys.stdin.read()
run = os.path.dirname(argv[argv.index("-o") + 1])
mode = os.environ.get("FAKE_MODE", "ok")
json.dump({"argv": sys.argv, "stdin": prompt, "env": dict(os.environ),
           "stdout_is_file": stat.S_ISREG(os.fstat(1).st_mode),
           "stderr_is_file": stat.S_ISREG(os.fstat(2).st_mode),
           "own_session": os.getsid(0) == os.getpid()},
          open(os.path.join(run, "fake.json"), "w"))
WARN = %(warn)s
def emit(e):
    sys.stdout.write(json.dumps(e) + "\n"); sys.stdout.flush(); time.sleep(0.01)
if mode == "silent":
    sys.exit(1)
emit({"type": "thread.started", "thread_id": "01a164bf-98e3-79e1-a6a8-27599561f3a9"})
emit(WARN); emit(WARN)
emit({"type": "turn.started"})
if mode in ("ok", "done_hang", "done_linger"):
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "first"}})
    emit({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "Answered Pat."}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1}})
    if mode == "done_hang":
        time.sleep(60)
    if mode == "done_linger":      # codex shutting down after its turn: writing last.txt etc.
        time.sleep(1)
        open(os.path.join(run, "exited-cleanly"), "w").write("1")
    sys.exit(0)
if mode == "fail":
    emit({"type": "error", "message": "Reconnecting... 5/5 (" + %(auth)r + ")"})
    emit({"type": "error", "message": %(auth)r})
    emit({"type": "turn.failed", "error": {"message": %(auth)r}})
    sys.exit(1)
if mode == "error":
    emit({"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"})
    emit({"type": "error", "message": "fatal: the model is gone"})
    sys.exit(1)
if mode == "hang":
    time.sleep(60)
if mode == "bg_child":             # backgrounds a child (`nohup ... &`), ends its turn, exits
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.path.join(run, "child.pid"), "w").write(str(child.pid))
    emit({"type": "turn.completed", "usage": {"input_tokens": 1}})
    sys.exit(0)
'''


def until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def proc_state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-codex-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.codex = self.root / "bin" / "codex"
        self.codex.parent.mkdir()
        self.codex.write_text(f"#!{sys.executable}\n" + FAKE % {"warn": repr(WARN), "auth": AUTH_MSG})
        self.codex.chmod(0o755)
        self.runs = self.root / "runs"
        self.inbox = self.root / "inbox"
        self.keys = iter(f"{i:016x}" for i in range(1, 100))
        self.r = self.runner()
        self.started = []
        self.addCleanup(self.kill_all)

    def runner(self, **kw):
        args = dict(codex=str(self.codex), profile="shuttle", model=None, runs_dir=self.runs,
                    inbox_dir=lambda: self.inbox, subprocess=subprocess, time=time,
                    secrets=types.SimpleNamespace(token_hex=lambda n=8: next(self.keys)))
        args.update(kw)
        return CodexRunner(**args)

    def start(self, mode="ok", prompt="line one\nhi there", r=None, model=None, **env):
        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MODE": mode, **env}
        h = (r or self.r).start(prompt, model, [], env, self.root)
        self.started.append(h["key"])
        return h["key"]

    def kill_all(self):
        for key in self.started:
            try:
                os.killpg(int((self.runs / key / "pid").read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def pid(self, key):
        return int((self.runs / key / "pid").read_text())

    def fake(self, key):
        p = self.runs / key / "fake.json"
        self.assertTrue(until(lambda: p.exists() and p.stat().st_size > 0))
        until(lambda: self.loads(p) is not None)
        return self.loads(p)

    @staticmethod
    def loads(p):
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None

    def status(self, key, r=None):
        return {row["id"]: row["status"] for row in (r or self.r).list()}.get(key)

    def wait_ended(self, key, r=None):
        self.assertTrue(until(lambda: self.status(key, r) == "ended"), self.status(key, r))

    def wait_gone(self, key):
        """Its process exited (a zombie, or reaped): the turn ending comes first."""
        pid = self.pid(key)
        self.assertTrue(until(lambda: proc_state(pid) in ("Z", None)))


class Start(Base):
    def test_argv_stdin_env_files_and_session(self):
        key = self.start(model="gpt-x", CLAUDE_CODE_SESSION_ID="s", CLAUDE_CODE_ENTRYPOINT="cli",
                         CLAUDECODE="1", CLAUDE_JOB_DIR="/jobs/abcd1234", KEEP="me")
        self.assertEqual(key, "0000000000000001")
        run = self.runs / key
        f = self.fake(key)
        self.assertEqual(f["argv"][1:], [
            "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox", "-p", "shuttle", "-m", "gpt-x",
            "-C", str(self.root), "-o", str(run / "last.txt"), "-"])
        self.assertEqual(f["argv"][0], str(self.codex))
        self.assertEqual(f["stdin"], "line one\nhi there")   # the prompt, untouched, never on argv
        env = f["env"]
        self.assertFalse([k for k in env if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"], env)
        self.assertNotIn("CLAUDE_JOB_DIR", env)    # an owner's tools key on it: a Claude job's dir
        self.assertEqual((env["SHUTTLE_BODY"], env["SHUTTLE_INBOX_DIR"], env["KEEP"]),
                         (key, str(self.inbox), "me"))
        self.assertTrue(f["stdout_is_file"] and f["stderr_is_file"] and f["own_session"], f)
        self.assertEqual(self.pid(key), int((run / "pid").read_text()))
        self.assertEqual((run / "stat").exists(), False)

    def test_no_model_no_m_flag_and_none_env_inherits_scrubbed(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "ok", "CLAUDE_CODE_X": "1", "CLAUDECODE": "1"}):
            h = self.r.start("p", None, [], None, self.root)
        self.started.append(h["key"])
        f = self.fake(h["key"])
        self.assertNotIn("-m", f["argv"])
        self.assertEqual(f["env"]["FAKE_MODE"], "ok")
        self.assertNotIn("CLAUDE_CODE_X", f["env"])
        self.assertNotIn("CLAUDECODE", f["env"])

    def test_claim_is_the_key_at_once(self):
        h = self.r.start("p", None, [], {"FAKE_MODE": "hang"}, self.root)
        self.started.append(h["key"])
        self.assertEqual(self.r.claim(h, set()), {h["key"]})
        self.assertEqual(self.r.claim(h, {h["key"]}), set())

    def test_a_deny_list_is_refused_before_anything_starts(self):
        with self.assertRaises(ValueError) as cm:
            self.r.start("p", None, ["mcp__mail"], {"FAKE_MODE": "ok"}, self.root)
        self.assertIn("mcp__mail", str(cm.exception))
        self.assertFalse(self.runs.exists())

    def test_resolve_model_is_the_configured_one(self):
        self.assertIsNone(self.r.resolve_model())
        self.assertEqual(self.runner(model="gpt-x").resolve_model(), "gpt-x")


class Lifecycle(Base):
    def test_working_then_ended_and_done(self):
        key = self.start("hang")
        self.assertTrue(until(lambda: (self.runs / key / "events.jsonl").stat().st_size > 0))
        self.assertEqual(self.r.list(), [{"id": key, "status": "working"}])
        self.assertTrue(self.r.working("working"))
        self.assertFalse(self.r.working("ended"))
        self.assertFalse(self.r.done(key))
        self.assertFalse(self.r.parked(key))
        key2 = self.start("ok")
        self.wait_ended(key2)
        self.assertTrue(self.r.done(key2))
        self.assertEqual((self.runs / key2 / "thread_id").read_text(),
                         "01a164bf-98e3-79e1-a6a8-27599561f3a9\n")

    def test_a_zombie_whose_turn_completed_is_ended(self):
        key = self.start("ok")
        pid = self.pid(key)
        self.assertTrue(until(lambda: proc_state(pid) in ("Z", None)))   # exited, nobody waited
        self.assertEqual(self.status(key, self.runner()), "ended")   # a runner built afresh
        self.assertIsNone(proc_state(pid))                             # and the zombie is gone

    def test_a_turn_that_completed_is_ended_whatever_the_pid_says(self):
        key = self.start("done_hang")
        self.assertTrue(until(lambda: self.r.done(key)))
        self.assertEqual(proc_state(self.pid(key)) in ("S", "R"), True)
        self.assertEqual(self.status(key), "ended")

    def test_a_runner_built_from_scratch_still_sees_a_live_run(self):
        key = self.start("hang")
        fresh = self.runner()                     # the bridge restarted: nothing in memory
        self.assertTrue(until(lambda: self.status(key, fresh) == "working"))
        fresh.stop(key)
        self.wait_ended(key, fresh)

    def test_a_run_that_is_not_our_child_is_read_from_proc(self):
        # After a restart the body is no child of this process: waitpid says
        # so, and /proc (its argv names the run dir) decides.
        run = self.runs / "feedfacefeedface"
        run.mkdir(parents=True)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(run / "last.txt")],
                             start_new_session=True)
        self.addCleanup(p.kill)
        (run / "pid").write_text(f"{p.pid}\n")
        with mock.patch.object(codex_mod.os, "waitpid", side_effect=ChildProcessError):
            self.assertEqual(self.status("feedfacefeedface"), "working")
        p.kill(), p.wait()
        with mock.patch.object(codex_mod.os, "waitpid", side_effect=ChildProcessError):
            self.assertEqual(self.status("feedfacefeedface"), "ended")


    def test_a_run_with_rc_written_never_waits_on_its_pid(self):
        # Its pid may since belong to another child of the bridge: reaping
        # that child here would steal its exit status (review F2).
        other = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
        self.assertTrue(until(lambda: proc_state(other.pid) == "Z"))
        run = self.runs / "0000000000000def"
        run.mkdir(parents=True)
        (run / "pid").write_text(f"{other.pid}\n")
        (run / "rc").write_text("0\n")
        self.assertEqual(self.status("0000000000000def"), "ended")
        self.assertEqual(other.wait(), 7)


class LastEntry(Base):
    def entry(self, mode):
        key = self.start(mode)
        self.wait_ended(key)
        return self.r.last_entry(key)

    def test_completed_is_done_with_the_last_agent_message(self):
        self.assertEqual(self.entry("ok"), {"state": "done", "detail": "", "text": "Answered Pat."})

    def test_turn_failed_is_the_harness_failure_shape(self):
        d = self.entry("fail")
        self.assertEqual(d, {"state": "blocked", "detail": AUTH_MSG, "text": AUTH_MSG})
        self.assertTrue(core.harness_failure(d))
        self.assertTrue(self.r.auth_failure(d["text"]))
        self.assertFalse(self.r.api_error(d))   # an auth failure is not "API error": it must park

    def test_a_fatal_error_event_without_a_turn_end_is_blocked(self):
        d = self.entry("error")
        self.assertEqual(d, {"state": "blocked", "detail": "fatal: the model is gone",
                             "text": "fatal: the model is gone"})
        self.assertTrue(self.r.api_error(d))
        self.assertFalse(self.r.auth_failure(d["text"]))

    def test_the_bypass_warnings_alone_are_not_a_failure(self):
        key = self.start("hang")
        self.assertTrue(until(lambda: '"turn.started"' in (self.runs / key / "events.jsonl").read_text()))
        d = self.r.last_entry(key)                      # two error items, both warnings
        self.assertEqual(d["state"], "working")         # mid-turn, not blocked
        self.assertFalse(self.r.api_error(d))
        self.r.stop(key)
        self.wait_ended(key)
        d = self.r.last_entry(key)                      # killed mid-turn: a turn cut off
        self.assertEqual(d["state"], "working")
        self.assertFalse(self.r.api_error(d))
        self.assertFalse(core.harness_failure(d))

    def test_gone_with_no_events_is_none(self):
        self.assertIsNone(self.entry("silent"))
        self.assertIsNone(self.r.last_entry("nosuchrun"))


class QuietStopPrune(Base):
    def test_quiet_s_is_the_events_file_age(self):
        key = self.start("ok")
        self.wait_ended(key)
        old = time.time() - 100
        os.utime(self.runs / key / "events.jsonl", (old, old))
        self.assertAlmostEqual(self.r.quiet_s(key), 100, delta=2)
        self.assertEqual(self.r.quiet_s("nosuchrun"), 0.0)

    def test_stop_kills_the_process_group(self):
        key = self.start("hang")
        self.assertTrue(until(lambda: self.status(key) == "working"))
        self.r.stop(key)
        self.wait_ended(key)
        self.assertTrue((self.runs / key).is_dir())     # kept for the reaper
        self.r.stop(key)                                 # already gone: quiet

    def test_stop_lets_a_finished_turn_exit_on_its_own(self):
        # The reaper's tick can land between turn.completed and codex's exit
        # (smoke run 3a, 2026-10-22): give it a moment rather than SIGTERM it.
        key = self.start("done_linger")
        self.assertTrue(until(lambda: self.r.done(key)))
        self.r.stop(key)
        self.assertTrue((self.runs / key / "exited-cleanly").exists())

    def test_stop_still_kills_a_finished_turn_that_wont_exit(self):
        key = self.start("done_hang")
        self.assertTrue(until(lambda: self.r.done(key)))
        t0 = time.time()
        self.r.stop(key)
        self.wait_ended(key)
        self.assertLess(time.time() - t0, codex_mod.STOP_GRACE_S + 3)

    def test_stop_long_after_the_turn_ended_does_not_wait(self):
        # The grace runs from turn.completed (events.jsonl's mtime), not from
        # the stop: a tick reaping several bodies waits STOP_GRACE_S at most
        # in all, and not at all for bodies that finished long ago (F4).
        key = self.start("done_hang")
        self.assertTrue(until(lambda: self.r.done(key)))
        old = time.time() - 60
        os.utime(self.runs / key / "events.jsonl", (old, old))
        t0 = time.time()
        self.r.stop(key)
        self.assertLess(time.time() - t0, 1.0)
        self.wait_ended(key)
        self.assertTrue(until(lambda: proc_state(self.pid(key)) in ("Z", None)))

    def test_stop_kills_what_a_body_left_behind_after_it_exited(self):
        # The leader exited; a child it backgrounded is still in its group (F3).
        key = self.start("bg_child")
        self.wait_gone(key)
        child = self.runs / key / "child.pid"
        self.assertTrue(until(lambda: child.exists() and child.read_text()))
        cpid = int(child.read_text())
        self.assertEqual(proc_state(cpid) in ("S", "R"), True)
        self.r.stop(key)
        self.assertTrue(until(lambda: proc_state(cpid) in ("Z", None)))

    def test_stop_leaves_a_reused_group_alone(self):
        # The leader is gone and its group id now names someone else's group,
        # whose members carry no SHUTTLE_BODY of this run's.
        run = self.runs / "0000000000000abd"
        run.mkdir(parents=True)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True,
                                 env={**os.environ, "SHUTTLE_BODY": "someoneelse00000"})
        self.addCleanup(lambda: (other.kill(), other.wait()))
        (run / "pid").write_text(f"{other.pid}\n")
        (run / "rc").write_text("0\n")
        self.r.stop("0000000000000abd")
        time.sleep(0.1)
        self.assertIsNone(other.poll())

    def test_stop_refuses_a_recycled_pid(self):
        run = self.runs / "0000000000000abc"
        run.mkdir(parents=True)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        self.addCleanup(lambda: (other.kill(), other.wait()))
        (run / "pid").write_text(f"{other.pid}\n")       # the pid now belongs to someone else
        self.r.stop("0000000000000abc")
        time.sleep(0.1)
        self.assertIsNone(other.poll())
        self.r.stop("nosuchrun")                          # no run dir: quiet

    def test_list_prunes_runs_ended_a_day_ago(self):
        old_key, new_key = self.start("ok"), self.start("ok")
        self.wait_gone(old_key), self.wait_gone(new_key)
        stale = time.time() - codex_mod.PRUNE_AFTER_S - 60
        for f in (self.runs / old_key).iterdir():
            os.utime(f, (stale, stale))
        self.assertEqual([row["id"] for row in self.r.list()], [new_key])
        self.assertFalse((self.runs / old_key).exists())

    def test_list_without_a_runs_dir_is_empty_and_unreadable_is_none_when_strict(self):
        self.assertEqual(self.r.list(strict=True), [])
        self.runs.write_text("not a dir")
        self.assertIsNone(self.r.list(strict=True))
        self.assertEqual(self.r.list(), [])


class CodexBridge(DispatchBridge):
    """The fake adapter with a real CodexRunner behind the core's leaf calls,
    the way the adapters' shims route them (b.agents -> core.agents(cfg) ...)."""

    def __init__(self, root, runner):
        super().__init__(root)
        self.cfg = Config(name="fake", state_dir=root / "kit-fake", alert=Path("/fake/alert"),
                          agent="kit", tag="kit", runner=runner)
        self.runner = runner

    def agents(self, strict=False):
        return core.agents(self.cfg, strict)

    def kill_agent(self, aid):
        self.killed.append(aid)
        core.kill_agent(self.cfg, aid)

    def job_quiet_s(self, aid):
        return core.job_quiet_s(self.cfg, aid)

    def job_parked(self, aid):
        return core.job_parked(self.cfg, aid)

    def job_done(self, aid):
        return core.job_done(self.cfg, aid)

    def _last_timeline(self, aid):
        return core.last_timeline(self.cfg, aid)

    def session_replied(self, aid, chan, since):
        return core.session_replied(self.cfg, aid, chan, since)

    def last_post_ts(self, aid):
        return core.last_post_ts(self.cfg, aid)


class Reaper(Base):
    """core.reap_and_count over real Codex runs decides as it does for Claude."""

    def setUp(self):
        super().setUp()
        self.b = CodexBridge(self.root, self.r)
        self.b.cfg.state_dir.mkdir(parents=True)
        self.events = self.b.nowlog.emit.__self__.events

    def rec(self, **kw):
        # Seen idle a while ago: a body that ended without turn.completed (its
        # process died) is reaped after reap_grace, as for Claude.
        r = {"idle_since": time.time() - self.b.cfg.reap_grace - 1, "chan": "R1", "msgs": [{"id": "1", "text": "hi"}], "attempt": 1,
             "spawned_at": time.time() - 60, "cid": "c-1", "ack_msg": "1"}
        r.update(kw)
        return r

    def ended(self, mode, reply=False):
        key = self.start(mode)
        self.wait_gone(key)
        if reply:   # what the send tool logs: "<epoch> <SHUTTLE_BODY> <chan> <bytes>", the whole key
            self.b.cfg.sent_log.write_text(f"{time.time():.3f} {key} R1 12\n")
        return key, {"owned": {key: self.rec()}}

    def test_done_is_reaped_the_same_tick(self):
        key, state = self.ended("ok", reply=True)
        state["owned"][key]["idle_since"] = 0     # done: no reap_grace
        self.assertEqual(core.reap_and_count(self.b, state), (0, []))
        self.assertEqual(state["owned"], {})
        self.assertEqual(self.events[-1][2]["outcome"], "replied")

    def test_a_zombie_that_completed_is_reaped_as_done_not_held(self):
        key = self.start("ok")
        self.b.cfg.sent_log.write_text(f"{time.time():.3f} {key} R1 12\n")
        pid = self.pid(key)
        self.assertTrue(until(lambda: proc_state(pid) in ("Z", None)))
        state = {"owned": {key: self.rec(idle_since=0)}}   # it was working last tick
        self.assertEqual(core.reap_and_count(self.b, state), (0, []))
        self.assertEqual((state["owned"], self.b.hung), ({}, []))

    def test_a_working_body_is_live(self):
        key = self.start("hang")
        self.assertTrue(until(lambda: self.status(key) == "working"))
        state = {"owned": {key: self.rec(idle_since=0)}}
        self.assertEqual(core.reap_and_count(self.b, state), (1, []))
        self.assertIn(key, state["owned"])

    def test_no_reply_and_no_timeline_is_retried(self):
        key, state = self.ended("silent")
        live, retries = core.reap_and_count(self.b, state)
        self.assertEqual((live, [r["why"] for r in retries]), (0, ["no timeline"]))

    def test_a_body_killed_mid_turn_is_retried_as_cut_off(self):
        key = self.start("hang")
        self.assertTrue(until(lambda: '"turn.started"' in (self.runs / key / "events.jsonl").read_text()))
        self.r.stop(key)
        self.wait_gone(key)
        state = {"owned": {key: self.rec()}}
        live, retries = core.reap_and_count(self.b, state)
        self.assertEqual([r["why"] for r in retries], ["ended working: cut off before its turn finished"])

    def test_a_failed_turn_is_retried_as_an_api_error(self):
        key, state = self.ended("error")
        live, retries = core.reap_and_count(self.b, state)
        self.assertEqual([r["why"] for r in retries], ["API error"])

    def test_an_auth_failure_is_retried_then_parked_and_paged(self):
        key, state = self.ended("fail")
        live, retries = core.reap_and_count(self.b, state)
        [retry] = retries
        self.assertTrue(retry["why"].startswith("ended blocked: unexpected status 401 Unauthorized"),
                        retry["why"])
        retry["attempt"] = self.b.cfg.max_attempts           # its second body died the same way
        dispatch.run_retries(self.b, state, [], 0, [retry], {})
        self.assertEqual([p["chan"] for p in state["auth_parked"]], ["R1"])
        self.assertEqual(self.b.parked_notices, ["R1"])
        [page] = self.b.pages
        self.assertIn("auth failure", page)
        self.assertIn("codex login status", page)

    def test_a_clean_end_replays_what_was_parked(self):
        key, state = self.ended("ok", reply=True)
        state["auth_parked"] = [{"chan": "P1", "attempt": 2, "msgs": [{"id": "9"}],
                                 "why": "ended blocked: unexpected status 401 Unauthorized",
                                 "parked_at": time.time() - 600, "checked": True}]
        state["owned"][key]["spawned_at"] = time.time() - 60
        core.reap_and_count(self.b, state)
        self.assertEqual([r["chan"] for r in state["retry_queue"]], ["P1"])

    def test_the_registry_page_names_the_runs_dir(self):
        self.runs.write_text("not a dir")
        state = {"owned": {"x" * 16: self.rec()}}
        for _ in range(self.b.cfg.alert_after):
            core.reap_and_count(self.b, state)
        [page] = self.b.pages
        self.assertTrue(page.startswith(f"kit-fake: the codex runs dir {self.runs} has been "
                                        f"unreadable {self.b.cfg.alert_after} ticks running"), page)
        self.assertIn("keep their rooms", page)


class Choosing(Base):
    class Inst:
        def __init__(self, root, raw):
            self.raw, self.path, self.dir = raw, root / "shuttle.json", root

        def expand(self, value):
            p = Path(os.path.expanduser(str(value)))
            return p if p.is_absolute() else self.dir / p

    def test_build_from_the_instance_and_the_adapter(self):
        b = types.SimpleNamespace(STATE_DIR=self.root / "state" / "kit-discord",
                                  inbox=types.SimpleNamespace(base=lambda: self.inbox),
                                  subprocess="SP", time="T", secrets="S")
        r = runner.build_runner(self.Inst(self.root, {"runner": {"kind": "codex"}}), b)
        self.assertIsInstance(r, CodexRunner)
        self.assertEqual((r.codex, r.profile, r.model, r.runs_dir, r.inbox_dir(), r.subprocess),
                         ("codex", "shuttle", None, b.STATE_DIR / "runs", self.inbox, "SP"))
        r = runner.build_runner(self.Inst(self.root, {"runner": {
            "kind": "codex", "codex": "bin/codex", "profile": "p", "model": "gpt-x",
            "runs_dir": "~/runs"}}), b)
        self.assertEqual((r.codex, r.profile, r.model, r.runs_dir),
                         (str(self.root / "bin/codex"), "p", "gpt-x", Path.home() / "runs"))

    def test_startup_errors(self):
        home = self.root / "codex-home"
        hook = self.root / "inbox_hook.py"
        inst = self.Inst(self.root, {"runner": {"kind": "codex", "codex": str(self.codex)}})
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
                mock.patch.object(codex_profile, "DEFAULT_HOOK", hook):
            errs = runner.startup_errors(inst)
            self.assertTrue(any("inbox hook" in e for e in errs), errs)
            self.assertTrue(any("shuttle.config.toml is missing" in e for e in errs), errs)
            hook.write_text("")
            codex_profile.write(home, "shuttle", hook)
            self.assertEqual(runner.startup_errors(inst), [])
            codex_profile.write(home, "shuttle", self.root / "elsewhere.py", force=True)
            [e] = runner.startup_errors(inst)
            self.assertIn("not the shuttle's profile", e)
            codex_profile.write(home, "shuttle", hook, force=True)
            inst.raw["runner"]["codex"] = str(self.root / "nope")
            [e] = runner.startup_errors(inst)
            self.assertIn("runner.codex", e)
            inst.raw["runner"]["codex"] = "no-such-codex-binary"
            [e] = runner.startup_errors(inst)
            self.assertIn("no-such-codex-binary", e)

    def test_deny_errors(self):
        inst = self.Inst(self.root, {"runner": {"kind": "codex"}, "always_deny": []})
        self.assertEqual(runner.deny_errors(inst, []), [])
        inst.raw["always_deny"] = ["mcp__x"]
        [e] = runner.deny_errors(inst)
        self.assertIn("always_deny", e)
        [_, e] = runner.deny_errors(inst, ["31"])
        self.assertIn("guest venues (31)", e)
        self.assertEqual(runner.deny_errors(self.Inst(self.root, {"always_deny": ["mcp__x"]}), ["31"]), [])


class Profile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-profile-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_file_is_the_measured_layout(self):
        import tomllib
        text = codex_profile.render(Path("/s/shuttle/hooks/inbox_hook.py"))
        cmd = "python3 /s/shuttle/hooks/inbox_hook.py"
        self.assertIn('[[hooks.PostToolUse]]\nmatcher = "*"\n[[hooks.PostToolUse.hooks]]\n'
                      f'type = "command"\ncommand = "{cmd}"\n', text)
        self.assertIn(f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "{cmd}"\n', text)
        self.assertEqual(tomllib.loads(text), {"hooks": {
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}})
        odd = tomllib.loads(codex_profile.render(Path('/a b/"q"/h.py')))
        self.assertEqual(odd["hooks"]["Stop"][0]["hooks"][0]["command"], "python3 '/a b/\"q\"/h.py'")

    def test_the_default_hook_is_the_shuttles(self):
        self.assertEqual(codex_profile.DEFAULT_HOOK,
                         Path(codex_profile.__file__).resolve().parent / "hooks" / "inbox_hook.py")

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "shuttle.codex_profile", *args],
                              capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent,
                              env={**os.environ, **(env or {})})

    def test_write_then_check(self):
        home = self.root / "home"
        r = self.cli("--codex-home", str(home), "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing", r.stderr)
        r = self.cli("--codex-home", str(home), "--hook", "/x/hook.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((home / "shuttle.config.toml").read_text(),
                         codex_profile.render(Path("/x/hook.py")))
        self.assertEqual(self.cli("--codex-home", str(home), "--hook", "/x/hook.py", "--check").returncode, 0)
        r = self.cli("--codex-home", str(home), "--check")   # written for another hook
        self.assertEqual(r.returncode, 1)
        self.assertIn("not the shuttle's profile", r.stderr)
        r = self.cli("--codex-home", str(home))                # ours, for another hook: refused
        self.assertEqual(r.returncode, 1)
        r = self.cli("--hook", "/x/hook.py", env={"CODEX_HOME": str(self.root / "env-home")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "env-home" / "shuttle.config.toml").is_file())

    def test_a_bad_profile_name_is_refused(self):
        for bad in ("../x", "a/b", "", "a.b", "a b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    codex_profile.write(self.root, bad)
                self.assertIn("profile name", codex_profile.check(self.root, bad))
        r = self.cli("--codex-home", str(self.root / "h"), "--profile", "../escape")
        self.assertEqual(r.returncode, 2)
        self.assertIn("profile name", r.stderr)
        self.assertFalse((self.root / "escape.config.toml").exists())
        self.assertEqual(codex_profile.write(self.root / "h", "ok_name-2"),
                         self.root / "h" / "ok_name-2.config.toml")

    def test_a_different_profile_file_is_kept_unless_forced(self):
        home = self.root / "home"
        home.mkdir()
        mine = home / "shuttle.config.toml"
        mine.write_text("model = \"mine\"\n")
        r = self.cli("--codex-home", str(home))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--force", r.stderr)
        self.assertEqual(mine.read_text(), "model = \"mine\"\n")
        with self.assertRaises(FileExistsError):
            codex_profile.write(home)
        r = self.cli("--codex-home", str(home), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(mine.read_text(), codex_profile.render())
        self.assertEqual(self.cli("--codex-home", str(home)).returncode, 0)   # ours already: fine


if __name__ == "__main__":
    unittest.main()
