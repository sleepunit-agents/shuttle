import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from shuttle.config import Config


def cfg(name):
    return Config(name=name, state_dir=Path(f"/s/kit-{name}"))


class ConfigTest(unittest.TestCase):
    def test_paths_derive_from_state_dir(self):
        c = cfg("slack")
        self.assertEqual(c.state_file, Path("/s/kit-slack/state.json"))
        self.assertEqual(c.state_lock, Path("/s/kit-slack/state.json.lock"))
        self.assertEqual(c.sent_log, Path("/s/kit-slack/sent.log"))

    def test_two_bridges_never_share_state(self):
        d, s = cfg("discord"), cfg("slack")
        self.assertTrue({d.state_file, d.state_lock, d.sent_log}.isdisjoint(
            {s.state_file, s.state_lock, s.sent_log}))

    def test_paths_can_be_given_one_by_one(self):
        c = Config(name="slack", state_dir=Path("/s"), sent_log=Path("/elsewhere/sent.log"))
        self.assertEqual((c.sent_log, c.state_file), (Path("/elsewhere/sent.log"), Path("/s/state.json")))

    def test_name_and_state_dir_are_required(self):
        with self.assertRaises(TypeError):
            Config(name="slack")

    def test_the_runner_owns_the_harness(self):
        # The claude binary, its job dirs and the model choice moved to the
        # runner (shuttle/runner.py, t-1438); the Config only carries it.
        for gone in ("claude", "jobs_dir", "model_resolver", "preferred_model"):
            self.assertNotIn(gone, Config.__dataclass_fields__)
        self.assertIsNone(cfg("slack").runner)

    def test_instance_defaults(self):
        c = cfg("slack")
        self.assertEqual((c.tag, c.agent, c.always_deny, c.alert, c.runner),
                         ("agent", "agent", (), None, None))

    def test_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            cfg("slack").name = "discord"


if __name__ == "__main__":
    unittest.main()
