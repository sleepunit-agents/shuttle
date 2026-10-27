"""The Slack adapter runs for any instance (t-1436): a generic agent, "kit",
with tiny briefs, no secrets needed to import, and an extension whose
dm_extra adds a scope (or misbehaves)."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shuttle import inbox, nowlog

ROOT = Path(__file__).resolve().parent.parent

BRIEFS = {
    "slack-dm": "kit DM $name/$slug t$tier: $tier_note|$card_note|$send $cid\n$new\n--\n$context",
    "slack-tier-2": "colleague $name ($role) flags via $send_discord $general",
    "slack-tier-3": "owner $name",
    "slack-card": "[card $slug: $card]",
    "slack-grant": " +grant $grant",
    "slack-restrict": " -restrict $restrict",
    "slack-group": "kit group $cid ($names)\n$roster\nt$tier ${note}${restricts}\n$cards[$slugs] "
                   "$send\n$new\n--\n$context",
    "slack-group-tier-2": "colleagues; flag via $send_discord $general",
    "slack-group-tier-3": "owners",
    "slack-group-owner-present": " +owner",
    "slack-group-restrict": " -$name: $restrict",
}

PEOPLE = {"people": {
    "robin": {"name": "Robin", "tier": 3, "slack": {"T1": ["U3"]}},
    "pat": {"name": "Pat", "role": "ops", "tier": 2, "slack": {"T1": ["U2"]},
            "grant": "may file", "restrict": "no merges"},
    "lee": {"name": "Lee", "role": "pm", "tier": 2, "slack": {"T1": ["U4"]}},
}}
ROUTES = {"notify": {"owner": "C9"}}
SECRETS = ("SLACK_BOT_TOKEN=xoxb-t\nSLACK_APP_TOKEN=xapp-t\n"
           "SLACK_TEAM_ID=T1\nSLACK_BOT_USER_ID=UBOT\n")

SCOPE_EXT = """
CALLS = []
def dm_extra(bridge, rec, tier):
    CALLS.append((bridge.SLUG, rec["name"], tier))
    if rec.get("scope"):
        return {"text": " SCOPE " + rec["scope"], "env": {"KIT_SCOPE": rec["scope"]},
                "on_claimed": lambda aid: CALLS.append(("claimed", aid))}
    return {"text": "", "env": None, "on_claimed": None}
"""

RAISING_EXT = """
def dm_extra(bridge, rec, tier):
    if rec.get("elsewhere"):
        raise ValueError("from another line")
    raise ValueError(rec.get("why", "boom"))
