"""The ground a parcel's images cover, and how layers are drawn on them.

A parcel is shown as several images — base year, target year, target year with a run's
detections marked — rendered by *different endpoints*. They only compare if every one
lands on the same ground at the same scale, so the extent is computed here, once, and
every endpoint calls this rather than repeating the arithmetic.

Extraction alone does not make them agree: identical output needs identical inputs, so
the callers must also pass the same ``size`` and ``buffer``. See the overlay route's
parameter contract in `ptax.api.runs`.
"""

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
from rio_tiler.models import ImageData
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from ptax.detection.detector import ParcelRaster
from ptax.parcels.footprint import utm_epsg_for

#: Markup colours, shared by the parcel viewer's overlay and the evaluation chips so the
#: two read the same way. Mutually distinguishable and distinct from the yellow/white
#: parcel outlines: the structure the score rests on, and the rest of what was detected.
STRUCTURE_RGB = (255, 60, 60)
NEW_BUILTUP_RGB = (80, 160, 255)


@dataclass(frozen=True)
class ParcelView:
    """Where a parcel's imagery should be read, and on what grid."""

    utm: int
    geom_utm: BaseGeometry
    buffer_m: float
    resolution_m: float
    #: EPSG:4326 bbox to search for intersecting assets, parcel plus context.
    search_bbox: tuple[float, float, float, float]


def plan_view(geom: BaseGeometry, *, size: int, buffer: float) -> ParcelView:
    """Resolve the read window for ``geom`` at ``size`` pixels with ``buffer`` context.

    Pure and deterministic: the same parcel, size and buffer always give the same window,
    which is exactly what makes two different years — read from two different sets of
    assets — land on the same ground.
    """
    utm = utm_epsg_for(geom)
    geom_utm = shapely_transform(
        Transformer.from_crs(4326, utm, always_xy=True).transform, geom
    )
    minx, miny, maxx, maxy = geom_utm.bounds
    long_side = max(maxx - minx, maxy - miny)
    buffer_m = long_side * buffer
    resolution_m = (long_side + 2 * buffer_m) / size

    west, south, east, north = geom.bounds
    dx, dy = (east - west) * buffer, (north - south) * buffer
    return ParcelView(
        utm=utm,
        geom_utm=geom_utm,
        buffer_m=buffer_m,
        resolution_m=resolution_m,
        search_bbox=(west - dx, south - dy, east + dx, north + dy),
    )


def paint(
    rgb: np.ndarray,
    raster: ParcelRaster,
    geometry: BaseGeometry,
    colour: tuple[int, int, int],
    *,
    width_px: int = 1,
    outline_only: bool = False,
) -> None:
    """Draw ``geometry`` onto ``rgb`` in place, on ``raster``'s grid.

    One code path for the parcel boundary and for every markup layer, so a caller cannot
    accidentally register one differently from another. ``outline_only`` traces the
    boundary; otherwise the interior is filled.
    """
    target = geometry.boundary if outline_only else geometry
    if target.is_empty:
        return
    drawn = rasterize(
        [(target, 1)],
        out_shape=raster.parcel_mask.shape,
        transform=raster.transform,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)
    for _ in range(width_px - 1):
        grown = drawn.copy()
        grown[1:] |= drawn[:-1]
        grown[:-1] |= drawn[1:]
        grown[:, 1:] |= drawn[:, :-1]
        grown[:, :-1] |= drawn[:, 1:]
        drawn = grown
    for band, value in enumerate(colour):
        rgb[band][drawn] = value


def render_png(raster: ParcelRaster, rgb: np.ndarray) -> tuple[bytes, str]:
    """(PNG bytes, ``X-Bounds`` header value) for ``rgb`` on ``raster``'s grid."""
    image = ImageData(
        np.ma.MaskedArray(rgb, mask=np.broadcast_to(~raster.mask, rgb.shape)),
        crs=raster.crs,
        bounds=raster.bounds,
    )
    bounds_4326 = transform_bounds(raster.crs, "EPSG:4326", *raster.bounds)
    return image.render(img_format="PNG"), ",".join(f"{v:.7f}" for v in bounds_4326)
