import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ptax import worker
from ptax.config import Settings
from ptax.db.models import Job
from ptax.db.session import get_engine
from ptax.jobs import registry
from ptax.jobs.queue import MAX_ATTEMPTS, JobInterrupted, RetryableError, enqueue
from ptax.worker import run_once, tick


@pytest.fixture
def committed_session(settings: Settings) -> Iterator[Session]:
    """A real committing session (SKIP LOCKED needs separate connections), cleaned up after."""
    engine = get_engine(settings.database_url)
    with Session(engine) as session:
        yield session
        session.rollback()
        session.execute(delete(Job).where(Job.type.like("test.%")))
        session.commit()


def _drain(settings: Settings, seen: list[uuid.UUID], lock: threading.Lock) -> None:
    engine = get_engine(settings.database_url)
    while True:
        with Session(engine) as session:
            job = run_once(session)
        if job is None:
            return
        with lock:
            seen.append(job.id)


def test_concurrent_workers_process_each_job_exactly_once(
    settings: Settings, committed_session: Session
) -> None:
    job_type = f"test.noop.{uuid.uuid4().hex[:8]}"
    handled: list[uuid.UUID] = []
    handled_lock = threading.Lock()

    @registry.job_handler(job_type)
    def _noop(db: Session, job: Job) -> None:
        with handled_lock:
            handled.append(job.id)

    ids = [enqueue(committed_session, job_type, None, {"n": i}).id for i in range(50)]
    committed_session.commit()

    seen: list[uuid.UUID] = []
    seen_lock = threading.Lock()
    workers = [threading.Thread(target=_drain, args=(settings, seen, seen_lock)) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)

    assert sorted(handled) == sorted(ids)
    assert sorted(seen) == sorted(ids)
    statuses = committed_session.execute(select(Job.status).where(Job.id.in_(ids))).scalars()
    assert set(statuses) == {"succeeded"}


def test_failing_handler_marks_job_failed_with_error(
    settings: Settings, committed_session: Session
) -> None:
    job_type = f"test.boom.{uuid.uuid4().hex[:8]}"

    @registry.job_handler(job_type)
    def _boom(db: Session, job: Job) -> None:
        raise RuntimeError("kaboom: the disk is on fire")

    job = enqueue(committed_session, job_type, None, {})
    committed_session.commit()

    with Session(get_engine(settings.database_url)) as session:
        assert run_once(session) is not None

    committed_session.refresh(job)
    assert job.status == "failed"
    assert job.attempts == 1
    assert "kaboom" in (job.error or "")
    assert job.finished_at is not None


def test_retryable_error_requeues_until_max_attempts(
    settings: Settings, committed_session: Session
) -> None:
    job_type = f"test.flaky.{uuid.uuid4().hex[:8]}"

    @registry.job_handler(job_type)
    def _flaky(db: Session, job: Job) -> None:
        raise RetryableError("upstream busy")

    job = enqueue(committed_session, job_type, None, {})
    committed_session.commit()
    engine = get_engine(settings.database_url)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        with Session(engine) as session:
            assert run_once(session) is not None
        committed_session.refresh(job)
        assert job.attempts == attempt
        assert job.status == ("queued" if attempt < MAX_ATTEMPTS else "failed")

    with Session(engine) as session:
        assert run_once(session) is None  # nothing left to claim


def test_interrupted_job_is_requeued_without_charging_an_attempt(
    settings: Settings, committed_session: Session
) -> None:
    """A graceful worker stop re-queues the job; five interruptions still end in success."""
    job_type = f"test.interrupted.{uuid.uuid4().hex[:8]}"
    calls = {"n": 0}

    @registry.job_handler(job_type)
    def _interruptible(db: Session, job: Job) -> None:
        calls["n"] += 1
        if calls["n"] <= 5:
            raise JobInterrupted("worker stopping")

    job = enqueue(committed_session, job_type, None, {})
    committed_session.commit()
    engine = get_engine(settings.database_url)

    for _ in range(5):
        with Session(engine) as session:
            assert run_once(session) is not None
        committed_session.refresh(job)
        assert job.status == "queued"
        assert job.attempts == 0
        assert "interrupted" in (job.error or "")

    with Session(engine) as session:
        assert run_once(session) is not None
    committed_session.refresh(job)
    assert job.status == "succeeded"
    assert job.attempts == 1


def test_stop_requested_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_stop", False)  # restored to False after the test
    assert worker.stop_requested() is False
    worker._request_stop(15, None)
    assert worker.stop_requested() is True


def test_worker_tick_survives_database_errors(settings: Settings) -> None:
    """Schema not migrated yet / Aurora resuming: the loop must log and retry, not exit."""
    no_schema_url = make_url(settings.database_url).set(database="postgres")
    engine = get_engine(no_schema_url.render_as_string(hide_password=False))
    assert tick(engine) is None  # "relation jobs does not exist" is swallowed and logged


def test_unknown_job_type_fails_cleanly(settings: Settings, committed_session: Session) -> None:
    job = enqueue(committed_session, "test.unregistered", None, {})
    committed_session.commit()
    with Session(get_engine(settings.database_url)) as session:
        run_once(session)
    committed_session.refresh(job)
    assert job.status == "failed"
    assert "no handler" in (job.error or "")
