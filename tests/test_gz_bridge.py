from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from horus.gz_bridge import (
    BridgeConfig,
    GazeboBridge,
    SSEDecoder,
    SSEEvent,
    format_commander_event,
    poll_mcp_state,
    publish_state_once,
    state_from_tool_result,
    stream_commander_events,
)


class _StringMsg:
    def __init__(self):
        self.data = ""


class _Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message.data)
        return True


class _Node:
    def __init__(self):
        self.publishers = {}
        self.subscription = None
        self.subscriptions = {}

    def advertise(self, topic, message_type):
        assert message_type is _StringMsg
        publisher = _Publisher()
        self.publishers[topic] = publisher
        return publisher

    def subscribe(self, message_type, topic, callback):
        assert message_type is _StringMsg
        if self.subscription is None:
            self.subscription = (topic, callback)
        self.subscriptions[topic] = callback
        return True


class _TaskHttpClient:
    def __init__(self):
        self.request = None
        self.requests = []
        self.called = asyncio.Event()

    async def post(self, url, *, headers, json=None):
        self.request = (url, headers, json)
        self.requests.append(self.request)
        self.called.set()
        return SimpleNamespace(raise_for_status=lambda: None)


class _SSEResponse:
    def __init__(self, lines):
        self.lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _SSEContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class _SSEHttpClient:
    def __init__(self):
        self.requests = []
        self.responses = [
            [
                "id: 40",
                'data: { "id": 40, "kind": "task_accepted", "text": "Find the red shirt" }',
                "",
                "id: 41",
                'data: { "id": 41, "kind": "assistant_delta", "text": "Loo" }',
                "",
            ],
            ["id: 42", 'data: { "id": 42, "kind": "final_result", "text": "Holding." }', ""],
        ]

    def stream(self, method, url, *, headers, timeout=None):
        self.requests.append((method, url, dict(headers)))
        lines = self.responses[len(self.requests) - 1]
        return _SSEContext(_SSEResponse(lines))


class _MCPStateSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def initialize(self):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def test_config_defaults_and_environment_contract():
    config = BridgeConfig.from_env({})

    assert config.commander_url == "http://127.0.0.1:8765"
    assert config.mcp_url == "http://127.0.0.1:8000/mcp"
    assert (config.task_topic, config.emergency_topic, config.reset_topic, config.events_topic, config.state_topic) == (
        "/horus/task",
        "/horus/emergency",
        "/horus/reset",
        "/horus/events",
        "/horus/state",
    )
    configured = BridgeConfig.from_env(
        {
            "HORUS_COMMANDER_URL": "http://commander:8765/",
            "HORUS_COMMANDER_TOKEN": "commander-secret",
            "HORUS_MCP_URL": "http://mcp:8000/mcp",
            "HORUS_MCP_TOKEN": "mcp-secret",
            "HORUS_EMERGENCY_TOPIC": "/control/emergency",
            "HORUS_RESET_TOPIC": "/control/reset",
        }
    )
    assert configured.commander_token == "commander-secret"
    assert configured.mcp_url == "http://mcp:8000/mcp"
    assert configured.mcp_token == "mcp-secret"
    assert configured.emergency_topic == "/control/emergency"
    assert configured.reset_topic == "/control/reset"


