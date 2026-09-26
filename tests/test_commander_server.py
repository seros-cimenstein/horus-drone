from __future__ import annotations

import asyncio
import json

import httpx

from horus.commander.agent import HorusCommander
from horus.commander.server import create_app
from horus.mcp_server.event_log import JsonlEventLog
from horus.config import CommanderSettings


_TOKEN = "commander-test-token"
_AUTH = [(b"authorization", f"Bearer {_TOKEN}".encode("ascii"))]


class _FakeCommander:
    async def submit_task(self, text: str) -> str:
        return f"handled: {text}"


class _FakeClaudeStream:
    def __init__(self, events):
        self._lines = iter(json.dumps(event).encode("utf-8") + b"\n" for event in events)

    async def readline(self):
        return next(self._lines, b"")


def test_claude_mcp_stream_events_reach_callback_without_credentials(tmp_path):
    callback_events = []
    event_log_path = tmp_path / "events.jsonl"
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        _TOKEN,
        CommanderSettings(),
        event_log=JsonlEventLog(event_log_path),
        event_callback=callback_events.append,
    )
    stream = _FakeClaudeStream(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Checking current state."},
                        {
                            "type": "tool_use",
                            "id": "use-1",
                            "name": "mcp__horus__get_state",
                            "input": {"api_key": "provider-secret", "access_token": _TOKEN},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "use-1",
                            "content": [{"type": "text", "text": "state is ready"}],
                        }
                    ]
                },
            },
            {"type": "result", "result": "State checked."},
        ]
    )

    result = asyncio.run(commander._consume_claude_stream(stream))
    logged_events = [
        json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["kind"] for event in callback_events]
    assert result == "State checked."
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "assistant_text" in kinds
    assert "final_result" in kinds
    assert [event["kind"] for event in logged_events] == kinds
    assert callback_events[-1]["text"] == "State checked."
    for serialized in (json.dumps(callback_events), event_log_path.read_text(encoding="utf-8")):
        assert "provider-secret" not in serialized
        assert _TOKEN not in serialized


class _SSECapture:
    def __init__(self, app, *, frame_count: int, headers=None, query: bytes = b""):
        self.app = app
        self.frame_count = frame_count
        self.headers = list(headers if headers is not None else _AUTH)
        self.query = query
        self.messages = []
        self.started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self._received_request = False
        self._task = None

    async def receive(self):
        if not self._received_request:
            self._received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.start":
            self.started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            seen = sum(line.startswith(b"id: ") for line in body.splitlines())
            prior = sum(
                line.startswith(b"id: ")
                for item in self.messages[:-1]
                if item["type"] == "http.response.body"
                for line in item.get("body", b"").splitlines()
            )
            if prior + seen >= self.frame_count:
                self.disconnected.set()

    async def start(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "query_string": self.query,
            "root_path": "",
            "headers": self.headers,
        }
        self._task = asyncio.create_task(self.app(scope, self.receive, self.send))
        await asyncio.wait_for(self.started.wait(), timeout=1)

    async def finish(self):
        await asyncio.wait_for(self._task, timeout=1)
        return self.messages


def _event_frames(messages):
    frames = []
    event_id = None
    data = None
    for message in messages:
        if message["type"] != "http.response.body":
            continue
        for line in message.get("body", b"").decode("utf-8").splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif not line and event_id is not None and data is not None:
                frames.append((event_id, data))
                event_id = None
                data = None
    return frames


def test_commander_stream_records_are_delivered_over_authenticated_sse(tmp_path):
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        _TOKEN,
        CommanderSettings(),
        event_log=JsonlEventLog(tmp_path / "events.jsonl"),
    )
    app = create_app(commander, _TOKEN)

    async def exercise_sse():
        live = _SSECapture(app, frame_count=1)
        await live.start()
        await commander._consume_claude_stream(
            _FakeClaudeStream([{"type": "result", "result": "live report"}])
        )
        return await live.finish()

    messages = asyncio.run(exercise_sse())
    frames = _event_frames(messages)
    assert [(event_id, event["kind"], event["text"]) for event_id, event in frames] == [
        (1, "final_result", "live report")
    ]


def test_commander_service_denies_requests_without_valid_bearer_credentials():
    app = create_app(_FakeCommander(), _TOKEN)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.post("/task", json={"text": "inspect the area"})
            wrong = await client.post(
                "/task",
                headers={"Authorization": "Bearer wrong-token"},
                json={"text": "inspect the area"},
            )
        return missing, wrong

    missing, wrong = asyncio.run(request())

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json() == {"error": "unauthorized"}


def test_authenticated_task_submission_returns_accepted_and_sse_reconnects():
    app = create_app(_FakeCommander(), _TOKEN)

    async def exercise_http_contract():
        live = _SSECapture(app, frame_count=2)
        await live.start()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = await client.post(
                "/task", headers={"Authorization": f"Bearer {_TOKEN}"}, json={"text": "  "}
            )
            accepted = await client.post(
                "/task",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"text": "inspect the red vehicle"},
            )
        events = await live.finish()
        return invalid, accepted, events

    invalid, accepted, messages = asyncio.run(exercise_http_contract())

    assert invalid.status_code == 400
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    task_id = accepted.json()["task_id"]
    frames = _event_frames(messages)
    assert [event_id for event_id, _ in frames] == [1, 2]
    assert [event["kind"] for _, event in frames] == ["task_accepted", "task_completed"]
    assert all(event["task_id"] == task_id for _, event in frames)
    assert frames[1][1]["result"] == "handled: inspect the red vehicle"

    async def reconnect_and_replay():
        reconnect = _SSECapture(
            app,
            frame_count=1,
            headers=[*_AUTH, (b"last-event-id", b"1")],
        )
        await reconnect.start()
        resumed_messages = await reconnect.finish()
        independent = _SSECapture(app, frame_count=2, query=b"after=0")
        await independent.start()
        independent_messages = await independent.finish()
        return resumed_messages, independent_messages

    resumed, independent = asyncio.run(reconnect_and_replay())
    resumed_frames = _event_frames(resumed)
    independent_frames = _event_frames(independent)
    assert [(event_id, event["kind"]) for event_id, event in resumed_frames] == [
        (2, "task_completed")
    ]
    assert [(event_id, event["kind"]) for event_id, event in independent_frames] == [
        (1, "task_accepted"),
        (2, "task_completed"),
    ]


class _FailingCommander:
    async def submit_task(self, text: str) -> str:
        raise RuntimeError("MCP session failed")


def test_commander_publishes_task_errors_after_acceptance():
    app = create_app(_FailingCommander(), _TOKEN)

    async def exercise_http_contract():
        live = _SSECapture(app, frame_count=2)
        await live.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/task",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"text": "check MCP state"},
            )
        messages = await live.finish()
        return response, messages

    response, messages = asyncio.run(exercise_http_contract())
    frames = _event_frames(messages)
    assert response.status_code == 202
    assert [event["kind"] for _, event in frames] == ["task_accepted", "task_error"]
    task_ids = [event["task_id"] for _, event in frames]
    assert task_ids == [response.json()["task_id"]] * 2
    assert frames[1][1]["error"] == "MCP session failed"
