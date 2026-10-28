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
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

PIECE = "shuttle"
PEOPLE = "people"     # owner: an agent's people document
OFFERS = "offers"     # piece: a bridge's people.json, offered for the owner to accept
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


def list_keys(collection):
    r = _store("list", collection)
    if r.returncode != 0:
        return []
    return [d["key"] for d in json.loads(r.stdout or "[]")]


def valid(doc):
    """Why a people document isn't one, or None."""
    if not isinstance(doc, dict):
        return "it's a JSON object"
    if set(doc) - {"people", "agents", "_comment"}:
        return "it has people, agents and nothing else"
    for section in ("people", "agents"):
        if section in doc and not isinstance(doc[section], dict):
            return f"{section} maps a slug to an entry"
    return None


# ---------------------------------------------------------------- the piece API: a bridge asks for its own

def api_answer(caller, argv, run_get=get, run_put=put):
    """{status, body} for `people` and `offer-people`, from an agent's bridge (its own agent only)."""
    def refuse(status, error):
        return {"status": status, "body": {"error": error}}
    if not isinstance(caller, dict) or caller.get("kind") != "agent" or not isinstance(caller.get("agent"), str):
        return refuse(403, "a bridge asks for its own agent's people")
    agent = caller["agent"]
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

    def __init__(self, file, now=time.time, remote=None):
        # The path, or a function giving it: an adapter's PEOPLE_FILE can be pointed elsewhere (its tests do).
        self._file = file if callable(file) else (lambda: file)
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
                got = self.remote(["people"])
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
                self.remote(["offer-people", "--doc", json.dumps(doc)])
            except Exception as e:
                print(f"people: offering people.json to loom failed ({e!r})", flush=True)
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
    return json.loads(out) if argv == ["people"] else {"ok": True}
