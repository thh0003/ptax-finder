"""Imagery jobs: NAIP ingest, county ArcGIS ingest, upload ingest, and coverage recomputation.

Each handler owns its year's status transitions. A failure leaves the year ``failed``
with a human-readable ``error`` (committed), then re-raises so the job is failed too,
the same contract as ``ptax.parcels.ingest``. Handlers check ``stop_requested()`` at
their commit boundaries and raise ``JobInterrupted`` so a graceful worker stop re-queues
the job to resume from the last committed item.
"""

import logging
import tempfile
import uuid
from pathlib import Path

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.config import get_settings
from ptax.county import imagery as county_imagery
from ptax.county.parcels import CountySourceError
from ptax.db.models import ImageryAsset, ImageryYear, Job, Parcel, Tenant
from ptax.imagery.cog import (
    MAX_BANDS,
    MIN_BANDS,
    SUPPORTED_DTYPES,
    RasterError,
    RasterInfo,
    bounds_to_crs,
    inspect_raster,
    to_cog,
)
from ptax.imagery.sources import NaipItem, get_naip_source
from ptax.jobs.queue import JobInterrupted
from ptax.jobs.registry import job_handler
from ptax.parcels.footprint import NoParcelLayer, footprint_for, utm_epsg_for
from ptax.storage import get_s3_client, imagery_asset_key
from ptax.worker import stop_requested

log = logging.getLogger("ptax.imagery")

# PRD: sub-metre imagery is the practical floor for seeing small structures.
MAX_RESOLUTION_M = 1.0


class ImageryError(Exception):
    """A problem with imagery that the admin needs to see."""


def _fail_year(db: Session, year: ImageryYear, message: str) -> None:
    db.rollback()
    year.status = "failed"
    year.error = message[:2000]
    db.commit()


def _check_stop() -> None:
    if stop_requested():
        raise JobInterrupted("worker stopping")


def register_asset(
    db: Session, asset: ImageryAsset, *, key: str, info: RasterInfo, size_bytes: int
) -> ImageryAsset:
    """Record a stored COG on ``asset``, mark it ready, and commit."""
    asset.status = "ready"
    asset.s3_key = key
    asset.bounds = from_shape(box(*info.bounds_4326), srid=4326)
    asset.epsg = info.epsg
    asset.width = info.width
    asset.height = info.height
    asset.band_count = min(info.band_count, 4)
    asset.resolution_m = info.resolution_m
    asset.size_bytes = size_bytes
    asset.error = None
    db.commit()
    return asset


def compute_coverage(db: Session, year: ImageryYear, footprint: BaseGeometry) -> None:
    """Fill the year's coverage, uncovered-parcel count, bounds, bands and resolution."""
    assets = (
        db.execute(
            select(ImageryAsset).where(
                ImageryAsset.year_id == year.id, ImageryAsset.status == "ready"
            )
        )
        .scalars()
        .all()
    )
    if not assets:
        year.coverage_pct = 0
        year.parcels_uncovered = None
        year.bounds = None
        return
    union_sql = func.ST_Union(ImageryAsset.bounds)
    covered = db.execute(
        select(union_sql).where(ImageryAsset.year_id == year.id, ImageryAsset.status == "ready")
    ).scalar_one()
    tenant = db.get_one(Tenant, year.tenant_id)
    utm = utm_epsg_for(footprint)
    fp_geom = func.ST_GeomFromText(footprint.wkt, 4326)
    fp_area = db.execute(select(func.ST_Area(func.ST_Transform(fp_geom, utm)))).scalar_one()
    covered_area = db.execute(
        select(func.ST_Area(func.ST_Transform(func.ST_Intersection(fp_geom, covered), utm)))
    ).scalar_one()
    year.coverage_pct = round(100 * covered_area / fp_area) if fp_area else 0
    year.parcels_uncovered = db.execute(
        select(func.count())
        .select_from(Parcel)
        .where(
            Parcel.layer_id == tenant.current_parcel_layer_id,
            ~func.ST_CoveredBy(Parcel.geom, covered),
        )
    ).scalar_one()
    year.bounds = from_shape(box(*to_shape(covered).bounds), srid=4326)
    year.band_count = min(a.band_count or 4 for a in assets)
    year.resolution_m = max(a.resolution_m or 0 for a in assets)


