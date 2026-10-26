"""The Discord adapter runs for any instance (t-1436): a generic agent, "kit",
with tiny briefs, no secrets on disk, and an extension that misbehaves."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shuttle import inbox, instance, nowlog

ROOT = Path(__file__).resolve().parent.parent

BRIEFS = {
    "discord": "kit in $where ($chan_id, #$chan_name) via $send\n$new\n--\n$context\n"
               "$owner_line\n${notes}END",
    "discord-owner": "[$label] is the owner ($id).",
    "discord-no-owner": "no owner.",
    "discord-guest": "GUEST($cards|$send $notify_chan)",
    "discord-peer": "PEERS(${peers}${pair})",
    "discord-peer-bot": "bot $label/$name/$note/$uid;",
    "discord-peer-webhook": "hook $name;",
    "discord-pair": "pair $name;",
    "discord-careful": "CAREFUL;",
}

PEOPLE = {"people": {"robin": {"name": "Robin", "tier": 3, "discord": ["11"]},
                     "kim": {"name": "Kim", "tier": 1, "discord": ["12"], "venues": ["31"]}},
          "agents": {"bolt": {"name": "Bolt", "kind": "peer agent bot", "discord": ["13"]}}}
ROUTES = {"guild": "1", "bots": {"kit": "19", "bolt": "13"}, "notify": {"kit": "39"},
          "careful": {"32": "careful room"}, "webhook_peers": {"14": "Hooky"},
          "channels": {"30": {"primary": "kit"}, "31": {"primary": "kit", "guest_venue": True},
                       "32": {"primary": "kit"}, "33": {"primary": "kit", "peer": True,
                                                        "pair": "Hooky"}}}


def make_instance(root, ext=None, routes=None, **doc_changes):
    """A "kit" instance in `root` that passes startup_check (a token, an
    executable alert). `doc_changes` replace shuttle.json keys; None drops one."""
    root = Path(root)
    (root / "briefs").mkdir(parents=True, exist_ok=True)
    for name, text in BRIEFS.items():
        (root / "briefs" / f"{name}.md").write_text(text)
    (root / "people.json").write_text(json.dumps(PEOPLE))
    (root / "routes.json").write_text(json.dumps(ROUTES if routes is None else routes))
    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "discord-token").write_text("tok\n")
    alert = root / "alert"
    alert.write_text("#!/bin/sh\n")
    alert.chmod(0o755)
    doc = {"agent": {"slug": "kit", "name": "Kit", "tag": "kit"}, "owner": {"name": "Robin"},
           "state_root": str(root / "state"), "secrets": str(root / "secrets"),
           "commands": {"send": {"discord": "/opt/kit/send"}, "alert": str(alert)},
           "always_deny": ["mcp__x"], "guest_deny": ["mcp__mail"],
           "channel_notes": {"careful": "discord-careful"}}
    if ext is not None:
        (root / "ext.py").write_text(ext)
        doc["ext"] = "ext.py"
    for k, v in doc_changes.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    p = root / "shuttle.json"
    p.write_text(json.dumps(doc))
    return p


def codex_ready(test):
    """A host a codex instance passes the startup check on: the shuttle's
    inbox hook, and the profile written for it in a CODEX_HOME of the test's."""
    from shuttle import codex_profile
    d = Path(tempfile.mkdtemp(prefix="shuttle-codex-home-"))
    test.addCleanup(shutil.rmtree, d, True)
    hook = d / "inbox_hook.py"
    hook.write_text("")
    codex_profile.write(d / "home", "shuttle", hook)
    for p in (mock.patch.dict(os.environ, {"CODEX_HOME": str(d / "home")}),
              mock.patch.object(codex_profile, "DEFAULT_HOOK", hook)):
        p.start()
        test.addCleanup(p.stop)


