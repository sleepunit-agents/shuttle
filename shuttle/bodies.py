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
MAX_TEXT = 4000       # a last entry's text, as reported
SENT_LINES = 300      # the tail of the house's sent log, as reported
SUB = re.compile(r"^[a-z][a-z0-9-]{0,39}/[a-z][a-z0-9-]{0,39}/(host|req/[0-9a-f]{16}|stop/[A-Za-z0-9-]{1,64}|inbox/[A-Za-z0-9-]{1,16})$")
# A host's profile is its service's name, `<bridge service>-bodies`: only that bridge's
# service asks it for bodies, and only the host's own service reports. Every body in
# a house carries the house's token, so without this a body could ask another house
# (with other secrets) to start a body of its choosing.
PROFILE = re.compile(r"^[a-z][a-z0-9-]{0,32}-bodies$")
HOST_SUFFIX = "-bodies"
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


class RemoteRunner(runner_mod.Runner):
    """A runner whose bodies run in the agent's house, through its body host.

    What the host reports is a snapshot at most SYNC_S old; the bridge reads it
    as it reads `claude agents`: a stale or missing one is "can't tell" (list
    strict -> None), never "every body is gone"."""

    CLAIM_POLLS = 20   # one a second: the host's beat, a house start is the bridge's to wait out
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
                snap = self.wire.get("host")
            except Exception as e:
                print(f"body host: can't read its report ({e!r})", flush=True)
                snap = None
            _SNAP[self.wire.profile] = (self.time.time(), snap)
        if not isinstance(snap, dict) or self.time.time() - float(snap.get("at") or 0) > STALE_S:
            return None
        return snap

    def _body(self, aid):
        return ((self._snap() or {}).get("bodies") or {}).get(aid) or {}

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
        return (self._snap() or {}).get("model") or self._model

    def start(self, prompt, model, deny, env, cwd):
        rid = self.secrets.token_hex(8)
        extra = {k: v for k, v in (env or {}).items() if ENV_PASS.match(k) and os.environ.get(k) != v}
        self.wire.put(f"req/{rid}", {"prompt": prompt, "model": model, "deny": list(deny or []),
                                     "env": extra, "at": self.time.time()})
        return {"rid": rid}

    def claim(self, handle, exclude):
        for _ in range(self.CLAIM_POLLS):
            self.time.sleep(1)
            ids = self.find(handle, exclude, fresh=True)
            if ids:
                return ids
        return set()

    def find(self, handle, exclude, fresh=False):
        rid = handle.get("rid")
        done = (self._snap(fresh) or {}).get("done") or {}
        ids = set(done.get(rid) or ()) - set(exclude)
        if rid in done:   # started, or tried and never showed: either way the host is done with it
            try:   # the request carried the prompt: gone once it's started
                self.wire.delete(f"req/{rid}")
            except Exception:
                pass
        return ids

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

    def __init__(self, wire, runner):
        self.wire, self.runner = wire, runner

    def _doc(self, aid):
        return self.wire.get(f"inbox/{str(aid)[:8]}")

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

    def delivered(self, aid):
        return list((((self.runner._snap() or {}).get("delivered")) or {}).get(str(aid)[:8]) or [])

    def close(self, aid):
        d = self._doc(aid)
        if not d:
            return []
        d["closed"] = True
        self.wire.put(f"inbox/{str(aid)[:8]}", d)
        took = {it.get("id") for it in self.delivered(aid)}
        return [it for it in d.get("pending") or [] if it.get("id") not in took]

    def meta(self, aid):
        m = (self._doc(aid) or {}).get("meta")
        return m if isinstance(m, dict) else {}

    def remove(self, aid):
        self.wire.delete(f"inbox/{str(aid)[:8]}")

    answered = staticmethod(local_inbox.answered)

    def sweep(self, bridge, owned8, gc, now=None):
        for a8 in gc:
            if a8 not in owned8:
                try:
                    self.remove(a8)
                except Exception:
                    pass
        return []


# ---------------------------------------------------------------- the host, in the agent's house

def _merge(a, b):
    return a + [x for x in b if x not in a]


