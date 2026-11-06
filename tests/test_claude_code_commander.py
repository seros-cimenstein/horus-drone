from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from horus.commander.agent import CommanderEvent, HorusCommander
from horus.commander.backends import APICommanderBackend, ClaudeCodeSession
from horus.config import CommanderSettings, ServerSettings
from horus.event_log import JsonlEventLog

# A stand-in camera picture: the bytes an MCP image block carries base64-encoded.
_PICTURE = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 16


def _fake_claude(tmp_path: Path, monkeypatch, *, crash_first: bool = False) -> tuple[Path, Path, Path, Path]:
    executable = tmp_path / "fake-claude"
    args_path = tmp_path / "args.json"
    inputs_path = tmp_path / "inputs.jsonl"
    launches_path = tmp_path / "launches.txt"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "Path(os.environ['HORUS_FAKE_ARGS']).write_text(json.dumps(args))\n"
        "config = json.loads(Path(args[args.index('--mcp-config') + 1]).read_text())\n"
        "Path(os.environ['HORUS_FAKE_CONFIG']).write_text(json.dumps(config))\n"
        "launches = Path(os.environ['HORUS_FAKE_LAUNCHES'])\n"
        "count = int(launches.read_text()) + 1 if launches.exists() else 1\n"
        "launches.write_text(str(count))\n"
        "print(json.dumps({'type':'system','subtype':'init','mcp_servers':[{'name':'horus'}]}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('type') == 'control_request':\n"
        "        print(json.dumps({'type':'control_response','response':{'subtype':'success'}}), flush=True)\n"
        "        continue\n"
        "    text = message['message']['content'][0]['text']\n"
        "    with open(os.environ['HORUS_FAKE_INPUTS'], 'a') as stream: stream.write(json.dumps(text)+'\\n')\n"
        f"    if {crash_first!r} and count == 1: sys.exit(23)\n"
        "    print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Handled: '+text}]}}), flush=True)\n"
        "    print(json.dumps({'type':'result','subtype':'success','result':'Handled: '+text}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("HORUS_FAKE_ARGS", str(args_path))
    monkeypatch.setenv("HORUS_FAKE_CONFIG", str(tmp_path / "mcp-config.json"))
    monkeypatch.setenv("HORUS_FAKE_INPUTS", str(inputs_path))
    monkeypatch.setenv("HORUS_FAKE_LAUNCHES", str(launches_path))
    return executable, args_path, inputs_path, launches_path


async def _records(session: ClaudeCodeSession) -> list[dict]:
    return [dict(record) async for record in session.stream_records()]


def test_persistent_session_maps_queued_operator_messages_to_results(tmp_path, monkeypatch):
    executable, args_path, inputs_path, launches_path = _fake_claude(tmp_path, monkeypatch)
    settings = CommanderSettings(
        claude_command=str(executable),
        max_turns=37,
        timeout_s=5.0,
        claude_model="opus",
        claude_effort="high",
    )
    backend = ClaudeCodeSession(
        "http://127.0.0.1:8000/mcp", "secret-token", settings, "HORUS safety prompt"
    )
    commander = HorusCommander("http://127.0.0.1:8000/mcp", "secret-token", settings)
    commander.backend = backend

    async def scenario():
        record_task = asyncio.create_task(_records(backend))
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        second = loop.create_future()
        await asyncio.gather(
            commander._process_message(CommanderEvent("inspect north", first)),
            commander._process_message(CommanderEvent("hold position", second)),
        )
        responses = await asyncio.gather(first, second)
        await backend.close()
        return responses, await record_task

    responses, records = asyncio.run(scenario())

    assert responses == ["Handled: [operator] inspect north", "Handled: [operator] hold position"]
    assert launches_path.read_text() == "1"
    messages = [json.loads(line) for line in inputs_path.read_text().splitlines()]
    assert messages == ["[operator] inspect north", "[operator] hold position"]
    args = json.loads(args_path.read_text())
    assert "-p" not in args
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args and "--include-partial-messages" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__horus__*"
    disallowed = args[args.index("--disallowedTools") + 1 : args.index("--append-system-prompt")]
    assert {
        "mcp__horus__wait_events", "mcp__horus__operator_emergency", "mcp__horus__operator_release",
        "mcp__horus__get_map_index", "mcp__horus__get_map_tile", "mcp__horus__get_live_map",
    } <= set(disallowed)
    assert "mcp__horus__wait_for_event" not in disallowed
    assert args[args.index("--append-system-prompt") + 1] == "HORUS safety prompt"
    assert args[args.index("--max-turns") + 1] == "37"
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "high"
    assert json.loads((tmp_path / "mcp-config.json").read_text()) == {
        "mcpServers": {
            "horus": {
                "type": "http",
                "url": "http://127.0.0.1:8000/mcp",
                "headers": {"Authorization": "Bearer secret-token"},
            }
        }
    }
    assert [record["kind"] for record in records].count("final_result") == 2


