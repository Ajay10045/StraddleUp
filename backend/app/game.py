from __future__ import annotations

import random
import time
import uuid
from copy import deepcopy
from typing import Any

from treys import Card, Evaluator


class GameError(ValueError):
    pass


RANKS = "23456789TJQKA"
SUITS = "shdc"


def new_state(session_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "version": 1,
        "status": "lobby",
        "config": config,
        "players": [],
        "buttonSeat": None,
        "handNumber": 0,
        "hand": None,
        "lastResult": None,
        "turnDeadline": None,
        "nextHandAt": None,
    }


def make_deck() -> list[str]:
    cards = [rank + suit for rank in RANKS for suit in SUITS]
    random.SystemRandom().shuffle(cards)
    return cards


def player_by_id(state: dict, player_id: str) -> dict | None:
    return next((p for p in state["players"] if p["id"] == player_id), None)


def player_by_seat(state: dict, seat: int) -> dict | None:
    return next((p for p in state["players"] if p["seat"] == seat), None)


def live_players(state: dict) -> list[dict]:
    return [p for p in state["players"] if p["status"] == "seated" and p["stack"] > 0]


def active_hand_players(state: dict) -> list[dict]:
    return [p for p in state["players"] if p.get("inHand") and not p.get("folded")]


def ordered_seats_after(state: dict, start_seat: int | None, players: list[dict] | None = None) -> list[int]:
    population = players if players is not None else state["players"]
    seats = sorted(p["seat"] for p in population)
    if not seats:
        return []
    if start_seat is None:
        return seats
    return sorted(seats, key=lambda seat: (seat - start_seat) % 9 or 9)


def next_eligible_seat(state: dict, after_seat: int, predicate) -> int | None:
    for seat in ordered_seats_after(state, after_seat):
        if seat == after_seat:
            continue
        p = player_by_seat(state, seat)
        if p and predicate(p):
            return seat
    return None


def validate_config(config: dict) -> dict:
    try:
        sb = int(config["smallBlind"])
        bb = int(config["bigBlind"])
        buyin = int(config["buyIn"])
        max_seats = int(config.get("maxSeats", 9))
        timeout = int(config.get("timeoutSeconds", 60))
        auto_next = int(config.get("autoNextHandSeconds", 8))
    except (KeyError, TypeError, ValueError) as exc:
        raise GameError("Stakes must be whole numbers.") from exc
    if not (sb > 0 and bb >= sb * 2 and buyin >= bb * 20 and 2 <= max_seats <= 9 and 10 <= timeout <= 300 and 3 <= auto_next <= 30):
        raise GameError("Use sensible blinds, a buy-in of at least 20 big blinds, 2–9 seats, a 10–300 second timer, and a 3–30 second next-hand delay.")
    return {"smallBlind": sb, "bigBlind": bb, "buyIn": buyin, "maxSeats": max_seats, "timeoutSeconds": timeout, "autoNextHandSeconds": auto_next}


def seat_player(state: dict, player_id: str, name: str, seat: int) -> None:
    if not 0 <= seat < state["config"]["maxSeats"]:
        raise GameError("That seat is unavailable.")
    player = player_by_id(state, player_id)
    if player is None:
        player = {"id": player_id, "name": name.strip()[:24], "seat": seat, "stack": state["config"]["buyIn"], "status": "seated", "connected": True, "ready": False, "inHand": False, "folded": False, "allIn": False, "holeCards": [], "handContribution": 0, "streetContribution": 0}
        state["players"].append(player)
    else:
        if state["status"] == "running" and player.get("seat") != seat:
            raise GameError("Seats cannot change during a hand.")
        occupant = player_by_seat(state, seat)
        if occupant and occupant["id"] != player_id:
            raise GameError("That seat is already taken.")
        player.update({"name": name.strip()[:24] or player["name"], "seat": seat, "status": "seated", "connected": True})
    state["version"] += 1


def set_ready(state: dict, player_id: str, ready: bool) -> None:
    if state["status"] != "lobby":
        raise GameError("The table is already in a hand.")
    player = require_seated(state, player_id)
    player["ready"] = ready
    state["version"] += 1


def require_seated(state: dict, player_id: str) -> dict:
    player = player_by_id(state, player_id)
    if not player or player["status"] != "seated":
        raise GameError("Choose a seat first.")
    return player