def store_cog(db: Session, asset: ImageryAsset, local_cog: Path) -> ImageryAsset:
    """Upload a finished COG to the asset's storage key and register it."""
    settings = get_settings()
    key = imagery_asset_key(asset.tenant_id, asset.year_id, asset.id)
    get_s3_client(settings).upload_file(str(local_cog), settings.s3_bucket, key)
    info = inspect_raster(str(local_cog))
    return register_asset(db, asset, key=key, info=info, size_bytes=local_cog.stat().st_size)


def _ingest_item(db: Session, year: ImageryYear, item: NaipItem, env: dict, clip: tuple) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"{item.id}.tif"
        to_cog(item.href, local, env=env, clip_bounds=bounds_to_crs(clip, item.epsg))
        asset = ImageryAsset(
            id=uuid.uuid4(),
            tenant_id=year.tenant_id,
            year_id=year.id,
            status="pending",
            s3_key="",
            source_ref=item.id,
        )
        db.add(asset)
        store_cog(db, asset, local)


@job_handler("imagery.ingest_naip")
def ingest_naip(db: Session, job: Job) -> None:
    year = db.get_one(ImageryYear, uuid.UUID(job.payload["year_id"]))
    if year.status not in ("queued", "processing"):
        return
    year.status = "processing"
    db.commit()
    try:
        tenant = db.get_one(Tenant, year.tenant_id)
        footprint = footprint_for(db, tenant)
        source = get_naip_source(get_settings())
        items = source.items_for(year.year, footprint)
        if not items:
            raise ImageryError(f"no NAIP tiles cover the county for {year.year}")
        done = set(
            db.execute(
                select(ImageryAsset.source_ref).where(
                    ImageryAsset.year_id == year.id, ImageryAsset.status == "ready"
                )
            ).scalars()
        )
        clip = footprint.bounds
        for item in items:
            if item.id in done:
                continue
            _check_stop()
            try:
                _ingest_item(db, year, item, source.gdal_env(), clip)
            except RasterError as exc:
                raise ImageryError(f"NAIP tile {item.id} {exc}") from exc
            except Exception as exc:
                raise ImageryError(f"NAIP tile {item.id} could not be copied: {exc}") from exc
            log.info("year %s: stored NAIP tile %s", year.id, item.id)
        compute_coverage(db, year, footprint)
        year.status = "ready"
        year.error = None
        db.commit()
    except JobInterrupted:
        db.rollback()
        raise
    except (ImageryError, NoParcelLayer) as exc:
        _fail_year(db, year, str(exc))
        raise
    except Exception as exc:
        _fail_year(db, year, f"ingest failed: {exc}")
        raise


@job_handler("imagery.ingest_arcgis")
def ingest_arcgis(db: Session, job: Job) -> None:
    """Build the year from the county tile cache at ``year.provider``, one COG per block.

    Blocks already stored are skipped, so a re-claimed job resumes where it stopped.
    """
    year = db.get_one(ImageryYear, uuid.UUID(job.payload["year_id"]))
    if year.status not in ("queued", "processing"):
        return
    year.status = "processing"
    db.commit()
    try:
        if not year.provider:
            raise ImageryError("no ArcGIS service recorded for this year")
        service = year.provider
        tenant = db.get_one(Tenant, year.tenant_id)
        footprint = footprint_for(db, tenant)
        done = set(
            db.execute(
                select(ImageryAsset.source_ref).where(
                    ImageryAsset.year_id == year.id, ImageryAsset.status == "ready"
                )
            ).scalars()
        )
        with county_imagery.http_client() as client:
            grid = county_imagery.tile_grid(client, service)
            blocks = county_imagery.plan_blocks(grid, footprint)
            for block, tiles in sorted(blocks.items()):
                ref = county_imagery.block_ref(service, grid, block)
                if ref in done:
                    continue
                _check_stop()
                with tempfile.TemporaryDirectory() as tmp:
                    mosaic = Path(tmp) / "mosaic.tif"
                    if county_imagery.write_block(client, service, grid, block, tiles, mosaic) == 0:
                        log.warning("year %s: the cache has no tiles for %s", year.id, ref)
                        continue
                    cog = Path(tmp) / "block.tif"
                    to_cog(str(mosaic), cog)
                    asset = ImageryAsset(
                        id=uuid.uuid4(),
                        tenant_id=year.tenant_id,
                        year_id=year.id,
                        status="pending",
                        s3_key="",
                        source_ref=ref,
                    )
                    db.add(asset)
                    store_cog(db, asset, cog)
                log.info("year %s: stored %s (%d tiles)", year.id, ref, len(tiles))
        compute_coverage(db, year, footprint)
        year.status = "ready"
        year.error = None
        db.commit()
    except JobInterrupted:
        db.rollback()
        raise
    except (ImageryError, NoParcelLayer, CountySourceError) as exc:
        _fail_year(db, year, str(exc))
        raise
    except Exception as exc:
        _fail_year(db, year, f"ingest failed: {exc}")
        raise


