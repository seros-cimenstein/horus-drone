"""Thread-safe event stream shared by the executor, perception, and MCP tools."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

# Final skill-task statuses (get_task_status, task_finished events); a task never leaves them.
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})

# EventBus kinds by publisher. The commander mirrors every EVENT_KINDS event to its log and the
# GUI; other kinds (vocabulary_changed) stay on the MCP wait_events stream only.
EXECUTOR_EVENT_KINDS = frozenset({
    "task_finished", "target_lost", "target_reacquired", "target_found", "link_lost",
    "link_restored", "auto_rth", "emergency", "landed", "precision_landing_stalled",
    "pilot_override", "pilot_released",
})
PLAN_EVENT_KINDS = frozenset({"plan_finished", "plan_log"})
# watch_region occupancy transitions, then watch lifecycle changes.
REGION_TRANSITION_KINDS = frozenset({"region_enter", "region_exit", "region_dwell"})
REGION_EVENT_KINDS = REGION_TRANSITION_KINDS | {
    "region_watch_paused", "region_watch_resumed", "region_watch_expired",
}
SCENE_EVENT_KINDS = REGION_EVENT_KINDS | {"object_detected"}
EVENT_KINDS = EXECUTOR_EVENT_KINDS | PLAN_EVENT_KINDS | SCENE_EVENT_KINDS
# Kinds that may wake the commander model (subject to its repeat limits). plan_log lines are
# GUI-only: the plan reports to the model once through plan_finished.
MODEL_WAKE_EVENT_KINDS = EVENT_KINDS - {"plan_log"}


class EventBus:
    """Store recent events and asynchronously wait for events from any publishing thread."""

    def __init__(self, capacity: int = 500):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("event bus capacity must be a positive integer")
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._lock = threading.Lock()

    def publish(self, kind: str, summary: str, **data: Any) -> int:
        """Append an event and return its monotonically increasing ID."""
        with self._lock:
            event_id = self._next_id
            self._next_id += 1
            self._events.append(
                {
                    "id": event_id,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "kind": str(kind),
                    "summary": str(summary),
                    "data": copy.deepcopy(data),
                }
            )
            return event_id

    async def wait(self, after_id: int, timeout_s: float) -> list[dict[str, Any]]:
        """Wait without blocking the asyncio loop; return buffered events newer than after_id."""
        cursor = int(after_id)
        timeout = max(0.0, float(timeout_s))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            with self._lock:
                events = [copy.deepcopy(event) for event in self._events if event["id"] > cursor]
            if events:
                return events
            remaining = deadline - loop.time()
            if remaining <= 0.0:
                return []
            await asyncio.sleep(min(0.05, remaining))