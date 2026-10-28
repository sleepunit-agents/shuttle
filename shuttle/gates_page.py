"""Each agent's people and channels, kept in loom (shuttle/gates.py), shown on
that agent's own page: who each id is and what they may ask of it; where it
answers without a mention, and where it never answers.

Each bridge still reads these from files beside it (people.json, routes.json)
until a household owner moves them into loom: the bridge offers its file, the
owner reads it here, as tables, and presses one button that saves exactly what
was shown (bound to the offer's revision, so a bridge that changes its offer
meanwhile is refused). Both writes are owner actions: loom gives this process
the grant that lets it write the `people` and `routes` collections (loom
docs/specs/piece-storage.md). An agent owner never sees this section.

Nothing here edits a document field by field yet: a page holds no field longer
than 2000 characters, and these documents are longer than that.
"""
import re

from shuttle import gates

AGENT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
CELL = 300   # a table cell, shortened past this
ROWS = 150   # rows a table shows


def _cut(s, n=CELL):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _count(doc):
    return len((doc or {}).get("people") or {}), len((doc or {}).get("agents") or {})


def _note(doc):
    """A document's own `_updated` note, shortened: how a person tells two copies apart."""
    u = (doc or {}).get("_updated")
    return _cut(u, 160) if isinstance(u, str) and u else ""


# ---------------------------------------------------------------- reading a people document

def owners_in(doc):
    """The slugs a document gives tier 3: whoever has it can do anything the owner can."""
    return sorted(k for k, v in ((doc or {}).get("people") or {}).items() if isinstance(v, dict) and v.get("tier") == 3)


TIERS = {3: "3 · owner", 2: "2 · trusted", 1: "1 · guest"}


def _where(e):
    out = []
    for platform in ("discord", "slack"):
        v = e.get(platform)
        if v:
            n = len(v) if isinstance(v, (list, dict)) else 1
            out.append(f"{platform} ({n} id{'s' if n != 1 else ''})" if n > 1 else platform)
    return ", ".join(out) or "—"


def _extras(e):
    bits = []
    for k in ("grant", "restrict", "projects", "field"):
        v = e.get(k)
        if v:
            bits.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, (list, tuple)) else ', '.join(map(str, v)) if isinstance(v, dict) else v}")
    return _cut("; ".join(bits)) if bits else ""


def people_blocks(doc):
    people = {k: v for k, v in ((doc or {}).get("people") or {}).items() if isinstance(v, dict)}
    agents = {k: v for k, v in ((doc or {}).get("agents") or {}).items() if isinstance(v, dict)}
    blocks = []
    top = owners_in(doc)
    if top:
        blocks.append({"type": "callout", "tone": "warn", "title": "Tier 3",
                       "text": f"Tier 3 can do anything you can, through this agent: {', '.join(top)}. Check each is someone who should."})
    order = sorted(people.items(), key=lambda kv: (-(kv[1].get("tier") or 0), kv[0]))
    rows = [[_cut(e.get("name") or slug), slug, TIERS.get(e.get("tier"), str(e.get("tier") or "—")), _where(e), _extras(e)]
            for slug, e in order[:ROWS]]
    blocks.append({"type": "table", "columns": ["Who", "Slug", "Tier", "Where", "Also"], "rows": rows})
    if len(order) > ROWS:
        blocks.append({"type": "text", "tone": "muted", "text": f"…and {len(order) - ROWS} more."})
    if agents:
        blocks.append({"type": "table", "columns": ["Agent", "Slug", "Kind", "Where"],
                       "rows": [[_cut(e.get("name") or slug), slug, _cut(e.get("kind") or "—"), _cut(e.get("via") or _where(e))]
                                for slug, e in sorted(agents.items())[:ROWS]]})
    return blocks


# ---------------------------------------------------------------- reading a route map

# ---------------------------------------------------------------- an agent's channels

def _sources(agent, get, keys):
    """{bridge agent: (its offer rev, `agent`'s part per that bridge's routes.json)} for every bridge
    whose file says something about `agent`."""
    out = {}
    for b in sorted(keys(gates.ROUTE_OFFERS)):
        try:   # one bridge's odd file must not take the page down
            offer, rev = get(gates.ROUTE_OFFERS, b)
            if offer and isinstance(offer.get("doc"), dict) and gates.routes_problem(offer["doc"]) is None:
                household, parts = gates.split(offer["doc"], b)
                if agent in parts:
                    out[b] = (rev, parts[agent], household, offer["doc"])
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _source_for(agent, sources):
    """Whose copy to show for `agent`: its own bridge's, else the copy that knows its bot."""
    if agent in sources:
        return agent
    with_bot = [b for b, v in sources.items() if v[1].get("bot")]
    return (with_bot or sorted(sources) or [None])[0]


