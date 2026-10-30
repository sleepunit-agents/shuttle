"""An agent's people, kept in loom (shuttle/gates.py, gates_page.py; loom's
docs/specs/bridges.md step 2): a bridge reads its own, offers its people.json
once, and only a household owner, on the page, makes an offer the gates."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shuttle import gates, gates_page  # noqa: E402

DOC = {"people": {"robin": {"name": "Robin", "tier": 3, "discord": ["11"]}}, "agents": {}}


class Store:
    """shuttle's store, in memory: (collection, key) -> (doc, rev)."""

    def __init__(self):
        self.d = {}

    def get(self, c, k):
        return self.d.get((c, k), (None, 0))

    def put(self, c, k, doc, if_rev=None):
        cur = self.d.get((c, k), (None, 0))[1]
        if if_rev is not None and cur != if_rev:
            raise ValueError("someone changed it since you opened it: reload and try again")
        self.d[(c, k)] = (doc, cur + 1)
        return cur + 1

    def keys(self, c):
        return [k for (cc, k) in self.d if cc == c]


class Api(unittest.TestCase):
    def setUp(self):
        self.s = Store()

    def ask(self, caller, argv):
        return gates.api_answer(caller, argv, run_get=self.s.get, run_put=self.s.put)

    def test_a_bridge_gets_its_own_agents_people_and_nobody_elses(self):
        self.s.put(gates.PEOPLE, "kit", DOC)
        self.s.put(gates.PEOPLE, "pip", {"people": {}})
        out = json.loads(self.ask({"kind": "agent", "agent": "kit", "service": "discord"}, ["people"])["body"]["stdout"])
        self.assertEqual(out, {"doc": DOC, "rev": 1})
        out = json.loads(self.ask({"kind": "agent", "agent": "zed"}, ["people"])["body"]["stdout"])
        self.assertEqual(out, {"doc": None, "rev": 0})
        for caller in ({"kind": "owner"}, {"kind": "piece", "subject": "x"}, {}):
            self.assertEqual(self.ask(caller, ["people"])["status"], 403)

    def test_an_offer_goes_to_offers_never_to_the_gates(self):
        r = self.ask({"kind": "agent", "agent": "pip", "service": "discord"}, ["offer-people", "--doc", json.dumps(DOC)])
        self.assertEqual(r["status"], 200)
        self.assertEqual(self.s.get(gates.PEOPLE, "pip"), (None, 0))
        self.assertEqual(self.s.get(gates.OFFERS, "pip")[0]["doc"], DOC)
        self.assertEqual(self.ask({"kind": "agent", "agent": "pip"}, ["offer-people", "--doc", "{bad"])["status"], 400)
        # Notes are fine: _comment, _updated.
        self.assertEqual(self.ask({"kind": "agent", "agent": "pip"}, ["offer-people", "--doc", json.dumps({**DOC, "_updated": "2026-10-28"})])["status"], 200)
        self.assertEqual(self.ask({"kind": "agent", "agent": "pip"}, ["offer-people", "--doc", '{"people": {}, "tier": 3}'])["status"], 400)


