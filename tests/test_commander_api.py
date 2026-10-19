"""Anthropic/OpenAI tools backends driven by fake provider clients (no network)."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from horus.commander.backends import MODEL_DISALLOWED_TOOLS, APICommanderBackend
from horus.config import CommanderSettings


_MCP_TOOLS = [
    SimpleNamespace(name=name, description=f"{name} tool", inputSchema={"type": "object"})
    for name in ("get_state", "wait_events", "hold")
]


class _OpenAIMessage:
    """Shape of ``openai.types.chat.ChatCompletionMessage`` the backend relies on."""

    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=False):
        payload = {"role": "assistant", "content": self.content, "tool_calls": None}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ]
        return {key: value for key, value in payload.items() if value is not None or not exclude_none}


def _install_fake_provider(monkeypatch, provider, script, requests, client_kwargs):
    """Install a fake SDK module whose model answers ``script(round)``.

    ``script`` returns ``("tool", name, arguments)`` or ``("text", text)``.
    """

    def reply(index):
        step = script(index)
        if provider == "openai":
            if step[0] == "tool":
                call = SimpleNamespace(
                    id=f"call-{index}",
                    function=SimpleNamespace(name=step[1], arguments=json.dumps(step[2])),
                )
                message = _OpenAIMessage(tool_calls=[call])
            else:
                message = _OpenAIMessage(content=step[1])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if step[0] == "tool":
            return SimpleNamespace(content=[
                SimpleNamespace(type="tool_use", id=f"call-{index}", name=step[1], input=step[2])
            ])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=step[1])])

    class Endpoint:
        async def create(self, **arguments):
            requests.append({**arguments, "messages": [dict(message) for message in arguments["messages"]]})
            return reply(len(requests) - 1)

    def make_client(**kwargs):
        client_kwargs.append(kwargs)
        if provider == "openai":
            return SimpleNamespace(chat=SimpleNamespace(completions=Endpoint()))
        return SimpleNamespace(messages=Endpoint())

    module = ModuleType(provider)
    if provider == "openai":
        module.AsyncOpenAI = make_client
    else:
        module.AsyncAnthropic = make_client
    monkeypatch.setitem(sys.modules, provider, module)


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text='{"ok": true}')])


def _backend(provider, session, **settings):
    return APICommanderBackend(
        provider,
        CommanderSettings(provider=provider, base_url="http://fake.invalid/v1", **settings),
        "api-key",
        session=session,
        mcp_tools=list(_MCP_TOOLS),
        system_prompt="HORUS safety prompt",
    )


async def _drain(backend):
    await backend.close()
    return [record async for record in backend.stream_records()]


def test_openai_backend_completes_a_tool_call_round_trip(monkeypatch):
    requests, client_kwargs = [], []
    _install_fake_provider(
        monkeypatch,
        "openai",
        lambda index: ("tool", "get_state", {"verbose": True}) if index == 0 else ("text", "drone is idle"),
        requests,
        client_kwargs,
    )
    session = _Session()
    backend = _backend("openai", session)

    async def scenario():
        result = await backend.send("[operator] report state")
        return result, await _drain(backend)

    result, records = asyncio.run(scenario())

    assert result == "drone is idle"
    assert client_kwargs == [{"api_key": "api-key", "base_url": "http://fake.invalid/v1"}]
    assert session.calls == [("get_state", {"verbose": True})]
    assert len(requests) == 2
    system, user, assistant, tool = requests[1]["messages"]
    assert system == {"role": "system", "content": "HORUS safety prompt"}
    assert user == {"role": "user", "content": "[operator] report state"}
    assert assistant["tool_calls"][0]["id"] == "call-0"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_state"
    assert tool == {"role": "tool", "tool_call_id": "call-0", "content": '{"ok": true}'}
    assert [record["kind"] for record in records] == [
        "tool_use",
        "tool_result",
        "assistant_text",
        "final_result",
    ]
    assert all(record["source"] == "openai" for record in records)
    assert records[0]["args"] == {"verbose": True}
    assert records[-1]["text"] == "drone is idle"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_api_backend_offers_only_the_tools_claude_code_allows(monkeypatch, provider):
    requests = []
    _install_fake_provider(monkeypatch, provider, lambda _index: ("text", "ok"), requests, [])
    backend = _backend(provider, _Session())

    assert asyncio.run(backend.send("[operator] hello")) == "ok"

    tools = requests[0]["tools"]
    names = [tool["function"]["name"] if provider == "openai" else tool["name"] for tool in tools]
    assert "wait_events" in MODEL_DISALLOWED_TOOLS
    assert names == ["get_state", "hold"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_api_backend_stops_after_max_turns_tool_call_rounds(monkeypatch, provider):
    requests = []
    _install_fake_provider(
        monkeypatch, provider, lambda _index: ("tool", "get_state", {}), requests, []
    )
    session = _Session()
    backend = _backend(provider, session, max_turns=3)

    with pytest.raises(RuntimeError, match=r"max_turns \(3\)"):
        asyncio.run(backend.send("[operator] loop forever"))

    assert len(requests) == 3
    assert len(session.calls) == 3
    assert backend._history == []  # the failed turn is rolled back entirely
