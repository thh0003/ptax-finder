"""Metrics over a stratified evaluation sample, reweighted to the county base rate.

The sample deliberately oversamples positives, so every rate has to be carried back to the
population before it means anything. These tests pin that arithmetic against figures
worked out by hand: a reweighting bug would otherwise surface as a detector result.
"""

import pytest

from ptax.eval.dataset import NEGATIVE_FUTURE, NEGATIVE_OLD, POSITIVE
from ptax.eval.metrics import (
    ScoredParcel,
    apply_audit,
    average_precision,
    parcel_weights,
    precision_at_top_fraction,
    stratum_counts,
    summarise,
)

# 100 positives against 900 negatives in the population: a 10% base rate.
COUNTY = {POSITIVE: 100, NEGATIVE_OLD: 800, NEGATIVE_FUTURE: 100}


def _sample(flagged_by_stratum: dict[str, int], size: int = 50) -> list[ScoredParcel]:
    """``size`` parcels per stratum, the first ``flagged_by_stratum[s]`` of them flagged."""
    parcels: list[ScoredParcel] = []
    for stratum, flagged in flagged_by_stratum.items():
        for i in range(size):
            parcels.append(
                ScoredParcel(
                    pid=f"{stratum}-{i}",
                    stratum=stratum,
                    score=1.0 if i < flagged else 0.0,
                    flagged=i < flagged,
                )
            )
    return parcels


def test_weights_carry_each_sampled_parcel_back_to_its_population() -> None:
    parcels = _sample({POSITIVE: 0, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0})
    weights = parcel_weights(stratum_counts(parcels), COUNTY)

    # 50 sampled from 100 positives: each stands for 2 parcels. 50 from 800: each for 16.
    assert weights[POSITIVE] == pytest.approx(2.0)
    assert weights[NEGATIVE_OLD] == pytest.approx(16.0)
    assert weights[NEGATIVE_FUTURE] == pytest.approx(2.0)


def test_reweighted_flag_rate_and_precision_match_the_hand_calculation() -> None:
    """TPR 0.6, FPR 0.1 on old and 0.2 on future, against a 10% base rate.

    By hand: the negatives split 800/900 and 100/900, so
        FPR       = (8/9) * 0.1 + (1/9) * 0.2 = 0.111111
        flag_rate = 0.1 * 0.6 + 0.9 * 0.111111 = 0.16
        precision = (0.1 * 0.6) / 0.16 = 0.375
        recall    = 0.6
        F1        = 2 * 0.375 * 0.6 / (0.375 + 0.6) = 0.461538
    """
    parcels = _sample({POSITIVE: 30, NEGATIVE_OLD: 5, NEGATIVE_FUTURE: 10})
    result = summarise(parcels, COUNTY)

    assert result.recall == pytest.approx(0.6)
    assert result.false_positive_rate == pytest.approx(0.111111, abs=1e-6)
    assert result.flag_rate == pytest.approx(0.16, abs=1e-9)
    assert result.precision == pytest.approx(0.375, abs=1e-9)
    assert result.f1 == pytest.approx(0.461538, abs=1e-6)
    assert result.base_rate == pytest.approx(0.1)


def test_raw_sample_rate_is_reported_beside_the_reweighted_one_and_differs() -> None:
    """The stratified sample's own flag rate is far higher; both must be visible."""
    parcels = _sample({POSITIVE: 30, NEGATIVE_OLD: 5, NEGATIVE_FUTURE: 10})
    result = summarise(parcels, COUNTY)

    assert result.raw_flag_rate == pytest.approx(45 / 150)
    assert result.raw_precision == pytest.approx(30 / 45)
    # Reporting the raw figure as if it were the county's would overstate precision ~1.8x.
    assert result.raw_precision > result.precision


def test_flagging_everything_gives_full_recall_and_base_rate_precision() -> None:
    parcels = _sample({POSITIVE: 50, NEGATIVE_OLD: 50, NEGATIVE_FUTURE: 50})
    result = summarise(parcels, COUNTY)

    assert result.recall == pytest.approx(1.0)
    assert result.flag_rate == pytest.approx(1.0)
    assert result.precision == pytest.approx(0.1)  # precision collapses to the base rate


def test_flagging_nothing_reports_zero_rather_than_dividing_by_zero() -> None:
    parcels = _sample({POSITIVE: 0, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0})
    result = summarise(parcels, COUNTY)

    assert result.flag_rate == 0.0
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.f1 == 0.0


