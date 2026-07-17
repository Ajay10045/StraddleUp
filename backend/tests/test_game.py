from app.game import (GameError, _showdown, act, auto_start_next_hand, end_session, legal_actions, new_state,
                      propose_time_extension, public_view, seat_player, set_sit_out, set_straddle, start_hand,
                      timeout_active_player, vote_time_extension)


CONFIG = {"smallBlind": 5, "bigBlind": 10, "buyIn": 1000, "maxSeats": 9, "timeoutSeconds": 30}


def setup_players(count=2):
    state = new_state("testtable", CONFIG)
    for seat in range(count):
        seat_player(state, f"p{seat}", f"Player {seat}", seat)
    return state


def setup_players_with_config(config, stacks):
    state = new_state("testtable", config)
    for seat, stack in enumerate(stacks):
        seat_player(state, f"p{seat}", f"Player {seat}", seat)
        state["players"][-1]["stack"] = stack
    return state


def test_starting_a_heads_up_hand_assigns_correct_blinds_and_turn():
    state = setup_players(2)
    start_hand(state)
    assert state["hand"]["smallBlindSeat"] == state["buttonSeat"]
    assert state["hand"]["bigBlindSeat"] != state["buttonSeat"]
    assert state["hand"]["actorSeat"] == state["buttonSeat"]
    assert sum(p["handContribution"] for p in state["players"]) == 15
    assert legal_actions(state, "p0")


def test_out_of_turn_action_is_rejected():
    state = setup_players(2)
    start_hand(state)
    other = "p1" if state["hand"]["actorSeat"] == 0 else "p0"
    try:
        act(state, other, "fold")
    except GameError as exc:
        assert "not your turn" in str(exc)
    else:
        raise AssertionError("out-of-turn action must fail")


def test_fold_awards_the_entire_pot():
    state = setup_players(2)
    start_hand(state)
    actor = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
    other = next(p for p in state["players"] if p is not actor)
    before = other["stack"]
    act(state, actor["id"], "fold")
    assert state["status"] == "complete"
    assert other["stack"] == before + 15


def test_heads_up_call_and_checks_progress_to_showdown():
    state = setup_players(2)
    start_hand(state)
    first = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
    act(state, first["id"], "call")
    second = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
    act(state, second["id"], "check")
    assert state["hand"]["street"] == "flop"
    for _ in range(3):
        first = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
        act(state, first["id"], "check")
        second = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
        act(state, second["id"], "check")
    assert state["status"] == "complete"
    assert len(state["lastResult"]["board"]) == 5


def showdown_state(contributions, holes, board):
    state = setup_players(len(contributions))
    state["status"] = "running"
    state["buttonSeat"] = 0
    state["hand"] = {"id": "hand", "street": "showdown", "deck": [], "board": board, "actions": [], "actorSeat": None, "pending": [], "currentBet": 0, "minRaise": 10, "smallBlindSeat": 0, "bigBlindSeat": 1}
    for index, player in enumerate(state["players"]):
        player.update({"stack": 0, "inHand": True, "folded": False, "allIn": True, "handContribution": contributions[index], "streetContribution": 0, "holeCards": holes[index]})
    return state


def test_uneven_all_in_builds_main_and_side_pots():
    state = showdown_state([100, 50, 100], [["Ah", "Ad"], ["Kh", "Kd"], ["Qh", "Qd"]], ["2s", "3c", "4d", "5h", "9s"])
    _showdown(state)
    assert state["status"] == "complete"
    assert [pot["amount"] for pot in state["lastResult"]["pots"]] == [150, 100]
    assert state["players"][0]["stack"] == 250
    assert state["players"][1]["stack"] == 0
    assert state["players"][2]["stack"] == 0


def test_split_pot_divides_chips_between_tied_players():
    state = showdown_state([101, 101], [["Ah", "Kd"], ["Qh", "Jd"]], ["2s", "3s", "4s", "5s", "6s"])
    _showdown(state)
    assert state["players"][0]["stack"] + state["players"][1]["stack"] == 202
    assert set(state["lastResult"]["pots"][0]["winners"]) == {"p0", "p1"}


def test_private_cards_are_not_leaked_to_other_players():
    state = setup_players(2)
    start_hand(state)
    view = public_view(state, "p0")
    assert len(next(p for p in view["players"] if p["id"] == "p0")["holeCards"]) == 2
    assert next(p for p in view["players"] if p["id"] == "p1")["holeCards"] == []


def test_expired_turn_auto_folds():
    state = setup_players(2)
    start_hand(state)
    state["turnDeadline"] = 1
    timed_out = timeout_active_player(state)
    assert timed_out in {"p0", "p1"}
    assert state["status"] == "complete"


def test_completed_hand_is_automatically_dealt_after_its_delay():
    state = setup_players(2)
    start_hand(state)
    actor = next(p for p in state["players"] if p["seat"] == state["hand"]["actorSeat"])
    act(state, actor["id"], "fold")
    assert state["nextHandAt"] is not None
    state["nextHandAt"] = 1
    assert auto_start_next_hand(state)
    assert state["status"] == "running"
    assert state["handNumber"] == 2