def test_gazebo_emergency_and_reset_topics_post_authenticated_commander_requests():
    async def scenario():
        config = BridgeConfig(commander_url="http://commander:8765/", commander_token="secret")
        node = _Node()
        http_client = _TaskHttpClient()
        bridge = GazeboBridge(node, _StringMsg, http_client, config, asyncio.get_running_loop())

        emergency = node.subscriptions["/horus/emergency"]
        await asyncio.to_thread(emergency, SimpleNamespace(data="land"))
        await asyncio.wait_for(http_client.called.wait(), timeout=1)
        assert http_client.request == (
            "http://commander:8765/emergency",
            {"Authorization": "Bearer secret"},
            {"action": "land"},
        )

        http_client.called.clear()
        reset = node.subscriptions["/horus/reset"]
        await asyncio.to_thread(reset, SimpleNamespace(data="reset"))
        await asyncio.wait_for(http_client.called.wait(), timeout=1)
        assert http_client.request == (
            "http://commander:8765/reset",
            {"Authorization": "Bearer secret"},
            None,
        )
        assert bridge.events_publisher is node.publishers["/horus/events"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("config", "missing_token"),
    (
        (BridgeConfig(), "HORUS_COMMANDER_TOKEN"),
        (BridgeConfig(commander_token="commander-secret"), "HORUS_MCP_TOKEN"),
    ),
)
def test_runtime_requires_both_tokens_before_loading_gazebo(
    monkeypatch, config, missing_token
):
    import horus.gz_bridge as gz_bridge

    def fail_if_loaded():
        raise AssertionError("Gazebo bindings must remain lazy")

    monkeypatch.setattr(
        gz_bridge,
        "_load_gazebo_bindings",
        fail_if_loaded,
    )

    with pytest.raises(RuntimeError, match=missing_token):
        asyncio.run(gz_bridge.run(config))


def test_sse_decoder_waits_for_boundary_and_keeps_reconnect_id():
    decoder = SSEDecoder()
    assert decoder.feed_line(": heartbeat") is None
    assert decoder.feed_line("id: 17") is None
    assert decoder.feed_line('data: { "kind":') is None
    assert decoder.feed_line('data: "task_accepted" }') is None
    event = decoder.feed_line("")

    assert event == SSEEvent("17", '{ "kind":\n"task_accepted" }')
    assert decoder.last_event_id == "17"
    assert format_commander_event(SSEEvent("18", '{ "id": 18, "kind": "done" }')) == (
        '{"id":18,"kind":"done"}'
    )
    # Invalid IDs containing NUL must not replace a valid reconnect cursor.
    decoder.feed_line("id: invalid\x00id")
    decoder.feed_line("data: {}")
    assert decoder.feed_line("").event_id == "17"


def test_gazebo_task_callback_posts_text_to_authenticated_commander():
    async def scenario():
        config = BridgeConfig(commander_url="http://commander:8765/", commander_token="secret")
        node = _Node()
        http_client = _TaskHttpClient()
        bridge = GazeboBridge(node, _StringMsg, http_client, config, asyncio.get_running_loop())
        topic, callback = node.subscription
        assert topic == "/horus/task"

        await asyncio.to_thread(callback, SimpleNamespace(data="fly to the marked landing pad"))
        await asyncio.wait_for(http_client.called.wait(), timeout=1)
        assert http_client.request == (
            "http://commander:8765/task",
            {"Authorization": "Bearer secret"},
            {"text": "fly to the marked landing pad"},
        )
        assert bridge.events_publisher is node.publishers["/horus/events"]
        assert bridge.state_publisher is node.publishers["/horus/state"]

    asyncio.run(scenario())


def test_mcp_state_text_is_parsed_and_published_as_compact_json():
    async def scenario():
        state = {"vehicle": {"armed": False, "mode": "GUIDED"}, "perception": None}
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text=json.dumps(state, indent=2))],
        )
        session = _MCPStateSession(result)
        publisher = _Publisher()

        published = await publish_state_once(session, publisher, _StringMsg)

        assert session.calls == [("get_state", {})]
        assert published == state
        assert publisher.messages == ['{"vehicle":{"armed":false,"mode":"GUIDED"},"perception":null}']


def test_fastmcp_wrapped_state_is_published_as_the_state_object():
    state = {"state": "IDLE", "perception": {"camera": "streaming"}}
    wrapped = SimpleNamespace(isError=False, structuredContent={"result": json.dumps(state)}, content=[])
    assert state_from_tool_result(wrapped) == state
    both = SimpleNamespace(
        isError=False,
        structuredContent={"result": json.dumps(state)},
        content=[SimpleNamespace(type="text", text=json.dumps(state))],
    )
    assert state_from_tool_result(both) == state


