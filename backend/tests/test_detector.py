import uuid

import numpy as np
import pytest
from geoalchemy2.shape import to_shape
from rasterio.crs import CRS
from rasterio.transform import from_origin
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.api.runs import DEFAULT_MIN_NEW_AREA_M2, DEFAULT_THRESHOLD
from ptax.config import Settings
from ptax.db.models import Parcel
from ptax.detection.detector import (
    ClassicalDetector,
    InsufficientCoverage,
    ParcelRaster,
    fit_radiometry,
    paired_samples,
)
from ptax.imagery.reader import assets_intersecting, read_parcel
from tests.conftest import FIXTURES_DIR, ingest_naip_year, ingest_upload_year

IMAGERY_DIR = FIXTURES_DIR / "imagery"
# The fixtures run at the *shipped* defaults. They could not while the score was a share
# of the parcel: a 20 x 15 m roof is only 0.013 of a 5.5-acre fixture parcel, so reaching
# the threshold would have needed a 6,287 m2 building. Scoring the structure instead makes
# a 300 m2 roof worth 300 m2 wherever it stands, which is the point of the change.
#
# These remain a wiring contract -- does the pipeline flag exactly the parcels that gained
# a roof -- not an accuracy claim. Accuracy is measured on real imagery by `ptax-eval`;
# see backend/eval/README.md, including what the detector cannot do.
FIXTURE_THRESHOLD = DEFAULT_THRESHOLD
FIELD = (70, 120, 60, 180)
ROOF = (150, 150, 150, 90)


