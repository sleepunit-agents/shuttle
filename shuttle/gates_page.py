"""The Bridges page's People and routes: each agent's gates and the household's
route map, kept in loom (shuttle/gates.py).

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


# ---------------------------------------------------------------- the section on the Bridges page

def section(get=gates.get, keys=gates.list_keys):
    """People and routes on the Bridges page: one row each, saying what's left to do."""
    agents = sorted(set(keys(gates.PEOPLE)) | set(keys(gates.OFFERS)))
    items, todo = [], 0
    for a in agents:
        doc, rev = get(gates.PEOPLE, a)
        offer, _ = get(gates.OFFERS, a)
        if doc is not None:
            p, g = _count(doc)
            items.append({"text": f"{a}'s people", "meta": f"in loom · {p} people, {g} agents", "page": f"people/{a}"})
        elif offer is not None:
            todo += 1
            p, g = _count(offer.get("doc"))
            # An offer is the bridge's own words: it opens for review, never a one-tap accept.
            items.append({"text": f"{a}'s people", "meta": f"to review · {p} people, {g} agents, from its people.json",
                          "tone": "warn", "page": f"offer/{a}"})
    routes, _ = get(gates.ROUTES, gates.HOUSEHOLD)
    if routes is not None:
        items.append({"text": "The household's routes", "meta": f"in loom · {len(routes.get('channels') or {})} channels",
                      "page": "routes"})
    else:
        for a in sorted(keys(gates.ROUTE_OFFERS)):
            todo += 1
            offer, _ = get(gates.ROUTE_OFFERS, a)
            doc = (offer or {}).get("doc") or {}
            note = _note(doc)
            items.append({"text": f"The household's routes, as {a}'s bridge has them",
                          "meta": f"to review · {len(doc.get('channels') or {})} channels" + (f" · updated {note}" if note else ""),
                          "tone": "warn", "page": f"routes-offer/{a}"})
    blocks = []
    if todo:
        blocks.append({"type": "text", "text": (
            "The bridges still read who's who, and which agent answers in which channel, from files beside them. "
            "Open each one marked to review, check it, and press the button at the bottom: it moves into loom as it is. "
            "Nothing changes for anyone when you do; it's the same data, kept in loom from then on.")})
    blocks.append({"type": "list", "items": items} if items else
                  {"type": "text", "tone": "muted", "text": "Nothing to review: no bridge has offered its files yet."})
    return {"id": "people", "title": "People and routes", "side": True, "blocks": blocks}


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


def page(agent, get=gates.get):
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    doc, _ = get(gates.PEOPLE, agent)
    if doc is None:
        return {"title": "Not found", "sections": []}
    return {"title": f"{agent}'s people", "intro": f"Who each Slack and Discord id is, and what they may ask {agent} for. "
            "Tier 3 is you; 2 is trusted; 1 is a guest, only in the places listed. In loom: changing one here comes next.",
            "sections": [{"id": "doc", "blocks": people_blocks(doc)}]}


def offer_page(agent, get=gates.get):
    """An agent's bridge's offered people.json, as tables, with one button that saves exactly this."""
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    offer, rev = get(gates.OFFERS, agent)
    if not offer or gates.valid(offer.get("doc")):
        return {"title": "Not found", "sections": []}
    blocks = people_blocks(offer["doc"])
    blocks.append({"type": "form", "action": "people-accept", "submit": f"Use these as {agent}'s people", "fields": [],
                   "note": "Saves exactly what's above into loom. Nothing changes for anyone: the bridge reads the same "
                           "people from loom instead of its file.",
                   "hidden": {"agent": agent, "offer_rev": str(rev)}})
    return {"title": f"{agent}'s people, to review",
            "intro": f"{agent}'s bridge still reads who's who from its people.json. This is that file. "
                     "Look it over, especially tier 3, and if it's right, use it.",
            "sections": [{"id": "offer", "blocks": blocks}]}


# ---------------------------------------------------------------- reading a route map

def _names(doc):
    """channel id -> its name, from the map itself."""
    return {cid: (e.get("name") if isinstance(e, dict) else None) for cid, e in ((doc or {}).get("channels") or {}).items()}