def _post(player: dict, amount: int) -> int:
    paid = min(player["stack"], amount)
    player["stack"] -= paid
    player["streetContribution"] += paid
    player["handContribution"] += paid
    if player["stack"] == 0:
        player["allIn"] = True
    return paid


def _new_hand_player(player: dict) -> None:
    player.update({"inHand": True, "folded": False, "allIn": False, "holeCards": [], "handContribution": 0, "streetContribution": 0, "leaveAfterHand": False})


def start_hand(state: dict) -> None:
    if state["status"] not in {"lobby", "complete"}:
        raise GameError("Finish the current hand first.")
    participants = live_players(state)
    if len(participants) < 2:
        raise GameError("At least two seated players with chips are required.")
    for p in participants:
        _new_hand_player(p)
        p["ready"] = False
    if state["buttonSeat"] is None or not player_by_seat(state, state["buttonSeat"]) or player_by_seat(state, state["buttonSeat"])["status"] != "seated":
        state["buttonSeat"] = min(p["seat"] for p in participants)
    else:
        nxt = next_eligible_seat(state, state["buttonSeat"], lambda p: p in participants)
        state["buttonSeat"] = nxt if nxt is not None else state["buttonSeat"]
    deck = make_deck()
    for p in ordered_players_from_button(state, participants):
        p["holeCards"] = [deck.pop(), deck.pop()]
    heads_up = len(participants) == 2
    sb_seat = state["buttonSeat"] if heads_up else next_eligible_seat(state, state["buttonSeat"], lambda p: p in participants)
    bb_seat = next_eligible_seat(state, sb_seat, lambda p: p in participants)
    sb_player, bb_player = player_by_seat(state, sb_seat), player_by_seat(state, bb_seat)
    _post(sb_player, state["config"]["smallBlind"])
    _post(bb_player, state["config"]["bigBlind"])
    first_actor = sb_seat if heads_up else next_eligible_seat(state, bb_seat, lambda p: p.get("inHand") and not p.get("allIn"))
    state["handNumber"] += 1
    state["status"] = "running"
    state["nextHandAt"] = None
    state["lastResult"] = None
    state["hand"] = {"id": uuid.uuid4().hex[:12], "street": "preflop", "deck": deck, "board": [], "smallBlindSeat": sb_seat, "bigBlindSeat": bb_seat, "currentBet": bb_player["streetContribution"], "minRaise": state["config"]["bigBlind"], "pending": [p["id"] for p in participants if not p.get("allIn")], "actorSeat": first_actor, "actions": []}
    if first_actor is None:
        _runout_and_showdown(state)
    else:
        _set_turn_deadline(state)
    state["version"] += 1


def ordered_players_from_button(state: dict, players: list[dict]) -> list[dict]:
    return [player_by_seat(state, seat) for seat in ordered_seats_after(state, state["buttonSeat"], players)]


def _set_turn_deadline(state: dict) -> None:
    state["turnDeadline"] = int(time.time() * 1000) + state["config"]["timeoutSeconds"] * 1000


def _pot_total(state: dict) -> int:
    return sum(p.get("handContribution", 0) for p in state["players"])


def _record_action(state: dict, player: dict, action: str, amount: int) -> None:
    state["hand"]["actions"].append({"sequence": len(state["hand"]["actions"]) + 1, "street": state["hand"]["street"], "playerId": player["id"], "type": action, "amount": amount, "at": int(time.time() * 1000)})


def _pending_others(state: dict, actor_id: str) -> list[str]:
    return [p["id"] for p in active_hand_players(state) if not p.get("allIn") and p["id"] != actor_id]


def legal_actions(state: dict, player_id: str) -> dict[str, Any]:
    if state["status"] != "running" or not state["hand"]:
        return {}
    player = player_by_id(state, player_id)
    if not player or player["seat"] != state["hand"]["actorSeat"] or player["folded"] or player["allIn"]:
        return {}
    to_call = max(0, state["hand"]["currentBet"] - player["streetContribution"])
    maximum = player["streetContribution"] + player["stack"]
    min_raise_to = state["hand"]["currentBet"] + state["hand"]["minRaise"] if state["hand"]["currentBet"] else state["config"]["bigBlind"]
    return {"fold": True, "check": to_call == 0, "call": to_call > 0, "allIn": player["stack"] > 0, "toCall": min(to_call, player["stack"]), "minRaiseTo": min(min_raise_to, maximum), "maxRaiseTo": maximum}


