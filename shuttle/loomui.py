"""The shuttle on loom's page (loom docs/extensions.md, "The page", contract v2).

    manifest(view)        `loom-shuttle ui --json`: the Bridges tab, its Home tile,
                          and an ask while a bridge is dark
    page(view, path)      `loom-shuttle ui page --json`: "" is the Bridges page;
                          any other path is Not found

Everything is read from the now board (nowlog): each bridge's snapshot
(<now_dir>/<bridge>.json) and the events log (events-YYYY-MM.jsonl, only the
month files that cover the last 24h, streamed), plus the instance's house
rules and people.json for linking a name to its loom person page.

Read-only, stdlib only, and it never prints a conversation's content or a
body's id: only venues, people's display names, counts and timings, the same as
the board. loom renders it; installing the shuttle is all it takes.
"""
import json
import statistics
import time
from pathlib import Path

DAY = 86400
RECONNECT_QUICK_S = 60      # a socket back up within this is a rotation, not an outage
LIST_ROWS = 200             # the contract's limit for a list's items


# A bridge is dark after this long without a snapshot: 3 intervals, at least
# 30s (the same rule as the hosted board).
def _dark_after(interval_s):
    return max(3 * (interval_s or 0), 30)


def _ago(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _secs(s):
    s = float(s)
    if s < 90:
        return f"{s:.0f}s"
    return _ago(s)


def _clock(t, now):
    """13:02, or "Oct 23 13:02" when it wasn't today."""
    lt, ln = time.localtime(t), time.localtime(now)
    if lt[:3] == ln[:3]:
        return time.strftime("%H:%M", lt)
    return time.strftime("%b %d %H:%M", lt)


def _midnight(now):
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _title(bridge):
    return bridge[:1].upper() + bridge[1:]


def _where(bridge, venue):
    """The board's venue as a place: "#general", "DM", "group DM"."""
    v = venue or "?"
    if v.endswith("-dm"):
        return "DM"
    if v.endswith("-group"):
        return "group DM"
    return v


def _tool(name):
    return name[5:] if name.startswith("mcp__") else name


# ---------------------------------------------------------------- reading


def _snapshots(now_dir):
    out = []
    for path in sorted(Path(now_dir).glob("*.json")):
        if path.name == "convs.json":
            continue
        try:
            snap = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(snap, dict) and snap.get("v") == 1 and isinstance(snap.get("bridge"), str):
            out.append(snap)
    return out


def _month_files(now_dir, since, now):
    """The events files covering [since, now]: this month's, and last month's
    when the window reaches back into it. Local time, as nowlog names them."""
    names = []
    for t in (since, now):
        name = time.strftime("events-%Y-%m.jsonl", time.localtime(t))
        if name not in names:
            names.append(name)
    return [Path(now_dir) / n for n in names]


KINDS = ("picked", "delivered", "finished", "socket")


def _events(now_dir, since, now):
    """Events with since <= t <= now, streamed line by line. A torn or foreign
    line is skipped."""
    for path in _month_files(now_dir, since, now):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"kind"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                    t = float(rec["t"])
                except (ValueError, KeyError, TypeError):
                    continue
                if since <= t <= now and rec.get("kind") in KINDS and isinstance(rec.get("bridge"), str):
                    yield rec


def _people(path):
    """{display name: slug} for people.json's people whose name is theirs alone.

    people.json's keys are the slugs loom's person cards use (the bridges ask
    `loom person show <slug>` with them). Its agents are loom entries too (loom's
    docs/specs/agents-in-people.md), by name and by the label a body sees,
    "Mark (peer agent bot)". A name two entries share links to neither."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    people = doc.get("people") if isinstance(doc, dict) else None
    if not isinstance(people, dict):
        return {}
    agents = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
    by_name = {}
    for group, is_agent in ((people, False), (agents, True)):
        for slug, rec in group.items():
            if not isinstance(rec, dict) or slug.startswith("_"):
                continue
            name = rec.get("name")
            if isinstance(name, str) and name:
                for n in {name, f"{name} ({rec.get('kind') or 'agent'})"} if is_agent else {name}:
                    by_name.setdefault(n, set()).add(slug)
    return {name: next(iter(slugs)) for name, slugs in by_name.items() if len(slugs) == 1}


def _slug_ok(slug):
    return (isinstance(slug, str) and 0 < len(slug) <= 190
            and all(c.islower() or c.isdigit() or c == "-" for c in slug))


class View:
    """One read of the board, at `now`. Built once per command."""

    def __init__(self, now_dir, now=None, rules=None, people_file=None, agent_name=None, agent_slug=None):
        self.now = time.time() if now is None else now
        self.agent_slug = agent_slug
        self.now_dir = Path(now_dir)
        self.rules = rules
        self.people_file = people_file
        self.agent_name = agent_name
        self.since = self.now - DAY
        self.midnight = _midnight(self.now)
        self.bridges = []
        for s in _snapshots(now_dir):
            at = float(s.get("at") or 0)
            dark = self.now - at > _dark_after(s.get("interval_s"))
            self.bridges.append({"name": s["bridge"], "snap": s, "at": at, "dark": dark})
        self._stats = None

    @classmethod
    def of(cls, inst, now=None):
        rules = {"guest_deny": list(inst.guest_deny), "always_deny": list(inst.always_deny),
                 "preferred_model": inst.preferred_model}
        return cls(inst.now_dir, now=now, rules=rules,
                   people_file=inst.dir / "people.json", agent_name=inst.agent_name, agent_slug=inst.agent_slug)

    def stats(self):
        """The last 24h of events, folded once: per bridge (turns and sends
        today, pickup waits, socket changes), per conversation, and the latest
        activity."""
        if self._stats is not None:
            return self._stats
        per = {}
        convs = {}
        who_of = {}        # cid -> who, from its picked/delivered events
        last = None        # (t, bridge, venue, who)
        for r in _events(self.now_dir, self.since, self.now):
            b = per.setdefault(r["bridge"], {"turns": 0, "sent": 0, "waits": [], "socket": []})
            kind, t = r["kind"], float(r["t"])
            if kind == "socket":
                b["socket"].append((t, r.get("state")))
                continue
            who = [w for w in (r.get("who") or []) if isinstance(w, str)]
            cid = r.get("cid")
            if who and cid:
                who_of[cid] = who
            elif cid in who_of:
                who = who_of[cid]
            if kind == "picked":
                if t >= self.midnight:
                    b["turns"] += 1
                try:
                    b["waits"].append(float(r.get("wait_s")))
                except (TypeError, ValueError):
                    pass
                key = (r["bridge"], str(r.get("venue") or "?"), tuple(who))
                c = convs.setdefault(key, {"turns": 0, "last": 0, "members": set()})
                c["turns"] += 1
                c["last"] = max(c["last"], t)
                c["members"].update(m for m in (r.get("members") or []) if _slug_ok(m))
            elif kind == "delivered" and t >= self.midnight:
                b["sent"] += 1
            if last is None or t >= last[0]:
                last = (t, r["bridge"], r.get("venue"), who)
        self._stats = {"per": per, "convs": convs, "last": last}
        return self._stats


# ---------------------------------------------------------------- the manifest


def owner_agents(caller):
    """An owner caller's agents (loom's docs/specs/owners.md): None for a household owner, or a
    caller loom didn't name, else the list. Only an agent owner is ever narrowed."""
    if not isinstance(caller, dict) or caller.get("kind") != "owner":
        return None
    a = caller.get("agents")
    if a is None or a == "*":
        return None
    return [x for x in a if isinstance(x, str)] if isinstance(a, list) else []


