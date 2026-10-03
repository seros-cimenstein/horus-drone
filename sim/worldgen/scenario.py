from __future__ import annotations

import math
from sim.worldgen.layout import PersonPlacement
from sim.worldgen.sdf import f
from sim.worldgen.surface import CANONICAL_MAP, MapSpec


def person_record(person: PersonPlacement) -> dict:
    record = {"model": person.model, "role": person.role, "asset": person.asset,
              "position": person.position, "yaw": person.yaw,
              "behavior": person.behavior, "group": person.group}
    if person.waypoints:
        record.update(waypoints=person.waypoints, speed=person.speed, mode=person.mode)
    if person.accel is not None:
        record["accel"] = person.accel
    if person.hold_s is not None:
        record["hold_s"] = person.hold_s
    if person.start_offset_s is not None:
        record["start_offset_s"] = person.start_offset_s
    return record


def _point(x: float, y: float) -> str:
    return f"[{f(x)}, {f(y)}]"


def yaml_agent(agent: dict) -> list[str]:
    kind = agent.get("kind", "pedestrian")
    lines = [
        f"  - model: {agent['model']}",
        f"    kind: {kind}",
        f"    group: {agent.get('group', 'ambient')}",
        f"    behavior: {agent['behavior']}",
    ]
    if kind == "animal":
        lines.append(f"    species: {agent['species']}")
    if "radius" in agent:
        lines.append(f"    radius_m: {f(agent['radius'])}")
    if "footprint" in agent:
        footprint = agent["footprint"]
        lines.append(f"    footprint: {{length_m: {f(footprint['length_m'])}, width_m: {f(footprint['width_m'])}}}")
    if agent["behavior"] in ("route", "wander", "follow"):
        lines.append(f"    speed_m_s: {f(agent['speed'])}")
        if "max_speed" in agent:
            lines.append(f"    max_speed_m_s: {f(agent['max_speed'])}")
        if "accel" in agent:
            lines.append(f"    accel_m_s2: {f(agent['accel'])}")
        if "decel" in agent:
            lines.append(f"    decel_m_s2: {f(agent['decel'])}")
        if "turn_radius" in agent:
            lines.append(f"    turn_radius_m: {f(agent['turn_radius'])}")
    if agent["behavior"] == "route":
        lines.append(f"    mode: {agent.get('mode', 'loop')}")
        if agent.get("start_offset_s") is not None:
            lines.append(f"    start_offset_s: {f(agent['start_offset_s'])}")
        lines.append("    waypoints:")
        for point in agent["waypoints"]:
            values = [f"x: {f(point[0])}", f"y: {f(point[1])}"]
            if len(point) > 2:
                values.append(f"hold_s: {f(point[2])}")
            lines.append("      - {" + ", ".join(values) + "}")
    elif agent["behavior"] == "wander":
        lines.append("    area: [" + ", ".join(_point(*p) for p in agent["area"]) + "]")
        if "pause" in agent:
            lines.append(f"    pause_s: [{f(agent['pause'][0])}, {f(agent['pause'][1])}]")
        if agent.get("avoid"):
            lines.append("    avoid_agents: true")
    elif agent["behavior"] == "follow":
        lines.append(f"    leader: {agent['leader']}")
        lines.append(f"    gap_m: [{f(agent['gap'][0])}, {f(agent['gap'][1])}]")
    elif agent["behavior"] == "idle":
        x, y = agent["position"]
        lines.append(f"    pose: {{x: {f(x)}, y: {f(y)}, yaw_deg: {f(math.degrees(agent.get('yaw', 0.0)))}}}")
        if agent.get("hold_s") is not None:
            lines.append("    fidget_deg: 10")
            lines.append("    fidget_period_s: 8")
    lines.append("    z_offset: 0.0")
    return lines


def scenario_text(name: str, seed: int, agents: list[dict],
                  ground_file: str | None = None, *, events: list[dict] | None = None,
                  interactions: bool = False, ground_map: MapSpec = CANONICAL_MAP) -> str:
    lines = [f"world: {name}", "rate_hz: 10", f"seed: {seed}"]
    if interactions:
        lines.append("interactions: true")
    if ground_file is not None:
        origin = repr(float(ground_map.min))
        lines.extend(("ground:", f"  origin_xy: [{origin}, {origin}]",
                      f"  cell_m: {repr(float(ground_map.cell_m))}",
                      f"  file: {ground_file}"))
    lines.append("agents:")
    for agent in agents:
        lines.extend(yaml_agent(agent))
    if events:
        lines.append("events:")
        for event in events:
            fields = [f"t_s: {f(event['t_s'])}", f"model: {event['model']}", f"type: {event['type']}"]
            for key in ("duration_s", "factor"):
                if key in event:
                    fields.append(f"{key}: {f(event[key])}")
            lines.append("  - {" + ", ".join(fields) + "}")
    return "\n".join(lines) + "\n"
