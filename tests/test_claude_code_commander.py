from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from horus.commander.agent import CommanderEvent, HorusCommander
from horus.config import CommanderSettings, ServerSettings


def _write_fake_claude(tmp_path: Path, monkeypatch, transcript: list[dict]) -> tuple[Path, Path, Path]:
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(
        "".join(json.dumps(event) + "\n" for event in transcript), encoding="utf-8"
    )
    args_path = tmp_path / "claude-args.json"
    config_path = tmp_path / "claude-mcp-config.json"
    executable = tmp_path / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "Path(os.environ['HORUS_CLAUDE_ARGS']).write_text(json.dumps(args), encoding='utf-8')\n"
        "config = args[args.index('--mcp-config') + 1]\n"
        "Path(os.environ['HORUS_CLAUDE_CONFIG']).write_text(Path(config).read_text(), encoding='utf-8')\n"
        "for line in Path(os.environ['HORUS_CLAUDE_TRANSCRIPT']).read_text().splitlines():\n"
        "    print(line, flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HORUS_CLAUDE_ARGS", str(args_path))
    monkeypatch.setenv("HORUS_CLAUDE_CONFIG", str(config_path))
    monkeypatch.setenv("HORUS_CLAUDE_TRANSCRIPT", str(transcript_path))
    return executable, args_path, config_path


def test_claude_code_backend_uses_only_horus_mcp_and_logs_stream_events(
    tmp_path, monkeypatch, capsys
):
    transcript = [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "horus"}]},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "I will inspect a fresh frame."},
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will inspect a fresh frame."},
                    {
                        "type": "tool_use",
                        "id": "use-1",
                        "name": "mcp__horus__get_frame",
                        "input": {"frame_id": 12},
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
                        "content": [
                            {"type": "text", "text": "frame 12 candidate 5"},
                            {"type": "image", "data": "base64-frame-payload"},
                        ],
                    }
                ]
            },
        },
        {
            "type": "result",
            "result": "Candidate 5 is uncertain; ask the operator.",
            "total_cost_usd": 0.42,
            "usage": {"input_tokens": 123, "output_tokens": 45},
            "num_turns": 2,
        },
    ]
    _, args_path, config_path = _write_fake_claude(tmp_path, monkeypatch, transcript)
    event_log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("HORUS_EVENT_LOG", str(event_log_path))
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        "unit-test-token",
        CommanderSettings(provider="claude-code", max_turns=3, timeout_s=10),
    )

    response = asyncio.run(commander._complete_claude_code("Find the red shirt", "HORUS safety prompt"))

    assert response == "Candidate 5 is uncertain; ask the operator."
    output = capsys.readouterr().out
    assert "I will inspect a fresh frame." in output
    assert "[claude tool] mcp__horus__get_frame" in output
    assert "[claude result] frame 12 candidate 5" in output
    assert "[claude final] Candidate 5 is uncertain; ask the operator." in output
    args = json.loads(args_path.read_text(encoding="utf-8"))
    assert args[args.index("-p") + 1] == "Find the red shirt"
    assert "--strict-mcp-config" in args
    assert args[args.index("--mcp-config") + 1]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--allowedTools") + 1] == "mcp__horus__*"
    disallowed_start = args.index("--disallowedTools") + 1
    append_prompt = args.index("--append-system-prompt")
    assert set(args[disallowed_start:append_prompt]) == {"Bash", "Edit", "Write", "Read", "WebFetch"}
    assert args[append_prompt + 1] == "HORUS safety prompt"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args
    assert "--include-partial-messages" in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--max-turns") + 1] == "3"

    mcp_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert list(mcp_config["mcpServers"]) == ["horus"]
    assert mcp_config["mcpServers"]["horus"] == {
        "type": "http",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": {"Authorization": "Bearer unit-test-token"},
    }

    events = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines()]
    kinds = [event["kind"] for event in events]
    assert "assistant_delta" in kinds
    assert "assistant_text" in kinds
    tool_use = next(event for event in events if event["kind"] == "tool_use")
    assert tool_use["tool"] == "mcp__horus__get_frame"
    assert tool_use["args"] == {"frame_id": 12}
    tool_result = next(event for event in events if event["kind"] == "tool_result")
    assert "frame 12 candidate 5" in tool_result["summary"]
    assert "base64-frame-payload" not in tool_result["summary"]
    final_result = next(event for event in events if event["kind"] == "final_result")
    assert final_result["text"] == "Candidate 5 is uncertain; ask the operator."
    assert final_result["usage"] == {"input_tokens": 123, "output_tokens": 45}
    assert final_result["total_cost_usd"] == 0.42
    assert final_result["num_turns"] == 2
    assert events[-1]["kind"] == "claude_code_finished"
    assert "unit-test-token" not in event_log_path.read_text(encoding="utf-8")


