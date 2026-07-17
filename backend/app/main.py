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
from .game import (GameError, act, approve_rebuy, auto_start_next_hand, end_session, new_state, player_by_id,
                   propose_time_extension, public_view, request_cashout, request_rebuy, seat_player, set_ready,
                   set_sit_out, set_straddle, start_hand, timeout_active_player, use_time_bank,
                   validate_config, vote_time_extension)
from .history import audit_hand_payload, completed_hand_payload
from .security import hash_passcode, issue_token, read_token, verify_passcode


class CreateSessionRequest(BaseModel):
    smallBlind: int = Field(ge=1)
    bigBlind: int = Field(ge=2)
    buyIn: int = Field(ge=1)
    maxSeats: int = Field(default=9, ge=2, le=9)
    timeoutSeconds: int = Field(default=60, ge=10, le=300)
    autoNextHandSeconds: int = Field(default=8, ge=3, le=30)
    sessionMinutes: int = Field(default=180, ge=15, le=720)
    timeBankSeconds: int = Field(default=60, ge=0, le=300)
    passcode: str = Field(min_length=4, max_length=64)


class RecoverHostRequest(BaseModel):
    passcode: str | None = Field(default=None, min_length=4, max_length=64)


class HostAccessRequest(BaseModel):
    accessCode: str = Field(min_length=8, max_length=128)


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
            state = record.game_state
            # Older builds allowed a player to buy into the table while a hand
            # was already running. Their chips were not part of that hand's
            # accounting baseline, which could stop the timeout loop forever.
            # Reconcile only players absent from the hand's starting snapshot.
            hand = state.get("hand") or {}
            starting = hand.get("startingStacks", {})
            if state.get("status") == "running" and starting and "initialChipTotal" in hand:
                late_joiner_chips = sum(player.get("stack", 0) for player in state.get("players", []) if player.get("id") not in starting)
                observed = sum(player.get("stack", 0) for player in state.get("players", [])) + sum(player.get("handContribution", 0) for player in state.get("players", []))
                if late_joiner_chips and observed == hand["initialChipTotal"] + late_joiner_chips:
                    hand["initialChipTotal"] = observed
            self.states[session_id] = state
            return state

    async def passcode_ok(self, session_id: str, passcode: str) -> bool:
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            return bool(record and verify_passcode(passcode, record.passcode_hash))

    async def recover_host(self, session_id: str, passcode: str | None = None) -> tuple[str, str]:
        if passcode and not await self.passcode_ok(session_id, passcode):
            raise GameError("Incorrect room passcode.")
        token = issue_token({"kind": "host", "session": session_id}, expires_in=86_400)
        invite_passcode = passcode or secrets.token_urlsafe(6)
        with SessionLocal() as db:
            record = db.get(GameSession, session_id)
            if not record or record.status == "closed":
                raise GameError("This table has ended.")
            record.host_token_hash = hashlib.sha256(token.encode()).hexdigest()
            record.passcode_hash = hash_passcode(invite_passcode)
            db.commit()
        return token, invite_passcode

    async def is_host(self, session_id: str, token: str | None) -> bool:
        payload = read_token(token or "")
        if not payload:
            return False
        if payload.get("kind") == "host_admin":
            return is_host_admin_token(token)
        if payload.get("kind") != "host" or payload.get("session") != session_id:
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

    async def close(self, session_id: str) -> bool:
        state = await self.get(session_id)
        queued = end_session(state)
        await self.save(session_id, "session_end_requested" if queued else "session_closed", {"settlement": state.get("settlement", [])})
        return queued

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


def is_host_admin_token(token: str | None) -> bool:
    payload = read_token(token or "")
    return bool(host_access_code()) and bool(payload) and payload.get("kind") == "host_admin"


def host_access_code() -> str:
    """The host credential is supplied only by the ignored local environment file."""
    return os.getenv("HOST_ACCESS_CODE", "")


def require_host_admin(token: str | None) -> None:
    if not is_host_admin_token(token):
        raise HTTPException(status_code=401, detail="Host access is required.")


def issue_host_admin_token() -> str:
    return issue_token({"kind": "host_admin"}, expires_in=7 * 24 * 60 * 60)


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


@api.get("/api/host/status")
async def host_status() -> dict:
    return {"configured": bool(host_access_code())}