"""


def make_instance(root, ext=SCOPE_EXT, routes=None, secrets=True, **doc_changes):
    """A "kit" instance in `root` that passes startup_check (credentials, an
    executable alert). `doc_changes` replace shuttle.json keys; None drops one."""
    root = Path(root)
    (root / "briefs").mkdir(parents=True, exist_ok=True)
    for name, text in BRIEFS.items():
        (root / "briefs" / f"{name}.md").write_text(text)
    (root / "people.json").write_text(json.dumps(PEOPLE))
    (root / "routes.json").write_text(json.dumps(ROUTES if routes is None else routes))
    (root / "secrets").mkdir(exist_ok=True)
    if secrets:
        (root / "secrets" / "kit-slack.env").write_text(SECRETS)
    alert = root / "alert"
    alert.write_text("#!/bin/sh\n")
    alert.chmod(0o755)
    doc = {"agent": {"slug": "kit", "name": "Kit", "tag": "kit"}, "owner": {"name": "Robin"},
           "state_root": str(root / "state"), "secrets": str(root / "secrets"),
           "commands": {"send": {"slack": "/opt/kit/slack-send", "chat": "/opt/kit/chat-send"},
                        "alert": str(alert), "loom": "/opt/kit/loom"},
           "always_deny": ["mcp__x"],
           "slack": {"secrets_env": "kit-slack.env",
                     "flag_to": {"via": "chat", "chan_from_routes": "notify.owner"}}}
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
    """Import adapters/slack.py against the instance at `path`, leaving the
    process-wide nowlog/inbox configuration as it found it."""
    saved = (dict(nowlog._configured), dict(inbox._configured))
    try:
        with mock.patch.dict(os.environ, {"SHUTTLE_INSTANCE": str(path)}):
            spec = importlib.util.spec_from_file_location("slack_under_test",
                                                          ROOT / "adapters" / "slack.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
    finally:
        nowlog._configured.clear()
        nowlog._configured.update(saved[0])
        inbox._configured.clear()
        inbox._configured.update(saved[1])
    m.page = mock.Mock()
    m.spawn_body = mock.Mock()
    m.person_cards = mock.Mock()
    m.person_cards.loom_card = lambda slug, loom=None: f"card of {slug} via {loom}"
    return m


NOW = int(time.time())


def entry(n, slug, text, age_s=0):
    """A DM line `age_s` seconds old (Slack ts: epoch seconds)."""
    return {"who": f"{slug.title()} [{slug}]", "text": text, "ts": f"{NOW - age_s}.{n:06d}",
            "uid": "U", "own": False}


class SlackAdapter(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-slack-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load_adapter(make_instance(self.root))

    def reload(self, ext=SCOPE_EXT, routes=None, **doc_changes):
        root = Path(tempfile.mkdtemp(prefix="shuttle-slack-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.root = root
        self.m = load_adapter(make_instance(root, ext=ext, routes=routes, **doc_changes))
        return self.m

    def reload_keep_files(self):
        self.m = load_adapter(self.root / "shuttle.json")
        return self.m.startup_check()

    def test_bridge_posts_are_logged_as_the_bridge_not_a_body(self):
        # the send command logs SHUTTLE_BODY first (t-1439), so the bridge sets it too
        self.assertEqual(self.m.BRIDGE_SEND_ENV["SHUTTLE_BODY"], "bridge")
        self.assertEqual(self.m.BRIDGE_SEND_ENV["CLAUDE_JOB_DIR"], "bridge")

    def test_a_shared_file_is_fetched_with_the_bots_token_and_the_body_gets_a_path(self):
        # Slack's link needs the bot's token, which a body never holds: the bridge saves the file and says where.
        import io
        from unittest import mock
        m = self.m
        seen = []

        class Resp(io.BytesIO):
            def __init__(self, data, ctype):
                super().__init__(data)
                self.headers = {"Content-Type": ctype}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("Authorization")))
            return Resp(b"\x89PNG...", "image/png")

        f = {"id": "F0CCPLMP68Z", "name": "../image.png", "size": 7,
             "url_private": "https://files.slack.com/files-pri/T1-F0CCPLMP68Z/image.png"}
        with mock.patch.object(m, "_open_file", opener):
            path, why = m.fetch_file(f, "1793129045.476674")
            self.assertIsNone(why)
            self.assertEqual(path.parent.parent, m.STATE_DIR / "files")
            self.assertEqual(path.name, "1793129045-476674-F0CCPLMP68Z-image.png", "no ../ in the name")
            self.assertEqual(path.read_bytes(), b"\x89PNG...")
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(seen, [(f["url_private"], "Bearer xoxb-t")])
            self.assertEqual(m.fetch_file(f, "1793129045.476674")[0], path)
            self.assertEqual(len(seen), 1, "fetched once")
            # The token goes only to Slack's file host; too big, or a page instead of the file, is said, not saved.
            self.assertEqual(m.fetch_file({**f, "url_private": "https://evil.example/x.png"}, "1.2"), (None, "no link to fetch"))
            self.assertEqual(m.fetch_file({**f, "size": 30 * 1048576}, "1.3"), (None, "too big to fetch (30 MB)"))
        with mock.patch.object(m, "_open_file", lambda req, timeout: Resp(b"<html>", "text/html; charset=utf-8")):
            self.assertIsNone(m.fetch_file(f, "1.4")[0])
        self.assertEqual(len(seen), 1)

    def test_import_needs_no_secrets_and_reads_the_instance(self):
        m = self.reload(secrets=False)
        self.assertEqual(m.STATE_DIR, self.root / "state" / "kit-slack")
        self.assertEqual(m.ARCHIVE_DIR, self.root / "state" / "kit-slack" / "archive")
        self.assertEqual(m.PEOPLE_FILE, self.root / "people.json")
        self.assertEqual((str(m.SLACK_SEND), str(m.FLAG_SEND), m.FLAG_CHAN),
                         ("/opt/kit/slack-send", "/opt/kit/chat-send", "C9"))
        cfg = m._cfg()
        self.assertEqual((cfg.tag, cfg.agent, cfg.always_deny, cfg.name), ("kit", "kit", ("mcp__x",), "slack"))
        with self.assertRaises(OSError):
            m.secret("TEAM_ID")   # read when first needed, not at import
        self.assertTrue(any("secrets" in e for e in m.startup_check()))
        (self.root / "secrets" / "kit-slack.env").write_text(SECRETS)
        m._ENV.clear()
        self.assertEqual((m.TEAM_ID, m.BOT_USER_ID, m.secret("BOT_TOKEN")), ("T1", "UBOT", "xoxb-t"))
        self.assertEqual(m.startup_check(), [])

    def test_a_test_can_still_set_the_ids(self):
        self.m.TEAM_ID, self.m.BOT_USER_ID = "T7", "U7"
        self.assertEqual((self.m.secret("TEAM_ID"), self.m.secret("BOT_USER_ID")), ("T7", "U7"))
        self.assertEqual(self.m.clean_text("hi <@U7>", {}), "hi @Kit")

    def test_a_dm_brief_for_a_generic_instance(self):
        rec = PEOPLE["people"]["pat"]
        prompt, env, on_claimed = self.m.render_dm("D2", "pat", rec, 2, [entry(2, "pat", "hello")],
                                                   [entry(1, "pat", "before")])
        self.assertEqual(prompt, "kit DM Pat/pat t2: colleague Pat (ops) flags via /opt/kit/chat-send C9"
                                 " +grant may file -restrict no merges|[card pat: card of pat via "
                                 "/opt/kit/loom]|/opt/kit/slack-send D2\nPat [pat]: hello\n--\n"
                                 "Pat [pat]: before")
        self.assertEqual((env, on_claimed), (None, None))
        prompt, _, _ = self.m.render_dm("D3", "robin", PEOPLE["people"]["robin"], 3, [], [])
        self.assertTrue(prompt.startswith("kit DM Robin/robin t3: owner Robin|"))
        self.assertTrue(prompt.endswith("\n\n--\n(no earlier messages seen)"))
        self.assertEqual(self.m.INSTANCE.ext().module.CALLS, [("kit", "Pat", 2)],
                         "dm_extra runs for a tier-2 DM only")

    def test_dm_extra_text_env_and_on_claimed_reach_the_spawn(self):
        rec = dict(PEOPLE["people"]["lee"], scope="site-a")
        self.m.spawn_session({}, "D4", "lee", rec, 2, [entry(1, "lee", "hi")], [], {})
        plan = self.m.spawn_body.call_args.args[1]
        self.assertIn("flags via /opt/kit/chat-send C9 SCOPE site-a|", plan["prompt"])
        self.assertEqual(plan["env"]["KIT_SCOPE"], "site-a")
        self.assertEqual(plan["env"]["PATH"], os.environ["PATH"], "the bridge's env plus the ext's")
        plan["on_claimed"]("aid-1")
        self.assertIn(("claimed", "aid-1"), self.m.INSTANCE.ext().module.CALLS)
        self.assertEqual(plan["deny"], [])

    def test_a_group_brief_for_a_generic_instance(self):
        m = self.m
        members = sorted(((s, PEOPLE["people"][s]) for s in ("pat", "lee", "robin")),
                         key=lambda p: p[1]["name"])
        conv = {"id": "G1", "group": True, "members": members, "slug": "Lee, Pat, Robin",
                "names": [r["name"] for _, r in members], "tier": 2}
        prompt = m.build_group_prompt(conv, [entry(2, "lee", "hey")], [])
        self.assertEqual(prompt.split("\n")[0], "kit group G1 (Lee; Pat; Robin)")
        self.assertIn("\n- Lee: registry slug `lee`, tier 2 (pm)\n", prompt)
        self.assertIn("\nt2 colleagues; flag via /opt/kit/chat-send C9 +owner -Pat: no merges\n", prompt)
        self.assertIn("[card lee: card of lee via /opt/kit/loom][card pat:", prompt)
        self.assertIn('["lee", "pat", "robin"] /opt/kit/slack-send\nLee [lee]: hey\n--\n'
                      "(no earlier messages seen)", prompt)
        self.assertNotIn("grant", prompt, "nobody's standing authority applies in a group")
        self.assertEqual(m.INSTANCE.ext().module.CALLS, [], "no dm_extra in a group")
        conv["tier"] = 3
        self.assertIn("\nt3 owners -Pat", m.build_group_prompt(conv, [], []))
        m.spawn_group_session({}, conv, [], [])
        self.assertEqual(m.spawn_body.call_args.args[1]["env"], None)

    def test_dollars_in_a_card_or_a_message_pass_through_verbatim(self):
        """Review focus 2: slots are substituted once, never re-templated."""
        text = "costs $5, see $new and ${context} and $$"
        card = "owes $5; says ${card} and $tier_note and $$"
        self.m.person_cards.loom_card = lambda slug, loom=None: card
        rec = dict(PEOPLE["people"]["pat"], grant="$grant", restrict="${restrict}")
        prompt, _, _ = self.m.render_dm("D2", "pat", rec, 2, [entry(1, "pat", text)], [])
        self.assertIn(f"[card pat: {card}]", prompt)
        self.assertIn(f"Pat [pat]: {text}\n--\n", prompt)
        self.assertIn(" +grant $grant -restrict ${restrict}|", prompt)
        members = [("pat", rec), ("lee", PEOPLE["people"]["lee"])]
        group = self.m.build_group_prompt({"id": "G1", "members": members, "names": ["Pat", "Lee"],
                                           "tier": 2}, [entry(1, "pat", text)], [])
        self.assertIn(f"[card pat: {card}]", group)
        self.assertIn(f"Pat [pat]: {text}\n--\n", group)

    def test_briefs_are_loaded_at_startup_and_kept(self):
        self.assertEqual(self.m.startup_check(), [])
        rec = PEOPLE["people"]["pat"]
        before = self.m.render_dm("D2", "pat", rec, 2, [], [])[0]
        for f in (self.root / "briefs").glob("*.md"):
            f.unlink()
        self.assertEqual(self.m.render_dm("D2", "pat", rec, 2, [], [])[0], before)

    def errors_for(self, routes=None, **doc_changes):
        return self.reload(routes=routes, **doc_changes).startup_check()

    def assertError(self, errors, *words):
        self.assertTrue(any(all(w in e for w in words) for e in errors), (words, errors))

    def test_startup_check_briefs(self):
        self.assertEqual(self.m.startup_check(), [])
        (self.root / "briefs" / "slack-dm.md").write_text("$name $tier_note $card_note $tierr")
        self.assertError(self.reload_keep_files(), "slack-dm.md", "tierr")
        self.reload()
        (self.root / "briefs" / "slack-tier-3.md").unlink()
        self.assertError(self.reload_keep_files(), "slack-tier-3.md")
        self.reload()
        (self.root / "briefs" / "slack-dm.md").write_text("$name $card_note")
        self.assertIn("slack-dm.md: never uses slot 'tier_note'", self.reload_keep_files())
        self.reload()
        (self.root / "briefs" / "slack-restrict.md").write_text("restricted")
        self.assertIn("slack-restrict.md: never uses slot 'restrict'", self.reload_keep_files())
        self.reload()
        (self.root / "briefs" / "slack-group.md").write_text("$note $cards")
        self.assertIn("slack-group.md: never uses slot 'restricts'", self.reload_keep_files())

    def test_startup_check_requires_always_deny_send_and_alert(self):
        self.assertError(self.errors_for(always_deny=None), "always_deny")
        self.assertEqual(self.errors_for(always_deny=[]), [])
        self.assertError(self.errors_for(commands={"send": {"chat": "/c"}, "alert": str(self.root / "alert")}),
                         "commands.send.slack")
        self.assertError(self.errors_for(commands={"send": {"slack": "/s", "chat": "/c"}}), "commands.alert")
        (self.root / "alert").chmod(0o644)
        self.assertError(self.reload_keep_files(), "commands.alert")

    def test_startup_check_flag_to_must_resolve_when_a_brief_uses_it(self):
        self.assertError(self.errors_for(routes={}), "slack.flag_to", "slack-group-tier-2.md",
                         "slack-tier-2.md")
        self.assertError(self.errors_for(slack={"secrets_env": "kit-slack.env",
                                                "flag_to": {"via": "nope", "chan": "C1"}}),
                         "slack.flag_to")
        self.assertEqual(self.errors_for(slack={"secrets_env": "kit-slack.env",
                                                "flag_to": {"via": "chat", "chan": "C1"}}), [])
        # briefs that never tell a body to flag anything need no flag_to
        self.reload(slack={"secrets_env": "kit-slack.env"})
        for name in ("slack-tier-2", "slack-group-tier-2"):
            (self.root / "briefs" / f"{name}.md").write_text("plain colleagues")
        self.assertEqual(self.reload_keep_files(), [])

    def test_startup_check_knows_the_runner_kinds(self):
        self.assertEqual(self.errors_for(runner={"kind": "claude"}), [])
        self.assertError(self.errors_for(runner={"kind": "codex?"}), "runner.kind", "codex?")

    def test_startup_check_a_codex_runner_denies_nothing(self):
        # t-1439 review C6: codex can't deny tools, so always_deny must be [].
        codex_ready(self)
        codex = {"kind": "codex", "codex": "/bin/true"}
        self.assertEqual(self.errors_for(runner=codex, always_deny=[]), [])
        self.assertError(self.errors_for(runner=codex), "codex", "always_deny")
        r = self.reload(runner=codex, always_deny=[])._B.runner
        self.assertEqual((r.name, r.runs_dir), ("codex", self.m.STATE_DIR / "runs"))

    def test_startup_check_secrets_and_ext(self):
        (self.root / "secrets" / "kit-slack.env").write_text("SLACK_BOT_TOKEN=x\n")
        self.assertError(self.reload_keep_files(), "SLACK_APP_TOKEN", "SLACK_TEAM_ID")
        self.assertError(self.errors_for(ext="def dm_extra(:\n"), "ext")

    def test_startup_check_flag_chan_must_be_a_non_empty_string(self):
        self.assertError(self.errors_for(routes={"notify": {"owner": {"x": 1}}}), "slack.flag_to")
        self.assertError(self.errors_for(routes={"notify": {"owner": ""}}), "slack.flag_to")
        self.assertError(self.errors_for(routes={"notify": {"owner": ["C9"]}}), "slack.flag_to")

    def test_the_ids_are_absent_not_an_error_without_secrets(self):
        m = self.reload(secrets=False)
        self.assertFalse(hasattr(m, "TEAM_ID"))
        self.assertFalse(hasattr(m, "BOT_USER_ID"))
        with self.assertRaises(OSError):
            m.secret("TEAM_ID")   # inside the bridge it stays a loud error

    def test_startup_check_runs_the_ext_check(self):
        self.assertError(self.errors_for(ext=SCOPE_EXT + "def check(inst):\n    return ['projects: bad']\n"),
                         "ext check", "projects: bad")
        self.assertError(self.errors_for(ext=SCOPE_EXT + "def check(inst):\n    raise ValueError('nope')\n"),
                         "ext check", "nope")
        self.assertError(self.errors_for(ext=SCOPE_EXT + "def check(inst):\n    return 'bad'\n"),
                         "ext check")
        self.assertEqual(self.errors_for(ext=SCOPE_EXT + "def check(inst):\n"
                                         "    assert inst.agent_slug == 'kit'\n    return []\n"), [])

    def test_main_refuses_to_start_without_websocket_client(self):
        with mock.patch.object(sys, "argv", ["slack.py"]), \
                mock.patch.dict(sys.modules, {"websocket": None}), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            self.m.main()
        self.assertIn("websocket-client", str(cm.exception.code))
        self.assertFalse(self.m.STATE_DIR.exists(), "it stopped before taking the lock")

    def test_an_unknown_tier_has_no_brief(self):
        with self.assertRaises(RuntimeError):
            self.m.render_dm("D2", "pat", PEOPLE["people"]["pat"], 4, [], [])


class DmExtraFailsClosed(unittest.TestCase):
    """A dm_extra that raises (or returns junk) spawns no body for that DM:
    it could be one whose scope came from the ext. The batch is held quietly
    until the instance's wait TTL, then dropped with the failure notice."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-slack-ext-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.m = load_adapter(make_instance(self.root, ext=RAISING_EXT))
        self.m.notify_failure = mock.Mock()
        self.err = io.StringIO()
        quiet = contextlib.redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def lines(self):
        return [l for l in self.err.getvalue().splitlines() if l]

    def test_a_raising_dm_extra_spawns_nothing_keeps_the_batch_and_pages_once_per_error(self):
        m = self.m
        state = {}
        rec = dict(PEOPLE["people"]["pat"])
        batch = [entry(1, "pat", "hi")]
        self.assertIs(m.spawn_session(state, "D2", "pat", rec, 2, batch, [], {}), False)
        m.spawn_session(state, "D2", "pat", dict(rec, why="another message, same line"), 2,
                        batch + [entry(2, "pat", "again")], [], {})
        m.spawn_body.assert_not_called()
        self.assertEqual([e["ts"] for e in state["leftover"]["D2"]], [f"{NOW}.000001", f"{NOW}.000002"],
                         "the DM waits for the next tick, nothing duplicated")
        self.assertEqual(m.page.call_count, 1)
        self.assertIn("ValueError('boom')", m.page.call_args.args[0])
        m.spawn_session(state, "D2", "pat", dict(rec, elsewhere=True), 2, batch, [], {})
        self.assertEqual(m.page.call_count, 2, "a second raise site pages again")
        m.spawn_body.assert_not_called()
        m.notify_failure.assert_not_called()
        self.assertEqual(len(self.lines()), 1, f"held quietly: one line when it starts failing\n{self.lines()}")
        self.assertIn("dm_extra failing for pat (D2)", self.lines()[0])

    def test_held_until_the_wait_ttl_then_dropped_with_the_failure_notice(self):
        m = self.m
        state = {}
        rec = PEOPLE["people"]["pat"]
        ttl = m._cfg().wait_ttl
        m.spawn_session(state, "D2", "pat", rec, 2, [entry(1, "pat", "hi", age_s=ttl - 60)], [], {})
        self.assertIn("D2", state["leftover"])
        m.notify_failure.assert_not_called()
        # the next tick: the held batch is now past the TTL (oldest message's ts)
        m.spawn_session(state, "D2", "pat", rec, 2, [entry(1, "pat", "hi", age_s=ttl + 1)], [], {})
        self.assertNotIn("D2", state["leftover"])
        m.notify_failure.assert_called_once()
        self.assertEqual(m.notify_failure.call_args.args[0], "D2")
        self.assertEqual(m.page.call_count, 2, "one page when it failed, one when it was dropped")
        self.assertIn("dropped", m.page.call_args.args[0])
        self.assertEqual(len(self.lines()), 2)
        self.assertIn("dropped", self.lines()[1])
        m.spawn_body.assert_not_called()
        # another DM dropped for the same error: its person is told, no second page
        m.spawn_session(state, "D5", "pat", rec, 2, [entry(1, "pat", "x", age_s=ttl + 1)], [], {})
        self.assertEqual((m.notify_failure.call_count, m.page.call_count), (2, 2))

    def test_a_recovery_is_logged_once(self):
        m = self.m
        state = {}
        rec = PEOPLE["people"]["pat"]
        m.spawn_session(state, "D2", "pat", rec, 2, [entry(1, "pat", "hi")], [], {})
        m.INSTANCE.ext().dm_extra = lambda b, r, t: {"text": "", "env": None, "on_claimed": None}
        self.assertIs(m.spawn_session(state, "D2", "pat", rec, 2, state["leftover"].pop("D2"), [], {}),
                      True)
        m.spawn_body.assert_called_once()
        m.spawn_session(state, "D2", "pat", rec, 2, [entry(2, "pat", "more")], [], {})
        self.assertEqual(len(self.lines()), 2)
        self.assertIn("works again", self.lines()[1])

    def test_a_refused_spawn_reaches_dispatch_as_false(self):
        rec = PEOPLE["people"]["pat"]
        conv = {"id": "D2", "slug": "pat", "person": rec}
        self.assertIs(self.m.spawn({}, conv, [entry(1, "pat", "hi")], 1, "new", {"people": {}}), False)

    def test_tier_three_and_groups_never_call_it(self):
        self.m.spawn_session({}, "D3", "robin", PEOPLE["people"]["robin"], 3, [entry(1, "robin", "x")], [], {})
        self.assertEqual(self.m.spawn_body.call_count, 1)
        self.m.page.assert_not_called()

    def test_a_dm_extra_with_the_wrong_shape_fails_closed(self):
        for bad in ('"text"', '{"text": 5}', '{"env": {"A": 1}}', '{"env": "A=1"}',
                    '{"on_claimed": "later"}'):
            with self.subTest(bad=bad):
                m = load_adapter(make_instance(self.root, ext=f"def dm_extra(b, rec, tier):\n    return {bad}\n"))
                m.notify_failure = mock.Mock()
                state = {}
                m.spawn_session(state, "D2", "pat", PEOPLE["people"]["pat"], 2, [entry(1, "pat", "hi")], [], {})
                m.spawn_body.assert_not_called()
                self.assertIn("D2", state["leftover"])
                self.assertEqual(m.page.call_count, 1)

    def test_no_ext_adds_nothing(self):
        root = Path(tempfile.mkdtemp(prefix="shuttle-slack-noext-"))
        self.addCleanup(shutil.rmtree, root, True)
        m = load_adapter(make_instance(root, ext=None))
        m.spawn_session({}, "D2", "pat", PEOPLE["people"]["pat"], 2, [entry(1, "pat", "hi")], [], {})
        plan = m.spawn_body.call_args.args[1]
        self.assertEqual((plan["env"], plan["on_claimed"]), (None, None))
        m.page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
