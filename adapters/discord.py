#!/usr/bin/env python3
"""<agent>-discord: polling bridge between a Discord server and an agent instance.

v1 design (stdlib only, no gateway): every POLL_SECONDS, poll each text
channel for messages newer than the stored cursor. New human messages spawn
a `claude --bg` session (interactive billing pool — NEVER claude -p) with
the channel context embedded; the session replies through the instance's
Discord send command. The bridge never parses model output — it only watches,
spawns, and advances cursors. Cursors persist across restarts; on first sight
of a channel the cursor initializes to "now" so history is never replayed.

Everything about the agent (who it is, its paths, ids, prose and extras) comes
from the instance file named by $SHUTTLE_INSTANCE (shuttle/instance.py, t-1436):
routes.json and people.json sit beside it, the briefs are its `discord*.md`,
and its extension module's tick(bridge, state) runs once per tick.
"""

import datetime
import fcntl
import json
import os
import re
import secrets  # the spawn token: the shuttle reaches it as b.secrets (t-1423)
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the shuttle checkout
from shuttle import instance as shuttle_instance  # noqa: E402  the instance file (t-1436)
from shuttle import nowlog  # noqa: E402  the "now board" (t-1403)
from shuttle import inbox  # noqa: E402  a working body hears follow-ups (t-1409)
from shuttle import people as person_cards  # noqa: E402  loom person cards (t-1381)
from shuttle import core as shuttle_core  # noqa: E402  the shared bridge core (t-1423)
from shuttle import upgrade  # noqa: E402  stop when idle and behind, so a fresh house starts on new code
from shuttle import dispatch as shuttle_dispatch  # noqa: E402
from shuttle import spawn as shuttle_spawn  # noqa: E402
from shuttle import selvedge as shuttle_selvedge  # noqa: E402
from shuttle import runner as shuttle_runner  # noqa: E402  the harness bodies run on (t-1438)
from shuttle.config import Config as ShuttleConfig  # noqa: E402

INSTANCE = shuttle_instance.load()   # $SHUTTLE_INSTANCE; exits if unset or unreadable
SLUG = INSTANCE.agent_slug
DISCORD = INSTANCE.section("discord")
# Board keys stay "<slug>|discord|<chan>" (t-1436 review focus 4). The
# conversation-id map lives with the Slack bridge's archive, which both read.
nowlog.configure(now_dir=INSTANCE.now_dir, ids_dir=INSTANCE.state_dir("slack") / "archive",
                 agent=SLUG)
inbox.configure(inbox_dir=INSTANCE.inbox_dir)

STATE_DIR = INSTANCE.state_dir("discord")
STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK_FILE = STATE_DIR / "state.json.lock"
API = "https://discord.com/api/v10"
CLAUDE = INSTANCE.commands.get("claude") or Path.home() / ".local/bin/claude"
DISCORD_SEND = (INSTANCE.commands.get("send") or {}).get("discord")
WORKDIR = INSTANCE.workdir   # the bodies' cwd
# This shuttle's checkout and the agent's repo the bodies work in: stale ones are picked up when idle.
UPGRADE = upgrade.Upgrader([Path(__file__).resolve().parent.parent, WORKDIR])


def _instance_routes():
    """routes.json beside the instance file, at start, for the instance ids
    (t-1423 step 7: they live with the instance, not in this source). The
    per-tick routing reloads it; these need a restart. Missing or unreadable:
    no ids, so the bridge polls nothing rather than guessing."""
    try:
        return json.loads((INSTANCE.dir / "routes.json").read_text())
    except (OSError, ValueError) as e:
        print(f"routes.json unreadable at start ({e!r}); no instance ids", flush=True)
        return {}


_IDS = _instance_routes()
GUILD_ID = _IDS.get("guild")
BOT_USER_ID = (_IDS.get("bots") or {}).get(SLUG)

PREFERRED_MODEL = INSTANCE.preferred_model
MODEL_RESOLVER = INSTANCE.commands.get("model")   # rate-limit-aware resolver
POLL_SECONDS = 15
# The bridge's own posts (hung / gave-up / stop / guest-cap notes) log as job
# "bridge" in sent.log, so they never count as a body's reply (session_replied
# matches the body's job id, or "-" for a post with no job). t-1411 review.
BRIDGE_SEND_ENV = {**os.environ, "CLAUDE_JOB_DIR": "bridge", "SHUTTLE_BODY": "bridge"}
# Page via the instance's alert command after this many consecutive failed ticks (~5 min at
# POLL_SECONDS). The service stays "active" while every tick throws, so
# systemd alone never notices a deaf bridge (t-1189: 2h of KeyError('cursors')
# after a test clobbered state.json, nobody paged).
TICK_ERROR_ALERT_AFTER = 20
ALERT = INSTANCE.commands.get("alert")
CHANNEL_COOLDOWN = 20          # min seconds between session spawns per channel
MAX_ATTEMPTS = 2               # spawns per message batch before giving up (API error OR silent death, t-501)
JOBS_DIR = Path.home() / ".claude/jobs"   # per-session timeline.jsonl lives here
CONTEXT_MESSAGES = 12          # recent messages embedded as context

# --- Resource guards (added 2026-08-16 after a host OOM crash) ---
# `claude --bg` sessions do NOT exit when done: they park as `idle` at
# ~400MB RSS each, waiting to be resumed. A chat bridge that spawns one per
# reply therefore accumulates resident sessions without bound (24 of them
# = ~9GB → container OOM → host-wide OOM cascade → host hard reset).
# Rules: reap idle children every tick, never exceed MAX_LIVE sessions,
# and refuse to spawn when the LXC is short on memory.
#
# Delivery guarantees (added 2026-08-18 after a session died on an upstream
# "API Error: Server error mid-response" and the message silently dropped):
# each owned session remembers the channel + message batch it was spawned
# for. If it ends in an API-error state, the batch is re-spawned once; if
# that fails too, a one-line notice is posted to the channel. Also: at most
# ONE live session per channel — later messages accumulate behind it and
# spawn together when it finishes (a queue, not a second uncoordinated body).
MAX_LIVE = 5                   # concurrent bridge-owned sessions (3 -> 5 with t-1409: nothing locked)
MIN_AVAIL_MB = 1500            # don't spawn below this MemAvailable
REAP_GRACE = 30                # idle this long before reaping, unless its job says done (then now)

# --- Presence (2026-09-16, the owner's ask): the human-facing signal
# that a body is on it. 👀 lands on the newest message of the batch the
# moment a body spawns, and a typing pulse runs while the body holds the
# channel and has not yet posted. Discord's typing state lasts ~10s per
# POST and the tick is 15s, so the pulse is its own thread. Both are best
# effort: a failed reaction or pulse is logged, never fatal.
ACK_EMOJI = "👀"
TYPING_EVERY = 8               # seconds between typing pulses per busy channel
# A body holds its channel until IT ends its turn (t-1409, 2026-10-20). Follow-ups
# that arrive while it works go to its inbox (bridge/inbox.py) and the
# bridge-inbox hook hands them over mid-turn; its Stop hook won't let the body
# end while any are unread. There used to be a 3-minute "quiet = stalled,
# channel free" release (STALL_S); it put a second, parallel body in the channel
# of every long build (20 times in 3 days). The owner: "you know when you're done."
# Two kills remain, for bodies that can't know they're done:
#  - PARKED (added 2026-08-24, a #general body armed a 1-hour Monitor and held
#    the channel 30+ min): its only in-flight work is monitors. Reaped.
#  - HUNG: wrote nothing at all for HANG_KILL_S while "working" (a dead API
#    call, a wedged tool). Killed, and the channel is told.
PARKED_KILL_S = 60             # parked (monitor-only) this long -> reap
HANG_KILL_S = 1800             # silent this long while working -> hung, kill

