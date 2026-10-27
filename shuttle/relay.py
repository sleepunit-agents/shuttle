"""Relay: one body asks another to say something (the instance's design note, t-1570).

A body anywhere (a house on any box) calls loom's piece API:

    piece-call shuttle relay --via slack --person <slug> --say "…" --why "…" [--from discord:#general]

The shuttle piece, on loom's machine, checks it and queues it here, one file a
bridge. The bridge takes its queue through the same API (`relay-take --via
slack`) and hands each relay to the conversation it names, as a message in that
person's DM: a body holding that DM gets it, else one starts. The receiving
body answers within that conversation's own rules; a relay never widens them.

No caps (the owner, 2026-10-26: every hard number put in as a human-in-the-middle
stop has turned out wrong). A degenerate loop is caught by its shape instead:
the same text to the same person again, while the last one is still recent,
is refused with why.
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

VIAS = ("slack",)            # the bridges a relay can go into; discord channels come next
SAY_MAX = 4000
WHY_MAX = 500
FROM_MAX = 120
REPEAT_WINDOW_S = 3600       # the same text to the same person inside this is the loop shape
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class RelayError(Exception):
    pass


def home(inst):
    """Where the queues live: the piece's own home under loom, else beside the instance's state."""
    h = os.environ.get("LOOM_PIECE_HOME")
    return (Path(h) if h else Path(inst.state_root) / f"{inst.agent_slug}-relay")


def _queue(inst, via):
    d = home(inst) / "relays"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{via}.jsonl"


def _locked(path):
    f = open(str(path) + ".lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _read(path):
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def _person(people, slug, via):
    p = (people.get("people") or {}).get(slug)
    if not p:
        raise RelayError(f"no person {slug} in people.json; relays go to a person by their slug")
    ids = p.get(via)
    if via == "slack":
        ids = [u for team in (ids or {}).values() for u in team] if isinstance(ids, dict) else (ids or [])
    if not ids:
        raise RelayError(f"{p.get('name', slug)} has no {via} id in people.json")
    return p


def queue(inst, people, *, via, person, say, why="", sender="", now=None):
    """Check a relay and queue it for its bridge. Returns the entry."""
    now = time.time() if now is None else now
    if via not in VIAS:
        raise RelayError(f"a relay goes into {', '.join(VIAS)} for now, not {via}")
    if not SLUG.match(person or ""):
        raise RelayError("--person is a person's slug from people.json")
    say = (say or "").strip()
    if not say:
        raise RelayError("--say is what to tell them, or what to do")
    if len(say) > SAY_MAX or len(why) > WHY_MAX or len(sender) > FROM_MAX:
        raise RelayError(f"--say is {SAY_MAX} characters at most, --why {WHY_MAX}, --from {FROM_MAX}")
    p = _person(people, person, via)
    digest = hashlib.sha256(f"{via}\0{person}\0{say}".encode()).hexdigest()[:16]
    path = _queue(inst, via)
    lock = _locked(path)
    try:
        # The loop shape: this exact text to this person already went, recently.
        sent = home(inst) / "relays" / "sent.jsonl"
        recent = [r for r in _read(sent) if now - r.get("at", 0) < REPEAT_WINDOW_S]
        if any(r.get("digest") == digest for r in recent):
            raise RelayError(f"the same words already went to {p.get('name', person)} on {via} within the hour; "
                             "say something new, or let that conversation answer")
        entry = {"id": "r-" + secrets.token_hex(4), "via": via, "person": person, "name": p.get("name", person),
                 "say": say, "why": why.strip(), "from": sender.strip(), "at": now, "digest": digest}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        sent.write_text("".join(json.dumps(r) + "\n" for r in recent[-200:] + [{"digest": digest, "at": now, "id": entry["id"]}]))
        return entry
    finally:
        lock.close()


def take(inst, via):
    """Everything queued for a bridge, removed from the queue."""
    if via not in VIAS:
        raise RelayError(f"no relay queue for {via}")
    path = _queue(inst, via)
    lock = _locked(path)
    try:
        out = _read(path)
        path.write_text("")
        return out
    finally:
        lock.close()


def brief(entry, agent_name):
    """The text the receiving body sees: plainly a relay, from whom and why, not from the person."""
    where = f" in {entry['from']}" if entry.get("from") else ""
    why = f" Why: {entry['why']}" if entry.get("why") else ""
    return (f"[Relay {entry['id']}: {agent_name}'s body{where} asked this conversation to pass something on to "
            f"{entry.get('name', entry['person'])}. It is not from them.{why}]\n{entry['say']}")


def api_answer(inst, people, request):
    """loom's piece API: {caller, path:'run', body:{argv}} -> {status, body}. This agent's bodies, and the owner."""
    def refuse(status, error):
        return {"status": status, "body": {"error": error}}
    if not isinstance(request, dict):
        return refuse(400, "not a piece API request")
    caller = request.get("caller") or {}
    if caller.get("kind") == "agent":
        if caller.get("agent") != inst.agent_slug:
            return refuse(403, f"these bridges are {inst.agent_slug}'s; another agent relays through its own")
    elif caller.get("kind") != "owner":
        return refuse(403, "only the owner and this agent's bodies relay")
    if request.get("path") != "run":
        return refuse(404, "the one call is run")
    argv = (request.get("body") or {}).get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 16 or not all(isinstance(a, str) and len(a) <= SAY_MAX + 16 for a in argv):
        return refuse(400, "argv is: relay --via V --person SLUG --say TEXT [--why W] [--from WHERE], or relay-take --via V")
    if argv[0] not in ("relay", "relay-take"):
        return refuse(403, "through the API: relay, relay-take")
    flags, i = {}, 1
    while i < len(argv):
        if not argv[i].startswith("--") or i + 1 >= len(argv):
            return refuse(400, f"expected --name value, got {argv[i]!r}")
        flags[argv[i][2:]] = argv[i + 1]
        i += 2
    try:
        if argv[0] == "relay":
            unknown = set(flags) - {"via", "person", "say", "why", "from"}
            if unknown:
                return refuse(400, f"relay doesn't take --{sorted(unknown)[0]}")
            e = queue(inst, people, via=flags.get("via", ""), person=flags.get("person", ""), say=flags.get("say", ""),
                      why=flags.get("why", ""), sender=flags.get("from", ""))
            return {"status": 200, "body": {"exit": 0, "stdout": f"{e['id']}: queued for {e['name']} on {e['via']}; "
                                            f"the body in that conversation takes it from here\n", "stderr": ""}}
        if argv[0] == "relay-take":
            out = take(inst, flags.get("via", ""))
            return {"status": 200, "body": {"exit": 0, "stdout": json.dumps(out) + "\n", "stderr": ""}}
    except RelayError as e:
        return {"status": 200, "body": {"exit": 1, "stdout": "", "stderr": f"relay: {e}\n"}}
    return refuse(403, "through the API: relay, relay-take")
