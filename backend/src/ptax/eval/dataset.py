"""The labelled evaluation set: label rule, area filter, stratified sampling, file format.

Labels come from the county assessor's ``BUILD_YR`` for a parcel's *principal structure*,
so they are objective and reproducible but noisy in one direction: a parcel that gained a
garage, an addition or a barn keeps its old year and is labelled negative. Task 3's audit
measures that rate; nothing here tries to hide it.

Sampling is stratified with a fixed quota per stratum, not proportional, and every stratum
records ``sampled / eligible``. The evaluation AOI is a growth fringe whose positive rate
is several times the county's, so a rate measured on the raw sample means nothing outside
it — ``ptax.eval.metrics`` reweights back to the county base rate using these ratios.

This module is pure: no network, no imagery, no database.
"""

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POSITIVE = "positive"
NEGATIVE_OLD = "negative_old"
NEGATIVE_FUTURE = "negative_future"
EXCLUDED = "excluded"

#: Sampling strata, in report order. ``EXCLUDED`` is deliberately absent: a parcel with no
#: assessor year cannot be scored as either outcome, so it leaves the population entirely
#: rather than diluting a reweighted rate.
STRATA = (POSITIVE, NEGATIVE_OLD, NEGATIVE_FUTURE)

#: Areas of interest, as (min_lon, min_lat, max_lon, max_lat). ``nw-hennepin`` is a growth
#: fringe in NW Hennepin County verified to hold 1038 parcels with NAIP coverage for
#: 2010/2013/2015/2017/2019/2021/2023 — the cross-resolution pair and the control pair.
AOIS: dict[str, tuple[float, float, float, float]] = {
    "nw-hennepin": (-93.60, 45.16, -93.57, 45.19),
}

#: ``PARCEL_AREA`` is square feet. Below the floor are condominium slivers and common-area
#: remnants; above the ceiling are farm and institutional parcels whose imagery is mostly
#: field. Neither tells us anything about detecting a house.
MIN_PARCEL_M2 = 400.0
MAX_PARCEL_M2 = 40_000.0
_SQFT_TO_M2 = 0.09290304

#: The only attributes that may reach a committed set file. The service query already
#: restricts ``outFields`` to these; this tuple is the backstop that keeps owner and
#: address fields out of the repository even if that query is ever widened.
PARCEL_FIELDS = ("PID", "BUILD_YR", "PARCEL_AREA", "STATE_CD")


def build_year(raw: object) -> int:
    """``BUILD_YR`` as an int, or 0 when the assessor has no year.

    The service returns this as *text* (``'0000'``, ``'1988'``), so a numeric comparison
    raises and a numeric ``WHERE`` clause silently matches nothing.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def area_m2(raw: object) -> float | None:
    """``PARCEL_AREA`` (square feet) in square metres, or None when absent."""
    try:
        return float(str(raw).strip()) * _SQFT_TO_M2
    except (TypeError, ValueError):
        return None


def label_for(year: int, *, base_year: int, target_year: int) -> str:
    """The stratum a ``BUILD_YR`` falls in for one base/target comparison.

    A structure built *in* the base year is already standing in the base capture, so the
    boundary is exclusive there and inclusive at the target year. Anything built after the
    target year is the hardest kind of negative — the lot is platted and often graded in
    the target imagery, but carries no building yet.
    """
    if year <= 0:
        return EXCLUDED
    if year <= base_year:
        return NEGATIVE_OLD
    if year <= target_year:
        return POSITIVE
    return NEGATIVE_FUTURE


def is_eligible(parcel: dict[str, Any]) -> bool:
    area = area_m2(parcel.get("PARCEL_AREA"))
    return area is not None and MIN_PARCEL_M2 <= area <= MAX_PARCEL_M2


def stratify(
    parcels: list[dict[str, Any]], *, base_year: int, target_year: int
) -> dict[str, list[dict[str, Any]]]:
    """Group eligible parcels into the three sampling strata.

    The area filter runs *before* labelling so an ineligible parcel never reaches a
    stratum — ``eligible`` counts recorded downstream are then population sizes for the
    parcels the detector would actually be asked about.
    """
    strata: dict[str, list[dict[str, Any]]] = {label: [] for label in STRATA}
    for parcel in parcels:
        if not is_eligible(parcel):
            continue
        label = label_for(
            build_year(parcel.get("BUILD_YR")), base_year=base_year, target_year=target_year
        )
        if label in strata:
            strata[label].append(parcel)
    return strata


@dataclass(frozen=True)
class Stratum:
    """One stratum's sampled parcels plus the population they were drawn from."""

    parcels: list[dict[str, Any]]
    eligible: int

    @property
    def sampled(self) -> int:
        return len(self.parcels)

    @property
    def inclusion_probability(self) -> float:
        """``sampled / eligible`` — the weight that reverses the stratified draw."""
        return self.sampled / self.eligible if self.eligible else 0.0