# Webhook/notification channels — never respond there (routes.json `ignore`).
IGNORE_CHANNELS = set(_IDS.get("ignore") or {})
# An allowlist (routes.json `only`): when set, the bridge polls these channels
# and the threads under them and nothing else, not even an @mention elsewhere.
# For a trial beside another bridge that still owns the rest of the guild: an
# unlisted channel is otherwise mentions-only, and both bridges would answer.
ONLY_CHANNELS = set(_IDS.get("only") or ())


def polled(cid):
    """Does this bridge read channel `cid` (a thread goes by its parent)?"""
    return cid not in IGNORE_CHANNELS and (not ONLY_CHANNELS or cid in ONLY_CHANNELS)

# Channel notes (t-1436): the instance's `channel_notes` maps a routes.json key
# (a list of channel ids) to a brief; a body spawned in one of those channels,
# or a thread under one, reads that brief where the notes go. In file order.
CHANNEL_NOTES = [(brief, set(_IDS.get(key) or {}))
                 for key, brief in INSTANCE.channel_notes.items()]
NOTIFY_CHANNEL = (_IDS.get("notify") or {}).get(SLUG)   # where a guest venue's worries go
# What a guest who has used up the day's spawns is told (after "(<tag>) ").
GUEST_CAP_NOTE = DISCORD.get("guest_cap_note") or "that's all from me for today. See you tomorrow!"

# Who is who — keyed by Discord snowflake, NOT username/display name (those
# can be changed by anyone at any time): the instance's people.json, the same
# file the Slack bridge reads, through the shuttle's one gate (t-1423 step 5).
# People carry their Discord user ids; peer agents are `agents` with their bot
# ids. Anyone not listed is a stranger until the owner introduces them; the spawn
# prompt says so. Reloaded every tick; an unreadable file keeps the last good
# copy.
PEOPLE_FILE = INSTANCE.dir / "people.json"
WHO = {}      # discord id -> (slug, record), people and peer bots
PEOPLE = {}   # discord id -> the label a body reads: "Pat", "Kit (peer agent bot)"

# --- Three-way routing (t-591, 2026-09-12) ---
# routes.json (beside the instance file) is the route map every agent's home
# reads a copy of: channel id -> {primary: <agent slug>|null, peer: bool}.
# Unlisted channel = mentions-only. Peer bot messages wake a body ONLY in a
# peer-enabled channel/thread AND only when they carry an explicit mention of
# this bot; everywhere else they are context.
# The exchange budget lives HERE, in the transport (one-shot bodies cannot
# count their own predecessors): PEER_TURNS_EACH spawns per thread for
# peer-originated batches, reset by any allowed human message there (NOT just
# routable ones — in a peer-primary venue the owner's messages route to nobody).
ROUTES_FILE = INSTANCE.dir / "routes.json"
# Guests (t-693, 2026-09-13): a guest is a human the owner introduced for ONE
# purpose: a people.json person at tier 1 with {venues: [chan ids],
# daily_spawns} (until t-1423, routes.json `guests`). A guest's message wakes a body only inside its venues
# (a thread counts via its parent); anywhere else it is nothing, even an
# @mention — Discord hiding the channels is the first fence, this is the
# second. Humans who are neither in PEOPLE nor a guest wake nothing anywhere.
# A venue marked guest_venue gets the guest brief and a narrowed tool set on
# EVERY body spawned there, because the guest reads every reply.
GUESTS = {}   # discord id -> record, the tier-1 people
_WHO_PAGED = False


def load_who(doc=None, page_if_empty=False):
    """Refresh WHO, PEOPLE and GUESTS from people.json (or `doc`). Never raises:
    on a file it can't read it keeps the last good copy, and with nothing loaded
    at all (a bad file at startup) nobody can wake a body here, which pages once
    when the tick asks (never at import: tests load this module)."""
    global _WHO_PAGED
    if doc is None:
        try:
            doc = json.loads(PEOPLE_FILE.read_text())
        except Exception as e:
            if WHO:
                print(f"people.json unreadable ({e!r}); keeping who was loaded last", flush=True)
            else:
                print(f"people.json unreadable ({e!r}) and nothing loaded yet: "
                      "nobody can wake a body on Discord", flush=True)
                if page_if_empty and not _WHO_PAGED:
                    _WHO_PAGED = True
                    page(f"{SLUG}-discord: people.json unreadable ({e!r}) and nothing loaded; "
                         "nobody can wake a body on Discord until it is fixed.")
            return
    who_idx = selvedge_index(doc, "discord")
    WHO.clear()
    WHO.update(who_idx)
    PEOPLE.clear()
    PEOPLE.update({uid: person_label(rec) for uid, (_, rec) in who_idx.items() if person_known(rec)})
    GUESTS.clear()
    GUESTS.update({uid: rec for uid, (_, rec) in who_idx.items()
                   if not rec["agent"] and rec.get("tier") == 1})
# What every body may not use is the instance's `always_deny` (in the Config);
# a guest venue's bodies also lose the instance's `guest_deny`: the guest reads
# every reply (t-1119/t-1142).
THREAD_FIRST_SIGHT_S = 3600    # a thread this young on first sight is read from its start
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_ANY_MENTION_RE = re.compile(r"<@[!&]?\d+>")


def load_routes():
    try:
        return json.loads(ROUTES_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"routes.json unreadable ({e!r}); mentions-only everywhere", flush=True)
        return {"channels": {}, "threads": {}, "bots": {SLUG: BOT_USER_ID}}


def peer_bot_ids(routes):
    return {v for k, v in routes.get("bots", {}).items() if k != SLUG and v} | set(webhook_peers(routes))


# Webhook peers (t-1053, 2026-10-06): an agent with no bot user (a ChatGPT
# agent, say) posts through a per-channel webhook. routes.json `webhook_peers` maps webhook id -> name. Such a
# message's author.id IS the webhook id and author.bot is true, so once the
# id is in `peers` it routes exactly like a peer bot: a wake only in a peer
# venue with an explicit mention of this bot. Keyed on the webhook id, never
# the display name: any webhook can call itself anything. Every other webhook stays inert.
WEBHOOK_PEERS = {}


def webhook_peers(routes):
    return {k: v for k, v in (routes.get("webhook_peers") or {}).items() if not k.startswith("_") and v}


