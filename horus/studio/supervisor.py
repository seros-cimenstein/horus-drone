"""Operator Studio session supervisor: manages one active drone world or hardware link."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

import httpx
import yaml

from horus.bootstrap import assets_ready, check_assets, ensure_assets
from horus.logging import unwrap_exception
from horus.mcp_client import open_session
from horus.studio.capabilities import check_capabilities
from horus.studio.link_shaper import LinkShaper
from horus.studio.mcp_configs import generate_mcp_configs
from horus.studio.preflight import list_serial_ports, run_preflight
from horus.studio.proc import ManagedProcess, ProcessGroup
from horus.studio.settings import StudioSettings, StudioSettingsStore, test_ai_connection
from horus.world.scenes import SCENE_NAMES, SCENE_SPECS
from horus.world.sitl import probe_tcp_port, wait_for_tcp_port

logger = logging.getLogger(__name__)


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _allocate_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _child_log_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Ensure child processes writing to `.log` files emit plain or JSON logs without ANSI escapes."""
    env = dict(base_env)
    fmt = env.get("HORUS_LOG_FORMAT", "").strip().lower()
    if fmt != "json":
        env["HORUS_LOG_FORMAT"] = "plain"
    return env


def _tail_log_lines(log_path: Path, max_lines: int = 15) -> str:
    if not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:]) if lines else ""


