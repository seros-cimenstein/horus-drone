#!/usr/bin/env python3
"""Execute one outdoor campaign episode inside the running simulation container."""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import time
import traceback
from typing import Any
import urllib.request

from sim.campaign.civilians import civilian_check
from sim.campaign.episode import STOP, Episode, json_write
from sim.campaign.scenarios import SCENARIOS
from sim.campaign.ttl import ttl_scale

GROUNDER_HEALTH_URL = "http://127.0.0.1:8100/health"
GROUNDER_HEALTH_TIMEOUT_S = 1.0
SITL_CONSOLE_LOG = Path("/tmp/ArduCopter.log")


def _grounder_status(start: Mapping[str, Any] | None, end: Mapping[str, Any] | None,
                     mcp: Mapping[str, Any] | None = None,
                     open_vocab_candidates: int = 0) -> dict[str, Any]:
    """Episode grounder state from every available signal, recording which one decided it.

    The /health probes can both miss a service that is loading or already serving (the start
    probe runs before startup, and a probe outside the sim container cannot reach its loopback),
    so the MCP pipeline's own grounder status and observed ``open_vocab`` candidates count too.
    """
    start = start if isinstance(start, Mapping) else {}
    end = end if isinstance(end, Mapping) else {}
    mcp = mcp if isinstance(mcp, Mapping) else {}
    signals: dict[str, bool] = {
        "start_probe": bool(start.get("active")),
        "end_probe": bool(end.get("active")),
        "mcp_pipeline": str(mcp.get("status") or "") == "ok",
        "open_vocab_candidates": int(open_vocab_candidates) > 0,
    }
    active = any(signals.values())
    decided_by = [name for name, value in signals.items() if value] or ["no_signal"]
    return {
        "active": active,
        "model": end.get("model") or start.get("model") or mcp.get("model"),
        "detail": None if active else (end.get("detail") or start.get("detail") or mcp.get("reason")),
        "decided_by": decided_by,
        "signals": signals,
        "open_vocab_candidates": int(open_vocab_candidates),
        "start": dict(start),
        "end": dict(end),
        "mcp": dict(mcp),
    }


def probe_grounder(url: str = GROUNDER_HEALTH_URL,
                   timeout_s: float = GROUNDER_HEALTH_TIMEOUT_S) -> dict[str, Any]:
    """One health probe of the loopback grounding service; never raises when it is absent."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"active": False, "model": None, "detail": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return {"active": False, "model": None, "detail": f"unexpected health response: {payload!r}"}
    model = payload.get("model")
    return {"active": True, "model": model if isinstance(model, str) else None,
            "detail": f"health probe ok at {url}"}


def _copy_sitl_console(directory: Path) -> str | None:
    """Copy SITL's console log (PreArm/arming reasons) beside the episode result.

    ArduPilot writes arming refusals to its own stdout, which start_sim.sh redirects to
    /tmp/ArduCopter.log inside the container; without this copy an episode that never armed
    leaves no evidence of why.
    """
    try:
        if not SITL_CONSOLE_LOG.is_file():
            return None
        destination = directory / "sitl-console.log"
        shutil.copyfile(SITL_CONSOLE_LOG, destination)
    except OSError:
        return None
    return destination.name


def _signal_stop(_signum: int, _frame: Any) -> None:
    STOP.set()


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("forest", "urban"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--sdf", required=True)
    parser.add_argument("--episode-dir", required=True)
    parser.add_argument("--tests", required=True)
    parser.add_argument("--vantages", type=int, default=8)
    parser.add_argument(
        "--ttl-scale", type=ttl_scale, default=1.0,
        help="multiply command TTLs and harness waits for hosts that run the sim below real time",
    )
    args = parser.parse_args(argv)
    args.tests = tuple(name for name in args.tests.split(",") if name)
    return args


def _final_metrics(episode: Episode, result: dict[str, Any]) -> None:
    """Metrics computed once, after the episode closed; the civilian check also gates the status."""
    try:
        result["civilian_check"] = civilian_check(episode.manifest, episode.manifest_path,
                                                  episode.civilian_trace_path)
        result["rtf"] = episode._rtf_stats()
        result["readers"] = episode.reader_status()
        result["gt_coverage"] = episode.gt_coverage()
        gt_rows = episode._gt.snapshot() if episode._gt else []
        result["sim_duration_s"] = (None if len(gt_rows) < 2
                                    else gt_rows[-1].get("sim_time", 0.0) - gt_rows[0].get("sim_time", 0.0))
    except Exception as exc:
        result["final_metrics_error"] = f"{type(exc).__name__}: {exc}"
    if result.get("civilian_check", {}).get("passed") is False:
        result["status"] = "failed"


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    directory = Path(args.episode_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "episode.pid").write_text(str(os.getpid()), encoding="utf-8")
    episode = None
    result: dict[str, Any] = {"kind": args.kind, "seed": args.seed,
                              "world": Path(args.manifest).name.removesuffix(".layout.json"),
                              "status": "failed", "tests": {}}
    started_wall, started_epoch = time.monotonic(), time.time()
    try:
        result["grounder"] = probe_grounder()
        episode = Episode(args)
        tests = asyncio.run(episode.run(SCENARIOS))
        result.update({
            "tests": tests, "status": "passed" if all(test.get("passed") is True for test in tests.values()) else "failed",
            "camera_info": episode.camera_info,
            "ttl_scale": args.ttl_scale,
            "initial_camera_pose": episode.initial_camera_pose,
            "rtf_wall_started_at": datetime.fromtimestamp(started_epoch, timezone.utc).isoformat(),
        })
    except BaseException as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "failed"
        (directory / "episode-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return_code = 1
    else:
        return_code = 0
    finally:
        if episode is not None:
            episode.close()
            if not STOP.is_set():
                _final_metrics(episode, result)
        result["wall_duration_s"] = time.monotonic() - started_wall
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        # The start probe ran before the service finished loading; probe again at the end so a
        # grounder that was active for the tests is reported as active.
        result["grounder"] = _grounder_status(
            result.get("grounder"), probe_grounder(),
            episode.mcp_grounder if episode is not None else None,
            episode.open_vocab_candidates if episode is not None else 0)
        console = _copy_sitl_console(directory)
        if console is not None:
            result["sitl_console_log"] = console
        json_write(directory / "episode_result.json", result)
        (directory / "episode.pid").unlink(missing_ok=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
