from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from horus.commander.agent import HorusCommander
from horus.commander.intents import emergency_action
from horus.commander.server import create_app
from horus.event_log import JsonlEventLog
from horus.config import CommanderSettings


_TOKEN = "commander-test-token"
_AUTH = [(b"authorization", f"Bearer {_TOKEN}".encode("ascii"))]


class _FakeCommander:
    event_callback = None

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
        await asyncio.wait_for(self.disconnected.wait(), timeout=5)
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


def test_page_version_changes_with_the_page_files_so_an_open_app_can_reload(tmp_path, monkeypatch):
    import horus.commander.server as server

    for name in ("index.html", "app.js", "app.css"):
        (tmp_path / name).write_text(f"{name} v1", encoding="utf-8")
    monkeypatch.setattr(server, "_WEB_DIR", tmp_path)
    app = create_app(_FakeCommander(), _TOKEN)

    async def versions():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/version")  # no token: it tells nothing the page does not
            same = await client.get("/version")
            (tmp_path / "app.js").write_text("app.js v2", encoding="utf-8")
            changed = await client.get("/version")
        return first, same, changed

    first, same, changed = asyncio.run(versions())

    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json() == same.json()
    assert changed.json()["version"] != first.json()["version"]


def test_the_control_app_page_loads_without_a_token_but_its_data_still_needs_one():
    app = create_app(_FakeCommander(), _TOKEN)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            pages = [await client.get(path) for path in (
                "/", "/app.js", "/app.css", "/manifest.webmanifest", "/sw.js", "/icon.svg",
                "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png",
            )]
            post_page = await client.post("/")
            guarded = [await client.get(path) for path in ("/events", "/index.html", "/web/app.js")]
            guarded.append(await client.post("/task", json={"text": "take off"}))
        return pages, post_page, guarded

    pages, post_page, guarded = asyncio.run(request())

    assert [page.status_code for page in pages] == [200] * 9
    assert [page.headers["content-type"].split(";")[0] for page in pages] == [
        "text/html", "text/javascript", "text/css", "application/manifest+json", "text/javascript",
        "image/svg+xml", "image/png", "image/png", "image/png",
    ]
    assert "<title>HORUS control app</title>" in pages[0].text
    # The installed phone app starts on the phone view, at the page the token-free files serve.
    manifest = pages[3].json()
    assert manifest["start_url"] == "/?view=phone" and manifest["display"] == "standalone"
    assert {icon["src"] for icon in manifest["icons"]} <= {"/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/icon.svg"}
    for page in pages:
        assert page.headers["content-security-policy"].startswith("default-src 'self'")
        assert page.headers["cache-control"] == "no-store"
        assert _TOKEN.encode() not in page.content
    assert (post_page.status_code, post_page.headers["allow"]) == (405, "GET")
    assert [response.status_code for response in guarded] == [401, 401, 401, 401]


def _block(**fields):
    return SimpleNamespace(**fields)


def _text_result(payload):
    return SimpleNamespace(isError=False, content=[_block(type="text", text=json.dumps(payload))])


