import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ptax.detection.detector import ParcelRaster
from ptax.eval.inventory import (
    BARS,
    MIN_LABELS,
    LabelError,
    draw_sample,
    labels_template,
    read_labels,
    render_sheets,
    score_inventory,
    stratum_of,
)

PII = ("owner_name", "ADDR1", "prop_street")


def test_strata_come_from_the_county_record() -> None:
    assert stratum_of({"det_gar_area": 480.0, "gar_area": 0}) == "detached_garage"
    assert stratum_of({"det_gar_area": 0, "gar_area": 400, "year_built": 1990}) == "attached_garage"
    assert stratum_of({"det_gar_area": 0, "gar_area": 0, "year_built": 1955}) == "house_no_garage"
    assert stratum_of({"total_living_area": 900}) == "house_no_garage"
    assert stratum_of({"year_built": 0, "total_living_area": 0}) == "vacant"
    assert stratum_of({}) == "vacant"


def _population() -> list[tuple[str, dict]]:
    """120 detached, 30 attached, 200 houses without a garage, 8 vacant."""
    parcels: list[tuple[str, dict]] = []
    for n in range(120):
        parcels.append((f"D{n:03}", {"det_gar_area": 500, "year_built": 1960}))
    for n in range(30):
        parcels.append((f"A{n:03}", {"gar_area": 400, "year_built": 1990}))
    for n in range(200):
        parcels.append((f"H{n:03}", {"year_built": 1950, "total_living_area": 1000}))
    for n in range(8):
        parcels.append((f"V{n:03}", {"year_built": 0}))
    return parcels


def test_the_sample_is_200_deterministic_and_covers_every_stratum() -> None:
    sample = draw_sample(_population(), seed=7)
    again = draw_sample(list(reversed(_population())), seed=7)
    other = draw_sample(_population(), seed=8)

    assert len(sample) == 200
    assert sample == again, "the draw depends on the seed, not the input order"
    assert sample != other
    assert len({p["PIN"] for p in sample}) == 200
    by_stratum: dict[str, int] = {}
    for parcel in sample:
        by_stratum[parcel["stratum"]] = by_stratum.get(parcel["stratum"], 0) + 1
    # Every small stratum is taken whole; the rest is topped up to 200.
    assert by_stratum["vacant"] == 8 and by_stratum["attached_garage"] == 30
    assert by_stratum["detached_garage"] >= 50 and by_stratum["house_no_garage"] >= 50
    assert [p["n"] for p in sample] == list(range(1, 201))


def test_the_labels_template_holds_no_owner_or_address_field(tmp_path: Path) -> None:
    sample = draw_sample(_population(), seed=7)
    template = labels_template(sample)
    text = json.dumps(template)
    for field in PII:
        assert field not in text
    first = template["parcels"][0]
    assert first == {
        "n": 1,
        "PIN": sample[0]["PIN"],
        "house": None,
        "garage": None,
        "shed": None,
        "pool": None,
        "other": None,
        "note": "",
    }
    assert any("attached garage" in rule for rule in template["rules"])


def _write_labels(tmp_path: Path, parcels: list[dict]) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"rules": [], "parcels": parcels}))
    return path


def _label(pin: str, **counts: int) -> dict:
    return {
        "n": 1,
        "PIN": pin,
        **{k: counts.get(k, 0) for k in ("house", "garage", "shed", "pool", "other")},
        "note": "",
    }


def test_labels_must_be_complete_counts(tmp_path: Path) -> None:
    good = _write_labels(tmp_path, [_label("P1", house=1), _label("P2")])
    assert read_labels(good) == {
        "P1": {"house": 1, "garage": 0, "shed": 0, "pool": 0, "other": 0},
        "P2": {"house": 0, "garage": 0, "shed": 0, "pool": 0, "other": 0},
    }
    unfilled = _label("P3")
    unfilled["garage"] = None
    negative = _label("P4", shed=-1)
    bad = _write_labels(tmp_path, [unfilled, negative])
    with pytest.raises(LabelError) as caught:
        read_labels(bad)
    message = str(caught.value)
    assert "P3" in message and "garage" in message and "P4" in message


