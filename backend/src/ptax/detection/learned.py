"""A detector that segments buildings in each year and scores the building that appeared.

The classical detector's measured failure is graded ground: bright, smooth, compact,
recently changed soil reads as a structure, and its highest-scoring parcels were lots
prepared for building but not yet built on. A model trained to see *buildings* should
not make that mistake, and it needs no change labels -- only building labels, which open
footprint data supplies. So this detector asks the segmenter "where are the buildings?"
of each year, and the change is the building area present in the target and not in the
base.

Everything after the two segmentations follows the classical detector's contract, so runs,
the parcel viewer and the evaluation harness consume either one unchanged: the same score
curve over the largest contiguous new structure, the same candidate rule, the same
coverage refusal, and the same two markup masks on the comparison grid.

The model itself is injected as ``predict(rgb) -> probability``. That keeps this module
free of torch -- production images never install it -- and lets its logic be tested with
a stub. `from_model_card` builds the real one, and only from weights whose hash matches
the frozen card.
"""

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ptax.detection.detector import (
    ChangeResult,
    ClassicalDetector,
    InsufficientCoverage,
    ParcelRaster,
    RadiometricFit,
    _dilate,
    _largest_structure,
)
from ptax.detection.model_store import sha256_file

Predict = Callable[[np.ndarray], np.ndarray]

#: The candidate the decision gate judges. Relative to the backend directory, where
#: `ptax-eval` runs, like every other evaluation path.
DEFAULT_CARD = Path("eval") / "models" / "segmenter-v1.json"


class SegmentationDetector:
    """New building area between two years' segmentations, scored as the classical one is."""

    MAX_NODATA_FRAC = ClassicalDetector.MAX_NODATA_FRAC
    SCORE_HALF_STRUCTURE_M2 = ClassicalDetector.SCORE_HALF_STRUCTURE_M2

    def __init__(self, predict: Predict, cutoff: float, name: str = "segmenter") -> None:
        self.predict = predict
        self.cutoff = cutoff
        self.name = name

    def compare(
        self,
        base: ParcelRaster,
        target: ParcelRaster,
        *,
        threshold: float,
        min_new_area_m2: float,
        fit: RadiometricFit | None = None,
    ) -> ChangeResult:
        """Score the largest building present in ``target`` and absent from ``base``.

        ``fit`` is accepted for the `Detector` contract and ignored: the segmenter was
        trained across four NAIP years with brightness and contrast jitter, so it absorbs
        capture-to-capture differences itself, and a correction fitted for the classical
        detector's NIR thresholds would only move the input away from what it learned on.
        """
        del fit
        if base.data.shape[1:] != target.data.shape[1:]:
            raise ValueError("base and target rasters must share one grid")
        parcel = base.parcel_mask & target.parcel_mask
        parcel_px = int(parcel.sum())
        if parcel_px == 0:
            raise InsufficientCoverage("base", 1.0)
        for which, raster in (("base", base), ("target", target)):
            nodata_frac = 1.0 - float((raster.mask & parcel).sum()) / parcel_px
            if nodata_frac > self.MAX_NODATA_FRAC:
                raise InsufficientCoverage(which, nodata_frac)
        valid = parcel & base.mask & target.mask
        nodata_frac = 1.0 - float(valid.sum()) / parcel_px

        base_building = self._buildings(base)
        target_building = self._buildings(target)
        # One pixel of growth absorbs sub-pixel misregistration between the captures, so
        # the edge of a roof standing in both years is not counted as new.
        new_building = target_building & ~_dilate(base_building) & valid

        px_area = base.resolution_m * base.resolution_m
        # The segmenter already encodes what a building looks like, so no shape filter.
        structure_px, structure_fill, structure_mask = _largest_structure(new_building, 0.0)
        structure_m2 = float(structure_px) * px_area
        new_building_m2 = float(new_building.sum()) * px_area
        score = structure_m2 / (structure_m2 + self.SCORE_HALF_STRUCTURE_M2)
        candidate = bool(score >= threshold and structure_m2 >= min_new_area_m2)
        valid_px = max(int(valid.sum()), 1)
        return ChangeResult(
            score=round(score, 4),
            candidate=candidate,
            indicators={
                "structure_m2": round(structure_m2, 1),
                "structure_fill": round(structure_fill, 3),
                "new_builtup_m2": round(new_building_m2, 1),
                "base_building_frac": round(float((base_building & valid).sum()) / valid_px, 4),
                "target_building_frac": round(float((target_building & valid).sum()) / valid_px, 4),
                "nodata_frac": round(nodata_frac, 4),
                "resolution_m": base.resolution_m,
                "model": self.name,
            },
            new_builtup_mask=new_building,
            structure_mask=structure_mask,
        )

    def _buildings(self, raster: ParcelRaster) -> np.ndarray:
        """Building mask for one year: RGB with missing pixels zeroed, as in training."""
        rgb = np.asarray(raster.data[:3], dtype=np.uint8).copy()
        rgb[:, ~raster.mask] = 0
        building: np.ndarray = self.predict(rgb) >= self.cutoff
        return building & raster.mask


def _load_predictor(weights: Path) -> Predict:
    """The trained model as ``predict``. Imports torch, so only called on demand."""
    from ptax.learn.model import load

    return load(weights)


def from_model_card(card_path: Path | None = None) -> SegmentationDetector:
    """The frozen candidate named by ``card_path``, refusing weights the card did not freeze.

    The decision gate judges one exact model. If the weights on disk were retrained or
    swapped after the card was written, their hash no longer matches, and scoring them
    would report a result for a model nobody froze.
    """
    path = card_path or DEFAULT_CARD
    card = json.loads(path.read_text())
    weights = path.parent / card["weights"]
    actual = sha256_file(weights)
    if actual != card["weights_sha256"]:
        raise ValueError(
            f"{weights} has sha256 {actual}, but {path} froze {card['weights_sha256']};"
            " retrained weights are a new candidate with a new card"
        )
    return SegmentationDetector(
        _load_predictor(weights), cutoff=float(card["cutoff"]), name=str(card["name"])
    )