def channel_blocks(agent, part, names=None):
    names = names or {}
    part = part or {}
    rows = [["#" + _cut(n or cid), cid] for cid, n in sorted((part.get("answers") or {}).items(), key=lambda kv: str(kv[1]))][:ROWS]
    blocks = [{"type": "table", "columns": ["Answers here without a mention", "Id"], "rows": rows} if rows else
              {"type": "text", "tone": "muted", "text": f"{agent} answers nowhere without a mention: only when someone mentions it."}]
    never = (part.get("extra") or {}).get("ignore") or {}
    kv = [{"label": "Its bot", "value": str(part.get("bot") or "—")},
          {"label": "Its bot's role", "value": str(part.get("bot_role") or "—")},
          {"label": "Its alerts go to", "value": "#" + str(names.get(part.get("notify")) or part.get("notify")) if part.get("notify") else "—"}]
    if part.get("threads"):
        kv.append({"label": "Threads it answers in", "value": str(len(part["threads"]))})
    if never:
        kv.append({"label": "Never answers in", "value": _cut(", ".join("#" + str(names.get(c) or c) for c in never))})
    for k, v in sorted((part.get("extra") or {}).items()):
        if k != "ignore":
            kv.append({"label": f"Its bridge's {k}", "value": _cut(", ".join("#" + str(names.get(c) or c) for c in v)
                                                                   if isinstance(v, (dict, list)) else v)})
    blocks.append({"type": "kv", "items": kv})
    return blocks


def _differences(agent, shown_by, sources):
    """How other bridges' copies differ from the one shown, about this agent's channels."""
    _, mine, _, _ = sources[shown_by]
    bits = []
    for b, (_, theirs, household, _) in sources.items():
        if b == shown_by:
            continue
        names = household.get("channel_names") or {}
        a, t = set(mine.get("answers") or {}), set(theirs.get("answers") or {})
        if t - a:
            bits.append(f"{b}'s copy also has {agent} answering in " + ", ".join("#" + str(names.get(c) or c) for c in sorted(t - a)))
        if a - t:
            bits.append(f"{b}'s copy doesn't have {agent} in " + ", ".join("#" + str((mine.get("answers") or {}).get(c) or c) for c in sorted(a - t)))
        if (theirs.get("bot") or None) != (mine.get("bot") or None):
            bits.append(f"{b}'s copy has {'a different bot' if theirs.get('bot') else 'no bot'} for {agent}")
    return bits


def _conflicts(agent, part, get, keys):
    """Channels where `part` answers and another agent's saved part does too."""
    out = []
    for other in keys(gates.CHANNELS):
        if other == agent:
            continue
        theirs, _ = get(gates.CHANNELS, other)
        both = set((part or {}).get("answers") or {}) & set((theirs or {}).get("answers") or {})
        out += [(cid, other) for cid in sorted(both)]
    return out


# ---------------------------------------------------------------- the agent's section and its pages

def agent_section(agent, get=gates.get, keys=gates.list_keys):
    """`agent`'s people and channels: one row each, with what's left to do."""
    if not AGENT.match(agent or ""):
        return None
    items = []
    doc, _ = get(gates.PEOPLE, agent)
    offer, _ = get(gates.OFFERS, agent)
    if doc is not None:
        p, g = _count(doc)
        items.append({"text": "People", "meta": f"{p} people, {g} agents", "page": f"people/{agent}"})
    elif offer is not None:
        p, g = _count(offer.get("doc"))
        items.append({"text": "People", "meta": f"to check · {p} people, {g} agents, from its bridge's file",
                      "tone": "warn", "page": f"people/{agent}"})
    part, _ = get(gates.CHANNELS, agent)
    if part is not None:
        items.append({"text": "Channels", "meta": f"answers in {len(part.get('answers') or {})} without a mention",
                      "page": f"channels/{agent}"})
    elif _sources(agent, get, keys):
        items.append({"text": "Channels", "meta": "to check · from its bridge's file", "tone": "warn", "page": f"channels/{agent}"})
    if not items:
        return None
    return {"id": f"people-{agent}", "title": "People and channels", "side": True, "blocks": [{"type": "list", "items": items}]}


