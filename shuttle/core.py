"""The bridge core: everything the Discord and Slack bridges do the same way.

Lifted from the instance's two bridge scripts once each function's two
versions behaved the same (t-1423 steps 1-2). Nothing here reads a module
global, so two bridges never touch each other's state. Two kinds of function:

- Leaf functions take the bridge's Config first and touch only what it names
  (state file, sent log, send and alert commands, and its runner: the harness
  that runs the bodies, shuttle/runner.py).

- Orchestrating functions take the bridge itself, `b`: the adapter. They call
  every other lifted function, and every platform hook, through it
  (`b.kill_agent(aid)`, never `kill_agent(b._cfg(), aid)`). For now `b` is the
  bridge module, whose shims call back into this file; a test that patches
  `m.kill_agent` therefore intercepts the reaper's kills too, and never kills
  a real body. What `b` provides:

    _cfg()                    the Config
    runner                    the bridge's Runner (the Config's), for what has no shim
    inbox, nowlog             shuttle.inbox and shuttle.nowlog (or test fakes)
    msg_key(m)                a message's id on the transport (Discord id, Slack ts)
    msg_order(m)              sort key, oldest first
    rec_label(rec)            who/where a body is working, for log lines
    rec_venue(rec)            the board's venue label
    rec_who(rec)              the board's list of names in the conversation
    batch_who(rec, batch)     the names behind one handed batch
    item_text(m)              a message as the body reads it in its inbox
    CONV_NOUN                 "channels" | "DMs", for the registry page
    and the shims for each function below, plus react(chan, msg, add=).
"""
import contextlib
import fcntl
import json
import subprocess
import sys
import time

from shuttle.runner import ClaudeRunner
from shuttle.spawn import NEVER_STARTED


# ---------------------------------------------------------------- state