def test_scoring_reproduces_hand_counted_precision_and_recall() -> None:
    labels: dict[str, dict[str, int]] = {}
    predicted: dict[str, dict[str, int] | None] = {}
    county: dict[str, dict] = {}

    def parcel(pin: str, label: dict[str, int], found: dict[str, int] | None, record: dict) -> None:
        labels[pin] = {"house": 0, "garage": 0, "shed": 0, "pool": 0, "other": 0, **label}
        predicted[pin] = found
        county[pin] = record

    # 20 houses: the model finds 19 (one miss), and invents a house on 1 empty lot.
    for n in range(19):
        parcel(f"H{n}", {"house": 1}, {"house": 1}, {"year_built": 1960})
    parcel("H19", {"house": 1}, {}, {"year_built": 1960})
    # 12 detached garages labelled: 9 found; plus 1 found where there is none.
    for n in range(12):
        found = {"house": 1, "garage": 1} if n < 9 else {"house": 1}
        parcel(f"G{n}", {"house": 1, "garage": 1}, found, {"det_gar_area": 400})
    # 3 pools labelled, all found: too few labels to judge.
    for n in range(3):
        parcel(f"P{n}", {"house": 1, "pool": 1}, {"house": 1, "pool": 1}, {"year_built": 2000})
    # 10 empty lots: 8 read empty, 1 with a phantom house, 1 with a phantom garage.
    for n in range(8):
        parcel(f"E{n}", {}, {}, {})
    parcel("E8", {}, {"house": 1}, {})
    parcel("E9", {}, {"garage": 1}, {})
    # One the model could not answer: reported, not scored.
    parcel("X0", {"house": 1}, None, {"year_built": 1970})

    report = score_inventory(predicted, labels, county)

    house = report.kinds["house"]
    # Labelled houses: 20 + 12 + 3 = 35 (X0 is unscored); found 34; one phantom.
    assert house.n == 35
    assert house.precision == pytest.approx(34 / 35)
    assert house.recall == pytest.approx(34 / 35)
    assert house.verdict == "pass"  # 0.971 >= 0.95 both ways

    garage = report.kinds["garage"]
    assert garage.n == 12
    assert garage.precision == pytest.approx(9 / 10)
    assert garage.recall == pytest.approx(9 / 12)
    assert garage.verdict == "fail"  # recall 0.75 < 0.85

    pool = report.kinds["pool"]
    assert (pool.n, pool.verdict) == (3, "too few labels to judge")
    assert pool.n < MIN_LABELS

    assert report.no_structures.n == 10
    assert report.no_structures.rate == pytest.approx(8 / 10)
    assert report.no_structures.verdict == "fail"
    assert report.unscored == ["X0"]
    # County agreement: of 12 recorded detached garages, the model found 9.
    assert report.county["detached garage on record → garage found"] == (9, 12)
    assert BARS["garage"] == 0.85


def test_sheets_number_each_parcel_and_never_show_markup(tmp_path: Path) -> None:
    raster = ParcelRaster(
        data=np.full((3, 40, 60), 120, dtype=np.uint8),
        mask=np.ones((40, 60), dtype=bool),
        parcel_mask=np.pad(np.ones((30, 50), dtype=bool), 5),
        transform=from_origin(0, 40, 1, 1),
        crs=CRS.from_epsg(32615),
        resolution_m=1.0,
        bounds=(0, 0, 60, 40),
    )
    sample = [{"n": n, "PIN": f"P{n}", "stratum": "vacant"} for n in range(1, 24)]
    rasters = {f"P{n}": raster for n in range(1, 24)}
    rasters["P5"] = None  # no imagery: still gets its numbered cell

    index = render_sheets(sample, rasters.get, tmp_path, per_sheet=20, cell_px=128)

    sheets = sorted(tmp_path.glob("sheet-*.png"))
    assert [s.name for s in sheets] == ["sheet-01.png", "sheet-02.png"]
    assert [c["n"] for c in index["cells"]] == list(range(1, 24))
    # Five to a row: parcel 5 ends the first row, parcel 21 starts the second sheet.
    assert index["cells"][4] == {
        "n": 5,
        "PIN": "P5",
        "sheet": "sheet-01.png",
        "row": 0,
        "column": 4,
        "imagery": False,
    }
    assert index["cells"][20]["sheet"] == "sheet-02.png"
    text = json.dumps(index)
    for field in (*PII, "structures", "score"):
        assert field not in text


def test_each_parcel_fills_its_cell_and_is_outlined_in_the_models_yellow(tmp_path: Path) -> None:
    # One pixel too big for a whole-step letterbox, which would halve it.
    raster = ParcelRaster(
        data=np.full((3, 65, 129), 120, dtype=np.uint8),
        mask=np.ones((65, 129), dtype=bool),
        parcel_mask=np.pad(np.ones((45, 109), dtype=bool), 10),
        transform=from_origin(0, 65, 1, 1),
        crs=CRS.from_epsg(32615),
        resolution_m=1.0,
        bounds=(0, 0, 129, 65),
    )
    render_sheets(
        [{"n": 1, "PIN": "P1", "stratum": "vacant"}], lambda pin: raster, tmp_path, cell_px=128
    )
    from PIL import Image

    sheet = np.asarray(Image.open(tmp_path / "sheet-01.png"))
    from ptax.eval.chips import GUTTER, GUTTER_VALUE

    cell = sheet[GUTTER : GUTTER + 128, GUTTER : GUTTER + 128]
    filled = ~(cell == GUTTER_VALUE).all(axis=2)
    assert filled.any(axis=0).sum() >= 126, "the parcel is scaled to the cell's width"
    yellow = (cell[:, :, 0] > 200) & (cell[:, :, 1] > 180) & (cell[:, :, 2] < 80)
    assert yellow.sum() > 300, "the boundary is drawn thick, in yellow"
