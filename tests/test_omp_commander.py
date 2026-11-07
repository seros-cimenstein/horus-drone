from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

from horus.commander.backends import OmpSession
from horus.config import CommanderSettings

# A stand-in camera picture: the bytes an MCP image block carries base64-encoded.
_PICTURE = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 16

_FAKE_OMP = r'''
import base64, json, os, sys
from pathlib import Path

mode = os.environ.get("HORUS_FAKE_MODE", "basic")
launches = Path(os.environ["HORUS_FAKE_LAUNCHES"])
count = int(launches.read_text()) + 1 if launches.exists() else 1
launches.write_text(str(count))
commands = Path(os.environ["HORUS_FAKE_COMMANDS"])

def emit(frame):
    print(json.dumps(frame), flush=True)

def emit_chunked(frame, chunk_size=65536):
    payload = json.dumps(frame).encode("utf-8")
    chunks = max(1, (len(payload) + chunk_size - 1) // chunk_size)
    for index in range(chunks):
        part = payload[index * chunk_size:(index + 1) * chunk_size]
        emit({
            "type": "rpc_chunk", "chunkId": "one", "index": index, "count": chunks,
            "byteLength": len(payload), "data": base64.b64encode(part).decode("ascii"),
        })

emit({
    "type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2],
    "maxFrameBytes": 1048576, "maxReassembledFrameBytes": 67108864,
})
generation = 0
active = None
aborted_once = False

def reply(prompt_id, text, tool=None, image=False, chunked=False):
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    if tool:
        emit({"type": "message_end", "message": {"role": "assistant", "content": [{
            "type": "toolCall", "id": "call-1", "name": tool,
            "arguments": {"i": "intent", "action": "hold"},
        }]}})
        emit({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": tool,
              "args": {"i": "intent", "action": "hold"}})
        content = [{"type": "text", "text": '{"altitude_m": 12}'}]
        if image:
            content.append({
                "type": "image", "data": os.environ["HORUS_FAKE_PICTURE"],
                "mimeType": "image/jpeg",
            })
        end = {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": tool,
               "result": {"content": content}, "isError": False}
        if chunked:
            emit_chunked(end)
        else:
            emit(end)
        emit({"type": "turn_start"})
    emit({"type": "message_end", "message": {"role": "assistant",
        "content": [{"type": "text", "text": text}], "stopReason": "stop"}})
    emit({"type": "agent_end", "messages": []})
    emit({"type": "prompt_result", "id": prompt_id, "agentInvoked": True,
          "status": "completed", "sessionSettled": True})

for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    with commands.open("a") as stream:
        stream.write(kind + "\n")
    if kind == "negotiate_protocol":
        emit({"id": message["id"], "type": "response", "command": "negotiate_protocol",
              "success": True})
    elif kind == "get_state":
        tools = [{"name": "mcp__horus_get_state"}, {"name": "mcp__horus_get_frame"}]
        if mode == "foreign_tools":
            tools.append({"name": "bash"})
        emit({"id": message["id"], "type": "response", "command": "get_state",
              "success": True, "data": {"dumpTools": tools}})
    elif kind == "new_session":
        generation += 1
        emit({"id": message["id"], "type": "response", "command": "new_session",
              "success": True, "data": {"cancelled": False}})
        active = None
    elif kind == "abort":
        if active is not None:
            emit({"type": "agent_end", "messages": []})
            emit({"type": "prompt_result", "id": active, "agentInvoked": True,
                  "status": "aborted", "sessionSettled": True})
            active = None
        aborted_once = True
    elif kind == "prompt":
        prompt_id = message["id"]
        text = message["message"]
        active = prompt_id
        emit({"id": prompt_id, "type": "response", "command": "prompt", "success": True})
        if os.environ.get("HORUS_FAKE_CRASH_FIRST") == "1" and count == 1:
            sys.exit(23)
        if mode == "silent":
            continue
        if mode == "abort" and not aborted_once:
            emit({"type": "agent_start"})
            emit({"type": "turn_start"})
            continue
        if mode == "turn_limit":
            for _ in range(5):
                emit({"type": "turn_start"})
            continue
        if mode == "generation":
            reply(prompt_id, f"generation {generation}: {text}")
        elif mode == "image":
            reply(prompt_id, "Handled: " + text, tool="mcp__horus_get_frame", image=True)
        elif mode == "other_image":
            reply(prompt_id, "Handled: " + text, tool="mcp__other_frame", image=True)
        elif mode == "chunked":
            reply(prompt_id, "Handled: " + text, tool="mcp__horus_get_frame", image=True,
                  chunked=True)
        elif mode == "tool":
            reply(prompt_id, "Handled: " + text, tool="mcp__horus_get_state")
        else:
            reply(prompt_id, "Handled: " + text)
'''


