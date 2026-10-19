#!/usr/bin/env python3
"""Run reproducible HORUS outdoor campaigns through the real SITL/MCP stack.

This host-side entry point intentionally uses only the Python standard library.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim.campaign.summary import aggregate_campaign, render_summary_markdown  # noqa: E402
from sim.campaign.ttl import ttl_scale  # noqa: E402

RUNNER = ROOT / "sim" / "run.sh"
DEFAULT_TESTS = ("perception", "select", "approach", "orbit", "scan", "follow", "rth", "hazard")
VALID_TESTS = frozenset(DEFAULT_TESTS)
SEED_SPACE_MAX = 99999


def random_seeds(count: int, *, low: int = 1, high: int = SEED_SPACE_MAX + 1) -> list[int]:
    """Distinct seeds for a randomized campaign; callers print them for reproduction."""
    if count < 1 or count > high - low:
        raise ValueError(f"random seed count must be between 1 and {high - low}")
    return secrets.SystemRandom().sample(range(low, high), count)


def _tail_copy(source: Path, destination: Path, lines: int = 300) -> None:
    if not source.is_file():
        return
    with source.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        block = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            take = min(block, size)
            size -= take
            stream.seek(size)
            data = stream.read(take) + data
    destination.write_bytes(b"\n".join(data.splitlines()[-lines:]) + b"\n")


def _run(command: list[str], *, cwd: Path = ROOT, timeout: float = 300.0,
         check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture, timeout=timeout)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}: {detail}")
    return result


def _run_before_deadline(command: list[str], deadline: float, *, cap: float = 300.0) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("episode wall timeout before command: " + " ".join(command))
    return _run(command, timeout=min(cap, remaining))


def _container() -> str:
    return os.environ.get("HORUS_SIM_CONTAINER_NAME", "horus-sim")


def container_state(container: str) -> dict[str, Any]:
    """Best-effort docker state of the simulation container after an episode died.

    ``OOMKilled`` is the only place docker records why a killed process never wrote a result,
    so the campaign keeps it in the episode record instead of losing it to ``stop --purge``.
    """
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            capture_output=True, text=True, timeout=20.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {"status": payload.get("Status"), "exit_code": payload.get("ExitCode"),
            "oom_killed": bool(payload.get("OOMKilled")), "error": str(payload.get("Error") or "")}


def _missing_result_failure(code: int | None, state: Mapping[str, Any] | None) -> str:
    """Explicit failure text for an episode that exited without writing episode_result.json."""
    detail = f"episode wrote no result file; exit code {code}"
    if isinstance(state, Mapping) and state:
        status = state.get("status")
        if state.get("oom_killed"):
            detail += f"; container {status or 'unknown'} OOM-killed (exit {state.get('exit_code')})"
        elif status and status != "running":
            detail += f"; container {status} (exit {state.get('exit_code')})"
    return f"{detail}; see episode.log"


def _episode_record(kind: str, seed: int | None, world: str, episode_dir: Path) -> dict[str, Any]:
    return {"kind": kind, "seed": seed, "world": world, "episode_dir": str(episode_dir),
            "started_at": datetime.now(timezone.utc).isoformat(), "tests": {}, "status": "failed"}


def _generate_world(args: argparse.Namespace, kind: str, seed: int | None, world: str,
                    deadline: float) -> tuple[Path, Path]:
    """Generate (or regenerate) the episode's world and return its manifest and SDF."""
    if args.canonical:
        _run_before_deadline([sys.executable, "-m", "sim.worldgen", "--seed", "2026", "--kind", kind], deadline)
        manifest = ROOT / "sim" / "worlds" / f"{world}.layout.json"
        sdf = ROOT / "sim" / "worlds" / f"{world}.sdf"
    else:
        _run_before_deadline([sys.executable, "-m", "sim.worldgen", "--procedural", "--seed", str(seed),
                              "--kind", kind, "--out", "sim/generated"], deadline)
        generated = ROOT / "sim" / "generated" / world
        manifest = generated / f"{world}.layout.json"
        sdf = generated / f"{world}.sdf"
    if not manifest.is_file() or not sdf.is_file():
        raise FileNotFoundError(f"world generator did not create {manifest} and {sdf}")
    return manifest, sdf


def _episode_command(args: argparse.Namespace, kind: str, seed: int | None, manifest: Path, sdf: Path,
                     episode_dir: Path, token: str) -> list[str]:
    return ["docker", "exec", "--workdir", "/horus", "--env", f"HORUS_MCP_TOKEN={token}", _container(),
            "/opt/horus-venv/bin/python", "/horus/sim/scripts/campaign_episode.py",
            "--kind", kind, "--seed", str(seed if seed is not None else (2027 if kind == "forest" else 2028)),
            "--manifest", "/horus/" + str(manifest.relative_to(ROOT)), "--sdf", "/horus/" + str(sdf.relative_to(ROOT)),
            "--episode-dir", "/horus/" + str(episode_dir.relative_to(ROOT)), "--tests", ",".join(args.tests),
            "--vantages", str(args.vantages), "--ttl-scale", str(args.ttl_scale)]


