# Detector evaluation on real imagery

Plan D (`docs/plans/2026-09-22-detector-accuracy.md`) measures every detector change
against real NAIP imagery over real county parcels, instead of against the synthetic
fixtures the detector was originally written to satisfy. This directory holds the
labelled sets and the measured results; the imagery cache and scoring output are
gitignored.

```
eval/
  nw-hennepin-2010-2021.json     committed  the cross-resolution evaluation set
  nw-hennepin-2013-2017.json     committed  the same-resolution control set
  visual-labels-nw-hennepin-2010-2021.json
                                 committed  all 300 parcels labelled by eye; the truth
  audit-nw-hennepin-2010-2021.json
                                 committed  the earlier 39-parcel BUILD_YR audit
  cache/<set>/                   ignored    NAIP items, written by the production COG writer
  out/                           ignored    per-run scores and chips
```

## Rebuilding from scratch

Needs network but **no AWS credentials**: parcels come from Hennepin County's open
ArcGIS service and imagery from Microsoft's Planetary Computer, which signs NAIP blobs
with a free short-lived token. Production is unaffected — it still reads NAIP from the
requester-pays `naip-analytic` bucket through `ptax.imagery.naip.NaipStacSource`.

```bash
cd backend
uv run ptax-eval build --aoi nw-hennepin --base-year 2010 --target-year 2021 --quota 100 --seed 20260922
uv run ptax-eval build --aoi nw-hennepin --base-year 2013 --target-year 2017 --quota 100 --seed 20260922
make eval-fetch                       # or: uv run ptax-eval fetch <set> --verify
make eval-score
```

`build` is deterministic: the same AOI, years, quota and seed produce a byte-identical
file. The imagery cache is roughly 235 MB for the primary set and 115 MB for the control.

## What the labels mean

