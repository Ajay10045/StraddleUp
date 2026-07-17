from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis
import socketio
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .database import GameEvent, GameSession, SessionLocal, init_database
from .game import (GameError, act, approve_rebuy, auto_start_next_hand, new_state, player_by_id, public_view,
                   request_cashout, request_rebuy, seat_player, set_ready, start_hand,
                   timeout_active_player, validate_config)
from .history import completed_hand_payload
from .security import hash_passcode, issue_token, read_token, verify_passcode


class CreateSessionRequest(BaseModel):
    smallBlind: int = Field(ge=1)
    bigBlind: int = Field(ge=2)
    buyIn: int = Field(ge=1)
    maxSeats: int = Field(default=9, ge=2, le=9)
    timeoutSeconds: int = Field(default=60, ge=10, le=300)
    autoNextHandSeconds: int = Field(default=8, ge=3, le=30)
    passcode: str = Field(min_length=4, max_length=64)


class GameService:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.redis: redis.Redis | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        url = os.getenv("REDIS_URL")
        if url:
            self.redis = redis.from_url(url, decode_responses=True)
            await self.redis.ping()

    async def create(self, config: dict, passcode: str) -> tuple[dict, str]:
        session_id = secrets.token_urlsafe(7).replace("-", "A").replace("_", "B")[:10]
        host_token = issue_token({"kind": "host", "session": session_id}, expires_in=86_400)
        state = new_state(session_id, config)
        with SessionLocal() as db:
            db.add(GameSession(id=session_id, passcode_hash=hash_passcode(passcode), host_token_hash=hashlib.sha256(host_token.encode()).hexdigest(), config=config, game_state=state))
            db.add(GameEvent(session_id=session_id, kind="session_created", payload={"config": config}))
            db.commit()
        self.states[session_id] = state
        await self._cache(session_id, state)
        return state, host_token

    async def get(self, session_id: str) -> dict:
        if session_id in self.states:
            return self.states[session_id]
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            if not record:
                raise GameError("This table no longer exists.")
            self.states[session_id] = record.game_state
            return record.game_state

    async def passcode_ok(self, session_id: str, passcode: str) -> bool:
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            return bool(record and verify_passcode(passcode, record.passcode_hash))

    async def is_host(self, session_id: str, token: str | None) -> bool:
        payload = read_token(token or "")
        if not payload or payload.get("kind") != "host" or payload.get("session") != session_id:
            return False
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            return bool(record and secrets.compare_digest(record.host_token_hash, hashlib.sha256((token or "").encode()).hexdigest()))

    async def save(self, session_id: str, kind: str, payload: dict | None = None) -> None:
        state = self.states[session_id]
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            if not record:
                raise GameError("This table no longer exists.")
            record.status = state["status"]
            record.game_state = state
            db.add(GameEvent(session_id=session_id, kind=kind, payload=payload or {}))
            db.commit()
        await self._cache(session_id, state)

    async def history(self, session_id: str) -> list[dict]:
        with SessionLocal() as db:
            if not db.get(GameSession, session_id):
                raise GameError("This table no longer exists.")
            return [{"kind": e.kind, "payload": e.payload, "at": e.created_at.isoformat()} for e in db.query(GameEvent).filter(GameEvent.session_id == session_id).order_by(GameEvent.id).all()]

    async def current_session_id(self) -> str | None:
        with SessionLocal() as db:
            record = db.query(GameSession).filter(GameSession.status.in_(["lobby", "running", "complete"])).order_by(GameSession.created_at.desc()).first()
            return record.id if record else None

    async def load_active_sessions(self) -> int:
        """Rehydrate all live sessions into the in-memory cache after a (re)start.

        The timer loop only advances sessions present in ``self.states``; without this a hand
        left in progress by a crash or redeploy would keep its persisted state but never have
        its turn timer resumed until a client happened to reconnect and trigger ``get()``.
        """
        with SessionLocal() as db:
            records = db.query(GameSession).filter(GameSession.status.in_(["lobby", "running", "complete"])).all()
            for record in records:
                self.states[record.id] = record.game_state
            return len(records)

    async def close(self, session_id: str) -> None:
        state = await self.get(session_id)
        state["status"] = "closed"
        state["version"] += 1
        await self.save(session_id, "session_closed", {})

    async def _cache(self, session_id: str, state: dict) -> None:
        if self.redis:
            await self.redis.set(f"straddleup:session:{session_id}", __import__("json").dumps(state))


