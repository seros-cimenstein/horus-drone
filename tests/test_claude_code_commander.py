from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from horus.commander.agent import CommanderEvent, HorusCommander
from horus.commander.backends import APICommanderBackend, ClaudeCodeSession
from horus.config import CommanderSettings, ServerSettings


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
    assert "mcp__horus__wait_events" in disallowed
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


def test_claude_stream_records_keep_tool_and_final_result_details():
    from horus.commander.backends import _claude_stream_records

    records = _claude_stream_records({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "name": "mcp__horus__get_state", "input": {"id": 3}},
            ]
        },
    })
    assert [record["kind"] for record in records] == ["assistant_text", "tool_use"]
    result = _claude_stream_records({"type": "result", "subtype": "success", "result": "Done"})
    assert result == [{"kind": "final_result", "text": "Done", "subtype": "success"}]


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
        second = await backend.send("[HORUS event #7 12:00Z] target_lost: target 3")
        await backend.close()
        return first, second

    assert asyncio.run(scenario()) == ("reply-1", "reply-2")
    assert requests[0] == [{"role": "user", "content": "[operator] inspect target"}]
    assert [message["role"] for message in requests[1]] == ["user", "assistant", "user"]
    assert requests[1][0]["content"] == "[operator] inspect target"
    assert requests[1][1]["content"][0].text == "reply-1"
    assert requests[1][2]["content"] == "[HORUS event #7 12:00Z] target_lost: target 3"


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


def test_anthropic_backend_retains_images_returned_by_mcp_tools(monkeypatch):
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
                    SimpleNamespace(type="image", mimeType="image/jpeg", data="frame-data"),
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
        return result

    assert asyncio.run(scenario()) == "The frame is clear."
    tool_result = requests[1][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "frame-data",
    }


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
        active = asyncio.create_task(backend.send("[operator] follow target 3"))
        waiting = asyncio.create_task(backend.send("[HORUS event #8] target_reacquired"))
        while not requests:
            await asyncio.sleep(0)
        await backend.interrupt()
        return await asyncio.wait_for(asyncio.gather(active, waiting, return_exceptions=True), 2.0)

    results = asyncio.run(scenario())

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    # The turn queued behind the interrupted one never reaches the model.
    assert requests == ["[operator] follow target 3"]


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

    assert session.calls == ["hold"]
    assert '"state":"HOLDING"' in result
    assert isinstance(before, RuntimeError) and "interrupted by an emergency" in str(before)
    assert after == "done"
    assert backend.sent == ["[operator] inspect the tree"]
