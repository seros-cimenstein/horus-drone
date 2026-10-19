"""Bounded skill executor; every autonomous translation is a closed-loop NED velocity command."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from horus.config import ExecutorSettings
from horus.executor.area import step_area
from horus.executor.geofence import Envelope
from horus.executor.perception_port import PerceptionPort
from horus.executor.precision_landing import PrecisionLanding
from horus.executor.skills import TRACKING_SKILLS, SkillContext, parse_pose, prepare, tracking_record
from horus.executor.target_range import TargetRanger
from horus.executor.target_skills import step_target
from horus.events import TERMINAL_TASK_STATUSES, EventBus
from horus.flight import FlightLink, Pose, Telemetry


logger = logging.getLogger(__name__)

class ExecutorState(str, Enum):
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    HOLDING = "HOLDING"
    RTH = "RTH"
    # A flight-mode change HORUS did not command (pilot RC switch, autopilot failsafe) owns the
    # vehicle; HORUS sends nothing until the vehicle is back in GUIDED or disarmed.
    PILOT = "PILOT"


# The flight-link operation that commands each autopilot mode HORUS uses.
_MODE_SENDERS: dict[str, Callable[[FlightLink], None]] = {
    "GUIDED": lambda flight: flight.set_guided(),
    "RTL": lambda flight: flight.rth(),
    "LAND": lambda flight: flight.land(),
}
_ACTION_MODES = {"rth": "RTL", "land": "LAND"}


class _Preempted(BaseException):
    """An emergency superseded the controller tick that was about to command the vehicle.

    A BaseException, like asyncio.CancelledError, so no step or precision-landing error handler
    can swallow it and fall back to its own hold/finish path.
    """


@dataclass(slots=True)
class SkillTask:
    task_id: str
    skill: str
    arguments: dict[str, Any]
    requested_target: Any
    executed_target: Any
    clamp_reason: str | None
    created_at: str
    created_monotonic: float
    expires_monotonic: float
    status: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    _phase: str = "start"
    _waypoints: list[Pose] = field(default_factory=list)
    _waypoint_index: int = 0
    _target_pose: Pose | None = None
    _arm_requested_monotonic: float = 0.0
    tracking: dict[str, Any] = field(default_factory=dict)
    _target_lost_at: float | None = None
    # World heading (deg) of the target at its last validated frame; a lost target is held facing it.
    _last_target_facing_deg: float | None = None
    _target_lost_at_sim_s: float | None = None
    _range_note_warned: bool = False
    _finished_event_published: bool = False
    _search_frame_id: int | None = None

    def terminate(self, status: str, error: str | None) -> None:
        """Enter a terminal status and freeze the caller-visible result."""
        self.status = status
        self.error = error
        self.result = {
            "success": status == "succeeded",
            "status": status,
            "requested_target": self.requested_target,
            "executed_target": self.executed_target,
            "clamp_reason": self.clamp_reason,
            "error": error,
        }
        if self.skill in TRACKING_SKILLS:
            self.result["tracking"] = dict(self.tracking)


class HorusExecutor:
    """Serialize skills and continuously command bounded velocity from current telemetry."""

    SKILLS = {"take_off", "goto", "approach", "follow", "orbit", "scan", "search"}
    ARM_RETRY_INTERVAL_S = 2.0
    HEADING_TOLERANCE_DEG = 5.0
    LINK_EVENT_DEBOUNCE_S = 5.0
    # A mode HORUS commanded counts as its own once the autopilot reports it. HEARTBEAT is 1 Hz in
    # autopilot time (~5.5 s wall at sim RTF 0.18), so an unconfirmed request is dropped only after
    # this long; a rejected request must not later mask a pilot's switch to the same mode.
    MODE_CHANGE_CONFIRM_S = 15.0

    def __init__(
        self,
        flight: FlightLink,
        settings: ExecutorSettings,
        perception: PerceptionPort | None = None,
        *,
        event_bus: EventBus | None = None,
        autostart: bool = True,
    ):
        self.envelope = Envelope(settings)
        self.flight = flight
        self.settings = settings
        self.perception = perception
        self.event_bus = event_bus
        self._ranger = TargetRanger(settings, perception)
        self._skill_context = SkillContext(settings, self.envelope, self._current_pose, perception is not None)
        self._lock = threading.RLock()
        # Lock order: _command_lock, then _lock. _command_lock covers only autopilot command
        # operations, never perception or model work, so an emergency waits at most for one
        # guarded command operation in progress (a hold is Guided plus a zero-velocity setpoint).
        self._command_lock = threading.Lock()
        # Bumped by every emergency; a worker command is sent only if the epoch its tick
        # snapshotted (kept per thread in _tick_context) is still current.
        self._emergency_epoch = 0
        self._tick_context = threading.local()
        self._state = ExecutorState.IDLE
        self._tasks: dict[str, SkillTask] = {}
        self._queue: deque[str] = deque()
        self._active_id: str | None = None
        self._stop = threading.Event()
        self._holding_since: float | None = None
        self._auto_rth_after_hold = False
        # Any MCP call (including the commander's event long-poll) proves the supervisory link.
        self._last_supervisor_contact = time.monotonic()
        self._heartbeat_state: bool | None = None
        self._heartbeat_change_since: float | None = None
        self._previous_armed: bool | None = None
        # Last autopilot mode seen in telemetry, the mode HORUS last requested and when (until
        # telemetry confirms it), and the mode a pilot/failsafe took over in (None = HORUS owns).
        self._observed_mode: str | None = None
        self._pending_mode: tuple[str, float] | None = None
        self._pilot_override: str | None = None
        self._precision = PrecisionLanding(self)
        self._worker: threading.Thread | None = None
        if autostart:
            self.start()

    @property
    def state(self) -> ExecutorState:
        with self._lock:
            return self._state

    @property
    def emergency_epoch(self) -> int:
        """Bumped by every emergency and pilot override; a plan stops when it changes."""
        with self._lock:
            return self._emergency_epoch

    def start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="horus-executor", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        period = 1.0 / self.settings.control_hz
        while not self._stop.is_set():
            started = time.monotonic()
            self.tick()
            self._stop.wait(max(0.0, period - (time.monotonic() - started)))

    def close(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.5)
        if self.state in (ExecutorState.EXECUTING, ExecutorState.HOLDING) or self._precision.active:
            with self._command_lock:
                self._flight_hold()
            self._precision.active = False

    def _telemetry(self) -> Telemetry:
        return self.flight.telemetry()

    def _fresh(self, updated_monotonic: float | None) -> bool:
        """Whether a sample is recent enough to drive the vehicle.

        Pose, heading, range and frame samples older than ``telemetry_max_age_s`` never drive
        translation; None (never received) is never fresh.
        """
        return self._ranger.fresh(updated_monotonic)

    def get_state(self) -> dict[str, Any]:
        telemetry = self._telemetry()
        with self._lock:
            active = None if self._active_id is None else self._tasks[self._active_id]
            snapshot = {
                "state": self._state.value,
                "active_task_id": None if active is None else active.task_id,
                "active_skill": None if active is None else active.skill,
                "observed_at": telemetry.observed_at,
                "precision_landing": {"active": self._precision.active, **self._precision.stats},
                "pilot_override": self._pilot_override,
            }
        snapshot["vehicle"] = telemetry.as_dict()
        return snapshot

    def submit_skill(
        self,
        skill: str,
        arguments: Mapping[str, Any] | None = None,
        ttl_s: float | None = None,
        *,
        expected_epoch: int | None = None,
    ) -> dict[str, Any]:
        """Queue a skill; ``expected_epoch`` refuses it if an emergency happened since then."""
        if skill == "hold":
            return self.hold()
        if skill == "rth":
            return self.rth()
        if skill == "land":
            return self.land()
        if skill not in self.SKILLS:
            raise ValueError(f"unknown skill {skill!r}")
        ttl = self.settings.command_ttl_s if ttl_s is None else float(ttl_s)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_s must be a finite positive number")
        args = dict(arguments or {})
        requested, executed, reason = prepare(skill, args, self._skill_context)
        now = time.monotonic()
        task_id = uuid.uuid4().hex[:12]
        task = SkillTask(
            task_id=task_id,
            skill=skill,
            arguments=args,
            requested_target=requested,
            executed_target=executed,
            clamp_reason=reason,
            created_at=datetime.now(timezone.utc).isoformat(),
            created_monotonic=now,
            expires_monotonic=now + ttl,
            tracking=tracking_record(skill, args),
        )
        with self._lock:
            if self._pilot_override is not None:
                raise RuntimeError(self._pilot_override_message())
            if expected_epoch is not None and expected_epoch != self._emergency_epoch:
                raise RuntimeError("an emergency or pilot override superseded this request")
            self._tasks[task_id] = task
            self._holding_since = None
            self._queue.append(task_id)
        return self._task_dict(task)

    def _publish_event(self, kind: str, summary: str, **data: Any) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(kind, summary, **data)

    def _current_pose(self) -> Pose | None:
        return self._telemetry().pose

    def _observe_vehicle_events(self) -> None:
        try:
            heartbeat_ok: bool | None = bool(self.flight.heartbeat_ok())
        except Exception:
            heartbeat_ok = None
        if heartbeat_ok is not None:
            now = time.monotonic()
            if self._heartbeat_state is None:
                self._heartbeat_state = heartbeat_ok
            elif heartbeat_ok == self._heartbeat_state:
                self._heartbeat_change_since = None
            elif self._heartbeat_change_since is None:
                self._heartbeat_change_since = now
            elif now - self._heartbeat_change_since >= self.LINK_EVENT_DEBOUNCE_S:
                # Only a change that persists is an event; a slow sim or bursty link must
                # not flap link_lost/link_restored at the commander every few seconds.
                self._heartbeat_state = heartbeat_ok
                self._heartbeat_change_since = None
                kind = "link_restored" if heartbeat_ok else "link_lost"
                summary = "Autopilot heartbeat restored" if heartbeat_ok else "Autopilot heartbeat lost"
                self._publish_event(kind, summary)

        telemetry = self._telemetry()
        armed = telemetry.armed
        self._observe_mode(telemetry.mode, armed)
        if self._previous_armed is True and not armed:
            pose = telemetry.pose
            if (
                pose is not None
                and math.isfinite(pose.up_m)
                and pose.up_m <= PrecisionLanding.HANDOFF_ALTITUDE_M
            ):
                self._publish_event(
                    "landed",
                    "Vehicle disarmed at low altitude",
                    position=pose.as_dict(),
                )
            self._release_pilot_override("vehicle disarmed", airborne=False)
        self._previous_armed = armed

    def _observe_mode(self, mode: str, armed: bool) -> None:
        """Yield to any armed flight-mode change HORUS did not command.

        A pilot's RC mode switch or an autopilot failsafe (battery, fence, EKF) owns the vehicle
        from then on: HORUS cancels its work and refuses flight commands instead of re-commanding
        GUIDED at the next task start, hold or command-TTL expiry. Switching back to GUIDED, or
        disarming, returns authority to HORUS.
        """
        mode = mode.strip().upper()
        if not mode or mode == "UNKNOWN":
            return
        now = time.monotonic()
        with self._lock:
            previous, self._observed_mode = self._observed_mode, mode
            pending = self._pending_mode
            commanded = pending is not None and pending[0] == mode
            if commanded or (
                pending is not None and now - pending[1] > self.MODE_CHANGE_CONFIRM_S
            ):
                self._pending_mode = None
            if previous is None or mode == previous or commanded or not armed:
                return
            overridden = self._pilot_override is not None
            if overridden and mode != "GUIDED":
                # The pilot moved between their own modes; HORUS stays out.
                self._pilot_override = mode
                return
        if overridden:
            self._release_pilot_override(f"vehicle switched {previous} -> GUIDED", airborne=True)
        elif mode != "GUIDED":
            self._yield_to_pilot(previous, mode)

    def _yield_to_pilot(self, previous: str, mode: str) -> None:
        reason = f"vehicle switched {previous} -> {mode} outside HORUS"
        with self._command_lock:
            with self._lock:
                # Supersede any tick already past its epoch snapshot, like an emergency.
                self._emergency_epoch += 1
                preempted = self._cancel_all_tasks_locked(f"pilot override: {reason}")
                self._pilot_override = mode
                self._pending_mode = None
                self._state = ExecutorState.PILOT
                self._holding_since = None
                self._auto_rth_after_hold = False
                # The camera stays recorded as nadir so the next skill restores its pitch.
                self._precision.reset(unavailable=False, release_gimbal=False)
        for task in preempted:
            self._publish_task_finished(task)
        logger.warning("pilot override: %s; HORUS flight commands suspended", reason)
        self._publish_event(
            "pilot_override",
            f"Pilot or autopilot took control ({reason}); HORUS refuses flight commands until "
            "the vehicle is switched back to GUIDED",
            mode=mode,
            previous_mode=previous,
        )

    def _release_pilot_override(self, summary: str, *, airborne: bool) -> None:
        with self._lock:
            if self._pilot_override is None:
                return
            self._pilot_override = None
            if airborne:
                # Same footing as after any task: GUIDED zero-velocity hold, link-loss RTH armed.
                self._enter_hold_locked()
            else:
                self._enter_idle_locked()
        logger.info("pilot override released: %s", summary)
        self._publish_event("pilot_released", f"HORUS control restored: {summary}")

    def _enter_hold_locked(self) -> None:
        """Hold with link-loss RTH armed; call with _lock held."""
        self._state = ExecutorState.HOLDING
        self._holding_since = time.monotonic()
        self._auto_rth_after_hold = True

    def _enter_idle_locked(self) -> None:
        """Nothing to hold: no zero-velocity stream and no link-loss RTH; call with _lock held."""
        self._state = ExecutorState.IDLE
        self._holding_since = None
        self._auto_rth_after_hold = False

    def _pilot_override_message(self) -> str:
        return (
            f"pilot override active (vehicle in {self._pilot_override}); HORUS sends no flight "
            "commands until the pilot switches the vehicle back to GUIDED"
        )

    def _mode_confirmed(self, mode: str) -> bool:
        """True when telemetry reports ``mode`` and no other HORUS mode request is outstanding."""
        with self._lock:
            return self._observed_mode == mode and self._pending_mode is None

    def _send_mode(self, mode: str) -> None:
        """Request an autopilot mode; callers hold _command_lock (directly or via _command)."""
        with self._lock:
            self._pending_mode = (mode, time.monotonic())
        _MODE_SENDERS[mode](self.flight)

    def _command_mode(self, mode: str) -> None:
        """Worker-thread mode request, skipped once the autopilot already reports that mode.

        Re-sending GUIDED on a GUIDED report that predates a pilot's switch would undo the switch
        before the next heartbeat could show it.
        """
        if not self._mode_confirmed(mode):
            self._command(self._send_mode, mode)

    def tick(self) -> None:
        """Perform one controller step; public for deterministic tests and embedded loops."""
        self._observe_vehicle_events()
        restore_default_pitch = False
        with self._lock:
            if self._pilot_override is not None:
                return
            if self._active_id is None:
                while self._queue:
                    task_id = self._queue.popleft()
                    task = self._tasks[task_id]
                    if task.status != "queued":
                        continue
                    if time.monotonic() >= task.expires_monotonic:
                        self._finish(task, "expired", "command TTL expired before execution")
                        continue
                    self._active_id = task_id
                    task.status = "running"
                    self._state = ExecutorState.EXECUTING
                    self._holding_since = None
                    # A skill preempting RTH/LAND precision landing must get the camera back.
                    restore_default_pitch = self._precision.reset(unavailable=False)
                    break
            task = None if self._active_id is None else self._tasks[self._active_id]
            rth_due = (
                task is None
                and self._state == ExecutorState.HOLDING
                and self._auto_rth_after_hold
                and self._holding_since is not None
                and time.monotonic() - max(self._holding_since, self._last_supervisor_contact)
                >= self.settings.link_loss_rth_delay_s
            )
            precision_mode = self._precision.fallback_mode
            holding = self._state == ExecutorState.HOLDING
            epoch = self._emergency_epoch
            self._tick_context.epoch = epoch
        try:
            if restore_default_pitch:
                self._command(self._set_default_gimbal_pitch)
            if task is None:
                if precision_mode is not None:
                    self._precision.step(precision_mode)
                elif rth_due:
                    if self._emergency("rth", source=None, expected_epoch=epoch) is not None:
                        self._publish_event("auto_rth", "Automatic return-to-home triggered")
                elif holding:
                    self._command_zero_velocity()
                return
            if task.cancel_requested:
                self._safe_hold()
                self._finish_running(task, "cancelled", "task cancelled")
                return
            if time.monotonic() >= task.expires_monotonic:
                self._safe_hold()
                self._finish_running(task, "expired", "command TTL expired; executor entered GUIDED hold")
                return
            try:
                completed = self._step(task)
            except Exception as exc:
                self._safe_hold()
                self._finish_running(task, "failed", str(exc))
                return
            if completed:
                self._safe_hold()
                self._finish_running(task, "succeeded", None)
        except _Preempted:
            # The emergency already recorded the task as cancelled and owns vehicle and state.
            logger.info("controller tick superseded by an emergency command")
        finally:
            self._tick_context.epoch = None

    def _command(self, send: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Send a worker-thread autopilot command unless an emergency superseded this tick."""
        with self._command_lock:
            epoch = getattr(self._tick_context, "epoch", None)
            if epoch is None:
                raise RuntimeError("executor flight commands must be issued from a controller tick")
            if epoch != self._emergency_epoch:
                raise _Preempted
            return send(*args, **kwargs)

    def _require_current_tick(self) -> None:
        """Refuse a worker state write after an emergency; call with _lock held."""
        if getattr(self._tick_context, "epoch", None) != self._emergency_epoch:
            raise _Preempted

    def _finish_running(self, task: SkillTask, status: str, error: str | None) -> None:
        with self._lock:
            # An emergency may have cancelled this task after its tick started; keep that outcome.
            if task.status == "running":
                self._finish(task, status, error)

    def _step(self, task: SkillTask) -> bool:
        if task._phase == "start":
            self._command_mode("GUIDED")
            if task.skill == "take_off":
                # Set the configured search angle after any prior precision landing.
                self._command(self._set_default_gimbal_pitch)
                altitude = float(task.executed_target["altitude_m"])
                pose = self._current_pose()
                task._target_pose = Pose(
                    0.0 if pose is None else pose.north_m,
                    0.0 if pose is None else pose.east_m,
                    altitude,
                )
                task._phase = "guiding"
                return False
            elif task.skill == "goto":
                task._target_pose = parse_pose(task.executed_target)
            elif task.skill in ("scan", "search"):
                task._waypoints = [parse_pose(point) for point in task.executed_target]
            task._phase = "control"
        if task.skill == "take_off" and task._phase == "guiding":
            if self._telemetry().mode.upper() != "GUIDED":
                return False
            self._command(self.flight.arm)
            task._arm_requested_monotonic = time.monotonic()
            task._phase = "arming"
            return False
        if task.skill == "take_off" and task._phase == "arming":
            if not self._telemetry().armed:
                # ArduPilot rejects arming while pre-arm checks settle (e.g. "Accels inconsistent"
                # after a cold start); keep asking until armed or the task TTL expires.
                now = time.monotonic()
                if now - task._arm_requested_monotonic >= self.ARM_RETRY_INTERVAL_S:
                    self._command(self.flight.arm)
                    task._arm_requested_monotonic = now
                return False
            self._command(self.flight.takeoff, float(task.executed_target["altitude_m"]))
            task._phase = "takeoff"
            return False
        if task.skill == "take_off" and task._phase == "takeoff":
            current = self._current_pose()
            if current is None:
                raise RuntimeError("local vehicle pose unavailable; refusing open-loop takeoff")
            if current.up_m < task._target_pose.up_m - self.settings.arrival_tolerance_m:
                return False
            task._phase = "control"
        if task.skill in ("take_off", "goto"):
            arrived = self._goto_step(task._target_pose)
            heading = task.executed_target.get("heading_deg")
            if arrived and heading is not None:
                current = float(self._telemetry().heading_deg or 0.0)
                error = (heading - current + 180.0) % 360.0 - 180.0
                if abs(error) > self.HEADING_TOLERANCE_DEG:
                    # Hover in place while turning to the requested heading.
                    self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0, yaw_deg=heading)
                    return False
            return arrived
        if task.skill in ("scan", "search"):
            return step_area(self, task)
        return step_target(self, task)

    def _horizontal_speed_limit(self, target_altitude_m: float | None = None) -> float:
        """Horizontal speed limit for the current leg: gated cruise speed or ``max_speed_m_s``.

        The cruise speed is allowed only while the vehicle's current altitude above home is
        at/above ``cruise_min_altitude_m`` AND the commanded target altitude is too, so the
        whole leg stays above the obstacle band; anywhere below it the proven ``max_speed_m_s``
        applies. A disabled cruise (None) always returns ``max_speed_m_s``.
        """
        cruise = self.settings.cruise_speed_m_s
        if cruise is None:
            return self.settings.max_speed_m_s
        gate = self.settings.cruise_min_altitude_m
        current = self._current_pose()
        if current is None or gate is None or current.up_m < gate:
            return self.settings.max_speed_m_s
        if target_altitude_m is not None and target_altitude_m < gate:
            return self.settings.max_speed_m_s
        return cruise

    def _goto_step(self, target: Pose) -> bool:
        current = self._current_pose()
        if current is None:
            raise RuntimeError("local vehicle pose unavailable; refusing open-loop movement")
        self._require_fresh(self._telemetry().pose_updated_monotonic, "pose")
        north, east, up = target.north_m - current.north_m, target.east_m - current.east_m, target.up_m - current.up_m
        horizontal = math.hypot(north, east)
        vertical = abs(up)
        if horizontal <= self.settings.arrival_tolerance_m and vertical <= self.settings.arrival_tolerance_m:
            self._command_zero_velocity()
            return True
        speed_limit = self._horizontal_speed_limit(target.up_m)
        scale = min(1.0, speed_limit / horizontal) if horizontal > 1e-6 else 0.0
        vn = north * scale
        ve = east * scale
        vu = max(-self.settings.max_vertical_speed_m_s, min(self.settings.max_vertical_speed_m_s, up))
        # Velocity-only SET_POSITION_TARGET_LOCAL_NED is the only translation command.
        self._command(self.flight.set_velocity_ned, vn, ve, -vu)
        return False

    def _require_fresh(self, updated_monotonic: float | None, label: str) -> None:
        if not self._fresh(updated_monotonic):
            age = math.inf if updated_monotonic is None else max(0.0, time.monotonic() - float(updated_monotonic))
            raise RuntimeError(f"vehicle {label} is stale ({age:.2f}s); entering hold")

    @staticmethod
    def _sim_time_s(telemetry: Telemetry) -> float | None:
        """Autopilot boot time in seconds (SYSTEM_TIME), or None when unknown."""
        if telemetry.time_boot_ms is None:
            return None
        value = float(telemetry.time_boot_ms) / 1000.0
        return value if math.isfinite(value) and value > 0 else None

    def _command_zero_velocity(self) -> None:
        self._command(self.flight.set_velocity_ned, 0.0, 0.0, 0.0)

    def _set_default_gimbal_pitch(self) -> None:
        try:
            self.flight.set_gimbal_pitch_yaw(self.settings.default_gimbal_pitch_deg, 0.0)
        except Exception as exc:  # camera aiming must never block flight
            logger.warning("could not set gimbal pitch: %s", exc)

    def _safe_hold(self) -> None:
        self._command(self._flight_hold)

    def _flight_hold(self) -> None:
        """GUIDED zero-velocity hold sent directly; callers own command ordering.

        Once telemetry confirms GUIDED only the setpoint is sent (see _command_mode).
        """
        if self._mode_confirmed("GUIDED"):
            self.flight.set_velocity_ned(0.0, 0.0, 0.0)
            return
        with self._lock:
            self._pending_mode = ("GUIDED", time.monotonic())
        self.flight.hold()

    def _publish_task_finished(self, task: SkillTask) -> None:
        if task._finished_event_published:
            return
        task._finished_event_published = True
        self._publish_event(
            "task_finished",
            f"{task.skill} task {task.status}",
            task_id=task.task_id,
            skill=task.skill,
            status=task.status,
            error=task.error,
        )

    def _finish(self, task: SkillTask, status: str, error: str | None) -> None:
        if task.skill in ("scan", "search") and task._waypoints:
            # A started scan reports the waypoints as re-clamped in flight; one that never
            # started keeps the clamped plan computed at submission.
            task.executed_target = [point.as_dict() for point in task._waypoints]
        task.terminate(status, error)
        if self._active_id == task.task_id:
            self._active_id = None
        if self._telemetry().armed:
            self._enter_hold_locked()
        else:
            # On the ground nothing needs holding: no zero-velocity stream and no link-loss RTL.
            self._enter_idle_locked()
        self._publish_task_finished(task)

    @staticmethod
    def _task_dict(task: SkillTask) -> dict[str, Any]:
        result = {
            "task_id": task.task_id,
            "skill": task.skill,
            "status": task.status,
            "created_at": task.created_at,
            "requested_target": task.requested_target,
            "executed_target": task.executed_target,
            "clamp_reason": task.clamp_reason,
            "result": task.result,
            "error": task.error,
        }
        if task.skill in TRACKING_SKILLS:
            result["tracking"] = dict(task.tracking)
        return result

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                task = self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown task_id {task_id!r}") from exc
            return self._task_dict(task)

    def note_supervisor_contact(self) -> None:
        """Record supervisory-link activity; idle-hold auto-RTH counts from the last contact."""
        self._last_supervisor_contact = time.monotonic()

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"unknown task_id {task_id!r}")
            if task.status in TERMINAL_TASK_STATUSES:
                return self._task_dict(task)
            if task.task_id == self._active_id:
                task.cancel_requested = True
            else:
                task.terminate("cancelled", "task cancelled before execution")
                self._publish_task_finished(task)
            return self._task_dict(task)

    def _emergency(
        self,
        action: str,
        *,
        source: str | None = "executor",
        expected_epoch: int | None = None,
    ) -> dict[str, Any] | None:
        """Preempt all work and command ``action``; ``expected_epoch`` makes it conditional.

        The epoch bump and the autopilot command happen under one _command_lock hold, so any worker
        command validated against the old epoch was sent before this one and cannot follow it.
        """
        preempted: list[SkillTask] = []
        with self._command_lock:
            with self._lock:
                if expected_epoch is not None and expected_epoch != self._emergency_epoch:
                    return None
                if self._pilot_override is not None:
                    if expected_epoch is not None:
                        return None
                    raise RuntimeError(self._pilot_override_message())
                self._emergency_epoch += 1
                preempted = self._cancel_all_tasks_locked(f"preempted by emergency {action}")
                self._holding_since = time.monotonic()
                self._auto_rth_after_hold = action == "hold"
                camera_down = self._precision.reset(unavailable=False)
                self._precision.fallback_mode = _ACTION_MODES.get(action)
                restore_default_pitch = action == "hold" and camera_down
                state = ExecutorState.RTH if action == "rth" else ExecutorState.HOLDING
                self._state = state
            if action == "hold":
                self._flight_hold()
                if restore_default_pitch:
                    self._set_default_gimbal_pitch()
            elif action in _ACTION_MODES:
                self._send_mode(_ACTION_MODES[action])
        for task in preempted:
            self._publish_task_finished(task)
        if source is not None:
            self._publish_event(
                "emergency",
                f"Emergency {action} requested",
                action=action,
                source=source,
            )
        return {"action": action, "state": state.value, "preempted": True}

    def hold(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("hold", source=source)

    def rth(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("rth", source=source)

    def land(self, *, source: str | None = "executor") -> dict[str, Any]:
        return self._emergency("land", source=source)

    def _cancel_all_tasks_locked(self, reason: str) -> list[SkillTask]:
        """Cancel every queued/running task and clear the queue; call with _lock held."""
        preempted: list[SkillTask] = []
        for task in self._tasks.values():
            if task.status in ("queued", "running"):
                task.terminate("cancelled", reason)
                preempted.append(task)
        self._active_id = None
        self._queue.clear()
        return preempted
