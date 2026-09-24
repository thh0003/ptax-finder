import pytest
from shapely.geometry import box

from ptax.reconcile.parcel import reconcile_parcel
from ptax.reconcile.types import CamaRecord, Detection
from ptax.tenancy.config import Thresholds

CRS = 3435  # US survey feet: one unit is (almost exactly) one foot
PARCEL = box(0, 0, 200, 200)
HOUSE_A = box(10, 10, 40, 60)  # 1500 sq ft
HOUSE_B = box(10, 10, 46, 60)  # 1800 sq ft: +300
GARAGE = box(100, 100, 120, 120)  # 400 sq ft


def _d(id_: str, year: str, geom, cls: str = "primary_structure", score: float = 0.9):  # noqa: ANN001, ANN202
    return Detection(id=id_, year=year, cls=cls, score=score, geom=geom)  # type: ignore[arg-type]


def _run(a, b, change=None, cama=()):  # noqa: ANN001, ANN202
    return reconcile_parcel(
        a,
        b,
        pin="P1",
        parcel_geom=PARCEL,
        year_a=2015,
        crs_epsg=CRS,
        thresholds=Thresholds(),
        change_polygons=change,
        cama=cama,
    )


def test_both_signals_agreeing_with_high_scores_is_high_confidence() -> None:
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding")], change=[GARAGE])
    assert result.status == "high_confidence" and result.change_model_agrees is True


def test_both_signals_agreeing_with_a_low_score_needs_review() -> None:
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding", score=0.6)], change=[GARAGE])
    assert result.status == "needs_review" and result.change_model_agrees is True


def test_segmentation_alone_needs_review() -> None:
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding")])
    assert result.status == "needs_review" and result.change_model_agrees is None


def test_a_change_model_that_misses_the_detection_disagrees() -> None:
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding")], change=[box(0, 0, 5, 5)])
    assert result.status == "needs_review" and result.change_model_agrees is False


def test_an_expansion_agrees_only_where_it_grew() -> None:
    grown_strip = box(40, 10, 46, 60)
    agree = _run([_d("a1", "A", HOUSE_A)], [_d("b1", "B", HOUSE_B)], change=[grown_strip])
    assert agree.status == "high_confidence"
    # Change over the old part of the house, not the addition, is no agreement.
    disagree = _run(
        [_d("a1", "A", HOUSE_A)], [_d("b1", "B", HOUSE_B)], change=[box(10, 10, 16, 60)]
    )
    assert disagree.change_model_agrees is False


def test_change_polygons_alone_need_review() -> None:
    result = _run([], [], change=[box(150, 150, 170, 170)])  # 400 sq ft inside the parcel
    assert result.status == "needs_review" and result.new_sqft_est == 0


def test_small_or_off_parcel_change_alone_is_no_change() -> None:
    result = _run([], [], change=[box(150, 150, 155, 155), box(300, 300, 400, 400)])
    assert result.status == "no_change" and result.change_model_agrees is True


def test_an_uncertain_would_be_new_detection_needs_review() -> None:
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding", score=0.3)])
    assert result.status == "needs_review"
    assert result.new_sqft_est == 0 and result.classes_added == ()


def test_only_already_assessed_changes_are_no_change() -> None:
    cama = [CamaRecord(pin="P1", cls="garage_outbuilding", area_sqft=390, year=2017)]
    result = _run([], [_d("b1", "B", GARAGE, cls="garage_outbuilding")], cama=cama)
    assert result.status == "no_change"


def test_nothing_is_no_change() -> None:
    result = _run([_d("a1", "A", HOUSE_A)], [_d("b1", "B", HOUSE_A)])
    assert result.status == "no_change" and result.new_sqft_est == 0
    assert [c.change_type for c in result.detections] == ["unchanged"]


def test_the_estimate_sums_new_areas_and_expansion_deltas() -> None:
    result = _run(
        [_d("a1", "A", HOUSE_A)],
        [_d("b1", "B", HOUSE_B), _d("b2", "B", GARAGE, cls="garage_outbuilding")],
    )
    assert result.new_sqft_est == pytest.approx(700, rel=1e-4)
    assert result.classes_added == ("garage_outbuilding", "primary_structure")
    assert result.pin == "P1"
