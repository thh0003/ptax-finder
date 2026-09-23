"""Turning a detected pixel mask into storable geometry.

The detector works in pixels on a projected grid whose resolution comes from the two
imagery years; the viewer draws on a display grid with a different resolution and a
buffer of surrounding context. Polygons are what survive that change of grid, so this
conversion is the seam between detection and display — and it has to preserve area, or
the markup stops agreeing with the `structure_m2` printed beside it.
"""

import math

import numpy as np
import pytest
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon
from shapely.ops import transform as shapely_transform

from ptax.detection.geometry import mask_to_multipolygon

#: A UTM zone (15N, covering Minnesota) so areas are in metres and comparable to m2.
UTM = CRS.from_epsg(26915)
#: Top-left corner inside that zone, and a 1 m grid.
ORIGIN_X, ORIGIN_Y = 500_000.0, 5_000_000.0


def _grid(resolution_m: float = 1.0):
    return from_origin(ORIGIN_X, ORIGIN_Y, resolution_m, resolution_m)


def _area_m2(geom: MultiPolygon) -> float:
    """Area of a 4326 geometry, measured back in the projected CRS it came from."""
    to_utm = Transformer.from_crs(4326, UTM, always_xy=True).transform
    return shapely_transform(to_utm, geom).area


def test_an_empty_mask_becomes_no_geometry() -> None:
    """Most parcels in a county detect nothing; they must store NULL, not an empty shape."""
    assert mask_to_multipolygon(np.zeros((8, 8), dtype=bool), _grid(), UTM) is None


def test_a_rectangle_keeps_its_area_through_the_projection() -> None:
    mask = np.zeros((20, 20), dtype=bool)
    mask[4:9, 6:14] = True  # 5 rows x 8 cols at 1 m = 40 m2

    geom = mask_to_multipolygon(mask, _grid(), UTM)

    assert geom is not None
    assert _area_m2(geom) == pytest.approx(40.0, rel=0.02)


def test_area_scales_with_the_grid_resolution() -> None:
    """The same pixels on a 2 m grid cover four times the ground."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[4:9, 6:14] = True

    geom = mask_to_multipolygon(mask, _grid(2.0), UTM)

    assert geom is not None
    assert _area_m2(geom) == pytest.approx(160.0, rel=0.02)


def test_the_result_is_in_lon_lat_degrees() -> None:
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 2:5] = True

    geom = mask_to_multipolygon(mask, _grid(), UTM)

    assert geom is not None
    lon, lat = geom.centroid.x, geom.centroid.y
    assert -180 <= lon <= 180 and -90 <= lat <= 90
    # UTM 15N at this northing is in western Minnesota.
    assert -98 < lon < -89 and 44 < lat < 46


def test_separate_blobs_become_separate_parts() -> None:
    """Scattered detections must stay countable — one merged part would hide them."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:5, 2:5] = True
    mask[12:15, 12:15] = True

    geom = mask_to_multipolygon(mask, _grid(), UTM)

    assert geom is not None
    assert len(geom.geoms) == 2


def test_a_multipolygon_is_returned_even_for_one_blob() -> None:
    """The column is MULTIPOLYGON; a bare Polygon would fail the type constraint."""
    mask = np.zeros((10, 10), dtype=bool)
    mask[3:6, 3:6] = True

    geom = mask_to_multipolygon(mask, _grid(), UTM)

    assert isinstance(geom, MultiPolygon)


def test_simplification_drops_vertices_without_moving_the_boundary_far() -> None:
    """Per-pixel staircase edges are what make stored geometry expensive.

    A diagonal edge produces a vertex per pixel step. Simplifying has to cut that count
    while keeping the boundary within the tolerance, or the markup drifts off the roof.
    """
    mask = np.zeros((40, 40), dtype=bool)
    for row in range(4, 36):
        mask[row, 4 : 4 + (row - 3)] = True  # a staircase hypotenuse

    detailed = mask_to_multipolygon(mask, _grid(), UTM, simplify_m=0.0)
    simplified = mask_to_multipolygon(mask, _grid(), UTM, simplify_m=2.0)

    assert detailed is not None and simplified is not None
    detailed_pts = sum(len(p.exterior.coords) for p in detailed.geoms)
    simplified_pts = sum(len(p.exterior.coords) for p in simplified.geoms)
    assert simplified_pts < detailed_pts
    # Area is preserved to within a few percent despite the dropped vertices.
    assert _area_m2(simplified) == pytest.approx(_area_m2(detailed), rel=0.05)


def test_a_mask_that_is_entirely_true_is_still_converted() -> None:
    """A detector failure that marks a whole parcel must be visible, not silently dropped.

    This is the symptom the viewer exists to make obvious, so it is the one case where
    returning None would hide exactly what a reader needs to see.
    """
    mask = np.ones((6, 6), dtype=bool)

    geom = mask_to_multipolygon(mask, _grid(), UTM)

    assert geom is not None
    assert _area_m2(geom) == pytest.approx(36.0, rel=0.02)


def test_area_survives_a_realistic_detection_resolution() -> None:
    """The run job compares at `comparison_resolution`, often 0.6-1.0 m, not exactly 1 m."""
    mask = np.zeros((30, 30), dtype=bool)
    mask[10:20, 10:25] = True  # 10 x 15 px

    geom = mask_to_multipolygon(mask, _grid(0.6), UTM)

    assert geom is not None
    expected = 10 * 15 * 0.6 * 0.6
    assert _area_m2(geom) == pytest.approx(expected, rel=0.02)
    assert not math.isnan(expected)
