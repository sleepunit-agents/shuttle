from datetime import datetime, timezone
"""loomui: the shuttle on loom's page (contract v2), from fixture now-dirs."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from shuttle import loomui

ROOT = Path(__file__).resolve().parent.parent

# 1 March, noon local: the last 24h reach back into February's events file.
NOW = time.mktime((2026, 3, 1, 12, 0, 0, 0, 0, -1))
MIDNIGHT = time.mktime((2026, 3, 1, 0, 0, 0, 0, 0, -1))


def snap(bridge, at, talking=(), waiting=(), interval=15, used=None, cap=5, socket=None):
    talking = list(talking)
    return {"v": 1, "agent": "a", "bridge": bridge, "at": at, "interval_s": interval,
            "slots": {"used": len(talking) if used is None else used, "cap": cap},
            "mem_mb": 1000, "socket": socket, "talking": talking, "waiting": list(waiting)}


def ev(t, bridge, kind, **f):
    return {"v": 1, "t": t, "agent": "a", "bridge": bridge, "kind": kind, **f}


# ---------------------------------------------------------------- the contract's shape

TONES = {"ok", "warn", "bad", "muted"}
SECTION_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PATH = re.compile(r"^[a-z0-9._~/-]{0,200}$")


def check_str(t, s):
    t.assertIsInstance(s, str)
    t.assertLessEqual(len(s), 2000)


def check_page_link(t, p):
    check_str(t, p)
    p = p[5:] if p.startswith("loom:") else p
    t.assertRegex(p, PATH)
    t.assertNotIn("..", p.split("/"))


def check_tone(t, o):
    if "tone" in o:
        t.assertIn(o["tone"], TONES)


def check_block(t, b):
    kind = b["type"]
    if kind == "stats":
        for i in b["items"]:
            check_str(t, i["label"]), check_str(t, i["value"]), check_tone(t, i)
    elif kind == "text":
        check_str(t, b["text"]), check_tone(t, b)
    elif kind == "kv":
        for i in b["items"]:
            check_str(t, i["label"]), check_str(t, i["value"])
    elif kind == "table":
        t.assertLessEqual(len(b["columns"]), 12)
        t.assertLessEqual(len(b["rows"]), 200)
        for row in b["rows"]:
            t.assertLessEqual(len(row), len(b["columns"]))
            for cell in row:
                if isinstance(cell, dict):
                    t.assertEqual(set(cell), {"text", "page"})
                    check_str(t, cell["text"]), check_page_link(t, cell["page"])
                else:
                    check_str(t, cell)
    elif kind == "status":
        for i in b["items"]:
            check_str(t, i["label"]), check_str(t, i.get("text", "")), check_tone(t, i)
            if "page" in i:
                check_page_link(t, i["page"])
    elif kind == "list":
        t.assertLessEqual(len(b["items"]), 200)
        for i in b["items"]:
            check_str(t, i["text"]), check_str(t, i.get("meta", "")), check_tone(t, i)
            if "avatar" in i:
                t.assertRegex(i["avatar"], r"^.{1,2}$")
            if "page" in i:
                check_page_link(t, i["page"])
            if "href" in i:
                t.assertNotIn("page", i)
                t.assertTrue(i["href"].startswith("https://"))
    elif kind == "callout":
        check_str(t, b["text"]), check_str(t, b.get("title", "")), check_tone(t, b)
    elif kind == "link":
        check_str(t, b["text"])
        if "page" in b:
            check_page_link(t, b["page"])
        else:
            t.assertTrue(b["href"].startswith(("http://", "https://")))
    else:
        t.fail(f"unexpected block {kind!r}")


def check_page(t, page):
    check_str(t, page["title"])
    t.assertLessEqual(len(page["sections"]), 10)
    for s in page["sections"]:
        t.assertRegex(s["id"], SECTION_ID)
        check_tone(t, s)
        t.assertLessEqual(len(s["blocks"]), 20)
        for b in s["blocks"]:
            check_block(t, b)
    t.assertLess(len(json.dumps(page)), 512 * 1024)
    t.assertNotIn("buttons", json.dumps(page))
    t.assertNotIn("form", [b["type"] for s in page["sections"] for b in s["blocks"]])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shuttle-loomui-"))
        self.people = self.dir / "people.json"

    def write(self, s):
        (self.dir / f"{s['bridge']}.json").write_text(json.dumps(s))

    def events(self, month, recs):
        with open(self.dir / f"events-{month}.jsonl", "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def view(self, now=NOW, **kw):
        kw.setdefault("rules", {"guest_deny": ["mcp__mail", "mcp__calendar"],
                                "always_deny": ["mcp__handoff"], "preferred_model": "model-x"})
        kw.setdefault("people_file", self.people)
        kw.setdefault("agent_name", "Robin")
        return loomui.View(self.dir, now=now, **kw)


# ---------------------------------------------------------------- the manifest


class Manifest(Fixture):
    def test_live_bridges_tile_and_no_asks(self):
        self.write(snap("chat", NOW - 5, talking=[{"venue": "#general", "who": ["Pat"], "active_s": 60}]))
        self.write(snap("talk", NOW - 2, interval=5, socket={"state": "up"}))
        m = loomui.manifest(self.view())
        self.assertEqual(m["v"], 2)
        self.assertEqual(m["tab"], {"label": "Bridges", "kind": "setting"})
        self.assertEqual(m["tile"], {"label": "Bridges", "value": "2 live", "detail": "#general, now",
                                     "tone": "ok", "page": ""})
        self.assertEqual(m["asks"], [])

    def test_each_conversation_is_the_agents_doing(self):
        # loom's Home card says, simply, what the agent has going: a conversation per body.
        self.write(snap("chat", NOW - 5, talking=[{"venue": "#general", "who": ["Pat"], "active_s": 60, "body_since": NOW - 300},
                                                   {"venue": "chat-dm", "who": ["Sam Lee"], "active_s": 5, "stalled": True}]))
        self.write(snap("talk", NOW - 9999))   # dark: says nothing
        m = loomui.manifest(self.view(agent_slug="robin"))
        self.assertEqual(m["agents"], {"robin": {"doing": [
            {"text": "Talking in #general", "kind": "talk", "where": "Chat · active 60s ago",
             "since": datetime.fromtimestamp(NOW - 300, timezone.utc).isoformat().replace("+00:00", "Z")},
            {"text": "Talking with Sam Lee", "kind": "talk", "where": "Chat DM · active just now · stalled"},
        ]}})
        # Nobody talking, or no agent to say it of: no agents key at all.
        self.write(snap("chat", NOW - 5))
        self.assertNotIn("agents", loomui.manifest(self.view(agent_slug="robin")))

    def test_detail_is_the_latest_conversation_else_idle(self):
        self.write(snap("chat", NOW - 5))
        self.assertEqual(loomui.manifest(self.view())["tile"]["detail"], "idle")
        self.events("2026-03", [ev(NOW - 600, "chat", "picked", cid="c-1", venue="#general", who=["Pat"], wait_s=3),
                                ev(NOW - 60, "chat", "finished", cid="c-1", venue="#general", outcome="replied")])
        self.assertEqual(loomui.manifest(self.view())["tile"]["detail"], "#general, 60s ago")

    def test_a_dm_says_who(self):
        self.write(snap("talk", NOW - 1))
        self.events("2026-03", [ev(NOW - 120, "talk", "picked", cid="c-2", venue="talk-dm", who=["Sam Lee"], wait_s=1)])
        self.assertEqual(loomui.manifest(self.view())["tile"]["detail"], "DM with Sam Lee, 2m ago")

    def test_a_dark_bridge_is_an_ask(self):
        dark_at = NOW - 3600
        self.write(snap("chat", NOW - 5))
        self.write(snap("talk", dark_at, interval=5))
        m = loomui.manifest(self.view())
        self.assertEqual(m["tile"]["value"], "1 live")
        self.assertEqual(m["tile"]["tone"], "bad")
        [ask] = m["asks"]
        self.assertEqual(ask["text"], "Talk bridge is dark since " + loomui._clock(dark_at, NOW))
        self.assertEqual(ask["kind"], "alert", "something to look at, not a decision")
        self.assertEqual(ask["page"], "")
        self.assertEqual(ask["meta"], "no word from it for 60m")

    def test_dark_is_three_intervals_at_least_30s(self):
        self.write(snap("chat", NOW - 29, interval=5))
        self.assertEqual(loomui.manifest(self.view())["asks"], [])
        self.write(snap("chat", NOW - 46, interval=15))
        self.assertEqual(len(loomui.manifest(self.view())["asks"]), 1)

    def test_no_bridges_at_all(self):
        m = loomui.manifest(self.view())
        self.assertEqual(m["tile"]["value"], "0 live")
        self.assertEqual(m["asks"], [])


# ---------------------------------------------------------------- the page


class Page(Fixture):
    def setUp(self):
        super().setUp()
        self.write(snap("chat", NOW - 4, talking=[{"cid": "c-1", "chan": "x", "venue": "#general", "who": ["Pat"],
                                                   "active_s": 4, "stalled": False, "queued": 2,
                                                   "body": "secret-body-id", "text": "message content"}],
                        waiting=[{"venue": "#b", "who": ["Sam"], "reason": "slots_full"}]))
        self.write(snap("talk", NOW - 2, interval=5, socket={"state": "up", "since": NOW - 100}))
        evs = [
            ev(MIDNIGHT + 60, "chat", "picked", cid="c-1", venue="#general", who=["Pat"], wait_s=20, body="b1"),
            ev(MIDNIGHT + 70, "chat", "delivered", cid="c-1", venue="#general", who=["Pat"], n=1, body="b1"),
            ev(MIDNIGHT + 90, "chat", "picked", cid="c-1", venue="#general", who=["Pat"], wait_s=28, body="b2"),
            ev(NOW - 50, "talk", "picked", cid="c-2", venue="talk-dm", who=["Sam Lee"],
               members=["sam-lee"], wait_s=6, body="b3"),
        ]
        # Socket rotations: down, up 2s later, every few hours.
        for k in range(3):
            t = NOW - 3600 * (1 + 4 * k)
            evs += [ev(t, "talk", "socket", state="down", since=t), ev(t + 2, "talk", "socket", state="up", since=t + 2)]
        self.events("2026-03", sorted(evs, key=lambda r: r["t"]))

    def page(self, path="", **kw):
        p = loomui.page(self.view(**kw), path)
        check_page(self, p)
        return p

    def section(self, p, sid):
        [s] = [s for s in p["sections"] if s["id"] == sid]
        return s

    def test_shape(self):
        p = self.page()
        self.assertEqual(p["title"], "Bridges")
        self.assertIn("Robin", p["intro"])
        self.assertIn("up to 5 at once", p["intro"])
        self.assertEqual([s["id"] for s in p["sections"]],
                         ["bridges", "talking", "conversations", "house-rules", "signin"])

    def test_one_status_row_a_bridge(self):
        [status] = self.section(self.page(), "bridges")["blocks"]
        self.assertEqual(status["items"], [
            {"label": "Chat", "tone": "ok", "text": "Up · 1 talking", "meta": "4s", "page": "bridge/chat"},
            {"label": "Talk", "tone": "ok", "text": "Up · 1 turn today", "meta": "2s", "page": "bridge/talk"},
        ])

    def test_talking_now_and_waiting(self):
        [lst] = self.section(self.page(), "talking")["blocks"]
        self.assertEqual(lst["items"], [
            {"text": "Pat", "meta": "Chat · #general · active 4s, 2 queued", "tone": "ok", "page": "conv/c-1"},
            {"text": "Sam", "meta": "Chat · #b · waiting", "tone": "warn"},
        ])

    def test_a_bridges_own_page(self):
        p = self.page("bridge/chat")
        self.assertEqual(p["title"], "Chat")
        [stats] = p["sections"][0]["blocks"][:1]
        self.assertEqual(p["sections"][1]["title"], "Now")
        [now] = p["sections"][1]["blocks"]
        self.assertEqual(stats["items"], [
            {"label": "State", "value": "running", "detail": "checked just now", "tone": "ok"},
            {"label": "Bodies", "value": "1/5", "detail": "1 talking, 1 waiting"},
            {"label": "Today", "value": "2 turns", "detail": "1 sent"},
            {"label": "Pickup, 24h", "value": "median 24s", "detail": "max 28s"},
        ])
        # Now: a row per conversation, talking or waiting (not a sentence under the tiles).
        self.assertEqual(now, {"type": "list", "items": [
            {"text": "#general with Pat", "meta": "active 4s · 2 queued", "page": "conv/c-1"},
            {"text": "#b with Sam", "meta": "waiting: every body is busy", "tone": "warn"}]})
        self.assertEqual(len(p["sections"][0]["blocks"]), 1)   # no socket, no note; Now is its own card
        # Idle: the Bodies tile says so, and nothing hangs under the tiles.
        talk = self.page("bridge/talk")["sections"][0]
        self.assertEqual(talk["blocks"][0]["items"][1]["detail"], "idle, nobody waiting")
        self.assertNotIn("list", [x["type"] for x in talk["blocks"]])
        self.assertEqual([i["text"] for i in p["sections"][2]["blocks"][0]["items"]], ["Pat"], "only its own conversations")

    def test_socket_rotation_note(self):
        talk = self.page("bridge/talk")["sections"][0]
        self.assertEqual(talk["blocks"][0]["items"][0]["value"], "socket up")
        note = talk["blocks"][-1]
        self.assertEqual(note["tone"], "muted")
        self.assertEqual(note["text"], "Socket reconnected 3 times in 24h, each back up within 2s; "
                                       "Talk rotates it every few hours.")

    def test_no_note_when_a_drop_was_an_outage(self):
        self.events("2026-03", [ev(NOW - 900, "talk", "socket", state="down"),
                                ev(NOW - 600, "talk", "socket", state="up")])
        talk = self.page("bridge/talk")["sections"][0]
        self.assertNotIn("muted", [b.get("tone") for b in talk["blocks"]])

    def test_a_dark_bridge(self):
        self.write(snap("talk", NOW - 600, interval=5))
        p = self.page()
        row = self.section(p, "bridges")["blocks"][0]["items"][1]
        self.assertEqual((row["tone"], row["text"]), ("bad", "No check in 10m · 1 turn today"))
        dark = self.section(p, "bridge-talk")
        self.assertEqual(dark["tone"], "bad")
        self.assertTrue(dark["blocks"][0]["text"].startswith("Dark: no snapshot since "))
        self.assertEqual(dark["blocks"][1]["page"], "loom:p/box")
        self.assertEqual(self.page("bridge/talk")["sections"][0]["blocks"][0]["items"][0]["value"], "dark")

    def test_conversations(self):
        sec = self.section(self.page(), "conversations")
        self.assertTrue(sec["side"])
        self.assertEqual(sec["blocks"][0]["items"], [
            {"text": "Pat", "meta": "Chat · #general · 2 turns", "avatar": "P", "page": "conv/c-1"},
            {"text": "Sam Lee", "meta": "Talk · DM · 1 turn", "avatar": "SL", "page": "conv/c-2"}])

    def test_ago_phrases_say_just_now_under_a_minute(self):
        self.assertEqual([loomui._ago_phrase(x) for x in (0, 5, 59, 60, 600, 7200)],
                         ["just now", "just now", "just now", "60s ago", "10m ago", "2h ago"])
        self.assertEqual(loomui._ago(0), "0s")             # a cell or a duration keeps its number

    def test_model_ids_read_as_people_say_them(self):
        self.assertEqual([loomui._model(m) for m in ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
                                                     "claude-fable-1-2[1m]", "opus-5-5", "model-x", "the runner's default")],
                         ["Opus 5.5", "Sonnet 5", "Haiku 4.5", "Fable 1.2", "Opus 5.5", "model-x", "the runner's default"])

    def test_house_rules_name_a_known_model(self):
        kv = self.section(self.page(rules={"preferred_model": "claude-opus-5-5"}), "house-rules")["blocks"][0]["items"]
        self.assertEqual(kv[2], {"label": "Model for new conversations", "value": "Opus 5.5"})

    def test_house_rules(self):
        rules = self.section(self.page(), "house-rules")
        self.assertTrue(rules["side"])
        kv, note = rules["blocks"]
        self.assertEqual(kv["items"], [{"label": "Guests can't use", "value": "mail, calendar"},
                                       {"label": "Nobody can use", "value": "handoff"},
                                       {"label": "Model for new conversations", "value": "model-x"}])
        self.assertEqual(note["tone"], "muted")
        self.assertIn("shuttle instance file", note["text"])

    def test_never_prints_content_or_body_ids(self):
        out = json.dumps(self.page()) + json.dumps(loomui.manifest(self.view()))
        for secret in ("message content", "secret-body-id", "b1", "b2", "b3"):
            self.assertNotIn(f'"{secret}"', out)
            self.assertNotIn(secret + " ", out)

    def test_unknown_path_is_not_found(self):
        for path in ("nope", "bridge/nope", "x/y"):
            self.assertEqual(self.page(path), {"title": "Not found", "sections": []})


class Window(Fixture):
    """The last 24h, from this month's file and last month's; nothing older is read."""

    def test_24h_spans_the_month_boundary(self):
        self.write(snap("chat", NOW - 1))
        self.events("2026-02", [ev(NOW - DAY_PLUS, "chat", "picked", cid="c-old", venue="#old", who=["Pat"], wait_s=999),
                                ev(NOW - 20 * 3600, "chat", "picked", cid="c-1", venue="#feb", who=["Pat"], wait_s=10)])
        self.events("2026-03", [ev(NOW - 3600, "chat", "picked", cid="c-2", venue="#mar", who=["Pat"], wait_s=30),
                                ev(NOW + 60, "chat", "picked", cid="c-3", venue="#future", who=["Pat"], wait_s=1)])
        self.events("2026-01", [ev(NOW - 3600, "chat", "picked", cid="c-x", venue="#jan", who=["Pat"], wait_s=5)])
        p = loomui.page(self.view(), "")
        rows = [i["meta"] for s in p["sections"] if s["id"] == "conversations" for i in s["blocks"][0]["items"]]
        self.assertEqual(sorted(rows), ["Chat · #feb · 1 turn", "Chat · #mar · 1 turn"])
        stats = loomui.page(self.view(), "bridge/chat")["sections"][0]["blocks"][0]["items"]
        self.assertEqual(stats[2]["value"], "1 turn")          # today: only March 1st's
        self.assertEqual(stats[3], {"label": "Pickup, 24h", "value": "median 20s", "detail": "max 30s"})

    def test_mid_month_reads_only_this_month(self):
        now = time.mktime((2026, 3, 20, 12, 0, 0, 0, 0, -1))
        self.assertEqual([p.name for p in loomui._month_files(self.dir, now - loomui.DAY, now)],
                         ["events-2026-03.jsonl"])

    def test_torn_lines_are_skipped(self):
        self.write(snap("chat", NOW - 1))
        (self.dir / "events-2026-03.jsonl").write_text(
            '{"kind": "picked", "t": \n' + json.dumps(ev(NOW - 5, "chat", "picked", venue="#ok", who=[], wait_s=1)) + "\n")
        p = loomui.page(self.view(), "bridge/chat")
        self.assertEqual(p["sections"][1]["blocks"][0]["items"], [{"text": "nobody named", "meta": "Chat · #ok · 1 turn"}])

    def test_empty_window(self):
        self.write(snap("chat", NOW - 1))
        p = loomui.page(self.view(), "bridge/chat")
        stats = p["sections"][0]["blocks"][0]["items"]
        self.assertEqual(stats[3], {"label": "Pickup, 24h", "value": "none"})
        self.assertEqual(p["sections"][1]["blocks"][0]["tone"], "muted")


DAY_PLUS = loomui.DAY + 60


class PeopleLinks(Fixture):
    """A name links to loom's person page only when its slug is known."""

    def rows(self, recs, people=None):
        self.write(snap("chat", NOW - 1))
        if people is not None:
            self.people.write_text(json.dumps(people))
        self.events("2026-03", recs)
        [sec] = [s for s in loomui.page(self.view(), "")["sections"] if s["id"] == "conversations"]
        return [[i["meta"].rsplit(" · ", 1)[0], {"text": i["text"], "page": i["page"]} if "page" in i else i["text"]]
                for i in sec["blocks"][0]["items"]]

    def picked(self, venue, who, **f):
        return ev(NOW - 100, "chat", "picked", venue=venue, who=who, wait_s=1, **f)

    def test_people_json_name_to_slug(self):
        rows = self.rows([self.picked("#a", ["Pat Smith"])],
                         {"people": {"pat-smith": {"name": "Pat Smith", "tier": 2}}})
        self.assertEqual(rows[0][1], {"text": "Pat Smith", "page": "loom:people/pat-smith"})

    def test_the_events_members_slug(self):
        rows = self.rows([self.picked("#a", ["Pat"], members=["pat-smith"])])
        self.assertEqual(rows[0][1], {"text": "Pat", "page": "loom:people/pat-smith"})

    def test_no_slug_is_guessed_from_a_name(self):
        rows = self.rows([self.picked("#a", ["Pat Smith"])], {"people": {}})
        self.assertEqual(rows[0][1], "Pat Smith")

    def test_shared_names_are_not_linked(self):
        people = {"people": {"pat-a": {"name": "Pat"}, "pat-b": {"name": "Pat"}},
                  "agents": {"pat-bot": {"name": "Pat", "kind": "bot"}}}
        rows = self.rows([self.picked("#a", ["Pat"])], people)
        self.assertEqual(rows[0][1], "Pat")

    def test_an_agent_links_to_its_entry_by_name_or_by_the_label_a_body_sees(self):
        # loom's docs/specs/agents-in-people.md: agents are directory entries too.
        people = {"people": {}, "agents": {"mark": {"name": "Mark", "kind": "peer agent bot"}, "gabe": {"name": "Gabe"}}}
        rows = self.rows([self.picked("#a", ["Mark (peer agent bot)"]), self.picked("#b", ["Gabe"]),
                          self.picked("#c", ["Gabe (agent)"])], people)
        self.assertEqual(sorted((r[1]["text"], r[1]["page"]) for r in rows),
                         [("Gabe", "loom:people/gabe"), ("Gabe", "loom:people/gabe"),
                          ("Mark", "loom:people/mark")])      # an agent by its name, never the label a body sees

    def test_a_group_is_not_linked(self):
        rows = self.rows([self.picked("chat-group", ["Pat", "Sam"], members=["pat", "sam"])])
        self.assertEqual(rows[0][:2], ["Chat · group DM", "Pat, Sam"])

    def test_a_bad_people_file_links_nothing(self):
        self.people.write_text("{not json")
        rows = self.rows([self.picked("#a", ["Pat"])])
        self.assertEqual(rows[0][1], "Pat")