def sample_strata(
    strata: dict[str, list[dict[str, Any]]], *, quota: int, seed: int
) -> dict[str, Stratum]:
    """Draw up to ``quota`` parcels from each stratum, reproducibly.

    Parcels are sorted by ``PID`` before drawing because the service does not promise a
    stable result order; without that sort the same seed would yield a different file on
    each run. A stratum smaller than the quota is taken whole and records an inclusion
    probability of 1.0, so a shortfall is visible in the file rather than silent.
    """
    sampled: dict[str, Stratum] = {}
    for label in STRATA:
        population = sorted(strata.get(label, []), key=lambda p: str(p.get("PID", "")))
        # Seeding per stratum keeps each draw independent of the others' sizes and order.
        rng = random.Random(f"{seed}:{label}")
        take = min(quota, len(population))
        chosen = rng.sample(population, take) if take else []
        chosen.sort(key=lambda p: str(p.get("PID", "")))
        sampled[label] = Stratum(parcels=chosen, eligible=len(population))
    return sampled


@dataclass(frozen=True)
class EvalSet:
    """A committed evaluation set: which parcels, which labels, and how they were drawn."""

    aoi: str
    base_year: int
    target_year: int
    seed: int
    strata: dict[str, Stratum]
    county_build_year_counts: dict[str, int]

    @property
    def county_strata(self) -> dict[str, int]:
        """County population size of each sampling stratum.

        These are the weights that carry a stratified sample's rates back to the county.
        Parcels with no assessor year are excluded here exactly as they are excluded from
        the sample, so the population the metrics describe is the one they were drawn from.
        """
        return {label: int(self.county_build_year_counts.get(label, 0)) for label in STRATA}

    @property
    def county_base_rate(self) -> float:
        """Share of labelled county parcels that gained a principal structure in the window.

        This is the prevalence every reported rate is reweighted to. Measuring it across the
        whole county needs no imagery — only assessor counts.
        """
        strata = self.county_strata
        total = sum(strata.values())
        return strata[POSITIVE] / total if total else 0.0


@dataclass(frozen=True)
class VisualLabels:
    """What the imagery shows, parcel by parcel, independent of ``BUILD_YR``.

    Produced by reading every parcel's base/target chip pair by eye. ``BUILD_YR`` answers
    "when was the principal structure recorded", which turned out to answer a different
    question from "does the target capture show something the base capture does not" --
    they disagreed on 31 of 300 parcels, in both directions.
    """

    improved: dict[str, bool]
    stratum: dict[str, str]

    def counts(self, stratum: str) -> tuple[int, int]:
        """``(improved, labelled)`` within one sampling stratum."""
        members = [pid for pid, name in self.stratum.items() if name == stratum]
        return sum(self.improved[pid] for pid in members), len(members)

    def rate(self, stratum: str) -> float:
        """Share of that stratum's labelled parcels showing an improvement."""
        improved, labelled = self.counts(stratum)
        return improved / labelled if labelled else 0.0


def read_visual_labels_payload(payload: dict[str, Any]) -> VisualLabels:
    return VisualLabels(
        improved={str(row["pid"]): bool(row["improved"]) for row in payload["labels"]},
        stratum={str(row["pid"]): str(row["stratum"]) for row in payload["labels"]},
    )


def read_visual_labels(path: Path) -> VisualLabels:
    return read_visual_labels_payload(json.loads(Path(path).read_text()))


def improvement_base_rate(labels: VisualLabels, county: dict[str, int]) -> float:
    """Share of county parcels showing a visible improvement, from the labelled sample.

    Each stratum's measured improvement rate is weighted by how much of the county that
    stratum holds. This replaces ``EvalSet.county_base_rate``, which was the share of
    parcels whose *recorded* build year falls in the window -- a different quantity, and
    on this AOI a much smaller one, because the stratum holding 92% of the county turns
    out to contain most of the visible improvements.

    A stratum with nothing labelled leaves the calculation entirely rather than counting
    as 0%: no evidence is not evidence of no improvement.
    """
    weighted = population = 0.0
    for stratum in STRATA:
        improved, labelled = labels.counts(stratum)
        if not labelled:
            continue
        size = county.get(stratum, 0)
        weighted += size * (improved / labelled)
        population += size
    return weighted / population if population else 0.0


def _clean(parcel: dict[str, Any]) -> dict[str, Any]:
    """A parcel reduced to the fields allowed in a committed file, plus its geometry."""
    kept: dict[str, Any] = {k: parcel[k] for k in PARCEL_FIELDS if k in parcel}
    if "geometry" in parcel:
        kept["geometry"] = parcel["geometry"]
    return kept


def write_set(path: Path, eval_set: EvalSet) -> None:
    """Serialise deterministically: same inputs, byte-identical file."""
    payload = {
        "aoi": eval_set.aoi,
        "base_year": eval_set.base_year,
        "target_year": eval_set.target_year,
        "seed": eval_set.seed,
        "county_build_year_counts": eval_set.county_build_year_counts,
        "strata": {
            label: {
                "eligible": eval_set.strata[label].eligible,
                "sampled": eval_set.strata[label].sampled,
                "inclusion_probability": round(
                    eval_set.strata[label].inclusion_probability, 6
                ),
                "parcels": [_clean(p) for p in eval_set.strata[label].parcels],
            }
            for label in STRATA
            if label in eval_set.strata
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_set(path: Path) -> EvalSet:
    payload = json.loads(Path(path).read_text())
    return EvalSet(
        aoi=payload["aoi"],
        base_year=payload["base_year"],
        target_year=payload["target_year"],
        seed=payload["seed"],
        strata={
            label: Stratum(parcels=body["parcels"], eligible=body["eligible"])
            for label, body in payload["strata"].items()
        },
        county_build_year_counts=payload["county_build_year_counts"],
    )
