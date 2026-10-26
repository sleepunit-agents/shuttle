"""The dispatch order, through the fake adapter: admit, hand, budget, hold, spawn."""
import time
import unittest

from shuttle import core, dispatch
from tests.test_reap import AID, Base, FakeBridge, msg, owned


class FakeNowlogWithHolds:
    @staticmethod
    def hold_reason(now, last_spawn, cooldown_s, busy, live, cap, avail_mb, min_mb):
        if now - last_spawn < cooldown_s:
            return "cooldown"
        if busy:
            return "queued"
        if live >= cap:
            return "slots_full"
        if avail_mb < min_mb:
            return "low_memory"
        return None


class DispatchBridge(FakeBridge):
    def __init__(self, root):
        super().__init__(root)
        self.verdict, self.blocked, self.avail = "ok", None, 8000
        self.spawns, self.holds_made, self.handed_sources, self.failures = [], [], [], []
        events = self.nowlog
        self.nowlog = type("N", (), {"emit": events.emit, "touch": events.touch,
                                     "hold_reason": staticmethod(FakeNowlogWithHolds.hold_reason)})()

    def admit(self, state, cid, batch, source, ctx):
        return (self.verdict, {"id": cid}) if self.verdict == "ok" else (self.verdict, "why")

    def hand(self, state, conv, aid, batch):
        return core.hand_over(self, state, conv["id"], aid, batch)

    def on_handed(self, state, conv, batch, source):
        self.handed_sources.append(source)

    def spend_blocked(self, state, conv, batch, ctx):
        return self.blocked

    def hold(self, state, holds, conv, batch, reason, rec=None, since=None):
        holds.append({"chan": conv["id"]})
        self.holds_made.append(reason)

    def spawn(self, state, conv, batch, attempt, source, ctx):
        self.spawns.append((source, attempt, [m["id"] for m in batch]))

    def notify_failure(self, chan, why="", parked=False):
        self.failures.append(chan)
        self.parked_notices = getattr(self, "parked_notices", []) + ([chan] if parked else [])

    def mem_available_mb(self):
        return self.avail

    conv_label = staticmethod(lambda conv: conv["id"])

    def channel_busy(self, state, cid):
        return core.channel_busy(self, state, cid)


def retry(**kw):
    rec = {"chan": "R1", "attempt": 1, "msgs": [msg(5)], "spawned_at": time.time()}
    rec.update(kw)
    return rec


