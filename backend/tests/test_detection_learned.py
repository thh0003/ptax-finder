"""The segmentation detector's change logic, with a stub standing in for the model.

What the model sees is measured against the visual labels by `ptax-eval score`. What is
pinned here is everything around it: how two years' building masks become new area, a
structure and a score on the same contract as the classical detector -- so runs, the
viewer and the evaluation harness consume either detector unchanged. No torch needed.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ptax.detection import learned
from ptax.detection.detector import (
    ChangeResult,
    ClassicalDetector,
    InsufficientCoverage,
    ParcelRaster,
)
from ptax.detection.learned import SegmentationDetector
from ptax.detection.registry import get_detector

SIZE = 64


def _raster(
    buildings: np.ndarray | None = None,
    *,
    mask: np.ndarray | None = None,
    resolution: float = 1.0,
) -> ParcelRaster:
    """A raster whose red band encodes where the stub model will see buildings."""
    data = np.zeros((3, SIZE, SIZE), dtype=np.uint8)
    if buildings is not None:
        data[0][buildings] = 255
    return ParcelRaster(
        data=data,
        mask=np.ones((SIZE, SIZE), bool) if mask is None else mask,
        parcel_mask=np.ones((SIZE, SIZE), bool),
        transform=from_origin(0.0, float(SIZE), resolution, resolution),
        crs=CRS.from_epsg(32615),
        resolution_m=resolution,
        bounds=(0.0, 0.0, SIZE * resolution, SIZE * resolution),
    )


def _stub(rgb: np.ndarray) -> np.ndarray:
    """A 'model' that sees a building exactly where the red band is saturated."""
    return (rgb[0] == 255).astype(np.float32)


def _block(top: int, left: int, height: int = 15, width: int = 20) -> np.ndarray:
    out = np.zeros((SIZE, SIZE), bool)
    out[top : top + height, left : left + width] = True
    return out


def _compare(base: ParcelRaster, target: ParcelRaster) -> ChangeResult:
    return SegmentationDetector(_stub, cutoff=0.5).compare(
        base, target, threshold=0.3, min_new_area_m2=37.2
    )


def test_a_building_only_in_the_target_is_the_structure_and_scores_on_the_classical_curve() -> None:
    block = _block(10, 12)

    result = _compare(_raster(), _raster(block))

    assert result.indicators["structure_m2"] == 300.0
    assert np.array_equal(result.structure_mask, block)
    half = ClassicalDetector.SCORE_HALF_STRUCTURE_M2
    assert result.score == pytest.approx(round(300.0 / (300.0 + half), 4))
    assert result.candidate is True


@pytest.mark.parametrize("shift", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_a_building_in_both_years_is_not_new_even_shifted_a_pixel(shift) -> None:
    base = _block(20, 20)
    target = _block(20 + shift[0], 20 + shift[1])

    result = _compare(_raster(base), _raster(target))

    assert result.indicators["new_builtup_m2"] == 0.0
    assert result.score == 0.0
    assert result.candidate is False


def test_the_masks_share_the_grid_and_the_structure_lies_within_the_new_area() -> None:
    target = _block(5, 5) | _block(40, 40, 5, 5)

    result = _compare(_raster(), _raster(target))

    assert result.new_builtup_mask is not None and result.structure_mask is not None
    assert result.new_builtup_mask.shape == result.structure_mask.shape == (SIZE, SIZE)
    assert not (result.structure_mask & ~result.new_builtup_mask).any()
    assert result.indicators["new_builtup_m2"] == 325.0


def test_area_follows_the_grid_resolution() -> None:
    result = SegmentationDetector(_stub, cutoff=0.5).compare(
        _raster(resolution=0.5),
        _raster(_block(10, 12), resolution=0.5),
        threshold=0.3,
        min_new_area_m2=37.2,
    )

    assert result.indicators["structure_m2"] == 75.0


@pytest.mark.parametrize("which", ["base", "target"])
def test_too_little_imagery_is_reported_for_the_right_year(which: str) -> None:
    sparse = np.zeros((SIZE, SIZE), bool)
    sparse[:10] = True
    rasters = {"base": _raster(), "target": _raster()}
    rasters[which] = _raster(mask=sparse)

    with pytest.raises(InsufficientCoverage) as excinfo:
        _compare(rasters["base"], rasters["target"])

    assert excinfo.value.which == which


def _card(tmp_path: Path, weights: bytes, recorded: bytes | None = None) -> Path:
    (tmp_path / "segmenter-test.pt").write_bytes(weights)
    card = tmp_path / "segmenter-test.json"
    digest = hashlib.sha256(recorded if recorded is not None else weights).hexdigest()
    card.write_text(
        json.dumps(
            {
                "name": "segmenter-test",
                "weights": "segmenter-test.pt",
                "weights_sha256": digest,
                "cutoff": 0.35,
            }
        )
    )
    return card


def test_the_registry_builds_the_frozen_candidate_with_its_cards_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[Path] = []
    monkeypatch.setattr(learned, "DEFAULT_CARD", _card(tmp_path, b"weights"))
    monkeypatch.setattr(learned, "_load_predictor", lambda path: loaded.append(path) or _stub)

    detector = get_detector("segmentation")

    assert isinstance(detector, SegmentationDetector)
    assert detector.cutoff == 0.35
    assert detector.name == "segmenter-test"
    assert loaded == [tmp_path / "segmenter-test.pt"]


def test_weights_that_do_not_match_the_card_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(learned, "DEFAULT_CARD", _card(tmp_path, b"retrained", recorded=b"frozen"))
    monkeypatch.setattr(learned, "_load_predictor", lambda path: _stub)

    with pytest.raises(ValueError, match="sha256"):
        get_detector("segmentation")
