"""Reconnect flow: a returning player (page refresh / dropped socket) must be restored to their
seat using only their reconnect token — no name or passcode — which is what the client now does
automatically on load instead of showing the join form again."""

import asyncio

import pytest

from app import main


pytestmark = pytest.mark.asyncio

CONFIG = {"smallBlind": 5, "bigBlind": 10, "buyIn": 1000, "maxSeats": 9, "timeoutSeconds": 30, "autoNextHandSeconds": 8}
PASSCODE = "table-pass"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    async def noop(*a, **k):
        return None

    captured = {}

    async def capture_emit(event, data=None, to=None, **k):
        captured.setdefault(to, []).append((event, data))

    monkeypatch.setattr(main.sio, "emit", capture_emit)
    monkeypatch.setattr(main.sio, "enter_room", noop)
    monkeypatch.setattr(main, "contexts", {})
    main.service.states = {}
    main.service.lock = asyncio.Lock()
    return captured


def _joined_payload(captured, sid):
    for event, data in captured.get(sid, []):
        if event == "joined":
            return data
    return None


async def _make_table():
    main.init_database()
    state, host_token = await main.service.create(dict(CONFIG), PASSCODE)
    return state["sessionId"], host_token


async def test_refresh_reconnect_restores_seat_with_token_only(isolate):
    captured = isolate
    session_id, _ = await _make_table()

    # First join: passcode + name, then take a seat.
    await main.join_room("sidA", {"sessionId": session_id, "passcode": PASSCODE, "name": "Ada"})
    joined = _joined_payload(captured, "sidA")
    assert joined and joined["playerId"]
    player_id = joined["playerId"]
    reconnect_token = joined["reconnectToken"]
    await main.choose_seat("sidA", {"name": "Ada", "seat": 3})

    # Simulate a refresh: brand-new socket, and crucially NO passcode / NO name — only the token.
    await main.join_room("sidB", {"sessionId": session_id, "reconnectToken": reconnect_token})

    rejoined = _joined_payload(captured, "sidB")
    assert rejoined is not None, "reconnect did not produce a joined event"
    # Same identity restored — not a fresh player.
    assert rejoined["playerId"] == player_id
    # And they are still seated in seat 3.
    state = await main.service.get(session_id)
    me = main.player_by_id(state, player_id)
    assert me is not None and me["seat"] == 3 and me["status"] == "seated"


async def test_reconnect_without_token_or_passcode_is_rejected(isolate):
    captured = isolate
    session_id, _ = await _make_table()

    # A brand-new visitor with neither a token nor a passcode must NOT be silently seated.
    await main.join_room("sidX", {"sessionId": session_id})

    assert _joined_payload(captured, "sidX") is None
    # They receive an error prompting for the passcode instead.
    errors = [d for e, d in captured.get("sidX", []) if e == "action_rejected"]
    assert errors and "passcode" in errors[0]["message"].lower()