class BridgeSide(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shuttle-gates-"))
        self.file = self.dir / "people.json"
        self.file.write_text(json.dumps({"people": {"kim": {"name": "Kim", "tier": 1}}}))
        self.t = [1000.0]
        self.calls = []
        self.loom = None

    def remote(self, argv):
        self.calls.append(argv[0])
        if argv[0] == "people":
            return None if self.loom == "absent" else {"doc": self.loom, "rev": 4 if self.loom else 0}
        return {"ok": True}

    def people(self):
        return gates.People(lambda: self.file, now=lambda: self.t[0], remote=self.remote)

    def test_the_file_until_loom_has_one_offered_once(self):
        p = self.people()
        self.assertIn("kim", p.doc()["people"])
        self.assertEqual(self.calls, ["people", "offer-people"])
        self.t[0] += gates.REFRESH_S
        p.doc()
        self.assertEqual(self.calls.count("offer-people"), 1, "offered once")

    def test_loom_once_it_has_one_refreshed_at_most_every_minute(self):
        self.loom = DOC
        p = self.people()
        self.assertEqual(p.doc(), DOC)
        self.assertEqual(p.version(), "loom:4")
        self.loom = {"people": {}}
        self.t[0] += 10
        self.assertEqual(p.doc(), DOC, "not asked again within the minute")
        self.t[0] += gates.REFRESH_S
        self.assertEqual(p.doc(), {"people": {}})

    def test_no_loom_to_ask_is_the_file_as_before(self):
        self.loom = "absent"
        p = self.people()
        self.assertIn("kim", p.doc()["people"])
        self.assertNotIn("offer-people", self.calls)


class Page(unittest.TestCase):
    def setUp(self):
        self.s = Store()

    def act(self, action, fields):
        return gates_page.act(action, fields, get=self.s.get, put=self.s.put, keys=self.s.keys)

    def test_an_agents_people_are_checked_on_its_page_and_saved_exactly_as_shown(self):
        self.s.put(gates.OFFERS, "pip", {"doc": DOC, "at": 1, "from": "discord"})
        sec = gates_page.agent_section("pip", get=self.s.get, keys=self.s.keys)
        self.assertEqual(sec["blocks"][0]["items"][0]["page"], "people/pip")
        self.assertEqual([a["page"] for a in gates_page.asks(get=self.s.get, keys=self.s.keys)], ["people/pip"])
        self.assertNotIn("agent", gates_page.asks(get=self.s.get, keys=self.s.keys)[0], "the household owner's alone")
        callout, table, form = gates_page.people_page("pip", get=self.s.get)["sections"][0]["blocks"]
        self.assertIn("robin", callout["text"], "tier 3 is flagged")
        self.assertEqual(table["rows"][0][:3], ["Robin", "robin", "3 · owner"])
        self.assertEqual((form["fields"], form["hidden"]), ([], {"agent": "pip", "offer_rev": "1"}), "no JSON box")
        self.s.put(gates.OFFERS, "pip", {"doc": {"people": {"evil": {"tier": 3}}}, "at": 2})
        self.assertFalse(self.act("people-accept", {"agent": "pip", "offer_rev": "1"})["ok"], "changed since shown")
        self.assertTrue(self.act("people-accept", {"agent": "pip", "offer_rev": "2"})["ok"])
        self.assertEqual(self.s.get(gates.PEOPLE, "pip")[0], {"people": {"evil": {"tier": 3}}})
        self.assertFalse(self.act("people-accept", {"agent": "pip", "offer_rev": "2"})["ok"], "never over a saved one")
        self.assertEqual(gates_page.asks(get=self.s.get, keys=self.s.keys), [])
        self.assertIsNone(self.act("people-save", {}))

    def test_a_long_document_fits_the_page(self):
        many = {"people": {f"p{i}": {"name": "N" * 40, "tier": 2, "slack": {"T": f"U{i}"}, "grant": ["x" * 50] * 20}
                           for i in range(300)}}
        self.s.put(gates.OFFERS, "pip", {"doc": many, "at": 1})
        for b in gates_page.people_page("pip", get=self.s.get)["sections"][0]["blocks"]:
            self.assertTrue(all(len(str(c)) <= gates_page.CELL for r in b.get("rows") or [] for c in r))
            self.assertLessEqual(len(b.get("rows") or []), gates_page.ROWS)


MAP = {"guild": "g1", "bots": {"kit": "b1", "pip": "b2"}, "bot_roles": {"kit": "r1"},
       "channels": {"c1": {"name": "general", "primary": "kit"}, "c2": {"name": "games", "primary": "pip"},
                    "c3": {"name": "lobby", "primary": None}},
       "threads": {}, "notify": {"kit": "c1", "general": "c1"}, "ignore": {"c9": 1}, "notes_channels": {"c1": 1},
       "webhook_peers": {"w1": "x"}, "peer_turns_each": None, "_comment": "notes"}


class Channels(unittest.TestCase):
    """Each agent's part of the route map is its own: a list of agents isn't the household's."""

    def setUp(self):
        self.s = Store()

    def act(self, action, fields):
        return gates_page.act(action, fields, get=self.s.get, put=self.s.put, keys=self.s.keys)

    def ask(self, agent):
        r = gates.api_answer({"kind": "agent", "agent": agent}, ["routes"], run_get=self.s.get, run_put=self.s.put,
                             run_keys=self.s.keys)
        return json.loads(r["body"]["stdout"])["doc"]

    def test_split_then_assembled_is_what_the_bridge_had(self):
        household, parts = gates.split(MAP, "kit")
        self.assertEqual(sorted(parts), ["kit", "pip"])
        self.assertEqual(parts["kit"]["answers"], {"c1": "general"})
        self.assertEqual(parts["kit"]["extra"], {"ignore": {"c9": 1}, "notes_channels": {"c1": 1}}, "the bridge's own")
        self.assertNotIn("extra", parts["pip"])
        self.assertEqual(sorted(household), ["channel_names", "guild", "notify", "peer_turns_each", "webhook_peers"])
        got = gates.assemble("kit", household, parts)
        self.assertEqual({k: v for k, v in MAP.items() if not k.startswith("_")}, got)

    def test_the_bridge_switches_only_once_every_agent_it_knows_is_saved(self):
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": MAP, "at": 1})
        self.assertEqual(sorted(a["page"] for a in gates_page.asks(get=self.s.get, keys=self.s.keys)),
                         ["channels/kit", "channels/pip"])
        form = gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"][-1]
        self.assertEqual((form["fields"], form["hidden"]), ([], {"agent": "kit", "from": "kit", "offer_rev": "1"}))
        r = self.act("channels-accept", form["hidden"])
        self.assertTrue(r["ok"])
        self.assertIn("once pip's are too", r["message"])
        self.assertIsNone(self.ask("kit"), "pip's channels aren't in loom yet: keep the file")
        pip_form = gates_page.channels_page("pip", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"][-1]
        self.assertEqual(pip_form["hidden"]["from"], "kit", "pip has no bridge: the copy that knows its bot")
        self.assertTrue(self.act("channels-accept", pip_form["hidden"])["ok"])
        self.assertEqual(self.ask("kit"), {k: v for k, v in MAP.items() if not k.startswith("_")})
        self.assertEqual(gates_page.asks(get=self.s.get, keys=self.s.keys), [])

    def test_two_copies_differ_about_an_agent_and_two_agents_claiming_a_channel_is_an_ask(self):
        newer = json.loads(json.dumps(MAP))
        newer["channels"]["c4"] = {"name": "fez-room", "primary": "kit"}
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": newer, "at": 1})
        self.s.put(gates.ROUTE_OFFERS, "pip", {"doc": MAP, "at": 1})
        blocks = gates_page.channels_page("pip", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"]
        self.assertEqual(blocks[-1]["hidden"]["from"], "pip", "its own bridge's copy first")
        kit_blocks = gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"]
        self.assertIn("pip's copy doesn't have kit in #fez-room", kit_blocks[0]["text"])
        self.s.put(gates.CHANNELS, "kit", {"answers": {"c1": "general"}})
        self.s.put(gates.CHANNELS, "pip", {"answers": {"c1": "general", "c2": "games"}})
        ask = [a for a in gates_page.asks(get=self.s.get, keys=self.s.keys) if "both" in a["text"]]
        self.assertEqual(len(ask), 1)
        self.assertIsNone(gates.assemble("kit", {}, {a: self.s.get(gates.CHANNELS, a)[0] for a in ("kit", "pip")})
                          ["channels"]["c1"]["primary"], "nobody answers unmentioned until it's settled")
        self.assertEqual(gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"][0]["title"],
                         "Two agents answer here")


class ChannelsHardening(unittest.TestCase):
    def setUp(self):
        self.s = Store()

    def test_a_malformed_offer_is_refused_and_never_breaks_the_page(self):
        r = gates.api_answer({"kind": "agent", "agent": "kit"}, ["offer-routes", "--doc", json.dumps({"channels": {"c1": "x"}})],
                             run_get=self.s.get, run_put=self.s.put)
        self.assertEqual(r["status"], 400)
        self.s.put(gates.ROUTE_OFFERS, "pip", {"doc": {"channels": ["nope"]}, "at": 1})
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": MAP, "at": 1})
        self.assertEqual(sorted(a["page"] for a in gates_page.asks(get=self.s.get, keys=self.s.keys)), ["channels/kit", "channels/pip"])
        gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)
        odd = json.loads(json.dumps(MAP))
        odd["ignore"] = [[1, 2], {"x": 1}]
        odd["notes_channels"] = [["nested"]]
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": odd, "at": 2})
        self.assertTrue(gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)["sections"], "shown, not raised")
        for bad in ({"notify": {"kit": ["c1"]}}, {"channels": {"c1": {"name": ["x"], "primary": "kit"}}}):
            self.assertIsNotNone(gates.routes_problem(bad))

    def test_an_agents_own_bridge_copy_wins_and_its_extras_are_kept(self):
        mine = json.loads(json.dumps(MAP))
        mine["ignore"] = {"c7": 1}
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": MAP, "at": 1})
        self.s.put(gates.ROUTE_OFFERS, "pip", {"doc": mine, "at": 1})
        act = lambda f: gates_page.act("channels-accept", f, get=self.s.get, put=self.s.put, keys=self.s.keys)
        self.assertFalse(act({"agent": "pip", "from": "kit", "offer_rev": "1"})["ok"], "pip's own bridge has a copy")
        self.assertTrue(act({"agent": "pip", "from": "pip", "offer_rev": "1"})["ok"])
        self.assertEqual(self.s.get(gates.CHANNELS, "pip")[0]["extra"]["ignore"], {"c7": 1})
        # Saved from another's copy before its own bridge offered: keep the file until it's checked again.
        self.s.put(gates.CHANNELS, "pip", {"answers": {"c2": "games"}}, if_rev=None)
        self.assertTrue(act({"agent": "kit", "from": "kit", "offer_rev": "1"})["ok"])
        self.assertIsNone(gates.routes_for("pip", self.s.get, self.s.keys))

    def test_the_household_part_is_shown_before_its_saved(self):
        self.s.put(gates.ROUTE_OFFERS, "kit", {"doc": MAP, "at": 1})
        blocks = gates_page.channels_page("kit", get=self.s.get, keys=self.s.keys)["sections"][0]["blocks"]
        d = [b for b in blocks if b["type"] == "details"]
        self.assertEqual(len(d), 1)
        self.assertIn("Webhook peers", [i["label"] for i in d[0]["blocks"][0]["items"]])


class SnapshotReads(unittest.TestCase):
    """A page reads each collection once, whatever it looks up (each `loom store` is a node process)."""

    def test_one_list_per_collection_and_the_same_answers(self):
        st = Store()
        st.put(gates.PEOPLE, "ada", DOC)
        st.put(gates.CHANNELS, "ada", {"answers": {"1": "general"}})
        st.put(gates.CHANNELS, "bo", {"answers": {"1": "general"}})
        calls = []

        def store(*args, stdin=None, env=None):
            calls.append(args)
            c = args[1]
            rows = [{"key": k, "doc": d, "rev": r} for (cc, k), (d, r) in st.d.items() if cc == c]
            return type("R", (), {"returncode": 0, "stdout": json.dumps(rows), "stderr": ""})()

        snap = gates.Snapshot(store)
        self.assertEqual(gates_page.asks(snap.get, snap.keys), gates_page.asks(st.get, st.keys))
        self.assertEqual(snap.get(gates.PEOPLE, "ada"), (DOC, 1))
        self.assertEqual(snap.get(gates.PEOPLE, "nobody"), (None, 0))
        self.assertEqual(sorted(snap.keys(gates.CHANNELS)), ["ada", "bo"])
        self.assertEqual(sorted(c[1] for c in calls), sorted(set(c[1] for c in calls)))   # each listed once

    def test_a_failed_list_is_an_error_for_get_and_empty_for_keys(self):
        fail = lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "down"})()   # noqa: E731
        snap = gates.Snapshot(fail)
        self.assertEqual(snap.keys(gates.OFFERS), [])
        with self.assertRaises(OSError):
            snap.get(gates.PEOPLE, "ada")


if __name__ == "__main__":
    unittest.main()
