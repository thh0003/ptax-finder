import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box

from ptax.reconcile.geometry import InstanceInfo, area_sqft, on_parcel, vectorize_instances
from ptax.reconcile.types import Detection

# A 100 x 100 px mask at 0.5 m/px, north-west corner at (500000, 4500000) in UTM 16N.
X0, Y0, RES = 500_000.0, 4_500_000.0, 0.5
TRANSFORM = from_origin(X0, Y0, RES, RES)


def _labels() -> np.ndarray:
    labels = np.zeros((100, 100), dtype=np.int32)
    labels[10:30, 10:50] = 1  # 20 x 40 px: a house
    labels[60:70, 60:70] = 2  # 10 x 10 px: a shed
    return labels


def test_instances_become_polygons_with_their_class_and_score() -> None:
    detections = vectorize_instances(
        _labels(),
        {1: InstanceInfo("primary_structure", 0.9), 2: InstanceInfo("shed", 0.6, occluded=True)},
        TRANSFORM,
        year="B",
    )
    by_cls = {d.cls: d for d in detections}
    house, shed = by_cls["primary_structure"], by_cls["shed"]
    assert house.year == "B" and house.score == 0.9 and not house.occluded
    # Pixels 10-50 across and 10-30 down, at 0.5 m.
    assert house.geom.bounds == pytest.approx((X0 + 5, Y0 - 15, X0 + 25, Y0 - 5))
    assert house.geom.area == pytest.approx(20 * 10)
    assert shed.occluded and shed.score == 0.6
    assert len({d.id for d in detections}) == 2


def test_a_label_without_an_info_entry_is_ignored() -> None:
    detections = vectorize_instances(
        _labels(), {1: InstanceInfo("primary_structure", 0.9)}, TRANSFORM, year="A"
    )
    assert [d.cls for d in detections] == ["primary_structure"]


def _detection(geom, id_: str) -> Detection:  # noqa: ANN001
    return Detection(id=id_, year="B", cls="shed", score=0.9, geom=geom)


def test_a_structure_is_kept_whole_only_when_its_centroid_is_inside_the_parcel() -> None:
    parcel = box(0, 0, 100, 100)
    inside = _detection(box(10, 10, 20, 20), "inside")
    straddling_in = _detection(box(90, 40, 110, 50), "straddle-in")  # centroid x=100: on edge
    mostly_in = _detection(box(85, 40, 105, 50), "mostly-in")  # centroid x=95
    mostly_out = _detection(box(95, 40, 115, 50), "mostly-out")  # centroid x=105
    outside = _detection(box(200, 200, 210, 210), "outside")

    kept = on_parcel([inside, straddling_in, mostly_in, mostly_out, outside], parcel)

    assert {d.id for d in kept} == {"inside", "mostly-in"}
    # Kept whole, not clipped to the parcel line.
    assert next(d for d in kept if d.id == "mostly-in").geom.bounds == (85, 40, 105, 50)


def test_area_is_square_feet_in_a_metre_crs() -> None:
    assert area_sqft(box(0, 0, 10, 10), 26916) == pytest.approx(1076.39, abs=0.01)


def test_area_is_square_feet_in_a_us_survey_foot_crs() -> None:
    # Illinois East, NAD83, US survey feet: a US survey foot is not an international foot.
    assert area_sqft(box(0, 0, 100, 100), 3435) == pytest.approx(10000.04, abs=0.01)
