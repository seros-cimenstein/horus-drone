from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from horus.config import HorusConfig
from horus.executor import HorusExecutor
from horus.flight import Pose
from horus.flight.link import Telemetry
from horus.mcp_server.server import create_application, create_mcp
from horus.repl.app import _interactive_arguments

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "repl-test-token"


@pytest.mark.parametrize(
    ("line", "tool"),
    [("/hold", "hold"), ("/rth", "rth"), ("/LAND", "land"), ("hold", "hold"), ("rth", "rth"), ("land", "land")],
)
def test_documented_slash_and_bare_emergencies_route_to_emergency_tools(line, tool):
    assert _interactive_arguments(line) == (tool, {})


@pytest.mark.parametrize(
    "line",
    ["land next to the hiker", "hold on, first scan the trees", "rth after the orbit", "don't land yet", "/land now"],
)
def test_sentences_starting_with_an_emergency_word_go_to_the_commander(line):
    assert _interactive_arguments(line) is None


def _row(candidate_id, label, shirt=None, quoted=True):
    """A candidate-table row as list_candidates returns it over MCP (label quoted, untrusted)."""
    row = {"id": candidate_id, "bbox": [0, 0, 30, 60], "attributes": {} if shirt is None else {"shirt_colour": shirt}}
    if quoted:
        row["untrusted_detector_labels"] = [json.dumps(label)]
    else:
        row["class_name"] = label
    return row


def _selection_row(candidate_id, *, box, confidence):
    return {
        "id": candidate_id,
        "bbox": box,
        "attributes": {"shirt_colour": "red"},
        "confidence": confidence,
    }


def test_select_candidate_rejects_a_low_confidence_largest_match():
    from horus.repl.app import _select_candidate

    rows = [_selection_row(4, box=[0, 0, 90, 90], confidence=0.27)]

    with pytest.raises(RuntimeError, match="confidence"):
        _select_candidate(rows, attribute="shirt_colour", equals="red", choose="largest")


def test_select_candidate_skips_a_low_confidence_match_for_a_confident_smaller_one():
    from horus.repl.app import _select_candidate

    rows = [
        _selection_row(4, box=[0, 0, 90, 90], confidence=0.27),
        _selection_row(5, box=[0, 0, 30, 60], confidence=0.83),
    ]

    assert _select_candidate(rows, attribute="shirt_colour", equals="red", choose="largest")["id"] == 5


def test_select_candidate_mission_step_honours_the_confidence_floor(tmp_path):
    import asyncio
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from horus.repl.app import run_mission

    mission = tmp_path / "select-candidate.yaml"
    mission.write_text(
        "steps:\n"
        "  - select_candidate:\n"
        "      attribute: shirt_colour\n"
        "      equals: red\n"
        "      choose: largest\n"
        "      store_as: target\n"
        "      min_confidence: 0.9\n",
        encoding="utf-8",
    )
    table = [_selection_row(4, box=[0, 0, 90, 90], confidence=0.83)]

    async def tool_call(_session, name, arguments=None):
        assert name == "list_candidates"
        return {"candidate_table": table}

    with patch("horus.repl.app._tool_call", side_effect=tool_call), redirect_stdout(io.StringIO()):
        with pytest.raises(RuntimeError, match=r"0\.9"):
            asyncio.run(run_mission(session=object(), path=mission))