def _wait_for_process_port(
    proc: ManagedProcess,
    host: str,
    port: int,
    timeout_s: float,
    log_path: Path,
    label: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: str | None = None
    while True:
        rc = proc.poll()
        if rc is not None:
            tail = _tail_log_lines(log_path)
            detail = f"\n{tail}" if tail else ""
            raise RuntimeError(f"{label} exited early with code {rc}.{detail}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = _tail_log_lines(log_path)
            err_note = f" (last probe error: {last_err})" if last_err else ""
            detail = f"\n{tail}" if tail else ""
            raise RuntimeError(f"{label} did not open {host}:{port} within {timeout_s:g}s{err_note}.{detail}")
        ok, err = probe_tcp_port(host, port, timeout_s=min(0.4, remaining))
        if ok:
            return
        last_err = err
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


@dataclass(slots=True)
class StudioState:
    lifecycle: str = "idle"  # idle | bootstrapping | starting | running | stopping | error
    mode: str = "none"  # none | builtin_sim | real_drone | gazebo | gta
    scene: str | None = None
    target_label: str = "No active drone session"
    run_id: str | None = None
    mcp_url: str | None = None
    spectator_url: str | None = None
    error: str | None = None
    rtf: float | None = None


class StudioSupervisor:
    """Supervises at most one active HORUS session and hot-binds ``HorusCommander`` + ``DisplayReader``."""

    def __init__(
        self,
        *,
        settings_store: StudioSettingsStore | None = None,
        link_shaper: LinkShaper | None = None,
        runs_root: Path | None = None,
        publish_event: Callable[[Mapping[str, Any]], Any] | None = None,
        commander: Any = None,
        display: Any = None,
        session_opener: Callable[[str, str], Any] = open_session,
    ) -> None:
        self.settings_store = settings_store or StudioSettingsStore()
        self.link_shaper = link_shaper or LinkShaper()
        self.runs_root = Path(runs_root) if runs_root is not None else Path("runs")
        self.publish_event = publish_event
        self.commander = commander
        self.display = display
        self._session_opener = session_opener

        self._lock = asyncio.Lock()
        self._procs = ProcessGroup()
        self._grounder_procs = ProcessGroup()
        self._grounder_url: str | None = None
        self._external_sim_stop_cmd: list[str] | None = None
        self._state = StudioState()
        self._active_mcp_token: str | None = None
        self._last_start_params: dict[str, Any] | None = None
        self._bootstrap_task: asyncio.Task[Any] | None = None
        self._http_client = httpx.AsyncClient(timeout=3.0)
        self._studio_port: int = 8765
        self._phone_token: str | None = None

    def set_control_endpoint(self, port: int, phone_token: str | None) -> None:
        self._studio_port = int(port)
        self._phone_token = phone_token

    def bind_commander_and_display(self, commander: Any, display: Any) -> None:
        self.commander = commander
        self.display = display

    def _emit_status(self) -> None:
        if self.publish_event is not None:
            try:
                self.publish_event({"kind": "studio_status", **self.status()})
            except Exception:
                pass

    def status(self) -> dict[str, Any]:
        raw_assets = check_assets()
        assets = {
            name: {
                "available": st.available,
                "optional": st.optional,
                "path": str(st.path) if st.path else None,
                "source": st.source,
                "detail": st.detail,
            }
            for name, st in raw_assets.items()
        }
        ai_public = self.settings_store.public_view()["ai_status"]
        return {
            **asdict(self._state),
            "assets": assets,
            "assets_ready": assets_ready(raw_assets),
            "ai_status": ai_public,
            "link_shaper": self.link_shaper.to_dict(),
            "scenes": [
                {
                    "name": spec.name,
                    "title": spec.title,
                    "subtitle": spec.subtitle,
                    "indoor": spec.indoor,
                    "prompts": list(spec.default_prompts),
                }
                for spec in SCENE_SPECS.values()
            ],
        }

    async def bootstrap_assets(self, *, include_models: bool = True, include_sitl: bool = True) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        def on_progress(name: str, done: int, total: int | None, state: str) -> None:
            if self.publish_event is not None:
                loop.call_soon_threadsafe(
                    self.publish_event,
                    {
                        "kind": "bootstrap_progress",
                        "asset": name,
                        "bytes_done": done,
                        "bytes_total": total,
                        "state": state,
                    },
                )

        prev = self._state.lifecycle
        if prev == "idle":
            self._state.lifecycle = "bootstrapping"
            self._emit_status()
        try:
            statuses = await asyncio.to_thread(
                ensure_assets,
                progress_cb=on_progress,
                include_models=include_models,
                include_sitl=include_sitl,
            )
            return {
                "ready": assets_ready(statuses),
                "assets": {
                    name: {
                        "available": st.available,
                        "optional": st.optional,
                        "detail": st.detail,
                        "source": st.source,
                    }
                    for name, st in statuses.items()
                },
            }
        finally:
            if self._state.lifecycle == "bootstrapping":
                self._state.lifecycle = "idle"
                self._emit_status()

    async def start_session(self, params: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await self._stop_unlocked()
            mode = str(params.get("mode") or "builtin_sim").strip().lower()
            self._last_start_params = dict(params)
            self._state = StudioState(
                lifecycle="starting",
                mode=mode,
                scene=str(params.get("scene") or "indoor_sar") if mode == "builtin_sim" else None,
                target_label=f"Starting {mode}…",
                run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:6]}",
            )
            self._emit_status()
            try:
                logger.info("Starting Studio session (%s, run_id=%s)", mode, self._state.run_id)
                if mode == "builtin_sim":
                    await self._start_builtin_sim(params)
                elif mode == "real_drone":
                    await self._start_real_drone(params)
                elif mode in {"gazebo", "gta"}:
                    await self._start_external_sim(mode, params)
                else:
                    raise ValueError(f"unsupported session mode {mode!r}")
                self._state.lifecycle = "running"
                self._state.error = None
                self._emit_status()
                logger.info("Studio session ready (%s: %s)", mode, self._state.target_label)
                return self.status()
            except Exception as exc:
                logger.exception("Studio failed to start session (%s)", mode)
                await self._stop_unlocked(keep_error=str(exc))
                self._state.lifecycle = "error"
                self._state.error = str(exc)
                self._emit_status()
                raise

    async def _start_builtin_sim(self, params: Mapping[str, Any]) -> None:
        scene = str(params.get("scene") or "indoor_sar").strip()
        if scene not in SCENE_NAMES:
            raise ValueError(f"unknown 3D scene {scene!r}")
        seed = int(params.get("seed", 42))

        assets = check_assets()
        if not assets["sitl"].available:
            raise RuntimeError(
                "ArduCopter SITL binary is not installed yet. Click 'Download Assets' or run `horus setup`."
            )

        run_id = self._state.run_id or uuid4().hex[:8]
        run_dir = (self.runs_root / "studio" / run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        map_dir = run_dir / "map_tiles"

        mavlink_port = _allocate_loopback_port()
        range_port = _allocate_loopback_port()
        camera_port = _allocate_loopback_port()
        spectator_port = _allocate_loopback_port()
        mcp_port = _allocate_loopback_port()
        json_port = _allocate_udp_port()
        json_in_port = _allocate_udp_port()
        while json_in_port == json_port:
            json_in_port = _allocate_udp_port()
        gimbal_port = _allocate_udp_port()
        mcp_token = secrets.token_urlsafe(24)

        from horus.bootstrap import preferred_linux_mujoco_gl

        world_env = _child_log_env(os.environ)
        gl = preferred_linux_mujoco_gl()
        if gl is not None:
            world_env.setdefault("MUJOCO_GL", gl)

        world_cmd = [
            sys.executable,
            "-m",
            "horus.world",
            "--scene",
            scene,
            "--seed",
            str(seed),
            "--run-dir",
            str(run_dir),
            "--json-port",
            str(json_port),
            "--camera-port",
            str(camera_port),
            "--spectator-port",
            str(spectator_port),
            "--gimbal-port",
            str(gimbal_port),
            "--range-port",
            str(range_port),
            "--map-dir",
            str(map_dir),
        ]
        world_log = run_dir / "world.log"
        world_proc = self._procs.spawn(
            "world",
            world_cmd,
            cwd=str(Path.cwd()),
            env=world_env,
            log_path=str(world_log),
        )
        await asyncio.to_thread(
            _wait_for_process_port,
            world_proc,
            "127.0.0.1",
            camera_port,
            25.0,
            world_log,
            "Built-in 3D world camera stream",
        )

        from horus.bootstrap import resolve_model_paths, resolve_sitl_binary
        from horus.world.sitl import compose_sitl_parms, resolve_default_parm

        sitl_bin, bundle_parm = resolve_sitl_binary()
        sitl_dir = run_dir / "sitl"
        sitl_dir.mkdir(parents=True, exist_ok=True)
        default_parm = resolve_default_parm(bundle_parm, sitl_dir)
        overrides_parm = compose_sitl_parms(scene, sitl_dir / "sitl-overrides.parm")
        sitl_cmd = [
            str(sitl_bin),
            "-S",
            "-I0",
            "--home",
            "-35.363262,149.165237,584.0,0.0",
            "--model",
            "JSON:127.0.0.1",
            "--sim-port-out",
            str(json_port),
            "--sim-port-in",
            str(json_in_port),
            "--speedup",
            "1",
            "--slave",
            "0",
            "--defaults",
            f"{default_parm},{overrides_parm}",
            "--serial0",
            f"tcp:{mavlink_port}:wait",
            "--serial2",
            f"tcp:{range_port}",
        ]
        sitl_log = run_dir / "sitl.log"
        sitl_proc = self._procs.spawn("sitl", sitl_cmd, cwd=str(sitl_dir), log_path=str(sitl_log))
        await asyncio.to_thread(
            _wait_for_process_port,
            sitl_proc,
            "127.0.0.1",
            mavlink_port,
            25.0,
            sitl_log,
            "ArduCopter SITL",
        )

        models = resolve_model_paths()
        yolo_path = str(models["yolov8n"]) if models["yolov8n"] else "none"
        reid_path = str(models["reid"]) if models["reid"] else "color_histogram"
        st = self.settings_store.load_settings()
        cfg_data = {
            "flight": {"connection": f"tcp:127.0.0.1:{mavlink_port}"},
            "gimbal": {"connection": f"udpin:127.0.0.1:{gimbal_port}"},
            "executor": {
                "speed_m_s": st.flight_speed_m_s if scene != "indoor_sar" else min(st.flight_speed_m_s, 2.0),
                "default_gimbal_pitch_deg": st.default_gimbal_pitch_deg,
                "link_loss_rth_delay_s": st.link_loss_rth_delay_s,
            },
            "perception": {
                "enabled": True,
                "source": f"sim://127.0.0.1:{camera_port}",
                "detector_model": yolo_path,
                "embedding_model": reid_path,
                "max_fps": st.camera_max_fps,
                "open_vocab_hz": st.open_vocab_hz,
                "grounder_url": st.grounder_url or None,
            },
            "server": {"host": "127.0.0.1", "port": mcp_port, "auth_token_env": "HORUS_MCP_TOKEN"},
            "map": {"provider": "gta", "data_path": str(map_dir)},
        }
        cfg_path = run_dir / "horus_studio.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg_data, sort_keys=False), encoding="utf-8")

        env = _child_log_env(
            {
                **os.environ,
                "HORUS_CONFIG": str(cfg_path),
                "HORUS_MCP_TOKEN": mcp_token,
                "HORUS_RUN_DIR": str(run_dir / "mcp_run"),
            }
        )
        mcp_cmd = [sys.executable, "-m", "horus.mcp_server.server"]
        mcp_log = run_dir / "mcp.log"
        mcp_proc = self._procs.spawn("mcp", mcp_cmd, cwd=str(Path.cwd()), env=env, log_path=str(mcp_log))
        await asyncio.to_thread(
            _wait_for_process_port,
            mcp_proc,
            "127.0.0.1",
            mcp_port,
            30.0,
            mcp_log,
            "Drone board (horus-mcp)",
        )

        mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
        self._active_mcp_token = mcp_token
        self._state.mcp_url = mcp_url
        self._state.spectator_url = f"http://127.0.0.1:{spectator_port}/frame.jpg"
        self._state.target_label = f"Built-in 3D · {SCENE_SPECS[scene].title}"
        self._state.scene = scene
        await self._rebind_clients(mcp_url, mcp_token)

    async def _start_real_drone(self, params: Mapping[str, Any]) -> None:
        submode = str(params.get("submode") or "remote").strip().lower()
        if submode == "remote":
            mcp_url = str(params.get("mcp_url") or "").strip()
            token = str(params.get("mcp_token") or "").strip() or self.settings_store.get_secret("REMOTE_MCP_TOKEN") or ""
            if not mcp_url or not token:
                raise ValueError("Remote companion board requires mcp_url and mcp_token")
            self._active_mcp_token = token
            self._state.mcp_url = mcp_url
            self._state.target_label = f"Companion · {mcp_url}"
            await self._rebind_clients(mcp_url, token)
            return

        if submode == "direct":
            endpoint = str(params.get("mavlink_connection") or "").strip()
            if not endpoint:
                raise ValueError("Direct flight controller requires a serial or UDP MAVLink endpoint")
            baud = int(params.get("baud", 115200))
            camera_source = params.get("camera_source")
            gimbal_conn = params.get("gimbal_connection") or None
            mcp_port = _allocate_loopback_port()
            mcp_token = secrets.token_urlsafe(24)
            run_id = self._state.run_id or uuid4().hex[:8]
            run_dir = (self.runs_root / "studio" / run_id).resolve()
            run_dir.mkdir(parents=True, exist_ok=True)
            cfg_data: dict[str, Any] = {
                "flight": {"connection": endpoint, "baud": baud},
                "gimbal": {"connection": gimbal_conn},
                "perception": {"enabled": bool(camera_source), "source": str(camera_source or "0")},
                "server": {"host": "127.0.0.1", "port": mcp_port, "auth_token_env": "HORUS_MCP_TOKEN"},
            }
            cfg_path = run_dir / "horus_real.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg_data, sort_keys=False), encoding="utf-8")
            env = _child_log_env({**os.environ, "HORUS_CONFIG": str(cfg_path), "HORUS_MCP_TOKEN": mcp_token})
            cmd = [sys.executable, "-m", "horus.mcp_server.server"]
            if not camera_source:
                cmd.append("--no-perception")
            self._procs.spawn("mcp", cmd, cwd=str(Path.cwd()), env=env, log_path=str(run_dir / "mcp.log"))
            if not await asyncio.to_thread(wait_for_tcp_port, "127.0.0.1", mcp_port, 30.0):
                raise RuntimeError("Local Drone board failed to connect to flight controller")
            mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
            self._active_mcp_token = mcp_token
            self._state.mcp_url = mcp_url
            self._state.target_label = f"Direct FC · {endpoint}"
            await self._rebind_clients(mcp_url, mcp_token)
            return

        raise ValueError(f"unknown real_drone submode {submode!r}")

    async def _start_external_sim(self, mode: str, params: Mapping[str, Any]) -> None:
        action = str(params.get("action") or "attach").strip().lower()
        if action == "attach":
            mcp_url = str(params.get("mcp_url") or "http://127.0.0.1:8000/mcp").strip()
            token = str(params.get("mcp_token") or os.environ.get("HORUS_MCP_TOKEN") or "").strip()
            if not token:
                raise ValueError(f"{mode.upper()} attach requires mcp_token or HORUS_MCP_TOKEN")
            self._active_mcp_token = token
            self._state.mcp_url = mcp_url
            self._state.target_label = f"{mode.upper()} · {mcp_url}"
            await self._rebind_clients(mcp_url, token)
            return

        if action != "launch":
            raise ValueError(f"unsupported external sim action {action!r}")

        if mode == "gazebo":
            run_sh = Path.cwd() / "sim" / "run.sh"
            if not run_sh.is_file():
                raise RuntimeError("sim/run.sh not found; run HORUS from the repo checkout or use Attach mode.")
            if shutil.which("docker") is None:
                raise RuntimeError("Docker CLI not found on PATH; cannot launch local Gazebo container.")
            argv = ["bash", str(run_sh), "start"]
            if params.get("gui"):
                argv.append("--gui")
            if params.get("procedural"):
                argv.append("--procedural")
                if params.get("kind") in {"forest", "urban"}:
                    argv.extend(["--kind", str(params["kind"])])
                if params.get("seed") is not None:
                    argv.extend(["--seed", str(int(params["seed"]))])
            elif params.get("world"):
                argv.append(str(params["world"]))

            run_id = self._state.run_id or uuid4().hex[:8]
            run_dir = (self.runs_root / "studio" / run_id).resolve()
            run_dir.mkdir(parents=True, exist_ok=True)
            mcp_token = str(params.get("mcp_token") or os.environ.get("HORUS_MCP_TOKEN") or secrets.token_urlsafe(24))
            env = {**os.environ, "HORUS_MCP_TOKEN": mcp_token}
            self._external_sim_stop_cmd = ["bash", str(run_sh), "stop"]
            self._procs.spawn("gazebo_sim", argv, cwd=str(Path.cwd()), env=env, log_path=str(run_dir / "gazebo.log"))
            if not await asyncio.to_thread(wait_for_tcp_port, "127.0.0.1", 8000, 60.0):
                raise RuntimeError("Gazebo Drone board did not open tcp:127.0.0.1:8000 within 60s (check gazebo.log)")
            mcp_url = "http://127.0.0.1:8000/mcp"
            self._active_mcp_token = mcp_token
            self._state.mcp_url = mcp_url
            self._state.target_label = f"Gazebo · {params.get('world') or 'default'}"
            await self._rebind_clients(mcp_url, mcp_token)
            return

        if mode == "gta":
            run_id = self._state.run_id or uuid4().hex[:8]
            run_dir = (self.runs_root / "studio" / run_id).resolve()
            run_dir.mkdir(parents=True, exist_ok=True)
            mcp_token = str(params.get("mcp_token") or os.environ.get("HORUS_MCP_TOKEN") or secrets.token_urlsafe(24))
            if os.name == "nt":
                from horus.bootstrap import resolve_sitl_binary
                from horus.world.sitl import compose_sitl_parms, resolve_default_parm

                sitl_bin, bundle_parm = resolve_sitl_binary()
                sitl_dir = run_dir / "sitl"
                sitl_dir.mkdir(parents=True, exist_ok=True)
                default_parm = resolve_default_parm(bundle_parm, sitl_dir)
                overrides = compose_sitl_parms("outdoor", sitl_dir / "sitl-overrides.parm")
                gta_addr = str(params.get("gta_host") or "127.0.0.1").strip()
                self._procs.spawn(
                    "gta_sitl",
                    [
                        str(sitl_bin), "-S", "-I0",
                        "--model", f"JSON:{gta_addr}",
                        "--defaults", f"{default_parm},{overrides}",
                        "--serial0", "tcp:127.0.0.1:5760:wait",
                        "--serial2", "tcp:127.0.0.1:5763",
                    ],
                    cwd=str(sitl_dir),
                    log_path=str(run_dir / "sitl.log"),
                )
                if not await asyncio.to_thread(wait_for_tcp_port, "127.0.0.1", 5760, 25.0):
                    raise RuntimeError("GTA ArduCopter.exe did not open tcp:127.0.0.1:5760")
                self._procs.spawn(
                    "gta_gimbal",
                    [sys.executable, "-m", "horus.gimbal.gta", "--mavlink", "udpout:127.0.0.1:14600", "--mod", f"{gta_addr}:5613", "--listen", "127.0.0.1:5614"],
                    cwd=str(Path.cwd()),
                    env=_child_log_env(os.environ),
                    log_path=str(run_dir / "gimbal.log"),
                )
                self._procs.spawn(
                    "gta_tof",
                    [sys.executable, "-m", "horus.tof_bridge.gta", "--listen", "127.0.0.1:5612", "--mavlink", "tcp:127.0.0.1:5763"],
                    cwd=str(Path.cwd()),
                    env=_child_log_env(os.environ),
                    log_path=str(run_dir / "tof.log"),
                )
                cfg_path = Path.cwd() / "config" / "horus_gta.yaml"
                env = _child_log_env({**os.environ, "HORUS_CONFIG": str(cfg_path), "HORUS_MCP_TOKEN": mcp_token})
                self._procs.spawn("gta_mcp", [sys.executable, "-m", "horus.mcp_server.server"], cwd=str(Path.cwd()), env=env, log_path=str(run_dir / "mcp.log"))
                if not await asyncio.to_thread(wait_for_tcp_port, "127.0.0.1", 8000, 30.0):
                    raise RuntimeError("GTA Drone board did not open tcp:127.0.0.1:8000")
            else:
                run_sh = Path.cwd() / "sim" / "run.sh"
                if not run_sh.is_file() or shutil.which("docker") is None:
                    raise RuntimeError("Linux GTA launch requires Docker + sim/run.sh (or use Attach mode).")
                env = {**os.environ, "HORUS_MCP_TOKEN": mcp_token}
                self._external_sim_stop_cmd = ["bash", str(run_sh), "stop"]
                self._procs.spawn("gta_sim", ["bash", str(run_sh), "start", "--gta"], cwd=str(Path.cwd()), env=env, log_path=str(run_dir / "gta.log"))
                if not await asyncio.to_thread(wait_for_tcp_port, "127.0.0.1", 8000, 60.0):
                    raise RuntimeError("GTA Drone board did not open tcp:127.0.0.1:8000 within 60s")
            mcp_url = "http://127.0.0.1:8000/mcp"
            self._active_mcp_token = mcp_token
            self._state.mcp_url = mcp_url
            self._state.target_label = "GTA V · Physical Collisions"
            await self._rebind_clients(mcp_url, mcp_token)
            return

        raise ValueError(f"cannot launch external sim mode {mode!r}")

    async def _rebind_clients(self, mcp_url: str, token: str) -> None:
        if self.display is not None and hasattr(self.display, "rebind"):
            await self.display.rebind(mcp_url, token)
        if self.commander is not None and hasattr(self.commander, "rebind"):
            cs, api_key, _ = self.settings_store.commander_settings()
            await self.commander.rebind(mcp_url, token, cs, api_key=api_key)

    async def stop_session(self) -> dict[str, Any]:
        async with self._lock:
            await self._stop_unlocked()
            self._emit_status()
            return self.status()

    async def restart_session(self) -> dict[str, Any]:
        params = dict(self._last_start_params or {"mode": "builtin_sim", "scene": "indoor_sar"})
        return await self.start_session(params)

    async def _stop_unlocked(self, *, keep_error: str | None = None) -> None:
        if self._state.lifecycle == "idle" and not self._procs.processes():
            return
        self._state.lifecycle = "stopping"
        self._emit_status()
        logger.info("Stopping Studio session (%s)", self._state.mode)
        if self.commander is not None and hasattr(self.commander, "unbind"):
            try:
                await self.commander.unbind()
            except Exception as exc:
                logger.debug("commander unbind note: %s", exc)
        if self.display is not None and hasattr(self.display, "unbind"):
            try:
                await self.display.unbind()
            except Exception as exc:
                logger.debug("display unbind note: %s", exc)
        await asyncio.to_thread(self._procs.stop_all)
        if self._external_sim_stop_cmd is not None:
            stop_cmd, self._external_sim_stop_cmd = self._external_sim_stop_cmd, None
            try:
                import subprocess

                await asyncio.to_thread(
                    subprocess.run, stop_cmd, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL, capture_output=True, timeout=15
                )
            except Exception as exc:
                logger.debug("external sim stop note: %s", exc)
        self._active_mcp_token = None
        self._state = StudioState(
            lifecycle="error" if keep_error else "idle",
            mode="none",
            target_label="No active drone session",
            error=keep_error,
        )

    async def close(self) -> None:
        async with self._lock:
            await self._stop_unlocked()
        self._procs.close()
        self._grounder_procs.close()
        await self._http_client.aclose()

    def capabilities(self, *, probe_external_tools: bool = True) -> list[dict[str, Any]]:
        return check_capabilities(self.settings_store, probe_external_tools=probe_external_tools)

    def mcp_configs(self, *, include_token: bool = False) -> dict[str, Any]:
        return generate_mcp_configs(
            self._state.mcp_url,
            bearer_token=self._active_mcp_token,
            include_token=include_token,
        )

    def grounder_status(self) -> dict[str, Any]:
        procs = self._grounder_procs.processes()
        running = any(p.poll() is None for p in procs)
        st = self.settings_store.load_settings()
        return {
            "running": running,
            "url": self._grounder_url or st.grounder_url,
            "pid": procs[0].pid if running and procs else None,
        }

    def start_grounder(self, *, backend: str = "grounding-dino", port: int = 8100) -> dict[str, Any]:
        self.stop_grounder()
        venv_py = Path.cwd() / "models" / "grounder-venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        py = str(venv_py) if venv_py.is_file() else sys.executable
        log_path = self.runs_root / "studio" / "grounder.log"
        self._grounder_procs.spawn(
            "grounder",
            [py, "-m", "horus.grounder.server", "--backend", backend, "--port", str(int(port))],
            cwd=str(Path.cwd()),
            log_path=str(log_path),
        )
        self._grounder_url = f"http://127.0.0.1:{int(port)}"
        return self.grounder_status()

    def stop_grounder(self) -> dict[str, Any]:
        self._grounder_procs.stop_all()
        self._grounder_url = None
        return self.grounder_status()

    def phone_status(self) -> dict[str, Any]:
        import json as _json
        import subprocess

        local_tok = self._phone_token or ""
        frag = f"#token={local_tok}" if local_tok else ""
        local_url = f"http://127.0.0.1:{self._studio_port}/?view=phone{frag}"
        ts_bin = shutil.which("tailscale")
        dns_name = ""
        certs = False
        serve_active = False
        if ts_bin:
            try:
                res = subprocess.run([ts_bin, "status", "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1.5, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    data = _json.loads(res.stdout)
                    dns_name = str((data.get("Self") or {}).get("DNSName", "")).rstrip(".")
                    certs = bool(data.get("CertDomains"))
                sres = subprocess.run([ts_bin, "serve", "status", "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1.5, check=False)
                if sres.returncode == 0 and sres.stdout.strip():
                    scfg = _json.loads(sres.stdout)
                    serve_active = bool(scfg.get("Web") or scfg.get("TCP"))
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        tailnet_url = f"https://{dns_name}/?view=phone{frag}" if (dns_name and serve_active) else None
        return {
            "local_url": local_url,
            "tailnet_url": tailnet_url,
            "phone_token_active": bool(local_tok),
            "tailscale_installed": ts_bin is not None,
            "tailscale_dns": dns_name or None,
            "tailscale_certs": certs,
            "serve_active": serve_active,
        }

    def phone_serve_action(self, action: str) -> dict[str, Any]:
        import subprocess

        if action not in {"start", "stop", "check"}:
            raise ValueError("action must be start, stop, or check")
        script = Path.cwd() / "scripts" / "phone_serve.sh"
        if os.name != "posix" or not script.is_file():
            raise RuntimeError("scripts/phone_serve.sh is only available on Linux/macOS repo checkouts; run `tailscale serve` manually on Windows.")
        env = {
            **os.environ,
            "HORUS_COMMANDER_URL": f"http://127.0.0.1:{self._studio_port}",
            **({"HORUS_COMMANDER_PHONE_TOKEN": self._phone_token} if self._phone_token else {}),
        }
        res = subprocess.run(["bash", str(script), action], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=10, check=False)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or f"phone_serve.sh {action} failed").strip())
        return {**self.phone_status(), "output": (res.stdout or "").strip()}

    async def spectator_frame(self, *, preset: str = "follow", show_tof: bool = True) -> bytes:
        url = self._state.spectator_url
        if not url:
            raise RuntimeError("No 3D spectator stream is active")
        resp = await self._http_client.get(url, params={"preset": preset, "tof": "1" if show_tof else "0"})
        if resp.status_code != 200:
            raise RuntimeError(f"Spectator frame unavailable (HTTP {resp.status_code})")
        return resp.content

    async def manual_move(self, action: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Execute an authenticated operator manual move or vision command (never waits on AI)."""
        allowed = {"take_off", "move", "turn", "look", "goto", "set_vocabulary", "point"}
        if action not in allowed:
            raise ValueError(f"manual action must be one of {sorted(allowed)}")
        if not self._state.mcp_url or not self._active_mcp_token:
            raise RuntimeError("No active Drone board session")
        from horus.mcp_client import call_json

        args = dict(params or {})
        try:
            async with self._session_opener(self._state.mcp_url, self._active_mcp_token) as session:
                if action in {"take_off", "move", "turn", "look", "goto"}:
                    await call_json(session, "operator_release")
                result = await call_json(session, action, args)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            inner = unwrap_exception(exc)
            if inner is not exc:
                raise RuntimeError(str(inner)) from inner
            raise
        return {"action": action, "result": result}

    async def preflight(self) -> list[dict[str, str]]:
        state_payload: Mapping[str, Any] | None = None
        if self.display is not None and self._state.lifecycle == "running":
            try:
                state_payload = await self.display.state()
            except Exception:
                state_payload = None
        ai_status = self.settings_store.public_view()["ai_status"]
        return run_preflight(state_payload, ai_status=ai_status, session_mode=self._state.mode)

    def hardware_ports(self) -> list[dict[str, str]]:
        return list_serial_ports()

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runs_root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.runs_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            stat_res = path.stat()
            entries.append({
                "name": path.relative_to(self.runs_root).as_posix(),
                "bytes": stat_res.st_size,
                "modified_utc": datetime.fromtimestamp(stat_res.st_mtime, tz=timezone.utc).isoformat(),
            })
        return entries

    def update_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.settings_store.load_settings()
        merged = {
            "ai_provider": payload.get("ai_provider", current.ai_provider),
            "model": payload.get("model", current.model),
            "base_url": payload.get("base_url", current.base_url),
            "reasoning_effort": payload.get("reasoning_effort", current.reasoning_effort),
            "claude_command": payload.get("claude_command", current.claude_command),
            "claude_effort": payload.get("claude_effort", current.claude_effort),
            "omp_command": payload.get("omp_command", current.omp_command),
            "omp_model": payload.get("omp_model", current.omp_model),
            "omp_thinking": payload.get("omp_thinking", current.omp_thinking),
            "default_scene": payload.get("default_scene", current.default_scene),
            "prefer_3d_view": payload.get("prefer_3d_view", current.prefer_3d_view),
            "theme": payload.get("theme", current.theme),
            "flight_speed_m_s": payload.get("flight_speed_m_s", current.flight_speed_m_s),
            "link_loss_rth_delay_s": payload.get("link_loss_rth_delay_s", current.link_loss_rth_delay_s),
            "default_gimbal_pitch_deg": payload.get("default_gimbal_pitch_deg", current.default_gimbal_pitch_deg),
            "camera_max_fps": payload.get("camera_max_fps", current.camera_max_fps),
            "open_vocab_hz": payload.get("open_vocab_hz", current.open_vocab_hz),
            "grounder_url": payload.get("grounder_url", current.grounder_url),
            "recent_hardware": payload.get("recent_hardware", current.recent_hardware),
        }
        self.settings_store.save_settings(StudioSettings.from_dict(merged))
        secrets_patch = payload.get("secrets")
        if isinstance(secrets_patch, Mapping):
            self.settings_store.update_secrets(secrets_patch)
        return self.settings_store.public_view()

    async def test_ai(self) -> dict[str, Any]:
        return await asyncio.to_thread(test_ai_connection, self.settings_store)