def _field(size: int = 100, bands: int = 4, res: float = 1.0) -> ParcelRaster:
    """A size x size m field with a mild checker texture, fully inside the parcel."""
    data = np.empty((bands, size, size), dtype=np.uint8)
    checker = ((np.arange(size)[None, :] // 3 + np.arange(size)[:, None] // 3) % 2) * 8 - 4
    for b in range(bands):
        data[b] = np.clip(FIELD[b] + checker, 1, 255)
    return ParcelRaster(
        data=data,
        mask=np.ones((size, size), dtype=bool),
        parcel_mask=np.ones((size, size), dtype=bool),
        transform=from_origin(0, size * res, res, res),
        crs=CRS.from_epsg(32615),
        resolution_m=res,
        bounds=(0, 0, size * res, size * res),
    )


def _with_roof(
    raster: ParcelRaster,
    row: int,
    col: int,
    h: int = 15,
    w: int = 20,
    colour: tuple[int, ...] = ROOF,
) -> ParcelRaster:
    data = raster.data.copy()
    for b in range(data.shape[0]):
        data[b, row : row + h, col : col + w] = colour[b]
    return ParcelRaster(
        data=data,
        mask=raster.mask,
        parcel_mask=raster.parcel_mask,
        transform=raster.transform,
        crs=raster.crs,
        resolution_m=raster.resolution_m,
        bounds=raster.bounds,
    )


def test_added_roof_is_a_candidate_with_area_and_score() -> None:
    base = _field()
    target = _with_roof(base, 40, 40)
    # The score is the largest contiguous new structure, independent of the parcel: this
    # 20 x 15 m roof is 300 m2, and 300 / (300 + 400) is 0.43.
    result = ClassicalDetector().compare(
        base, target, threshold=DEFAULT_THRESHOLD, min_new_area_m2=DEFAULT_MIN_NEW_AREA_M2
    )
    assert 0.40 <= result.score <= 0.46
    assert result.candidate is True
    assert abs(result.indicators["new_builtup_m2"] - 300) / 300 < 0.15
    assert result.indicators["veg_loss_m2"] > 200
    assert result.indicators["resolution_m"] == 1.0
    assert result.indicators["nodata_frac"] == 0


def test_unchanged_field_scores_near_zero() -> None:
    base = _field()
    result = ClassicalDetector().compare(base, base, threshold=0.3, min_new_area_m2=40)
    assert result.score < 0.02
    assert result.candidate is False


def test_existing_roof_shifted_one_pixel_is_not_a_candidate() -> None:
    base = _with_roof(_field(), 40, 40)
    target = _with_roof(_field(), 41, 41)
    result = ClassicalDetector().compare(base, target, threshold=0.3, min_new_area_m2=40)
    assert result.candidate is False
    assert result.indicators["new_builtup_m2"] < 40


def test_three_band_imagery_uses_visible_greenness() -> None:
    base = _field(bands=3)
    target = _with_roof(base, 40, 40)
    # Same 300 m2 roof as the 4-band case, so the same 0.43.
    result = ClassicalDetector().compare(
        base, target, threshold=DEFAULT_THRESHOLD, min_new_area_m2=DEFAULT_MIN_NEW_AREA_M2
    )
    assert result.candidate is True
    assert result.score >= 0.40


def test_insufficient_coverage_raises_for_the_right_year() -> None:
    base = _field()
    target = _with_roof(base, 40, 40)
    mask = target.mask.copy()
    mask[:, :60] = False  # 60% of the parcel has no target imagery
    holed = ParcelRaster(
        data=target.data,
        mask=mask,
        parcel_mask=target.parcel_mask,
        transform=target.transform,
        crs=target.crs,
        resolution_m=target.resolution_m,
        bounds=target.bounds,
    )
    with pytest.raises(InsufficientCoverage) as exc:
        ClassicalDetector().compare(base, holed, threshold=0.3, min_new_area_m2=40)
    assert exc.value.which == "target"
    with pytest.raises(InsufficientCoverage) as exc:
        ClassicalDetector().compare(holed, base, threshold=0.3, min_new_area_m2=40)
    assert exc.value.which == "base"


def _candidates(
    db: Session,
    settings: Settings,
    tenant_id: uuid.UUID,
    layer_id: str,
    base_year_id: str,
    target_year_id: str,
    resolution_m: float,
) -> tuple[set[str], set[str]]:
    """(candidate refs, refs that could be scored) over the layer's parcels."""
    detector = ClassicalDetector()
    candidates, scored = set(), set()
    parcels = db.execute(select(Parcel).where(Parcel.layer_id == uuid.UUID(layer_id))).scalars()
    for parcel in parcels:
        geom = to_shape(parcel.geom)
        base_assets = assets_intersecting(db, tenant_id, uuid.UUID(base_year_id), geom.bounds)
        target_assets = assets_intersecting(db, tenant_id, uuid.UUID(target_year_id), geom.bounds)
        base = read_parcel(settings, base_assets, geom, resolution_m=resolution_m)
        target = read_parcel(settings, target_assets, geom, resolution_m=resolution_m)
        if base is None or target is None:
            continue
        try:
            result = detector.compare(
                base,
                target,
                threshold=FIXTURE_THRESHOLD,
                min_new_area_m2=DEFAULT_MIN_NEW_AREA_M2,
            )
        except InsufficientCoverage:
            continue
        scored.add(parcel.parcel_ref[-6:])
        if result.candidate:
            candidates.add(parcel.parcel_ref[-6:])
    return candidates, scored


def test_fixture_years_flag_exactly_the_planted_roofs(
    client, db: Session, settings: Settings, tenant_with_admin, ingested_layer
) -> None:
    headers = tenant_with_admin["admin_headers"]
    tenant = tenant_with_admin["tenant"]
    y2021 = ingest_naip_year(client, db, headers, 2021)
    y2023 = ingest_naip_year(client, db, headers, 2023)
    y2025 = ingest_upload_year(
        client,
        db,
        settings,
        tenant,
        headers,
        2025,
        {"ortho_2025_partial.tif": (IMAGERY_DIR / "ortho_2025_partial.tif").read_bytes()},
    )

    candidates, scored = _candidates(
        db, settings, tenant.id, ingested_layer["id"], y2021["id"], y2023["id"], 1.0
    )
    assert len(scored) == 25
    assert candidates == {"000003", "000007", "000012"}

    candidates, scored = _candidates(
        db, settings, tenant.id, ingested_layer["id"], y2023["id"], y2025["id"], 1.0
    )
    assert len(scored) == 15
    assert candidates == {"000001"}


# Bare soil: bright, low NDVI, locally smooth -- indistinguishable from a roof under the
# v1 cue, and the reason the real-imagery baseline scored below its own base rate.
SOIL = (150, 140, 120, 160)
# A pale roof. The shared ROOF tone sits only ~13 brightness units above SOIL, which is
# below CONTRAST_T: a roof that matches the soil it stands on is a real ceiling of a
# contrast cue, not something to tune the threshold around.
LIGHT_ROOF = (205, 200, 195, 120)


def _bare(size: int = 100, bands: int = 4, res: float = 1.0) -> ParcelRaster:
    """A size x size m graded/ploughed field with the same mild texture as ``_field``."""
    data = np.empty((bands, size, size), dtype=np.uint8)
    checker = ((np.arange(size)[None, :] // 3 + np.arange(size)[:, None] // 3) % 2) * 8 - 4
    for b in range(bands):
        data[b] = np.clip(SOIL[b] + checker, 1, 255)
    return ParcelRaster(
        data=data,
        mask=np.ones((size, size), dtype=bool),
        parcel_mask=np.ones((size, size), dtype=bool),
        transform=from_origin(0, size * res, res, res),
        crs=CRS.from_epsg(32615),
        resolution_m=res,
        bounds=(0, 0, size * res, size * res),
    )


def test_bare_soil_is_not_built_up() -> None:
    """A vacant graded lot must not read as a structure.

    On the real evaluation set, parcels that were still vacant in both captures measured
    0.949 -> 1.000 built-up, because "non-vegetated, bright and locally smooth" describes
    ploughed soil exactly as well as it describes a roof.
    """
    result = ClassicalDetector().compare(
        _bare(), _bare(), threshold=0.3, min_new_area_m2=40
    )
    assert result.indicators["base_builtup_frac"] < 0.2
    assert result.indicators["target_builtup_frac"] < 0.2


def test_building_on_bare_soil_raises_the_built_up_fraction() -> None:
    """The cue must point the right way on the event it exists to detect.

    On real imagery the v1 cue ran 0.677 -> 0.569 across genuine new construction: a
    finished house replaces graded soil with lawn, so "built-up" *fell* where a building
    appeared, and untouched lots outranked real builds.
    """
    base = _bare()
    target = _with_roof(_bare(), 40, 40, colour=LIGHT_ROOF)
    result = ClassicalDetector().compare(
        base, target, threshold=DEFAULT_THRESHOLD, min_new_area_m2=DEFAULT_MIN_NEW_AREA_M2
    )

    delta = result.indicators["target_builtup_frac"] - result.indicators["base_builtup_frac"]
    assert delta > 0, "building a house must not lower the built-up fraction"
    assert result.candidate is True
    assert abs(result.indicators["new_builtup_m2"] - 300) / 300 < 0.35


def _recaptured(
    raster: ParcelRaster, gain: float = 1.20, offset: float = 8.0, nir_gain: float = 0.80
) -> ParcelRaster:
    """The same ground, imaged again under different conditions.

    A second capture differs in more than overall brightness: contrast is stretched or
    compressed by sun angle, haze and the sensor's own processing, and NIR moves with
    season. Nothing on the ground has changed, so a detector must report no new structure.
    """
    bands = raster.data.astype(np.float32)
    mean = bands[:3].mean()
    out = bands.copy()
    out[:3] = (bands[:3] - mean) * gain + mean + offset
    if bands.shape[0] >= 4:
        out[3] = bands[3] * nir_gain
    return ParcelRaster(
        data=np.clip(out, 1, 255).astype(np.uint8),
        mask=raster.mask,
        parcel_mask=raster.parcel_mask,
        transform=raster.transform,
        crs=raster.crs,
        resolution_m=raster.resolution_m,
        bounds=raster.bounds,
    )


def _mottled(size: int = 100, res: float = 1.0) -> ParcelRaster:
    """Ground with a continuum of local contrast, as real imagery has.

    A flat field with one roof is bimodal: everything is far above or far below any
    contrast threshold, so nothing sits near it and a contrast stretch changes no
    decision. Real ground is mottled -- patchy soil, mown and unmown grass, tracks,
    canopy shadow -- so a spread of pixels sits just below the threshold, and that is the
    population a stretch pushes across it.
    """
    y, x = np.mgrid[0:size, 0:size]
    swell = 14.0 * np.sin(x / 9.0) * np.cos(y / 11.0)
    checker = ((x // 3 + y // 3) % 2) * 8 - 4
    data = np.empty((4, size, size), dtype=np.uint8)
    for b in range(4):
        data[b] = np.clip(FIELD[b] + swell + checker, 1, 255)
    # Vigour varies across real ground, so NDVI is a spread rather than one value and a
    # modest NIR shift moves a slice of it across VEG_T_NDVI. A uniform field would flip
    # all at once or not at all, which is not how the real bias shows up.
    vigour = 1.0 + 0.30 * np.sin(x / 13.0 + 1.0) * np.cos(y / 7.0)
    data[3] = np.clip(FIELD[3] * vigour, 1, 255)
    return ParcelRaster(
        data=data,
        mask=np.ones((size, size), dtype=bool),
        parcel_mask=np.ones((size, size), dtype=bool),
        transform=from_origin(0, size * res, res, res),
        crs=CRS.from_epsg(32615),
        resolution_m=res,
        bounds=(0, 0, size * res, size * res),
    )


def _run_fit(base: ParcelRaster, target: ParcelRaster, parcels: int = 40) -> object:
    """A run-level fit, as `detection.run` builds it: from many parcels, not from one."""
    bases, targets = [], []
    for _ in range(parcels):
        pair = paired_samples(base, target, stride=1)
        assert pair is not None
        bases.append(pair[0])
        targets.append(pair[1])
    return fit_radiometry(bases, targets)


def test_a_recapture_of_unchanged_ground_reports_no_new_structure() -> None:
    """The dominant remaining artefact on real imagery, in one fixture.

    Across the evaluation set the 2021 built-up fraction ran about 4x the 2010 figure on
    parcels that did not change, and the same-resolution control showed the same gap, so
    it is the capture that differs rather than the grid. A contrast cue is immune to a
    uniform brightness offset but not to a contrast *stretch*: scaling the spread pushes
    more pixels past CONTRAST_T and manufactures new built-up area out of nothing.
    """
    base = _with_roof(_mottled(), 40, 40)
    target = _recaptured(base)
    fit = _run_fit(base, target)

    result = ClassicalDetector().compare(
        base, target, threshold=0.3, min_new_area_m2=40, fit=fit
    )

    assert result.indicators["new_builtup_m2"] < 40
    assert result.score < 0.05
    assert result.candidate is False


def test_radiometric_fit_is_recorded_for_every_scored_parcel() -> None:
    base = _with_roof(_mottled(), 40, 40)
    target = _recaptured(base)
    result = ClassicalDetector().compare(
        base, target, threshold=0.3, min_new_area_m2=40, fit=_run_fit(base, target)
    )

    assert result.indicators["radiometric_gain"] == pytest.approx(1 / 0.80, abs=0.25)
    assert result.indicators["radiometric_sample_parcels"] == 40
    assert "radiometric_offset" in result.indicators


def test_a_run_level_fit_does_not_erase_the_change_on_one_parcel() -> None:
    """The reason the fit is run-level rather than per parcel.

    A correction fitted on a single parcel is anchored by that parcel's own pixels, so a
    new roof pulls the fit toward erasing itself. A correction fitted across parcels of
    unchanged ground cannot be moved by any one of them, and must still let a real
    structure through.
    """
    unchanged = _with_roof(_mottled(), 40, 40)
    fit = _run_fit(unchanged, _recaptured(unchanged))

    # Same capture difference, but this parcel also gained a building on open ground
    # well away from the one it already had.
    built = _recaptured(
        _with_roof(
            _with_roof(_mottled(), 40, 40), 12, 15, h=18, w=24, colour=LIGHT_ROOF
        )
    )
    result = ClassicalDetector().compare(
        unchanged, built, threshold=0.05, min_new_area_m2=40, fit=fit
    )

    assert result.indicators["new_builtup_m2"] > 100
    assert result.candidate is True


def _with_structures(base: ParcelRaster, areas_m2: list[int]) -> list[ParcelRaster]:
    """The same parcel, each time with a single new building of a different footprint."""
    out = []
    for area in areas_m2:
        side = int(round(area**0.5))
        out.append(_with_roof(base, 20, 20, h=side, w=side, colour=LIGHT_ROOF))
    return out


def test_score_ranks_structures_by_size_without_saturating() -> None:
    """A bigger building must score higher, and nothing may pin at the top of the range.

    The v1 score reached ~1.0 by a few hundred m2, so a quarter of real parcels sat at
    >= 0.99 and `threshold` could not separate them at all.
    """
    base = _mottled()
    scores = [
        ClassicalDetector().compare(base, t, threshold=0.3, min_new_area_m2=40).score
        for t in _with_structures(base, [100, 400, 900, 1600])
    ]

    assert scores == sorted(scores), f"scores must increase with footprint: {scores}"
    assert len(set(scores)) == 4, f"each footprint must be distinguishable: {scores}"
    # 100, 400, 900 and 1600 m2 map to about 0.20, 0.50, 0.69 and 0.80: a warehouse still
    # outranks a house instead of tying with it at the ceiling.
    assert max(scores) < 0.98, f"the largest must not pin at the top: {scores}"


def test_threshold_separates_across_its_range() -> None:
    """`threshold` has to bite across 0-1, or the run parameter is decorative."""
    base = _mottled()
    targets = _with_structures(base, [100, 400, 900, 1600])
    flagged_at = []
    for threshold in (0.1, 0.3, 0.5, 0.7, 0.9):
        flagged_at.append(
            sum(
                ClassicalDetector()
                .compare(base, t, threshold=threshold, min_new_area_m2=40)
                .candidate
                for t in targets
            )
        )

    assert flagged_at == sorted(flagged_at, reverse=True), flagged_at
    assert len(set(flagged_at)) >= 3, f"threshold barely moves the outcome: {flagged_at}"


def test_score_is_the_largest_contiguous_structure_not_a_share_of_the_parcel() -> None:
    """The same building must rate the same on a small lot and on a large one.

    A share-based score ties a house's rating to the size of the lot it sits on: 300 m2 is
    0.30 of a quarter-acre lot but 0.013 of five acres, so the same house was scored an
    order of magnitude apart. A structure detector has to measure the structure.
    """
    small = _mottled(size=60)
    large = _mottled(size=200)
    roof = dict(h=15, w=20, colour=LIGHT_ROOF)

    on_small = ClassicalDetector().compare(
        small, _with_roof(small, 20, 20, **roof), threshold=0.1, min_new_area_m2=37.2
    )
    on_large = ClassicalDetector().compare(
        large, _with_roof(large, 20, 20, **roof), threshold=0.1, min_new_area_m2=37.2
    )

    assert on_small.score == pytest.approx(on_large.score, abs=0.05)
    assert on_small.candidate is True and on_large.candidate is True


def test_scattered_speckle_never_adds_up_to_a_structure() -> None:
    """Total new area can be large while no single structure exists; only the largest
    contiguous component counts, so disagreement spread over a big parcel scores low."""
    base = _mottled(size=200)
    target = _with_roof(base, 10, 10, h=9, w=9, colour=LIGHT_ROOF)
    for row in range(30, 190, 20):  # many small patches, none a building
        target = _with_roof(target, row, row, h=9, w=9, colour=LIGHT_ROOF)

    result = ClassicalDetector().compare(
        base, target, threshold=0.1, min_new_area_m2=200.0
    )

    assert result.indicators["new_builtup_m2"] > 400, "total area is genuinely large"
    assert result.indicators["structure_m2"] < 200, "but no single structure is"
    assert result.candidate is False


def test_structure_size_gates_candidacy_at_min_new_area_m2() -> None:
    base = _mottled()
    small = _with_roof(base, 20, 20, h=6, w=6, colour=LIGHT_ROOF)  # 36 m2, under 400 sq ft
    big = _with_roof(base, 20, 20, h=12, w=12, colour=LIGHT_ROOF)  # 144 m2

    under = ClassicalDetector().compare(base, small, threshold=0.0, min_new_area_m2=37.2)
    over = ClassicalDetector().compare(base, big, threshold=0.0, min_new_area_m2=37.2)

    assert under.candidate is False
    assert over.candidate is True
    assert over.indicators["structure_m2"] > under.indicators["structure_m2"]
