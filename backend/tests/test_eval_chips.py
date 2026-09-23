"""Contact-sheet rendering for the evaluation set, including the detector's markup.

The chips were built for labelling by eye, and that use has to stay blind to the detector:
the 300 visual labels were read without seeing what it marked. The markup panel is
therefore opt-in, and without it a pair must render exactly as before.
"""

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ptax.detection.detector import ParcelRaster
from ptax.eval.chips import GUTTER, OUTLINE_VALUE, marked, pair
from ptax.eval.cli import _rank
from ptax.imagery.preview import NEW_BUILTUP_RGB, STRUCTURE_RGB

SIZE = 20


def _raster(value: int = 90) -> ParcelRaster:
    """A uniform 20 x 20 parcel, fully covered, with the parcel filling rows/cols 2..17."""
    data = np.full((4, SIZE, SIZE), value, dtype=np.uint8)
    data[:, 0, 0] = 200  # some spread so the percentile stretch has a range to work with
    parcel = np.zeros((SIZE, SIZE), dtype=bool)
    parcel[2:18, 2:18] = True
    return ParcelRaster(
        data=data,
        mask=np.ones((SIZE, SIZE), dtype=bool),
        parcel_mask=parcel,
        transform=from_origin(500_000, 5_000_000, 1.0, 1.0),
        crs=CRS.from_epsg(26915),
        resolution_m=1.0,
        bounds=(500_000, 4_999_980, 500_020, 5_000_000),
    )


def _masks() -> tuple[np.ndarray, np.ndarray]:
    """A wide detection with a smaller scoring structure inside it."""
    new_builtup = np.zeros((SIZE, SIZE), dtype=bool)
    new_builtup[5:14, 5:14] = True
    structure = np.zeros((SIZE, SIZE), dtype=bool)
    structure[7:11, 7:11] = True
    return new_builtup, structure


def test_the_marked_panel_draws_both_layers_in_their_own_colours() -> None:
    new_builtup, structure = _masks()

    rgb = marked(_raster(), new_builtup, structure)

    assert tuple(rgb[8, 8]) == STRUCTURE_RGB  # inside the scoring structure
    assert tuple(rgb[5, 5]) == NEW_BUILTUP_RGB  # detected, but not what scored
    assert tuple(rgb[15, 15]) not in (STRUCTURE_RGB, NEW_BUILTUP_RGB)  # untouched ground


def test_the_colours_match_the_viewer_so_the_two_views_read_the_same() -> None:
    from ptax.api import runs

    assert runs.STRUCTURE_RGB == STRUCTURE_RGB
    assert runs.NEW_BUILTUP_RGB == NEW_BUILTUP_RGB


def test_the_parcel_outline_stays_visible_over_the_markup() -> None:
    """A detection that reaches the boundary must not paint over it."""
    new_builtup = np.zeros((SIZE, SIZE), dtype=bool)
    new_builtup[2:18, 2:18] = True  # covers the whole parcel, boundary included
    structure = np.zeros((SIZE, SIZE), dtype=bool)

    rgb = marked(_raster(), new_builtup, structure)

    assert tuple(rgb[2, 10]) == (OUTLINE_VALUE,) * 3


def test_masks_on_a_different_grid_are_refused() -> None:
    wrong = np.zeros((SIZE + 1, SIZE), dtype=bool)
    with pytest.raises(ValueError):
        marked(_raster(), wrong, wrong)


def test_without_markup_a_pair_is_two_panels_wide() -> None:
    """The labelling path: unchanged from before the markup existed."""
    image = pair(_raster(), _raster(120))
    assert image.shape == (SIZE, 2 * SIZE + GUTTER, 3)


def test_with_markup_a_pair_gains_a_third_panel() -> None:
    new_builtup, structure = _masks()

    image = pair(_raster(), _raster(120), marks=(new_builtup, structure))

    assert image.shape == (SIZE, 3 * SIZE + 2 * GUTTER, 3)
    third = image[:, 2 * (SIZE + GUTTER) :]
    assert tuple(third[8, 8]) == STRUCTURE_RGB
    # The first two panels carry no markup at all.
    first_two = image[:, : 2 * SIZE + GUTTER].reshape(-1, 3)
    assert not any(tuple(p) in (STRUCTURE_RGB, NEW_BUILTUP_RGB) for p in first_two)


def test_ranking_puts_the_highest_score_first_and_unscored_parcels_last() -> None:
    order = _rank({"a": 0.2, "b": None, "c": 0.9, "d": 0.2, "e": 0.0})

    # Ties break on the parcel id so the same set always yields the same sheets.
    assert order == ["c", "a", "d", "e", "b"]


def test_a_detector_aware_index_names_the_detector_and_a_blind_one_does_not(tmp_path) -> None:
    """Sheets from two detectors must be told apart; the blind labelling index stays as it was."""
    import json

    from ptax.eval.chips import ChipEntry, write_index
    from ptax.eval.dataset import POSITIVE

    entry = ChipEntry("p1", POSITIVE, 0, 0, "a.png", score=0.5)
    write_index(tmp_path / "aware.json", [entry], 2, detector="segmentation")
    write_index(tmp_path / "blind.json", [entry], 2)

    assert json.loads((tmp_path / "aware.json").read_text())["detector"] == "segmentation"
    assert "detector" not in json.loads((tmp_path / "blind.json").read_text())

