from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

from horus.events import EventBus


def test_event_bus_orders_events_and_returns_only_retained_ids():
    bus = EventBus(capacity=2)
    assert [bus.publish("sample", str(value), value=value) for value in range(3)] == [1, 2, 3]

    events = asyncio.run(bus.wait(after_id=0, timeout_s=0.0))

    assert [event["id"] for event in events] == [2, 3]
    assert [event["summary"] for event in events] == ["1", "2"]
    assert all(set(event) == {"id", "time", "kind", "summary", "data"} for event in events)
    assert events[0]["data"] == {"value": 1}
    assert datetime.fromisoformat(events[0]["time"]).tzinfo is not None


def test_event_bus_wait_times_out_without_events():
    bus = EventBus()
    started = time.monotonic()

    events = asyncio.run(bus.wait(after_id=0, timeout_s=0.03))

    assert events == []
    assert time.monotonic() - started >= 0.02


def test_event_bus_wait_receives_publish_from_another_thread_without_blocking_loop():
    bus = EventBus()
    published = threading.Event()

    def publish_later():
        time.sleep(0.02)
        bus.publish("executor", "finished", task_id="task-1")
        published.set()

    async def wait_with_loop_activity():
        publisher = threading.Thread(target=publish_later)
        publisher.start()
        ticks = 0
        waiter = asyncio.create_task(bus.wait(after_id=0, timeout_s=0.5))
        while not waiter.done():
            ticks += 1
            await asyncio.sleep(0.001)
        events = await waiter
        publisher.join(timeout=1.0)
        return ticks, events

    ticks, events = asyncio.run(wait_with_loop_activity())

    assert published.is_set()
    assert ticks > 1
    assert events[0]["kind"] == "executor"
    assert events[0]["data"] == {"task_id": "task-1"}
