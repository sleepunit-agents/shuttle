"""Who may wake a body: one gate for every bridge (t-1423 step 5).

The selvedge is this instance's access data, people.json (who) and routes.json
(where). It lives with the instance (beside its shuttle.json), never in this repo,
because it holds platform ids; this module only reads what the adapter loaded.

people.json, the parts read here:

    {"people": {slug: {"name", "tier", "venues"?,      # 3 the owner, 2 trusted, 1 guest
                       "discord": [user id, ...],
                       "slack": {team id: [user id, ...]}, ...}},
     "agents": {slug: {"name", "kind", "discord"?: [bot user id, ...], ...}}}

The rule, the same on every platform:

- tier 2 and up: anywhere;
- tier 1 (a guest): only inside its `venues`;
- an agent listed with this platform's ids (a peer bot): anywhere. Its turns
  are routed by the adapter's peer rules; being listed only means it counts as
  someone, for the stop word and the board's labels;
- anyone else: nowhere.

Not enforcement: bodies run as the same user that owns these files and the
code that reads them (notes/decisions/2026-10-21-shuttle-beside-loom.md, rule 5).
"""


def index(doc, platform, team=None, sections=("agents", "people")):
    """{platform user id: (slug, record)}. Slack ids are per workspace: pass
    the team id. `sections` says whose ids count (Slack passes only people: an
    agent never passes its gate); a person's entry wins over an agent's for the
    same id. Never raises: an entry of the wrong shape is skipped, so a bad
    edit locks out that entry, not the bridge."""
    out = {}
    if not isinstance(doc, dict):
        return out
    for section in sections:
        entries = doc.get(section)
        if not isinstance(entries, dict):
            continue
        for slug, rec in entries.items():
            if not isinstance(slug, str) or slug.startswith("_") or not isinstance(rec, dict):
                continue
            ids = rec.get(platform)
            if isinstance(ids, dict):
                ids = ids.get(team)
            if isinstance(ids, (str, int)):
                ids = [ids]
            if not isinstance(ids, list):
                continue
            for uid in ids:
                if isinstance(uid, (str, int)) and not isinstance(uid, bool):
                    out[str(uid)] = (slug, dict(rec, agent=section == "agents"))
    return out


def known(rec):
    """Someone the gate can say yes to somewhere: an agent, or a person with a
    tier. Anyone else is a stranger to a body, whatever the file calls them."""
    return bool(rec) and (rec.get("agent") or isinstance(rec.get("tier"), int))


def may_wake(rec, venue=None):
    """(ok, why) for a person or agent record from index(), or None."""
    if not rec:
        return False, "not registered"
    if rec.get("agent"):
        return True, None
    tier = rec.get("tier")
    if not isinstance(tier, int):
        return False, "no tier"
    if tier >= 2:
        return True, None
    if tier == 1 and venue is not None and venue in (rec.get("venues") or []):
        return True, None
    return False, f"tier {tier}" + (" outside its venues" if tier == 1 else "")


def label(rec):
    """The name a body reads beside a message: "Robin", "Bolt (peer agent
    bot)", "Kid (guest)". Resolved from the platform id, never a display name."""
    name = rec.get("name") or "?"
    if rec.get("agent"):
        return f"{name} ({rec.get('kind') or 'agent'})"
    if rec.get("tier") == 1:
        return f"{name} (guest)"
    return name
