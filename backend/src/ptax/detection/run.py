"""``run.execute``: score every parcel of a run's layer, in resumable committed batches.

A ``change`` run compares two years with a detector; an ``inventory`` run asks the vision
model for the structures on each parcel in one year. Both share the batch loop below.

Parcels are visited in geohash order so consecutive reads hit the same COG blocks. After
every batch the rows are committed and the run's counters updated, then the run's status
is re-read (an admin may have cancelled it) and ``stop_requested()`` is checked (a
graceful worker stop re-queues the job, which resumes by skipping parcels that already
have ``run_parcels`` rows).
"""

import logging
import math
import resource
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import shapely
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.config import Settings, get_settings
from ptax.db.models import ImageryAsset, ImageryYear, Job, Parcel, Run, RunParcel
from ptax.detection.detector import (
    ChangeResult,
    ClassicalDetector,
    Detector,
    InsufficientCoverage,
    ParcelRaster,
    RadiometricFit,
    fit_radiometry,
    paired_samples,
)
from ptax.detection.geometry import mask_to_multipolygon
from ptax.imagery.preview import plan_view
from ptax.imagery.reader import read_parcel
from ptax.jobs.queue import MAX_ATTEMPTS, JobInterrupted, RetryableError
from ptax.jobs.registry import job_handler
from ptax.vision.client import VisionClient, VisionUnavailable
from ptax.vision.inventory import BUFFER, IMAGE_PX, InventoryError, inventory_parcel
from ptax.worker import stop_requested

log = logging.getLogger("ptax.detection")

BATCH_SIZE = 200
#: An inventory parcel costs seconds of model time, not milliseconds: commit (and let a
#: cancel or a worker stop land) every few parcels rather than every few minutes.
INVENTORY_BATCH_SIZE = 10
#: Waits before retrying a parcel when the vision model is unreachable. After the last,
#: the job is re-queued (``RetryableError``) with the run still `running`.
VISION_RETRY_DELAYS: tuple[float, ...] = (15, 60, 180)
MIN_RESOLUTION_M = 0.5
MAX_PIXELS = 4_000_000
GEOHASH_PRECISION = 8
#: A ready asset and its bounds (EPSG:4326), for cheap per-parcel intersection tests.
AssetBox = tuple[ImageryAsset, BaseGeometry]
# Below this valid fraction a parcel counts as having no imagery at all: a sliver along
# an asset edge (reprojected bounds overlap by a pixel or two) is not "partial coverage".
NO_COVERAGE_VALID_FRAC = 0.05


def _now() -> datetime:
    return datetime.now(UTC)


def _ready_assets(db: Session, year_id: uuid.UUID) -> list[AssetBox]:
    assets = (
        db.execute(
            select(ImageryAsset).where(
                ImageryAsset.year_id == year_id, ImageryAsset.status == "ready"
            )
        )
        .scalars()
        .all()
    )
    return [(a, box(*to_shape(cast(WKBElement, a.bounds)).bounds)) for a in assets]


def _intersecting(assets: list[AssetBox], bounds: tuple) -> list[ImageryAsset]:
    envelope = box(*bounds)
    return [a for a, b in assets if b.intersects(envelope)]


def comparison_resolution(base: ImageryYear, target: ImageryYear, parcel_bounds_m2: float) -> float:
    """Coarsest of the two years (floored at 0.5 m), coarsened so the window stays bounded."""
    res = max(base.resolution_m or 0, target.resolution_m or 0, MIN_RESOLUTION_M)
    return max(res, math.sqrt(parcel_bounds_m2 / MAX_PIXELS))