def test_persistent_session_restarts_after_process_crash(tmp_path, monkeypatch):
    executable, _, _, launches_path = _fake_claude(tmp_path, monkeypatch, crash_first=True)
    session = ClaudeCodeSession(
        "http://127.0.0.1:8000/mcp",
        "token",
        CommanderSettings(claude_command=str(executable), timeout_s=5.0),
        "system prompt",
    )

    async def scenario():
        record_task = asyncio.create_task(_records(session))
        try:
            await session.send("[operator] first task")
        except RuntimeError as exc:
            assert "exited" in str(exc)
        else:
            raise AssertionError("the first fake process must crash")
        response = await session.send("[operator] next task")
        await session.close()
        return response, await record_task

    response, records = asyncio.run(scenario())
    assert response == "Handled: [operator] next task"
    assert launches_path.read_text() == "2"
    assert any(record["kind"] == "session_restarted" for record in records)


# Behaves like Claude Code's stream-json input (probed 2026-11-01): each turn (one system/init,
# one result) starts with the oldest queued message, even when several are queued; a message
# that arrives during a turn is folded into it at the next tool-call boundary; one that arrives
# after the last boundary waits and starts the next turn. So one `result` can answer several.
_FOLDING_CLAUDE = """\
import json, sys, threading, time
queued, lock = [], threading.Lock()
def read():
    for line in sys.stdin:
        message = json.loads(line)
        if message.get('type') == 'user':
            with lock:
                queued.append(message['message']['content'][0]['text'])
threading.Thread(target=read, daemon=True).start()
def take(everything):
    with lock:
        taken = list(queued) if everything else queued[:1]
        del queued[:len(taken)]
    return taken
turn = 0
while True:
    texts = take(False)
    if not texts:
        time.sleep(0.02)
        continue
    turn += 1
    print(json.dumps({'type': 'system', 'subtype': 'init', 'mcp_servers': [{'name': 'horus'}]}), flush=True)
    for boundary in range(2):  # two tool calls; queued messages are folded in after each
        time.sleep(0.3)
        texts += take(True)
    time.sleep(0.3)  # the final answer: anything arriving now waits for the next turn
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': f'turn {turn}: ' + ' | '.join(texts)}), flush=True)
"""


def test_messages_folded_into_a_running_turn_are_answered_instead_of_waiting_out_the_timeout(
    tmp_path, monkeypatch
):
    import horus.commander.backends as backends

    monkeypatch.setattr(backends, "_FOLDED_MESSAGE_GRACE_S", 0.3, raising=False)
    executable = tmp_path / "folding-claude"
    executable.write_text(f"#!{sys.executable}\n{_FOLDING_CLAUDE}", encoding="utf-8")
    executable.chmod(0o755)
    session = ClaudeCodeSession(
        "http://127.0.0.1:8000/mcp", "token",
        CommanderSettings(claude_command=str(executable), timeout_s=5.0), "system prompt",
    )

    async def scenario():
        records = asyncio.create_task(_records(session))
        # Two messages at once: the second is folded into the first one's turn.
        both = await asyncio.gather(session.send("[operator] one"), session.send("[operator] two"))
        # A mission, and an event that arrives while its turn runs (the GTA run's task_finished).
        mission = asyncio.create_task(session.send("[operator] fly north"))
        await asyncio.sleep(0.1)
        folded = asyncio.create_task(session.send("[HORUS event] take_off finished"))
        first = await asyncio.gather(mission, folded)
        # HOME: its event starts a turn, the operator notice is folded into it, and the landing
        # event arrives during the final answer, so it starts the next turn.
        rth = asyncio.create_task(session.send("[HORUS event] emergency rth"))
        await asyncio.sleep(0.05)
        notice = asyncio.create_task(session.send("[operator emergency] rth executed"))
        await asyncio.sleep(0.7)
        landed = asyncio.create_task(session.send("[HORUS event] landed"))
        second = await asyncio.gather(rth, notice, landed)
        await session.close()
        await records
        return both, first, second

    (one, two), (mission, folded), (rth, notice, landed) = asyncio.run(scenario())

    # Every message is answered, none waits out timeout_s (before: TimeoutError, then a reset).
    assert one == two == "turn 1: [operator] one | [operator] two"
    assert mission.startswith("turn 2:") and "fly north" in mission
    assert folded == mission  # folded into the mission's turn, answered by its result
    assert rth.startswith("turn 3:") and "emergency rth" in rth
    assert landed.startswith("turn 4:") and "landed" in landed
    assert notice.startswith(("turn 3:", "turn 4:"))