def manifest(view):
    live = [b for b in view.bridges if not b["dark"]]
    dark = [b for b in view.bridges if b["dark"]]
    tile = {"label": "Bridges", "value": f"{len(live)} live", "detail": _activity(view),
            "tone": "bad" if dark else "ok", "page": ""}
    asks = [{"text": f"{_title(b['name'])} bridge is dark since {_clock(b['at'], view.now)}",
             "meta": f"no word from it for {_ago(view.now - b['at'])}",
             "page": "", "label": "Look", **({"agent": view.agent_slug} if view.agent_slug else {})} for b in dark][:10]
    # owners: 2: an agent owner sees these bridges only if they own the agent they serve.
    # Each agent's people and channels on its own Bridges tab (loom docs/extensions.md, agentTab).
    return {"v": 2, "owners": 2, "tab": {"label": "Bridges"}, "agentTab": {"label": "Bridges"}, "tile": tile, "asks": asks}


def _activity(view):
    """The most recent conversation: one talking now, else the last event in
    24h, else "idle"."""
    best = None
    for b in view.bridges:
        if b["dark"]:
            continue
        for t in b["snap"].get("talking") or []:
            quiet = float(t.get("active_s") or 0)
            if best is None or quiet < best[0]:
                best = (quiet, b["name"], t.get("venue"), t.get("who") or [])
    if best is not None:
        return f"{_place(best[1], best[2], best[3])}, now"
    last = view.stats()["last"]
    if last is None:
        return "idle"
    t, bridge, venue, who = last
    return f"{_place(bridge, venue, who)}, {_ago(view.now - t)} ago"