def act(state: dict, player_id: str, action: str, amount: int | None = None) -> None:
    if state["status"] != "running" or not state["hand"]:
        raise GameError("There is no active hand.")
    player = require_seated(state, player_id)
    if player["seat"] != state["hand"]["actorSeat"]:
        raise GameError("It is not your turn.")
    action = action.lower()
    to_call = max(0, state["hand"]["currentBet"] - player["streetContribution"])
    before_bet = state["hand"]["currentBet"]
    paid = 0
    if action == "fold":
        player["folded"] = True
    elif action == "check":
        if to_call:
            raise GameError("You must call, raise, or fold.")
    elif action == "call":
        if not to_call:
            raise GameError("There is nothing to call.")
        paid = _post(player, to_call)
    elif action in {"bet", "raise", "allin"}:
        target = player["streetContribution"] + player["stack"] if action == "allin" else int(amount or 0)
        if target <= state["hand"]["currentBet"]:
            raise GameError("Raise amount must exceed the current bet.")
        if target > player["streetContribution"] + player["stack"]:
            raise GameError("That exceeds your stack.")
        raise_size = target - state["hand"]["currentBet"]
        is_all_in = target == player["streetContribution"] + player["stack"]
        if target < state["config"]["bigBlind"] and not is_all_in:
            raise GameError("Opening bet must be at least the big blind.")
        if before_bet and raise_size < state["hand"]["minRaise"] and not is_all_in:
            raise GameError("Raise is too small.")
        paid = _post(player, target - player["streetContribution"])
        state["hand"]["currentBet"] = target
        if raise_size >= state["hand"]["minRaise"]:
            state["hand"]["minRaise"] = raise_size
    else:
        raise GameError("Unknown action.")
    _record_action(state, player, action, paid)
    remaining = active_hand_players(state)
    if len(remaining) == 1:
        _award_uncontested(state, remaining[0])
        return
    if action in {"bet", "raise", "allin"} and state["hand"]["currentBet"] > before_bet:
        state["hand"]["pending"] = _pending_others(state, player_id)
    else:
        state["hand"]["pending"] = [pid for pid in state["hand"]["pending"] if pid != player_id]
    _advance(state)
    state["version"] += 1


def _advance(state: dict) -> None:
    pending = [pid for pid in state["hand"]["pending"] if (p := player_by_id(state, pid)) and not p["folded"] and not p["allIn"]]
    state["hand"]["pending"] = pending
    if not pending:
        _advance_street(state)
        return
    current = state["hand"]["actorSeat"]
    next_seat = next_eligible_seat(state, current, lambda p: p["id"] in pending)
    state["hand"]["actorSeat"] = next_seat
    _set_turn_deadline(state)


def _advance_street(state: dict) -> None:
    hand = state["hand"]
    street = hand["street"]
    if street == "river":
        _showdown(state)
        return
    reveal = 3 if street == "preflop" else 1
    hand["board"].extend([hand["deck"].pop() for _ in range(reveal)])
    hand["street"] = {"preflop": "flop", "flop": "turn", "turn": "river"}[street]
    for p in state["players"]:
        p["streetContribution"] = 0
    hand["currentBet"] = 0
    hand["minRaise"] = state["config"]["bigBlind"]
    pending = [p["id"] for p in active_hand_players(state) if not p["allIn"]]
    hand["pending"] = pending
    if len(active_hand_players(state)) <= 1:
        _award_uncontested(state, active_hand_players(state)[0])
    elif not pending:
        _runout_and_showdown(state)
    else:
        hand["actorSeat"] = next_eligible_seat(state, state["buttonSeat"], lambda p: p["id"] in pending)
        _set_turn_deadline(state)


def _runout_and_showdown(state: dict) -> None:
    while len(state["hand"]["board"]) < 5:
        state["hand"]["board"].append(state["hand"]["deck"].pop())
    state["hand"]["street"] = "showdown"
    _showdown(state)


def _award_uncontested(state: dict, winner: dict) -> None:
    pot = _pot_total(state)
    winner["stack"] += pot
    _finish_hand(state, [{"amount": pot, "winners": [winner["id"]], "eligible": [winner["id"]]}], "uncontested")


