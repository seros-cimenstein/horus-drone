#!/usr/bin/env python3
"""Run deterministic S7 latency trials against the authenticated HORUS MCP API.

No LLM is used: each source frame selects the largest candidate classified with a
blue shirt. The client either follows that frame's ID or points its old-frame box
and follows the returned current ID. In-process D waits for SITL `SYSTEM_TIME.time_boot_ms`
to advance by D simulation seconds. Each JSONL record includes measured simulation and
wall delays, trial RTF, sim-time-to-target, identity success, and raw evidence. With
netem, D is wall-clock time; measured RTF estimates its equivalent simulation delay.
Each trial is appended as one JSON object to a uniquely named ``runs/s7/*.jsonl``
file. Success requires the selected blue-shirt target to be horizontally centered
(follow steers heading) in two successive fresh MCP frames; task completion or expiry
alone is never success.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any
import uuid

from horus.candidates import candidate_bbox
from horus.events import TERMINAL_TASK_STATUSES
from horus.mcp_client import DEFAULT_MCP_URL, call_json, open_session


DELAY_POINTS = (0.0, 0.5, 1.0, 2.0, 3.0)
# Success needs the target centered in this many successive fresh frames.
CENTERED_OBSERVATIONS = 2
# Calls one MCP tool and returns its decoded JSON result.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
AsyncSleep = Callable[[float], Awaitable[None]]


def _candidate_id(candidate: Mapping[str, Any]) -> int | None:
    value = candidate.get("id", candidate.get("mark"))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _shirt_colour(candidate: Mapping[str, Any]) -> str | None:
    attributes = candidate.get("attributes")
    if isinstance(attributes, Mapping):
        for key in ("shirt_colour", "shirt_color"):
            value = attributes.get(key)
            if value is not None:
                return str(value).strip().casefold()
    for key in ("shirt_colour", "shirt_color"):
        value = candidate.get(key)
        if value is not None:
            return str(value).strip().casefold()
    return None


def select_largest_blue_candidate(
    candidates: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Select by largest source-pixel box; break equal-area ties by lowest tracker ID."""
    ranked: list[tuple[float, int, int, Mapping[str, Any]]] = []
    for order, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping) or _shirt_colour(candidate) != "blue":
            continue
        candidate_id = _candidate_id(candidate)
        box = candidate_bbox(candidate)
        if candidate_id is None or box is None:
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        ranked.append((-area, candidate_id, order, candidate))
    return min(ranked, default=(0.0, 0, 0, None))[3]