def people_page(agent, get=gates.get):
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    doc, _ = get(gates.PEOPLE, agent)
    if doc is not None:
        return {"title": f"{agent}'s people", "intro": f"Who each Slack and Discord id is, and what they may ask {agent} for. "
                "Tier 3 is you; 2 is trusted; 1 is a guest, only in the places listed. Changing one here comes next.",
                "sections": [{"id": "doc", "blocks": people_blocks(doc)}]}
    offer, rev = get(gates.OFFERS, agent)
    if not offer or gates.valid(offer.get("doc")):
        return {"title": "Not found", "sections": []}
    blocks = people_blocks(offer["doc"])
    blocks.append({"type": "form", "action": "people-accept", "submit": f"Use these as {agent}'s people", "fields": [],
                   "note": "Saves exactly what's above into loom. Nothing changes for anyone: the bridge reads the same "
                           "people from loom instead of its file.",
                   "hidden": {"agent": agent, "offer_rev": str(rev)}})
    return {"title": f"{agent}'s people, to check",
            "intro": f"{agent}'s bridge still reads who's who from a file. This is that file. "
                     "Look it over, especially tier 3, and if it's right, use it.",
            "sections": [{"id": "offer", "blocks": blocks}]}


def channels_page(agent, get=gates.get, keys=gates.list_keys):
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    household, _ = get(gates.ROUTES, gates.HOUSEHOLD)
    names = (household or {}).get("channel_names") or {}
    part, _ = get(gates.CHANNELS, agent)
    if part is not None:
        blocks = channel_blocks(agent, part, names)
        for cid, other in _conflicts(agent, part, get, keys):
            blocks.insert(0, {"type": "callout", "tone": "warn", "title": "Two agents answer here",
                              "text": f"#{names.get(cid) or cid}: {agent} and {other} both answer without a mention, so "
                                      "neither does until one stops: a mention decides."})
        return {"title": f"{agent}'s channels", "intro": f"Where {agent} answers in Discord when nobody's mentioned. "
                "A mention always reaches the agent mentioned. Changing these here comes next.",
                "sections": [{"id": "doc", "blocks": blocks}]}
    sources = _sources(agent, get, keys)
    shown_by = _source_for(agent, sources)
    if shown_by is None:
        return {"title": "Not found", "sections": []}
    rev, mine, hh, _ = sources[shown_by]
    blocks = []
    diffs = _differences(agent, shown_by, sources)
    if diffs:
        blocks.append({"type": "callout", "tone": "warn", "title": "The bridges' copies differ",
                       "text": _cut("; ".join(diffs), 1800) + f". This shows {shown_by}'s copy; using it settles it."})
    for cid, other in _conflicts(agent, mine, get, keys):
        blocks.append({"type": "callout", "tone": "warn", "title": "Two agents would answer here",
                       "text": f"#{(mine.get('answers') or {}).get(cid) or cid}: {other} already answers there without a mention."})
    blocks += channel_blocks(agent, mine, hh.get("channel_names") or {})
    note = _note(sources[shown_by][3])
    if note:
        blocks.append({"type": "kv", "items": [{"label": f"{shown_by}'s copy last updated", "value": note}]})
    first = household is None
    if first:   # saved with it: shown first
        kv = [{"label": "Discord server", "value": str(hh.get("guild") or "—")},
              {"label": "Channel names", "value": str(len(hh.get("channel_names") or {}))},
              {"label": "Webhook peers", "value": _cut(", ".join(str(k) for k in (hh.get("webhook_peers") or {}) if not str(k).startswith("_")) or "—")},
              {"label": "Guests", "value": str(len([k for k in (hh.get("guests") or {}) if not str(k).startswith("_")]))},
              {"label": "Peer turns each", "value": str(hh.get("peer_turns_each") if hh.get("peer_turns_each") is not None else "no cap")},
              {"label": "Household alerts", "value": _cut(", ".join(f"{k}: #{(hh.get('channel_names') or {}).get(v) or v}" for k, v in (hh.get("notify") or {}).items())) or "—"}]
        blocks.append({"type": "details", "summary": "Also saved: the household's part of this file", "blocks": [{"type": "kv", "items": kv}]})
    blocks.append({"type": "form", "action": "channels-accept", "submit": f"Use these as {agent}'s channels", "fields": [],
                   "note": "Saves exactly what's above into loom" + (
                       ", with the household's part of the same file (the Discord server, channel names, webhook peers)"
                       if first else "") + ". The bridges keep reading their files until every agent's channels are in loom, "
                       "then switch with nothing changing.",
                   "hidden": {"agent": agent, "from": shown_by, "offer_rev": str(rev)}})
    return {"title": f"{agent}'s channels, to check",
            "intro": f"Where {agent} answers in Discord when nobody's mentioned, as {shown_by}'s bridge has it in its file. "
                     "If it's right, use it.",
            "sections": [{"id": "offer", "blocks": blocks}]}


