from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from horus.commander.agent import HorusCommander
from horus.commander.server import create_app, emergency_action
from horus.mcp_server.event_log import JsonlEventLog
from horus.config import CommanderSettings


_TOKEN = "commander-test-token"
_AUTH = [(b"authorization", f"Bearer {_TOKEN}".encode("ascii"))]


class _FakeCommander:
    async def submit_task(self, text: str) -> str:
        return f"handled: {text}"


def test_commander_event_records_reach_callback_and_redact_credentials(tmp_path):
    callback_events = []
    event_log_path = tmp_path / "events.jsonl"
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        _TOKEN,
        CommanderSettings(),
        event_log=JsonlEventLog(event_log_path),
        event_callback=callback_events.append,
    )

    commander._append_event({
        "kind": "tool_use",
        "tool": "mcp__horus__get_state",
        "args": {"api_key": "provider-secret", "access_token": _TOKEN},
    })
    commander._append_event({"kind": "tool_result", "summary": "state is ready"})
    commander._append_event({"kind": "assistant_text", "text": "Checking current state."})
    commander._append_event({"kind": "final_result", "text": "State checked."})
    serialized_log = event_log_path.read_text(encoding="utf-8")
    logged_events = [json.loads(line) for line in serialized_log.splitlines()]
    kinds = [event["kind"] for event in callback_events]

    assert kinds == ["tool_use", "tool_result", "assistant_text", "final_result"]
    assert [event["kind"] for event in logged_events] == kinds
    assert callback_events[-1]["text"] == "State checked."
    for serialized in (json.dumps(callback_events), serialized_log):
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


def test_commander_events_are_delivered_over_authenticated_sse(tmp_path):
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
        commander._append_event({"kind": "final_result", "text": "live report"})
        return await live.finish()

    frames = _event_frames(asyncio.run(exercise_sse()))
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


def test_emergency_grammar_accepts_only_short_imperatives():
    for command, expected in (
        ("stop", "hold"),
        ("STOP!", "hold"),
        ("halt now.", "hold"),
        ("abort immediately", "hold"),
        ("hold", "hold"),
        ("freeze!", "hold"),
        ("land", "land"),
        ("land now", "land"),
        ("land immediately.", "land"),
        ("rth", "rth"),
        ("return home now!", "rth"),
    ):
        assert emergency_action(command) == expected
    for command in (
        "please stop the drone",
        "land near the red chair",
        "I think we should return home",
        "stop after reaching the doorway",
        "return home when safe",
        "land now because the battery is low",
    ):
        assert emergency_action(command) is None


def test_emergency_endpoint_executes_direct_mcp_action_and_interrupts_backend():
    class Session:
        def __init__(self):
            self.calls = []
            self.called = asyncio.Event()

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            self.called.set()
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text="land command accepted")],
            )

    class Backend:
        def __init__(self):
            self.interrupted = asyncio.Event()
            self.notice = asyncio.Event()
            self.messages = []

        async def interrupt(self):
            self.interrupted.set()

        async def send(self, text):
            self.messages.append(text)
            self.notice.set()
            return "acknowledged"

        async def reset(self):
            pass

    async def scenario():
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp", "mcp-token", CommanderSettings()
        )
        session = Session()
        backend = Backend()
        commander._mcp_session = session
        commander.backend = backend
        commander._mission_active = True
        app = create_app(commander, _TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/emergency",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"action": "land"},
            )
        await asyncio.wait_for(session.called.wait(), timeout=1)
        await asyncio.wait_for(backend.interrupted.wait(), timeout=1)
        await asyncio.wait_for(backend.notice.wait(), timeout=1)
        return response, session, backend, app.event_hub.events_after(0)

    response, session, backend, events = asyncio.run(scenario())
    assert response.status_code == 202
    assert response.json()["action"] == "land"
    assert session.calls == [("land", {})]
    assert backend.messages == ["[operator emergency] land executed: land command accepted"]
    assert [event["status"] for event in events if event["kind"] == "emergency"] == [
        "requested",
        "executed",
    ]