def _run_episode_process(command: list[str], episode_dir: Path, deadline: float, timeout_minutes: float,
                         record: dict[str, Any]) -> int | None:
    """Run the in-container episode until it exits or the wall deadline SIGTERMs it."""
    with (episode_dir / "episode.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            return process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            record["failure"] = f"episode exceeded {timeout_minutes:g} minute wall timeout"
            try:
                _run(["docker", "exec", _container(), "/opt/horus-venv/bin/python", "-c",
                      "import os,signal; p='/horus/" + str(episode_dir.relative_to(ROOT)) + "/episode.pid'; "
                      "pid=int(open(p).read()); os.kill(pid,signal.SIGTERM)"], timeout=15.0, check=False)
                process.wait(timeout=30.0)
            except (subprocess.TimeoutExpired, OSError, RuntimeError):
                process.kill()
                process.wait()
            return 124
        finally:
            (episode_dir / "episode.pid").unlink(missing_ok=True)


def _merge_episode_result(record: dict[str, Any], episode_dir: Path, code: int | None) -> None:
    """Fold episode_result.json into the record and name the failure when it did not pass."""
    result_file = episode_dir / "episode_result.json"
    if result_file.is_file():
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                record.update(payload)
        except (json.JSONDecodeError, OSError) as exc:
            record["failure"] = f"invalid episode result: {exc}"
    else:
        # A clean exit code still means "failed" when nothing was written: the episode was
        # interrupted (host OOM, container stop, SIGKILL) before it could report anything.
        state = container_state(_container())
        if state:
            record["container_state"] = state
        record["failure"] = _missing_result_failure(code, state)
    if code and not record.get("failure"):
        record["failure"] = f"episode process exited {code}; see episode.log"
    if record.get("status") != "passed" and not record.get("failure"):
        tests = record.get("tests", {})
        failed = [
            f"{name}: {result.get('reason') or result.get('status', 'failed')}"
            for name, result in tests.items()
            if isinstance(result, Mapping) and result.get("passed") is not True
        ] if isinstance(tests, Mapping) else []
        if failed:
            record["failure"] = "; ".join(failed)
        elif isinstance(record.get("civilian_check"), Mapping) and record["civilian_check"].get("passed") is False:
            record["failure"] = "civilian scenario check failed"
    record["status"] = "passed" if not record.get("failure") and record.get("status") == "passed" else "failed"


def _collect_logs_and_stop(record: dict[str, Any], episode_dir: Path, active_run: Path | None) -> None:
    """Copy the simulation logs beside the episode, then stop the simulation."""
    if active_run is None:
        active_file = ROOT / "sim" / "run" / ".active"
        if active_file.exists():
            active_run = ROOT / "sim" / "run" / active_file.read_text(encoding="utf-8").strip()
    if active_run is not None and active_run.is_dir():
        log_dir = active_run / "logs"
        civilians = log_dir / "civilians.jsonl"
        if civilians.is_file():
            shutil.copy2(civilians, episode_dir / "civilians.jsonl")
        for name in ("gazebo.log", "sitl.log"):
            _tail_copy(log_dir / name, episode_dir / f"{name}.tail.log")
    try:
        state = _run([str(RUNNER), "status"], timeout=20.0, check=False, capture=True)
        if "state=running" in (state.stdout or "") or "state=created" in (state.stdout or ""):
            _run([str(RUNNER), "stop", "--purge"], timeout=120.0, check=False, capture=True)
        elif active_run is not None and active_run.exists():
            shutil.rmtree(active_run)
    except Exception as exc:
        record["failure"] = (record.get("failure", "") + f"; teardown failed: {exc}").strip("; ")
        record["status"] = "failed"


def _one_episode(args: argparse.Namespace, kind: str, seed: int | None) -> dict[str, Any]:
    world = f"horus_{kind}" if args.canonical else f"horus_{kind}_p{seed}"
    episode_dir = args.out / world
    episode_dir.mkdir(parents=True, exist_ok=True)
    record = _episode_record(kind, seed, world, episode_dir)
    started = time.monotonic()
    episode_deadline = started + args.timeout_minutes * 60.0
    active_run: Path | None = None
    try:
        manifest, sdf = _generate_world(args, kind, seed, world, episode_deadline)
        record["manifest"] = str(manifest.relative_to(ROOT))
        record["sdf"] = str(sdf.relative_to(ROOT))
        # run.sh start returns once the world is running (up to 900 s for 1.2 km generated worlds).
        _run_before_deadline([str(RUNNER), "start", "/horus/" + str(sdf.relative_to(ROOT))],
                             episode_deadline, cap=960.0)
        active_id = (ROOT / "sim" / "run" / ".active").read_text(encoding="utf-8").strip()
        active_run = ROOT / "sim" / "run" / active_id
        record["sim_run_id"] = active_id
        command = _episode_command(args, kind, seed, manifest, sdf, episode_dir, secrets.token_urlsafe(32))
        code = _run_episode_process(command, episode_dir, episode_deadline, args.timeout_minutes, record)
        _merge_episode_result(record, episode_dir, code)
    except (Exception, KeyboardInterrupt) as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["wall_duration_s"] = time.monotonic() - started
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        _collect_logs_and_stop(record, episode_dir, active_run)
        if (episode_dir / "events.jsonl").is_file():
            record["events"] = str((episode_dir / "events.jsonl").relative_to(ROOT))
        record["status"] = "passed" if not record.get("failure") and record.get("status") == "passed" else "failed"
    return record


def _parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", action="store_true", help="run each canonical world once")
    parser.add_argument("--kinds", nargs="+", choices=("forest", "urban"), default=("forest", "urban"))
    parser.add_argument("--seeds", nargs="+", type=int, default=None,
                        help="procedural seeds per kind (default: 1 2 3)")
    parser.add_argument("--random", type=int, metavar="N", default=None,
                        help="draw N distinct random seeds instead of --seeds and print them")
    parser.add_argument("--tests", default=",".join(DEFAULT_TESTS), help="comma-separated tests")
    parser.add_argument("--vantages", type=int, default=8)
    parser.add_argument(
        "--ttl-scale", type=ttl_scale, default=1.0,
        help="multiply per-command TTLs for hosts that run the sim below real time",
    )
    parser.add_argument("--out", type=Path, help="campaign output directory")
    parser.add_argument(
        "--timeout-minutes", type=float, default=None,
        help="wall timeout per episode (default: 45 min x --ttl-scale, because a host that "
             "needs TTL scaling also needs proportionally more wall time)",
    )
    args = parser.parse_args(argv)
    selected = tuple(dict.fromkeys(part.strip() for part in args.tests.split(",") if part.strip()))
    unknown = sorted(set(selected) - VALID_TESTS)
    if not selected or unknown:
        parser.error(f"invalid --tests values: {', '.join(unknown) if unknown else 'empty list'}")
    if args.vantages < 1:
        parser.error("--vantages must be positive")
    if args.timeout_minutes is None:
        # A host slow enough to need TTL scaling needs the same factor of wall time: a full
        # 8-test episode is ~20 min of simulation, i.e. ~90 min wall at RTF 0.25.
        args.timeout_minutes = 45.0 * max(1.0, args.ttl_scale)
    if not math.isfinite(args.timeout_minutes) or args.timeout_minutes <= 0:
        parser.error("--timeout-minutes must be finite and positive")
    if args.random is not None:
        if args.seeds is not None:
            parser.error("--random and --seeds are mutually exclusive")
        if not 1 <= args.random <= SEED_SPACE_MAX:
            parser.error(f"--random must be between 1 and {SEED_SPACE_MAX}")
        args.seeds = random_seeds(args.random)
    elif args.seeds is None:
        args.seeds = [1, 2, 3]
    elif not args.seeds or any(seed < 1 for seed in args.seeds):
        parser.error("--seeds must contain positive integers")
    args.tests = selected
    args.out = args.out or ROOT / "runs" / "campaign" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out = args.out if args.out.is_absolute() else ROOT / args.out
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.random is not None:
        seeds_text = " ".join(str(seed) for seed in args.seeds)
        print(f"Random seeds ({args.random}): {seeds_text}", flush=True)
        print(f"Reproduce with: --seeds {seeds_text}", flush=True)
    episodes: list[dict[str, Any]] = []
    schedule = [(kind, None) for kind in args.kinds] if args.canonical else [
        (kind, seed) for kind in args.kinds for seed in args.seeds
    ]
    for index, (kind, seed) in enumerate(schedule, 1):
        print(f"[{index}/{len(schedule)}] {kind} seed={seed if seed is not None else 'canonical'}", flush=True)
        episode = _one_episode(args, kind, seed)
        episodes.append(episode)
        print(f"  {episode.get('status')}: {episode.get('wall_duration_s', 0.0):.1f}s"
              + (f" — {episode.get('failure')}" if episode.get("failure") else ""), flush=True)
        rtf_mean = ((episode.get("rtf") or {}) if isinstance(episode.get("rtf"), Mapping) else {}).get("mean")
        if isinstance(rtf_mean, (int, float)) and 0.0 < rtf_mean < 0.5:
            print(f"  local hint: measured mean RTF {rtf_mean:.2f}; use --ttl-scale "
                  f"{max(1, round(1.0 / rtf_mean))} and a wall timeout of at least "
                  f"45 min x that scale (this run used {args.timeout_minutes:g} min)", flush=True)
        summary = {"created_at": datetime.now(timezone.utc).isoformat(), "episode_count": len(episodes),
                   "seeds": list(args.seeds), "kinds": list(args.kinds), "tests": list(args.tests),
                   "random_seed_count": args.random, "episodes": episodes,
                   "aggregate": aggregate_campaign(episodes)}
        (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return 0 if all(episode.get("status") == "passed" for episode in episodes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
