"""Where people are, as the services name them: when someone the bridge knows
(people.json) speaks, the bridge says which id they spoke from and the name the
service shows for it ("@plee"; "Pat Lee (@plee)"), as a `seen` event on the
now board. loom's machine files it on that person's card (the id and its label
in the card's `where`), so a person's page says how to reach them in words.

Once a day per person, venue and id, and again whenever the name changes: a
bridge's memory, so a restart says it once more, which costs one event.
Never raises.
"""
import time

from shuttle import nowlog

DAY = 86400
_said = {}   # (venue, id) -> (label, at)


def label(display=None, handle=None, bot=False):
    """The name a service shows: "Display (@handle)", "@handle" alone, a bot by its name."""
    display = (display or "").strip() or None
    handle = (handle or "").strip() or None
    if bot:
        return display or handle
    if display and handle and display.lower() != handle.lower():
        return f"{display} (@{handle})"
    return f"@{handle}" if handle else display


def seen(bridge, venue, uid, slug, name, now=None, emit=None):
    """Say that `slug` spoke as `uid` on `venue`, named `name`. True when it was said."""
    try:
        if not (bridge and venue and uid and slug and name):
            return False
        now = time.time() if now is None else now
        last = _said.get((venue, str(uid)))
        if last and last[0] == name and now - last[1] < DAY:
            return False
        _said[(venue, str(uid))] = (name, now)
        (emit or nowlog.emit)(bridge, "seen", slug=slug, venue=venue, id=str(uid), label=str(name)[:200])
        return True
    except Exception as e:  # noqa: BLE001 - the board is best effort
        print(f"reach: {e!r}", flush=True)
        return False
