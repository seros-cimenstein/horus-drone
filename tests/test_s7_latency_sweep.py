from __future__ import annotations

import asyncio
import json

import pytest

from horus.perception.grounding import FrameBuffer, GroundingPointer
from horus.perception.types import (
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    StaleFrameError,
)
from scripts.latency_sweep import run_trial, select_largest_blue_candidate


def _candidate(
    candidate_id: int,
    bbox: tuple[float, float, float, float],
    *,
    frame_id: int = 1,
    timestamp: float = 1.0,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        bbox=bbox,
        class_name="person",
        confidence=0.9,
        frame_id=frame_id,
        timestamp=timestamp,
        attributes={"shirt_colour": "blue"},
    )


def test_blue_candidate_selection_is_largest_then_lowest_id():
    candidates = [
        {"id": 3, "bbox": [0, 0, 10, 10], "attributes": {"shirt_colour": "blue"}},
        {"id": 9, "bbox": [0, 0, 20, 20], "attributes": {"shirt_colour": "red"}},
        {"id": 7, "bbox": [0, 0, 20, 20], "attributes": {"shirt_colour": "blue"}},
        {"id": 2, "bbox": [0, 0, 20, 20], "attributes": {"shirt_colour": "blue"}},
    ]

    assert select_largest_blue_candidate(candidates) is candidates[3]
    assert select_largest_blue_candidate(candidates[1:2]) is None


class _GeometryOnlyTracker:
    def __init__(self, current: Candidate):
        self.current = current
        self.encode_calls = 0

    def validate_candidate(self, candidate_id: int):
        return self.current if candidate_id == self.current.id else None

    def encode(self, image, bbox):
        self.encode_calls += 1
        return None


class _DescriptionGrounder:
    def ground(self, image, description):
        assert description == "blue-shirt person"
        return [GroundingBox((0.0, 0.0, 10.0, 10.0), 0.95, description)]


def _buffer_with_reidentified_target() -> tuple[FrameBuffer, _GeometryOnlyTracker]:
    old = _candidate(1, (0.0, 0.0, 10.0, 10.0), frame_id=1, timestamp=1.0)
    middle = _candidate(3, (5.0, 0.0, 15.0, 10.0), frame_id=2, timestamp=2.0)
    current = _candidate(3, (10.0, 0.0, 20.0, 10.0), frame_id=3, timestamp=3.0)
    frames = FrameBuffer(max_seconds=10, max_frames=10)
    for frame_id, timestamp, candidates in (
        (1, 1.0, (old,)),
        (2, 2.0, (middle,)),
        (3, 3.0, (current,)),
    ):
        frames.append(Frame(frame_id, timestamp, image=None, candidates=candidates))
    return frames, _GeometryOnlyTracker(current)


def test_box_compensation_replays_multiple_buffered_frames_only_when_enabled():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker)

    with pytest.raises(NoGroundingMatch, match="compensation disabled"):
        pointer.point(1, [0, 0, 10, 10], compensate=False)
    assert tracker.encode_calls == 0

    result = pointer.point(1, box=[0, 0, 10, 10], compensate=True)
    assert result.frame_id == 3
    assert result.candidate.id == 3
    assert tracker.encode_calls == 1


def test_legacy_description_still_grounds_and_revalidates():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker, _DescriptionGrounder())

    result = pointer.point(1, "blue-shirt person")

    assert result.candidate.id == 3
    assert result.grounded_box.label == "blue-shirt person"


def test_box_rejects_malformed_and_expired_references_clearly():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker)

    with pytest.raises(ValueError, match="finite"):
        pointer.point(1, box=[0, 0, float("nan"), 10])

    expired = FrameBuffer(max_seconds=10, max_frames=2)
    for frame in frames.snapshot()[1:]:
        expired.append(frame)
    with pytest.raises(StaleFrameError, match="frame 1 is no longer"):
        GroundingPointer(expired, tracker).point(1, box=[0, 0, 10, 10])


def _wire_candidate(candidate_id: int, bbox: list[float]) -> dict:
    return {
        "id": candidate_id,
        "bbox": bbox,
        "attributes": {"shirt_colour": "blue"},
        "confidence": 0.9,
    }