def _run_resolution_mission(tmp_path, tables, *, extra="", settle_s=1.0, timeout_s=3.0, age_s=0.1, rpc_s=0.0, repeat_frame=False):
    """Run resolve_candidate → follow against scripted tables (fake clock; 0.25 s poll period).

    Each list_candidates call returns the next table (the last one repeats) as a new frame of
    observation age ``age_s``; ``rpc_s`` is the simulated round-trip time of each call.
    """
    import asyncio
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from horus.repl.app import run_mission

    mission = tmp_path / "resolve.yaml"
    mission.write_text(
        "steps:\n"
        "  - resolve_candidate:\n"
        "      class_name: Person\n"
        f"{extra}"
        "      store_as: target\n"
        f"      timeout_s: {timeout_s}\n"
        f"      settle_s: {settle_s}\n"
        "  - tool: follow\n"
        "    arguments: {id: $target.id, ttl_s: 2.0}\n"
        "    wait: false\n",
        encoding="utf-8",
    )
    clock = [100.0]
    remaining = list(tables)
    calls = []

    async def tool_call(_session, name, arguments=None):
        calls.append((name, arguments))
        if name == "list_candidates":
            clock[0] += rpc_s
            frame_id = 1 if repeat_frame else sum(1 for called, _ in calls if called == "list_candidates")
            pose = {"time_boot_ms": 50_000 + 250 * frame_id, "time_boot_age_s": 0.1}
            table = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return {"frame_id": frame_id, "observation_age_s": age_s, "pose": pose, "candidate_table": table}
        return {"task_id": "follow-1"}

    async def sleep(seconds):
        clock[0] += seconds

    out = io.StringIO()
    with patch("horus.repl.app._tool_call", side_effect=tool_call), patch("horus.repl.app.asyncio.sleep", sleep), \
            patch("horus.repl.app.time.monotonic", lambda: clock[0]), redirect_stdout(out):
        asyncio.run(run_mission(session=object(), path=mission))
    line = next(line for line in out.getvalue().splitlines() if "resolve_candidate:" in line)
    return json.loads(line.split("resolve_candidate: ", 1)[1]), calls


def test_resolve_candidate_follows_the_single_public_cue_match(tmp_path):
    outcome, calls = _run_resolution_mission(
        tmp_path,
        [[], [_row(4, "person", "red"), _row(5, "car")], [_row(4, "person", "red"), _row(6, "person", "blue")]],
        extra="      attribute: shirt_colour\n      equals: red\n",
    )
    assert outcome["outcome"] == "resolved" and outcome["candidate"]["id"] == 4
    # The decision frame is recorded with its autopilot clock for offline scoring.
    observation = outcome["observation"]
    assert observation["frame_time_boot_ms"] == 50_000 + 250 * observation["frame_id"]
    assert observation["matches"] == [{"id": 4, "bbox": [0, 0, 30, 60]}]
    assert calls[-1] == ("follow", {"id": 4, "ttl_s": 2.0})


def test_resolve_candidate_abstains_when_two_targets_match_during_the_settle_window(tmp_path):
    # The second matching person is visible only briefly; picking the "largest" would hide that.
    outcome, calls = _run_resolution_mission(
        tmp_path,
        [[_row(4, "person", quoted=False)], [_row(4, "person"), _row(9, "person")], [_row(4, "person")]],
    )
    assert (outcome["outcome"], outcome["reason"], outcome["candidate_ids"]) == ("abstain", "ambiguous", [4, 9])
    assert outcome["observation"]["frame_id"] == 2  # the frame that showed both
    assert [m["id"] for m in outcome["observation"]["matches"]] == [4, 9]
    assert "follow" not in [name for name, _ in calls]


def test_resolve_candidate_abstains_without_a_match_before_the_timeout(tmp_path):
    outcome, calls = _run_resolution_mission(tmp_path, [[_row(5, "car")]])
    assert (outcome["outcome"], outcome["reason"]) == ("abstain", "no_match")
    assert [name for name, _ in calls] == ["list_candidates"] * 12  # 0, 0.25 … 2.75 s; none at 3.0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"age_s": 2.0}, "no_fresh_observation"),  # stalled camera: old frames never count
        ({"repeat_frame": True}, "unstable"),  # the same frame re-served is one sighting, not two
    ],
)
def test_resolve_candidate_needs_fresh_new_frames(tmp_path, kwargs, reason):
    outcome, calls = _run_resolution_mission(tmp_path, [[_row(4, "person")]], **kwargs)
    assert (outcome["outcome"], outcome["reason"]) == ("abstain", reason)
    assert "follow" not in [name for name, _ in calls]


