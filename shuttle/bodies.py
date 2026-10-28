"""Bodies in the agent's house (loom docs/specs/bridges.md, "Bodies before the one
service", step 5): the bridge says who to wake and for what; a body host in the
agent's own house wakes them. Shuttle never hosts a body: it says who to wake, and for what.

Everything crosses through shuttle's store in loom, collection `bodies` (an
`agent` collection with no history: message text in flight is gone once
deleted). Every key is the agent's own, `<agent>/<profile>/…`, where the
profile names the house service the host runs as (its secrets are what its
bodies may use, as the bridge's house's were):

    <agent>/<profile>/host          the host's snapshot, rewritten every sync:
                                    {at, model, bodies: {aid: {...}}, delivered: {aid8: [...]}, done: {rid: [aid]}}
    <agent>/<profile>/req/<rid>     a body to start: {prompt, model, deny, env, at}
    <agent>/<profile>/stop/<aid>    stop that body
    <agent>/<profile>/inbox/<aid8>  a body's follow-ups: {meta, pending: [...], closed}

The bridge's side is RemoteRunner (shuttle/runner.py's interface) and
RemoteInbox (shuttle/inbox.py's); the host's is Host. Neither knows the other
is in another house. Stdlib only.
"""
import json
import math
import os
import re
import secrets as _secrets
import time

from shuttle import inbox as local_inbox
from shuttle import runner as runner_mod

COLLECTION = "bodies"
STALE_S = 90          # a host snapshot older than this: no host (the bridge then can't tell)
SYNC_S = 2            # the host's pace
REQ_TTL_S = 300       # a request older than this was given up on by its bridge
BAD_RESUME_BEATS = 15   # a malformed last report: beats before the host adopts what's running instead
RESUME_FIND_S = 30    # a launch a restarted host finds in its last report: how long it's looked for
ENDED_S = 900         # how long an ended body's last entry and sends stay reported (its death is judged on them)
MAX_TEXT = 4000       # a last entry's text, as reported
SENT_LINES = 300      # the tail of the house's sent log, as reported
SUB = re.compile(r"^[a-z][a-z0-9-]{0,39}/[a-z][a-z0-9-]{0,39}/(host|req/[0-9a-f]{16}|stop/[A-Za-z0-9-]{1,64}|inbox/[A-Za-z0-9-]{1,16})$")
# A host's profile is its service's name, `<bridge service>-bodies`: only that bridge's
# service asks it for bodies, and only the host's own service reports. Every body in
# a house carries the house's token, so without this a body could ask another house
# (with other secrets) to start a body of its choosing.
PROFILE = re.compile(r"^[a-z][a-z0-9-]{0,32}-bodies$")
HOST_SUFFIX = "-bodies"
RID = re.compile(r"^[0-9a-f]{16}$")   # a request id, and a Claude launch's spawn token
# Only these may ride along to a body's environment: the launcher's own scope
# switches (Slack's field scope), never a credential.
ENV_PASS = re.compile(r"^(SHUTTLE_[A-Z0-9_]+|BRIDGE_[A-Z0-9_]+)$")


# ---------------------------------------------------------------- the piece API side (loom-shuttle api)

