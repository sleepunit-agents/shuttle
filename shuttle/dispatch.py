"""Where a batch goes: to the conversation's working body, onto the board as
held, or to a fresh body (t-1423 step 3).

Each bridge had three hand-written paths for this: retries, leftovers and new
messages. t-1405 was a retry that one path lost and another kept. Now the
retry and leftover queues are handled here, once, and new messages end in
`offer`; only what decides that a new message is for the agent at all (Discord's
routes, stop word and peer budget; Slack's DM gate) stays in the adapter.

Every source takes the same steps, in the same order:

    admit      may this batch reach a body? (ok / wait a while / drop)
    hand       a body holds the conversation: give it the batch (no slot needed)
    budget     may a NEW body be spent on it? (Discord: peer budget, guest cap)
    hold       slots, memory, cooldown: not yet
    spawn

As in core.py, `b` is the adapter and every call goes through it. The hooks
this file adds (see core.py for the rest):

    admit(state, cid, batch, source, ctx)  -> ("ok", conv) | ("wait", why) | ("drop", why)
    hand(state, conv, aid, batch)          -> bool, the bridge's hand_over
    on_handed(state, conv, batch, source)  what a hand-over spends (Discord: a peer turn)
    spend_blocked(state, conv, batch, ctx) -> why a new body may not be spent, or None
    hold(state, holds, conv, batch, reason, rec=None, since=None)
    spawn(state, conv, batch, attempt, source, ctx)   -> False if it refused (no body)
    conv_label(conv)                       "#general" | "pat-smith", for log lines

A conv is the adapter's own dict; the core reads only conv["id"]. ctx is
whatever the adapter needs this tick (Discord: channels and routes; Slack:
people), passed through untouched.
"""
import time

from shuttle import core

# t-1320: a batch given up on for an auth reason pages distinctly from any
# other give-up. The 2026-10-17 outage (every bg body died "Login expired" for
# ~1h, 15:14-16:07 CT) sat in the bridge journal, unpaged, until a human went
# looking. What counts as an auth failure is the runner's (b.runner.auth_failure,
# t-1439); this name is Claude's pattern, kept for callers that import it.
AUTH_FAILURE_RE = core.AUTH_FAILURE_RE


def _spawn(b, state, conv, batch, attempt, source, ctx, live, now=None):
    """(live, spawned). An adapter's spawn that returns False refused to start
    a body (it has kept or dropped the batch itself): that spends no live slot
    and starts no cooldown. Any other return (None) is a body started."""
    state.get("now_wait", {}).pop(conv["id"], None)
    if b.spawn(state, conv, batch, attempt, source, ctx) is False:
        return live, False
    state.setdefault("last_spawn", {})[conv["id"]] = now or time.time()
    return live + 1, True


def dedup(b, state, batch, label):
    """The batch without messages that already spawned a body (a re-delivery,
    or a re-poll after a crash before the cursor moved). A mixed batch keeps
    its new messages (PR#9)."""
    spawned = state.setdefault("spawned_msgs", {})
    dups = [b.msg_key(m) for m in batch if b.msg_key(m) in spawned]
    if not dups:
        return batch
    fresh = [m for m in batch if b.msg_key(m) not in spawned]
    print(f"dedup: {label} — {len(dups)} already-spawned message(s) {dups}; "
          f"dropping them, {len(fresh)} fresh message(s) remain", flush=True)
    return fresh


def _broken_promise(b, state, rec, msgs):
    """A replayed batch (its person was told not to resend, t-1358) that is
    dropped after all gets the plain failure notice instead of silence."""
    if core.was_replayed(b, state, msgs):
        b.notify_failure(rec["chan"], rec.get("why", "login expired"))


def run_retries(b, state, holds, live, retries, ctx):
    """Re-spawn batches whose body died without replying, including ones that
    waited for a slot (t-1405: they used to be dropped). The queue stays in
    state while this runs, so a mid-tick save keeps the ones not handled yet;
    each leaves the queue before its body is spawned. Returns live."""
    cfg = b._cfg()
    queue = state.setdefault("retry_queue", [])
    queue.extend(retries)
    core.expire_auth_parked(b, state)
    for rec in list(queue):
        label = b.rec_label(rec)
        if rec.get("attempt", 1) >= cfg.max_attempts:
            queue.remove(rec)
            why = rec.get("why", "unknown")
            print(f"giving up {label} after {rec['attempt']} attempts", flush=True)
            auth = bool(b.runner.auth_failure(why))
            park = auth and not core.was_replayed(b, state, rec.get("msgs") or [])   # t-1358
            if park:
                state.setdefault("auth_parked", []).append({**rec, "parked_at": time.time()})
            b.notify_failure(rec["chan"], why, parked=park)
            if auth:
                b.page(f"{cfg.agent}-{cfg.name} bridge giving up {label} after {rec['attempt']} "
                       f"attempts — auth failure ({why}). {b.runner.auth_hint}")
            continue
        msgs = rec.get("msgs") or []
        verdict, conv = b.admit(state, rec.get("chan"), msgs, "retry", ctx)
        if verdict == "drop":
            queue.remove(rec)
            print(f"retry {label} dropped: {conv}", flush=True)
            continue
        if verdict == "wait":
            if time.time() - rec.setdefault("queued_at", time.time()) >= cfg.wait_ttl:
                queue.remove(rec)
                print(f"retry {label} dropped: {conv} for {cfg.wait_ttl}s", flush=True)
                _broken_promise(b, state, rec, msgs)
            continue
        holder = b.holding_body(state, conv["id"])
        if holder:
            if b.hand(state, conv, holder, msgs):
                b.on_handed(state, conv, msgs, "retry")
                queue.remove(rec)   # the conversation's body takes it: never two bodies
            else:
                b.hold(state, holds, conv, msgs, "retry", rec=rec)
            continue
        blocked = b.spend_blocked(state, conv, msgs, ctx)
        if blocked:
            queue.remove(rec)
            print(f"retry {label} dropped: {blocked}", flush=True)
            _broken_promise(b, state, rec, msgs)
            continue
        if live >= cfg.max_live:
            print(f"retry {label} deferred: {live} live sessions", flush=True)
            b.hold(state, holds, conv, msgs, "retry", rec=rec)
            continue
        queue.remove(rec)
        live, _ = _spawn(b, state, conv, msgs, rec.get("attempt", 1) + 1, "retry", ctx, live)
    return live


