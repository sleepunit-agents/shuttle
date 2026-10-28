"""Upgrade when idle (shuttle/upgrade.py): a bridge stops itself only when
nobody's talking and its code is behind, so the service starts it fresh."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shuttle import upgrade  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Idle(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.checked = []
        self.stale = True
        self.u = upgrade.Upgrader(["/x/shuttle"], now=self.clock, env={},
                                  check=lambda r: self.checked.append(r.name) or (("a" * 40, "b" * 40) if self.stale else None))

    def test_never_while_someone_is_talking_or_waiting(self):
        for _ in range(10):
            self.clock.t += 3600
            self.assertIsNone(self.u.tick([{"cid": "c"}], []))
            self.assertIsNone(self.u.tick([], [{"chan": "d"}]))
        self.assertEqual(self.checked, [], "git isn't even asked while busy")

    def test_after_five_idle_minutes_and_at_most_every_ten(self):
        self.assertIsNone(self.u.tick([], []))
        self.clock.t += upgrade.IDLE_S - 1
        self.assertIsNone(self.u.tick([], []))
        self.clock.t += 1
        self.assertIn("upgrading: shuttle is behind (aaaaaaa → bbbbbbb)", self.u.tick([], []))
        self.stale = False
        self.clock.t += 60
        self.assertIsNone(self.u.tick([], []))
        self.assertEqual(self.checked, ["shuttle"], "not asked again within ten minutes")
        self.clock.t += upgrade.CHECK_S
        self.assertIsNone(self.u.tick([], []))
        self.assertEqual(self.checked, ["shuttle", "shuttle"])

    def test_a_conversation_resets_the_idle_clock(self):
        self.u.tick([], [])
        self.clock.t += upgrade.IDLE_S - 10
        self.u.tick([{"cid": "c"}], [])
        self.clock.t += 20
        self.assertIsNone(self.u.tick([], []))

    def test_off_means_off(self):
        u = upgrade.Upgrader(["/x/shuttle"], now=self.clock, env={"SHUTTLE_UPGRADE": "off"}, check=lambda r: ("a", "b"))
        self.clock.t += 10_000
        u.tick([], [])
        self.clock.t += 10_000
        self.assertIsNone(u.tick([], []))


class Behind(unittest.TestCase):
    def git(self, cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="shuttle-upgrade-"))
        self.up, self.work, self.clone = d / "up.git", d / "work", d / "clone"
        self.git(d, "init", "-q", "--bare", "-b", "main", str(self.up))
        self.git(d, "clone", "-q", str(self.up), str(self.work))
        (self.work / "f").write_text("1")
        self.git(self.work, "add", "f")
        self.git(self.work, "commit", "-q", "-m", "one")
        self.git(self.work, "push", "-q", "origin", "HEAD:main")
        self.git(d, "clone", "-q", str(self.up), str(self.clone))

    def test_up_to_date_is_not_behind(self):
        self.assertIsNone(upgrade.behind(self.clone))

    def test_behind_after_upstream_moves(self):
        (self.work / "f").write_text("2")
        self.git(self.work, "commit", "-q", "-am", "two")
        self.git(self.work, "push", "-q", "origin", "HEAD:main")
        b = upgrade.behind(self.clone)
        self.assertIsNotNone(b)
        self.assertNotEqual(b[0], b[1])

    def test_a_checkout_with_its_own_commits_is_left_alone(self):
        (self.clone / "g").write_text("mine")
        self.git(self.clone, "add", "g")
        self.git(self.clone, "commit", "-q", "-m", "local")
        self.assertIsNone(upgrade.behind(self.clone))

    def test_not_a_repo_is_never_behind(self):
        self.assertIsNone(upgrade.behind(Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