def load_state(cfg, defaults=()):
    """state.json, or {} if missing or unreadable. Each key in `defaults` is set
    to {} when absent: a partial file (t-1189: {"spawned_msgs": {}}) must not
    wedge every tick."""
    try:
        state = json.loads(cfg.state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    for key in defaults:
        state.setdefault(key, {})
    return state


def save_state(cfg, state):
    """Write state.json atomically (tmp file, then rename)."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(cfg.state_file)


@contextlib.contextmanager
def state_locked(cfg):
    """Hold LOCK_EX on state.json.lock for the full read-modify-write cycle.
    Blocking: a second holder waits. The kernel drops the lock if the process
    dies, so a crash between acquire and save_state is safe; the next holder
    starts from the last state saved. (Socket Mode fans each event out to every
    open connection: two Slack bridge processes alive at once in a restart race
    would otherwise clobber each other's dedup record, t-908.)"""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = open(cfg.state_lock, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def prune_spawned_msgs(state):
    """Drop dedup entries older than 2 hours: a safety net for records never
    cleaned up by a reap (bodies that vanished without one)."""
    cutoff = time.time() - 7200
    spawned = state.get("spawned_msgs", {})
    for k in [k for k, ts in spawned.items() if ts < cutoff]:
        del spawned[k]


def holding_body(state, cid):
    """The owned body currently holding this conversation (alive, turn not
    over), or None."""
    for aid, r in state.get("owned", {}).items():
        if (isinstance(r, dict) and r.get("chan") == cid and r.get("idle_since") == 0
                and r.get("busy", True)):
            return aid
    return None


def channel_busy(b, state, cid):
    """A bridge-owned body for this conversation is still working."""
    return b.holding_body(state, cid) is not None


# ---------------------------------------------------------------- bodies

# The bridge's runner (shuttle/runner.py) does all of this; these one-liners
# exist because the adapters' shims (b.agents, b.kill_agent, ...) call the
# shuttle by name, and tests swap those shims on the adapter module.

def agents(cfg, strict=False):
    """Every body the runner runs, [{id, status, ...}]. On error: [], or None
    with strict=True, for a caller that must not read a failed call as "every
    body is gone" (the reaper)."""
    return cfg.runner.list(strict)


def kill_agent(cfg, agent_id):
    cfg.runner.stop(agent_id)


def job_intent(cfg, aid):
    """The prompt a Claude body's job was started with ("" if unknown): the
    Claude runner's claim looks for its spawn token in it."""
    return cfg.runner.intent(aid)


def job_done(cfg, aid):
    """The body ended its own turn."""
    return cfg.runner.done(aid)


def job_parked(cfg, aid):
    """The body's only in-flight work is monitors (it ended its turn and is
    'watching'; nobody is ever going to act on that)."""
    return cfg.runner.parked(aid)


def job_quiet_s(cfg, aid):
    """Seconds since the body last showed activity (0 if it never has)."""
    return cfg.runner.quiet_s(aid)


def last_timeline(cfg, aid):
    """The body's last timeline entry, or None."""
    return cfg.runner.last_entry(aid)


def _sent_lines(cfg):
    try:
        return cfg.sent_log.read_text().splitlines()[-2000:]
    except OSError:
        return []


def _posted_by(job, aid):
    """A sent.log job field names this body: its aid8 (a Claude job dir's
    basename) or its whole id ($SHUTTLE_BODY, a Codex body's 16-hex key).
    "bridge" and "-" are never a body's."""
    return job not in ("-", "bridge") and job[:8] == aid[:8]


def last_post_ts(cfg, aid):
    """Epoch of this job's latest post through the bridge's send tool, or 0."""
    last = 0.0
    for line in _sent_lines(cfg):
        parts = line.split()
        if len(parts) >= 3 and _posted_by(parts[1], aid):
            try:
                last = max(last, float(parts[0]))
            except ValueError:
                pass
    return last


def session_replied(cfg, aid, chan_id, since_ts):
    """True if the send tool logged a post by this job (or, for a caller that
    did not carry a job id, to this conversation after the spawn). The bridge's
    own notices log as job "bridge", so they never count."""
    for line in _sent_lines(cfg):
        parts = line.split()
        if len(parts) < 3:
            continue
        if _posted_by(parts[1], aid):
            return True
        if parts[1] == "-" and parts[2] == str(chan_id):
            try:
                if float(parts[0]) >= since_ts - 5:
                    return True
            except ValueError:
                pass
    return False


def session_failed(b, aid):
    """True if the session's last timeline entry is an API error, as the
    runner reads one (Claude: a block whose text says "API Error")."""
    d = b._last_timeline(aid)
    if d is None:
        return False
    return b.runner.api_error(d)


def session_died_silent(b, aid, rec):
    """The body ended without posting and without finishing its turn cleanly:
    no timeline at all (killed before it drew breath, t-501), a turn cut off
    with no closing message, or the harness's own failure entry ("blocked",
    its text the same words as its detail: "Login expired", "API Error: …").
    A body that reached `done`, or closed its turn with a message under a
    status line of its own ("working: waiting on Quinn's merge"), chose not
    to post: a judgment, not a failure. Counting those as deaths re-woke a
    fresh body on an already-handled message ~15 times in 5 days (t-1074,
    t-1081, 2026-10-21)."""
    if b.session_replied(aid, rec.get("chan"), rec.get("spawned_at", 0)):
        return None
    d = b._last_timeline(aid)
    if d is None:
        return "no timeline"
    text, detail = (d.get("text") or "").strip(), (d.get("detail") or "").strip()
    if d.get("state") == "done" or (text and not harness_failure(d)):
        return None
    return f"ended {d.get('state')}: {(detail or text)[:80]}"


def death_reason(b, aid, rec):
    """Why a body that ended should be RETRIED, or None. A body that already
    replied is never retried, even if its last turn hit an API error: it may
    have answered many follow-ups (t-1409), and a retry would re-answer them
    all. What it read but never answered is passed on by _drop instead."""
    if b.session_replied(aid, rec.get("chan"), rec.get("spawned_at", 0)):
        return None
    return "API error" if b.session_failed(aid) else b.session_died_silent(aid, rec)


# ---------------------------------------------------------------- notices

HUNG = ("({tag}) I went silent for {min} min (hung, not thinking) and was stopped. "
        "Poke me again if you still need it.")
FAILED = "({tag}) my reply died twice before I could send it ({why}). Poke me again in a minute."
AUTH_PARKED = ("({tag}) my login lapsed before I could answer ({why}). I'll pick this up "
               "as soon as it's back; no need to resend.")


def _send(cfg, chan, text):
    subprocess.run([str(cfg.send), chan, text], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30, env=cfg.send_env)


def notify_hung(cfg, chan, quiet):
    _send(cfg, chan, HUNG.format(tag=cfg.tag, min=int(quiet // 60)))


def notify_failure(cfg, chan, why="upstream API error", parked=False):
    _send(cfg, chan, (AUTH_PARKED if parked else FAILED).format(tag=cfg.tag, why=why))


def page(cfg, msg):
    """Fire the instance's alert command (no Claude auth needed). Never raises."""
    try:
        subprocess.run([str(cfg.alert), msg], timeout=30, check=False)
    except Exception as e:
        print(f"alert failed: {e!r}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the board

def talking_items(b, state):
    """The board's talking items for the bridge-owned bodies."""
    items = []
    for aid, rec in state.get("owned", {}).items():
        if not isinstance(rec, dict) or not rec.get("chan"):
            continue
        items.append({"cid": rec.get("cid"), "chan": rec["chan"], "venue": b.rec_venue(rec),
                      "who": b.rec_who(rec), "body": aid, "body_since": rec.get("spawned_at"),
                      "active_s": rec.get("quiet_s"), "stalled": not rec.get("busy", True),
                      "handed": rec.get("handed", 0)})
    return items


def record_hold(b, state, holds, *, cid, chan, venue, who, n, since, reason):
    """Record a waiting conversation for the snapshot; emit `wait` when its
    reason changes. The caller computes the fields inside its own try."""
    holds.append({"cid": cid, "chan": chan, "venue": venue,
                  "who": who, "n": n, "since": since, "reason": reason})
    seen = state.setdefault("now_wait", {})
    if seen.get(chan) != reason:
        seen[chan] = reason
        b.nowlog.emit(b._cfg().name, "wait", cid=cid, chan=chan, venue=venue,
                      who=who, n=n, since=since, reason=reason)


def finished(b, aid, rec, why=None):
    """Emit `finished` for a dropped owned record. Never raises."""
    try:
        if not rec.get("chan"):
            return
        name = b._cfg().name
        if why:
            outcome = "gave_up" if rec.get("attempt", 1) >= b._cfg().max_attempts else "failed"
        elif b.session_replied(aid, rec["chan"], rec.get("spawned_at", 0)):
            outcome = "replied"
            b.nowlog.touch(name, rec["chan"], time.time())
        else:
            outcome = "reaped"
        b.nowlog.emit(name, "finished", cid=rec.get("cid"), chan=rec["chan"],
                      venue=b.rec_venue(rec), body=aid, outcome=outcome)
    except Exception as e:
        print(f"now board: finished: {e!r}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- hand-over and drop

def hand_over(b, state, chan, aid, batch):
    """Give a batch to the body holding the conversation (its inbox). True if
    handed; False if its inbox is gone or closed (the caller holds the batch)."""
    if not batch:
        return True
    items = [{"id": b.msg_key(m), "text": b.item_text(m), "msg": m} for m in batch]
    if not b.inbox.put(aid, items):
        return False
    rec = state["owned"][aid]
    now = time.time()
    spawned = state.setdefault("spawned_msgs", {})
    for m in batch:
        spawned[b.msg_key(m)] = now
    newest = b.msg_key(max(batch, key=b.msg_order))
    b.react(chan, newest, add=True)
    rec.setdefault("acks", []).append({"msg": newest, "at": now})
    rec["handed"] = rec.get("handed", 0) + len(batch)
    # The bridge's own copy: inbox files are writable by the body, so on drop
    # they only say WHICH ids are pending/read; content and author come from here.
    rec.setdefault("handed_msgs", []).extend(batch)
    print(f"handed {len(batch)} msg(s) {b.rec_label(rec)} to working session {aid[:8]}",
          flush=True)
    try:
        name = b._cfg().name
        b.nowlog.touch(name, chan, now)
        b.nowlog.emit(name, "delivered", cid=rec.get("cid"), chan=chan, venue=b.rec_venue(rec),
                      who=b.batch_who(rec, batch), n=len(batch), body=aid)
    except Exception as e:
        print(f"now board: delivered: {e!r}", file=sys.stderr, flush=True)
    # The bridge's copy of what it handed must be on disk before anything else
    # can go wrong this tick: a crash would otherwise lose which body holds it.
    b.save_state(state)
    return True


def release_spawned_msgs(b, state, rec):
    """Forget the dedup entries for the body's spawning batch once it is
    reaped, so the guard doesn't grow forever. By then the body has replied or
    been given up on; a late re-delivery is caught by the per-message check."""
    spawned = state.get("spawned_msgs", {})
    for m in rec.get("msgs", []):
        spawned.pop(b.msg_key(m), None)


def clear_ack(b, rec):
    """Take down every 👀 this body put up: the spawn's, and one per hand-over."""
    if not isinstance(rec, dict):
        return
    for msg in [rec.get("ack_msg")] + [a.get("msg") for a in rec.get("acks", [])]:
        if msg:
            b.react(rec.get("chan"), msg, add=False)


def drop(b, state, aid, rec, retry=None, ended=False):
    """Never raises: one body's odd inbox must not stop the tick."""
    try:
        b._drop_inner(state, aid, rec, retry, ended)
    except Exception as e:
        print(f"drop {aid[:8]}: {e!r}; forgetting it without hand-on", file=sys.stderr, flush=True)
        state.setdefault("inbox_gc", []).append(aid[:8])
        b._clear_ack(rec)
        state.get("owned", {}).pop(aid, None)


def drop_inner(b, state, aid, rec, retry, ended):
    """Forget an owned body: close its inbox and pass on what it never handled.
    - pending (arrived after its last check) always becomes a fresh batch in
      state["leftover"];
    - if it did NOT end its own turn (killed, vanished, parked, hung), items it
      read but never answered go too;
    - if it is being retried, everything it was handed rides in the retry batch
      instead, since the retry body starts from scratch.
    Content always comes from the bridge's own rec["handed_msgs"]; the inbox
    files (body-writable) only say which ids are pending or were read when.
    The inbox dir is only closed here and queued in state["inbox_gc"]; it is
    removed next tick, after this state (with the leftovers) has been saved."""
    try:
        delivered = b.inbox.delivered(aid)
        pending_ids = {it.get("id") for it in b.inbox.close(aid)}
    except Exception as e:   # can't tell what it handled: hand everything on (dupes beat loss)
        print(f"inbox close {aid[:8]}: {e!r}", file=sys.stderr, flush=True)
        delivered, pending_ids = [], {b.msg_key(m) for m in rec.get("handed_msgs", [])}
    ours = {b.msg_key(m): m for m in rec.get("handed_msgs", [])}
    pending = [ours[i] for i in pending_ids if i in ours]
    read_at = {it.get("id"): it.get("rt") for it in delivered}
    gc = state.setdefault("inbox_gc", [])
    if aid[:8] not in gc:
        gc.append(aid[:8])
    if retry is not None:
        retry["msgs"] = list(retry.get("msgs") or []) + \
            [m for i, m in ours.items() if i in read_at or i in pending_ids]
        retry.pop("handed_msgs", None)
    else:
        unhandled = []
        if not ended:
            posted = b.last_post_ts(aid)
            unhandled = [m for i, m in ours.items()
                         if i in read_at and not b.inbox.answered(posted, read_at[i])]
        batch = sorted(unhandled + pending, key=b.msg_order)
        if batch and rec.get("chan"):
            print(f"session {aid[:8]} left {len(batch)} msg(s) {b.rec_label(rec)} "
                  "unhandled; they go to a fresh body", flush=True)
            state.setdefault("leftover", {}).setdefault(rec["chan"], []).extend(batch)
    b._finished(aid, rec, rec.get("why"))
    b._release_spawned_msgs(state, rec)
    b._clear_ack(rec)
    state.get("owned", {}).pop(aid, None)


def inbox_sweep(b, state):
    """Start of tick: remove this bridge's inbox dirs whose body it doesn't own:
    the ones drop queued last tick (the state holding their leftovers has been
    saved since) and any stray. Nothing is ever recovered from an unowned dir:
    inbox files are body-writable, so only the bridge's own records (owned,
    handed_msgs) can produce a spawn. The other bridge's dirs, in the same base
    dir, are left to it (inbox.sweep, t-1411)."""
    owned8 = {a[:8] for a in state.get("owned", {})}
    for a8 in b.inbox.sweep(b._cfg().name, owned8, set(state.get("inbox_gc", []))):
        print(f"inbox {a8}: not owned by the bridge; removed", flush=True)
    state["inbox_gc"] = []


# ---------------------------------------------------------------- the reaper

# t-1358: a batch given up on for an auth reason waits in state["auth_parked"]
# for one more attempt once auth is back, unless it is older than this (the
# conversation has moved on) or a body has run in that conversation since
# (they re-poked). Its message ids then sit in state["auth_replayed"] so it is
# never parked twice: the owned record a spawn builds carries no flags.
AUTH_REPLAY_TTL = 6 * 3600

# Claude's auth pattern, kept under its old name for callers that import it.
# The reaper and the give-up ask the bridge's runner (b.runner.auth_failure),
# so each harness's own login failure parks (t-1439).
AUTH_FAILURE_RE = ClaudeRunner.AUTH_FAILURE_RE


def harness_failure(d):
    """A last-timeline entry the harness wrote for its own failure ("Login
    expired", "API Error: …"): blocked, its text the same words as its detail."""
    text, detail = (d.get("text") or "").strip(), (d.get("detail") or "").strip()
    return d.get("state") == "blocked" and text == detail


def was_replayed(b, state, batch):
    replayed = state.get("auth_replayed", {})
    return any(b.msg_key(m) in replayed for m in batch)


def expire_auth_parked(b, state):
    """Every tick: a parked batch past AUTH_REPLAY_TTL gets the plain failure
    notice (it was promised a pickup that never came) and leaves; replay marks
    are kept twice as long, then forgotten."""
    now = time.time()
    keep = []
    for rec in state.get("auth_parked", []):
        if now - rec.get("parked_at", 0) > AUTH_REPLAY_TTL:
            print(f"auth replay {b.rec_label(rec)} expired: auth never came back", flush=True)
            b.notify_failure(rec["chan"], rec.get("why", "login expired"))
        else:
            keep.append(rec)
    if "auth_parked" in state:
        state["auth_parked"] = keep
    marks = state.get("auth_replayed", {})
    for k in [k for k, at in marks.items() if now - at > 2 * AUTH_REPLAY_TTL]:
        marks.pop(k)


def replay_auth_parked(b, state, aid, rec):
    """A body just ended cleanly. If it started after a batch was parked and
    its own last entry is no harness failure, auth works: re-queue that batch
    for one last attempt. (A body that replied before the login lapsed also
    ends "cleanly"; its start time rules it out.)"""
    parked = state.get("auth_parked")
    if not parked:
        return
    d = b._last_timeline(aid)
    if d is None or harness_failure(d) or b.runner.auth_failure(d.get("text") or ""):
        return
    started, last_spawn = rec.get("spawned_at", 0), state.get("last_spawn", {})
    queue, keep = state.setdefault("retry_queue", []), []
    marks = state.setdefault("auth_replayed", {})
    for p in parked:
        at, label = p.get("parked_at", 0), b.rec_label(p)
        if started <= at:
            keep.append(p)
        elif last_spawn.get(p.get("chan"), 0) > at:
            print(f"auth replay {label} dropped: a body has run there since", flush=True)
        else:
            print(f"auth is back: replaying {label}", flush=True)
            r = {k: v for k, v in p.items() if k not in ("parked_at", "checked", "why")}
            r["attempt"] = b._cfg().max_attempts - 1
            for m in r.get("msgs") or []:
                marks[b.msg_key(m)] = time.time()
            queue.append(r)
    state["auth_parked"] = keep


def _check_death(b, state, aid, rec, retries, verb):
    """On a body's first end: if it should be retried, note that on it and queue a copy."""
    if not rec.get("chan") or rec.get("checked"):
        return None
    rec["checked"] = True
    why = b.death_reason(aid, rec)
    if not why:
        replay_auth_parked(b, state, aid, rec)
        return None
    rec["why"] = why
    print(f"session {aid[:8]} {b.rec_label(rec)} {verb} without replying "
          f"— {why} (attempt {rec.get('attempt', 1)})", flush=True)
    retry = dict(rec)
    retries.append(retry)
    return retry


def settle_unconfirmed(b, state, retries):
    """Launches held while the registry couldn't be read (spawn.unclaimed), now
    that it can: a body that did start is adopted as owned, one that didn't is
    retried. Either way the batch is no longer left unanswered."""
    held = state.get("unconfirmed") or []
    owned = state.setdefault("owned", {})
    for u in held:
        rec = {k: v for k, v in u.items() if k not in ("handle", "since")}
        ids = b.runner.find(u.get("handle") or {}, set(owned))
        if len(ids) == 1:
            aid = next(iter(ids))
            owned[aid] = {**rec, "idle_since": 0, "spawned_at": u.get("since", 0)}
            print(f"adopted {aid[:8]} {b.rec_label(rec)}: it started while the registry was unreadable", flush=True)
        else:
            print(f"retrying {b.rec_label(rec)}: {NEVER_STARTED}", flush=True)
            retries.append({**rec, "why": NEVER_STARTED})
    state["unconfirmed"] = []
    state.pop("unconfirmed_paged", None)


def reap_and_count(b, state):
    """Reap bridge-owned bodies whose turn is over, and the two kinds that
    can't end themselves (parked on monitors, hung). Returns (live, retries):
    retries = records whose body died on an API error or silently and still
    have attempts left (the caller re-spawns them). Messages a dropped body
    never handled land in state["leftover"] (the caller spawns for them)."""
    cfg = b._cfg()
    owned = state.setdefault("owned", {})     # agent_id -> record dict
    for aid, v in list(owned.items()):        # upgrade legacy scalar records
        if not isinstance(v, dict):
            owned[aid] = {"idle_since": v}
    live = 0
    retries = []
    now = time.time()
    present = b.agents(strict=True)
    if present is None:
        # Can't see the registry: count every owned body live, drop nothing.
        # Dead bodies keep their conversations while this lasts, so say so.
        n = state["registry_failures"] = state.get("registry_failures", 0) + 1
        if n == cfg.alert_after:
            b.page(f"{cfg.agent}-{cfg.name}: "
                   + b.runner.registry_hint.format(n=n, conv=b.CONV_NOUN))
        return len(owned), retries
    state["registry_failures"] = 0
    settle_unconfirmed(b, state, retries)
    for a in present:
        aid = a.get("id")
        rec = owned.get(aid)
        if rec is None:
            continue
        status = a.get("status")
        if b.runner.working(status):
            rec["idle_since"] = 0
            quiet = b.job_quiet_s(aid)
            parked = b.job_parked(aid)
            if parked and quiet >= cfg.parked_kill_s:
                b.kill_agent(aid)
                print(f"reaped parked session {aid[:8]} {b.rec_label(rec)} "
                      f"(monitor-only, quiet {int(quiet)}s)", flush=True)
                b._drop(state, aid, rec)
                continue
            if quiet >= cfg.hang_kill_s:
                b.kill_agent(aid)
                print(f"killed hung session {aid[:8]} {b.rec_label(rec)} "
                      f"(silent {int(quiet)}s)", flush=True)
                if rec.get("chan"):
                    b.notify_hung(rec["chan"], quiet)
                b._drop(state, aid, rec)
                continue
            rec["quiet_s"] = quiet
            if rec.get("busy", True) and parked:
                print(f"session {aid[:8]} {b.rec_label(rec)} parked on monitors; "
                      "conversation free", flush=True)
            rec["busy"] = not parked
            live += 1
        else:
            first = rec.get("idle_since") or now
            rec["idle_since"] = first
            # A body whose job says done ended its own turn: reap it now, so 👀
            # and the board card clear on the same tick. Otherwise (status not
            # yet known, or flickering) give it reap_grace.
            ended = b.job_done(aid)
            if ended or now - first >= cfg.reap_grace:
                rec["busy"] = False
                retry = _check_death(b, state, aid, rec, retries, "died")
                b.kill_agent(aid)
                print(f"reaped {status} session {aid[:8]}", flush=True)
                b._drop(state, aid, rec, retry, ended=ended)
            else:
                live += 1
    # ids that vanished from the registry on their own: `claude stop`/kill by
    # something that is not us (the chain-check reaper took c875052c this way,
    # t-501). The job dir outlives the registry entry, so the same silent-death
    # test applies before the record is forgotten.
    present_ids = {a.get("id") for a in present}
    for aid in list(owned):
        if aid not in present_ids:
            rec = owned[aid]
            retry = _check_death(b, state, aid, rec, retries, "vanished")
            b._drop(state, aid, rec, retry, ended=b.job_done(aid))
    return live, retries
