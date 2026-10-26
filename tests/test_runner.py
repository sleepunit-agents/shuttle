"""The runner seam (t-1438): ClaudeRunner against a fake `claude` and a temp jobs dir.

The fake `claude` is a script that prints a canned `agents --json` (or exits
with the code it is told) and logs every call, so list() and stop() run a
real process without touching a real body. start() and resolve_model() go
through a fake subprocess, the way the adapters' tests swap b.subprocess.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from shuttle import runner
from shuttle.runner import ClaudeRunner, Runner

TOKEN = "feedface00000000"


class FakeInstance:
    def __init__(self, raw):
        self.raw, self.path = raw, Path("/i/shuttle.json")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-runner-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.jobs = self.root / "jobs"
        self.log = self.root / "calls.log"
        self.claude = self.root / "claude"
        self.claude.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"open({str(self.log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "rc = int(os.environ.get('FAKE_RC', '0'))\n"
            "if sys.argv[1:3] == ['agents', '--json'] and not rc:\n"
            "    sys.stdout.write(os.environ.get('FAKE_AGENTS', ''))\n"
            "sys.exit(rc)\n")
        self.claude.chmod(0o755)
        self.r = self.runner()

    def runner(self, **kw):
        args = dict(claude=self.claude, jobs_dir=self.jobs, model_resolver=None,
                    preferred_model="claude-pref", subprocess=subprocess, time=time,
                    secrets=types.SimpleNamespace(token_hex=lambda n=8: TOKEN))
        args.update(kw)
        return ClaudeRunner(**args)

    def env(self, **kw):
        patch = {k: v for k, v in kw.items()}
        old = {k: os.environ.get(k) for k in patch}
        os.environ.update(patch)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])

    def job(self, aid, state=None, timeline=None, age=None):
        d = self.jobs / aid[:8]
        d.mkdir(parents=True, exist_ok=True)
        if state is not None:
            (d / "state.json").write_text(json.dumps(state) if not isinstance(state, str) else state)
        if timeline is not None:
            (d / "timeline.jsonl").write_text("".join(json.dumps(e) + "\n" for e in timeline))
        if age is not None:
            for f in d.iterdir():
                os.utime(f, (time.time() - age,) * 2)


class TheInterface(unittest.TestCase):
    def test_every_method_must_be_provided(self):
        r = Runner()
        for name, args in (("resolve_model", ()), ("start", ("p", "m", [], None, "/")),
                           ("claim", ({}, set())), ("list", ()), ("working", ("x",)),
                           ("stop", ("a",)), ("done", ("a",)), ("parked", ("a",)),
                           ("quiet_s", ("a",)), ("last_entry", ("a",)),
                           ("api_error", ({},)), ("auth_failure", ("x",))):
            with self.subTest(name), self.assertRaises(NotImplementedError):
                getattr(r, name)(*args)


class Registry(Base):
    def test_list_ok(self):
        self.env(FAKE_RC="0", FAKE_AGENTS='[{"id": "a1", "status": "working"}]')
        self.assertEqual(self.r.list(), [{"id": "a1", "status": "working"}])
        self.assertEqual(self.r.list(strict=True), [{"id": "a1", "status": "working"}])
        self.env(FAKE_AGENTS="")
        self.assertEqual(self.r.list(strict=True), [])   # nothing running is an answer
        self.assertEqual(json.loads(self.log.read_text().splitlines()[0]), ["agents", "--json"])

    def test_list_failing_is_empty_or_none_when_strict(self):
        self.env(FAKE_RC="3", FAKE_AGENTS="")
        self.assertEqual(self.r.list(), [])
        self.assertIsNone(self.r.list(strict=True))
        self.env(FAKE_RC="0", FAKE_AGENTS="{not json")
        self.assertIsNone(self.r.list(strict=True))
        self.assertIsNone(self.runner(claude=self.root / "missing").list(strict=True))

    def test_working(self):
        for s in ("running", "working", "busy"):
            self.assertTrue(self.r.working(s), s)
        for s in ("idle", "done", None, ""):
            self.assertFalse(self.r.working(s), s)

    def test_stop_kills_by_id(self):
        self.env(FAKE_RC="0")
        self.r.stop("abcd1234-x")
        self.assertEqual(json.loads(self.log.read_text()), ["kill", "abcd1234-x"])


class JobDir(Base):
    def test_done_and_intent(self):
        self.job("abcd1234-x", {"state": "done", "intent": "hi"})
        self.assertTrue(self.r.done("abcd1234-x"))
        self.assertEqual(self.r.intent("abcd1234-x"), "hi")
        self.job("eeee0000-x", {"state": "working"})
        self.assertFalse(self.r.done("eeee0000-x"))
        self.assertEqual(self.r.intent("eeee0000-x"), "")
        self.assertFalse(self.r.done("nope0000"))
        self.job("bad00000", "{half")
        self.assertFalse(self.r.done("bad00000"))

    def test_parked_only_on_monitors_with_nothing_queued(self):
        cases = [({"tasks": 1, "kinds": ["monitor"]}, True),
                 ({"tasks": 2, "kinds": ["monitor", "monitor"]}, True),
                 ({"tasks": 1, "kinds": ["monitor"], "queued": 1}, False),
                 ({"tasks": 2, "kinds": ["monitor", "bash"]}, False),
                 ({"tasks": 0, "kinds": ["monitor"]}, False),
                 ({"tasks": 1, "kinds": []}, False), (None, False)]
        for inflight, want in cases:
            with self.subTest(inflight=inflight):
                self.job("abcd1234-x", {"inFlight": inflight})
                self.assertEqual(self.r.parked("abcd1234-x"), want)
        self.assertFalse(self.r.parked("nope0000"))

    def test_quiet_s_is_the_newest_of_state_and_timeline(self):
        clock = types.SimpleNamespace(time=lambda: 5000.0)
        r = self.runner(time=clock)
        self.job("abcd1234-x", {}, [{"state": "working"}])
        os.utime(self.jobs / "abcd1234" / "state.json", (4000.0, 4000.0))
        os.utime(self.jobs / "abcd1234" / "timeline.jsonl", (4900.0, 4900.0))
        self.assertEqual(r.quiet_s("abcd1234-x"), 100.0)
        self.assertEqual(r.quiet_s("nope0000"), 0.0)
        self.job("cccc0000-x", age=0)   # an empty dir: never wrote
        self.assertEqual(r.quiet_s("cccc0000-x"), 0.0)

    def test_last_entry(self):
        self.job("abcd1234-x", timeline=[{"state": "working"},
                                         {"state": "blocked", "detail": "API Error: 500", "text": "x"}])
        self.assertEqual(self.r.last_entry("abcd1234-x"),
                         {"state": "blocked", "detail": "API Error: 500", "text": "x"})
        self.assertIsNone(self.r.last_entry("nope0000"))
        self.job("eeee0000-x", timeline=[])
        self.assertIsNone(self.r.last_entry("eeee0000-x"))
        (self.jobs / "eeee0000" / "timeline.jsonl").write_text("{half\n")
        self.assertIsNone(self.r.last_entry("eeee0000-x"))


class Launch(Base):
    def setUp(self):
        super().setUp()
        self.launched, self.ran = [], []

        def popen(argv, **kw):
            stdin = types.SimpleNamespace(data=b"", closed=False)
            stdin.write = lambda d: setattr(stdin, "data", stdin.data + d)
            stdin.close = lambda: setattr(stdin, "closed", True)
            self.launched.append((argv, kw, stdin))
            return types.SimpleNamespace(stdin=stdin)

        self.sp = types.SimpleNamespace(Popen=popen, PIPE=-1, DEVNULL=-3,
                                        run=lambda argv, **k: self.ran.append(argv)
                                        or types.SimpleNamespace(stdout=self.answer))
        self.answer = ""
        self.registry = [[{"id": "old"}]]
        self.r = self.runner(subprocess=self.sp, agents=lambda: self.registry[0])

    def test_start_argv_and_the_token_on_line_2(self):
        h = self.r.start("You are Kit.\nsecond line\nthird", "claude-x", ["a", "b"], {"E": "1"}, self.root)
        argv, kw, stdin = self.launched[0]
        self.assertEqual(argv, [str(self.claude), "--bg", "--model", "claude-x",
                                "--dangerously-skip-permissions", "--disallowedTools", "a", "b"])
        self.assertEqual(kw, {"cwd": str(self.root), "stdin": -1, "stdout": -3, "stderr": -3,
                              "start_new_session": True, "env": {"E": "1"}})
        self.assertEqual(stdin.data.decode(), f"You are Kit.\n(bridge spawn token {TOKEN}: ignore this line)\n"
                                              "second line\nthird")
        self.assertTrue(stdin.closed)
        self.assertNotIn(TOKEN, " ".join(argv))   # never in /proc cmdline
        self.assertEqual(h, {"token": TOKEN, "before": {"old"}})

    def test_start_with_nothing_to_deny_has_no_flag(self):
        self.r.start("one line", "claude-x", [], None, self.root)
        argv, kw, stdin = self.launched[0]
        self.assertEqual(argv, [str(self.claude), "--bg", "--model", "claude-x",
                                "--dangerously-skip-permissions"])
        self.assertIsNone(kw["env"])
        self.assertEqual(stdin.data.decode(), f"one line\n(bridge spawn token {TOKEN}: ignore this line)\n")

    def test_start_strips_an_inherited_shuttle_body(self):
        # A bridge started under a Codex body inherits its SHUTTLE_BODY; a
        # Claude body (or the --bg daemon) must never key on it (t-1439 F1).
        self.r.start("one", "claude-x", [], {"E": "1", "SHUTTLE_BODY": "c0dec0de"}, self.root)
        self.assertEqual(self.launched[-1][1]["env"], {"E": "1"})
        old = os.environ.get("SHUTTLE_BODY")
        os.environ["SHUTTLE_BODY"] = "c0dec0de"
        try:
            self.r.start("one", "claude-x", [], None, self.root)
            env = self.launched[-1][1]["env"]
            self.assertNotIn("SHUTTLE_BODY", env)
            self.assertEqual(env, {k: v for k, v in os.environ.items() if k != "SHUTTLE_BODY"})
        finally:
            os.environ.pop("SHUTTLE_BODY") if old is None else os.environ.__setitem__("SHUTTLE_BODY", old)

    def test_start_without_shuttle_body_passes_env_as_is(self):
        # Spawn parity: no SHUTTLE_BODY anywhere, so env is exactly the plan's.
        old = os.environ.pop("SHUTTLE_BODY", None)
        try:
            plan_env = {"E": "1"}
            self.r.start("one", "claude-x", [], plan_env, self.root)
            self.assertIs(self.launched[-1][1]["env"], plan_env)
            self.r.start("one", "claude-x", [], None, self.root)
            self.assertIsNone(self.launched[-1][1]["env"])
        finally:
            if old is not None:
                os.environ["SHUTTLE_BODY"] = old

    def test_resolve_model_only_a_claude_answer_counts(self):
        self.assertEqual(self.r.resolve_model(), "claude-pref")   # no resolver: not even run
        self.assertEqual(self.ran, [])
        r = self.runner(subprocess=self.sp, model_resolver=Path("/bin/res"))
        self.answer = "claude-sonnet-x\n"
        self.assertEqual(r.resolve_model(), "claude-sonnet-x")
        self.assertEqual(self.ran, [["/bin/res", "claude-pref"]])
        for answer in ("", "gpt-5", "Claude-x", "error: capped"):
            self.answer = answer
            self.assertEqual(r.resolve_model(), "claude-pref", answer)
        self.sp.run = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
        self.assertEqual(r.resolve_model(), "claude-pref")


class Claim(Base):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        self.polls = []
        self.intent = {}
        clock = types.SimpleNamespace(sleep=self.sleeps.append, time=time.time)

        def agents():
            self.polls.append(1)
            return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        self.r = self.runner(time=clock, agents=agents, intent=lambda aid: self.intent.get(aid, ""))
        self.tagged = f"x\n(bridge spawn token {TOKEN}: ignore this line)"
        self.h = {"token": TOKEN, "before": {"old"}}

    def test_claims_exactly_the_new_body_with_the_token(self):
        self.seq = [[{"id": "old"}], [{"id": "old"}, {"id": "mine"}, {"id": "other"}, {"id": None}]]
        self.intent = {"mine": self.tagged, "other": "x\n(bridge spawn token 0000: ignore", "old": self.tagged}
        self.assertEqual(self.r.claim(self.h, set()), {"mine"})
        self.assertEqual(self.sleeps, [1, 1])

    def test_a_pasted_token_line_is_not_this_token(self):
        self.seq = [[{"id": "new"}]]
        self.intent = {"new": f"quoted: (bridge spawn token {TOKEN[:-1]}: ignore"}
        self.assertEqual(self.r.claim(self.h, set()), set())

    def test_never_an_owned_body(self):
        self.seq = [[{"id": "owned"}, {"id": "mine"}]]
        self.intent = {"owned": self.tagged, "mine": self.tagged}
        self.assertEqual(self.r.claim(self.h, {"owned"}), {"mine"})
        self.seq = [[{"id": "owned"}]]
        self.assertEqual(self.r.claim(self.h, {"owned"}), set())

    def test_gives_up_after_ten_polls(self):
        self.seq = [[{"id": "old"}]]
        self.assertEqual(self.r.claim(self.h, set()), set())
        self.assertEqual((self.sleeps, len(self.polls)), ([1] * 10, 10))

    def test_two_matches_both_come_back(self):
        self.seq = [[{"id": "a"}, {"id": "b"}]]
        self.intent = {"a": self.tagged, "b": self.tagged}
        self.assertEqual(self.r.claim(self.h, set()), {"a", "b"})   # the caller acts only on one

    def test_by_default_it_polls_its_own_registry_and_job_dirs(self):
        self.env(FAKE_RC="0", FAKE_AGENTS='[{"id": "abcd1234-new", "status": "working"}]')
        self.job("abcd1234-new", {"intent": self.tagged})
        r = self.runner(time=types.SimpleNamespace(sleep=lambda s: None))
        self.assertEqual(r.claim(self.h, set()), {"abcd1234-new"})


class Choosing(unittest.TestCase):
    def test_kind_defaults_to_claude(self):
        self.assertEqual(runner.kind(FakeInstance({})), "claude")
        self.assertEqual(runner.kind(FakeInstance({"runner": {}})), "claude")
        self.assertEqual(runner.kind(FakeInstance({"runner": {"kind": "claude"}})), "claude")

    def test_errors(self):
        self.assertEqual(runner.errors(FakeInstance({})), [])
        self.assertEqual(runner.errors(FakeInstance({"runner": {"kind": "claude"}})), [])
        self.assertEqual(runner.errors(FakeInstance({"runner": {"kind": "codex"}})), [])
        [e] = runner.errors(FakeInstance({"runner": {"kind": "gemini"}}))
        self.assertIn("runner.kind 'gemini' is not one of: claude, codex", e)
        [e] = runner.errors(FakeInstance({"runner": {"kind": "codex", "model": 5}}))
        self.assertIn("runner.model must be a string or null", e)
        [e] = runner.errors(FakeInstance({"runner": ["claude"]}))
        self.assertIn("runner must be an object", e)

    def test_build_runner_takes_the_adapters_seams(self):
        calls = []
        b = types.SimpleNamespace(
            CLAUDE=Path("/c/claude"), JOBS_DIR=Path("/j"), MODEL_RESOLVER=Path("/m"),
            PREFERRED_MODEL="claude-p", subprocess="SP", time="T", secrets="S",
            agents=lambda: calls.append("agents") or [], _job_intent=lambda aid: calls.append(aid) or "")
        r = runner.build_runner(FakeInstance({}), b)
        self.assertIsInstance(r, ClaudeRunner)
        self.assertEqual((r.claude, r.jobs_dir, r.model_resolver, r.preferred_model,
                          r.subprocess, r.time, r.secrets),
                         (Path("/c/claude"), Path("/j"), Path("/m"), "claude-p", "SP", "T", "S"))
        # claim's polls go through the adapter's shims, looked up at call time
        b.agents = lambda: calls.append("swapped") or [{"id": "n"}]
        r._agents(), r._intent("n")
        self.assertEqual(calls, ["swapped", "n"])
        with self.assertRaises(ValueError):
            runner.build_runner(FakeInstance({"runner": {"kind": "nope"}}), b)


if __name__ == "__main__":
    unittest.main()


class KindValidationTest(unittest.TestCase):
    def _inst(self, runner):
        class I:   # the two attributes errors() reads
            path = "shuttle.json"
            raw = {"runner": runner}
        return I()

    def test_non_string_kind_is_an_error_not_a_crash(self):
        from shuttle import runner
        self.assertTrue(runner.errors(self._inst({"kind": ["codex"]})))

    def test_empty_kind_is_an_error_not_claude(self):
        from shuttle import runner
        self.assertTrue(runner.errors(self._inst({"kind": ""})))
        self.assertTrue(runner.errors(self._inst({"kind": 0})))

    def test_absent_kind_is_claude(self):
        from shuttle import runner
        self.assertEqual(runner.errors(self._inst({})), [])
