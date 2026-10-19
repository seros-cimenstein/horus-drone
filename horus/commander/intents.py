"""Operator text that bypasses the model: the emergency and reset grammar every front-end shares."""

from __future__ import annotations

_EMERGENCY_WORDS = {
    "stop": "hold",
    "halt": "hold",
    "abort": "hold",
    "hold": "hold",
    "freeze": "hold",
    "land": "land",
    "rth": "rth",
    "return home": "rth",
}


def emergency_action(text: str) -> str | None:
    """Map a short imperative emergency command to hold/land/rth; anything longer stays with the model.

    A trailing "now"/"immediately" and trailing punctuation are ignored, so "STOP!" and
    "land now" are emergencies while "land next to the hiker" is a request for the commander.
    """
    command = " ".join(text.lower().split()).rstrip(".,!?;:")
    words = command.split()
    if words and words[-1] in {"now", "immediately"}:
        words.pop()
        command = " ".join(words)
    return _EMERGENCY_WORDS.get(command)


def is_reset_command(text: str) -> bool:
    """True for the operator phrases that end the current mission ("new mission", "reset")."""
    return text.strip().lower().rstrip(".!?") in {"new mission", "reset"}
