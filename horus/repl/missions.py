"""Deterministic YAML mission runner behind ``horus --mission``."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import yaml

from horus.candidates import candidate_bbox
from horus.commander import HorusCommander
from horus.events import TERMINAL_TASK_STATUSES
from horus.mcp_client import call_json

EMERGENCY_TOOLS = {"hold", "rth", "land"}
MISSION_SKILLS = frozenset({"take_off", "goto", "approach", "follow", "orbit", "scan", "search", *EMERGENCY_TOOLS})
# Detector-confidence floor for select_candidate; a step overrides it with min_confidence.
DEFAULT_SELECTION_MIN_CONFIDENCE = 0.5
# wait_for_task blocks for at most this long per call.
_WAIT_FOR_TASK_MAX_S = 60.0


def _as_float(raw: Any) -> float:
    """A real (non-bool) number as float; NaN for anything else, so finiteness checks reject it."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return math.nan
    try:
        return float(raw)
    except OverflowError:
        return math.nan


def _finite_seconds(spec: Mapping[str, Any], name: str, default: Any, *, step: str) -> float:
    """Validate ``spec[name]`` (or ``default``) as a finite positive number of seconds."""
    value = _as_float(spec.get(name, default))
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{step} {name} must be a finite positive number")
    return value


async def _wait_task(session: Any, task_id: str, timeout_s: float) -> dict[str, Any]:
    expires = time.monotonic() + timeout_s
    while (remaining := expires - time.monotonic()) > 0.0:
        status = await call_json(
            session, "wait_for_task", {"task_id": task_id, "timeout_s": min(remaining, _WAIT_FOR_TASK_MAX_S)}
        )
        if not isinstance(status, Mapping):
            raise RuntimeError(f"unexpected task status response: {status}")
        if status.get("status") in TERMINAL_TASK_STATUSES:
            return {key: value for key, value in status.items() if key not in {"waited_s", "timed_out"}}
    await call_json(session, "cancel", {"task_id": task_id})
    raise TimeoutError(f"mission task {task_id} did not finish within {timeout_s:g}s")


@dataclass
class _MissionRun:
    """State shared by one mission's step handlers across its repeated runs."""

    session: Any
    path: Path
    name: str
    driver_path: Path | None
    driver_args: list[str]
    server_url: str | None
    bearer_token: str | None
    commander_settings: Any
    driver_process: asyncio.subprocess.Process | None = None
    driver_started_at: float | None = None
    commander: HorusCommander | None = None
    commander_task: asyncio.Task | None = None
    run_number: int = 0
    run_started_at: datetime | None = None
    variables: dict[str, Any] = field(default_factory=dict)

    async def start_driver(self) -> None:
        if self.driver_process is None:
            self.driver_started_at = time.monotonic()
            self.driver_process = await asyncio.create_subprocess_exec(
                sys.executable, str(self.driver_path), *self.driver_args
            )

    def check_driver(self) -> None:
        if self.driver_process is not None and self.driver_process.returncode is not None:
            raise RuntimeError(f"mission driver exited with status {self.driver_process.returncode}")


def _step_body(step: Mapping[str, Any], kind: str, number: int) -> Mapping[str, Any]:
    body = step[kind]
    if set(step) != {kind} or not isinstance(body, Mapping):
        raise ValueError(f"mission {kind} step {number} is malformed")
    return body