def routes_blocks(doc, other=None, other_name=None):
    doc = doc or {}
    channels = {k: v for k, v in (doc.get("channels") or {}).items() if isinstance(v, dict)}
    blocks = []
    if other is not None:
        a, b = channels, {k: v for k, v in ((other or {}).get("channels") or {}).items() if isinstance(v, dict)}
        only_here = sorted(set(a) - set(b))
        only_there = sorted(set(b) - set(a))
        moved = sorted(k for k in set(a) & set(b) if a[k].get("primary") != b[k].get("primary"))
        ign_a, ign_b = set(doc.get("ignore") or {}), set((other or {}).get("ignore") or {})
        if only_here or only_there or moved or ign_a != ign_b:
            def label(ids, src):
                return ", ".join("#" + str(src[k].get("name") or k) for k in ids[:12]) + ("…" if len(ids) > 12 else "")
            bits = []
            if only_here:
                bits.append(f"only here: {label(only_here, a)}")
            if only_there:
                bits.append(f"only in {other_name}'s: {label(only_there, b)}")
            if moved:
                bits.append(f"a different primary in {other_name}'s: {label(moved, a)}")
            if ign_b - ign_a:
                bits.append(f"{other_name}'s also never answers in {len(ign_b - ign_a)} more channel(s): "
                            f"{', '.join(sorted(ign_b - ign_a))[:300]}")
            if ign_a - ign_b:
                bits.append(f"this one never answers in {len(ign_a - ign_b)} channel(s) {other_name}'s does: "
                            f"{', '.join(sorted(ign_a - ign_b))[:300]}")
            blocks.append({"type": "callout", "tone": "warn", "title": f"Not the same as {other_name}'s copy",
                           "text": _cut("; ".join(bits), 1800) + ". Whichever you use becomes the one every bridge reads."})
        else:
            blocks.append({"type": "callout", "title": f"Same as {other_name}'s copy",
                           "text": "Every channel and primary agent matches. Either one is fine."})
    note = _note(doc)
    if note:
        blocks.append({"type": "kv", "items": [{"label": "Last updated", "value": note}]})
    rows = [["#" + _cut(e.get("name") or cid), e.get("primary") or "nobody (mentions only)", cid]
            for cid, e in sorted(channels.items(), key=lambda kv: str(kv[1].get("name") or kv[0]))[:ROWS]]
    blocks.append({"type": "table", "columns": ["Channel", "Answers without a mention", "Id"], "rows": rows})
    bots = doc.get("bots") or {}
    names = _names(doc)
    ignore = doc.get("ignore") or {}
    kv = [{"label": "Bots", "value": _cut(", ".join(sorted(bots))) or "—"},
          {"label": "Never answers in", "value": _cut(", ".join("#" + str(names.get(c) or c) for c in ignore)) or "—"}]
    blocks.append({"type": "kv", "items": kv})
    return blocks


def routes_page(get=gates.get):
    doc, _ = get(gates.ROUTES, gates.HOUSEHOLD)
    if doc is None:
        return {"title": "Not found", "sections": []}
    return {"title": "The household's routes", "intro": "Which agent answers in each channel when nobody's mentioned. "
            "A mention always reaches the agent mentioned. Every agent's bridge reads this. In loom: changing it here comes next.",
            "sections": [{"id": "doc", "blocks": routes_blocks(doc)}]}


def routes_offer_page(agent, get=gates.get, keys=gates.list_keys):
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    offer, rev = get(gates.ROUTE_OFFERS, agent)
    if not offer or not isinstance(offer.get("doc"), dict):
        return {"title": "Not found", "sections": []}
    others = [a for a in sorted(keys(gates.ROUTE_OFFERS)) if a != agent]
    other = (get(gates.ROUTE_OFFERS, others[0])[0] or {}).get("doc") if others else None
    blocks = routes_blocks(offer["doc"], other, others[0] if others else None)
    blocks.append({"type": "form", "action": "routes-accept", "submit": "Use this as the household's routes", "fields": [],
                   "note": "Saves exactly what's above into loom, for every agent's bridge. Nothing changes in the channels "
                           "that match what each bridge has now.",
                   "hidden": {"agent": agent, "offer_rev": str(rev)}})
    return {"title": f"Routes, as {agent}'s bridge has them",
            "intro": f"Which agent answers in each Discord channel when nobody's mentioned. Each bridge kept its own copy "
                     f"of this file; this is {agent}'s. One copy goes into loom and every bridge reads it from then on.",
            "sections": [{"id": "offer", "blocks": blocks}]}


# ---------------------------------------------------------------- the actions

def act(action, fields, get=gates.get, put=gates.put):
    """Use an offer as it was shown. None for an action that isn't People's or routes'."""
    agent = str(fields.get("agent") or "")
    if action not in ("people-accept", "routes-accept"):
        return None
    if not AGENT.match(agent):
        return {"ok": False, "message": "That isn't an agent."}
    try:
        if action == "people-accept":
            offer, rev = get(gates.OFFERS, agent)
            if not offer or str(rev) != str(fields.get("offer_rev") or ""):
                return {"ok": False, "message": f"{agent}'s bridge changed its offer since you opened it: open it again."}
            why = gates.valid(offer.get("doc"))
            if why:
                return {"ok": False, "message": f"Not saved: {why}."}
            put(gates.PEOPLE, agent, offer["doc"], if_rev=0)
            return {"ok": True, "message": f"{agent}'s people are in loom; its bridge reads them within a minute.",
                    "page": f"people/{agent}"}
        offer, rev = get(gates.ROUTE_OFFERS, agent)
        if not offer or str(rev) != str(fields.get("offer_rev") or "") or not isinstance(offer.get("doc"), dict):
            return {"ok": False, "message": "That bridge changed its offer since you opened it: open it again."}
        put(gates.ROUTES, gates.HOUSEHOLD, offer["doc"], if_rev=0)
        return {"ok": True, "message": "The household's routes are in loom; every bridge reads them within a minute.",
                "page": "routes"}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except OSError as e:
        return {"ok": False, "message": f"loom's store refused it: {e}"}