def bot_role_map(routes):
    """role id -> bot user id, from routes.json `bot_roles`. A bot that joined
    via an integration owns a managed role of the same name, and Discord's
    autocomplete offers THAT for "@<bot name>" — so a human pinging the bot
    writes <@&roleid>, not <@userid>, and a user-mention-only match never sees
    it (t-729: the owner pinged the bot four times and woke nothing). Only roles listed here resolve; @everyone and people-roles never
    summon."""
    bots = routes.get("bots", {})
    out = {}
    for key, rids in (routes.get("bot_roles") or {}).items():
        uid = bots.get(key)
        if not uid:
            continue
        for rid in (rids if isinstance(rids, list) else [rids]):
            out[str(rid)] = uid
    return out


def route_entry(routes, chan):
    """Effective route for a channel or thread dict: explicit thread entry,
    else the parent channel's entry, else {} (mentions-only, no peer)."""
    cid = chan["id"]
    if chan.get("is_thread"):
        ent = routes.get("threads", {}).get(cid)
        if ent is not None:
            return ent
        return routes.get("channels", {}).get(chan.get("parent_id"), {})
    return routes.get("channels", {}).get(cid, {})


def explicit_mentions(msg, bot_roles=None):
    """User ids written as <@id> in the text — NOT the automatic reply-ping
    (Discord adds the replied-to author to `mentions` without putting <@id>
    in content), NOT roles, NOT @everyone. The message's resolved `mentions`
    list is the authority that the id is a real user; the text says it was
    deliberate. (The peer contract: reply pings are context, not a summons.)

    Role mentions count ONLY for a bot's own managed role (bot_roles in
    routes.json), resolved to that bot's user id: `mention_roles` is the
    authority the role was really pinged, the text says it was deliberate.
    @everyone and every other role stay what they were — not a summons."""
    resolved = {u.get("id") for u in msg.get("mentions", [])}
    content = msg.get("content", "")
    ids = {m for m in _MENTION_RE.findall(content) if m in resolved}
    if bot_roles:
        pinged = {str(r) for r in msg.get("mention_roles", [])}
        ids |= {bot_roles[r] for r in _ROLE_MENTION_RE.findall(content)
                if r in pinged and r in bot_roles}
    return ids


def venue_of(chan):
    return chan.get("parent_id") if chan.get("is_thread") else chan.get("id")


def members_of(msgs):
    """The people.json people behind a batch, as slugs (agents and strangers
    aren't members): the board's who-is-where (t-1423 step 6)."""
    out = set()
    for m in msgs:
        slug, rec = WHO.get(str((m.get("author") or {}).get("id")), (None, None))
        if slug and not rec["agent"]:
            out.add(slug)
    return sorted(out)


def owner_line():
    """Who the owner is, for the brief: the first tier-3 person's label and id
    from people.json (`discord-owner.md`), or `discord-no-owner.md`."""
    for uid, (_, rec) in sorted(WHO.items()):
        if not rec["agent"] and rec.get("tier") == 3:
            return INSTANCE.brief("discord-owner", label=person_label(rec), id=uid)
    return INSTANCE.brief("discord-no-owner")


# The floor under a guest venue's rules is the instance's `discord-guest.md`:
# the rules themselves live in the agent's memory (identity + cards, t-1423
# step 6.5), where a lane rewriting a card or the self-model could drop one by
# accident; the floor holds regardless. It can't be skipped: main() refuses to
# start without the file (check(), required), and render_brief refuses a guest
# venue whose cards are unread or whose rendered guest brief is empty.


# A guest venue's readers (routes.json `readers`, loom person-card slugs) and
# their cards: a body spawned there always carries them, or isn't spawned.
READER_CARD_TTL_S = 300
_CARDS = {}          # slug -> (fetched at, rendered card); a stale one beats none
_CARDS_PAGED = set()


def reader_cards_for(routes, ch):
    """The rendered cards of a guest venue's readers, or None if the venue
    lists none or any card can't be read (now or earlier in this process)."""
    slugs = route_entry(routes, ch).get("readers") or []
    out = []
    for slug in slugs:
        hit = _CARDS.get(slug)
        if hit is None or time.time() - hit[0] >= READER_CARD_TTL_S:
            text = person_cards.loom_card(slug, loom=INSTANCE.commands.get("loom"))
            if text is not None:
                hit = _CARDS[slug] = (time.time(), text)
        if hit is None:
            return None
        out.append(hit[1])
    return "\n\n".join(out) or None


def guest_venue_unready(ch):
    """Why a body may not be spawned in this guest venue yet, or None. Pages
    once per venue per process."""
    if not ch.get("guest_venue") or ch.get("reader_cards"):
        return None
    why = "guest venue without its readers' cards"
    print(f"hold #{ch.get('name')}: {why} (routes.json readers, loom person cards)", flush=True)
    if ch["id"] not in _CARDS_PAGED:
        _CARDS_PAGED.add(ch["id"])
        page(f"{SLUG}-discord: #{ch.get('name')} is a guest venue and its readers' loom cards "
             "can't be read; no body spawns there until they can.")
    return why


def human_allowed(aid, venue):
    """Tier 2+ people and the peer bots anywhere; a guest only inside its venues."""
    return may_wake(WHO.get(aid, (None, None))[1], venue)[0]


def route_reason(msg, entry, peers, venue=None, bot_roles=None):
    """Why this message wakes a body here, or None if it does not.
    Returns 'human' | 'human-mention' | 'peer' | None."""
    author = msg.get("author", {})
    aid = author.get("id")
    if aid == BOT_USER_ID:
        return None
    wh = msg.get("webhook_id")
    if wh is not None and (wh not in WEBHOOK_PEERS or aid != wh):
        return None
    if msg.get("type") not in (0, 19):      # DEFAULT and REPLY
        return None
    if not author.get("bot", False) and not human_allowed(aid, venue):
        return None                          # stranger, or a guest outside its venue
    explicit = explicit_mentions(msg, bot_roles)
    if author.get("bot", False):
        if aid not in peers:
            return None
        if entry.get("peer") and BOT_USER_ID in explicit:
            return "peer"
        # Pair channel (t-1053): a DM the owner can read, so any post from the
        # paired webhook peer wakes the primary, no mention.
        if (entry.get("peer") and entry.get("primary") == SLUG and entry.get("pair")
                and WEBHOOK_PEERS.get(aid) == entry["pair"]):
            return "peer"
        return None
    if BOT_USER_ID in explicit:
        return "human-mention"
    if explicit & peers:
        return None                          # addressed to the peer only
    return "human" if entry.get("primary") == SLUG else None


# Human stop (t-591; a peer's rule, letters 2026-09-12T18:27Z). In a peer venue a
# human message that is ONLY "stop" or "cancel" (mentions and trailing ! or .
# aside) halts the exchange there, whoever it is addressed to: the venue's
# bridge-owned bodies are killed, held/queued peer batches are dropped, and
# every later peer batch is skipped — persisted in state.peer_stopped, so a
# restart stays stopped. It is not a summons: no body is spawned for it, the
# transport posts one line. Any allowed human non-stop message in the venue
# lifts it (and renews the peer budget) — routable or not, see `attended`. Per venue: a stop in a channel does
# not reach its threads, nor a thread its parent.
_STOP_RE = re.compile(r"^(stop|cancel)[.!]*$", re.IGNORECASE)