def _wire_frame(
    frame_id: int,
    timestamp: float,
    bbox: list[float],
    candidate_id: int = 44,
) -> dict:
    candidate = _wire_candidate(candidate_id, bbox)
    table_row = dict(candidate)
    return {
        "frame_id": frame_id,
        "timestamp": timestamp,
        "width": 100,
        "height": 100,
        "candidates": [candidate],
        "candidate_table": [table_row],
    }


class _FakeMCP:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.frames = [
            _wire_frame(1, 1.0, [10, 10, 30, 30]),
            _wire_frame(2, 2.0, [40, 40, 60, 60], candidate_id=56),
            _wire_frame(3, 3.0, [40, 40, 60, 60], candidate_id=56),
        ]
        self.frame_index = 0
        self.point_candidate = _wire_candidate(55, [10, 10, 30, 30])
        self.timeline: list[str] = []
        self.sim_time_ms = 10_000
        self.clock_samples: list[int] = []
        self.action_sim_times: list[tuple[str, int]] = []

    async def call(self, tool: str, arguments: dict):
        self.timeline.append(tool)
        self.events.append((tool, dict(arguments)))
        if tool == "get_state":
            self.clock_samples.append(self.sim_time_ms)
            return {"vehicle": {"time_boot_ms": self.sim_time_ms}}
        if tool == "get_frame":
            self.sim_time_ms += 50
            frame = self.frames[min(self.frame_index, len(self.frames) - 1)]
            self.frame_index += 1
            return frame
        if tool in ("point", "follow"):
            self.action_sim_times.append((tool, self.sim_time_ms))
            if tool == "point":
                return {"frame_id": 1, "candidate": self.point_candidate, "match_confidence": 1.0}
            return {"task_id": "task-1", "status": "queued", **arguments}
        if tool == "get_task_status":
            return {"task_id": "task-1", "status": "running"}
        if tool == "cancel":
            return {"task_id": "task-1", "status": "cancelled"}
        raise AssertionError(f"unexpected MCP tool {tool}")


async def _clocked_sleep(seconds: float, clock: list[float], label: str, events: list):
    events.append((label, seconds))
    clock[0] += seconds


def test_box_trial_waits_for_sim_time_and_records_sim_and_wall_delays():
    fake = _FakeMCP()
    clock = [10.0]
    poll_events: list[tuple[str, float]] = []

    async def poll_wait(seconds: float):
        fake.sim_time_ms += int(round(seconds * 620.0))
        await _clocked_sleep(seconds, clock, "poll", poll_events)

    result = asyncio.run(run_trial(
        fake.call,
        delay_s=0.5,
        mode="box",
        compensate=True,
        wait=poll_wait,
        clock=lambda: clock[0],
        timeout_s=3.0,
        poll_interval_s=0.1,
    ))

    tool_order = [event[0] for event in fake.events]
    assert tool_order[:4] == ["get_state", "get_frame", "get_state", "get_state"]
    assert fake.action_sim_times[0][0] == "point"
    assert fake.action_sim_times[0][1] - result["sim_time_boot_ms_delay_start"] >= 500
    assert fake.action_sim_times[1][0] == "follow"
    assert result["sim_delay_s"] >= 0.5
    assert result["wall_delay_s"] > result["sim_delay_s"]
    assert result["wall_delay_basis"] == "measured_clock_wait"
    assert poll_events[0] == ("poll", 0.1)
    assert result["success"] is True
    assert result["result_status"] == "target_centered"
    assert result["time_to_target_s"] == pytest.approx(0.162)
    assert result["wall_time_to_target_s"] == pytest.approx(0.1)
    assert result["wrong_target"] is False
    assert result["raw_matching_data"]["candidate"]["id"] == 55
    point_arguments = next(args for tool, args in fake.events if tool == "point")
    follow_arguments = next(args for tool, args in fake.events if tool == "follow")
    assert point_arguments == {
        "frame_id": 1,
        "box": [10.0, 10.0, 30.0, 30.0],
        "compensate": True,
    }
    assert follow_arguments["id"] == 55
    assert json.loads(json.dumps(result))["success"] is True
    assert result["condition"] == {
        "delay_s": 0.5,
        "delay_clock": "sitl_sim_time",
        "action_mode": "box",
        "box_compensation": True,
        "compensation_source": "per_call_override",
    }
    assert result["raw_cleanup_data"]["status"] == "cancelled"


