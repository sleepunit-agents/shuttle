#!/usr/bin/env python3
"""<agent>-slack: Socket Mode bridge between a Slack workspace and an agent instance.

DMs and group DMs only. Slack pushes `message.im` / `message.mpim` events over
a websocket (Socket Mode: we dial OUT, nothing inbound, no public URL). Each
inbound human DM is identified by its Slack user id — never the display name —
and checked three ways against the workspace before the registry is even
consulted (is_stranger / team_id / is_restricted). Registered people spawn a
`claude --bg` body (interactive billing pool — NEVER claude -p) fenced by their
tier; everyone else is left on read. The bridge never parses model output:
it watches, spawns, reacts 👀, and keeps a local transcript so a body has
context without hitting conversations.history (rate-limited to 1 req/min for
non-Marketplace apps created after 2025-05-29 — polling is not an option).

Session hygiene (reap bodies whose turn is over, cap live bodies, memory
guard, retry once on upstream API error, kill parked or hung bodies) is the
same posture as the Discord adapter — see the commentary there for the
incidents that shaped it. A body holds its DM until its turn ends: messages
that arrive while it works go to its inbox (shuttle/inbox.py) and the
instance's inbox hook hands them over between tool calls (t-1409, ported to
Slack in t-1411); whatever it never saw goes to a fresh body.

Everything about the agent (who it is, its paths, ids, prose and extras) comes
from the instance file named by $SHUTTLE_INSTANCE (shuttle/instance.py, t-1436):
routes.json and people.json sit beside it, the briefs are its `slack*.md`, the
workspace credentials are the env-file secret `slack.secrets_env` (INSTANCE.read_secret),
and its extension module's dm_extra(bridge, rec, tier) adds to a tier-2 DM.

Needs websocket-client (imported when the socket opens, so tests can load this
module without it).
"""

import fcntl
import json
import os
import queue
import re
import secrets  # the spawn token: the shuttle reaches it as b.secrets (t-1423)
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the shuttle checkout
from shuttle import instance as shuttle_instance  # noqa: E402  the instance file (t-1436)
from shuttle import nowlog  # noqa: E402  the "now board" (t-1403)
from shuttle import inbox  # noqa: E402  a working body hears follow-ups (t-1409)
from shuttle import relay  # noqa: E402  another body asks this bridge to say something (t-1570)
from shuttle import people as person_cards  # noqa: E402  loom person cards (t-1381)
from shuttle import core as shuttle_core  # noqa: E402  the shared bridge core (t-1423)
from shuttle import dispatch as shuttle_dispatch  # noqa: E402
from shuttle import spawn as shuttle_spawn  # noqa: E402
from shuttle import selvedge as shuttle_selvedge  # noqa: E402
from shuttle import runner as shuttle_runner  # noqa: E402  the harness bodies run on (t-1438)
from shuttle.config import Config as ShuttleConfig  # noqa: E402

INSTANCE = shuttle_instance.load()   # $SHUTTLE_INSTANCE; exits if unset or unreadable
SLUG = INSTANCE.agent_slug
SLACK = INSTANCE.section("slack")
# Board keys stay "<slug>|slack|<chan>" (t-1436 review focus 4); the
# conversation-id map lives in this bridge's archive.
nowlog.configure(now_dir=INSTANCE.now_dir, ids_dir=INSTANCE.state_dir("slack") / "archive",
                 agent=SLUG)
inbox.configure(inbox_dir=INSTANCE.inbox_dir)

# The workspace credentials: an env file (SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
# SLACK_TEAM_ID, SLACK_BOT_USER_ID) in the instance's secrets dir, read when
# first needed (startup_check() reads it at start), so importing needs no secrets.
SECRETS = SLACK.get("secrets_env") or "slack.env"   # the secret's name (INSTANCE.read_secret)
PEOPLE_FILE = INSTANCE.dir / "people.json"
ROUTES_FILE = INSTANCE.dir / "routes.json"
STATE_DIR = INSTANCE.state_dir("slack")
STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK_FILE = STATE_DIR / "state.json.lock"
# Durable DM archive (t-1371): every ingested DM message, both directions,
# one JSON line each, a file per month. Nothing else keeps message text past
# ~30 days (transcripts age out, history is a 40-line buffer). Lives under
# STATE_DIR, not the instance's workdir, because that may be pushed somewhere
# and this is colleagues' words.
ARCHIVE_DIR = STATE_DIR / "archive"
API = "https://slack.com/api"
CLAUDE = INSTANCE.commands.get("claude") or Path.home() / ".local/bin/claude"
SLACK_SEND = (INSTANCE.commands.get("send") or {}).get("slack")
LOOM = INSTANCE.commands.get("loom")   # person cards (`loom person show`)


def _instance_routes():
    """routes.json beside the instance file, read at start ({} if missing)."""
    try:
        return json.loads(ROUTES_FILE.read_text())
    except (OSError, ValueError) as e:
        print(f"routes.json unreadable at start ({e!r})", flush=True)
        return {}


_IDS = _instance_routes()


def _routes_value(dotted):
    """routes.json's value at a dotted key ("notify.general"), or None."""
    v = _IDS
    for part in (dotted or "").split("."):
        if not part or not isinstance(v, dict):
            return None
        v = v.get(part)
    return v


# Where a tier-2 ask gets flagged for the owner (the briefs' $send_discord and
# $general): shuttle.json `slack.flag_to` = {"via": <a commands.send key>,
# "chan_from_routes": <dotted routes.json key>} (or "chan": <a channel id>).
# Read at start; startup_check() refuses a brief that uses them when they
# don't resolve.
FLAG_TO = SLACK.get("flag_to") or {}
FLAG_SEND = (INSTANCE.commands.get("send") or {}).get(FLAG_TO.get("via"))
FLAG_CHAN = FLAG_TO.get("chan") or _routes_value(FLAG_TO.get("chan_from_routes"))
WORKDIR = INSTANCE.workdir   # the bodies' cwd

PREFERRED_MODEL = INSTANCE.preferred_model
MODEL_RESOLVER = INSTANCE.commands.get("model")   # rate-limit-aware resolver
TICK_SECONDS = 5
CHANNEL_COOLDOWN = 20
MAX_ATTEMPTS = 2
ALERT = INSTANCE.commands.get("alert")
JOBS_DIR = Path.home() / ".claude/jobs"
CONTEXT_MESSAGES = 12
HISTORY_KEEP = 40              # local transcript lines kept per DM
USER_CACHE_S = 3600            # users.info verdicts are cached this long

