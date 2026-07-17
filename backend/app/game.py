from __future__ import annotations

import random
import time
import uuid
from copy import deepcopy
from itertools import combinations
from typing import Any

from treys import Card, Evaluator


class GameError(ValueError):
    pass


RANKS = "23456789TJQKA"
SUITS = "shdc"
RANK_VALUES = {rank: index + 2 for index, rank in enumerate(RANKS)}
RANK_WORDS = {14: "Ace", 13: "King", 12: "Queen", 11: "Jack", 10: "Ten", 9: "Nine", 8: "Eight", 7: "Seven", 6: "Six", 5: "Five", 4: "Four", 3: "Three", 2: "Two"}
RANK_PLURALS = {14: "Aces", 13: "Kings", 12: "Queens", 11: "Jacks", 10: "Tens", 9: "Nines", 8: "Eights", 7: "Sevens", 6: "Sixes", 5: "Fives", 4: "Fours", 3: "Threes", 2: "Twos"}


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
        "sessionStartedAt": None,
        "sessionEndsAt": None,
        "timeExtensionProposal": None,
        "endAfterHand": False,
        "settlement": [],
        "chat": [],
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
    return [p for p in state["players"] if p["status"] == "seated" and p["stack"] > 0 and not p.get("sittingOut") and not p.get("waitingForNextHand")]


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
        session_minutes = int(config.get("sessionMinutes", 180))
        time_bank = int(config.get("timeBankSeconds", 60))
    except (KeyError, TypeError, ValueError) as exc:
        raise GameError("Stakes must be whole numbers.") from exc
    if not (sb > 0 and bb >= sb * 2 and buyin >= bb * 20 and 2 <= max_seats <= 9 and 10 <= timeout <= 300 and 3 <= auto_next <= 30 and 15 <= session_minutes <= 720 and 0 <= time_bank <= 300):
        raise GameError("Use sensible blinds, a buy-in of at least 20 big blinds, 2–9 seats, a 10–300 second timer, a 15–720 minute session, and a 0–300 second time bank.")
    return {"smallBlind": sb, "bigBlind": bb, "buyIn": buyin, "maxSeats": max_seats, "timeoutSeconds": timeout, "autoNextHandSeconds": auto_next, "sessionMinutes": session_minutes, "timeBankSeconds": time_bank}


def seat_player(state: dict, player_id: str, name: str, seat: int) -> None:
    if state["status"] == "closed":
        raise GameError("This table has ended.")
    if not 0 <= seat < state["config"]["maxSeats"]:
        raise GameError("That seat is unavailable.")
    player = player_by_id(state, player_id)
    if player is None:
        player = {"id": player_id, "name": name.strip()[:24], "seat": seat, "stack": state["config"]["buyIn"], "status": "seated", "connected": True, "ready": False, "inHand": False, "folded": False, "allIn": False, "holeCards": [], "handContribution": 0, "streetContribution": 0, "buyInTotal": state["config"]["buyIn"], "sittingOut": False, "sitOutNextHand": False, "straddleNextHand": False, "waitingForNextHand": state["status"] == "running", "timeBankRemaining": state["config"].get("timeBankSeconds", 60)}
        state["players"].append(player)
        # A late joiner is waiting for the next hand. Include their chips in the
        # active hand's conservation baseline so timers can still settle safely.
        if state["status"] == "running" and state.get("hand") and "initialChipTotal" in state["hand"]:
            state["hand"]["initialChipTotal"] += player["stack"]
    else:
        if state["status"] == "running" and player.get("seat") != seat:
            raise GameError("Seats cannot change during a hand.")
        occupant = next((p for p in state["players"] if p["status"] == "seated" and p["seat"] == seat), None)
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
    if amount < 0:
        raise GameError("Chip contributions cannot be negative.")
    paid = min(player["stack"], amount)
    player["stack"] -= paid
    player["streetContribution"] += paid
    player["handContribution"] += paid
    if player["stack"] == 0:
        player["allIn"] = True
    return paid


def _new_hand_player(player: dict) -> None:
    player.update({"inHand": True, "folded": False, "allIn": False, "holeCards": [], "handContribution": 0, "streetContribution": 0, "leaveAfterHand": False})


