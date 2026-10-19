from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from horus.config import HorusConfig
from horus.mcp_server.client import main
from horus.mcp_server.server import create_application, create_mcp
from horus.perception.types import Candidate, Frame, SetOfMarkPayload

from tests.fakes import FakeFlight

ROOT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "horus.yaml"
_JPEG_FIXTURE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDyOiiiuw5D/9k="
)


class _Executor:
    def note_supervisor_contact(self):
        pass

    def get_state(self):
        return {
            "state": "idle",
            "vehicle": {
                "pose": {"north_m": 1.0, "east_m": 2.0, "altitude_m": 3.0},
                "observed_at": "test-observation",
                "observation_age_s": 0.0,
            },
        }


class _Perception:
    def __init__(self, frame: Frame):
        self.frame = frame

    def camera_status(self):
        return {"camera": "streaming", "last_frame_age_s": 0.0, "reason": None}

    def processing_stats(self):
        return {}

    def get_frame(self, frame_id=None):
        if frame_id is not None and frame_id != self.frame.frame_id:
            return None
        return self.frame

    def latest_observation(self):
        return self.frame

    def list_candidates(self):
        return [candidate.to_dict() for candidate in self.frame.candidates]


def _make_mcp(monkeypatch):
    candidate = Candidate(
        id=4,
        bbox=(20.0, 30.0, 120.0, 180.0),
        class_name="person",
        confidence=0.95,
        frame_id=42,
        timestamp=time.monotonic(),
    )
    frame = Frame(
        frame_id=42,
        timestamp=time.monotonic(),
        image=None,
        pose={"north_m": 1.0, "east_m": 2.0, "altitude_m": 3.0},
        candidates=(candidate,),
    )

    def render(frame, *, max_dimension, jpeg_quality=82):
        assert max_dimension <= 640
        return SetOfMarkPayload(
            frame_id=frame.frame_id,
            timestamp=frame.timestamp,
            pose=frame.pose,
            image_jpeg=_JPEG_FIXTURE,
            candidates=(
                {
                    "mark": candidate.id,
                    "id": candidate.id,
                    "bbox": [20, 30, 120, 180],
                    "class_name": candidate.class_name,
                    "confidence": candidate.confidence,
                },
            ),
            width=320,
            height=240,
        )

    monkeypatch.setattr("horus.perception.render.render_set_of_marks", render)
    return create_mcp(HorusConfig(), _Executor(), FakeFlight(), _Perception(frame))


def test_horus_mcp_call_uses_streamable_http_and_saves_image(
    monkeypatch, tmp_path, capsys, serve
):
    mcp = _make_mcp(monkeypatch)
    from mcp.server.fastmcp.utilities.types import Image

    @mcp.tool(structured_output=False)
    def echo_bearer_for_privacy_check() -> str:
        return "Bearer test-client-token"

    @mcp.tool(structured_output=False)
    def two_images():
        return [Image(data=_JPEG_FIXTURE, format="jpeg"), Image(data=_JPEG_FIXTURE, format="jpeg")]

    server = serve(create_application(mcp, token="test-client-token"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORUS_MCP_URL", server.url)
    monkeypatch.setenv("HORUS_MCP_TOKEN", "test-client-token")
    monkeypatch.setenv("HORUS_CONFIG", str(ROOT_CONFIG))
    main(["list-tools"])
    tools_output = capsys.readouterr().out
    assert "get_state" in tools_output
    assert "get_frame" in tools_output

    main(["get-prompt", "horus_commander"])
    prompt_output = capsys.readouterr().out
    assert "event-driven" in prompt_output
    assert "current tracker IDs" in prompt_output

    main(["get_state"])
    state_output = capsys.readouterr().out
    assert '"state":"idle"' in state_output
    assert "test-client-token" not in state_output
    main(["echo_bearer_for_privacy_check"])
    private_output = capsys.readouterr().out
    assert "test-client-token" not in private_output
    assert "Bearer [REDACTED]" in private_output

    main(["get_frame"])
    frame_output = capsys.readouterr().out
    text_result = json.loads(frame_output.splitlines()[0])
    assert text_result["width"] <= 640
    assert text_result["height"] <= 640
    assert text_result["timestamp"]
    assert text_result["observation_age_s"] >= 0
    assert text_result["candidate_table"][0]["id"] == 4
    assert '"candidate_table"' in frame_output
    assert "test-client-token" not in frame_output
    saved_path = Path(frame_output.strip().splitlines()[-1])
    assert saved_path.is_file()
    saved_path = saved_path.resolve()
    assert saved_path.parent == tmp_path / "runs" / "manager"
    jpeg = saved_path.read_bytes()
    assert jpeg.startswith(b"\xff\xd8\xff")
    assert "image_base64" not in frame_output
    main(["two_images"])
    image_paths = [
        Path(path).resolve()
        for path in capsys.readouterr().out.strip().splitlines()
    ]
    assert len(image_paths) == 2
    assert image_paths[0].name.endswith("_1.jpg")
    assert image_paths[1].name.endswith("_2.jpg")
    assert all(path.is_file() for path in image_paths)


@pytest.mark.parametrize(
    ("missing_setting", "message"),
    [
        ("HORUS_MCP_TOKEN", "HORUS_MCP_TOKEN is required"),
        ("HORUS_MCP_URL", "HORUS_MCP_URL is required"),
    ],
)
def test_horus_mcp_call_reports_missing_required_environment(
    monkeypatch, capsys, missing_setting, message
):
    monkeypatch.setenv("HORUS_MCP_URL", "http://127.0.0.1:8000/mcp")
    monkeypatch.setenv("HORUS_MCP_TOKEN", "test-client-token")
    monkeypatch.setenv("HORUS_CONFIG", str(ROOT_CONFIG))
    monkeypatch.delenv(missing_setting)

    with pytest.raises(SystemExit) as error:
        main(["list-tools"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert message in stderr
    assert "test-client-token" not in stderr


def test_horus_mcp_call_reads_the_token_from_the_configured_auth_token_env(
    monkeypatch, capsys, tmp_path
):
    config = tmp_path / "horus.yaml"
    config.write_text("server:\n  auth_token_env: CUSTOM_MCP_TOKEN\n", encoding="utf-8")
    monkeypatch.setenv("HORUS_MCP_URL", "http://127.0.0.1:8000/mcp")
    monkeypatch.setenv("HORUS_MCP_TOKEN", "default-token")
    monkeypatch.delenv("CUSTOM_MCP_TOKEN", raising=False)

    with pytest.raises(SystemExit) as error:
        main(["--config", str(config), "list-tools"])

    assert error.value.code == 2
    assert "CUSTOM_MCP_TOKEN is required" in capsys.readouterr().err


def test_open_session_sends_the_bearer_token_and_initializes(monkeypatch):
    import asyncio
    import contextlib

    import mcp
    import mcp.client.streamable_http as streamable_http

    from horus.mcp_client import open_session

    opened = []

    @contextlib.asynccontextmanager
    async def fake_transport(url, headers):
        opened.append((url, headers))
        yield "read", "write", None

    class FakeSession:
        initialized = False

        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            self.initialized = True

    monkeypatch.setattr(streamable_http, "streamablehttp_client", fake_transport)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    async def scenario():
        async with open_session("http://mcp:8000/mcp", "secret") as session:
            return session.initialized

    assert asyncio.run(scenario()) is True
    assert opened == [("http://mcp:8000/mcp", {"Authorization": "Bearer secret"})]