def is_stop(msg):
    text = _ANY_MENTION_RE.sub("", msg.get("content", "")).strip()
    return is_human(msg) and bool(_STOP_RE.match(text))


def halt_venue(state, ch):
    """Kill this venue's bridge-owned bodies and say so once. Returns kills."""
    owned = state.setdefault("owned", {})
    killed = 0
    for aid, rec in list(owned.items()):
        if isinstance(rec, dict) and rec.get("chan") == ch["id"]:
            kill_agent(aid)
            inbox.close(aid)   # a stop drops what was waiting for it too
            state.setdefault("inbox_gc", []).append(aid[:8])
            _release_spawned_msgs(state, rec)
            _clear_ack(rec)
            owned.pop(aid, None)
            killed += 1
    state.get("leftover", {}).pop(ch["id"], None)
    state["retry_queue"] = [r for r in state.get("retry_queue", []) if r.get("chan") != ch["id"]]
    print(f"human stop in #{ch['name']}: killed {killed} body(s), peer turns held", flush=True)
    subprocess.run([str(DISCORD_SEND), ch["id"],
                    f"({INSTANCE.agent_tag}) stopped here{f' — ended {killed} running body' if killed else ''}. "
                    "I won't take peer turns in this venue until a human message that isn't stop/cancel."],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, env=BRIDGE_SEND_ENV)
    return killed

_TOKEN = []   # read on first use (main() reads it at start), so importing needs no secrets


def token():
    if not _TOKEN:
        _TOKEN.append(INSTANCE.read_secret(DISCORD.get("token_file") or "discord-token").strip())
    return _TOKEN[0]


