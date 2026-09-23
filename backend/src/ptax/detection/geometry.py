"""Detected pixel masks as storable, drawable geometry.

`ClassicalDetector.compare` works in pixels on a grid whose resolution is derived from
both imagery years and the parcel's extent. The viewer draws on a *different* grid: a
requested pixel size with a buffer of surrounding context. Nothing registers across that
change of grid except geometry, so a detection is stored as EPSG:4326 polygons and
re-rasterised at whatever resolution the display asks for.

Pure: numpy and shapely in, geometry out. No database, no imagery, no HTTP.
"""

from collections.abc import Callable
from functools import lru_cache

import numpy as np
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.features import shapes
from rasterio.transform import Affine
from shapely.geometry import MultiPolygon, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

#: Default simplification, as a multiple of the source grid's pixel size. Polygonising a
#: raster yields a vertex at every pixel step, so a diagonal roof edge costs one vertex
#: per pixel; half a pixel of tolerance removes the staircase without moving the boundary
#: anywhere a reader could see at display resolution.
SIMPLIFY_PIXEL_FRACTION = 0.5


@lru_cache(maxsize=32)
def _to_4326(crs_key: str) -> Callable[..., tuple[float, float]]:
    """A cached source-CRS → EPSG:4326 transform.

    Building a `Transformer` costs 0.7 ms, measured — against 0.2 ms for the entire
    polygonisation it supports, and it was being rebuilt twice per parcel. A run visits
    hundreds of thousands of parcels through one or two UTM zones, so this is the same
    handful of transformers over and over. Keyed on the CRS's string form because
    `rasterio.crs.CRS` is not reliably hashable across versions.
    """
    return Transformer.from_crs(CRS.from_string(crs_key), 4326, always_xy=True).transform


def mask_to_multipolygon(
    mask: np.ndarray,
    transform: Affine,
    crs: CRS,
    *,
    simplify_m: float | None = None,
) -> MultiPolygon | None:
    """``mask``'s True pixels as one EPSG:4326 MultiPolygon, or None when nothing is set.

    ``None`` rather than an empty geometry is what lets a caller store NULL: most parcels
    in a county detect nothing, and an empty MultiPolygon in every row would be both
    misleading and a cost paid hundreds of thousands of times.

    ``simplify_m`` is a tolerance in the *source* CRS's units (metres for the projected
    grids this runs on); it defaults to half a pixel. Pass ``0.0`` to keep every vertex.
    """
    if not mask.any():
        return None

    pixel_m = abs(transform.a)
    tolerance = SIMPLIFY_PIXEL_FRACTION * pixel_m if simplify_m is None else simplify_m

    # `shapes` walks the raster once and yields one feature per connected run of equal
    # values; masking to the True pixels keeps the False background out of the result.
    binary = mask.astype(np.uint8)
    parts = [shape(geom) for geom, _ in shapes(binary, mask=mask, transform=transform)]
    if not parts:
        return None

    merged = unary_union(parts)
    if tolerance > 0:
        merged = merged.simplify(tolerance, preserve_topology=True)

    merged = shapely_transform(_to_4326(crs.to_string()), merged)

    # The storage column is MULTIPOLYGON: a single blob still has to arrive as one.
    if merged.geom_type == "Polygon":
        return MultiPolygon([merged])
    if merged.geom_type == "MultiPolygon":
        return merged
    # Unreachable in practice, and deliberately kept: `simplify(preserve_topology=True)`
    # over polygonal input returned only Polygon or MultiPolygon across 400 random masks
    # at four tolerances when this was checked. Should a shapely change ever break that,
    # dropping a run's markup for one parcel is a far better failure than raising and
    # failing a county-sized run, so this keeps the areal parts and moves on.
    polygons = [g for g in getattr(merged, "geoms", []) if g.geom_type == "Polygon"]
    return MultiPolygon(polygons) if polygons else None