class _DisplaySession:
    """Drone board stand-in: records every display call and answers like the real tools."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.fail == "transport":
            # anyio wraps transport errors in an exception group (the backport on Python 3.10).
            try:
                group = ExceptionGroup
            except NameError:
                from exceptiongroup import ExceptionGroup as group
            raise group("unhandled errors in a TaskGroup", [ConnectionError("drone board connection reset")])
        if self.fail == "tool":
            return SimpleNamespace(isError=True, content=[_block(type="text", text="perception unavailable: no camera frames")])
        if name == "get_state":
            return _text_result({"state": "IDLE"})
        if name == "list_candidates":
            return SimpleNamespace(isError=False, content=[
                _block(type="image", data="/9j/AAAA", mimeType="image/jpeg"),
                _block(type="text", text=json.dumps({"frame_id": 4, "candidate_table": [{"id": 7}]})),
            ])
        if name in {"get_map_index", "get_map_tile"}:
            if arguments.get("known_version") == "v1":
                return _text_result({"version": "v1", "unchanged": True})
            if name == "get_map_index":
                return _text_result({"version": "v1", "provider": "gta", "levels": [{"level": 0, "size_m": 250}],
                                     "tiles": {"0": [[x, 0, 300] for x in range(60)]}})
            if (arguments["level"], arguments["x"], arguments["y"]) != (0, 2, -1):
                return _text_result({"version": "v1", "missing": True})
            places = [{"id": f"p{i}", "name": json.dumps(f"Seat {i}"), "kind": json.dumps("seat"),
                       "north_m": -i, "east_m": 600} for i in range(60)]
            return _text_result({"version": "v1", "level": 0, "x": 2, "y": -1, "ways": [], "places": places})
        if name == "get_live_map":
            return _text_result({"vehicle_pose": {"north_m": 3.0, "east_m": -2.0, "up_m": 10.0},
                                 "heading_deg": 90.0, "marks": [], "candidates": []})
        raise AssertionError(f"display read called a non-display tool: {name}")


def _display_opener(session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def opener(url, token):
        yield session

    return opener


def _run_display(session, requests):
    from horus.commander.display import DisplayReader

    commander = HorusCommander("http://127.0.0.1:8000/mcp", "mcp-token", CommanderSettings())

    class _ForbiddenSession:
        async def call_tool(self, name, arguments):
            raise AssertionError("display reads must never use the AI pilot's session")

    commander._mcp_session = _ForbiddenSession()
    display = DisplayReader("http://127.0.0.1:8000/mcp", "mcp-token", opener=_display_opener(session))
    app = create_app(commander, _TOKEN, display=display)

    async def scenario():
        reader = asyncio.create_task(display.run())
        while display._session is None:
            await asyncio.sleep(0)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [await client.get(path, headers=headers) for path, headers in requests]
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        return responses

    return asyncio.run(scenario())


def test_display_reads_use_only_passive_tools_on_their_own_session():
    auth = {"Authorization": f"Bearer {_TOKEN}"}
    session = _DisplaySession()

    state, frame, unauthorized = _run_display(session, [
        ("/api/state", auth), ("/api/frame", auth), ("/api/state", {}),
    ])

    assert state.json() == {"state": "IDLE"}
    assert frame.json() == {
        "frame_id": 4, "candidate_table": [{"id": 7}], "image_base64": "/9j/AAAA", "mime_type": "image/jpeg",
    }
    assert unauthorized.status_code == 401
    assert [name for name, _ in session.calls] == ["get_state", "list_candidates"]
    assert all(arguments.get("passive") is True for _, arguments in session.calls)


def test_display_reads_answer_503_when_the_drone_board_fails_or_has_no_frames():
    auth = {"Authorization": f"Bearer {_TOKEN}"}

    (no_frames,) = _run_display(_DisplaySession(fail="tool"), [("/api/frame", auth)])
    broken, after = _run_display(_DisplaySession(fail="transport"), [("/api/state", auth), ("/api/state", auth)])

    assert (no_frames.status_code, no_frames.json()) == (503, {"error": "perception unavailable: no camera frames"})
    assert broken.status_code == 503
    assert broken.json()["error"] == "drone board unavailable: ConnectionError: drone board connection reset"
    assert after.status_code == 503
    unconfigured = asyncio.run(_get(create_app(_FakeCommander(), _TOKEN), "/api/state", auth))
    assert (unconfigured.status_code, unconfigured.json()) == (503, {"error": "display reads are not configured"})


def test_the_map_index_and_tiles_download_once_per_version_gzipped_and_then_answer_304():
    auth = {"Authorization": f"Bearer {_TOKEN}"}
    session = _DisplaySession()

    index, index_again, plain, tile, tile_again, other_tag, empty, live = _run_display(session, [
        ("/api/map/index", {**auth, "Accept-Encoding": "gzip"}),
        ("/api/map/index", {**auth, "If-None-Match": '"v1"'}),
        ("/api/map/index", {**auth, "Accept-Encoding": "identity"}),
        ("/api/map/tile?level=0&x=2&y=-1", {**auth, "Accept-Encoding": "gzip"}),
        ("/api/map/tile?level=0&x=2&y=-1", {**auth, "If-None-Match": '"v1/0/2/-1"'}),
        # Another tile's tag says nothing about this tile.
        ("/api/map/tile?level=0&x=2&y=-1", {**auth, "If-None-Match": '"v1/0/3/-1"'}),
        ("/api/map/tile?level=0&x=9&y=9", auth),
        ("/api/map/live", auth),
    ])

    assert (index.status_code, index.headers["etag"], index.headers["content-encoding"]) == (200, '"v1"', "gzip")
    assert len(index.json()["tiles"]["0"]) == 60 and "unchanged" not in index.json()
    assert (index_again.status_code, index_again.content, index_again.headers["etag"]) == (304, b"", '"v1"')
    assert "content-encoding" not in plain.headers and plain.json() == index.json()
    assert (tile.status_code, tile.headers["etag"], tile.headers["content-encoding"]) == (200, '"v1/0/2/-1"', "gzip")
    assert tile.headers["cache-control"] == "no-cache" and len(tile.json()["places"]) == 60
    assert (tile_again.status_code, tile_again.content, tile_again.headers["etag"]) == (304, b"", '"v1/0/2/-1"')
    assert other_tag.status_code == 200 and other_tag.json() == tile.json()
    assert empty.status_code == 404
    assert live.json()["heading_deg"] == 90.0
    # The index travels once; later requests only ask whether the stored version still holds.
    assert [(name, {k: v for k, v in args.items() if k != "passive"}) for name, args in session.calls] == [
        ("get_map_index", {}),
        ("get_map_index", {"known_version": "v1"}),
        ("get_map_index", {"known_version": "v1"}),
        ("get_map_tile", {"level": 0, "x": 2, "y": -1}),
        ("get_map_tile", {"level": 0, "x": 2, "y": -1, "known_version": "v1"}),
        ("get_map_tile", {"level": 0, "x": 2, "y": -1}),
        ("get_map_tile", {"level": 0, "x": 9, "y": 9}),
        ("get_live_map", {}),
    ]
    assert all(arguments.get("passive") is True for _, arguments in session.calls)


def test_a_malformed_tile_request_is_refused_without_asking_the_drone_board():
    auth = {"Authorization": f"Bearer {_TOKEN}"}
    session = _DisplaySession()
    queries = ["level=3&x=0&y=0", "level=0&x=a&y=0", "level=0&x=0", "level=0&x=1.5&y=0", "level=0&x=0&x=1&y=0"]

    responses = _run_display(session, [(f"/api/map/tile?{query}", auth) for query in queries])

    assert [response.status_code for response in responses] == [400] * len(queries)
    assert session.calls == []


def test_the_phone_token_is_accepted_next_to_the_laptop_token_and_never_echoed():
    phone = "phone-secret-token"
    app = create_app(_FakeCommander(), _TOKEN, phone_token=phone)
    app.event_hub.publish({"kind": "assistant_text", "text": f"keys {phone} and {_TOKEN}"})

    def status(application, secret):
        return asyncio.run(_get(application, "/api/state", {"Authorization": f"Bearer {secret}"})).status_code

    # 503: authorized, no display configured; 401: refused before any route runs.
    assert (status(app, _TOKEN), status(app, phone), status(app, "phone-secret-tokeX")) == (503, 503, 401)
    # Restarting the service without the phone token revokes the phone; a blank one adds nothing.
    assert status(create_app(_FakeCommander(), _TOKEN), phone) == 401
    assert status(create_app(_FakeCommander(), _TOKEN, phone_token="  "), "  ") == 401
    history = json.dumps(app.event_hub.events_after(0))
    assert phone not in history and _TOKEN not in history


async def _get(app, path, headers):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


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
    event_callback = None

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
        ("home", "rth"),
        ("go home!", "rth"),
    ):
        assert emergency_action(command) == expected
    for command in (
        "please stop the drone",
        "land near the red chair",
        "I think we should return home",
        "stop after reaching the doorway",
        "return home when safe",
        "land now because the battery is low",
        "go home after the scan",
        "home is to the north",
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
    assert session.calls == [("operator_emergency", {"action": "land"})]
    (notice,) = backend.messages
    assert notice.startswith("[operator emergency] land executed: land command accepted")
    assert "refused until the operator's next message" in notice
    assert [event["status"] for event in events if event["kind"] == "emergency"] == [
        "requested",
        "executed",
    ]
    # Replayed history keeps when each event happened (the page shows it).
    assert all(isinstance(event.get("time"), str) and event["time"] for event in events)


class _RecordingBackend:
    def __init__(self):
        self.messages = []
        self.received = asyncio.Event()

    async def send(self, text):
        self.messages.append(text)
        self.received.set()
        return "ok"

    async def interrupt(self):
        pass

    async def reset(self):
        pass


class _OneBatchSession:
    """Serves one ``wait_events`` batch, then blocks like an idle server."""

    def __init__(self, events):
        self.events = events
        self.drained = asyncio.Event()  # set once the pump asks again, i.e. processed the batch
        self._served = False

    async def call_tool(self, name, arguments):
        assert name == "wait_events"
        if not self._served:
            self._served = True
            payload = {"events": self.events, "last_id": self.events[-1]["id"]}
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        self.drained.set()
        await asyncio.Event().wait()


def _pump_one_batch(events, *, mission_active=True):
    """Run the commander's event pump over one batch; return (model messages, GUI mirror)."""
    async def scenario():
        mirrored = []
        backend = _RecordingBackend()
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp", "token", CommanderSettings(), event_callback=mirrored.append
        )
        commander.backend = backend
        commander._mission_active = mission_active
        session = _OneBatchSession(events)
        pump = asyncio.create_task(commander._pump_events(session))
        await asyncio.wait_for((backend.received if mission_active else session.drained).wait(), timeout=2)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return backend.messages, mirrored

    return asyncio.run(scenario())


