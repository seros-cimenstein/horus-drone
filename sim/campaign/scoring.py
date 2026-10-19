"""Candidate scoring shared by the episode harness and the campaign summary.

Importable without the HORUS package: ``horus`` is only loaded when a candidate is scored.
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
import math
from typing import Any


def candidate_bbox(candidate: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """Return a valid xyxy box from a HORUS candidate without mutating its payload."""
    from horus.candidates import candidate_bbox as parse

    return parse(candidate)


def candidate_center(candidate: Mapping[str, Any]) -> tuple[float, float] | None:
    box = candidate_bbox(candidate)
    return None if box is None else ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def selection_min_confidence() -> float:
    """The runner's detector-confidence floor for ``select_candidate`` (single source of truth)."""
    from horus.repl.missions import DEFAULT_SELECTION_MIN_CONFIDENCE

    return float(DEFAULT_SELECTION_MIN_CONFIDENCE)


def selection_rule(attribute: str, equals: Any, *, min_confidence: float | None = None) -> dict[str, Any]:
    """Describe the selection rule the runner applies, for scoring metadata."""
    return {
        "attribute": attribute,
        "equals": equals,
        "choose": "largest",
        "min_confidence": selection_min_confidence() if min_confidence is None else float(min_confidence),
    }


def select_largest_attribute_candidate(
    candidates: Iterable[Mapping[str, Any]], *, attribute: str = "shirt_colour", equals: Any,
    min_confidence: float | None = None,
) -> Mapping[str, Any] | None:
    """Apply the runner's ``select_candidate`` rule, including its confidence floor.

    The runner owns this rule (``horus.repl.missions._find_candidate``): largest exact-attribute
    match whose detector confidence passes the floor, low ID breaking ties. Scoring must use
    the same function, or the harness will credit selections the runner can never make.
    """
    from horus.repl.missions import _find_candidate

    floor = selection_min_confidence() if min_confidence is None else float(min_confidence)
    return _find_candidate(candidates, attribute=attribute, equals=equals, choose="largest",
                           min_confidence=floor)


def _eligible_candidates(
    table: list[Mapping[str, Any]], point: tuple[float, float], *, expand_px: float, nearest_px: float,
    taken: Container[int] = (),
) -> list[tuple[int, float, int]]:
    """``(priority, centre distance, index)`` of every candidate a torso point may match.

    Priority 0 is a torso inside the box expanded by ``expand_px``; priority 1 a centre within
    ``nearest_px``.
    """
    choices: list[tuple[int, float, int]] = []
    for candidate_index, candidate in enumerate(table):
        if candidate_index in taken:
            continue
        box = candidate_bbox(candidate)
        centre = candidate_center(candidate)
        if box is None or centre is None:
            continue
        distance = math.hypot(point[0] - centre[0], point[1] - centre[1])
        inside = (box[0] - expand_px <= point[0] <= box[2] + expand_px
                  and box[1] - expand_px <= point[1] <= box[3] + expand_px)
        if inside:
            choices.append((0, distance, candidate_index))
        elif distance <= nearest_px:
            choices.append((1, distance, candidate_index))
    return choices


def match_candidates_to_people(
    candidates: Iterable[Mapping[str, Any]],
    projected_people: Mapping[str, tuple[float, float] | None],
    *,
    expand_px: float = 15.0,
    nearest_px: float = 40.0,
) -> dict[str, Any]:
    """Greedily make deterministic one-to-one torso-to-box associations.

    A torso inside a box expanded by ``expand_px`` is preferred. Otherwise the
    nearest candidate centre is eligible only within ``nearest_px``.
    """
    table = [candidate for candidate in candidates if isinstance(candidate, Mapping)]
    edges: list[tuple[int, float, int, str]] = []
    errors: dict[str, float | None] = {name: None for name in projected_people}
    for person_index, (name, point) in enumerate(projected_people.items()):
        if point is None or not all(math.isfinite(value) for value in point):
            continue
        choices = _eligible_candidates(table, point, expand_px=expand_px, nearest_px=nearest_px)
        if choices:
            priority, distance, _candidate_index = min(choices)
            edges.append((priority, distance, person_index, name))
            # Store the best possible error for diagnostics even if another person
            # wins the one-to-one conflict for this candidate.
            errors[name] = distance
    assigned_people: dict[str, Mapping[str, Any]] = {}
    assigned_indices: set[int] = set()
    for _priority, _distance, _person_index, name in sorted(edges):
        # Re-evaluate the candidate selected by this person's best edge. If it is
        # already taken, use the next eligible candidate while preserving priority.
        available = _eligible_candidates(table, projected_people[name], expand_px=expand_px,
                                         nearest_px=nearest_px, taken=assigned_indices)
        if available:
            _priority, distance, index = min(available)
            assigned_people[name] = table[index]
            assigned_indices.add(index)
            errors[name] = distance
    unmatched = [candidate for index, candidate in enumerate(table) if index not in assigned_indices]
    return {"matches": assigned_people, "unmatched_candidates": unmatched, "center_error_px": errors}


def score_detection_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute overall recall and stratified detection counts from person observations."""
    entries = [row for row in rows if isinstance(row, Mapping)]
    strata: dict[str, dict[str, dict[str, int]]] = {
        field: {} for field in ("slant_bin", "posture", "clothing", "los_blocked")
    }
    total = detected = 0
    confusion: dict[str, dict[str, int]] = {}
    for row in entries:
        total += 1
        hit = bool(row.get("detected"))
        detected += int(hit)
        for field in strata:
            key = str(row.get(field, "unknown"))
            bucket = strata[field].setdefault(key, {"seen": 0, "detected": 0})
            bucket["seen"] += 1
            bucket["detected"] += int(hit)
        if hit:
            actual = str(row.get("clothing", "unknown"))
            reported = str(row.get("shirt_colour", "unknown"))
            confusion.setdefault(actual, {})[reported] = confusion.setdefault(actual, {}).get(reported, 0) + 1
    return {"seen": total, "detected": detected, "recall": detected / total if total else None,
            "strata": strata, "shirt_colour_confusion": confusion}