def test_skipped_parcels_leave_the_denominator_rather_than_counting_as_negatives() -> None:
    """A parcel the detector could not score is not evidence either way."""
    parcels = _sample({POSITIVE: 10, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0})
    # The last 10 of the 50 positives could not be scored (partial imagery coverage).
    for index in range(40, 50):
        parcels[index] = ScoredParcel(
            pid=parcels[index].pid,
            stratum=parcels[index].stratum,
            score=None,
            flagged=False,
            skipped_reason="partial_coverage",
        )
    result = summarise(parcels, COUNTY)

    assert result.scored == 140
    assert result.skipped == 10
    assert result.recall == pytest.approx(10 / 40)


def test_average_precision_separates_a_perfect_ranker_from_a_blind_one() -> None:
    weights = {POSITIVE: 2.0, NEGATIVE_OLD: 16.0, NEGATIVE_FUTURE: 2.0}
    perfect = [
        ScoredParcel(pid=f"p{i}", stratum=POSITIVE, score=0.9 - i * 0.01, flagged=True)
        for i in range(10)
    ] + [
        ScoredParcel(pid=f"n{i}", stratum=NEGATIVE_OLD, score=0.1 - i * 0.001, flagged=False)
        for i in range(10)
    ]
    assert average_precision(perfect, weights) == pytest.approx(1.0)

    # Every parcel scored identically: ranking carries no information, so average
    # precision collapses to the weighted share of positives.
    tied = [
        ScoredParcel(pid=p.pid, stratum=p.stratum, score=0.5, flagged=p.flagged) for p in perfect
    ]
    share = (10 * 2.0) / (10 * 2.0 + 10 * 16.0)
    assert average_precision(tied, weights) == pytest.approx(share, abs=1e-9)


def test_top_fraction_precision_reads_the_head_of_the_ranking() -> None:
    weights = {POSITIVE: 1.0, NEGATIVE_OLD: 1.0, NEGATIVE_FUTURE: 1.0}
    parcels = [
        ScoredParcel(pid=f"p{i}", stratum=POSITIVE, score=1.0 - i * 0.01, flagged=True)
        for i in range(10)
    ] + [
        ScoredParcel(pid=f"n{i}", stratum=NEGATIVE_OLD, score=0.5 - i * 0.01, flagged=False)
        for i in range(90)
    ]
    # The top 10% of the ranking is exactly the ten positives.
    assert precision_at_top_fraction(parcels, weights, 0.10) == pytest.approx(1.0)
    # The top 20% adds ten negatives.
    assert precision_at_top_fraction(parcels, weights, 0.20) == pytest.approx(0.5)


def test_an_empty_stratum_does_not_crash_or_silently_vanish() -> None:
    parcels = [
        ScoredParcel(pid=f"p{i}", stratum=POSITIVE, score=1.0, flagged=True) for i in range(5)
    ]
    result = summarise(parcels, COUNTY)

    assert result.per_stratum[POSITIVE].scored == 5
    assert result.per_stratum[NEGATIVE_OLD].scored == 0
    assert result.per_stratum[NEGATIVE_OLD].flag_rate == 0.0
    # With no negatives sampled the false-positive rate is unknown, not zero.
    assert result.false_positive_rate is None or result.false_positive_rate == 0.0


def _audit(*verdicts: tuple[str, str, str | None]) -> dict:
    return {
        "audited_total": len(verdicts),
        "verdicts": [
            {"pid": pid, "verdict": verdict, "corrected_stratum": target}
            for pid, verdict, target in verdicts
        ],
    }


def test_audit_moves_a_mislabelled_negative_into_the_positive_stratum() -> None:
    """A parcel that gained an outbuilding is a positive; BUILD_YR cannot see it."""
    parcels = _sample({POSITIVE: 0, NEGATIVE_OLD: 1, NEGATIVE_FUTURE: 0})
    flagged_negative = next(p for p in parcels if p.stratum == NEGATIVE_OLD and p.flagged)

    corrected, moved = apply_audit(
        parcels, _audit((flagged_negative.pid, "corrected", POSITIVE))
    )

    assert moved == 1
    assert next(p for p in corrected if p.pid == flagged_negative.pid).stratum == POSITIVE
    assert summarise(corrected, COUNTY).recall > summarise(parcels, COUNTY).recall


def test_audit_can_remove_a_parcel_from_the_population_entirely() -> None:
    """A positive whose build may post-date the capture is evidence for neither outcome."""
    parcels = _sample({POSITIVE: 5, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0})
    dropped = parcels[0].pid

    corrected, moved = apply_audit(parcels, _audit((dropped, "corrected", "excluded")))

    assert moved == 1
    assert all(p.pid != dropped for p in corrected)
    assert summarise(corrected, COUNTY).scored == summarise(parcels, COUNTY).scored - 1


def test_agreed_and_unclear_verdicts_change_nothing() -> None:
    parcels = _sample({POSITIVE: 3, NEGATIVE_OLD: 3, NEGATIVE_FUTURE: 3})
    audit = _audit(
        (parcels[0].pid, "agreed", None),
        (parcels[1].pid, "unclear", None),
        (parcels[2].pid, "corrected", None),  # corrected but no target recorded
    )

    corrected, moved = apply_audit(parcels, audit)

    assert moved == 0
    assert corrected == parcels