def score_parcel(
    run: Run,
    parcel: Parcel,
    base: ImageryYear,
    target: ImageryYear,
    base_assets: list[AssetBox],
    target_assets: list[AssetBox],
    detector: Detector,
    fit: RadiometricFit | None = None,
) -> RunParcel:
    settings = get_settings()
    geom = to_shape(parcel.geom)
    row = RunParcel(run_id=run.id, parcel_id=parcel.id, parcel_ref=parcel.parcel_ref)
    # Parcel extent in metres (approximate, from the 4326 bbox) sizes the read window.
    minx, miny, maxx, maxy = geom.bounds
    extent_m2 = (
        (maxx - minx)
        * 111_000
        * math.cos(math.radians((miny + maxy) / 2))
        * ((maxy - miny) * 111_000)
    )
    resolution = comparison_resolution(base, target, max(extent_m2, 1.0))
    base_raster = read_parcel(
        settings, _intersecting(base_assets, geom.bounds), geom, resolution_m=resolution
    )
    if base_raster is None:
        row.skipped_reason = "no_coverage_base"
        return row
    target_raster = read_parcel(
        settings, _intersecting(target_assets, geom.bounds), geom, resolution_m=resolution
    )
    if target_raster is None:
        row.skipped_reason = "no_coverage_target"
        return row
    try:
        result = detector.compare(
            base_raster,
            target_raster,
            threshold=run.threshold,
            min_new_area_m2=run.min_new_area_m2,
            fit=fit,
        )
    except InsufficientCoverage as exc:
        row.skipped_reason = (
            f"no_coverage_{exc.which}"
            if exc.nodata_frac >= 1 - NO_COVERAGE_VALID_FRAC
            else "partial_coverage"
        )
        return row
    row.score = result.score
    row.candidate = result.candidate
    row.indicators = result.indicators
    _record_detected_area(row, result, base_raster)
    return row


def vision_client(settings: Settings) -> VisionClient:
    return VisionClient.from_settings(settings)


def _as_multipolygon(geometry: object) -> MultiPolygon | None:
    polygons = [
        g
        for g in getattr(geometry, "geoms", [geometry])
        if isinstance(g, Polygon) and not g.is_empty
    ]
    return MultiPolygon(polygons) if polygons else None


def inventory_row(
    run: Run,
    parcel: Parcel,
    assets: list[AssetBox],
    client: VisionClient,
) -> RunParcel:
    """One parcel's structure inventory: what the model sees on the run's one year.

    A reply the model cannot get right is recorded as ``model_error`` with the raw text,
    never guessed at. An unreachable model is waited out briefly, then raised as
    ``RetryableError``: an outage says nothing about the parcel.
    """
    settings = get_settings()
    geom = to_shape(parcel.geom)
    row = RunParcel(run_id=run.id, parcel_id=parcel.id, parcel_ref=parcel.parcel_ref)
    view = plan_view(geom, size=IMAGE_PX, buffer=BUFFER)
    raster = read_parcel(
        settings,
        _intersecting(assets, view.search_bbox),
        geom,
        resolution_m=view.resolution_m,
        buffer_m=view.buffer_m,
    )
    if raster is None or not raster.mask[raster.parcel_mask].any():
        row.skipped_reason = "no_coverage"
        return row
    for attempt, delay in enumerate((*VISION_RETRY_DELAYS, None)):
        try:
            result = inventory_parcel(client, raster, view.geom_utm)
            break
        except InventoryError as exc:
            row.skipped_reason = "model_error"
            row.indicators = {"model": run.model_name, "error": str(exc), "raw": exc.raw[:4000]}
            return row
        except VisionUnavailable as exc:
            if delay is None:
                raise RetryableError(f"vision model unavailable: {exc}") from exc
            log.warning("run %s: vision model unavailable (%s); retry %d", run.id, exc, attempt + 1)
            time.sleep(delay)

    row.score = max((s.confidence for s in result.structures), default=0.0)
    row.candidate = False
    row.indicators = {
        "structures": [
            {"kind": s.kind, "confidence": s.confidence, "box": list(s.box)}
            for s in result.structures
        ],
        "summary": result.summary,
        "kinds": result.kinds,
        "counts": result.counts,
        "model": run.model_name,
        "resolution_m": round(view.resolution_m, 4),
    }
    if result.structures:
        to_4326 = Transformer.from_crs(view.utm, 4326, always_xy=True).transform
        union = shapely.union_all([s.polygon for s in result.structures])
        markup = _as_multipolygon(shapely_transform(to_4326, union))
        if markup is not None:
            row.structure_geom = from_shape(markup, srid=4326)
    return row


def _record_detected_area(row: RunParcel, result: ChangeResult, raster: ParcelRaster) -> None:
    """Store where the run found the change, on the grid it scored.

    Hot path: this runs once per parcel inside the batch loop, over runs that cover
    hundreds of thousands of parcels. Most of them detect nothing, so the empty case must
    cost one ``.any()`` and no polygonisation at all -- which is also what leaves the
    column NULL rather than holding an empty shape.
    """
    for column, mask in (
        ("new_builtup_geom", result.new_builtup_mask),
        ("structure_geom", result.structure_mask),
    ):
        if mask is None or not mask.any():
            continue
        geom = mask_to_multipolygon(mask, raster.transform, raster.crs)
        if geom is not None:
            setattr(row, column, from_shape(geom, srid=4326))