def asks(get=gates.get, keys=gates.list_keys):
    """What's waiting for a household owner: offers to check, and channels two agents claim.
    Never tagged with an agent: loom shows an agent owner the asks tagged with their agents, and
    these are the household owner's alone (the pages refuse anyone else)."""
    out = []
    for a in sorted(set(keys(gates.OFFERS)) - set(keys(gates.PEOPLE))):
        out.append({"text": f"Check {a}'s people", "meta": "its bridge still reads who's who from a file",
                    "page": f"people/{a}", "label": "Check"})
    saved = set(keys(gates.CHANNELS))
    wanted = set()
    for b in keys(gates.ROUTE_OFFERS):
        try:
            offer, _ = get(gates.ROUTE_OFFERS, b)
            if offer and isinstance(offer.get("doc"), dict) and gates.routes_problem(offer["doc"]) is None:
                wanted |= set(gates.split(offer["doc"], b)[1])
        except (AttributeError, TypeError, ValueError):
            continue
    for a in sorted(wanted - saved):
        out.append({"text": f"Check {a}'s channels", "meta": "where it answers without a mention, still in a bridge's file",
                    "page": f"channels/{a}", "label": "Check"})
    parts = {a: get(gates.CHANNELS, a)[0] for a in saved}
    for cid, who in sorted(gates.claims({a: p for a, p in parts.items() if isinstance(p, dict)}).items()):
        if len(who) > 1:
            out.append({"text": f"{' and '.join(who)} both answer in a channel without a mention",
                        "meta": "neither does until one stops", "page": f"channels/{who[0]}", "label": "Look"})
    return out


# ---------------------------------------------------------------- the actions

def act(action, fields, get=gates.get, put=gates.put, keys=gates.list_keys):
    """Use an offer as it was shown. None for an action that isn't People's or channels'."""
    agent = str(fields.get("agent") or "")
    if action not in ("people-accept", "channels-accept"):
        return None
    if not AGENT.match(agent):
        return {"ok": False, "message": "That isn't an agent."}
    try:
        if action == "people-accept":
            offer, rev = get(gates.OFFERS, agent)
            if not offer or str(rev) != str(fields.get("offer_rev") or ""):
                return {"ok": False, "message": f"{agent}'s bridge changed its file since you opened it: open it again."}
            why = gates.valid(offer.get("doc"))
            if why:
                return {"ok": False, "message": f"Not saved: {why}."}
            put(gates.PEOPLE, agent, offer["doc"], if_rev=0)
            return {"ok": True, "message": f"{agent}'s people are in loom; its bridge reads them within a minute.",
                    "page": f"people/{agent}"}
        src = str(fields.get("from") or "")
        offer, rev = get(gates.ROUTE_OFFERS, src) if AGENT.match(src) else (None, 0)
        if not offer or str(rev) != str(fields.get("offer_rev") or "") or not isinstance(offer.get("doc"), dict):
            return {"ok": False, "message": "That bridge changed its file since you opened it: open it again."}
        household, parts = gates.split(offer["doc"], src)
        if agent not in parts:
            return {"ok": False, "message": f"That file says nothing about {agent}."}
        if src != agent and get(gates.ROUTE_OFFERS, agent)[0] is not None:
            # Its own bridge has a copy: that one carries its own extras (where it never answers, ...).
            return {"ok": False, "message": f"{agent}'s own bridge has offered its file: use that one."}
        if get(gates.ROUTES, gates.HOUSEHOLD)[0] is None:
            put(gates.ROUTES, gates.HOUSEHOLD, household, if_rev=0)
        put(gates.CHANNELS, agent, parts[agent], if_rev=0)
        left = sorted(set(parts) - set(keys(gates.CHANNELS)) - {agent})
        return {"ok": True, "page": f"channels/{agent}", "message": f"{agent}'s channels are in loom. " + (
            f"The bridges switch once {', '.join(left)}'s are too." if left else "The bridges read them within a minute.")}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except OSError as e:
        return {"ok": False, "message": f"loom's store refused it: {e}"}