# Step handlers return False to skip the rest of the current run.
async def _step_start_driver(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    if set(step) != {"start_driver"}:
        raise ValueError(f"mission start_driver step {number} is malformed")
    await run.start_driver()
    print(f"  {number}. start_driver: {run.driver_path.name}")
    return True


async def _step_select_candidate(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    selection = _step_body(step, "select_candidate", number)
    required = {"attribute", "equals", "choose", "store_as"}
    if not required.issubset(selection) or not set(selection).issubset(required | {"timeout_s", "min_confidence"}):
        raise ValueError(
            f"mission select_candidate step {number} needs attribute, equals, "
            "choose, and store_as; timeout_s and min_confidence are optional"
        )
    store_as = _variable_name(selection["store_as"])
    min_confidence = _as_float(selection.get("min_confidence", DEFAULT_SELECTION_MIN_CONFIDENCE))
    if not 0.0 <= min_confidence <= 1.0:  # also rejects NaN
        raise ValueError("select_candidate min_confidence must be a finite number in [0, 1]")
    timeout_s = (
        _finite_seconds(selection, "timeout_s", None, step="select_candidate") if "timeout_s" in selection else None
    )
    rule = {key: selection[key] for key in ("attribute", "equals", "choose")}
    rule["min_confidence"] = min_confidence
    if timeout_s is None:
        selected = _select_candidate(await _list_candidate_table(run.session), **rule)
    else:
        selected = await _poll_candidate(run.session, selection, timeout_s, rule)
    run.variables[store_as] = selected
    print(f"  {number}. select_candidate: {json.dumps(selected, ensure_ascii=False)}")
    return True


async def _poll_candidate(
    session: Any, selection: Mapping[str, Any], timeout_s: float, rule: Mapping[str, Any]
) -> dict[str, Any]:
    """Poll list_candidates every 0.5 s until a candidate matches ``rule`` within ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    candidates: list[Any] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise _candidate_selection_timeout(selection, timeout_s, candidates)
        try:
            candidates = await asyncio.wait_for(_list_candidate_table(session), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise _candidate_selection_timeout(selection, timeout_s, candidates) from exc
        selected = _find_candidate(candidates, **rule)
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise _candidate_selection_timeout(selection, timeout_s, candidates)
        if selected is not None:
            return selected
        await asyncio.sleep(min(0.5, remaining))


async def _step_resolve_candidate(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    resolution = _resolution_spec(_step_body(step, "resolve_candidate", number))
    outcome = await _resolve_candidate(run.session, resolution)
    run.variables[resolution["store_as"]] = outcome["candidate"] if outcome["outcome"] == "resolved" else outcome
    print(f"  {number}. resolve_candidate: {json.dumps(outcome, ensure_ascii=False)}")
    if outcome["outcome"] == "abstain":
        # Abstaining is a valid answer: the remaining steps need an identity.
        print(f"  run {run.run_number} abstained; remaining steps skipped")
        return False
    return True


async def _step_assert_reacquired(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    config = _resolve_mission_value(_step_body(step, "assert_reacquired", number), run.variables)
    result_path, result = _write_reacquisition_result(
        mission_path=run.path,
        mission_name=run.name,
        run_number=run.run_number,
        started_at=run.run_started_at,
        config=config,
    )
    print(f"  {number}. assert_reacquired: {result_path}")
    if not result["assertions"]["passed"]:
        raise RuntimeError(f"mission reacquisition assertion failed; result written to {result_path}")
    return True


async def _step_text(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    text = step["text"]
    if set(step) != {"text"} or not isinstance(text, str) or not text.strip():
        raise ValueError(f"mission text step {number} must contain only non-empty text")
    if run.commander is None:
        if not run.server_url or not run.bearer_token or run.commander_settings is None:
            raise ValueError("commander mission steps need server credentials and settings")
        run.commander = HorusCommander(run.server_url, run.bearer_token, run.commander_settings)
        run.commander_task = asyncio.create_task(run.commander.run())
    response = await run.commander.submit_task(text)
    print(f"  {number}. commander: {response}")
    return True


async def _step_tool(run: _MissionRun, number: int, step: Mapping[str, Any]) -> bool:
    name = str(step.get("tool", ""))
    args = _resolve_mission_value(step.get("arguments", {}), run.variables)
    if not name or not isinstance(args, Mapping):
        raise ValueError(f"mission step {number} needs tool and arguments mapping")
    if name not in MISSION_SKILLS:
        raise ValueError(f"mission step {number} uses unsupported skill {name!r}")
    result = await call_json(run.session, name, args)
    print(f"  {number}. {name}: {json.dumps(result, ensure_ascii=False)}")
    task_id = result.get("task_id") if isinstance(result, Mapping) else None
    stored_result = result
    if task_id and step.get("wait", True):
        timeout = float(step.get("timeout_s", args.get("ttl_s", 60.0)))
        terminal = await _wait_task(run.session, str(task_id), timeout)
        print(f"     result: {json.dumps(terminal, ensure_ascii=False)}")
        expected_status = step.get("expect_status", "succeeded")
        if terminal.get("status") != expected_status and not step.get("continue_on_failure", False):
            raise RuntimeError(f"mission step {number} ended {terminal.get('status')}: {terminal.get('error')}")
        stored_result = terminal
    store_as = step.get("store_as")
    if store_as is not None:
        run.variables[_variable_name(store_as)] = stored_result
    if not task_id and step.get("delay_s"):
        await asyncio.sleep(float(step["delay_s"]))
    return True


# A step's kind is its first key found here, in this order; "tool" steps are the fallback.
_STEP_HANDLERS: dict[str, Callable[[_MissionRun, int, Mapping[str, Any]], Awaitable[bool]]] = {
    "start_driver": _step_start_driver,
    "select_candidate": _step_select_candidate,
    "resolve_candidate": _step_resolve_candidate,
    "assert_reacquired": _step_assert_reacquired,
    "text": _step_text,
    "tool": _step_tool,
}


def _mission_driver(mission: Mapping[str, Any], mission_path: Path) -> tuple[Path | None, list[str], float | None, bool]:
    """Validate the optional driver: path, arguments, minimum duration, and a start_driver step."""
    driver_path: Path | None = None
    driver_name = mission.get("driver")
    if driver_name is not None:
        if not isinstance(driver_name, str) or not driver_name.strip():
            raise ValueError("mission driver must be a non-empty relative path")
        driver_path = (mission_path.parent / driver_name).resolve()
        if not driver_path.is_file():
            raise FileNotFoundError(f"mission driver does not exist: {driver_path}")
    driver_args = mission.get("driver_args", [])
    if not isinstance(driver_args, list):
        raise ValueError("mission driver_args must be a list of strings")
    if any(not isinstance(argument, str) for argument in driver_args):
        raise ValueError("mission driver_args items must be strings")
    if "driver_args" in mission and driver_path is None:
        raise ValueError("mission driver_args requires a driver")
    driver_min_duration = None
    if mission.get("driver_min_duration_s") is not None:
        if driver_path is None:
            raise ValueError("mission driver_min_duration_s requires a driver")
        driver_min_duration = _finite_seconds(mission, "driver_min_duration_s", None, step="mission")
    # A start_driver step launches the driver at that point in the mission (e.g. just before
    # `follow`, so the target's route is timed from the follow); without one it starts first.
    driver_step = any(isinstance(step, Mapping) and "start_driver" in step for step in mission["steps"])
    if driver_step and driver_path is None:
        raise ValueError("mission start_driver step requires a mission driver")
    return driver_path, driver_args, driver_min_duration, driver_step


async def run_mission(
    session: Any,
    path: str | Path,
    repeats: int = 1,
    *,
    server_url: str | None = None,
    bearer_token: str | None = None,
    commander_settings: Any = None,
) -> None:
    """Execute deterministic mission steps, optional commander prompts, and a target driver."""
    mission_path = Path(path)
    with mission_path.open("r", encoding="utf-8") as stream:
        mission = yaml.safe_load(stream)
    if not isinstance(mission, Mapping) or not isinstance(mission.get("steps"), list):
        raise ValueError("mission YAML must contain a steps list")
    if repeats < 1:
        raise ValueError("repeat count must be positive")
    driver_path, driver_args, driver_min_duration, driver_step = _mission_driver(mission, mission_path)
    run = _MissionRun(
        session=session,
        path=mission_path,
        name=str(mission.get("name", mission_path.stem)),
        driver_path=driver_path,
        driver_args=driver_args,
        server_url=server_url,
        bearer_token=bearer_token,
        commander_settings=commander_settings,
    )
    print(f"Mission: {run.name} ({repeats} run(s))")
    try:
        if driver_path is not None and not driver_step:
            await run.start_driver()
        for run_index in range(repeats):
            run.run_number = run_index + 1
            run.variables = {}
            run.run_started_at = datetime.now(timezone.utc)
            print(f"Run {run.run_number}/{repeats}")
            for number, step in enumerate(mission["steps"], 1):
                run.check_driver()
                if not isinstance(step, Mapping):
                    raise ValueError(f"mission step {number} must be a mapping")
                kind = next((kind for kind in _STEP_HANDLERS if kind in step), "tool")
                if not await _STEP_HANDLERS[kind](run, number, step):
                    break
        run.check_driver()
        if driver_min_duration is not None and run.driver_started_at is not None:
            remaining = driver_min_duration - (time.monotonic() - run.driver_started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        run.check_driver()
    finally:
        try:
            if run.commander is not None:
                await run.commander.stop()
            if run.commander_task is not None:
                active_error = sys.exc_info()[0] is not None
                result = await asyncio.gather(run.commander_task, return_exceptions=True)
                if isinstance(result[0], BaseException) and not active_error:
                    raise result[0]
        finally:
            if run.driver_process is not None:
                await _stop_mission_driver(run.driver_process)


def _variable_name(value: Any) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise ValueError("mission variable names must be valid identifiers")
    return value


def _resolve_mission_value(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        path = value[1:].split(".")
        if not path[0] or path[0] not in variables:
            raise ValueError(f"mission variable {value!r} is not defined")
        resolved = variables[path[0]]
        for component in path[1:]:
            if isinstance(resolved, Mapping) and component in resolved:
                resolved = resolved[component]
            elif isinstance(resolved, (list, tuple)) and component.isdigit():
                index = int(component)
                if index >= len(resolved):
                    raise ValueError(f"mission variable path {value!r} is out of range")
                resolved = resolved[index]
            else:
                raise ValueError(f"mission variable path {value!r} is not defined")
        return resolved
    if isinstance(value, Mapping):
        return {key: _resolve_mission_value(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_mission_value(item, variables) for item in value]
    return value


async def _list_candidate_table(session: Any) -> list[Any]:
    response = await call_json(session, "list_candidates")
    candidates = response.get("candidate_table") if isinstance(response, Mapping) else None
    if not isinstance(candidates, list):
        raise RuntimeError("list_candidates returned an invalid response")
    return candidates


def _candidate_selection_timeout(
    selection: Mapping[str, Any], timeout_s: float, candidates: list[Any]
) -> TimeoutError:
    return TimeoutError(
        f"select_candidate timed out after {timeout_s:g}s waiting for "
        f"attribute {selection['attribute']!r} == {selection['equals']!r}; "
        f"last candidate table summary: {_candidate_table_summary(candidates)}"
    )


def _candidate_table_summary(candidates: list[Any]) -> str:
    rows: list[Any] = []
    for candidate in candidates[:10]:
        if isinstance(candidate, Mapping):
            attributes = candidate.get("attributes")
            rows.append(
                {
                    "id": candidate.get("id"),
                    "class_name": candidate.get("class_name"),
                    "attributes": (
                        dict(attributes) if isinstance(attributes, Mapping) else attributes
                    ),
                }
            )
        else:
            rows.append(candidate)
    summary: dict[str, Any] = {"count": len(candidates), "candidates": rows}
    if len(candidates) > len(rows):
        summary["omitted"] = len(candidates) - len(rows)
    return json.dumps(summary, ensure_ascii=False, default=str)


def _find_candidate(
    candidates: list[Any],
    *,
    attribute: Any,
    equals: Any,
    choose: Any,
    min_confidence: float = DEFAULT_SELECTION_MIN_CONFIDENCE,
) -> dict[str, Any] | None:
    """Largest exact-attribute match whose detector confidence passes the floor.

    A row that carries no confidence value at all is not gated: MCP candidate tables always
    carry the detector confidence, so only synthetic/partial tables omit it. A confidence that
    is present but not a finite number can never pass a numeric floor.
    """
    if not isinstance(attribute, str) or not attribute:
        raise ValueError("select_candidate attribute must be a non-empty string")
    if choose != "largest":
        raise ValueError("select_candidate choose must be 'largest'")
    matches: list[tuple[float, int, dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        attributes = candidate.get("attributes")
        if not isinstance(attributes, Mapping) or attributes.get(attribute) != equals:
            continue
        raw_id = candidate.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
            raise ValueError("matching candidates must have positive integer IDs")
        if "confidence" in candidate:
            try:
                confidence = float(candidate["confidence"])
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(confidence) or confidence < min_confidence:
                continue
        box = candidate_bbox(candidate)
        if box is None:
            raise ValueError(f"candidate {raw_id} has no valid bounding box")
        x1, y1, x2, y2 = box
        matches.append(((x2 - x1) * (y2 - y1), raw_id, dict(candidate)))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], -item[1]))[2]


def _select_candidate(
    candidates: list[Any],
    *,
    attribute: Any,
    equals: Any,
    choose: Any,
    min_confidence: float = DEFAULT_SELECTION_MIN_CONFIDENCE,
) -> dict[str, Any]:
    selected = _find_candidate(
        candidates,
        attribute=attribute,
        equals=equals,
        choose=choose,
        min_confidence=min_confidence,
    )
    if selected is None:
        raise _candidate_selection_error(
            candidates, attribute=attribute, equals=equals, min_confidence=min_confidence
        )
    return selected


def _candidate_selection_error(
    candidates: list[Any], *, attribute: Any, equals: Any, min_confidence: float
) -> RuntimeError:
    """Report whether nothing matched the attribute, or every match failed the confidence floor."""
    rejected: list[tuple[float, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        attributes = candidate.get("attributes")
        if not isinstance(attributes, Mapping) or attributes.get(attribute) != equals:
            continue
        try:
            confidence = float(candidate.get("confidence"))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(confidence):
            rejected.append((confidence, candidate.get("id")))
    if rejected:
        best_confidence, best_id = max(rejected)
        return RuntimeError(
            f"no candidate matched attribute {attribute!r} == {equals!r} with confidence "
            f">= {min_confidence:g}; the best match was {best_confidence:.2f} (id {best_id})"
        )
    return RuntimeError(f"no candidate matched attribute {attribute!r} == {equals!r}")


def _resolution_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a resolve_candidate step: one identity from a public cue, or abstain."""
    allowed = {"class_name", "attribute", "equals", "store_as", "timeout_s", "settle_s"}
    if not {"class_name", "store_as"}.issubset(spec) or not set(spec).issubset(allowed):
        raise ValueError(
            "resolve_candidate needs class_name and store_as; attribute/equals, timeout_s and "
            "settle_s are optional"
        )
    if ("attribute" in spec) != ("equals" in spec):
        raise ValueError("resolve_candidate attribute and equals must be given together")
    class_name = spec["class_name"]
    if not isinstance(class_name, str) or not class_name.strip():
        raise ValueError("resolve_candidate class_name must be a non-empty label")
    if "attribute" in spec and (not isinstance(spec["attribute"], str) or not spec["attribute"]):
        raise ValueError("resolve_candidate attribute must be a non-empty string")

    return {
        "class_name": class_name.strip(),
        "attribute": spec.get("attribute"),
        "equals": spec.get("equals"),
        "store_as": _variable_name(spec["store_as"]),
        "timeout_s": _finite_seconds(spec, "timeout_s", 10.0, step="resolve_candidate"),
        "settle_s": _finite_seconds(spec, "settle_s", 1.0, step="resolve_candidate"),
    }


def _candidate_label(candidate: Mapping[str, Any]) -> str | None:
    """Tracker label of a candidate-table row; MCP rows carry it JSON-quoted as untrusted data."""
    if isinstance(candidate.get("class_name"), str):
        return candidate["class_name"]
    labels = candidate.get("untrusted_detector_labels")
    if isinstance(labels, list) and labels:
        try:
            label = json.loads(labels[0])
        except (TypeError, ValueError):
            return None
        return label if isinstance(label, str) else None
    return None


def _cue_matches(candidates: list[Any], resolution: Mapping[str, Any]) -> list[dict[str, Any]]:
    wanted = resolution["class_name"].casefold()
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        label = _candidate_label(candidate)
        if label is None or label.casefold() != wanted:
            continue
        if resolution["attribute"] is not None:
            attributes = candidate.get("attributes")
            if not isinstance(attributes, Mapping) or attributes.get(resolution["attribute"]) != resolution["equals"]:
                continue
        matches.append(dict(candidate))
    return matches


# Same freshness limit as PerceptionSystem.validate_candidate's default max_age_seconds.
_RESOLVE_MAX_OBSERVATION_AGE_S = 0.75


def _observation_record(response: Mapping[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    pose = response.get("pose") if isinstance(response.get("pose"), Mapping) else {}
    return {
        "frame_id": response.get("frame_id"),
        "observation_age_s": response.get("observation_age_s"),
        # Autopilot clock at frame capture, for joining the observation to simulator/flight logs.
        "frame_time_boot_ms": pose.get("time_boot_ms"),
        "frame_time_boot_age_s": pose.get("time_boot_age_s"),
        "matches": [{"id": match["id"], "bbox": match.get("bbox")} for match in matches],
    }


async def _resolve_candidate(session: Any, resolution: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a public cue to exactly one current tracker ID, or abstain.

    Only fresh (≤0.75 s), newly processed frames count. A first match must appear before
    ``timeout_s``; the step then observes ``settle_s`` more; every call is bounded by the
    remaining time, so ``timeout_s + settle_s`` caps the whole step. Two or more matching IDs in
    one fresh frame is ``ambiguous`` (never a pick); a single match must persist under the same
    ID into the decision frame and appear in at least two fresh frames, like candidate validation.
    """
    deadline = time.monotonic() + resolution["timeout_s"]
    settle_end: float | None = None
    last_frame: Any = None
    fresh_frames = 0
    sightings: dict[int, int] = {}
    ambiguous: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    matches: list[dict[str, Any]] = []
    while True:
        remaining = (settle_end if settle_end is not None else deadline) - time.monotonic()
        if remaining <= 0.0:
            break
        try:
            response = await asyncio.wait_for(call_json(session, "list_candidates"), timeout=remaining)
        except asyncio.TimeoutError:
            break
        table = response.get("candidate_table") if isinstance(response, Mapping) else None
        if not isinstance(table, list):
            raise RuntimeError("list_candidates returned an invalid response")
        age = response.get("observation_age_s")
        frame_id = response.get("frame_id")
        fresh = isinstance(age, (int, float)) and age <= _RESOLVE_MAX_OBSERVATION_AGE_S
        if fresh and frame_id != last_frame:
            last_frame = frame_id
            fresh_frames += 1
            matches = _cue_matches(table, resolution)
            decision = _observation_record(response, matches)
            for match in matches:
                sightings[int(match["id"])] = sightings.get(int(match["id"]), 0) + 1
            if len(matches) > 1 and ambiguous is None:
                ambiguous = decision
            if matches and settle_end is None and time.monotonic() < deadline:
                settle_end = time.monotonic() + resolution["settle_s"]
        remaining = (settle_end if settle_end is not None else deadline) - time.monotonic()
        if remaining <= 0.0:
            break
        await asyncio.sleep(min(0.25, remaining))
    common = {"cue": {key: resolution[key] for key in ("class_name", "attribute", "equals")}}
    if ambiguous is not None:
        return {
            "outcome": "abstain", "reason": "ambiguous",
            "candidate_ids": sorted(match["id"] for match in ambiguous["matches"]),
            "observation": ambiguous, **common,
        }
    if fresh_frames == 0:
        return {"outcome": "abstain", "reason": "no_fresh_observation", "candidate_ids": [], **common}
    if settle_end is None:
        return {"outcome": "abstain", "reason": "no_match", "candidate_ids": [], "observation": decision, **common}
    if len(matches) != 1 or sightings[int(matches[0]["id"])] < 2:
        return {
            "outcome": "abstain", "reason": "unstable",
            "candidate_ids": sorted(sightings), "observation": decision, **common,
        }
    return {"outcome": "resolved", "candidate": matches[0], "observation": decision, **common}


def _write_reacquisition_result(
    *,
    mission_path: Path,
    mission_name: str,
    run_number: int,
    started_at: datetime,
    config: Any,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(config, Mapping):
        raise ValueError("assert_reacquired must be a mapping")
    required = {"target", "follow", "output_dir", "min_time_s", "max_time_s"}
    allowed = required | {"expected_follow_status"}
    if not required.issubset(config) or set(config) - allowed:
        raise ValueError("assert_reacquired needs target, follow, output_dir, min_time_s, and max_time_s")
    target, follow = config["target"], config["follow"]
    if not isinstance(target, Mapping) or not isinstance(follow, Mapping):
        raise ValueError("assert_reacquired target and follow must resolve to mappings")
    raw_target_id = target.get("id")
    if isinstance(raw_target_id, bool) or not isinstance(raw_target_id, int) or raw_target_id < 1:
        raise ValueError("assert_reacquired target needs a positive integer id")
    try:
        min_time = float(config["min_time_s"])
        max_time = float(config["max_time_s"])
    except (TypeError, ValueError) as exc:
        raise ValueError("assert_reacquired time limits must be numeric") from exc
    if not math.isfinite(min_time) or not math.isfinite(max_time) or min_time < 0 or max_time < min_time:
        raise ValueError("assert_reacquired time limits must be finite and ordered")
    output_dir = config["output_dir"]
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("assert_reacquired output_dir must be a non-empty relative path")
    tracking = follow.get("tracking")
    if not isinstance(tracking, Mapping):
        tracking = {}
    reference_id = tracking.get("reference_id")
    matched_id = tracking.get("matched_id")
    reacquire_time = tracking.get("time_to_reacquire_s")
    similarity = tracking.get("embedding_similarity")
    similarity_threshold = tracking.get("embedding_similarity_threshold")
    try:
        reference_id = int(reference_id)
        matched_id = int(matched_id)
        reacquire_time = float(reacquire_time)
        similarity = float(similarity)
        similarity_threshold = float(similarity_threshold)
    except (TypeError, ValueError, OverflowError):
        reference_id = matched_id = None
        reacquire_time = similarity = similarity_threshold = math.nan
    id_preserved = matched_id == raw_target_id
    id_rematched = matched_id is not None and matched_id != raw_target_id
    embedding_validated = (
        tracking.get("reacquired") is True
        and tracking.get("embedding_validated") is True
        and reference_id == raw_target_id
        and similarity >= similarity_threshold
    )
    time_valid = math.isfinite(reacquire_time) and min_time <= reacquire_time <= max_time
    expected_status = config.get("expected_follow_status", "expired")
    follow_status_valid = follow.get("status") == expected_status
    id_mapping_recorded = id_preserved != id_rematched
    assertions = {
        "follow_ended_as_expected": follow_status_valid,
        "same_identity_embedding_validated": embedding_validated,
        "time_to_reacquire_within_bounds": time_valid,
        "id_mapping_recorded": id_mapping_recorded,
        "id_preserved": id_preserved,
        "id_rematched": id_rematched,
        "passed": follow_status_valid
        and embedding_validated
        and time_valid
        and id_mapping_recorded,
    }
    record = {
        "schema_version": 1,
        "mission": mission_name,
        "run": run_number,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "selected_target": dict(target),
        "follow": {
            "task_id": follow.get("task_id"),
            "status": follow.get("status"),
            "error": follow.get("error"),
            "tracking": dict(tracking),
        },
        "reacquisition": {
            "same_identity": embedding_validated,
            "time_to_reacquire_s": reacquire_time if math.isfinite(reacquire_time) else None,
            "reference_id": reference_id,
            "matched_id": matched_id,
            "id_preserved": id_preserved,
            "id_rematched": id_rematched,
            "embedding_similarity": similarity if math.isfinite(similarity) else None,
            "embedding_similarity_threshold": (
                similarity_threshold if math.isfinite(similarity_threshold) else None
            ),
        },
        "assertions": assertions,
    }
    directory = (mission_path.parent / output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    result_path = directory / f"{mission_path.stem}-run-{run_number:03d}-{timestamp}.json"
    with result_path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result_path, record


async def _stop_mission_driver(process: asyncio.subprocess.Process) -> None:
    """Stop a background mission driver, force-killing it only if it ignores SIGTERM."""
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except TimeoutError:
        process.kill()
        await process.wait()
