"""F450 quadrotor physics + ArduPilot JSON SITL UDP lockstep over MuJoCo."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import os
import socket
import struct
import threading
import time
from typing import Iterator, Sequence

import mujoco
import numpy as np

from horus.world.scenes import SceneBundle

MAGIC_16 = 18458
MAGIC_32 = 29569
GRAVITY = 9.80665
ACCEL_RANGE_MS2 = 16.0 * GRAVITY
GYRO_RANGE_RAD_S = 2000.0 * math.pi / 180.0

# ArduPilot quad X motor layout:
# 1: front-right (CCW), 2: back-left (CCW), 3: front-left (CW), 4: back-right (CW).
_MOTOR_RIGHT = (1.0, -1.0, -1.0, 1.0)
_MOTOR_FORWARD = (1.0, -1.0, 1.0, -1.0)
_MOTOR_YAW = (1.0, 1.0, -1.0, -1.0)


@dataclass(frozen=True, slots=True)
class ServoFrame:
    """Decoded ArduPilot SITL JSON-backend binary servo packet."""

    frame_rate: int
    frame_count: int
    pwm: tuple[int, ...]


def decode_servo_frame(data: bytes) -> ServoFrame | None:
    """Decode an ArduPilot UDP 9002 binary servo packet; return None when invalid."""
    if len(data) < 8:
        return None
    magic, frame_rate, frame_count = struct.unpack_from("<HHI", data, 0)
    channels = 16 if magic == MAGIC_16 else 32 if magic == MAGIC_32 else 0
    if channels == 0 or len(data) != 8 + 2 * channels or frame_rate == 0:
        return None
    pwm = struct.unpack_from(f"<{channels}H", data, 8)
    return ServoFrame(frame_rate=frame_rate, frame_count=frame_count, pwm=pwm)


def pwm_to_throttle(pwm: int) -> float:
    """Map ArduPilot 1000..2000 us motor PWM to normalized throttle 0..1."""
    return max(0.0, min(1.0, (pwm - 1000) / 1000.0))


@dataclass(slots=True)
class Airframe:
    """F450-class quadrotor propulsion and airframe model (matching QuadPhysics.cs)."""

    mass_kg: float = 1.5
    prop_diameter_in: float = 10.0
    prop_pitch_in: float = 4.5
    prop_blades: int = 2
    motor_kv: float = 920.0
    battery_cells: int = 4
    motor_time_constant_s: float = 0.04
    yaw_torque_per_thrust_m: float = 0.016
    radius_m: float = 0.12

    @property
    def hub_offset_m(self) -> float:
        return 0.0156 * self.prop_diameter_in

    @property
    def max_rpm(self) -> float:
        d, p, b, kv = self.prop_diameter_in, self.prop_pitch_in, self.prop_blades, self.motor_kv
        k = 5.74345e-15 * (d ** 3.39007) * (p ** 1.38378) * (b ** 0.654997) * (kv ** 3)
        io, rm, n = 0.45, 0.1, self.battery_cells
        v = n * 3.7
        rc = max(120.0 / (2 ** n), 5.0) / 1000.0
        r = n * rc + rm
        a = k * r * r
        b_coef = -(2.0 * k * v * r + 1.0)
        c = k * v * v + io
        disc = max(b_coef * b_coef - 4.0 * a * c, 0.0)
        current = (-b_coef - math.sqrt(disc)) / (2.0 * a) if a > 1e-12 else c / -b_coef
        vm = v - n * rc * current - current * rm
        rpm = max(vm, 1.0) * kv
        tip_cap = 340.0 / (math.pi * d * 0.0254) * 60.0
        return min(rpm, tip_cap)

    def thrust(self, rpm: float, axial_speed: float = 0.0) -> float:
        """One propeller's thrust (N) at ``rpm`` and axial airspeed (m/s)."""
        if rpm < 5.0:
            return 0.0
        stat = (
            7.47958e-5 * (self.prop_blades ** 4.70298)
            + 2.525e-11 * self.prop_pitch_in * (self.prop_diameter_in ** 3) * rpm * rpm
        )
        pitch_speed = rpm * self.prop_pitch_in * 0.0254 / 60.0
        inflow = max(0.0, 1.0 - max(axial_speed, 0.0) / max(1.1 * pitch_speed, 1e-6))
        return stat * inflow

    def hover_throttle(self) -> float:
        """Per-motor throttle (0..1) yielding static hover thrust."""
        lo, hi = 0.0, 1.0
        max_r = self.max_rpm
        target = self.mass_kg * GRAVITY
        for _ in range(50):
            mid = 0.5 * (lo + hi)
            if 4.0 * self.thrust(mid * max_r, 0.0) > target:
                hi = mid
            else:
                lo = mid
        return 0.5 * (lo + hi)


