"""Shared state, tool registration and result formatting for the HORUS MCP tool modules."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import update_wrapper
from typing import Any, Mapping

from horus.config import HorusConfig
from horus.event_log import JsonlEventLog
from horus.events import EventBus
from horus.executor import HorusExecutor
from horus.executor.plan import PlanRunner
from horus.flight import FlightLink

_LOG = logging.getLogger(__name__)


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    if hasattr(value, "as_dict"):
        return plain(value.as_dict())
    if is_dataclass(value):
        return plain(asdict(value))
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    return str(value)


def to_json(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def observation_age(observation: Any) -> tuple[str | None, float | None]:
    if observation is None:
        return None, None
    if isinstance(observation, Mapping):
        timestamp = observation.get("timestamp", observation.get("observed_at"))
        age = observation.get("observation_age_s", observation.get("age_s"))
    else:
        timestamp = getattr(observation, "timestamp", getattr(observation, "observed_at", None))
        age = getattr(observation, "observation_age_s", getattr(observation, "age_s", None))
    if age is None:
        monotonic = (
            observation.get("monotonic_time")
            if isinstance(observation, Mapping)
            else getattr(observation, "monotonic_time", None)
        )
        if monotonic is None and isinstance(timestamp, (int, float)):
            monotonic = timestamp
        if monotonic is not None:
            age = max(0.0, time.monotonic() - float(monotonic))
    return (None if timestamp is None else str(timestamp), None if age is None else float(age))


def candidate_result(candidate: Any) -> dict[str, Any]:
    data = plain(candidate)
    if not isinstance(data, dict):
        data = {"candidate": data}
    labels = []
    for key in ("class_name", "label", "recognized_class"):
        if key in data:
            labels.append(json.dumps(str(data.pop(key)), ensure_ascii=False))
    if labels:
        data["untrusted_detector_labels"] = labels
    return data


def _short_result(result: Any) -> Any:
    """Summarize a tool result without ever copying image content into the log."""
    if isinstance(result, (list, tuple)):
        summary = []
        for item in result:
            if type(item).__name__ == "Image" or getattr(item, "type", None) == "image":
                summary.append({"type": "image", "mimeType": getattr(item, "mimeType", "image/jpeg")})
            elif isinstance(item, str):
                summary.append({"type": "text", "text": item[:500]})
            elif getattr(item, "type", None) == "text":
                summary.append({"type": "text", "text": str(getattr(item, "text", ""))[:500]})
            else:
                summary.append(str(item)[:200])
        return summary
    if isinstance(result, str):
        return result[:500]
    return plain(result)


class ToolContext:
    """What every HORUS tool module needs: the server's components and its tool registrar."""

    def __init__(
        self,
        mcp: Any,
        config: HorusConfig,
        executor: HorusExecutor,
        flight: FlightLink,
        perception: Any,
        *,
        perception_unavailable_reason: str | None,
        event_log: JsonlEventLog | None,
        image_publisher: Any,
        event_bus: EventBus,
    ) -> None:
        self.mcp = mcp
        self.config = config
        self.executor = executor
        self.flight = flight
        self.perception = perception
        self.perception_unavailable_reason = perception_unavailable_reason
        self.event_log = event_log
        self.image_publisher = image_publisher
        self.event_bus = event_bus
        self.plans = PlanRunner(executor, require_perception=self.require_perception)

    def tool(self, function: Callable[..., Any], **options: Any) -> None:
        """Register ``function(ctx, **arguments)`` as an event-logged MCP tool.

        ``options`` go to ``mcp.tool()``. A call with ``passive=True`` (read-only GUI telemetry)
        is neither logged nor counted as supervisory-link contact, so a display polling state can
        never keep link-loss RTH from firing; every other call counts at its start and end.
        """
        if inspect.iscoroutinefunction(function):
            async def call(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("passive") is True:
                    return await function(self, *args, **kwargs)
                with self._supervised(function.__name__, args, kwargs) as outcome:
                    outcome["result"] = await function(self, *args, **kwargs)
                return outcome["result"]
        else:
            def call(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("passive") is True:
                    return function(self, *args, **kwargs)
                with self._supervised(function.__name__, args, kwargs) as outcome:
                    outcome["result"] = function(self, *args, **kwargs)
                return outcome["result"]

        update_wrapper(call, function)
        signature = inspect.signature(function, eval_str=True)
        context_parameter, *parameters = signature.parameters
        call.__signature__ = signature.replace(
            parameters=[signature.parameters[name] for name in parameters]
        )
        call.__annotations__ = {
            key: value for key, value in function.__annotations__.items() if key != context_parameter
        }
        self.mcp.tool(**options)(call)

    @contextmanager
    def _supervised(self, name: str, args: Any, kwargs: Any) -> Iterator[dict[str, Any]]:
        self.executor.note_supervisor_contact()
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        outcome: dict[str, Any] = {"result": None}
        try:
            yield outcome
        except Exception as exc:
            self._log_tool_event(name, args, kwargs, started_at, started, None, exc)
            raise
        else:
            self._log_tool_event(name, args, kwargs, started_at, started, outcome["result"], None)
        finally:
            self.executor.note_supervisor_contact()

    def _log_tool_event(
        self, name: str, args: Any, kwargs: Any, started_at: str, started: float, result: Any,
        error: Exception | None,
    ) -> None:
        if self.event_log is None:
            return
        event: dict[str, Any] = {
            "timestamp": started_at,
            "tool": name,
            "args": {"positional": args, "named": kwargs},
            "result": (
                _short_result(result)
                if error is None
                else {"error_type": type(error).__name__, "error": str(error)[:500]}
            ),
            "latency_ms": (time.monotonic() - started) * 1000.0,
            "state_snapshot": self._state_snapshot(),
        }
        try:
            self.event_log.append(event)
        except Exception:
            _LOG.exception("Failed to append HORUS MCP event")

    def _state_snapshot(self) -> dict[str, Any]:
        try:
            executor_state = plain(self.executor.get_state())
        except Exception as exc:
            executor_state = {"unavailable": f"{type(exc).__name__}: {exc}"}
        try:
            link_state = plain(self.flight.telemetry().as_dict())
        except Exception as exc:
            link_state = {"unavailable": f"{type(exc).__name__}: {exc}"}
        try:
            observation = self.perception.latest_observation() if self.perception is not None else None
            observed_at, age = observation_age(observation)
            observation_state = {"observed_at": observed_at, "observation_age_s": age}
        except Exception as exc:
            observation_state = {"unavailable": f"{type(exc).__name__}: {exc}"}
        try:
            perception_state = self.camera_status()
        except Exception as exc:
            perception_state = {"unavailable": f"{type(exc).__name__}: {exc}"}
        return {
            "executor": executor_state,
            "link": link_state,
            "observation": observation_state,
            "perception": perception_state,
        }

    def camera_status(self) -> dict[str, Any]:
        if self.perception is None:
            return {
                "camera": "disabled",
                "last_frame_age_s": None,
                "reason": self.perception_unavailable_reason or "perception is disabled",
            }
        return dict(self.perception.camera_status())

    def perception_system(self) -> Any:
        """Return perception even when its camera is not streaming (scene tools report staleness)."""
        if self.perception is None:
            reason = self.perception_unavailable_reason or "perception is disabled"
            raise RuntimeError(f"perception unavailable: {reason}")
        return self.perception

    def require_perception(self) -> Any:
        """Return perception only while its camera is streaming."""
        system = self.perception_system()
        status = self.camera_status()
        if status.get("camera") != "streaming":
            reason = status.get("reason") or f"camera is {status.get('camera', 'unavailable')}"
            raise RuntimeError(f"perception unavailable: {reason}")
        return system

    def submit(self, skill: str, arguments: dict[str, Any], ttl_s: float | None) -> str:
        """Queue a direct skill; a running plan owns the queue until it ends or is cancelled."""
        self.plans.require_idle()
        return to_json(self.executor.submit_skill(skill, arguments, ttl_s))