def session_expired(state: dict) -> bool:
    return bool(state.get("sessionEndsAt") and state["sessionEndsAt"] <= int(time.time() * 1000))


def settlement_summary(state: dict) -> list[dict]:
    return [{
        "playerId": player["id"],
        "name": player["name"],
        "boughtIn": player.get("buyInTotal", state["config"]["buyIn"]),
        "chipsOut": player.get("cashoutAmount", player.get("stack", 0)),
        "net": player.get("cashoutAmount", player.get("stack", 0)) - player.get("buyInTotal", state["config"]["buyIn"]),
        "status": player.get("status", "seated"),
    } for player in state["players"]]


def end_session(state: dict) -> bool:
    """Return true when closure is queued behind the current hand."""
    if state["status"] == "running":
        state["endAfterHand"] = True
        state["version"] += 1
        return True
    state["status"] = "closed"
    state["nextHandAt"] = None
    state["turnDeadline"] = None
    state["settlement"] = settlement_summary(state)
    state["version"] += 1
    return False


def set_sit_out(state: dict, player_id: str, sitting_out: bool) -> None:
    player = require_seated(state, player_id)
    if state["status"] == "running" and player.get("inHand"):
        player["sitOutNextHand"] = sitting_out
    else:
        player["sittingOut"] = sitting_out
        player["sitOutNextHand"] = False
    state["version"] += 1


def set_straddle(state: dict, player_id: str, enabled: bool) -> None:
    if state["status"] not in {"lobby", "complete"}:
        raise GameError("Choose a straddle only between hands.")
    player = require_seated(state, player_id)
    if player.get("sittingOut"):
        raise GameError("Sit back in before choosing a straddle.")
    player["straddleNextHand"] = enabled
    state["version"] += 1


def use_time_bank(state: dict, player_id: str) -> int:
    if state["status"] != "running" or not state.get("hand") or state["hand"].get("actorSeat") != require_seated(state, player_id)["seat"]:
        raise GameError("Your time bank is only available on your turn.")
    player = require_seated(state, player_id)
    available = int(player.get("timeBankRemaining", 0))
    if available <= 0:
        raise GameError("Your time bank is empty.")
    used = min(30, available)
    player["timeBankRemaining"] = available - used
    state["turnDeadline"] = max(state["turnDeadline"] or 0, int(time.time() * 1000)) + used * 1000
    state["version"] += 1
    return used


def propose_time_extension(state: dict, player_id: str, minutes: int) -> None:
    require_seated(state, player_id)
    if state["status"] == "running" or not session_expired(state):
        raise GameError("Time extensions can be proposed between hands after the session limit is reached.")
    if not 5 <= minutes <= 120:
        raise GameError("Choose an extension from 5 to 120 minutes.")
    voters = [p["id"] for p in state["players"] if p["status"] == "seated"]
    state["timeExtensionProposal"] = {"minutes": minutes, "votes": {player_id: True}, "voters": voters}
    state["version"] += 1


def vote_time_extension(state: dict, player_id: str, approve: bool) -> bool:
    require_seated(state, player_id)
    proposal = state.get("timeExtensionProposal")
    if not proposal or player_id not in proposal.get("voters", []):
        raise GameError("There is no time-extension vote for you.")
    proposal["votes"][player_id] = approve
    if not approve:
        state["timeExtensionProposal"] = None
        state["version"] += 1
        return False
    if all(proposal["votes"].get(voter) is True for voter in proposal["voters"]):
        state["sessionEndsAt"] = int(time.time() * 1000) + proposal["minutes"] * 60_000
        state["timeExtensionProposal"] = None
        state["version"] += 1
        return True
    state["version"] += 1
    return False