@api.post("/api/host/login")
async def host_login(request: HostAccessRequest) -> dict:
    configured_code = host_access_code()
    if not configured_code:
        raise HTTPException(status_code=503, detail="Host access is not configured on this server.")
    if not secrets.compare_digest(request.accessCode, configured_code):
        raise HTTPException(status_code=401, detail="Incorrect host access code.")
    return {"hostToken": issue_host_admin_token()}


@api.get("/api/host/tables")
async def host_tables(x_host_token: str | None = Header(default=None)) -> dict:
    require_host_admin(x_host_token)
    with SessionLocal() as db:
        records = db.query(GameSession).order_by(GameSession.updated_at.desc()).all()
        active_tables = []
        archived_tables = []
        for record in records:
            state = record.game_state or {}
            table = {
                "sessionId": record.id,
                "status": record.status,
                "createdAt": record.created_at.isoformat(),
                "updatedAt": record.updated_at.isoformat(),
                "config": record.config,
                "handNumber": state.get("handNumber", 0),
                "players": [{"id": player.get("id"), "name": player.get("name"), "status": player.get("status"), "connected": player.get("connected", False)} for player in state.get("players", [])],
            }
            (archived_tables if record.status == "closed" else active_tables).append(table)
    return {"activeTables": active_tables, "archivedTables": archived_tables}


@api.get("/api/host/sessions/{session_id}/summary")
async def host_session_summary(session_id: str, x_host_token: str | None = Header(default=None)) -> dict:
    require_host_admin(x_host_token)
    with SessionLocal() as db:
        record = db.get(GameSession, session_id)
        if not record:
            raise HTTPException(status_code=404, detail="That table was not found.")
        state = record.game_state or {}
        history_events = db.query(GameEvent).filter(GameEvent.session_id == session_id).order_by(GameEvent.id).all()
        completed_hands = sum(event.kind == "hand_completed" for event in history_events)
        return {
            "sessionId": record.id,
            "status": record.status,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "handNumber": state.get("handNumber", 0),
            "completedHands": completed_hands,
            "config": record.config,
            "players": [{"id": player.get("id"), "name": player.get("name"), "status": player.get("status"), "boughtIn": player.get("buyInTotal", 0), "stack": player.get("stack", 0)} for player in state.get("players", [])],
            "settlement": state.get("settlement", []),
            "events": [{"kind": event.kind, "payload": audit_hand_payload(event.payload), "at": event.created_at.isoformat()} for event in history_events if event.kind == "hand_completed"],
        }


@api.post("/api/host/sessions/{session_id}/invite")
async def rotate_invite(session_id: str, x_host_token: str | None = Header(default=None)) -> dict:
    require_host_admin(x_host_token)
    invite_passcode = secrets.token_urlsafe(6)
    with SessionLocal() as db:
        record = db.get(GameSession, session_id)
        if not record or record.status == "closed":
            raise HTTPException(status_code=404, detail="That active table was not found.")
        record.passcode_hash = hash_passcode(invite_passcode)
        db.add(GameEvent(session_id=session_id, kind="invite_passcode_rotated", payload={}))
        db.commit()
    return {"sessionId": session_id, "invitePasscode": invite_passcode}


@api.post("/api/sessions")
async def create_session(request: CreateSessionRequest, x_host_token: str | None = Header(default=None)) -> dict:
    try:
        require_host_admin(x_host_token)
        config = validate_config(request.model_dump(exclude={"passcode"}))
        state, token = await service.create(config, request.passcode)
        return {"sessionId": state["sessionId"], "hostToken": token}
    except GameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.post("/api/sessions/{session_id}/recover-host")
async def recover_host(session_id: str, request: RecoverHostRequest, x_host_token: str | None = Header(default=None)) -> dict:
    try:
        require_host_admin(x_host_token)
        token, invite_passcode = await service.recover_host(session_id, request.passcode)
        return {"hostToken": token, "sessionId": session_id, "invitePasscode": invite_passcode}
    except GameError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


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
        payload = audit_hand_payload(event["payload"])
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
        queued = await service.close(session_id)
        await emit_table(session_id)
    return {"ok": True, "queued": queued}


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
            if state["status"] in {"complete", "closed"}:
                await service.save(context["sessionId"], "hand_completed", completed_hand_payload(state))
                if state["status"] == "closed":
                    await service.save(context["sessionId"], "session_closed", {"settlement": state.get("settlement", [])})
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


