"""Shared Gazebo Python bindings and simulation-clock support for drivers."""

from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GzBindings:
    node: Any
    pose: Any
    boolean: Any
    pose_vector: Any | None = None


def load_gz_bindings(*, include_pose_vector: bool = False) -> GzBindings:
    """Load Gazebo bindings lazily so pure motion code works without Gazebo installed."""
    try:
        from gz.msgs10.boolean_pb2 import Boolean

        pose_vector = None
        if include_pose_vector:
            # Import the vector descriptor before Pose so protobuf registers its nested type.
            from gz.msgs10.pose_v_pb2 import Pose_V

            pose_vector = Pose_V
        from gz.msgs10.pose_pb2 import Pose
        from gz.transport13 import Node
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Gazebo Python bindings are unavailable; install python3-gz-transport13 and "
            "python3-gz-msgs10"
        ) from exc
    return GzBindings(Node, Pose, Boolean, pose_vector)


class _SimClock:
    """Gazebo simulation time from world statistics, extrapolated between messages.

    Statistics are read from a ``gz topic -e`` subprocess rather than an in-process subscription:
    a Python transport callback in this process starves the thread that delivers set_pose service
    replies, so requests time out. A pipe reader thread releases the GIL while it waits.
    """

    _SIM_TIME = re.compile(r"sim_time\s*\{([^}]*)\}")
    _SIM_SEC = re.compile(r"\bsec:\s*(\d+)")
    _SIM_NSEC = re.compile(r"\bnsec:\s*(\d+)")
    _RTF = re.compile(r"real_time_factor:\s*([0-9.eE+-]+)")

    def __init__(self, topic: str = "/world/horus_indoor/stats") -> None:
        self.topic = topic
        self._sample: tuple[float, float, float] | None = None  # sim_s, wall_s, real_time_factor
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            # stdbuf: the gz CLI block-buffers when piped, which would deliver the clock in bursts.
            ["stdbuf", "-oL", "gz", "topic", "-e", "-t", topic],
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        buffer: list[str] = []
        assert self._process.stdout is not None
        for line in self._process.stdout:
            buffer.append(line)
            if not line.strip():  # messages are separated by a blank line
                self._parse("".join(buffer))
                buffer.clear()

    def _parse(self, text: str) -> None:
        sim = self._SIM_TIME.search(text)
        rtf = self._RTF.search(text)
        if sim is None:
            return
        sec = self._SIM_SEC.search(sim.group(1))
        nsec = self._SIM_NSEC.search(sim.group(1))
        sim_seconds = int(sec.group(1) if sec else 0) + int(nsec.group(1) if nsec else 0) * 1e-9
        factor = float(rtf.group(1)) if rtf else 0.0
        with self._lock:
            self._sample = (
                sim_seconds,
                time.monotonic(),
                max(0.0, factor) if math.isfinite(factor) else 0.0,
            )

    def close(self) -> None:
        self._process.terminate()

    def wait_ready(
        self, timeout_s: float = 30.0, stop_event: threading.Event | None = None,
        keep_waiting: Callable[[float], bool] | None = None,
    ) -> None:
        """Block until the first statistics message arrives.

        ``keep_waiting(elapsed_s)`` is consulted once ``timeout_s`` elapses; returning True
        extends the wait by another ``timeout_s``. Callers use it when the lockstep world
        legitimately cannot run yet (no flight client connected), where a fatal timeout would
        stop a simulation that is merely idle. A dead ``gz topic`` reader stays fatal.
        """
        started = time.monotonic()
        deadline = started + timeout_s
        while self() is None:
            if stop_event is not None and stop_event.is_set():
                return
            if self._process.poll() is not None:
                raise RuntimeError(f"no Gazebo world statistics received on {self.topic}")
            if time.monotonic() > deadline:
                elapsed = time.monotonic() - started
                if keep_waiting is None or not keep_waiting(elapsed):
                    raise RuntimeError(f"no Gazebo world statistics received on {self.topic}")
                deadline = time.monotonic() + timeout_s
            time.sleep(0.05)

    def __call__(self) -> float | None:
        with self._lock:
            sample = self._sample
        if sample is None:
            return None
        sim_seconds, wall_seconds, rtf = sample
        # Extrapolate at most one second past the last message so a paused world stays paused.
        return sim_seconds + min(time.monotonic() - wall_seconds, 1.0) * rtf
