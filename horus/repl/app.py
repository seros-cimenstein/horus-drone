"""Terminal control loop and deterministic YAML mission runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from horus.commander import HorusCommander
from horus.config import HorusConfig, load_config

DEFAULT_SERVER_URL = "http://127.0.0.1:8000/mcp"
EMERGENCY_TOOLS = {"hold", "rth", "land"}


async def _tool_call(session: Any, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
    result = await session.call_tool(name, dict(arguments or {}))
    text = "\n".join(
        str(block.text) for block in getattr(result, "content", []) if getattr(block, "type", None) == "text"
    )
    if getattr(result, "isError", False):
        raise RuntimeError(text or f"MCP tool {name} failed")
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return text


async def _wait_task(session: Any, task_id: str, timeout_s: float) -> dict[str, Any]:
    expires = time.monotonic() + timeout_s
    while time.monotonic() < expires:
        status = await _tool_call(session, "get_task_status", {"task_id": task_id})
        if not isinstance(status, Mapping):
            raise RuntimeError(f"unexpected task status response: {status}")
        if status.get("status") in {"succeeded", "failed", "cancelled", "expired"}:
            return dict(status)
        await asyncio.sleep(0.2)
    await _tool_call(session, "cancel", {"task_id": task_id})
    raise TimeoutError(f"mission task {task_id} did not finish within {timeout_s:g}s")


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
    driver_path: Path | None = None
    driver_name = mission.get("driver")
    if driver_name is not None:
        if not isinstance(driver_name, str) or not driver_name.strip():
            raise ValueError("mission driver must be a non-empty relative path")
        driver_path = (mission_path.parent / driver_name).resolve()
        if not driver_path.is_file():
            raise FileNotFoundError(f"mission driver does not exist: {driver_path}")
    driver_min_duration = mission.get("driver_min_duration_s")
    if driver_min_duration is not None:
        if driver_path is None:
            raise ValueError("mission driver_min_duration_s requires a driver")
        if (
            isinstance(driver_min_duration, bool)
            or not isinstance(driver_min_duration, (int, float))
            or not math.isfinite(driver_min_duration)
            or driver_min_duration <= 0
        ):
            raise ValueError("mission driver_min_duration_s must be a finite positive number")
    commander: HorusCommander | None = None
    commander_task: asyncio.Task | None = None
    driver_process: asyncio.subprocess.Process | None = None
    driver_started_at: float | None = None
    mission_name = str(mission.get("name", mission_path.stem))
    print(f"Mission: {mission_name} ({repeats} run(s))")
    # A `start_driver` step launches the driver at that point in the mission (e.g. just before
    # `follow`, so the target's route is timed from the follow); without one it starts first.
    driver_step = any(isinstance(step, Mapping) and "start_driver" in step for step in mission["steps"])
    if driver_step and driver_path is None:
        raise ValueError("mission start_driver step requires a mission driver")
    try:
        if driver_path is not None and not driver_step:
            driver_started_at = time.monotonic()
            driver_process = await asyncio.create_subprocess_exec(sys.executable, str(driver_path))
        for run_index in range(repeats):
            variables: dict[str, Any] = {}
            run_started_at = datetime.now(timezone.utc)
            print(f"Run {run_index + 1}/{repeats}")
            for number, step in enumerate(mission["steps"], 1):
                if driver_process is not None and driver_process.returncode is not None:
                    raise RuntimeError(
                        f"mission driver exited with status {driver_process.returncode}"
                    )
                if not isinstance(step, Mapping):
                    raise ValueError(f"mission step {number} must be a mapping")
                if "start_driver" in step:
                    if set(step) != {"start_driver"}:
                        raise ValueError(f"mission start_driver step {number} is malformed")
                    if driver_process is None:
                        driver_started_at = time.monotonic()
                        driver_process = await asyncio.create_subprocess_exec(
                            sys.executable, str(driver_path)
                        )
                    print(f"  {number}. start_driver: {driver_path.name}")
                    continue
                if "select_candidate" in step:
                    if set(step) != {"select_candidate"} or not isinstance(
                        step["select_candidate"], Mapping
                    ):
                        raise ValueError(f"mission select_candidate step {number} is malformed")
                    selection = step["select_candidate"]
                    if set(selection) != {"attribute", "equals", "choose", "store_as"}:
                        raise ValueError(
                            f"mission select_candidate step {number} needs attribute, equals, choose, and store_as"
                        )
                    store_as = _variable_name(selection["store_as"])
                    response = await _tool_call(session, "list_candidates")
                    candidates = (
                        response.get("candidate_table")
                        if isinstance(response, Mapping)
                        else None
                    )
                    if not isinstance(candidates, list):
                        raise RuntimeError("list_candidates returned an invalid response")
                    selected = _select_candidate(
                        candidates,
                        attribute=selection["attribute"],
                        equals=selection["equals"],
                        choose=selection["choose"],
                    )
                    variables[store_as] = selected
                    print(f"  {number}. select_candidate: {json.dumps(selected, ensure_ascii=False)}")
                    continue
                if "assert_reacquired" in step:
                    if set(step) != {"assert_reacquired"} or not isinstance(
                        step["assert_reacquired"], Mapping
                    ):
                        raise ValueError(f"mission assert_reacquired step {number} is malformed")
                    config = _resolve_mission_value(step["assert_reacquired"], variables)
                    result_path, result = _write_reacquisition_result(
                        mission_path=mission_path,
                        mission_name=mission_name,
                        run_number=run_index + 1,
                        started_at=run_started_at,
                        config=config,
                    )
                    print(f"  {number}. assert_reacquired: {result_path}")
                    if not result["assertions"]["passed"]:
                        raise RuntimeError(
                            f"mission reacquisition assertion failed; result written to {result_path}"
                        )
                    continue
                if "text" in step:
                    text = step["text"]
                    if set(step) != {"text"} or not isinstance(text, str) or not text.strip():
                        raise ValueError(f"mission text step {number} must contain only non-empty text")
                    if commander is None:
                        if not server_url or not bearer_token or commander_settings is None:
                            raise ValueError("commander mission steps need server credentials and settings")
                        commander = HorusCommander(server_url, bearer_token, commander_settings)
                        commander_task = asyncio.create_task(commander.run())
                    response = await commander.submit_task(text)
                    print(f"  {number}. commander: {response}")
                    continue
                name = str(step.get("tool", ""))
                args = _resolve_mission_value(step.get("arguments", {}), variables)
                if not name or not isinstance(args, Mapping):
                    raise ValueError(f"mission step {number} needs tool and arguments mapping")
                if name not in {"take_off", "goto", "approach", "follow", "orbit", "scan", *EMERGENCY_TOOLS}:
                    raise ValueError(f"mission step {number} uses unsupported skill {name!r}")
                result = await _tool_call(session, name, args)
                print(f"  {number}. {name}: {json.dumps(result, ensure_ascii=False)}")
                task_id = result.get("task_id") if isinstance(result, Mapping) else None
                stored_result = result
                if task_id and step.get("wait", True):
                    timeout = float(step.get("timeout_s", args.get("ttl_s", 60.0)))
                    terminal = await _wait_task(session, str(task_id), timeout)
                    print(f"     result: {json.dumps(terminal, ensure_ascii=False)}")
                    expected_status = step.get("expect_status", "succeeded")
                    if terminal.get("status") != expected_status and not step.get(
                        "continue_on_failure", False
                    ):
                        raise RuntimeError(
                            f"mission step {number} ended {terminal.get('status')}: {terminal.get('error')}"
                        )
                    stored_result = terminal
                store_as = step.get("store_as")
                if store_as is not None:
                    variables[_variable_name(store_as)] = stored_result
                if not task_id and step.get("delay_s"):
                    await asyncio.sleep(float(step["delay_s"]))
        if driver_process is not None and driver_process.returncode is not None:
            raise RuntimeError(f"mission driver exited with status {driver_process.returncode}")
        if driver_min_duration is not None and driver_started_at is not None:
            remaining = driver_min_duration - (time.monotonic() - driver_started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        if driver_process is not None and driver_process.returncode is not None:
            raise RuntimeError(f"mission driver exited with status {driver_process.returncode}")
    finally:
        try:
            if commander is not None:
                await commander.stop()
            if commander_task is not None:
                active_error = sys.exc_info()[0] is not None
                result = await asyncio.gather(commander_task, return_exceptions=True)
                if isinstance(result[0], BaseException) and not active_error:
                    raise result[0]
        finally:
            if driver_process is not None:
                await _stop_mission_driver(driver_process)


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


def _select_candidate(
    candidates: list[Any], *, attribute: Any, equals: Any, choose: Any
) -> dict[str, Any]:
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
        bbox = candidate.get("bbox", candidate.get("box"))
        if isinstance(bbox, Mapping):
            coordinates = (
                bbox.get("x1", bbox.get("left")),
                bbox.get("y1", bbox.get("top")),
                bbox.get("x2", bbox.get("right")),
                bbox.get("y2", bbox.get("bottom")),
            )
        elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            coordinates = bbox
        else:
            raise ValueError(f"candidate {raw_id} has no valid bounding box")
        try:
            x1, y1, x2, y2 = map(float, coordinates)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candidate {raw_id} has an invalid bounding box") from exc
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
            raise ValueError(f"candidate {raw_id} has an invalid bounding box")
        matches.append(((x2 - x1) * (y2 - y1), raw_id, dict(candidate)))
    if not matches:
        raise RuntimeError(f"no candidate matched attribute {attribute!r} == {equals!r}")
    return max(matches, key=lambda item: (item[0], -item[1]))[2]


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


def _interactive_arguments(line: str) -> tuple[str, dict[str, Any]] | None:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not parts:
        return None
    command = parts[0].lower()
    if command in ("/quit", "/exit"):
        return "__quit__", {}
    if command == "/state":
        return "get_state", {}
    if command in EMERGENCY_TOOLS:
        return command, {}
    if command == "/goto":
        if len(parts) != 4:
            raise ValueError("usage: /goto NORTH_M EAST_M ALTITUDE_M")
        return "goto", {
            "pose": {
                "north_m": float(parts[1]),
                "east_m": float(parts[2]),
                "up_m": float(parts[3]),
            },
        }
    if command == "/take_off":
        if len(parts) > 2:
            raise ValueError("usage: /take_off [ALTITUDE_M]")
        return "take_off", {"altitude_m": float(parts[1]) if len(parts) == 2 else 2.0, "ttl_s": 30.0}
    if command in ("/approach", "/follow", "/orbit"):
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f"usage: {command} TRACKER_ID [DISTANCE_M]")
        key = "radius_m" if command == "/orbit" else "dist_m"
        args = {"id": int(parts[1]), "ttl_s": 30.0}
        if len(parts) == 3:
            args[key] = float(parts[2])
        return command[1:], args
    return None


async def interactive(server_url: str, token: str, config: HorusConfig) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    commander: HorusCommander | None = None
    commander_task: asyncio.Task | None = None
    print("HORUS REPL. /state /goto N E ALT /hold /rth /land /take_off [ALT]; free text goes to commander.")
    try:
        async with streamablehttp_client(server_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                while True:
                    try:
                        line = await asyncio.to_thread(input, "horus> ")
                    except EOFError:
                        break
                    if not line.strip():
                        continue
                    try:
                        parsed = _interactive_arguments(line)
                        if parsed is None:
                            if commander is None:
                                commander = HorusCommander(server_url, token, config.commander)
                                commander_task = asyncio.create_task(commander.run())
                            response = await commander.submit_task(line)
                            print(response)
                        elif parsed[0] == "__quit__":
                            break
                        else:
                            result = await _tool_call(session, parsed[0], parsed[1])
                            print(json.dumps(result, ensure_ascii=False, indent=2))
                    except Exception as exc:
                        print(f"error: {exc}", file=sys.stderr)
    finally:
        if commander is not None:
            await commander.stop()
        if commander_task is not None:
            try:
                await commander_task
            except Exception:
                pass


async def _run(args: argparse.Namespace, config: HorusConfig) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    server_url = args.server_url or os.environ.get("HORUS_MCP_URL", DEFAULT_SERVER_URL)
    token = os.environ.get(config.server.auth_token_env)
    if not token:
        raise RuntimeError(f"{config.server.auth_token_env} must be set")
    headers = {"Authorization": f"Bearer {token}"}
    if args.mission:
        async with streamablehttp_client(server_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await run_mission(
                    session,
                    args.mission,
                    args.repeat,
                    server_url=server_url,
                    bearer_token=token,
                    commander_settings=config.commander,
                )
    else:
        await interactive(server_url, token, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="HORUS terminal REPL and skill/commander mission runner")
    parser.add_argument("--server-url", help=f"MCP streamable HTTP URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--config", help="HORUS YAML config file")
    parser.add_argument("--mission", help="run a missions/*.yaml file and exit")
    parser.add_argument("--repeat", type=int, default=1, help="repeat mission N times")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args, load_config(args.config)))
    except (RuntimeError, ValueError, OSError, TimeoutError) as exc:
        parser.exit(2, f"horus: {exc}\n")


if __name__ == "__main__":
    main()
