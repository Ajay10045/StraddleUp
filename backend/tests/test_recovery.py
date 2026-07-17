"""Restart-recovery tests: a running hand persisted to the DB must resume after the process
loses its in-memory state (crash / redeploy), so the turn timer keeps advancing it."""

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
def reset_service():
    main.service.states = {}
    main.service.lock = asyncio.Lock()
    yield


async def _create_running_session():
    main.init_database()
    state, _ = await main.service.create(dict(CONFIG), "passcode1")
    session_id = state["sessionId"]
    for seat in range(3):
        main.seat_player(state, f"player{seat}", f"Player {seat}", seat)
    start_hand(state)
    await main.service.save(session_id, "hand_started", {"handNumber": state["handNumber"]})
    return session_id


async def test_active_session_is_rehydrated_into_memory_after_restart():
    session_id = await _create_running_session()
    # Simulate a process restart: the durable DB row survives, the in-memory cache is gone.
    main.service.states = {}

    resumed = await main.service.load_active_sessions()

    assert resumed >= 1
    assert session_id in main.service.states
    assert main.service.states[session_id]["status"] == "running"


async def test_rehydrated_session_timer_can_auto_fold_the_expired_actor():
    session_id = await _create_running_session()
    # The actor's clock is already expired at the moment of the "restart"; persist it so the
    # reloaded-from-DB state carries the expired deadline.
    main.service.states[session_id]["turnDeadline"] = 1
    await main.service.save(session_id, "test_expire_turn", {})
    main.service.states = {}  # wipe in-memory state as a crash would

    await main.service.load_active_sessions()

    # The timer loop only advances sessions present in service.states; after rehydration the
    # expired actor must be auto-foldable exactly as before the restart.
    state = main.service.states[session_id]
    expired_actor = player_by_seat(state, state["hand"]["actorSeat"])
    folded_id = main.timeout_active_player(state)

    assert folded_id == expired_actor["id"]


async def test_closed_sessions_are_not_rehydrated():
    # Close a table that is NOT mid-hand so it closes immediately. (When a hand is running,
    # close() defers to endAfterHand and the session legitimately stays live until the hand ends.)
    main.init_database()
    state, _ = await main.service.create(dict(CONFIG), "table-pass")
    session_id = state["sessionId"]
    main.seat_player(state, "player0", "Player 0", 0)
    closed_immediately = not await main.service.close(session_id)
    assert closed_immediately and state["status"] == "closed"
    main.service.states = {}

    await main.service.load_active_sessions()

    assert session_id not in main.service.states
