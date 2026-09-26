#!/usr/bin/env python3
"""Arm ArduCopter in Guided, climb to 5 m, report telemetry, and land."""

from __future__ import annotations

import os
import sys
import time

from pymavlink import mavutil

TARGET_ALTITUDE_M = 5.0
ALTITUDE_TOLERANCE_M = 0.5
HEARTBEAT_TIMEOUT_S = 60.0
TAKEOFF_TIMEOUT_S = 90.0
LAND_TIMEOUT_S = 120.0
NAVIGATION_TIMEOUT_S = 60.0
POSITION_INTERVAL_US = 200_000
READINESS_INTERVAL_US = 500_000
PREARM_TIMEOUT_S = 60.0
PREARM_CHECK_RESPONSE_S = 1.0
PREARM_CHECK_INTERVAL_S = 1.0


def wait_heartbeat(master: mavutil.mavlink_connection, timeout_s: float) -> object:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        heartbeat = master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if heartbeat is not None:
            return heartbeat
    raise TimeoutError(f"No MAVLink heartbeat within {timeout_s:.0f} seconds")


def set_mode(master: mavutil.mavlink_connection, name: str, timeout_s: float = 15.0) -> None:
    mapping = master.mode_mapping()
    if not mapping or name not in mapping:
        raise RuntimeError(f"Autopilot does not advertise mode {name!r}")
    expected = mapping[name]
    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        expected,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        heartbeat = master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if heartbeat is not None and heartbeat.custom_mode == expected:
            print(f"Mode confirmed: {name}", flush=True)
            return
    raise TimeoutError(f"Autopilot did not enter {name} within {timeout_s:.0f} seconds")


def send_command(master: mavutil.mavlink_connection, command: int, *params: float) -> None:
    padded = list(params) + [0.0] * (7 - len(params))
    master.mav.command_long_send(
        master.target_system,
        master.target_component,
        command,
        0,
        *padded[:7],
    )


def wait_for_navigation_ready(
    master: mavutil.mavlink_connection, timeout_s: float
) -> None:
    # MAVLink estimator flags: attitude and absolute horizontal/vertical position.
    required_ekf_flags = 0x01 | 0x10 | 0x20
    gps_fix_type = 0
    ekf_flags = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = master.recv_match(blocking=True, timeout=1)
        if message is None:
            continue
        if message.get_type() == "GPS_RAW_INT":
            gps_fix_type = message.fix_type
        elif message.get_type() == "EKF_STATUS_REPORT":
            ekf_flags = message.flags
        if (
            gps_fix_type >= 3
            and (ekf_flags & required_ekf_flags) == required_ekf_flags
        ):
            print("Navigation ready: 3D GPS fix and EKF position", flush=True)
            return
    raise TimeoutError(
        f"GPS/EKF navigation did not become ready within {timeout_s:.0f} seconds"
    )


