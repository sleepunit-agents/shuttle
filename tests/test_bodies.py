"""Bodies in the agent's house (shuttle/bodies.py; loom docs/specs/bridges.md,
"Bodies before the one service"): the bridge says who to wake and for what, the
agent's body host wakes them, and everything crosses through shuttle's store,
each agent its own keys."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shuttle import bodies, inbox  # noqa: E402
from shuttle.runner import ClaudeRunner  # noqa: E402


class Store:
    """shuttle's store, in memory."""

    def __init__(self):
        self.d = {}

    def get(self, c, k):
        return self.d.get((c, k), (None, 0))

    def put(self, c, k, doc, if_rev=None):
        rev = self.d.get((c, k), (None, 0))[1] + 1
        self.d[(c, k)] = (json.loads(json.dumps(doc)), rev)
        return rev

    def delete(self, c, k):
        self.d.pop((c, k), None)

    def docs(self, c, prefix):
        return sorted((k, d) for (cc, k), (d, _) in self.d.items() if cc == c and k.startswith(prefix))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Harness:
    """A local runner in the agent's house: start() makes a body, claim() finds it."""

    name, auth_hint = "fake", "check the fake"

    def __init__(self):
        self.bodies, self.started, self.stopped, self.n = {}, [], [], 0

    def resolve_model(self):
        return "model-x"

    def start(self, prompt, model, deny, env, cwd):
        self.n += 1
        aid = f"ab{self.n:02d}cdef-0000"
        self.bodies[aid] = {"status": "busy", "done": False}
        self.started.append({"prompt": prompt, "model": model, "deny": deny, "env": env, "cwd": cwd})
        return aid

    def claim(self, handle, exclude):
        return {handle} - set(exclude)

    def list(self, strict=False):
        return [{"id": a, "status": b["status"]} for a, b in self.bodies.items()]

    def stop(self, aid):
        self.stopped.append(aid)
        self.bodies.pop(aid, None)

    def done(self, aid):
        return self.bodies.get(aid, {}).get("done", False)

    def parked(self, aid):
        return False

    def quiet_s(self, aid):
        return 3.0

    def last_entry(self, aid):
        return {"state": "blocked", "detail": "", "text": "API Error: overloaded"} if aid in self.bodies else None


