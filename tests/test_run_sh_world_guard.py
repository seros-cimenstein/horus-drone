"""`sim/run.sh start` runs one HORUS world per host: Gazebo or GTA V, never both.

The script runs against stub ``docker`` and ``pgrep`` executables. The docker stub exits 97 at
``docker run``, so a start the guard lets through stops there without touching a real container.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER_RUN_REACHED = 97

_DOCKER = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "${STUB_LOG}"
case "$1 ${2:-}" in
  "image inspect") exit 0 ;;
  "container inspect") exit 1 ;;
esac
case "$1" in
  ps) printf '%b' "${STUB_DOCKER_PS:-}"; exit 0 ;;
  run) exit 97 ;;
  inspect)
    # The running container: its State.Running and its horus.world label.
    case "$*" in
      *State.Running*) printf '%s\\n' "${STUB_RUNNING:-false}" ;;
      *horus.world*) printf '%s\\n' "${STUB_WORLD_LABEL:-}" ;;
    esac
    exit 0 ;;
  exec) exit 0 ;;
esac
exit 0
"""

_PGREP = """#!/usr/bin/env bash
printf 'pgrep %s\\n' "$*" >> "${STUB_LOG}"
if [[ -n "${STUB_PGREP_MATCH:-}" && "$*" == *"${STUB_PGREP_MATCH}"* ]]; then
  exit 0
fi
exit 1
"""

GTA_GAME = "[G]TA5_Enhanced"
GAZEBO = "[g]z sim"


def _run(tmp_path: Path, *args: str, pgrep_match: str = "", docker_ps: str = "", **env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", _DOCKER), ("pgrep", _PGREP)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    environment = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "HORUS_SIM_GPU": "off",
        "STUB_LOG": str(log),
        "STUB_PGREP_MATCH": pgrep_match,
        "STUB_DOCKER_PS": docker_ps,
        **env,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "sim" / "run.sh"), *args],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    return result, log.read_text().splitlines()


def _docker_runs(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("docker run ")]


def test_gta_container_gets_no_gazebo_environment_and_gazebo_keeps_it(tmp_path):
    _, gta_calls = _run(tmp_path, "start", "--gta", GZ_IP="10.0.0.1", GZ_PARTITION="horus")
    (gta_run,) = _docker_runs(gta_calls)
    assert "GZ_" not in gta_run

    _, gazebo_calls = _run(tmp_path, "start", "horus_indoor.sdf", GZ_IP="10.0.0.1", GZ_PARTITION="horus")
    (gazebo_run,) = _docker_runs(gazebo_calls)
    assert "--env GZ_IP=10.0.0.1" in gazebo_run and "--env GZ_PARTITION=horus" in gazebo_run
    assert "GZ_SIM_RESOURCE_PATH=" in gazebo_run


def test_gta_always_starts_the_physics_world(tmp_path):
    result, calls = _run(tmp_path, "start", "--gta", HORUS_GTA_PHYSICS_ADDRESS="192.168.1.20")
    assert result.returncode == DOCKER_RUN_REACHED, result.stderr
    (gta_run,) = _docker_runs(calls)
    assert "--env HORUS_GTA_PHYSICS_ADDRESS=192.168.1.20" in gta_run
    assert gta_run.endswith("/horus/sim/scripts/start_gta_sitl.sh")

    # The camera-only mode is gone: the help shows only the physics start.
    help_text = subprocess.run(
        ["bash", str(ROOT / "sim" / "run.sh"), "--help"], capture_output=True, text=True, timeout=60,
    ).stdout
    assert "physical object" in help_text and "bridge" not in help_text


@pytest.mark.parametrize(("label", "refused"), [("gta", True), ("gazebo", False)])
def test_frame_is_refused_on_a_gta_container(tmp_path, label, refused):
    result, calls = _run(tmp_path, "frame", STUB_RUNNING="true", STUB_WORLD_LABEL=label)

    grabs = [call for call in calls if call.startswith("docker exec ") and "grab_frame.py" in call]
    if refused:
        assert result.returncode == 2
        assert "runs GTA" in result.stderr
        assert grabs == []
    else:
        assert result.returncode == 0, result.stderr
        assert len(grabs) == 1


