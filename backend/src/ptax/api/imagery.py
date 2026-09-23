"""/api/imagery: NAIP discovery and ingest, uploaded imagery years, previews."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ptax.auth.deps import CurrentUser, get_current_user, require_role
from ptax.db.models import ImageryAsset, ImageryYear, Parcel, Tenant, UserRole
from ptax.db.session import get_db
from ptax.imagery.naip import CatalogError
from ptax.imagery.preview import paint, plan_view, render_png
from ptax.imagery.reader import (
    assets_intersecting,
    read_bounds_preview,
    read_parcel,
    zoom_range,
)
from ptax.imagery.sources import ImagerySource, get_source_dependency
from ptax.jobs.queue import enqueue
from ptax.parcels.footprint import NoParcelLayer, footprint_for

OUTLINE_RGB = (255, 255, 0)
OUTLINE_PX = 2
PNG_HEADERS = {"Cache-Control": "private, max-age=3600"}

router = APIRouter(prefix="/imagery")
admin_only = require_role(UserRole.admin)


class ImageryAssetOut(BaseModel):
    id: uuid.UUID
    status: str
    original_filename: str | None
    source_ref: str | None
    error: str | None


class ImageryYearOut(BaseModel):
    id: uuid.UUID
    year: int
    source: str
    provider: str | None
    status: str
    band_count: int | None
    resolution_m: float | None
    coverage_pct: float | None
    parcels_uncovered: int | None
    bounds: list[float] | None
    # Zoom hints for a MapLibre raster source over /api/tiles.
    min_zoom: int
    max_zoom: int
    error: str | None
    created_at: datetime
    assets: list[ImageryAssetOut]


class NaipIngestIn(BaseModel):
    year: int = Field(ge=1990, le=2100)


def year_out(year: ImageryYear, assets: list[ImageryAsset]) -> ImageryYearOut:
    min_zoom, max_zoom = zoom_range(year.resolution_m)
    return ImageryYearOut(
        id=year.id,
        year=year.year,
        source=year.source,
        provider=year.provider,
        status=year.status,
        band_count=year.band_count,
        resolution_m=year.resolution_m,
        coverage_pct=year.coverage_pct,
        parcels_uncovered=year.parcels_uncovered,
        bounds=list(to_shape(year.bounds).bounds) if year.bounds is not None else None,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        error=year.error,
        created_at=year.created_at,
        assets=[
            ImageryAssetOut(
                id=a.id,
                status=a.status,
                original_filename=a.original_filename,
                source_ref=a.source_ref,
                error=a.error,
            )
            for a in sorted(assets, key=lambda a: a.created_at)
        ],
    )


def _assets_by_year(db: Session, year_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    grouped: dict[uuid.UUID, list[ImageryAsset]] = {yid: [] for yid in year_ids}
    if year_ids:
        for asset in db.execute(
            select(ImageryAsset).where(ImageryAsset.year_id.in_(year_ids))
        ).scalars():
            grouped[asset.year_id].append(asset)
    return grouped


def get_year(db: Session, user: CurrentUser, year_id: uuid.UUID) -> ImageryYear:
    year = db.get(ImageryYear, year_id)
    if year is None or year.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "imagery year not found")
    return year


@router.get("/years", response_model=list[ImageryYearOut])
def list_years(
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ImageryYearOut]:
    years = (
        db.execute(
            select(ImageryYear)
            .where(ImageryYear.tenant_id == user.tenant_id)
            .order_by(ImageryYear.created_at.desc())
        )
        .scalars()
        .all()
    )
    assets = _assets_by_year(db, [y.id for y in years])
    return [year_out(y, assets[y.id]) for y in years]


@router.get("/years/{year_id}", response_model=ImageryYearOut)
def get_year_route(
    year_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImageryYearOut:
    year = get_year(db, user, year_id)
    return year_out(year, _assets_by_year(db, [year.id])[year.id])


class UploadYearIn(BaseModel):
    year: int = Field(ge=1990, le=2100)
    provider: str | None = Field(default=None, max_length=120)


class AssetIn(BaseModel):
    upload_key: str = Field(min_length=1, max_length=1024)
    original_filename: str = Field(min_length=1, max_length=255)


@router.post("/years", response_model=ImageryYearOut, status_code=status.HTTP_201_CREATED)
def create_upload_year(
    body: UploadYearIn,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ImageryYearOut:
    """Create an upload year; files are registered against it, then finalized."""
    _footprint_or_409(db, user)
    exists = db.execute(
        select(ImageryYear.id).where(
            ImageryYear.tenant_id == user.tenant_id,
            ImageryYear.year == body.year,
            ImageryYear.source == "upload",
        )
    ).first()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"upload year {body.year} already exists")
    provider = body.provider.strip() if body.provider else None
    year = ImageryYear(
        tenant_id=user.tenant_id,
        year=body.year,
        source="upload",
        provider=provider or None,
        status="queued",
        created_by=user.id,
    )
    db.add(year)
    db.commit()
    db.refresh(year)
    return year_out(year, [])


@router.post(
    "/years/{year_id}/assets", response_model=ImageryAssetOut, status_code=status.HTTP_201_CREATED
)
def register_upload_asset(
    year_id: uuid.UUID,
    body: AssetIn,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ImageryAssetOut:
    """Attach an uploaded GeoTIFF to a year as a pending asset (processed on finalize)."""
    year = get_year(db, user, year_id)
    if year.source != "upload":
        raise HTTPException(status.HTTP_409_CONFLICT, "files can only be added to upload years")
    if year.status == "processing":
        raise HTTPException(status.HTTP_409_CONFLICT, "year is processing; wait for it to finish")
    expected_prefix = f"tenants/{user.tenant_id}/imagery/"
    if not body.upload_key.startswith(expected_prefix):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "upload_key is not an upload of this tenant"
        )
    asset = ImageryAsset(
        tenant_id=user.tenant_id,
        year_id=year.id,
        status="pending",
        s3_key=body.upload_key,
        original_filename=body.original_filename,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return ImageryAssetOut(
        id=asset.id,
        status=asset.status,
        original_filename=asset.original_filename,
        source_ref=asset.source_ref,
        error=asset.error,
    )


@router.post(
    "/years/{year_id}/finalize", response_model=ImageryYearOut, status_code=status.HTTP_202_ACCEPTED
)
def finalize_upload_year(
    year_id: uuid.UUID,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ImageryYearOut:
    """Queue validation, COG conversion and the coverage report for the pending files."""
    year = get_year(db, user, year_id)
    if year.source != "upload":
        raise HTTPException(status.HTTP_409_CONFLICT, "only upload years are finalized")
    if year.status == "processing":
        raise HTTPException(status.HTTP_409_CONFLICT, "year is already processing")
    pending = db.execute(
        select(ImageryAsset.id).where(
            ImageryAsset.year_id == year.id, ImageryAsset.status == "pending"
        )
    ).first()
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no pending files to process")
    year.status = "processing"
    year.error = None
    enqueue(db, "imagery.ingest_upload", user.tenant_id, {"year_id": str(year.id)})
    db.commit()
    db.refresh(year)
    return year_out(year, _assets_by_year(db, [year.id])[year.id])


class ExistingYear(BaseModel):
    id: uuid.UUID
    status: str


class NaipAvailability(BaseModel):
    year: int
    item_count: int
    coverage_pct: int
    estimated_gb: float
    gsd_m: float
    existing: ExistingYear | None


def _footprint_or_409(db: Session, user: CurrentUser):  # noqa: ANN202 - shapely geometry
    tenant = db.get_one(Tenant, user.tenant_id)
    try:
        return footprint_for(db, tenant)
    except NoParcelLayer as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "no parcel layer") from exc


@router.get("/naip/available", response_model=list[NaipAvailability])
def naip_available(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    source: ImagerySource = Depends(get_source_dependency),
) -> list[NaipAvailability]:
    """NAIP years covering the county footprint, with an ingest size estimate."""
    footprint = _footprint_or_409(db, user)
    try:
        years = source.list_years(footprint)
    except CatalogError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"NAIP catalog unavailable: {exc}"
        ) from exc
    existing = {
        row.year: row
        for row in db.execute(
            select(ImageryYear).where(
                ImageryYear.tenant_id == user.tenant_id, ImageryYear.source == "naip"
            )
        ).scalars()
    }
    return [
        NaipAvailability(
            year=y.year,
            item_count=len(y.items),
            coverage_pct=y.coverage_pct,
            estimated_gb=y.estimated_bytes / 1e9,
            gsd_m=y.gsd_m,
            existing=(
                ExistingYear(id=existing[y.year].id, status=existing[y.year].status)
                if y.year in existing
                else None
            ),
        )
        for y in years
    ]


@router.post("/naip/ingest", response_model=ImageryYearOut, status_code=status.HTTP_201_CREATED)
def naip_ingest(
    body: NaipIngestIn,
    request: Request,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
    source: ImagerySource = Depends(get_source_dependency),
) -> ImageryYearOut:
    """Queue the copy of one NAIP year for the county; refused above the size cap."""
    footprint = _footprint_or_409(db, user)
    try:
        items = source.items_for(body.year, footprint)
    except CatalogError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"NAIP catalog unavailable: {exc}"
        ) from exc
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no NAIP imagery for {body.year}")
    estimated_gb = sum(i.estimated_bytes for i in items) / 1e9
    cap = request.app.state.settings.naip_max_ingest_gb
    if estimated_gb > cap:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"estimated {estimated_gb:.1f} GB exceeds NAIP_MAX_INGEST_GB={cap:g}",
        )

    year = db.execute(
        select(ImageryYear).where(
            ImageryYear.tenant_id == user.tenant_id,
            ImageryYear.year == body.year,
            ImageryYear.source == "naip",
        )
    ).scalar_one_or_none()
    if year is not None and year.status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"NAIP {body.year} is already {year.status}")
    if year is None:
        year = ImageryYear(
            tenant_id=user.tenant_id, year=body.year, source="naip", created_by=user.id
        )
        db.add(year)
    else:
        # Retry: keep the tiles that did land, drop the ones that failed.
        db.execute(
            delete(ImageryAsset).where(
                ImageryAsset.year_id == year.id, ImageryAsset.status != "ready"
            )
        )
    year.status = "queued"
    year.error = None
    db.flush()
    enqueue(db, "imagery.ingest_naip", user.tenant_id, {"year_id": str(year.id)})
    db.commit()
    db.refresh(year)
    return year_out(year, _assets_by_year(db, [year.id])[year.id])


def _ready_year(db: Session, user: CurrentUser, year_id: uuid.UUID) -> ImageryYear:
    year = get_year(db, user, year_id)
    if year.status != "ready" or year.bounds is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "imagery year is not ready")
    return year


@router.get("/years/{year_id}/thumbnail.png", response_class=Response)
def year_thumbnail(
    year_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """A small rendering of the whole year, for the Imagery page."""
    year = _ready_year(db, user, year_id)
    bounds = to_shape(year.bounds).bounds
    assets = assets_intersecting(db, user.tenant_id, year.id, bounds)
    img = read_bounds_preview(request.app.state.settings, assets, bounds)
    if img is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(img.render(img_format="PNG"), media_type="image/png", headers=PNG_HEADERS)


@router.get("/years/{year_id}/parcels/{parcel_id}/preview.png", response_class=Response)
def parcel_preview(
    year_id: uuid.UUID,
    parcel_id: uuid.UUID,
    request: Request,
    size: int = Query(512, ge=64, le=2048),
    buffer: float = Query(0.25, ge=0, le=2, description="context as a fraction of the parcel"),
    outline: bool = Query(False, description="draw the parcel boundary"),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """The parcel and its surroundings, north-up, with an ``X-Bounds`` (EPSG:4326) header."""
    year = _ready_year(db, user, year_id)
    parcel = db.get(Parcel, parcel_id)
    if parcel is None or parcel.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel not found")
    geom = to_shape(parcel.geom)
    # The extent lives in `imagery.preview` so the run overlay renders the same ground.
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
    if outline:
        paint(rgb, raster, view.geom_utm, OUTLINE_RGB, width_px=OUTLINE_PX, outline_only=True)
    body, bounds = render_png(raster, rgb)
    return Response(
        body, media_type="image/png", headers={**PNG_HEADERS, "X-Bounds": bounds}
    )


