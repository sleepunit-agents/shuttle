"""The orchestrating core functions, driven through a fake adapter.

FakeBridge plays the part the adapters (adapters/*.py) play: it builds the Config,
supplies the hooks, and routes each lifted name back into the core, the way
the bridges' shims do. Nothing here starts a process or posts anything.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from shuttle import core
from shuttle.config import Config
from shuttle.runner import ClaudeRunner

AID = "abcd1234-0000-0000-0000-000000000000"


class FakeInbox:
    def __init__(self):
        self.boxes, self.read, self.closed, self.swept = {}, {}, set(), []

    def open_inbox(self, aid, label, chan, bridge):
        self.boxes.setdefault(aid, [])
        self.opened = getattr(self, "opened", []) + [(aid, label, chan, bridge)]

    def put(self, aid, items):
        if aid in self.closed:
            return False
        self.boxes.setdefault(aid, []).extend(items)
        return True

    def delivered(self, aid):
        return [{"id": i, "rt": rt} for i, rt in self.read.get(aid, {}).items()]

    def close(self, aid):
        self.closed.add(aid)
        read = self.read.get(aid, {})
        return [it for it in self.boxes.get(aid, []) if it["id"] not in read]

    @staticmethod
    def answered(posted, read_at):
        return bool(read_at) and posted > read_at

    def sweep(self, name, owned8, gc):
        self.swept.append((name, owned8, gc))
        return sorted(gc - owned8)


class FakeNowlog:
    def __init__(self):
        self.events, self.touched = [], []

    def emit(self, name, kind, **kw):
        self.events.append((name, kind, kw))

    def touch(self, name, chan, now):
        self.touched.append((name, chan))


class FakeRunner:
    """Only what the core asks the runner directly; the rest goes through the
    fake's shims below, as on a real adapter. How failures read is Claude's."""
    working = staticmethod(lambda status: status in ("running", "working", "busy"))
    registry_hint, auth_hint = ClaudeRunner.registry_hint, ClaudeRunner.auth_hint
    AUTH_FAILURE_RE = ClaudeRunner.AUTH_FAILURE_RE
    api_error, auth_failure = ClaudeRunner.api_error, ClaudeRunner.auth_failure


class FakeBridge:
    CONV_NOUN = "rooms"

    def __init__(self, root, name="fake"):
        self.cfg = Config(name=name, state_dir=root / f"kit-{name}", alert=Path("/fake/alert"),
                          agent="kit", tag="kit", always_deny=("mcp__handoff",))
        self.runner = FakeRunner()
        self.inbox, self.nowlog = FakeInbox(), FakeNowlog()
        self.present = []           # what `claude agents` lists; None = the call failed
        self.killed, self.reacts, self.hung, self.pages, self.saved = [], [], [], [], []
        self.quiet, self.parked, self.done, self.posted, self.replied = {}, set(), set(), {}, set()
        self.timeline = {}

    # hooks
    def _cfg(self):
        return self.cfg

    msg_key = staticmethod(lambda m: m.get("id"))
    msg_order = staticmethod(lambda m: int(m["id"]))
    rec_label = staticmethod(lambda rec: f"in {rec.get('chan')}")
    rec_venue = staticmethod(lambda rec: "fake-room")
    rec_who = staticmethod(lambda rec: ["Pat"])
    batch_who = staticmethod(lambda rec, batch: ["Pat"])
    item_text = staticmethod(lambda m: f"Pat: {m['text']}")

    # platform and process calls, faked
    def agents(self, strict=False):
        return None if self.present is None else list(self.present)

    def kill_agent(self, aid):
        self.killed.append(aid)

    def react(self, chan, msg, add=True):
        self.reacts.append((msg, add))

    def notify_hung(self, chan, quiet):
        self.hung.append(chan)

    def page(self, msg):
        self.pages.append(msg)

    def save_state(self, state):
        self.saved.append(json.dumps(state))

    def job_quiet_s(self, aid):
        return self.quiet.get(aid, 5.0)

    def job_parked(self, aid):
        return aid in self.parked

    def job_done(self, aid):
        return aid in self.done

    def last_post_ts(self, aid):
        return self.posted.get(aid, 0.0)

    def session_replied(self, aid, chan, since):
        return aid in self.replied

    def _last_timeline(self, aid):
        return self.timeline.get(aid)

    # the lifted names route back into the core, as the bridges' shims do
    def session_failed(self, aid):
        return core.session_failed(self, aid)

    def session_died_silent(self, aid, rec):
        return core.session_died_silent(self, aid, rec)

    def death_reason(self, aid, rec):
        return core.death_reason(self, aid, rec)

    def _finished(self, aid, rec, why=None):
        core.finished(self, aid, rec, why)

    def _release_spawned_msgs(self, state, rec):
        core.release_spawned_msgs(self, state, rec)

    def _clear_ack(self, rec):
        core.clear_ack(self, rec)

    def _drop(self, state, aid, rec, retry=None, ended=False):
        core.drop(self, state, aid, rec, retry, ended)

    def _drop_inner(self, state, aid, rec, retry, ended):
        core.drop_inner(self, state, aid, rec, retry, ended)

    def holding_body(self, state, cid):
        return core.holding_body(state, cid)


def msg(i, text="hi"):
    return {"id": str(i), "text": text}


def owned(**kw):
    rec = {"idle_since": 0, "chan": "R1", "msgs": [msg(1)], "attempt": 1,
           "spawned_at": time.time() - 60, "cid": "c-1", "ack_msg": "1"}
    rec.update(kw)
    return rec


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-reap-"))
        self.b = FakeBridge(self.root)


class Reap(Base):
    def test_working_body_is_live_and_keeps_its_room(self):
        state = {"owned": {AID: owned()}}
        self.b.present = [{"id": AID, "status": "working"}]
        self.assertEqual(core.reap_and_count(self.b, state), (1, []))
        self.assertTrue(core.channel_busy(self.b, state, "R1"))
        self.assertEqual(self.b.killed, [])

    def test_hung_body_is_killed_told_and_dropped(self):
        state = {"owned": {AID: owned()}}
        self.b.present = [{"id": AID, "status": "working"}]
        self.b.quiet[AID] = self.b.cfg.hang_kill_s
        core.reap_and_count(self.b, state)
        self.assertEqual((self.b.killed, self.b.hung), ([AID], ["R1"]))
        self.assertNotIn(AID, state["owned"])
        self.assertIn(("1", False), self.b.reacts)

    def test_parked_body_frees_the_room_then_is_reaped(self):
        state = {"owned": {AID: owned()}}
        self.b.present = [{"id": AID, "status": "working"}]
        self.b.parked.add(AID)
        self.assertEqual(core.reap_and_count(self.b, state)[0], 1)
        self.assertFalse(core.channel_busy(self.b, state, "R1"))
        self.b.quiet[AID] = self.b.cfg.parked_kill_s
        core.reap_and_count(self.b, state)
        self.assertEqual(self.b.killed, [AID])

    def test_done_body_is_reaped_the_same_tick(self):
        state = {"owned": {AID: owned()}}
        self.b.present = [{"id": AID, "status": "idle"}]
        self.b.done.add(AID)
        self.b.replied.add(AID)
        self.assertEqual(core.reap_and_count(self.b, state), (0, []))
        self.assertEqual(self.b.nowlog.events[-1][2]["outcome"], "replied")

    def test_silent_death_is_retried_with_what_it_was_handed(self):
        state = {"owned": {AID: owned()}}
        core.hand_over(self.b, state, "R1", AID, [msg(2)])
        self.b.present = []                       # vanished from the registry
        live, retries = core.reap_and_count(self.b, state)
        self.assertEqual(live, 0)
        self.assertEqual(retries[0]["why"], "no timeline")
        self.assertEqual([m["id"] for m in retries[0]["msgs"]], ["1", "2"])
        self.assertNotIn("R1", state.get("leftover", {}))

    def test_a_body_that_replied_is_never_retried(self):
        state = {"owned": {AID: owned()}}
        self.b.timeline[AID] = {"state": "blocked", "text": "API Error: 529"}
        self.b.replied.add(AID)
        self.assertEqual(core.reap_and_count(self.b, state), (0, []))

    def test_registry_failure_drops_nothing_and_pages_once(self):
        state = {"owned": {AID: owned()}}
        self.b.present = None
        for _ in range(self.b.cfg.alert_after + 3):
            self.assertEqual(core.reap_and_count(self.b, state), (1, []))
        self.assertEqual(len(self.b.pages), 1)
        self.assertTrue(self.b.pages[0].startswith("kit-fake: "), self.b.pages[0])
        self.assertIn("keep their rooms", self.b.pages[0])
        self.assertIn(AID, state["owned"])

    def test_legacy_scalar_record_is_upgraded(self):
        state = {"owned": {AID: 0}}
        self.b.present = [{"id": AID, "status": "working"}]
        core.reap_and_count(self.b, state)
        self.assertEqual(state["owned"][AID]["idle_since"], 0)


class HandOverAndDrop(Base):
    def test_hand_over_marks_reacts_and_saves(self):
        state = {"owned": {AID: owned()}}
        self.assertTrue(core.hand_over(self.b, state, "R1", AID, [msg(3, "x"), msg(2, "y")]))
        rec = state["owned"][AID]
        self.assertEqual(self.b.inbox.boxes[AID][0]["text"], "Pat: x")
        self.assertEqual(self.b.reacts, [("3", True)])         # the newest, by msg_order
        self.assertEqual([a["msg"] for a in rec["acks"]], ["3"])
        self.assertEqual(rec["handed"], 2)
        self.assertEqual(set(state["spawned_msgs"]), {"2", "3"})
        self.assertEqual(len(self.b.saved), 1)
        kind, kw = self.b.nowlog.events[-1][1:]
        self.assertEqual((kind, kw["n"], kw["venue"], kw["who"]), ("delivered", 2, "fake-room", ["Pat"]))
        self.assertNotIn('"x"', json.dumps(kw))               # the board never carries text

    def test_closed_inbox_refuses(self):
        state = {"owned": {AID: owned()}}
        self.b.inbox.closed.add(AID)
        self.assertFalse(core.hand_over(self.b, state, "R1", AID, [msg(2)]))
        self.assertEqual(self.b.saved, [])

    def test_killed_body_passes_on_what_it_read_but_never_answered(self):
        state = {"owned": {AID: owned()}}
        core.hand_over(self.b, state, "R1", AID, [msg(2)])
        core.hand_over(self.b, state, "R1", AID, [msg(3)])
        core.hand_over(self.b, state, "R1", AID, [msg(4)])
        self.b.inbox.read[AID] = {"2": 100.0, "3": 300.0}     # 4 never read
        self.b.posted[AID] = 200.0                             # answered 2, not 3
        core.drop(self.b, state, AID, state["owned"][AID])
        self.assertEqual([m["id"] for m in state["leftover"]["R1"]], ["3", "4"])
        self.assertEqual(sorted(m for m, add in self.b.reacts if not add), ["1", "2", "3", "4"])
        self.assertEqual(state["inbox_gc"], ["abcd1234"])

    def test_body_that_ended_itself_handled_what_it_read(self):
        state = {"owned": {AID: owned()}}
        core.hand_over(self.b, state, "R1", AID, [msg(2)])
        self.b.inbox.read[AID] = {"2": 100.0}
        core.drop(self.b, state, AID, state["owned"][AID], ended=True)
        self.assertNotIn("R1", state.get("leftover", {}))

    def test_a_failing_drop_still_forgets_the_body(self):
        state = {"owned": {AID: owned()}}
        self.b._drop_inner = lambda *a: (_ for _ in ()).throw(RuntimeError("odd"))
        core.drop(self.b, state, AID, state["owned"][AID])
        self.assertNotIn(AID, state["owned"])
        self.assertEqual(self.b.reacts, [("1", False)])

    def test_sweep_uses_this_bridges_name(self):
        state = {"owned": {AID: owned()}, "inbox_gc": ["dead0000"]}
        core.inbox_sweep(self.b, state)
        self.assertEqual(self.b.inbox.swept, [("fake", {"abcd1234"}, {"dead0000"})])
        self.assertEqual(state["inbox_gc"], [])


class Board(Base):
    def test_hold_emits_wait_once_per_reason(self):
        state, holds = {}, []
        for reason in ("cap", "cap", "memory"):
            core.record_hold(self.b, state, holds, cid="c-1", chan="R1", venue="fake-room",
                             who=["Pat"], n=1, since=1.0, reason=reason)
        self.assertEqual(len(holds), 3)
        self.assertEqual([e[2]["reason"] for e in self.b.nowlog.events], ["cap", "memory"])

    def test_talking_items_use_the_adapters_labels(self):
        state = {"owned": {AID: owned(handed=2), "nochan00": {"idle_since": 0}}}
        items = core.talking_items(self.b, state)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["venue"], items[0]["who"], items[0]["handed"]),
                         ("fake-room", ["Pat"], 2))


if __name__ == "__main__":
    unittest.main()


class AuthReplay(Base):
    """t-1358: batches given up on for auth wait in state["auth_parked"]; a
    body that started after them and ends cleanly proves auth is back and
    re-queues them, once."""

    CLEAN = {"state": "done", "detail": "answered", "text": "Answered Pat."}

    def parked(self, chan="P1", age=600, **kw):
        rec = {"chan": chan, "attempt": 2, "msgs": [msg(9)], "why": "Login expired",
               "parked_at": time.time() - age, "checked": True}
        rec.update(kw)
        return rec

    def end(self, state, entry=CLEAN, **rec):
        state["owned"][AID] = owned(**rec)
        self.b.present = [{"id": AID, "status": "idle"}]
        self.b.done.add(AID)
        self.b.replied.add(AID)
        self.b.timeline[AID] = entry
        core.reap_and_count(self.b, state)

    def test_a_clean_end_requeues_parked_batches_for_one_more_attempt(self):
        state = {"owned": {}, "auth_parked": [self.parked()]}
        self.end(state)
        q = state["retry_queue"]
        self.assertEqual(([r["chan"] for r in q], q[0]["attempt"]),
                         (["P1"], self.b.cfg.max_attempts - 1))
        self.assertNotIn("checked", q[0])
        self.assertEqual((state["auth_parked"], list(state["auth_replayed"])), ([], ["9"]))

    def test_a_body_older_than_the_outage_proves_nothing(self):
        # It replied before the login lapsed, so death_reason calls it clean.
        state = {"owned": {}, "auth_parked": [self.parked(age=60)]}
        self.end(state, spawned_at=time.time() - 600)
        self.assertEqual((state.get("retry_queue", []), len(state["auth_parked"])), ([], 1))

    def test_a_body_whose_last_entry_is_a_harness_failure_proves_nothing(self):
        state = {"owned": {}, "auth_parked": [self.parked()]}
        self.end(state, {"state": "blocked", "detail": "Login expired", "text": "Login expired"})
        self.assertEqual(len(state["auth_parked"]), 1)

    def test_a_death_does_not_replay(self):
        state = {"owned": {AID: owned()}, "auth_parked": [self.parked()]}
        self.b.present = [{"id": AID, "status": "idle"}]
        self.b.timeline[AID] = {"state": "blocked", "detail": "Login expired", "text": "Login expired"}
        core.reap_and_count(self.b, state)
        self.assertEqual(len(state["auth_parked"]), 1)

    def test_a_conversation_reanswered_since_is_dropped(self):
        state = {"owned": {}, "auth_parked": [self.parked(chan="POKED")],
                 "last_spawn": {"POKED": time.time() - 60}}
        self.end(state)
        self.assertEqual((state.get("retry_queue", []), state["auth_parked"]), ([], []))
