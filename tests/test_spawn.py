"""Starting a body, through the fake adapter. Nothing real is launched: the
fake's subprocess, clock and token source stand in for the bridge module's."""
import types
from pathlib import Path
import unittest

from shuttle import spawn
from shuttle.runner import ClaudeRunner
from tests.test_reap import Base, FakeBridge, msg

TOKEN = "feedface00000000"
NEW = "feed0001-0000"


class SpawnBridge(FakeBridge):
    def __init__(self, root):
        super().__init__(root)
        self.cfg = self.cfg.__class__(**{**self.cfg.__dict__, "workdir": root})
        self.launched, self.claimed_hooks = [], []
        self.registry = [[], [{"id": NEW}]]   # before the launch, then each claim poll
        self.intent = {NEW: f"x\n(bridge spawn token {TOKEN}: ignore"}

        def popen(argv, **kw):
            stdin = types.SimpleNamespace(data=b"", close=lambda: None)
            stdin.write = lambda d: setattr(stdin, "data", stdin.data + d)
            self.launched.append((argv, kw, stdin))
            return types.SimpleNamespace(stdin=stdin)

        self.subprocess = types.SimpleNamespace(Popen=popen, PIPE=-1, DEVNULL=-3,
                                                run=lambda *a, **k: types.SimpleNamespace(stdout="nope"))
        self.time = types.SimpleNamespace(time=lambda: 1000.0, sleep=lambda s: None)
        self.secrets = types.SimpleNamespace(token_hex=lambda n=8: TOKEN)
        self.nowlog.conversation = lambda *a: "c-9"
        # The real Claude runner, built the way shuttle.runner.build_runner
        # builds it: launching through this fake's subprocess, clock and token
        # source, claiming through its agents and _job_intent.
        self.runner = ClaudeRunner(
            claude=Path("/fake/claude"), jobs_dir=root / "jobs",
            model_resolver=Path("/fake/resolver"), preferred_model="claude-pref",
            subprocess=self.subprocess, time=self.time, secrets=self.secrets,
            agents=lambda: self.agents(), intent=lambda aid: self._job_intent(aid))

    def agents(self, strict=False):
        return self.registry.pop(0) if len(self.registry) > 1 else self.registry[0]

    def _job_intent(self, aid):
        return self.intent.get(aid, "")

    def resolve_model(self):
        return spawn.resolve_model(self)

    batch_since = staticmethod(lambda batch: 900.0)


def plan(**kw):
    p = {"chan": "R1", "label": "#room", "venue": "room", "inbox_label": "room", "who": ["Pat"],
         "members": ["pat"],
         "prompt": "You are Kit.\nsecond line", "deny": [], "env": None, "rec": {"x": 1}}
    p.update(kw)
    return p