def _fake_omp(tmp_path: Path, monkeypatch, *, mode: str = "basic", crash_first: bool = False):
    executable = tmp_path / "fake-omp"
    executable.write_text(f"#!{sys.executable}\n{_FAKE_OMP}", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("HORUS_FAKE_LAUNCHES", str(tmp_path / "launches.txt"))
    monkeypatch.setenv("HORUS_FAKE_COMMANDS", str(tmp_path / "commands.txt"))
    monkeypatch.setenv("HORUS_FAKE_PICTURE", base64.b64encode(_PICTURE).decode("ascii"))
    monkeypatch.setenv("HORUS_FAKE_MODE", mode)
    if crash_first:
        monkeypatch.setenv("HORUS_FAKE_CRASH_FIRST", "1")
    return executable


def _session(executable: Path, **overrides) -> OmpSession:
    values = {
        "provider": "omp",
        "omp_command": str(executable),
        "omp_model": "opencode-go/deepseek-v4.1-flash",
        "omp_thinking": "max",
        "timeout_s": 5.0,
    }
    values.update(overrides)
    settings = CommanderSettings(**values)
    return OmpSession("http://127.0.0.1:8000/mcp", "secret-token", settings, "HORUS safety prompt")


async def _records(session: OmpSession) -> list[dict]:
    return [dict(record) async for record in session.stream_records()]


def _commands(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_omp_turn_streams_tool_records_and_final_result(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="tool")
    session = _session(executable)

    async def scenario():
        records = asyncio.create_task(_records(session))
        answer = await session.send("[operator] inspect the drone")
        await session.close()
        return answer, await records

    answer, records = asyncio.run(scenario())

    assert answer == "Handled: [operator] inspect the drone"
    kinds = [record["kind"] for record in records]
    assert "omp_started" in kinds and "assistant_text" in kinds and "final_result" in kinds
    tool_use = next(record for record in records if record["kind"] == "tool_use")
    # Records name the bare drone board tool, and omp's own "i" intent argument is harness
    # metadata, not a drone-board argument.
    assert tool_use == {
        "source": "omp", "kind": "tool_use", "tool": "get_state",
        "args": {"action": "hold"}, "tool_use_id": "call-1",
    }
    tool_result = next(record for record in records if record["kind"] == "tool_result")
    assert tool_result["tool_use_id"] == "call-1" and tool_result["is_error"] is False
    assert "altitude_m" in tool_result["summary"]
    assert records[-1]["kind"] == "omp_finished"



def test_omp_image_results_become_ai_views_only_for_horus_tools(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="image")
    session = _session(executable)

    async def scenario():
        records = asyncio.create_task(_records(session))
        await session.send("[operator] look")
        await session.close()
        return await records

    records = asyncio.run(scenario())

    views = [record for record in records if record["kind"] == "ai_view"]
    assert len(views) == 1
    view = views[0]
    assert {key: view[key] for key in ("source", "tool", "tool_use_id", "mime_type", "image")} == {
        "source": "omp", "tool": "get_frame", "tool_use_id": "call-1",
        "mime_type": "image/jpeg", "image": _PICTURE,
    }
    assert all("image" not in record for record in records if record["kind"] != "ai_view")


def test_omp_reassembles_protocol_v2_chunked_frames_with_an_image(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="chunked")
    session = _session(executable)

    async def scenario():
        records = asyncio.create_task(_records(session))
        answer = await session.send("[operator] look")
        await session.close()
        return answer, await records

    answer, records = asyncio.run(scenario())

    assert answer == "Handled: [operator] look"
    views = [record for record in records if record["kind"] == "ai_view"]
    assert views[0]["image"] == _PICTURE


def test_omp_does_not_count_another_servers_image_as_an_ai_view(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="other_image")
    session = _session(executable)

    async def scenario():
        records = asyncio.create_task(_records(session))
        await session.send("[operator] look")
        await session.close()
        return await records

    records = asyncio.run(scenario())

    assert [record for record in records if record["kind"] == "ai_view"] == []
    result = next(record for record in records if record["kind"] == "tool_result")
    assert result["summary"].endswith("[1 image(s), payload omitted]")


def test_omp_interrupt_aborts_the_turn_and_the_next_message_runs(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="abort")
    session = _session(executable)

    async def scenario():
        await session._ensure_started()
        active = asyncio.create_task(session.send("[operator] fly north"))
        await asyncio.sleep(0.2)
        await session.interrupt()
        with pytest.raises(RuntimeError, match="aborted"):
            await active
        answer = await session.send("[operator] status?")
        await session.close()
        return answer

    assert asyncio.run(scenario()) == "Handled: [operator] status?"


def test_omp_reset_starts_a_new_session(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="generation")
    session = _session(executable)

    async def scenario():
        first = await session.send("[operator] one")
        await session.reset()
        second = await session.send("[operator] two")
        await session.close()
        return first, second

    first, second = asyncio.run(scenario())

    assert first == "generation 0: [operator] one"
    assert second == "generation 1: [operator] two"
    assert "new_session" in _commands(tmp_path / "commands.txt")


def test_omp_timeout_bounds_silence_and_resets(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="silent")
    session = _session(executable, timeout_s=0.6)

    async def scenario():
        records = asyncio.create_task(_records(session))
        with pytest.raises(TimeoutError, match="wrote nothing"):
            await session.send("[operator] fly to P1")
        await session.close()
        return await records

    records = asyncio.run(scenario())

    assert any(record["kind"] == "omp_timeout" for record in records)
    assert "abort" in _commands(tmp_path / "commands.txt")
    assert "new_session" in _commands(tmp_path / "commands.txt")


def test_omp_restarts_after_process_death(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, crash_first=True)
    session = _session(executable)

    async def scenario():
        records = asyncio.create_task(_records(session))
        with pytest.raises(RuntimeError):
            await session.send("[operator] first")
        answer = await session.send("[operator] next")
        await session.close()
        return answer, await records

    answer, records = asyncio.run(scenario())

    assert answer == "Handled: [operator] next"
    assert (tmp_path / "launches.txt").read_text() == "2"
    assert any(record["kind"] == "session_restarted" for record in records)


def test_omp_max_turns_aborts_the_turn(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="turn_limit")
    session = _session(executable, max_turns=2)

    async def scenario():
        with pytest.raises(RuntimeError, match="max_turns"):
            await session.send("[operator] wander")
        await asyncio.sleep(0.2)
        await session.close()

    asyncio.run(scenario())

    assert "abort" in _commands(tmp_path / "commands.txt")


def test_omp_refuses_a_tool_surface_with_a_non_horus_tool(tmp_path, monkeypatch):
    executable = _fake_omp(tmp_path, monkeypatch, mode="foreign_tools")
    session = _session(executable)

    async def scenario():
        with pytest.raises(RuntimeError, match="only the HORUS drone-board tools"):
            await session.send("[operator] inspect")
        await session.close()

    asyncio.run(scenario())