MAX_LIVE = 5                   # 2 -> 4 with the inbox (t-1411), 5 to match Discord (t-1423)
MIN_AVAIL_MB = 1500
REAP_GRACE = 30                # idle this long before reaping, unless its job says done (then now)
PARKED_KILL_S = 60             # parked (monitor-only) this long -> reap
HANG_KILL_S = 1800             # silent this long while working -> hung, kill (the only other kill)
TICK_ERROR_ALERT_AFTER = 60    # `claude agents` failing this many 5s ticks running -> page
LEFTOVER_TTL_S = 3600          # a leftover/retry kept through users.info failures is dropped after this
# The bridge's own posts (hung / gave-up notes) log as job "bridge" in sent.log,
# so they never count as a body's reply (session_replied matches the body's
# job id, or "-" for a post with no job).
BRIDGE_SEND_ENV = {**os.environ, "CLAUDE_JOB_DIR": "bridge", "SHUTTLE_BODY": "bridge"}


def load_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# The credentials, by the names this module has always used for them. They are
# read from SECRETS on first use, not at import; a test sets one by assigning
# the module global (m.TEAM_ID = "T1"), which wins over the file.
_SECRET_KEYS = {"BOT_TOKEN": "SLACK_BOT_TOKEN", "APP_TOKEN": "SLACK_APP_TOKEN",
                "TEAM_ID": "SLACK_TEAM_ID", "BOT_USER_ID": "SLACK_BOT_USER_ID"}
_ENV = {}


def slack_env():
    """The secrets env file, read once."""
    if not _ENV:
        _ENV.update(load_env(INSTANCE.read_secret(SECRETS)))
    return _ENV


def secret(name):
    """BOT_TOKEN, APP_TOKEN, TEAM_ID or BOT_USER_ID: a module global a test
    set, else the secrets file's value."""
    v = globals().get(name)
    return v if v is not None else slack_env()[_SECRET_KEYS[name]]


def __getattr__(name):
    """m.TEAM_ID etc. from outside, read lazily. With no secrets file (or no
    such key) the name is simply absent, so hasattr() is safe; inside the
    bridge, secret() stays a loud error."""
    if name in _SECRET_KEYS:
        try:
            return secret(name)
        except (OSError, KeyError) as e:
            raise AttributeError(f"{name}: {e!r}") from None
    raise AttributeError(name)


# ---------------------------------------------------------------- Slack API

