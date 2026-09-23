"""``python -m ptax.worker``: drain the job queue until told to stop."""

import logging
import signal
import time
import traceback

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ptax.config import get_settings
from ptax.db.models import Job
from ptax.db.session import get_engine
from ptax.jobs import registry
from ptax.jobs.queue import (
    JobInterrupted,
    RetryableError,
    claim_next,
    mark_failed,
    mark_succeeded,
    requeue,
)

log = logging.getLogger("ptax.worker")

IDLE_SLEEP_SECONDS = 2.0

# Set by SIGTERM/SIGINT. Long handlers poll stop_requested() at their commit boundaries
# and raise JobInterrupted so the job is re-queued instead of being killed mid-flight.
_stop = False


def stop_requested() -> bool:
    return _stop


def _request_stop(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    log.info("signal %s received; finishing current job", signum)


def run_once(db: Session) -> Job | None:
    """Claim and run one job. Returns the job (in its final state) or None if the queue is empty."""
    job = claim_next(db)
    if job is None:
        return None

    handler = registry.get_handler(job.type)
    started = time.monotonic()
    if handler is None:
        mark_failed(db, job, f"no handler registered for job type {job.type!r}", retry=False)
        log.error("job %s type=%s has no handler", job.id, job.type)
        return job

    try:
        handler(db, job)
    except JobInterrupted as exc:
        db.rollback()
        requeue(db, job, str(exc))
        log.info("job %s type=%s interrupted, re-queued: %s", job.id, job.type, exc)
    except RetryableError as exc:
        db.rollback()
        mark_failed(db, job, f"retryable: {exc}", retry=True)
        log.warning("job %s type=%s retryable failure: %s", job.id, job.type, exc)
    except Exception as exc:  # noqa: BLE001 - any handler failure must be recorded, not crash the loop
        db.rollback()
        last_line = traceback.format_exc().strip().splitlines()[-1]
        mark_failed(db, job, f"{type(exc).__name__}: {last_line}", retry=False)
        log.exception("job %s type=%s failed", job.id, job.type)
    else:
        mark_succeeded(db, job)
        log.info("job %s type=%s ok in %.1fs", job.id, job.type, time.monotonic() - started)
    return job


def tick(engine: Engine) -> Job | None:
    """One poll with its own session; database errors are logged, never fatal.

    The schema may not be migrated yet (API task still starting) or Aurora may be resuming
    from pause — the loop just tries again after the idle sleep.
    """
    try:
        with Session(engine) as db:
            return run_once(db)
    except SQLAlchemyError as exc:
        log.warning("database unavailable, retrying: %s", str(exc).splitlines()[0])
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Register handlers by importing the modules that define them.
    import ptax.detection.run  # noqa: F401
    import ptax.imagery.ingest  # noqa: F401
    import ptax.parcels.ingest  # noqa: F401

    settings = get_settings()
    engine = get_engine(settings.database_url)

    # Fargate sends SIGTERM and waits (30 s by default) before SIGKILL.
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    log.info("worker started")
    while not stop_requested():
        if tick(engine) is None:
            time.sleep(IDLE_SLEEP_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