def test_operator_emergency_wakes_the_model_once_despite_its_echoed_event():
    class Session:
        def __init__(self):
            self.held = asyncio.Event()
            self.delivered = asyncio.Event()

        async def call_tool(self, name, arguments):
            if name == "operator_emergency":
                self.held.set()
                return SimpleNamespace(
                    isError=False, content=[SimpleNamespace(type="text", text="held")]
                )
            assert name == "wait_events"
            await asyncio.wait_for(self.held.wait(), timeout=5)
            if not self.delivered.is_set():
                self.delivered.set()
                payload = {
                    "events": [{
                        "id": 1, "time": "t1", "kind": "emergency",
                        "summary": "Emergency hold requested",
                        "data": {"action": "hold", "source": "operator"},
                    }],
                    "last_id": 1,
                }
                return SimpleNamespace(
                    isError=False, content=[SimpleNamespace(type="text", text=json.dumps(payload))]
                )
            await asyncio.Event().wait()

    async def scenario():
        mirrored = []
        commander = HorusCommander(
            "http://127.0.0.1:8000/mcp", "token", CommanderSettings(),
            event_callback=mirrored.append,
        )
        backend = _RecordingBackend()
        session = Session()
        commander.backend = backend
        commander._mcp_session = session
        commander._mission_active = True
        commander._session_ready.set()
        pump = asyncio.create_task(commander._pump_events(session))
        await commander.emergency("hold")
        await asyncio.wait_for(session.delivered.wait(), timeout=1)
        await asyncio.sleep(1.3)  # past the pump's 1 s batching window
        pump.cancel()
        await asyncio.gather(pump, *commander._event_message_tasks, return_exceptions=True)
        return backend.messages, mirrored, commander._own_operator_emergencies

    messages, mirrored, unseen_echoes = asyncio.run(scenario())
    assert len(messages) == 1 and "hold" in messages[0]
    assert any(event.get("kind") == "emergency" and event.get("source") == "mcp-event" for event in mirrored)
    assert unseen_echoes == []  # the echo was consumed, so a later identical action still skips once


