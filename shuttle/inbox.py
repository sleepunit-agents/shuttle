"""Per-body inbox: how a working bridge body hears follow-ups (t-1409).

While a body holds a channel, the bridge appends that channel's new messages
to the body's inbox instead of holding them back; the bridge-inbox hook hands
them to the body after its next tool call, and its Stop hook won't let the
body end its turn while any are unread. When the turn ends the bridge closes
the inbox; anything still pending (arrived after the body's last check) goes
to a fresh body.

Layout: <inbox dir>/<aid8>/ (dir 0700, files 0600), aid8 = the first 8
chars of the agent id, which is also the session id's prefix (the hook only
knows the session id):
    meta.json       {"venue": "#general", "chan": "...", "bridge": "discord"}
    pending.jsonl   one {"id", "text", "msg"} per message, appended by the bridge
    delivered.jsonl what the hook handed over (for counting / debugging)
    closed          present once the bridge has closed it; the hook then no-ops
    lock            fcntl lock shared by bridge and hook

This module is the only code that touches these files. Stdlib only. Message
text lives here only while the body runs; remove() deletes the directory.

The inbox dir is $SHUTTLE_INBOX_DIR if set (tests), else what the adapter
passed to configure() at start (the instance's inbox_dir).
"""
import fcntl
import json
import math
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path


_configured = {"inbox_dir": None, "remote": None}


def configure(inbox_dir=None, remote=None):
    """Set the inbox dir (the instance's inbox_dir); None leaves it as it was.
    The env var still wins. `remote` (shuttle/bodies.py RemoteInbox): the bridge's
    bodies run in another house, so its calls go there; the hook's never do."""
    if inbox_dir:
        _configured["inbox_dir"] = Path(inbox_dir)
    if remote is not None:
        _configured["remote"] = remote


def _remote(name):
    """The remote backend's `name`, when the bridge's bodies are elsewhere; else None."""
    r = _configured["remote"]
    return getattr(r, name) if r is not None else None


def base():
    env = os.environ.get("SHUTTLE_INBOX_DIR")
    if env:
        return Path(env)
    if _configured["inbox_dir"]:
        return _configured["inbox_dir"]
    raise RuntimeError("inbox dir not configured: call inbox.configure(inbox_dir=...) "
                       "or set SHUTTLE_INBOX_DIR")


def path_for(aid):
    return base() / str(aid)[:8]


@contextmanager
def _locked(d):
    fd = os.open(d / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_jsonl(p):
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _append_jsonl(p, items):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")


def open_inbox(aid, venue, chan, bridge):
    """Create the inbox for a body the bridge just spawned."""
    if _remote("open_inbox"):
        return _remote("open_inbox")(aid, venue, chan, bridge)
    d = path_for(aid)
    base().mkdir(mode=0o700, parents=True, exist_ok=True)
    d.mkdir(mode=0o700, exist_ok=True)
    with _locked(d):
        fd = os.open(d / "meta.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"venue": venue, "chan": chan, "bridge": bridge}, f)
    return d


def is_open(aid):
    if _remote("is_open"):
        return _remote("is_open")(aid)
    d = path_for(aid)
    return d.is_dir() and not (d / "closed").exists()


def put(aid, items, now=None):
    """Bridge side: queue items ({"id", "text", "msg"}) for the body, stamped
    with "t" (hand-over time). False if the inbox is missing or closed (the
    caller then holds them as before)."""
    if _remote("put"):
        return _remote("put")(aid, items, now)
    d = path_for(aid)
    if not d.is_dir():
        return False
    t = now if now is not None else time.time()
    with _locked(d):
        if (d / "closed").exists():
            return False
        _append_jsonl(d / "pending.jsonl", [dict(it, t=t) for it in items])
    return True


def take(aid, now=None):
    """Hook side: everything pending, moved to delivered and stamped "rt" (the
    time the body read it). [] if closed or missing."""
    d = path_for(aid)
    if not d.is_dir():
        return []
    rt = now if now is not None else time.time()
    with _locked(d):
        if (d / "closed").exists():
            return []
        items = [dict(it, rt=rt) for it in _read_jsonl(d / "pending.jsonl")]
        if items:
            _append_jsonl(d / "delivered.jsonl", items)
            (d / "pending.jsonl").unlink(missing_ok=True)
    return items


def answered(posted, rt):
    """Did a post at `posted` (sent.log: whole seconds, floored) come after the
    body read the item at `rt`? Only a post in a LATER second counts: one in the
    same second may have been sent just before the read."""
    try:   # rt comes from a body-writable file: anything odd counts as unanswered
        posted, rt = float(posted), float(rt)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(posted) and math.isfinite(rt)):
        return False
    return int(posted) > int(rt)


def close(aid):
    """Bridge side, when the body's turn ended: no more deliveries. Returns what
    was still pending (the caller gives it to a fresh body). Pending stays on
    disk until remove(), so a bridge that dies before saving its state can
    close again after restart and get the same items back."""
    if _remote("close"):
        return _remote("close")(aid)
    d = path_for(aid)
    if not d.is_dir():
        return []
    with _locked(d):
        (d / "closed").touch(mode=0o600)
        return _read_jsonl(d / "pending.jsonl")


def all_ids():
    """aid8 of every inbox on disk."""
    try:
        return [p.name for p in base().iterdir() if p.is_dir()]
    except OSError:
        return []


def delivered(aid):
    """What the hook has handed over so far."""
    if _remote("delivered"):
        return _remote("delivered")(aid)
    return _read_jsonl(path_for(aid) / "delivered.jsonl")


def meta(aid):
    """meta.json as a dict ({} if missing or not an object: it is body-writable)."""
    if _remote("meta"):
        return _remote("meta")(aid)
    try:
        d = json.loads((path_for(aid) / "meta.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def remove(aid):
    if _remote("remove"):
        return _remote("remove")(aid)
    shutil.rmtree(path_for(aid), ignore_errors=True)


STRAY_S = 3600   # a dir no bridge claims (no readable meta) is removed after this


def sweep(bridge, owned8, gc, now=None):
    """Remove `bridge`'s inbox dirs whose body it no longer owns: every aid8 in
    `gc` (dropped by this bridge last tick) and any other dir whose meta names
    this bridge. Both bridges share the base dir, so the other bridge's dirs are
    never touched; a dir with no readable meta (a stray, or one being created
    right now) goes only once it is STRAY_S old. Returns the strays removed
    (not in gc), for the caller to log."""
    if _remote("sweep"):
        return _remote("sweep")(bridge, owned8, gc, now)
    now = time.time() if now is None else now
    strays = []
    for a8 in all_ids():
        try:   # one odd dir (body-writable) must not stop the sweep or the tick
            if a8 in owned8:
                continue
            if a8 in gc:
                remove(a8)
                continue
            owner = meta(a8).get("bridge")
            if owner is None:
                if now - path_for(a8).stat().st_mtime < STRAY_S:
                    continue
            elif owner != bridge:
                continue
            remove(a8)
            strays.append(a8)
        except Exception:
            continue
    return strays