_SLOW_CLAUDE = """\
import json, sys, time
for line in sys.stdin:
    if json.loads(line).get('type') != 'user':
        continue
    print(json.dumps({'type': 'system', 'subtype': 'init', 'mcp_servers': [{'name': 'horus'}]}), flush=True)
    for step in range(STEPS):  # a long mission: a tool call and its result every 0.2 s
        time.sleep(0.2)
        print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'step {step}'}]}}), flush=True)
    time.sleep(SILENCE)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'arrived'}), flush=True)
"""


@pytest.mark.parametrize(("steps", "silence", "answered"), [(8, 0.0, True), (0, 2.0, False)])
def test_timeout_bounds_silence_not_a_long_mission_that_keeps_working(tmp_path, steps, silence, answered):
    executable = tmp_path / "slow-claude"
    executable.write_text(
        f"#!{sys.executable}\n" + _SLOW_CLAUDE.replace("STEPS", str(steps)).replace("SILENCE", str(silence)),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    session = ClaudeCodeSession(
        "http://127.0.0.1:8000/mcp", "token",
        CommanderSettings(claude_command=str(executable), timeout_s=0.8), "system prompt",
    )

    async def scenario():
        records = asyncio.create_task(_records(session))
        try:
            return await session.send("[operator] fly to P1")
        except TimeoutError as exc:
            return exc
        finally:
            await session.close()
            await records

    outcome = asyncio.run(scenario())

    # 8 steps take 1.6 s, twice timeout_s, but output never pauses for 0.8 s; 2 s of silence does.
    if answered:
        assert outcome == "arrived"
    else:
        assert isinstance(outcome, TimeoutError) and "wrote nothing" in str(outcome)


def test_claude_stream_records_keep_thinking_tool_and_final_result_details():
    from horus.commander.backends import _claude_stream_records

    records = _claude_stream_records({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "The pose is stale; read state first.", "signature": "x"},
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "name": "mcp__horus__get_state", "input": {"id": 3}},
            ]
        },
    })
    assert [record["kind"] for record in records] == ["thinking", "assistant_text", "tool_use"]
    assert records[0]["text"] == "The pose is stale; read state first."
    result = _claude_stream_records({"type": "result", "subtype": "success", "result": "Done"})
    assert result == [{"kind": "final_result", "text": "Done", "subtype": "success"}]


# Claude Code's stream-json for one HORUS tool call that returns a picture (its tool_result
# carries Anthropic image blocks), plus one image from a tool that is not a HORUS tool.
_LOOKING_CLAUDE = """\
import json, os, sys
picture = os.environ['HORUS_FAKE_PICTURE']
print(json.dumps({'type': 'system', 'subtype': 'init', 'mcp_servers': [{'name': 'horus'}]}), flush=True)
for line in sys.stdin:
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': picture}}
    print(json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'toolu_look', 'name': 'mcp__horus__list_candidates', 'input': {}},
        {'type': 'tool_use', 'id': 'toolu_other', 'name': 'mcp__other__screenshot', 'input': {}},
    ]}}), flush=True)
    print(json.dumps({'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_look', 'content': [{'type': 'text', 'text': '{"frame_id": 7}'}, image]},
        {'type': 'tool_result', 'tool_use_id': 'toolu_other', 'content': [image]},
    ]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'I see a plaza.'}), flush=True)
"""


