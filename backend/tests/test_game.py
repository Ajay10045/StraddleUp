from app.game import (GameError, _showdown, act, auto_start_next_hand, legal_actions, new_state, public_view,
                      seat_player, start_hand, timeout_active_player)


CONFIG = {"smallBlind": 5, "bigBlind": 10, "buyIn": 1000, "maxSeats": 9, "timeoutSeconds": 30}


def setup_players(count=2):
    state = new_state("testtable", CONFIG)
    for seat in range(count):
        seat_player(state, f"p{seat}", f"Player {seat}", seat)
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
