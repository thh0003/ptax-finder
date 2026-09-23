"""Reading stored imagery back: map tiles, previews, and per-parcel rasters.

Everything goes through rio-tiler mosaics over the ``imagery_assets`` intersecting the
request, inside ``rasterio.Env(**gdal_env(settings))`` so ``s3://`` URIs resolve to
MinIO locally and to S3 (task role) in AWS.
"""

import math
import uuid
from typing import Any

import morecantile
import numpy as np
import rasterio
from geoalchemy2.shape import from_shape
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from rio_tiler.errors import EmptyMosaicError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData
from rio_tiler.mosaic import mosaic_reader
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.config import Settings
from ptax.db.models import ImageryAsset
from ptax.detection.detector import ParcelRaster
from ptax.imagery.gdal import gdal_env, s3_uri
from ptax.parcels.footprint import utm_epsg_for

TMS = morecantile.tms.get("WebMercatorQuad")
TILE_SIZE = 256
RGB = (1, 2, 3)
MIN_ZOOM = 8
# Web Mercator ground resolution at zoom 22 (equator) is ~0.037 m/px.
ZOOM_22_RES_M = 0.037


def zoom_range(resolution_m: float | None) -> tuple[int, int]:
    if not resolution_m or resolution_m <= 0:
        return MIN_ZOOM, 22
    max_zoom = 22 - int(math.ceil(math.log2(resolution_m / ZOOM_22_RES_M)))
    return MIN_ZOOM, max(MIN_ZOOM, min(max_zoom, 22))


def assets_intersecting(
    db: Session,
    tenant_id: uuid.UUID,
    year_id: uuid.UUID,
    bounds_4326: tuple[float, float, float, float],
) -> list[ImageryAsset]:
    envelope = from_shape(box(*bounds_4326), srid=4326)
    return list(
        db.execute(
            select(ImageryAsset)
            .where(
                ImageryAsset.tenant_id == tenant_id,
                ImageryAsset.year_id == year_id,
                ImageryAsset.status == "ready",
                ImageryAsset.bounds.ST_Intersects(envelope),
            )
            .order_by(ImageryAsset.created_at)
        ).scalars()
    )


def _uris(settings: Settings, assets: list[ImageryAsset]) -> list[str]:
    return [s3_uri(settings, a.s3_key) for a in assets]


def read_tile(
    settings: Settings, assets: list[ImageryAsset], z: int, x: int, y: int
) -> ImageData | None:
    if not assets:
        return None
    env = gdal_env(settings)

    # rasterio.Env is thread-local and mosaic_reader reads in a pool: enter it per read.
    def tiler(uri: str, *args, **kwargs) -> ImageData:  # noqa: ANN002, ANN003
        with rasterio.Env(**env), Reader(uri) as src:
            return src.tile(*args, **kwargs)

    try:
        img, _ = mosaic_reader(
            _uris(settings, assets), tiler, x, y, z, tilesize=TILE_SIZE, indexes=RGB
        )
    except (EmptyMosaicError, TileOutsideBounds):
        return None
    return img


def read_bounds_preview(
    settings: Settings,
    assets: list[ImageryAsset],
    bounds_4326: tuple[float, float, float, float],
    max_size: int = 256,
) -> ImageData | None:
    """A small RGB rendering of ``bounds_4326`` (for thumbnails), in Web Mercator."""
    if not assets:
        return None
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", "EPSG:3857", *bounds_4326)
    width, height = _fit(maxx - minx, maxy - miny, max_size)
    return _read_part(
        gdal_env(settings),
        _uris(settings, assets),
        (minx, miny, maxx, maxy),
        CRS.from_epsg(3857),
        width,
        height,
        RGB,
    )


def read_parcel(
    settings: Settings,
    assets: list[ImageryAsset],
    geom_4326: BaseGeometry,
    *,
    resolution_m: float,
    buffer_m: float = 0.0,
) -> ParcelRaster | None:
    """The parcel (plus ``buffer_m`` around it) resampled onto a UTM grid at ``resolution_m``."""
    return read_parcel_uris(
        gdal_env(settings),
        _uris(settings, assets),
        geom_4326,
        resolution_m=resolution_m,
        buffer_m=buffer_m,
    )


def read_parcel_uris(
    env: dict[str, Any],
    uris: list[str],
    geom_4326: BaseGeometry,
    *,
    resolution_m: float,
    buffer_m: float = 0.0,
) -> ParcelRaster | None:
    """``read_parcel`` by URI rather than by asset row.

    The offline evaluation harness (``ptax.eval``) reads cached local COGs and must warp
    them exactly as the run job warps stored assets: the detector defect it measures is a
    property of how two years land on one comparison grid, so a second reader would be
    measuring a different program. Everything shared lives here; ``read_parcel`` only
    resolves settings into an env and asset rows into URIs.
    """
    if not uris:
        return None
    utm = utm_epsg_for(geom_4326)
    to_utm = Transformer.from_crs(4326, utm, always_xy=True).transform
    geom_utm = shapely_transform(to_utm, geom_4326)
    minx, miny, maxx, maxy = geom_utm.bounds
    minx, miny, maxx, maxy = minx - buffer_m, miny - buffer_m, maxx + buffer_m, maxy + buffer_m
    width = max(1, int(math.ceil((maxx - minx) / resolution_m)))
    height = max(1, int(math.ceil((maxy - miny) / resolution_m)))
    # Snap the extent to whole pixels so the grid really is `resolution_m`.
    maxx, miny = minx + width * resolution_m, maxy - height * resolution_m
    crs = CRS.from_epsg(utm)
    img = _read_part(env, uris, (minx, miny, maxx, maxy), crs, width, height)
    if img is None:
        return None
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    parcel_mask = rasterize(
        [(geom_utm, 1)], out_shape=(height, width), transform=transform, dtype="uint8"
    ).astype(bool)
    return ParcelRaster(
        data=np.asarray(img.data, dtype=np.uint8),
        mask=np.asarray(img.mask) > 0,
        parcel_mask=parcel_mask,
        transform=transform,
        crs=crs,
        resolution_m=resolution_m,
        bounds=(minx, miny, maxx, maxy),
    )


def _read_part(
    env: dict[str, Any],
    uris: list[str],
    bounds: tuple[float, float, float, float],
    crs: CRS,
    width: int,
    height: int,
    indexes: tuple[int, ...] | None = None,
) -> ImageData | None:
    def part(uri: str, *args, **kwargs) -> ImageData:  # noqa: ANN002, ANN003
        with rasterio.Env(**env), Reader(uri) as src:
            return src.part(*args, **kwargs)

    try:
        img, _ = mosaic_reader(
            uris,
            part,
            bounds,
            bounds_crs=crs,
            dst_crs=crs,
            width=width,
            height=height,
            max_size=None,
            indexes=indexes,
        )
    except (EmptyMosaicError, TileOutsideBounds):
        return None
    return img


def _fit(extent_x: float, extent_y: float, max_size: int) -> tuple[int, int]:
    scale = max_size / max(extent_x, extent_y)
    return max(1, round(extent_x * scale)), max(1, round(extent_y * scale))