def load_adapter(path):
    """Import adapters/discord.py against the instance at `path`, leaving the
    process-wide nowlog/inbox configuration as it found it."""
    saved = (dict(nowlog._configured), dict(inbox._configured))
    try:
        with mock.patch.dict(os.environ, {"SHUTTLE_INSTANCE": str(path)}):
            spec = importlib.util.spec_from_file_location("discord_under_test",
                                                          ROOT / "adapters" / "discord.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
    finally:
        nowlog._configured.clear()
        nowlog._configured.update(saved[0])
        inbox._configured.clear()
        inbox._configured.update(saved[1])
    m.page = mock.Mock()
    m.spawn_body = mock.Mock()
    return m


def msg(n, uid, text, name="u"):
    return {"id": str(n), "type": 0, "content": text, "author": {"id": uid, "username": name}}


class DiscordAdapter(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-discord-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load_adapter(make_instance(self.root))

    def chan(self, cid, **kw):
        return dict({"id": cid, "name": f"c{cid}"}, **kw)

    def reload(self, routes=None, **doc_changes):
        """The adapter against a fresh instance in a new dir."""
        root = Path(tempfile.mkdtemp(prefix="shuttle-discord-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.root = root
        self.m = load_adapter(make_instance(root, routes=routes, **doc_changes))
        return self.m

    def test_bridge_posts_are_logged_as_the_bridge_not_a_body(self):
        # the send command logs SHUTTLE_BODY first (t-1439), so the bridge sets it too
        self.assertEqual(self.m.BRIDGE_SEND_ENV["SHUTTLE_BODY"], "bridge")
        self.assertEqual(self.m.BRIDGE_SEND_ENV["CLAUDE_JOB_DIR"], "bridge")

    def test_import_needs_no_secrets_and_reads_the_instance(self):
        shutil.rmtree(self.root / "secrets")
        m = self.m
        self.assertEqual((m.SLUG, m.BOT_USER_ID, m.GUILD_ID, m.NOTIFY_CHANNEL), ("kit", "19", "1", "39"))
        self.assertEqual(m.STATE_DIR, self.root / "state" / "kit-discord")
        self.assertEqual(m.ROUTES_FILE, self.root / "routes.json")
        cfg = m._cfg()
        self.assertEqual((cfg.tag, cfg.agent, cfg.always_deny), ("kit", "kit", ("mcp__x",)))
        with self.assertRaises(OSError):
            m.token()   # read when first needed, not at import
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "discord-token").write_text("tok\n")
        self.assertEqual(m.token(), "tok")
        self.assertEqual(m.startup_check(), [])

    def test_render_brief_for_a_generic_instance(self):
        prompt, deny = self.m.render_brief(self.chan("30"), [msg(2, "11", "hello", "rob")],
                                           [msg(1, "77", "older", "x"), msg(0, "11", "oldest", "rob")])
        self.assertEqual(prompt, "kit in channel #c30 (30, #c30) via /opt/kit/send\n"
                                 "rob [Robin]: hello\n--\n"
                                 "rob [Robin]: oldest\nx [UNKNOWN id=77]: older\n"
                                 "[Robin] is the owner (11).\nEND")
        self.assertEqual(deny, [])

    def test_channel_notes_peers_and_pair(self):
        m = self.m
        m.WEBHOOK_PEERS.update(m.webhook_peers(ROUTES))
        prompt, _ = m.render_brief(self.chan("32"), [], [])
        self.assertTrue(prompt.endswith("\nCAREFUL;END"))
        prompt, _ = m.render_brief(self.chan("40", is_thread=True, parent_id="32"), [], [])
        self.assertTrue(prompt.endswith("\nCAREFUL;END"), "a thread under a noted channel")
        prompt, _ = m.render_brief(self.chan("33", peer=True, pair="Hooky"), [], [])
        self.assertTrue(prompt.endswith(
            "\nPEERS(bot Bolt (peer agent bot)/Bolt/peer agent bot/13;hook Hooky;pair Hooky;)END"))

    def test_a_guest_venue_carries_its_floor_and_deny(self):
        prompt, deny = self.m.render_brief(self.chan("31", guest_venue=True, reader_cards="CARDS"), [], [])
        self.assertTrue(prompt.endswith("\nGUEST(CARDS|/opt/kit/send 39)END"))
        self.assertEqual(deny, ["mcp__mail"])

    def test_a_guest_venue_without_cards_refuses_to_spawn(self):
        """Review focus 1: never a guest venue body without its rules."""
        for cards in (None, ""):
            with self.subTest(cards=cards), self.assertRaises(RuntimeError):
                self.m.spawn_session(self.chan("31", guest_venue=True, reader_cards=cards),
                                     [msg(1, "12", "hi")], [], {})
        self.m.spawn_body.assert_not_called()

    def test_a_guest_venue_with_an_empty_or_missing_floor_refuses_to_spawn(self):
        ch = self.chan("31", guest_venue=True, reader_cards="CARDS")
        guest = self.root / "briefs" / "discord-guest.md"
        guest.write_text("  \n")
        with self.assertRaises(RuntimeError):
            self.m.spawn_session(ch, [msg(1, "12", "hi")], [], {})
        self.m.spawn_body.assert_not_called()
        self.assertTrue(any("discord-guest.md" in e for e in self.m.startup_check()))
        self.reload()
        (self.root / "briefs" / "discord-guest.md").unlink()
        with self.assertRaises(FileNotFoundError):
            self.m.spawn_session(ch, [msg(1, "12", "hi")], [], {})
        self.m.spawn_body.assert_not_called()
        self.assertTrue(any("discord-guest.md" in e for e in self.m.startup_check()))

    def test_a_guest_venue_with_no_guest_deny_fails_closed(self):
        m = self.reload(guest_deny=[])
        with self.assertRaises(RuntimeError):
            m.spawn_session(self.chan("31", guest_venue=True, reader_cards="CARDS"),
                            [msg(1, "12", "hi")], [], {})
        m.spawn_body.assert_not_called()
        self.assertTrue(any("guest_deny" in e for e in m.startup_check()))
        m = self.reload(guest_deny=None)
        self.assertTrue(any("guest_deny" in e for e in m.startup_check()))
        # no guest venue in routes: an empty guest_deny is fine
        routes = dict(ROUTES, channels={"30": {"primary": "kit"}})
        m = self.reload(routes=routes, guest_deny=[])
        self.assertEqual(m.startup_check(), [])

    def test_a_guest_prompt_that_drops_the_cards_refuses(self):
        (self.root / "briefs" / "discord.md").write_text("no notes here: $where")
        with self.assertRaises(RuntimeError):
            self.m.render_brief(self.chan("31", guest_venue=True, reader_cards="CARDS"), [], [])
        self.assertIn("discord.md: never uses slot 'notes'", self.m.startup_check())
        self.reload()
        (self.root / "briefs" / "discord-guest.md").write_text("floor without cards $send $notify_chan")
        self.assertIn("discord-guest.md: never uses slot 'cards'", self.m.startup_check())

    def test_dollars_in_a_reader_card_pass_through_verbatim(self):
        cards = "## Pat\nowes $5; says ${cards} and $new and $$"
        prompt, _ = self.m.render_brief(self.chan("31", guest_venue=True, reader_cards=cards), [], [])
        self.assertIn(f"GUEST({cards}|", prompt)

    def test_briefs_are_loaded_at_startup_and_kept(self):
        self.assertEqual(self.m.startup_check(), [])
        before = self.m.render_brief(self.chan("30"), [], [])
        for f in (self.root / "briefs").glob("*.md"):
            f.unlink()
        self.assertEqual(self.m.render_brief(self.chan("30"), [], []), before)

    def test_dollars_in_message_text_pass_through_verbatim(self):
        """Review focus 2: slots are substituted once, never re-templated."""
        text = "costs $5, see $new and ${context} and $$"
        prompt, _ = self.m.render_brief(self.chan("30"), [msg(2, "11", text, "rob")], [])
        self.assertIn(f"rob [Robin]: {text}\n--\n", prompt)

    def test_spawn_session_spawns_what_render_brief_renders(self):
        ch = self.chan("31", guest_venue=True, reader_cards="CARDS")
        self.m.spawn_session(ch, [msg(1, "12", "hi")], [], {})
        plan = self.m.spawn_body.call_args.args[1]
        self.assertEqual((plan["prompt"], plan["deny"]), self.m.render_brief(ch, [msg(1, "12", "hi")], []))

    def errors_for(self, routes=None, **doc_changes):
        return self.reload(routes=routes, **doc_changes).startup_check()

    def assertError(self, errors, *words):
        self.assertTrue(any(all(w in e for w in words) for e in errors), (words, errors))

    def test_startup_check(self):
        self.assertEqual(self.m.startup_check(), [])
        (self.root / "briefs" / "discord.md").write_text("$where $conext")
        self.assertError(self.reload_keep_files(), "conext")
        (self.root / "briefs" / "discord-careful.md").unlink()
        self.assertError(self.reload_keep_files(), "discord-careful.md")

    def reload_keep_files(self):
        """The adapter re-imported over this dir's (edited) files."""
        self.m = load_adapter(self.root / "shuttle.json")
        return self.m.startup_check()

    def test_startup_check_requires_always_deny_but_allows_it_empty(self):
        self.assertError(self.errors_for(always_deny=None), "always_deny")
        self.assertEqual(self.errors_for(always_deny=[]), [])

    def test_startup_check_channel_notes_need_their_routes_key(self):
        self.assertError(self.errors_for(channel_notes={"nope": "discord-careful"}), "nope", "routes.json")
        self.assertError(self.errors_for(routes=dict(ROUTES, careful="32")), "careful", "list")
        self.assertEqual(self.errors_for(routes=dict(ROUTES, careful=["32"])), [])

    def test_startup_check_requires_an_executable_alert(self):
        self.assertError(self.errors_for(commands={"send": {"discord": "/opt/kit/send"}}), "commands.alert")
        self.assertError(self.errors_for(commands={"send": {"discord": "/opt/kit/send"},
                                                   "alert": "/nonexistent/alert"}), "commands.alert")
        (self.root / "alert").chmod(0o644)
        self.assertError(self.reload_keep_files(), "commands.alert")

    def test_startup_check_guest_venue_needs_a_notify_channel(self):
        self.assertError(self.errors_for(routes=dict(ROUTES, notify={})), "notify", "kit")
        self.assertEqual(self.errors_for(routes=dict(ROUTES, notify={},
                                                     channels={"30": {"primary": "kit"}})), [])

    def test_startup_check_runs_the_ext_check(self):
        self.reload()
        root = self.root
        make_instance(root, ext="def check(inst):\n    return [f'{inst.agent_slug}: data broken']\n")
        self.assertError(self.reload_keep_files(), "ext check", "kit: data broken")
        make_instance(root, ext="def check(inst):\n    raise ValueError('nope')\n")
        self.assertError(self.reload_keep_files(), "ext check", "nope")
        make_instance(root, ext="def check(inst):\n    return []\n")
        self.assertEqual(self.reload_keep_files(), [])

    def test_startup_check_knows_the_runner_kinds(self):
        self.assertEqual(self.errors_for(runner={"kind": "claude"}), [])
        self.assertError(self.errors_for(runner={"kind": "nope"}), "runner.kind", "nope", "claude")
        self.assertError(self.errors_for(runner="claude"), "runner must be an object")

    def test_startup_check_a_codex_runner_denies_nothing(self):
        # t-1439 review C6: codex can't deny tools, so an always_deny or a guest
        # venue stops the start, never a start() that raises every tick.
        codex_ready(self)
        no_guests = dict(ROUTES, channels={"30": {"primary": "kit"}})
        codex = {"kind": "codex", "codex": "/bin/true"}
        self.assertEqual(self.errors_for(routes=no_guests, runner=codex, always_deny=[]), [])
        self.assertError(self.errors_for(routes=no_guests, runner=codex), "codex", "always_deny")
        self.assertError(self.errors_for(runner=codex, always_deny=[]), "codex", "guest venues (31)")
        self.assertEqual(self.errors_for(), [])   # claude denies both
        r = self.reload(routes=no_guests, runner=codex, always_deny=[])._B.runner
        with mock.patch.dict(self.m.inbox._configured, {"inbox_dir": self.root / "ib"}):
            self.assertEqual((r.name, r.runs_dir, r.inbox_dir()),
                             ("codex", self.m.STATE_DIR / "runs", self.root / "ib"))

    def test_startup_check_a_codex_runner_needs_its_binary_and_profile(self):
        no_guests = dict(ROUTES, channels={"30": {"primary": "kit"}})
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "no-codex-home")}):
            errors = self.errors_for(routes=no_guests, always_deny=[],
                                     runner={"kind": "codex", "codex": "/nonexistent/codex"})
        self.assertError(errors, "runner.codex", "/nonexistent/codex")
        self.assertError(errors, "shuttle.config.toml is missing")

    def test_the_runner_is_the_claude_runner_built_from_the_adapter(self):
        r = self.m._B.runner
        self.assertEqual((r.name, r.claude, r.jobs_dir), ("claude", Path(self.m.CLAUDE), self.m.JOBS_DIR))
        self.m.CLAUDE, self.m.JOBS_DIR = Path("/elsewhere/claude"), self.root / "jobs"   # tests re-point these
        r = self.m._cfg().runner
        self.assertEqual((r.claude, r.jobs_dir), (Path("/elsewhere/claude"), self.root / "jobs"))
        self.assertIs(r.subprocess, self.m.subprocess)

    def test_startup_check_requires_the_bots_own_id(self):
        self.assertError(self.errors_for(routes=dict(ROUTES, bots={"bolt": "13"})), "bots", "kit")


