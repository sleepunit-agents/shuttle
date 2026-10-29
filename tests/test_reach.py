"""The names people go by (shuttle/reach.py): a bridge that hears someone
people.json knows says which id they spoke from and what the service calls
them, once a day or when the name changes, for their card on loom."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shuttle import reach
from tests import test_discord_adapter as td
from tests import test_slack_adapter as ts


class Label(unittest.TestCase):
    def test_how_a_service_names_someone(self):
        self.assertEqual(reach.label("Pat Lee", "plee"), "Pat Lee (@plee)")
        self.assertEqual(reach.label("plee", "plee"), "@plee")
        self.assertEqual(reach.label(None, "plee"), "@plee")
        self.assertEqual(reach.label("Pat Lee", None), "Pat Lee")
        self.assertEqual(reach.label(None, "Bolt Bot", bot=True), "Bolt Bot")
        self.assertIsNone(reach.label(None, None))


class Seen(unittest.TestCase):
    def setUp(self):
        reach._said.clear()
        self.out = []
        self.emit = lambda bridge, kind, **f: self.out.append((bridge, kind, f))

    def test_once_a_day_and_again_when_the_name_changes(self):
        t = 1_000_000.0
        self.assertTrue(reach.seen("slack", "slack", "U2", "pat", "Pat (@pat)", now=t, emit=self.emit))
        self.assertFalse(reach.seen("slack", "slack", "U2", "pat", "Pat (@pat)", now=t + 3600, emit=self.emit))
        self.assertTrue(reach.seen("slack", "slack", "U2", "pat", "Pat Lee (@pat)", now=t + 3601, emit=self.emit))
        self.assertTrue(reach.seen("slack", "slack", "U2", "pat", "Pat Lee (@pat)", now=t + 3601 + reach.DAY, emit=self.emit))
        self.assertEqual(self.out[0], ("slack", "seen", {"slug": "pat", "venue": "slack", "id": "U2", "label": "Pat (@pat)"}))
        self.assertEqual(len(self.out), 3)

    def test_nothing_without_a_name_and_never_a_raise(self):
        self.assertFalse(reach.seen("slack", "slack", "U2", "pat", None, emit=self.emit))
        def boom(*a, **k):
            raise OSError("down")
        self.assertFalse(reach.seen("slack", "slack", "U9", "pat", "x", emit=boom))


class Adapters(unittest.TestCase):
    def setUp(self):
        reach._said.clear()
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-reach-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.object(reach.nowlog, "emit")
        self.emit = p.start()
        self.addCleanup(p.stop)

    def test_discord_says_the_name_of_someone_it_knows_and_no_one_else(self):
        m = td.load_adapter(td.make_instance(self.root))
        m.load_who()
        m.who({"id": "11", "username": "robin_l", "global_name": "Robin"})
        m.who({"id": "13", "username": "Bolt", "bot": True})
        m.who({"id": "99", "username": "stranger"})
        self.assertEqual([c.kwargs for c in self.emit.call_args_list], [
            {"slug": "robin", "venue": "discord", "id": "11", "label": "Robin (@robin_l)"},
            {"slug": "bolt", "venue": "discord", "id": "13", "label": "Bolt"}])

    def test_slack_says_it_once_its_gate_knows_who_it_is(self):
        m = ts.load_adapter(ts.make_instance(self.root))
        m.user_verdict = lambda state, uid: (True, None, {"name": "plee", "real_name": "Pat Lee"})
        people = {"U2": ("pat", {"name": "Pat", "tier": 2})}
        m.spawn_gate({}, people, "U2")
        m.spawn_gate({}, people, "U8")   # not in people.json: nothing
        self.assertEqual([c.kwargs for c in self.emit.call_args_list],
                         [{"slug": "pat", "venue": "slack", "id": "U2", "label": "Pat Lee (@plee)"}])


if __name__ == "__main__":
    unittest.main()