def _place(bridge, venue, who):
    where = _where(bridge, venue)
    if where in ("DM", "group DM") and who:
        return f"{where} with {', '.join(who)}"
    return where


# ---------------------------------------------------------------- the page


NOT_FOUND = {"title": "Not found", "sections": []}


def page(view, path, query=None):
    """"": one row a bridge (the board's shape), who's talking now, the last
    24h's conversations; "bridge/<name>": that bridge's numbers."""
    if path.startswith("bridge/"):
        b = next((b for b in view.bridges if b["name"] == path[len("bridge/"):]), None)
        return _bridge_page(view, b) if b else dict(NOT_FOUND)
    if path != "":
        return dict(NOT_FOUND)
    caps = {(b["snap"].get("slots") or {}).get("cap") for b in view.bridges}
    who = view.agent_name or "the agent"
    intro = f"Where people talk to {who}. Each bridge watches a chat service and hands each conversation to a body"
    intro += f", up to {caps.pop()} at once." if len(caps) == 1 and None not in caps else "."
    sections = [_bridges(view)]
    sections += [_dark_section(view, b) for b in view.bridges[:7] if b["dark"]]
    talking = _talking(view)
    if talking:
        sections.append(talking)
    sections.append(_conversations(view))
    if view.rules is not None:
        sections.append(_rules(view.rules))
    sections.append({"id": "signin", "title": "Sign in", "side": True, "blocks": [
        {"type": "link", "text": "Sign a harness in (Claude, Codex): its token goes into the secrets store", "page": "signin"}]})
    return {"title": "Bridges", "intro": intro, "sections": sections}


def _state(view, b):
    """(word, tone) for a bridge: dark, its socket's state, else running."""
    sock = b["snap"].get("socket") if isinstance(b["snap"].get("socket"), dict) else None
    if b["dark"]:
        return f"No check in {_ago(view.now - b['at'])}", "bad"
    if sock and sock.get("state") != "up":
        return f"Socket {sock.get('state') or '?'}", "warn"
    return "Up", "ok"


def _bridges(view):
    """One status row a bridge: its state, what it did today, how long since it checked in."""
    per = view.stats()["per"]
    items = []
    for b in view.bridges[:7]:
        word, tone = _state(view, b)
        st = per.get(b["name"], {"turns": 0})
        used = (b["snap"].get("slots") or {}).get("used", 0)
        busy = f"{used} talking" if used and not b["dark"] else _plural(st["turns"], "turn") + " today"
        items.append({"label": _title(b["name"]), "tone": tone, "text": f"{word} · {busy}",
                      "meta": _ago(view.now - b["at"]), "page": f"bridge/{b['name']}"})
    if not items:
        return {"id": "bridges", "title": "", "blocks": [{"type": "text", "text": "No bridge has checked in yet.", "tone": "muted"}]}
    return {"id": "bridges", "title": "", "blocks": [{"type": "status", "items": items}]}


def _dark_section(view, b):
    """A bridge that stopped checking in: what's known, and where to restart it."""
    return {"id": _section_id(b["name"]), "title": _title(b["name"]), "tone": "bad", "blocks": [
        {"type": "text", "text": _now_line(view, b)},
        {"type": "link", "text": "Restart its house from Boxes", "page": "loom:p/box"}]}


