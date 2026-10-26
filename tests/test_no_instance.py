"""The guard: nothing specific to one deployment lives in this repo (t-1436).

Walks shuttle/, adapters/, tests/, tools/ and README.md and fails, with
file:line, on any case-insensitive match of an instance word. This file is
skipped (its pattern list and samples would match themselves); the patterns
are still built from fragments so they read as no instance's name.

An instance can add private names (its people, its agents) without
committing them: point SHUTTLE_GUARD_EXTRA at a file with one regex per
line (blank lines and lines starting with # are skipped).
"""
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("shuttle", "adapters", "tests", "tools")
SCAN_FILES = ("README.md",)

# Built by joining, so no pattern's source text matches another pattern.
_A, _R, _T = "a", "r", "t"
BUILTIN = [
    r"\b" + _A + _R + _T + r"\b",           # the agent's own name as a word
    r"\b" + _A + _R + _T + r"[-_|]",        # its slugs: <name>-discord, <name>_x, <name>|slack
    "jona" + "than", "bar" + "ket", "onter" + "ris", "ct" + "eh",
    r"\bpo" + r"nd\b", "po" + "nd-?field", "empty" + r"\s+cup",
    # the #packages feature (a bare "package" is an ordinary word)
    "#pack" + "ages", "packages" + "_channel", "package" + "-watch", "pkg" + "_tick",
    "~/" + _A.upper() + _R + _T, "/home/" + _A + _R + _T,
    "sleep" + "unit", "materi" + "alized", "alli" + "son", "patr" + "ick",
]


def _patterns():
    pats = list(BUILTIN)
    extra = os.environ.get("SHUTTLE_GUARD_EXTRA")
    if extra:
        for line in Path(extra).expanduser().read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return [re.compile(p, re.IGNORECASE) for p in pats]


def _files():
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if "__pycache__" in p.parts or not p.is_file() or p == Path(__file__).resolve():
                continue
            yield p
    for f in SCAN_FILES:
        yield ROOT / f


def scan(lines_by_name, pats):
    return [f"{name}:{n}: {m.group(0)!r} ({p.pattern})"
            for name, lines in lines_by_name
            for n, line in enumerate(lines, 1)
            for p in pats for m in [p.search(line)] if m]


class NoInstance(unittest.TestCase):
    def test_no_instance_words(self):
        pats = _patterns()
        found = []
        for p in _files():
            try:
                lines = p.read_text().splitlines()
            except UnicodeDecodeError:
                continue
            found += scan([(str(p.relative_to(ROOT)), lines)], pats)
        self.assertEqual(found, [], "instance-specific words in shuttle:\n" + "\n".join(found))

    def test_ordinary_words_pass(self):
        pats = _patterns()
        ok = ["start_new_session", "restart-race", "respond", "apart", "party", "smart",
              "the shuttle package", "dispatch", "a pondering mind", "depart", "chartreuse"]
        self.assertEqual(scan([("x", ok)], pats), [])

    def test_instance_words_fail(self):
        pats = _patterns()
        bad = ["Art said", "art-discord", "art_slack", "art|slack", "~/Art", "/home/art",
               "#packages", "pond-field", "Empty  Cup"]
        for line in bad:
            self.assertTrue(scan([("x", [line])], pats), line)


if __name__ == "__main__":
    unittest.main()