def api(path, method="GET", body=None):
    headers = {"Authorization": f"Bot {token()}", "User-Agent": f"{SLUG}-discord/1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}   # 204 No Content on PUT/DELETE/POST-typing


def react(chan_id, msg_id, add=True):
    """Add or remove the bot's ACK_EMOJI on a message. Idempotent; never raises."""
    if not (chan_id and msg_id):
        return
    emoji = urllib.parse.quote(ACK_EMOJI)
    try:
        api(f"/channels/{chan_id}/messages/{msg_id}/reactions/{emoji}/@me",
            method="PUT" if add else "DELETE")
    except Exception as e:
        print(f"react {'add' if add else 'remove'} {chan_id}/{msg_id}: {e!r}", flush=True)


def typing(chan_id):
    try:
        api(f"/channels/{chan_id}/typing", method="POST")
    except Exception as e:
        print(f"typing {chan_id}: {e!r}", flush=True)


def presence_loop():
    """Daemon thread: every TYPING_EVERY seconds, pulse typing in each channel
    whose owned body is still working and owes a reply; each 👀 (the spawn's,
    and one per batch handed over mid-turn, t-1409) drops once the body has
    posted after it (sent.log) — the reply is the acknowledgment now.
    Reads state.json without the lock (writes are atomic replaces) and keeps
    its own memory of which acks it already cleared."""
    cleared = set()
    while True:
        try:
            owned = load_state().get("owned", {})
            for aid, rec in owned.items():
                if not isinstance(rec, dict) or not rec.get("chan"):
                    continue
                if rec.get("idle_since") != 0 or not rec.get("busy", True):
                    continue
                acks = [(rec.get("ack_msg"), rec.get("spawned_at", 0))] + \
                       [(a.get("msg"), a.get("at", 0)) for a in rec.get("acks", [])]
                owes = False
                posted = last_post_ts(aid)
                read_at = {it.get("id"): it.get("rt") for it in inbox.delivered(aid)}
                for i, (msg, at) in enumerate(acks):
                    if (aid, msg) in cleared:
                        continue
                    # the spawn's ack: any post by the job (or the channel fallback);
                    # a handed batch's ack: a post by the job after it was handed over
                    if (session_replied(aid, rec["chan"], at) if i == 0
                            else inbox.answered(posted, read_at.get(msg))):
                        cleared.add((aid, msg))
                        react(rec["chan"], msg, add=False)
                    else:
                        owes = True
                if owes:
                    typing(rec["chan"])
            cleared = {k for k in cleared if k[0] in owned}   # forget dropped records
        except Exception as e:
            print(f"presence error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(TYPING_EVERY)


def load_state():
    return shuttle_core.load_state(_cfg(), ("cursors", "last_spawn"))


# The shared core (the shuttle library, t-1423). Bridge code calls these module-level
# names, never shuttle_core directly, so tests that patch them still intercept
# (the instance's shim test). The Config is built per call from this module's
# globals, which tests re-point at temp dirs; so is its runner (shuttle/runner.py:
# CLAUDE, JOBS_DIR, MODEL_RESOLVER, PREFERRED_MODEL, subprocess, time, secrets).
def _cfg():
    return ShuttleConfig(name="discord", state_dir=STATE_DIR,
                         state_file=STATE_FILE, state_lock=STATE_LOCK_FILE, sent_log=SENT_LOG,
                         send=DISCORD_SEND, send_env=BRIDGE_SEND_ENV, alert=ALERT,
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
CONV_NOUN = "channels"


def msg_key(m):
    return m.get("id")


def msg_order(m):
    return int(m["id"])


def rec_label(rec):
    return f"in #{rec.get('chan_name')}"


def rec_venue(rec):
    return rec.get("venue") or "?"


def rec_who(rec):
    return rec.get("who") or []


def batch_who(rec, batch):
    return participants(batch)


def item_text(m):
    return fmt(m)


def conv_label(conv):
    return f"#{conv['name']}"


def admit(state, cid, batch, source, ctx):
    ch = ctx["by_id"].get(cid)
    if ch is None:
        return "wait", "channel not found"   # a thread the lookup missed
    # a body already working here has its rules; only a new one waits for the cards
    unready = None if holding_body(state, cid) else guest_venue_unready(ch)
    return ("wait", unready) if unready else ("ok", ch)


def hand(state, conv, aid, batch):
    return hand_over(state, conv, aid, batch)


def on_handed(state, conv, batch, source):
    # new messages: the per-channel loop counts peer turns by route reason
    if source != "new" and all(m.get("author", {}).get("bot") for m in batch):
        count_spawn(state, conv["id"], batch)   # a handed peer turn is still a turn


def spend_blocked(state, conv, batch, ctx):
    return leftover_blocked(state, conv, batch, ctx["routes"])


def hold(state, holds, conv, batch, reason, rec=None, since=None):
    if rec is not None:
        _hold(state, holds, conv, batch, reason, rec.get("cid"), who_names=rec.get("who"),
              since=rec.get("spawned_at"))
    else:
        _hold(state, holds, conv, batch, reason, since=since)


def spawn(state, conv, batch, attempt, source, ctx):
    context = api(f"/channels/{conv['id']}/messages?limit={CONTEXT_MESSAGES}")
    spawn_session(conv, batch, context, state, attempt=attempt)
    if source != "new":   # the per-channel loop spends new messages' turns itself
        count_spawn(state, conv["id"], batch)


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
    return oldest_epoch(batch, None)


def save_state(state):
    shuttle_core.save_state(_cfg(), state)


def state_locked():
    return shuttle_core.state_locked(_cfg())


def is_human(msg):
    author = msg.get("author", {})
    return (
        not author.get("bot", False)
        and author.get("id") != BOT_USER_ID
        and msg.get("webhook_id") is None
        and msg.get("type") in (0, 19)      # DEFAULT and REPLY
    )


def who(author):
    """Render an author as 'username [Pat]' or 'username [UNKNOWN id=…]'."""
    name = author.get("username", "?")
    uid = author.get("id", "")
    known = (PEOPLE.get(uid)
             or (f"{WEBHOOK_PEERS[uid]} (peer agent via webhook)" if uid in WEBHOOK_PEERS and author.get("bot") else None))
    return f"{name} [{known}]" if known else f"{name} [UNKNOWN id={uid}]"


def fmt(msg):
    author = who(msg.get("author", {}))
    content = msg.get("content", "")
    for att in msg.get("attachments", []):
        content += f" [attachment: {att.get('filename')} {att.get('url')}]"
    return f"{author}: {content}"


# --- Now board (t-1403): ids, names, counts, times, states, reasons — never message text.
def iso_epoch(ts):
    """Discord ISO 8601 timestamp -> epoch seconds; None if unparseable."""
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def participants(msgs):
    """Sorted unique author names of a batch: the bracketed name from who()
    when the author is known, else the username."""
    names = set()
    for msg in msgs:
        author = msg.get("author") or {}
        label = who(author)
        known = label[label.rfind("[") + 1:-1] if label.endswith("]") else ""
        names.add(author.get("username", "?") if (not known or known.startswith("UNKNOWN")) else known)
    return sorted(names)


def oldest_epoch(msgs, default):
    """Epoch of the oldest message in a batch (by snowflake), else default."""
    if not msgs:
        return default
    ids = [m for m in msgs if isinstance(m, dict) and str(m.get("id", "")).isdigit()]
    if not ids:
        return default
    return iso_epoch(min(ids, key=lambda x: int(x["id"])).get("timestamp")) or default


def chan_venue(ch):
    """Board venue label for a channel/thread dict (t-1403). Never message
    text: a Discord thread's title defaults to its starter message, so a thread
    is "#<parent> thread" (or bare "thread"), never its own name. A tick sets
    ch["venue"] once the parent is known."""
    try:
        if ch.get("venue"):
            return ch["venue"]
        if ch.get("is_thread"):
            return "thread"
        return "#" + str(ch.get("name") or "?")
    except Exception:
        return "?"


def resolve_thread_venues(channels):
    """Stamp ch["venue"] on every channel dict; threads get their parent's name."""
    by_id = {c.get("id"): c for c in channels}
    for c in channels:
        try:
            if c.get("is_thread"):
                parent = by_id.get(c.get("parent_id"))
                pname = parent.get("name") if parent and not parent.get("is_thread") else None
                c["venue"] = f"#{pname} thread" if pname else "thread"
            else:
                c["venue"] = "#" + str(c.get("name") or "?")
        except Exception:
            c["venue"] = "?"


def talking_items(state):
    return shuttle_core.talking_items(_B, state)


def _hold(state, holds, ch, batch, reason, cid=None, who_names=None, since=None):
    """Record a waiting channel for the snapshot; emit `wait` on a new reason.
    Every field is computed in here so a bad message cannot raise out of tick()."""
    try:
        batch = batch or []
        shuttle_core.record_hold(_B, state, holds, cid=cid, chan=ch["id"], venue=chan_venue(ch),
                                 who=who_names or participants(batch), n=len(batch),
                                 since=oldest_epoch(batch, since or time.time()), reason=reason)
    except Exception as e:
        print(f"now board: hold: {e!r}", file=sys.stderr, flush=True)


def _finished(aid, rec, why=None):
    shuttle_core.finished(_B, aid, rec, why)


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


def last_post_ts(aid):
    return shuttle_core.last_post_ts(_cfg(), aid)


def death_reason(aid, rec):
    return shuttle_core.death_reason(_B, aid, rec)


def session_died_silent(aid, rec):
    return shuttle_core.session_died_silent(_B, aid, rec)


def job_quiet_s(aid):
    return shuttle_core.job_quiet_s(_cfg(), aid)


def job_parked(aid):
    return shuttle_core.job_parked(_cfg(), aid)


def prune_spawned_msgs(state):
    shuttle_core.prune_spawned_msgs(state)


def _clear_ack(rec):
    shuttle_core.clear_ack(_B, rec)


def _release_spawned_msgs(state, rec):
    shuttle_core.release_spawned_msgs(_B, state, rec)


def job_done(aid):
    """The session's job says its turn is over (state "done"): it ended itself."""
    return shuttle_core.job_done(_cfg(), aid)


def _drop(state, aid, rec, retry=None, ended=False):
    shuttle_core.drop(_B, state, aid, rec, retry, ended)


def _drop_inner(state, aid, rec, retry, ended):
    shuttle_core.drop_inner(_B, state, aid, rec, retry, ended)


def notify_hung(chan, quiet):
    shuttle_core.notify_hung(_cfg(), chan, quiet)


def reap_and_count(state):
    return shuttle_core.reap_and_count(_B, state)


LEFTOVER_TTL_S = 3600          # a retry or leftover whose channel can't be found is dropped after this


def inbox_sweep(state):
    shuttle_core.inbox_sweep(_B, state)


def leftover_blocked(state, ch, batch, routes):
    """The spawn gates the per-channel loop applies, for a leftover batch:
    a stopped peer venue, the peer-turn budget, a guest's daily cap. Returns a
    reason to drop the batch, or None."""
    cid = ch["id"]
    if all(m.get("author", {}).get("bot") for m in batch):
        if cid in state.get("peer_stopped", {}):
            return "peer venue stopped"
        budget = int(routes.get("peer_turns_each") or 0)
        if budget > 0 and state.get("peer_turns", {}).get(cid, 0) >= budget:
            return "peer budget exhausted"
    gids = {m.get("author", {}).get("id") for m in batch if not m.get("author", {}).get("bot")}
    if gids and gids <= set(GUESTS):
        gid = sorted(gids)[0]
        cap = int(GUESTS[gid].get("daily_spawns", 30))
        if state.get("guest_spawns", {}).get(time.strftime("%Y-%m-%d"), {}).get(gid, 0) >= cap:
            return "guest daily cap"
    return None


def count_spawn(state, cid, batch):
    """Spend what a spawn spends: a peer turn, or a guest's daily spawn."""
    if all(m.get("author", {}).get("bot") for m in batch):
        turns = state.setdefault("peer_turns", {})
        turns[cid] = turns.get(cid, 0) + 1
    gids = {m.get("author", {}).get("id") for m in batch if not m.get("author", {}).get("bot")}
    if gids and gids <= set(GUESTS):
        gid, day = sorted(gids)[0], time.strftime("%Y-%m-%d")
        g = state.setdefault("guest_spawns", {}).setdefault(day, {})
        g[gid] = g.get(gid, 0) + 1


def holding_body(state, cid):
    return shuttle_core.holding_body(state, cid)


def hand_over(state, ch, aid, batch):
    return shuttle_core.hand_over(_B, state, ch["id"], aid, batch)


def channel_busy(state, cid):
    return shuttle_core.channel_busy(_B, state, cid)


def notify_failure(chan, why="upstream API error", parked=False):
    shuttle_core.notify_failure(_cfg(), chan, why, parked)


def mem_available_mb():
    return nowlog.mem_available_mb()


def resolve_model():
    return shuttle_spawn.resolve_model(_B)


def selvedge_index(doc, platform, team=None, sections=("agents", "people")):
    return shuttle_selvedge.index(doc, platform, team, sections)


def may_wake(rec, venue=None):
    return shuttle_selvedge.may_wake(rec, venue)


def person_label(rec):
    return shuttle_selvedge.label(rec)


def person_known(rec):
    return shuttle_selvedge.known(rec)


# The briefs this bridge renders, and the slots it gives each (t-1436). main()
# passes this to INSTANCE.check() at start, with the channel notes' briefs
# added (they get no slots), so a missing file or a misspelled slot stops the
# bridge before it reaches a person.
BRIEF_SLOTS = {
    "discord": ("where", "chan_id", "chan_name", "new", "context", "send", "owner_line", "notes"),
    "discord-owner": ("label", "id"),
    "discord-no-owner": (),
    "discord-guest": ("cards", "send", "notify_chan"),
    "discord-peer": ("peers", "pair"),
    "discord-peer-bot": ("label", "name", "note", "uid"),
    "discord-peer-webhook": ("name",),
    "discord-pair": ("name",),
}
REQUIRED_BRIEFS = ("discord", "discord-guest")
# Slots a brief must actually use: without `notes` the guest floor, cards and
# channel notes vanish from every prompt; without `cards` a guest venue's body
# runs without its readers' cards.
BRIEF_USES = {"discord": ("notes",), "discord-guest": ("cards",)}


def brief_slots():
    return {**BRIEF_SLOTS, **{brief: () for brief, _ in CHANNEL_NOTES}}


def render_brief(channel, new_msgs, context_msgs):
    """The prompt a body spawned for this batch reads, and the tools it may not
    use beyond the instance's always_deny: (prompt, deny). Spawns nothing.
    `context_msgs` is newest first, as the channel API returns it.

    Conduct lives in the agent's memory (t-1423 step 6.5): the brief names which
    rules apply and carries only the mechanics. Slot values are substituted
    once and never re-templated, so a `$` in a message stays verbatim."""
    chan_id, chan_name = channel["id"], channel["name"]
    context = "\n".join(fmt(m) for m in reversed(context_msgs))
    new = "\n".join(fmt(m) for m in new_msgs)
    send = str(DISCORD_SEND)
    notes = "".join(INSTANCE.brief(brief) for brief, chans in CHANNEL_NOTES
                    if chan_id in chans or channel.get("parent_id") in chans)
    deny = []
    guest = bool(channel.get("guest_venue"))
    if guest:
        # never a guest venue without its rules (the readers' cards and the
        # floor) or without its narrowed tools
        if not channel.get("reader_cards"):
            raise RuntimeError(f"#{chan_name} is a guest venue and its readers' cards are unread")
        if not INSTANCE.guest_deny:
            raise RuntimeError(f"#{chan_name} is a guest venue and the instance's guest_deny is empty")
        guest_note = INSTANCE.brief("discord-guest", cards=channel["reader_cards"], send=send,
                                    notify_chan=NOTIFY_CHANNEL)
        if not guest_note.strip():
            raise RuntimeError(f"#{chan_name} is a guest venue and the guest brief is empty")
        notes += guest_note
        deny = list(INSTANCE.guest_deny)
    if channel.get("peer"):
        peers = "".join(
            INSTANCE.brief("discord-peer-bot", label=person_label(rec), name=rec.get("name"),
                           note=rec.get("note") or rec.get("kind"), uid=uid)
            for uid, (_, rec) in sorted(WHO.items()) if rec["agent"])
        peers += "".join(INSTANCE.brief("discord-peer-webhook", name=name)
                         for name in sorted(set(WEBHOOK_PEERS.values())))
        pair = INSTANCE.brief("discord-pair", name=channel["pair"]) if channel.get("pair") else ""
        notes += INSTANCE.brief("discord-peer", peers=peers, pair=pair)
    where = f"thread #{chan_name}" if channel.get("is_thread") else f"channel #{chan_name}"
    prompt = INSTANCE.brief("discord", where=where, chan_id=chan_id, chan_name=chan_name,
                            new=new, context=context, send=send, owner_line=owner_line(),
                            notes=notes)
    if guest and channel["reader_cards"] not in prompt:
        raise RuntimeError(f"#{chan_name} is a guest venue and its brief dropped the readers' cards")
    return prompt, deny


def spawn_session(channel, new_msgs, context_msgs, state, attempt=1):
    chan_id, chan_name = channel["id"], channel["name"]
    prompt, deny = render_brief(channel, new_msgs, context_msgs)
    try:
        who_names = participants(new_msgs)
    except Exception as e:
        print(f"now board: participants: {e!r}", file=sys.stderr, flush=True)
        who_names = []
    venue = chan_venue(channel)
    spawn_body(state, {
        "chan": chan_id, "label": f"#{chan_name}", "venue": venue, "inbox_label": venue,
        "who": who_names, "members": members_of(new_msgs), "prompt": prompt,
        "deny": deny, "env": None,
        "rec": {"chan_name": chan_name, "venue": venue, "who": who_names},
    }, new_msgs, attempt)


def active_threads(routes):
    """Public/private threads currently active in the guild, as channel-like
    dicts (id, name, parent_id, is_thread, peer). Archived threads drop off
    this list; a message that revives one is picked up on the next tick
    because Discord un-archives it. Threads under unpolled parents are skipped."""
    try:
        data = api(f"/guilds/{GUILD_ID}/threads/active")
    except Exception as e:
        print(f"threads/active: {e!r}", flush=True)
        return []
    out = []
    for t in data.get("threads", []):
        if t.get("type") not in (11, 12) or not polled(t.get("parent_id")):
            continue
        th = {"id": t["id"], "name": t.get("name", "?"), "parent_id": t.get("parent_id"),
              "is_thread": True, "created": t.get("thread_metadata", {}).get("create_timestamp")}
        th["peer"] = bool(route_entry(routes, th).get("peer"))
        th["pair"] = route_entry(routes, th).get("pair")
        th["guest_venue"] = bool(route_entry(routes, th).get("guest_venue"))
        out.append(th)
    return out


def snowflake_age_s(sid):
    try:
        return time.time() - (((int(sid) >> 22) + 1420070400000) / 1000)
    except (TypeError, ValueError):
        return 10**9


def tick(state):
    prune_spawned_msgs(state)   # expire old dedup entries (2h TTL, safety net)
    routes = load_routes()
    load_who(page_if_empty=True)
    WEBHOOK_PEERS.clear()
    WEBHOOK_PEERS.update(webhook_peers(routes))
    peers = peer_bot_ids(routes)
    bot_roles = bot_role_map(routes)   # "@<bot>" autocompletes to its managed role
    channels = [
        c for c in api(f"/guilds/{GUILD_ID}/channels")
        if c["type"] == 0 and polled(c["id"])
    ]
    for c in channels:
        c["peer"] = bool(route_entry(routes, c).get("peer"))
        c["pair"] = route_entry(routes, c).get("pair")
        c["guest_venue"] = bool(route_entry(routes, c).get("guest_venue"))
    channels += active_threads(routes)
    inbox_sweep(state)
    live, retries = reap_and_count(state)
    resolve_thread_venues(channels)
    for c in channels:
        if c.get("guest_venue"):
            c["reader_cards"] = reader_cards_for(routes, c)
    holds = []   # now board: what is waiting and why (t-1403)
    ctx = {"by_id": {c["id"]: c for c in channels}, "routes": routes}   # channels + active threads
    live = run_retries(state, holds, live, retries, ctx)
    live = run_leftovers(state, holds, live, ctx)
    for ch in channels:
        cid = ch["id"]
        cursor = state["cursors"].get(cid)
        if cursor is None:
            # First sight: set cursor to latest without replaying history —
            # except a thread young enough that its opening message IS the
            # news (a thread id is a snowflake older than every message in it).
            if ch.get("is_thread") and snowflake_age_s(cid) < THREAD_FIRST_SIGHT_S:
                state["cursors"][cid] = cid
            else:
                latest = api(f"/channels/{cid}/messages?limit=1")
                state["cursors"][cid] = latest[0]["id"] if latest else "0"
                continue
            cursor = state["cursors"][cid]
        try:
            msgs = api(f"/channels/{cid}/messages?after={cursor}&limit=100")
        except urllib.error.HTTPError as e:
            print(f"poll #{ch['name']}: HTTP {e.code}", flush=True)
            continue
        if not msgs:
            continue
        msgs.sort(key=lambda m: int(m["id"]))
        entry = route_entry(routes, ch)
        reasons = {m["id"]: route_reason(m, entry, peers, venue_of(ch), bot_roles) for m in msgs}
        turns = state.setdefault("peer_turns", {})
        stopped = state.setdefault("peer_stopped", {})
        start = 0
        if ch.get("peer"):
            stops = [i for i, m in enumerate(msgs)
                     if is_stop(m) and human_allowed(m["author"].get("id"), venue_of(ch))]
            if stops:
                if cid not in stopped:
                    live = max(0, live - halt_venue(state, ch))
                stopped[cid] = msgs[stops[-1]]["id"]
                start = stops[-1] + 1
                # Past the stop now, so a later deferral never re-handles it.
                state["cursors"][cid] = msgs[stops[-1]]["id"]
        human = [m for m in msgs[start:] if reasons[m["id"]]]
        # "Attended" = any allowed human spoke in the venue, routable or not.
        # In a peer-primary venue the owner's plain messages route to nobody
        # (they are the peer's), and a role ping is not a user mention — so the
        # budget/stop tests below must NOT go through route_reason, or the
        # backstop's own "a message from <owner> resets it" is a lie there
        # (2026-09-15: a peer venue sat muzzled through eight of the owner's messages).
        attended = any(is_human(m) and human_allowed(m["author"].get("id"), venue_of(ch))
                       for m in msgs[start:])
        if cid in stopped:
            if attended:
                print(f"stop lifted in #{ch['name']} by a human message", flush=True)
                stopped.pop(cid)
            else:
                state["cursors"][cid] = msgs[-1]["id"]
                continue
        if attended:
            turns.pop(cid, None)
            state.get("peer_budget_notified", {}).pop(cid, None)
        if not human:
            state["cursors"][cid] = msgs[-1]["id"]
            continue
        # Peer-exchange budget, enforced by the transport (t-591): a batch that
        # is ONLY peer turns spends one of ours; any allowed human message in
        # the venue (see `attended`) renews the budget. Persisted in state, so
        # a restart never buys another round.
        # null/0 disables it (the owner, 2026-10-02: "Disable that thing");
        # the human brake is still the stop word (is_stop above).
        budget = int(routes.get("peer_turns_each") or 0)
        if budget > 0 and all(reasons[m["id"]] == "peer" for m in human):
            if turns.get(cid, 0) >= budget:
                print(f"peer budget exhausted in #{ch['name']} ({budget}); not spawning", flush=True)
                if not state.setdefault("peer_budget_notified", {}).get(cid):
                    subprocess.run([str(DISCORD_SEND), cid,
                                    f"({INSTANCE.agent_tag}) loop backstop: {budget} of my turns here without a human message; "
                                    f"I'm stopping. A message from {INSTANCE.owner_name} in this venue resets it."],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, env=BRIDGE_SEND_ENV)
                    state["peer_budget_notified"][cid] = True
                state["cursors"][cid] = msgs[-1]["id"]
                continue
        # Per-message-ID dedup guard: a crash before the cursor advanced re-polls
        # messages that already spawned a body.
        human = dedup(state, human, f"#{ch['name']}")
        if not human:
            state["cursors"][cid] = msgs[-1]["id"]
            continue
        # Guest daily cap (t-693): a batch whose humans are ALL guests spends one
        # of that guest's daily spawns; the owner in the batch never counts.
        guest_ids = {m["author"].get("id") for m in human if reasons[m["id"]] != "peer"}
        guest_batch = bool(guest_ids) and guest_ids <= set(GUESTS)
        day = time.strftime("%Y-%m-%d")
        gcount = state.setdefault("guest_spawns", {})
        for k in [k for k in gcount if k != day]:
            gcount.pop(k)
        if guest_batch:
            gid = sorted(guest_ids)[0]
            cap = int(GUESTS[gid].get("daily_spawns", 30))
            if gcount.get(day, {}).get(gid, 0) >= cap:
                note = state.setdefault("guest_cap_notified", {})
                if note.get(gid) != day:
                    subprocess.run([str(DISCORD_SEND), cid,
                                    f"({INSTANCE.agent_tag}) {GUEST_CAP_NOTE}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, env=BRIDGE_SEND_ENV)
                    note[gid] = day
                print(f"guest cap reached for {gid} in #{ch['name']} ({cap}/day); not spawning", flush=True)
                state["cursors"][cid] = msgs[-1]["id"]
                continue
        # To the body working this channel (it hears these mid-turn, t-1409),
        # else held (the cursor stays, so a later tick picks them up), else a
        # fresh body.
        if not holding_body(state, cid) and guest_venue_unready(ch):
            continue   # leave the cursor: the next tick tries again
        outcome, live = offer(state, holds, live, ch, human, ctx)
        if outcome == "held":
            continue
        if outcome == "spawned" and guest_batch:
            gcount.setdefault(day, {})[gid] = gcount.get(day, {}).get(gid, 0) + 1
        if all(reasons[m["id"]] == "peer" for m in human):
            turns[cid] = turns.get(cid, 0) + 1   # a handed peer turn is still a turn
        state["cursors"][cid] = msgs[-1]["id"]
    try:
        held = {h["chan"] for h in holds}
        state["now_wait"] = {c: r for c, r in state.get("now_wait", {}).items() if c in held}
        nowlog.write_now("discord", nowlog.snapshot(
            "discord", POLL_SECONDS, MAX_LIVE, mem_available_mb(), talking_items(state), holds))
    except Exception as e:
        print(f"now board: snapshot: {e!r}", file=sys.stderr, flush=True)
    save_state(state)
    # Idle and behind: the main loop stops after this tick; cursors are saved, so nothing is missed.
    why = UPGRADE.tick(talking_items(state), holds)
    if why:
        UPGRADING.append(why)


def page(msg):
    shuttle_core.page(_cfg(), msg)


# The instance's extension tick (t-1436): behaviour only one instance has, run
# once per tick under the state lock, before the bridge's own tick. It gets
# this module as `bridge` (the _B proxy: api, save_state, is_human,
# human_allowed, INSTANCE, ...). A hook that raises never takes the tick down
# with it: it is logged every time and paged once per distinct error.
_EXT_PAGED = set()


def _error_site(e):
    """An exception's type and innermost frame: the same failure gives the same
    key even when its message carries a timestamp or an id."""
    tb = traceback.extract_tb(e.__traceback__)
    where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "?"
    return f"{type(e).__name__}@{where}"


def run_ext_tick(state):
    try:
        INSTANCE.ext().tick(_B, state)
    except Exception as e:
        print(f"ext tick error: {e!r}", file=sys.stderr, flush=True)
        key = _error_site(e)
        if key not in _EXT_PAGED:
            _EXT_PAGED.add(key)
            page(f"{SLUG}-discord: the instance's ext tick failed: {e!r}. "
                 "The bridge's own tick goes on; this error won't page again.")


def tick_once():
    with state_locked():
        # Re-read from disk inside the lock so that if two bridge
        # processes somehow overlap (e.g. a systemd restart race),
        # the second writer always sees the first's committed state
        # rather than its own stale in-memory copy.  The lock is held
        # for the full read-modify-write cycle through save_state().
        state = load_state()
        run_ext_tick(state)
        tick(state)


def startup_check():
    """Everything the bridge needs from the instance, checked once, dry (no
    network, nothing written). Each item fails closed at runtime too, but
    silently or for every tick; here it stops the start with a reason:
    the briefs render with the slots this bridge gives them and use the ones
    they must (check()); always_deny is stated (an explicit [] is allowed);
    the alert command can run; routes.json names this bot and every
    channel_notes key as a list; a guest venue has a guest_deny and a notify
    channel; the runner is one this shuttle has and can run here, and can deny
    what this bridge denies (codex: no always_deny, no guest venue); the ext
    module loads and its check(instance) finds nothing; the token is readable. Loading the briefs
    here also keeps them for the run (Instance._text).
    Returns a list of problems, [] when all is well."""
    path = INSTANCE.path
    errors = INSTANCE.check(brief_slots(), required=REQUIRED_BRIEFS, uses=BRIEF_USES)
    if DISCORD_SEND is None:
        errors.append(f"{path}: commands.send.discord is not set")
    if not isinstance(INSTANCE.raw.get("always_deny"), list):
        errors.append(f"{path}: always_deny is required: the tools no body may use "
                      "(a list; [] to deny nothing)")
    alert = INSTANCE.commands.get("alert")
    if not (isinstance(alert, Path) and alert.is_file() and os.access(alert, os.X_OK)):
        errors.append(f"{path}: commands.alert ({alert}) must be an executable file: "
                      "without it every page is lost")
    if not (_IDS.get("bots") or {}).get(SLUG):
        errors.append(f"{ROUTES_FILE}: bots.{SLUG} (this bot's own user id) is missing")
    for key, brief in INSTANCE.channel_notes.items():
        if key not in _IDS:
            errors.append(f"channel_notes: {ROUTES_FILE} has no key {key!r} (for {brief}.md)")
        elif not isinstance(_IDS[key], (list, dict)):
            errors.append(f"channel_notes: {ROUTES_FILE} {key!r} must be a list of channel ids "
                          "(or an object keyed by them)")
    guest_venues = [cid for section in ("channels", "threads")
                    for cid, ent in (_IDS.get(section) or {}).items()
                    if isinstance(ent, dict) and ent.get("guest_venue")]
    if guest_venues:
        if not INSTANCE.guest_deny:
            errors.append(f"{path}: guest_deny is empty and {ROUTES_FILE} has guest venues "
                          f"({', '.join(guest_venues)}): their bodies would keep every tool")
        if not NOTIFY_CHANNEL:
            errors.append(f"{ROUTES_FILE}: notify.{SLUG} is missing and there are guest venues: "
                          "their bodies would have nowhere to raise a worry")
    errors += INSTANCE.runner_errors()   # runner.kind is one this shuttle has, and can run here
    # A runner that can't deny tools (codex) refuses an always_deny or a guest venue
    # here, at start, not as a start() that raises every tick (t-1439 review C6).
    errors += INSTANCE.runner_deny_errors(guest_venues)
    errors += INSTANCE.ext_errors()   # the ext loads, and its own check(instance) passes
    try:
        token()
    except OSError as e:
        errors.append(f"discord token: {e!r}")
    return errors


def main():
    # This is a daemon, not a CLI: any argv means someone (probably a body
    # guessing at a read interface) is about to start a second bridge.
    if len(sys.argv) > 1:
        sys.exit(
            f"{SLUG}-discord takes no arguments — it is the bridge daemon "
            f"(one instance, run by {SLUG}-discord.service). To read a channel, "
            "use the Discord API or the instance's send command, not this script."
        )
    errors = startup_check()
    if errors:
        sys.exit(f"instance check failed: {INSTANCE.path}\n" + "\n".join(errors))
    print(f"instance check ok: {INSTANCE.path}", flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "bridge.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"{SLUG}-discord bridge already running (bridge.lock held) — refusing to double-spawn.")
    print(f"{SLUG}-discord bridge up: poll={POLL_SECONDS}s preferred={PREFERRED_MODEL} "
          f"(via {MODEL_RESOLVER})", flush=True)
    threading.Thread(target=presence_loop, name="presence", daemon=True).start()
    failed_ticks = 0
    while True:
        try:
            tick_once()
            if failed_ticks >= TICK_ERROR_ALERT_AFTER:
                page(f"{SLUG}-discord bridge recovered after {failed_ticks} failed ticks.")
            failed_ticks = 0
        except Exception as e:  # keep the loop alive through transient errors
            failed_ticks += 1
            print(f"tick error: {e!r}", file=sys.stderr, flush=True)
            if failed_ticks == TICK_ERROR_ALERT_AFTER:
                page(f"{SLUG}-discord bridge is deaf: {failed_ticks} consecutive tick errors "
                     f"(~{failed_ticks * POLL_SECONDS // 60} min), latest {e!r}. "
                     f"journalctl --user -u {SLUG}-discord.service")
        if UPGRADING:
            print(UPGRADING[0], flush=True)
            sys.exit(0)
        time.sleep(POLL_SECONDS)


UPGRADING = []   # set by a tick that's idle and behind: stop, and the service starts fresh

load_who()   # who is who from the first poll on; each tick reloads it


if __name__ == "__main__":
    main()
