"""Concurrency / serialization regression tests for the Socket.IO handlers.

Each state-mutating handler in app.main takes ``service.lock`` around its
read-modify-persist sequence (mirroring the timer loop). These tests drive the real
handlers concurrently with the socket transport stubbed and assert the table stays
consistent: chips are conserved, no player is double-charged or driven negative, and
overlapping requests don't deal two hands.

They guard against a regression where a handler starts awaiting *between* reading state
and finishing its mutation (or drops the lock) — at which point concurrent handlers would
interleave and corrupt shared state. To make that window observable the DB save is stubbed
to yield control at a real await point, the way production's DB+Redis save does.
"""

import asyncio

import pytest

from app import main
from app.game import player_by_seat, start_hand


pytestmark = pytest.mark.asyncio


CONFIG = {
    "smallBlind": 5,
    "bigBlind": 10,
    "buyIn": 1000,
    "maxSeats": 9,
    "timeoutSeconds": 30,
    "autoNextHandSeconds": 8,
}


@pytest.fixture(autouse=True)
def stub_socket(monkeypatch):
    # The handlers under test call sio.emit / sio.enter_room and read the module-level
    # `contexts` map. Stub the socket layer so no real transport is needed, and isolate
    # per-test state so sessions/players never leak between tests.
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main.sio, "emit", noop)
    monkeypatch.setattr(main.sio, "enter_room", noop)
    monkeypatch.setattr(main, "contexts", {})
    main.service.states = {}
    # service.lock is created at import time bound to the import-time event loop; each async
    # test runs on its own loop, so rebind a fresh lock to the loop the test is running on.
    main.service.lock = asyncio.Lock()
    yield


async def _seated_running_session(player_count=3):
    """Create a persisted session with `player_count` seated players and a hand in progress."""
    main.init_database()
    state, _host_token = await main.service.create(dict(CONFIG), "passcode1")
    session_id = state["sessionId"]
    for seat in range(player_count):
        pid = f"player{seat}"
        main.seat_player(state, pid, f"Player {seat}", seat)
        main.contexts[f"sid{seat}"] = {"sessionId": session_id, "playerId": pid, "isHost": seat == 0}
    start_hand(state)
    await main.service.save(session_id, "hand_started", {"handNumber": state["handNumber"]})
    return session_id


def _total_chips(state):
    stacks = sum(p["stack"] for p in state["players"])
    # handContribution is chips taken out of stacks and sitting in the pot *while a hand
    # runs*. On completion the pot is awarded back into stacks (but handContribution isn't
    # cleared until the next deal), so only count it for a live hand to avoid double-counting.
    if state["status"] == "running":
        stacks += sum(p.get("handContribution", 0) for p in state["players"])
    return stacks


@pytest.fixture
def slow_save(monkeypatch):
    """Make service.save yield control at a real await point, widening the interleave window.

    The in-memory test save is nearly synchronous; production save() awaits DB + Redis I/O.
    Injecting a yield reproduces that suspension so the handlers' mutual exclusion via
    service.lock is what actually keeps the concurrent calls consistent.
    """
    original = main.service.save

    async def slow(session_id, kind, payload=None):
        await asyncio.sleep(0)
        return await original(session_id, kind, payload)

    monkeypatch.setattr(main.service, "save", slow)


async def test_duplicate_action_from_same_player_stays_consistent(slow_save):
    # The current actor fires the same action twice at once (double-click / retried socket
    # message). The handlers must serialize and leave the player charged for at most one call.
    session_id = await _seated_running_session(3)
    state = await main.service.get(session_id)
    starting_chips = _total_chips(state)
    actor = player_by_seat(state, state["hand"]["actorSeat"])
    actor_sid = next(s for s, c in main.contexts.items() if c["playerId"] == actor["id"])
    stack_before = actor["stack"]
    sb_seat = state["hand"]["smallBlindSeat"]
    call_amount = CONFIG["bigBlind"] - (CONFIG["smallBlind"] if actor["seat"] == sb_seat else 0)

    await asyncio.gather(
        main.player_action(actor_sid, {"action": "call"}),
        main.player_action(actor_sid, {"action": "call"}),
    )

    state = await main.service.get(session_id)
    assert _total_chips(state) == starting_chips
    assert all(p["stack"] >= 0 for p in state["players"])
    # Charged for one call at most — never double-charged by an interleaved second action.
    assert stack_before - actor["stack"] <= call_amount


async def test_player_action_and_timeout_fold_stay_consistent(slow_save):
    # A player acts at the same instant the turn timer would auto-fold that same actor.
    # Both paths reach game.act(); serialized, exactly one lands and the table stays sound.
    session_id = await _seated_running_session(3)
    state = await main.service.get(session_id)
    starting_chips = _total_chips(state)
    actor = player_by_seat(state, state["hand"]["actorSeat"])
    actor_sid = next(s for s, c in main.contexts.items() if c["playerId"] == actor["id"])
    state["turnDeadline"] = 1  # already expired -> timer will auto-fold on its tick

    async def one_timer_tick():
        async with main.service.lock:
            st = await main.service.get(session_id)
            timed_out = main.timeout_active_player(st)
            if timed_out:
                await main.service.save(session_id, "timeout_fold", {"playerId": timed_out})
                await main.emit_table(session_id)

    await asyncio.gather(
        main.player_action(actor_sid, {"action": "call"}),
        one_timer_tick(),
    )

    state = await main.service.get(session_id)
    assert _total_chips(state) == starting_chips
    assert all(p["stack"] >= 0 for p in state["players"])
    # The actor did not both fold (timer) and pay a call (own action) in the same breath.
    sb_seat = (state["hand"] or {}).get("smallBlindSeat")
    posted_blind = CONFIG["smallBlind"] if actor["seat"] == sb_seat else 0
    assert not (actor["folded"] and actor["streetContribution"] > posted_blind)


async def _play_hand_to_completion(session_id):
    """Fold each actor in turn until the hand ends uncontested, leaving status 'complete'."""
    while True:
        state = await main.service.get(session_id)
        if state["status"] != "running" or not state.get("hand"):
            return
        actor = player_by_seat(state, state["hand"]["actorSeat"])
        sid = next(s for s, c in main.contexts.items() if c["playerId"] == actor["id"])
        await main.player_action(sid, {"action": "fold"})


async def test_double_start_hand_deals_only_one_hand(slow_save):
    session_id = await _seated_running_session(3)
    # Finish the in-progress hand cleanly so chips return to stacks and status is 'complete'.
    await _play_hand_to_completion(session_id)
    state = await main.service.get(session_id)
    assert state["status"] == "complete"
    starting_hand_number = state["handNumber"]
    assert _total_chips(state) == 3 * CONFIG["buyIn"]

    host_sid = next(s for s, c in main.contexts.items() if c["isHost"])

    # Two concurrent start requests must not deal two overlapping hands.
    await asyncio.gather(main.start(host_sid), main.start(host_sid))

    state = await main.service.get(session_id)
    assert state["handNumber"] == starting_hand_number + 1
    assert state["status"] == "running"
    assert _total_chips(state) == 3 * CONFIG["buyIn"]
