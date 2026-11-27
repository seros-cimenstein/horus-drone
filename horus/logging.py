"""Centralized HORUS logging, Rich console rendering, secret redaction, and error hooks."""

from __future__ import annotations

import argparse
import io
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from horus.event_log import _BEARER_RE, _DATA_IMAGE_RE, _OMITTED, _REDACTED, _SECRET_PARAM_RE

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme
    from rich.traceback import install as install_rich_traceback

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive fallback if rich is absent
    Console = None  # type: ignore[assignment,misc]
    RichHandler = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    Theme = None  # type: ignore[assignment,misc]
    install_rich_traceback = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False

_FRAGMENT_TOKEN_RE = re.compile(r"(#token=)[^&\s]+", re.IGNORECASE)
_MIN_SECRET_LEN = 8
_VALID_FORMATS = frozenset({"auto", "rich", "plain", "json"})
_THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "PIL",
    "matplotlib",
    "multipart",
    "asyncio",
    "uvicorn.access",
)

_SECRETS_LOCK = threading.RLock()
_REGISTERED_SECRETS: set[str] = set()

if _RICH_AVAILABLE:
    HORUS_THEME = Theme(
        {
            "horus.brand": "bold cyan",
            "horus.ok": "bold green",
            "horus.warn": "bold yellow",
            "horus.err": "bold red",
            "horus.muted": "dim",
            "horus.key": "bold white",
            "horus.url": "underline cyan",
            "logging.level.debug": "dim cyan",
            "logging.level.info": "bold green",
            "logging.level.warning": "bold yellow",
            "logging.level.error": "bold red",
            "logging.level.critical": "bold white on red",
        }
    )
else:  # pragma: no cover
    HORUS_THEME = None


def register_secret(secret: str | None) -> None:
    """Register a runtime secret (bearer token, API key) for log/traceback redaction."""
    if not secret:
        return
    cleaned = str(secret).strip()
    if len(cleaned) < _MIN_SECRET_LEN:
        return
    with _SECRETS_LOCK:
        _REGISTERED_SECRETS.add(cleaned)


def clear_registered_secrets() -> None:
    """Clear registered runtime secrets (primarily for test isolation)."""
    with _SECRETS_LOCK:
        _REGISTERED_SECRETS.clear()


def redact_text(text: str, extra_secrets: Iterable[str | None] = ()) -> str:
    """Redact bearer tokens, query/fragment secrets, inline images, and registered secrets."""
    if not text:
        return text
    out = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    out = _SECRET_PARAM_RE.sub(rf"\1{_REDACTED}", out)
    out = _FRAGMENT_TOKEN_RE.sub(rf"\1{_REDACTED}", out)
    out = _DATA_IMAGE_RE.sub(_OMITTED, out)

    with _SECRETS_LOCK:
        secrets_snapshot = set(_REGISTERED_SECRETS)
    for item in extra_secrets:
        if item:
            cleaned = str(item).strip()
            if len(cleaned) >= _MIN_SECRET_LEN:
                secrets_snapshot.add(cleaned)

    if secrets_snapshot:
        for secret in sorted(secrets_snapshot, key=len, reverse=True):
            if secret in out:
                out = out.replace(secret, _REDACTED)
    return out


class SecretRedactingFilter(logging.Filter):
    """Filter that scrubs secrets from log messages, format arguments, and cached tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        args = record.args
        if isinstance(args, Mapping):
            record.args = {
                key: redact_text(val) if isinstance(val, str) else val
                for key, val in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(
                redact_text(val) if isinstance(val, str) else val for val in args
            )
        elif isinstance(args, str):
            record.args = redact_text(args)
        if isinstance(record.exc_text, str):
            record.exc_text = redact_text(record.exc_text)
        return True


class ComponentFilter(logging.Filter):
    """Ensure every LogRecord carries a ``component`` attribute."""

    def __init__(self, component: str = "horus") -> None:
        super().__init__()
        self.component = component or "horus"

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "component", None):
            record.component = self.component
        return True


class _RedactingFormatterMixin:
    """Scrub secrets from formatted exception tracebacks as well as log messages."""

    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib logging method name
        formatted = super().formatException(ei)  # type: ignore[misc]
        return redact_text(formatted)


class PlainLogFormatter(_RedactingFormatterMixin, logging.Formatter):
    """Deterministic UTC plain-text log formatter with zero ANSI escape sequences."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(component)s] %(name)s:%(lineno)d: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )


class JsonLogFormatter(_RedactingFormatterMixin, logging.Formatter):
    """Structured single-line JSON formatter with UTC timestamps and secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "component": getattr(record, "component", "horus"),
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "pid": record.process,
            "thread": record.threadName,
            "file": record.filename,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = redact_text(record.exc_text)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


if _RICH_AVAILABLE:

    class _HorusRichHandler(RichHandler):
        """RichHandler that also redacts secrets inside rendered Rich tracebacks."""

        def render(
            self,
            *,
            record: logging.LogRecord,
            traceback: Any,
            message_renderable: Any,
        ) -> Any:
            if traceback is not None:
                buf = io.StringIO()
                temp = Console(
                    file=buf,
                    theme=HORUS_THEME,
                    force_terminal=self.console.is_terminal,
                    color_system=self.console.color_system,
                    no_color=self.console.no_color,
                    width=self.console.width,
                )
                temp.print(traceback)
                redacted = redact_text(buf.getvalue().rstrip("\n"))
                traceback = Text.from_ansi(redacted)
            return super().render(
                record=record,
                traceback=traceback,
                message_renderable=message_renderable,
            )


def get_console(*, stderr: bool = True, force_terminal: bool | None = None) -> Any:
    """Return a configured Rich Console (respecting NO_COLOR / FORCE_COLOR), or None if unavailable."""
    if not _RICH_AVAILABLE:
        return None
    no_color = bool(os.environ.get("NO_COLOR", "").strip())
    if force_terminal is None and os.environ.get("FORCE_COLOR", "").strip():
        force_terminal = True
    return Console(
        stderr=stderr,
        theme=HORUS_THEME,
        no_color=no_color,
        force_terminal=force_terminal,
    )


def resolve_log_level(level: str | int | None = None) -> int:
    """Resolve an explicit or ``HORUS_LOG_LEVEL`` level, falling back to INFO safely."""
    if isinstance(level, int):
        return level
    candidate = (level if isinstance(level, str) else os.environ.get("HORUS_LOG_LEVEL")) or "INFO"
    name = candidate.strip().upper()
    if name.isdigit():
        return int(name)
    resolved = logging.getLevelName(name)
    return resolved if isinstance(resolved, int) else logging.INFO


def resolve_log_format(fmt: str | None = None) -> str:
    """Resolve effective log format (``rich``, ``plain``, or ``json``)."""
    raw = (fmt or os.environ.get("HORUS_LOG_FORMAT") or "auto").strip().lower()
    if raw not in _VALID_FORMATS:
        raw = "auto"
    if raw == "auto":
        is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
        no_color = bool(os.environ.get("NO_COLOR", "").strip())
        if _RICH_AVAILABLE and is_tty and not no_color:
            return "rich"
        return "plain"
    if raw == "rich" and not _RICH_AVAILABLE:
        return "plain"
    return raw


def _make_console_handler(effective_fmt: str) -> logging.Handler:
    if effective_fmt == "rich" and _RICH_AVAILABLE:
        console = get_console(stderr=True)
        handler: logging.Handler = _HorusRichHandler(
            console=console,
            rich_tracebacks=True,
            tracebacks_show_locals=bool(os.environ.get("HORUS_TRACEBACK_LOCALS", "").strip()),
            tracebacks_suppress=[argparse],
            show_time=True,
            show_level=True,
            show_path=True,
            markup=False,
            omit_repeated_times=False,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    stream_handler = logging.StreamHandler(sys.stderr)
    if effective_fmt == "json":
        stream_handler.setFormatter(JsonLogFormatter())
    else:
        stream_handler.setFormatter(PlainLogFormatter())
    return stream_handler


def configure_logging(
    *,
    level: str | int | None = None,
    fmt: str | None = None,
    log_file: str | Path | None = None,
    component: str = "horus",
    rich_tracebacks: bool | None = None,
    extra_secrets: Iterable[str | None] = (),
    quiet_third_party: bool = True,
    force: bool = True,
) -> logging.Logger:
    """Configure root HORUS logging with Rich/plain/JSON output, secret redaction, and optional file rotation."""
    for secret in extra_secrets:
        register_secret(secret)

    resolved_level = resolve_log_level(level)
    effective_fmt = resolve_log_format(fmt)
    file_target = log_file if log_file is not None else os.environ.get("HORUS_LOG_FILE")

    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:
                pass

    root.setLevel(resolved_level)

    redactor = SecretRedactingFilter()
    comp_filter = ComponentFilter(component)

    console_handler = _make_console_handler(effective_fmt)
    console_handler.setLevel(resolved_level)
    console_handler.addFilter(comp_filter)
    console_handler.addFilter(redactor)
    root.addHandler(console_handler)

    if file_target:
        target_path = Path(file_target).expanduser()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            target_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(
            JsonLogFormatter() if effective_fmt == "json" else PlainLogFormatter()
        )
        file_handler.addFilter(comp_filter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    use_rich_tb = (effective_fmt == "rich") if rich_tracebacks is None else bool(rich_tracebacks)
    if use_rich_tb and _RICH_AVAILABLE and install_rich_traceback is not None:
        tb_console = get_console(stderr=True)
        if tb_console is not None:
            install_rich_traceback(
                console=tb_console,
                show_locals=bool(os.environ.get("HORUS_TRACEBACK_LOCALS", "").strip()),
                suppress=[argparse],
            )

    if quiet_third_party:
        for name in _THIRD_PARTY_LOGGERS:
            if name == "uvicorn.access":
                tp_level = logging.INFO if resolved_level <= logging.DEBUG else logging.ERROR
            elif name == "httpx":
                tp_level = logging.INFO if resolved_level <= logging.DEBUG else logging.WARNING
            else:
                tp_level = logging.WARNING
            logging.getLogger(name).setLevel(tp_level)

    return logging.getLogger(f"horus.{component}" if component != "horus" else "horus")


def unwrap_exception(exc: BaseException) -> BaseException:
    """Unwrap anyio / TaskGroup ExceptionGroup wrappers to the underlying cause."""
    sub = getattr(exc, "exceptions", None)
    while isinstance(sub, (list, tuple)) and sub and isinstance(sub[0], BaseException):
        exc = sub[0]
        sub = getattr(exc, "exceptions", None)
    return exc


def format_exception_summary(exc: BaseException) -> str:
    """Return a concise, secret-redacted ``TypeName: message`` string."""
    inner = unwrap_exception(exc)
    detail = redact_text(str(inner)).strip()
    type_name = type(inner).__name__
    return f"{type_name}: {detail}" if detail else type_name


def render_error(
    title: str,
    exc: BaseException | str,
    *,
    hint: str | None = None,
    show_traceback: bool = False,
) -> None:
    """Render an error summary cleanly to stderr using Rich when interactive, or plain text otherwise."""
    if isinstance(exc, BaseException):
        summary = format_exception_summary(exc)
        tb_exc: BaseException | None = exc
    else:
        summary = redact_text(str(exc))
        tb_exc = None

    console = get_console(stderr=True)
    is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
    no_color = bool(os.environ.get("NO_COLOR", "").strip())
    if _RICH_AVAILABLE and console is not None and is_tty and not no_color and Panel is not None:
        body = Text(summary)
        if hint:
            body.append(f"\n\nHint: {redact_text(hint)}", style="horus.muted")
        console.print(Panel(body, title=f"[horus.err]{title}[/]", border_style="red", expand=False))
        if show_traceback and tb_exc is not None:
            console.print_exception(show_locals=False)
        return

    print(f"error: {title}: {summary}", file=sys.stderr)
    if hint:
        print(f"hint: {redact_text(hint)}", file=sys.stderr)
    if show_traceback and tb_exc is not None:
        import traceback

        print(
            redact_text("".join(traceback.format_exception(type(tb_exc), tb_exc, tb_exc.__traceback__))),
            file=sys.stderr,
        )


def install_unhandled_exception_hooks(logger_name: str = "horus") -> None:
    """Install ``sys.excepthook`` and ``threading.excepthook`` to log uncaught exceptions cleanly."""
    log = logging.getLogger(logger_name)

    def _sys_hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> Any:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> Any:
        if args.exc_type is SystemExit:
            return
        thread_name = args.thread.name if args.thread is not None else "unknown"
        log.error(
            "Unhandled exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def add_logging_arguments(parser: argparse.ArgumentParser) -> None:
    """Register standard ``--log-level``, ``--log-format``, and ``--log-file`` CLI flags."""
    group = parser.add_argument_group("logging options")
    group.add_argument(
        "--log-level",
        default=None,
        help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL; env: HORUS_LOG_LEVEL)",
    )
    group.add_argument(
        "--log-format",
        choices=("auto", "rich", "plain", "json"),
        default=None,
        help="Log output format (default: auto; env: HORUS_LOG_FORMAT)",
    )
    group.add_argument(
        "--log-file",
        default=None,
        help="Optional rotating log file path (env: HORUS_LOG_FILE)",
    )