def test_task_grammar_bypasses_model_only_for_exact_emergency_text():
    class Commander(_FakeCommander):
        def __init__(self):
            self.submitted = []
            self.emergencies = []
            self.submitted_event = asyncio.Event()
            self.emergency_event = asyncio.Event()

        async def submit_task(self, text):
            self.submitted.append(text)
            self.submitted_event.set()
            return "sent to model"

        async def emergency(self, action):
            self.emergencies.append(action)
            self.emergency_event.set()
            return "executed"
    async def scenario():
        commander = Commander()
        app = create_app(commander, _TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            emergency = await client.post(
                "/task",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"text": "STOP!"},
            )
            normal = await client.post(
                "/task",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"text": "Please land near the marked pad after checking it."},
            )
            await asyncio.wait_for(
                asyncio.gather(commander.submitted_event.wait(), commander.emergency_event.wait()),
                timeout=1,
            )
        return commander, emergency, normal

    commander, emergency, normal = asyncio.run(scenario())
    assert emergency.status_code == normal.status_code == 202
    assert commander.emergencies == ["hold"]
    assert commander.submitted == ["Please land near the marked pad after checking it."]


def test_event_pump_coalesces_relevant_events_and_skips_without_active_mission():
    class Backend:
        def __init__(self):
            self.messages = []
            self.received = asyncio.Event()

        async def send(self, text):
            self.messages.append(text)
            self.received.set()
            return "event handled"

        async def interrupt(self):
            pass

        async def reset(self):
            pass

    class EventSession:
        def __init__(self):
            self.first = True
            self.received = asyncio.Event()

        async def call_tool(self, name, arguments):
            assert name == "wait_events"
            if self.first:
                self.first = False
                self.received.set()
                payload = {
                    "events": [
                        {"id": 1, "time": "t1", "kind": "task_finished", "summary": "done", "data": {}},
                        {"id": 2, "time": "t2", "kind": "target_lost", "summary": "track 4", "data": {}},
                    ],
                    "last_id": 2,
                }
                return SimpleNamespace(
                    isError=False,
                    content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                )
            await asyncio.Event().wait()

    async def run_case(active: bool):
        mirrored = []
        mirrored_signal = asyncio.Event()
        backend = Backend()
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp",
            "token",
            CommanderSettings(),
            event_callback=lambda event: (mirrored.append(event), mirrored_signal.set()),
        )
        commander.backend = backend
        commander._mission_active = active
        session = EventSession()
        pump = asyncio.create_task(commander._pump_events(session))
        await asyncio.wait_for(mirrored_signal.wait(), timeout=1)
        await asyncio.wait_for(session.received.wait(), timeout=1)
        if active:
            await asyncio.wait_for(backend.received.wait(), timeout=1)
        else:
            await asyncio.sleep(0)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return backend.messages, mirrored

    active_messages, active_events = asyncio.run(run_case(True))
    assert active_messages == [
        "[HORUS event #1 t1] task_finished: done\n"
        "[HORUS event #2 t2] target_lost: track 4"
    ]
    assert [event["kind"] for event in active_events] == ["task_finished", "target_lost"]

    inactive_messages, inactive_events = asyncio.run(run_case(False))
    assert inactive_messages == []
    assert [event["kind"] for event in inactive_events] == ["task_finished", "target_lost"]


