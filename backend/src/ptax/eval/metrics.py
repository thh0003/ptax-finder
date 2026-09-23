"""Detector metrics over a stratified sample, carried back to the county population.

The evaluation set draws a fixed quota from each stratum, so its own positive rate is
several times the county's. Every rate reported from it therefore has to be reweighted, or
it describes a population that does not exist: on the shipped set the raw sample runs at
roughly a 33% positive rate against a county base rate of 5.6%, which flatters precision by
about sixfold.

One weighting scheme does all of it. A parcel sampled from a stratum of ``N`` county
parcels when ``n`` were drawn stands for ``N / n`` parcels, and every figure below -- flag
rate, precision, recall, average precision, top-of-ranking precision -- is that same
weighted count. Raw sample figures are reported beside the reweighted ones, because a
reader needs to see how few parcels an estimate rests on.

This module is pure: it takes labels and scores and returns numbers.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from ptax.eval.dataset import POSITIVE, STRATA


@dataclass(frozen=True)
class ScoredParcel:
    """One parcel: how it was sampled, whether it improved, and what the detector said.

    ``stratum`` and ``positive`` answer different questions and must not be conflated.
    ``stratum`` is how the parcel entered the sample, so it sets the parcel's weight and
    can never be revised. ``positive`` is whether an improvement is actually visible
    between the two captures, which is the outcome the detector is judged against.

    They started out as the same thing, because ``BUILD_YR`` was the only label available.
    Reading all 300 parcels by eye showed the two disagree on 31 of them in both
    directions, so the outcome now travels separately. Leaving ``positive`` unset falls
    back to the stratum, which is what every ``BUILD_YR``-labelled result used.
    """

    pid: str
    stratum: str
    score: float | None
    flagged: bool
    skipped_reason: str | None = None
    positive: bool | None = None

    @property
    def scored(self) -> bool:
        """A parcel the detector declined to score is not evidence either way."""
        return self.skipped_reason is None and self.score is not None

    @property
    def improved(self) -> bool:
        """Whether this parcel is a positive, by label if it has one and stratum if not."""
        return self.stratum == POSITIVE if self.positive is None else self.positive


@dataclass(frozen=True)
class StratumResult:
    """Raw sample counts for one stratum. No weighting applied."""

    stratum: str
    scored: int
    flagged: int
    skipped: int

    @property
    def flag_rate(self) -> float:
        return self.flagged / self.scored if self.scored else 0.0


@dataclass(frozen=True)
class Summary:
    """Reweighted headline figures, with the raw sample beside them."""

    base_rate: float
    flag_rate: float
    precision: float
    recall: float
    f1: float
    false_positive_rate: float | None
    raw_flag_rate: float
    raw_precision: float
    scored: int
    skipped: int
    per_stratum: dict[str, StratumResult] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)


def stratum_counts(parcels: list[ScoredParcel]) -> dict[str, StratumResult]:
    """Raw per-stratum counts, with every known stratum present even when empty."""
    results: dict[str, StratumResult] = {}
    for stratum in STRATA:
        members = [p for p in parcels if p.stratum == stratum]
        scored = [p for p in members if p.scored]
        results[stratum] = StratumResult(
            stratum=stratum,
            scored=len(scored),
            flagged=sum(1 for p in scored if p.flagged),
            skipped=len(members) - len(scored),
        )
    return results


def parcel_weights(
    counts: dict[str, StratumResult], county: dict[str, int]
) -> dict[str, float]:
    """How many county parcels each sampled parcel stands for, per stratum.

    A stratum with nothing scored gets weight 0 rather than dividing by zero; it then
    contributes nothing to a weighted total, which is the honest treatment of a stratum
    that carries no evidence.
    """
    return {
        stratum: (county.get(stratum, 0) / result.scored if result.scored else 0.0)
        for stratum, result in counts.items()
    }


def _weighted(
    parcels: list[ScoredParcel], weights: dict[str, float]
) -> tuple[float, float, float, float]:
    """(total, flagged, flagged-and-positive, positive) weight over scored parcels.

    The positive weight is summed per parcel rather than taken as the positive stratum's
    share, because an improvement can now sit in any stratum.
    """
    total = flagged = true_positive = positive = 0.0
    for parcel in parcels:
        if not parcel.scored:
            continue
        weight = weights.get(parcel.stratum, 0.0)
        total += weight
        if parcel.improved:
            positive += weight
        if parcel.flagged:
            flagged += weight
            if parcel.improved:
                true_positive += weight
    return total, flagged, true_positive, positive


def summarise(parcels: list[ScoredParcel], county: dict[str, int]) -> Summary:
    """Every headline figure, reweighted to ``county``'s stratum sizes."""
    counts = stratum_counts(parcels)
    weights = parcel_weights(counts, county)
    total, flagged, true_positive, positive_weight = _weighted(parcels, weights)

    negative_weight = total - positive_weight
    negative_flagged = flagged - true_positive

    base_rate = positive_weight / total if total else 0.0
    flag_rate = flagged / total if total else 0.0
    precision = true_positive / flagged if flagged else 0.0
    recall = true_positive / positive_weight if positive_weight else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    # With no negative sampled at all the false-positive rate is unknown, not zero.
    false_positive_rate = negative_flagged / negative_weight if negative_weight else None

    scored = [p for p in parcels if p.scored]
    raw_flagged = [p for p in scored if p.flagged]
    return Summary(
        base_rate=base_rate,
        flag_rate=flag_rate,
        precision=precision,
        recall=recall,
        f1=f1,
        false_positive_rate=false_positive_rate,
        raw_flag_rate=len(raw_flagged) / len(scored) if scored else 0.0,
        raw_precision=(
            sum(1 for p in raw_flagged if p.improved) / len(raw_flagged)
            if raw_flagged
            else 0.0
        ),
        scored=len(scored),
        skipped=sum(r.skipped for r in counts.values()),
        per_stratum=counts,
        weights=weights,
    )


