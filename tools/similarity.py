#!/usr/bin/env python3
"""How alike are two bridges? For every top-level function both files define,
print difflib's ratio over the two sources, highest first.

    python3 tools/similarity.py adapters/discord.py adapters/slack.py

1.00 IDENT means byte-identical. Use it to pick what to lift into the shared
core and to check what is left. It only compares same-named functions, so a
rename hides a duplicate: read the list, don't just trust the numbers.
"""
import ast
import difflib
import sys
from pathlib import Path


def functions(path):
    src = Path(path).read_text()
    return {n.name: ast.get_source_segment(src, n) for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def compare(a_path, b_path):
    a, b = functions(a_path), functions(b_path)
    return sorted(((difflib.SequenceMatcher(None, a[k], b[k]).ratio(), k, a[k] == b[k])
                   for k in a.keys() & b.keys()), key=lambda r: (-r[0], r[1]))


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    for ratio, name, same in compare(argv[1], argv[2]):
        print(f"{ratio:.2f} {name}{' IDENT' if same else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