def start_hand(state: dict) -> None:
    if state["status"] not in {"lobby", "complete"}:
        raise GameError("Finish the current hand first.")
    if state.get("sessionEndsAt") and session_expired(state):
        raise GameError("The session limit has been reached. Everyone must approve an extension before another hand.")
    participants = live_players(state)
    if len(participants) < 2:
        raise GameError("At least two seated players with chips are required.")
    if state.get("sessionStartedAt") is None:
        state["sessionStartedAt"] = int(time.time() * 1000)
        state["sessionEndsAt"] = state["sessionStartedAt"] + state["config"].get("sessionMinutes", 180) * 60_000
    initial_chip_total = sum(player["stack"] for player in state["players"])
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
    straddle_seat = None
    if not heads_up:
        candidate_seat = next_eligible_seat(state, bb_seat, lambda p: p in participants)
        candidate = player_by_seat(state, candidate_seat) if candidate_seat is not None else None
        if candidate and candidate.get("straddleNextHand") and candidate["stack"] > 0:
            _post(candidate, state["config"]["bigBlind"] * 2)
            straddle_seat = candidate_seat
        for player in participants:
            player["straddleNextHand"] = False
    current_bet = player_by_seat(state, straddle_seat)["streetContribution"] if straddle_seat is not None else bb_player["streetContribution"]
    first_actor = sb_seat if heads_up else next_eligible_seat(state, straddle_seat if straddle_seat is not None else bb_seat, lambda p: p.get("inHand") and not p.get("allIn"))
    state["handNumber"] += 1
    state["status"] = "running"
    state["nextHandAt"] = None
    state["lastResult"] = None
    state["hand"] = {
        "id": uuid.uuid4().hex[:12],
        "street": "preflop",
        "deck": deck,
        "board": [],
        "smallBlindSeat": sb_seat,
        "bigBlindSeat": bb_seat,
        "straddleSeat": straddle_seat,
        "currentBet": current_bet,
        "minRaise": state["config"]["bigBlind"],
        "pending": [p["id"] for p in participants if not p.get("allIn")],
        "raiseLocked": [],
        "actorSeat": first_actor,
        "actions": [],
        "initialChipTotal": initial_chip_total,
        "startingStacks": {player["id"]: player["stack"] + player["handContribution"] for player in participants},
    }
    _assert_chip_conservation(state)
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


def _assert_chip_conservation(state: dict, after_payout: bool = False) -> None:
    hand = state.get("hand")
    if not hand or "initialChipTotal" not in hand:
        return
    chips_in_stacks = sum(player["stack"] for player in state["players"])
    expected = hand["initialChipTotal"]
    observed = chips_in_stacks if after_payout else chips_in_stacks + _pot_total(state)
    if observed != expected:
        raise GameError(f"Chip accounting invariant failed: expected {expected}, observed {observed}.")


def _record_action(state: dict, player: dict, action: str, chips_committed: int, street_total: int | None = None) -> None:
    state["hand"]["actions"].append({
        "sequence": len(state["hand"]["actions"]) + 1,
        "street": state["hand"]["street"],
        "playerId": player["id"],
        "type": action,
        # `amount` remains for compatibility; the explicit fields remove ambiguity.
        "amount": chips_committed,
        "chipsCommitted": chips_committed,
        "streetTotal": player["streetContribution"] if street_total is None else street_total,
        "handTotal": player["handContribution"],
        "potAfter": _pot_total(state),
        "at": int(time.time() * 1000),
    })


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
    can_raise = player_id not in state["hand"].get("raiseLocked", [])
    return {"fold": True, "check": to_call == 0, "call": to_call > 0, "allIn": player["stack"] > 0, "canRaise": can_raise, "toCall": min(to_call, player["stack"]), "minRaiseTo": min(min_raise_to, maximum), "maxRaiseTo": maximum}


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
        if action != "allin" and player_id in state["hand"].get("raiseLocked", []):
            raise GameError("A short all-in raised the wager; you may call or fold, but cannot re-raise.")
        if target <= state["hand"]["currentBet"] and action != "allin":
            raise GameError("Raise amount must exceed the current bet.")
        if target > player["streetContribution"] + player["stack"]:
            raise GameError("That exceeds your stack.")
        if action == "allin" and target <= state["hand"]["currentBet"]:
            # An all-in is also a valid short call. It must not be treated as a raise.
            paid = _post(player, target - player["streetContribution"])
        else:
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
                state["hand"]["raiseLocked"] = []
            elif before_bet:
                # A short all-in can be called, but cannot reopen raising for prior actors.
                state["hand"]["raiseLocked"] = _pending_others(state, player_id)
    else:
        raise GameError("Unknown action.")
    _record_action(state, player, action, paid)
    _assert_chip_conservation(state)
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


