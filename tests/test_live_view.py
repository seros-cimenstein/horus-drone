from __future__ import annotations

import logging
from types import SimpleNamespace

from horus.mcp_server.live_view import LivePerceptionView
from horus.perception import Frame


class _Perception:
    def processing_stats(self):
        return {"frames": 1, "fps": None, "detections": 0, "ignored": 0}


class _Publisher:
    available = True

    def __init__(self):
        self.images = []

    def publish_bgr(self, image):
        self.images.append(image)


def test_render_failure_never_reaches_the_camera_loop_and_stops_rendering(caplog):
    caplog.set_level(logging.INFO)
    publisher = _Publisher()
    times = iter([0.0, 1.0, 20.0])
    view = LivePerceptionView(_Perception(), publisher, clock=lambda: next(times))
    broken = SimpleNamespace(frame=Frame(frame_id=1, timestamp=0.0, image=None), candidates=())

    view(broken)
    view(broken)
    view(broken)

    assert publisher.images == []
    records = [record for record in caplog.records if record.name == "horus.mcp_server.live_view"]
    # One warning when rendering is disabled; throughput logging continues on its interval.
    assert [record.levelno for record in records].count(logging.WARNING) == 1
    assert [record.levelno for record in records].count(logging.INFO) == 2