class Host:
    """Starts the bodies the bridge asks for with the house's own runner, mirrors their
    inboxes into the house's inbox dir, stops what it's told to, and reports. One sync a beat.

    `runner` is a local runner (runner.ClaudeRunner); `inbox` is shuttle/inbox.py, configured
    to the house's inbox dir. Exits (run() returns) after `idle_s` with no live body and nothing asked."""

    def __init__(self, wire, runner, inbox=local_inbox, cwd=None, time=time, idle_s=600, sent_log=None,
                 always_deny=()):
        self.wire, self.runner, self.inbox = wire, runner, inbox
        self.always_deny = list(always_deny)   # the instance's: every body here loses these, whatever it's asked
        self.sent_log = sent_log   # where the send tool logs, in this house
        self.cwd = cwd or os.getcwd()
        self.time, self.idle_s = time, idle_s
        self.done = {}            # rid -> [aid]: what the bridge's claim reads
        self.mine = set()         # aids this host started
        self.mirrored = {}        # aid8 -> ids already written to the local pending
        self.last_busy = self.time.time()
        self.got = {}
        self._resumed = False

    def _resume(self):
        """A restarted host picks up where its last report left off: the bodies it started are
        still its own (else the bridge reads them as dead and retries them)."""
        self._resumed = True
        try:
            prev = self.wire.get("host") or {}
        except Exception:
            return
        self.mine |= set(prev.get("bodies") or {})
        self.done = {rid: list(ids) for rid, ids in (prev.get("done") or {}).items() if isinstance(ids, list)}
        self.mine |= {a for ids in self.done.values() for a in ids}

    def snapshot(self):
        bodies, delivered = {}, {}
        for a in self.runner.list() or []:
            aid = a.get("id")
            if aid not in self.mine:
                continue
            last = self.runner.last_entry(aid)
            if isinstance(last, dict):
                last = {k: (str(last.get(k) or "")[:MAX_TEXT]) for k in ("state", "detail", "text")}
            bodies[aid] = {"status": a.get("status"), "done": self.runner.done(aid), "parked": self.runner.parked(aid),
                           "quiet_s": self.runner.quiet_s(aid), "last": last}
        for a8 in list(self.mirrored):
            delivered[a8] = [{"id": it.get("id"), "rt": it.get("rt")} for it in self.inbox.delivered(a8)]
        self.mine &= set(bodies) | {a for ids in self.done.values() for a in ids}
        return {"at": self.time.time(), "model": self.runner.resolve_model(), "bodies": bodies,
                "delivered": delivered, "done": self.done, "sent": self._sent()}

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

    def step(self):
        if not self._resumed:
            self._resume()
        snap = self.snapshot()
        self.got = got = self.wire.sync(snap)
        reqs = got.get("req") or {}
        # A request the bridge has deleted was claimed: forget its answer.
        self.done = {rid: ids for rid, ids in self.done.items() if rid in reqs}
        started = False
        for rid, r in reqs.items():
            if rid in self.done:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("prompt"), str) or \
                    self.time.time() - float(r.get("at") or 0) > REQ_TTL_S:
                # Malformed, or asked so long ago the bridge has given up on it: its text goes now.
                self._forget(f"req/{rid}")
                continue
            started = True
            env = {**os.environ, **{k: str(v) for k, v in (r.get("env") or {}).items() if ENV_PASS.match(k)}}
            h = self.runner.start(r["prompt"], r.get("model") or self.runner.resolve_model(),
                                  _merge([str(x) for x in r.get("deny") or []], self.always_deny),
                                  env if r.get("env") else None, self.cwd)
            ids = sorted(self.runner.claim(h, self.mine))
            self.done[rid] = ids
            self.mine |= set(ids)
            print(f"body host: started {ids or 'nothing'} for request {rid}", flush=True)
        for aid in (got.get("stop") or {}):
            if aid in self.mine:
                self.runner.stop(aid)
            self._forget(f"stop/{aid}")
        self._mirror(got.get("inbox") or {})
        if started:   # the bridge is waiting on these: say so now, not a beat later
            self.wire.put("host", self.snapshot())
        if any(not b["done"] for b in snap["bodies"].values()) or reqs:
            self.last_busy = self.time.time()

    def _forget(self, sub):
        try:
            self.wire.delete(sub)
        except Exception:
            pass

    def _mirror(self, docs):
        for a8, d in docs.items():
            if not isinstance(d, dict):
                continue
            m = d.get("meta") or {}
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
            if d.get("closed"):
                self.inbox.close(a8)
        for a8 in set(self.mirrored) - set(docs):   # the bridge removed it
            self.inbox.remove(a8)
            del self.mirrored[a8]

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
