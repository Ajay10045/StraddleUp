from typing import Any


def completed_hand_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Create an immutable, host-readable completed-hand record."""
    hand = state["hand"] or {}
    return {
        "handNumber": state["handNumber"],
        "result": state["lastResult"],
        "board": hand.get("board", []),
        "actions": hand.get("actions", []),
        "players": [
            {
                "id": player["id"],
                "name": player["name"],
                "seat": player["seat"],
                "holeCards": player.get("holeCards", []),
                "contribution": player.get("handContribution", 0),
                "endingStack": player.get("stack", 0),
                "folded": player.get("folded", False),
            }
            for player in state["players"]
            if player.get("handContribution", 0) > 0 or player.get("inHand")
        ],
    }
