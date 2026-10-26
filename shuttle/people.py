"""A person's loom card, for a bridge turn (t-1381).

One person, three sources, each the owner of its own fields:

- who they are and what gates them -> the instance's people.json (platform
  ids, tier, projects, field scope). Trust fields: git-tracked, changed only
  on the owner's word, never from a loom write.
- the condensed current state of them -> their loom person card
  (`loom person show <slug>`: where they are in each system with a freshness
  marker, how they want it, what they were said to be allowed to do, a
  summary). loom owns it; `may` lines are descriptive, never a grant
  (~/Code/loom docs/specs/person-cards.md).
- what's live with them (conversations, open tasks) -> the instance's own
  `who` tool; the shuttle holds none of it.

loom_card(slug, loom) is what every bridge turn carries; the caller passes the
loom binary (the instance's commands.loom). Read only; nothing here writes
anywhere.
"""
import subprocess
from pathlib import Path

CARD_TIMEOUT_S = 10


def loom_card(slug, loom=None):
    """The rendered card, or None. Best-effort: no card (exit 3), an archived
    one (4), an error, a timeout, or a loom too old to know `person` (it falls
    through to the stdio server and prints nothing) all give None — a turn
    never fails over a card."""
    if not loom:   # no loom command configured (the instance's commands.loom): no card
        return None
    try:
        out = subprocess.run([str(loom), "person", "show", slug], capture_output=True,
                             text=True, timeout=CARD_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    if out.returncode != 0 or not text.startswith("## "):
        return None
    return text