@sio.on("host_release_disconnected_player")
async def release_disconnected_player(sid: str, data: dict) -> None:
    try:
        context = require_host(sid)
        async with service.lock:
            state = await service.get(context["sessionId"])
            player = player_by_id(state, str(data.get("playerId", "")))
            if not player or player.get("status") != "seated" or player.get("connected"):
                raise GameError("Only a disconnected seated player can be released.")
            amount = request_cashout(state, player["id"])
            await service.save(context["sessionId"], "disconnected_player_released", {"playerId": player["id"], "amount": amount})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("set_sit_out")
async def sit_out(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            sitting_out = bool(data.get("sittingOut", True))
            set_sit_out(state, context["playerId"], sitting_out)
            await service.save(context["sessionId"], "sit_out_changed", {"playerId": context["playerId"], "sittingOut": sitting_out})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("set_straddle")
async def straddle(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            enabled = bool(data.get("enabled", True))
            set_straddle(state, context["playerId"], enabled)
            await service.save(context["sessionId"], "straddle_changed", {"playerId": context["playerId"], "enabled": enabled})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("use_time_bank")
async def time_bank(sid: str) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            used = use_time_bank(state, context["playerId"])
            await service.save(context["sessionId"], "time_bank_used", {"playerId": context["playerId"], "seconds": used})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("propose_time_extension")
async def propose_extension(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            minutes = int(data.get("minutes", 30))
            propose_time_extension(state, context["playerId"], minutes)
            await service.save(context["sessionId"], "time_extension_proposed", {"playerId": context["playerId"], "minutes": minutes})
            await emit_table(context["sessionId"])
    except (GameError, KeyError, TypeError, ValueError) as exc:
        await emit_error(sid, str(exc))


@sio.on("vote_time_extension")
async def vote_extension(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            approve = bool(data.get("approve", False))
            passed = vote_time_extension(state, context["playerId"], approve)
            await service.save(context["sessionId"], "time_extension_vote", {"playerId": context["playerId"], "approve": approve, "passed": passed})
            await emit_table(context["sessionId"])
    except (GameError, KeyError) as exc:
        await emit_error(sid, str(exc))


@sio.on("send_chat")
async def send_chat(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            player = player_by_id(state, context["playerId"])
            if not player or player.get("status") != "seated":
                raise GameError("Choose a seat before chatting.")
            if state["status"] in {"running", "closed"}:
                raise GameError("Table chat is paused during a live hand to prevent influencing play.")
            text = " ".join(str(data.get("text", "")).split())[:240]
            if not text:
                raise GameError("Enter a message first.")
            entry = {"id": secrets.token_urlsafe(6), "playerId": player["id"], "name": player["name"], "text": text, "at": int(__import__("time").time() * 1000)}
            state.setdefault("chat", []).append(entry)
            state["chat"] = state["chat"][-100:]
            await service.save(context["sessionId"], "chat_message", entry)
            await emit_table(context["sessionId"])
    except (GameError, KeyError, TypeError) as exc:
        await emit_error(sid, str(exc))


@sio.on("send_reaction")
async def send_reaction(sid: str, data: dict) -> None:
    try:
        context = contexts[sid]
        async with service.lock:
            state = await service.get(context["sessionId"])
            player = player_by_id(state, context["playerId"])
            if not player or player.get("status") != "seated":
                raise GameError("Choose a seat before reacting.")
            reaction = str(data.get("reaction", ""))
            if reaction not in {"👏", "🔥", "😎", "🃏", "♠️", "💰", "GG"}:
                raise GameError("That reaction is not available at this table.")
            entry = {"id": secrets.token_urlsafe(6), "playerId": player["id"], "name": player["name"], "text": reaction, "kind": "reaction", "at": int(__import__("time").time() * 1000)}
            state.setdefault("chat", []).append(entry)
            state["chat"] = state["chat"][-100:]
            await service.save(context["sessionId"], "chat_reaction", entry)
            await emit_table(context["sessionId"])
    except (GameError, KeyError, TypeError) as exc:
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
