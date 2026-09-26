from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horus.repl.app import _select_candidate, _write_reacquisition_result, run_mission


class _TextCommander:
    instances = []

    def __init__(self, server_url, bearer_token, settings):
        self.server_url = server_url
        self.bearer_token = bearer_token
        self.settings = settings
        self.stopped = asyncio.Event()
        self.tasks = []
        self.instances.append(self)

    async def run(self):
        await self.stopped.wait()

    async def submit_task(self, text):
        self.tasks.append(text)
        return "Target remains uncertain."

    async def stop(self):
        self.stopped.set()


class TestCommanderMissions:
    def test_red_shirt_mission_uses_commander_and_finishes_its_session(self):
        _TextCommander.instances.clear()
        output = io.StringIO()
        mission = Path(__file__).parents[1] / "missions" / "S6_find_red_shirt.yaml"
        with patch("horus.repl.app.HorusCommander", _TextCommander):
            with redirect_stdout(output):
                asyncio.run(
                    run_mission(
                        session=object(),
                        path=mission,
                        server_url="http://horus/mcp",
                        bearer_token="token",
                        commander_settings=object(),
                    )
                )

        commander = _TextCommander.instances[0]
        assert len(commander.tasks) == 1
        assert "red shirt" in commander.tasks[0]
        assert commander.stopped.is_set()

    def test_s5_selects_blue_target_and_writes_reacquisition_result(self, tmp_path):
        class BackgroundDriver:
            returncode = None
            terminated = False

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            async def wait(self):
                return self.returncode

        candidates = [
            {"id": 5, "bbox": [0, 0, 100, 150], "attributes": {"shirt_colour": "blue"}},
            {"id": 8, "bbox": [0, 0, 70, 120], "attributes": {"shirt_colour": "blue"}},
            {"id": 9, "bbox": [0, 0, 300, 300], "attributes": {"shirt_colour": "red"}},
        ]
        terminal = {
            "task_id": "follow-1",
            "status": "expired",
            "tracking": {
                "reference_id": 5,
                "matched_id": 5,
                "reacquired": True,
                "time_to_reacquire_s": 2.02,
                "embedding_validated": True,
                "embedding_similarity": 0.94,
                "embedding_similarity_threshold": 0.72,
                "id_preserved": True,
                "id_rematched": False,
            },
        }
        calls = []

        async def tool_call(_session, name, arguments=None):
            calls.append((name, arguments))
            if name == "take_off":
                return {}
            if name == "list_candidates":
                return {"candidate_table": candidates}
            if name == "follow":
                return {"task_id": "follow-1"}
            if name == "get_task_status":
                return terminal
            raise AssertionError(f"unexpected tool {name}")

        output_dir = tmp_path / "outputs"
        mission = Path(__file__).parents[1] / "missions" / "S5_follow_occlusion.yaml"
        driver = BackgroundDriver()

        async def launch_driver(*_arguments):
            calls.append(("<driver>", None))
            return driver

        start_driver = AsyncMock(side_effect=launch_driver)
        call_tool = AsyncMock(side_effect=tool_call)
        sleep = AsyncMock()

        def write_result(**arguments):
            config = dict(arguments["config"])
            config["output_dir"] = str(output_dir)
            arguments["config"] = config
            return _write_reacquisition_result(**arguments)

        with patch("horus.repl.app.asyncio.create_subprocess_exec", start_driver):
            with patch("horus.repl.app._tool_call", call_tool):
                with patch("horus.repl.app.asyncio.sleep", sleep):
                    with patch(
                        "horus.repl.app._write_reacquisition_result",
                        side_effect=write_result,
                    ):
                        asyncio.run(run_mission(session=object(), path=mission))

        # The target's route starts after selection, right before follow.
        assert [name for name, _ in calls] == [
            "take_off",
            "list_candidates",
            "<driver>",
            "follow",
            "get_task_status",
        ]
        start_driver.assert_awaited_once_with(
            sys.executable,
            str((mission.parent / "../scripts/move_target.py").resolve()),
        )
        follow_arguments = next(arguments for name, arguments in calls if name == "follow")
        assert follow_arguments == {"id": 5, "ttl_s": 45.0}
        assert sleep.await_args.args[0] > 29.0
        assert driver.terminated
        result_files = list(output_dir.glob("S5_follow_occlusion-run-001-*.json"))
        assert len(result_files) == 1
        result = json.loads(result_files[0].read_text())
        assert result["selected_target"]["id"] == 5
        assert result["reacquisition"]["same_identity"]
        assert result["reacquisition"]["time_to_reacquire_s"] == 2.02
        assert result["reacquisition"]["id_preserved"]
        assert not result["reacquisition"]["id_rematched"]
        assert result["assertions"]["passed"]

    def test_select_candidate_rejects_unsupported_rule(self):
        with pytest.raises(ValueError, match="choose"):
            _select_candidate(
                [],
                attribute="shirt_colour",
                equals="blue",
                choose="smallest",
            )
