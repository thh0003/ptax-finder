import pytest
from shapely.geometry import box

from ptax.reconcile.matching import CamaRecord, classify
from ptax.reconcile.types import Detection
from ptax.tenancy.config import Thresholds

CRS = 3435  # Illinois East, US survey feet: one unit is (almost exactly) one foot
T = Thresholds()


def _d(id_: str, year: str, geom, cls: str = "primary_structure", score: float = 0.9, **kw):  # noqa: ANN001, ANN003, ANN202
    return Detection(id=id_, year=year, cls=cls, score=score, geom=geom, **kw)  # type: ignore[arg-type]


def _run(a, b, cama=()):  # noqa: ANN001, ANN202
    results = classify(a, b, pin="P1", year_a=2015, crs_epsg=CRS, thresholds=T, cama=cama)
    return {r.detection.id: r for r in results}


def test_an_identical_building_is_unchanged() -> None:
    out = _run([_d("a1", "A", box(0, 0, 30, 50))], [_d("b1", "B", box(0, 0, 30, 50))])
    assert out["b1"].change_type == "unchanged" and out["b1"].matched_a_id == "a1"
    assert out["b1"].iou == pytest.approx(1.0)
    assert "a1" not in out, "a matched Year A detection is represented by its Year B match"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (box(0, 0, 30, 50), box(2, 0, 32, 50)),  # a house shifted 2 ft
        # A 10 ft shed shifted 8 ft: raw IoU 0.11 < 0.3, but the 3 ft buffers bring it to 0.32.
        (box(0, 0, 10, 10), box(8, 0, 18, 10)),
    ],
)
def test_misregistration_within_tolerance_still_matches(a, b) -> None:  # noqa: ANN001
    out = _run([_d("a1", "A", a)], [_d("b1", "B", b)])
    assert out["b1"].matched_a_id == "a1" and out["b1"].change_type == "unchanged"


def test_a_structure_moved_well_beyond_tolerance_is_new_and_the_old_one_removed() -> None:
    out = _run(
        [_d("a1", "A", box(0, 0, 20, 20), cls="garage_outbuilding")],
        [_d("b1", "B", box(60, 0, 80, 20), cls="garage_outbuilding")],
    )
    assert out["b1"].change_type == "new" and out["b1"].area_sqft == pytest.approx(400, rel=1e-4)
    assert out["a1"].change_type == "removed"


def test_a_new_garage_is_new_and_a_tiny_object_is_not_reported() -> None:
    out = _run(
        [_d("a1", "A", box(0, 0, 30, 50))],
        [
            _d("b1", "B", box(0, 0, 30, 50)),
            _d("b2", "B", box(100, 100, 120, 120), cls="garage_outbuilding"),
            _d("b3", "B", box(200, 200, 205, 210), cls="shed"),  # 50 sq ft
        ],
    )
    assert out["b2"].change_type == "new"
    assert out["b3"].change_type == "unchanged", "below min_new_area_sqft: too small to report"


@pytest.mark.parametrize(
    ("width", "expected", "delta"),
    [(36, "expanded", 300), (31, "unchanged", 50)],  # 1500 -> 1800 and 1500 -> 1550 sq ft
)
def test_growth_must_pass_both_expansion_thresholds(width: int, expected: str, delta: int) -> None:
    out = _run([_d("a1", "A", box(0, 0, 30, 50))], [_d("b1", "B", box(0, 0, width, 50))])
    assert out["b1"].change_type == expected
    assert out["b1"].delta_sqft == pytest.approx(delta, rel=1e-4)


def test_a_small_percentage_growth_of_a_big_building_is_not_an_expansion() -> None:
    # 10000 -> 10500 sq ft: +500 passes the absolute threshold but is only 5%.
    out = _run([_d("a1", "A", box(0, 0, 100, 100))], [_d("b1", "B", box(0, 0, 105, 100))])
    assert out["b1"].change_type == "unchanged"


def test_a_demolished_shed_is_removed() -> None:
    out = _run([_d("a1", "A", box(0, 0, 10, 12), cls="shed")], [])
    assert out["a1"].change_type == "removed"


@pytest.mark.parametrize(
    "flags",
    [{"score": 0.3}, {"occluded": True}, {"tile_flagged": True}],
)
def test_an_unreliable_detection_is_uncertain_and_remembers_what_it_would_be(flags) -> None:  # noqa: ANN001
    out = _run([], [_d("b1", "B", box(0, 0, 20, 20), cls="garage_outbuilding", **flags)])
    assert out["b1"].change_type == "uncertain"
    assert out["b1"].would_be == "new"


def test_matching_is_one_to_one_by_best_overlap() -> None:
    # Two Year B buildings overlap one Year A building; only the better match takes it.
    out = _run(
        [_d("a1", "A", box(0, 0, 20, 20))],
        [_d("b1", "B", box(0, 0, 20, 20)), _d("b2", "B", box(10, 0, 30, 20))],
    )
    assert out["b1"].matched_a_id == "a1"
    assert out["b2"].matched_a_id is None and out["b2"].change_type == "new"


def _garage_on_empty_lot(cama):  # noqa: ANN001, ANN202
    return _run([], [_d("b1", "B", box(0, 0, 20, 20), cls="garage_outbuilding")], cama=cama)


def test_a_new_garage_the_county_already_assessed_is_suppressed() -> None:
    record = CamaRecord(pin="P1", cls="garage_outbuilding", area_sqft=390, year=2017)
    out = _garage_on_empty_lot([record])
    assert out["b1"].change_type == "new" and out["b1"].already_assessed


@pytest.mark.parametrize(
    "record",
    [
        CamaRecord(pin="P1", cls="pool", area_sqft=390, year=2017),  # another class
        CamaRecord(pin="P1", cls="garage_outbuilding", area_sqft=200, year=2017),  # size off
        CamaRecord(pin="P1", cls="garage_outbuilding", area_sqft=390, year=2010),  # before A
        CamaRecord(pin="P2", cls="garage_outbuilding", area_sqft=390, year=2017),  # other PIN
    ],
)
def test_a_cama_record_that_does_not_correspond_does_not_suppress(record: CamaRecord) -> None:
    assert not _garage_on_empty_lot([record])["b1"].already_assessed


def test_one_cama_record_suppresses_at_most_one_detection() -> None:
    record = CamaRecord(pin="P1", cls="shed", area_sqft=120, year=2016)
    out = _run(
        [],
        [
            _d("b1", "B", box(0, 0, 10, 12), cls="shed"),
            _d("b2", "B", box(50, 50, 60, 62), cls="shed"),
        ],
        cama=[record],
    )
    assert sum(r.already_assessed for r in out.values()) == 1


def test_an_expansion_is_compared_to_cama_by_its_added_area() -> None:
    record = CamaRecord(pin="P1", cls="primary_structure", area_sqft=300, year=2018)
    out = _run([_d("a1", "A", box(0, 0, 30, 50))], [_d("b1", "B", box(0, 0, 36, 50))], [record])
    assert out["b1"].change_type == "expanded" and out["b1"].already_assessed
