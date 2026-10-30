"""Who is who, and who may wake a body: an agent's people document, kept in
shuttle's store in loom (loom docs/specs/piece-storage.md, bridges.md step 2),
not in a people.json beside the instance.

The document keeps people.json's shape ({"people": {...}, "agents": {...}},
read by selvedge.py), one per agent: collection `people`, key = the agent's
slug. It's an owner collection: only a household owner, on loom's page, writes
it. A bridge reads its own agent's through shuttle's piece API (`people`), and
until loom has one for it, keeps reading its people.json, which it offers to
loom (`offer-people`, a piece collection) for the owner to accept on the page.

On loom's machine (the piece's own process) the store is `loom store shuttle …`;
in a house it's POST $LOOM_URL/api/shuttle/run as the house's token.
"""
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

PIECE = "shuttle"
PEOPLE = "people"     # owner: an agent's people document
OFFERS = "offers"     # piece: a bridge's people.json, offered for the owner to accept
CHANNELS = "channels"  # owner: an agent's own part of the route map, key = the agent (split() below)
ROUTES = "routes"      # owner: the household's part (the server, channel names, webhook peers), key HOUSEHOLD
ROUTE_OFFERS = "route-offers"   # piece: a bridge's routes.json, offered (key: its agent)
HOUSEHOLD = "household"
# The household owner's rule (2026-10-28): what's shaped like a list of agents isn't the household's.
# A routes.json is mostly per agent (each one's bot, the channels it answers in, where its alerts go),
# so each agent's part is its own document; the household keeps only what has no agent in it.
AGENT_CORE = ("bots", "bot_roles", "channels", "threads", "notify")
HOUSEHOLD_KEYS = ("guild", "webhook_peers", "peer_turns_each", "guests")
REFRESH_S = 60


# ---------------------------------------------------------------- loom's store, on loom's machine

def _store(*args, stdin=None, env=None):
    loom = os.environ.get("LOOM_BIN") or "loom"
    return subprocess.run([loom, "store", PIECE, *args], input=stdin, capture_output=True, text=True, timeout=20,
                          env=env or os.environ)


