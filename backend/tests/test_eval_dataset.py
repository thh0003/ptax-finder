"""Pure logic of the evaluation set builder: label rule, area filter, seeded sampling.

Network access (`ptax.eval.sources`) is exercised by the `ptax-eval build` command in the
plan's Definition of Done, not here.
"""

import json
from pathlib import Path

import pytest

from ptax.eval.dataset import (
    AOIS,
    EXCLUDED,
    NEGATIVE_FUTURE,
    NEGATIVE_OLD,
    POSITIVE,
    EvalSet,
    Stratum,
    area_m2,
    build_year,
    improvement_base_rate,
    label_for,
    read_set,
    read_visual_labels,
    read_visual_labels_payload,
    sample_strata,
    stratify,
    write_set,
)

#: County-wide stratum populations, shaped like `sources.county_build_year_counts`.
COUNTY_COUNTS = {
    "total": 448090,
    POSITIVE: 25167,
    NEGATIVE_OLD: 387280,
    NEGATIVE_FUTURE: 20000,
    "no_year": 15643,
    "base_year": 2010,
    "target_year": 2021,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1988", 1988),
        ("0000", 0),
        ("  2015  ", 2015),
        ("", 0),
        (None, 0),
        ("N/A", 0),
        (1997, 1997),
    ],
)
def test_build_year_coerces_the_services_text_field(raw: object, expected: int) -> None:
    """BUILD_YR arrives as text ('0000', '1988'); a numeric comparison would raise."""
    assert build_year(raw) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2010, NEGATIVE_OLD),  # built in the base year itself: present in both captures
        (2011, POSITIVE),  # first year that can appear between the two captures
        (2021, POSITIVE),  # built in the target year: last year that can appear
        (2022, NEGATIVE_FUTURE),  # platted or graded by 2021, built after it
        (1900, NEGATIVE_OLD),
        (0, EXCLUDED),  # no assessor year: vacant land or a missing record
    ],
)
def test_label_rule_at_every_boundary_year(year: int, expected: str) -> None:
    assert label_for(year, base_year=2010, target_year=2021) == expected


def test_area_filter_drops_condo_slivers_and_multi_hectare_outliers() -> None:
    # PARCEL_AREA is square feet; the observed AOI spans 1 482 to 4 929 332 sq ft.
    assert area_m2("1482.55735832") == pytest.approx(137.7, abs=0.5)
    assert area_m2("10655.742284065") == pytest.approx(990.0, abs=0.5)
    assert area_m2(None) is None
    assert area_m2("") is None


def test_stratify_applies_the_area_filter_before_labelling() -> None:
    parcels = [
        {"PID": "sliver", "BUILD_YR": "2015", "PARCEL_AREA": "1482"},  # 138 m2: too small
        {"PID": "farm", "BUILD_YR": "2015", "PARCEL_AREA": "4929332"},  # 458 ha: too large
        {"PID": "new", "BUILD_YR": "2015", "PARCEL_AREA": "10655"},  # 990 m2
        {"PID": "old", "BUILD_YR": "1955", "PARCEL_AREA": "10655"},
        {"PID": "future", "BUILD_YR": "2023", "PARCEL_AREA": "10655"},
        {"PID": "unknown", "BUILD_YR": "0000", "PARCEL_AREA": "10655"},
    ]
    strata = stratify(parcels, base_year=2010, target_year=2021)

    assert [p["PID"] for p in strata[POSITIVE]] == ["new"]
    assert [p["PID"] for p in strata[NEGATIVE_OLD]] == ["old"]
    assert [p["PID"] for p in strata[NEGATIVE_FUTURE]] == ["future"]
    # Excluded is not a sampling stratum: both the unlabelled and the out-of-range parcels
    # leave the population entirely, so they cannot dilute a reweighted rate.
    assert EXCLUDED not in strata


def _population(n: int, label_year: int) -> list[dict[str, str]]:
    return [
        {"PID": f"{label_year}-{i:04d}", "BUILD_YR": str(label_year), "PARCEL_AREA": "10655"}
        for i in range(n)
    ]


def test_sampling_is_reproducible_for_one_seed_and_differs_across_seeds() -> None:
    strata = stratify(
        _population(50, 2015) + _population(50, 1955),
        base_year=2010,
        target_year=2021,
    )
    first = sample_strata(strata, quota=10, seed=20260922)
    again = sample_strata(strata, quota=10, seed=20260922)
    other = sample_strata(strata, quota=10, seed=1)

    assert [p["PID"] for p in first[POSITIVE].parcels] == [
        p["PID"] for p in again[POSITIVE].parcels
    ]
    assert [p["PID"] for p in first[POSITIVE].parcels] != [
        p["PID"] for p in other[POSITIVE].parcels
    ]


def test_sample_records_inclusion_probability_so_rates_can_be_reweighted() -> None:
    strata = stratify(
        _population(50, 2015) + _population(200, 1955),
        base_year=2010,
        target_year=2021,
    )
    sample = sample_strata(strata, quota=10, seed=20260922)

    assert sample[POSITIVE].sampled == 10
    assert sample[POSITIVE].eligible == 50
    assert sample[POSITIVE].inclusion_probability == pytest.approx(0.2)
    assert sample[NEGATIVE_OLD].inclusion_probability == pytest.approx(0.05)


