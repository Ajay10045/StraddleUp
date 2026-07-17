from copy import deepcopy
from typing import Any

from .game import winner_hand_details


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


def audit_hand_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add modern winner descriptions while preserving immutable legacy events."""
    audit = deepcopy(payload)
    result = audit.get("result") or {}
    for pot in result.get("pots", []):
        if not pot.get("winnerHands"):
            pot["winnerHands"] = winner_hand_details(audit.get("board", []), audit.get("players", []), pot.get("winners", []))
    return audit