def _talking(view):
    """Who each live bridge is talking to right now, and who's waiting."""
    items = []
    for b in view.bridges[:7]:
        if b["dark"]:
            continue
        for t in b["snap"].get("talking") or []:
            who = ", ".join(t.get("who") or []) or "nobody named"
            bits = [f"active {_ago(t.get('active_s') or 0)}"] + (["stalled"] if t.get("stalled") else []) \
                + ([f"{t['queued']} queued"] if t.get("queued") else [])
            items.append({"text": who, "meta": f"{_title(b['name'])} · {_where(b['name'], t.get('venue'))} · {', '.join(bits)}",
                          "tone": "warn" if t.get("stalled") else "ok"})
        for w in b["snap"].get("waiting") or []:
            who = ", ".join(w.get("who") or []) or "nobody named"
            items.append({"text": who, "meta": f"{_title(b['name'])} · {_where(b['name'], w.get('venue'))} · waiting", "tone": "warn"})
    return {"id": "talking", "title": "Talking now", "blocks": [{"type": "list", "items": items[:LIST_ROWS]}]} if items else None


def _bridge_page(view, b):
    sec = _bridge_section(view, b)
    sec["title"] = ""
    # What it's in right now gets its own card, like the conversations below it (not a line under the tiles).
    now = [x for x in sec["blocks"] if x["type"] == "list"]
    sec["blocks"] = [x for x in sec["blocks"] if x["type"] != "list"]
    cards = [sec] + ([{"id": "now", "title": "Now", "blocks": now}] if now else [])
    return {"title": _title(b["name"]), "sections": cards + [_conversations(view, only=b["name"])]}


def _section_id(name):
    s = "".join(c if c.isascii() and (c.isalnum() or c == "-") else "-" for c in name.lower())
    return ("bridge-" + s)[:40]


def _bridge_section(view, b):
    snap, now = b["snap"], view.now
    st = view.stats()["per"].get(b["name"], {"turns": 0, "sent": 0, "waits": [], "socket": []})
    slots = snap.get("slots") or {}
    sock = snap.get("socket") if isinstance(snap.get("socket"), dict) else None
    if b["dark"]:
        state, tone = "dark", "bad"
    elif sock:
        state = f"socket {sock.get('state') or '?'}"
        tone = "ok" if sock.get("state") == "up" else "warn"
    else:
        state, tone = "running", "ok"
    waits = st["waits"]
    pickup = ({"label": "Pickup, 24h", "value": f"median {_secs(statistics.median(waits))}",
               "detail": f"max {_secs(max(waits))}"} if waits
              else {"label": "Pickup, 24h", "value": "none"})
    stats = [
        {"label": "State", "value": state, "detail": f"checked {_ago(now - b['at'])} ago", "tone": tone},
        {"label": "Bodies", "value": f"{slots.get('used', 0)}/{slots.get('cap', '?')}", "detail": _bodies_detail(b)},
        {"label": "Today", "value": _plural(st["turns"], "turn"), "detail": f"{st['sent']} sent"},
        pickup,
    ]
    # What's happening now: a row per conversation it's in, not a sentence; idle says so on the Bodies tile.
    blocks = [{"type": "stats", "items": stats}]
    if b["dark"]:
        blocks.append({"type": "text", "tone": "bad", "text": _now_line(view, b)})
    elif _now_rows(b):
        blocks.append({"type": "list", "items": _now_rows(b)})
    note = _socket_note(st["socket"], b["name"])
    if note:
        blocks.append({"type": "text", "text": note, "tone": "muted"})
    sec = {"id": _section_id(b["name"]), "title": _title(b["name"]), "blocks": blocks}
    if b["dark"]:
        sec["tone"] = "bad"
    return sec


def _bodies_detail(b):
    """The Bodies tile's line: idle and nobody waiting, or how many are talking and waiting."""
    if b["dark"]:
        return "not known"
    snap = b["snap"]
    talking, waiting = len(snap.get("talking") or []), len(snap.get("waiting") or [])
    if not talking and not waiting:
        return "idle, nobody waiting"
    return ", ".join(x for x in (f"{talking} talking" if talking else "", f"{waiting} waiting" if waiting else "") if x)


