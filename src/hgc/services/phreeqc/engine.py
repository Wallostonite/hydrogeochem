"""Process-isolated PHREEQC execution.

Three properties of IPhreeqc force this design:

1. It is a stateful C library and is not thread-safe. Two threads sharing one instance
   corrupt each other's solutions; two instances in one process still contend on library
   globals in some builds. So: one instance per *process*, never shared across threads.
2. A running `run_string` call cannot be interrupted from Python. A timeout therefore has
   to be enforced by killing the child, which means the executor must be disposable.
3. Loading a thermodynamic database costs 100-400 ms (llnl.dat is the worst). Children
   cache one instance per database and are recycled after N tasks to bound C-side leaks.
"""

from __future__ import annotations

import contextlib
import hashlib
import multiprocessing as mp
import os
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

try:  # `resource` is Unix-only. On Windows the sandbox limits are unavailable, so the
    # sanitizer and container isolation carry the load instead.
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

from ...domain.errors import (
    EngineUnavailableError,
    PhreeqcError,
    PhreeqcTimeoutError,
    ValidationError,
)
from ...logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- child side


_instances: dict[str, Any] = {}
_child_limits_applied = False


def _apply_child_limits(memory_mb: int, cpu_seconds: int) -> None:
    """Defence in depth: even if the sanitizer misses a keyword, the child cannot write
    a file, cannot allocate the box to death, and cannot spin forever."""
    global _child_limits_applied
    if _child_limits_applied or resource is None:
        return
    soft_mem = memory_mb * 1024 * 1024
    for limit, value in (
        (resource.RLIMIT_AS, soft_mem),
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 256),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError) as exc:  # pragma: no cover - platform dependent
            log.warning("rlimit_not_applied", extra={"limit": limit, "error": str(exc)})
    _child_limits_applied = True


def _child_init(memory_mb: int, cpu_seconds: int) -> None:  # pragma: no cover - subprocess
    _apply_child_limits(memory_mb, cpu_seconds)


def _get_instance(database_path: str) -> Any:  # pragma: no cover - subprocess
    instance = _instances.get(database_path)
    if instance is None:
        from phreeqpy.iphreeqc.phreeqc_dll import IPhreeqc

        instance = IPhreeqc()
        instance.load_database(database_path)
        _instances[database_path] = instance
    return instance


def _child_run(input_text: str, database_path: str) -> dict[str, Any]:  # pragma: no cover
    started = time.perf_counter()
    instance = _get_instance(database_path)
    error = ""
    try:
        instance.run_string(input_text)
    except Exception as exc:  # phreeqpy raises on non-zero return
        error = f"{exc}"
    with contextlib.suppress(Exception):
        error = error or instance.get_error_string()
    try:
        warning = instance.get_warning_string()
    except Exception:
        warning = ""
    try:
        selected = instance.get_selected_output_array() if not error else []
    except Exception:
        selected = []
    return {
        "selected_output": selected,
        "error": error.strip(),
        "warning": warning.strip(),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "pid": os.getpid(),
    }


# -------------------------------------------------------------------------- parent side


@dataclass(slots=True)
class RawPhreeqcOutput:
    selected_output: list[list[Any]]
    duration_ms: int
    database: str
    database_sha256: str
    engine_version: str
    warnings: list[str] = field(default_factory=list)


