"""Thread-safe, secret-aware JSON Lines event logging for HORUS."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_PARAM_RE = re.compile(
    r"([?&](?:access_token|api_key|apikey|auth|authorization|token|secret|password)=)[^&#\s]+",
    re.IGNORECASE,
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9_+/=-]+$")
_BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_+/=-])")
_DATA_IMAGE_RE = re.compile(r"data:image/[^;,\s]+;base64,[A-Za-z0-9_+/=-]+", re.IGNORECASE)
_SENSITIVE_PARTS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
_REDACTED = "[REDACTED]"
_OMITTED = "[omitted payload]"


def _normalized_key(key: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).casefold().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    parts = set(normalized.split("_"))
    return (
        bool(parts & _SENSITIVE_PARTS)
        or normalized in _SENSITIVE_PARTS
        or {"api", "key"} <= parts
    )


def _is_image_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return (
        "image" in normalized
        or "jpeg" in normalized
        or "base64" in normalized
        or normalized in {"jpg", "png", "image_data"}
    )


def _is_encoded_image(compact: str) -> bool:
    try:
        decoded = base64.b64decode(
            compact + "=" * (-len(compact) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error):
        return False
    return decoded.startswith(
        (
            b"\xff\xd8\xff",
            b"\x89PNG\r\n\x1a\n",
            b"GIF8",
            b"BM",
            b"II*\x00",
            b"MM\x00*",
            b"\x00\x00\x01\x00",
        )
    ) or (decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP")


def _safe_value(value: Any, *, key: str = "") -> Any:
    """Convert event data to JSON-safe values while excluding credentials and images."""
    if _is_sensitive_key(key):
        return _REDACTED
    if _is_image_key(key):
        return _OMITTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _OMITTED
    if isinstance(value, str):
        value = _BEARER_RE.sub("Bearer [REDACTED]", value)
        value = _SECRET_PARAM_RE.sub(r"\1[REDACTED]", value)
        value = _DATA_IMAGE_RE.sub(_OMITTED, value)
        compact = "".join(value.split())
        if len(compact) >= 32 and _BASE64_RE.fullmatch(compact) and _is_encoded_image(compact):
            return _OMITTED
        return _BASE64_TOKEN_RE.sub(
            lambda match: _OMITTED if _is_encoded_image(match.group()) else match.group(),
            value,
        )
    if isinstance(value, Mapping):
        return {str(item_key): _safe_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_safe_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except Exception:
            pass
    return _safe_value(str(value))


def event_log_path_from_environment() -> Path:
    """Resolve the shared HORUS event log path using server/REPL precedence.

    ``HORUS_EVENT_LOG`` overrides ``HORUS_RUN_DIR/events.jsonl``. Without either
    override, the active server run directory is read from ``runs/horus/current-run``.
    """
    event_path = os.environ.get("HORUS_EVENT_LOG")
    if event_path:
        return Path(event_path).expanduser()
    run_dir = os.environ.get("HORUS_RUN_DIR")
    if run_dir:
        return Path(run_dir).expanduser() / "events.jsonl"
    pointer = Path("runs/horus/current-run")
    try:
        selected_run_dir = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "No HORUS event log is configured; start HORUS MCP or set HORUS_EVENT_LOG/HORUS_RUN_DIR"
        ) from exc
    if not selected_run_dir:
        raise RuntimeError(f"HORUS MCP run pointer is empty: {pointer}")
    return Path(selected_run_dir).expanduser() / "events.jsonl"


class JsonlEventLog:
    """Append sanitized JSON events to a JSONL file, one complete record per line."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event: Mapping[str, Any]) -> None:
        """Append an event atomically within this process, adding a UTC timestamp if absent."""
        record = _safe_value(event)
        if not isinstance(record, dict):
            raise TypeError("event must be a mapping")
        if "timestamp" not in record:
            record["timestamp"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