def test_claude_code_image_results_become_ai_views_kept_out_of_events_and_the_log(tmp_path, monkeypatch):
    encoded = base64.b64encode(_PICTURE).decode("ascii")
    monkeypatch.setenv("HORUS_FAKE_PICTURE", encoded)
    executable = tmp_path / "looking-claude"
    executable.write_text(f"#!{sys.executable}\n{_LOOKING_CLAUDE}", encoding="utf-8")
    executable.chmod(0o755)
    settings = CommanderSettings(claude_command=str(executable), timeout_s=5.0)
    events = []
    log_path = tmp_path / "events.jsonl"
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp", "secret-token", settings,
        event_log=JsonlEventLog(log_path), event_callback=events.append,
    )
    backend = ClaudeCodeSession("http://127.0.0.1:8000/mcp", "secret-token", settings, "prompt")
    commander.backend = backend

    async def scenario():
        forward = asyncio.create_task(commander._forward_backend_records(backend))
        response = asyncio.get_running_loop().create_future()
        await commander._process_message(CommanderEvent("look once", response))
        answer = await response
        await backend.close()
        await forward
        return answer

    assert asyncio.run(scenario()) == "I see a plaza."
    views = [event for event in events if event["kind"] == "ai_view"]
    # Only the HORUS tool's picture counts; the other tool's image is not what the AI pilot saw.
    assert len(views) == 1
    view = views[0]
    assert {key: view[key] for key in ("source", "tool", "tool_use_id", "mime_type", "bytes")} == {
        "source": "claude-code", "tool": "list_candidates", "tool_use_id": "toolu_look",
        "mime_type": "image/jpeg", "bytes": len(_PICTURE),
    }
    assert commander.ai_views.get(view["view_id"]).data == _PICTURE
    assert commander.ai_views.latest().view_id == view["view_id"] and len(commander.ai_views) == 1
    log_text = log_path.read_text(encoding="utf-8")
    assert "ai_view" in log_text
    assert all("image" not in event for event in events)
    for serialized in (json.dumps(events), log_text):
        assert encoded[:64] not in serialized


def test_claude_stream_records_decode_image_tool_results_and_skip_invalid_ones():
    from horus.commander.backends import _claude_stream_records

    encoded = base64.b64encode(_PICTURE).decode("ascii")
    records = _claude_stream_records({"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "toolu_1", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "not base64!"}},
            {"type": "image", "source": {"type": "url", "url": "https://example.invalid/x.jpg"}},
        ],
    }]}})
    assert [record["kind"] for record in records] == ["tool_result", "ai_view"]
    assert records[0]["summary"] == "[3 image(s), payload omitted]"
    assert records[1] == {"kind": "ai_view", "tool_use_id": "toolu_1", "mime_type": "image/png", "image": _PICTURE}


def test_mcp_server_defaults_to_loopback_only():
    assert ServerSettings().host == "127.0.0.1"
    for host in ("0.0.0.0", "192.168.1.2"):
        with pytest.raises(ValueError):
            ServerSettings(host=host)


def test_api_backend_retains_operator_and_event_history_per_mission(monkeypatch):
    requests = []

    class Messages:
        async def create(self, **arguments):
            requests.append([dict(message) for message in arguments["messages"]])
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"reply-{len(requests)}")]
            )

    client = SimpleNamespace(messages=Messages())
    anthropic = ModuleType("anthropic")
    anthropic.AsyncAnthropic = lambda **_kwargs: client
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    backend = APICommanderBackend(
        "anthropic",
        CommanderSettings(provider="anthropic"),
        "api-key",
        session=object(),
        mcp_tools=[],
        system_prompt="HORUS safety prompt",
    )

    async def scenario():
        first = await backend.send("[operator] inspect target")
        second = await backend.send("[HORUS event #7 12:00Z] link_lost: Autopilot heartbeat lost")
        await backend.close()
        return first, second

    assert asyncio.run(scenario()) == ("reply-1", "reply-2")
    assert requests[0] == [{"role": "user", "content": "[operator] inspect target"}]
    assert [message["role"] for message in requests[1]] == ["user", "assistant", "user"]
    assert requests[1][0]["content"] == "[operator] inspect target"
    assert requests[1][1]["content"][0].text == "reply-1"
    assert requests[1][2]["content"] == "[HORUS event #7 12:00Z] link_lost: Autopilot heartbeat lost"


