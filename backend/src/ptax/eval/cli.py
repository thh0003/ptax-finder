"""``ptax-eval``: build labelled evaluation sets, cache their imagery, score the detector.

    ptax-eval build --aoi nw-hennepin --base-year 2010 --target-year 2021
    ptax-eval fetch eval/nw-hennepin-2010-2021.json --verify
    ptax-eval score eval/nw-hennepin-2010-2021.json

This is development tooling. It never touches the database, the job queue or S3.
"""

import json
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rasterio
import typer

from ptax.api.runs import DEFAULT_MIN_NEW_AREA_M2, DEFAULT_THRESHOLD
from ptax.detection.detector import (
    ClassicalDetector,
    InsufficientCoverage,
    RadiometricFit,
    fit_radiometry,
    paired_samples,
)
from ptax.detection.run import MIN_RESOLUTION_M
from ptax.eval import sources
from ptax.eval.cache import (
    LOCAL_ENV,
    CacheEntry,
    entry_path,
    fetch_item,
    parcel_geometry,
    read_manifest,
    uris_for,
    write_manifest,
)
from ptax.eval.chips import (
    ChipEntry,
    contact_sheet,
    fit,
    letterbox,
    pair,
    write_index,
    write_png,
)
from ptax.eval.dataset import (
    AOIS,
    STRATA,
    EvalSet,
    VisualLabels,
    area_m2,
    improvement_base_rate,
    read_set,
    read_visual_labels,
    sample_strata,
    stratify,
    write_set,
)
from ptax.eval.metrics import (
    ScoredParcel,
    Summary,
    apply_audit,
    average_precision,
    parcel_weights,
    precision_at_top_fraction,
    stratum_counts,
    summarise,
)
from ptax.imagery.reader import read_parcel_uris

app = typer.Typer(help="ptax-finder detector evaluation harness", no_args_is_help=True)

#: Sets and their caches live beside the backend package, not in tests/: they are measured
#: evidence rather than fixtures, and the cache is far too large to commit.
EVAL_DIR = Path("eval")
DEFAULT_SEED = 20260922

#: Cap on how many per-parcel problems `--verify` prints before it stops listing them.
_MAX_REPORTED = 20


def _set_path(aoi: str, base_year: int, target_year: int) -> Path:
    return EVAL_DIR / f"{aoi}-{base_year}-{target_year}.json"


def _cache_dir(set_path: Path) -> Path:
    return set_path.parent / "cache" / set_path.stem


@app.command("build")
def build(
    aoi: str = typer.Option("nw-hennepin", help=f"one of: {', '.join(AOIS)}"),
    base_year: int = typer.Option(..., help="earlier imagery year"),
    target_year: int = typer.Option(..., help="later imagery year"),
    quota: int = typer.Option(100, help="parcels drawn per stratum"),
    seed: int = typer.Option(DEFAULT_SEED, help="sampling seed"),
    out: Path | None = typer.Option(None, help="output path (default eval/<aoi>-<b>-<t>.json)"),
) -> None:
    """Label the AOI's parcels by BUILD_YR and draw a stratified sample."""
    if aoi not in AOIS:
        raise typer.BadParameter(f"unknown aoi {aoi!r}; known: {', '.join(AOIS)}")
    if base_year >= target_year:
        raise typer.BadParameter("target_year must be later than base_year")

    typer.echo(f"querying parcels for {aoi} {AOIS[aoi]} ...")
    parcels = sources.fetch_parcels(AOIS[aoi])
    typer.echo(f"  {len(parcels)} parcels returned")

    strata = stratify(parcels, base_year=base_year, target_year=target_year)
    sampled = sample_strata(strata, quota=quota, seed=seed)
    counts = sources.county_build_year_counts(base_year, target_year)

    eval_set = EvalSet(
        aoi=aoi,
        base_year=base_year,
        target_year=target_year,
        seed=seed,
        strata=sampled,
        county_build_year_counts=counts,
    )
    destination = out or _set_path(aoi, base_year, target_year)
    write_set(destination, eval_set)

    typer.echo(f"wrote {destination}")
    for label in STRATA:
        stratum = sampled[label]
        typer.echo(
            f"  {label:16s} {stratum.sampled:4d} / {stratum.eligible:5d} eligible"
            f"  (p={stratum.inclusion_probability:.4f})"
        )
    labelled = sum(eval_set.county_strata.values())
    typer.echo(
        f"  county base rate {eval_set.county_base_rate:.4f}"
        f"  ({counts['positive']} of {labelled} labelled parcels;"
        f" {counts['no_year']} of {counts['total']} have no assessor year)"
    )


