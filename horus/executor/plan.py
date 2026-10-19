"""Onboard runtime for provider-written mission plans (the MCP ``run_plan`` tool).

A provider may send a whole mission as one short program instead of one tool call per step; the
drone then runs it to the end without needing the supervisory link. The language follows TypeFly's
plan format (github.com/typefly/TypeFly, ``typefly/plan_execution.py``, Apache-2.0): a restricted
Python subset of function calls, single-name assignments, ``if``/``elif``/``else`` and
``for <name> in range(<int literals>)`` loops with ``break``/``continue``/``pass``.

HORUS walks the parsed AST itself instead of compiling and ``exec``-ing it, so a plan reaches only
the functions in ``PLAN_FUNCTIONS``; every statement checks cancellation, emergencies and the plan
deadline; and arithmetic is float-only (``+ - * /``) so a bounded loop cannot grow a value into a
memory bomb. Every flight call becomes an ordinary executor skill with the same clamps, TTLs and
ArduPilot safety layer as a direct MCP call. Any emergency (hold/rth/land from MCP, the operator
or link-loss RTH), a pilot override or a cancelled step aborts the plan.
"""

from __future__ import annotations

import ast
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from horus.events import TERMINAL_TASK_STATUSES

logger = logging.getLogger(__name__)

MAX_PLAN_CHARS = 4000
MAX_STRING_CHARS = 500
MAX_LOOP_ITERS = 100
MAX_TOTAL_LOOP_ITERS = 1000
MAX_STATEMENTS = 5000
MAX_SKILL_CALLS = 100
MAX_WAIT_S = 60.0
DEFAULT_PLAN_TTL_S = 600.0
MAX_PLAN_TTL_S = 1800.0
MAX_LOG_LINES = 100
POLL_S = 0.1
_CANCEL_GRACE_S = 5.0
_TERMINAL_PLAN = frozenset({"succeeded", "failed", "cancelled", "aborted"})


@dataclass(frozen=True, slots=True)
class _Signature:
    params: tuple[str, ...]
    required: int
    doc: str


PLAN_FUNCTIONS: dict[str, _Signature] = {
    "take_off": _Signature(
        ("altitude_m", "heading_deg", "ttl_s"), 0, "take off; True when the task succeeded"
    ),
    "goto": _Signature(
        ("north_m", "east_m", "up_m", "heading_deg", "ttl_s"), 3,
        "fly to a local NED pose (metres, up positive); True on arrival",
    ),
    "approach": _Signature(
        ("id", "dist_m", "target_size_m", "ttl_s"), 1,
        "approach a tracker ID to dist_m; True on arrival",
    ),
    "follow": _Signature(
        ("id", "duration_s", "dist_m", "target_size_m"), 2,
        "follow a tracker ID for duration_s; True when it ran for the whole duration",
    ),
    "orbit": _Signature(
        ("id", "duration_s", "radius_m", "target_size_m"), 2,
        "orbit a tracker ID for duration_s; True when it ran for the whole duration",
    ),
    "scan": _Signature(
        ("north_min_m", "north_max_m", "east_min_m", "east_max_m", "altitude_m", "spacing_m",
         "ttl_s"), 4,
        "raster-scan an area; True when the whole area was covered",
    ),
    "search": _Signature(
        ("north_min_m", "north_max_m", "east_min_m", "east_max_m", "class_name", "attribute",
         "equals", "reference_id", "altitude_m", "spacing_m", "ttl_s"), 4,
        "scan until a validated cue match; the matched tracker ID, or -1 when none or ambiguous",
    ),
    "wait": _Signature(("seconds",), 1, f"hold position for up to {MAX_WAIT_S:g} s"),
    "log": _Signature(("message",), 1, "report a line to the provider (plan status and events)"),
    "hold": _Signature((), 0, "emergency hold; ends the plan"),
    "rth": _Signature((), 0, "return home and land; ends the plan"),
    "land": _Signature((), 0, "land; ends the plan"),
}

_RESERVED_NAMES = frozenset(PLAN_FUNCTIONS) | {"range", "True", "False", "None"}
_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}
_ARITHMETIC = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


class PlanError(ValueError):
    """The plan text is not a valid HORUS plan; nothing was executed."""


class _PlanFailed(RuntimeError):
    """A runtime error inside the plan (bad value, undefined name, budget exceeded)."""


class _PlanStopped(BaseException):
    """Stop the interpreter; ``status`` becomes the plan's final status."""

    def __init__(self, status: str, reason: str | None):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _Break(BaseException):
    pass


