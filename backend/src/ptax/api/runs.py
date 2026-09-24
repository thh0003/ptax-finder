"""/api/runs: start, list, inspect and cancel base-vs-target comparison runs."""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely.ops import transform as shapely_transform
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.api.imagery import OUTLINE_PX, OUTLINE_RGB, PNG_HEADERS
from ptax.auth.deps import CurrentUser, get_current_user, require_role
from ptax.config import Settings
from ptax.db.models import ImageryYear, Parcel, Run, RunParcel, Tenant, UserRole
from ptax.db.session import get_db
from ptax.detection.model_store import published_card
from ptax.imagery.preview import (
    NEW_BUILTUP_RGB,
    STRUCTURE_RGB,
    paint,
    plan_view,
    render_png,
)
from ptax.imagery.reader import assets_intersecting, read_parcel
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

DetectorName = Literal["classical", "segmentation"]
#: The segmenter passed its decision gate (backend/eval/README.md) and is the default.
#: The classical detector stays selectable as the fallback and the comparison.
DEFAULT_DETECTOR: DetectorName = "segmentation"


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
    detector: DetectorName
    #: The segmenter model the run was given; None for a classical run.
    model_name: str | None
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
    detector: DetectorName = DEFAULT_DETECTOR


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
        detector=run.detector,  # type: ignore[arg-type]  # the CHECK constraint holds it
        model_name=run.model_name,
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