class Retries(Base):
    def setUp(self):
        super().setUp()
        self.b = DispatchBridge(self.root)

    def test_spawns_with_the_next_attempt_and_leaves_the_queue(self):
        state = {"owned": {}}
        self.assertEqual(dispatch.run_retries(self.b, state, [], 0, [retry()], {}), 1)
        self.assertEqual((self.b.spawns, state["retry_queue"]), ([("retry", 2, ["5"])], []))
        self.assertIn("R1", state["last_spawn"])

    def test_a_working_body_takes_it_even_with_slots_full(self):
        state = {"owned": {AID: owned()}}
        dispatch.run_retries(self.b, state, [], self.b.cfg.max_live, [retry()], {})
        self.assertEqual((self.b.spawns, self.b.handed_sources, state["retry_queue"]),
                         ([], ["retry"], []))

    def test_the_budget_drops_it_before_the_slot_check(self):
        self.b.blocked = "guest daily cap"
        state = {"owned": {}}
        dispatch.run_retries(self.b, state, [], self.b.cfg.max_live, [retry()], {})
        self.assertEqual((self.b.holds_made, state["retry_queue"]), ([], []))

    def test_slots_full_holds_it_for_later(self):
        state = {"owned": {}}
        dispatch.run_retries(self.b, state, [], self.b.cfg.max_live, [retry()], {})
        self.assertEqual((self.b.holds_made, len(state["retry_queue"])), (["retry"], 1))

    def test_out_of_attempts_gives_up_before_admitting(self):
        self.b.verdict = "wait"
        state = {"owned": {}}
        dispatch.run_retries(self.b, state, [], 0, [retry(attempt=2, why="no timeline")], {})
        self.assertEqual((self.b.failures, self.b.pages, state["retry_queue"]), (["R1"], [], []))

    def test_an_auth_failure_pages(self):
        state = {"owned": {}}
        dispatch.run_retries(self.b, state, [], 0, [retry(attempt=2, why="Login expired")], {})
        self.assertEqual(len(self.b.pages), 1)
        self.assertTrue(self.b.pages[0].startswith("kit-fake bridge giving up"), self.b.pages[0])

    def test_an_auth_give_up_is_parked_for_replay(self):
        # t-1358: the batch waits for auth to come back; the person is told so.
        state = {"owned": {}}
        dispatch.run_retries(self.b, state, [], 0, [retry(attempt=2, why="Login expired")], {})
        parked = state["auth_parked"]
        self.assertEqual(([p["chan"] for p in parked], [m["id"] for m in parked[0]["msgs"]]),
                         (["R1"], ["5"]))
        self.assertEqual((self.b.parked_notices, state["retry_queue"]), (["R1"], []))

    def test_other_give_ups_and_a_failed_replay_are_not_parked(self):
        # A replay is known by its message ids, not a flag: the owned record a
        # spawn builds carries none, so a flag would be gone by its give-up.
        state = {"owned": {}, "auth_replayed": {"6": time.time()}}
        dispatch.run_retries(self.b, state, [], 0, [retry(attempt=2, why="no timeline"),
                                                    retry(chan="R2", attempt=2, why="Login expired",
                                                          msgs=[msg(6)])], {})
        self.assertEqual((state.get("auth_parked", []), self.b.failures, self.b.parked_notices),
                         ([], ["R1", "R2"], []))

    def test_a_replay_dropped_after_all_gets_the_plain_notice(self):
        self.b.blocked = "guest daily cap"
        state = {"owned": {}, "auth_replayed": {"5": time.time()}}
        dispatch.run_retries(self.b, state, [], 0, [retry()], {})
        self.assertEqual((self.b.failures, self.b.parked_notices), (["R1"], []))

    def test_a_parked_batch_that_outlives_the_ttl_gets_the_plain_notice(self):
        old = {"chan": "P1", "msgs": [msg(9)], "why": "Login expired",
               "parked_at": time.time() - core.AUTH_REPLAY_TTL - 1}
        state = {"owned": {}, "auth_parked": [old], "auth_replayed": {"1": 0}}
        dispatch.run_retries(self.b, state, [], 0, [], {})
        self.assertEqual((self.b.failures, state["auth_parked"], state["auth_replayed"]),
                         (["P1"], [], {}))


class Leftovers(Base):
    def setUp(self):
        super().setUp()
        self.b = DispatchBridge(self.root)

    def test_spawns_a_fresh_body(self):
        state = {"owned": {}, "leftover": {"R1": [msg(5)]}}
        self.assertEqual(dispatch.run_leftovers(self.b, state, [], 0, {}), 1)
        self.assertEqual((self.b.spawns, state["leftover"], state["leftover_since"]),
                         ([("leftover", 1, ["5"])], {}, {}))

    def test_low_memory_holds(self):
        self.b.avail = 10
        state = {"owned": {}, "leftover": {"R1": [msg(5)]}}
        dispatch.run_leftovers(self.b, state, [], 0, {})
        self.assertEqual((self.b.holds_made, list(state["leftover"])), (["low_memory"], ["R1"]))

    def test_no_cooldown_for_leftovers(self):
        state = {"owned": {}, "leftover": {"R1": [msg(5)]}, "last_spawn": {"R1": time.time()}}
        dispatch.run_leftovers(self.b, state, [], 0, {})
        self.assertEqual(len(self.b.spawns), 1)