def api_answer(caller, argv, store):
    """{status, body} for `bodies …` from an agent's house, its own keys only; None for anything else.
    `store` has get(c, k), put(c, k, doc), delete(c, k), docs(c, prefix) -> [(key, doc)]."""
    if not (isinstance(argv, list) and argv and argv[0] == "bodies"):
        return None

    def out(v):
        return {"status": 200, "body": {"exit": 0, "stdout": json.dumps(v) + "\n", "stderr": ""}}

    def refuse(status, error):
        return {"status": status, "body": {"error": error}}
    if not isinstance(caller, dict) or caller.get("kind") != "agent" or not isinstance(caller.get("agent"), str):
        return refuse(403, "bodies are an agent's own")
    agent, service = caller["agent"], caller.get("service")
    op, rest = (argv[1] if len(argv) > 1 else ""), argv[2:]

    def key(sub, write=False):
        k = f"{agent}/{sub}"
        if not SUB.match(k):
            raise ValueError(f"not a bodies key: {sub!r}")
        profile, kind = sub.split("/")[0], sub.split("/")[1]
        if not PROFILE.match(profile):
            raise ValueError(f"not a body host: {profile!r}")
        bridge = profile[:-len(HOST_SUFFIX)]
        if service not in (profile, bridge):
            raise PermissionError(f"only the {bridge} bridge and its {profile} host reach {profile}")
        # The bridge asks (req, stop, inbox) and the host reports (host); each may drop what it's done with.
        if write and (service == profile) != (kind == "host") and not (op == "del" and kind in ("req", "stop")):
            raise PermissionError(f"the {service} service doesn't write {kind}")
        return k

    def doc_arg():
        if len(rest) != 3 or rest[1] != "--doc":
            raise ValueError("usage: bodies put|sync <key> --doc JSON")
        d = json.loads(rest[2])
        if not isinstance(d, dict):
            raise ValueError("a document is a JSON object")
        return d
    try:
        if op == "get" and len(rest) == 1:
            doc, rev = store.get(COLLECTION, key(rest[0]))
            return out({"doc": doc, "rev": rev})
        if op == "put" and rest:
            store.put(COLLECTION, key(rest[0], write=True), doc_arg())
            return out({"ok": True})
        if op == "del" and len(rest) == 1:
            store.delete(COLLECTION, key(rest[0], write=True))
            return out({"ok": True})
        if op == "sync":
            # The host's one call a beat: its snapshot in, everything waiting for it out.
            profile = rest[0] if rest else ""
            if not PROFILE.match(profile):
                raise ValueError("bodies sync <profile> --doc JSON")
            if service != profile:
                raise PermissionError(f"only the {profile} service syncs as {profile}")
            store.put(COLLECTION, key(f"{profile}/host", write=True), doc_arg())
            got = {}
            for kind in ("req", "stop", "inbox"):
                pre = f"{agent}/{profile}/{kind}/"
                got[kind] = {k[len(pre):]: d for k, d in store.docs(COLLECTION, pre)}
            return out(got)
    except PermissionError as e:
        return refuse(403, str(e))
    except ValueError as e:
        return refuse(400, str(e))
    return refuse(400, "bodies get|put|del <key>, or bodies sync <profile> --doc JSON")


# ---------------------------------------------------------------- the wire, as a house sees it

class Wire:
    """`bodies` calls as this house's agent: `call(argv)` -> the parsed stdout (gates' _remote_call shape)."""

    def __init__(self, profile, call=None):
        if not PROFILE.match(profile or ""):
            raise ValueError(f"a body host's profile is a service name, not {profile!r}")
        self.profile = profile
        self.call = call or _api

    def get(self, sub):
        return (self.call(["bodies", "get", f"{self.profile}/{sub}"]) or {}).get("doc")

    def put(self, sub, doc):
        self.call(["bodies", "put", f"{self.profile}/{sub}", "--doc", json.dumps(doc)])

    def delete(self, sub):
        self.call(["bodies", "del", f"{self.profile}/{sub}"])

    def sync(self, snapshot):
        return self.call(["bodies", "sync", self.profile, "--doc", json.dumps(snapshot)]) or {}