def get(collection, key):
    """(doc, rev) from shuttle's store, or (None, 0) when there's none."""
    r = _store("get", collection, key)
    if r.returncode == 3:
        return None, 0
    if r.returncode != 0:
        raise OSError(f"loom store: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    return d["doc"], d["rev"]


def put(collection, key, doc, if_rev=None):
    """Write a document; with if_rev, only over that revision. Returns the new rev. An owner
    collection needs the grant loom gives an owner's page action (LOOM_STORE_GRANT)."""
    args = ["put", collection, key] + (["--if-rev", str(if_rev)] if if_rev is not None else [])
    r = _store(*args, stdin=json.dumps(doc))
    if r.returncode == 4:
        raise ValueError("someone changed it since you opened it: reload and try again")
    if r.returncode != 0:
        raise OSError(f"loom store: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)["rev"]


def delete(collection, key):
    r = _store("del", collection, key)
    if r.returncode not in (0, 3):
        raise OSError(f"loom store: {r.stderr.strip()[:200]}")


def docs(collection, prefix):
    """[(key, doc)] of a collection's documents under a key prefix."""
    r = _store("list", collection, "--prefix", prefix, "--limit", "1000")
    if r.returncode != 0:
        raise OSError(f"loom store: {r.stderr.strip()[:200]}")
    return [(d["key"], d.get("doc")) for d in json.loads(r.stdout or "[]")]


def list_keys(collection):
    r = _store("list", collection)
    if r.returncode != 0:
        return []
    return [d["key"] for d in json.loads(r.stdout or "[]")]


class Snapshot:
    """One read of each collection, for a page or manifest that looks up many documents: `get` and `keys` as
    above, from a single `loom store list` per collection, not a process per document. Each `loom store` is a
    whole node process, and the manifest looked up every agent's channels and route offer one at a time on
    each tick (seen at 107% CPU, 2026-10-30). Only for reading: nothing here sees a write made after it read."""

    def __init__(self, store=None):
        self._store = store or _store
        self._lists = {}

    def _list(self, collection):
        if collection not in self._lists:
            r = self._store("list", collection, "--limit", "1000")
            if r.returncode != 0:
                raise OSError(f"loom store: {r.stderr.strip()[:200]}")
            self._lists[collection] = {d["key"]: (d.get("doc"), d.get("rev", 0)) for d in json.loads(r.stdout or "[]")}
        return self._lists[collection]

    def get(self, collection, key):
        return self._list(collection).get(key, (None, 0))

    def keys(self, collection):
        try:
            return list(self._list(collection))
        except OSError:
            return []


def valid(doc):
    """Why a people document isn't one, or None."""
    if not isinstance(doc, dict):
        return "it's a JSON object"
    # Keys starting with _ are notes (_comment, _updated); anything else is people or agents.
    if {k for k in doc if not str(k).startswith("_")} - {"people", "agents"}:
        return "it has people, agents (and _notes) and nothing else"
    for section in ("people", "agents"):
        if section in doc and not isinstance(doc[section], dict):
            return f"{section} maps a slug to an entry"
    return None


# ---------------------------------------------------------------- the route map, per agent

def routes_problem(doc):
    """Why a routes.json can't be read as one, or None (a bridge's offer is checked before it's kept)."""
    if not isinstance(doc, dict):
        return "a route map is a JSON object"
    for k in ("bots", "bot_roles", "channels", "threads", "notify"):
        if k in doc and doc[k] is not None and not isinstance(doc[k], dict):
            return f"{k} maps ids to entries"
    for k in ("channels", "threads"):
        for v in (doc.get(k) or {}).values():
            if not isinstance(v, dict) or not (v.get("primary") is None or isinstance(v.get("primary"), str)):
                return f"each of {k} is {{name, primary}}, primary an agent or null"
            if not (v.get("name") is None or isinstance(v.get("name"), str)):
                return f"a name in {k} is text"
    for k, v in (doc.get("notify") or {}).items():
        if not (v is None or isinstance(v, str)):
            return "notify maps a name to a channel id"
    return None


def split(doc, offered_by):
    """A routes.json, as the household's part and each agent's: (household, {agent: part}).
    What isn't about every agent (ignore, only, a brief's channel list, ...) is the offering
    bridge's own and goes to its agent's part."""
    doc = doc if isinstance(doc, dict) and routes_problem(doc) is None else {}
    bots, roles = doc.get("bots") or {}, doc.get("bot_roles") or {}
    channels = {k: v for k, v in (doc.get("channels") or {}).items() if isinstance(v, dict)}
    threads = {k: v for k, v in (doc.get("threads") or {}).items() if isinstance(v, dict)}
    notify = doc.get("notify") or {}
    agents = set(bots) | {v.get("primary") for v in list(channels.values()) + list(threads.values()) if v.get("primary")}
    agents = sorted(a for a in agents if isinstance(a, str) and a and not a.startswith("_"))
    parts = {}
    for a in agents:
        parts[a] = {"bot": bots.get(a), "bot_role": roles.get(a),
                    "answers": {cid: v.get("name") or cid for cid, v in channels.items() if v.get("primary") == a},
                    "threads": {tid: v for tid, v in threads.items() if v.get("primary") == a},
                    "notify": notify.get(a)}
    if offered_by:
        own = parts.setdefault(offered_by, {"bot": None, "bot_role": None, "answers": {}, "threads": {}, "notify": None})
        own["extra"] = {k: v for k, v in doc.items()
                        if k not in AGENT_CORE + HOUSEHOLD_KEYS and not str(k).startswith("_")}
    household = {k: doc[k] for k in HOUSEHOLD_KEYS if k in doc}
    household["channel_names"] = {cid: v.get("name") or cid for cid, v in channels.items()}
    household["notify"] = {k: v for k, v in notify.items() if k not in agents}
    return household, parts


def claims(parts):
    """channel id -> the agents that answer there without a mention (more than one is a conflict)."""
    out = {}
    for a, p in sorted(parts.items()):
        for cid in (p or {}).get("answers") or {}:
            out.setdefault(cid, []).append(a)
    return out


def assemble(agent, household, parts):
    """routes.json, as `agent`'s bridge reads it, from the household's part and every agent's.
    A channel two agents both answer in has no primary: a mention decides, until an owner settles it."""
    household = household or {}
    own = parts.get(agent) or {}
    names = dict(household.get("channel_names") or {})
    out = {k: household[k] for k in HOUSEHOLD_KEYS if k in household}
    out["bots"] = {a: p.get("bot") for a, p in parts.items() if p.get("bot")}
    out["bot_roles"] = {a: p.get("bot_role") for a, p in parts.items() if p.get("bot_role")}
    chans = {cid: {"name": n, "primary": None} for cid, n in names.items()}
    for cid, who in claims(parts).items():
        name = names.get(cid) or (parts[who[0]].get("answers") or {}).get(cid) or cid
        chans[cid] = {"name": name, "primary": who[0] if len(who) == 1 else None}
    out["channels"] = chans
    out["threads"] = {tid: v for p in parts.values() for tid, v in (p.get("threads") or {}).items()}
    out["notify"] = {**(household.get("notify") or {}), **{a: p.get("notify") for a, p in parts.items() if p.get("notify")}}
    out.update({k: v for k, v in (own.get("extra") or {}).items() if k not in out})
    return out


def routes_for(agent, run_get, list_keys):
    """`agent`'s assembled route map, once loom has the household's part and every agent's part its
    map names; else None (the bridge keeps its routes.json: a partial map would drop someone's channels)."""
    household, _ = run_get(ROUTES, HOUSEHOLD)
    if household is None:
        return None
    saved = {a: run_get(CHANNELS, a)[0] for a in list_keys(CHANNELS)}
    saved = {a: p for a, p in saved.items() if isinstance(p, dict)}
    if agent not in saved:
        return None
    offer, _ = run_get(ROUTE_OFFERS, agent)
    if offer and isinstance(offer.get("doc"), dict):
        _, wanted = split(offer["doc"], agent)
        if set(wanted) - set(saved):
            return None
        # Its own bridge's extras (never-answer list, only, ...) must be in its saved part, else keep the file.
        if (wanted.get(agent) or {}).get("extra") and not saved[agent].get("extra"):
            return None
    return assemble(agent, household, saved)


# ---------------------------------------------------------------- the piece API: a bridge asks for its own

def api_answer(caller, argv, run_get=get, run_put=put, run_keys=None):
    """{status, body} for `people` and `offer-people`, from an agent's bridge (its own agent only)."""
    def refuse(status, error):
        return {"status": status, "body": {"error": error}}
    if not isinstance(caller, dict) or caller.get("kind") != "agent" or not isinstance(caller.get("agent"), str):
        return refuse(403, "a bridge asks for its own agent's people")
    agent = caller["agent"]
    # The household's route map: every agent's bridge reads the same one.
    if argv == ["routes"]:
        doc = routes_for(agent, run_get, run_keys or list_keys)
        ver = None
        if doc is not None:   # changes whenever any part does
            ver = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12]
        return {"status": 200, "body": {"exit": 0, "stdout": json.dumps({"doc": doc, "rev": ver}) + "\n", "stderr": ""}}
    if len(argv) == 3 and argv[:2] == ["offer-routes", "--doc"]:
        try:
            doc = json.loads(argv[2])
        except ValueError:
            return refuse(400, "the offered document isn't JSON")
        why = routes_problem(doc)
        if why:
            return refuse(400, f"not a route map: {why}")
        run_put(ROUTE_OFFERS, agent, {"doc": doc, "at": time.time(), "from": caller.get("service") or caller.get("house") or "a bridge"})
        return {"status": 200, "body": {"exit": 0, "stdout": "offered\n", "stderr": ""}}
    if argv == ["people"]:
        doc, rev = run_get(PEOPLE, agent)
        return {"status": 200, "body": {"exit": 0, "stdout": json.dumps({"doc": doc, "rev": rev}) + "\n", "stderr": ""}}
    if len(argv) == 3 and argv[:2] == ["offer-people", "--doc"]:
        try:
            doc = json.loads(argv[2])
        except ValueError:
            return refuse(400, "the offered document isn't JSON")
        why = valid(doc)
        if why:
            return refuse(400, f"not a people document: {why}")
        run_put(OFFERS, agent, {"doc": doc, "at": time.time(), "from": caller.get("service") or caller.get("house") or "a bridge"})
        return {"status": 200, "body": {"exit": 0, "stdout": "offered\n", "stderr": ""}}
    return None


# ---------------------------------------------------------------- the bridge's side

class People:
    """The agent's people document, as the bridge reads it: from loom once loom has one, else its
    people.json (offered to loom once). Refreshed at most every REFRESH_S; a failed read keeps the
    last good document. version() changes when the document does."""

    def __init__(self, file, now=time.time, remote=None, verb="people", offer="offer-people"):
        # The path, or a function giving it: an adapter's PEOPLE_FILE can be pointed elsewhere (its tests do).
        self._file = file if callable(file) else (lambda: file)
        self.verb, self.offer = verb, offer
        self.now = now
        self.remote = remote if remote is not None else _remote_call
        self._doc, self._ver, self._at, self._offered = None, None, 0.0, False

    @property
    def file(self):
        return Path(self._file())

    def doc(self):
        # From loom: at most every REFRESH_S. From the file: whenever it changes, as before.
        if self._doc is not None and str(self._ver).startswith("loom:") and self.now() - self._at < REFRESH_S:
            return self._doc
        got = None
        if self.now() - self._at >= REFRESH_S or self._doc is None:
            self._at = self.now()
            try:
                got = self.remote([self.verb])
            except Exception as e:  # loom unreachable: keep what we have
                print(f"people: loom unreachable ({e!r}); keeping what was loaded", flush=True)
                if self._doc is not None:
                    return self._doc
            if got and got.get("doc") is not None:
                self._doc, self._ver = got["doc"], f"loom:{got['rev']}"
                return self._doc
        ver = f"file:{self.file.stat().st_mtime}"
        if self._doc is not None and ver == self._ver:
            return self._doc
        doc = json.loads(self.file.read_text())          # raises as reading people.json always did
        self._doc, self._ver = doc, ver
        if got is not None and not self._offered:
            self._offered = True
            try:
                self.remote([self.offer, "--doc", json.dumps(doc)])
            except Exception as e:
                print(f"{self.verb}: offering {self.file.name} to loom failed ({e!r})", flush=True)
        return self._doc

    def version(self):
        self.doc()
        return self._ver


def _remote_call(argv):
    """shuttle's piece API, as this house: {"doc", "rev"} for `people`; None where there's no loom to ask."""
    url, tok = os.environ.get("LOOM_URL"), os.environ.get("LOOM_TOKEN_FILE")
    if not (url and tok):
        return None
    base = url.rstrip("/").removesuffix("/mcp")
    token = Path(tok).read_text().strip()
    req = urllib.request.Request(f"{base}/api/shuttle/run", data=json.dumps({"argv": argv}).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    if body.get("exit", 1) != 0:
        raise OSError(body.get("stderr") or "shuttle's API refused")
    out = body.get("stdout") or ""
    return json.loads(out) if argv in (["people"], ["routes"]) else {"ok": True}


def Routes(file, **kw):
    """The household's route map, as a bridge reads it: loom's once loom has it, else its routes.json (offered once)."""
    return People(file, verb="routes", offer="offer-routes", **kw)
