"""Gazebo ``gz topic`` protobuf-text framing and parsing, shared by every CLI reader.

Standard library only: the landing-error script, the simulation clock and the campaign episode
all read Gazebo topics through the ``gz`` CLI, never through Python transport bindings.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

# Gazebo protobuf-text messages are echoed one message at a time; this only bounds a
# pathological stream. A message that exceeds it is flushed (never silently truncated).
TOPIC_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
TOPIC_BODY_MARKERS = ("pose {", "real_time_factor:")
RTF_WINDOW_S = 10.0

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NAME = re.compile(r'\bname\s*:\s*"((?:\\.|[^"\\])*)"')


def _brace_delta(text: str) -> int:
    """Net protobuf-text brace delta of a line, ignoring braces inside quoted strings."""
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def topic_messages(lines: Iterable[str], *, max_message_bytes: int = TOPIC_MAX_MESSAGE_BYTES
                   ) -> Iterator[tuple[str, bool]]:
    """Split a ``gz topic -e`` protobuf-text stream into whole messages.

    Messages are delimited by blank lines, ``---`` markers or a top-level ``header {``
    once a body is present.  Nested ``header {`` blocks (every Pose_V entity has one)
    never split a message.  Yields ``(text, truncated)``; ``truncated`` is True only
    when one message exceeds ``max_message_bytes`` and had to be flushed early.
    """
    buffer: list[str] = []
    pending_bytes = 0
    depth = 0
    has_body = False

    def flush() -> str:
        nonlocal pending_bytes, depth, has_body
        text = "".join(buffer)
        buffer.clear()
        pending_bytes = 0
        depth = 0
        has_body = False
        return text

    for line in lines:
        stripped = line.strip()
        if stripped == "":
            if buffer and depth == 0:
                yield flush(), False
            continue
        if stripped == "---" or (stripped.startswith("header {") and depth == 0 and has_body):
            if buffer:
                yield flush(), False
            if stripped == "---":
                continue
        if not has_body and any(marker in line for marker in TOPIC_BODY_MARKERS):
            has_body = True
        depth = max(0, depth + _brace_delta(line))
        buffer.append(line)
        pending_bytes += len(line)
        if pending_bytes > max_message_bytes:
            yield flush(), True
    if buffer:
        yield flush(), False


def braced(text: str, label: str) -> list[str]:
    """Extract protobuf-text blocks for ``label`` while respecting quoted strings."""
    blocks: list[str] = []
    for match in re.finditer(rf"\b{re.escape(label)}\s*\{{", text):
        start = match.end()
        depth = 1
        quoted = escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[start:index])
                    break
    return blocks


def scalar(block: str, field: str) -> float | None:
    """First numeric value of ``field`` in a protobuf-text block, or None when omitted."""
    match = re.search(rf"\b{re.escape(field)}\s*:\s*({_NUMBER})", block)
    return None if match is None else float(match.group(1))


def repeated_scalars(block: str, field: str) -> list[float]:
    """Values of a repeated numeric field, packed (``k: [..]``) or one per line."""
    packed = re.search(rf"\b{re.escape(field)}\s*:\s*\[([^\]]*)\]", block)
    if packed is not None:
        return [float(value) for value in re.findall(_NUMBER, packed.group(1))]
    return [float(match.group(1))
            for match in re.finditer(rf"\b{re.escape(field)}\s*:\s*({_NUMBER})", block)]


def named_poses(text: str) -> Iterator[tuple[str, str, str | None]]:
    """``(name, position block, orientation block or None)`` for every named Pose_V entry."""
    for block in braced(text, "pose"):
        name = _NAME.search(block)
        positions = braced(block, "position")
        if name is None or not positions:
            continue
        orientations = braced(block, "orientation")
        yield name.group(1), positions[0], orientations[0] if orientations else None


def parse_pose_info(text: str) -> dict[str, Any] | None:
    """One Pose_V sample: ``models[name] = [x, y, z, yaw, roll, pitch]`` plus its sim time."""
    models: dict[str, list[float]] = {}
    for name, position_block, orientation_block in named_poses(text):
        if orientation_block is None:
            continue
        position = [scalar(position_block, key) for key in ("x", "y", "z")]
        orientation = [scalar(orientation_block, key) for key in ("x", "y", "z", "w")]
        if any(value is None for value in (*position, *orientation)):
            continue
        ox, oy, oz, ow = (float(value) for value in orientation)
        yaw = math.atan2(2.0 * (ow * oz + ox * oy), 1.0 - 2.0 * (oy * oy + oz * oz))
        roll = math.atan2(2.0 * (ow * ox + oy * oz), 1.0 - 2.0 * (ox * ox + oy * oy))
        pitch = math.asin(max(-1.0, min(1.0, 2.0 * (ow * oy - oz * ox))))
        models[name] = [float(position[0]), float(position[1]), float(position[2]), yaw, roll, pitch]
    if not models:
        return None
    stamps = braced(text, "stamp")
    sim_time = None
    if stamps:
        sec, nsec = scalar(stamps[0], "sec"), scalar(stamps[0], "nsec")
        if sec is not None:
            sim_time = sec + (nsec or 0.0) * 1e-9
    return {"wall_time": time.time(), "sim_time": sim_time, "models": models}


def sim_time_s(text: str) -> float | None:
    """Simulation time of a world-statistics message; omitted ``sec``/``nsec`` fields are zero."""
    blocks = braced(text, "sim_time")
    if not blocks:
        return None
    sec, nsec = scalar(blocks[0], "sec"), scalar(blocks[0], "nsec")
    return (sec or 0.0) + (nsec or 0.0) * 1e-9


def parse_stats(text: str) -> dict[str, float] | None:
    """One world-statistics sample: simulation time and the wall time it was read.

    The message's own ``real_time_factor`` is wrong under lockstep (it read 0.01-0.02 on forest
    runs whose sim time advanced at 0.22 of wall time); rates come from ``measured_rate``.
    """
    sim_time = sim_time_s(text)
    return None if sim_time is None else {"wall_time": time.time(), "sim_time_s": sim_time}


def parse_camera_info(text: str, topic: str) -> dict[str, Any]:
    """Runtime CameraInfo intrinsics (``intrinsics.k``, else ``projection.p``) and the HFOV."""
    width, height = scalar(text, "width"), scalar(text, "height")
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("Gazebo CameraInfo has no valid image dimensions")
    matrices = braced(text, "intrinsics")
    k = repeated_scalars(matrices[0], "k") if matrices else []
    source = "intrinsics.k"
    if len(k) < 9:
        projections = braced(text, "projection")
        p = repeated_scalars(projections[0], "p") if projections else []
        if len(p) < 12:
            raise ValueError("Gazebo CameraInfo lacks a 3x3 intrinsic or 3x4 projection matrix")
        fx, cx, fy, cy = p[0], p[2], p[5], p[6]
        source = "projection.p"
    else:
        fx, cx, fy, cy = k[0], k[2], k[4], k[5]
    width, height = int(width), int(height)
    if fx <= 0 or fy <= 0 or not all(math.isfinite(value) for value in (fx, fy, cx, cy)):
        raise ValueError("Gazebo CameraInfo contains invalid focal lengths or principal point")
    hfov_rad = math.atan2(cx, fx) + math.atan2(width - cx, fx)
    return {
        "topic": topic, "width": width, "height": height, "fx_px": fx, "fy_px": fy,
        "cx_px": cx, "cy_px": cy, "horizontal_fov_rad": hfov_rad,
        "horizontal_fov_deg": math.degrees(hfov_rad), "intrinsics_source": source,
    }


def merge_pose_models(previous: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Gazebo's sparse Pose_V entities while advancing the snapshot timestamp."""
    combined = dict(previous.get("models", {})) if isinstance(previous, Mapping) else {}
    models = update.get("models")
    if isinstance(models, Mapping):
        combined.update(models)
    result = dict(update)
    result["models"] = combined
    return result


def measured_rate(rows: Sequence[Mapping[str, Any]], window_s: float = RTF_WINDOW_S) -> float | None:
    """Simulation seconds per wall second over the last ``window_s`` of statistics samples."""
    timed = [row for row in rows if isinstance(row.get("sim_time_s"), (int, float))
             and isinstance(row.get("wall_time"), (int, float))]
    if len(timed) < 2:
        return None
    latest = timed[-1]
    earlier = next((row for row in reversed(timed[:-1])
                    if latest["wall_time"] - row["wall_time"] >= window_s), timed[0])
    elapsed = latest["wall_time"] - earlier["wall_time"]
    advanced = latest["sim_time_s"] - earlier["sim_time_s"]
    return advanced / elapsed if elapsed > 0 and advanced >= 0 else None


class TopicReader:
    """Read Gazebo protobuf text through the CLI, never through Python transport bindings."""

    def __init__(self, topic: str, output: Path, parser: Callable[[str], dict[str, Any] | None], *,
                 stop: threading.Event, max_hz: float = 5.0, sample_latest: bool = False,
                 rate_reference: TopicReader | None = None):
        self.topic, self.output, self.parser = topic, output, parser
        self.stop_event = stop
        self.max_hz = max_hz
        self.sample_latest = sample_latest
        self.rate_reference = rate_reference
        self.last_sim_time: float | None = None
        self.rows: list[dict[str, Any]] = []
        self.latest_row: dict[str, Any] | None = None
        self.lock = threading.Lock()
        self.error: str | None = None
        self.parse_failures = 0
        self.truncated_messages = 0
        self.messages = 0
        self.process: subprocess.Popen[Any] | None = None
        if not sample_latest:
            self.process = subprocess.Popen(
                ["stdbuf", "-oL", "gz", "topic", "-e", "-t", topic], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        self.thread = threading.Thread(target=self._read, name=f"topic-{topic.rsplit('/', 1)[-1]}", daemon=True)
        self.thread.start()

    def _append(self, text: str) -> None:
        row = self.parser(text)
        if row is None:
            with self.lock:
                self.parse_failures += 1
                if self.error is None:
                    self.error = f"gz topic payload on {self.topic} did not parse"
            return
        sim_time = row.get("sim_time", row.get("sim_time_s"))
        if isinstance(sim_time, (int, float)) and self.last_sim_time is not None:
            if 0.0 <= float(sim_time) - self.last_sim_time < 1.0 / self.max_hz:
                return
        if isinstance(sim_time, (int, float)):
            self.last_sim_time = float(sim_time)
        with self.lock:
            self.messages += 1
            if isinstance(row.get("models"), Mapping):
                self.latest_row = merge_pose_models(self.latest_row, row)
            else:
                self.latest_row = row
            self.rows.append(row)
            if len(self.rows) > 20000:
                del self.rows[:1000]
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")

    def _consume(self, lines: Iterable[str]) -> None:
        for text, truncated in topic_messages(lines):
            if truncated:
                with self.lock:
                    self.truncated_messages += 1
                    self.error = (f"gz topic message on {self.topic} exceeded "
                                  f"{TOPIC_MAX_MESSAGE_BYTES} bytes; flushed early")
            self._append(text)

    def _read(self) -> None:
        if self.sample_latest:
            self._read_latest()
            return
        assert self.process is not None and self.process.stdout is not None
        self._consume(self.process.stdout)
        if self.process.returncode not in (None, 0) and not self.stop_event.is_set():
            self.error = f"gz topic reader exited with status {self.process.returncode}"

    def _read_latest(self) -> None:
        # Pose info can publish thousands of large snapshots per wall second. Repeated
        # one-message CLI subscriptions avoid building an unbounded stdout backlog.
        while not self.stop_event.is_set():
            try:
                result = subprocess.run(
                    ["gz", "topic", "-e", "-n", "1", "-t", self.topic],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
                if result.returncode == 0:
                    self.error = None
                    self._consume(result.stdout.splitlines(keepends=True))
                else:
                    self.error = f"gz topic sampler exited with status {result.returncode}"
            except subprocess.TimeoutExpired:
                self.error = "gz topic sampler timed out waiting for a pose message"
            measured = self.rate_reference.measured_rate() if self.rate_reference is not None else None
            rate = measured if measured is not None and measured > 0 else 0.2
            interval = 1.0 / (self.max_hz * rate)
            self.stop_event.wait(min(30.0, max(0.05, interval)))

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.rows)

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return self.latest_row

    def measured_rate(self, window_s: float = RTF_WINDOW_S) -> float | None:
        with self.lock:
            return measured_rate(self.rows[-200:], window_s)

    def status(self) -> dict[str, Any]:
        """Reader health for the episode result: errors are never swallowed."""
        with self.lock:
            return {
                "topic": self.topic,
                "error": self.error,
                "messages": self.messages,
                "parse_failures": self.parse_failures,
                "truncated_messages": self.truncated_messages,
                "samples": len(self.rows),
            }

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=5)