class SpawnBody(Base):
    def setUp(self):
        super().setUp()
        self.b = SpawnBridge(self.root)

    def test_argv_stdin_and_the_token_line(self):
        spawn.spawn_body(self.b, {}, plan(deny=["mcp__spotify"]), [msg(2), msg(3)], attempt=2)
        argv, kw, stdin = self.b.launched[0]
        self.assertEqual(argv, ["/fake/claude", "--bg", "--model", "claude-pref",
                                "--dangerously-skip-permissions",
                                "--disallowedTools", "mcp__handoff", "mcp__spotify"])
        self.assertEqual((kw["cwd"], kw["env"], kw["start_new_session"]), (str(self.root), None, True))
        self.assertEqual(stdin.data.decode().splitlines(),
                         ["You are Kit.", f"(bridge spawn token {TOKEN}: ignore this line)", "second line"])

    def spawn_line(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spawn.spawn_body(self.b, {}, plan(), [msg(2)])
        return [l for l in out.getvalue().splitlines() if l.startswith("spawned session")]

    def test_the_spawn_line_names_the_model(self):
        self.assertEqual(self.spawn_line(), ["spawned session for #room (1 new msg, attempt 1) on claude-pref"])

    def test_the_spawn_line_without_a_model_says_the_runner_default(self):
        self.b.resolve_model = lambda: None    # a codex runner with no runner.model
        self.assertEqual(self.spawn_line(),
                         ["spawned session for #room (1 new msg, attempt 1) on the runner's default model"])

    def test_always_deny_comes_from_the_config(self):
        self.b.cfg = self.b.cfg.__class__(**{**self.b.cfg.__dict__, "always_deny": ("a", "b")})
        spawn.spawn_body(self.b, {}, plan(deny=["c"]), [msg(2)])
        self.assertEqual(self.b.launched[0][0][-4:], ["--disallowedTools", "a", "b", "c"])

    def test_nothing_to_deny_leaves_no_dangling_flag(self):
        self.b.cfg = self.b.cfg.__class__(**{**self.b.cfg.__dict__, "always_deny": ()})
        spawn.spawn_body(self.b, {}, plan(deny=[]), [msg(2)])
        self.assertEqual(self.b.launched[0][0], ["/fake/claude", "--bg", "--model", "claude-pref",
                                                 "--dangerously-skip-permissions"])

    def test_resolve_model_runs_the_configured_resolver(self):
        calls = []
        self.b.runner.model_resolver = "/bin/res"
        self.b.subprocess.run = lambda argv, **k: calls.append(argv) or types.SimpleNamespace(stdout="")
        spawn.resolve_model(self.b)
        self.assertEqual(calls, [["/bin/res", "claude-pref"]])

    def test_claims_the_body_that_carries_the_token(self):
        state = {}
        spawn.spawn_body(self.b, state, plan(), [msg(3), msg(2)], attempt=2)
        rec = state["owned"][NEW]
        self.assertEqual((rec["chan"], rec["attempt"], rec["ack_msg"], rec["cid"], rec["x"]),
                         ("R1", 2, "3", "c-9", 1))
        self.assertEqual(state["spawned_msgs"], {"2": 1000.0, "3": 1000.0})
        self.assertEqual(self.b.reacts, [("3", True)])
        self.assertEqual(self.b.inbox.opened, [(NEW, "room", "R1", "fake")])
        self.assertEqual(self.b.saved and len(self.b.saved), 1)
        kind, kw = self.b.nowlog.events[-1][1:]
        self.assertEqual((kind, kw["body"], kw["wait_s"], kw["who"]), ("picked", NEW, 100.0, ["Pat"]))
        self.assertEqual((kw["members"], rec["members"]), (["pat"], ["pat"]))

    def test_a_body_without_the_token_is_never_claimed(self):
        self.b.intent = {NEW: "someone pasted: (bridge spawn token 0000: ignore"}
        state = {}
        spawn.spawn_body(self.b, state, plan(), [msg(2)])
        self.assertNotIn("owned", state)
        self.assertIsNone(self.b.nowlog.events[-1][2]["body"])
        self.assertEqual(state["spawned_msgs"], {"2": 1000.0})   # the dedup guard still holds

    def test_on_claimed_runs_only_for_a_single_match(self):
        hits = []
        spawn.spawn_body(self.b, {}, plan(on_claimed=hits.append), [msg(2)])
        self.assertEqual(hits, [NEW])
        hits.clear()
        self.b.registry = [[], [{"id": NEW}, {"id": "feed0002-0000"}]]
        self.b.intent["feed0002-0000"] = self.b.intent[NEW]
        spawn.spawn_body(self.b, {}, plan(on_claimed=hits.append), [msg(2)])
        self.assertEqual(hits, [])

    def test_an_owned_body_is_never_reclaimed(self):
        state = {"owned": {NEW: {"chan": "elsewhere"}}}
        spawn.spawn_body(self.b, state, plan(), [msg(2)])
        self.assertEqual(state["owned"][NEW], {"chan": "elsewhere"})

    def test_resolve_model_falls_back_to_the_preferred(self):
        self.assertEqual(spawn.resolve_model(self.b), "claude-pref")
        self.b.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="claude-sonnet-x\n")
        self.assertEqual(spawn.resolve_model(self.b), "claude-sonnet-x")


if __name__ == "__main__":
    unittest.main()
