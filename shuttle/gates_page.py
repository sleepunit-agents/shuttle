"""The Bridges page's People: each agent's gates, kept in loom (shuttle/gates.py).

A household owner sees, per agent, whether its people document is in loom yet
(and who last changed it), or still a people.json its bridge has offered; takes
an offer with Accept; and edits a document on its own page. Both writes are
owner actions: loom gives this process the grant that lets it write the
`people` collection (loom docs/specs/piece-storage.md). An agent owner never
sees this section.
"""
import json
import re

from shuttle import gates

AGENT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def _count(doc):
    return len((doc or {}).get("people") or {}), len((doc or {}).get("agents") or {})


def section(get=gates.get, keys=gates.list_keys):
    """The People section of the Bridges page: one row an agent, with what to do about it."""
    agents = sorted(set(keys(gates.PEOPLE)) | set(keys(gates.OFFERS)))
    items = []
    for a in agents:
        doc, rev = get(gates.PEOPLE, a)
        offer, _ = get(gates.OFFERS, a)
        if doc is not None:
            p, g = _count(doc)
            items.append({"text": f"{a}'s people", "meta": f"in loom · {p} people, {g} agents · revision {rev}",
                          "page": f"people/{a}"})
        elif offer is not None:
            p, g = _count(offer.get("doc"))
            items.append({"text": f"{a}'s people", "meta": f"still a people.json ({p} people, {g} agents), offered by its bridge",
                          "tone": "warn", "act": {"action": "people-accept", "label": "Accept", "fields": {"agent": a},
                                                  "confirm": f"{a}'s bridge reads its people from loom from now on, and its file is only a fallback."}})
    blocks = [{"type": "list", "items": items}] if items else [
        {"type": "text", "tone": "muted", "text": "No agent's people are in loom yet. A bridge offers its people.json the first time it runs."}]
    blocks.append({"type": "text", "tone": "muted",
                   "text": "Who each platform id is, and who may wake a body. Only you change these; a bridge reads its own agent's."})
    return {"id": "people", "title": "People and gates", "side": True, "blocks": blocks}


def page(agent, get=gates.get):
    if not AGENT.match(agent or ""):
        return {"title": "Not found", "sections": []}
    doc, rev = get(gates.PEOPLE, agent)
    if doc is None:
        return {"title": "Not found", "sections": []}
    return {"title": f"{agent}'s people", "intro": "Who each platform id is (discord, slack ids), their tier (3 owner, 2 trusted, 1 guest, "
            "with venues), and the peer agents. Saved only over the revision you opened.",
            "sections": [{"id": "edit", "blocks": [{"type": "form", "action": "people-save", "submit": "Save",
                                                    "hidden": {"agent": agent, "rev": str(rev)},
                                                    "fields": [{"name": "doc", "label": "The document", "kind": "textarea",
                                                                "value": json.dumps(doc, indent=2, ensure_ascii=False), "required": True}]}]}]}


def act(action, fields, get=gates.get, put=gates.put):
    """Accept an offer, or save an edit. None for an action that isn't People's."""
    agent = str(fields.get("agent") or "")
    if action not in ("people-accept", "people-save"):
        return None
    if not AGENT.match(agent):
        return {"ok": False, "message": "That isn't an agent."}
    try:
        if action == "people-accept":
            offer, _ = get(gates.OFFERS, agent)
            if not offer or gates.valid(offer.get("doc")):
                return {"ok": False, "message": f"{agent}'s bridge hasn't offered a people document."}
            put(gates.PEOPLE, agent, offer["doc"], if_rev=0)
            return {"ok": True, "message": f"{agent}'s people are in loom; its bridge reads them within a minute.", "page": f"people/{agent}"}
        try:
            doc = json.loads(fields.get("doc") or "")
        except ValueError:
            return {"ok": False, "message": "That isn't JSON."}
        why = gates.valid(doc)
        if why:
            return {"ok": False, "message": f"Not saved: {why}."}
        rev = int(fields.get("rev") or "0")
        new = put(gates.PEOPLE, agent, doc, if_rev=rev)
        return {"ok": True, "message": f"Saved {agent}'s people (revision {new}); its bridge reads them within a minute.", "page": f"people/{agent}"}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except OSError as e:
        return {"ok": False, "message": f"loom's store refused it: {e}"}