def test_mcp_poller_connects_with_bearer_token_and_uses_half_second_period():
    async def scenario():
        state = {"vehicle": {"armed": True}}
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text=json.dumps(state))],
        )
        session = _MCPStateSession(result)
        publisher = _Publisher()
        calls = []
        pauses = []

        class StreamContext:
            async def __aenter__(self):
                return "read-stream", "write-stream", None

            async def __aexit__(self, *exc):
                return False

        def open_mcp(url, *, headers):
            calls.append((url, dict(headers)))
            return StreamContext()

        class SessionContext:
            def __init__(self, read_stream, write_stream):
                assert (read_stream, write_stream) == ("read-stream", "write-stream")

            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        async def stop_after_poll(delay):
            pauses.append(delay)
            raise asyncio.CancelledError

        try:
            await poll_mcp_state(
                BridgeConfig(mcp_url="http://mcp:8000/mcp", mcp_token="mcp-secret"),
                publisher,
                _StringMsg,
                session_factory=SessionContext,
                streamable_http_factory=open_mcp,
                sleep=stop_after_poll,
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("MCP state polling should remain active until cancelled")

        assert calls == [
            ("http://mcp:8000/mcp", {"Authorization": "Bearer mcp-secret"})
        ]
        assert session.calls == [("get_state", {})]
        assert publisher.messages == ['{"vehicle":{"armed":true}}']
        assert len(pauses) == 1 and 0 <= pauses[0] <= 0.5

    asyncio.run(scenario())


def test_event_stream_resumes_after_hidden_events_and_forwards_readable_lines():
    async def scenario():
        client = _SSEHttpClient()
        publisher = _Publisher()
        sleep_calls = 0

        async def stop_after_second_connection(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                raise asyncio.CancelledError

        try:
            await stream_commander_events(
                client,
                publisher,
                _StringMsg,
                BridgeConfig(commander_url="http://commander:8765", commander_token="secret"),
                sleep=stop_after_second_connection,
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("event stream should remain connected until cancelled")

        assert client.requests == [
            (
                "GET",
                "http://commander:8765/events",
                {"Authorization": "Bearer secret"},
            ),
            (
                "GET",
                "http://commander:8765/events",
                {"Authorization": "Bearer secret", "Last-Event-ID": "41"},
            ),
        ]
        # The token delta is not shown but still advances the reconnect cursor.
        assert publisher.messages == [
            "▶ Task: Find the red shirt",
            "✔ Final: Holding.",
        ]

    asyncio.run(scenario())


def test_commander_events_render_tool_calls_and_errors_readably():
    def line(data):
        return format_commander_event(SSEEvent("1", data))

    assert line('{"kind":"tool_use","tool":"mcp__horus__follow","args":{"id":7}}') == (
        '→ follow {"id":7}'
    )
    assert line('{"kind":"tool_result","summary":"ok","is_error":false}') == "← ok"
    assert line('{"kind":"tool_result","summary":"stale frame","is_error":true}') == (
        "✗ stale frame"
    )
    assert line('{"kind":"task_error","error":"boom"}') == "✗ Task failed: boom"
    assert line('{"kind":"emergency","action":"land","status":"executed","result":"ok"}') == (
        "!! Emergency LAND executed: ok"
    )
    assert line('{"kind":"session_restarted"}') == "↻ Claude session restarted"
    assert line('{"kind":"target_lost","summary":"track 3"}') == "• target lost: track 3"
    assert line('{"kind":"assistant_text","text":"Checking\\n  state"}') == "Claude: Checking state"
    long_line = line('{"kind":"assistant_text","text":"' + "x" * 1000 + '"}')
    assert len(long_line) == len("Claude: ") + 400 and long_line.endswith("…")
    assert line("not json") == "not json"


def test_mcp_tool_errors_and_missing_json_are_not_published_as_state():
    for result, expected_error in (
        (SimpleNamespace(isError=True, content=[]), RuntimeError),
        (SimpleNamespace(isError=False, content=[SimpleNamespace(type="image")]), ValueError),
    ):
        try:
            state_from_tool_result(result)
        except expected_error:
            pass
        else:
            raise AssertionError(f"expected {expected_error.__name__}")
