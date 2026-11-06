from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from horus.config import HorusConfig
from horus.executor import HorusExecutor
from horus.flight import Pose
from horus.mcp_server.server import create_application, create_mcp
from horus.repl.app import _interactive_arguments

from tests.fakes import FakeFlight

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "repl-test-token"


@pytest.mark.parametrize(
    ("line", "tool"),
    [
        ("/hold", "hold"), ("/rth", "rth"), ("/LAND", "land"), ("hold", "hold"), ("rth", "rth"),
        ("land", "land"), ("stop now", "hold"), ("STOP!", "hold"), ("Return home", "rth"),
        ("/home", "rth"), ("/HOME", "rth"), ("home", "rth"), ("go home!", "rth"), ("Go home now", "rth"),
    ],
)
def test_slash_and_short_imperative_emergencies_route_to_emergency_tools(line, tool):
    assert _interactive_arguments(line) == (tool, {})


@pytest.mark.parametrize(
    "line",
    ["land next to the hiker", "hold on, first scan the trees", "rth after the orbit", "don't land yet", "/land now"],
)
def test_sentences_starting_with_an_emergency_word_go_to_the_commander(line):
    assert _interactive_arguments(line) is None


@pytest.mark.parametrize("line", ["/follow 3", "/approach 2 1.5", "/orbit 4 2"])
def test_removed_target_skill_commands_are_not_mcp_calls(line):
    # approach/follow/orbit are gone; like any unknown slash line they are not turned into a tool call.
    assert _interactive_arguments(line) is None


# A Claude Code stand-in: operator turns never finish on their own (like a long model turn);
# only an interrupt control request ends them. Emergency notices are acknowledged at once.
_SLOW_CLAUDE = """\
import json, os, sys
log = open(os.environ["HORUS_FAKE_CLAUDE_LOG"], "a", buffering=1)
print(json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "horus"}]}), flush=True)
turn_open = False
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        log.write(json.dumps({"kind": "interrupt"}) + "\\n")
        print(json.dumps({"type": "control_response", "response": {"subtype": "success"}}), flush=True)
        if turn_open:
            turn_open = False
            print(json.dumps({"type": "result", "subtype": "error_during_execution", "result": ""}), flush=True)
        continue
    text = message["message"]["content"][0]["text"]
    log.write(json.dumps({"kind": "user", "text": text}) + "\\n")
    if text.startswith("[operator emergency]"):
        print(json.dumps({"type": "result", "subtype": "success", "result": "acknowledged"}), flush=True)
    else:
        turn_open = True
"""


def _wait_for(predicate, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _claude_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_repl_slash_hold_interrupts_a_running_commander_turn_while_input_stays_live(tmp_path, serve):
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    executor = HorusExecutor(flight, HorusConfig().executor, autostart=False)
    active = executor.submit_skill("goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}})
    server = serve(create_application(create_mcp(HorusConfig(), executor, flight, None), token=TOKEN))
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(f"#!{sys.executable}\n{_SLOW_CLAUDE}", encoding="utf-8")
    fake_claude.chmod(0o755)
    claude_log = tmp_path / "claude.jsonl"
    config = tmp_path / "horus.yaml"
    config.write_text(
        f"commander:\n  claude_command: {json.dumps(str(fake_claude))}\n  timeout_s: 120\n", encoding="utf-8"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HORUS_MCP_TOKEN": TOKEN,
        "HORUS_FAKE_CLAUDE_LOG": str(claude_log),
        "HORUS_EVENT_LOG": str(tmp_path / "events.jsonl"),
    }
    repl = subprocess.Popen(
        [sys.executable, "-m", "horus.repl.app", "--server-url", server.url, "--config", str(config)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        repl.stdin.write("inspect the north wall\n")
        repl.stdin.flush()
        assert _wait_for(lambda: any(e.get("text") == "[operator] inspect the north wall" for e in _claude_log(claude_log)))
        # Typed while the model turn is still running: one request that must never reach the model,
        # then the documented emergency command.
        repl.stdin.write("then follow the person in red\n/hold\n")
        repl.stdin.flush()
        assert _wait_for(lambda: ("hold",) in flight.commands)
        assert _wait_for(lambda: any(e["kind"] == "interrupt" for e in _claude_log(claude_log)))
        stdout, stderr = repl.communicate(timeout=30)  # EOF ends the REPL
    finally:
        if repl.poll() is None:
            repl.kill()
            repl.communicate()
        executor.close()

    assert repl.returncode == 0, stderr
    assert executor.get_task_status(active["task_id"])["status"] == "cancelled"
    assert '"state":"HOLDING"' in stdout.replace(" ", "")
    texts = [e["text"] for e in _claude_log(claude_log) if e["kind"] == "user"]
    assert "[operator] then follow the person in red" not in texts
    assert "[operator] /hold" not in texts
    assert texts[0] == "[operator] inspect the north wall"
    # The emergency is logged as the operator's, through the commander path too.
    operator = [e for e in _claude_log(tmp_path / "events.jsonl") if e.get("source") == "operator"]
    assert [(e["kind"], e["action"], e["status"]) for e in operator] == [("emergency", "hold", "executed")]


def test_repl_home_without_a_commander_is_logged_as_an_operator_emergency_and_hand_flight_ends_it(
    tmp_path, serve
):
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    executor = HorusExecutor(flight, HorusConfig().executor, autostart=False)
    server = serve(create_application(create_mcp(HorusConfig(), executor, flight, None), token=TOKEN))
    event_log = tmp_path / "events.jsonl"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HORUS_MCP_TOKEN": TOKEN,
        "HORUS_EVENT_LOG": str(event_log),
    }
    try:
        repl = subprocess.run(
            [sys.executable, "-m", "horus.repl.app", "--server-url", server.url,
             "--config", str(ROOT / "config" / "horus.yaml")],
            cwd=tmp_path,
            env=env,
            input="/home\n/take_off 3\n/quit\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        executor.close()

    assert repl.returncode == 0, repl.stderr
    assert ("rth",) in flight.commands
    # The operator flying by hand takes the drone back from its own emergency.
    assert '"skill": "take_off"' in repl.stdout
    assert executor.get_state()["operator_lock"] is None
    # No model was started: the operator's emergency is the only record.
    records = _claude_log(event_log)
    assert len(records) == 1, records
    record = records[0]
    assert (record["kind"], record["action"], record["source"], record["status"]) == (
        "emergency", "rth", "operator", "executed",
    )
    assert record["result"]["action"] == "rth"
