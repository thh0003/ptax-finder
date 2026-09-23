"""Training data for the building segmenter: where it may come from, and how it is cut.

The one thing this module must never get wrong is leakage: a training chip drawn from the
evaluation AOI would make every score the gate reads self-graded. The rest pins the chip
geometry, because a label shifted one pixel off its image teaches a roof's edge as ground.
"""

import numpy as np
import pytest
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.ops import transform as shapely_transform

from ptax.detection.detector import ParcelRaster
from ptax.eval.dataset import AOIS
from ptax.eval.training_data import (
    CHIP_PX,
    TRAIN_AOIS,
    assert_disjoint_from_eval,
    built_out_share,
    chip_boxes,
    make_chip,
    screen_tiles,
    split_for,
)

DISTANCE_CRS = 26915
TO_DISTANCE = Transformer.from_crs(4326, DISTANCE_CRS, always_xy=True).transform
FROM_DISTANCE = Transformer.from_crs(DISTANCE_CRS, 4326, always_xy=True).transform


def _east_of_eval(metres: float) -> tuple[float, float, float, float]:
    """A 1 m box ``metres`` due east of the evaluation AOI's east edge, in EPSG:26915."""
    west, south, east, north = AOIS["nw-hennepin"]
    edge_x, edge_y = TO_DISTANCE(east, (south + north) / 2)
    corner = box(edge_x + metres, edge_y, edge_x + metres + 1.0, edge_y + 1.0)
    return shapely_transform(FROM_DISTANCE, corner).bounds


def test_an_aoi_overlapping_the_evaluation_area_is_refused() -> None:
    west, south, east, north = AOIS["nw-hennepin"]

    with pytest.raises(ValueError, match="nw-hennepin"):
        assert_disjoint_from_eval((west + 0.01, south + 0.01, east + 0.05, north))


def test_an_aoi_999_metres_away_is_refused() -> None:
    with pytest.raises(ValueError, match="nw-hennepin"):
        assert_disjoint_from_eval(_east_of_eval(999.0))


def test_an_aoi_1001_metres_away_is_accepted() -> None:
    assert_disjoint_from_eval(_east_of_eval(1001.0))


def test_distance_is_measured_in_metres_not_degrees() -> None:
    """0.012 deg of longitude is ~943 m at 45 N; read as latitude degrees it is ~1 330 m."""
    west, south, east, north = AOIS["nw-hennepin"]

    with pytest.raises(ValueError):
        assert_disjoint_from_eval((east + 0.012, south, east + 0.02, north))


@pytest.mark.parametrize("name", sorted(TRAIN_AOIS))
def test_every_training_aoi_clears_the_evaluation_area(name: str) -> None:
    assert_disjoint_from_eval(TRAIN_AOIS[name])


def test_built_out_share_counts_only_parcels_with_a_year() -> None:
    parcels = [{"BUILD_YR": "1990"}, {"BUILD_YR": "2005"}, {"BUILD_YR": "0000"}, {}]

    share, dated = built_out_share(parcels)

    assert (share, dated) == (0.5, 2)


def _raster(height: int, width: int) -> tuple[ParcelRaster, float, float]:
    origin_e, origin_n = 478_000.0, 4_970_000.0
    data = np.zeros((3, height, width), dtype=np.uint8)
    raster = ParcelRaster(
        data=data,
        mask=np.ones((height, width), dtype=bool),
        parcel_mask=np.ones((height, width), dtype=bool),
        transform=from_origin(origin_e, origin_n, 1.0, 1.0),
        crs=CRS.from_epsg(32615),
        resolution_m=1.0,
        bounds=(origin_e, origin_n - height, origin_e + width, origin_n),
    )
    return raster, origin_e, origin_n


def test_a_chips_label_lines_up_with_its_image_pixel_for_pixel() -> None:
    raster, origin_e, origin_n = _raster(CHIP_PX, CHIP_PX)
    raster.data[:, 100:120, 50:80] = 255
    roof = box(origin_e + 50, origin_n - 120, origin_e + 80, origin_n - 100)
    to_wgs84 = Transformer.from_crs(32615, 4326, always_xy=True).transform

    chip = make_chip(raster, [shapely_transform(to_wgs84, roof)])

    assert chip.image.shape == (3, CHIP_PX, CHIP_PX)
    assert np.array_equal(chip.label, chip.image[0] == 255)


def test_a_chip_read_a_pixel_large_is_cropped_and_a_short_one_padded_as_invalid() -> None:
    large, _, _ = _raster(CHIP_PX + 1, CHIP_PX + 1)
    short, _, _ = _raster(CHIP_PX - 2, CHIP_PX)

    assert make_chip(large, []).image.shape == (3, CHIP_PX, CHIP_PX)
    padded = make_chip(short, [])
    assert padded.valid.shape == (CHIP_PX, CHIP_PX)
    assert not padded.valid[-2:].any()
    assert padded.valid[: CHIP_PX - 2].all()


def test_chips_tile_an_aoi_in_whole_chip_steps() -> None:
    boxes = chip_boxes(TRAIN_AOIS[sorted(TRAIN_AOIS)[0]])

    assert boxes
    rows = {b.row for b in boxes}
    cols = {b.col for b in boxes}
    assert len(boxes) == len(rows) * len(cols)
    first = next(b for b in boxes if b.row == 0 and b.col == 0)
    to_utm = Transformer.from_crs(4326, 32615, always_xy=True).transform
    minx, miny, maxx, maxy = shapely_transform(to_utm, first.geometry).bounds
    assert maxx - minx == pytest.approx(CHIP_PX, abs=0.5)
    assert maxy - miny == pytest.approx(CHIP_PX, abs=0.5)


def test_the_split_is_by_place_so_every_year_of_a_tile_lands_on_the_same_side() -> None:
    columns = 10

    sides = [split_for(col, columns) for col in range(columns)]

    assert sides.count("validation") == 2
    # A contiguous strip, so a validation tile's neighbours are mostly validation too.
    assert sides[-2:] == ["validation", "validation"]


def test_the_label_screen_excludes_tiles_far_from_their_aoi_median() -> None:
    fractions = {(0, c): 0.20 + 0.01 * (c % 3) for c in range(20)}
    fractions[(0, 5)] = 0.0  # a footprint gap in a built-out neighbourhood
    fractions[(0, 6)] = 0.9

    excluded = screen_tiles(fractions)

    assert set(excluded) == {(0, 5), (0, 6)}
    assert all("median" in reason for reason in excluded.values())
