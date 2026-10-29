"""The drone's own map: what it learned itself, in every world (no third-party data needed).

- ``Marks``: named points the commander placed (``mark_place``), persisted as ``marks.json`` in the
  run directory, so a restarted MCP server in the same run keeps them.

Pure standard library; thread-safe where several threads touch it.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from horus.untrusted import MAX_TEXT_LENGTH

MAX_MARKS = 500
MAX_NOTE_LENGTH = 200


class Marks:
    def __init__(self, path: Path | None):
        """``path`` None keeps marks in memory only (no run directory)."""
        self.path = path
        self._lock = threading.Lock()
        self._marks: list[dict[str, Any]] = []
        if path is not None and path.is_file():
            self._marks = list(json.loads(path.read_text(encoding="utf-8")).get("marks", ()))

    def add(self, name: str, north_m: float, east_m: float, note: str = "") -> dict[str, Any]:
        name, note = str(name).strip(), str(note)
        if not name or len(name) > MAX_TEXT_LENGTH:
            raise ValueError(f"name must be 1-{MAX_TEXT_LENGTH} characters")
        if len(note) > MAX_NOTE_LENGTH:
            raise ValueError(f"note must be at most {MAX_NOTE_LENGTH} characters")
        if not all(math.isfinite(v) for v in (north_m, east_m)):
            raise ValueError("north_m and east_m must be finite")
        mark = {"name": name, "north_m": round(float(north_m), 2), "east_m": round(float(east_m), 2),
                "note": note, "marked_at": time.time()}
        with self._lock:
            if len(self._marks) >= MAX_MARKS:
                raise ValueError(f"at most {MAX_MARKS} marks per run")
            self._marks.append(mark)
            if self.path is not None:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"marks": self._marks}, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, self.path)
        return dict(mark)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(mark) for mark in self._marks]
