from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horus.repl.missions import _select_candidate, _write_reacquisition_result, run_mission


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
        await asyncio.wait_for(self.stopped.wait(), timeout=5)

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
        with patch("horus.repl.missions.HorusCommander", _TextCommander):
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
            if name == "wait_for_task":
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

        with patch("horus.repl.missions.asyncio.create_subprocess_exec", start_driver):
            with patch("horus.repl.missions.call_json", call_tool):
                with patch("horus.repl.missions.asyncio.sleep", sleep):
                    with patch(
                        "horus.repl.missions._write_reacquisition_result",
                        side_effect=write_result,
                    ):
                        asyncio.run(run_mission(session=object(), path=mission))

        # The target's route starts after selection, right before follow.
        assert [name for name, _ in calls] == [
            "take_off",
            "list_candidates",
            "<driver>",
            "follow",
            "wait_for_task",
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

    def test_select_candidate_polls_until_a_later_candidate_matches(self, tmp_path):
        mission = tmp_path / "select-candidate.yaml"
        mission.write_text(
            "steps:\n"
            "  - select_candidate:\n"
            "      attribute: shirt_colour\n"
            "      equals: green\n"
            "      choose: largest\n"
            "      store_as: target\n"
            "      timeout_s: 2.0\n"
            "  - tool: follow\n"
            "    arguments:\n"
            "      id: $target.id\n"
            "      ttl_s: 2.0\n"
            "    wait: false\n",
            encoding="utf-8",
        )
        responses = iter(
            [
                {"candidate_table": []},
                {
                    "candidate_table": [
                        {
                            "id": 42,
                            "class_name": "person",
                            "bbox": [0, 0, 30, 50],
                            "attributes": {"shirt_colour": "green"},
                        }
                    ]
                },
            ]
        )
        calls = []

        async def tool_call(_session, name, arguments=None):
            calls.append((name, arguments))
            if name == "list_candidates":
                return next(responses)
            if name == "follow":
                return {"task_id": "follow-1"}
            raise AssertionError(f"unexpected tool {name}")

        call_tool = AsyncMock(side_effect=tool_call)
        sleep = AsyncMock()
        with patch("horus.repl.missions.call_json", call_tool):
            with patch("horus.repl.missions.asyncio.sleep", sleep):
                with redirect_stdout(io.StringIO()):
                    asyncio.run(run_mission(session=object(), path=mission))

        assert [name for name, _ in calls] == [
            "list_candidates",
            "list_candidates",
            "follow",
        ]
        assert calls[-1][1] == {"id": 42, "ttl_s": 2.0}
        sleep.assert_awaited_once_with(0.5)

    def test_select_candidate_timeout_reports_last_candidate_table(self, tmp_path):
        mission = tmp_path / "select-candidate-timeout.yaml"
        mission.write_text(
            "steps:\n"
            "  - select_candidate:\n"
            "      attribute: shirt_colour\n"
            "      equals: green\n"
            "      choose: largest\n"
            "      store_as: target\n"
            "      timeout_s: 0.01\n",
            encoding="utf-8",
        )
        calls = []

        async def tool_call(_session, name, arguments=None):
            calls.append(name)
            if name == "list_candidates":
                return {
                    "candidate_table": [
                        {
                            "id": 9,
                            "class_name": "person",
                            "bbox": [0, 0, 20, 40],
                            "attributes": {"shirt_colour": "red"},
                        }
                    ]
                }
            raise AssertionError(f"unexpected tool {name}")

        call_tool = AsyncMock(side_effect=tool_call)
        with patch("horus.repl.missions.call_json", call_tool):
            with redirect_stdout(io.StringIO()):
                with pytest.raises(TimeoutError) as exc_info:
                    asyncio.run(run_mission(session=object(), path=mission))

        message = str(exc_info.value)
        assert "after 0.01s" in message
        assert "last candidate table summary" in message
        assert '"id": 9' in message
        assert '"shirt_colour": "red"' in message
        assert calls and set(calls) == {"list_candidates"}

    def test_select_candidate_without_timeout_queries_once(self, tmp_path):
        mission = tmp_path / "select-candidate-once.yaml"
        mission.write_text(
            "steps:\n"
            "  - select_candidate:\n"
            "      attribute: shirt_colour\n"
            "      equals: green\n"
            "      choose: largest\n"
            "      store_as: target\n",
            encoding="utf-8",
        )
        call_tool = AsyncMock(return_value={"candidate_table": []})
        sleep = AsyncMock()
        with patch("horus.repl.missions.call_json", call_tool):
            with patch("horus.repl.missions.asyncio.sleep", sleep):
                with redirect_stdout(io.StringIO()):
                    with pytest.raises(RuntimeError, match="no candidate matched"):
                        asyncio.run(run_mission(session=object(), path=mission))

        assert call_tool.await_count == 1
        call_tool.assert_awaited_once()
        sleep.assert_not_awaited()

    @pytest.mark.parametrize(
        "timeout_literal", ["0", "-1", ".nan", ".inf", "true", "null", "[]"]
    )
    def test_select_candidate_rejects_invalid_timeout(self, tmp_path, timeout_literal):
        mission = tmp_path / "select-candidate-invalid-timeout.yaml"
        mission.write_text(
            "steps:\n"
            "  - select_candidate:\n"
            "      attribute: shirt_colour\n"
            "      equals: green\n"
            "      choose: largest\n"
            "      store_as: target\n"
            f"      timeout_s: {timeout_literal}\n",
            encoding="utf-8",
        )
        call_tool = AsyncMock(return_value={"candidate_table": []})
        with patch("horus.repl.missions.call_json", call_tool):
            with redirect_stdout(io.StringIO()):
                with pytest.raises(
                    ValueError, match="timeout_s must be a finite positive number"
                ):
                    asyncio.run(run_mission(session=object(), path=mission))

        call_tool.assert_not_awaited()


class TestMissionDriverArgs:
    def test_driver_args_are_appended_to_the_literal_driver_command(self, tmp_path):
        mission = tmp_path / "outdoor.yaml"
        driver = tmp_path / "civilian_life.py"
        driver.touch()
        arguments = [
            "--scenario",
            "sim/scenarios/horus_urban.yaml",
            "--group",
            "follow_jogger",
        ]
        mission.write_text(
            "driver: civilian_life.py\n"
            "driver_args:\n"
            "  - --scenario\n"
            "  - sim/scenarios/horus_urban.yaml\n"
            "  - --group\n"
            "  - follow_jogger\n"
            "steps:\n"
            "  - start_driver: {}\n",
            encoding="utf-8",
        )

        class BackgroundDriver:
            returncode = None

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return self.returncode

        process = BackgroundDriver()
        launch = AsyncMock(return_value=process)
        with patch("horus.repl.missions.asyncio.create_subprocess_exec", launch):
            asyncio.run(run_mission(session=object(), path=mission))

        launch.assert_awaited_once_with(
            sys.executable, str(driver.resolve()), *arguments
        )
        assert process.returncode == -15

    def test_driver_args_requires_a_driver(self, tmp_path):
        mission = tmp_path / "missing_driver.yaml"
        mission.write_text(
            "driver_args: [--group, follow_jogger]\nsteps: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="driver_args requires a driver"):
            asyncio.run(run_mission(session=object(), path=mission))

    @pytest.mark.parametrize(
        ("driver_args", "error"),
        [
            ("--group follow_jogger", "list of strings"),
            ("[--group, 7]", "items must be strings"),
        ],
    )
    def test_driver_args_rejects_non_list_or_non_string_items(self, tmp_path, driver_args, error):
        mission = tmp_path / "invalid_driver_args.yaml"
        (tmp_path / "driver.py").touch()
        mission.write_text(
            f"driver: driver.py\ndriver_args: {driver_args}\nsteps: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=error):
            asyncio.run(run_mission(session=object(), path=mission))
