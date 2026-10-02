from __future__ import annotations

import base64
import json
import socket
import threading
import time
from urllib.request import Request, urlopen

import cv2
import numpy as np
import pytest

from horus.grounder.client import GrounderClient, GrounderUnavailable
from horus.grounder.server import make_server
from horus.perception.types import GroundingBox


class FakeBackend:
    model = "fake-grounder"

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    def detect(
        self,
        image: np.ndarray,
        prompts: list[str],
        *,
        box_threshold: float,
        text_threshold: float,
    ) -> list[GroundingBox]:
        if self.delay_s:
            time.sleep(self.delay_s)
        height, width = image.shape[:2]
        if "person" not in prompts:
            return []
        return [
            GroundingBox(
                bbox=(width * 0.1, height * 0.2, width * 0.6, height * 0.9),
                confidence=max(box_threshold, 0.8),
                label="person",
            )
        ]


@pytest.fixture
def grounder_server():
    server = make_server("127.0.0.1", 0, FakeBackend())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_and_detect_http_contract(grounder_server) -> None:
    _, url = grounder_server
    with urlopen(f"{url}/health", timeout=2) as response:
        health = json.loads(response.read())
    assert health == {"ok": True, "model": "fake-grounder"}

    image = np.zeros((40, 80, 3), dtype=np.uint8)
    ok, jpeg = cv2.imencode(".jpg", image)
    assert ok
    payload = {
        "image_jpeg_b64": base64.b64encode(jpeg.tobytes()).decode("ascii"),
        "prompts": ["person", "cabinet"],
        "box_threshold": 0.4,
        "text_threshold": 0.2,
    }
    request = Request(
        f"{url}/detect",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        result = json.loads(response.read())
    assert result["model"] == "fake-grounder"
    assert result["latency_ms"] >= 0
    assert result["boxes"] == [
        {"bbox": [8.0, 8.0, 48.0, 36.0], "score": 0.8, "label": "person"}
    ]


def test_client_round_trip_returns_grounding_boxes(grounder_server) -> None:
    _, url = grounder_server
    client = GrounderClient(url, timeout_s=2)

    boxes = client.detect(np.zeros((40, 80, 3), dtype=np.uint8), ["person"])

    assert boxes == [GroundingBox((8.0, 8.0, 48.0, 36.0), 0.8, "person")]
    assert client.health() == {"ok": True, "model": "fake-grounder"}


def test_client_raises_for_refused_connection() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    client = GrounderClient(f"http://127.0.0.1:{port}", timeout_s=0.2)

    with pytest.raises(GrounderUnavailable):
        client.health()


def test_client_raises_when_request_times_out() -> None:
    server = make_server("127.0.0.1", 0, FakeBackend(delay_s=0.3))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = GrounderClient(f"http://127.0.0.1:{server.server_port}", timeout_s=0.05)
    try:
        with pytest.raises(GrounderUnavailable):
            client.detect(np.zeros((40, 80, 3), dtype=np.uint8), ["person"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
