from __future__ import annotations

from typing import Any

import pytest

from hgc.db.memory import InMemoryRunRepository
from hgc.domain.errors import PhreeqcTimeoutError
from hgc.domain.models import RunRequest, RunStatus
from hgc.services.phreeqc.engine import RawPhreeqcOutput
from hgc.services.runs import RunService, compute_input_hash


class FakeEngine:
    """Stands in for IPhreeqc so the orchestration logic is testable anywhere."""

    engine_version = "fake/1.0"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls = 0
        self._fail = fail_with

    def database_checksum(self, name: str) -> str:
        return "deadbeef"

    def run(self, input_text: str, database: str, timeout_s: float | None = None) -> Any:
        self.calls += 1
        if self._fail:
            raise self._fail
        return RawPhreeqcOutput(
            selected_output=[["pH", "si_Calcite"], [7.4, 0.31]],
            duration_ms=12,
            database=database,
            database_sha256="deadbeef",
            engine_version=self.engine_version,
        )


def service(engine: Any) -> RunService:
    return RunService(engine=engine, repository=InMemoryRunRepository())


def test_identical_submissions_execute_once(sample, spec):
    engine = FakeEngine()
    svc = service(engine)
    first = svc.submit(RunRequest(sample=sample, spec=spec))
    second = svc.submit(RunRequest(sample=sample, spec=spec))

    assert first.status is RunStatus.succeeded
    assert second.id == first.id
    assert engine.calls == 1


def test_force_bypasses_the_idempotency_cache(sample, spec):
    engine = FakeEngine()
    svc = service(engine)
    svc.submit(RunRequest(sample=sample, spec=spec))
    svc.submit(RunRequest(sample=sample, spec=spec, force=True))
    assert engine.calls == 2


def test_timeout_is_recorded_against_the_run_not_lost(sample, spec):
    svc = service(FakeEngine(fail_with=PhreeqcTimeoutError("too slow")))
    run = svc.submit(RunRequest(sample=sample, spec=spec))
    assert run.status is RunStatus.failed
    assert run.error_code == "phreeqc_timeout"
    assert run.input_text  # the input survives the failure, so it can be reproduced


def test_hash_changes_with_the_thermodynamic_database():
    a = compute_input_hash("SOLUTION 1\nEND", "phreeqc.dat", "v1", "sha-a")
    b = compute_input_hash("SOLUTION 1\nEND", "llnl.dat", "v1", "sha-a")
    c = compute_input_hash("SOLUTION 1\nEND", "phreeqc.dat", "v1", "sha-b")
    assert a != b != c and a != c


def test_slow_keywords_are_routed_to_the_queue(sample, spec):
    class Queue:
        def __init__(self) -> None:
            self.enqueued: list[Any] = []

        def enqueue_run(self, run_id: Any) -> None:
            self.enqueued.append(run_id)

    queue = Queue()
    svc = RunService(engine=FakeEngine(), repository=InMemoryRunRepository(), queue=queue)
    run = svc.submit(RunRequest(raw_input="KINETICS 1\n    Calcite\nEND\n", spec=spec))
    assert run.status is RunStatus.queued
    assert queue.enqueued == [run.id]


def test_unsafe_custom_input_never_reaches_the_engine(spec):
    engine = FakeEngine()
    svc = service(engine)
    with pytest.raises(Exception):
        svc.submit(RunRequest(raw_input="DUMP\n    -file /tmp/x\nEND\n", spec=spec))
    assert engine.calls == 0