def _anthropic_tool_backend(monkeypatch, requests, tool_gate):
    """Model asks for get_state on '[operator] inspect'; answers every other turn with text."""

    class Messages:
        async def create(self, **arguments):
            messages = [dict(message) for message in arguments["messages"]]
            requests.append(messages)
            last = messages[-1]["content"]
            if last == "[operator] inspect":
                return SimpleNamespace(content=[
                    SimpleNamespace(type="tool_use", id=f"call-{len(requests)}", name="get_state", input={})
                ])
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"reply-{len(requests)}")])

    anthropic = ModuleType("anthropic")
    anthropic.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)

    class Session:
        async def call_tool(self, name, arguments):
            tool_gate["started"].set()
            await asyncio.wait_for(tool_gate["release"].wait(), timeout=5)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="{}")])

    return APICommanderBackend(
        "anthropic",
        CommanderSettings(provider="anthropic"),
        "api-key",
        session=Session(),
        mcp_tools=[SimpleNamespace(name="get_state", description="", inputSchema={})],
        system_prompt="HORUS safety prompt",
    )


def _dangling_tool_calls(messages):
    answered = {
        block["tool_use_id"]
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    return [
        block.id
        for message in messages
        if message["role"] == "assistant"
        for block in message["content"]
        if getattr(block, "type", None) == "tool_use" and block.id not in answered
    ]


def test_interrupting_a_tool_call_rolls_back_the_turn_so_the_next_request_is_valid(monkeypatch):
    requests = []

    async def scenario():
        gate = {"started": asyncio.Event(), "release": asyncio.Event()}
        backend = _anthropic_tool_backend(monkeypatch, requests, gate)
        active = asyncio.create_task(backend.send("[operator] inspect"))
        await asyncio.wait_for(gate["started"].wait(), timeout=1)
        waiting = asyncio.create_task(backend.send("[HORUS event #3 t] task_finished: done"))
        await asyncio.sleep(0)
        await backend.interrupt()
        outcomes = await asyncio.gather(active, waiting, return_exceptions=True)
        reply = await backend.send("[operator] status?")
        await backend.close()
        return outcomes, reply

    outcomes, reply = asyncio.run(scenario())

    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert reply == f"reply-{len(requests)}"
    assert requests[-1] == [{"role": "user", "content": "[operator] status?"}]
    assert _dangling_tool_calls(requests[-1]) == []


def test_cancelling_a_queued_turn_keeps_the_active_turn_history(monkeypatch):
    requests = []

    async def scenario():
        gate = {"started": asyncio.Event(), "release": asyncio.Event()}
        backend = _anthropic_tool_backend(monkeypatch, requests, gate)
        active = asyncio.create_task(backend.send("[operator] inspect"))
        await asyncio.wait_for(gate["started"].wait(), timeout=1)
        queued = asyncio.create_task(backend.send("[operator] queued"))
        await asyncio.sleep(0)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        gate["release"].set()
        first = await active
        second = await backend.send("[operator] status?")
        await backend.close()
        return first, second

    first, second = asyncio.run(scenario())

    assert first == "reply-2"
    assert second == "reply-3"
    roles = [message["role"] for message in requests[-1]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert requests[-1][0]["content"] == "[operator] inspect"
    assert requests[-1][-1]["content"] == "[operator] status?"
    assert _dangling_tool_calls(requests[-1]) == []


def test_anthropic_backend_retains_images_returned_by_mcp_tools_and_reports_them_as_ai_views(monkeypatch):
    encoded = base64.b64encode(_PICTURE).decode("ascii")
    requests = []
    responses = [
        SimpleNamespace(content=[
            SimpleNamespace(type="tool_use", id="frame-call", name="get_frame", input={})
        ]),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="The frame is clear.")]),
    ]

    class Messages:
        async def create(self, **arguments):
            requests.append([dict(message) for message in arguments["messages"]])
            return responses.pop(0)

    anthropic = ModuleType("anthropic")
    anthropic.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)

    class Session:
        async def call_tool(self, name, arguments):
            assert name == "get_frame"
            assert arguments == {}
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(type="text", text='{"frame_id":7}'),
                    SimpleNamespace(type="image", mimeType="image/jpeg", data=encoded),
                ],
            )

    backend = APICommanderBackend(
        "anthropic",
        CommanderSettings(provider="anthropic"),
        "api-key",
        session=Session(),
        mcp_tools=[SimpleNamespace(name="get_frame", description="", inputSchema={})],
        system_prompt="HORUS safety prompt",
    )

    async def scenario():
        result = await backend.send("[operator] inspect the current frame")
        await backend.close()
        return result, [dict(record) async for record in backend.stream_records()]

    result, records = asyncio.run(scenario())
    assert result == "The frame is clear."
    tool_result = requests[1][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": encoded,
    }
    # The picture the model received, for the control app (HorusCommander keeps it by id).
    assert [record for record in records if record["kind"] == "ai_view"] == [{
        "source": "anthropic", "kind": "ai_view", "tool": "get_frame", "tool_use_id": "frame-call",
        "mime_type": "image/jpeg", "image": _PICTURE,
    }]