RAISING_EXT = """
CALLS = []
def tick(bridge, state):
    CALLS.append(bridge.SLUG)
    if state.get("elsewhere"):
        raise ValueError("from another line")
    raise ValueError(state.get("why", "boom"))
"""


class ExtTick(unittest.TestCase):
    """Review focus 5: an ext hook that raises never kills the tick."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-discord-ext-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load_adapter(make_instance(self.root, ext=RAISING_EXT))

    def test_a_raising_ext_is_logged_paged_once_per_error_and_the_tick_goes_on(self):
        m = self.m
        ticks = []
        state = {"cursors": {}}
        with mock.patch.object(m, "state_locked", mock.MagicMock()), \
                mock.patch.object(m, "load_state", lambda: state), \
                mock.patch.object(m, "tick", ticks.append):
            m.tick_once()
            state["why"] = "same line, another message (a timestamp, an id)"
            m.tick_once()
            state["elsewhere"] = True
            m.tick_once()
            m.tick_once()
        self.assertEqual(len(ticks), 4)
        self.assertEqual(m.INSTANCE.ext().module.CALLS, ["kit"] * 4)
        # paged once per (exception type, innermost frame): two raise sites, two pages
        self.assertEqual(m.page.call_count, 2)
        self.assertIn("ValueError('boom')", m.page.call_args_list[0].args[0])
        self.assertIn("from another line", m.page.call_args_list[1].args[0])

    def test_no_ext_is_a_no_op(self):
        root = Path(tempfile.mkdtemp(prefix="shuttle-discord-noext-"))
        self.addCleanup(shutil.rmtree, root, True)
        m = load_adapter(make_instance(root))
        m.run_ext_tick({})
        m.page.assert_not_called()


class OnlyChannels(unittest.TestCase):
    """routes.json `only`: a one-channel trial beside another bridge polls
    that channel and its threads, and no mention elsewhere reaches it."""

    THREADS = {"threads": [{"id": "50", "type": 11, "parent_id": "30"},
                           {"id": "51", "type": 11, "parent_id": "32"},
                           {"id": "52", "type": 11, "parent_id": "34"}]}

    def adapter(self, **changes):
        root = Path(tempfile.mkdtemp(prefix="shuttle-discord-only-"))
        self.addCleanup(shutil.rmtree, root, True)
        return load_adapter(make_instance(root, routes={**ROUTES, **changes}))

    def test_without_only_every_unignored_channel_is_polled(self):
        m = self.adapter(ignore=["34"])
        self.assertEqual([c for c in ("30", "32", "34", "99") if m.polled(c)], ["30", "32", "99"])

    def test_only_polls_its_channels_and_ignore_still_wins(self):
        m = self.adapter(only=["30", "34"], ignore=["34"])
        self.assertEqual([c for c in ("30", "32", "34", "99") if m.polled(c)], ["30"])

    def test_only_keeps_threads_under_its_channels(self):
        m = self.adapter(only=["30"])
        with mock.patch.object(m, "api", lambda path: self.THREADS):
            self.assertEqual([t["id"] for t in m.active_threads(ROUTES)], ["50"])


class TickRolePings(unittest.TestCase):
    """A ping of this bot's managed role (routes.json `bot_roles`) wakes it
    through the real poll path, tick(), not only through route_reason when a
    test hands it the role map: tick() built every reason without it, so
    "@Kit" as Discord autocompletes it (the role) woke nothing live."""

    ROUTES = {**ROUTES, "only": ["30", "35"],
              "bot_roles": {"kit": ["91"], "bolt": "92"},
              "channels": {**ROUTES["channels"], "35": {"primary": "bolt", "peer": True}}}
    THREAD = {"id": "55", "type": 11, "parent_id": "35", "name": "t55"}

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="shuttle-discord-roles-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.m = load_adapter(make_instance(root, routes=self.ROUTES))

    def offered(self, cid, *msgs):
        """Run one tick with `msgs` new in channel or thread `cid`; the ids of
        the messages it offered to a body there."""
        m, got = self.m, []
        news = {cid: list(msgs)}

        def api(path):
            if path.endswith("/channels"):
                return [{"id": c, "type": 0, "name": f"c{c}"} for c in ("30", "35")]
            if path.endswith("/threads/active"):
                return {"threads": [self.THREAD]}
            return news.pop(path.split("/")[2], [])

        def offer(state, holds, live, conv, batch, ctx):
            got.extend((conv["id"], x["id"]) for x in batch)
            return "spawned", live

        state = {"cursors": {"30": "1", "35": "1", "55": "1"}}
        with mock.patch.object(m, "api", api), mock.patch.object(m, "offer", offer), \
                mock.patch.object(m, "reap_and_count", lambda s: (0, [])), \
                mock.patch.object(m, "inbox_sweep", lambda s: None), \
                mock.patch.object(m, "save_state", lambda s: None), \
                mock.patch.object(m.nowlog, "write_now", lambda *a: None):
            m.tick(state)
        return [i for c, i in got if c == cid]

    @staticmethod
    def ping(n, uid, text, users=(), roles=(), bot=False):
        x = msg(n, uid, text)
        x["mentions"] = [{"id": u} for u in users]
        x["mention_roles"] = list(roles)
        x["author"]["bot"] = bot
        return x

    def test_a_role_ping_of_this_bot_wakes_it_in_a_peer_primary_venue(self):
        self.assertEqual(self.offered("35", self.ping(2, "11", "<@&91> look", roles=["91"])), ["2"])

    def test_a_role_ping_wakes_it_in_a_thread_inheriting_the_route(self):
        self.assertEqual(self.offered("55", self.ping(2, "11", "<@&91> look", roles=["91"])), ["2"])

    def test_a_user_mention_still_wakes_it(self):
        self.assertEqual(self.offered("35", self.ping(2, "11", "<@19> look", users=["19"])), ["2"])

    def test_what_does_not_summon_it_still_does_not(self):
        quiet = [self.ping(2, "11", "plain, in the peer's venue"),
                 self.ping(3, "11", "<@&92> the peer's role", roles=["92"]),
                 self.ping(4, "11", "<@&93> some other role", roles=["93"]),
                 self.ping(5, "11", "a reply", users=["19"]),           # the automatic reply ping
                 self.ping(6, "11", "<@&91> written, never pinged")]     # no mention_roles
        self.assertEqual(self.offered("35", *quiet), [])

    def test_the_peers_role_is_the_peers_even_in_our_venue(self):
        self.assertEqual(self.offered("30", self.ping(2, "11", "<@&92> yours", roles=["92"])), [])
        self.assertEqual(self.offered("30", self.ping(3, "11", "plain, in ours")), ["3"])
        self.assertEqual(self.offered("30", self.ping(4, "11", "<@&91> <@&92> both", roles=["91", "92"])),
                         ["4"])

    def test_a_peer_summons_it_by_role_only_in_a_peer_venue(self):
        self.assertEqual(self.offered("35", self.ping(2, "13", "<@&91> yours", roles=["91"], bot=True)),
                         ["2"])
        self.assertEqual(self.offered("30", self.ping(3, "13", "<@&91> yours", roles=["91"], bot=True)),
                         [])


if __name__ == "__main__":
    unittest.main()
