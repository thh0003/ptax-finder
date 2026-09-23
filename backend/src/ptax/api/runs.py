"""/api/runs: start, list, inspect and cancel base-vs-target comparison runs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.auth.deps import CurrentUser, get_current_user, require_role
from ptax.db.models import ImageryYear, Parcel, Run, Tenant, UserRole
from ptax.db.session import get_db
from ptax.jobs.queue import enqueue

router = APIRouter(prefix="/runs")
admin_only = require_role(UserRole.admin)

# The most precision-favourable point the classical detector actually reaches: 30% of
# parcels flagged at 7.9% precision against a 6.0% base rate. It is above chance and it
# is NOT a usable review queue -- see backend/eval/README.md, which records that no
# (threshold, min structure) pair yields a small queue above the base rate.
DEFAULT_THRESHOLD = 0.3
# Smallest contiguous new structure worth reporting: 400 sq ft, the size of a
# one-car garage or a small addition.
DEFAULT_MIN_NEW_AREA_M2 = 37.2


class RunYearOut(BaseModel):
    id: uuid.UUID
    year: int
    source: str
    provider: str | None


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    base_year: RunYearOut
    target_year: RunYearOut
    threshold: float
    min_new_area_m2: float
    parcels_total: int
    parcels_processed: int
    candidates: int
    parcels_skipped: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunIn(BaseModel):
    base_year_id: uuid.UUID
    target_year_id: uuid.UUID
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0, le=1)
    min_new_area_m2: float = Field(default=DEFAULT_MIN_NEW_AREA_M2, ge=0)


def _year_out(year: ImageryYear) -> RunYearOut:
    return RunYearOut(id=year.id, year=year.year, source=year.source, provider=year.provider)


def _out(run: Run, years: dict[uuid.UUID, ImageryYear]) -> RunOut:
    return RunOut(
        id=run.id,
        status=run.status,
        base_year=_year_out(years[run.base_year_id]),
        target_year=_year_out(years[run.target_year_id]),
        threshold=run.threshold,
        min_new_area_m2=run.min_new_area_m2,
        parcels_total=run.parcels_total,
        parcels_processed=run.parcels_processed,
        candidates=run.candidates,
        parcels_skipped=run.parcels_skipped,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _years_for(db: Session, runs: list[Run]) -> dict[uuid.UUID, ImageryYear]:
    ids = {r.base_year_id for r in runs} | {r.target_year_id for r in runs}
    if not ids:
        return {}
    return {
        y.id: y for y in db.execute(select(ImageryYear).where(ImageryYear.id.in_(ids))).scalars()
    }


def _get_run(db: Session, user: CurrentUser, run_id: uuid.UUID) -> Run:
    run = db.get(Run, run_id)
    if run is None or run.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


def _ready_year(db: Session, user: CurrentUser, year_id: uuid.UUID, label: str) -> ImageryYear:
    year = db.get(ImageryYear, year_id)
    if year is None or year.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} year not found")
    if year.status != "ready":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label} year is not ready ({year.status})"
        )
    return year


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def create_run(
    body: RunIn,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> RunOut:
    """Queue a comparison of every current-layer parcel between two ready years."""
    base = _ready_year(db, user, body.base_year_id, "base")
    target = _ready_year(db, user, body.target_year_id, "target")
    if target.year <= base.year:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Target year must be later than base year"
        )
    tenant = db.get_one(Tenant, user.tenant_id)
    if tenant.current_parcel_layer_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no parcel layer")
    total = db.execute(
        select(func.count())
        .select_from(Parcel)
        .where(Parcel.layer_id == tenant.current_parcel_layer_id)
    ).scalar_one()
    run = Run(
        tenant_id=user.tenant_id,
        layer_id=tenant.current_parcel_layer_id,
        base_year_id=base.id,
        target_year_id=target.id,
        status="queued",
        threshold=body.threshold,
        min_new_area_m2=body.min_new_area_m2,
        parcels_total=total,
        created_by=user.id,
    )
    db.add(run)
    db.flush()
    enqueue(db, "run.execute", user.tenant_id, {"run_id": str(run.id)})
    db.commit()
    db.refresh(run)
    return _out(run, {base.id: base, target.id: target})


@router.get("", response_model=list[RunOut])
def list_runs(
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[RunOut]:
    runs = (
        db.execute(
            select(Run).where(Run.tenant_id == user.tenant_id).order_by(Run.created_at.desc())
        )
        .scalars()
        .all()
    )
    years = _years_for(db, runs)
    return [_out(r, years) for r in runs]


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    run_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunOut:
    run = _get_run(db, user, run_id)
    return _out(run, _years_for(db, [run]))


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    run_id: uuid.UUID,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> RunOut:
    """Stop a queued run before it starts, or a running one at its next batch boundary."""
    run = _get_run(db, user, run_id)
    if run.status not in ("queued", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is {run.status}")
    run.status = "cancelled"
    db.commit()
    db.refresh(run)
    return _out(run, _years_for(db, [run]))