def _now_rows(b):
    """A row per conversation the bridge is in now: where and with whom, how long, queued or stalled."""
    rows = []
    for t in b["snap"].get("talking") or []:
        who = ", ".join(t.get("who") or []) or "nobody named"
        bits = [f"active {_ago(t.get('active_s') or 0)}"]
        if t.get("queued"):
            bits.append(f"{t['queued']} queued")
        if t.get("stalled"):
            bits.append("stalled")
        rows.append({"text": f"{_where(b['name'], t.get('venue'))} with {who}", "meta": " · ".join(bits), **({"tone": "warn"} if t.get("stalled") else {})})
    for w in b["snap"].get("waiting") or []:
        who = ", ".join(w.get("who") or []) or "nobody named"
        why = {"slots_full": "every body is busy"}.get(w.get("reason"), w.get("reason") or "")
        rows.append({"text": f"{_where(b['name'], w.get('venue'))} with {who}", "meta": "waiting" + (f": {why}" if why else ""), "tone": "warn"})
    return rows


def _now_line(view, b):
    if b["dark"]:
        return f"Dark: no snapshot since {_clock(b['at'], view.now)}, so who it's talking to isn't known."
    snap = b["snap"]
    parts = []
    for t in snap.get("talking") or []:
        who = ", ".join(t.get("who") or []) or "nobody named"
        bits = [f"active {_ago(t.get('active_s') or 0)}"]
        if t.get("stalled"):
            bits.append("stalled")
        if t.get("queued"):
            bits.append(f"{t['queued']} queued")
        parts.append(f"{_where(b['name'], t.get('venue'))} with {who} ({', '.join(bits)})")
    line = ("Talking now in " + "; ".join(parts) + ".") if parts else "Idle."
    waiting = len(snap.get("waiting") or [])
    return line + (f" {waiting} waiting." if waiting else " Nobody waiting.")


def _socket_note(changes, bridge):
    """"Socket reconnected 5 times in 24h ..." when every drop in the window
    came back up quickly: the platform rotating the socket, not an outage.
    Anything else (a long drop, one still down) gets no note; the state shows it."""
    drops, down_at, longest = 0, None, 0.0
    for t, state in changes:
        if state == "down":
            down_at = t
        elif state == "up" and down_at is not None:
            drops += 1
            longest = max(longest, t - down_at)
            down_at = None
    if drops < 2 or down_at is not None or longest > RECONNECT_QUICK_S:
        return None
    return (f"Socket reconnected {drops} times in 24h, each back up within {_secs(max(longest, 1))}; "
            f"{_title(bridge)} rotates it every few hours.")


def _conversations(view, only=None):
    st = view.stats()
    people = _people(view.people_file) if view.people_file else {}
    items = []
    for (bridge, venue, who), c in sorted(st["convs"].items(), key=lambda kv: (-kv[1]["turns"], -kv[1]["last"])):
        if only and bridge != only:
            continue
        cell = _who_cell(who, c["members"], people)
        text = cell["text"] if isinstance(cell, dict) else cell
        item = {"text": text, "meta": f"{_title(bridge)} · {_where(bridge, venue)} · {_plural(c['turns'], 'turn')}"}
        if len(who) == 1:
            item["avatar"] = _initials(who[0])
        if isinstance(cell, dict):
            item["page"] = cell["page"]
        items.append(item)
    if not items:
        blocks = [{"type": "text", "text": "No conversations in the last 24h.", "tone": "muted"}]
    else:
        blocks = [{"type": "list", "items": items[:LIST_ROWS]}]
    sec = {"id": "conversations", "title": "Conversations, last 24h", "blocks": blocks}
    if not only:
        sec["side"] = True
    return sec


def _initials(name):
    words = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _who_cell(who, members, people):
    """The names, linked to loom's person page when it's one person whose slug
    is known: from the event's members (people.json slugs) or people.json by
    display name. Never a slug guessed from a name."""
    text = ", ".join(who) or "nobody named"
    if len(who) != 1:
        return text
    slug = next(iter(members)) if len(members) == 1 else people.get(who[0])
    if slug and _slug_ok(slug):
        return {"text": text, "page": f"loom:people/{slug}"}
    return text


def _rules(rules):
    def tools(names):
        return ", ".join(_tool(n) for n in names) or "nothing"

    model = rules.get("preferred_model") or "the runner's default"
    return {"id": "house-rules", "title": "House rules", "side": True, "blocks": [
        {"type": "kv", "items": [
            {"label": "Guests can't use", "value": tools(rules.get("guest_deny") or [])},
            {"label": "Nobody can use", "value": tools(rules.get("always_deny") or [])},
            {"label": "Model for new conversations", "value": str(model)},
        ]},
        {"type": "text", "text": "Set in the shuttle instance file, not here.", "tone": "muted"},
    ]}