class RunRefused(Exception):
    """A run that cannot be queued as asked; ``status_code`` is the HTTP answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def queue_run(
    db: Session,
    settings: Settings,
    *,
    tenant: Tenant,
    created_by: uuid.UUID,
    base: ImageryYear,
    target: ImageryYear,
    detector: DetectorName,
    threshold: float = DEFAULT_THRESHOLD,
    min_new_area_m2: float = DEFAULT_MIN_NEW_AREA_M2,
) -> Run:
    """Create a run and its job, uncommitted. Shared by the API and `ptax-admin start-run`,
    so the two cannot disagree about what a valid run is.

    A segmenter run records the model named by ``settings.segmenter_model`` and its hash
    now, from the published card: the worker later loads exactly that, or refuses. With
    no published card the run is refused here rather than failing on the worker.
    """
    if target.year <= base.year:
        raise RunRefused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Target year must be later than base year"
        )
    if tenant.current_parcel_layer_id is None:
        raise RunRefused(status.HTTP_409_CONFLICT, "no parcel layer")
    model_name = model_sha256 = None
    if detector == "segmentation":
        card = published_card(settings, settings.segmenter_model)
        if card is None:
            raise RunRefused(
                status.HTTP_409_CONFLICT,
                f"segmenter model {settings.segmenter_model} is not published",
            )
        model_name, model_sha256 = settings.segmenter_model, str(card["weights_sha256"])
    total = db.execute(
        select(func.count())
        .select_from(Parcel)
        .where(Parcel.layer_id == tenant.current_parcel_layer_id)
    ).scalar_one()
    run = Run(
        tenant_id=tenant.id,
        layer_id=tenant.current_parcel_layer_id,
        base_year_id=base.id,
        target_year_id=target.id,
        status="queued",
        threshold=threshold,
        min_new_area_m2=min_new_area_m2,
        detector=detector,
        model_name=model_name,
        model_sha256=model_sha256,
        parcels_total=total,
        created_by=created_by,
    )
    db.add(run)
    db.flush()
    enqueue(db, "run.execute", tenant.id, {"run_id": str(run.id)})
    return run


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def create_run(
    body: RunIn,
    request: Request,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> RunOut:
    """Queue a comparison of every current-layer parcel between two ready years."""
    base = _ready_year(db, user, body.base_year_id, "base")
    target = _ready_year(db, user, body.target_year_id, "target")
    try:
        run = queue_run(
            db,
            request.app.state.settings,
            tenant=db.get_one(Tenant, user.tenant_id),
            created_by=user.id,
            base=base,
            target=target,
            detector=body.detector,
            threshold=body.threshold,
            min_new_area_m2=body.min_new_area_m2,
        )
    except RunRefused as exc:
        # Refused before anything was added, so there is nothing to roll back.
        raise HTTPException(exc.status_code, str(exc)) from exc
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


# --- Reading a run's per-parcel results --------------------------------------------------

#: Bounded so one request cannot ask for a county. The viewer pages.
MAX_PARCEL_PAGE = 200


class RunParcelOut(BaseModel):
    parcel_id: uuid.UUID
    parcel_ref: str
    score: float | None
    candidate: bool
    skipped_reason: str | None
    #: Whether this run recorded where it found the change. False for a parcel that
    #: detected nothing, was skipped, or was scored before the markup existed -- all
    #: three render no markup, and the viewer needs to tell that from "not loaded yet".
    has_markup: bool


class RunParcelDetailOut(RunParcelOut):
    indicators: dict[str, Any] | None


def _parcel_out(row: RunParcel) -> RunParcelOut:
    return RunParcelOut(
        parcel_id=row.parcel_id,
        parcel_ref=row.parcel_ref,
        score=row.score,
        candidate=row.candidate,
        skipped_reason=row.skipped_reason,
        has_markup=row.new_builtup_geom is not None or row.structure_geom is not None,
    )


class RunParcelPage(BaseModel):
    items: list[RunParcelOut]
    total: int


@router.get("/{run_id}/parcels", response_model=RunParcelPage)
def list_run_parcels(
    run_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=MAX_PARCEL_PAGE),
    offset: int = Query(0, ge=0),
    candidate: bool | None = Query(None, description="restrict to flagged parcels"),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunParcelPage:
    """A run's parcels, highest score first.

    Skipped parcels have a NULL score and sort last -- "not scored" is not the best
    result in the run. `parcel_ref` breaks ties so paging cannot show one parcel twice
    and drop another; without a total order, two pages of an unordered tie group overlap.
    """
    run = _get_run(db, user, run_id)
    where = [RunParcel.run_id == run.id]
    if candidate is not None:
        where.append(RunParcel.candidate.is_(candidate))

    total = db.execute(
        select(func.count()).select_from(RunParcel).where(*where)
    ).scalar_one()
    rows = db.execute(
        select(RunParcel)
        .where(*where)
        .order_by(RunParcel.score.desc().nullslast(), RunParcel.parcel_ref)
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return RunParcelPage(items=[_parcel_out(r) for r in rows], total=total)


@router.get("/{run_id}/parcels/{parcel_id}", response_model=RunParcelDetailOut)
def get_run_parcel(
    run_id: uuid.UUID,
    parcel_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunParcelDetailOut:
    """One parcel's stored result: the score, the measurements behind it, and whether
    this run recorded a markup to draw."""
    run = _get_run(db, user, run_id)
    row = db.get(RunParcel, (run.id, parcel_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel not in this run")
    return RunParcelDetailOut(**_parcel_out(row).model_dump(), indicators=row.indicators)


# --- The marked-up target image ---------------------------------------------------------
#
# Three colours, mutually distinguishable (and distinct from the boundary's yellow): the
# structure the score rests on, the rest of what the run detected, and the parcel outline.
# Drawn in one colour the view still renders, and silently loses the distinction between
# what produced the score and what did not. The colours live in `ptax.imagery.preview` so
# the evaluation chips draw the same markup the same way.


@router.get("/{run_id}/parcels/{parcel_id}/overlay.png", response_class=Response)
def parcel_overlay(
    run_id: uuid.UUID,
    parcel_id: uuid.UUID,
    request: Request,
    # ⛔ These three must stay identical in name, type, range and default to
    # `parcel_preview` (ptax/api/imagery.py). The shared `plan_view` only guarantees the
    # same ground for the same inputs, and this is a separate HTTP call -- a differing
    # default here misregisters the markup while every other test still passes.
    size: int = Query(512, ge=64, le=2048),
    buffer: float = Query(0.25, ge=0, le=2, description="context as a fraction of the parcel"),
    outline: bool = Query(False, description="draw the parcel boundary"),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """The run's target year for one parcel, with the area it detected drawn on it.

    The markup comes from what the run *recorded* when it scored the parcel, never from
    re-running detection: a reassessment that gets challenged has to show the picture the
    decision rested on, and the detector changes underneath.
    """
    run = _get_run(db, user, run_id)
    row = db.get(RunParcel, (run_id, parcel_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel not in this run")
    parcel = db.get(Parcel, parcel_id)
    if parcel is None or parcel.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel not found")

    year = db.get_one(ImageryYear, run.target_year_id)
    geom = to_shape(parcel.geom)
    view = plan_view(geom, size=size, buffer=buffer)
    assets = assets_intersecting(db, user.tenant_id, year.id, view.search_bbox)
    raster = read_parcel(
        request.app.state.settings,
        assets,
        geom,
        resolution_m=view.resolution_m,
        buffer_m=view.buffer_m,
    )
    if raster is None or not raster.mask[raster.parcel_mask].any():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    rgb = raster.data[:3].copy()
    to_utm = Transformer.from_crs(4326, view.utm, always_xy=True).transform
    # Draw order: the wider detection first, the scoring structure over it, the boundary
    # last so it stays readable against both.
    for column, colour in (
        (row.new_builtup_geom, NEW_BUILTUP_RGB),
        (row.structure_geom, STRUCTURE_RGB),
    ):
        if column is not None:
            paint(rgb, raster, shapely_transform(to_utm, to_shape(column)), colour)
    if outline:
        paint(rgb, raster, view.geom_utm, OUTLINE_RGB, width_px=OUTLINE_PX, outline_only=True)

    body, bounds = render_png(raster, rgb)
    return Response(body, media_type="image/png", headers={**PNG_HEADERS, "X-Bounds": bounds})