def test_another_control_apps_operator_emergency_still_wakes_the_model():
    messages, _ = _pump_one_batch([{
        "id": 1, "time": "t1", "kind": "emergency", "summary": "Emergency rth requested",
        "data": {"action": "rth", "source": "operator"},
    }])
    assert len(messages) == 1 and "Emergency rth requested" in messages[0]


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
    events = [
        {"id": 1, "time": "t1", "kind": "task_finished", "summary": "done", "data": {}},
        {"id": 2, "time": "t2", "kind": "pilot_override", "summary": "pilot took control", "data": {}},
    ]

    active_messages, active_events = _pump_one_batch(events)
    assert len(active_messages) == 1  # one coalesced wake-up carrying both events
    assert "done" in active_messages[0] and "pilot took control" in active_messages[0]
    assert [event["kind"] for event in active_events] == ["task_finished", "pilot_override"]

    inactive_messages, inactive_events = _pump_one_batch(events, mission_active=False)
    assert inactive_messages == []
    assert [event["kind"] for event in inactive_events] == ["task_finished", "pilot_override"]


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
    flapping = []
    for index in range(10):
        kind = "link_lost" if index % 2 == 0 else "link_restored"
        flapping.append({"id": index + 1, "time": "t", "kind": kind, "summary": kind, "data": {}})
    flapping += [
        {"id": 11, "time": "t", "kind": "object_detected", "summary": "cabinet", "data": {"label": "cabinet"}},
        {"id": 12, "time": "t", "kind": "object_detected", "summary": "cabinet", "data": {"label": "Cabinet"}},
    ]

    messages, mirrored = _pump_one_batch(flapping)

    assert len(mirrored) == 12  # the GUI log still shows every event
    lines = messages[0].splitlines()
    assert [line.split("] ")[1].split(":")[0] for line in lines[:-1]] == [
        "link_lost", "link_restored", "object_detected"
    ]
    for repeats in ("link_lost x4", "link_restored x4", "object_detected x1"):
        assert repeats in lines[-1]