# --- Outcome label independent of sampling stratum -------------------------------------
#
# `BUILD_YR` was the truth until 300 parcels were read by eye and disagreed on 31 of them,
# in both directions: parcels recorded as built in 2021 whose capture predates the build,
# and parcels recorded before 2010 that visibly gained an outbuilding. The stratum still
# decides a parcel's sampling weight -- it is how the parcel was drawn -- but it no longer
# decides whether the parcel is a positive.


def test_a_visible_improvement_in_a_negative_stratum_counts_as_a_true_positive() -> None:
    """The parcel was drawn as `negative_old` but gained a barn; flagging it is correct."""
    parcels = _sample({POSITIVE: 0, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0})
    relabelled = [
        ScoredParcel(
            pid=p.pid,
            stratum=p.stratum,
            score=1.0,
            flagged=True,
            positive=(p.stratum == NEGATIVE_OLD and p.pid.endswith("-0")),
        )
        for p in parcels
    ]

    result = summarise(relabelled, COUNTY)

    # One of 50 `negative_old` parcels is a positive, and that stratum is 800 of 1000
    # county parcels, so the population's positive share is (800/50) / 1000 = 1.6%.
    assert result.base_rate == pytest.approx(0.016)
    # Everything is flagged, so precision is exactly that base rate and recall is 1.
    assert result.precision == pytest.approx(0.016)
    assert result.recall == pytest.approx(1.0)


def test_a_positive_stratum_parcel_with_no_visible_change_is_not_a_positive() -> None:
    """A 2021 `BUILD_YR` whose house post-dates the 2021 capture shows nothing to find."""
    flagged_all = {POSITIVE: 50, NEGATIVE_OLD: 0, NEGATIVE_FUTURE: 0}
    by_build_year = _sample(flagged_all)
    by_eye = [
        ScoredParcel(
            pid=p.pid, stratum=p.stratum, score=p.score, flagged=p.flagged, positive=False
        )
        for p in by_build_year
    ]

    assert summarise(by_build_year, COUNTY).precision > 0.0
    # Nothing in the population is a positive any more, so no flag can be a true one.
    assert summarise(by_eye, COUNTY).base_rate == pytest.approx(0.0)
    assert summarise(by_eye, COUNTY).precision == pytest.approx(0.0)


def test_the_outcome_label_defaults_to_the_stratum_so_older_results_are_unchanged() -> None:
    parcels = _sample({POSITIVE: 30, NEGATIVE_OLD: 5, NEGATIVE_FUTURE: 10})
    explicit = [
        ScoredParcel(
            pid=p.pid,
            stratum=p.stratum,
            score=p.score,
            flagged=p.flagged,
            positive=p.stratum == POSITIVE,
        )
        for p in parcels
    ]

    assert summarise(parcels, COUNTY) == summarise(explicit, COUNTY)


def test_ranking_metrics_follow_the_outcome_label_not_the_stratum() -> None:
    """A ranker that puts every relabelled positive first scores 1.0, whatever its stratum."""
    parcels = [
        ScoredParcel(
            pid=f"{stratum}-{i}",
            stratum=stratum,
            # The five relabelled positives per stratum sort above everything else.
            score=1.0 if i < 5 else 0.0,
            flagged=False,
            positive=i < 5,
        )
        for stratum in (POSITIVE, NEGATIVE_OLD, NEGATIVE_FUTURE)
        for i in range(50)
    ]
    weights = parcel_weights(stratum_counts(parcels), COUNTY)

    assert average_precision(parcels, weights) == pytest.approx(1.0)
    assert precision_at_top_fraction(parcels, weights, 0.01) == pytest.approx(1.0)


def test_an_audit_correction_keeps_the_parcel_s_visual_label() -> None:
    """Re-stratifying a parcel must not silently revert it to stratum-as-truth.

    `--audit` corrects the *stratum*; `--labels` supplies the *outcome*. They answer
    different questions, so an audit that moves a parcel between strata has to leave its
    by-eye verdict alone — otherwise the two flags together reinstate exactly the
    conflation the visual labels were introduced to remove.
    """
    parcels = [
        ScoredParcel(
            pid="p0", stratum=NEGATIVE_OLD, score=1.0, flagged=True, positive=False
        )
    ]

    corrected, moved = apply_audit(parcels, _audit(("p0", "corrected", POSITIVE)))

    assert moved == 1
    assert corrected[0].stratum == POSITIVE  # the audit's correction applies
    assert corrected[0].positive is False  # the eye's verdict does not change
    assert corrected[0].improved is False
