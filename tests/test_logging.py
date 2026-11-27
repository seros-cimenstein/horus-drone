from __future__ import annotations

import json
import logging
import sys
import threading

import pytest

from horus import cli
from horus.logging import (
    clear_registered_secrets,
    configure_logging,
    install_unhandled_exception_hooks,
    register_secret,
    resolve_log_format,
    unwrap_exception,
)


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    prev_handlers = list(root.handlers)
    prev_level = root.level
    prev_excepthook = sys.excepthook
    prev_thread_hook = threading.excepthook
    clear_registered_secrets()
    try:
        yield
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            if h not in prev_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        for h in prev_handlers:
            root.addHandler(h)
        root.setLevel(prev_level)
        sys.excepthook = prev_excepthook
        threading.excepthook = prev_thread_hook
        clear_registered_secrets()


def test_resolve_log_format_respects_no_color_and_non_tty(monkeypatch) -> None:
    monkeypatch.delenv("HORUS_LOG_FORMAT", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert resolve_log_format("auto") == "plain"
    assert resolve_log_format("json") == "json"


def test_plain_json_and_rich_formats_with_secret_redaction(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    secret = "sk-horus-super-secret-token-987654321"
    register_secret(secret)

    # 1. Plain format + rotating file log
    log_path = tmp_path / "nested" / "horus.log"
    log = configure_logging(level="DEBUG", fmt="plain", log_file=log_path, component="test")
    log.info("Auth Bearer my-bearer-secret-123 and ?api_key=abc123&ok=1 and #token=frag999")
    log.info("Tuple arg secret=%s", secret)
    log.info("Mapping arg token=%(tok)s", {"tok": secret})
    log.info("Inline image data:image/jpeg;base64,/9j/4AAQSkZJRg==")
    try:
        raise RuntimeError(f"upstream failed with {secret} and Bearer another-secret-xyz")
    except RuntimeError:
        log.exception("Caught failure")

    err_out = capsys.readouterr().err
    assert "\x1b[" not in err_out
    assert secret not in err_out
    assert "my-bearer-secret-123" not in err_out
    assert "abc123" not in err_out
    assert "frag999" not in err_out
    assert "another-secret-xyz" not in err_out
    assert "[REDACTED]" in err_out
    assert "[omitted payload]" in err_out

    file_text = log_path.read_text(encoding="utf-8")
    assert "\x1b[" not in file_text
    assert secret not in file_text
    assert "another-secret-xyz" not in file_text
    assert "[test]" in file_text

    # 2. JSON format
    json_log_path = tmp_path / "nested" / "horus.jsonl"
    jlog = configure_logging(level="INFO", fmt="json", log_file=json_log_path, component="api")
    jlog.warning("Warning with %s", secret)
    try:
        raise ValueError(f"bad key {secret}")
    except ValueError:
        jlog.exception("Value failure")

    json_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(json_lines) == 2
    first = json.loads(json_lines[0])
    second = json.loads(json_lines[1])
    assert first["level"] == "WARNING"
    assert first["component"] == "api"
    assert secret not in first["message"]
    assert "[REDACTED]" in first["message"]
    assert secret not in second["exception"]
    assert "[REDACTED]" in second["exception"]

    # 3. Rich format with brackets in message (markup=False safety) and redacted traceback
    monkeypatch.delenv("NO_COLOR", raising=False)
    rlog = configure_logging(level="INFO", fmt="rich", log_file=log_path, component="studio")
    rlog.info("[operator emergency] bracketed message [1, 2, 3]")
    try:
        raise RuntimeError(f"boom {secret}")
    except RuntimeError:
        rlog.exception("Rich traceback test")
    rich_err = capsys.readouterr().err
    assert secret not in rich_err
    assert "[operator emergency]" in rich_err
    # File output must stay ANSI-free even when console is rich
    assert "\x1b[" not in log_path.read_text(encoding="utf-8")


def test_unwrap_exception_unwraps_nested_groups() -> None:
    root = ValueError("underlying cause")
    grp = ExceptionGroup("outer", [ExceptionGroup("inner", [root])])
    assert unwrap_exception(grp) is root


def test_unhandled_thread_exception_hook_logs_error(capsys) -> None:
    configure_logging(level="INFO", fmt="plain", component="hooks")
    install_unhandled_exception_hooks("horus.test_hooks")

    def boom() -> None:
        raise RuntimeError("background thread crash")

    t = threading.Thread(target=boom, name="worker-test")
    t.start()
    t.join()

    err = capsys.readouterr().err
    assert "Unhandled exception in thread worker-test" in err
    assert "RuntimeError: background thread crash" in err


def test_horus_logs_list_tail_and_level_filter(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    runs_dir = tmp_path / "runs"
    studio_dir = runs_dir / "studio"
    run_dir = studio_dir / "20261127T120000Z_abc123"
    (run_dir / "mcp_run").mkdir(parents=True)

    (studio_dir / "studio.log").write_text(
        "2026-11-27T12:00:00Z INFO     [studio] horus.cli:1: Studio started\n"
        "2026-11-27T12:00:01Z ERROR    [studio] horus.cli:2: Studio failed port check\n",
        encoding="utf-8",
    )
    (run_dir / "mcp.log").write_text(
        "2026-11-27T12:00:02Z DEBUG    [mcp] horus.mcp:10: Debug trace\n"
        "2026-11-27T12:00:03Z WARNING  [mcp] horus.mcp:11: Camera stalled\n",
        encoding="utf-8",
    )
    (run_dir / "world.log").write_text("World ready\n", encoding="utf-8")
    (run_dir / "mcp_run" / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-11-27T12:00:04Z", "kind": "emergency", "action": "hold"}) + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["logs", "--runs-dir", str(runs_dir), "--list"])
    assert rc == 0
    list_out = capsys.readouterr().out
    assert "studio.log" in list_out
    assert "mcp.log" in list_out
    assert "events.jsonl" in list_out

    rc = cli.main(["logs", "--runs-dir", str(runs_dir), "--service", "mcp", "--tail", "1"])
    assert rc == 0
    tail_out = capsys.readouterr().out
    assert "Camera stalled" in tail_out
    assert "Debug trace" not in tail_out

    rc = cli.main(["logs", "--runs-dir", str(runs_dir), "--service", "all", "--level", "ERROR"])
    assert rc == 0
    err_out = capsys.readouterr().out
    assert "Studio failed port check" in err_out
    assert "Studio started" not in err_out
    assert "Camera stalled" not in err_out