def wait_for_prearm_ready(
    master: mavutil.mavlink_connection, timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    last_reason = ""
    while time.monotonic() < deadline:
        send_command(master, mavutil.mavlink.MAV_CMD_RUN_PREARM_CHECKS)
        response_deadline = min(
            time.monotonic() + PREARM_CHECK_RESPONSE_S, deadline
        )
        ack_result = None
        prearm_reason = ""
        while time.monotonic() < response_deadline:
            message = master.recv_match(
                blocking=True, timeout=response_deadline - time.monotonic()
            )
            if message is None:
                continue
            if (
                message.get_type() == "COMMAND_ACK"
                and message.command == mavutil.mavlink.MAV_CMD_RUN_PREARM_CHECKS
            ):
                ack_result = message.result
            elif message.get_type() == "STATUSTEXT":
                status = str(message.text).rstrip("\x00")
                if status.startswith("PreArm:"):
                    prearm_reason = status
        if ack_result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
            raise RuntimeError(
                f"Pre-arm check command returned result {ack_result!r}"
            )
        if not prearm_reason:
            print("Pre-arm checks passed", flush=True)
            return
        if prearm_reason != last_reason:
            print(f"Waiting for pre-arm readiness: {prearm_reason}", flush=True)
            last_reason = prearm_reason
        time.sleep(
            min(PREARM_CHECK_INTERVAL_S, max(0.0, deadline - time.monotonic()))
        )
    raise TimeoutError(
        f"ArduPilot pre-arm checks did not pass within {timeout_s:.0f} seconds"
    )


def wait_armed(
    master: mavutil.mavlink_connection, armed: bool, timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        heartbeat = master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if heartbeat is None:
            continue
        is_armed = bool(
            heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        )
        if is_armed == armed:
            print("Motors armed" if armed else "Motors disarmed", flush=True)
            return
    state = "armed" if armed else "disarmed"
    raise TimeoutError(f"Autopilot did not become {state} within {timeout_s:.0f} seconds")


def takeoff_altitude(master: mavutil.mavlink_connection) -> float:
    deadline = time.monotonic() + TAKEOFF_TIMEOUT_S
    in_band_samples = 0
    latest_altitude = 0.0
    last_sample_print = 0.0
    while time.monotonic() < deadline:
        position = master.recv_match(
            type="GLOBAL_POSITION_INT", blocking=True, timeout=2
        )
        if position is None:
            continue
        latest_altitude = position.relative_alt / 1000.0
        now = time.monotonic()
        in_band = abs(latest_altitude - TARGET_ALTITUDE_M) <= ALTITUDE_TOLERANCE_M
        if now - last_sample_print >= 0.5 or in_band:
            print(f"S0 ALTITUDE SAMPLE: relative_alt={latest_altitude:.2f} m", flush=True)
            last_sample_print = now
        if in_band:
            in_band_samples += 1
            if in_band_samples >= 3:
                return latest_altitude
        else:
            in_band_samples = 0
    raise TimeoutError(
        f"Did not reach {TARGET_ALTITUDE_M:.1f} m; last relative altitude "
        f"was {latest_altitude:.2f} m"
    )


def land(master: mavutil.mavlink_connection) -> None:
    set_mode(master, "LAND")
    wait_armed(master, False, LAND_TIMEOUT_S)




def connect_to_mavlink(connection: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return mavutil.mavlink_connection(connection, autoreconnect=True)
        except ConnectionRefusedError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"MAVLink endpoint {connection} did not accept a connection "
                    f"within {timeout_s:.0f} seconds"
                ) from exc
            time.sleep(min(1.0, remaining))
def main() -> int:
    connection = os.environ.get("HORUS_MAVLINK_CONNECTION", "tcp:127.0.0.1:5760")
    armed = False
    master = None
    try:
        print(f"Connecting to MAVLink at {connection}", flush=True)
        master = connect_to_mavlink(connection, HEARTBEAT_TIMEOUT_S)
        wait_heartbeat(master, HEARTBEAT_TIMEOUT_S)
        print(
            f"Heartbeat from system {master.target_system}, "
            f"component {master.target_component}",
            flush=True,
        )
        message_intervals = (
            (
                mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT,
                POSITION_INTERVAL_US,
            ),
            (mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, READINESS_INTERVAL_US),
            (mavutil.mavlink.MAVLINK_MSG_ID_EKF_STATUS_REPORT, READINESS_INTERVAL_US),
        )
        for message_id, interval_us in message_intervals:
            send_command(
                master,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                message_id,
                interval_us,
            )
        wait_for_navigation_ready(master, NAVIGATION_TIMEOUT_S)

        set_mode(master, "GUIDED")
        wait_for_prearm_ready(master, PREARM_TIMEOUT_S)
        armed = True
        send_command(
            master,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            1,
        )
        wait_armed(master, True, 30.0)

        send_command(
            master,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0,
            0,
            0,
            0,
            0,
            0,
            TARGET_ALTITUDE_M,
        )
        altitude = takeoff_altitude(master)
        print(
            f"S0 TAKEOFF OK: GLOBAL_POSITION_INT.relative_alt="
            f"{altitude:.2f} m (target {TARGET_ALTITUDE_M:.1f} m)",
            flush=True,
        )

        land(master)
        armed = False
        print("S0 LAND OK: vehicle is disarmed", flush=True)
        return 0
    except Exception:
        if armed and master is not None:
            print("Flight proof failed; requesting LAND mode before exiting.", file=sys.stderr)
            try:
                set_mode(master, "LAND", timeout_s=5.0)
            except Exception as exc:  # best effort after the primary failure
                print(f"LAND mode request also failed: {exc}", file=sys.stderr)
        raise
    finally:
        if master is not None:
            master.close()


if __name__ == "__main__":
    raise SystemExit(main())