# ---------------------------------------------------------------- the command


class Clock(unittest.TestCase):
    def test_the_date_always_and_a_twelve_hour_time(self):
        at = lambda *hm: time.mktime((2026, 3, 1, *hm, 0, 0, 0, -1))
        self.assertEqual(loomui._clock(at(11, 0), NOW), "Mar 1, 11:00 AM")      # today too
        self.assertEqual(loomui._clock(at(0, 5), NOW), "Mar 1, 12:05 AM")
        self.assertEqual(loomui._clock(at(12, 0), NOW), "Mar 1, 12:00 PM")
        self.assertEqual(loomui._clock(at(23, 59), NOW), "Mar 1, 11:59 PM")
        self.assertEqual(loomui._clock(time.mktime((2025, 12, 31, 14, 2, 0, 0, 0, -1)), NOW), "Dec 31 2025, 2:02 PM")


class Conversation(Fixture):
    """A conversation's own page (conv/<cid>): what came of it, from its bodies'
    `did` records, and its turns, from the board's events. Never its words."""

    def setUp(self):
        super().setUp()
        self.people.write_text(json.dumps({"people": {"pat-lee": {"name": "Pat"}},
                                           "agents": {"kit": {"name": "Kit", "kind": "peer agent bot"}}}))
        t = NOW - 3600
        self.events("2026-03", [
            ev(t, "slack", "picked", cid="c-0007", chan="D1", venue="slack-dm", who=["Pat"], members=["pat-lee"], wait_s=12.0, body="abcd1234"),
            ev(t + 30, "slack", "delivered", cid="c-0007", chan="D1", venue="slack-dm", who=["Pat"], n=1, body="abcd1234"),
            ev(t + 95, "slack", "finished", cid="c-0007", chan="D1", venue="slack-dm", body="abcd1234", outcome="replied"),
            ev(t + 96, "slack", "did", body="abcd1234", chan="D1", venue="slack-dm",
               tasks=[{"id": "t-1682", "did": ["claim", "close"]}], prs=[{"ref": "acme/widgets#371", "did": "merged"}],
               knowledge=[{"slug": "ours/site/notes", "did": "wrote"}], asks=[{"kind": "secret", "what": "github-token"}],
               memories=2, files=3, commits=1, pushes=1, tools={"Bash": 8, "loom remember": 2},
               cost={"usd": 0.9877, "ms": 89934, "started": 0, "added": 3, "removed": 1, "models": {"claude-opus-5-5": 0.9}}),
            ev(t + 97, "slack", "did", body="ffff0000", tasks=[{"id": "t-9", "did": ["close"]}]),   # another conversation's body
            ev(t + 200, "slack", "picked", cid="c-0008", chan="D2", venue="slack-dm", who=["Someone"], wait_s=1.0, body="ffff0000"),
            ev(t + 300, "discord", "picked", cid="c-0010", chan="C9", venue="#general", who=["Pat"], members=["pat-lee"], wait_s=3.0, body="11112222"),
            ev(t + 400, "discord", "picked", cid="c-0010", chan="C9", venue="#general", who=["Kit (peer agent bot)"], wait_s=4.0, body="33334444"),
        ])

    def test_what_came_of_it_and_its_turns(self):
        page = loomui.page(self.view(agent_slug="robin"), "conv/c-0007")
        check_page(self, page)
        self.assertEqual(page["title"], "Slack DM with Pat")
        self.assertEqual(page["intro"], "Robin has answered 1 turn here since Mar 1, 11:00 AM, the last 60m ago.")
        came = next(s for s in page["sections"] if s["id"] == "came-of-it")
        items = came["blocks"][0]["items"]
        self.assertEqual([(i["text"], i.get("page") or i.get("href")) for i in items], [
            ("t-1682", "loom:p/tasks/task/t-1682"),
            ("acme/widgets#371", "https://github.com/acme/widgets/pull/371"),
            ("ours/site/notes", "loom:knowledge/ours/site/notes"),
            ("Asked for a token: github-token", "loom:inbox")])
        self.assertEqual(items[0]["meta"], "Task · claimed, closed")
        self.assertEqual(came["blocks"][1]["text"], "2 memories saved · 3 files changed · 1 commit · 1 push.")
        self.assertNotIn("t-9", json.dumps(page))          # only this conversation's bodies
        work = {i["label"]: i["value"] for i in next(s for s in page["sections"] if s["id"] == "work")["blocks"][0]["items"]}
        self.assertEqual((work["Time worked"], work["Cost"], work["Lines"], work["Models"]), ("89s", "$0.99", "+3 −1", "Opus 5.5"))
        who = next(s for s in page["sections"] if s["id"] == "who")["blocks"][0]["items"]
        self.assertEqual(who, [{"text": "Robin", "agent": "robin", "meta": "Answers here", "page": "loom:a/robin"},
                               {"text": "Pat", "avatar": "P", "page": "loom:people/pat-lee"}])
        turns = next(s for s in page["sections"] if s["id"] == "turns")["blocks"][0]
        self.assertEqual(turns["columns"], ["When", "From", "Waited", "Took", "Ended", "Sent"])
        self.assertEqual(turns["rows"], [["Mar 1, 11:00 AM", "Pat", "12s", "1m", "Replied", "1"]])

    def test_whose_it_is_and_a_peer_agent_as_itself(self):
        page = loomui.page(self.view(agent_slug="robin"), "conv/c-0010")
        check_page(self, page)
        who = next(s for s in page["sections"] if s["id"] == "who")["blocks"][0]["items"]
        self.assertEqual([(i["text"], i.get("agent"), i.get("meta")) for i in who],
                         [("Robin", "robin", "Answers here"), ("Pat", None, None), ("Kit", "kit", "Peer agent")])
        rows = next(s for s in page["sections"] if s["id"] == "turns")["blocks"][0]["rows"]
        self.assertEqual([r[1] for r in rows], ["Kit", "Pat"])       # newest first; Kit by name, not its label

    def test_its_rows_link_to_it(self):
        page = loomui.page(self.view(), "")
        rows = next(s for s in page["sections"] if s["id"] == "conversations")["blocks"][0]["items"]
        self.assertEqual({(r["text"], r.get("page")) for r in rows}, {("Pat", "conv/c-0007"), ("Someone", "conv/c-0008"),
                                                                     ("Pat", "conv/c-0010"), ("Kit", "conv/c-0010")})

    def test_live_and_its_doing_link_to_it(self):
        self.write(snap("slack", NOW - 5, talking=[{"cid": "c-0007", "venue": "slack-dm", "who": ["Pat"], "active_s": 40, "body": "abcd1234"}]))
        view = self.view()
        page = loomui.page(view, "conv/c-0007")
        self.assertEqual(page["sections"][0]["blocks"][0]["title"], "Talking now")
        self.assertEqual([d.get("page") for d in loomui._doing(view)], ["conv/c-0007"])

    def test_before_any_record_it_says_so(self):
        self.events("2026-03", [ev(NOW - 60, "discord", "picked", cid="c-0009", chan="C", venue="#general", who=["Ann"], wait_s=2.0, body="12345678")])
        page = loomui.page(self.view(), "conv/c-0009")
        self.assertEqual(page["title"], "#general on Discord")
        came = next(s for s in page["sections"] if s["id"] == "came-of-it")
        self.assertIn("Nothing recorded yet", came["blocks"][0]["text"])

    def test_a_bad_record_never_breaks_the_page(self):
        """A did record is a body's word: junk in it is skipped, a bad slug isn't a link, and a record from
        another bridge's body with the same id isn't this conversation's."""
        self.events("2026-03", [
            ev(NOW - 3000, "slack", "did", body="abcd1234", memories="lots", files=None, tools=["Bash"], tasks="t-1",
               knowledge=[{"slug": "../../act/x?y", "did": "wrote"}, {"slug": "ok/page", "did": "wrote"}],
               cost={"usd": "NaN", "ms": "x", "models": ["a"]}),
            ev(NOW - 2999, "discord", "did", body="abcd1234", tasks=[{"id": "t-666", "did": ["close"]}]),
        ])
        page = loomui.page(self.view(), "conv/c-0007")
        check_page(self, page)
        items = next(s for s in page["sections"] if s["id"] == "came-of-it")["blocks"][0]["items"]
        self.assertEqual([(i["text"], i.get("page")) for i in items], [("../../act/x?y", None), ("ok/page", "loom:knowledge/ok/page")])
        self.assertNotIn("t-666", json.dumps(page))

    def test_unknown_or_malformed_is_not_found(self):
        for p in ("conv/c-4242", "conv/../x", "conv/"):
            self.assertEqual(loomui.page(self.view(), p)["title"], "Not found")