def test_region_events_wake_the_mission_once_per_watch_and_kind_and_count_repeats():
    def region(event_id, kind, watch_id, target_id):
        return {
            "id": event_id,
            "time": "t",
            "kind": kind,
            "summary": f'Target {target_id} "person" {kind} ({watch_id})',
            "data": {"watch_id": watch_id, "target_id": target_id},
        }

    messages, mirrored = _pump_one_batch([
        region(1, "region_enter", "watch-1", 4),
        region(2, "region_enter", "watch-1", 5),
        region(3, "region_enter", "watch-2", 6),
        region(4, "region_dwell", "watch-1", 4),
        region(5, "region_enter", "watch-1", 7),
        region(6, "region_watch_expired", "watch-2", 0),
    ])

    assert len(mirrored) == 6
    lines = messages[0].splitlines()
    assert [line.split("]")[0] for line in lines[:-1]] == [
        "[HORUS event #1 t", "[HORUS event #3 t", "[HORUS event #4 t", "[HORUS event #6 t"
    ]
    assert "region_enter[watch-1] x2" in lines[-1]


def test_wait_only_events_are_neither_mirrored_nor_wake_the_mission():
    messages, mirrored = _pump_one_batch([
        {"id": 1, "time": "t", "kind": "vocabulary_changed", "summary": "terms", "data": {}},
        {"id": 2, "time": "t", "kind": "task_finished", "summary": "goto succeeded", "data": {}},
    ])

    assert [event["kind"] for event in mirrored] == ["task_finished"]
    assert len(messages) == 1 and "goto succeeded" in messages[0] and "terms" not in messages[0]


def test_commander_service_reads_the_mcp_token_from_the_configured_auth_token_env(
    monkeypatch, capsys, tmp_path
):
    import pytest

    from horus.commander.server import main

    config = tmp_path / "horus.yaml"
    config.write_text("server:\n  auth_token_env: CUSTOM_MCP_TOKEN\n", encoding="utf-8")
    monkeypatch.setenv("HORUS_COMMANDER_TOKEN", "commander-secret")
    monkeypatch.setenv("HORUS_MCP_TOKEN", "default-secret")
    monkeypatch.delenv("CUSTOM_MCP_TOKEN", raising=False)

    with pytest.raises(SystemExit) as error:
        main(["--config", str(config)])

    assert error.value.code == 2
    assert "CUSTOM_MCP_TOKEN must be set" in capsys.readouterr().err


def test_start_up_prints_the_app_address_with_the_token_only_to_the_terminal(monkeypatch, capsys, tmp_path):
    import socket

    from horus.commander import server

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = "commander+start/up token"
    apps = []

    class _StartedCommander:
        event_callback = None

        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            # Stop once the service is reachable; main() then reports the session ended.
            for _ in range(200):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)

        async def stop(self):
            pass

    class _IdleDisplay:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            await asyncio.Event().wait()

    def capture_app(*args, **kwargs):
        apps.append(create_app(*args, **kwargs))
        return apps[-1]

    monkeypatch.setattr(server, "HorusCommander", _StartedCommander)
    monkeypatch.setattr(server, "DisplayReader", _IdleDisplay)
    monkeypatch.setattr(server, "create_app", capture_app)
    monkeypatch.setenv("HORUS_COMMANDER_TOKEN", token)
    monkeypatch.setenv("HORUS_MCP_TOKEN", "mcp-token")
    monkeypatch.setenv("HORUS_EVENT_LOG", str(tmp_path / "events.jsonl"))

    try:
        server.main(["--port", str(port)])
    except RuntimeError as exc:
        assert "stopped unexpectedly" in str(exc)

    out = capsys.readouterr().out
    url = f"http://127.0.0.1:{port}/#token=commander%2Bstart%2Fup%20token"
    assert f"HORUS control app: {url}" in out.splitlines()
    assert server.control_app_url(port, token) == url
    (app,) = apps
    assert token not in json.dumps(app.event_hub.events_after(0))
    events_log = tmp_path / "events.jsonl"
    assert not events_log.exists() or token not in events_log.read_text(encoding="utf-8")


