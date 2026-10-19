"""Latency ring buffer of processed frames and their tracked candidates."""

from __future__ import annotations

from threading import RLock

from .types import Frame, StaleFrameError


class FrameBuffer:
    """Thread-safe time- and count-bounded ring of frames and their tracked candidates."""

    def __init__(self, *, max_seconds: float = 3.0, max_frames: int = 90) -> None:
        if max_seconds <= 0 or max_frames < 1:
            raise ValueError("max_seconds and max_frames must be positive")
        self.max_seconds = float(max_seconds)
        self.max_frames = int(max_frames)
        self._frames: list[Frame] = []
        self._lock = RLock()

    def append(self, frame: Frame) -> None:
        """Retain a processed frame; IDs and timestamps must advance monotonically."""
        with self._lock:
            if self._frames:
                latest = self._frames[-1]
                if frame.frame_id <= latest.frame_id:
                    raise ValueError("frame_id must increase monotonically")
                if frame.timestamp < latest.timestamp:
                    raise ValueError("frame timestamps must not go backwards")
            self._frames.append(frame)
            cutoff = frame.timestamp - self.max_seconds
            while self._frames and (
                len(self._frames) > self.max_frames or self._frames[0].timestamp < cutoff
            ):
                self._frames.pop(0)

    def get(self, frame_id: int) -> Frame:
        """Get one retained frame or raise ``StaleFrameError`` when it has expired."""
        with self._lock:
            for frame in self._frames:
                if frame.frame_id == frame_id:
                    return frame
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def snapshot(self) -> tuple[Frame, ...]:
        """Return a stable chronological view of retained frame objects."""
        with self._lock:
            return tuple(self._frames)

    def latest(self) -> Frame | None:
        """Return the newest retained frame, if any."""
        with self._lock:
            return self._frames[-1] if self._frames else None

    def frames_after(self, frame_id: int) -> tuple[Frame, ...]:
        """Return the requested frame and all later frames in chronological order."""
        frames = self.snapshot()
        for index, frame in enumerate(frames):
            if frame.frame_id == frame_id:
                return frames[index:]
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