class PhreeqcEngine:
    """Owns the worker pool. One instance per API/worker process; thread-safe to call."""

    def __init__(
        self,
        database_dir: Path,
        allowed_databases: tuple[str, ...],
        workers: int = 4,
        timeout_s: float = 20.0,
        max_tasks_per_child: int = 200,
        child_memory_mb: int = 1024,
    ) -> None:
        self._dir = Path(database_dir)
        self._allowed = set(allowed_databases)
        self._workers = workers
        self._timeout_s = timeout_s
        self._max_tasks_per_child = max_tasks_per_child
        self._child_memory_mb = child_memory_mb
        self._ctx = mp.get_context("spawn")
        self._lock = Lock()
        self._pool: ProcessPoolExecutor | None = None
        self._checksums: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------------

    def _new_pool(self) -> ProcessPoolExecutor:
        kwargs: dict[str, Any] = {
            "max_workers": self._workers,
            "mp_context": self._ctx,
            "initializer": _child_init,
            "initargs": (self._child_memory_mb, int(self._timeout_s * 2) + 5),
        }
        try:  # max_tasks_per_child bounds C-side leaks; 3.11+ with a non-fork context
            return ProcessPoolExecutor(max_tasks_per_child=self._max_tasks_per_child, **kwargs)
        except TypeError:  # pragma: no cover - older interpreters
            return ProcessPoolExecutor(**kwargs)

    def start(self) -> None:
        with self._lock:
            if self._pool is None:
                self._pool = self._new_pool()
                log.info("phreeqc_pool_started", extra={"workers": self._workers})

    def shutdown(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None

    def _recycle(self, reason: str) -> None:
        """A hung child cannot be interrupted, so the whole pool is replaced.

        Expensive and rare. `hgc_phreeqc_pool_recycles_total` is alerted on: a rising rate
        means some class of input reliably hangs the engine and needs a guard rail.
        """
        log.warning("phreeqc_pool_recycled", extra={"reason": reason})
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = self._new_pool()

    # -- databases ---------------------------------------------------------------

    def resolve_database(self, name: str) -> Path:
        if name not in self._allowed:
            raise ValidationError(
                f"database {name!r} is not available",
                allowed=sorted(self._allowed),
            )
        path = self._dir / name
        if not path.is_file():
            raise EngineUnavailableError(f"database {name!r} is not installed on this node")
        return path

    def database_checksum(self, name: str) -> str:
        """A saturation index is only reproducible alongside the exact database that produced it."""
        if name not in self._checksums:
            digest = hashlib.sha256(self.resolve_database(name).read_bytes()).hexdigest()
            self._checksums[name] = digest
        return self._checksums[name]

    def verify_databases(self) -> dict[str, str]:
        """Called at startup and by /readyz. Missing databases must fail the pod, not a request."""
        found: dict[str, str] = {}
        for name in sorted(self._allowed):
            path = self._dir / name
            if path.is_file():
                found[name] = self.database_checksum(name)
        if not found:
            raise EngineUnavailableError(f"no PHREEQC databases found in {self._dir}")
        return found

    @property
    def engine_version(self) -> str:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return f"phreeqpy/{version('phreeqpy')}"
        except PackageNotFoundError:  # pragma: no cover - source checkout
            return "phreeqpy/unknown"

    # -- execution ---------------------------------------------------------------

    def run(
        self, input_text: str, database: str, timeout_s: float | None = None
    ) -> RawPhreeqcOutput:
        path = self.resolve_database(database)
        deadline = timeout_s or self._timeout_s
        self.start()
        assert self._pool is not None

        started = time.perf_counter()
        try:
            future = self._pool.submit(_child_run, input_text, str(path))
            payload = future.result(timeout=deadline)
        except FutureTimeout:
            self._recycle("timeout")
            raise PhreeqcTimeoutError(
                f"PHREEQC did not finish within {deadline:.0f}s",
                timeout_s=deadline,
            ) from None
        except BrokenExecutor as exc:
            self._recycle("broken_pool")
            raise EngineUnavailableError("PHREEQC worker pool crashed; retry shortly") from exc

        if payload["error"]:
            raise PhreeqcError(_first_useful_line(payload["error"]), phreeqc_error=payload["error"])

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RawPhreeqcOutput(
            selected_output=payload["selected_output"],
            duration_ms=payload.get("duration_ms", elapsed_ms),
            database=database,
            database_sha256=self.database_checksum(database),
            engine_version=self.engine_version,
            warnings=[w for w in payload["warning"].splitlines() if w.strip()],
        )


def _first_useful_line(error_text: str) -> str:
    """PHREEQC error strings are verbose; surface the first actionable line, keep the rest."""
    for line in error_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("-"):
            return stripped[:400]
    return error_text.strip()[:400] or "PHREEQC reported an unspecified error"
