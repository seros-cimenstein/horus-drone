"""Cross-platform child process supervision with orphan prevention (Win32 Job Objects / POSIX process groups)."""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _create_windows_kill_on_close_job() -> int | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception as exc:
        logger.warning("Could not initialize Win32 Job Object: %s", exc)
        return None


class ManagedProcess:
    """One supervised subprocess bound to a process group (POSIX) or Job Object (Windows)."""

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
        job_handle: int | None = None,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.cwd = None if cwd is None else os.fspath(cwd)
        self.env = None if env is None else dict(env)
        self.log_path = None if log_path is None else os.fspath(log_path)
        self._job_handle = job_handle
        self._process: subprocess.Popen[bytes] | None = None
        self._returncode: int | None = None
        self._log_file: Any = None

    def start(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        stdout_target: Any = subprocess.DEVNULL
        if self.log_path is not None:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            self._log_file = open(self.log_path, "wb")
            stdout_target = self._log_file

        kwargs: dict[str, Any] = {
            "cwd": self.cwd,
            "env": self.env,
            "stdout": stdout_target,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        proc = subprocess.Popen(self.argv, **kwargs)
        self._process = proc
        if os.name == "nt" and self._job_handle:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(self._job_handle, int(proc._handle))  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("AssignProcessToJobObject failed for %s: %s", self.name, exc)
        return proc

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def poll(self) -> int | None:
        if self._process is None:
            return self._returncode
        code = self._process.poll()
        if code is not None:
            self._returncode = code
        return code

    def stop(self, timeout_s: float = 4.0) -> None:
        proc, self._process = self._process, None
        if proc is not None:
            if proc.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            proc.kill()
                    else:
                        proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
            self._returncode = proc.poll()
        handle, self._log_file = self._log_file, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


class ProcessGroup:
    """Manages ordered startup/teardown of child processes with zero-orphan guarantees."""

    def __init__(self) -> None:
        self._processes: list[ManagedProcess] = []
        self._job_handle: int | None = _create_windows_kill_on_close_job()

    def spawn(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
    ) -> ManagedProcess:
        proc = ManagedProcess(
            name,
            argv,
            cwd=cwd,
            env=env,
            log_path=log_path,
            job_handle=self._job_handle,
        )
        proc.start()
        self._processes.append(proc)
        return proc

    def processes(self) -> tuple[ManagedProcess, ...]:
        return tuple(self._processes)

    def stop_all(self, timeout_s: float = 4.0) -> None:
        """Stop processes in reverse start order (Drone board -> SITL -> Test world)."""
        for proc in reversed(self._processes):
            try:
                proc.stop(timeout_s=timeout_s)
            except Exception as exc:
                logger.warning("Error stopping child %s: %s", proc.name, exc)
        self._processes.clear()

    def close(self) -> None:
        self.stop_all()
        if os.name == "nt" and self._job_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._job_handle)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._job_handle = None
