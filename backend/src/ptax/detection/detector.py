"""Per-parcel change detection: the ``Detector`` contract and the v1 classical detector.

Both years arrive as ``ParcelRaster`` values on one common grid (see
``ptax.imagery.reader.read_parcel``). The classical detector is pure numpy: it derives
per-pixel vegetation and built-up masks from colour, brightness and texture, cleans them
morphologically, and measures new built-up area and vegetation loss inside the parcel.
A learned detector is a later plan behind the same ``compare`` signature.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import rasterio
from rasterio.crs import CRS


@dataclass(frozen=True)
class ParcelRaster:
    """A parcel's imagery on a north-up projected grid.

    ``data`` is (bands, h, w) uint8 (RGB or RGB+NIR); ``mask`` is True where imagery
    exists; ``parcel_mask`` is True inside the parcel polygon.
    """

    data: np.ndarray
    mask: np.ndarray
    parcel_mask: np.ndarray
    transform: rasterio.Affine
    crs: CRS
    resolution_m: float
    bounds: tuple[float, float, float, float]  # in ``crs``


@dataclass(frozen=True)
class ChangeResult:
    """A parcel's verdict, plus the masks the verdict was computed from.

    The masks travel with the result so a run can record *where* it found the change, on
    the same grid it scored. Recomputing them later would answer with whatever the
    detector does today, which is not necessarily what produced the stored score — and a
    reassessment that gets challenged has to show the picture the decision rested on.

    Both are on the comparison grid, so ``ParcelRaster.transform`` and ``.crs`` georeference
    them. ``structure_mask`` is always a subset of ``new_builtup_mask``. An empty mask is a
    real measurement ("nothing was detected"); ``None`` means no mask was produced at all.
    """

    score: float
    candidate: bool
    indicators: dict[str, Any]
    new_builtup_mask: np.ndarray | None = None
    structure_mask: np.ndarray | None = None


class InsufficientCoverage(Exception):
    """Too much of the parcel has no imagery in ``which`` ("base" | "target") to score it."""

    def __init__(self, which: str, nodata_frac: float) -> None:
        super().__init__(f"{which} year has no imagery for {nodata_frac:.0%} of the parcel")
        self.which = which
        self.nodata_frac = nodata_frac


#: Reported when a run had no usable fit, so a stored row says so rather than omitting it.
_NO_FIT: dict[str, float | int | None] = {
    "radiometric_gain": None,
    "radiometric_offset": None,
    "radiometric_sample_parcels": 0,
}


@dataclass(frozen=True)
class RadiometricFit:
    """A per-band linear correction carrying one capture's response onto another's.

    Fitted **once per year pair over many parcels**, not per parcel. Two captures of the
    same ground differ in more than brightness: measured on unchanged parcels, NIR falls
    179 -> 160 between 2010 and 2021 (185 -> 130 on the 2013/2017 control) while brightness
    and local contrast barely move, so an absolute ``VEG_T_NDVI`` under-reads vegetation in
    the later year and the vegetated fraction drops 0.586 -> 0.441 on ground that did not
    change. That inflates both the built-up candidate mask and vegetation loss.

    Scope is the whole point. A fit computed from a single parcel is dragged by that
    parcel's own change -- measured, it removed the bias but cost precision 0.0745 -> 0.0528
    and recall 0.72 -> 0.37, because a new roof is a large share of one parcel's pixels.
    Across a sample of parcels, unchanged ground dominates and no single parcel can move it.
    """

    gains: tuple[float, ...]
    offsets: tuple[float, ...]
    sampled_parcels: int

    def apply(self, data: np.ndarray) -> np.ndarray:
        out = data.astype(np.float32).copy()
        for band in range(min(data.shape[0], len(self.gains))):
            out[band] = out[band] * self.gains[band] + self.offsets[band]
        return np.clip(out, 0, 255).astype(np.uint8)

    @property
    def indicators(self) -> dict[str, float | int]:
        """The NIR fit, which is the band that actually moves between captures."""
        index = 3 if len(self.gains) >= 4 else 0
        return {
            "radiometric_gain": round(self.gains[index], 4),
            "radiometric_offset": round(self.offsets[index], 2),
            "radiometric_sample_parcels": self.sampled_parcels,
        }


#: Percentiles the fit matches: the lower half of the distribution, which is ground cover
#: in nearly every parcel. The upper tail is where new roofs land, so anchoring there would
#: let construction pull the correction toward erasing itself.
FIT_PERCENTILES = (5.0, 25.0, 50.0)
#: Widest per-band gain the fit may apply before the two captures are treated as unmatched.
MAX_FIT_GAIN = 3.0
#: Below this many paired sample pixels the fit is noise and is not applied.
MIN_FIT_PIXELS = 20_000


def fit_radiometry(
    base_samples: list[np.ndarray],
    target_samples: list[np.ndarray],
) -> RadiometricFit | None:
    """Fit ``target`` onto ``base`` from paired per-parcel pixel samples.

    Each entry is a (bands, n) array of pixels valid in both years for one parcel. None
    when there is too little to fit, which leaves the detector unnormalised rather than
    applying a correction derived from noise.
    """
    if not base_samples or len(base_samples) != len(target_samples):
        return None
    base = np.concatenate(base_samples, axis=1).astype(np.float32)
    target = np.concatenate(target_samples, axis=1).astype(np.float32)
    if base.shape != target.shape or base.shape[1] < MIN_FIT_PIXELS:
        return None

    gains: list[float] = []
    offsets: list[float] = []
    low, mid, high = FIT_PERCENTILES
    for band in range(base.shape[0]):
        b_lo, b_mid, b_hi = np.percentile(base[band], (low, mid, high))
        t_lo, t_mid, t_hi = np.percentile(target[band], (low, mid, high))
        spread = float(t_hi - t_lo)
        gain = float(b_hi - b_lo) / spread if spread > 1e-6 else 1.0
        gain = min(max(gain, 1.0 / MAX_FIT_GAIN), MAX_FIT_GAIN)
        gains.append(gain)
        offsets.append(float(b_mid) - gain * float(t_mid))
    return RadiometricFit(
        gains=tuple(gains), offsets=tuple(offsets), sampled_parcels=len(base_samples)
    )


def paired_samples(
    base: ParcelRaster, target: ParcelRaster, stride: int = 3
) -> tuple[np.ndarray, np.ndarray] | None:
    """Pixels of one parcel valid in both years, decimated, for the run-level fit."""
    if base.data.shape != target.data.shape:
        return None
    valid = base.parcel_mask & target.parcel_mask & base.mask & target.mask
    valid = valid[::stride, ::stride]
    if not valid.any():
        return None
    return base.data[:, ::stride, ::stride][:, valid], target.data[:, ::stride, ::stride][:, valid]


class Detector(Protocol):
    def compare(
        self,
        base: ParcelRaster,
        target: ParcelRaster,
        *,
        threshold: float,
        min_new_area_m2: float,
        fit: RadiometricFit | None = None,
    ) -> ChangeResult: ...


class ClassicalDetector:
    """Colour/brightness/texture change detector for gained built-up area."""

    # Vegetation: NDVI when NIR is present, else excess-green on the visible bands.
    VEG_T_NDVI = 0.2
    VEG_T_EXG = 0.08
    # Built-up: a structure stands out from the ground around it. "Non-vegetated, bright
    # and locally smooth" (the v1 rule) describes a ploughed field exactly as well as a
    # roof, which is why vacant parcels measured 0.95-1.00 built-up on real imagery and
    # finishing a house *lowered* the fraction as graded soil became lawn. What separates
    # the two is context, not absolute brightness: a roof differs from its surroundings at
    # building scale, an open field does not differ from itself.
    CONTRAST_T = 18.0
    CONTEXT_M = 60.0
    TEXTURE_T = 12.0
    TEXTURE_WINDOW = 5
    # Water and deep shadow are non-vegetated, smooth, and strongly darker than their
    # surroundings, so contrast alone would call a pond a building. Both sit far lower in
    # NIR than any dry surface.
    WATER_NIR = 45.0
    # Smallest structure worth reporting, as a span in metres. New built-up area must
    # survive an opening at this scale, so a roof counts and speckle does not.
    MIN_STRUCTURE_M = 8.0
    # A parcel with more than this fraction of missing pixels in either year is skipped.
    MAX_NODATA_FRAC = 0.5
    # The score measures the *largest contiguous new structure*, not a share of the parcel
    # and not the sum of everything that changed. A share ties a house's rating to the lot
    # it sits on -- 300 m2 is 0.30 of a quarter acre but 0.013 of five acres -- and a sum
    # adds up scattered year-to-year disagreement until a big empty parcel outranks a small
    # built one. A single connected component is immune to both: a building is the same
    # building wherever it stands.
    #
    # score = structure / (structure + SCORE_HALF_STRUCTURE_M2), so a structure of exactly
    # SCORE_HALF_STRUCTURE_M2 scores 0.5 and the curve keeps rising without ever reaching
    # 1.0. A warehouse therefore still outranks a house, where a capped ratio tied every
    # large building at the top -- the saturation that made the v1 score's threshold inert.
    # Inverting it: a threshold t selects structures of at least SCALE * t / (1 - t), so
    # 0.2 is about 100 m2, 0.5 is 400 m2, and 0.7 is about 930 m2.
    SCORE_HALF_STRUCTURE_M2 = 400.0
    # Share of its bounding box a blob must fill to count as a structure. Buildings are
    # compact and near-rectangular; changed field and canopy is ragged.
    MIN_STRUCTURE_FILL = 0.75
    # Retained for the stored `veg_loss_m2` indicator; vegetation loss no longer feeds the
    # score, having ranked at 0.045 on its own -- it tracks season more than construction.
    VEG_LOSS_WEIGHT = 0.25

    def compare(
        self,
        base: ParcelRaster,
        target: ParcelRaster,
        *,
        threshold: float,
        min_new_area_m2: float,
        fit: RadiometricFit | None = None,
    ) -> ChangeResult:
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

        target_data = fit.apply(target.data) if fit is not None else target.data
        veg_base, built_base = self._classify(base.data, base.resolution_m)
        veg_target, built_target = self._classify(target_data, target.resolution_m)

        px_area = base.resolution_m * base.resolution_m
        # A building is a compact blob; year-to-year disagreement is scattered speckle.
        # Without this, a large rural lot accumulates more spurious area than a suburban
        # lot gains real roof (824 m2 against 259 m2 on the evaluation set), so absolute
        # area ranked untouched acreage above real construction.
        new_builtup = _open(built_target & ~built_base & valid, self._span(base.resolution_m))
        veg_loss = veg_base & ~veg_target & valid
        new_builtup_m2 = float(new_builtup.sum()) * px_area
        veg_loss_m2 = float(veg_loss.sum()) * px_area
        parcel_m2 = float(valid.sum()) * px_area or px_area

        valid_px = max(int(valid.sum()), 1)
        base_frac = float((built_base & valid).sum()) / valid_px
        target_frac = float((built_target & valid).sum()) / valid_px
        builtup_delta = target_frac - base_frac
        structure_px, structure_fill, structure_mask = _largest_structure(
            new_builtup, self.MIN_STRUCTURE_FILL
        )
        structure_m2 = float(structure_px) * px_area
        score = structure_m2 / (structure_m2 + self.SCORE_HALF_STRUCTURE_M2)
        # `min_new_area_m2` is the smallest structure worth reporting, not a total: a
        # parcel qualifies when one contiguous thing that big appeared.
        candidate = bool(score >= threshold and structure_m2 >= min_new_area_m2)
        return ChangeResult(
            score=round(score, 4),
            candidate=candidate,
            indicators={
                "structure_m2": round(structure_m2, 1),
                "structure_fill": round(structure_fill, 3),
                "new_builtup_m2": round(new_builtup_m2, 1),
                "new_builtup_frac": round(new_builtup_m2 / parcel_m2, 4),
                "veg_loss_m2": round(veg_loss_m2, 1),
                "veg_loss_frac": round(veg_loss_m2 / parcel_m2, 4),
                "base_builtup_frac": round(base_frac, 4),
                "target_builtup_frac": round(target_frac, 4),
                "builtup_delta": round(builtup_delta, 4),
                "nodata_frac": round(nodata_frac, 4),
                "resolution_m": base.resolution_m,
                **(fit.indicators if fit is not None else _NO_FIT),
            },
            new_builtup_mask=new_builtup,
            structure_mask=structure_mask,
        )

    def _span(self, resolution_m: float) -> int:
        """``MIN_STRUCTURE_M`` in pixels on this comparison grid, at least 1."""
        return max(1, int(round(self.MIN_STRUCTURE_M / max(resolution_m, 0.01))))

    def _classify(
        self, data: np.ndarray, resolution_m: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """(vegetated, built-up) boolean masks for one year's pixels."""
        bands = data.astype(np.float32)
        r, g, b = bands[0], bands[1], bands[2]
        nir = bands[3] if data.shape[0] >= 4 else None
        if nir is not None:
            greenness = (nir - r) / (nir + r + 1.0)
            vegetated = greenness > self.VEG_T_NDVI
        else:
            greenness = (2.0 * g - r - b) / 255.0
            vegetated = greenness > self.VEG_T_EXG
        brightness = (r + g + b) / 3.0

        # How far each pixel sits from the ground around it, measured over a window wider
        # than a building but narrower than a field. A uniform expanse scores ~0 however
        # bright it is; a roof scores strongly either way, so dark roofs count too.
        context = max(3, _odd(self.CONTEXT_M / max(resolution_m, 0.01)))
        contrast = np.abs(brightness - _window_mean(brightness, context))
        texture = _local_std(brightness, self.TEXTURE_WINDOW)

        wet = nir < self.WATER_NIR if nir is not None else np.zeros_like(brightness, bool)
        candidate = ~vegetated & ~wet & (contrast > self.CONTRAST_T)
        # Roof edges are high-contrast, so the smooth "core" misses a rim of
        # TEXTURE_WINDOW // 2 pixels; grow it back, but only into candidate pixels
        # (geodesic dilation) so textured ground never joins in.
        builtup = candidate & (texture < self.TEXTURE_T)
        for _ in range(self.TEXTURE_WINDOW // 2):
            builtup = _dilate(builtup) & candidate
        return _clean(vegetated), _clean(builtup)


def _odd(value: float) -> int:
    """Nearest odd integer at least 1: a centred window needs an odd width."""
    width = max(1, int(round(value)))
    return width if width % 2 else width + 1


def _window_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Mean over a window x window neighbourhood, same shape as ``values``.

    Cost is independent of the window width (cumulative sums), which matters because the
    context window is tens of metres across and ``score_parcel`` runs once per parcel for
    a whole county.
    """
    pad = window // 2
    padded = np.pad(values.astype(np.float64), pad, mode="edge")
    return _box_mean(padded, window)


def _local_std(values: np.ndarray, window: int) -> np.ndarray:
    """Standard deviation over a window x window neighbourhood (cumulative sums, no scipy)."""
    pad = window // 2
    padded = np.pad(values.astype(np.float64), pad, mode="edge")
    mean = _box_mean(padded, window)
    mean_sq = _box_mean(padded * padded, window)
    return np.sqrt(np.maximum(mean_sq - mean * mean, 0.0))


def _box_mean(padded: np.ndarray, window: int) -> np.ndarray:
    integral = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64)
    integral[1:, 1:] = padded.cumsum(axis=0).cumsum(axis=1)
    h, w = padded.shape[0] - window + 1, padded.shape[1] - window + 1
    total = (
        integral[window : window + h, window : window + w]
        - integral[:h, window : window + w]
        - integral[window : window + h, :w]
        + integral[:h, :w]
    )
    return total / float(window * window)


def _erode(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:] &= mask[:-1]
    out[:-1] &= mask[1:]
    out[:, 1:] &= mask[:, :-1]
    out[:, :-1] &= mask[:, 1:]
    out[1:, 1:] &= mask[:-1, :-1]
    out[:-1, :-1] &= mask[1:, 1:]
    out[1:, :-1] &= mask[:-1, 1:]
    out[:-1, 1:] &= mask[1:, :-1]
    return out


def _dilate(mask: np.ndarray) -> np.ndarray:
    return ~_erode(~mask)


def _largest_structure(mask: np.ndarray, min_fill: float) -> tuple[int, float, np.ndarray]:
    """(pixels, bounding-box fill, mask) of the biggest *building-shaped* blob in ``mask``.

    The blob itself is returned alongside its measurements so a run can store the exact
    area it scored. Deriving it again from the stored polygons would re-answer the
    question with today's code, which is not necessarily what produced the score.

    Size alone does not separate a roof from a year-to-year artefact: measured on the
    evaluation set, spurious blobs on established parcels run larger at the tail than real
    houses do (603 m2 against 315 m2 at the 90th percentile). Shape does separate them --
    a new structure fills 96% of its bounding box, compact and near-rectangular, while a
    patch of changed field or canopy fills 67% and only 33% at the lower quartile.

    So a component counts only when it fills at least ``min_fill`` of its bounding box, and
    the largest qualifying one is the structure.

    Union-find over the set pixels rather than the whole raster: after the opening in
    ``compare`` the mask is sparse -- a few hundred pixels on a typical parcel -- so the
    work scales with what actually changed rather than with parcel size. Pure numpy plus a
    small Python loop keeps the detector free of a scipy dependency for one function.
    """
    coords = np.argwhere(mask)
    if coords.size == 0:
        return 0, 0.0, np.zeros_like(mask, dtype=bool)
    height, width = mask.shape
    index = np.full((height, width), -1, dtype=np.int64)
    index[coords[:, 0], coords[:, 1]] = np.arange(len(coords))

    parent = list(range(len(coords)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    # Row-major order, so only the four already-visited neighbours need checking.
    for position, (row, column) in enumerate(coords):
        for delta_row, delta_column in ((-1, -1), (-1, 0), (-1, 1), (0, -1)):
            neighbour_row, neighbour_column = row + delta_row, column + delta_column
            if 0 <= neighbour_row < height and 0 <= neighbour_column < width:
                neighbour = index[neighbour_row, neighbour_column]
                if neighbour >= 0:
                    union(position, int(neighbour))

    roots = np.array([find(node) for node in range(len(coords))])
    best_px, best_fill = 0, 0.0
    best_member: np.ndarray | None = None
    for root in np.unique(roots):
        member = coords[roots == root]
        rows, columns = member[:, 0], member[:, 1]
        box = (rows.max() - rows.min() + 1) * (columns.max() - columns.min() + 1)
        fill = len(member) / float(box)
        if fill >= min_fill and len(member) > best_px:
            best_px, best_fill, best_member = len(member), fill, member

    structure = np.zeros_like(mask, dtype=bool)
    if best_member is not None:
        structure[best_member[:, 0], best_member[:, 1]] = True
    return best_px, best_fill, structure


def _open(mask: np.ndarray, span: int) -> np.ndarray:
    """Morphological opening with a ``span`` x ``span`` element.

    Anything narrower than ``span`` in either direction is erased; anything at least that
    wide survives at its original extent. Used to require that reported new built-up area
    be a blob a building could cast, not a scatter of disagreeing pixels.
    """
    if span <= 1:
        return mask
    eroded = mask
    for _ in range(span // 2):
        eroded = _erode(eroded)
    for _ in range(span // 2):
        eroded = _dilate(eroded)
    return eroded


def _clean(mask: np.ndarray) -> np.ndarray:
    """Morphological opening then closing with a 3x3 element: drops speckle and slivers."""
    opened = _dilate(_erode(mask))
    return _erode(_dilate(opened))
