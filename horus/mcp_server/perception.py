"""Camera frame, candidate, pointing and detection-mode MCP tools."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from horus.mcp_server.context import ToolContext, candidate_result, observation_age, plain, to_json

_LOG = logging.getLogger(__name__)


def _frame_tool_content(ctx: ToolContext, frame: Any, *, publish: bool = True) -> list[Any]:
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.types import TextContent

    from horus.perception import Frame
    from horus.perception.render import render_set_of_marks

    if not isinstance(frame, Frame):
        raise RuntimeError("perception returned a frame in an unsupported format")
    render_limit = min(640, int(ctx.config.perception.render_width_px))
    payload = render_set_of_marks(frame, max_dimension=render_limit)
    if publish and ctx.image_publisher is not None:
        try:
            ctx.image_publisher.publish(payload)
        except Exception:
            _LOG.warning("Optional set-of-mark image publishing failed", exc_info=True)
    timestamp, age = observation_age(frame)
    result = {
        "frame_id": payload.frame_id,
        "timestamp": timestamp or str(payload.timestamp),
        "pose": plain(payload.pose),
        "observation_age_s": age,
        "width": payload.width,
        "height": payload.height,
        "candidate_table": [candidate_result(candidate) for candidate in payload.candidates],
    }
    return [
        Image(data=payload.image_jpeg, format="jpeg"),
        TextContent(type="text", text=to_json(result)),
    ]


def _require_frame(ctx: ToolContext, frame_id: int | None = None) -> Any:
    frame = ctx.require_perception().get_frame(frame_id)
    if frame is None:
        raise RuntimeError("perception unavailable: waiting for the first camera frame")
    return frame


def get_frame(ctx: ToolContext, frame_id: int | None = None) -> list[Any]:
    """Return a ≤640px set-of-mark JPEG and its current tracker candidate table.

    ``frame_id`` selects a retained camera frame; an expired ID is stale and raises
    an error rather than silently returning an older frame. Tracker IDs are the only
    target identities; any object can be targeted through set_vocabulary or by
    point(frame_id, description|box), which seeds a new tracker ID when needed.
    Candidate source identifies detector, open-vocabulary, or point-created tracks.
    """
    return _frame_tool_content(ctx, _require_frame(ctx, frame_id))


def get_raw_frame(ctx: ToolContext, frame_id: int | None = None) -> list[Any]:
    """Return the camera frame as a JPEG at source resolution with nothing drawn on it.

    No boxes, tracker IDs, labels or downscaling: use it to inspect what the marks or
    get_frame's 640 px reduction hide. ``frame_id`` selects a retained frame as for
    get_frame. Its pixels are source-image pixels, the geometry point(frame_id, box)
    takes. It names no targets: tracker IDs come only from get_frame/list_candidates.
    """
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.types import TextContent

    from horus.perception import Frame
    from horus.perception.render import render_raw_jpeg

    frame = _require_frame(ctx, frame_id)
    if not isinstance(frame, Frame):
        raise RuntimeError("perception returned a frame in an unsupported format")
    image_jpeg = render_raw_jpeg(frame)
    height, width = (int(size) for size in frame.image.shape[:2])
    timestamp, age = observation_age(frame)
    result = {
        "frame_id": frame.frame_id,
        "timestamp": timestamp or str(frame.timestamp),
        "pose": plain(frame.pose),
        "observation_age_s": age,
        "width": width,
        "height": height,
    }
    return [
        Image(data=image_jpeg, format="jpeg"),
        TextContent(type="text", text=to_json(result)),
    ]


def list_candidates(ctx: ToolContext, passive: bool = False) -> list[Any]:
    """Return a ≤640px set-of-mark JPEG and the latest tracker candidate table.

    Use current tracker IDs as the only target identities. Any object can become a
    target through set_vocabulary or point(frame_id, description|box), which seeds
    a new tracker ID when no current track matches. Candidate source identifies
    detector, open-vocabulary, or point-created tracks. ``passive=True`` is for read-only
    displays: such a call is not logged, does not count as supervisory-link contact and
    does not replace the model's last set-of-mark view. Commanders leave it false.
    """
    return _frame_tool_content(ctx, _require_frame(ctx), publish=not passive)


def point(
    ctx: ToolContext,
    frame_id: int,
    description: str | None = None,
    box: list[float] | None = None,
    compensate: bool | None = None,
) -> str:
    """Resolve a description or old-frame pixel box to a current tracker ID.

    If no current track matches, a grounded/compensated box seeds a new tracker ID.
    ``frame_id`` identifies the observation containing the query. ``box`` is
    source-image [x1,y1,x2,y2] geometry, never an identity. Stale frames raise an
    error. ``compensate`` controls latency compensation for older frames; current
    frame boxes can seed directly. Tracker IDs remain the only identity handles.
    """
    system = ctx.require_perception()
    default_compensation = ctx.config.perception.point_compensation_enabled
    result = system.point(
        frame_id,
        description=description,
        box=box,
        compensate=default_compensation if compensate is None else compensate,
    )
    timestamp, age = observation_age(result)
    payload = plain(result)
    if not isinstance(payload, dict):
        payload = {"result": payload}
    candidate = payload.get("candidate")
    if age is None and isinstance(candidate, Mapping):
        timestamp, age = observation_age(candidate)
    payload["observed_at"] = timestamp
    payload["observation_age_s"] = age
    grounded = payload.get("grounded_box")
    if isinstance(grounded, dict) and "label" in grounded:
        grounded["untrusted_grounder_label"] = json.dumps(str(grounded.pop("label")), ensure_ascii=False)
    if isinstance(candidate, Mapping):
        payload["candidate"] = candidate_result(candidate)
    return to_json(payload)


def set_vocabulary(ctx: ToolContext, terms: list[str]) -> str:
    """Continuously detect up to sixteen free-text object terms in incoming frames.

    A match becomes an ``open_vocab`` candidate named after its term, with its matched text in
    ``attributes.grounder_span``, and raises an ``object_detected`` event.
    """
    return to_json(ctx.require_perception().set_vocabulary(terms))


def set_mode(ctx: ToolContext, name: str) -> str:
    """Run (``active``) or pause (``paused``) camera detection; not an autopilot flight mode."""
    return to_json(ctx.require_perception().set_mode(name))


def register(mcp: Any, ctx: ToolContext) -> None:
    ctx.tool(get_frame, structured_output=False)
    ctx.tool(get_raw_frame, structured_output=False)
    ctx.tool(list_candidates, structured_output=False)
    for function in (point, set_vocabulary, set_mode):
        ctx.tool(function)