def _ranked(parcels: list[ScoredParcel]) -> list[ScoredParcel]:
    """Scored parcels, best first. Ties keep a stable order so runs are reproducible."""
    scored = [p for p in parcels if p.scored]
    return sorted(scored, key=lambda p: (-(p.score or 0.0), p.pid))


def average_precision(parcels: list[ScoredParcel], weights: dict[str, float]) -> float:
    """Weighted average precision: the threshold-free view of how well the score ranks.

    This is what tells Tasks 4-6 whether the underlying signal improved while the score is
    still saturated. Without it a saturated score hides every gain until Task 7 recalibrates
    it.

    Tied scores are treated as one group, so a detector that gives every parcel the same
    score scores the population's positive share rather than an accidental 1.0 from however
    the ties happened to sort.
    """
    ranked = _ranked(parcels)
    total_positive = sum(weights.get(p.stratum, 0.0) for p in ranked if p.improved)
    if not total_positive:
        return 0.0

    seen = hits = score_sum = 0.0
    index = 0
    while index < len(ranked):
        group_end = index
        while group_end < len(ranked) and ranked[group_end].score == ranked[index].score:
            group_end += 1
        group = ranked[index:group_end]
        group_weight = sum(weights.get(p.stratum, 0.0) for p in group)
        group_hits = sum(weights.get(p.stratum, 0.0) for p in group if p.improved)
        seen += group_weight
        hits += group_hits
        # Every positive in a tied group is retrieved at the same precision.
        score_sum += group_hits * (hits / seen if seen else 0.0)
        index = group_end
    return score_sum / total_positive


def precision_at_top_fraction(
    parcels: list[ScoredParcel], weights: dict[str, float], fraction: float
) -> float:
    """Weighted precision over the highest-scoring ``fraction`` of the population.

    This is the figure that matters for a precision-first queue: a reviewer works down
    from the top, so what the top 1% looks like decides whether the queue is worth opening.
    """
    ranked = _ranked(parcels)
    total = sum(weights.get(p.stratum, 0.0) for p in ranked)
    if not total or fraction <= 0:
        return 0.0

    budget = total * fraction
    taken = hits = 0.0
    for parcel in ranked:
        weight = weights.get(parcel.stratum, 0.0)
        if taken + weight > budget and taken > 0:
            break
        taken += weight
        if parcel.improved:
            hits += weight
    return hits / taken if taken else 0.0


def apply_audit(
    parcels: list[ScoredParcel], audit: dict[str, Any]
) -> tuple[list[ScoredParcel], int]:
    """Re-stratify audited parcels, returning the corrected list and how many moved.

    The audit is a measurement of the labels, not of the detector, so it is applied as an
    overlay rather than written back into the set: `score` reports raw and audited figures
    side by side so a reader can see how much of a change is label correction. A parcel
    corrected to ``excluded`` leaves the population entirely, exactly as an unlabelled
    parcel does.
    """
    corrections = {
        v["pid"]: v["corrected_stratum"]
        for v in audit.get("verdicts", [])
        if v.get("verdict") == "corrected" and v.get("corrected_stratum")
    }
    if not corrections:
        return parcels, 0

    updated: list[ScoredParcel] = []
    moved = 0
    for parcel in parcels:
        target = corrections.get(parcel.pid)
        if target is None:
            updated.append(parcel)
            continue
        moved += 1
        if target not in STRATA:
            continue  # corrected out of the population, e.g. to `excluded`
        # `replace` rather than a field-by-field rebuild: the audit corrects the *stratum*
        # and must leave everything else — above all the visual `positive` label — intact.
        # Listing fields by hand silently dropped `positive`, so running `--audit` with
        # `--labels` reverted the corrected parcel to stratum-as-truth.
        updated.append(replace(parcel, stratum=target))
    return updated, moved
