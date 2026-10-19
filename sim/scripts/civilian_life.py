"""Scenario-driven kinematic pedestrians, animals and vehicles for Gazebo worlds."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import math
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from scripts.gz_sim import _SimClock, load_gz_bindings
from sim.campaign.civilian_spec import (
    AgentSpec,
    LoopPolyline,
    Scenario,
    Waypoint,
    agent_error,
    load_scenario,
    next_route_index,
    point_in_polygon,
    route_cycle_length,
    segment_stays_in_polygon,
    select_agents,
)
from sim.worldgen.paths import FilletedPath

_EPSILON = 1e-9
_MAX_STEP_S = 0.05
_PEDESTRIAN_TURN_RATE = math.pi
_ANIMAL_TURN_RATE = {"dog": 4.0, "cat": 4.0, "deer": 2.5, "pigeon": 6.0, "crow": 6.0}
_VEHICLE_LATERAL_ACCEL_M_S2 = 2.0
_EMERGENCY_DECEL_M_S2 = 6.5
_STANDOFF_M = 0.6
_WANDER_REPLAN_S = 3.0
_CLOCK_STARTUP_TIMEOUT_S = 120.0


@dataclass(slots=True)
class RouteCursor:
    index: int
    direction: int = 1
    progress_m: float = 0.0
    hold_remaining_s: float = 0.0
    finished: bool = False


@dataclass(slots=True)
class AgentState:
    spec: AgentSpec
    x: float
    y: float
    yaw: float
    speed_m_s: float = 0.0
    cursor: RouteCursor | None = None
    rng: random.Random | None = None
    goal: tuple[float, float] | None = None
    pause_remaining_s: float = 0.0
    arc_m: float = 0.0  # vehicles: absolute path arc length; followers: absolute leader-loop arc
    stop_ptr: int = 0  # vehicles: index into the (lap-repeated) stop sequence
    hold_remaining_s: float = 0.0
    finished: bool = False
    travel_m: float = 0.0
    blocked_s: float = 0.0
    startled_s: float = 0.0
    reversed_by_event: bool = False
    yielding_to: str | None = None

    def copy(self) -> AgentState:
        cursor = None if self.cursor is None else replace(self.cursor)
        rng = None
        if self.rng is not None:
            rng = random.Random(0)
            rng.setstate(self.rng.getstate())
        return replace(self, cursor=cursor, rng=rng)


# --------------------------------------------------------------------------- geometry helpers

def _wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _stable_seed(seed: int, model: str) -> int:
    digest = hashlib.sha256(f"{seed}:{model}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _route_cursor(spec: AgentSpec) -> RouteCursor:
    cursor = RouteCursor(0, hold_remaining_s=spec.waypoints[0].hold_s if spec.start_offset_s == 0 else 0.0)
    if spec.speed_m_s is None or spec.start_offset_s <= 0.0 or len(spec.waypoints) < 2:
        return cursor
    distance = spec.speed_m_s * spec.start_offset_s
    cycle_length = route_cycle_length(spec)
    if spec.mode in ("loop", "pingpong"):
        if cycle_length <= _EPSILON:
            return cursor
        distance %= cycle_length
    while distance > _EPSILON and not cursor.finished:
        next_index, direction = next_route_index(cursor.index, cursor.direction, len(spec.waypoints), spec.mode)
        if next_index is None:
            cursor.finished = True
            break
        cursor.direction = direction
        current, target = spec.waypoints[cursor.index], spec.waypoints[next_index]
        segment_length = math.hypot(target.x - current.x, target.y - current.y)
        if segment_length <= _EPSILON:
            cursor.index = next_index
            if spec.mode == "once" and cursor.index == len(spec.waypoints) - 1:
                cursor.finished = True
            continue
        step = min(distance, segment_length - cursor.progress_m)
        cursor.progress_m += step
        distance -= step
        if cursor.progress_m >= segment_length - _EPSILON:
            cursor.index = next_index
            cursor.progress_m = 0.0
            if spec.mode == "once" and cursor.index == len(spec.waypoints) - 1:
                cursor.finished = True
    return cursor


# --------------------------------------------------------------------------- motion

class MotionEngine:
    """Pure simulation-time motion state for a selected set of scenario agents.

    Every decision happens on a fixed 0.05 s step grid anchored at the first update, so poses
    do not depend on how often ``update`` is called. Vehicles drive curvature-bounded
    filleted paths and brake (within ``decel_m_s2``) for corners, stops and route ends;
    accelerating pedestrians/animals brake for dwells, ends and wander goals. With
    ``interactions`` enabled every moving agent yields to agents in its forward corridor.
    Events change targets only; acceleration/deceleration limits still apply.
    """

    def __init__(self, scenario: Scenario, groups: tuple[str, ...] | list[str] | None = None) -> None:
        self.scenario = scenario
        self.agents = select_agents(scenario, groups)
        selected = {agent.model for agent in self.agents}
        self._paths: dict[str, FilletedPath] = {}
        self._loops: dict[str, LoopPolyline] = {}
        for spec in self.agents:
            if spec.kind == "vehicle":
                closed = spec.mode == "loop"
                points = [(w.x, w.y, w.hold_s) for w in spec.waypoints]
                try:
                    self._paths[spec.model] = FilletedPath(points, spec.turn_radius_m, closed=closed, name=spec.model)
                except ValueError as exc:
                    raise agent_error(spec.model, "waypoints", str(exc)) from exc
            if spec.behavior == "follow":
                if spec.leader not in selected:
                    raise agent_error(spec.model, "leader", "must be selected together with its follower")
                leader = next(agent for agent in self.agents if agent.model == spec.leader)
                self._loops[spec.model] = LoopPolyline(leader.waypoints)
        self._events = tuple(event for event in scenario.events if event.model in selected)
        self._states: dict[str, AgentState] = {}
        self._time_s: float | None = None
        self._start_time_s: float | None = None
        self._next_step_s: float | None = None
        self._step_index = 0
        self._output_states: dict[str, AgentState] | None = None
        for spec in self.agents:
            if spec.behavior != "follow":
                self._states[spec.model] = self._new_state(spec)
        for spec in self.agents:
            if spec.behavior == "follow":
                self._states[spec.model] = self._new_follower(spec)

    # ---- construction

    def _new_state(self, spec: AgentSpec) -> AgentState:
        if spec.behavior == "idle":
            assert spec.pose is not None
            x, y, yaw = spec.pose
            return AgentState(spec, x, y, yaw)
        if spec.kind == "vehicle":
            path = self._paths[spec.model]
            assert spec.speed_m_s is not None
            arc = spec.speed_m_s * spec.start_offset_s
            if not path.closed:
                arc = min(arc, path.length)
            x, y, heading, _ = path.sample(arc)
            state = AgentState(spec, x, y, heading, arc_m=arc)
            stops = path.stops
            ptr = 0
            if stops:
                lap = math.floor(arc / path.length) if path.closed else 0
                ptr = lap * len(stops) + bisect_right([s for s, _i, _h in stops], arc - lap * path.length - _EPSILON)
                if spec.start_offset_s == 0.0 and stops[0][0] == 0.0 and stops[0][2] > 0.0:
                    state.hold_remaining_s = stops[0][2]
                    ptr = 1  # the initial dwell is this stop
            state.stop_ptr = ptr
            if not path.closed and arc >= path.length - _EPSILON:
                state.finished = True
            return state
        if spec.behavior == "route":
            cursor = _route_cursor(spec)
            state = AgentState(spec, spec.waypoints[0].x, spec.waypoints[0].y, 0.0, cursor=cursor)
            self._set_route_position(state)
            target = self._route_target(state)
            if target is not None:
                state.yaw = math.atan2(target.y - state.y, target.x - state.x)
            elif len(spec.waypoints) > 1:
                previous, last = spec.waypoints[-2], spec.waypoints[-1]
                state.yaw = math.atan2(last.y - previous.y, last.x - previous.x)
            return state
        rng = random.Random(_stable_seed(self.scenario.seed, spec.model))
        state = AgentState(spec, 0.0, 0.0, 0.0, rng=rng)
        state.x, state.y = self._sample_area(spec, rng)
        state.goal = self._sample_area(spec, rng, (state.x, state.y))
        state.yaw = math.atan2(state.goal[1] - state.y, state.goal[0] - state.x)
        return state

    def _leader_arc(self, follower: AgentSpec, leader: AgentState) -> float:
        """Absolute arc of a loop-route leader: its starting arc plus distance travelled."""
        assert leader.spec.speed_m_s is not None
        loop = self._loops[follower.model]
        start = (leader.spec.speed_m_s * leader.spec.start_offset_s) % loop.length
        return start + leader.travel_m

    def _new_follower(self, spec: AgentSpec) -> AgentState:
        loop = self._loops[spec.model]
        arc = self._leader_arc(spec, self._states[spec.leader]) - sum(spec.gap_m) / 2
        x, y, heading = loop.sample(arc)
        return AgentState(spec, x, y, heading, arc_m=arc)

    @staticmethod
    def _sample_area(spec: AgentSpec, rng: random.Random, from_point: tuple[float, float] | None = None
                     ) -> tuple[float, float]:
        minimum_x = min(point[0] for point in spec.area)
        maximum_x = max(point[0] for point in spec.area)
        minimum_y = min(point[1] for point in spec.area)
        maximum_y = max(point[1] for point in spec.area)
        for _ in range(100_000):
            point = (rng.uniform(minimum_x, maximum_x), rng.uniform(minimum_y, maximum_y))
            if point_in_polygon(point, spec.area) and (
                from_point is None or segment_stays_in_polygon(from_point, point, spec.area)
            ):
                return point
        raise ValueError(f"agent {spec.model!r} field 'area' cannot produce a reachable point")

    # ---- route helpers (pedestrians and animals)

    def _route_target(self, state: AgentState) -> Waypoint | None:
        assert state.cursor is not None
        if state.cursor.finished:
            return None
        target_index, direction = next_route_index(
            state.cursor.index, state.cursor.direction, len(state.spec.waypoints), state.spec.mode)
        if target_index is None:
            return None
        state.cursor.direction = direction
        return state.spec.waypoints[target_index]

    def _set_route_position(self, state: AgentState) -> None:
        assert state.cursor is not None
        current = state.spec.waypoints[state.cursor.index]
        target = self._route_target(state)
        if target is None:
            state.x, state.y = current.x, current.y
            return
        length = math.hypot(target.x - current.x, target.y - current.y)
        fraction = 1.0 if length <= _EPSILON else min(1.0, state.cursor.progress_m / length)
        state.x = current.x + (target.x - current.x) * fraction
        state.y = current.y + (target.y - current.y) * fraction

    def _route_height(self, state: AgentState) -> float:
        assert state.cursor is not None
        ground = self.scenario.ground
        current = state.spec.waypoints[state.cursor.index]
        target = self._route_target(state)
        if target is None:
            return current.z if current.z is not None else ground.height(state.x, state.y) + state.spec.z_offset
        if current.z is None and target.z is None:
            return ground.height(state.x, state.y) + state.spec.z_offset
        length = math.hypot(target.x - current.x, target.y - current.y)
        fraction = 1.0 if length <= _EPSILON else min(1.0, state.cursor.progress_m / length)
        start_z = current.z if current.z is not None else ground.height(current.x, current.y) + state.spec.z_offset
        end_z = target.z if target.z is not None else ground.height(target.x, target.y) + state.spec.z_offset
        return start_z + (end_z - start_z) * fraction

    def _distance_to_route_stop(self, state: AgentState, horizon: float) -> float:
        """Route distance to the next dwell or route end (``inf`` beyond ``horizon``)."""
        assert state.cursor is not None
        cursor, spec = state.cursor, state.spec
        index, direction = cursor.index, cursor.direction
        travelled = -cursor.progress_m
        for _ in range(2 * len(spec.waypoints) + 2):
            target_index, direction = next_route_index(index, direction, len(spec.waypoints), spec.mode)
            if target_index is None:
                return max(0.0, travelled)
            a, b = spec.waypoints[index], spec.waypoints[target_index]
            travelled += math.hypot(b.x - a.x, b.y - a.y)
            if travelled > horizon:
                return math.inf
            if b.hold_s > _EPSILON or (spec.mode == "once" and target_index == len(spec.waypoints) - 1):
                return max(0.0, travelled)
            index = target_index
        return math.inf

    def _path_direction(self, state: AgentState) -> tuple[float, float] | None:
        if state.spec.behavior == "route" and state.cursor is not None:
            target = self._route_target(state)
            if target is None:
                return None
            dx, dy = target.x - state.x, target.y - state.y
        elif state.spec.behavior == "wander" and state.goal is not None:
            dx, dy = state.goal[0] - state.x, state.goal[1] - state.y
        else:
            return None
        length = math.hypot(dx, dy)
        if length <= _EPSILON:
            return None
        return dx / length, dy / length

    # ---- events and interactions

    def _event_effect(self, model: str, t: float) -> tuple[float, bool, bool, bool]:
        """(speed factor, stopped, startled, reversed) for an agent at elapsed time ``t``."""
        factor, stopped, startled, reversed_now = 1.0, False, False, False
        for event in self._events:
            if event.t_s > t + _EPSILON:
                break
            if event.model != model or t >= event.t_s + event.duration_s - _EPSILON:
                continue
            if event.type in ("hesitate", "breakdown"):
                stopped = True
            elif event.type == "speed_change":
                factor = event.factor
            elif event.type == "startle":
                startled = True
            elif event.type == "reverse":
                reversed_now = True
        return factor, stopped, startled, reversed_now

    def _apply_route_reversal(self, state: AgentState, reversed_now: bool) -> None:
        """Turn a loop pedestrian around without moving it on the polyline: the cursor keeps
        its position on the traversed edge, only the sense of travel changes."""
        cursor = state.cursor
        assert cursor is not None
        if cursor.hold_remaining_s > _EPSILON:
            return  # the turn waits out the dwell
        spec = state.spec
        target_index, _direction = next_route_index(cursor.index, cursor.direction,
                                                    len(spec.waypoints), spec.mode)
        if target_index is not None and cursor.progress_m > _EPSILON:
            current, target = spec.waypoints[cursor.index], spec.waypoints[target_index]
            length = math.hypot(target.x - current.x, target.y - current.y)
            cursor.index = target_index
            cursor.progress_m = max(0.0, length - cursor.progress_m)
        cursor.direction = -cursor.direction
        state.reversed_by_event = reversed_now

    def _heading(self, state: AgentState) -> float:
        if state.spec.kind == "vehicle":
            return self._paths[state.spec.model].sample(state.arc_m)[2]
        if state.spec.behavior == "follow":
            return self._loops[state.spec.model].sample(state.arc_m)[2]
        direction = self._path_direction(state)
        return state.yaw if direction is None else math.atan2(direction[1], direction[0])

    def _blockers(self, states: dict[str, AgentState]) -> dict[str, tuple[str, float]]:
        """For each moving agent, the nearest agent in its forward corridor and the free gap."""
        if not self.scenario.interactions:
            return {}
        movers = [state for state in states.values()
                  if state.spec.behavior != "idle" and not (state.spec.behavior == "wander" and not state.spec.avoid_agents)]
        everyone = sorted(states.values(), key=lambda state: state.spec.model)
        found: dict[str, tuple[str, float]] = {}
        for state in sorted(movers, key=lambda state: state.spec.model):
            heading = self._heading(state)
            hx, hy = math.cos(heading), math.sin(heading)
            decel = state.spec.decel_m_s2 or 2.0
            lookahead = state.speed_m_s * state.speed_m_s / (2 * decel) + 3.0 + state.spec.half_length
            best: tuple[str, float] | None = None
            for other in everyone:
                if other is state or other.spec.model == state.spec.leader or state.spec.model == other.spec.leader:
                    continue
                dx, dy = other.x - state.x, other.y - state.y
                ahead = dx * hx + dy * hy
                if ahead <= 0.0 or ahead > lookahead + other.spec.half_length:
                    continue
                lateral = abs(-dx * hy + dy * hx)
                other_extent = max(other.spec.half_length, other.spec.half_width)
                if lateral > state.spec.half_width + other_extent + 0.3:
                    continue
                gap = ahead - state.spec.half_length - other_extent
                if best is None or gap < best[1]:
                    best = (other.spec.model, gap)
            if best is not None:
                found[state.spec.model] = best
        # Mutual conflicts: pedestrians/animals keep right of way over vehicles, otherwise the
        # lexically smaller model name proceeds; the other keeps yielding.
        for model, (other, _gap) in list(found.items()):
            if found.get(other, (None,))[0] != model or model not in found:
                continue
            rank = lambda name: (states[name].spec.kind == "vehicle", name)  # noqa: E731
            winner = min(model, other, key=rank)
            found.pop(winner, None)
        return found

    # ---- stepping

    def _speed_update(self, state: AgentState, target: float, dt: float, urgent: bool = False) -> float:
        """Accelerate within accel_m_s2; slow within decel_m_s2, or emergency braking when a
        conflicting agent (``urgent``) requires it."""
        spec = state.spec
        if spec.accel_m_s2 is None:
            return max(0.0, target)
        old = state.speed_m_s
        new = min(old + spec.accel_m_s2 * dt, target)
        decel = _EMERGENCY_DECEL_M_S2 if urgent or spec.decel_m_s2 is None else spec.decel_m_s2
        return max(0.0, max(new, old - decel * dt))

    def _target_speed(self, state: AgentState, t: float, blockers: dict[str, tuple[str, float]],
                      dt: float) -> tuple[float, bool]:
        """(target speed, urgent) where urgent means a conflicting agent forces the limit."""
        spec = state.spec

        def stopping(distance: float, decel: float) -> float:
            # Largest speed v for this step such that, after travelling (old + v) / 2 * dt, the
            # vehicle can still stop within the remaining distance at ``decel``:
            # v^2 <= 2 * decel * (distance - (old + v) * dt / 2).
            room = distance - state.speed_m_s * dt / 2.0
            disc = decel * decel * dt * dt + 8.0 * decel * room
            if room <= 0.0 or disc <= 0.0:
                return 0.0
            return max(0.0, (-decel * dt + math.sqrt(disc)) / 2.0)

        assert spec.speed_m_s is not None and spec.max_speed_m_s is not None
        factor, stopped, startled, _reversed_now = self._event_effect(spec.model, t)
        target = 0.0 if stopped else (
            spec.max_speed_m_s if startled else min(spec.speed_m_s * factor, spec.max_speed_m_s))
        decel = spec.decel_m_s2
        blocker = blockers.get(spec.model)
        yield_limit = math.inf
        if blocker is not None:
            yield_limit = stopping(max(0.0, blocker[1] - _STANDOFF_M), decel or 2.0)
        if decel is None:
            return min(target, yield_limit), False
        if spec.kind == "vehicle":
            path = self._paths[spec.model]
            horizon = target * target / (2 * decel) + 5.0
            for ahead, curvature in path.curvature_ahead(state.arc_m, horizon):
                corner = math.sqrt(_VEHICLE_LATERAL_ACCEL_M_S2 / curvature)
                target = min(target, math.sqrt(corner * corner + 2 * decel * ahead))
            stop = self._next_vehicle_stop(state)
            if stop is not None:
                target = min(target, stopping(stop[0] - state.arc_m, decel))
        elif spec.behavior == "route":
            distance = self._distance_to_route_stop(state, target * target / (2 * decel) + 2.0)
            target = min(target, stopping(distance, decel))
        elif spec.behavior == "wander" and state.goal is not None:
            distance = math.hypot(state.goal[0] - state.x, state.goal[1] - state.y)
            target = min(target, stopping(distance, decel))
        return min(target, yield_limit), yield_limit < target

    def _next_vehicle_stop(self, state: AgentState) -> tuple[float, float, bool] | None:
        """(absolute arc, hold seconds, is_route_end) of the next dwell or route end ahead."""
        path = self._paths[state.spec.model]
        count = len(path.stops)
        if path.closed:
            for offset in range(count):
                lap, index = divmod(state.stop_ptr + offset, count)
                arc, _waypoint, hold = path.stops[index]
                if hold > _EPSILON:
                    return arc + lap * path.length, hold, False
            return None
        for index in range(state.stop_ptr, count):
            arc, _waypoint, hold = path.stops[index]
            if hold > _EPSILON:
                return arc, hold, False
        return path.length, 0.0, True

    def _advance_vehicle(self, state: AgentState, distance: float) -> None:
        path = self._paths[state.spec.model]
        stop = self._next_vehicle_stop(state)
        arc = state.arc_m + distance
        if stop is not None and arc >= stop[0] - 1e-6:
            arc = stop[0]
            state.speed_m_s = 0.0
            if stop[2]:
                state.finished = True
            else:
                state.hold_remaining_s = stop[1]
                # Advance the pointer past this stop.
                while True:
                    lap, index = divmod(state.stop_ptr, len(path.stops))
                    state.stop_ptr += 1
                    if path.stops[index][2] > _EPSILON and abs(path.stops[index][0] + lap * path.length - arc) < 1e-6:
                        break
        state.travel_m += arc - state.arc_m
        state.arc_m = arc
        x, y, heading, _ = path.sample(arc)
        state.x, state.y, state.yaw = x, y, heading

    def _advance_route(self, state: AgentState, distance: float) -> None:
        assert state.cursor is not None
        cursor = state.cursor
        remaining = distance
        for _ in range(max(4, len(state.spec.waypoints) * 4)):
            if cursor.finished or cursor.hold_remaining_s > _EPSILON:
                break
            target_index, direction = next_route_index(
                cursor.index, cursor.direction, len(state.spec.waypoints), state.spec.mode)
            if target_index is None:
                cursor.finished = True
                break
            cursor.direction = direction
            current, target = state.spec.waypoints[cursor.index], state.spec.waypoints[target_index]
            length = math.hypot(target.x - current.x, target.y - current.y)
            available = max(0.0, length - cursor.progress_m)
            step = min(remaining, available)
            cursor.progress_m += step
            remaining -= step
            state.travel_m += step
            if length > _EPSILON:
                fraction = cursor.progress_m / length
                state.x = current.x + (target.x - current.x) * fraction
                state.y = current.y + (target.y - current.y) * fraction
            if cursor.progress_m >= length - _EPSILON:
                cursor.index, cursor.progress_m = target_index, 0.0
                state.x, state.y = target.x, target.y
                if state.spec.mode == "once" and cursor.index == len(state.spec.waypoints) - 1:
                    cursor.finished = True
                cursor.hold_remaining_s = target.hold_s
                if cursor.hold_remaining_s > _EPSILON or cursor.finished:
                    state.speed_m_s = 0.0
                    break
            if remaining <= _EPSILON:
                break

    def _advance_wander(self, state: AgentState, distance: float) -> None:
        assert state.goal is not None and state.rng is not None
        remaining = math.hypot(state.goal[0] - state.x, state.goal[1] - state.y)
        state.travel_m += min(distance, remaining)
        if distance + _EPSILON < remaining:
            fraction = distance / remaining if remaining > _EPSILON else 1.0
            state.x += (state.goal[0] - state.x) * fraction
            state.y += (state.goal[1] - state.y) * fraction
            return
        state.x, state.y = state.goal
        state.speed_m_s = 0.0
        low, high = state.spec.pause_s
        state.pause_remaining_s = state.rng.uniform(low, high)
        if state.pause_remaining_s <= _EPSILON:
            state.goal = self._sample_area(state.spec, state.rng, (state.x, state.y))

    def _turn_toward(self, state: AgentState, heading: float, dt: float) -> None:
        rate = _PEDESTRIAN_TURN_RATE if state.spec.kind == "pedestrian" else _ANIMAL_TURN_RATE[state.spec.species]
        difference = _wrap_angle(heading - state.yaw)
        state.yaw = _wrap_angle(state.yaw + max(-rate * dt, min(rate * dt, difference)))

    def _step(self, dt: float, t: float, states: dict[str, AgentState] | None = None) -> None:
        """Advance ``states`` (default: the canonical fixed-step states) by ``dt`` at elapsed time ``t``."""
        working = self._states if states is None else states
        snapshot = {model: (state.travel_m, state.speed_m_s) for model, state in working.items()}
        blockers = self._blockers(working)
        for model in sorted(working):
            state = working[model]
            spec = state.spec
            state.yielding_to = blockers.get(model, (None,))[0]
            if spec.behavior == "idle":
                continue
            if spec.kind == "vehicle":
                self._step_vehicle(state, dt, t, blockers)
            elif spec.behavior == "follow":
                self._step_follower(state, dt, t, blockers, working, snapshot)
            else:
                self._step_walker(state, dt, t, blockers)

    def _step_vehicle(self, state: AgentState, dt: float, t: float, blockers) -> None:
        if state.finished:
            state.speed_m_s = 0.0
            return
        if state.hold_remaining_s > _EPSILON:
            state.hold_remaining_s = max(0.0, state.hold_remaining_s - dt)
            state.speed_m_s = 0.0
            return
        old = state.speed_m_s
        target, urgent = self._target_speed(state, t, blockers, dt)
        state.speed_m_s = self._speed_update(state, target, dt, urgent)
        self._advance_vehicle(state, (old + state.speed_m_s) * 0.5 * dt)

    def _step_walker(self, state: AgentState, dt: float, t: float, blockers) -> None:
        spec = state.spec
        if state.cursor is not None and state.cursor.hold_remaining_s > _EPSILON:
            state.cursor.hold_remaining_s = max(0.0, state.cursor.hold_remaining_s - dt)
            state.speed_m_s = 0.0
            return
        if state.cursor is not None:
            _factor, _stopped, _startled, reversed_now = self._event_effect(spec.model, t)
            if reversed_now != state.reversed_by_event:
                self._apply_route_reversal(state, reversed_now)
        if spec.behavior == "wander":
            _factor, _stopped, startled, _reversed_now = self._event_effect(spec.model, t)
            assert state.rng is not None
            if startled and state.startled_s <= 0.0:
                # Flee: a fresh far goal inside the area, taken at full speed.
                state.goal = self._sample_area(spec, state.rng, (state.x, state.y))
                state.pause_remaining_s = 0.0
            state.startled_s = 1.0 if startled else 0.0
            if state.pause_remaining_s > 0.0:
                state.pause_remaining_s = max(0.0, state.pause_remaining_s - dt)
                state.speed_m_s = 0.0
                if state.pause_remaining_s <= _EPSILON:
                    state.goal = self._sample_area(spec, state.rng, (state.x, state.y))
                return
            if spec.model in blockers:
                state.blocked_s += dt
                if state.blocked_s >= _WANDER_REPLAN_S and state.speed_m_s <= 0.05:
                    state.goal = self._sample_area(spec, state.rng, (state.x, state.y))
                    state.blocked_s = 0.0
            else:
                state.blocked_s = 0.0
        direction = self._path_direction(state)
        if direction is None and state.cursor is not None and not state.cursor.finished:
            self._advance_route(state, 0.0)  # skip zero-length legs
            direction = self._path_direction(state)
        target, urgent = (0.0, False) if direction is None else self._target_speed(state, t, blockers, dt)
        old = state.speed_m_s
        state.speed_m_s = self._speed_update(state, target, dt, urgent)
        distance = state.speed_m_s * dt if spec.accel_m_s2 is None else (old + state.speed_m_s) * 0.5 * dt
        if direction is not None and state.speed_m_s > _EPSILON:
            self._turn_toward(state, math.atan2(direction[1], direction[0]), dt)
        if state.cursor is not None:
            self._advance_route(state, distance)
        elif state.goal is not None:
            self._advance_wander(state, distance)

    def _step_follower(self, state: AgentState, dt: float, t: float, blockers, working, snapshot) -> None:
        spec = state.spec
        loop = self._loops[spec.model]
        travel, leader_speed = snapshot[spec.leader]
        leader_arc = self._leader_arc(spec, replace(working[spec.leader], travel_m=travel))
        gap = leader_arc - state.arc_m
        low, high = spec.gap_m
        aim = (low + high) / 2
        target = max(0.0, min(spec.max_speed_m_s or 0.0, leader_speed + 1.2 * (gap - aim)))
        if gap <= low:
            target = 0.0
        decel = spec.decel_m_s2 or 2.0
        comfort = min(target, math.sqrt(2 * decel * max(0.0, gap - low)))
        _factor, stopped, _startled, _reversed_now = self._event_effect(spec.model, t)
        if stopped:
            comfort = 0.0
        limit = comfort
        blocker = blockers.get(spec.model)
        if blocker is not None:
            limit = min(limit, math.sqrt(2 * decel * max(0.0, blocker[1] - _STANDOFF_M)))
        # Closing on the leader (it stopped short) or a blocker justifies hard braking.
        urgent = limit < comfort or gap < low + 0.3
        old = state.speed_m_s
        state.speed_m_s = self._speed_update(state, limit, dt, urgent)
        distance = (old + state.speed_m_s) * 0.5 * dt
        state.arc_m += distance
        state.travel_m += distance
        x, y, heading = loop.sample(state.arc_m)
        state.x, state.y = x, y
        if distance > _EPSILON:
            self._turn_toward(state, heading, dt)

    def update(self, sim_time_s: float) -> dict[str, tuple[float, float, float, float]]:
        """Advance to an absolute Gazebo simulation timestamp and return model poses."""
        if isinstance(sim_time_s, bool) or not isinstance(sim_time_s, (int, float)) or not math.isfinite(sim_time_s):
            raise ValueError("simulation time must be finite")
        sim_time = float(sim_time_s)
        if self._time_s is not None and sim_time < self._time_s:
            raise ValueError("simulation time must not move backwards")
        if self._time_s is None:
            self._time_s = sim_time
            self._start_time_s = sim_time
            self._next_step_s = sim_time + _MAX_STEP_S
            self._output_states = self._states
            return self._poses(self._states, sim_time)

        assert self._start_time_s is not None and self._next_step_s is not None
        while self._next_step_s <= sim_time + _EPSILON:
            self._step(_MAX_STEP_S, self._step_index * _MAX_STEP_S)
            self._step_index += 1
            self._next_step_s = self._start_time_s + (self._step_index + 1) * _MAX_STEP_S
        self._time_s = sim_time
        state_time = self._start_time_s + self._step_index * _MAX_STEP_S
        remaining = sim_time - state_time
        if remaining > _EPSILON:
            elapsed = self._step_index * _MAX_STEP_S
            self._output_states = {model: state.copy() for model, state in self._states.items()}
            self._step(remaining, elapsed, self._output_states)
            # Turn-rate-limited headings interpolate towards the fixed-step endpoint so yaw
            # samples are cadence-independent (vehicle/follower headings come from their paths).
            future = {model: state.copy() for model, state in self._states.items()}
            self._step(_MAX_STEP_S, elapsed, future)
            fraction = remaining / _MAX_STEP_S
            for model, state in self._output_states.items():
                if state.spec.behavior in ("idle", "follow") or state.spec.kind == "vehicle":
                    continue
                start_yaw = self._states[model].yaw
                state.yaw = _wrap_angle(start_yaw + _wrap_angle(future[model].yaw - start_yaw) * fraction)
        else:
            self._output_states = self._states
        return self._poses(self._output_states, sim_time)

    def _poses(self, states: dict[str, AgentState], timestamp: float) -> dict[str, tuple[float, float, float, float]]:
        ground = self.scenario.ground
        poses: dict[str, tuple[float, float, float, float]] = {}
        for model, state in sorted(states.items()):
            spec = state.spec
            if spec.behavior == "idle":
                assert spec.pose is not None
                yaw = spec.pose[2] + math.radians(spec.fidget_deg) * math.sin(
                    2.0 * math.pi * timestamp / spec.fidget_period_s)
                z = ground.height(state.x, state.y) + spec.z_offset
            elif state.cursor is not None:
                yaw, z = state.yaw, self._route_height(state)
            else:
                yaw, z = state.yaw, ground.height(state.x, state.y) + spec.z_offset
            poses[model] = (state.x, state.y, z, yaw)
        return poses

    def poses(self, sim_time_s: float | None = None) -> dict[str, tuple[float, float, float, float]]:
        timestamp = self._time_s if sim_time_s is None else sim_time_s
        if timestamp is None:
            timestamp = 0.0
        states = self._states if self._output_states is None else self._output_states
        return self._poses(states, timestamp)

    def states(self) -> dict[str, AgentState]:
        """Current output states (speed, yielding) for diagnostics and oracles."""
        return self._states if self._output_states is None else self._output_states


# --------------------------------------------------------------------------- Gazebo driver

def _publish_pose_vector(node: Any, world: str, pose_vector_type: Any, boolean_type: Any,
                         poses: dict[str, tuple[float, float, float, float]]) -> None:
    request = pose_vector_type()
    for model, (x, y, z, yaw) in sorted(poses.items()):
        pose = request.pose.add()
        pose.name = model
        pose.position.x, pose.position.y, pose.position.z = x, y, z
        pose.orientation.z = math.sin(yaw / 2.0)
        pose.orientation.w = math.cos(yaw / 2.0)
    service = f"/world/{world}/set_pose_vector"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            result, response = node.request(service, request, pose_vector_type, boolean_type, 1000)
            if result and response.data:
                return
            last_error = RuntimeError("service returned false")
        except Exception as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.1)
    raise RuntimeError(f"Gazebo set_pose_vector failed for world {world!r}: {last_error}") from last_error


def _append_trace(stream: TextIO, sim_time_s: float, poses: dict[str, tuple[float, float, float, float]]) -> None:
    stream.write(json.dumps({"t": sim_time_s, "agents": {model: list(pose) for model, pose in poses.items()}}) + "\n")
    stream.flush()


def run_driver(
    node: Any,
    pose_vector_type: Any,
    boolean_type: Any,
    engine: MotionEngine,
    sim_clock: Any,
    *,
    duration_s: float | None = None,
    trace_path: str | Path | None = None,
    stop_event: threading.Event | None = None,
    sleep: Any = time.sleep,
) -> None:
    interval = 1.0 / engine.scenario.rate_hz
    start_time = sim_clock()
    if start_time is None:
        raise RuntimeError(f"no simulation time received for world {engine.scenario.world!r}")
    next_update = start_time
    last_trace_time: float | None = None
    stop = stop_event or threading.Event()
    trace_stream: TextIO | None = None
    if trace_path is not None:
        trace_stream = Path(trace_path).open("a", encoding="utf-8")
    try:
        while not stop.is_set():
            current_time = sim_clock()
            if current_time is None:
                sleep(0.005)
                continue
            if duration_s is not None and current_time - start_time > duration_s:
                break
            if current_time >= next_update:
                poses = engine.update(current_time)
                if poses:
                    _publish_pose_vector(node, engine.scenario.world, pose_vector_type, boolean_type, poses)
                if trace_stream is not None and (last_trace_time is None or current_time - last_trace_time >= 0.5 - _EPSILON):
                    _append_trace(trace_stream, current_time, poses)
                    last_trace_time = current_time
                next_update += interval
                if next_update <= current_time:
                    next_update = current_time + interval
            if duration_s is not None and current_time - start_time >= duration_s:
                break
            if current_time < next_update:
                sleep(min(0.005, max(0.0, next_update - current_time)))
    finally:
        if trace_stream is not None:
            trace_stream.close()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--group", action="append", default=None, metavar="NAME")
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--duration-s", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.duration_s is not None and (not math.isfinite(args.duration_s) or args.duration_s < 0.0):
        parser.error("--duration-s must be a finite nonnegative number")
    try:
        scenario = load_scenario(args.scenario)
        engine = MotionEngine(scenario, args.group)
        bindings = load_gz_bindings(include_pose_vector=True)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"civilian_life: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()
    previous_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    stats_topic = f"/world/{scenario.world}/stats"
    sim_clock = _SimClock(stats_topic)
    try:
        if sim_clock() is None:
            print(f"Waiting for Gazebo world statistics on {stats_topic} "
                  f"(up to {_CLOCK_STARTUP_TIMEOUT_S:g} s)", flush=True)

        def keep_waiting(elapsed_s: float) -> bool:
            # The lockstep world only advances while a flight client holds SITL's MAVLink port,
            # so an idle simulation is not a failure; keep driving once the client connects.
            print(f"civilian_life: still waiting for Gazebo world statistics on {stats_topic} "
                  f"after {elapsed_s:g} s (no flight client holding SITL yet?); continuing", flush=True)
            return True

        sim_clock.wait_ready(timeout_s=_CLOCK_STARTUP_TIMEOUT_S, stop_event=stop,
                             keep_waiting=keep_waiting)
        if stop.is_set():
            return 0
        print(f"Driving {len(engine.agents)} civilian agents in {scenario.world} at {scenario.rate_hz:g} Hz", flush=True)
        run_driver(
            bindings.node(), bindings.pose_vector, bindings.boolean, engine, sim_clock,
            duration_s=args.duration_s, trace_path=args.trace, stop_event=stop,
        )
    except (OSError, RuntimeError) as exc:
        print(f"civilian_life: {exc}", file=sys.stderr)
        return 1
    finally:
        sim_clock.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