def _straight_high(ranks: list[int]) -> int | None:
    unique = sorted(set(ranks), reverse=True)
    if unique == [14, 5, 4, 3, 2]:
        return 5
    return unique[0] if len(unique) == 5 and unique[0] - unique[-1] == 4 else None


def _describe_five_cards(cards: list[str]) -> str:
    """Describe the exact five-card hand that won a pot for readable history."""
    ranks = [RANK_VALUES[card[0].upper()] for card in cards]
    counts = sorted(((ranks.count(rank), rank) for rank in set(ranks)), reverse=True)
    flush = len({card[1].lower() for card in cards}) == 1
    straight_high = _straight_high(ranks)
    if flush and straight_high:
        return f"{RANK_WORDS[straight_high]}-high straight flush"
    if counts[0][0] == 4:
        return f"Four {RANK_PLURALS[counts[0][1]]}"
    if counts[0][0] == 3 and counts[1][0] == 2:
        return f"{RANK_PLURALS[counts[0][1]]} full of {RANK_PLURALS[counts[1][1]]}"
    if flush:
        return f"{RANK_WORDS[max(ranks)]}-high flush"
    if straight_high:
        return f"{RANK_WORDS[straight_high]}-high straight"
    if counts[0][0] == 3:
        return f"Three {RANK_PLURALS[counts[0][1]]}"
    if counts[0][0] == 2 and counts[1][0] == 2:
        high_pair, low_pair = sorted((counts[0][1], counts[1][1]), reverse=True)
        return f"Two pair, {RANK_PLURALS[high_pair]} and {RANK_PLURALS[low_pair]}"
    if counts[0][0] == 2:
        return f"Pair of {RANK_PLURALS[counts[0][1]]}"
    return f"{RANK_WORDS[max(ranks)]}-high"


def _winning_hand_description(evaluator: Evaluator, board: list[str], hole_cards: list[str]) -> str:
    all_cards = board + hole_cards
    scored = [(evaluator.evaluate([], [Card.new(card[0].upper() + card[1]) for card in combo]), list(combo)) for combo in combinations(all_cards, 5)]
    _, best_cards = min(scored, key=lambda item: item[0])
    return _describe_five_cards(best_cards)


def winner_hand_details(board: list[str], players: list[dict], winner_ids: list[str]) -> list[dict]:
    """Return readable winning-hand descriptions for live and legacy hand records."""
    evaluator = Evaluator()
    by_id = {player.get("id"): player for player in players}
    details = []
    for player_id in winner_ids:
        player = by_id.get(player_id, {})
        hole_cards = player.get("holeCards", [])
        if len(board) == 5 and len(hole_cards) == 2:
            details.append({"playerId": player_id, "description": _winning_hand_description(evaluator, board, hole_cards)})
    return details


def _showdown(state: dict) -> None:
    hand = state["hand"]
    if len(hand["board"]) < 5:
        _runout_and_showdown(state)
        return
    evaluator = Evaluator()
    board = [Card.new(card[0].upper() + card[1]) for card in hand["board"]]
    contributions = sorted({p["handContribution"] for p in state["players"] if p["handContribution"] > 0})
    previous, pots, refunds = 0, [], []
    for threshold in contributions:
        contributors = [p for p in state["players"] if p["handContribution"] >= threshold]
        amount = (threshold - previous) * len(contributors)
        eligible = [p for p in contributors if not p["folded"]]
        if len(contributors) == 1:
            # A player cannot win an unmatched part of their own wager. Return it.
            contributor = contributors[0]
            contributor["stack"] += amount
            refunds.append({"amount": amount, "playerId": contributor["id"]})
        elif amount and eligible:
            scores = {p["id"]: evaluator.evaluate(board, [Card.new(c[0].upper() + c[1]) for c in p["holeCards"]]) for p in eligible}
            best = min(scores.values())
            winners = [p for p in eligible if scores[p["id"]] == best]
            share, remainder = divmod(amount, len(winners))
            for p in winners:
                p["stack"] += share
            for seat in ordered_seats_after(state, state["buttonSeat"], winners)[:remainder]:
                player_by_seat(state, seat)["stack"] += 1
            winner_hands = winner_hand_details(hand["board"], winners, [player["id"] for player in winners])
            pots.append({"amount": amount, "winners": [p["id"] for p in winners], "eligible": [p["id"] for p in eligible], "handClass": evaluator.class_to_string(evaluator.get_rank_class(best)), "winnerHands": winner_hands})
        previous = threshold
    _finish_hand(state, pots, "showdown", refunds)


