from __future__ import annotations

import json

import pytest

from horus import cli


def test_cli_doctor_json(capsys) -> None:
    rc = cli.main(["doctor", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "python" in data and "assets" in data and "mujoco" in data


def test_cli_setup_check_returns_status(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HORUS_ASSETS_DIR", str(tmp_path))
    monkeypatch.delenv("HORUS_SITL_BIN", raising=False)
    monkeypatch.delenv("HORUS_YOLO_MODEL", raising=False)
    monkeypatch.delenv("HORUS_REID_MODEL", raising=False)
    rc = cli.main(["setup", "--check"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "[MISSING]" in out


def test_cli_legacy_flags_dispatch_to_tui(monkeypatch) -> None:
    called: list[list[str]] = []

    def fake_repl(argv=None):
        called.append(list(argv or []))

    monkeypatch.setattr("horus.repl.app.main", fake_repl)
    rc = cli.main(["--server-url", "http://127.0.0.1:8000/mcp"])
    assert rc == 0
    assert called == [["--server-url", "http://127.0.0.1:8000/mcp"]]


def test_cli_help_exits_cleanly(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "studio" in out
    assert "logs" in out


def test_cli_doctor_human_output(capsys, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "HORUS Doctor" in out
    assert "MuJoCo GL" in out


def test_cli_logs_help_exits_cleanly(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["logs", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--service" in out
    assert "--follow" in out
