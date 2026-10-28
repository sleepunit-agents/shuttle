"""Starting a body (t-1423 step 4): the launch, the claim, the bookkeeping.

The adapter writes the prompt and says what is particular to this body (a
`plan`); everything else is the same for every bridge and lives here: which
model, the tools no bridge body may use, the 👀, the dedup record, the new
body's inbox and owned record, the board events, and the save.

How a body is launched and found again is the bridge's runner's
(`b.runner`, shuttle/runner.py): `start` where the launch goes, `claim`
where the wait for the new body goes. The runner launches through the
adapter (`b.subprocess`, `b.time`, `b.secrets`): the bridges' tests swap
those on the bridge module, and must never start a real body.

A plan is a dict:

    chan         the conversation id
    label        "#general" | "pat-smith": log lines and the board's conversation
    venue        the board's venue label
    inbox_label  how the body's inbox names where it is
    who          the board's list of names
    members      the people in it, as people.json slugs: the board's who-is-where
    prompt       the brief; line 1 stays its opening (the Claude runner puts its token on line 2)
    deny         tools denied on top of cfg.always_deny (Discord's guest venues)
    env          the launcher's env, or None to inherit (Slack's field scope)
    rec          extra fields for the owned record
    on_claimed   callable(aid) once exactly one body is claimed, or None
"""
import sys

DEFAULT_MODEL_WORDS = "the runner's default model"   # the spawn line when no model is set


def resolve_model(b):
    """The model the next body starts on: the runner's choice (for Claude, the
    instance's model resolver, failing open to the preferred model)."""
    return b.runner.resolve_model()


NEVER_STARTED = "its body never started"


def unclaimed(b, state, plan, batch, attempt, handle):
    """A launch with no body to claim. If the registry answers, it never
    started: retry it like a body that died without replying. If the registry
    can't be read, it may be running unseen: hold it, page once, and let the
    reaper settle it when the registry answers (core.settle_unconfirmed)."""
    rec = {"chan": plan["chan"], "msgs": batch, "attempt": attempt,
           "members": plan.get("members", []), **plan.get("rec", {})}
    label = b.rec_label(rec)
    starting = b.runner.undecided(handle)
    if b.agents(strict=True) is not None and not starting:
        print(f"no body for {label}: {NEVER_STARTED}; queued to retry", flush=True)
        state.setdefault("retry_queue", []).append({**rec, "why": NEVER_STARTED})
        return
    keep = {k: v for k, v in handle.items() if k != "before"}   # a set: not JSON, and not needed later
    state.setdefault("unconfirmed", []).append({**rec, "handle": keep, "since": b.time.time()})
    if starting:   # its body host is still starting it: settled when it's done, never retried meanwhile
        print(f"no body yet for {label}: still starting in the agent's house; held unconfirmed", flush=True)
        return
    print(f"no body seen for {label}: the registry can't be read; held unconfirmed", flush=True)
    if not state.get("unconfirmed_paged"):
        state["unconfirmed_paged"] = True
        cfg = b._cfg()
        b.page(f"{cfg.agent}-{cfg.name}: a reply couldn't be confirmed ({label}): the bridge can't read "
               "its harness's registry, so messages may be going unanswered. Check the bridge's house "
               "(pids, `claude agents`).")


def spawn_body(b, state, plan, batch, attempt=1):
    """Launch a body for `batch` and claim it. Saves state before returning."""
    cfg = b._cfg()
    runner = b.runner
    chan = plan["chan"]
    model = b.resolve_model()
    # The instance's always-deny tools (e.g. a token that answers as the owner,
    # t-1119) plus the plan's own. The prompt goes to the runner, never argv,
    # so message content never shows in /proc/<pid>/cmdline (t-307).
    deny = [*cfg.always_deny, *plan.get("deny", [])]
    handle = runner.start(plan["prompt"], model, deny, plan.get("env"), cfg.workdir)
    print(f"spawned session for {plan['label']} ({len(batch)} new msg, attempt {attempt}) "
          f"on {model or DEFAULT_MODEL_WORDS}", flush=True)
    ack_msg = b.msg_key(max(batch, key=b.msg_order)) if batch else None
    b.react(chan, ack_msg, add=True)
    cid = None
    picked_t = oldest_t = b.time.time()
    try:
        oldest_t = b.batch_since(batch) or picked_t
        cid = b.nowlog.conversation(cfg.name, chan, plan["label"],
                                    b.msg_key(min(batch, key=b.msg_order)), oldest_t)
    except Exception as e:
        print(f"now board: conversation: {e!r}", file=sys.stderr, flush=True)
    # The dedup guard, recorded before the claim wait below: a crash there (an
    # API error, an SSL error mid-tick) must still leave it, so the next tick
    # doesn't spawn a second body for the same messages (t-323, t-908).
    spawned_msgs = state.setdefault("spawned_msgs", {})
    now_ts = b.time.time()
    for m in batch:
        spawned_msgs[b.msg_key(m)] = now_ts
    # Claim the new body once it registers, never one already owned.
    owned_ids = runner.claim(handle, set(state.get("owned", {})))
    if not owned_ids:
        # The messages are already marked spawned (the dedup guard above), so a
        # body that never shows would leave them unanswered for good, with the
        # 👀 already on them (2026-10-26: nine DMs, the house out of pids).
        unclaimed(b, state, plan, batch, attempt, handle)
    for nid in owned_ids:
        try:
            b.inbox.open_inbox(nid, plan["inbox_label"], chan, cfg.name)
        except Exception as e:   # no inbox = follow-ups wait, as before
            print(f"inbox open {nid[:8]}: {e!r}", file=sys.stderr, flush=True)
        state.setdefault("owned", {})[nid] = {
            "idle_since": 0, "chan": chan, "msgs": batch, "attempt": attempt,
            "spawned_at": now_ts, "ack_msg": ack_msg, "cid": cid,
            "members": plan.get("members", []), **plan.get("rec", {}),
        }
    # A claim is a match the runner found, not an id this launch returned (for
    # Claude, the spawn token in free-text job intent): act on it only when
    # exactly one body matched.
    if plan.get("on_claimed") and len(owned_ids) == 1:
        plan["on_claimed"](next(iter(owned_ids)))
    b.nowlog.emit(cfg.name, "picked", cid=cid, chan=chan, venue=plan["venue"], who=plan["who"],
                  members=plan.get("members", []), wait_s=max(0.0, picked_t - oldest_t),
                  body=next(iter(owned_ids), None))
    # Flush the dedup guard and the owned record now: the tick's final save is
    # the norm, this one covers a crash between here and there.
    b.save_state(state)
