"""From a model's instance mask to detections on a parcel, with areas in square feet."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache

import numpy as np
import shapely
from pyproj import CRS
from rasterio.features import shapes
from rasterio.transform import Affine
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from ptax.reconcile.types import Detection, Year

#: One international foot, in metres.
FOOT_M = 0.3048


@dataclass(frozen=True)
class InstanceInfo:
    """What the model said about one instance label."""

    cls: str
    score: float
    occluded: bool = False
    tile_flagged: bool = False


def vectorize_instances(
    labels: np.ndarray,
    instances: Mapping[int, InstanceInfo],
    transform: Affine,
    *,
    year: Year,
) -> list[Detection]:
    """One detection per labelled instance, polygonised on the mask's grid and simplified
    by half a pixel. Labels without an ``instances`` entry (background, 0) are ignored."""
    tolerance = abs(transform.a) / 2
    pieces: dict[int, list[BaseGeometry]] = {}
    for geometry, value in shapes(labels.astype(np.int32), transform=transform):
        label = int(value)
        if label in instances:
            pieces.setdefault(label, []).append(shape(geometry))
    detections = []
    for label, parts in sorted(pieces.items()):
        info = instances[label]
        geom = shapely.union_all(parts).simplify(tolerance, preserve_topology=True)
        detections.append(
            Detection(
                id=f"{year}-{label}",
                year=year,
                cls=info.cls,
                score=info.score,
                geom=geom,
                occluded=info.occluded,
                tile_flagged=info.tile_flagged,
            )
        )
    return detections


def on_parcel(detections: Iterable[Detection], parcel: BaseGeometry) -> list[Detection]:
    """The detections whose centroid lies strictly inside the parcel, kept whole.

    A structure crossing a parcel line is counted on exactly one parcel -- the one holding
    its centroid -- and never clipped, so its area is the structure's, not a fragment's.
    """
    return [d for d in detections if parcel.contains(d.geom.centroid)]


@cache
def _sqft_per_unit2(crs_epsg: int) -> float:
    metres_per_unit = CRS.from_epsg(crs_epsg).axis_info[0].unit_conversion_factor
    return (metres_per_unit / FOOT_M) ** 2


def feet_per_unit(crs_epsg: int) -> float:
    """International feet in one linear unit of the projected CRS ``crs_epsg``."""
    return float(_sqft_per_unit2(crs_epsg) ** 0.5)


def area_sqft(geom: BaseGeometry, crs_epsg: int) -> float:
    """Area in international square feet, for a geometry in the projected CRS ``crs_epsg``
    (metres, international feet and US survey feet all convert correctly)."""
    return float(geom.area) * _sqft_per_unit2(crs_epsg)
