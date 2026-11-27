from __future__ import annotations

import base64
import json
import socket
import threading
import time
from urllib.request import Request, urlopen

import httpx
import numpy as np
import pytest

from horus.grounder.backends import CloudVLMGrounder, create_backend
from horus.grounder.client import GrounderClient, GrounderUnavailable
from horus.grounder.server import make_server
from horus.perception.types import GroundingBox

cv2 = pytest.importorskip("cv2")


class FakeBackend:
    model_id = "fake-grounder"

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


def test_cloud_vlm_grounder_parses_clamps_and_filters_boxes() -> None:
    response_body = {
        "choices": [
            {
                "message": {
                    "content": (
                        '```json\n{"detections": ['
                        '{"label": "red backpack", "score": 0.91, "box_2d": [200, 100, 800, 600]},'
                        '{"label": "noise", "score": 0.15, "box_2d": [0, 0, 100, 100]},'
                        '{"label": "clamped", "score": 0.75, "box_2d": [-50, 500, 1200, 1100]}'
                        "]}\n```"
                    )
                }
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json=response_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        backend = CloudVLMGrounder(
            api_key="test-key",
            base_url="https://vlm.example.test/v1/",
            model="gemini-2.5-flash",
            client=client,
        )
        backend.load()
        boxes = backend.detect(
            np.zeros((100, 200, 3), dtype=np.uint8),
            ["red backpack", "clamped"],
            box_threshold=0.35,
            text_threshold=0.25,
        )

    assert len(boxes) == 2
    assert boxes[0] == GroundingBox((20.0, 20.0, 120.0, 80.0), 0.91, "red backpack")
    assert boxes[1] == GroundingBox((100.0, 0.0, 200.0, 100.0), 0.75, "clamped")


def test_cloud_vlm_grounder_requires_api_key(monkeypatch) -> None:
    for var in (
        "HORUS_GROUNDER_API_KEY",
        "HORUS_GROUNDER_BASE_URL",
        "HORUS_GROUNDER_MODEL",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HORUS_CONFIG_DIR", "/tmp/horus-nonexistent-cfg-test")
    backend = create_backend("cloud-vlm")
    assert isinstance(backend, CloudVLMGrounder)
    with pytest.raises(RuntimeError, match="requires GEMINI_API_KEY"):
        backend.load()


def test_create_backend_supports_aliases() -> None:
    assert create_backend("grounding-dino").model == "grounding-dino-tiny"
    assert create_backend("owlv2").model == "owlv2-base-patch16-ensemble"