def _process_upload(db: Session, asset: ImageryAsset) -> None:
    """Validate one uploaded file, store it as a COG, and delete the original upload."""
    settings = get_settings()
    s3 = get_s3_client(settings)
    with tempfile.TemporaryDirectory() as tmp:
        original = Path(tmp) / "upload.tif"
        s3.download_file(settings.s3_bucket, asset.s3_key, str(original))
        info = inspect_raster(str(original))
        if info.band_count < MIN_BANDS:
            raise RasterError(f"has {info.band_count} band(s); 3 or 4 required")
        if info.dtype not in SUPPORTED_DTYPES:
            raise RasterError(f"{info.dtype} pixels are not supported (uint8/uint16 only)")
        if info.resolution_m > MAX_RESOLUTION_M:
            raise RasterError(
                f"resolution {info.resolution_m:.2f} m/px is coarser than {MAX_RESOLUTION_M:g} m"
            )
        if info.is_cog and info.dtype == "uint8" and info.band_count <= MAX_BANDS:
            cog = original  # already what we store; no conversion pass
        else:
            cog = Path(tmp) / "converted.tif"
            to_cog(str(original), cog)
        upload_key = asset.s3_key
        store_cog(db, asset, cog)
    s3.delete_object(Bucket=settings.s3_bucket, Key=upload_key)


@job_handler("imagery.ingest_upload")
def ingest_upload(db: Session, job: Job) -> None:
    year = db.get_one(ImageryYear, uuid.UUID(job.payload["year_id"]))
    if year.status != "processing":
        return
    try:
        tenant = db.get_one(Tenant, year.tenant_id)
        footprint = footprint_for(db, tenant)
        pending = (
            db.execute(
                select(ImageryAsset)
                .where(ImageryAsset.year_id == year.id, ImageryAsset.status == "pending")
                .order_by(ImageryAsset.created_at)
            )
            .scalars()
            .all()
        )
        for asset in pending:
            _check_stop()
            name = asset.original_filename or asset.s3_key.rsplit("/", 1)[-1]
            try:
                _process_upload(db, asset)
            except RasterError as exc:
                db.rollback()
                asset.status = "failed"
                asset.error = f"{name} {exc}"[:2000]
                db.commit()
                log.warning("year %s: rejected %s: %s", year.id, name, exc)
            except JobInterrupted:
                raise
            except Exception as exc:  # noqa: BLE001 - keep processing the other files
                db.rollback()
                asset.status = "failed"
                asset.error = f"{name} could not be processed: {exc}"[:2000]
                db.commit()
                log.exception("year %s: failed processing %s", year.id, name)
        assets = (
            db.execute(select(ImageryAsset).where(ImageryAsset.year_id == year.id)).scalars().all()
        )
        if not any(a.status == "ready" for a in assets):
            first_error = next((a.error for a in assets if a.error), "no files")
            raise ImageryError(f"no valid imagery files: {first_error}")
        compute_coverage(db, year, footprint)
        year.status = "ready"
        year.error = None
        db.commit()
    except JobInterrupted:
        db.rollback()
        raise
    except (ImageryError, NoParcelLayer) as exc:
        _fail_year(db, year, str(exc))
        raise
    except Exception as exc:
        _fail_year(db, year, f"ingest failed: {exc}")
        raise


@job_handler("imagery.recompute_coverage")
def recompute_coverage(db: Session, job: Job) -> None:
    """After a new parcel layer becomes current, re-derive every ready year's gap report."""
    tenant = db.get_one(Tenant, uuid.UUID(job.payload["tenant_id"]))
    footprint = footprint_for(db, tenant)
    years = (
        db.execute(
            select(ImageryYear).where(
                ImageryYear.tenant_id == tenant.id, ImageryYear.status == "ready"
            )
        )
        .scalars()
        .all()
    )
    for year in years:
        compute_coverage(db, year, footprint)
    db.commit()
