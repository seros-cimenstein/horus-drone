"""Per-feature requirement checks for every HORUS simulation, hardware, agent, and phone capability."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from horus.bootstrap import _check_mujoco_gl, check_assets
from horus.studio.preflight import list_serial_ports
from horus.studio.settings import StudioSettingsStore


def _run_quick(argv: list[str], timeout_s: float = 1.5) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _ssh_has_koko() -> bool:
    ssh_cfg = Path.home() / ".ssh" / "config"
    if not ssh_cfg.is_file():
        return False
    try:
        for line in ssh_cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("host ") and "koko" in stripped.split()[1:]:
                return True
    except OSError:
        return False
    return False


def check_capabilities(
    settings_store: StudioSettingsStore,
    *,
    cache_dir: Path | None = None,
    probe_external_tools: bool = True,
) -> list[dict[str, Any]]:
    """Evaluate readiness and requirement checks for all HORUS features."""
    repo_root = Path.cwd()
    assets = check_assets(cache_dir)
    settings = settings_store.load_settings()
    ai_view = settings_store.public_view()

    # 1. Built-in 3D World
    gl = _check_mujoco_gl()
    b3d_checks = [
        {"name": "MuJoCo offscreen GL", "ok": bool(gl["ok"]), "detail": gl["detail"], "fix_hint": "Install `mujoco` and OpenGL/EGL/OSMesa drivers."},
        {"name": "ArduCopter SITL binary", "ok": assets["sitl"].available, "detail": assets["sitl"].detail, "fix_hint": "Click Download SITL & Models or run `horus setup`."},
        {"name": "YOLOv8n ONNX model", "ok": assets["yolov8n"].available, "detail": assets["yolov8n"].detail, "fix_hint": "Run `horus setup` or set HORUS_YOLO_MODEL."},
        {"name": "OSNet ReID ONNX model", "ok": assets["reid"].available, "optional": True, "detail": assets["reid"].detail, "fix_hint": "Optional for enhanced person ReID (falls back to color_histogram). Run `horus setup` or set HORUS_REID_MODEL."},
    ]
    builtin_cap = {
        "id": "builtin_3d",
        "title": "Built-in 3D World",
        "tier": "core",
        "ready": bool(gl["ok"]) and assets["sitl"].available,
        "summary": "Ready to launch" if (gl["ok"] and assets["sitl"].available) else "Needs SITL / GL setup",
        "checks": b3d_checks,
    }

    # 2. Gazebo Harmonic
    docker_bin = shutil.which("docker")
    run_sh = (repo_root / "sim" / "run.sh").is_file()
    sim_image_ok = False
    sim_image_detail = "docker CLI not installed"
    if docker_bin and probe_external_tools:
        rc, out = _run_quick([docker_bin, "image", "inspect", os.environ.get("HORUS_SIM_IMAGE", "horus-sim:jammy-harmonic-copter-4.7.1-slim"), "--format", "{{.Id}}"], timeout_s=2.0)
        sim_image_ok = rc == 0 and bool(out)
        sim_image_detail = "horus-sim image present" if sim_image_ok else "horus-sim image not built (`./sim/run.sh build`)"
    elif docker_bin:
        sim_image_detail = "docker present (image check skipped)"

    koko_configured = _ssh_has_koko()
    gazebo_checks = [
        {"name": "sim/run.sh launcher", "ok": run_sh, "detail": "Found sim/run.sh" if run_sh else "Not running from HORUS repo checkout", "fix_hint": "Run HORUS from the repository checkout or attach to a remote Gazebo Drone board."},
        {"name": "Docker CLI", "ok": docker_bin is not None, "detail": docker_bin or "docker not found on PATH", "fix_hint": "Install Docker Engine or run Gazebo remotely on Koko."},
        {"name": "Simulation Docker image", "ok": sim_image_ok, "detail": sim_image_detail, "fix_hint": "Run `./sim/run.sh build`."},
        {"name": "Koko SSH host", "ok": koko_configured, "optional": True, "detail": "Host `koko` in ~/.ssh/config" if koko_configured else "No `koko` entry in ~/.ssh/config", "fix_hint": "Optional: configure SSH host `koko` for remote Gazebo runs."},
    ]
    gazebo_cap = {
        "id": "gazebo",
        "title": "Gazebo Harmonic",
        "tier": "optional",
        "ready": run_sh and docker_bin is not None and sim_image_ok,
        "summary": "Local Docker ready" if (run_sh and docker_bin and sim_image_ok) else ("Remote Koko / Attach available" if koko_configured else "Optional — Docker image or checkout not installed"),
        "checks": gazebo_checks,
    }

    # 3. GTA V Integration
    gta_mod = (repo_root / "gta" / "horus_mod").is_dir()
    gta_tiles = (repo_root / "data" / "maps" / "gta" / "tiles" / "index.json").is_file()
    gta_gimbal_ok = _has_module("horus.gimbal.gta") and _has_module("horus.tof_bridge.gta")
    gta_ready = gta_gimbal_ok and (assets["sitl"].available or (run_sh and docker_bin is not None))
    gta_checks = [
        {"name": "GTA gimbal & ToF modules", "ok": gta_gimbal_ok, "detail": "horus.gimbal.gta + horus.tof_bridge.gta ready" if gta_gimbal_ok else "GTA bridge modules missing", "fix_hint": "Reinstall horus-drone."},
        {"name": "SITL / Container launcher", "ok": assets["sitl"].available or (run_sh and docker_bin is not None), "detail": "Native SITL or Docker sim/run.sh available" if (assets["sitl"].available or (run_sh and docker_bin)) else "Neither native SITL nor Docker sim/run.sh found", "fix_hint": "Run `horus setup` (Windows native) or install Docker + `./sim/run.sh build` (Linux)."},
        {"name": "GTA whole-map height tiles", "ok": gta_tiles, "optional": True, "detail": "data/maps/gta/tiles/index.json found" if gta_tiles else "Whole-map tiles not built yet", "fix_hint": "Optional: run `scripts/maps/build_gta_tiles.py` for full Los Santos map."},
        {"name": "GTA mod source (gta/horus_mod)", "ok": gta_mod, "optional": True, "detail": "Found gta/horus_mod" if gta_mod else "Not in repo checkout", "fix_hint": "Install HorusGta.dll & horus_capture.asi inside your GTA V directory."},
    ]
    gta_cap = {
        "id": "gta",
        "title": "GTA V Simulation",
        "tier": "optional",
        "ready": gta_ready,
        "summary": "Ready (launch or attach)" if gta_ready else "Optional — needs SITL or Docker sim/run.sh",
        "checks": gta_checks,
    }

    # 4. Real Drone Hardware
    ports = list_serial_ports()
    has_mav = _has_module("pymavlink")
    has_ser = _has_module("serial")
    real_checks = [
        {"name": "pymavlink + pyserial", "ok": has_mav and has_ser, "detail": "Installed" if (has_mav and has_ser) else "Missing pymavlink/pyserial", "fix_hint": "Install `horus-drone`."},
        {"name": "Serial ports detected", "ok": len(ports) > 0, "optional": True, "detail": ", ".join(p["device"] for p in ports) if ports else "No USB/UART serial ports plugged in (UDP/Remote MCP still works)", "fix_hint": "Plug in Pixhawk/Cube USB or telemetry radio, or use UDP/Remote Companion."},
    ]
    real_cap = {
        "id": "real_drone",
        "title": "Real Drone / Companion",
        "tier": "core",
        "ready": has_mav and has_ser,
        "summary": f"{len(ports)} serial port(s) detected" if ports else "UDP / Remote MCP ready (no serial plugged in)",
        "checks": real_checks,
    }

    # 5. Open-Vocab Grounder
    grounder_venv = (repo_root / "models" / "grounder-venv").exists()
    torch_ok = _has_module("torch") and _has_module("transformers")
    grounder_checks = [
        {"name": "Grounder runtime (venv or torch)", "ok": grounder_venv or torch_ok, "optional": True, "detail": "models/grounder-venv present" if grounder_venv else ("torch + transformers installed" if torch_ok else "Optional Grounder dependencies not installed"), "fix_hint": "Install `horus-drone[grounder]` or set up `models/grounder-venv`."},
        {"name": "Configured Grounder URL", "ok": bool(settings.grounder_url), "optional": True, "detail": settings.grounder_url or "Disabled", "fix_hint": "Set Grounder URL under Flight & Vision settings."},
    ]
    grounder_cap = {
        "id": "grounder",
        "title": "Open-Vocab Grounder",
        "tier": "optional",
        "ready": grounder_venv or torch_ok,
        "summary": "Installed" if (grounder_venv or torch_ok) else "Optional (install horus-drone[grounder])",
        "checks": grounder_checks,
    }

    # 6. Agents & AI Models (omp, pi, claude, codex, API keys)
    omp_bin = shutil.which(settings.omp_command or "omp") or shutil.which("omp")
    pi_bin = shutil.which("pi")
    claude_bin = shutil.which(settings.claude_command or "claude") or shutil.which("claude")
    codex_bin = shutil.which("codex")
    any_key = any(ai_view["secrets_configured"].get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CUSTOM_API_KEY"))
    ai_configured = bool(ai_view["ai_status"]["configured"])
    agent_checks = [
        {"name": "AI Provider / Key", "ok": ai_configured, "detail": ai_view["ai_status"]["detail"], "fix_hint": "Add an API key or pick an installed CLI in Settings -> AI & Agents."},
        {"name": "OMP / Pi Agent CLI", "ok": bool(omp_bin or pi_bin), "optional": ai_configured, "detail": f"omp={omp_bin or 'missing'}, pi={pi_bin or 'missing'}", "fix_hint": "Install omp/pi on PATH or set custom binary path in AI & Agents."},
        {"name": "Claude Code CLI", "ok": bool(claude_bin), "optional": ai_configured, "detail": claude_bin or "claude not found on PATH", "fix_hint": "Install Claude Code CLI (`npm i -g @anthropic-ai/claude-code`)."},
        {"name": "Codex CLI (MCP client)", "ok": bool(codex_bin), "optional": True, "detail": codex_bin or "codex not found on PATH", "fix_hint": "Optional: install Codex CLI to connect directly via `codex mcp add`."},
        {"name": "Cloud API Key stored", "ok": any_key, "optional": ai_configured, "detail": "At least one API key configured" if any_key else "No API keys saved yet", "fix_hint": "Paste OpenAI/Anthropic/Gemini/OpenRouter key in AI & Agents."},
    ]
    agents_cap = {
        "id": "agents",
        "title": "AI Models & Agents",
        "tier": "core",
        "ready": bool(ai_configured or omp_bin or pi_bin or claude_bin or codex_bin),
        "summary": ai_view["ai_status"]["detail"],
        "checks": agent_checks,
    }

    # 7. Phone PWA & Tailscale
    tailscale_bin = shutil.which("tailscale")
    phone_script = (repo_root / "scripts" / "phone_serve.sh").is_file()
    ts_dns = ""
    ts_certs = False
    if tailscale_bin and probe_external_tools:
        rc, out = _run_quick([tailscale_bin, "status", "--json"], timeout_s=1.5)
        if rc == 0 and out:
            try:
                st = json.loads(out)
                ts_dns = str((st.get("Self") or {}).get("DNSName", "")).rstrip(".")
                ts_certs = bool(st.get("CertDomains"))
            except json.JSONDecodeError:
                pass
    phone_checks = [
        {"name": "Tailscale CLI", "ok": tailscale_bin is not None, "optional": True, "detail": tailscale_bin or "tailscale not installed", "fix_hint": "Install Tailscale for HTTPS phone access over your tailnet."},
        {"name": "Tailscale HTTPS Certs", "ok": ts_certs, "optional": True, "detail": f"Tailnet DNS: {ts_dns}" if ts_dns else "Tailnet HTTPS certificates not active", "fix_hint": "Enable MagicDNS + HTTPS Certificates in Tailscale admin console."},
        {"name": "phone_serve.sh helper", "ok": phone_script, "optional": True, "detail": "Found scripts/phone_serve.sh" if phone_script else "Local ?view=phone URL still works directly", "fix_hint": "Run from repo checkout to use tailscale serve wrapper."},
    ]
    phone_cap = {
        "id": "phone_pwa",
        "title": "Phone Companion PWA",
        "tier": "optional",
        "ready": bool(tailscale_bin and ts_certs),
        "summary": f"HTTPS ready ({ts_dns})" if (tailscale_bin and ts_certs and ts_dns) else "Optional — Local QR ready; Tailscale HTTPS optional",
        "checks": phone_checks,
    }

    return [
        builtin_cap,
        gazebo_cap,
        gta_cap,
        real_cap,
        grounder_cap,
        agents_cap,
        phone_cap,
    ]
