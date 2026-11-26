"""Direct cross-platform ArduCopter SITL launcher (no sim_vehicle.py / MAVProxy)."""

from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Sequence

from horus.bootstrap import resolve_sitl_binary

_INDOOR_OR_OUTDOOR = {
    "indoor_sar": "indoor",
    "indoor": "indoor",
    "forest_sar": "outdoor",
    "urban_plaza": "outdoor",
    "procedural": "outdoor",
    "outdoor": "outdoor",
}


def _packaged_parm(name: str) -> str:
    """Read a packaged ArduPilot ``.parm`` fragment (falling back to repo config/ardupilot)."""
    try:
        resource = importlib.resources.files("horus.world").joinpath("data", "ardupilot", name)
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except Exception:
        pass
    repo_fallback = Path(__file__).resolve().parents[2] / "config" / "ardupilot" / name
    if repo_fallback.is_file():
        return repo_fallback.read_text(encoding="utf-8")
    raise FileNotFoundError(f"ArduPilot parameter fragment {name!r} not found")


def resolve_default_parm(sitl_default_parm: Path | None, output_dir: Path) -> Path:
    """Return ``copter-default.parm`` path (from bundle or packaged fallback copied into ``output_dir``)."""
    if sitl_default_parm is not None and sitl_default_parm.is_file():
        return sitl_default_parm
    target = output_dir / "copter-default.parm"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_packaged_parm("copter-default.parm"), encoding="utf-8")
    return target


def compose_sitl_parms(
    profile: str,
    output_path: Path,
    *,
    extra_lines: Sequence[str] = (),
) -> Path:
    """Compose ``sitl-overrides.parm`` from packaged fragments plus JSON-physics defaults."""
    slot = _INDOOR_OR_OUTDOOR.get(profile, "outdoor")
    parts = [
        _packaged_parm("common.parm").rstrip(),
        "",
        _packaged_parm(f"{slot}.parm").rstrip(),
        "",
        "# Built-in 3D JSON lockstep defaults",
        "LOG_DISARMED 0",
        "FRAME_CLASS 1",
        "FRAME_TYPE 1",
        "SIM_RATE_HZ 800",
        "PRX_FILT 0",
    ]
    if extra_lines:
        parts.extend(["", *extra_lines])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return output_path


def probe_tcp_port(host: str, port: int, timeout_s: float = 0.5) -> tuple[bool, str | None]:
    """Attempt one TCP connect to ``host:port`` and return ``(ok, error_detail)``."""
    connect_timeout = min(1.0, max(0.2, float(timeout_s)))
    try:
        # Use direct AF_INET socket for IPv4 literals to bypass getaddrinfo / IPv6 ambiguity.
        socket.inet_aton(host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(connect_timeout)
            sock.connect((host, int(port)))
            return True, None
        finally:
            sock.close()
    except OSError as exc:
        if exc.errno is None:
            # inet_aton rejected a hostname; fall back to create_connection
            try:
                with socket.create_connection((host, int(port)), timeout=connect_timeout):
                    return True, None
            except OSError as conn_exc:
                return False, f"{type(conn_exc).__name__}: {conn_exc}"
        return False, f"{type(exc).__name__}: {exc}"


def wait_for_tcp_port(host: str, port: int, timeout_s: float = 30.0) -> bool:
    """Poll until ``host:port`` accepts a TCP connection or ``timeout_s`` expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ok, _ = probe_tcp_port(host, port, timeout_s=0.25)
        if ok:
            return True
        time.sleep(0.1)
    return False


class SitlProcess:
    """Managed direct ``arducopter`` / ``ArduCopter.exe`` subprocess in an isolated directory."""

    def __init__(
        self,
        run_dir: Path,
        *,
        scene: str = "indoor_sar",
        mavlink_port: int = 5760,
        range_port: int = 5763,
        json_address: str = "127.0.0.1",
        json_port_out: int | None = None,
        json_port_in: int | None = None,
        home: tuple[float, float, float, float] = (-35.363262, 149.165237, 584.0, 0.0),
        sitl_binary: Path | None = None,
        default_parm: Path | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.sitl_dir = self.run_dir / "sitl"
        self.scene = scene
        self.mavlink_port = int(mavlink_port)
        self.range_port = int(range_port)
        self.json_address = json_address
        self.json_port_out = int(json_port_out) if json_port_out is not None else None
        self.json_port_in = int(json_port_in) if json_port_in is not None else None
        self.home = home
        self._sitl_binary = sitl_binary
        self._default_parm = default_parm
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: object | None = None

    def build_command(self) -> tuple[list[str], Path]:
        """Prepare parameters in ``sitl_dir`` and return ``(argv, cwd)``."""
        self.sitl_dir.mkdir(parents=True, exist_ok=True)
        if self._sitl_binary is None:
            binary, bundle_parm = resolve_sitl_binary()
        else:
            binary = self._sitl_binary
            bundle_parm = self._default_parm or (binary.parent / "copter-default.parm")
        default_parm = resolve_default_parm(
            self._default_parm if self._default_parm is not None else bundle_parm,
            self.sitl_dir,
        )
        overrides = compose_sitl_parms(self.scene, self.sitl_dir / "sitl-overrides.parm")
        home_str = f"{self.home[0]},{self.home[1]},{self.home[2]},{self.home[3]}"
        argv = [
            str(binary),
            "-S",
            "-I0",
            "--home",
            home_str,
            "--model",
            f"JSON:{self.json_address}",
        ]
        if self.json_port_out is not None:
            argv.extend(["--sim-port-out", str(self.json_port_out)])
        if self.json_port_in is not None:
            argv.extend(["--sim-port-in", str(self.json_port_in)])
        argv.extend([
            "--speedup",
            "1",
            "--slave",
            "0",
            "--defaults",
            f"{default_parm},{overrides}",
            "--serial0",
            f"tcp:{self.mavlink_port}:wait",
            "--serial2",
            f"tcp:{self.range_port}",
        ])
        return argv, self.sitl_dir

    def start(self, *, wait_ready: bool = True, timeout_s: float = 30.0) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        argv, cwd = self.build_command()
        log_path = self.sitl_dir / "sitl.log"
        log_file = log_path.open("wb")
        self._log_handle = log_file
        popen_kwargs: dict[str, object] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        self._process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **popen_kwargs,  # type: ignore[arg-type]
        )
        if wait_ready:
            if not wait_for_tcp_port("127.0.0.1", self.mavlink_port, timeout_s=timeout_s):
                self.stop()
                raise RuntimeError(
                    f"SITL did not open MAVLink tcp:127.0.0.1:{self.mavlink_port} within {timeout_s:g}s"
                )
        return self._process

    def poll(self) -> int | None:
        return None if self._process is None else self._process.poll()

    def stop(self, timeout_s: float = 5.0) -> None:
        proc, self._process = self._process, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        handle, self._log_handle = self._log_handle, None
        if handle is not None:
            try:
                handle.close()  # type: ignore[attr-defined]
            except Exception:
                pass