def run_leftovers(b, state, holds, live, ctx):
    """Messages a body never handled (arrived after its last check, or it was
    killed before answering): to whoever holds the conversation now, else a
    fresh body, else they wait. They survive restarts. Returns live."""
    cfg = b._cfg()
    leftover = state.setdefault("leftover", {})
    since = state.setdefault("leftover_since", {})
    for cid in list(since):
        if cid not in leftover:
            since.pop(cid)

    def forget(cid):
        leftover.pop(cid, None)
        since.pop(cid, None)

    for cid in list(leftover):
        batch = leftover[cid]
        since.setdefault(cid, time.time())
        if not batch:
            forget(cid)
            continue
        verdict, conv = b.admit(state, cid, batch, "leftover", ctx)
        if verdict == "drop":
            print(f"leftover in {cid} dropped: {conv} ({len(batch)} msg(s))", flush=True)
            forget(cid)
            continue
        if verdict == "wait":
            if time.time() - since[cid] >= cfg.wait_ttl:
                print(f"leftover in {cid} dropped: {conv} for {cfg.wait_ttl}s "
                      f"({len(batch)} msg(s))", flush=True)
                forget(cid)
            continue
        holder = b.holding_body(state, cid)
        if holder and b.hand(state, conv, holder, batch):
            b.on_handed(state, conv, batch, "leftover")
            forget(cid)
            continue
        blocked = b.spend_blocked(state, conv, batch, ctx)
        if blocked:
            print(f"leftover in {b.conv_label(conv)} dropped: {blocked}", flush=True)
            forget(cid)
            continue
        reason = b.nowlog.hold_reason(time.time(), 0, 0, holder is not None, live, cfg.max_live,
                                      b.mem_available_mb(), cfg.min_avail_mb)
        if reason:
            b.hold(state, holds, conv, batch, reason)
            continue
        forget(cid)
        live, _ = _spawn(b, state, conv, batch, 1, "leftover", ctx, live)
    return live


def offer(b, state, holds, live, conv, batch, ctx):
    """New messages the adapter has admitted and deduplicated: to the body
    working the conversation (mid-turn, cooldown or not; not while older
    leftovers wait, so order holds), else held, else a fresh body.
    Returns (outcome, live), outcome "handed" | "held" | "spawned" | "refused"
    (the adapter's spawn returned False and dealt with the batch itself); on
    "held" the adapter keeps the batch for a later tick."""
    cfg = b._cfg()
    cid = conv["id"]
    holder = b.holding_body(state, cid)
    if holder and cid not in state.get("leftover", {}) and b.hand(state, conv, holder, batch):
        b.on_handed(state, conv, batch, "new")
        state.get("now_wait", {}).pop(cid, None)
        return "handed", live
    now = time.time()
    avail = b.mem_available_mb()
    reason = b.nowlog.hold_reason(now, state.setdefault("last_spawn", {}).get(cid, 0),
                                  cfg.cooldown_s, b.channel_busy(state, cid), live, cfg.max_live,
                                  avail, cfg.min_avail_mb)
    if reason:
        if reason == "slots_full":
            print(f"hold {b.conv_label(conv)}: {live} live sessions (cap {cfg.max_live})", flush=True)
        elif reason == "low_memory":
            print(f"hold {b.conv_label(conv)}: MemAvailable {avail}MB < {cfg.min_avail_mb}MB",
                  flush=True)
        if not any(h["chan"] == cid for h in holds):   # a deferred retry already holds it
            b.hold(state, holds, conv, batch, reason, since=now)
        return "held", live
    live, spawned = _spawn(b, state, conv, batch, 1, "new", ctx, live, now)
    return ("spawned" if spawned else "refused"), live
