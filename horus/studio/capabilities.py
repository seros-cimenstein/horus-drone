"""Per-feature requirement checks for every HORUS simulation, hardware, agent, and phone capability."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from horus.bootstrap import _check_mujoco_gl, check_assets
from horus.grounder.backends import resolve_cloud_vlm_config
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


def _detect_gta_mod_installed() -> tuple[bool, str | None]:
    """Check whether HorusGta.dll is installed in a real GTA V game directory."""
    env_dir = os.environ.get("HORUS_GTA_DIR", "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    if sys.platform == "win32":
        candidates.extend([
            Path(r"C:\Program Files\Rockstar Games\Grand Theft Auto V"),
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V"),
            Path(r"C:\Program Files\Epic Games\GTAV"),
        ])
    for folder in candidates:
        if (folder / "HorusGta.dll").is_file():
            return True, str(folder)
    return False, None


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
        {
            "name": "MuJoCo offscreen GL",
            "ok": bool(gl["ok"]),
            "detail": gl["detail"],
            "fix_hint": "Install `mujoco` and OpenGL/EGL/OSMesa graphics drivers for headless 3D rendering.",
        },
        {
            "name": "ArduCopter SITL binary",
            "ok": assets["sitl"].available,
            "detail": assets["sitl"].detail,
            "fix_hint": "Click Download SITL & Models above or run `horus setup` in your terminal.",
        },
        {
            "name": "YOLOv8n ONNX model",
            "ok": assets["yolov8n"].available,
            "detail": assets["yolov8n"].detail,
            "fix_hint": "Click Download SITL & Models above or run `horus setup` (or set HORUS_YOLO_MODEL).",
        },
        {
            "name": "OSNet ReID ONNX model",
            "ok": assets["reid"].available,
            "optional": True,
            "detail": assets["reid"].detail,
            "fix_hint": "Optional for stronger person re-identification (automatically falls back to color_histogram). Run `horus setup`.",
        },
    ]
    builtin_cap = {
        "id": "builtin_3d",
        "title": "Built-in 3D World",
        "tier": "core",
        "ready": bool(gl["ok"]) and assets["sitl"].available and assets["yolov8n"].available,
        "summary": (
            "Ready to launch"
            if (bool(gl["ok"]) and assets["sitl"].available and assets["yolov8n"].available)
            else "Download SITL & models or verify GL drivers"
        ),
        "checks": b3d_checks,
    }

    # 2. Gazebo Harmonic (optional feature, but Docker + image are required to run it locally)
    docker_bin = shutil.which("docker")
    run_sh = (repo_root / "sim" / "run.sh").is_file()
    sim_image_ok = False
    sim_image_detail = "Requires Docker Engine first"
    if docker_bin and probe_external_tools:
        rc, out = _run_quick(
            [docker_bin, "image", "inspect", os.environ.get("HORUS_SIM_IMAGE", "horus-sim:jammy-harmonic-copter-4.7.1-slim"), "--format", "{{.Id}}"],
            timeout_s=2.0,
        )
        sim_image_ok = rc == 0 and bool(out)
        sim_image_detail = "horus-sim image present" if sim_image_ok else "horus-sim Docker image not built yet"
    elif docker_bin:
        sim_image_detail = "Docker present (image check skipped)"

    koko_configured = _ssh_has_koko()
    gazebo_checks = [
        {
            "name": "sim/run.sh launcher",
            "ok": run_sh,
            "detail": "Found sim/run.sh in repo checkout" if run_sh else "Not running from a HORUS git checkout",
            "fix_hint": "Required for local Docker launch: run from the HORUS repository root, or use Attach Existing for a remote Gazebo server.",
        },
        {
            "name": "Docker Engine CLI",
            "ok": docker_bin is not None,
            "detail": docker_bin or "docker not found on PATH",
            "fix_hint": "Required to launch Gazebo locally: install Docker Engine, or use Attach Existing to connect to a remote Gazebo instance (e.g. Koko).",
        },
        {
            "name": "Gazebo Simulation Docker image",
            "ok": sim_image_ok,
            "detail": sim_image_detail,
            "fix_hint": "Required to launch Gazebo locally: build the container with `./sim/run.sh build`.",
        },
        {
            "name": "Koko SSH host",
            "ok": koko_configured,
            "optional": True,
            "detail": "Host `koko` found in ~/.ssh/config" if koko_configured else "No `koko` entry in ~/.ssh/config",
            "fix_hint": "Optional: configure SSH host `koko` if you offload Gazebo runs to a remote workstation.",
        },
    ]
    gazebo_cap = {
        "id": "gazebo",
        "title": "Gazebo Harmonic",
        "tier": "optional",
        "ready": run_sh and docker_bin is not None and sim_image_ok,
        "summary": (
            "Local Docker ready"
            if (run_sh and docker_bin and sim_image_ok)
            else (
                "Remote Koko / Attach available (Docker required for local launch)"
                if koko_configured
                else "Requires Docker + ./sim/run.sh build for local Gazebo"
            )
        ),
        "checks": gazebo_checks,
    }

    # 3. GTA V Simulation (requires external Windows GTA V install + HorusGta.dll)
    gta_tiles = (repo_root / "data" / "maps" / "gta" / "tiles" / "index.json").is_file()
    gta_gimbal_ok = _has_module("horus.gimbal.gta") and _has_module("horus.tof_bridge.gta")
    bridge_ok = gta_gimbal_ok and (assets["sitl"].available or (run_sh and docker_bin is not None))
    gta_mod_installed, gta_mod_dir = _detect_gta_mod_installed()
    if gta_mod_installed:
        gta_mod_detail = f"HorusGta.dll found in {gta_mod_dir}"
        gta_mod_hint = "GTA V mod detected."
    elif sys.platform != "win32":
        gta_mod_detail = "Cannot auto-detect GTA V on Linux: requires a Windows PC (or remote host) running GTA V with HorusGta.dll"
        gta_mod_hint = "Install HorusGta.dll and horus_capture.asi in your GTA V folder on Windows, start GTA V, then enter its host IP."
    else:
        gta_mod_detail = "HorusGta.dll not detected in standard GTA V folders"
        gta_mod_hint = "Copy HorusGta.dll and horus_capture.asi into your GTA V folder or set HORUS_GTA_DIR."

    gta_ready = bridge_ok and gta_mod_installed
    gta_checks = [
        {
            "name": "HORUS GTA bridge & SITL",
            "ok": bridge_ok,
            "detail": (
                "GTA bridge modules + SITL ready"
                if bridge_ok
                else "Missing SITL binary or Docker launcher for GTA flight link"
            ),
            "fix_hint": "Run `horus setup` (native SITL) or install Docker + `./sim/run.sh build`.",
        },
        {
            "name": "GTA V game & HorusGta.dll",
            "ok": gta_mod_installed,
            "detail": gta_mod_detail,
            "fix_hint": gta_mod_hint,
        },
        {
            "name": "Los Santos height map tiles",
            "ok": gta_tiles,
            "optional": True,
            "detail": "data/maps/gta/tiles/index.json found" if gta_tiles else "Whole-map tiles not built yet",
            "fix_hint": "Optional for full Los Santos map overlay: run `python scripts/maps/build_gta_tiles.py`.",
        },
    ]
    gta_cap = {
        "id": "gta",
        "title": "GTA V Simulation",
        "tier": "optional",
        "ready": gta_ready,
        "summary": "Ready to connect" if gta_ready else "Requires GTA V + HorusGta.dll (external game)",
        "checks": gta_checks,
    }

    # 4. Real Drone Hardware
    ports = list_serial_ports()
    has_mav = _has_module("pymavlink")
    has_ser = _has_module("serial")
    real_checks = [
        {
            "name": "pymavlink + pyserial",
            "ok": has_mav and has_ser,
            "detail": "Installed" if (has_mav and has_ser) else "Missing pymavlink/pyserial",
            "fix_hint": "Reinstall `horus-drone` dependencies.",
        },
        {
            "name": "USB / UART serial port",
            "ok": len(ports) > 0,
            "optional": True,
            "detail": (
                ", ".join(p["device"] for p in ports)
                if ports
                else "No USB flight controller plugged in (UDP and Remote Companion Board still work)"
            ),
            "fix_hint": "Plug in a Pixhawk/Cube via USB or telemetry radio, or connect over UDP / Remote Companion.",
        },
    ]
    real_cap = {
        "id": "real_drone",
        "title": "Real Drone / Companion",
        "tier": "core",
        "ready": has_mav and has_ser,
        "summary": (
            f"{len(ports)} serial port(s) detected"
            if ports
            else "Ready for UDP or Remote Companion (no USB cable detected)"
        ),
        "checks": real_checks,
    }

    # 5. Open-Vocab Grounder
    grounder_venv = (repo_root / "models" / "grounder-venv").exists()
    torch_ok = _has_module("torch") and _has_module("transformers")
    cloud_vlm = resolve_cloud_vlm_config(settings_store)
    grounder_ready = grounder_venv or torch_ok or (cloud_vlm is not None)
    grounder_checks = [
        {
            "name": "Cloud VLM Grounding (zero install)",
            "ok": cloud_vlm is not None,
            "optional": True,
            "detail": (
                f"Ready via {cloud_vlm[2]}"
                if cloud_vlm
                else "No GEMINI_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY set"
            ),
            "fix_hint": "Add a Gemini, OpenAI, or OpenRouter key under Settings -> AI & Vision for zero-install open-vocab grounding.",
        },
        {
            "name": "Local PyTorch runtime (offline)",
            "ok": grounder_venv or torch_ok,
            "optional": True,
            "detail": (
                "models/grounder-venv present"
                if grounder_venv
                else ("torch + transformers installed" if torch_ok else "Local PyTorch weights not installed")
            ),
            "fix_hint": "For offline open-vocab grounding without cloud APIs: `pip install -e \".[grounder]\"`.",
        },
        {
            "name": "Grounder Service URL",
            "ok": bool(settings.grounder_url),
            "optional": True,
            "detail": settings.grounder_url or "Disabled",
            "fix_hint": "Set the Grounder service URL under Settings -> AI & Vision.",
        },
    ]
    grounder_summary = (
        "Local PyTorch ready"
        if (grounder_venv or torch_ok)
        else (
            f"Cloud VLM ready ({cloud_vlm[2]})"
            if cloud_vlm
            else "Choose a Cloud VLM key OR install local PyTorch (`horus-drone[grounder]`)"
        )
    )
    grounder_cap = {
        "id": "grounder",
        "title": "Open-Vocab Grounder",
        "tier": "optional",
        "ready": grounder_ready,
        "summary": grounder_summary,
        "checks": grounder_checks,
    }

    # 6. Agents & AI Models: only the active AI provider connection is required; individual CLIs/keys are optional choices
    omp_bin = shutil.which(settings.omp_command or "omp") or shutil.which("omp")
    pi_bin = shutil.which("pi")
    claude_bin = shutil.which(settings.claude_command or "claude") or shutil.which("claude")
    codex_bin = shutil.which("codex")
    any_key = any(
        ai_view["secrets_configured"].get(k)
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CUSTOM_API_KEY")
    )
    ai_configured = bool(ai_view["ai_status"]["configured"])
    agent_checks = [
        {
            "name": "Active AI Pilot Provider",
            "ok": ai_configured,
            "detail": (
                ai_view["ai_status"]["detail"]
                if ai_configured
                else "No AI provider connected yet (manual flight still works without AI)"
            ),
            "fix_hint": "Pick any one provider in Settings -> AI & Vision: add an API key (OpenAI, Anthropic, Gemini, OpenRouter), connect Ollama, or choose an installed CLI.",
        },
        {
            "name": "Cloud API Key (OpenAI / Anthropic / Gemini / OpenRouter)",
            "ok": any_key,
            "optional": True,
            "detail": "At least one API key stored" if any_key else "Not set (optional if using a local CLI or Ollama)",
            "fix_hint": "Optional: paste a cloud API key in Settings -> AI & Vision.",
        },
        {
            "name": "Claude Code CLI",
            "ok": bool(claude_bin),
            "optional": True,
            "detail": claude_bin or "Not installed (optional provider)",
            "fix_hint": "Optional: install Claude Code CLI (`npm i -g @anthropic-ai/claude-code`).",
        },
        {
            "name": "OMP / Pi Agent CLI",
            "ok": bool(omp_bin or pi_bin),
            "optional": True,
            "detail": f"omp={omp_bin or 'not installed'}, pi={pi_bin or 'not installed'}",
            "fix_hint": "Optional: install omp or pi on PATH if you want to fly using an OMP/Pi CLI agent.",
        },
        {
            "name": "Codex CLI (MCP client)",
            "ok": bool(codex_bin),
            "optional": True,
            "detail": codex_bin or "Not installed (optional external MCP client)",
            "fix_hint": "Optional: install Codex CLI to connect externally via `codex mcp add`.",
        },
    ]
    agents_cap = {
        "id": "agents",
        "title": "AI Models & Agents",
        "tier": "core",
        "ready": ai_configured,
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
        {
            "name": "Local Wi-Fi LAN HTTPS (--lan)",
            "ok": True,
            "optional": True,
            "detail": "Built-in self-signed TLS via `horus studio --phone --lan` (no Tailscale required)",
            "fix_hint": "Run `horus studio --phone --lan` for Wi-Fi PWA access without Tailscale.",
        },
        {
            "name": "Tailscale CLI",
            "ok": tailscale_bin is not None,
            "optional": True,
            "detail": tailscale_bin or "Not installed (only needed for remote cellular access)",
            "fix_hint": "Optional: install Tailscale for HTTPS phone access outside your local Wi-Fi.",
        },
        {
            "name": "Tailscale HTTPS Certs",
            "ok": ts_certs,
            "optional": True,
            "detail": f"Tailnet DNS: {ts_dns}" if ts_dns else "Tailnet HTTPS certificates not active",
            "fix_hint": "Optional: enable MagicDNS + HTTPS Certificates in your Tailscale admin console.",
        },
        {
            "name": "phone_serve.sh helper",
            "ok": phone_script,
            "optional": True,
            "detail": "Found scripts/phone_serve.sh" if phone_script else "Direct ?view=phone URL still works",
            "fix_hint": "Optional: run from the HORUS repo checkout to use the Tailscale serve wrapper.",
        },
    ]
    phone_cap = {
        "id": "phone_pwa",
        "title": "Phone Companion PWA",
        "tier": "optional",
        "ready": bool(tailscale_bin and ts_certs),
        "summary": (
            f"Tailscale HTTPS ready ({ts_dns})"
            if (tailscale_bin and ts_certs and ts_dns)
            else "Local Wi-Fi QR & `--lan` HTTPS ready; Tailscale is optional for remote cellular access"
        ),
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