#: Parcels read to fit a run's radiometric correction. Enough that unchanged ground
#: dominates, few enough that the pre-pass is a small fraction of a county-sized run.
FIT_SAMPLE_PARCELS = 120


def _fit_for_run(
    db: Session,
    run: Run,
    base: ImageryYear,
    target: ImageryYear,
    base_assets: list[AssetBox],
    target_assets: list[AssetBox],
    ordered_ids: Sequence[uuid.UUID],
) -> RadiometricFit | None:
    """One capture-to-capture correction for the whole run, fitted before scoring starts.

    Two captures of the same ground differ in NIR response, which moves vegetation across
    an absolute NDVI cutoff and manufactures both built-up area and vegetation loss. The
    correction has to be fitted across many parcels: a per-parcel fit is anchored by that
    parcel's own change, which measured out at precision 0.0745 -> 0.0528 and recall
    0.72 -> 0.37 on the evaluation set, because a new roof is a large share of one parcel.

    Parcels are taken evenly across the run's geohash ordering so the sample spans the
    county rather than one corner of it.
    """
    settings = get_settings()
    if not ordered_ids:
        return None
    step = max(1, len(ordered_ids) // FIT_SAMPLE_PARCELS)
    sample_ids = ordered_ids[::step][:FIT_SAMPLE_PARCELS]
    parcels = {
        p.id: p for p in db.execute(select(Parcel).where(Parcel.id.in_(sample_ids))).scalars()
    }

    base_samples: list[Any] = []
    target_samples: list[Any] = []
    for pid in sample_ids:
        parcel = parcels.get(pid)
        if parcel is None:
            continue
        geom = to_shape(parcel.geom)
        minx, miny, maxx, maxy = geom.bounds
        extent_m2 = (
            (maxx - minx)
            * 111_000
            * math.cos(math.radians((miny + maxy) / 2))
            * ((maxy - miny) * 111_000)
        )
        resolution = comparison_resolution(base, target, max(extent_m2, 1.0))
        base_raster = read_parcel(
            settings, _intersecting(base_assets, geom.bounds), geom, resolution_m=resolution
        )
        target_raster = read_parcel(
            settings, _intersecting(target_assets, geom.bounds), geom, resolution_m=resolution
        )
        if base_raster is None or target_raster is None:
            continue
        pair = paired_samples(base_raster, target_raster)
        if pair is None:
            continue
        base_samples.append(pair[0])
        target_samples.append(pair[1])
    return fit_radiometry(base_samples, target_samples)


def _detector_for(run: Run) -> Detector:
    """The detector a comparison run recorded, built once before any parcel is scored.

    Only the classical detector remains. A run recorded with the removed segmentation
    detector (still queued from before its removal) fails here, before any parcel is
    scored, rather than being silently scored by a different detector than it records.
    """
    if run.detector == "classical":
        return ClassicalDetector()
    raise ValueError(f"run {run.id} records detector {run.detector!r}, which no longer exists")


def _peak_rss_mb() -> float:
    """Peak resident memory of this worker process, in MiB.

    ``ru_maxrss`` is KiB on Linux (the Fargate worker) but bytes on macOS.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0


def flush_batch(db: Session, run: Run, rows: list[RunParcel]) -> None:
    """Persist one batch of results and advance the run's counters (one commit)."""
    db.add_all(rows)
    run.parcels_processed += len(rows)
    run.candidates += sum(1 for r in rows if r.candidate)
    run.parcels_skipped += sum(1 for r in rows if r.skipped_reason is not None)
    db.commit()


def _restore_counters(db: Session, run: Run) -> set[uuid.UUID]:
    """Recompute the counters from persisted rows (a resumed run trusts rows, not the row)."""
    done = set(db.execute(select(RunParcel.parcel_id).where(RunParcel.run_id == run.id)).scalars())
    run.parcels_processed = len(done)
    run.candidates = db.execute(
        select(func.count())
        .select_from(RunParcel)
        .where(RunParcel.run_id == run.id, RunParcel.candidate.is_(True))
    ).scalar_one()
    run.parcels_skipped = db.execute(
        select(func.count())
        .select_from(RunParcel)
        .where(RunParcel.run_id == run.id, RunParcel.skipped_reason.is_not(None))
    ).scalar_one()
    return done


@job_handler("run.execute")
def execute_run(db: Session, job: Job) -> None:
    run = db.get_one(Run, uuid.UUID(job.payload["run_id"]))
    if run.status not in ("queued", "running"):
        return  # cancelled while queued, or already finished
    run.status = "running"
    run.started_at = run.started_at or _now()
    done = _restore_counters(db, run)
    db.commit()
    try:
        settings = get_settings()
        ordered_ids = (
            db.execute(
                select(Parcel.id)
                .where(Parcel.layer_id == run.layer_id)
                .order_by(
                    func.ST_GeoHash(func.ST_Centroid(Parcel.geom), GEOHASH_PRECISION), Parcel.id
                )
            )
            .scalars()
            .all()
        )
        score: Callable[[Parcel], RunParcel]
        if run.kind == "inventory":
            assets = _ready_assets(db, run.base_year_id)
            client = vision_client(settings)

            def score(parcel: Parcel) -> RunParcel:
                return inventory_row(run, parcel, assets, client)

            batch_size = INVENTORY_BATCH_SIZE
        else:
            if run.target_year_id is None:
                raise ValueError(f"change run {run.id} has no target year")
            base = db.get_one(ImageryYear, run.base_year_id)
            target = db.get_one(ImageryYear, run.target_year_id)
            base_assets = _ready_assets(db, base.id)
            target_assets = _ready_assets(db, target.id)
            detector = _detector_for(run)
            fit = _fit_for_run(db, run, base, target, base_assets, target_assets, ordered_ids)
            if fit is not None:
                log.info(
                    "run %s radiometric fit over %d parcels: gains %s",
                    run.id,
                    fit.sampled_parcels,
                    [round(g, 3) for g in fit.gains],
                )
            else:
                log.warning("run %s has no usable radiometric fit; scoring unnormalised", run.id)

            def score(parcel: Parcel) -> RunParcel:
                return score_parcel(
                    run, parcel, base, target, base_assets, target_assets, detector, fit
                )

            batch_size = BATCH_SIZE

        todo = [pid for pid in ordered_ids if pid not in done]
        started = time.monotonic()
        for start in range(0, len(todo), batch_size):
            batch_ids = todo[start : start + batch_size]
            parcels = {
                p.id: p
                for p in db.execute(select(Parcel).where(Parcel.id.in_(batch_ids))).scalars()
            }
            rows = [score(parcels[pid]) for pid in batch_ids]
            flush_batch(db, run, rows)
            db.refresh(run)
            if run.status == "cancelled":
                run.finished_at = _now()
                db.commit()
                log.info("run %s cancelled after %d parcels", run.id, run.parcels_processed)
                return
            if stop_requested():
                raise JobInterrupted("worker stopping")
        run.status = "succeeded"
        run.finished_at = _now()
        db.commit()
        # Per-parcel cost from this worker's own share of the run (a resumed run counts
        # only what it scored), reads included: that is what a county run pays.
        mean_parcel_ms = (time.monotonic() - started) * 1000.0 / max(len(todo), 1)
        log.info(
            "run %s done: %d parcels, %d candidates, %d skipped; detector %s%s;"
            " mean_parcel_ms %.1f peak_rss_mb %.0f",
            run.id,
            run.parcels_processed,
            run.candidates,
            run.parcels_skipped,
            run.detector,
            f" ({run.model_name})" if run.model_name else "",
            mean_parcel_ms,
            _peak_rss_mb(),
        )
    except JobInterrupted:
        db.rollback()
        raise
    except RetryableError as exc:
        # Like an interruption, the run stays `running` and the re-queued job resumes from
        # the committed batches -- unless this was the job's last attempt, when a run left
        # `running` would never finish.
        db.rollback()
        if job.attempts >= MAX_ATTEMPTS:
            run.status = "failed"
            run.error = str(exc)[:2000]
            run.finished_at = _now()
            db.commit()
        raise
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.finished_at = _now()
        db.commit()
        raise