def _frame_candidates(frame: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = frame.get("candidates")
    if isinstance(candidates, list):
        return [row for row in candidates if isinstance(row, Mapping)]
    table = frame.get("candidate_table")
    if isinstance(table, list):
        return [row for row in table if isinstance(row, Mapping)]
    return []


def _tracking_record(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Keep frame/candidate tracking data but omit repeated JPEG payloads."""
    record = dict(frame)
    for key in ("image_base64", "jpeg_base64", "image_jpeg"):
        record.pop(key, None)
    return record



def _candidate_centered(
    frame: Mapping[str, Any], candidate_id: int, tolerance: float
) -> bool:
    width, height = frame.get("width"), frame.get("height")
    try:
        width, height = float(width), float(height)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        return False
    rows = frame.get("candidate_table")
    candidates = rows if isinstance(rows, list) else _frame_candidates(frame)
    candidate = next(
        (row for row in candidates if isinstance(row, Mapping) and _candidate_id(row) == candidate_id),
        None,
    )
    box = candidate_bbox(candidate) if candidate is not None else None
    if box is None:
        return False
    # Horizontal only: follow steers heading, not camera pitch or altitude, and a standing person
    # seen by a level camera at 2 m sits below the image centre however well the drone tracks them.
    center_x = (box[0] + box[2]) / 2
    return abs(center_x - width / 2) <= tolerance * width


def _same_target_identity(
    reference: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Compare stable appearance/profile evidence while allowing a changed tracker ID."""
    reference_colour = _shirt_colour(reference)
    candidate_colour = _shirt_colour(candidate)
    if reference_colour is None or candidate_colour != reference_colour:
        return False
    if reference_colour != "blue":
        return False
    for key in ("class_id", "recognized_class"):
        expected, actual = reference.get(key), candidate.get(key)
        if expected is not None and actual is not None and actual != expected:
            return False
    try:
        reference_profile = float(reference.get("target_match_confidence"))
    except (TypeError, ValueError, OverflowError):
        reference_profile = 0.0
    if reference_profile > 0:
        try:
            candidate_profile = float(candidate.get("target_match_confidence"))
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(candidate_profile) or candidate_profile <= 0:
            return False
    return True


def _current_identity_candidate(
    frame: Mapping[str, Any],
    reference: Mapping[str, Any],
    followed_id: int,
) -> Mapping[str, Any] | None:
    candidates = _frame_candidates(frame)
    same_id = next((row for row in candidates if _candidate_id(row) == followed_id), None)
    if same_id is not None:
        return same_id if _same_target_identity(reference, same_id) else None
    matches = [row for row in candidates if _same_target_identity(reference, row)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and reference.get("target_match_confidence") is not None:
        scored: list[tuple[float, Mapping[str, Any]]] = []
        for row in matches:
            try:
                score = float(row.get("target_match_confidence"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(score):
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], _candidate_id(item[1]) or 0))
        if scored and scored[0][0] > 0 and (
            len(scored) == 1 or scored[0][0] > scored[1][0]
        ):
            return scored[0][1]
    return None


def _task_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    task_id = value.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


def _task_status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status", value.get("state"))
        return str(status).casefold() if status is not None else None
    return None


def _terminal_status(status: str | None) -> bool:
    return status in TERMINAL_TASK_STATUSES


def _int_field(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _time_boot_ms(state: Any) -> int:
    vehicle = state.get("vehicle") if isinstance(state, Mapping) else None
    raw = vehicle.get("time_boot_ms") if isinstance(vehicle, Mapping) else None
    if isinstance(raw, bool):
        raise RuntimeError("get_state response has no integer vehicle.time_boot_ms")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("get_state response has no integer vehicle.time_boot_ms") from exc
    if value != raw or not 0 <= value <= 0xFFFFFFFF:
        raise RuntimeError("get_state response has no unsigned vehicle.time_boot_ms")
    return value


def _sim_clock_delta_s(start_ms: int, end_ms: int) -> float | None:
    delta_ms = end_ms - start_ms
    if delta_ms < 0:
        if start_ms >= 0xF0000000 and end_ms <= 0x0FFFFFFF:
            delta_ms = (end_ms - start_ms) & 0xFFFFFFFF
        else:
            return None
    return delta_ms / 1000.0

async def run_trial(
    call: ToolCall,
    *,
    delay_s: float,
    mode: str,
    compensate: bool | None,
    wait: AsyncSleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.25,
    center_tolerance: float = 0.10,
    delay_source: str = "in_process_sleep",
) -> dict[str, Any]:
    """Run one trial using SITL time for in-process delay and target latency.

    Netem delay is wall-clock; ``measured_rtf`` converts its configured wall delay
    into an estimated equivalent simulation delay for that trial.
    """
    compensation_source = (
        "server_default" if mode == "box" and compensate is None
        else "per_call_override" if mode == "box"
        else "not_applicable"
    )
    delay_clock = "sitl_sim_time" if delay_source == "in_process_sleep" else "wall_time"
    result: dict[str, Any] = {
        "delay_s": float(delay_s),
        "delay_clock": delay_clock,
        "delay_source": delay_source,
        "sim_delay_s": None,
        "wall_delay_s": None,
        "wall_delay_basis": None,
        "sim_delay_estimated": False,
        "sim_clock_source": "MAVLink SYSTEM_TIME.time_boot_ms",
        "sim_time_boot_ms_start": None,
        "sim_time_boot_ms_end": None,
        "trial_sim_elapsed_s": None,
        "trial_wall_elapsed_s": None,
        "measured_rtf": None,
        "mode": mode,
        "compensation": compensate if mode == "box" else None,
        "compensation_source": compensation_source,
        "condition": {
            "delay_s": float(delay_s),
            "delay_clock": delay_clock,
            "action_mode": mode,
            "box_compensation": compensate if mode == "box" else None,
            "compensation_source": compensation_source,
        },
        "selected_reference": None,
        "result_status": "error",
        "success": False,
        "time_to_target_s": None,
        "wall_time_to_target_s": None,
        "wrong_target": False,
        "raw_tracking_data": [],
        "raw_matching_data": None,
        "raw_action_data": None,
        "raw_task_status_data": [],
        "raw_cleanup_data": None,
    }

    async def read_sim_time_ms() -> int:
        return _time_boot_ms(await call("get_state", {}))

    async def wait_for_sim_delay() -> tuple[int, int, float, float]:
        start_ms = await read_sim_time_ms()
        wall_started = clock()
        if delay_s <= 0.0:
            return start_ms, start_ms, 0.0, 0.0
        while True:
            current_ms = await read_sim_time_ms()
            sim_elapsed = _sim_clock_delta_s(start_ms, current_ms)
            if sim_elapsed is None:
                raise RuntimeError("SITL SYSTEM_TIME clock reset during delay injection")
            if sim_elapsed >= delay_s:
                return start_ms, current_ms, sim_elapsed, max(0.0, clock() - wall_started)
            wall_elapsed = clock() - wall_started
            if wall_elapsed >= timeout_s:
                raise TimeoutError(
                    f"SITL simulation clock did not advance by {delay_s:g}s within {timeout_s:g}s"
                )
            poll_s = max(0.01, poll_interval_s)
            await wait(min(poll_s, timeout_s - wall_elapsed))

    task_id: str | None = None
    last_task_status: str | None = None
    trial_wall_started = clock()
    trial_start_ms: int | None = None
    target_start_ms: int | None = None
    target_start_wall: float | None = None

    try:
        trial_start_ms = await read_sim_time_ms()
        result["sim_time_boot_ms_start"] = trial_start_ms
        source = await call("get_frame", {})
        if not isinstance(source, Mapping):
            raise RuntimeError("get_frame did not return a JSON object")
        result["raw_tracking_data"].append(
            {"phase": "reference", **_tracking_record(source)}
        )
        reference_frame_id = _int_field(source.get("frame_id"))
        selected = select_largest_blue_candidate(_frame_candidates(source))
        if reference_frame_id is None:
            raise RuntimeError("get_frame response has no integer frame_id")
        if selected is None:
            result["result_status"] = "no_blue_candidate"
            return result
        selected_id = _candidate_id(selected)
        selected_box = candidate_bbox(selected)
        if selected_id is None or selected_box is None:
            result["result_status"] = "invalid_blue_candidate"
            return result
        result["selected_reference"] = {
            "frame_id": reference_frame_id,
            "id": selected_id,
            "bbox": list(selected_box),
            "shirt_colour": _shirt_colour(selected),
            "class_id": selected.get("class_id"),
            "recognized_class": selected.get("recognized_class"),
            "target_match_confidence": selected.get("target_match_confidence"),
            "candidate": dict(selected),
        }

        if delay_source == "in_process_sleep":
            delay_start_ms, delay_end_ms, sim_delay, wall_delay = await wait_for_sim_delay()
            result["sim_time_boot_ms_delay_start"] = delay_start_ms
            result["sim_time_boot_ms_delay_end"] = delay_end_ms
            result["sim_delay_s"] = sim_delay
            result["wall_delay_s"] = wall_delay
            result["wall_delay_basis"] = "measured_clock_wait"
        elif delay_source == "netem":
            result["wall_delay_s"] = float(delay_s)
            result["wall_delay_basis"] = "configured_tc_netem_delay"
            result["sim_delay_estimated"] = True
        else:
            raise ValueError(f"unsupported delay source {delay_source!r}")

        action_id = selected_id
        if mode == "box":
            point_arguments: dict[str, Any] = {
                "frame_id": reference_frame_id,
                "box": list(selected_box),
            }
            if compensate is not None:
                point_arguments["compensate"] = compensate
            point_result = await call("point", point_arguments)
            result["raw_matching_data"] = point_result
            if not isinstance(point_result, Mapping):
                result["result_status"] = "point_missing_candidate"
                return result
            matched_candidate = point_result.get("candidate")
            action_id = (
                _candidate_id(matched_candidate)
                if isinstance(matched_candidate, Mapping)
                else _int_field(point_result.get("id"))
            )
            if action_id is None:
                result["result_status"] = "point_missing_candidate"
                return result
            if (
                isinstance(matched_candidate, Mapping)
                and not _same_target_identity(result["selected_reference"], matched_candidate)
            ):
                result["wrong_target"] = True
                result["result_status"] = "wrong_target_at_point"
                return result

        target_start_ms = await read_sim_time_ms()
        target_start_wall = clock()
        follow_arguments = {"id": action_id, "ttl_s": timeout_s}
        action_result = await call("follow", follow_arguments)
        result["raw_action_data"] = action_result
        task_id = _task_id(action_result)
        previous_frame_id = reference_frame_id
        previous_timestamp: float | None = None
        raw_timestamp = source.get("timestamp", source.get("observed_at"))
        try:
            previous_timestamp = float(raw_timestamp) if raw_timestamp is not None else None
        except (TypeError, ValueError, OverflowError):
            pass
        centered_streak = 0

        while True:
            observation = await call("get_frame", {})
            if not isinstance(observation, Mapping):
                raise RuntimeError("get_frame did not return a JSON object while tracking")
            frame_id = _int_field(observation.get("frame_id"))
            timestamp_value = observation.get("timestamp", observation.get("observed_at"))
            try:
                timestamp = float(timestamp_value) if timestamp_value is not None else None
            except (TypeError, ValueError, OverflowError):
                timestamp = None
            fresh = frame_id is not None and frame_id > previous_frame_id
            if fresh and previous_timestamp is not None and timestamp is not None:
                fresh = timestamp > previous_timestamp
            tracking_row = _tracking_record(observation)
            tracking_row["fresh_after_previous"] = fresh
            result["raw_tracking_data"].append({"phase": "tracking", **tracking_row})
            if fresh and frame_id is not None:
                previous_frame_id = frame_id
                if timestamp is not None:
                    previous_timestamp = timestamp
                target_candidate = _current_identity_candidate(
                    observation, result["selected_reference"], action_id
                )
                target_id = (
                    _candidate_id(target_candidate)
                    if target_candidate is not None
                    else None
                )
                centered = (
                    target_id is not None
                    and _candidate_centered(observation, target_id, center_tolerance)
                )
                if centered:
                    centered_streak += 1
                    if centered_streak >= CENTERED_OBSERVATIONS:
                        target_end_ms = await read_sim_time_ms()
                        target_elapsed = _sim_clock_delta_s(target_start_ms, target_end_ms)
                        if target_elapsed is None:
                            raise RuntimeError("SITL SYSTEM_TIME clock reset during target tracking")
                        result["result_status"] = "target_centered"
                        result["success"] = True
                        result["time_to_target_s"] = target_elapsed
                        result["wall_time_to_target_s"] = max(
                            0.0, clock() - target_start_wall
                        )
                        break
                else:
                    centered_streak = 0
                    if any(
                        candidate_id != target_id
                        and _candidate_centered(
                            observation, candidate_id, center_tolerance
                        )
                        for candidate in _frame_candidates(observation)
                        if (candidate_id := _candidate_id(candidate)) is not None
                    ):
                        result["wrong_target"] = True

            if task_id is not None:
                task_status_data = await call(
                    "get_task_status", {"task_id": task_id}
                )
                result["raw_task_status_data"].append(task_status_data)
                last_task_status = _task_status(task_status_data)
            if _terminal_status(last_task_status):
                result["result_status"] = (
                    "task_succeeded_without_target"
                    if last_task_status == "succeeded"
                    else f"task_{last_task_status}"
                )
                break
            elapsed = clock() - target_start_wall
            if elapsed >= timeout_s:
                result["result_status"] = "timed_out"
                break
            await wait(min(poll_interval_s, max(0.0, timeout_s - elapsed)))
    except Exception as exc:
        result["result_status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if task_id is not None and not _terminal_status(last_task_status):
            try:
                result["raw_cleanup_data"] = await call(
                    "cancel", {"task_id": task_id}
                )
            except Exception as cleanup_exc:
                result["cleanup_error"] = (
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        result["trial_wall_elapsed_s"] = max(0.0, clock() - trial_wall_started)
        if trial_start_ms is not None:
            try:
                trial_end_ms = await read_sim_time_ms()
                result["sim_time_boot_ms_end"] = trial_end_ms
                trial_sim_elapsed = _sim_clock_delta_s(trial_start_ms, trial_end_ms)
                if trial_sim_elapsed is None:
                    raise RuntimeError("SITL SYSTEM_TIME clock reset during trial")
                result["trial_sim_elapsed_s"] = trial_sim_elapsed
                wall_elapsed = result["trial_wall_elapsed_s"]
                if wall_elapsed > 0.0:
                    result["measured_rtf"] = trial_sim_elapsed / wall_elapsed
                if delay_source == "netem" and result["measured_rtf"] is not None:
                    result["sim_delay_s"] = float(delay_s) * result["measured_rtf"]
            except Exception as clock_exc:
                result["clock_measurement_error"] = (
                    f"{type(clock_exc).__name__}: {clock_exc}"
                )
    return result


def _parse_delays(value: str) -> tuple[float, ...]:
    try:
        delays = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delays must be comma-separated seconds") from exc
    if not delays or any(not math.isfinite(delay) or delay not in DELAY_POINTS for delay in delays):
        raise argparse.ArgumentTypeError("delays must be selected from 0,0.5,1,2,3 seconds")
    if len(set(delays)) != len(delays):
        raise argparse.ArgumentTypeError("delays must not contain duplicates")
    return delays


def _parse_modes(value: str) -> tuple[str, ...]:
    modes = tuple(part.strip().casefold() for part in value.split(","))
    if not modes or any(mode not in ("id", "box") for mode in modes):
        raise argparse.ArgumentTypeError("modes must be comma-separated id and/or box")
    if len(set(modes)) != len(modes):
        raise argparse.ArgumentTypeError("modes must not contain duplicates")
    return modes


def _create_output_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    while True:
        path = directory / f"s7-latency-{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644)
        except FileExistsError:
            continue
        os.close(descriptor)
        return path


def _netem(delay_s: float, interface: str, action: str) -> None:
    script = Path(__file__).with_name("netem.sh")
    if action == "up":
        delay = f"{delay_s:g}s"
        subprocess.run(
            ["sudo", "bash", str(script), "up", interface, delay, "0%"],
            check=True,
        )
    else:
        subprocess.run(
            ["sudo", "bash", str(script), "down", interface],
            check=True,
        )


async def run_client(args: argparse.Namespace) -> Path:
    server_url = args.server_url or os.environ.get("HORUS_MCP_URL", DEFAULT_MCP_URL)
    token = args.token or os.environ.get("HORUS_MCP_TOKEN")
    if not token:
        raise RuntimeError("set --token or HORUS_MCP_TOKEN for MCP bearer authentication")
    output_dir = args.output_dir or Path(__file__).resolve().parents[1] / "runs" / "s7"
    output_path = _create_output_file(output_dir)
    async with open_session(server_url, token) as session:

        async def call(tool: str, arguments: dict[str, Any]) -> Any:
            return await call_json(session, tool, arguments)

        with output_path.open("a", encoding="utf-8") as records:
            trial_number = 0
            for delay_s in args.delays:
                external_netem = args.netem_interface is not None
                netem_attempted = False
                try:
                    if external_netem:
                        netem_attempted = True
                        _netem(delay_s, args.netem_interface, "up")
                    for mode in args.modes:
                        if mode == "id":
                            compensation_values: tuple[bool | None, ...] = (None,)
                        elif args.compensation == "both":
                            compensation_values = (True, False)
                        elif args.compensation == "on":
                            compensation_values = (True,)
                        elif args.compensation == "off":
                            compensation_values = (False,)
                        else:
                            compensation_values = (None,)
                        for compensate in compensation_values:
                            for repetition in range(args.n):
                                trial_number += 1
                                trial = await run_trial(
                                    call,
                                    delay_s=delay_s,
                                    mode=mode,
                                    compensate=compensate,
                                    wait=asyncio.sleep,
                                    timeout_s=args.timeout,
                                    poll_interval_s=args.poll_interval,
                                    center_tolerance=args.center_tolerance,
                                    delay_source="netem" if external_netem else "in_process_sleep",
                                )
                                trial["trial_number"] = trial_number
                                trial["repetition"] = repetition + 1
                                records.write(json.dumps(
                                    trial, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                                ) + "\n")
                                records.flush()
                                print(
                                    f"trial {trial_number}: D={delay_s:g}s mode={mode} "
                                    f"compensation={trial['compensation']} "
                                    f"status={trial['result_status']} success={trial['success']}"
                                )
                finally:
                    if netem_attempted:
                        _netem(delay_s, args.netem_interface, "down")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic S7 experiment client (no LLM): largest blue-shirt candidate, "
            "SITL-time delay, then ID or compensated box pointing."
        ),
        epilog=(
            "Default sweep: D=0,0.5,1,2,3 seconds; N=20; ID plus box compensation on/off. "
            "Without --netem-interface, D waits for MAVLink SYSTEM_TIME.time_boot_ms to "
            "advance by D simulation seconds. --netem-interface shapes the separate "
            "commander↔MCP veth, never MAVLink; its D is wall-clock, and each trial "
            "records measured RTF for conversion to simulation time."
        ),
    )
    parser.add_argument("--server-url", help="MCP streamable-HTTP URL (default HORUS_MCP_URL or localhost)")
    parser.add_argument("--token", help="bearer token (default HORUS_MCP_TOKEN)")
    parser.add_argument(
        "-n",
        "--n",
        "--trials",
        type=int,
        default=20,
        help="N repetitions per delay × mode × compensation condition (default: 20)",
    )
    parser.add_argument(
        "--delays",
        type=_parse_delays,
        default=DELAY_POINTS,
        help="selected D values in seconds (default: 0,0.5,1,2,3)",
    )
    parser.add_argument(
        "--modes",
        type=_parse_modes,
        default=("id", "box"),
        help="comma-separated id and/or box modes (default: id,box)",
    )
    parser.add_argument(
        "--compensation",
        choices=("both", "on", "off", "default"),
        default="both",
        help="box mode: both (default), on, off, or default (use server config)",
    )
    parser.add_argument(
        "--netem-interface",
        help="use sudo scripts/netem.sh on the client veth (configured D is wall-clock)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="follow task TTL and observation timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=0.25, help="delay between fresh-frame polls in seconds")
    parser.add_argument("--center-tolerance", type=float, default=0.10, help="per-axis image-center tolerance as a fraction")
    parser.add_argument("--output-dir", type=Path, help="JSONL directory (default: repository runs/s7)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n must be at least 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    if not math.isfinite(args.poll_interval) or args.poll_interval < 0:
        parser.error("--poll-interval must be a non-negative finite number")
    if not math.isfinite(args.center_tolerance) or not 0 < args.center_tolerance <= 0.5:
        parser.error("--center-tolerance must be in (0, 0.5]")
 
    try:
        output_path = asyncio.run(run_client(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    print(f"S7 JSONL written to {output_path}")


if __name__ == "__main__":
    main()