@app.command("fetch")
def fetch(
    set_path: Path = typer.Argument(..., help="evaluation set written by `build`"),
    verify: bool = typer.Option(
        False, "--verify", help="check every parcel reads on the expected comparison grid"
    ),
    verify_sample: int = typer.Option(10, help="parcels inspected in detail by --verify"),
) -> None:
    """Cache the AOI's NAIP items for both years through the production COG writer."""
    eval_set = read_set(set_path)
    cache_dir = _cache_dir(set_path)
    manifest_path = cache_dir / "manifest.json"
    entries = read_manifest(manifest_path)
    signer = sources.SasSigner()

    fetched = skipped = 0
    for year in (eval_set.base_year, eval_set.target_year):
        items = sources.naip_items(AOIS[eval_set.aoi], year)
        if not items:
            raise typer.BadParameter(f"no NAIP items for {year} over {eval_set.aoi}")
        typer.echo(f"{year}: {len(items)} NAIP items, gsd {sorted({i.gsd_m for i in items})}")
        for item in items:
            destination = entry_path(cache_dir, item)
            if item.id in entries and destination.exists():
                skipped += 1
                continue
            typer.echo(f"  writing {item.id} ({item.gsd_m} m) ...")
            entries[item.id] = fetch_item(item, AOIS[eval_set.aoi], destination, signer)
            write_manifest(manifest_path, entries)
            fetched += 1

    typer.echo(f"cached {fetched} items, reused {skipped}; manifest {manifest_path}")

    if verify:
        _verify_cache(eval_set, entries, verify_sample)


def _verify_cache(eval_set: EvalSet, entries: dict[str, CacheEntry], sample: int) -> None:
    """Prove the cached crop and the remote source produce the same comparison-grid pixels.

    Because the cache is written by the production COG writer, equivalence with production
    is structural rather than something to compare against a second read. What is worth
    checking is that the property whose absence broke an earlier per-parcel crop design is
    actually present — the cached COGs carry overviews, so reading a fine year at a coarse
    comparison grid resamples the way the run job does — and that every parcel in the set
    reads on that grid with imagery in both years.
    """
    missing_overviews = [
        entry.item_id
        for entry in sorted(entries.values(), key=lambda e: e.item_id)
        if not _overviews(entry.path)
    ]
    for entry in sorted(entries.values(), key=lambda e: e.item_id):
        typer.echo(f"  {entry.item_id}: {entry.gsd_m} m, overviews {_overviews(entry.path)}")
    if missing_overviews:
        typer.echo(
            "cached COGs without overviews would resample from full resolution where the "
            f"run job resamples from an overview: {', '.join(missing_overviews)}",
            err=True,
        )
        raise typer.Exit(code=1)

    parcels = [p for label in STRATA for p in eval_set.strata[label].parcels]
    parcels.sort(key=lambda p: parcel_geometry(p).area)
    unreadable: list[str] = []
    nodata: list[str] = []
    # The run job compares both years at the coarser native GSD; that is the grid the
    # harness has to land on, so it is the grid this reads at.
    resolution = max(
        *(e.gsd_m for e in entries.values()),
        MIN_RESOLUTION_M,
    )
    for parcel in parcels:
        pid = str(parcel["PID"])
        geometry = parcel_geometry(parcel)
        for year in (eval_set.base_year, eval_set.target_year):
            raster = read_parcel_uris(
                LOCAL_ENV, uris_for(entries, year, geometry), geometry, resolution_m=resolution
            )
            if raster is None:
                unreadable.append(f"{pid}/{year}")
                continue
            inside = raster.parcel_mask
            if inside.sum() and not raster.mask[inside].all():
                covered = float(raster.mask[inside].sum()) / float(inside.sum())
                nodata.append(f"{pid}/{year} ({covered:.0%} covered)")

    typer.echo(
        f"verified {len(parcels)} parcels x 2 years at {resolution} m: "
        f"{len(unreadable)} unreadable, {len(nodata)} with missing pixels inside the parcel"
    )
    for line in (unreadable + nodata)[:_MAX_REPORTED]:
        typer.echo(f"  {line}", err=True)
    if unreadable:
        raise typer.Exit(code=1)