def test_resolve_candidate_rejects_an_id_seen_only_in_the_decision_frame(tmp_path):
    # ID 4 is replaced by ID 7 on the last frame of the settle window: 7 is not yet stable.
    outcome, _ = _run_resolution_mission(tmp_path, [[_row(4, "person")]] * 3 + [[_row(7, "person")]])
    assert (outcome["outcome"], outcome["reason"], outcome["candidate_ids"]) == ("abstain", "unstable", [4, 7])


def test_resolve_candidate_ignores_a_first_match_returned_after_the_deadline(tmp_path):
    # Slow round trips: the call made before the deadline returns the first match after it.
    outcome, calls = _run_resolution_mission(tmp_path, [[], [], [_row(4, "person")]], timeout_s=1.0, rpc_s=0.45)
    assert (outcome["outcome"], outcome["reason"]) == ("abstain", "no_match")
    assert "follow" not in [name for name, _ in calls]


def test_resolve_candidate_bounds_a_hanging_list_candidates_call():
    import asyncio
    from unittest.mock import patch

    from horus.repl.app import _resolution_spec, _resolve_candidate

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    resolution = _resolution_spec({"class_name": "person", "store_as": "t", "timeout_s": 0.3, "settle_s": 0.2})
    started = time.monotonic()
    with patch("horus.repl.app._tool_call", side_effect=hang):
        outcome = asyncio.run(_resolve_candidate(object(), resolution))
    assert outcome["reason"] == "no_fresh_observation"
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "spec",
    [{"store_as": "t"}, {"class_name": "person", "store_as": "t", "attribute": "shirt_colour"},
     {"class_name": "person", "store_as": "t", "choose": "largest"}, {"class_name": "person", "store_as": "t", "timeout_s": 0},
     {"class_name": "person", "store_as": "t", "settle_s": 0}],
)
def test_resolve_candidate_rejects_malformed_specs(spec):
    from horus.repl.app import _resolution_spec

    with pytest.raises(ValueError):
        _resolution_spec(spec)


class _InertFlight:
    """No sockets: fresh GUIDED telemetry, and every command is recorded."""

    def __init__(self):
        self.commands: list[str] = []

    def telemetry(self):
        now = time.monotonic()
        return Telemetry(
            "2026-09-30T00:00:00+00:00",
            now,
            Pose(0.0, 0.0, 2.0),
            mode="GUIDED",
            armed=True,
            heartbeat_monotonic=now,
            heading_deg=0.0,
            pose_updated_monotonic=now,
            heading_updated_monotonic=now,
        )

    def heartbeat_ok(self, max_age_s: float = 3.0) -> bool:
        return True

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.commands.append(name)


class _RunningServer:
    def __init__(self, app):
        import uvicorn

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.socket.setblocking(False)
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", access_log=False)
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.server.started, "MCP test server did not start"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)
        self.socket.close()


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


def test_repl_slash_hold_interrupts_a_running_commander_turn_while_input_stays_live(tmp_path):
    flight = _InertFlight()
    executor = HorusExecutor(flight, HorusConfig().executor, autostart=False)
    active = executor.submit_skill("goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}})
    server = _RunningServer(create_application(create_mcp(HorusConfig(), executor, flight, None), token=TOKEN))
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
        assert _wait_for(lambda: "hold" in flight.commands)
        assert _wait_for(lambda: any(e["kind"] == "interrupt" for e in _claude_log(claude_log)))
        stdout, stderr = repl.communicate(timeout=30)  # EOF ends the REPL
    finally:
        if repl.poll() is None:
            repl.kill()
            repl.communicate()
        server.close()
        executor.close()

    assert repl.returncode == 0, stderr
    assert executor.get_task_status(active["task_id"])["status"] == "cancelled"
    assert '"state":"HOLDING"' in stdout.replace(" ", "")
    texts = [e["text"] for e in _claude_log(claude_log) if e["kind"] == "user"]
    assert "[operator] then follow the person in red" not in texts
    assert "[operator] /hold" not in texts
    assert texts[0] == "[operator] inspect the north wall"
