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
        return gates_page.act(action, fields, get=self.s.get, put=self.s.put)

    def test_an_offer_is_reviewed_then_saved_over_the_offer_shown(self):
        self.s.put(gates.OFFERS, "pip", {"doc": DOC, "at": 1, "from": "discord"})
        sec = gates_page.section(get=self.s.get, keys=self.s.keys)
        row = sec["blocks"][0]["items"][0]
        self.assertEqual(row["page"], "offer/pip")
        self.assertNotIn("act", row, "never a one-tap accept")
        pg = gates_page.offer_page("pip", get=self.s.get)
        callout, form = pg["sections"][0]["blocks"]
        self.assertIn("robin", callout["text"], "tier 3 is flagged")
        self.assertEqual(form["hidden"], {"agent": "pip", "offer_rev": "1"})
        # The bridge changes its offer after the owner opened it: refused.
        self.s.put(gates.OFFERS, "pip", {"doc": {"people": {"evil": {"tier": 3}}}, "at": 2})
        self.assertFalse(self.act("people-accept", {"agent": "pip", "offer_rev": "1", "doc": form["fields"][0]["value"]})["ok"])
        # Opened again and saved as the owner read (and trimmed) it.
        form = gates_page.offer_page("pip", get=self.s.get)["sections"][0]["blocks"][-1]
        trimmed = {"people": {}, "agents": {}}
        self.assertTrue(self.act("people-accept", {"agent": "pip", "offer_rev": form["hidden"]["offer_rev"], "doc": json.dumps(trimmed)})["ok"])
        self.assertEqual(self.s.get(gates.PEOPLE, "pip"), (trimmed, 1))
        # Accepting again doesn't overwrite what's there now.
        self.assertFalse(self.act("people-accept", {"agent": "pip", "offer_rev": "2", "doc": json.dumps(DOC)})["ok"])

    def test_saving_is_over_the_revision_opened_and_checked(self):
        self.s.put(gates.PEOPLE, "kit", DOC)
        form = gates_page.page("kit", get=self.s.get)["sections"][0]["blocks"][0]
        self.assertEqual(form["hidden"], {"agent": "kit", "rev": "1"})
        edited = dict(DOC, people={**DOC["people"], "kim": {"name": "Kim", "tier": 1}})
        self.assertTrue(self.act("people-save", {"agent": "kit", "rev": "1", "doc": json.dumps(edited)})["ok"])
        stale = self.act("people-save", {"agent": "kit", "rev": "1", "doc": json.dumps(DOC)})
        self.assertFalse(stale["ok"])
        self.assertIn("reload", stale["message"])
        self.assertFalse(self.act("people-save", {"agent": "kit", "rev": "2", "doc": "{nope"})["ok"])
        self.assertFalse(self.act("people-save", {"agent": "kit", "rev": "2", "doc": '{"people": {}, "x": 1}'})["ok"])
        self.assertFalse(self.act("people-save", {"agent": "../x", "rev": "2", "doc": "{}"})["ok"])
        self.assertIsNone(self.act("something-else", {}))


if __name__ == "__main__":
    unittest.main()