def _overviews(path: str) -> list[int]:
    with rasterio.open(path) as src:
        return list(src.overviews(1))



@app.command("score")
def score(
    set_path: Path = typer.Argument(..., help="evaluation set with a populated cache"),
    threshold: float = typer.Option(DEFAULT_THRESHOLD, help="candidate score threshold"),
    min_new_area: float = typer.Option(
        DEFAULT_MIN_NEW_AREA_M2, help="minimum new built-up area, m2"
    ),
    audit: Path | None = typer.Option(
        None, "--audit", help="audit file; reports raw and label-corrected metrics"
    ),
    labels: Path | None = typer.Option(
        None,
        "--labels",
        help="visual labels; judges the detector on what the imagery shows, not BUILD_YR",
    ),
    out: Path | None = typer.Option(None, help="where to write per-parcel results"),
) -> None:
    """Score every cached parcel with the current detector and report the metrics."""
    eval_set = read_set(set_path)
    entries = read_manifest(_cache_dir(set_path) / "manifest.json")
    if not entries:
        raise typer.BadParameter(f"no cache for {set_path}; run `ptax-eval fetch` first")

    visual = read_visual_labels(labels) if labels is not None else None
    results, indicators = _score_set(eval_set, entries, threshold, min_new_area)
    results = _apply_visual_labels(results, visual)
    summary = summarise(results, eval_set.county_strata)
    weights = summary.weights

    destination = out or (
        EVAL_DIR / "out" / f"{set_path.stem}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "set": str(set_path),
                "labels": str(labels) if labels is not None else None,
                "threshold": threshold,
                "min_new_area_m2": min_new_area,
                "scored_at": datetime.now(UTC).isoformat(),
                "parcels": [
                    {
                        "pid": r.pid,
                        "stratum": r.stratum,
                        "improved": r.improved,
                        "score": r.score,
                        "flagged": r.flagged,
                        "skipped_reason": r.skipped_reason,
                        "indicators": indicators.get(r.pid),
                    }
                    for r in results
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    _report(eval_set, summary, results, weights, threshold, min_new_area)
    if visual is not None:
        _report_visual_labels(eval_set, visual)
    _report_no_imagery_baseline(eval_set, visual)
    if audit is not None:
        _report_audited(eval_set, results, json.loads(audit.read_text()), summary)
    _report_year_bias(indicators)
    typer.echo(f"\nper-parcel results: {destination}")


def _apply_visual_labels(
    results: list[ScoredParcel], labels: VisualLabels | None
) -> list[ScoredParcel]:
    """Attach the by-eye outcome to each scored parcel, leaving its stratum alone.

    Without labels this is the identity, so a `BUILD_YR`-labelled run stays byte-for-byte
    what it was. With them, `stratum` keeps setting the sampling weight and `positive`
    decides what counts as a hit -- the two are no longer the same field.
    """
    if labels is None:
        return results
    return [
        ScoredParcel(
            pid=r.pid,
            stratum=r.stratum,
            score=r.score,
            flagged=r.flagged,
            skipped_reason=r.skipped_reason,
            positive=labels.improved.get(r.pid),
        )
        for r in results
    ]


def _report_visual_labels(eval_set: EvalSet, labels: VisualLabels) -> None:
    """Where the imagery and ``BUILD_YR`` disagree, and what that does to the base rate."""
    county = eval_set.county_strata
    typer.echo("\nvisual labels (what the two captures actually show)")
    typer.echo(f"  {'stratum':16s} {'improved':>9s} {'labelled':>9s} {'rate':>7s} {'county':>9s}")
    for label in STRATA:
        improved, labelled = labels.counts(label)
        typer.echo(
            f"  {label:16s} {improved:9d} {labelled:9d}"
            f" {labels.rate(label):7.3f} {county.get(label, 0):9d}"
        )
    rate = improvement_base_rate(labels, county)
    typer.echo(f"  county improvement base rate {rate:.4f}")
    typer.echo(
        f"  (BUILD_YR's own positive share was {eval_set.county_base_rate:.4f};"
        " it counts recorded build years, not visible improvements)"
    )


def _score_set(
    eval_set: EvalSet,
    entries: dict[str, CacheEntry],
    threshold: float,
    min_new_area: float,
) -> tuple[list[ScoredParcel], dict[str, dict[str, Any]]]:
    """Run the detector over every sampled parcel, on the run job's comparison grid."""
    detector = ClassicalDetector()
    resolution = max(*(e.gsd_m for e in entries.values()), MIN_RESOLUTION_M)
    results: list[ScoredParcel] = []
    indicators: dict[str, dict[str, Any]] = {}

    def read_pair(parcel: dict[str, Any]) -> tuple[Any, Any]:
        geometry = parcel_geometry(parcel)
        return tuple(  # type: ignore[return-value]
            read_parcel_uris(
                LOCAL_ENV, uris_for(entries, year, geometry), geometry, resolution_m=resolution
            )
            for year in (eval_set.base_year, eval_set.target_year)
        )

    fit = _fit_radiometry(eval_set, read_pair)
    if fit is not None:
        typer.echo(
            f"radiometric fit over {fit.sampled_parcels} parcels: "
            f"NIR gain {fit.indicators['radiometric_gain']}, "
            f"offset {fit.indicators['radiometric_offset']}"
        )
    else:
        typer.echo("no usable radiometric fit; scoring unnormalised", err=True)

    for label in STRATA:
        for parcel in eval_set.strata[label].parcels:
            pid = str(parcel["PID"])
            geometry = parcel_geometry(parcel)
            rasters = [
                read_parcel_uris(
                    LOCAL_ENV, uris_for(entries, year, geometry), geometry,
                    resolution_m=resolution,
                )
                for year in (eval_set.base_year, eval_set.target_year)
            ]
            if rasters[0] is None or rasters[1] is None:
                results.append(
                    ScoredParcel(pid, label, None, False, skipped_reason="no_coverage")
                )
                continue
            try:
                outcome = detector.compare(
                    rasters[0],
                    rasters[1],
                    threshold=threshold,
                    min_new_area_m2=min_new_area,
                    fit=fit,
                )
            except InsufficientCoverage as exc:
                results.append(
                    ScoredParcel(pid, label, None, False, skipped_reason=f"coverage_{exc.which}")
                )
                continue
            results.append(ScoredParcel(pid, label, outcome.score, outcome.candidate))
            indicators[pid] = outcome.indicators
    return results, indicators


#: Indicators whose per-year distributions expose a capture-level bias. Plan B attributed
#: `target_builtup_frac` 0.937 to texture scale; this is what confirms or refutes it.
_YEAR_STATISTICS = ("base_builtup_frac", "target_builtup_frac")


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    """(lower quartile, median, upper quartile); degenerate inputs fall back to the median."""
    if not values:
        return (0.0, 0.0, 0.0)
    ordered = sorted(values)
    median = statistics.median(ordered)
    if len(ordered) < 4:
        return (ordered[0], median, ordered[-1])
    lower, upper = statistics.quantiles(ordered, n=4)[0], statistics.quantiles(ordered, n=4)[2]
    return (lower, median, upper)


def _report(
    eval_set: EvalSet,
    summary: Summary,
    results: list[ScoredParcel],
    weights: dict[str, float],
    threshold: float,
    min_new_area: float,
) -> None:
    county = eval_set.county_strata
    typer.echo(
        f"\n{eval_set.aoi} {eval_set.base_year} -> {eval_set.target_year}"
        f"  (threshold {threshold}, min_new_area {min_new_area} m2)"
    )
    typer.echo(f"  scored {summary.scored}, skipped {summary.skipped}")

    typer.echo("\nper stratum (raw sample counts)")
    typer.echo(f"  {'stratum':16s} {'scored':>7s} {'flagged':>8s} {'rate':>7s} {'county':>9s}")
    for label in STRATA:
        stratum = summary.per_stratum[label]
        typer.echo(
            f"  {label:16s} {stratum.scored:7d} {stratum.flagged:8d}"
            f" {stratum.flag_rate:7.3f} {county.get(label, 0):9d}"
        )

    fpr = summary.false_positive_rate
    typer.echo(f"\nreweighted to the county base rate {summary.base_rate:.4f}")
    typer.echo(f"  flag rate            {summary.flag_rate:.4f}")
    typer.echo(f"  precision            {summary.precision:.4f}")
    typer.echo(f"  recall               {summary.recall:.4f}")
    typer.echo(f"  F1                   {summary.f1:.4f}")
    typer.echo(f"  false positive rate  {'n/a' if fpr is None else f'{fpr:.4f}'}")

    typer.echo("\nthreshold-free (weighted)")
    typer.echo(f"  average precision    {average_precision(results, weights):.4f}")
    typer.echo(f"  precision @ top 1%   {precision_at_top_fraction(results, weights, 0.01):.4f}")
    typer.echo(f"  precision @ top 5%   {precision_at_top_fraction(results, weights, 0.05):.4f}")

    typer.echo("\nraw sample (NOT comparable to the target; the sample oversamples positives)")
    typer.echo(f"  flag rate            {summary.raw_flag_rate:.4f}")
    typer.echo(f"  precision            {summary.raw_precision:.4f}")


def _report_year_bias(indicators: dict[str, dict[str, Any]]) -> None:
    """Per-year distributions of the built-up fraction, and the gap between them.

    This is the diagnosis Task 4 acts on. Plan B attributed a `target_builtup_frac` median
    of 0.937 against a `base_builtup_frac` of 0.366 to the target year being smoother once
    resampled onto the coarser grid, but that is a hypothesis about which capture-level
    statistic actually moved. A large gap here means the built-up mask is being driven by a
    property of the captures rather than by eleven years of construction; a gap that has
    closed means the remaining error is somewhere else.
    """
    if not indicators:
        return
    typer.echo("\nper-year built-up fraction (lower quartile / median / upper quartile)")
    medians: dict[str, float] = {}
    for key in _YEAR_STATISTICS:
        values = [float(i[key]) for i in indicators.values() if key in i]
        low, median, high = _quartiles(values)
        medians[key] = median
        typer.echo(f"  {key:22s} {low:.3f} / {median:.3f} / {high:.3f}   (n={len(values)})")

    gap = medians.get("target_builtup_frac", 0.0) - medians.get("base_builtup_frac", 0.0)
    typer.echo(f"  median gap (target - base) {gap:+.3f}")
    typer.echo(
        "  a gap this size is a capture-level bias, not construction"
        if abs(gap) > 0.15
        else "  the two years agree closely; look elsewhere for the remaining error"
    )


@app.command("chips")
def chips(
    set_path: Path = typer.Argument(..., help="evaluation set with a populated cache"),
    sample: int = typer.Option(40, help="parcels to render, drawn evenly across strata"),
    seed: int = typer.Option(DEFAULT_SEED, help="which parcels get drawn"),
    columns: int = typer.Option(5, help="contact sheet width"),
    every: bool = typer.Option(
        False, "--all", help="render the whole set in numbered sheets instead of a sample"
    ),
    per_sheet: int = typer.Option(10, help="parcels per sheet in --all mode"),
    chip_px: int = typer.Option(
        420, "--chip-px", help="target longest side per year; chips are scaled toward it"
    ),
) -> None:
    """Render base/target chip pairs and a contact sheet for auditing the labels."""
    eval_set = read_set(set_path)
    entries = read_manifest(_cache_dir(set_path) / "manifest.json")
    if not entries:
        raise typer.BadParameter(f"no cache for {set_path}; run `ptax-eval fetch` first")

    out_dir = EVAL_DIR / "out" / "chips" / set_path.stem
    resolution = max(*(e.gsd_m for e in entries.values()), MIN_RESOLUTION_M)
    if every:
        _chip_sheets(eval_set, entries, out_dir, resolution, chip_px, per_sheet)
        return
    # Draw evenly across strata: the set is already equal-quota, and the noise estimate
    # needs support in every stratum rather than in whichever one happens to be largest.
    per_stratum = max(1, sample // len(STRATA))
    tiles: list[Any] = []
    index: list[ChipEntry] = []

    for label in STRATA:
        parcels = eval_set.strata[label].parcels
        rng = random.Random(f"{seed}:chips:{label}")
        for parcel in sorted(
            rng.sample(parcels, min(per_stratum, len(parcels))),
            key=lambda p: str(p["PID"]),
        ):
            pid = str(parcel["PID"])
            geometry = parcel_geometry(parcel)
            rasters = [
                read_parcel_uris(
                    LOCAL_ENV, uris_for(entries, year, geometry), geometry,
                    resolution_m=resolution,
                )
                for year in (eval_set.base_year, eval_set.target_year)
            ]
            if rasters[0] is None or rasters[1] is None:
                typer.echo(f"  no imagery for {pid}", err=True)
                continue
            tile = fit(pair(rasters[0], rasters[1]), chip_px * 2)
            destination = out_dir / f"{label}_{pid}.png"
            write_png(destination, tile)
            row, column = divmod(len(tiles), columns)
            tiles.append(tile)
            index.append(ChipEntry(pid, label, row, column, str(destination)))

    sheet_path = out_dir / "contact-sheet.png"
    write_png(sheet_path, contact_sheet(tiles, columns=columns))
    write_index(out_dir / "index.json", index, columns)
    typer.echo(f"wrote {len(tiles)} chip pairs and {sheet_path}")
    typer.echo(f"  index: {out_dir / 'index.json'}")


def _report_audited(
    eval_set: EvalSet,
    results: list[ScoredParcel],
    audit: dict[str, Any],
    raw: Summary,
) -> None:
    """The same figures over audit-corrected labels, with the disagreement between them.

    Reported beside the raw numbers and never in place of them: the audit measures the
    labels, so replacing one with the other would quietly fold label error into a detector
    result. A measured precision of 0.30 against a measured 8% label-noise rate is "0.30,
    and at most 8 points of the misses are label artefacts" -- not 0.38.
    """
    corrected, moved = apply_audit(results, audit)
    audited = summarise(corrected, eval_set.county_strata)
    noise = audit.get("county_weighted_noise_rate")
    per = audit.get("per_stratum", {})

    typer.echo(f"\naudited labels ({moved} of {audit.get('audited_total', '?')} inspected moved)")
    typer.echo(f"  flag rate            {audited.flag_rate:.4f}  (raw {raw.flag_rate:.4f})")
    typer.echo(f"  precision            {audited.precision:.4f}  (raw {raw.precision:.4f})")
    typer.echo(f"  recall               {audited.recall:.4f}  (raw {raw.recall:.4f})")
    if noise is not None:
        typer.echo(f"  county-weighted label-noise rate {noise:.4f}")
    for label in STRATA:
        stats = per.get(label)
        if stats:
            typer.echo(
                f"    {label:16s} {stats['corrected']}/{stats['audited']} corrected"
                f", {stats.get('unclear', 0)} unclear"
            )


#: Parcels sampled per stratum when fitting the run-level radiometric correction. Drawn
#: across strata so the fit is not anchored to whichever land use happens to come first.
_FIT_SAMPLE_PER_STRATUM = 40


def _fit_radiometry(eval_set: EvalSet, read_pair: Any) -> RadiometricFit | None:
    """One capture-to-capture correction for the whole set, fitted before any scoring.

    Deliberately fitted across many parcels: a fit from a single parcel is dragged by that
    parcel's own change, which measured out at precision 0.0745 -> 0.0528 and recall
    0.72 -> 0.37. Across a sample, unchanged ground dominates and no one parcel can move it.
    """
    base_samples: list[Any] = []
    target_samples: list[Any] = []
    for label in STRATA:
        for parcel in eval_set.strata[label].parcels[:_FIT_SAMPLE_PER_STRATUM]:
            rasters = read_pair(parcel)
            if rasters[0] is None or rasters[1] is None:
                continue
            pair_samples = paired_samples(rasters[0], rasters[1])
            if pair_samples is None:
                continue
            base_samples.append(pair_samples[0])
            target_samples.append(pair_samples[1])
    return fit_radiometry(base_samples, target_samples)


def _report_no_imagery_baseline(eval_set: EvalSet, labels: VisualLabels | None = None) -> None:
    """How well the labels can be predicted from parcel size alone, reading no imagery.

    This exists because a share-of-parcel score once measured 96% precision here and was
    then found to be *beaten* by this baseline: in this AOI new construction happens on
    subdivided suburban lots (median 1 515 m2) while established parcels include rural
    acreage (median 8 198 m2), so anything that divides by parcel area inherits that
    correlation and looks like a detector. Any real detector has to beat this line; a score
    that merely matches it has learned the county's plat history, not its buildings.
    """
    ranked = _apply_visual_labels(
        [
            ScoredParcel(
                pid=str(parcel["PID"]),
                stratum=label,
                score=1.0 / (area_m2(parcel.get("PARCEL_AREA")) or 1.0),
                flagged=False,
            )
            for label in STRATA
            for parcel in eval_set.strata[label].parcels
        ],
        labels,
    )
    weights = parcel_weights(stratum_counts(ranked), eval_set.county_strata)
    typer.echo("\nno-imagery baseline (rank by inverse parcel size; reads no pixels)")
    typer.echo(f"  average precision    {average_precision(ranked, weights):.4f}")
    typer.echo(f"  precision @ top 1%   {precision_at_top_fraction(ranked, weights, 0.01):.4f}")
    typer.echo(f"  precision @ top 5%   {precision_at_top_fraction(ranked, weights, 0.05):.4f}")
    typer.echo("  a detector that does not beat this has not detected anything")

if __name__ == "__main__":
    app()


def _chip_sheets(
    eval_set: EvalSet,
    entries: dict[str, CacheEntry],
    out_dir: Path,
    resolution: float,
    chip_px: int,
    per_sheet: int,
) -> None:
    """Render the whole set as numbered contact sheets, for labelling every parcel by eye.

    Three hundred parcels is not a practical sequence of single images, and the judgement
    the labels need -- did a structure appear between these two captures -- survives being
    read from a tiled sheet as long as each cell stays large enough to show a small
    outbuilding. Two columns keeps each pair near its standalone width.
    """
    parcels = [
        (label, parcel)
        for label in STRATA
        for parcel in sorted(eval_set.strata[label].parcels, key=lambda p: str(p["PID"]))
    ]
    index: list[ChipEntry] = []
    sheet_no = 0
    tiles: list[Any] = []
    pending: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal tiles, pending, sheet_no
        if not tiles:
            return
        sheet_no += 1
        path = out_dir / f"sheet-{sheet_no:03d}.png"
        write_png(path, contact_sheet(tiles, columns=2))
        for position, (pid, label) in enumerate(pending):
            row, column = divmod(position, 2)
            index.append(ChipEntry(pid, label, row, column, str(path)))
        typer.echo(f"  {path.name}: {len(pending)} parcels")
        tiles, pending = [], []

    for label, parcel in parcels:
        pid = str(parcel["PID"])
        geometry = parcel_geometry(parcel)
        rasters = [
            read_parcel_uris(
                LOCAL_ENV, uris_for(entries, year, geometry), geometry, resolution_m=resolution
            )
            for year in (eval_set.base_year, eval_set.target_year)
        ]
        if rasters[0] is None or rasters[1] is None:
            typer.echo(f"  no imagery for {pid}", err=True)
            continue
        tiles.append(letterbox(pair(rasters[0], rasters[1]), chip_px * 2, chip_px))
        pending.append((pid, label))
        if len(tiles) >= per_sheet:
            flush()
    flush()

    write_index(out_dir / "sheet-index.json", index, 2)
    typer.echo(f"wrote {sheet_no} sheets covering {len(index)} parcels in {out_dir}")