def test_a_stratum_smaller_than_the_quota_is_taken_whole_and_says_so() -> None:
    """The 2013-2017 control has 58 eligible positives against a quota of 100."""
    strata = stratify(_population(58, 2015), base_year=2013, target_year=2017)
    sample = sample_strata(strata, quota=100, seed=20260922)

    assert sample[POSITIVE].sampled == 58
    assert sample[POSITIVE].eligible == 58
    assert sample[POSITIVE].inclusion_probability == pytest.approx(1.0)


def test_eval_set_round_trips_and_omits_owner_and_address_fields(tmp_path) -> None:
    strata = stratify(_population(4, 2015), base_year=2010, target_year=2021)
    # A parcel carrying owner/address attributes must not reach the committed file even if
    # the service ever returns them; the query restricts outFields, this is the backstop.
    strata[POSITIVE][0]["OWNER_NM"] = "A PERSON"
    strata[POSITIVE][1]["STREET_NM"] = "PARK LA"
    sample = sample_strata(strata, quota=4, seed=20260922)

    path = tmp_path / "set.json"
    original = EvalSet(
        aoi="nw-hennepin",
        base_year=2010,
        target_year=2021,
        seed=20260922,
        strata={k: Stratum(**vars(v)) for k, v in sample.items()},
        county_build_year_counts=COUNTY_COUNTS,
    )
    write_set(path, original)
    loaded = read_set(path)

    assert loaded.base_year == 2010
    assert loaded.target_year == 2021
    # Parcels with no assessor year are outside the sample, so they are outside the
    # denominator too: the base rate describes the population the sample was drawn from.
    assert loaded.county_strata == {
        POSITIVE: 25167,
        NEGATIVE_OLD: 387280,
        NEGATIVE_FUTURE: 20000,
    }
    assert loaded.county_base_rate == pytest.approx(25167 / (25167 + 387280 + 20000), abs=1e-6)
    assert [p["PID"] for p in loaded.strata[POSITIVE].parcels] == [
        p["PID"] for p in original.strata[POSITIVE].parcels
    ]
    text = path.read_text()
    for forbidden in ("OWNER_NM", "TAXPAYER_NM", "HOUSE_NO", "STREET_NM", "A PERSON"):
        assert forbidden not in text


def test_write_set_is_byte_identical_across_runs(tmp_path) -> None:
    strata = stratify(_population(6, 2015), base_year=2010, target_year=2021)
    sample = sample_strata(strata, quota=3, seed=20260922)
    eval_set = EvalSet(
        aoi="nw-hennepin",
        base_year=2010,
        target_year=2021,
        seed=20260922,
        strata={k: Stratum(**vars(v)) for k, v in sample.items()},
        county_build_year_counts=COUNTY_COUNTS,
    )
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write_set(first, eval_set)
    write_set(second, eval_set)

    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text())["seed"] == 20260922


def test_default_aoi_matches_the_verified_bounding_box() -> None:
    assert AOIS["nw-hennepin"] == (-93.60, 45.16, -93.57, 45.19)


# --- Visual labels ---------------------------------------------------------------------


def _labels(**counts: tuple[int, int]) -> dict:
    """`stratum=(improved, total)` rendered as a label file's `labels` list."""
    return {
        "labels": [
            {
                "pid": f"{stratum}-{i}",
                "stratum": stratum,
                "improved": i < improved,
                "reason": "structure_added" if i < improved else "no_change",
            }
            for stratum, (improved, total) in counts.items()
            for i in range(total)
        ]
    }


def test_visual_labels_are_read_back_per_parcel(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(_labels(**{POSITIVE: (2, 3), NEGATIVE_OLD: (0, 2)})))

    labels = read_visual_labels(path)

    assert labels.improved[f"{POSITIVE}-0"] is True
    assert labels.improved[f"{POSITIVE}-2"] is False
    assert labels.improved[f"{NEGATIVE_OLD}-0"] is False
    assert labels.rate(POSITIVE) == pytest.approx(2 / 3)
    assert labels.rate(NEGATIVE_OLD) == pytest.approx(0.0)
    assert labels.rate(NEGATIVE_FUTURE) == 0.0  # nothing labelled: contributes nothing


def test_improvement_base_rate_weights_each_stratum_by_its_county_size() -> None:
    """A 14% improvement rate in the stratum holding 92% of the county dominates.

    This is the whole reason the rate has to be re-estimated rather than read off
    `BUILD_YR`: the assessor's positive stratum is a rounding error next to
    `negative_old`, so even a low improvement rate there outweighs it.
    """
    labels = read_visual_labels_payload(
        _labels(**{POSITIVE: (84, 100), NEGATIVE_OLD: (14, 100), NEGATIVE_FUTURE: (1, 100)})
    )
    county = {POSITIVE: 100, NEGATIVE_OLD: 1840, NEGATIVE_FUTURE: 60}

    # 0.84*(100/2000) + 0.14*(1840/2000) + 0.01*(60/2000) = 0.042 + 0.1288 + 0.0003
    assert improvement_base_rate(labels, county) == pytest.approx(0.1711)


def test_a_stratum_with_no_labels_is_dropped_rather_than_counted_as_zero(tmp_path: Path) -> None:
    """An unlabelled stratum carries no evidence; assuming 0% would invent some."""
    labels = read_visual_labels_payload(_labels(**{POSITIVE: (1, 2)}))
    county = {POSITIVE: 100, NEGATIVE_OLD: 900, NEGATIVE_FUTURE: 0}

    # Only the positive stratum is labelled, so the rate is that stratum's own rate.
    assert improvement_base_rate(labels, county) == pytest.approx(0.5)