def _showdown(state: dict) -> None:
    hand = state["hand"]
    if len(hand["board"]) < 5:
        _runout_and_showdown(state)
        return
    evaluator = Evaluator()
    board = [Card.new(card[0].upper() + card[1]) for card in hand["board"]]
    contributions = sorted({p["handContribution"] for p in state["players"] if p["handContribution"] > 0})
    previous, pots = 0, []
    for threshold in contributions:
        contributors = [p for p in state["players"] if p["handContribution"] >= threshold]
        amount = (threshold - previous) * len(contributors)
        eligible = [p for p in contributors if not p["folded"]]
        if amount and eligible:
            scores = {p["id"]: evaluator.evaluate(board, [Card.new(c[0].upper() + c[1]) for c in p["holeCards"]]) for p in eligible}
            best = min(scores.values())
            winners = [p for p in eligible if scores[p["id"]] == best]
            share, remainder = divmod(amount, len(winners))
            for p in winners:
                p["stack"] += share
            for seat in ordered_seats_after(state, state["buttonSeat"], winners)[:remainder]:
                player_by_seat(state, seat)["stack"] += 1
            pots.append({"amount": amount, "winners": [p["id"] for p in winners], "eligible": [p["id"] for p in eligible], "handClass": evaluator.class_to_string(evaluator.get_rank_class(best))})
        previous = threshold
    _finish_hand(state, pots, "showdown")


def _finish_hand(state: dict, pots: list[dict], result_type: str) -> None:
    hand = state["hand"]
    state["lastResult"] = {"handNumber": state["handNumber"], "type": result_type, "board": hand["board"], "pots": pots, "at": int(time.time() * 1000)}
    state["status"] = "complete"
    state["turnDeadline"] = None
    state["nextHandAt"] = int(time.time() * 1000) + state["config"].get("autoNextHandSeconds", 8) * 1000
    hand["actorSeat"] = None
    for p in state["players"]:
        if p.get("leaveAfterHand"):
            p["status"] = "cashout"
            p["cashoutAmount"] = p["stack"]
        p["inHand"] = False
    state["version"] += 1


def request_cashout(state: dict, player_id: str) -> int | None:
    player = require_seated(state, player_id)
    if state["status"] == "running" and player.get("inHand"):
        player["leaveAfterHand"] = True
        state["version"] += 1
        return None
    player["status"] = "cashout"
    player["cashoutAmount"] = player["stack"]
    state["version"] += 1
    return player["stack"]


def request_rebuy(state: dict, player_id: str) -> None:
    player = require_seated(state, player_id)
    if state["status"] == "running":
        raise GameError("Rebuys are available between hands.")
    player["rebuyRequested"] = True
    state["version"] += 1


def approve_rebuy(state: dict, player_id: str) -> int:
    player = require_seated(state, player_id)
    if not player.get("rebuyRequested"):
        raise GameError("No rebuy is awaiting approval.")
    amount = state["config"]["buyIn"]
    player["stack"] += amount
    player["rebuyRequested"] = False
    state["version"] += 1
    return amount


def timeout_active_player(state: dict) -> str | None:
    if state["status"] != "running" or not state["turnDeadline"] or state["turnDeadline"] > int(time.time() * 1000):
        return None
    actor = player_by_seat(state, state["hand"]["actorSeat"])
    if not actor:
        return None
    act(state, actor["id"], "fold")
    return actor["id"]


def auto_start_next_hand(state: dict) -> bool:
    if state["status"] != "complete" or not state.get("nextHandAt") or state["nextHandAt"] > int(time.time() * 1000):
        return False
    try:
        start_hand(state)
        return True
    except GameError:
        state["nextHandAt"] = None
        state["version"] += 1
        return False


def public_view(state: dict, viewer_id: str | None = None, is_host: bool = False) -> dict:
    view = deepcopy(state)
    for player in view["players"]:
        cards = player.pop("holeCards", [])
        player["holeCards"] = cards if player["id"] == viewer_id or (state["status"] == "complete" and not player.get("folded")) else []
    if view.get("hand"):
        view["hand"].pop("deck", None)
        view["hand"]["pot"] = _pot_total(state)
    view["legalActions"] = legal_actions(state, viewer_id) if viewer_id else {}
    view["viewerId"] = viewer_id
    view["isHost"] = is_host
    return view