@dataclass(slots=True)
class QuadState:
    """NED-ready IMU + pose snapshot produced after each physics step."""

    timestamp_s: float
    pos_ned: tuple[float, float, float]
    vel_ned: tuple[float, float, float]
    gyro_frd: tuple[float, float, float]
    accel_frd: tuple[float, float, float]
    attitude_ned: tuple[float, float, float]  # roll, pitch, yaw (rad)
    motor_rpm: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    trail_enu: list[tuple[float, float, float]] = field(default_factory=list)


def _sat(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def encode_sitl_state(state: QuadState) -> bytes:
    """Serialize ``QuadState`` as the newline-delimited JSON line expected by ArduPilot SITL."""
    payload = {
        "timestamp": round(state.timestamp_s, 9),
        "imu": {
            "gyro": [
                round(_sat(state.gyro_frd[0], GYRO_RANGE_RAD_S), 9),
                round(_sat(state.gyro_frd[1], GYRO_RANGE_RAD_S), 9),
                round(_sat(state.gyro_frd[2], GYRO_RANGE_RAD_S), 9),
            ],
            "accel_body": [
                round(_sat(state.accel_frd[0], ACCEL_RANGE_MS2), 9),
                round(_sat(state.accel_frd[1], ACCEL_RANGE_MS2), 9),
                round(_sat(state.accel_frd[2], ACCEL_RANGE_MS2), 9),
            ],
        },
        "position": [round(v, 9) for v in state.pos_ned],
        "velocity": [round(v, 9) for v in state.vel_ned],
        "attitude": [round(v, 9) for v in state.attitude_ned],
    }
    return ("\n" + json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _rotmat_to_quat_wxyz(mat: np.ndarray) -> np.ndarray:
    """Convert a 3x3 proper rotation matrix into MuJoCo quaternion ``[w, x, y, z]``."""
    q = np.zeros(4, dtype=np.float64)
    mujoco.mju_mat2Quat(q, mat.reshape(9))
    return q


def _euler_ned_from_axes(right_enu: np.ndarray, forward_enu: np.ndarray, up_enu: np.ndarray) -> tuple[float, float, float]:
    """Compute NED (roll, pitch, yaw) in radians from ENU body axes (X=East, Y=North, Z=Up)."""
    # ENU (e, n, u) -> NED (n, e, -u)
    def to_ned(v: np.ndarray) -> tuple[float, float, float]:
        return float(v[1]), float(v[0]), float(-v[2])

    fn, fe, fd = to_ned(forward_enu)
    rn, re, rd = to_ned(right_enu)
    _, _, dd = to_ned(-up_enu)
    pitch = math.asin(max(-1.0, min(1.0, -fd)))
    if fn * fn + fe * fe > 1e-12:
        yaw = math.atan2(fe, fn)
        roll = math.atan2(rd, dd)
    else:
        yaw = math.atan2(-rn, re)
        roll = 0.0
    return roll, pitch, yaw


@contextmanager
def high_res_windows_timer() -> Iterator[None]:
    """Enable 1 ms timer resolution on Windows while UDP lockstep is active."""
    if os.name != "nt":
        yield
        return
    winmm = None
    try:
        import ctypes

        winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
        winmm.timeBeginPeriod(1)
    except Exception:
        winmm = None
    try:
        yield
    finally:
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass


class MuJoCoQuadWorld:
    """MuJoCo-backed 3D rigid-body & contact world driven by ArduPilot JSON SITL."""

    def __init__(self, bundle: SceneBundle, *, airframe: Airframe | None = None) -> None:
        self.bundle = bundle
        self.airframe = airframe or Airframe()
        self.model = mujoco.MjModel.from_xml_string(bundle.xml)
        self.data = mujoco.MjData(self.model)
        self.lock = threading.RLock()

        self._quad_body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "quad")
        quad_joint_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, "quad_root")
        self._qpos_adr = int(self.model.jnt_qposadr[quad_joint_id])
        self._dof_adr = int(self.model.jnt_dofadr[quad_joint_id])
        self._drone_cam_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "drone_cam")
        self._spectator_cam_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "spectator_cam")
        self._range_sensor_names = (
            "range_front",
            "range_front_right",
            "range_back_right",
            "range_back",
            "range_back_left",
            "range_front_left",
            "range_down",
        )
        self._range_sensor_adrs = tuple(
            int(self.model.sensor_adr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SENSOR, name)])
            for name in self._range_sensor_names
        )

        self._motor_rpm = [0.0, 0.0, 0.0, 0.0]
        self._max_rpm = self.airframe.max_rpm
        self._prev_vel_enu = np.zeros(3, dtype=np.float64)
        self._origin_enu = np.array(self.model.body_pos[self._quad_body_id], dtype=np.float64)
        self._gimbal_pitch_deg = -15.0
        self._gimbal_yaw_deg = 0.0
        self._spectator_preset = "follow"
        self._show_tof = True
        self._trail_enu: list[tuple[float, float, float]] = []
        self._last_trail_t = -1.0
        self.last_state = self._snapshot_state(dt=0.00125)

        mujoco.mj_forward(self.model, self.data)
        self._apply_camera_poses()
        self.last_state = self._snapshot_state(dt=0.00125)

    def set_pose(
        self,
        pos_enu: Sequence[float],
        quat_wxyz: Sequence[float] = (1.0, 0.0, 0.0, 0.0),
    ) -> None:
        """Teleport the quad to ``pos_enu`` and ``quat_wxyz`` with zero velocity."""
        with self.lock:
            self.data.qpos[self._qpos_adr : self._qpos_adr + 3] = np.asarray(pos_enu, dtype=np.float64)
            self.data.qpos[self._qpos_adr + 3 : self._qpos_adr + 7] = np.asarray(quat_wxyz, dtype=np.float64)
            self.data.qvel[self._dof_adr : self._dof_adr + 6] = 0.0
            self._prev_vel_enu = np.zeros(3, dtype=np.float64)
            mujoco.mj_forward(self.model, self.data)
            self._apply_camera_poses()
            self.last_state = self._snapshot_state(dt=0.00125)

    def set_linear_velocity(self, vel_enu: Sequence[float]) -> None:
        """Set world linear velocity (ENU m/s) for the quad freejoint."""
        with self.lock:
            self.data.qvel[self._dof_adr : self._dof_adr + 3] = np.asarray(vel_enu, dtype=np.float64)

    def set_gimbal(self, pitch_deg: float, yaw_deg: float) -> None:
        """Set horizon-locked pitch (deg) and nose-relative yaw (deg) for ``drone_cam``."""
        with self.lock:
            self._gimbal_pitch_deg = max(-90.0, min(45.0, float(pitch_deg)))
            self._gimbal_yaw_deg = max(-160.0, min(160.0, float(yaw_deg)))

    def gimbal_angles(self) -> tuple[float, float]:
        with self.lock:
            return self._gimbal_pitch_deg, self._gimbal_yaw_deg

    def set_spectator_view(self, preset: str = "follow", show_tof: bool = True) -> None:
        with self.lock:
            if preset in {"follow", "overhead", "orbit"}:
                self._spectator_preset = preset
            self._show_tof = bool(show_tof)

    def _aero_drag_enu(self, vel_enu: np.ndarray, right_u: np.ndarray, fwd_u: np.ndarray, up_u: np.ndarray) -> np.ndarray:
        speed = float(np.linalg.norm(vel_enu))
        if speed < 0.01:
            return np.zeros(3, dtype=np.float64)
        vh = vel_enu / speed
        area = (
            abs(float(np.dot(vh, up_u))) * 0.040
            + abs(float(np.dot(vh, right_u))) * 0.013
            + abs(float(np.dot(vh, fwd_u))) * 0.012
        )
        scale = (self.airframe.prop_diameter_in / 5.0) ** 2
        f_body = vh * (-0.5 * 1.184 * area * scale * speed * speed)
        dm = self.airframe.prop_diameter_in * 0.0254
        cos_a = max(-1.0, min(1.0, float(np.dot(-vh, up_u))))
        v_perp = speed * math.sqrt(max(1.0 - cos_a * cos_a, 0.0))
        mean_rpm = 0.25 * sum(self._motor_rpm)
        j = max(0.0, min(1.0, speed / max((mean_rpm / 60.0) * dm, 0.001)))
        blade_f = (self.airframe.prop_blades - 3.0) * 0.2 + 3.0
        f_rotor = vh * (-0.5 * 1.225 * (0.5 * dm * blade_f) * v_perp * v_perp * (0.024 + (0.008 - 0.024) * j) * 4.0)
        return f_body + f_rotor

    def step_throttles(self, throttles: Sequence[float], dt: float) -> QuadState:
        """Advance MuJoCo physics by ``dt`` seconds under 4 motor throttles (0..1)."""
        dt = max(1e-4, min(0.02, float(dt)))
        with self.lock:
            self.model.opt.timestep = dt
            mat = self.data.xmat[self._quad_body_id].reshape(3, 3)
            right_u = mat[:, 0]
            fwd_u = mat[:, 1]
            up_u = mat[:, 2]
            vel_enu = np.array(self.data.qvel[self._dof_adr : self._dof_adr + 3], dtype=np.float64)
            self._prev_vel_enu = vel_enu.copy()

            axial = float(np.dot(vel_enu, up_u))
            lag = min(1.0, dt / max(self.airframe.motor_time_constant_s, 1e-6))
            total_thrust = 0.0
            tau_r = 0.0
            tau_f = 0.0
            tau_d = 0.0
            arm = self.airframe.hub_offset_m
            for idx in range(4):
                thr = max(0.0, min(1.0, float(throttles[idx]) if idx < len(throttles) else 0.0))
                self._motor_rpm[idx] += (thr * self._max_rpm - self._motor_rpm[idx]) * lag
                thrust_n = self.airframe.thrust(self._motor_rpm[idx], axial)
                total_thrust += thrust_n
                tau_r += _MOTOR_FORWARD[idx] * arm * thrust_n
                tau_f += -_MOTOR_RIGHT[idx] * arm * thrust_n
                tau_d += _MOTOR_YAW[idx] * self.airframe.yaw_torque_per_thrust_m * thrust_n

            force_enu = up_u * total_thrust + self._aero_drag_enu(vel_enu, right_u, fwd_u, up_u)
            torque_body = np.array([tau_r, tau_f, -tau_d], dtype=np.float64)
            torque_enu = mat @ torque_body

            self.data.xfrc_applied[self._quad_body_id, 0:3] = force_enu
            self.data.xfrc_applied[self._quad_body_id, 3:6] = torque_enu

            mujoco.mj_step(self.model, self.data)
            self._apply_camera_poses()
            state = self._snapshot_state(dt=dt)
            self.last_state = state
            return state

    def step_servo(self, frame: ServoFrame) -> QuadState:
        """Advance one ArduPilot JSON servo step."""
        dt = 1.0 / max(1, frame.frame_rate)
        throttles = [pwm_to_throttle(frame.pwm[i]) for i in range(4)]
        return self.step_throttles(throttles, dt)

    def _apply_camera_poses(self) -> None:
        if self._drone_cam_id >= 0:
            mat_body = self.data.xmat[self._quad_body_id].reshape(3, 3)
            _, _, yaw_ned = _euler_ned_from_axes(mat_body[:, 0], mat_body[:, 1], mat_body[:, 2])
            cam_yaw_ned = yaw_ned + math.radians(self._gimbal_yaw_deg)
            pitch = math.radians(self._gimbal_pitch_deg)
            # Horizontal forward in ENU at yaw_ned (0=North +Y, pi/2=East +X)
            fwd_horiz = np.array([math.sin(cam_yaw_ned), math.cos(cam_yaw_ned), 0.0], dtype=np.float64)
            right_horiz = np.array([math.cos(cam_yaw_ned), -math.sin(cam_yaw_ned), 0.0], dtype=np.float64)
            up_world = np.array([0.0, 0.0, 1.0], dtype=np.float64)
            fwd_cam = fwd_horiz * math.cos(pitch) + up_world * math.sin(pitch)
            up_cam = -fwd_horiz * math.sin(pitch) + up_world * math.cos(pitch)
            # MuJoCo camera local axes: +X=right, +Y=up, -Z=forward -> +Z=-forward
            r_world_cam = np.column_stack((right_horiz, up_cam, -fwd_cam))
            r_body_cam = mat_body.T @ r_world_cam
            self.model.cam_quat[self._drone_cam_id] = _rotmat_to_quat_wxyz(r_body_cam)

        if self._spectator_cam_id >= 0:
            pos = self.data.xpos[self._quad_body_id]
            t = float(self.data.time)
            if self._spectator_preset == "overhead":
                eye = np.array([pos[0], pos[1] - 0.5, max(10.0, pos[2] + 10.0)], dtype=np.float64)
            elif self._spectator_preset == "orbit":
                ang = 0.25 * t
                eye = np.array([pos[0] + 4.2 * math.cos(ang), pos[1] + 4.2 * math.sin(ang), pos[2] + 2.4], dtype=np.float64)
            else:  # follow
                eye = np.array([pos[0] - 2.6, pos[1] - 3.0, max(1.0, pos[2] + 1.8)], dtype=np.float64)
            fwd = pos - eye
            dist = float(np.linalg.norm(fwd))
            fwd = fwd / max(dist, 1e-6)
            world_up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
            right = np.cross(fwd, world_up)
            if float(np.linalg.norm(right)) < 1e-4:
                right = np.array([1.0, 0.0, 0.0], dtype=np.float64)
            else:
                right = right / float(np.linalg.norm(right))
            up = np.cross(right, fwd)
            r_world_spec = np.column_stack((right, up, -fwd))
            self.model.cam_pos[self._spectator_cam_id] = eye
            self.model.cam_quat[self._spectator_cam_id] = _rotmat_to_quat_wxyz(r_world_spec)

        mujoco.mj_kinematics(self.model, self.data)
        mujoco.mj_camlight(self.model, self.data)

    def _snapshot_state(self, *, dt: float) -> QuadState:
        pos_enu = self.data.xpos[self._quad_body_id] - self._origin_enu
        vel_enu = self.data.qvel[self._dof_adr : self._dof_adr + 3]
        mat = self.data.xmat[self._quad_body_id].reshape(3, 3)
        right_u, fwd_u, up_u = mat[:, 0], mat[:, 1], mat[:, 2]

        # Body angular velocity in MuJoCo: qvel[dof+3:dof+6] is angular velocity in body frame.
        w_body = self.data.qvel[self._dof_adr + 3 : self._dof_adr + 6]
        # FRD rates: p about nose (+fwd), q about right (+right), r about down (-up)
        gyro_frd = (float(w_body[1]), float(w_body[0]), float(-w_body[2]))

        acc_enu = (vel_enu - self._prev_vel_enu) / max(dt, 1e-6)
        spec_force_enu = acc_enu + np.array([0.0, 0.0, GRAVITY], dtype=np.float64)
        accel_frd = (
            float(np.dot(spec_force_enu, fwd_u)),
            float(np.dot(spec_force_enu, right_u)),
            float(-np.dot(spec_force_enu, up_u)),
        )
        pos_ned = (float(pos_enu[1]), float(pos_enu[0]), float(-pos_enu[2]))
        vel_ned = (float(vel_enu[1]), float(vel_enu[0]), float(-vel_enu[2]))
        attitude = _euler_ned_from_axes(right_u, fwd_u, up_u)

        t = float(self.data.time)
        if t - self._last_trail_t >= 0.25:
            p_abs = self.data.xpos[self._quad_body_id]
            self._trail_enu.append((float(p_abs[0]), float(p_abs[1]), float(p_abs[2])))
            if len(self._trail_enu) > 240:
                self._trail_enu.pop(0)
            self._last_trail_t = t

        return QuadState(
            timestamp_s=t,
            pos_ned=pos_ned,
            vel_ned=vel_ned,
            gyro_frd=gyro_frd,
            accel_frd=accel_frd,
            attitude_ned=attitude,
            motor_rpm=(
                self._motor_rpm[0],
                self._motor_rpm[1],
                self._motor_rpm[2],
                self._motor_rpm[3],
            ),
            trail_enu=list(self._trail_enu),
        )

    def read_rangefinders_m(self) -> dict[str, float]:
        """Return latest 7 rangefinder distances in metres (capped at 4.0 m when no hit)."""
        with self.lock:
            readings: dict[str, float] = {}
            for name, adr in zip(self._range_sensor_names, self._range_sensor_adrs):
                val = float(self.data.sensordata[adr])
                readings[name] = 4.0 if val < 0.0 else max(0.05, min(4.0, val))
            return readings

    def render_camera(
        self,
        renderer: mujoco.Renderer,
        camera: str = "drone_cam",
    ) -> np.ndarray:
        """Render ``camera`` into an HxWx3 uint8 RGB array under the simulation lock."""
        with self.lock:
            self._apply_camera_poses()
            opt = mujoco.MjvOption()
            opt.sitegroup[:] = 0
            opt.flags[mujoco.mjtVisFlag.mjVIS_RANGEFINDER] = int(
                camera == "spectator_cam" and self._show_tof
            )
            renderer.update_scene(self.data, camera=camera, scene_option=opt)
            return renderer.render().copy()

    def handle_servo_packet(
        self,
        packet: bytes,
        last_count: int | None = None,
        last_reply: bytes | None = None,
    ) -> tuple[int | None, bytes | None]:
        """Decode one UDP 9002 servo packet, step physics if new, and return ``(count, json_reply)``."""
        servo = decode_servo_frame(packet)
        if servo is None:
            return last_count, None
        if last_count is not None and servo.frame_count == last_count and last_reply is not None:
            return last_count, last_reply
        state = self.step_servo(servo)
        return servo.frame_count, encode_sitl_state(state)

    def serve_udp_lockstep(
        self,
        stop_event: threading.Event,
        *,
        host: str = "127.0.0.1",
        port: int = 9002,
    ) -> None:
        """Run the blocking ArduPilot JSON SITL UDP lockstep loop until ``stop_event``."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, int(port)))
        sock.settimeout(0.1)
        last_count: int | None = None
        last_reply: bytes | None = None
        with high_res_windows_timer():
            try:
                while not stop_event.is_set():
                    try:
                        packet, addr = sock.recvfrom(512)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    last_count, reply = self.handle_servo_packet(packet, last_count, last_reply)
                    if reply is None:
                        continue
                    last_reply = reply
                    sock.sendto(reply, addr)
            finally:
                sock.close()
