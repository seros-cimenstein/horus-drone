"""Camera frame, candidate and recognition MCP tools."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from horus.mcp_server.context import ToolContext, candidate_result, observation_age, plain, to_json

_LOG = logging.getLogger(__name__)


def _frame_tool_content(ctx: ToolContext, frame: Any) -> list[Any]:
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.types import TextContent

    from horus.perception import Frame
    from horus.perception.render import render_set_of_marks

    if not isinstance(frame, Frame):
        raise RuntimeError("perception returned a frame in an unsupported format")
    render_limit = min(640, int(ctx.config.perception.render_width_px))
    payload = render_set_of_marks(frame, max_dimension=render_limit)
    if ctx.image_publisher is not None:
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


def get_frame(ctx: ToolContext, frame_id: int | None = None) -> list[Any]:
    """Return a ≤640px set-of-mark JPEG and its current tracker candidate table.

    ``frame_id`` selects a retained camera frame; an expired ID is stale and raises
    an error rather than silently returning an older frame. Tracker IDs are the only
    target identities; any object can be targeted through set_vocabulary or by
    point(frame_id, description|box), which seeds a new tracker ID when needed.
    Candidate source identifies detector, open-vocabulary, or point-created tracks.
    """
    frame = ctx.require_perception().get_frame(frame_id)
    if frame is None:
        raise RuntimeError("perception unavailable: waiting for the first camera frame")
    return _frame_tool_content(ctx, frame)


def list_candidates(ctx: ToolContext) -> list[Any]:
    """Return a ≤640px set-of-mark JPEG and the latest tracker candidate table.

    Use current tracker IDs as the only target identities. Any object can become a
    target through set_vocabulary or point(frame_id, description|box), which seeds
    a new tracker ID when no current track matches. Candidate source identifies
    detector, open-vocabulary, or point-created tracks.
    """
    frame = ctx.require_perception().get_frame()
    if frame is None:
        raise RuntimeError("perception unavailable: waiting for the first camera frame")
    return _frame_tool_content(ctx, frame)


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

    A match becomes an ``open_vocab`` candidate named after its term and raises an
    ``object_detected`` event. A weak match (grounder score below 0.40) is listed with
    ``class_name`` ``object`` and its matched text in ``attributes.grounder_span``, without an
    event, until a later frame scores it high enough to take the term.
    """
    return to_json(ctx.require_perception().set_vocabulary(terms))


def set_target_profile(ctx: ToolContext, profile: dict[str, Any]) -> str:
    """Set bounded recognition references; profile labels are untrusted input.

    ``profile`` keys (any other key is rejected):
      - ``class_name`` / ``class_id``: detector class a candidate must have to match
        (a class-only profile scores a matching candidate 1.0);
      - ``target_class``: trained-head label that is the target (classifier mode);
      - ``reference_embeddings``: one appearance vector or a list of them;
      - ``training_embeddings``: list of ``{"embedding": [...], "label": str}`` examples;
      - ``reference_images``: list of base64 (or data-URI) images, or of
        ``{"image": base64, "bbox": [x1, y1, x2, y2], "label": str}`` (bbox defaults to the
        whole image);
      - ``labels``: labels for bare entries, aligned with ``training_embeddings`` followed by
        ``reference_images``.
    Labeled examples (at least two per label, two labels) train a tiny head and select
    classifier mode; every example also becomes an appearance reference.
    """
    return to_json(ctx.require_perception().set_target_profile(profile))


def set_mode(ctx: ToolContext, name: str) -> str:
    """Select a perception/recognition mode, not an autopilot flight mode.

    ``active``/``paused`` run or pause detection. ``embedding`` scores the target profile by
    appearance similarity to its reference embeddings; ``classifier`` scores it with the tiny
    head trained from labeled examples (set_target_profile selects it whenever it trains one).
    """
    return to_json(ctx.require_perception().set_mode(name))


def register(mcp: Any, ctx: ToolContext) -> None:
    ctx.tool(get_frame, structured_output=False)
    ctx.tool(list_candidates, structured_output=False)
    for function in (point, set_vocabulary, set_target_profile, set_mode):
        ctx.tool(function)