class _Continue(BaseException):
    pass


def function_reference() -> str:
    """One line per plan function, for tool descriptions."""
    lines = []
    for name, signature in PLAN_FUNCTIONS.items():
        params = [
            param if index < signature.required else f"{param}=None"
            for index, param in enumerate(signature.params)
        ]
        lines.append(f"{name}({', '.join(params)}): {signature.doc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- validation


def validate_plan(source: str) -> ast.Module:
    """Parse and check ``source`` against the plan grammar; raise PlanError on any violation."""
    if not isinstance(source, str) or not source.strip():
        raise PlanError("plan must be a non-empty string")
    if len(source) > MAX_PLAN_CHARS:
        raise PlanError(f"plan exceeds {MAX_PLAN_CHARS} characters")
    try:
        module = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise PlanError(f"plan syntax error at line {exc.lineno}: {exc.msg}") from exc
    _Validator().block(module.body, 1)
    return module


class _Validator:
    def block(self, statements: list[ast.stmt], loop_product: int) -> None:
        for statement in statements:
            self.statement(statement, loop_product)

    def statement(self, node: ast.stmt, loop_product: int) -> None:
        if isinstance(node, ast.Expr):
            self.expression(node.value)
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise _invalid(node, "only single-name assignment is allowed")
            self.assignable(node.targets[0])
            self.expression(node.value)
        elif isinstance(node, ast.If):
            self.expression(node.test)
            self.block(node.body, loop_product)
            self.block(node.orelse, loop_product)
        elif isinstance(node, ast.For):
            self.loop(node, loop_product)
        elif isinstance(node, (ast.Break, ast.Continue, ast.Pass)):
            return
        else:
            raise _invalid(node, f"statement {type(node).__name__} is not allowed")

    def assignable(self, target: ast.Name) -> None:
        if target.id in _RESERVED_NAMES or target.id.startswith("_"):
            raise _invalid(target, f"cannot assign to {target.id!r}")

    def loop(self, node: ast.For, loop_product: int) -> None:
        if node.orelse:
            raise _invalid(node, "for/else is not allowed")
        if not isinstance(node.target, ast.Name):
            raise _invalid(node, "loop variable must be a simple name")
        self.assignable(node.target)
        call = node.iter
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "range"
            and not call.keywords
            and 1 <= len(call.args) <= 3
        ):
            raise _invalid(node, "loops must be 'for <name> in range(<1-3 integer literals>)'")
        bounds = [_int_literal(arg) for arg in call.args]
        if len(bounds) == 3 and bounds[2] == 0:
            raise _invalid(node, "range() step must not be zero")
        count = len(range(*bounds))
        if count > MAX_LOOP_ITERS:
            raise _invalid(node, f"a loop may run at most {MAX_LOOP_ITERS} times")
        total = loop_product * max(count, 1)
        if total > MAX_TOTAL_LOOP_ITERS:
            raise _invalid(node, f"nested loops may run at most {MAX_TOTAL_LOOP_ITERS} times")
        self.block(node.body, total)

    def expression(self, node: ast.expr) -> None:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, str):
                if len(value) > MAX_STRING_CHARS:
                    raise _invalid(node, f"string literals are limited to {MAX_STRING_CHARS} characters")
            elif not (value is None or isinstance(value, (bool, int, float))):
                raise _invalid(node, f"literal of type {type(value).__name__} is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in PLAN_FUNCTIONS or node.id == "range":
                raise _invalid(node, f"{node.id} must be called")
        elif isinstance(node, ast.Call):
            self.call(node)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
                raise _invalid(node, f"operator {type(node.op).__name__} is not allowed")
            self.expression(node.operand)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                self.expression(value)
        elif isinstance(node, ast.Compare):
            if any(type(op) not in _COMPARE for op in node.ops):
                raise _invalid(node, "only == != < <= > >= comparisons are allowed")
            self.expression(node.left)
            for comparator in node.comparators:
                self.expression(comparator)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _ARITHMETIC:
                raise _invalid(node, "only + - * / arithmetic is allowed")
            self.expression(node.left)
            self.expression(node.right)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    if value.format_spec is not None or value.conversion != -1:
                        raise _invalid(node, "f-string format specs and conversions are not allowed")
                    self.expression(value.value)
                else:
                    self.expression(value)
        else:
            raise _invalid(node, f"expression {type(node).__name__} is not allowed")

    def call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in PLAN_FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise _invalid(node, f"{name} is not a plan function")
        signature = PLAN_FUNCTIONS[node.func.id]
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise _invalid(node, "*args are not allowed")
        if len(node.args) > len(signature.params):
            raise _invalid(node, f"{node.func.id} takes at most {len(signature.params)} arguments")
        given = set(signature.params[: len(node.args)])
        for keyword in node.keywords:
            if keyword.arg is None:
                raise _invalid(node, "**kwargs are not allowed")
            if keyword.arg not in signature.params:
                raise _invalid(node, f"{node.func.id} has no argument {keyword.arg!r}")
            if keyword.arg in given:
                raise _invalid(node, f"{node.func.id} got {keyword.arg!r} twice")
            given.add(keyword.arg)
        missing = [param for param in signature.params[: signature.required] if param not in given]
        if missing:
            raise _invalid(node, f"{node.func.id} is missing {', '.join(missing)}")
        for arg in node.args:
            self.expression(arg)
        for keyword in node.keywords:
            self.expression(keyword.value)


def _int_literal(node: ast.expr) -> int:
    value: Any = None
    if isinstance(node, ast.Constant):
        value = node.value
    elif (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        value = -node.operand.value
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid(node, "range() arguments must be integer literals")
    return value


def _invalid(node: ast.AST, message: str) -> PlanError:
    return PlanError(f"line {getattr(node, 'lineno', '?')}: {message}")


# --------------------------------------------------------------------------- runtime


@dataclass(slots=True)
class _PlanRun:
    plan_id: str
    module: ast.Module
    created_at: str
    deadline: float
    epoch: int
    status: str = "running"
    error: str | None = None
    current_task_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    statements: int = 0
    skill_calls: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)


class PlanRunner:
    """Run at most one validated plan at a time against a HorusExecutor."""

    def __init__(
        self,
        executor: Any,
        *,
        require_perception: Callable[[], Any] | None = None,
    ):
        self.executor = executor
        self._require_perception = require_perception
        self._lock = threading.Lock()
        self._runs: dict[str, _PlanRun] = {}
        self._active: str | None = None

    def submit(self, source: str, ttl_s: float | None = None) -> dict[str, Any]:
        module = validate_plan(source)
        ttl = DEFAULT_PLAN_TTL_S if ttl_s is None else float(ttl_s)
        if not math.isfinite(ttl) or ttl <= 0:
            raise PlanError("ttl_s must be a finite positive number")
        ttl = min(ttl, MAX_PLAN_TTL_S)
        with self._lock:
            if self._active is not None:
                raise RuntimeError(f"plan {self._active} is still running; cancel_plan it first")
            run = _PlanRun(
                plan_id=uuid.uuid4().hex[:12],
                module=module,
                created_at=datetime.now(timezone.utc).isoformat(),
                deadline=time.monotonic() + ttl,
                epoch=self.executor.emergency_epoch,
            )
            self._runs[run.plan_id] = run
            self._active = run.plan_id
            snapshot = self._snapshot(run)
        threading.Thread(
            target=self._execute, args=(run,), name=f"horus-plan-{run.plan_id}", daemon=True
        ).start()
        return snapshot

    def status(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(plan_id)
            if run is None:
                raise KeyError(f"unknown plan_id {plan_id!r}")
            return self._snapshot(run)

    def cancel(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(plan_id)
            if run is None:
                raise KeyError(f"unknown plan_id {plan_id!r}")
            run.cancel.set()
            return self._snapshot(run)

    def require_idle(self) -> None:
        """Direct skill tools call this: a running plan owns the executor queue."""
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    f"plan {self._active} is running and owns the skill queue; cancel_plan it "
                    "first (emergency hold/rth/land always work and abort the plan)"
                )

    @staticmethod
    def _snapshot(run: _PlanRun) -> dict[str, Any]:
        return {
            "plan_id": run.plan_id,
            "status": run.status,
            "created_at": run.created_at,
            "error": run.error,
            "current_task_id": run.current_task_id,
            "skill_calls": run.skill_calls,
            "steps": [dict(step) for step in run.steps],
            "logs": list(run.logs),
            "variables": dict(run.variables),
        }

    def _execute(self, run: _PlanRun) -> None:
        status, error = "succeeded", None
        interpreter = _Interpreter(run, self._functions(run), self._checkpoint)
        try:
            interpreter.block(run.module.body)
        except _PlanStopped as stop:
            status, error = stop.status, stop.reason
        except (_Break, _Continue):
            status, error = "failed", "break/continue outside a loop"
        except (_PlanFailed, ArithmeticError, TypeError, ValueError, KeyError) as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # a plan must never take the MCP server down
            logger.exception("plan %s crashed", run.plan_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        with self._lock:
            run.status = status
            run.error = error
            run.current_task_id = None
            run.variables = {
                name: value for name, value in interpreter.env.items()
                if value is None or isinstance(value, (bool, int, float, str))
            }
            self._active = None
        logger.info("plan %s %s%s", run.plan_id, status, f": {error}" if error else "")
        self.executor._publish_event(
            "plan_finished", f"plan {run.plan_id} {status}" + (f": {error}" if error else ""),
            plan_id=run.plan_id, status=status, error=error,
        )

    def _checkpoint(self, run: _PlanRun) -> None:
        """Called before every statement and while waiting; stops the plan when it must end."""
        if run.cancel.is_set():
            raise _PlanStopped("cancelled", "plan cancelled")
        if self.executor.emergency_epoch != run.epoch:
            raise _PlanStopped(
                "aborted", "an emergency, link-loss RTH or pilot override superseded the plan"
            )
        if time.monotonic() >= run.deadline:
            raise _PlanStopped("failed", "plan TTL expired")
        # The running plan is the onboard supervisor: it keeps link-loss RTH from firing during
        # its own waits and step changes; RTH applies again as soon as the plan ends.
        self.executor.note_supervisor_contact()

    # ------------------------------------------------------------------- plan functions

    def _functions(self, run: _PlanRun) -> dict[str, Callable[..., Any]]:
        def skill(name: str, arguments: dict[str, Any], ttl_s: Any = None) -> dict[str, Any]:
            return self._run_skill(run, name, arguments, ttl_s)

        def take_off(altitude_m=None, heading_deg=None, ttl_s=None):
            arguments = _present(altitude_m=_number(altitude_m), heading_deg=_number(heading_deg))
            return skill("take_off", arguments, ttl_s)["status"] == "succeeded"

        def goto(north_m, east_m, up_m, heading_deg=None, ttl_s=None):
            pose = {"north_m": _number(north_m), "east_m": _number(east_m), "up_m": _number(up_m)}
            arguments = _present(pose=pose, heading_deg=_number(heading_deg))
            return skill("goto", arguments, ttl_s)["status"] == "succeeded"

        def approach(id, dist_m=None, target_size_m=None, ttl_s=None):  # noqa: A002
            self._perception()
            arguments = _present(
                id=_tracker_id(id), dist_m=_number(dist_m), target_size_m=_number(target_size_m)
            )
            return skill("approach", arguments, ttl_s)["status"] == "succeeded"

        def timed(name: str, arguments: dict[str, Any], duration_s: Any) -> bool:
            # follow/orbit run until their TTL; expiring on time is their normal end. A run the
            # plan deadline cut short did not last the requested duration and reports False.
            duration = _duration(duration_s)
            fits = run.deadline - time.monotonic() >= duration
            return skill(name, arguments, duration)["status"] == "expired" and fits

        def follow(id, duration_s, dist_m=None, target_size_m=None):  # noqa: A002
            self._perception()
            arguments = _present(
                id=_tracker_id(id), dist_m=_number(dist_m), target_size_m=_number(target_size_m)
            )
            return timed("follow", arguments, duration_s)

        def orbit(id, duration_s, radius_m=None, target_size_m=None):  # noqa: A002
            self._perception()
            arguments = _present(
                id=_tracker_id(id), radius_m=_number(radius_m), target_size_m=_number(target_size_m)
            )
            return timed("orbit", arguments, duration_s)

        def scan(north_min_m, north_max_m, east_min_m, east_max_m, altitude_m=None,
                 spacing_m=None, ttl_s=None):
            area = _area(north_min_m, north_max_m, east_min_m, east_max_m, altitude_m, spacing_m)
            return skill("scan", {"area": area}, ttl_s)["status"] == "succeeded"

        def search(north_min_m, north_max_m, east_min_m, east_max_m, class_name=None,
                   attribute=None, equals=None, reference_id=None, altitude_m=None,
                   spacing_m=None, ttl_s=None):
            self._perception()
            area = _area(north_min_m, north_max_m, east_min_m, east_max_m, altitude_m, spacing_m)
            arguments = _present(
                area=area,
                class_name=_text(class_name),
                attribute=_text(attribute),
                equals=_text(equals),
                reference_id=None if reference_id is None else _tracker_id(reference_id),
            )
            result = skill("search", arguments, ttl_s)
            found = (result.get("tracking") or {}).get("found_id")
            return int(found) if result["status"] == "succeeded" and found is not None else -1

        def wait(seconds):
            requested = _number(seconds)
            if requested < 0:
                raise _PlanFailed("wait() needs a non-negative number of seconds")
            duration = min(requested, MAX_WAIT_S)
            if duration < requested:
                log(f"wait({requested:g}) clamped to {MAX_WAIT_S:g} s")
            end = time.monotonic() + duration
            while time.monotonic() < end:
                self._checkpoint(run)
                time.sleep(min(POLL_S, max(0.0, end - time.monotonic())))

        def log(message):
            line = str(message)[:MAX_STRING_CHARS]
            with self._lock:
                if len(run.logs) >= MAX_LOG_LINES:
                    raise _PlanFailed(f"plan logged more than {MAX_LOG_LINES} lines")
                run.logs.append(line)
            self.executor._publish_event("plan_log", line, plan_id=run.plan_id)

        def terminal(action: str) -> Callable[[], None]:
            def end() -> None:
                self._checkpoint(run)
                try:
                    getattr(self.executor, action)(source="plan")
                except RuntimeError as exc:
                    raise _PlanStopped("aborted", f"{action} refused: {exc}") from exc
                with self._lock:
                    run.steps.append({"call": action, "status": "requested"})
                raise _PlanStopped("succeeded", None)

            return end

        return {
            "take_off": take_off, "goto": goto, "approach": approach, "follow": follow,
            "orbit": orbit, "scan": scan, "search": search, "wait": wait, "log": log,
            "hold": terminal("hold"), "rth": terminal("rth"), "land": terminal("land"),
        }

    def _perception(self) -> None:
        if self._require_perception is not None:
            try:
                self._require_perception()
            except RuntimeError as exc:
                raise _PlanFailed(str(exc)) from exc

    def _run_skill(
        self, run: _PlanRun, name: str, arguments: dict[str, Any], ttl_s: Any
    ) -> dict[str, Any]:
        self._checkpoint(run)
        if run.skill_calls >= MAX_SKILL_CALLS:
            raise _PlanFailed(f"plan exceeded {MAX_SKILL_CALLS} skill calls")
        remaining = run.deadline - time.monotonic()
        ttl = self.executor.settings.command_ttl_s if ttl_s is None else _number(ttl_s)
        try:
            task = self.executor.submit_skill(
                name, arguments, min(ttl, remaining), expected_epoch=run.epoch
            )
        except ValueError as exc:
            raise _PlanFailed(f"{name}: {exc}") from exc
        except RuntimeError as exc:
            raise _PlanStopped("aborted", f"{name} refused: {exc}") from exc
        task_id = task["task_id"]
        step = {"call": name, "task_id": task_id, "status": task["status"]}
        with self._lock:
            run.skill_calls += 1
            run.current_task_id = task_id
            run.steps.append(step)
        while True:
            status = self.executor.get_task_status(task_id)
            with self._lock:
                step["status"] = status["status"]
                if status.get("error"):
                    step["error"] = status["error"]
            if status["status"] in TERMINAL_TASK_STATUSES:
                break
            try:
                self._checkpoint(run)
            except _PlanStopped:
                self._cancel_task(task_id)
                raise
            time.sleep(POLL_S)
        with self._lock:
            run.current_task_id = None
        if status["status"] == "cancelled":
            self._checkpoint(run)  # names the cause when it was cancel_plan or an emergency
            raise _PlanStopped("aborted", f"{name} task cancelled: {status.get('error')}")
        return status

    def _cancel_task(self, task_id: str) -> None:
        """Stop the plan's running step before reporting the plan stopped."""
        try:
            self.executor.cancel(task_id)
        except KeyError:
            return
        deadline = time.monotonic() + _CANCEL_GRACE_S
        while time.monotonic() < deadline:
            if self.executor.get_task_status(task_id)["status"] in TERMINAL_TASK_STATUSES:
                return
            time.sleep(POLL_S)


class _Interpreter:
    def __init__(
        self,
        run: _PlanRun,
        functions: Mapping[str, Callable[..., Any]],
        checkpoint: Callable[[_PlanRun], None],
    ):
        self.run = run
        self.functions = functions
        self.checkpoint = checkpoint
        self.env: dict[str, Any] = {}

    def block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.statement(statement)

    def statement(self, node: ast.stmt) -> None:
        self.checkpoint(self.run)
        self.run.statements += 1
        if self.run.statements > MAX_STATEMENTS:
            raise _PlanFailed(f"plan exceeded {MAX_STATEMENTS} statements")
        if isinstance(node, ast.Expr):
            self.evaluate(node.value)
        elif isinstance(node, ast.Assign):
            self.env[node.targets[0].id] = self.evaluate(node.value)
        elif isinstance(node, ast.If):
            self.block(node.body if self.truth(self.evaluate(node.test)) else node.orelse)
        elif isinstance(node, ast.For):
            for value in range(*(_int_literal(arg) for arg in node.iter.args)):
                self.env[node.target.id] = value
                try:
                    self.block(node.body)
                except _Continue:
                    continue
                except _Break:
                    break
        elif isinstance(node, ast.Break):
            raise _Break
        elif isinstance(node, ast.Continue):
            raise _Continue

    def evaluate(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.env:
                raise _PlanFailed(f"line {node.lineno}: name {node.id!r} is not defined")
            return self.env[node.id]
        if isinstance(node, ast.Call):
            args = [self.evaluate(arg) for arg in node.args]
            kwargs = {keyword.arg: self.evaluate(keyword.value) for keyword in node.keywords}
            return self.functions[node.func.id](*args, **kwargs)
        if isinstance(node, ast.UnaryOp):
            operand = self.evaluate(node.operand)
            if isinstance(node.op, ast.Not):
                return not self.truth(operand)
            value = _number(operand, f"line {node.lineno}: unary minus/plus")
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BoolOp):
            result: Any = None
            for value_node in node.values:
                result = self.evaluate(value_node)
                truth = self.truth(result)
                if isinstance(node.op, ast.And) and not truth:
                    return result
                if isinstance(node.op, ast.Or) and truth:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self.evaluate(node.left)
            for op, comparator_node in zip(node.ops, node.comparators):
                right = self.evaluate(comparator_node)
                try:
                    if not _COMPARE[type(op)](left, right):
                        return False
                except TypeError as exc:
                    raise _PlanFailed(f"line {node.lineno}: cannot compare {left!r} and {right!r}") from exc
                left = right
            return True
        if isinstance(node, ast.BinOp):
            where = f"line {node.lineno}: arithmetic"
            left = _number(self.evaluate(node.left), where)
            right = _number(self.evaluate(node.right), where)
            try:
                value = float(_ARITHMETIC[type(node.op)](left, right))
            except ZeroDivisionError as exc:
                raise _PlanFailed(f"line {node.lineno}: division by zero") from exc
            if not math.isfinite(value):
                raise _PlanFailed(f"line {node.lineno}: arithmetic result is not finite")
            return value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value_node in node.values:
                value = self.evaluate(
                    value_node.value if isinstance(value_node, ast.FormattedValue) else value_node
                )
                if not (value is None or isinstance(value, (bool, int, float, str))):
                    raise _PlanFailed(f"line {node.lineno}: cannot format {type(value).__name__}")
                parts.append(str(value))
            return "".join(parts)[:MAX_STRING_CHARS]
        raise _PlanFailed(f"expression {type(node).__name__} is not supported")

    @staticmethod
    def truth(value: Any) -> bool:
        if value is None or isinstance(value, (bool, int, float, str)):
            return bool(value)
        raise _PlanFailed(f"{type(value).__name__} has no truth value in a plan")


# --------------------------------------------------------------------------- argument coercion


def _number(value: Any, what: str = "argument") -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _PlanFailed(f"{what} must be a finite number, got {value!r}")
    return value


def _tracker_id(value: Any) -> int:
    number = _number(value, "tracker id")
    if number is None or number != int(number) or number < 1:
        raise _PlanFailed(f"tracker id must be a positive integer, got {value!r}")
    return int(number)


def _duration(value: Any) -> float:
    number = _number(value, "duration_s")
    if number is None or number <= 0:
        raise _PlanFailed(f"duration_s must be positive, got {value!r}")
    return float(number)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _PlanFailed(f"expected a string, got {value!r}")
    return value


def _area(north_min, north_max, east_min, east_max, altitude_m, spacing_m) -> dict[str, float]:
    area = {
        "north_min_m": _number(north_min), "north_max_m": _number(north_max),
        "east_min_m": _number(east_min), "east_max_m": _number(east_max),
    }
    if any(value is None for value in area.values()):
        raise _PlanFailed("area bounds must be numbers")
    return {**area, **_present(altitude_m=_number(altitude_m), spacing_m=_number(spacing_m))}


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
