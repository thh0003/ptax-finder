"""Postgres-backed job queue.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so any number of worker processes can drain
the same table without handing a job to two of them. This scales with the database
rather than a broker, which is plenty for per-county batch work.
"""

import uuid
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ptax.db.models import Job

MAX_ATTEMPTS = 3


class RetryableError(Exception):
    """Raised by a handler to ask for the job to be re-queued (up to MAX_ATTEMPTS)."""


class JobInterrupted(Exception):
    """Raised by a long handler at a commit boundary when the worker is stopping.

    The job is re-queued without charging an attempt: a graceful restart is not a
    failure, and a multi-hour run must survive any number of deploys.
    """


def enqueue(
    db: Session, job_type: str, tenant_id: uuid.UUID | None, payload: dict[str, Any]
) -> Job:
    job = Job(type=job_type, tenant_id=tenant_id, payload=payload, status="queued")
    db.add(job)
    db.flush()
    return job


_CLAIM_SQL = text(
    """
    UPDATE jobs
       SET status = 'running', started_at = now(), attempts = attempts + 1
     WHERE id = (
           SELECT id FROM jobs
            WHERE status = 'queued'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
     )
 RETURNING id
    """
)


def claim_next(db: Session) -> Job | None:
    """Atomically move the oldest queued job to ``running`` and return it (committed)."""
    job_id = db.execute(_CLAIM_SQL).scalar_one_or_none()
    db.commit()
    if job_id is None:
        return None
    return db.get_one(Job, job_id)


def mark_succeeded(db: Session, job: Job) -> None:
    job.status = "succeeded"
    job.error = None
    job.finished_at = func.now()
    db.commit()


def requeue(db: Session, job: Job, reason: str) -> None:
    """Put an interrupted job back in the queue; the interrupted attempt is not counted."""
    job.status = "queued"
    job.error = f"interrupted: {reason}"[-2000:]
    job.attempts = max(job.attempts - 1, 0)
    job.finished_at = None
    db.commit()


def mark_failed(db: Session, job: Job, error: str, *, retry: bool) -> None:
    """Record the failure; re-queue when retryable and attempts remain."""
    job.error = error[-2000:]
    if retry and job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        job.finished_at = None
    else:
        job.status = "failed"
        job.finished_at = func.now()
    db.commit()
