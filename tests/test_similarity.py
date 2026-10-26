import tempfile
import unittest
from pathlib import Path

from tools.similarity import compare

A = "def same(x):\n    return x\n\ndef near(x):\n    return x + 1\n\ndef only_a():\n    pass\n"
B = "def same(x):\n    return x\n\ndef near(x):\n    return x + 2\n\ndef only_b():\n    pass\n"


class SimilarityTest(unittest.TestCase):
    def test_compares_only_shared_names_identical_first(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.py"), Path(d, "b.py")
            a.write_text(A)
            b.write_text(B)
            rows = compare(a, b)
        self.assertEqual([r[1] for r in rows], ["same", "near"])
        self.assertEqual(rows[0][0], 1.0)
        self.assertTrue(rows[0][2])
        self.assertFalse(rows[1][2])
        self.assertLess(rows[1][0], 1.0)


if __name__ == "__main__":
    unittest.main()