def test_folded_flop_bet_has_correct_pot_payout_and_net_result():
    config = {"smallBlind": 50, "bigBlind": 100, "buyIn": 5000, "maxSeats": 2, "timeoutSeconds": 60}
    state = setup_players_with_config(config, [5000, 5000])
    start_hand(state)

    act(state, "p0", "raise", 900)  # Small blind raises to 900, paying 850 more.
    assert state["hand"]["actions"][-1]["chipsCommitted"] == 850
    assert state["hand"]["actions"][-1]["streetTotal"] == 900
    assert state["hand"]["actions"][-1]["potAfter"] == 1000

    act(state, "p1", "call")
    act(state, "p1", "check")
    act(state, "p0", "bet", 400)
    act(state, "p1", "fold")

    result = state["lastResult"]
    assert result["potTotal"] == 2200
    assert result["totalCommitted"] == 2200
    assert state["players"][0]["stack"] == 5900
    assert state["players"][1]["stack"] == 4100
    assert sum(player["stack"] for player in state["players"]) == 10_000
    by_player = {entry["playerId"]: entry for entry in result["playerResults"]}
    assert by_player["p0"] == {"playerId": "p0", "startingStack": 5000, "endingStack": 5900, "committed": 1300, "payout": 2200, "net": 900}
    assert by_player["p1"] == {"playerId": "p1", "startingStack": 5000, "endingStack": 4100, "committed": 900, "payout": 0, "net": -900}


def test_short_all_in_can_call_a_larger_bet_without_creating_chips():
    config = {"smallBlind": 5, "bigBlind": 10, "buyIn": 1000, "maxSeats": 2, "timeoutSeconds": 60}
    state = setup_players_with_config(config, [1000, 500])
    start_hand(state)
    act(state, "p0", "raise", 600)
    act(state, "p1", "allin")
    assert state["players"][1]["allIn"]
    assert state["players"][1]["handContribution"] == 500
    assert sum(player["stack"] for player in state["players"]) + sum(player["handContribution"] for player in state["players"]) == 1500


def test_unmatched_showdown_excess_is_refunded_not_awarded_as_a_side_pot():
    state = showdown_state([100, 50], [["Ah", "Ad"], ["Kh", "Kd"]], ["2s", "3c", "4d", "5h", "9s"])
    state["hand"].update({"initialChipTotal": 150, "startingStacks": {"p0": 100, "p1": 50}})
    _showdown(state)
    assert state["lastResult"]["potTotal"] == 100
    assert state["lastResult"]["refunds"] == [{"amount": 50, "playerId": "p0"}]
    assert state["players"][0]["stack"] == 150
    assert state["players"][1]["stack"] == 0


def test_cashout_frees_seat_and_preserves_settlement_ledger():
    state = setup_players(2)
    state["players"][0]["buyInTotal"] = 1200
    from app.game import request_cashout, settlement_summary
    request_cashout(state, "p0")
    assert state["players"][0]["status"] == "cashout"
    assert state["players"][0]["seat"] == -1
    seat_player(state, "p2", "Player 2", 0)
    settlement = settlement_summary(state)
    assert next(entry for entry in settlement if entry["playerId"] == "p0")["net"] == -200


def test_session_extension_requires_all_seated_players_to_approve():
    state = setup_players(2)
    state["sessionEndsAt"] = 1
    propose_time_extension(state, "p0", 30)
    assert vote_time_extension(state, "p1", True) is True
    assert state["timeExtensionProposal"] is None
    assert state["sessionEndsAt"] > 1


def test_sit_out_and_straddle_are_applied_to_next_hand():
    state = setup_players(3)
    set_sit_out(state, "p2", True)
    assert len([p for p in state["players"] if p["status"] == "seated" and not p.get("sittingOut")]) == 2
    set_sit_out(state, "p2", False)
    set_straddle(state, "p0", True)
    start_hand(state)
    assert state["hand"]["straddleSeat"] == 0
    assert state["hand"]["currentBet"] == state["config"]["bigBlind"] * 2


def test_end_session_returns_settlement_when_not_in_hand():
    state = setup_players(2)
    state["players"][0]["stack"] = 1100
    state["players"][0]["buyInTotal"] = 1000
    assert end_session(state) is False
    assert state["status"] == "closed"
    assert state["settlement"][0]["net"] == 100


def test_late_joiner_does_not_break_timeout_accounting():
    state = setup_players(2)
    start_hand(state)
    seat_player(state, "p2", "Late Player", 2)
    assert state["players"][-1]["waitingForNextHand"]
    state["turnDeadline"] = 1
    assert timeout_active_player(state) in {"p0", "p1"}


def test_short_stack_wins_only_the_matched_main_pot_and_excess_is_refunded():
    # In a 50/100 game a small blind with 400 has only 350 left to add, but
    # their all-in total is 400. A deeper opponent's unmatched chips are never
    # awarded to the short stack.
    state = showdown_state([400, 19_600], [["Ah", "Ad"], ["Kh", "Kd"]], ["2s", "3c", "4d", "5h", "9s"])
    state["hand"].update({"initialChipTotal": 20_000, "startingStacks": {"p0": 400, "p1": 19_600}})
    _showdown(state)
    result = state["lastResult"]
    assert result["potTotal"] == 800
    assert result["refunds"] == [{"amount": 19_200, "playerId": "p1"}]
    assert state["players"][0]["stack"] == 800
    assert result["playerResults"][0] == {"playerId": "p0", "startingStack": 400, "endingStack": 800, "committed": 400, "payout": 800, "net": 400}


def test_showdown_history_names_the_exact_winning_hand():
    state = showdown_state([100, 100], [["Ah", "Kh"], ["9d", "9c"]], ["Qh", "Jh", "Th", "2c", "3d"])
    _showdown(state)
    winner_hand = state["lastResult"]["pots"][0]["winnerHands"]
    assert winner_hand == [{"playerId": "p0", "description": "Ace-high straight flush"}]
