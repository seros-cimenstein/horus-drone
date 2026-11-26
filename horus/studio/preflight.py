"""Hardware serial discovery and structured pre-flight readiness checklist."""

from __future__ import annotations

from typing import Any, Mapping


def list_serial_ports() -> list[dict[str, str]]:
    """Return available USB/UART serial ports across Windows and Linux."""
    try:
        from serial.tools import list_ports

        ports = []
        for port in list_ports.comports():
            ports.append({
                "device": str(port.device),
                "description": str(port.description or ""),
                "hwid": str(port.hwid or ""),
            })
        return ports
    except Exception:
        return []


def run_preflight(
    state_payload: Mapping[str, Any] | None,
    *,
    ai_status: Mapping[str, Any] | None = None,
    session_mode: str = "idle",
) -> list[dict[str, str]]:
    """Build a structured pass/warn/fail pre-flight checklist from live drone telemetry."""
    items: list[dict[str, str]] = []
    if not state_payload:
        items.append({
            "id": "mavlink",
            "label": "Drone link & telemetry",
            "status": "fail" if session_mode != "idle" else "warn",
            "detail": "No active drone telemetry yet",
        })
        items.append({
            "id": "ai",
            "label": "AI model backend",
            "status": "pass" if (ai_status or {}).get("configured") else "warn",
            "detail": str((ai_status or {}).get("detail") or "Configure AI provider in Settings"),
        })
        return items

    vehicle = state_payload.get("vehicle") or {}
    age = vehicle.get("observation_age_s")
    fresh = isinstance(age, (int, float)) and age <= 1.5
    items.append({
        "id": "telemetry",
        "label": "Telemetry freshness",
        "status": "pass" if fresh else "fail",
        "detail": f"Age {age:.2f}s" if isinstance(age, (int, float)) else "No pose timestamp",
    })

    ekf_ok = bool(vehicle.get("ekf_ok", True)) and vehicle.get("pose") is not None
    items.append({
        "id": "ekf",
        "label": "EKF3 local position",
        "status": "pass" if ekf_ok else "fail",
        "detail": "Local NED pose active" if ekf_ok else "Waiting for EKF3 convergence",
    })

    battery = vehicle.get("battery_pct")
    if isinstance(battery, (int, float)):
        bat_status = "pass" if battery >= 25 else "warn" if battery >= 15 else "fail"
        bat_detail = f"{battery:.0f}%"
    else:
        bat_status = "warn"
        bat_detail = "Battery telemetry unavailable"
    items.append({"id": "battery", "label": "Battery level", "status": bat_status, "detail": bat_detail})

    rng_m = vehicle.get("ground_range_m")
    items.append({
        "id": "rangefinder",
        "label": "Downward rangefinder",
        "status": "pass" if isinstance(rng_m, (int, float)) else "warn",
        "detail": f"{rng_m:.2f} m" if isinstance(rng_m, (int, float)) else "No downward ToF sample",
    })

    prox = vehicle.get("proximity") or state_payload.get("proximity")
    items.append({
        "id": "tof",
        "label": "ToF proximity ring",
        "status": "pass" if prox else "warn",
        "detail": "Proximity sectors active" if prox else "No horizontal ToF sectors reported",
    })

    perception = state_payload.get("perception") or {}
    cam_state = str(perception.get("camera") or "disabled")
    cam_status = "pass" if cam_state == "streaming" else "warn" if cam_state in {"disabled", "connecting"} else "fail"
    items.append({
        "id": "camera",
        "label": "Camera vision stream",
        "status": cam_status,
        "detail": f"{cam_state}" + (f" ({perception['reason']})" if perception.get("reason") else ""),
    })

    pilot_override = bool(vehicle.get("pilot_override"))
    items.append({
        "id": "rc_override",
        "label": "RC pilot override",
        "status": "warn" if pilot_override else "pass",
        "detail": "PILOT override active (RC sticks control vehicle)" if pilot_override else "GUIDED authority clear",
    })

    items.append({
        "id": "ai",
        "label": "AI model backend",
        "status": "pass" if (ai_status or {}).get("configured") else "warn",
        "detail": str((ai_status or {}).get("detail") or "Optional for manual flight; required for AI missions"),
    })
    return items