class Bodies(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.clock = Clock()
        self.dir = Path(tempfile.mkdtemp(prefix="shuttle-bodies-"))
        inbox.configure(inbox_dir=self.dir)
        bodies._SNAP.clear()
        self.harness = Harness()
        self.bridge_wire = bodies.Wire("discord", call=self.as_agent("kit"))
        self.runner = bodies.RemoteRunner(self.bridge_wire, policy=ClaudeRunner.__new__(ClaudeRunner),
                                          time=self.clock, secrets=type("S", (), {"token_hex": staticmethod(lambda n: "0123456789abcdef")}))
        self.inbox = bodies.RemoteInbox(self.bridge_wire, self.runner)
        self.host = self.new_host()

    def new_host(self):
        return bodies.Host(bodies.Wire("discord", call=self.as_agent("kit")), self.harness, inbox=inbox,
                           cwd="/work", time=self.clock, idle_s=60)

    def as_agent(self, agent):
        def call(argv):
            r = bodies.api_answer({"kind": "agent", "agent": agent}, argv, self.store)
            if r["status"] != 200:
                raise OSError(r["body"]["error"])
            return json.loads(r["body"]["stdout"])
        return call

    def beat(self):
        self.host.step()
        bodies._SNAP.clear()

    def test_the_bridge_asks_and_the_host_wakes_the_body_in_its_house(self):
        self.assertIsNone(self.runner.list(strict=True), "no host yet: can't tell, never 'every body is gone'")
        self.beat()
        h = self.runner.start("brief\nhello", "model-y", ["WebFetch"], {"SHUTTLE_SCOPE": "field", "SECRET_TOKEN": "x"}, "/elsewhere")
        self.assertEqual(self.runner.find(h, set()), set(), "not started until the host's next beat")
        self.beat()
        (aid,) = self.runner.find(h, set())
        s = self.harness.started[0]
        self.assertEqual((s["prompt"], s["model"], s["deny"], s["cwd"]), ("brief\nhello", "model-y", ["WebFetch"], "/work"))
        self.assertEqual(s["env"].get("SHUTTLE_SCOPE"), "field")
        self.assertNotIn("SECRET_TOKEN", s["env"], "a credential never rides through the store")
        self.assertEqual(self.store.docs("bodies", "kit/discord/req/"), [], "the prompt is gone once claimed")
        self.beat()
        self.assertEqual(self.runner.list(strict=True), [{"id": aid, "status": "busy"}])
        self.assertTrue(self.runner.working("busy"))
        self.assertTrue(self.runner.api_error(self.runner.last_entry(aid)))
        self.assertEqual(self.runner.quiet_s(aid), 3.0)
        self.assertEqual(self.runner.resolve_model(), "model-x")
        self.beat()
        self.assertEqual(len(self.harness.started), 1, "started once")
        self.harness.bodies[aid]["done"] = True
        self.beat()
        self.assertTrue(self.runner.done(aid))
        self.runner.stop(aid)
        self.beat()
        self.assertEqual(self.harness.stopped, [aid])
        self.assertEqual(self.store.docs("bodies", "kit/discord/stop/"), [])

    def test_follow_ups_reach_the_body_once_and_what_it_read_comes_back(self):
        self.beat()
        h = self.runner.start("brief", "m", [], None, "/w")
        self.beat()
        (aid,) = self.runner.find(h, set())
        self.inbox.open_inbox(aid, "#room", "c1", "discord")
        self.assertTrue(self.inbox.put(aid, [{"id": "m1", "text": "one more thing", "msg": {}}], now=5))
        self.beat()
        self.beat()
        self.assertEqual([i["id"] for i in inbox.take(aid, now=9)], ["m1"], "the body's hook reads it in its own house")
        self.assertEqual(inbox.take(aid), [], "once")
        self.inbox.put(aid, [{"id": "m2", "text": "and another", "msg": {}}])
        self.host = self.new_host()   # the host restarts: nothing is handed over twice
        self.beat()
        self.assertEqual([i["id"] for i in inbox.take(aid, now=10)], ["m2"])
        self.beat()
        self.assertEqual([i["id"] for i in self.inbox.delivered(aid)], ["m1", "m2"])
        self.assertEqual(self.runner.list(strict=True)[0]["id"], aid, "a restarted host still owns its bodies")
        self.inbox.put(aid, [{"id": "m3", "text": "late", "msg": {}}])
        self.assertEqual([i["id"] for i in self.inbox.close(aid)], ["m3"], "what the body never read goes to a fresh one")
        self.beat()
        self.assertEqual(inbox.take(aid), [], "closed in the house too")
        self.inbox.remove(aid)
        self.beat()
        self.assertFalse(inbox.path_for(aid).exists(), "and gone from the house once the bridge drops it")

    def test_no_prompt_outlives_its_request(self):
        self.beat()
        # Nobody took it for REQ_TTL_S (the house was down): the bridge gave up, the text goes.
        self.runner.start("brief\nold words", "m", [], None, "/w")
        self.clock.t += bodies.REQ_TTL_S + 1
        self.beat()
        self.assertEqual(self.harness.started, [])
        self.assertEqual(self.store.docs("bodies", "kit/discord/req/"), [])
        # Tried and never showed: the bridge reads that as never started, and the text goes too.
        self.harness.claim = lambda handle, exclude: set()
        h = self.runner.start("brief\nnew words", "m", [], None, "/w")
        self.beat()
        self.assertEqual(self.runner.find(h, set()), set())
        self.assertEqual(self.store.docs("bodies", "kit/discord/req/"), [])

    def test_each_agent_reaches_only_its_own_keys(self):
        self.as_agent("kit")(["bodies", "put", "discord/req/0123456789abcdef", "--doc", json.dumps({"prompt": "p"})])
        self.assertEqual(self.as_agent("pip")(["bodies", "sync", "discord", "--doc", "{}"])["req"], {})
        for bad in (["bodies", "get", "../kit/discord/host"], ["bodies", "put", "discord/req/nothex", "--doc", "{}"],
                    ["bodies", "get", "discord/elsewhere"]):
            with self.assertRaises(OSError):
                self.as_agent("pip")(bad)
        for caller in ({"kind": "owner"}, {"kind": "piece"}, {}):
            self.assertEqual(bodies.api_answer(caller, ["bodies", "get", "discord/host"], self.store)["status"], 403)
        self.assertIsNone(bodies.api_answer({"kind": "agent", "agent": "kit"}, ["people"], self.store))

    def test_a_stale_report_is_cant_tell_and_the_host_leaves_when_idle(self):
        self.beat()
        self.assertEqual(self.runner.list(strict=True), [])
        self.clock.t += bodies.STALE_S + 1
        self.assertIsNone(self.runner.list(strict=True))
        self.assertEqual(self.runner.list(), [])
        self.assertTrue(self.host.idle())

    def test_the_bridges_inbox_calls_go_remote_once_configured(self):
        calls = []

        class Fake:
            def __getattr__(self, name):
                return lambda *a: calls.append(name) or "remote"
        inbox.configure(remote=Fake())
        try:
            for f, args in (("open_inbox", ("a", "v", "c", "b")), ("put", ("a", [])), ("close", ("a",)),
                            ("delivered", ("a",)), ("meta", ("a",)), ("remove", ("a",)), ("is_open", ("a",)),
                            ("sweep", ("b", set(), set()))):
                self.assertEqual(getattr(inbox, f)(*args), "remote")
            self.assertEqual(calls, ["open_inbox", "put", "close", "delivered", "meta", "remove", "is_open", "sweep"])
        finally:
            inbox._configured["remote"] = None


class Config(unittest.TestCase):
    def inst(self, runner):
        return type("I", (), {"raw": {"runner": runner}, "path": "shuttle.json"})()

    def test_runner_host_names_a_host_for_every_bridge_or_one_each(self):
        from shuttle import runner as r
        self.assertIsNone(r.host_profile(self.inst({"kind": "claude"}), "discord"))
        self.assertEqual(r.host_profile(self.inst({"host": "bodies"}), "slack"), "bodies")
        each = self.inst({"host": {"discord": "discord-bodies", "slack": "slack-bodies"}})
        self.assertEqual(r.host_profile(each, "slack"), "slack-bodies")
        self.assertIsNone(r.host_profile(each, "teams"), "a bridge it doesn't name runs its own bodies")
        self.assertEqual(r.errors(each), [])
        for bad in ({"host": "Bad Name"}, {"host": {}}, {"host": {"discord": 3}}):
            self.assertIn("runner.host", r.errors(self.inst(bad))[0])


if __name__ == "__main__":
    unittest.main()