def _finish_hand(state: dict, pots: list[dict], result_type: str, refunds: list[dict] | None = None) -> None:
    hand = state["hand"]
    refunds = refunds or []
    player_results = [
        _player_hand_result(hand, player)
        for player in state["players"]
        if player.get("handContribution", 0) or player.get("inHand")
    ]
    state["lastResult"] = {
        "handNumber": state["handNumber"],
        "type": result_type,
        "board": hand["board"],
        "pots": pots,
        "refunds": refunds,
        "potTotal": sum(pot["amount"] for pot in pots),
        "totalCommitted": _pot_total(state),
        "playerResults": player_results,
        "at": int(time.time() * 1000),
    }
    state["status"] = "complete"
    state["turnDeadline"] = None
    state["nextHandAt"] = int(time.time() * 1000) + state["config"].get("autoNextHandSeconds", 8) * 1000
    hand["actorSeat"] = None
    for p in state["players"]:
        if p.get("leaveAfterHand"):
            _cash_out_player(p)
        if p.get("sitOutNextHand"):
            p["sittingOut"] = True
            p["sitOutNextHand"] = False
        p["waitingForNextHand"] = False
        p["inHand"] = False
    if state.get("endAfterHand"):
        state["status"] = "closed"
        state["nextHandAt"] = None
        state["settlement"] = settlement_summary(state)
    _assert_chip_conservation(state, after_payout=True)
    state["version"] += 1


def _player_hand_result(hand: dict, player: dict) -> dict:
    starting_stack = hand.get("startingStacks", {}).get(player["id"], player["stack"] + player.get("handContribution", 0))
    committed = player.get("handContribution", 0)
    return {
        "playerId": player["id"],
        "startingStack": starting_stack,
        "endingStack": player["stack"],
        "committed": committed,
        # This derives the actual payout from final chip movement, including odd chips and refunds.
        "payout": player["stack"] - (starting_stack - committed),
        "net": player["stack"] - starting_stack,
    }


def _cash_out_player(player: dict) -> None:
    player["status"] = "cashout"
    player["cashoutAmount"] = player["stack"]
    player["formerSeat"] = player["seat"]
    player["seat"] = -1
    player["sittingOut"] = False


def request_cashout(state: dict, player_id: str) -> int | None:
    player = require_seated(state, player_id)
    if state["status"] == "running" and player.get("inHand"):
        player["leaveAfterHand"] = True
        state["version"] += 1
        return None
    _cash_out_player(player)
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
    player["buyInTotal"] = player.get("buyInTotal", state["config"]["buyIn"]) + amount
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
    if session_expired(state):
        state["nextHandAt"] = None
        state["version"] += 1
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
        player["holeCards"] = cards if player["id"] == viewer_id or (state["status"] in {"complete", "closed"} and not player.get("folded")) else []
    if view.get("hand"):
        view["hand"].pop("deck", None)
        view["hand"]["pot"] = _pot_total(state)
    view["legalActions"] = legal_actions(state, viewer_id) if viewer_id else {}
    view["viewerId"] = viewer_id
    view["isHost"] = is_host
    # Server clock at emit time (epoch ms). turnDeadline/nextHandAt are server-relative, so the
    # client uses this to correct for client-vs-server clock drift instead of its own Date.now().
    view["serverTime"] = int(time.time() * 1000)
    return view