service = GameService()
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*", transports=["websocket", "polling"])
api = FastAPI(title="StraddleUp")
contexts: dict[str, dict[str, Any]] = {}


async def emit_table(session_id: str) -> None:
    state = await service.get(session_id)
    for sid, context in list(contexts.items()):
        if context["sessionId"] == session_id:
            await sio.emit("table_snapshot", public_view(state, context.get("playerId"), context.get("isHost", False)), to=sid)


async def emit_error(sid: str, message: str) -> None:
    await sio.emit("action_rejected", {"message": message}, to=sid)


def require_host(sid: str) -> dict:
    context = contexts.get(sid)
    if not context or not context.get("isHost"):
        raise GameError("Only the host can do that.")
    return context


@api.on_event("startup")
async def startup() -> None:
    init_database()
    try:
        await service.connect()
    except Exception as exc:
        # The database remains the durable source of truth. Redis is a fast live-state cache.
        print(f"Redis unavailable; continuing without live cache: {exc}")
    resumed = await service.load_active_sessions()
    if resumed:
        print(f"Resumed {resumed} active session(s) from durable storage.")
    asyncio.create_task(timer_loop())


@api.get("/health")
async def health() -> dict:
    return {"ok": True}


@api.get("/api/current-session")
async def current_session() -> dict:
    return {"sessionId": await service.current_session_id()}


@api.post("/api/sessions")
async def create_session(request: CreateSessionRequest) -> dict:
    try:
        if await service.current_session_id():
            raise HTTPException(status_code=409, detail="A private table is already active. Join it or have the host end it first.")
        config = validate_config(request.model_dump(exclude={"passcode"}))
        state, token = await service.create(config, request.passcode)
        return {"sessionId": state["sessionId"], "hostToken": token}
    except GameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str, x_host_token: str | None = Header(default=None)) -> dict:
    if not await service.is_host(session_id, x_host_token):
        raise HTTPException(status_code=401, detail="Host authorization required.")
    return {"events": await service.history(session_id)}


@api.get("/api/sessions/{session_id}/history/public")
async def public_session_history(session_id: str, x_player_token: str | None = Header(default=None)) -> dict:
    token = read_token(x_player_token or "")
    state = await service.get(session_id)
    if not token or token.get("kind") != "player" or token.get("session") != session_id or not player_by_id(state, token.get("player", "")):
        raise HTTPException(status_code=401, detail="A seated player token is required.")
    events = await service.history(session_id)
    public_events = []
    for event in events:
        if event["kind"] != "hand_completed":
            continue
        payload = event["payload"]
        public_events.append({
            "kind": event["kind"],
            "at": event["at"],
            "payload": {
                "handNumber": payload.get("handNumber"),
                "board": payload.get("board", []),
                "result": payload.get("result", {}),
                "actions": payload.get("actions", []),
                "players": [{key: value for key, value in player.items() if key != "holeCards"} for player in payload.get("players", [])],
            },
        })
    return {"events": public_events}


@api.post("/api/sessions/{session_id}/close")
async def close_session(session_id: str, x_host_token: str | None = Header(default=None)) -> dict:
    if not await service.is_host(session_id, x_host_token):
        raise HTTPException(status_code=401, detail="Host authorization required.")
    async with service.lock:
        await service.close(session_id)
    return {"ok": True}


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None = None) -> bool:
    return True


@sio.event
async def disconnect(sid: str) -> None:
    context = contexts.pop(sid, None)
    if not context or not context.get("playerId"):
        return
    same_player_elsewhere = any(c.get("playerId") == context["playerId"] and c.get("sessionId") == context["sessionId"] for c in contexts.values())
    if not same_player_elsewhere:
        try:
            async with service.lock:
                state = await service.get(context["sessionId"])
                player = player_by_id(state, context["playerId"])
                if player:
                    player["connected"] = False
                    state["version"] += 1
                    await service.save(context["sessionId"], "player_disconnected", {"playerId": player["id"]})
                    await emit_table(context["sessionId"])
        except GameError:
            pass