def test_optional_api_prompt_uses_imagecontent_and_candidate_table():
    class Session:
        async def call_tool(self, name, arguments):
            assert name == "get_frame"
            assert arguments == {}
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps({
                            "frame_id": 12,
                            "timestamp": "now",
                            "observation_age_s": 0.1,
                            "pose": {"north_m": 1},
                            "candidate_table": [
                                {"id": 7, "attributes": {"shirt_colour": "red"}}
                            ],
                        }),
                    ),
                    SimpleNamespace(type="image", mimeType="image/jpeg", data="jpeg-base64"),
                ],
                isError=False,
            )

    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        "unit-test-token",
        CommanderSettings(provider="anthropic"),
        api_key="unit-test-api-key",
    )
    prompt, image = asyncio.run(
        commander._event_prompt(Session(), CommanderEvent(kind="observation"))
    )
    metadata = json.loads(
        prompt.split("Observation metadata: ", 1)[1].split("\nEvery skill", 1)[0]
    )
    assert image == "jpeg-base64"
    assert metadata["candidates"] == [
        {"id": 7, "attributes": {"shirt_colour": "red"}}
    ]


def test_claude_code_backend_rejects_additional_mcp_servers(tmp_path, monkeypatch):
    transcript = [{
        "type": "system",
        "subtype": "init",
        "mcp_servers": [{"name": "horus"}, {"name": "other"}],
    }]
    _write_fake_claude(tmp_path, monkeypatch, transcript)
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        "unit-test-token",
        CommanderSettings(provider="claude-code", timeout_s=10),
    )
    try:
        asyncio.run(commander._complete_claude_code("Test isolation", "HORUS safety prompt"))
    except RuntimeError as exc:
        assert "only the HORUS MCP server" in str(exc)
    else:
        raise AssertionError("Claude Code must not load MCP servers beyond HORUS")


def test_claude_code_backend_enforces_configured_timeout(tmp_path, monkeypatch):
    executable, _, _ = _write_fake_claude(tmp_path, monkeypatch, [])
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        "unit-test-token",
        CommanderSettings(provider="claude-code", timeout_s=0.05),
    )
    try:
        asyncio.run(commander._complete_claude_code("Test timeout", "HORUS safety prompt"))
    except TimeoutError as exc:
        assert "timeout_s=0.05" in str(exc)
    else:
        raise AssertionError("Claude Code exceeding its configured timeout must be terminated")


def test_default_commander_backend_needs_no_provider_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HORUS_EVENT_LOG", raising=False)
    monkeypatch.delenv("HORUS_RUN_DIR", raising=False)
    assert CommanderSettings().provider == "claude-code"
    HorusCommander("http://127.0.0.1:8000/mcp", "token", CommanderSettings())


def test_mcp_server_defaults_to_loopback_only():
    assert ServerSettings().host == "127.0.0.1"
    for host in ("0.0.0.0", "192.168.1.2"):
        try:
            ServerSettings(host=host)
        except ValueError as exc:
            assert "loopback" in str(exc)
        else:
            raise AssertionError(f"non-loopback listener {host} must be rejected")


def test_operator_task_runs_claude_with_the_servers_commander_prompt(tmp_path, monkeypatch):
    frame = {"type": "image", "data": "A" * 400_000, "mimeType": "image/jpeg"}  # one very long line
    transcript = [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "horus", "status": "connected"}]},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": [frame]}]}},
        {"type": "result", "result": "Found the red shirt as ID 2."},
    ]
    _, args_path, _ = _write_fake_claude(tmp_path, monkeypatch, transcript)

    class Session:
        async def get_prompt(self, name):
            assert name == "horus_commander"
            return SimpleNamespace(
                messages=[SimpleNamespace(content=SimpleNamespace(type="text", text="You are the HORUS commander."))]
            )

    commander = HorusCommander(
        "http://127.0.0.1:8000/mcp",
        "token",
        CommanderSettings(provider="claude-code", timeout_s=30.0, claude_model="opus", claude_effort="high"),
    )
    event = CommanderEvent("task", text="Find the person in the red shirt.")
    result = asyncio.run(commander._handle_event(Session(), event, None))

    assert result == "Found the red shirt as ID 2."
    args = json.loads(args_path.read_text())
    assert args[args.index("-p") + 1] == "Find the person in the red shirt."
    assert args[args.index("--append-system-prompt") + 1] == "You are the HORUS commander."
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "high"