def test_netem_delay_is_wall_time_and_uses_measured_rtf_for_conversion():
    fake = _FakeMCP()
    clock = [10.0]
    poll_events: list[tuple[str, float]] = []

    async def poll_wait(seconds: float):
        fake.sim_time_ms += int(round(seconds * 620.0))
        await _clocked_sleep(seconds, clock, "poll", poll_events)

    result = asyncio.run(run_trial(
        fake.call,
        delay_s=0.5,
        mode="id",
        compensate=None,
        wait=poll_wait,
        clock=lambda: clock[0],
        timeout_s=3.0,
        poll_interval_s=0.1,
        delay_source="netem",
    ))

    assert fake.action_sim_times[0] == ("follow", 10_050)
    assert poll_events == [("poll", 0.1)]
    assert result["delay_clock"] == "wall_time"
    assert result["wall_delay_s"] == 0.5
    assert result["wall_delay_basis"] == "configured_tc_netem_delay"
    assert result["sim_delay_estimated"] is True
    assert result["measured_rtf"] is not None
    assert result["sim_delay_s"] == pytest.approx(0.5 * result["measured_rtf"])
    assert result["time_to_target_s"] > result["wall_time_to_target_s"]


def test_task_expiry_without_centering_is_not_success():
    fake = _FakeMCP()
    fake.frames = [
        _wire_frame(1, 1.0, [10, 10, 30, 30]),
        _wire_frame(2, 2.0, [10, 10, 30, 30]),
    ]

    async def no_wait(_: float):
        return None

    async def call(tool: str, arguments: dict):
        if tool in ("get_state", "get_frame"):
            return await fake.call(tool, arguments)
        if tool == "follow":
            return {"task_id": "task-1", "status": "running"}
        if tool == "get_task_status":
            return {"task_id": "task-1", "status": "expired"}
        raise AssertionError(tool)

    result = asyncio.run(run_trial(
        call,
        delay_s=0.0,
        mode="id",
        compensate=None,
        wait=no_wait,
        clock=lambda: 1.0,
    ))

    assert result["result_status"] == "task_expired"
    assert result["success"] is False
    assert result["time_to_target_s"] is None



def test_centered_red_distractor_sets_wrong_target_metric():
    fake = _FakeMCP()
    fake.frames = [
        _wire_frame(1, 1.0, [10, 10, 30, 30]),
        _wire_frame(2, 2.0, [10, 10, 30, 30]),
    ]
    red_candidate = {
        "id": 99,
        "bbox": [40, 40, 60, 60],
        "attributes": {"shirt_colour": "red"},
        "confidence": 0.9,
    }
    fake.frames[1]["candidates"].append(red_candidate)
    fake.frames[1]["candidate_table"].append(dict(red_candidate))

    async def no_wait(_: float):
        return None

    async def call(tool: str, arguments: dict):
        if tool in ("get_state", "get_frame"):
            return await fake.call(tool, arguments)
        if tool == "follow":
            return {"task_id": "task-1", "status": "running"}
        if tool == "get_task_status":
            return {"task_id": "task-1", "status": "expired"}
        raise AssertionError(tool)

    result = asyncio.run(run_trial(
        call,
        delay_s=0.0,
        mode="id",
        compensate=None,
        wait=no_wait,
        clock=lambda: 1.0,
    ))

    assert result["success"] is False
    assert result["wrong_target"] is True



def test_decodes_fastmcp_wrapped_json_text_result():
    from mcp.types import CallToolResult, TextContent

    from scripts.latency_sweep import _decode_tool_result, _time_boot_ms

    text = '{"state": "IDLE", "vehicle": {"time_boot_ms": 49050}}'
    response = CallToolResult(content=[TextContent(type="text", text=text)], structuredContent={"result": text})
    assert _time_boot_ms(_decode_tool_result(response)) == 49050



def test_target_below_image_center_counts_as_centered_for_heading_tracking():
    from scripts.latency_sweep import _candidate_centered

    frame = {"width": 640, "height": 480, "candidate_table": [{"id": 3, "bbox": [300, 237, 340, 343]}]}
    assert _candidate_centered(frame, 3, 0.10)  # 50 px below centre, horizontally centred
    frame["candidate_table"][0]["bbox"] = [400, 180, 440, 300]
    assert not _candidate_centered(frame, 3, 0.10)  # 100 px right of centre