@sio.on("join_room")
async def join_room(sid: str, data: dict) -> None:
    session_id = str(data.get("sessionId", ""))
    reconnect = read_token(str(data.get("reconnectToken", "")))
    is_reconnect = bool(reconnect and reconnect.get("kind") == "player" and reconnect.get("session") == session_id)
    try:
        async with service.lock:
            state = await service.get(session_id)
            host_token = data.get("hostToken")
            is_host = await service.is_host(session_id, host_token)
            player_id = reconnect.get("player") if is_reconnect else None
            if player_id and not player_by_id(state, player_id):
                player_id = None
                is_reconnect = False
            if not is_reconnect and not is_host:
                if not await service.passcode_ok(session_id, str(data.get("passcode", ""))):
                    raise GameError("Incorrect room passcode.")
            if not player_id:
                player_id = secrets.token_urlsafe(9)
                token = issue_token({"kind": "player", "session": session_id, "player": player_id}, expires_in=86_400)
            else:
                token = str(data.get("reconnectToken"))
                player = player_by_id(state, player_id)
                player["connected"] = True
                await service.save(session_id, "player_reconnected", {"playerId": player_id})
            contexts[sid] = {"sessionId": session_id, "playerId": player_id, "isHost": is_host}
            await sio.enter_room(sid, session_id)
            await sio.emit("joined", {"playerId": player_id, "reconnectToken": token, "isHost": is_host}, to=sid)
            await sio.emit("table_snapshot", public_view(state, player_id, is_host), to=sid)
            if is_reconnect:
                await emit_table(session_id)
    except GameError as exc:
        await emit_error(sid, str(exc))


@sio.on("choose_seat")
async def choose_seat(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            name = str(data.get("name", "")).strip()
            if not name:
                raise GameError("Enter a display name.")
            seat_player(state, context["playerId"], name, int(data.get("seat")))
            await service.save(context["sessionId"], "player_seated", {"playerId": context["playerId"], "seat": int(data["seat"]), "name": name})
            await emit_table(context["sessionId"])
    except (GameError, KeyError, ValueError, TypeError) as exc:
        await emit_error(sid, str(exc))


@sio.on("set_ready")
async def ready(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            set_ready(state, context["playerId"], bool(data.get("ready", True)))
            await service.save(context["sessionId"], "player_ready", {"playerId": context["playerId"], "ready": bool(data.get("ready", True))})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("start_hand")
async def start(sid: str) -> None:
    try:
        context = require_host(sid)
        async with service.lock:
            state = await service.get(context["sessionId"])
            start_hand(state)
            await service.save(context["sessionId"], "hand_started", {"handNumber": state["handNumber"]})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("player_action")
async def player_action(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            act(state, context["playerId"], str(data.get("action")), data.get("amount"))
            await service.save(context["sessionId"], "player_action", {"playerId": context["playerId"], "action": data.get("action"), "amount": data.get("amount")})
            if state["status"] == "complete":
                await service.save(context["sessionId"], "hand_completed", completed_hand_payload(state))
            await emit_table(context["sessionId"])
    except (GameError, KeyError, TypeError, ValueError) as exc:
        await emit_error(sid, str(exc))


@sio.on("request_rebuy")
async def rebuy_request(sid: str) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            request_rebuy(state, context["playerId"])
            await service.save(context["sessionId"], "rebuy_requested", {"playerId": context["playerId"]})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("host_approve_rebuy")
async def rebuy_approve(sid: str, data: dict) -> None:
    try:
        context = require_host(sid)
        async with service.lock:
            state = await service.get(context["sessionId"])
            player_id = str(data.get("playerId"))
            amount = approve_rebuy(state, player_id)
            await service.save(context["sessionId"], "rebuy_approved", {"playerId": player_id, "amount": amount})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("request_cashout")
async def cashout(sid: str) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            amount = request_cashout(state, context["playerId"])
            await service.save(context["sessionId"], "cashout_requested", {"playerId": context["playerId"], "amount": amount})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


async def timer_loop() -> None:
    while True:
        await asyncio.sleep(0.5)
        for session_id in list(service.states):
            async with service.lock:
                try:
                    state = await service.get(session_id)
                    timed_out = timeout_active_player(state)
                    if timed_out:
                        await service.save(session_id, "timeout_fold", {"playerId": timed_out})
                        if state["status"] == "complete":
                            await service.save(session_id, "hand_completed", completed_hand_payload(state))
                        await emit_table(session_id)
                    elif auto_start_next_hand(state):
                        await service.save(session_id, "hand_started", {"handNumber": state["handNumber"], "automatic": True})
                        await emit_table(session_id)
                except Exception as exc:
                    print(f"Timer error for {session_id}: {exc}")


application = socketio.ASGIApp(sio, other_asgi_app=api)