class Offer(Base):
    def setUp(self):
        super().setUp()
        self.b = DispatchBridge(self.root)

    def test_cooldown_holds_a_new_message(self):
        state = {"owned": {}, "last_spawn": {"R1": time.time()}}
        self.assertEqual(dispatch.offer(self.b, state, [], 0, {"id": "R1"}, [msg(5)], {}),
                         ("held", 0))
        self.assertEqual(self.b.holds_made, ["cooldown"])

    def test_a_working_body_hears_it_inside_the_cooldown(self):
        state = {"owned": {AID: owned()}, "last_spawn": {"R1": time.time()}}
        self.assertEqual(dispatch.offer(self.b, state, [], 1, {"id": "R1"}, [msg(5)], {})[0],
                         "handed")

    def test_not_handed_while_older_leftovers_wait(self):
        state = {"owned": {AID: owned()}, "leftover": {"R1": [msg(4)]}}
        self.assertEqual(dispatch.offer(self.b, state, [], 1, {"id": "R1"}, [msg(5)], {})[0],
                         "held")

    def test_one_hold_per_conversation(self):
        holds = [{"chan": "R1"}]
        state = {"owned": {}, "last_spawn": {"R1": time.time()}}
        dispatch.offer(self.b, state, holds, 0, {"id": "R1"}, [msg(5)], {})
        self.assertEqual((len(holds), self.b.holds_made), (1, []))


class RefusedSpawn(Base):
    """An adapter's spawn that returns False refused to start a body (e.g. its
    instance extension failed): no live slot is spent and no cooldown starts."""

    def setUp(self):
        super().setUp()
        self.b = DispatchBridge(self.root)
        self.b.spawn = lambda *a, **k: False

    def test_offer(self):
        state = {"owned": {}}
        self.assertEqual(dispatch.offer(self.b, state, [], 2, {"id": "R1"}, [msg(5)], {}),
                         ("refused", 2))
        self.assertNotIn("R1", state.get("last_spawn", {}))

    def test_leftovers_and_retries(self):
        state = {"owned": {}, "leftover": {"R1": [msg(5)]}}
        self.assertEqual(dispatch.run_leftovers(self.b, state, [], 3, {}), 3)
        self.assertEqual(dispatch.run_retries(self.b, {"owned": {}}, [], 3, [retry()], {}), 3)

    def test_none_still_counts_as_spawned(self):
        self.b.spawn = lambda *a, **k: None
        self.assertEqual(dispatch.offer(self.b, {"owned": {}}, [], 2, {"id": "R1"}, [msg(5)], {}),
                         ("spawned", 3))


if __name__ == "__main__":
    unittest.main()


class AuthReplayCycle(Base):
    def test_a_replay_that_dies_on_auth_again_is_never_parked_twice(self):
        b = DispatchBridge(self.root)
        dead = {"state": "blocked", "detail": "Login expired", "text": "Login expired"}
        state = {"owned": {}}
        # 1. out of attempts on an auth failure: parked
        dispatch.run_retries(b, state, [], 0, [retry(attempt=2, why="Login expired")], {})
        state["auth_parked"][0]["parked_at"] -= 600
        # 2. a fresh body ends cleanly: the batch is re-queued and spawned
        state["owned"][AID] = owned(chan="X9")
        b.present, b.timeline[AID] = [{"id": AID, "status": "idle"}], {"state": "done", "text": "ok"}
        b.done.add(AID)
        live, retries = core.reap_and_count(b, state)
        dispatch.run_retries(b, state, [], live, retries, {})
        self.assertEqual(b.spawns[-1], ("retry", b.cfg.max_attempts, ["5"]))
        # 3. the replay's body (an owned record with no flags) dies on auth again
        replay = "feed0000-0000-0000-0000-000000000000"
        state["owned"][replay] = owned(chan="R1", msgs=[msg(5)], attempt=b.cfg.max_attempts)
        b.present, b.timeline[replay] = [], dead
        live, retries = core.reap_and_count(b, state)
        self.assertEqual([r["why"] for r in retries], ["ended blocked: Login expired"])
        dispatch.run_retries(b, state, [], live, retries, {})
        self.assertEqual((state["auth_parked"], b.parked_notices[1:], b.failures[-1]), ([], [], "R1"))
