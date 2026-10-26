"""Shared module for the "now board" (t-1403): what the Discord and Slack
bridges are handling right now, what is waiting and why.

Each bridge writes a full-state snapshot (<bridge>.json, replaced atomically)
and appends transitions to events-YYYY-MM.jsonl. Conversation ids (c-NNNN) are
the same ones dm-convos.py mints: ids.json maps "{chan}|{who}|{first ts}" ->
c-NNNN and only ever grows.

Observability must never take a bridge down: every writer swallows its errors.
Stdlib only. Paths are resolved at call time: $SHUTTLE_NOW_DIR / $SHUTTLE_NOW_IDS
if set (tests), else what the adapter passed to configure() at start. The
agent slug (configure(agent=...)) is the "agent" of every record and the first
part of every conversation slot key ("<agent>|<bridge>|<chan>").
"""
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

GAP = 7200   # same silence window as dm-convos.py


_configured = {"now_dir": None, "ids_dir": None, "agent": None}


def configure(now_dir=None, ids_dir=None, agent=None):
    """Set the board's dirs and the agent slug (from the instance). Only what is
    given changes; None leaves a value as it was. The env vars still win for the
    dirs."""
    if now_dir:
        _configured["now_dir"] = Path(now_dir)
    if ids_dir:
        _configured["ids_dir"] = Path(ids_dir)
    if agent:
        _configured["agent"] = agent


def _agent():
    return _configured["agent"] or "agent"


def now_dir():
    env = os.environ.get("SHUTTLE_NOW_DIR")
    if env:
        return Path(env)
    if _configured["now_dir"]:
        return _configured["now_dir"]
    raise RuntimeError("now dir not configured: call nowlog.configure(now_dir=...) "
                       "or set SHUTTLE_NOW_DIR")


def ids_dir():
    env = os.environ.get("SHUTTLE_NOW_IDS")
    if env:
        return Path(env)
    if _configured["ids_dir"]:
        return _configured["ids_dir"]
    raise RuntimeError("conversation-id dir not configured: call nowlog.configure(ids_dir=...) "
                       "or set SHUTTLE_NOW_IDS")


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 10**9


def hold_reason(now, last_spawn, cooldown_s, channel_busy, live, cap, avail_mb, min_mb):
    """Why a waiting channel is not being spawned yet, in the bridges' check order."""
    if now - last_spawn < cooldown_s:
        return "cooldown"
    if channel_busy:
        return "queued"
    if live >= cap:
        return "slots_full"
    if avail_mb < min_mb:
        return "low_memory"
    return None


def _mkdir(d):
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.chmod(0o700)   # mode= is umask-masked and ignored for existing dirs


def _remote():
    """(url, token file) for loom's now piece, when the board's reader isn't on
    this machine (a bridge in a house): $SHUTTLE_NOW_REMOTE names the piece
    ("now"), and LOOM_URL / LOOM_TOKEN_FILE say where and as whom. Else None."""
    piece = os.environ.get("SHUTTLE_NOW_REMOTE")
    url = os.environ.get("LOOM_URL")
    tok = os.environ.get("LOOM_TOKEN_FILE")
    if not (piece and url and tok):
        return None
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/api/{piece}", tok


def _post(path, body):
    """One write to loom's now piece. Short timeout: the board can lag, the bridge can't."""
    import urllib.request
    target = _remote()
    if not target:
        return
    base, tok = target
    with open(tok) as f:
        token = f.read().strip()
    req = urllib.request.Request(f"{base}/{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=3) as r:
        r.read()


def _swallow(where, exc):
    print(f"nowlog: {where}: {exc!r}", file=sys.stderr)


_remote_failed = {"at": 0.0}


def _swallow_remote(where, exc):
    """Like _swallow, at most once a minute: loom being down shouldn't fill the log every tick."""
    if time.time() - _remote_failed["at"] >= 60:
        _remote_failed["at"] = time.time()
        _swallow(where, exc)


def _write_private(path, text):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


@contextmanager
def _locked(directory, name):
    _mkdir(directory)
    with open(directory / name, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _mint(ids, key):
    if key not in ids:
        ids[key] = "c-%04d" % (1 + max((int(v[2:]) for v in ids.values()), default=0))
    return ids[key]


def cids_for_keys(keys):
    d = ids_dir()
    with _locked(d, "ids.json.lock"):
        ids = _load(d / "ids.json")
        before = len(ids)
        out = {k: _mint(ids, k) for k in keys}
        if len(ids) != before:
            _write_private(d / "ids.json", json.dumps(ids, indent=0) + "\n")
    return out


def cid_for_key(key):
    return cids_for_keys([key])[key]


def conversation(bridge, chan, who_key, msg_key, msg_t):
    """The c- id of the conversation a message at msg_t belongs to; None on any error."""
    try:
        d = now_dir()
        day = time.strftime("%Y-%m-%d", time.localtime(msg_t))
        slot = f"{_agent()}|{bridge}|{chan}"
        with _locked(d, "convs.json.lock"):
            table = _load(d / "convs.json")
            cur = table.get(slot)
            if cur and msg_t - cur["last"] <= GAP and cur["day"] == day:
                cur["last"] = max(cur["last"], msg_t)
            else:
                cur = table[slot] = {"cid": cid_for_key(f"{chan}|{who_key}|{msg_key}"),
                                     "last": msg_t, "day": day}
            _write_private(d / "convs.json", json.dumps(table, indent=0) + "\n")
            return cur["cid"]
    except Exception as e:
        _swallow("conversation", e)
        return None


def touch(bridge, chan, t):
    """The agent replied: its own activity keeps the conversation open."""
    try:
        d = now_dir()
        with _locked(d, "convs.json.lock"):
            table = _load(d / "convs.json")
            cur = table.get(f"{_agent()}|{bridge}|{chan}")
            if cur:
                cur["last"] = max(cur["last"], t)
                _write_private(d / "convs.json", json.dumps(table, indent=0) + "\n")
    except Exception as e:
        _swallow("touch", e)
        pass


def emit(bridge, kind, **fields):
    try:
        d = now_dir()
        _mkdir(d)
        t = time.time()
        rec = {"v": 1, "t": t, "agent": _agent(), "bridge": bridge, "kind": kind, **fields}
        path = d / time.strftime("events-%Y-%m.jsonl", time.localtime(t))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        _swallow("emit", e)
    try:
        _post("event", {"record": rec})
    except Exception as e:
        _swallow_remote("emit (remote)", e)


def snapshot(bridge, interval_s, cap, mem_mb, talking, holds, socket=None):
    talking = [dict(t, queued=0) for t in talking]
    by_chan = {t["chan"]: t for t in talking}
    waiting = []
    for h in holds:
        if h["reason"] == "queued" and h["chan"] in by_chan:
            by_chan[h["chan"]]["queued"] = h["n"]
        else:
            waiting.append(h)
    return {"v": 1, "agent": _agent(), "bridge": bridge, "at": time.time(),
            "interval_s": interval_s, "slots": {"used": len(talking), "cap": cap},
            "mem_mb": mem_mb, "socket": socket, "talking": talking, "waiting": waiting}


def write_now(bridge, snap):
    try:
        d = now_dir()
        _mkdir(d)
        _write_private(d / f"{bridge}.json", json.dumps(snap))
    except Exception as e:
        _swallow("write_now", e)
    try:
        _post("snapshot", {"bridge": bridge, "snap": snap})
    except Exception as e:
        _swallow_remote("write_now (remote)", e)