def test_gazebo_is_refused_while_the_gta_game_runs(tmp_path):
    result, calls = _run(tmp_path, "start", "horus_indoor.sdf", pgrep_match=GTA_GAME)

    assert result.returncode == 1
    assert "gta_game.sh stop" in result.stderr
    assert _docker_runs(calls) == []


def test_gta_is_refused_while_gazebo_runs(tmp_path):
    result, calls = _run(tmp_path, "start", "--gta", pgrep_match=GAZEBO)

    assert result.returncode == 1
    assert "gz sim" in result.stderr
    assert _docker_runs(calls) == []


@pytest.mark.parametrize(("args", "running"), [
    # A labelled GTA world under another container name, against a Gazebo start...
    (("start", "horus_indoor.sdf"), 'other-sim|gta|"/horus/sim/scripts/start_gta_sitl.sh"\\n'),
    # ...a labelled Gazebo world against a GTA start...
    (("start", "--gta"), 'other-sim|gazebo|"/horus/sim/scripts/start_sim.sh horus_indoor.sdf"\\n'),
    # ...and an older, unlabelled world container, known by its start script.
    (("start", "horus_indoor.sdf"), 'other-sim||"/horus/sim/scripts/start_sim.sh horus_indoor.sdf"\\n'),
])
def test_a_second_world_container_is_refused_whatever_its_name(tmp_path, args, running):
    result, calls = _run(
        tmp_path, *args, docker_ps=running, HORUS_SIM_CONTAINER_NAME="horus-sim-2",
    )

    assert result.returncode == 1
    assert "other-sim" in result.stderr and "already running" in result.stderr
    assert _docker_runs(calls) == []


@pytest.mark.parametrize(("args", "label"), [
    (("start", "horus_indoor.sdf"), "gazebo"),
    (("start", "--gta"), "gta"),
])
def test_a_lone_world_starts_with_its_world_label(tmp_path, args, label):
    # Another, non-HORUS container does not count as a world.
    result, calls = _run(tmp_path, *args, docker_ps='postgres||"docker-entrypoint.sh postgres"\\n')

    assert result.returncode == DOCKER_RUN_REACHED, result.stderr
    (run,) = _docker_runs(calls)
    assert f"--label horus.world={label}" in run
    if label == "gta":
        # The GTA profile is the container default config.
        assert "--env HORUS_CONFIG=/horus/config/horus_gta.yaml" in run


@pytest.mark.parametrize(("args", "config"), [
    (("start", "--gta"), "/horus/config/horus_forest.yaml"),
    (("start", "horus_indoor.sdf"), "/horus/config/horus_gta.yaml"),
])
def test_a_config_from_the_other_world_is_refused_before_docker_run(tmp_path, args, config):
    result, calls = _run(tmp_path, *args, HORUS_CONFIG=config)

    assert result.returncode == 2
    assert config in result.stderr
    assert _docker_runs(calls) == []


def test_gui_client_is_not_blocked_by_a_running_world(tmp_path):
    xauthority = tmp_path / "Xauthority"
    xauthority.write_text("")
    result, calls = _run(
        tmp_path, "gui-client",
        pgrep_match=GTA_GAME,
        docker_ps='horus-sim|gazebo|"/horus/sim/scripts/start_sim.sh horus_indoor.sdf"\\n',
        HORUS_REMOTE_IP="10.0.0.2", HORUS_LOCAL_IP="10.0.0.1",
        XAUTHORITY=str(xauthority),
    )

    # The client only observes a remote world: the guard never runs. Without an X11 socket it
    # stops at the GUI check instead of docker run, but never at the guard.
    assert "already running" not in result.stderr and "on this host" not in result.stderr
    assert not [call for call in calls if call.startswith(("pgrep ", "docker ps"))]
    assert all("horus.world" not in run for run in _docker_runs(calls))
    if Path("/tmp/.X11-unix").is_dir():
        assert result.returncode == DOCKER_RUN_REACHED, result.stderr