_SIGTERM_SERVICE = """
import asyncio, sys
from pathlib import Path
from horus.commander import server

pid_file = Path(sys.argv[1])

class _Commander:
    event_callback = None

    def __init__(self, *args, **kwargs):
        self._stopped = asyncio.Event()

    async def run(self):
        # Stands in for the Claude Code process the real commander keeps for the mission.
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(600)",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        pid_file.write_text(str(child.pid))
        try:
            await self._stopped.wait()
        finally:
            child.kill()
            await child.wait()

    async def stop(self):
        self._stopped.set()

class _Display:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self):
        await asyncio.Event().wait()

server.HorusCommander = _Commander
server.DisplayReader = _Display
server.main(["--port", sys.argv[2]])
"""


def test_sigterm_stops_the_service_through_its_cleanup_so_no_model_process_is_left(tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    pid_file = tmp_path / "model.pid"
    env = dict(
        os.environ,
        HORUS_COMMANDER_TOKEN="commander-token",
        HORUS_MCP_TOKEN="mcp-token",
        HORUS_EVENT_LOG=str(tmp_path / "events.jsonl"),
    )
    service = subprocess.Popen(
        [sys.executable, "-c", _SIGTERM_SERVICE, str(pid_file), str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    model_pid = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and service.poll() is None:
            if pid_file.exists() and pid_file.read_text():
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    pass
            time.sleep(0.05)
        model_pid = int(pid_file.read_text())
        os.kill(model_pid, 0)  # the stand-in model process runs while the service is up

        service.send_signal(signal.SIGTERM)
        output, _ = service.communicate(timeout=20)

        assert service.returncode == 0, output
        try:
            os.kill(model_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("the model process outlived the service")
    finally:
        if service.poll() is None:
            service.kill()
            service.wait()
        if model_pid is not None:
            try:
                os.kill(model_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_each_service_run_names_its_event_stream_so_pages_notice_a_restart():
    async def run_header(app):
        app.event_hub.publish({"kind": "task_accepted", "text": "hover"})
        capture = _SSECapture(app, frame_count=1)
        await capture.start()
        messages = await capture.finish()
        start = next(m for m in messages if m["type"] == "http.response.start")
        return dict(start["headers"])[b"x-horus-run"], _event_frames(messages)

    first_app, restarted_app = create_app(_FakeCommander(), _TOKEN), create_app(_FakeCommander(), _TOKEN)
    first, first_frames = asyncio.run(run_header(first_app))
    again, _ = asyncio.run(run_header(first_app))
    restarted, restarted_frames = asyncio.run(run_header(restarted_app))

    assert first == again != restarted
    # Ids restart with the run, which is why the page needs the run id to replay correctly.
    assert first_frames[0][0] == restarted_frames[0][0] == 1


_PICTURE = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8


class _PictureBackend:
    """A backend whose model received one picture from get_frame."""

    def __init__(self, pictures):
        self.pictures = pictures

    async def stream_records(self):
        for index, picture in enumerate(self.pictures):
            yield {
                "source": "anthropic", "kind": "ai_view", "tool": "get_frame",
                "tool_use_id": f"call-{index}", "mime_type": "image/jpeg", "image": picture,
            }


def test_ai_views_are_fetched_by_id_with_either_token_and_never_travel_through_events(tmp_path):
    import base64

    phone = "phone-secret-token"
    log_path = tmp_path / "events.jsonl"
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp", _TOKEN, CommanderSettings(), event_log=JsonlEventLog(log_path)
    )
    app = create_app(commander, _TOKEN, phone_token=phone)

    async def exercise():
        none_yet = await _get(app, "/api/ai-view/latest", {"Authorization": f"Bearer {phone}"})
        live = _SSECapture(app, frame_count=1, headers=[(b"authorization", f"Bearer {phone}".encode())])
        await live.start()
        await commander._forward_backend_records(_PictureBackend([_PICTURE]))
        frames = _event_frames(await live.finish())
        view_id = frames[0][1]["view_id"]
        responses = {}
        for name, headers in {
            "phone": {"Authorization": f"Bearer {phone}"},
            "laptop": {"Authorization": f"Bearer {_TOKEN}"},
            "missing": {},
            "wrong": {"Authorization": "Bearer phone-secret-tokeX"},
        }.items():
            responses[name] = await _get(app, f"/api/ai-view/{view_id}", headers)
        latest = await _get(app, "/api/ai-view/latest", {"Authorization": f"Bearer {phone}"})
        unauthorized_latest = await _get(app, "/api/ai-view/latest", {})
        unknown = await _get(app, "/api/ai-view/0123456789abcdef", {"Authorization": f"Bearer {_TOKEN}"})
        posted = await _post(app, f"/api/ai-view/{view_id}", {"Authorization": f"Bearer {_TOKEN}"})
        return none_yet, frames, responses, latest, unauthorized_latest, unknown, posted

    none_yet, frames, responses, latest, unauthorized_latest, unknown, posted = asyncio.run(exercise())

    assert none_yet.status_code == 404
    [(_, event)] = frames
    assert {key: event[key] for key in ("kind", "source", "tool", "tool_use_id", "mime_type", "bytes")} == {
        "kind": "ai_view", "source": "anthropic", "tool": "get_frame", "tool_use_id": "call-0",
        "mime_type": "image/jpeg", "bytes": len(_PICTURE),
    }
    for name in ("phone", "laptop"):
        response = responses[name]
        assert response.status_code == 200
        assert response.content == _PICTURE
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, max-age=86400, immutable"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert responses["missing"].status_code == responses["wrong"].status_code == 401
    assert unauthorized_latest.status_code == 401
    assert latest.status_code == 200 and latest.json()["view_id"] == event["view_id"]
    assert latest.json()["bytes"] == len(_PICTURE)
    assert unknown.status_code == 404
    assert posted.status_code == 405
    # Neither the stream, its replay history nor the event log carries the picture.
    encoded = base64.b64encode(_PICTURE).decode("ascii")
    for serialized in (json.dumps(frames), json.dumps(app.event_hub.events_after(0)), log_path.read_text()):
        assert encoded[:64] not in serialized and "image\"" not in serialized.replace("image/jpeg", "")


async def _post(app, path, headers):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=headers)


def test_ai_view_store_keeps_the_newest_views_by_count_and_bytes_and_evicted_ids_answer_404():
    from horus.commander.ai_views import AIViewStore

    store = AIViewStore(max_count=3, max_bytes=100)
    first, second, third, fourth = (store.add(bytes([n]) * 10, "image/jpeg", "get_frame") for n in range(4))
    # Count: the fourth pushes the first out.
    assert store.get(first.view_id) is None
    assert [store.get(view.view_id) for view in (second, third, fourth)] == [second, third, fourth]
    big = store.add(b"b" * 60, "image/png", "list_candidates")  # 4 views > 3: second goes
    assert store.get(second.view_id) is None and store.total_bytes == 80
    bigger = store.add(b"c" * 50, "image/jpeg", "point")  # third (count), fourth and big (bytes) go
    assert [view.view_id for view in (third, fourth, big) if store.get(view.view_id)] == []
    assert store.latest() == bigger and len(store) == 1 and store.total_bytes == 50
    # Never kept: larger than the whole budget, empty, or not a picture type.
    assert store.add(b"x" * 101, "image/jpeg", "get_frame") is None
    assert store.add(b"", "image/jpeg", "get_frame") is None
    assert store.add(b"<svg/>", "image/svg+xml", "get_frame") is None
    assert store.latest() == bigger and store.total_bytes == 50

    commander = HorusCommander("http://127.0.0.1:8000/mcp", _TOKEN, CommanderSettings(), ai_views=store)
    app = create_app(commander, _TOKEN)
    auth = {"Authorization": f"Bearer {_TOKEN}"}

    async def fetch():
        return [
            (await _get(app, f"/api/ai-view/{view.view_id}", auth)).status_code for view in (first, bigger)
        ]

    assert asyncio.run(fetch()) == [404, 200]