class Entry(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="shuttle-loomui-inst-"))
        now = d / "now"
        now.mkdir()
        (now / "chat.json").write_text(json.dumps(snap("chat", time.time())))
        (d / "secrets").mkdir()
        (d / "people.json").write_text(json.dumps({"people": {"pat-smith": {"name": "Pat Smith"}}}))
        (d / "shuttle.json").write_text(json.dumps({
            "agent": {"slug": "a", "name": "Robin"}, "now_dir": str(now), "secrets": str(d / "secrets"),
            "guest_deny": ["mcp__mail"], "always_deny": ["mcp__handoff"], "preferred_model": "model-x"}))
        self.env = {**os.environ, "SHUTTLE_INSTANCE": str(d / "shuttle.json")}

    def run_tool(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "loom-shuttle"), *args], input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=10)

    def test_ui_json_is_the_manifest(self):
        r = self.run_tool("ui", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)
        self.assertEqual((m["v"], m["tab"]["label"], m["tile"]["value"]), (2, "Bridges", "1 live"))

    def test_an_agent_owner_sees_these_bridges_only_if_they_own_the_agent(self):
        # loom's docs/specs/owners.md: the instance serves agent "a".
        page = lambda caller: json.loads(self.run_tool("ui", "page", "--json", stdin=json.dumps({"path": "", "caller": caller})).stdout)
        theirs = page({"kind": "owner", "who": "loren", "agents": ["a"]})
        self.assertIn("Robin", theirs["intro"])
        self.assertNotIn("house-rules", [x["id"] for x in theirs["sections"]])
        self.assertNotIn("signin", [x["id"] for x in theirs["sections"]])
        other = page({"kind": "owner", "who": "loren", "agents": ["b"]})
        self.assertIn("isn't one of your agents", json.dumps(other))
        self.assertNotIn("Pat Smith", json.dumps(other))
        signin = json.loads(self.run_tool("ui", "page", "--json", stdin=json.dumps({"path": "signin", "caller": {"kind": "owner", "agents": ["a"]}})).stdout)
        self.assertIn("household owner's", json.dumps(signin))
        acted = json.loads(self.run_tool("ui", "act", "--json", stdin=json.dumps({"action": "signin-start", "fields": {"harness": "claude"}, "caller": {"kind": "owner", "agents": ["a"]}})).stdout)
        self.assertFalse(acted["ok"])
        # The manifest says it understands agent owners, and its asks name the agent.
        m = json.loads(self.run_tool("ui", "--json").stdout)
        self.assertEqual(m["owners"], 2)
        # A household owner's page, as before.
        self.assertIn("house-rules", [x["id"] for x in page({"kind": "owner", "who": "pat", "agents": "*"})["sections"]])

    def test_ui_page_json_reads_the_request_on_stdin(self):
        r = self.run_tool("ui", "page", "--json", stdin=json.dumps({"path": "", "query": {}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads(r.stdout)
        self.assertEqual(p["title"], "Bridges")
        self.assertIn("Robin", p["intro"])
        rules = [x for x in p["sections"] if x["id"] != "signin"][-1]
        self.assertEqual(rules["blocks"][0]["items"][0]["value"], "mail")
        self.assertEqual(p["sections"][-1]["blocks"][0]["page"], "signin")
        r = self.run_tool("ui", "page", "--json", stdin=json.dumps({"path": "elsewhere", "query": {}}))
        self.assertEqual(json.loads(r.stdout), {"title": "Not found", "sections": []})

    def test_no_actions(self):
        r = self.run_tool("ui", "act", "--json", stdin=json.dumps({"action": "x", "path": "", "fields": {}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_anything_but_ui_is_usage(self):
        self.assertEqual(self.run_tool("frob").returncode, 2)
        self.assertEqual(self.run_tool("ui", "frob", "--json").returncode, 2)


if __name__ == "__main__":
    unittest.main()
