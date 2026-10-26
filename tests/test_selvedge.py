import unittest

from shuttle import selvedge

DOC = {
    "_comment": "fixture: made-up ids",
    "people": {
        "boss": {"name": "Boss", "tier": 3, "discord": ["100"], "slack": {"T1": ["U100"]}},
        "pat": {"name": "Pat", "tier": 2, "slack": {"T1": ["U200"], "T2": ["U900"]}},
        "kid": {"name": "Kid", "tier": 1, "discord": ["300"], "venues": ["V1"]},
        "old": {"name": "Old", "slack": {"T1": ["U400"]}},
    },
    "agents": {
        "_comment": "",
        "bolt": {"name": "Bolt", "kind": "peer agent bot", "discord": ["500"]},
        "echo": {"name": "Echo", "kind": "ChatGPT-based agent", "via": "U0X"},
    },
}


class Selvedge(unittest.TestCase):
    def test_index_per_platform_and_team(self):
        d = selvedge.index(DOC, "discord")
        self.assertEqual(sorted(d), ["100", "300", "500"])
        self.assertEqual(sorted(selvedge.index(DOC, "slack", "T1")), ["U100", "U200", "U400"])
        self.assertEqual(sorted(selvedge.index(DOC, "slack", "T2")), ["U900"])
        self.assertTrue(d["500"][1]["agent"])
        self.assertFalse(d["100"][1]["agent"])

    def test_the_rule(self):
        d = selvedge.index(DOC, "discord")
        s = selvedge.index(DOC, "slack", "T1")
        cases = [(d["100"][1], None, True), (s["U200"][1], None, True),
                 (d["300"][1], "V1", True), (d["300"][1], "V2", False), (d["300"][1], None, False),
                 (d["500"][1], "V9", True), (s["U400"][1], None, False), (None, None, False)]
        for rec, venue, want in cases:
            with self.subTest(rec=rec and rec["name"], venue=venue):
                self.assertEqual(selvedge.may_wake(rec, venue)[0], want)

    def test_labels(self):
        d = selvedge.index(DOC, "discord")
        self.assertEqual([selvedge.label(d[k][1]) for k in ("100", "300", "500")],
                         ["Boss", "Kid (guest)", "Bolt (peer agent bot)"])

    def test_a_missing_or_odd_file_lets_nobody_in(self):
        for doc in (None, {}, {"people": {"x": "not a record"}}):
            self.assertEqual(selvedge.index(doc, "discord"), {})

    def test_odd_shapes_skip_the_entry_never_raise(self):
        docs = [{"people": None}, {"people": []}, [], "x",
                {"people": {"a": {"tier": 2, "discord": 5}, "b": {"tier": 2, "discord": "77"},
                            "c": {"tier": 2, "discord": [None, {}, True, "88"]}, "d": [1],
                            "e": {"tier": "2", "discord": ["99"]}}}]
        for doc in docs:
            with self.subTest(doc=doc):
                selvedge.index(doc, "discord")
        idx = selvedge.index(docs[-1], "discord")
        self.assertEqual(sorted(idx), ["5", "77", "88", "99"])
        self.assertFalse(selvedge.may_wake(idx["99"][1])[0])      # a string tier is no tier
        self.assertFalse(selvedge.known(idx["99"][1]))

    def test_slack_counts_only_people_and_a_person_beats_an_agent(self):
        doc = {"people": {"p": {"name": "P", "tier": 2, "slack": {"T1": ["U1"]}}},
               "agents": {"a": {"name": "A", "kind": "bot", "slack": {"T1": ["U1", "U2"]}}}}
        self.assertEqual(selvedge.index(doc, "slack", "T1")["U1"][0], "p")
        self.assertNotIn("U2", selvedge.index(doc, "slack", "T1", sections=("people",)))


if __name__ == "__main__":
    unittest.main()