def _api(argv):
    """shuttle's piece API as this house (LOOM_URL, LOOM_TOKEN_FILE): the parsed stdout."""
    import urllib.request
    from pathlib import Path
    url, tok = os.environ.get("LOOM_URL"), os.environ.get("LOOM_TOKEN_FILE")
    if not (url and tok):
        raise OSError("no loom to ask: LOOM_URL and LOOM_TOKEN_FILE are unset")
    base = url.rstrip("/").removesuffix("/mcp")
    req = urllib.request.Request(f"{base}/api/shuttle/run", data=json.dumps({"argv": argv}).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {Path(tok).read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    if body.get("exit", 1) != 0:
        raise OSError(body.get("stderr") or body.get("error") or "shuttle's API refused")
    return json.loads(body.get("stdout") or "null")


# ---------------------------------------------------------------- the bridge's side: the runner

_SNAP = {}   # profile -> (fetched_at, snapshot): the bridge builds a runner per call, the cache outlives it


def _valid(snap, now=None):
    """The host's report, if it has the shape the bridge reads; else None ("can't tell"). Anything
    in the host's house can write it, so a malformed one must never crash a tick, and one from
    the future must never read as fresh forever."""
    if not isinstance(snap, dict):
        return None
    try:
        at = float(snap.get("at"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(at) or (now is not None and at > now + 60):
        return None
    if not isinstance(snap.get("starting") or {}, dict):
        return None
    for k in ("bodies", "ended", "done", "delivered", "acked"):
        if not isinstance(snap.get(k, {}), dict):
            return None
    for b in list((snap.get("bodies") or {}).values()) + list((snap.get("ended") or {}).values()):
        if not isinstance(b, dict) or not isinstance(b.get("last") or {}, dict):
            return None
        if not all(isinstance(v, str) for v in (b.get("last") or {}).values()):
            return None
        try:
            float(b.get("quiet_s") or 0)
        except (TypeError, ValueError):
            return None
    if not all(isinstance(v, list) for v in (snap.get("done") or {}).values()) or \
            not all(isinstance(v, list) for v in (snap.get("delivered") or {}).values()) or \
            not isinstance(snap.get("sent") or [], list) or not isinstance(snap.get("inboxes") or [], list):
        return None
    return snap


class RemoteRunner(runner_mod.Runner):
    """A runner whose bodies run in the agent's house, through its body host.

    What the host reports is a snapshot at most SYNC_S old; the bridge reads it
    as it reads `claude agents`: a stale, missing or malformed one is "can't tell"
    (list strict -> None), never "every body is gone". A launch the bridge gives up
    on is withdrawn, so the host never starts it late (and stops it if it had)."""

    CLAIM_POLLS = 30   # one a second: the host takes asks one at a time, each up to its own claim
    REFRESH_S = 1.0

    def __init__(self, wire, policy, time=time, secrets=_secrets, model=None):
        self.wire, self.policy, self.time, self.secrets = wire, policy, time, secrets
        self.name = f"{policy.name} in the agent's house"
        self.registry_hint = (f"the {wire.profile} body host hasn't reported for {{n}} ticks; the bridge can't reap, "
                              "so dead bodies keep their {conv}. Is the agent's house up?")
        self.auth_hint = policy.auth_hint
        self._model = model

    def _snap(self, fresh=False):
        at, snap = _SNAP.get(self.wire.profile, (0.0, None))
        if fresh or snap is None or self.time.time() - at >= self.REFRESH_S:
            try:
                snap = _valid(self.wire.get("host"), self.time.time())
            except Exception as e:
                print(f"body host: can't read its report ({e!r})", flush=True)
                snap = None
            _SNAP[self.wire.profile] = (self.time.time(), snap)
        if snap is None or self.time.time() - float(snap["at"]) > STALE_S:
            return None
        return snap

    def _body(self, aid):
        snap = self._snap() or {}
        return (snap.get("bodies") or {}).get(aid) or (snap.get("ended") or {}).get(aid) or {}

    def host_up(self):
        return self._snap() is not None

    # the text rules are the harness's, wherever it runs
    def api_error(self, entry):
        return self.policy.api_error(entry)

    def auth_failure(self, text):
        return self.policy.auth_failure(text)

    def working(self, status):
        return self.policy.working(status)

    def resolve_model(self):
        m = (self._snap() or {}).get("model")
        return m if isinstance(m, str) and m else self._model

    def start(self, prompt, model, deny, env, cwd):
        rid = self.secrets.token_hex(8)
        extra = {k: v for k, v in (env or {}).items() if ENV_PASS.match(k) and os.environ.get(k) != v}
        self.wire.put(f"req/{rid}", {"prompt": prompt, "model": model, "deny": list(deny or []),
                                     "env": extra, "at": self.time.time()})
        return {"rid": rid}

    def _withdraw(self, rid):
        # The prompt goes with it; a host that started it anyway stops what it started.
        try:
            self.wire.put(f"req/{rid}", {"withdrawn": True, "at": self.time.time()})
        except Exception as e:
            print(f"body host: couldn't withdraw request {rid} ({e!r})", flush=True)

    def _look(self, handle, exclude, fresh=False):
        """(ids, decided): decided is False while the host is still starting it, or hasn't got to it."""
        rid = handle.get("rid")
        snap = self._snap(fresh) or {}
        done = snap.get("done") or {}
        ids = {str(a) for a in done.get(rid) or ()} - set(exclude)
        if ids:
            try:   # the request carried the prompt: gone once it's started
                self.wire.delete(f"req/{rid}")
            except Exception:
                pass
        return ids, bool(snap) and rid in done and rid not in (snap.get("starting") or {})

    def claim(self, handle, exclude):
        for _ in range(self.CLAIM_POLLS):
            self.time.sleep(1)
            ids, _ = self._look(handle, exclude, fresh=True)
            if ids:
                return ids
        self._give_up(handle)
        return set()

    def find(self, handle, exclude):
        """One look (the bridge settling a launch it couldn't confirm). A miss while the host
        answers is final, unless the host is still starting it: the bridge retries the batch,
        so the request is withdrawn."""
        ids, _ = self._look(handle, exclude, fresh=True)
        if not ids:
            self._give_up(handle)
        return ids

    def undecided(self, handle):
        snap = self._snap()
        return snap is not None and handle.get("rid") in (snap.get("starting") or {})

    def _give_up(self, handle):
        snap = self._snap()
        if snap is not None and handle.get("rid") not in (snap.get("starting") or {}):
            self._withdraw(handle.get("rid"))

    def list(self, strict=False):
        snap = self._snap()
        if snap is None:
            if strict:
                print(f"body host {self.wire.profile}: no report in {STALE_S}s; not reaping this tick", flush=True)
                return None
            return []
        return [{"id": aid, "status": b.get("status")} for aid, b in (snap.get("bodies") or {}).items()]

    def stop(self, aid):
        try:
            self.wire.put(f"stop/{aid}", {"at": self.time.time()})
        except Exception as e:
            print(f"body host: couldn't ask it to stop {aid} ({e!r})", flush=True)

    def intent(self, aid):
        return ""   # bodies are claimed by request id, never by their prompt

    def done(self, aid):
        return bool(self._body(aid).get("done"))

    def parked(self, aid):
        return bool(self._body(aid).get("parked"))

    def quiet_s(self, aid):
        return float(self._body(aid).get("quiet_s") or 0)

    def last_entry(self, aid):
        return self._body(aid).get("last") or None

    def sent_lines(self):
        """What the bodies posted, from the host's sent log: how the core knows a body answered."""
        return [str(x) for x in (self._snap() or {}).get("sent") or []]


# ---------------------------------------------------------------- the bridge's side: the inbox

class RemoteInbox:
    """shuttle/inbox.py's bridge-side calls, kept in the store for a body in another house.
    The host mirrors each into its own inbox dir, where the body's hook reads it."""

    CLOSE_WAIT_S = 8   # how long close() waits for the host to have closed it too

    def __init__(self, wire, runner):
        self.wire, self.runner = wire, runner

    def _doc(self, aid):
        d = self.wire.get(f"inbox/{str(aid)[:8]}")
        return d if isinstance(d, dict) else None

    def open_inbox(self, aid, venue, chan, bridge):
        self.wire.put(f"inbox/{str(aid)[:8]}", {"meta": {"venue": venue, "chan": chan, "bridge": bridge},
                                                 "pending": [], "closed": False})

    def is_open(self, aid):
        d = self._doc(aid)
        return bool(d) and not d.get("closed")

    def put(self, aid, items, now=None):
        d = self._doc(aid)
        if not d or d.get("closed"):
            return False
        t = now if now is not None else time.time()
        d["pending"] = list(d.get("pending") or []) + [dict(it, t=t) for it in items]
        self.wire.put(f"inbox/{str(aid)[:8]}", d)
        return True

    def delivered(self, aid, snap=None):
        snap = snap if snap is not None else (self.runner._snap() or {})
        return [it for it in (snap.get("delivered") or {}).get(str(aid)[:8]) or [] if isinstance(it, dict)]

    def close(self, aid):
        """Closed here, then (when something's still pending) wait for the host to have closed it
        in the house too: only then is "what the body read" final, and the rest goes to a fresh body."""
        a8 = str(aid)[:8]
        d = self._doc(aid)
        if not d:
            return []
        d["closed"] = True
        self.wire.put(f"inbox/{a8}", d)
        pending = [it for it in d.get("pending") or [] if isinstance(it, dict)]
        snap = self.runner._snap(fresh=True) or {}
        took = {it.get("id") for it in self.delivered(aid, snap)}
        if any(it.get("id") not in took for it in pending):
            for _ in range(self.CLOSE_WAIT_S):
                if a8 in (snap.get("acked") or {}):
                    break
                self.runner.time.sleep(1)
                snap = self.runner._snap(fresh=True) or {}
            took = {it.get("id") for it in self.delivered(aid, snap)}
        if snap:   # what the bridge reads next (delivered) is this final report, not an older one
            _SNAP[self.runner.wire.profile] = (self.runner.time.time(), snap)
        return [it for it in pending if it.get("id") not in took]

    def meta(self, aid):
        m = (self._doc(aid) or {}).get("meta")
        return m if isinstance(m, dict) else {}

    def remove(self, aid):
        self.wire.delete(f"inbox/{str(aid)[:8]}")

    answered = staticmethod(local_inbox.answered)

    def sweep(self, bridge, owned8, gc, now=None):
        """Drop this bridge's inboxes whose body it no longer owns: the ones it dropped last tick,
        and any other the host holds for it (a crash between close and save left one behind)."""
        strays = []
        held = [str(a) for a in (self.runner._snap() or {}).get("inboxes") or []]
        for a8 in set(gc) | set(held):
            if a8 in owned8:
                continue
            try:
                if a8 not in gc and self.meta(a8).get("bridge") != bridge:
                    continue
                self.remove(a8)
                if a8 not in gc:
                    strays.append(a8)
            except Exception:
                continue
        return strays


# ---------------------------------------------------------------- the host, in the agent's house

def _merge(a, b):
    return a + [x for x in b if x not in a]


class Host:
    """Starts the bodies the bridge asks for with the house's own runner, mirrors their
    inboxes into the house's inbox dir, stops what it's told to, and reports. One sync a beat.

    `runner` is a local runner (runner.ClaudeRunner); `inbox` is shuttle/inbox.py, configured
    to the house's inbox dir. Exits (run() returns) after `idle_s` with no live body and nothing asked.

    It never reports what it can't see: a failed registry read, or a restart before it has read
    its last report back, skips the beat (the bridge then reads "can't tell"), never "no bodies"."""

    def __init__(self, wire, runner, inbox=local_inbox, cwd=None, time=time, idle_s=600, sent_log=None,
                 always_deny=()):
        self.wire, self.runner, self.inbox = wire, runner, inbox
        self.always_deny = list(always_deny)   # the instance's: every body here loses these, whatever it's asked
        self.sent_log = sent_log   # where the send tool logs, in this house
        self.cwd = cwd or os.getcwd()
        self.time, self.idle_s = time, idle_s
        self.done = {}            # rid -> [aid]: what the bridge's claim reads
        self.starting = {}        # rid -> launch handle, between start and claim (a crash there must not start it twice)
        self.mine = set()         # aids this host started
        self.ended = {}           # aid -> (when, its last report): its death is judged on these
        self.mirrored = {}        # aid8 -> ids already written to the local pending
        self.acked = {}           # aid8 -> when the house's copy was closed
        self.seen = {}            # aid -> its entry in the last report (an ended body's last word)
        self.resuming = {}        # rid -> until when a launch found in my last report is looked for
        self.last_snap = None     # the last report sent: patched cheaply when a launch starts
        self.last_busy = self.time.time()
        self.got = {}
        self._resumed = False
        self._bad_resumes = 0

    def _resume(self):
        """A restarted host picks up where its last report left off: the bodies it started are
        still its own (else the bridge reads them as dead and retries them), and a launch it was
        claiming when it stopped is found, never started again. Until this has read the last
        report (or found there is none), the host doesn't report."""
        try:
            raw = self.wire.get("host")
        except Exception as e:
            print(f"body host: can't read my last report yet ({e!r}); not reporting until I can", flush=True)
            return False
        prev = _valid(raw) if raw is not None else {}
        if prev is None:   # there is one and it isn't mine to read: never start over as if I had no bodies
            self._bad_resumes += 1
            if self._bad_resumes < BAD_RESUME_BEATS:
                print("body host: my last report is malformed; not reporting yet", flush=True)
                return False
            # Every body running in this house is the host's own (nothing else starts them here):
            # adopt them all rather than stall, so none is read as dead.
            listed = self.runner.list(strict=True)
            if listed is None:
                return False
            print(f"body host: my last report stayed malformed for {BAD_RESUME_BEATS} beats; "
                  f"adopting the {len(listed)} bodies running here", flush=True)
            self.mine |= {a.get("id") for a in listed if a.get("id")}
            self._resumed = True
            return True
        self.mine |= set(prev.get("bodies") or {})
        self.done = {rid: [str(a) for a in ids] for rid, ids in (prev.get("done") or {}).items()}
        self.mine |= {a for ids in self.done.values() for a in ids}
        self.ended = {aid: (self.time.time(), b) for aid, b in (prev.get("ended") or {}).items()}
        starting = prev.get("starting") if isinstance(prev.get("starting"), dict) else {}
        for rid, h in starting.items():
            # Anything in this house can write the report: only a handle of the launch's own shape is
            # looked for, and only among bodies nobody owns yet.
            if rid in self.done or not RID.match(str(rid)) or not isinstance(h, dict) or \
                    not isinstance(h.get("token"), str) or not RID.match(h["token"]):
                continue
            # Looked for over a whole claim window (it may not have registered yet), still reported
            # as starting so the bridge doesn't give up on it meanwhile.
            self.starting[rid] = {"token": h["token"], "before": []}
            self.resuming[rid] = self.time.time() + RESUME_FIND_S
        self._resumed = True
        return True

    def _find_resumed(self):
        """True when a launch was settled (the bridge is waiting on it: report now)."""
        settled = False
        for rid, until in list(self.resuming.items()):
            try:
                ids = sorted(self.runner.find(self.starting[rid], self.mine))
            except Exception as e:
                print(f"body host: finding launch {rid} again failed ({e!r})", flush=True)
                ids = []
            if len(ids) == 1 or self.time.time() >= until:
                self.done[rid] = ids if len(ids) == 1 else []
                self.mine |= set(self.done[rid])
                del self.resuming[rid]
                self.starting.pop(rid, None)
                settled = True
        return settled

    def _entry(self, aid, status):
        last = self.runner.last_entry(aid)
        if isinstance(last, dict):
            last = {k: (str(last.get(k) or "")[:MAX_TEXT]) for k in ("state", "detail", "text")}
        return {"status": status, "done": self.runner.done(aid), "parked": self.runner.parked(aid),
                "quiet_s": self.runner.quiet_s(aid), "last": last if isinstance(last, dict) else None}

    def snapshot(self):
        """The report, or None when the registry can't be read (then nothing is reported)."""
        listed = self.runner.list(strict=True)
        if listed is None:
            if self.mine or self.starting or self.resuming:
                return None
            # Nothing of mine to misread: "no bodies" is exactly true. (A fresh house's registry
            # can't answer until its first body starts the daemon, and it never would if this waited.)
            listed = []
        bodies, delivered = {}, {}
        for a in listed:
            aid = a.get("id")
            if aid in self.mine:
                bodies[aid] = self._entry(aid, a.get("status"))
        now = self.time.time()
        claimed = {a for ids in self.done.values() for a in ids}
        for aid in self.mine - set(bodies) - claimed:   # ended: keep what its death is judged on, a while
            if aid not in self.ended:
                e, seen = self._entry(aid, "gone"), self.seen.get(aid) or {}
                self.ended[aid] = (now, dict(e, last=e["last"] or seen.get("last"), done=e["done"] or seen.get("done", False)))
        self.seen = bodies
        self.ended = {a: v for a, v in self.ended.items() if now - v[0] < ENDED_S and a not in bodies}
        self.mine = set(bodies) | claimed | set(self.ended)
        for a8 in list(self.mirrored):
            delivered[a8] = [{"id": it.get("id"), "rt": it.get("rt")} for it in self.inbox.delivered(a8)]
        handles = {rid: {k: v for k, v in h.items() if k != "before"} for rid, h in self.starting.items()}
        return {"at": now, "model": self.runner.resolve_model(), "bodies": bodies,
                "ended": {a: v[1] for a, v in self.ended.items()}, "delivered": delivered, "done": self.done,
                "starting": handles, "acked": dict(self.acked), "inboxes": sorted(self.mirrored),
                "sent": self._sent()}

    def _sent(self):
        """The sent log's lines for this host's bodies (and job "-", a send that named none:
        the core matches those by channel), the last SENT_LINES. "<epoch> <job> <chan> <bytes>": no text."""
        try:
            lines = self.sent_log.read_text().splitlines()[-2000:] if self.sent_log else []
        except OSError:
            return []
        mine8 = {a[:8] for a in self.mine}
        out = [ln for ln in lines if len(ln.split()) >= 3 and (ln.split()[1] == "-" or ln.split()[1][:8] in mine8)]
        return out[-SENT_LINES:]

    def _report(self):
        snap = self.snapshot()
        if snap is not None:
            self.wire.put("host", snap)
            self.last_snap = snap
        else:   # the registry can't be read: the last report, with what's changed since (claims, launches)
            self._patch()

    def _patch(self):
        if self.last_snap is None:
            return
        # A body started since the last report is busy as far as anyone can tell: never "gone".
        known = dict(self.last_snap.get("bodies") or {})
        for aid in self.mine - set(known) - set(self.last_snap.get("ended") or {}):
            known[aid] = {"status": "busy", "done": False, "parked": False, "quiet_s": 0, "last": None}
        self.wire.put("host", dict(self.last_snap, at=self.time.time(), bodies=known, done=dict(self.done), starting={
            r: {k: v for k, v in hh.items() if k != "before"} for r, hh in self.starting.items()}))

    def step(self):
        if not self._resumed and not self._resume():
            return
        snap = self.snapshot()
        if snap is None:
            print("body host: the registry can't be read; not reporting this beat", flush=True)
            return
        self.got = got = self.wire.sync(snap)
        self.last_snap = snap
        started = self._find_resumed()
        reqs = got.get("req") or {}
        # A request the bridge has deleted was claimed: forget its answer.
        self.done = {rid: ids for rid, ids in self.done.items() if rid in reqs}
        for rid, r in reqs.items():
            try:
                started |= self._take(rid, r)
            except Exception as e:   # one bad ask must not hold up the rest
                print(f"body host: request {rid} failed ({e!r})", flush=True)
                self.starting.pop(rid, None)
        for aid in (got.get("stop") or {}):
            try:
                if aid in self.mine:
                    self.runner.stop(aid)
            except Exception as e:
                print(f"body host: stopping {aid} failed ({e!r})", flush=True)
            self._forget(f"stop/{aid}")
        self._mirror(got.get("inbox") or {})
        if started or self.acked.keys() - set(snap["acked"]):   # the bridge is waiting on these: now, not a beat later
            self._report()
        if any(not b["done"] for b in snap["bodies"].values()) or reqs:
            self.last_busy = self.time.time()

    def _take(self, rid, r):
        """One ask. True when a body was started for it."""
        if not isinstance(r, dict):
            self._forget(f"req/{rid}")
            return False
        if r.get("withdrawn"):
            # The bridge gave up on it and retried: whatever this started for it nobody will claim.
            for aid in self.done.pop(rid, []):
                self.runner.stop(aid)
            self._forget(f"req/{rid}")
            return False
        if rid in self.starting:
            return False
        try:
            age = self.time.time() - float(r.get("at") or 0)
        except (TypeError, ValueError):
            age = REQ_TTL_S + 1
        if age > REQ_TTL_S or not isinstance(r.get("prompt"), str):
            # Malformed, or asked so long ago the bridge has given up on it: its text goes now.
            self.done.pop(rid, None)
            self._forget(f"req/{rid}")
            return False
        if rid in self.done:
            return False
        env = {**os.environ, **{k: str(v) for k, v in (r.get("env") or {}).items() if ENV_PASS.match(k)}} \
            if isinstance(r.get("env"), dict) and r["env"] else None
        h = self.runner.start(r["prompt"], r.get("model") or self.runner.resolve_model(),
                              _merge([str(x) for x in r.get("deny") or []], self.always_deny), env, self.cwd)
        self.starting[rid] = h
        try:   # on record before the wait, cheaply (no registry read): a crash now finds it, never re-starts it
            self._patch()
        except Exception:
            pass
        ids = sorted(self.runner.claim(h, self.mine))
        self.starting.pop(rid, None)
        self.done[rid] = ids
        self.mine |= set(ids)
        print(f"body host: started {ids or 'nothing'} for request {rid}", flush=True)
        return True

    def _forget(self, sub):
        try:
            self.wire.delete(sub)
        except Exception:
            pass

    def _mirror(self, docs):
        for a8, d in docs.items():
            try:
                self._mirror_one(a8, d)
            except Exception as e:
                print(f"body host: inbox {a8} failed ({e!r})", flush=True)
        for a8 in set(self.mirrored) - set(docs):   # the bridge removed it
            self.inbox.remove(a8)
            del self.mirrored[a8]
            self.acked.pop(a8, None)

    def _mirror_one(self, a8, d):
        if not isinstance(d, dict):
            return
        m = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        if a8 not in self.mirrored:
            # What's already here (a restarted host) is never handed over twice.
            here = self.inbox.delivered(a8) + self.inbox._read_jsonl(self.inbox.path_for(a8) / "pending.jsonl")
            if not self.inbox.path_for(a8).is_dir():
                self.inbox.open_inbox(a8, m.get("venue"), m.get("chan"), m.get("bridge"))
            self.mirrored[a8] = {it.get("id") for it in here}
        new = [it for it in d.get("pending") or [] if isinstance(it, dict) and it.get("id") not in self.mirrored[a8]]
        if new and self.inbox.put(a8, [{k: v for k, v in it.items() if k != "t"} for it in new],
                                  now=new[-1].get("t")):
            self.mirrored[a8] |= {it.get("id") for it in new}
        if d.get("closed") and a8 not in self.acked:
            self.inbox.close(a8)   # under the inbox lock: after this the hook takes nothing more
            self.acked[a8] = self.time.time()

    def idle(self):
        """No live body and nothing asked for idle_s (0: never, for a host run as a kept-up service)."""
        return bool(self.idle_s) and self.time.time() - self.last_busy >= self.idle_s

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:
                print(f"body host: sync failed ({e!r})", flush=True)
            if self.idle():
                print(f"body host: idle {self.idle_s}s, leaving", flush=True)
                return
            self.time.sleep(SYNC_S)