> **`BUILD_YR` is the *sampling* label, not the truth.** Every parcel has since been read
> by eye and the two disagree on 31 of 300. The outcome the detector is judged against now
> comes from `visual-labels-nw-hennepin-2010-2021.json`; see
> [Tasks 10–11](#tasks-1011--visual-labels-replace-build_yr-2026-09-22). The table below
> still describes how parcels were *drawn*, which is what sets their weight.

Labels come from the assessor's `BUILD_YR` — the year a parcel's **principal structure**
was built, attached to the parcel's **current** geometry.

| Stratum | Rule | What it is |
|---|---|---|
| `positive` | `base_year < BUILD_YR <= target_year` | the structure appeared between the two captures |
| `negative_old` | `0 < BUILD_YR <= base_year` | already standing in the base capture |
| `negative_future` | `BUILD_YR > target_year` | platted, often graded, not yet built — the hardest negatives |
| `excluded` | no year | vacant land or a missing record; leaves the population entirely |

Parcels outside 400–40 000 m² are dropped before labelling: below that are condominium
slivers and common-area remnants, above it are farm and institutional parcels whose
imagery is mostly field.

**The labels are noisy in known directions, and the harness does not hide it.** A 1955
house that gained a detached garage, a large addition or a barn keeps its old year and is
labelled negative, so the detector is right to flag it. A teardown-rebuild is labelled
positive but shows a building in both years, so the detector is right not to flag much new
built-up area. Task 3 measures that rate on an audited subset, and every metric after it
is reported beside the measured noise rate.

## Why every rate is reweighted

The evaluation AOI is a growth fringe: 135 of 792 eligible parcels gained a principal
structure between 2010 and 2021, about 17%. County-wide the figure is **5.99%** (25 167 of
420 207 *labelled* parcels; a further 27 851 carry no assessor year and are excluded from
both the sample and this denominator). The sample is drawn with a fixed quota per stratum
on top of that, so a raw rate over the sample is roughly six times the truth.

Every reported flag rate and precision is therefore reweighted to the county base rate,
using the `sampled / eligible` ratio each set file records per stratum:

```
flag_rate = pi * TPR + (1 - pi) * FPR
precision = pi * TPR / flag_rate
```

The 5.99% above is the share of parcels whose *recorded build year* falls in the window.
The share showing a *visible improvement* — the thing the application looks for — was
measured at **17.95%** once all 300 parcels were labelled by eye; that is the base rate
every current figure is compared against. See
[Tasks 10–11](#tasks-1011--visual-labels-replace-build_yr-2026-09-22).

The plan's target is ≤2% flagged at ≥60% precision — a short, trustworthy queue rather
than a complete inventory, not a defect to tune away. **It is not reached** against either
base rate; see the measured ceiling under Task 7 and its re-measurement under Tasks 10–11.

## Why the cache stores whole NAIP items

The cache holds each NAIP item clipped to the AOI and written through
`ptax.imagery.cog.to_cog`, the same writer the ingest job uses — so the harness reads the
artifact production would have stored.

An earlier design cached a small crop per parcel and failed its own equivalence check: 10
of 30 reads disagreed with the source, about 90% of samples each, **all on the 2021 target
year**. A real quarter-quad COG carries overviews (`[2, 4, 8, 16, 32]`); a 96×113 crop
carries none. Reading a 0.6 m year at the 1.0 m comparison grid resamples from the `/2`
overview when one exists and from full resolution when it does not. Production's
`cog_translate` profile builds overviews, so the crops were feeding the detector pixels
production never sees — on exactly the year whose built-up fraction is the defect under
investigation.

Items are clipped to the AOI plus a 300 m buffer (`AOI_CLIP_BUFFER_M`). Parcels are
selected by *intersecting* the AOI box, so an edge parcel extends past it; clipping to the
box exactly left 12 parcel-years unreadable and 50 with missing pixels inside the parcel.
With the buffer both sets verify clean.

`ptax-eval fetch --verify` checks what matters now: that every cached COG carries
overviews, and that all 300 parcels (258 for the control) read on the comparison grid in
both years with no missing pixels inside the parcel.

## Results

| Date | Set | Command | Result |
|---|---|---|---|
| 2026-09-22 | `nw-hennepin-2010-2021` | `ptax-eval build` | 100/135 `positive`, 100/141 `negative_old`, 100/238 `negative_future`; county base rate 0.0599 |
| 2026-09-22 | `nw-hennepin-2013-2017` | `ptax-eval build` | 58/58 `positive`, 100/190 `negative_old`, 100/266 `negative_future`; county base rate 0.0236 |
| 2026-09-22 | `nw-hennepin-2010-2021` | `ptax-eval fetch --verify` | 4 items cached (2010 at 1.0 m, 2021 at 0.6 m), all with overviews; 300 parcels x 2 years read clean |
| 2026-09-22 | `nw-hennepin-2013-2017` | `ptax-eval fetch --verify` | 4 items cached, all 1.0 m, all with overviews; 258 parcels x 2 years read clean |
| 2026-09-22 | `nw-hennepin-2010-2021` | `ptax-eval score` | **baseline**: flag rate 0.9623, precision 0.0442, recall 0.7100, AP 0.0334, p@top1% 0.0000 |
| 2026-09-22 | `nw-hennepin-2013-2017` | `ptax-eval score` | **baseline**: flag rate 0.9334, precision 0.0240, recall 0.9483, AP 0.0189 |

The baseline and what it refutes are written up in
[`baseline-nw-hennepin-2010-2021.md`](baseline-nw-hennepin-2010-2021.md). Two findings
drive the rest of the plan:

- **Precision is below the base rate** (0.0442 against 0.0599), so the detector currently
  ranks worse than random, and `precision @ top 1%` is 0.
- **Plan B's resolution diagnosis does not hold.** The cross-resolution pair's year gap is
  +0.015; the *same-resolution* control's is +0.160. A resolution mismatch cannot explain a
  bias that is worse where there is no mismatch. The real defect is that `_classify` reads
  bare ground as built-up — vacant parcels measure 0.949 → 1.000 built-up, while real
  construction *lowers* the built-up fraction (0.677 → 0.569) because a house replaces
  graded soil with lawn.

## Measured label noise

39 parcels (13 per stratum) were audited by reading rendered base/target chip pairs. The
plan's Task 3 says 40; `chips` draws evenly across the three strata (`sample // 3`), so a
request for 40 yields the nearest multiple of three. The shortfall is one parcel and is
recorded in the audit file's own `method` field rather than rounded over.
verdicts and reasons are in [`audit-nw-hennepin-2010-2021.json`](audit-nw-hennepin-2010-2021.json).

| stratum | audited | corrected | unclear | disagreement |
|---|---|---|---|---|
| `positive` | 13 | 2 | 0 | 0.154 |
| `negative_old` | 13 | 1 | 1 | 0.077 |
| `negative_future` | 13 | 0 | 0 | 0.000 |

**County-weighted label-noise rate: 0.080.** `negative_old` dominates because it is 92% of
the county, so its 7.7% disagreement carries nearly all the weight.

Two distinct noise modes, both predicted by the label rule's known limits:

- **Additions and outbuildings** (`2812023430002`): a parcel built in the 1900s gains a
  shed and a gravel pad by 2021. `BUILD_YR` tracks only the principal structure, so the
  label says negative while the detector is right to flag it.
- **The capture date, not the calendar year** (`2712023120057`, `2712023120058`): both
  carry `BUILD_YR = 2021` and show open field in both captures. The 2021 NAIP capture is
  **2021-06-18**, so a structure recorded in 2021 can post-date the image. **15 of the 100
  sampled positives sit in that boundary year**, which bounds this mode at up to 15% of the
  positive stratum.

One parcel (`2212023310037`) shows a *demolition* — a large structure removed between
captures. Correctly labelled negative here, since this plan detects gains only.

**Applying the corrections moves precision from 0.0442 to 0.0446 and recall from 0.7100 to
0.7172.** That is the useful conclusion: an 8% label-noise rate cannot account for a
precision that sits *below* the base rate. The detector's failure is real, not a labelling
artefact.

## Task 6 — re-derived classification (2026-09-22)

The v1 cue called a pixel built-up when it was non-vegetated, bright and locally smooth,
which describes a ploughed field as well as a roof. It is replaced by a **local-contrast**
cue: a structure differs from the ground around it at building scale, an open field does
not differ from itself. Water and deep shadow are excluded by NIR, and reported new
built-up area must survive an opening at `MIN_STRUCTURE_M`, so a roof counts and a scatter
of disagreeing pixels does not.

**The cue now points the right way** — the criterion Task 6 was given:

| stratum | base → target built-up, before | after |
|---|---|---|
| `positive` (real construction) | 0.677 → 0.569 (**−0.108**) | 0.019 → 0.205 (**+0.169**) |
| `negative_old` | 0.212 → 0.381 | 0.121 → 0.169 |
| `negative_future` (vacant) | **0.949 → 1.000** | **0.000 → 0.003** |

Vacant land no longer reads as built-up, and building a house now raises the fraction
instead of lowering it.

### Measured, both sets

| | 2010 → 2021 before | after | control 2013 → 2017 before | after |
|---|---|---|---|---|
| flag rate | 0.9623 | **0.5792** | 0.9334 | **0.2140** |
| precision | 0.0442 | **0.0745** | 0.0240 | **0.0324** |
| recall | 0.7100 | 0.7200 | 0.9483 | 0.2931 |
| F1 | 0.0832 | 0.1350 | 0.0468 | 0.0583 |
| average precision | 0.0334 | 0.0398 | 0.0189 | 0.0175 |
| precision @ top 5% | 0.0000 | 0.0253 | 0.0087 | 0.0170 |

With audited labels the primary set reads flag rate 0.5763, precision 0.0766, recall
0.7374. **Precision now exceeds the base rate on both sets** (0.0745 against 0.0599, and
0.0324 against 0.0236), where before it sat below on the primary — the detector has gone
from worse-than-random to better-than-random.

**Stated plainly: two things did not improve.** The control set's average precision fell
slightly, 0.0189 → 0.0175, and its recall fell hard, 0.9483 → 0.2931 — the 2013–2017
window's builds are smaller and the `MIN_STRUCTURE_M` opening drops them. That is a real
cost of a precision-first operating point on a shorter window, not a rounding artefact.

### Parameter sweep

Picked by weighted average precision, as the plan requires. Per-parcel scoring cost was
0.0009–0.0010 s at every point (the added context window is an integral-image mean, so its
cost does not grow with window size). Task 6's DoD asked for this to be within 2x of
Task 5's cost, but the execution order was reversed by the Task 2 deviation, so Task 5 did
not yet exist when this was measured; the absolute figure above should be read against the
Task 2 baseline instead, and at ~1 ms per parcel it is not a constraint on a county run.

| `CONTRAST_T` | `MIN_STRUCTURE_M` | flag rate | precision | recall | AP |
|---|---|---|---|---|---|
| 12 | 3 | 0.9799 | 0.0611 | 1.0000 | 0.0380 |
| 12 | 5 | 0.8550 | 0.0665 | 0.9500 | 0.0399 |
| 12 | 8 | 0.6388 | 0.0741 | 0.7900 | 0.0400 |
| 18 | 3 | 0.9635 | 0.0578 | 0.9300 | 0.0373 |
| 18 | 5 | 0.8128 | 0.0663 | 0.9000 | 0.0394 |
| **18** | **8** | **0.5792** | **0.0745** | **0.7200** | **0.0398** |
| 24 | 3 | 0.9488 | 0.0543 | 0.8600 | 0.0373 |
| 24 | 5 | 0.7800 | 0.0630 | 0.8200 | 0.0391 |
| 24 | 8 | 0.5179 | 0.0729 | 0.6300 | 0.0388 |
| 30 | 3 | 0.8873 | 0.0527 | 0.7800 | 0.0369 |
| 30 | 5 | 0.7112 | 0.0648 | 0.7700 | 0.0386 |
| 30 | 8 | 0.4350 | 0.0661 | 0.4800 | 0.0373 |

**Average precision is flat across the whole grid — 0.0369 to 0.0400.** No setting ranks
meaningfully better than any other, so picking the AP maximum (12 / 8, AP 0.0400) over the
runner-up (18 / 8, AP 0.0398) would be fitting to noise. `CONTRAST_T = 18`,
`MIN_STRUCTURE_M = 8` was chosen instead as the best *precision* on the AP plateau, which
is the axis the target operating point actually cares about.

### The target is not met, and this is how far off it is

Target: ≤ 2% flagged at ≥ 60% precision. Measured: **57.9% flagged at 7.5% precision.**
Precision is 8× short and the flag rate 29× too high.

Two measured reasons remain, and both belong to later tasks rather than to the cue:

1. **A year-level bias is now the largest remaining artefact.** Across all strata the 2021
   built-up fraction runs about 4× the 2010 figure (median 0.035 → 0.140, a +0.105 gap)
   even on parcels that did not change. The captures are September 2010 and June 2021, and
   the chips show it plainly: 2010 renders dull and blue-cast where 2021 is vividly green.
   That is Task 5's radiometric normalisation.
2. **The score still saturates.** 24% of parcels score ≥ 0.99 and the median is 0.736, so
   `threshold` cannot separate them and `precision @ top 1%` is still 0. That is Task 7.

Whether the classical cue can reach the target once those are fixed is still open. The AP
plateau above is the first real evidence that it may not — no parameter choice moved the
ranking — and if Tasks 5 and 7 do not move it either, that plateau is the measured ceiling
this plan was told to record as the trigger for a learned detector.

## Task 5 — radiometric normalisation (2026-09-22)

### What actually differs between captures

Measured on `negative_old` parcels only — established ground that did not change, so any
year difference is the capture rather than the ground:

| pair | vegetated fraction | contrast p90 | brightness | NIR |
|---|---|---|---|---|
| 2010 → 2021 | **0.586 → 0.441** | 47.1 → 49.3 | 114.0 → 112.8 | **179 → 160** |
| 2013 → 2017 | **0.705 → 0.443** | 46.7 → 42.6 | 112.5 → 96.3 | **185 → 130** |

Brightness and local contrast barely move; **NIR falls**. An absolute `VEG_T_NDVI` then
under-reads vegetation in the later year, which both enlarges the built-up candidate mask
and manufactures vegetation loss. On the synthetic fixture the mechanism is visible in
isolation: an NIR-only recapture of unchanged ground drives the score from 0.044 to 0.785
with *zero* new built-up area — the score was coming entirely from phantom vegetation loss.

### Scope is the finding

The correction is fitted **once per year pair across many parcels**, not per parcel. The
per-parcel version the plan originally specified removes the bias but takes the signal with
it, because a new roof is a large share of one parcel's pixels and drags that parcel's own
correction toward erasing itself:

| fit scope | flag rate | precision | recall | AP |
|---|---|---|---|---|
| none (Task 6) | 0.5792 | 0.0745 | 0.7200 | 0.0398 |
| per parcel, 25/50/75 | 0.4220 | 0.0369 | 0.2600 | 0.0377 |
| per parcel, 5/25/50 | 0.4194 | 0.0528 | 0.3700 | 0.0378 |
| **per year pair, 5/25/50** | 0.5375 | 0.0713 | 0.6400 | **0.0413** |

### Measured, both sets

| | 2010 → 2021 before | after | control 2013 → 2017 before | after |
|---|---|---|---|---|
| flag rate | 0.5792 | 0.5375 | 0.2140 | **0.1319** |
| precision | 0.0745 | 0.0713 | 0.0324 | **0.0772** |
| recall | 0.7200 | 0.6400 | 0.2931 | **0.4310** |
| average precision | 0.0398 | **0.0413** | 0.0175 | **0.0833** |
| precision @ top 5% | 0.0253 | **0.0375** | 0.0170 | **0.1472** |
| year gap | +0.105 | +0.093 | +0.070 | **+0.030** |

**The control pair improves out of all proportion**: average precision 0.0175 → 0.0833, a
4.8× gain, and precision 0.0324 → 0.0772, now more than 3× its 0.0236 base rate. Both sets
reach their best average precision and best top-of-ranking precision so far.

**Two things did not move as hoped.** On the primary pair, precision and recall are
slightly *below* Task 6's (0.0713 against 0.0745, 0.64 against 0.72) — the gain there is in
ranking, not at the default threshold, which Task 7 has yet to set. And the primary pair's
year gap barely closed, +0.105 → +0.093, while the control's halved, +0.070 → +0.030. That
residual is the one thing radiometry cannot explain: the primary pair is the
cross-resolution one, so what is left is a grid effect — which is exactly what **Task 4**
was written to remove, and it now has a measured target where at the Task 2 baseline it had
none.

Production runs the same fit: `detection/run.py` samples `FIT_SAMPLE_PARCELS` evenly across
the run's geohash ordering before scoring starts, so a county run is normalised the way the
harness is.

## Task 7 — the score, and the measured ceiling (2026-09-22)

> **The figures in this section are measured against `BUILD_YR`.** They are kept as the
> record of what was measured at the time. Re-measured against the visual labels the
> detector looks considerably better in absolute terms and the ceiling still stands; see
> [Tasks 10–11](#tasks-1011--visual-labels-replace-build_yr-2026-09-22).

### A score that measures structures

The score is the area of the **largest contiguous, building-shaped** component of new
built-up area: `structure_m2 / (structure_m2 + SCORE_HALF_STRUCTURE_M2)`, so a 400 m2
structure scores 0.5 and the curve keeps rising without ever reaching 1.0. A parcel is a
candidate when that structure clears `min_new_area_m2`, whose meaning changed from "total
new built-up area" to "smallest structure worth reporting" and whose default is now
**37.2 m2 (400 ft2)**.

A component counts as a structure only if it fills at least 75% of its bounding box.
Measured on the evaluation set, that separates the two populations cleanly: a real new
structure fills **0.963** of its box (0.920 at the lower quartile) while a patch of changed
field or canopy fills **0.669** (0.328 at the lower quartile).

### Why the earlier share-based score was wrong

An intermediate version scored the change in built-up *share* of the parcel and measured
1.43% flagged at 96.1% precision — apparently meeting the target. It does not survive a
control that should have been there from the start:

| | ranking by **inverse parcel size**, reading no pixels | share-based score |
|---|---|---|
| average precision | **0.4793** | 0.4665 |
| precision @ top 1% | **1.0000** | 0.9629 |

**The share-based score was beaten by a ranker that never opens an image.** In this AOI new
construction sits on subdivided suburban lots (median parcel 1 515 m2) while established
parcels include rural acreage (median 8 198 m2), so any score that divides by parcel area
inherits that correlation and looks like a detector. `ptax-eval score` now prints this
baseline on every run, under the line *"a detector that does not beat this has not detected
anything"*, so the mistake cannot be made quietly again.

### What the detector actually achieves

| | structure-based (honest) | share-based (confounded) |
|---|---|---|
| flag rate | 0.3016 | 0.0143 |
| precision | **0.0794** | 0.0961 |
| recall | 0.4000 | 0.2300 |
| average precision | **0.0592** | 0.4665 |
| no-imagery baseline AP | 0.4793 | 0.4793 |

Precision 0.079 against a base rate of 0.060: **above chance, and far below both the target
and the no-imagery baseline.**

### The ceiling

Target: ≤ 2% flagged at ≥ 60% precision. Sweeping every combination of threshold
(0.0–0.6) and minimum structure size (37–800 m2):

> **No pair produces a queue of 6% or smaller with precision above the base rate.**

At a 400 m2 minimum the flagged set contains **no positives at all** — every large
structure the detector finds sits on a parcel that did not gain a building. The only
regime above chance flags 30–48% of the county, which is not a review queue.

### Threshold curve (single axis, 0.1-0.9)

At the shipped minimum structure size of 37.2 m²:

| threshold | flag rate | precision | recall |
|---|---|---|---|
| 0.1 | 0.4792 | 0.0737 | 0.5900 |
| 0.2 | 0.4570 | 0.0695 | 0.5300 |
| **0.3** | **0.3016** | **0.0794** | **0.4000** |
| 0.4 | 0.1899 | 0.0252 | 0.0800 |
| 0.5 | 0.0743 | 0.0000 | 0.0000 |
| 0.6 | 0.0186 | 0.0000 | 0.0000 |
| 0.7 | 0.0184 | 0.0000 | 0.0000 |
| 0.8 | 0.0000 | 0.0000 | 0.0000 |
| 0.9 | 0.0000 | 0.0000 | 0.0000 |

Eight distinct flag rates, so `threshold` does discriminate across its range — the
saturation that made the v1 parameter inert is gone. **But the curve is also the clearest
single statement of the ceiling**: at threshold 0.6 the flag rate is 1.86%, *inside* the
≤2% target, and precision is **zero**. The queue shrinks to the right size only by
emptying itself of real construction. Precision peaks at 0.0794 where 30% of the county is
flagged, and collapses the moment the queue becomes small enough to work.

This is the measured ceiling the plan was told to record rather than argue. Three separate
lines of evidence agree:

1. **Average precision is flat under tuning** — 0.0369 to 0.0400 across a 12-point sweep of
   the classification parameters, and no better after the score was rebuilt.
2. **No usable operating point exists** — the 2-D sweep above.
3. **A no-imagery baseline outranks every version of the detector** — AP 0.4793 against
   0.0592.

What a classical cue can and cannot do here is now bounded by measurement. Brightness,
smoothness, contrast against surroundings, NIR-derived vegetation, compactness and
contiguity have all been applied; what remains unseparated is a roof from a similarly
bright, similarly compact patch of ground at 1 m resolution. **This is the trigger for the
learned-detector plan**, with the harness, the labelled sets, the audit and the baseline
already in place to judge it by.

### What did improve, and is worth keeping

Measured against the Task 2 baseline, on the same parcels with the same labels:

| | baseline | now |
|---|---|---|
| vacant land read as built-up | 0.949 → 1.000 | **0.000 → 0.003** |
| built-up delta on real construction | **−0.108** | **+0.169** |
| year-level bias (control pair) | +0.160 | +0.030 |
| precision vs base rate | 0.0442 vs 0.0599 (**below**) | 0.0794 vs 0.0599 (**above**) |

The detector went from ranking *worse than random* to better than random, and the three
mechanism defects the plan set out to fix — bare ground read as structure, construction
lowering the built-up fraction, and an uncorrected NIR shift between captures — are fixed
and covered by tests. It is the *sufficiency* of the classical cue that fails, not those
repairs.

## Task 8 — fixtures at the shipped defaults (2026-09-22)

The fixtures now run at the **shipped** `DEFAULT_THRESHOLD` and `DEFAULT_MIN_NEW_AREA_M2`.
They could not under the share-based score: a planted 20 × 15 m roof is 0.013 of a 5.5-acre
fixture parcel, so flagging it would have needed a 6 287 m2 building and the tests had to
run at a special threshold. Scoring the structure makes a 300 m2 roof worth 300 m2 wherever
it stands, which removed the workaround — the clearest practical confirmation that scoring
structures rather than shares was the right correction.

`test_fixture_years_flag_exactly_the_planted_roofs` still requires exactly parcels 3, 7 and
12 for 2021 → 2023 and parcel 1 for 2023 → 2025, and `test_runs.py` still requires exactly
3 candidates end to end. `naip_2023.tif` carries a `RECAPTURE_GAIN` of
`(1.06, 1.04, 1.02, 0.84)` so the fixture year pair differs radiometrically the way two real
captures do, exercising the Task 5 normalisation that it previously bypassed.

## Task 9 — deployed stack (2026-09-22)

Run on the deployed stack in `us-east-1` (account `525390918028`, ALB
`PtaxCo-ApiLB-XNbHhnzCM4z6-1909514806`), tenant `Demo County`, over the **same 25 parcels
and the same two NAIP years** Plan B used:

| | threshold / min | candidates | skipped |
|---|---|---|---|
| Plan B, v1 detector | 0.3 / 40 m² | **23 / 25** | 0 |
| Plan B, v1 detector | 0.6 / 200 m² | 21 / 25 | 0 |
| Plan B, v1 detector | 0.9 / 500 m² | 19 / 25 | 0 |
| **Plan D, shipped defaults** | **0.3 / 37.2 m²** | **2 / 25** | 0 |

**92% flagged → 8% flagged on identical ground and identical imagery.** The ingest path
agrees with the harness: both show the collapse in flag rate, so the offline measurements
describe the program that actually runs in AWS.

The run-level radiometric fit executed in production, from the worker log:

```
run 6a72d085 radiometric fit over 25 parcels: gains [0.89, 0.933, 0.333, 1.172]
run 6a72d085 done: 25 parcels, 2 candidates, 0 skipped
```

An NIR gain of **1.172** raises the 2021 capture toward the 2010 one — the same direction
and rough magnitude the harness measured offline (NIR 179 → 160 on unchanged ground). That
confirms the Task 5 wiring in `detection/run.py`, which the unit tests cannot: they exercise
the fit, not the job that builds it from a geohash-spread sample.

**What this run does not show.** The deployed tenant carries the 25-parcel synthetic fixture
layer over real ground, not a labelled county layer, so there is no `BUILD_YR` here and
**precision is not computable**. This is an ingest-path agreement check, not an independent
confirmation of the 7.9% precision measured offline on 300 labelled Hennepin parcels. The
flag rates also are not directly comparable — 8% here against 30% on the evaluation set —
because the two parcel populations differ; only the direction and magnitude of the collapse
transfer.

### Operating note

`/api/me` and the other authenticated endpoints require a Cognito **access** token and
reject an **ID** token with `{"detail":"not an access token"}`. Obtain it with
`aws cognito-idp initiate-auth ... --query 'AuthenticationResult.AccessToken'`.

Per-task before/after measurements for the remaining tasks are appended here.

## Tasks 10–11 — visual labels replace `BUILD_YR` (2026-09-22)

Every figure above this section was measured against the assessor's `BUILD_YR`. That is
not the question the application asks. The application takes a base image and a newer
image of the same parcel and looks for improvements; `BUILD_YR` records when a principal
structure was *registered*, which is a different quantity that happens to correlate.

So all 300 parcels were read by eye from their base/target chip pairs
(`uv run ptax-eval chips <set> --all`, 30 sheets of 10) and labelled directly:
**does the target capture show an improvement the base capture does not?**

```bash
uv run ptax-eval score eval/nw-hennepin-2010-2021.json \
  --labels eval/visual-labels-nw-hennepin-2010-2021.json
```

`visual-labels-nw-hennepin-2010-2021.json` carries a verdict and a closed-set reason
(`structure_added`, `addition_or_outbuilding`, `teardown_rebuild`, `no_change`,
`ground_change_only`, `unclear`) for every parcel, alongside the `BUILD_YR` stratum it was
drawn from. Only the first three count as an improvement: a field changing colour, a pond
drying and a lot being graded with nothing built on it are **not** improvements, and a
teardown-rebuild is (a new structure stands where an old one did). The **3 `unclear`
parcels count as negatives**, so a detector that flags them is charged a false positive —
the conservative direction, and at 1% of the set it moves no figure below. The stratum still sets each parcel's sampling weight — that is how it was
drawn and cannot be revised — but it no longer decides whether the parcel is a positive.
`ScoredParcel` now carries the two separately.

### How wrong `BUILD_YR` was

| stratum | improved, by eye | labelled | rate | disagrees with `BUILD_YR` |
|---|---|---|---|---|
| `positive` | 84 | 100 | 0.840 | **16** |
| `negative_old` | 14 | 100 | 0.140 | **14** |
| `negative_future` | 1 | 100 | 0.010 | 1 |
| **total** | 99 | 300 | | **31 (10.3%)** |

It is wrong in **both** directions, which is why the earlier 8.0% one-directional
"label-noise rate" from the 39-parcel audit understated it:

- **16 `positive` parcels show no improvement.** Almost all of them are graded, platted
  lots with a `BUILD_YR` of 2021 photographed on **2021-06-18** — the house is recorded in
  the capture year but post-dates the capture. Sheet 8 is ten of these in a row.
- **14 `negative_old` parcels visibly improved** — a new machine shed, an outbuilding, a
  pool, a finished house on what was a construction site in 2010. `BUILD_YR` cannot see
  any of it, because the principal structure's year did not change.

### The base rate was wrong, and badly

`negative_old` is **92%** of the county's labelled parcels. A 14% improvement rate there
dominates everything else:

| | `BUILD_YR` | visual labels |
|---|---|---|
| county base rate | 0.0599 | **0.1795** |

The county-wide rate of parcels showing a *visible improvement* between 2010 and 2021 is
three times the rate of parcels whose *recorded build year* falls in that window. Every
precision figure above this section was therefore compared against a base rate less than a
third of the right one.

### Re-measured at the shipped defaults (threshold 0.3, 37.2 m²)

| | vs `BUILD_YR` | vs visual labels |
|---|---|---|
| base rate | 0.0599 | **0.1795** |
| flag rate | 0.3016 | 0.3016 |
| precision | 0.0794 | **0.2874** |
| recall | 0.4000 | 0.4828 |
| average precision | 0.0592 | **0.2236** |
| no-imagery baseline AP | 0.4793 | **0.2578** |

Precision **0.287 against a 0.180 base rate — a 1.6× lift**, up from a 1.3× lift. The
detector is meaningfully better than the `BUILD_YR` figures suggested, because a good part
of what was counted against it was real construction that the assessor's year could not
see. The no-imagery baseline also falls hard (AP 0.4793 → 0.2578): ranking by inverse
parcel size was largely predicting *which parcels get new principal structures recorded*,
not which parcels gain buildings, so relabelling removes most of that confound.

### The ceiling stands, and is now sharper

Re-running the same two sweeps against the visual labels. Both tables come from
`ptax-eval score` with `--threshold` and `--min-new-area` varied over the grid — nothing
else is needed to reproduce them:

```bash
uv run ptax-eval score eval/nw-hennepin-2010-2021.json \
  --labels eval/visual-labels-nw-hennepin-2010-2021.json \
  --threshold 0.6 --min-new-area 37.2      # -> flag rate 0.0186, precision 0.0000
```

Threshold curve at 37.2 m²:

| threshold | flag rate | precision | recall |
|---|---|---|---|
| 0.1 | 0.4792 | 0.2623 | 0.7002 |
| 0.2 | 0.4570 | 0.2470 | 0.6289 |
| **0.3** | **0.3016** | **0.2874** | **0.4828** |
| 0.4 | 0.1899 | 0.1677 | 0.1774 |
| 0.5 | 0.0743 | 0.0000 | 0.0000 |
| 0.6 | 0.0186 | 0.0000 | 0.0000 |
| 0.7 | 0.0184 | 0.0000 | 0.0000 |
| 0.8–0.9 | 0.0000 | 0.0000 | 0.0000 |

2-D sweep, precision (flag rate), `*` where precision exceeds the 0.1795 base rate:

| threshold | 37 m² | 100 m² | 200 m² | 400 m² | 800 m² |
|---|---|---|---|---|---|
| 0.0–0.1 | 0.262 (0.479)\* | 0.247 (0.457)\* | 0.269 (0.268)\* | 0.000 (0.074) | 0.000 (0.019) |
| 0.2 | 0.247 (0.457)\* | 0.247 (0.457)\* | 0.269 (0.268)\* | 0.000 (0.074) | 0.000 (0.019) |
| **0.3** | **0.287 (0.302)\*** | 0.287 (0.302)\* | 0.269 (0.268)\* | 0.000 (0.074) | 0.000 (0.019) |
| 0.4 | 0.168 (0.190) | 0.168 (0.190) | 0.168 (0.190) | 0.000 (0.074) | 0.000 (0.019) |
| 0.5 | 0.000 (0.074) | 0.000 (0.074) | 0.000 (0.074) | 0.000 (0.074) | 0.000 (0.019) |
| 0.6 | 0.000 (0.019) | 0.000 (0.019) | 0.000 (0.019) | 0.000 (0.019) | 0.000 (0.019) |

> **No pair reaches ≥60% precision at ≤2% flagged.** The best precision anywhere on the
> grid is 0.287, at a 30% flag rate.

The shape of the failure is unchanged and now stated against the right truth: **precision
goes to exactly zero the moment the queue shrinks to a workable size.** At threshold 0.6
the flag rate is 1.86% — inside the target — and not one flagged parcel improved. The
largest structures the detector finds are all false; it fills the top of its own ranking
with them, which is why precision @ top 1% is **0.0000** while the no-imagery baseline's
is 1.0000.

**The ceiling finding is not withdrawn.** It was stated as "no combination of threshold and
minimum structure size produces a small queue above the base rate", and against corrected
labels, a corrected base rate and a corrected baseline, that is still exactly what the
measurement says. What changes is the size of the gap: the classical detector is a 1.6×
lift over chance rather than a 1.3× one, and still an order of magnitude short of a queue
an assessor could work. It remains the trigger for the learned detector.

### What this means for the ML plan

The earlier note that an ML evaluation set should be **matched or banded on parcel size**
is withdrawn. Parcel size is irrelevant to the application: it must find improvements on a
parcel of any size. Banding on size would build a set that does not resemble the county
and would answer a question no one asked.

The real lesson is the one this task demonstrates: **label the imagery, not the assessor's
records.** The no-imagery baseline exists to catch a detector that has learned something
other than buildings, and relabelling cut its average precision nearly in half — most of
what it was "detecting" was an artefact of what `BUILD_YR` records, not of what the ground
looks like. Judge the learned detector on these visual labels, and keep printing the
baseline beside it.

## What the detector marks (2026-09-23)

`ptax-eval chips` can draw the detector's markup, in the parcel viewer's colours — red
for the structure the score rests on, blue for the rest of the detected new built-up area:

```bash
uv run ptax-eval chips eval/nw-hennepin-2010-2021.json --all --ranked --limit 20 --markup
```

`--markup` adds a third panel per parcel; `--ranked` orders the set by detector score
across strata; `--limit` keeps the first N. Output goes to `out/chips/<set>/ranked/` (or
`markup/`), never over the labelling sheets. Without flags the command renders the same
sheets as before, byte for byte, **blind to the detector** — the visual labels must stay
uninfluenced by what it marked.

The detector's twenty highest-scoring parcels, joined to the visual labels:

- **Ranks 1–13 are all false positives** (`no_change` or `ground_change_only`). The first
  real improvement is rank 14; ranks 14–20 are all real.
- **None of the top ten scoring structures is a roof.** Each is bare ground: a harvested
  or mowed field stripe, a dirt track, a dried pond, graded lots, bare yard patches.

Graded lots are the clearest case. The structure filter rewards compact, near-rectangular
blobs, and a graded house lot is exactly that — so the detector's *largest* structures are
lots that have been prepared for building but not yet built on. This is the concrete
mechanism behind `precision @ top 1% = 0.0000`, and the failure mode a learned detector
has to separate: bright, smooth, compact, recently disturbed ground versus a roof.

## Learned detector — segmenter-v1 and the decision gate (2026-09-23)

Plan: `docs/plans/2026-09-23-learned-change-detector.md`. A U-Net (ImageNet ResNet-34
encoder) segments buildings in each year; the change is building area present in the
target and absent from the base (one pixel of tolerance for misregistration), scored with
the classical detector's curve, `structure_m2 / (structure_m2 + 400)`, behind the same
`Detector` seam. Selected with `--detector segmentation`; `classical` stays the default and
reproduces its earlier output exactly.

### How it was trained, and why the score is independent

```bash
uv sync --group ml                                 # torch + segmentation-models-pytorch
uv run --group ml ptax-eval train-data             # chips from the training AOIs only
uv run --group ml ptax-eval train                  # writes eval/models/segmenter-v1.{pt,json}
```

- **Labels:** Microsoft's open US building footprints (Minnesota file, ODbL), burned onto
  each chip's own 1.0 m grid. Chips are read through `read_parcel_uris`, the run job's own
  reader, so training imagery is resampled exactly as inference imagery is.
- **Where:** Richfield, Minnetonka, Brooklyn Park and Crystal — each ≥ 90% of dated
  parcels built by 2000 (0.903–0.930), so a footprint marks a building standing in every
  capture used. Edina (0.820) and three rural west-Hennepin candidates (0.44–0.72) failed
  that check and were dropped. Every training AOI is ≥ 1 km from `nw-hennepin`, checked in
  EPSG:26915 by `assert_disjoint_from_eval` at both data-build and training time.
- **When:** NAIP 2010, 2015, 2019 and 2021 over the same ground. 1 100 training chips and
  316 validation chips (256 × 256); validation is the trailing 20% of each AOI's tile
  columns, the same tiles in every year. Six tiles were dropped by the label screen.
- **Chosen on validation only:** 27 epochs, early-stopped with the best at epoch 22;
  probability cutoff 0.40. Validation building IoU **0.588** — per year 2010 **0.612**,
  2015 0.601, 2019 0.561, 2021 0.580 — and 0.476 on 48 px parcel-sized crops.
- **Frozen before evaluation:** `eval/models/segmenter-v1.json` (committed) records
  `weights_sha256` `95e57ba6…a55b57` and `frozen_at` 2026-09-23T17:10:48Z. The first score
  against the visual labels ran at 17:11:10Z. The detector refuses weights whose hash
  differs from the card; a retrain is a new candidate (`segmenter-v2`), never a
  replacement.

Every evaluation run of this candidate, not only the best:

| Run | What | Result |
|---|---|---|
| 1 | `score --detector segmentation --labels …` | the figures below |
| 2 | the same, after fixing the per-year report to read the segmenter's indicators | per-parcel results identical to run 1 |
| 3 | `chips --all --ranked --limit 20 --markup --detector segmentation` | read below |
| 4 | the same `score` on CPU (`PTAX_TORCH_DEVICE=cpu`), 2026-09-23 | identical to run 1: every flag and every score equal on all 300 parcels (max difference 0.0000) |
| 5 | the same `score` on MPS, for the comparison | identical to run 4 |

No setting was changed after any of them. Runs 4 and 5 were made by the production-integration plan
(`docs/plans/2026-09-23-segmenter-production-integration.md`) to prove the CPU inference
production workers run reproduces the gated result; Fargate has no GPU and no MPS.

### Measured, at the 0.1795 county base rate

`ptax-eval score eval/nw-hennepin-2010-2021.json --labels eval/visual-labels-nw-hennepin-2010-2021.json --detector segmentation`, all 300 parcels scored:

| | classical | no-imagery baseline | **segmenter-v1** |
|---|---|---|---|
| Average precision | 0.2236 (95% CI 0.141–0.367) | 0.2578 | **0.5879 (95% CI 0.422–0.772)** |
| Precision @ top 1% | 0.0000 | 1.0000 | **1.0000** |
| Precision @ top 5% | 0.0000 | 0.3036 | **0.7856** |
| Flag rate at defaults (0.3, 37.2 m²) | 0.3016 | — | **0.1425** |
| Precision at defaults | 0.2874 | — | **0.6077** |
| Recall at defaults | 0.4828 | — | **0.4823** |
| `no_change` parcels flagged | 20 of 126 | — | **6 of 126** |

Intervals are a stratified bootstrap (1 000 replicates, resampled within each stratum, weights
recomputed per replicate), now printed by every `score` run.

**Split by parcel size** (median 1 451 m²), weighted:

| | classical AP | segmenter AP | segmenter precision @ top 1% |
|---|---|---|---|
| Below the median (150 parcels, 35 improved) | 0.648 | **0.980** | 1.000 |
| At or above the median (150, 64 improved) | 0.212 | **0.540** | 1.000 |

Small, parcel-only crops were the named inference risk; they are where the segmenter is
strongest. Large parcels are harder for both detectors, and the gain there is the larger.

**The 2010 domain shift did not appear.** On the 126 parcels labelled `no_change`, the
segmenter's base-year building fraction (quartiles 0.000 / 0.020 / 0.093) matches the
target year's (0.000 / 0.020 / 0.087): it reads 2010 roofs as it reads 2021 roofs.

### The ≤2% / ≥60% product target

Met on the point estimate, and fragile. At threshold 0.51 (structures ≥ 416 m²) the
segmenter flags **1.70%** of the county at **precision 1.00, recall 0.095** — 14 sampled
parcels, all genuinely improved. At 0.50 one more `negative_old` parcel enters, a false
positive, and because each `negative_old` parcel stands for ~3 900 county parcels, precision
falls to 0.67 at a 2.8% flag rate. Fourteen parcels is not enough to call that operating
point settled; a second labelled area would.

| threshold | flag rate | precision | recall |
|---|---|---|---|
| 0.30 | 0.1425 | 0.6077 | 0.4823 |
| 0.40 | 0.0708 | 0.8613 | 0.3395 |
| 0.50 | 0.0280 | 0.6710 | 0.1047 |
| 0.60 | 0.0122 | 1.0000 | 0.0680 |

### What it marks at the top of its ranking

`out/chips/nw-hennepin-2010-2021/segmentation/ranked/`, read by eye and joined to the
visual labels:

- **19 of the top 20 are real improvements**, against 0 of the classical detector's top 13.
- **Every red structure in the top 20 sits on a roof.** No graded lot, field stripe, dirt
  track or dried pond appears — the classical detector's named failure is gone, even though
  training saw little open field or bare soil.
- **Driveway spill:** on several parcels (ranks 1, 12, 20) the structure runs from the
  roof onto the pale driveway or pad beside it, inflating `structure_m2`. It moves scores,
  not which parcels rank high.
- **The one false positive (rank 16)** is a house already standing in 2010 that the
  segmenter missed in the base year, so it was counted as new — the base-year miss the
  domain-shift risk named, here as a single case rather than a pattern.

### CPU cost

Production workers are CPU-only. Over the 300 parcels on the dev machine's CPU (Apple
M4 Max), one thread: `compare` — two inferences on a ≥ 256 px canvas — averages **73.6 ms**
per parcel (p95 79.6 ms); reading both years averages 53.6 ms. A 200 000-parcel run is
**4.1 CPU-hours of inference, 7.1 with reads**. A Fargate vCPU is likely slower than an M4
core, so budget a multiple of that; it is still a CPU job, not a GPU one.

**Measured on Fargate (2026-09-23):** the deployed 2 vCPU worker averaged 542.8 ms per parcel
for `segmenter-v1` against 130.5 ms for the classical detector over the same 25 parcels and
reads, so inference is about 410 ms per parcel there -- about 5.6x the M4 figure. A
200 000-parcel run is about 30 hours on one worker. CPU inference reproduces the gated
result exactly (evaluation runs 4 and 5 above). Details in the top-level README.

### Decision: **pass**

The gate required AP's 95% bootstrap lower bound above both 0.2236 and 0.2578 and precision
@ top 1% above zero. The lower bound is **0.422** and precision @ top 1% is **1.0000**.
`segmenter-v1` is the candidate for production integration — a per-run detector choice, the
model in the worker image, run records naming the detector — which is a separate plan.
Open before that ships: a second labelled evaluation area (the ≤ 2% target rests on 14
parcels here, and one area cannot show generalisation), and the Fargate-measured inference
time.
