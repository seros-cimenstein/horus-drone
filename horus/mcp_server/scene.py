"""Camera-relative scene MCP tools: image motion and region watches."""

from __future__ import annotations

from typing import Any

from horus.mcp_server.context import ToolContext, to_json


def get_candidate_motion(ctx: ToolContext, id: int) -> str:
    """Estimate a tracker ID's recent IMAGE motion from processed camera frames.

    Units are fractions of frame width/height per second (x right, y down): camera-relative
    image motion, never a metric or ground speed. ``camera_motion.status`` is "moving" when
    measured vehicle/gimbal motion exceeded tolerance (the rates include ego-motion),
    "within_tolerance" when it did not (sub-tolerance motion and the components listed in
    ``camera_motion.unmeasured`` may still contribute), or "unknown" without fresh camera
    state. When
    ``available`` is false, ``reason`` says why (no_frames, unknown_track, stale_frames,
    not_visible, insufficient_samples); never read a missing estimate as zero motion.
    Labels are quoted untrusted detector strings.
    """
    return to_json(ctx.perception_system().candidate_motion(id))


def watch_region(
    ctx: ToolContext,
    name: str,
    box: list[float],
    class_name: str | None = None,
    dwell_s: float | None = None,
    absence_s: float = 1.0,
    ttl_s: float | None = None,
) -> str:
    """Watch a fixed-camera image rectangle and publish region events to wait_events.

    ``box`` is [x1, y1, x2, y2] in fractions of the current frame (0..1, x right, y down).
    A target is inside when its box centre is inside. ``class_name`` matches any detector or
    open-vocabulary label case-insensitively (omit for all classes). Events: region_enter,
    region_exit (after ``absence_s`` unseen inside; reason left_region or disappeared),
    region_dwell (after ``dwell_s`` of continuous observation inside), region_watch_paused /
    region_watch_resumed and region_watch_expired (``ttl_s``). The watch is anchored to the
    current camera position, heading, vehicle roll/pitch and gimbal attitude: a measured move
    beyond tolerance pauses it with no transitions, and on return it re-adopts occupancy
    without inventing enter/exit events. Unmeasured camera changes
    (``unmeasured_camera_components``, e.g. zoom) are not detected.
    Targets already inside are initial occupants. Reusing a name replaces that watch. The
    watch never moves the vehicle; choose bounded skills yourself.
    """
    system = ctx.require_perception()
    return to_json(
        system.watch_region(
            name, box, class_name=class_name, dwell_s=dwell_s, absence_s=absence_s, ttl_s=ttl_s
        )
    )


def list_region_watches(ctx: ToolContext) -> str:
    """List region watches: status (active, stale, paused + reason), camera reference,
    occupants with timers, and stream freshness. Stale/paused occupancy is last confirmed
    state, not current truth."""
    return to_json(ctx.perception_system().list_region_watches())


def remove_region_watch(ctx: ToolContext, watch_id: str) -> str:
    """Remove a region watch by ID; unknown or expired IDs are an error.

    The watch generates no new events once this returns; events it published earlier stay
    in the wait_events history.
    """
    return to_json(ctx.perception_system().remove_region_watch(watch_id))


def register(mcp: Any, ctx: ToolContext) -> None:
    for function in (get_candidate_motion, watch_region, list_region_watches, remove_region_watch):
        ctx.tool(function)