def test_api_backend_interrupt_cancels_active_and_lock_waiting_turns(monkeypatch):
    requests = []

    class Messages:
        async def create(self, **arguments):
            requests.append(arguments["messages"][-1]["content"])
            await asyncio.Event().wait()  # a model turn that has not answered yet

    anthropic = ModuleType("anthropic")
    anthropic.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    backend = APICommanderBackend(
        "anthropic", CommanderSettings(provider="anthropic"), "api-key",
        session=object(), mcp_tools=[], system_prompt="prompt",
    )

    async def scenario():
        active = asyncio.create_task(backend.send("[operator] go to the door"))
        waiting = asyncio.create_task(backend.send("[HORUS event #8] link_restored"))
        while not requests:
            await asyncio.sleep(0)
        await backend.interrupt()
        return await asyncio.wait_for(asyncio.gather(active, waiting, return_exceptions=True), 2.0)

    results = asyncio.run(scenario())

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    # The turn queued behind the interrupted one never reaches the model.
    assert requests == ["[operator] go to the door"]


def test_emergency_discards_operator_requests_queued_before_it_but_not_later_ones(monkeypatch):
    monkeypatch.delenv("HORUS_EVENT_LOG", raising=False)
    monkeypatch.delenv("HORUS_RUN_DIR", raising=False)

    class Session:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments):
            self.calls.append(name)
            text = '{"action":"hold","state":"HOLDING","preempted":true}'
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=text)])

    class Backend:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)
            return "done"

        async def interrupt(self):
            pass

    session = Session()
    backend = Backend()
    commander = HorusCommander("http://127.0.0.1:8000/mcp", "token", CommanderSettings())

    async def scenario():
        # Typed while the commander is still connecting: queued, not yet sent to any model.
        before = asyncio.create_task(commander.submit_task("follow the person in red"))
        await asyncio.sleep(0)
        result = await commander.emergency("hold", session=session)
        commander.backend = backend  # the commander finishes starting and drains its queue
        await commander._process_message(commander._events.get_nowait())
        after = asyncio.create_task(commander.submit_task("inspect the tree"))
        await asyncio.sleep(0)
        await commander._process_message(commander._events.get_nowait())
        return result, await asyncio.gather(before, after, return_exceptions=True)

    result, (before, after) = asyncio.run(scenario())

    assert session.calls == ["operator_emergency"]
    assert '"state":"HOLDING"' in result
    assert isinstance(before, RuntimeError) and "interrupted by an emergency" in str(before)
    assert after == "done"
    assert backend.sent == ["[operator] inspect the tree"]


def test_the_operators_next_mission_or_reset_releases_the_emergency_lock_before_the_model_acts(
    monkeypatch,
):
    monkeypatch.delenv("HORUS_EVENT_LOG", raising=False)
    monkeypatch.delenv("HORUS_RUN_DIR", raising=False)
    order = []

    class Session:
        async def call_tool(self, name, arguments):
            order.append(("tool", name, dict(arguments)))
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="{}")])

    class Backend:
        async def send(self, text):
            order.append(("model", text))
            return "done"

        async def interrupt(self):
            pass

        async def reset(self):
            order.append(("model", "reset"))

    commander = HorusCommander("http://127.0.0.1:8000/mcp", "token", CommanderSettings())
    commander._mcp_session = Session()
    commander.backend = Backend()

    async def scenario():
        await commander.emergency("land")
        mission = asyncio.create_task(commander.submit_task("take off again"))
        await asyncio.sleep(0)
        await commander._process_message(commander._events.get_nowait())
        await mission
        await commander.emergency("hold")
        await commander.reset()

    asyncio.run(scenario())

    assert [entry for entry in order if entry[0] == "tool"] == [
        ("tool", "operator_emergency", {"action": "land"}),
        ("tool", "operator_release", {}),
        ("tool", "operator_emergency", {"action": "hold"}),
        ("tool", "operator_release", {}),
    ]
    # The lock ends before the model gets the operator's new mission, never after it.
    assert order.index(("tool", "operator_release", {})) < order.index(("model", "[operator] take off again"))