def test_reset_endpoint_and_operator_reset_discard_the_active_mission():
    class Commander(_FakeCommander):
        def __init__(self):
            self.reset_count = 0
            self.reset_events = asyncio.Queue()

        async def reset(self):
            self.reset_count += 1
            await self.reset_events.put(None)

    async def scenario():
        commander = Commander()
        app = create_app(commander, _TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/reset", headers={"Authorization": f"Bearer {_TOKEN}"})
            await asyncio.wait_for(commander.reset_events.get(), timeout=1)
            task_reset = await client.post(
                "/task",
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json={"text": "new mission"},
            )
            await asyncio.wait_for(commander.reset_events.get(), timeout=1)
        return response, task_reset, commander

    response, task_reset, commander = asyncio.run(scenario())
    assert response.status_code == 200
    assert task_reset.status_code == 202
    assert commander.reset_count == 2


def test_event_pump_wakes_once_for_a_flapping_link_and_reports_the_suppressed_repeats():
    class Backend:
        def __init__(self):
            self.messages = []
            self.received = asyncio.Event()

        async def send(self, text):
            self.messages.append(text)
            self.received.set()
            return "ok"

    flapping = []
    for index in range(10):
        kind = "link_lost" if index % 2 == 0 else "link_restored"
        flapping.append({"id": index + 1, "time": "t", "kind": kind, "summary": kind, "data": {}})
    flapping += [
        {"id": 11, "time": "t", "kind": "object_detected", "summary": "cabinet", "data": {"label": "cabinet"}},
        {"id": 12, "time": "t", "kind": "object_detected", "summary": "cabinet", "data": {"label": "Cabinet"}},
    ]

    class EventSession:
        def __init__(self):
            self.first = True

        async def call_tool(self, name, arguments):
            if self.first:
                self.first = False
                payload = {"events": flapping, "last_id": 12}
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=json.dumps(payload))])
            await asyncio.Event().wait()

    async def scenario():
        mirrored = []
        backend = Backend()
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp", "token", CommanderSettings(), event_callback=mirrored.append
        )
        commander.backend = backend
        commander._mission_active = True
        commander._session_ready.set()
        pump = asyncio.create_task(commander._pump_events(EventSession()))
        await asyncio.wait_for(backend.received.wait(), timeout=2)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return backend.messages, mirrored

    messages, mirrored = asyncio.run(scenario())

    assert len(mirrored) == 12  # the GUI log still shows every event
    lines = messages[0].splitlines()
    assert [line.split("] ")[1].split(":")[0] for line in lines[:-1]] == [
        "link_lost", "link_restored", "object_detected"
    ]
    assert lines[-1] == (
        "[HORUS events suppressed as repeats since the last message: "
        "link_lost x4, link_restored x4, object_detected x1]"
    )


def test_region_events_wake_the_mission_once_per_watch_and_kind_and_count_repeats():
    class Backend:
        def __init__(self):
            self.messages = []
            self.received = asyncio.Event()

        async def send(self, text):
            self.messages.append(text)
            self.received.set()
            return "ok"

    def region(event_id, kind, watch_id, target_id):
        return {
            "id": event_id,
            "time": "t",
            "kind": kind,
            "summary": f'Target {target_id} "person" {kind} ({watch_id})',
            "data": {"watch_id": watch_id, "target_id": target_id},
        }

    batch = [
        region(1, "region_enter", "watch-1", 4),
        region(2, "region_enter", "watch-1", 5),
        region(3, "region_enter", "watch-2", 6),
        region(4, "region_dwell", "watch-1", 4),
        region(5, "region_enter", "watch-1", 7),
        region(6, "region_watch_expired", "watch-2", 0),
    ]

    class EventSession:
        def __init__(self):
            self.first = True

        async def call_tool(self, name, arguments):
            if self.first:
                self.first = False
                payload = {"events": batch, "last_id": 6}
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=json.dumps(payload))])
            await asyncio.Event().wait()

    async def scenario():
        mirrored = []
        backend = Backend()
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp", "token", CommanderSettings(), event_callback=mirrored.append
        )
        commander.backend = backend
        commander._mission_active = True
        pump = asyncio.create_task(commander._pump_events(EventSession()))
        await asyncio.wait_for(backend.received.wait(), timeout=2)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return backend.messages, mirrored

    messages, mirrored = asyncio.run(scenario())

    assert len(mirrored) == 6
    lines = messages[0].splitlines()
    assert [line.split("]")[0] for line in lines[:-1]] == [
        "[HORUS event #1 t", "[HORUS event #3 t", "[HORUS event #4 t", "[HORUS event #6 t"
    ]
    assert lines[-1] == "[HORUS events suppressed as repeats since the last message: region_enter[watch-1] x2]"