def api(method, token=None, **params):
    token = token or secret("BOT_TOKEN")
    req = urllib.request.Request(          # always POST — Slack rejects a bare GET
        f"{API}/{method}", data=urllib.parse.urlencode(params).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "User-Agent": f"{SLUG}-slack/1",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(f"{method}: {d.get('error')}")
    return d


def react(channel, ts, add=True):
    """Add or remove the bot's 👀 on a message. Idempotent; never raises."""
    if not (channel and ts):
        return
    try:
        api("reactions.add" if add else "reactions.remove", channel=channel, timestamp=ts, name="eyes")
    except Exception as e:
        if "already_reacted" not in str(e) and "no_reaction" not in str(e):
            print(f"react {'add' if add else 'remove'} {channel}/{ts}: {e!r}", flush=True)


# ---------------------------------------------------------------- people

def load_people():
    """slack user id -> (slug, record) for this workspace."""
    try:
        d = json.loads(PEOPLE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"people.json unreadable: {e!r}", flush=True)
        return {}
    return selvedge_index(d, "slack", secret("TEAM_ID"), sections=("people",))   # agents never pass the gate


def user_verdict(state, uid):
    """Three-way workspace check via users.info. Returns (ok, reason, profile).
    ok=False means: external (Slack Connect), foreign team, guest, bot, or
    deleted — never spawn for these regardless of the registry."""
    cache = state.setdefault("users", {})
    c = cache.get(uid)
    if c and time.time() - c.get("at", 0) < USER_CACHE_S:
        return c["ok"], c["reason"], c["profile"]
    try:
        u = api("users.info", user=uid)["user"]
    except Exception as e:
        print(f"users.info {uid} failed: {e!r}", flush=True)
        return False, f"users.info failed: {e}", {}
    reason = None
    if u.get("is_stranger"):
        reason = "Slack Connect stranger"
    elif u.get("team_id") != secret("TEAM_ID"):
        reason = f"foreign team {u.get('team_id')}"
    elif u.get("is_restricted") or u.get("is_ultra_restricted"):
        reason = "guest account"
    elif u.get("is_bot") or u.get("deleted"):
        reason = "bot or deleted"
    profile = {
        "name": u.get("name"),
        "real_name": u.get("real_name") or u.get("profile", {}).get("real_name"),
        "title": u.get("profile", {}).get("title"),
        "tz": u.get("tz"),
    }
    cache[uid] = {"ok": reason is None, "reason": reason, "profile": profile,
                  "at": time.time()}
    return reason is None, reason, profile


# ---------------------------------------------------------------- state

def load_state():
    return shuttle_core.load_state(_cfg())


# The shared core (the shuttle library, t-1423). Bridge code calls these module-level
# names, never shuttle_core directly, so tests that patch them still intercept
# (the instance's shim test). The Config is built per call from this module's
# globals, which tests re-point at temp dirs; so is its runner (shuttle/runner.py:
# CLAUDE, JOBS_DIR, MODEL_RESOLVER, PREFERRED_MODEL, subprocess, time, secrets).
def _cfg():
    return ShuttleConfig(name="slack", state_dir=STATE_DIR,
                         state_file=STATE_FILE, state_lock=STATE_LOCK_FILE, sent_log=SENT_LOG,
                         send=SLACK_SEND, send_env=BRIDGE_SEND_ENV, alert=ALERT,
                         max_attempts=MAX_ATTEMPTS, reap_grace=REAP_GRACE,
                         parked_kill_s=PARKED_KILL_S, hang_kill_s=HANG_KILL_S,
                         alert_after=TICK_ERROR_ALERT_AFTER, max_live=MAX_LIVE,
                         min_avail_mb=MIN_AVAIL_MB, cooldown_s=CHANNEL_COOLDOWN,
                         wait_ttl=LEFTOVER_TTL_S, workdir=WORKDIR, tag=INSTANCE.agent_tag,
                         agent=SLUG, always_deny=INSTANCE.always_deny,
                         runner=shuttle_runner.build_runner(INSTANCE, _B))


class _Bridge:
    """This module, as the shuttle core's adapter. Every lookup goes to the
    module's globals at call time, so a test that patches `m.kill_agent` also
    patches the core's calls to it. `runner` is the Config's runner, built
    afresh each time like the Config."""
    def __getattr__(self, attr):
        if attr == "runner":
            return _cfg().runner
        try:
            return globals()[attr]
        except KeyError:
            raise AttributeError(attr) from None


_B = _Bridge()


# The adapter hooks the core calls (see shuttle/core.py).
CONV_NOUN = "DMs"


def msg_key(m):
    return m.get("ts")


def msg_order(m):
    return _ts_key(m)


def rec_label(rec):
    return f"in the {'group DM' if rec.get('venue') == 'slack-group' else 'DM'} with {rec.get('who')}"


def rec_venue(rec):
    return rec.get("venue") or "slack-dm"


def rec_who(rec):
    return rec.get("who_names") or [display_name(rec.get("who"))]


def batch_who(rec, batch):
    return rec_who(rec)


def item_text(m):
    return fmt_line(m)


def conv_label(conv):
    return conv["slug"]


def admit(state, cid, batch, source, ctx):
    # Only a 1:1 DM (its id starts with D) takes the 1:1 path, which carries the
    # sender's own scopes and authority; anything else is gated as a room, even
    # if the "groups" flag were lost (a restore, a hand edit).
    if state.get("groups", {}).get(cid) or not str(cid).startswith("D"):
        return group_gate(state, ctx["people"], cid)
    verdict, p = spawn_gate(state, ctx["people"], batch[-1].get("uid") if batch else None)
    if verdict != "ok":
        return verdict, p
    return "ok", {"id": cid, "slug": p[0], "person": p[1]}


def hand(state, conv, aid, batch):
    return hand_over(state, conv["id"], aid, batch)


def on_handed(state, conv, batch, source):
    pass


def spend_blocked(state, conv, batch, ctx):
    return None


def hold(state, holds, conv, batch, reason, rec=None, since=None):
    if conv.get("group"):
        _hold(state, holds, conv["id"], conv["slug"], batch, reason, (rec or {}).get("cid"),
              names=conv["names"], venue="slack-group")
        return
    _hold(state, holds, conv["id"], conv["slug"], batch, reason, (rec or {}).get("cid"))


def spawn(state, conv, batch, attempt, source, ctx):
    context = state.get("history", {}).get(conv["id"], [])[-CONTEXT_MESSAGES:]
    if conv.get("group"):
        spawn_group_session(state, conv, batch, context, attempt=attempt)
        return
    return spawn_session(state, conv["id"], conv["slug"], conv["person"], conv["person"]["tier"],
                         batch, context, ctx["people"], attempt=attempt)


def run_retries(state, holds, live, retries, ctx):
    return shuttle_dispatch.run_retries(_B, state, holds, live, retries, ctx)


def run_leftovers(state, holds, live, ctx):
    return shuttle_dispatch.run_leftovers(_B, state, holds, live, ctx)


def offer(state, holds, live, conv, batch, ctx):
    return shuttle_dispatch.offer(_B, state, holds, live, conv, batch, ctx)


def dedup(state, batch, label):
    return shuttle_dispatch.dedup(_B, state, batch, label)


def spawn_body(state, plan, batch, attempt=1):
    shuttle_spawn.spawn_body(_B, state, plan, batch, attempt)


def batch_since(batch):
    return oldest_ts(batch)


def save_state(state):
    shuttle_core.save_state(_cfg(), state)


def state_locked():
    return shuttle_core.state_locked(_cfg())


# ---------------------------------------------------------------- sessions (same posture as the Discord adapter)

def agents(strict=False):
    return shuttle_core.agents(_cfg(), strict)


def kill_agent(agent_id):
    shuttle_core.kill_agent(_cfg(), agent_id)


SENT_LOG = STATE_DIR / "sent.log"   # written by the send command: "<epoch> <job> <chan> <bytes>"


def _job_intent(aid):
    return shuttle_core.job_intent(_cfg(), aid)


def _last_timeline(aid):
    return shuttle_core.last_timeline(_cfg(), aid)


def session_failed(aid):
    return shuttle_core.session_failed(_B, aid)


def session_replied(aid, chan_id, since_ts):
    return shuttle_core.session_replied(_cfg(), aid, chan_id, since_ts)


def session_died_silent(aid, rec):
    return shuttle_core.session_died_silent(_B, aid, rec)


def last_post_ts(aid):
    return shuttle_core.last_post_ts(_cfg(), aid)


def death_reason(aid, rec):
    return shuttle_core.death_reason(_B, aid, rec)


def job_done(aid):
    """The session's job says its turn is over (state "done"): it ended itself."""
    return shuttle_core.job_done(_cfg(), aid)


def job_quiet_s(aid):
    return shuttle_core.job_quiet_s(_cfg(), aid)


def job_parked(aid):
    return shuttle_core.job_parked(_cfg(), aid)


def prune_spawned_msgs(state):
    shuttle_core.prune_spawned_msgs(state)


def _release_spawned_msgs(state, rec):
    shuttle_core.release_spawned_msgs(_B, state, rec)


# --- Now board (t-1403): ids, names, counts, times, states, reasons — never message text.
_NAMES = {"mtime": None, "names": {}}


def display_name(slug):
    """people.json display name for a slug (the slug itself if unknown)."""
    try:
        mt = PEOPLE_FILE.stat().st_mtime
        if mt != _NAMES["mtime"]:
            d = json.loads(PEOPLE_FILE.read_text()).get("people", {})
            _NAMES.update(mtime=mt, names={k: v.get("name") for k, v in d.items()})
        return _NAMES["names"].get(slug) or slug
    except Exception:
        return slug


def oldest_ts(msgs):
    """Epoch of the oldest message in a batch (Slack ts strings), else None."""
    try:
        return min(float(m["ts"]) for m in msgs)
    except (TypeError, ValueError, KeyError):
        return None


def talking_items(state):
    return shuttle_core.talking_items(_B, state)


def _hold(state, holds, chan, slug, batch, reason, cid=None, names=None, venue="slack-dm"):
    """Record a waiting DM (or group DM) for the snapshot; emit `wait` on a new
    reason. Every field is computed in here so a bad record cannot raise out of tick()."""
    try:
        shuttle_core.record_hold(_B, state, holds, cid=cid, chan=chan, venue=venue,
                                 who=names or [display_name(slug)], n=len(batch),
                                 since=oldest_ts(batch), reason=reason)
    except Exception as e:
        print(f"now board: hold: {e!r}", file=sys.stderr, flush=True)


def _finished(aid, rec, why=None):
    shuttle_core.finished(_B, aid, rec, why)


# Socket Mode health, updated from the ws thread's callbacks (plain dict writes;
# the tick only reads). "down" until the first open.
SOCKET = {"state": "down", "since": time.time(), "last_frame": 0.0}
SOCKET_SILENT_S = 90   # three missed 30s pings


def _sock_set(state):
    if SOCKET["state"] != state:
        SOCKET["state"] = state
        SOCKET["since"] = time.time()
        nowlog.emit("slack", "socket", state=state, since=SOCKET["since"])


def _sock_up():
    try:
        SOCKET["last_frame"] = time.time()
        _sock_set("up")
    except Exception as e:
        print(f"now board: socket: {e!r}", file=sys.stderr, flush=True)


def _sock_frame():
    try:
        SOCKET["last_frame"] = time.time()
    except Exception as e:
        print(f"now board: socket: {e!r}", file=sys.stderr, flush=True)


def _sock_down():
    try:
        _sock_set("down")
    except Exception as e:
        print(f"now board: socket: {e!r}", file=sys.stderr, flush=True)


def socket_view(now):
    """Copy of SOCKET, forced down when no frame/pong for SOCKET_SILENT_S."""
    v = dict(SOCKET)
    if now - v["last_frame"] > SOCKET_SILENT_S:
        v["state"] = "down"
    return v


def _drop(state, aid, rec, retry=None, ended=False):
    shuttle_core.drop(_B, state, aid, rec, retry, ended)


def _clear_ack(rec):
    shuttle_core.clear_ack(_B, rec)


def _ts_key(m):
    try:
        return float(m["ts"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _drop_inner(state, aid, rec, retry, ended):
    shuttle_core.drop_inner(_B, state, aid, rec, retry, ended)


def notify_hung(chan, quiet):
    shuttle_core.notify_hung(_cfg(), chan, quiet)


def reap_and_count(state):
    return shuttle_core.reap_and_count(_B, state)


def inbox_sweep(state):
    shuttle_core.inbox_sweep(_B, state)


def holding_body(state, cid):
    return shuttle_core.holding_body(state, cid)


def hand_over(state, cid, aid, batch):
    return shuttle_core.hand_over(_B, state, cid, aid, batch)


def spawn_gate(state, people, uid):
    """The registry/workspace gate for a batch the bridge already accepted once
    (a leftover or a retry), re-checked before it reaches a body again:
    ("ok", person) | ("wait", why) for a users.info failure (keep it a while) |
    ("drop", why) for a real no (stranger, guest, unregistered, tier < 2)."""
    ok, reason, _profile = user_verdict(state, uid)
    if not ok:
        return ("wait" if str(reason or "").startswith("users.info failed") else "drop"), reason
    p = people.get(uid)
    if not p or not may_wake(p[1])[0]:   # the one gate (t-1423 step 5): tier 2+ on Slack
        return "drop", "not registered at tier 2+"
    return "ok", p


def channel_busy(state, cid):
    return shuttle_core.channel_busy(_B, state, cid)


def mem_available_mb():
    return nowlog.mem_available_mb()


def resolve_model():
    return shuttle_spawn.resolve_model(_B)


def selvedge_index(doc, platform, team=None, sections=("agents", "people")):
    return shuttle_selvedge.index(doc, platform, team, sections)


def may_wake(rec, venue=None):
    return shuttle_selvedge.may_wake(rec, venue)


# ---------------------------------------------------------------- rendering

_MENTION = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


def clean_text(text, people):
    def m(mo):
        uid = mo.group(1)
        if uid == secret("BOT_USER_ID"):
            return f"@{INSTANCE.agent_name}"
        p = people.get(uid)
        return f"@{p[1]['name']}" if p else f"@{uid}"
    text = _MENTION.sub(m, text or "")
    text = _LINK.sub(lambda mo: mo.group(2) or mo.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def archive(cid, entry, people, venue="slack-dm"):
    """Append one DM message to the monthly archive. Never raises: losing an
    archive line must not cost the reply."""
    try:
        sec = float(entry["ts"])
        p = people.get(entry["uid"])
        # venue: where the conversation happened. Slack DMs today; other
        # surfaces (Discord, email, ...) write the same shape with their own.
        line = {"ts": entry["ts"], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(sec)),
                "venue": venue, "chan": cid, "uid": entry["uid"], "own": entry["own"],
                "slug": SLUG if entry["own"] else (p[0] if p else None),
                "text": entry["text"]}
        ARCHIVE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = ARCHIVE_DIR / time.strftime("%Y-%m.jsonl", time.localtime(sec))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"archive: failed for {cid} {entry.get('ts')}: {e}", flush=True)


def fmt_line(entry):
    return f"{entry['who']}: {entry['text']}"


# ---------------------------------------------------------------- spawning

# The briefs this bridge renders, and the slots it gives each (t-1436). Conduct
# itself lives in the agent's memory (t-1423 step 6.5): a brief names which
# rules apply and carries only the mechanics. startup_check() passes these to
# INSTANCE.check(), so a missing file or a misspelled slot stops the bridge
# before it reaches a person. Slot values are substituted once and never
# re-templated, so a `$` in a message or a card stays verbatim.
#   slack-dm            a 1:1 DM's brief; tier_note = slack-tier-<tier> + the
#                       ext's dm_extra text + slack-grant + slack-restrict
#   slack-card          a person card block (card_note), tier >= 2
#   slack-group         a group DM's brief; note = slack-group-tier-<tier>
#                       (+ slack-group-owner-present), restricts = one
#                       slack-group-restrict per member who has one
# $send_discord / $general are where a tier-2 ask gets flagged (FLAG_TO).
TIERS = (2, 3)   # the tiers a Slack body runs at (people.json; may_wake needs 2+)
BRIEF_SLOTS = {
    "slack-dm": ("name", "slug", "tier", "tier_note", "card_note", "cid", "send", "new", "context"),
    **{f"slack-tier-{t}": ("name", "role", "send_discord", "general") for t in TIERS},
    "slack-card": ("slug", "card"),
    "slack-grant": ("grant",),
    "slack-restrict": ("restrict",),
    "slack-group": ("cid", "names", "roster", "tier", "note", "restricts", "cards", "slugs",
                    "send", "new", "context"),
    **{f"slack-group-tier-{t}": ("send_discord", "general") for t in TIERS},
    "slack-group-owner-present": (),
    "slack-group-restrict": ("name", "restrict"),
}
REQUIRED_BRIEFS = tuple(BRIEF_SLOTS)
# Slots a brief must actually use: without them a person's tier rules, scope,
# standing restriction or card would vanish from the body's brief.
BRIEF_USES = {
    "slack-dm": ("tier_note", "card_note"),
    "slack-card": ("card",),
    "slack-grant": ("grant",),
    "slack-restrict": ("restrict",),
    "slack-group": ("note", "restricts", "cards"),
    "slack-group-restrict": ("restrict",),
}
FLAG_SLOTS = ("send_discord", "general")


def card_note(slug, tier):
    """The person-card block for tier >= 2 speakers, or "" (t-1381). Best-effort:
    a missing card or any error gives no block, and never fails the turn."""
    if tier < 2:
        return ""
    try:
        card = person_cards.loom_card(slug, loom=LOOM)
    except Exception as e:
        print(f"person card for {slug} failed: {e!r}", flush=True)
        return ""
    return INSTANCE.brief("slack-card", slug=slug, card=card) if card else ""


class DmExtraFailed(Exception):
    """The instance's dm_extra raised or returned something unusable. The DM
    gets no body: its extras may be the body's scope (an env, a note)."""


def dm_extra(rec, tier):
    """The instance's additions to a tier-2 DM, from its ext's
    dm_extra(bridge, rec, tier) -> {"text": str, "env": dict|None,
    "on_claimed": callable|None}: text goes after the tier note, env holds
    variables added to the body's launch environment, on_claimed(aid) runs
    once the new body is claimed. No hook: nothing added. Anything else (a
    raise, a wrong shape) is DmExtraFailed, with the original as __cause__."""
    try:
        out = INSTANCE.ext().dm_extra(_B, rec, tier)
        out = {} if out is None else out
        if not isinstance(out, dict):
            raise TypeError(f"dm_extra returned {type(out).__name__}, not a dict")
        text, env, on_claimed = out.get("text") or "", out.get("env"), out.get("on_claimed")
        if not isinstance(text, str):
            raise TypeError("dm_extra's text is not a str")
        if env is not None and not (isinstance(env, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            raise TypeError("dm_extra's env is not a dict of str to str")
        if on_claimed is not None and not callable(on_claimed):
            raise TypeError("dm_extra's on_claimed is not callable")
    except Exception as e:
        raise DmExtraFailed(repr(e)) from e
    return {"text": text, "env": env or None, "on_claimed": on_claimed}


def render_dm(cid, slug, rec, tier, new, context):
    """A 1:1 DM body's brief and launch extras: (prompt, env, on_claimed). Pure
    apart from reading the person card and asking the ext, so a turn's prompt
    can be rendered dry. Raises DmExtraFailed when the ext fails."""
    if tier not in TIERS:
        raise RuntimeError(f"no Slack brief for tier {tier!r} ({slug})")
    name = rec["name"]
    tier_note = INSTANCE.brief(f"slack-tier-{tier}", name=name, role=rec.get("role", ""),
                               send_discord=FLAG_SEND, general=FLAG_CHAN)
    env = on_claimed = None
    if tier == 2:
        extra = dm_extra(rec, tier)
        tier_note += extra["text"]
        if rec.get("grant"):
            # A standing authority is a gate, so it rides from people.json,
            # never from the card (t-1386) — and survives a card read failing.
            tier_note += INSTANCE.brief("slack-grant", grant=rec["grant"])
        if rec.get("restrict"):
            tier_note += INSTANCE.brief("slack-restrict", restrict=rec["restrict"])
        env = {**os.environ, **extra["env"]} if extra["env"] else None
        on_claimed = extra["on_claimed"]
    new_txt = "\n".join(fmt_line(e) for e in new)
    ctx_txt = "\n".join(fmt_line(e) for e in context) or "(no earlier messages seen)"
    prompt = INSTANCE.brief("slack-dm", name=name, slug=slug, tier=tier, tier_note=tier_note,
                            card_note=card_note(slug, tier), cid=cid, send=SLACK_SEND,
                            new=new_txt, context=ctx_txt)
    return prompt, env, on_claimed


# A dm_extra failure (t-1436) fails closed for that DM: no body is spawned (it
# could be one whose scope, a note and an env, came from the ext). The batch is
# held as a leftover and tried again each tick, quietly: one log line when the
# DM starts failing, one when it works again or is dropped. It is held for the
# instance's wait TTL (cfg.wait_ttl), measured from the oldest held message's
# own ts, since run_leftovers resets leftover_since before every spawn; then it
# is dropped and the person gets the standard failure notice. Each distinct
# failure (exception type + where it was raised) pages once when it starts and
# once when it first drops a DM.
_EXT_PAGED = set()
_DROP_PAGED = set()
_DM_FAILING = {}   # cid -> the failure's site, while that DM's dm_extra fails


def _error_site(e):
    """An exception's type and innermost frame: the same failure gives the same
    key even when its message carries a timestamp or an id."""
    tb = traceback.extract_tb(e.__traceback__)
    where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "?"
    return f"{type(e).__name__}@{where}"


def _dm_extra_failed(state, cid, slug, batch, err):
    cause = err.__cause__ or err
    key = _error_site(cause)
    leftover = state.setdefault("leftover", {})
    held = leftover.setdefault(cid, [])
    have = {msg_key(m) for m in held}
    held.extend(m for m in batch if msg_key(m) not in have)
    if cid not in _DM_FAILING:
        print(f"dm_extra failing for {slug} ({cid}): {cause!r}; no body spawned, holding "
              f"{len(held)} msg (up to {_cfg().wait_ttl}s)", file=sys.stderr, flush=True)
    _DM_FAILING[cid] = key
    if key not in _EXT_PAGED:
        _EXT_PAGED.add(key)
        page(f"{SLUG}-slack: the instance's dm_extra failed for {slug}: {cause!r}. No body was "
             "spawned for that DM; its messages wait until this is fixed. This error won't page again.")
    since = oldest_ts(held)
    if since is not None and time.time() - since < _cfg().wait_ttl:
        return
    leftover.pop(cid, None)
    _DM_FAILING.pop(cid, None)
    print(f"dm_extra still failing for {slug} ({cid}) after {_cfg().wait_ttl}s: dropped "
          f"{len(held)} msg, told them", file=sys.stderr, flush=True)
    notify_failure(cid, "a setup error on my side")
    if key not in _DROP_PAGED:
        _DROP_PAGED.add(key)
        page(f"{SLUG}-slack: dropped {len(held)} message(s) from {slug} after {_cfg().wait_ttl}s "
             f"of dm_extra failing ({cause!r}); they got the failure notice. Later DMs this "
             "error drops won't page again.")


def spawn_session(state, cid, slug, rec, tier, new, context, people, attempt=1):
    """Spawn the DM's body; False if dm_extra failed and none was spawned
    (dispatch then spends no slot on it)."""
    try:
        prompt, env, on_claimed = render_dm(cid, slug, rec, tier, new, context)
    except DmExtraFailed as e:
        _dm_extra_failed(state, cid, slug, new, e)
        return False
    if _DM_FAILING.pop(cid, None):
        print(f"dm_extra works again for {slug} ({cid})", file=sys.stderr, flush=True)
    # The ext's env and on_claimed go to the launch as they are: env (when the
    # ext gives one) is the bridge's own environment plus its variables, and
    # on_claimed runs once exactly one new body is claimed (shuttle/spawn.py).
    spawn_body(state, {
        "chan": cid, "label": slug, "venue": "slack-dm",
        "inbox_label": f"your Slack DM with {rec['name']}", "who": [display_name(slug)],
        "members": [slug],
        "prompt": prompt, "deny": [], "env": env, "rec": {"who": slug},
        "on_claimed": on_claimed,
    }, new, attempt)
    return True


# ---------------------------------------------------------------- group DMs (t-1422)
# A group DM (mpim) is one room with several people in it. Its members are
# fixed (adding someone makes a new group), so they're looked up once and
# cached. The lowest tier present governs the room: a body only answers when
# every member would get one on their own, and then under the rules for the
# least trusted of them. Anyone not in people.json is a stranger, never
# skipped, so a stranger in the room means no body. A person's DM extras (the
# ext's dm_extra, e.g. a scope) don't apply in a group (they belong to one
# person, not a room), and nobody's standing authority does either; everybody's
# standing restriction does.

GROUP_MEMBERS_TTL_S = 3600


def group_members(state, cid):
    """The group's member user ids, from cache or conversations.members; None
    if Slack can't say right now."""
    cache = state.setdefault("group_members", {})
    hit = cache.get(cid)
    if hit and time.time() - hit.get("at", 0) < GROUP_MEMBERS_TTL_S:
        return hit["uids"]
    try:
        uids = api("conversations.members", channel=cid).get("members") or []
    except Exception as e:
        print(f"conversations.members {cid} failed: {e!r}", flush=True)
        return None
    cache[cid] = {"at": time.time(), "uids": uids}
    return uids


def group_gate(state, people, cid):
    """("ok", conv) for a room every member may reach a body from, with the
    lowest tier present; ("wait", why) while Slack can't say who is in it;
    ("drop", why) when anyone in it may not."""
    uids = group_members(state, cid)
    if uids is None:
        return "wait", "conversations.members failed: who is in the group is unknown"
    members = []
    for uid in uids:
        if uid == secret("BOT_USER_ID"):
            continue
        verdict, p = spawn_gate(state, people, uid)
        if verdict != "ok":
            return verdict, f"group member {uid}: {p}"
        if not isinstance(p[1].get("tier"), int):
            return "drop", f"group member {uid}: no tier"
        members.append(p)
    if len(members) < 2:
        return "drop", "not a group: fewer than two other members"
    members.sort(key=lambda p: p[1]["name"])
    names = [p[1]["name"] for p in members]
    return "ok", {"id": cid, "group": True, "slug": ", ".join(names),
                  "members": members, "names": names,
                  "tier": min(p[1]["tier"] for p in members)}


def build_group_prompt(conv, new, context):
    """A group DM's brief. Pure apart from reading the members' cards."""
    cid, tier = conv["id"], conv["tier"]
    roster = "\n".join(f"- {rec['name']}: registry slug `{slug}`, tier {rec['tier']}"
                       + (f" ({rec['role']})" if rec.get("role") else "")
                       for slug, rec in conv["members"])
    note = INSTANCE.brief(f"slack-group-tier-{tier if tier in TIERS else 2}",
                          send_discord=FLAG_SEND, general=FLAG_CHAN)
    if tier == 2 and any(rec.get("tier") == 3 for _, rec in conv["members"]):
        note += INSTANCE.brief("slack-group-owner-present")
    restricts = "".join(INSTANCE.brief("slack-group-restrict", name=rec["name"],
                                       restrict=rec["restrict"])
                        for _, rec in conv["members"] if rec.get("restrict"))
    cards = "".join(card_note(slug, rec["tier"]) for slug, rec in conv["members"])
    slugs = ", ".join(f'"{slug}"' for slug, _ in conv["members"])
    new_txt = "\n".join(fmt_line(e) for e in new)
    ctx_txt = "\n".join(fmt_line(e) for e in context) or "(no earlier messages seen)"
    return INSTANCE.brief("slack-group", cid=cid, names="; ".join(conv["names"]), roster=roster,
                          tier=tier, note=note, restricts=restricts, cards=cards, slugs=slugs,
                          send=SLACK_SEND, new=new_txt, context=ctx_txt)


def spawn_group_session(state, conv, new, context, attempt=1):
    spawn_body(state, {
        "chan": conv["id"], "label": conv["slug"], "venue": "slack-group",
        "inbox_label": "your Slack group DM with " + ", ".join(conv["names"]),
        "who": conv["names"], "members": [slug for slug, _ in conv["members"]],
        "prompt": build_group_prompt(conv, new, context), "deny": [], "env": None,
        "rec": {"who": conv["slug"], "who_names": conv["names"], "venue": "slack-group"},
    }, new, attempt)


def notify_failure(chan, why="upstream API error", parked=False):
    shuttle_core.notify_failure(_cfg(), chan, why, parked)


def page(msg):
    shuttle_core.page(_cfg(), msg)


# ---------------------------------------------------------------- socket mode

EVENTS = queue.Queue()
# Restart safety (t-1423 step 0.5). An envelope is acked only while the bridge
# is accepting; once it's stopping, Slack keeps what isn't acked and re-sends
# it to the next connection. The lock makes "check, ack, queue" one step, so
# nothing is acked after drain() has emptied the queue.
ACCEPTING = True
ACCEPT_LOCK = threading.Lock()
STOP = threading.Event()   # set by SIGTERM; the main loop drains and exits
PENDING_MAX_AGE = 86400    # on start, replay pending DMs up to a day old


def handle_frame(ws, raw):
    """One Socket Mode frame: ack the envelope and queue an events_api payload
    for the main loop, unless the bridge has stopped accepting."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return
    t = d.get("type")
    _sock_frame()
    if t == "hello":
        print("socket mode: hello", flush=True)
        _sock_up()
        return
    if t == "disconnect":
        print(f"socket mode: disconnect ({d.get('reason')})", flush=True)
        ws.close()
        return
    with ACCEPT_LOCK:
        if not ACCEPTING:
            return
        env_id = d.get("envelope_id")
        if env_id:
            ws.send(json.dumps({"envelope_id": env_id}))
        if t == "events_api":
            EVENTS.put(d.get("payload", {}))


def stop_accepting():
    global ACCEPTING
    with ACCEPT_LOCK:
        ACCEPTING = False


def drain(people):
    """Shutdown: stop acking, then ingest every event already acked into
    `pending` and save it, so the next start replies to it. Returns how many
    events were drained."""
    stop_accepting()
    n = 0
    with state_locked():
        state = load_state()
        while True:
            try:
                ingest(state, EVENTS.get_nowait(), people)
            except queue.Empty:
                break
            n += 1
        save_state(state)
    return n


def restore_pending(state, now):
    """Start: keep pending DMs (a restart used to drop them; the spawned_msgs
    guard skips any already handled), except entries over PENDING_MAX_AGE old.
    Returns (kept, dropped)."""
    kept = dropped = 0
    pending = state.setdefault("pending", {})
    for cid in list(pending):
        fresh = []
        for e in pending[cid]:
            try:
                ok = now - float(e.get("ts")) <= PENDING_MAX_AGE
            except (TypeError, ValueError):
                ok = False
            if ok:
                fresh.append(e)
                kept += 1
            else:
                dropped += 1
        if fresh:
            pending[cid] = fresh
        else:
            pending.pop(cid)
    return kept, dropped


def ws_loop():
    """Own thread: open a Socket Mode connection, ack every envelope
    immediately, hand the payload to the main loop. Reconnect forever."""
    import websocket  # websocket-client: only the live socket needs it
    backoff = 5
    while True:
        try:
            url = api("apps.connections.open", token=secret("APP_TOKEN"))["url"]
        except Exception as e:
            print(f"apps.connections.open failed: {e!r}; retry in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        def on_error(ws, err):
            print(f"socket mode error: {err!r}", flush=True)

        app = websocket.WebSocketApp(url, on_message=handle_frame, on_error=on_error,
                                     on_open=lambda ws: _sock_up(),
                                     on_pong=lambda ws, data: _sock_frame(),
                                     on_close=lambda ws, code, msg: _sock_down())
        app.run_forever(ping_interval=30, ping_timeout=10)
        _sock_down()
        print("socket mode: connection closed; reconnecting", flush=True)
        backoff = 5
        time.sleep(2)


# ---------------------------------------------------------------- main loop

def ingest(state, payload, people):
    """Turn an events_api payload into history + pending entries."""
    ev = payload.get("event", {})
    kind = ev.get("channel_type")
    if ev.get("type") != "message" or kind not in ("im", "mpim"):   # mpim: group DMs, t-1422
        return
    if ev.get("subtype") not in (None, "file_share", "bot_message"):
        return  # edits, deletes, joins, …  (bot_message = our own posts; kept for context)
    cid, ts, uid = ev.get("channel"), ev.get("ts"), ev.get("user")
    if not cid or not ts:
        return
    seen = state.setdefault("seen", {}).setdefault(cid, [])
    if ts in seen:
        return  # Slack retry of an already-acked envelope
    seen.append(ts)
    del seen[:-200]
    text = clean_text(ev.get("text"), people)
    for f in ev.get("files", []) or []:
        text += f" [file: {f.get('name')} {f.get('url_private', '')}]"
    if uid == secret("BOT_USER_ID") or ev.get("bot_id"):
        who = INSTANCE.agent_name
        entry = {"who": who, "text": text, "ts": ts, "uid": uid, "own": True}
    else:
        p = people.get(uid)
        who = f"{p[1]['name']} [{p[0]}]" if p else f"[UNKNOWN id={uid}]"
        entry = {"who": who, "text": text, "ts": ts, "uid": uid, "own": False}
    if kind == "mpim":
        state.setdefault("groups", {})[cid] = True
    archive(cid, entry, people, "slack-group" if kind == "mpim" else "slack-dm")
    hist = state.setdefault("history", {}).setdefault(cid, [])
    hist.append(entry)
    del hist[:-HISTORY_KEEP]
    if not entry["own"]:
        state.setdefault("pending", {}).setdefault(cid, []).append(entry)


RELAY_EVERY_S = 15   # how often the bridge asks loom for relays queued to it (shuttle/relay.py)


def relay_in(state, people, run=subprocess.run, now=None):
    """Relays queued for this bridge (by another body, through loom's piece API) become
    messages in the person's DM: the body holding it gets them, else one starts."""
    now = time.time() if now is None else now
    if now - float(state.get("relay_checked") or 0) < RELAY_EVERY_S:
        return 0
    state["relay_checked"] = now
    pc = INSTANCE.commands.get("piece_call") or shutil.which("piece-call")
    if not pc:
        return 0
    try:
        r = run([str(pc), "shuttle", "relay-take", "--via", "slack"], capture_output=True, text=True, timeout=20)
        items = json.loads(r.stdout or "[]") if r.returncode == 0 else []
        if r.returncode:
            print(f"relay-take: {r.stderr.strip()[:200]}", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"relay-take: {e!r}", flush=True)
        return 0
    by_slug = {slug: (uid, rec) for uid, (slug, rec) in people.items()}
    n = 0
    for e in items if isinstance(items, list) else []:
        uid, rec = by_slug.get(e.get("person"), (None, None))
        if not uid:
            page(f"relay {e.get('id')}: {e.get('person')} has no Slack id in this workspace; not delivered")
            continue
        try:
            cid = api("conversations.open", users=uid)["channel"]["id"]
        except Exception as err:
            page(f"relay {e.get('id')} to {e.get('person')}: couldn't open the DM ({err!r}); not delivered")
            continue
        entry = {"who": f"{rec['name']} [{e.get('person')}] (relay)", "text": relay.brief(e, INSTANCE.agent_name),
                 "ts": f"{now + n / 1000:.6f}", "uid": uid, "own": False, "relay": e.get("id")}
        hist = state.setdefault("history", {}).setdefault(cid, [])
        hist.append(entry)
        del hist[:-HISTORY_KEEP]
        state.setdefault("pending", {}).setdefault(cid, []).append(entry)
        print(f"relay {e.get('id')} from {e.get('from') or '?'} -> {e.get('person')} ({cid})", flush=True)
        n += 1
    return n


def tick(state, people):
    prune_spawned_msgs(state)   # expire old dedup entries (2h TTL, safety net)
    while True:
        try:
            ingest(state, EVENTS.get_nowait(), people)
        except queue.Empty:
            break
    relay_in(state, people)
    inbox_sweep(state)
    live, retries = reap_and_count(state)
    holds = []   # now board: what is waiting and why (t-1403)
    ctx = {"people": people}
    live = run_retries(state, holds, live, retries, ctx)
    live = run_leftovers(state, holds, live, ctx)
    pending = state.setdefault("pending", {})
    for cid in list(pending):
        batch = pending[cid]
        if not batch:
            pending.pop(cid, None)
            continue
        # The DM is 1:1 — every non-own message in it is from the same human.
        uid = batch[-1]["uid"]
        verdict, conv = admit(state, cid, batch, "new", ctx)
        if verdict == "wait":   # users.info failed: keep it, like a leftover, for a while
            if time.time() - (oldest_ts(batch) or time.time()) >= LEFTOVER_TTL_S:
                print(f"on read: {uid} ({conv}) for {LEFTOVER_TTL_S}s — {len(batch)} msg dropped",
                      flush=True)
                pending.pop(cid, None)
            continue
        if verdict == "drop":
            profile = state.get("users", {}).get(uid, {}).get("profile") or {}
            print(f"on read: {uid} ({profile.get('real_name') or profile.get('name')}, "
                  f"{conv}) — {len(batch)} msg", flush=True)
            pending.pop(cid, None)
            continue
        # Per-ts dedup guard: Socket Mode fans the same event out to every open
        # connection, so a restart-race overlap of two bridge processes would
        # otherwise double-spawn on the same DM message (t-908).
        pending[cid] = batch = dedup(state, batch, conv["slug"])
        if not batch:
            pending.pop(cid, None)
            continue
        outcome, live = offer(state, holds, live, conv, batch, ctx)
        if outcome != "held":
            pending.pop(cid, None)
    try:
        held = {h["chan"] for h in holds}
        state["now_wait"] = {c: r for c, r in state.get("now_wait", {}).items() if c in held}
        nowlog.write_now("slack", nowlog.snapshot(
            "slack", TICK_SECONDS, MAX_LIVE, mem_available_mb(), talking_items(state), holds,
            socket=socket_view(time.time())))
    except Exception as e:
        print(f"now board: snapshot: {e!r}", file=sys.stderr, flush=True)
    save_state(state)


def _brief_uses(name, slot):
    """Whether brief `name` renders slot `slot` (False if it can't render)."""
    sentinels = {k: f"\x00{k}\x00" for k in BRIEF_SLOTS[name]}
    try:
        return sentinels[slot] in INSTANCE.brief(name, **sentinels)
    except Exception:
        return False   # INSTANCE.check() reports it


def startup_check():
    """Everything the bridge needs from the instance, checked once, dry (no
    network, nothing written). Each item fails closed at runtime too, but
    silently or for every DM; here it stops the start with a reason: the
    briefs render with the slots this bridge gives them and use the ones they
    must (check()); the send command is set; always_deny is stated (an
    explicit [] is allowed); the alert command can run; a brief that tells a
    body where to flag an ask has a slack.flag_to that resolves to a command
    and a channel id (a non-empty string); the runner is one this shuttle has,
    can run here, and can deny always_deny (codex: it must be []); the ext module loads and its
    check(instance) finds nothing; the secrets file has the four credentials.
    Loading the briefs here also keeps them for the run (Instance._text).
    Returns a list of problems, [] when all is well."""
    path = INSTANCE.path
    errors = INSTANCE.check(BRIEF_SLOTS, required=REQUIRED_BRIEFS, uses=BRIEF_USES)
    if SLACK_SEND is None:
        errors.append(f"{path}: commands.send.slack is not set")
    if not isinstance(INSTANCE.raw.get("always_deny"), list):
        errors.append(f"{path}: always_deny is required: the tools no body may use "
                      "(a list; [] to deny nothing)")
    alert = INSTANCE.commands.get("alert")
    if not (isinstance(alert, Path) and alert.is_file() and os.access(alert, os.X_OK)):
        errors.append(f"{path}: commands.alert ({alert}) must be an executable file: "
                      "without it every page is lost")
    flagging = sorted({name for name in BRIEF_SLOTS for slot in FLAG_SLOTS
                       if slot in BRIEF_SLOTS[name] and _brief_uses(name, slot)})
    if flagging and not (FLAG_SEND and isinstance(FLAG_CHAN, str) and FLAG_CHAN):
        errors.append(f"{path}: slack.flag_to ({FLAG_TO}) must name a commands.send entry (via) "
                      f"and a channel (chan, or chan_from_routes in {ROUTES_FILE}): "
                      f"{', '.join(n + '.md' for n in flagging)} tell a body to flag asks there")
    errors += INSTANCE.runner_errors()   # runner.kind is one this shuttle has, and can run here
    # A runner that can't deny tools (codex) refuses an always_deny here, at start,
    # not as a start() that raises every spawn (t-1439 review C6). Slack has no
    # guest venues: its plans deny nothing beyond always_deny.
    errors += INSTANCE.runner_deny_errors()
    errors += INSTANCE.ext_errors()   # the ext loads, and its own check(instance) passes
    try:
        env = slack_env()
        missing = [k for k in _SECRET_KEYS.values() if not env.get(k)]
        if missing:
            errors.append(f"{SECRETS}: missing {', '.join(missing)}")
    except OSError as e:
        errors.append(f"slack secrets: {e!r}")
    return errors


def main():
    # This is a daemon, not a CLI: any argv means someone (probably a sleeve
    # guessing at a read interface) is about to start a second bridge.
    if len(sys.argv) > 1:
        sys.exit(
            f"{SLUG}-slack takes no arguments — it is the bridge daemon "
            f"(one instance, run by {SLUG}-slack.service). To send a Slack message, "
            "use the instance's send command, not this script."
        )
    errors = startup_check()
    if errors:
        sys.exit(f"instance check failed: {INSTANCE.path}\n" + "\n".join(errors))
    print(f"instance check ok: {INSTANCE.path}", flush=True)
    # The socket thread imports websocket-client when it opens; without it that
    # thread would die and leave a bridge that ticks but never hears a message.
    try:
        import websocket  # noqa: F401
    except ImportError as e:
        sys.exit(f"{SLUG}-slack needs websocket-client for Socket Mode ({e!r}): run it under "
                 "the Python that has it installed. Not starting.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "bridge.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"{SLUG}-slack bridge already running (bridge.lock held) — refusing to double-spawn.")
    me = api("auth.test")
    print(f"{SLUG}-slack bridge up: team={me.get('team')} bot={me.get('user')} "
          f"preferred={PREFERRED_MODEL} (via {MODEL_RESOLVER})", flush=True)
    with state_locked():
        state = load_state()
        kept, dropped = restore_pending(state, time.time())
        save_state(state)
    if kept or dropped:
        print(f"pending from before the restart: {kept} kept, {dropped} dropped (over a day old)",
              flush=True)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    threading.Thread(target=ws_loop, daemon=True).start()
    people_mtime = 0
    people = {}
    while not STOP.is_set():
        try:
            mt = PEOPLE_FILE.stat().st_mtime
            if mt != people_mtime:
                people = load_people()
                people_mtime = mt
                print(f"people registry: {len(people)} slack ids", flush=True)
            with state_locked():
                # Re-read from disk inside the lock so that if two bridge
                # processes somehow overlap (e.g. a systemd restart race, or
                # Socket Mode handing the same event to two live
                # connections), the second writer always sees the first's
                # committed spawned_msgs/pending rather than a stale
                # in-memory copy. Matches the Discord adapter (t-323).
                state = load_state()
                try:
                    tick(state, people)
                except Exception:
                    # Unlike the Discord bridge (poll + cursor: a failed tick
                    # just re-fetches the same messages next time), tick()
                    # drains EVENTS.get_nowait() — once ingest() dequeues a
                    # Slack event it is gone from the queue for good. Persist
                    # whatever was merged into `state` before the failure
                    # (reloading fresh from disk next iteration must not also
                    # discard it) and re-raise so the outer handler still
                    # logs and backs off.
                    save_state(state)
                    raise
        except Exception as e:
            print(f"tick error: {e!r}", file=sys.stderr, flush=True)
        STOP.wait(TICK_SECONDS)
    n = drain(people)
    print(f"stopping: drained {n} queued event(s) into pending", flush=True)


if __name__ == "__main__":
    main()
