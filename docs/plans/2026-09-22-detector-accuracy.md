# Parcel Structure Change Detection — Plan D: Detector Accuracy on Real Imagery

Created: 2026-09-22
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 1
Worktree: No
Type: Feature

## Summary

**Goal:** A NAIP 2010 → 2021 run over real county parcels returns a reviewable queue — at most 2% of scored parcels flagged at the shipped defaults, with at least 60% precision on a labelled evaluation set — and every detector change in this plan is recorded as a before/after measurement on that set rather than argued from synthetic fixtures.

Depends on Plan B (`docs/plans/2026-09-21-imagery-and-detection.md`): `ImagerySource`, stored COGs, `read_parcel`, the `Detector` seam, `runs`/`run_parcels` with per-parcel `indicators`. Nothing here changes the database schema or the API contract, so Plan C (`docs/plans/2026-09-21-review-workflow.md`) is unaffected and can proceed in parallel.

## Problem (measured, not hypothesised)

Plan B's Task 10 ran the deployed stack over 25 parcels with real NAIP 2010 (1.0 m) as base and 2021 (0.6 m) as target. Results:

- **23 of 25 parcels flagged as candidates.** Tightening run parameters barely moved it: `threshold 0.6 / 200 m²` → 21, `threshold 0.9 / 500 m²` → 19.
- Stored indicators across the 25 parcels:

  | indicator | min | median | max |
  |---|---|---|---|
  | `new_builtup_m2` | 22 | 3351 | 14893 |
  | `veg_loss_m2` | 0 | 2203 | 11558 |
  | `base_builtup_frac` | 0.061 | 0.366 | 0.998 |
  | `target_builtup_frac` | 0.253 | **0.937** | 1.000 |
  | `nodata_frac` | 0 | 0 | 0 |

Three defects are visible in those numbers:

1. ~~**A year-level bias in the built-up mask (the dominant one).**~~ **Refuted by Task 2 — see `## Deviations`.** Plan B attributed `target_builtup_frac` 0.937 against `base_builtup_frac` 0.366 to the 0.6 m raster being smoother once resampled onto the 1.0 m grid. Over 300 real parcels that gap measures **+0.015**, and the *same-resolution* control measures **+0.160** — worse where there is no mismatch to blame. The real defect is that `ClassicalDetector._classify` marks a pixel built-up when it is non-vegetated, bright and *locally smooth* (`texture < TEXTURE_T`), which describes bare soil and gravel as readily as a roof: vacant parcels read 0.949 → 1.000 built-up, and finishing a house *lowers* the fraction (0.677 → 0.569) as graded soil becomes lawn.
2. **Score saturation.** `score = 1 - exp(-(new + 0.25·veg_loss) / 200)` reaches ~1.0 by a few hundred m², so `threshold` cannot discriminate once the masks disagree at all; only `min_new_area_m2` still bites, and at 500 m² it still passed 19 parcels. The score carries almost no ranking information in this regime, which also makes Plan C's queue ordering meaningless.
3. **No radiometric normalisation between years.** Absolute cutoffs (`BRIGHT_T = 80`, `VEG_T_NDVI = 0.2`) are applied to two captures with different sensors, seasons, sun angles and post-processing. A wholesale brightness or NDVI shift moves every pixel across the cutoff at once.

The parcels that did *not* flag were those already ~99% built-up in the base year — consistent with all three defects and with nothing in the detector actually recognising structures.

## Out of Scope

- ML / learned segmentation — still a later plan behind the same `Detector` interface. Task 6 may add further hand-derived cues (NIR-derived indices, edge and shape structure) inside `ClassicalDetector`, but trains nothing.
- Database schema, API contract, job-runner and new frontend features. Task 7 does update the two duplicated default constants in `frontend/src/api/runs.ts` in step with `backend/src/ptax/api/runs.py`; that is a value change, not a feature.
- Detecting demolitions, pools or solar; oblique/DSM inputs (PRD out of scope).
- Per-tenant threshold settings — still a run parameter; revisit only if evaluation shows counties genuinely need different values.
- Running the evaluation harness in CI. It needs network and a local imagery cache; only its pure logic (label rule, sampling, metrics) is unit-tested in `make test-backend`.

## Approach

**Chosen:** A `ptax-eval` harness (`backend/src/ptax/eval/`) that scores real Hennepin County parcels over real NAIP through the *production* `read_parcel` path, with labels derived from the county's public `BUILD_YR` attribute and a measured label-noise error bar; then fix the defects one at a time, re-measuring after each.
**Why:** Every previous tuning signal came from synthetic fixtures the detector was written against, which is how a 92% flag rate reached AWS — so each change here has to be falsifiable against real imagery, at the cost of a build-and-cache step before any tuning can start. Hand-labelling 100–300 parcels was rejected because `BUILD_YR` gives the same truth objectively and reproducibly; trusting `BUILD_YR` *without* the Task 3 audit was rejected because reporting precision against labels of unknown quality repeats the synthetic-fixture mistake in a new form.

## Global Constraints

- Target operating point: **≤ 2% of scored parcels flagged** and **≥ 60% precision**, at the shipped default run parameters, **reweighted to the county base rate** (below) — never as a raw count over the evaluation sample.
- County base rate: **5.99%** of *labelled* Hennepin parcels have a `BUILD_YR` inside an eleven-year window (25 167 of 420 207 for 2011–2021, measured 2026-09-22; a further 27 851 parcels carry no assessor year and are excluded from both the sample and this denominator). The evaluation AOI is a growth fringe at roughly three times that, so a raw flag rate measured on it is not comparable to the target.
- The target is therefore a **precision-first** operating point: at a 6% base rate, flagging 2% at 60% precision is about **20% recall**. Low recall is the chosen operating point, not a defect — Tasks 6 and 7 optimise the top of the ranking, not total catch.
- Evaluation pair: NAIP **2010 (1.0 m) → 2021 (0.6 m)** over Hennepin County, MN. Control pair: **2013 (1.0 m) → 2017 (1.0 m)**, same AOI.
- Committed evaluation data carries only `PID`, geometry, `BUILD_YR`, `PARCEL_AREA`, `STATE_CD`, label and stratum. Never `OWNER_NM`, `TAXPAYER_NM*`, `HOUSE_NO`, `STREET_NM` or any other owner/address field.
- Planetary Computer is evaluation tooling only. The production NAIP path stays Earth Search + requester-pays `naip-analytic` via `ptax.imagery.naip.NaipStacSource`; no production code reads a Planetary Computer URL.
- Cached imagery is stored at each year's **native** GSD and CRS, through the production COG writer so it carries the same overviews. Nothing in the cache may be pre-resampled onto the comparison grid.
- Python 3.13, `ruff` line-length 100 with `E,F,I,UP,B`, `mypy` over the `ptax` package.

## Context for Implementer

`BUILD_YR` is the assessor's year-built for a parcel's **principal structure**, attached to the parcel's **current** geometry. It therefore gives clean positives (a parcel whose principal structure was built inside the comparison window) and *noisy* negatives: a 1955 house that gained a detached garage, a large addition or a barn in 2015 is labelled negative, and the detector is right to flag it. A teardown-rebuild is labelled positive but shows a building in both years, so the detector is right *not* to flag much new built-up area. Both directions are real label error, not detector error, which is why Task 3 measures the rate on an audited subset and every later metric is reported with that error bar rather than as a bare number. Treat a measured precision of 0.6 with a measured label-noise floor of 0.15 as "0.6, and at most 0.15 of the misses are label artefacts" — never silently subtract one from the other.

The harness must score through `ptax.imagery.reader`, not through a reader of its own. Defect 1 is a property of how the two years land on one comparison grid, so a harness that resampled differently would be measuring a different program than the one that runs in AWS. Task 1 splits `read_parcel` so the harness and `detection/run.py` share one warp path.

## Runtime Environment

- Local stack: `make dev-up` (PostGIS, MinIO, cognito-local), `make seed`. The harness needs **none** of it — it reads cached local GeoTIFFs — but `make test-backend` does.
- Backend tests: `cd backend && uv run pytest -q`.
- Harness: `cd backend && uv run ptax-eval <command>`. Network required for `build` and `fetch` only.
- This session has **no AWS credentials** (`aws sts get-caller-identity` fails), so requester-pays `naip-analytic` is unreachable here. Task 9 runs on the deployed stack and is user-owned for that reason.

## File Structure

- `backend/src/ptax/eval/__init__.py` (create) — package marker.
- `backend/src/ptax/eval/sources.py` (create) — public data access: Hennepin parcel query (ArcGIS REST → GeoJSON) and Planetary Computer NAIP discovery + SAS signing. Network lives here and nowhere else.
- `backend/src/ptax/eval/dataset.py` (create) — pure: AOI registry, `BUILD_YR` → label rule, stratified seeded sampling, eval-set read/write. No I/O beyond the set file.
- `backend/src/ptax/eval/cache.py` (create) — per-parcel native-GSD window cache and its manifest.
- `backend/src/ptax/eval/metrics.py` (create) — pure: precision / recall / flag rate / PR-AUC / top-k precision / per-stratum breakdown, with audit corrections applied.
- `backend/src/ptax/eval/chips.py` (create) — base/target chip pairs and the contact sheet used for the Task 3 audit.
- `backend/src/ptax/eval/cli.py` (create) — `ptax-eval` Typer app: `build`, `fetch`, `score`, `chips`.
- `backend/eval/README.md` (create) — how to rebuild the set and the cache from scratch.
- `backend/eval/*.json` (create) — committed evaluation sets and the audit file. Small; no imagery.
- `backend/tests/test_eval_dataset.py`, `backend/tests/test_eval_metrics.py` (create) — unit coverage for the two pure modules.

## Assumptions

- Hennepin County's `HennepinData/LAND_PROPERTY/MapServer/1` and Planetary Computer's `naip` STAC collection stay publicly reachable without credentials. Verified from this machine on 2026-09-22 (1038 parcels and NAIP 2010/2013/2017/2021 returned for the default AOI; a signed blob read returned `206`). Tasks 1 and 2 depend on this.
- `BUILD_YR` is populated for enough of the AOI to stratify. Verified on 2026-09-22 over the default AOI, after the 400–40 000 m² area filter: **2010 → 2021** gives 135 `positive`, 141 `negative_old`, 238 `negative_future`, 278 `excluded`; the **2013 → 2017** control gives 58 `positive`, 190 `negative_old`, 266 `negative_future`. Tasks 1 and 3 depend on these counts; a materially different result means the service changed and the set must be rebuilt before any metric is trusted.
- `BUILD_YR` is a **text** field holding values like `'0000'`, `'1988'`. A numeric `WHERE BUILD_YR >= 2011` fails on the service and a numeric comparison in Python raises on the string. Compare as quoted strings in the query (`BUILD_YR > '2010'`) and coerce explicitly in `dataset.py`. Task 1 depends on this.
- The local 2010 → 2021 pair reproduces the AWS defect. Same product and same two resolutions, but a different quarter-quad (`4509352` here, `4509359` on AWS). Task 2's RED depends on it; if the local flag rate comes out low, Task 2 records the measured number and the plan's problem statement is re-derived from it rather than from Plan B's.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Label noise (additions, outbuildings, teardown-rebuilds) is large enough to make precision meaningless | High | Medium | Task 3 measures it on a stratified audited subset and writes per-parcel corrections; every metric after Task 3 is reported with the measured noise rate beside it |
| Normalising away the resolution bias also destroys real signal, so metrics "improve" only because the detector stopped detecting | Medium | High | Task 4's DoD requires the same-resolution control pair (2013 → 2017, both 1.0 m) to hold recall within 5 points as well as the cross-resolution pair improving |
| Public endpoints change shape or disappear, leaving the set unreproducible | Medium | High | The labelled sets are committed (labels + geometry, no imagery), so only the cache needs refetching; the cache manifest records each STAC item id so another source can be pointed at the same windows |
| Classical cues cannot reach ≤ 2% / ≥ 60% at 1 m | Medium | Medium | Task 6 may add NIR-derived and edge/shape cues; if the target is still missed the plan lands the harness plus the best measured detector and records the ceiling with evidence as the trigger for the ML plan (decided with the user during planning) |

## Goal Verification

### Truths

1. A reader who did not write this plan can reproduce every accuracy claim in it from the committed evaluation sets plus `ptax-eval fetch` and `ptax-eval score`, without AWS credentials and without re-running an ingest.
2. Each of Tasks 4–7 has a recorded before/after measurement on the labelled set stored under `backend/eval/`, and a task that did not move its metric says so rather than being marked done on a code change alone.

## E2E Test Scenarios

### TS-001: The local fixture run still flags exactly the planted roofs
**Priority:** Critical
**Preconditions:** `make dev-up` and `make seed` done; signed in as `admin@demo.test`; Demo County's fixture layer ready; fixture imagery years 2021 and 2023 ingested.
**Mapped Tasks:** Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `/runs` | Base and target year selects list the ready fixture years |
| 2 | Select base `2021`, target `2023`, leave threshold and min area at their shipped defaults, click Start run | A run appears `queued`, then `succeeded` |
| 3 | Read the finished run's summary line | `25 processed`, `3 candidates`, `0 skipped` — the three planted roofs, unchanged by Tasks 4–7 |
| 4 | Open the run and read the per-parcel scores | The three candidate parcels rank above every non-candidate; scores are spread, not all ~1.0 |

### TS-002: The deployed stack reaches the recorded operating point on real NAIP
**Priority:** Critical
**Preconditions:** Deployed stack reachable; signed in at the ALB as the provisioned admin; a real county parcel layer and NAIP 2010 + 2021 ingested and ready.
**Mapped Tasks:** Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to Runs and start `2010 → 2021` at the shipped defaults | Run reaches `succeeded` |
| 2 | Read the run summary | Candidate count is in the single-digit percent range and within 5 percentage points of the reweighted flag rate Task 7 measured offline |
| 3 | Open the candidate list ordered by score | Scores span the range rather than clustering at 1.0, and the top-ranked parcels show visible new structures in the target-year preview |

## Progress Tracking

- [x] Task 1: Shared reader seam + labelled evaluation sets from real parcels and real NAIP
- [x] Task 2: Metrics harness and the baseline measurement (the RED for Tasks 4–7)
- [x] Task 3: Visual audit of a stratified subset → measured label-noise rate
- [x] Task 4: Normalise the year-level bias in the built-up mask — closed without implementation under the ceiling decision (see `## Deviations`)
- [x] Task 5: Radiometric normalisation between the two years
- [x] Task 6: Re-derive the built-up / vegetation classification against the harness
- [x] Task 7: A score that ranks, and re-tuned defaults — structure size; measured ceiling recorded
- [x] Task 8: Re-baseline the synthetic fixtures and Plan B's detector/run tests
- [x] Task 9: Re-run on the deployed stack and record the measured operating point
- [x] Task 10: Visual labels for all 300 parcels — replace `BUILD_YR` as ground truth
- [x] Task 11: Re-measure Tasks 2 and 5–7 against the visual labels and re-derive the ceiling

## Implementation Tasks

### Task 1: Shared reader seam and labelled evaluation sets

**Objective:** Split `read_parcel` so the harness and the production run job warp imagery through one code path, then build two committed evaluation sets — the cross-resolution pair and a same-resolution control — from Hennepin County parcels labelled by `BUILD_YR`, and cache the matching NAIP windows locally at native GSD. This is the foundation every later task measures against; nothing here changes detector behaviour.

**Files:**

- Modify: `backend/src/ptax/imagery/reader.py`
- Create: `backend/src/ptax/eval/__init__.py`
- Create: `backend/src/ptax/eval/sources.py`
- Create: `backend/src/ptax/eval/dataset.py`
- Create: `backend/src/ptax/eval/cache.py`
- Create: `backend/src/ptax/eval/cli.py`
- Create: `backend/eval/README.md`
- Create: `backend/eval/nw-hennepin-2010-2021.json`
- Create: `backend/eval/nw-hennepin-2013-2017.json`
- Modify: `backend/pyproject.toml`
- Modify: `.gitignore`
- Modify: `Makefile`
- Test: `backend/tests/test_eval_dataset.py`
- Test: `backend/tests/test_tiles.py`

**Key Decisions / Notes:**

- Split `read_parcel` into `read_parcel_uris(env, uris, geom_4326, *, resolution_m, buffer_m=0.0)` carrying the body at `backend/src/ptax/imagery/reader.py:113`, plus a thin `read_parcel(settings, assets, ...)` wrapper that builds `env` from `gdal_env(settings)` and URIs from `_uris`. `_read_part` at `backend/src/ptax/imagery/reader.py:152` takes the env the same way. Existing callers in `detection/run.py` and `api/imagery.py` keep their signatures; `tests/test_tiles.py:102` must still pass unchanged.
- `sources.py` holds all network access. Parcels: `GET https://gis.hennepin.us/arcgis/rest/services/HennepinData/LAND_PROPERTY/MapServer/1/query` with an `esriGeometryEnvelope`, `f=geojson`, `outFields` limited to `PID,BUILD_YR,PARCEL_AREA,STATE_CD` — the constraint on committed fields is enforced at the query, not by filtering afterwards. `maxRecordCount` is 2000, so page with `resultOffset`. NAIP: `POST https://planetarycomputer.microsoft.com/api/stac/v1/search` for `collections: ["naip"]`, then sign each `assets.image.href` with a token from `https://planetarycomputer.microsoft.com/api/sas/v1/token/naip`. Tokens expire in about an hour — re-sign per batch rather than once per run.
- Label rule in `dataset.py`, given `base_year` and `target_year`: `positive` when `base_year < BUILD_YR <= target_year`; `negative_old` when `0 < BUILD_YR <= base_year`; `negative_future` when `BUILD_YR > target_year` (platted and graded but not yet built — the hardest negatives, kept as their own stratum so their contribution is always visible); `excluded` when `BUILD_YR` is absent or `0`. Exclude parcels outside 400–40 000 m² (`PARCEL_AREA` is square feet) to drop condo slivers and the multi-hectare outliers.
- Default AOI `nw-hennepin` = bbox `-93.60, 45.16, -93.57, 45.19`, verified to return 1038 parcels with NAIP 2010/2013/2015/2017/2019/2021/2023 coverage. Keep the AOI registry a module constant so a second box can be appended when the counts below are short.
- **Sample by stratum with recorded inclusion probabilities, not proportionally.** Draw a fixed quota per stratum (100 `positive`, 100 `negative_old`, 100 `negative_future`) and store each stratum's `sampled / eligible` ratio in the set file. A proportional draw of 300 from the ~792 eligible parcels would yield only ~51 positives — too few for a stable recall estimate — while an unweighted stratified draw silently reports a 33% base rate. Recording the inclusion probability is what lets Task 2 reweight back to the county rate in `## Global Constraints`; without it neither flag rate nor precision means anything outside this AOI.
- Also record the eligible population size per stratum for the AOI *and* the county-wide `BUILD_YR` counts behind the base rate, so the reweighting inputs live with the set rather than in this plan.
- Cache each NAIP **item** clipped to the AOI (plus a 300 m buffer) and written through `ptax.imagery.cog.to_cog` — the same writer the ingest job uses — under `backend/eval/cache/<set>/`. The AOI is covered by two quarter-quads per year, so this is 4 files per set, not one per parcel. The manifest records the STAC item id, native GSD, CRS and the written 4326 bounds per entry.
- **Cache at item granularity, because a per-parcel crop cannot reproduce production's resampling.** A real quarter-quad COG carries overviews (`[2, 4, 8, 16, 32]`); a few-hundred-pixel crop carries none. Reading a 0.6 m year at the 1.0 m comparison grid resamples from the `/2` overview when one exists and from full resolution when it does not, and `cog_translate` builds overviews for every stored asset — so a crop feeds the detector pixels production never sees, on exactly the year whose built-up fraction is the defect under investigation. Storing the item as production would have stored it makes equivalence structural rather than something to chase.
- **Clip to the AOI plus `AOI_CLIP_BUFFER_M` (300 m).** Parcels are selected by *intersecting* the AOI box, so an edge parcel extends past it; a clip to the box exactly leaves those parcels without imagery over their far end.
- `.gitignore` gains `backend/eval/cache/` and `backend/eval/out/`; the `*.json` sets stay committed. `pyproject.toml` gains `ptax-eval = "ptax.eval.cli:app"` under `[project.scripts]`. `Makefile` gains `eval-fetch` and `eval-score` next to `imagery-fixtures`. No new third-party dependency — rasterio, rio-tiler and numpy cover everything, including PNG output in Task 3.
- Unit-test the pure parts only: the label rule at each boundary year, the area filter, seeded sampling reproducibility, and set round-tripping. `sources.py` is exercised by the DoD command, not by a network test.

**Definition of Done:**

- [ ] `ptax-eval build --aoi nw-hennepin --base-year 2010 --target-year 2021 --quota 100 --seed 20260922` writes `backend/eval/nw-hennepin-2010-2021.json` with 100 parcels in each of the three strata, each stratum carrying its `sampled / eligible` ratio, and the same command run twice produces a byte-identical file.
- [ ] `ptax-eval build --aoi nw-hennepin --base-year 2013 --target-year 2017 --quota 100 --seed 20260922` writes the control set; its `positive` stratum takes all 58 eligible parcels and records `58 / 58`, so the shortfall against the quota is visible in the file rather than silent.
- [ ] Both set files record the county-wide `BUILD_YR` counts used as the base rate, and the recorded rate is within a point of the 5.6% in `## Global Constraints`.
- [ ] Neither committed set contains the strings `OWNER_NM`, `TAXPAYER_NM`, `HOUSE_NO` or `STREET_NM`.
- [ ] `ptax-eval fetch backend/eval/nw-hennepin-2010-2021.json` caches the AOI's NAIP items for both years; the manifest's recorded GSD is 1.0 for 2010 and 0.6 for 2021, and re-running it re-downloads nothing.
- [ ] Production-equivalence is proven, not asserted: `ptax-eval fetch --verify` reports that **every** cached COG carries overviews — the property whose absence made an earlier per-parcel crop design disagree with the source on ~90% of target-year samples — and that all 300 parcels (258 in the control) read on the comparison grid in both years with zero unreadable and zero missing pixels inside the parcel. A failure raises `AOI_CLIP_BUFFER_M` and refetches; the buffer that was needed is recorded in `backend/eval/README.md`.
- [ ] `read_parcel_uris` and `read_parcel` produce identical `data`, `mask`, `parcel_mask`, transform, CRS and bounds for the same parcel (`tests/test_tiles.py`), so the harness and the run job share one warp.
- [ ] Verify: `cd backend && uv run pytest -q tests/test_eval_dataset.py tests/test_tiles.py && uv run ruff check . && uv run mypy` — mypy against the recorded baseline of 7 pre-existing errors in Plan B's code (see `## Deviations`), with zero new ones.

### Task 2: Metrics harness and the baseline measurement

**Objective:** Score the cached evaluation set with today's `ClassicalDetector` through the shared reader and report precision, recall, flag rate, PR-AUC, top-k precision and a per-stratum breakdown, alongside the per-year distributions of the statistics the classifier thresholds. This produces the RED every later task is measured against, and its distribution report decides whether Task 4 or Task 5 addresses the dominant bias first.

**Files:**

- Create: `backend/src/ptax/eval/metrics.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `backend/eval/README.md`
- Create: `backend/eval/baseline-nw-hennepin-2010-2021.md`
- Test: `backend/tests/test_eval_metrics.py`

**Key Decisions / Notes:**

- `ptax-eval score <set.json> [--threshold] [--min-new-area]` reads the cache, builds both `ParcelRaster`s via `read_parcel_uris` at `comparison_resolution`'s grid (`backend/src/ptax/detection/run.py:61`) so the harness sizes its window exactly as the run job does, calls `ClassicalDetector().compare`, writes per-parcel score and indicators to `backend/eval/out/<slug>-<timestamp>.json`, and prints the metrics table.
- Report threshold-dependent metrics (flag rate, precision, recall, F1 at the given parameters) *and* threshold-free ones (PR-AUC over score, precision at the top 1% and top 5%). The threshold-free pair is what tells Tasks 4–6 whether the underlying signal improved even while the score is still saturated; without it a saturated score hides every gain until Task 7.
- **Every rate is reweighted to the county base rate before it is reported.** Measure per-stratum rates on the sample (true-positive rate on `positive`, false-positive rate on each negative stratum), then combine them with the inclusion probabilities and the base rate that Task 1 stored in the set file: `flag_rate = π·TPR + (1−π)·FPR`, `precision = π·TPR / flag_rate`. The sample is stratified at roughly a 33% positive rate against a county rate of 5.6%, so a raw count over the sample overstates precision by about sixfold — reporting it would hand every later task a number that looks like the target while being nowhere near it.
- Print the raw per-stratum counts beside the reweighted figures. The reweighting is the headline, but a reader needs the counts to see how few parcels an estimate rests on.
- Also emit, per year and per stratum, the median and interquartile range of `brightness`, `texture`, and greenness (NDVI for 4-band), plus `base_builtup_frac` / `target_builtup_frac`. This is the diagnosis: Plan B attributed `target_builtup_frac` 0.937 to texture scale, and this report is what confirms or refutes it.
- If the report shows the brightness or greenness shift — not texture — dominates, do Task 5 before Task 4 and record the swap under Deviations. Fitting a texture fix to a radiometric cause would tune constants to an artefact, which is the failure this plan exists to stop.
- `metrics.py` stays pure (takes labels and scores, returns numbers) so `test_eval_metrics.py` can cover it with hand-built cases: all-flagged, none-flagged, a perfect ranker, a random ranker, ties in score, and an empty stratum.
- Record the run in `backend/eval/baseline-nw-hennepin-2010-2021.md` — the numbers, the command, and the date — because later tasks quote it and `out/` is gitignored.

**Definition of Done:**

- [ ] `ptax-eval score backend/eval/nw-hennepin-2010-2021.json` completes over the whole cached set and prints base-rate-reweighted flag rate, precision, recall and F1, plus PR-AUC, precision@top-1%, precision@top-5% and the raw counts per stratum.
- [ ] A hand-checkable reweighting case is covered in `tests/test_eval_metrics.py`: known per-stratum rates and a known base rate produce the flag rate and precision computed by hand in the test, so a reweighting error cannot pass as a detector result.
- [ ] The baseline is recorded in `backend/eval/baseline-nw-hennepin-2010-2021.md` with the exact command and date, and it reproduces Plan B's failure at the current defaults (`threshold 0.3`, `min_new_area_m2 40`). Record the measured reweighted flag rate against Plan B's 92%; a gap of more than 15 points is itself a finding that must be explained in that file before Task 4 starts, because Tasks 4–7 calibrate their before/after against this number and a much milder local reproduction would under-fix the production defect while still passing every later DoD.
- [ ] The report states which per-year statistic separates the two years most, with the medians that show it.
- [ ] Verify: `cd backend && uv run pytest -q tests/test_eval_metrics.py && uv run ruff check . && uv run mypy`

### Task 3: Visual audit of a stratified subset

**Objective:** Render base/target chip pairs for a stratified sample of the labelled set, inspect them, and record per-parcel corrections plus an overall label-noise rate. Every accuracy number after this task is reported next to that rate, so a reader can tell detector error from assessor-record error.

**Files:**

- Create: `backend/src/ptax/eval/chips.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `backend/src/ptax/eval/metrics.py`
- Create: `backend/eval/audit-nw-hennepin-2010-2021.json`
- Modify: `backend/eval/README.md`
- Test: `backend/tests/test_eval_metrics.py`

**Key Decisions / Notes:**

- `ptax-eval chips <set.json> --sample 40 --seed 20260922` writes, per sampled parcel, one PNG with the base year left and the target year right at a common scale, plus a contact sheet tiling all of them and an index JSON mapping grid position → `PID` and stratum. Draw evenly across the three strata — roughly 13 each — so the noise estimate has support in every one; the set is already equal-quota per stratum, so an even draw needs no further weighting here.
- Write PNGs with rasterio's PNG driver over a numpy array — no new dependency. Chips land in gitignored `backend/eval/out/chips/`; only the audit JSON is committed.
- The audit records, per inspected parcel, `agreed` / `corrected` with a reason drawn from a closed set: `addition_or_outbuilding` (negative that really did gain structure), `teardown_rebuild` (positive that had a building in both years), `geometry_mismatch` (parcel split or re-platted since the base year), `imagery_unclear`, `other`. A closed set keeps the noise rate computable instead of prose.
- Corrections feed `metrics.py` through an optional `--audit` argument: metrics are then reported twice, once on raw labels and once on audited labels, with the disagreement rate printed between them. Never silently replace one with the other.
- Combine the per-stratum disagreement rates with the same base-rate weights Task 2 uses, so the reported noise rate is a county-level figure comparable with the county-level precision beside it. State it as an estimate from an audited sample of 40, with its per-stratum counts — not as a property of the whole set.

**Definition of Done:**

- [ ] `ptax-eval chips backend/eval/nw-hennepin-2010-2021.json --sample 40 --seed 20260922` writes 40 chip pairs and a contact sheet in which base and target are visually distinguishable and the parcel fills a usable part of the frame.
- [ ] `backend/eval/audit-nw-hennepin-2010-2021.json` holds a verdict for all 40 with a reason from the closed set for each correction, and records the resulting per-stratum and overall label-noise rate.
- [ ] `ptax-eval score backend/eval/nw-hennepin-2010-2021.json --audit backend/eval/audit-nw-hennepin-2010-2021.json` prints raw metrics, audited metrics, and the disagreement rate between them.
- [ ] Verify: `cd backend && uv run pytest -q tests/test_eval_metrics.py && uv run ruff check . && uv run mypy`

### Task 4: Normalise the year-level bias in the built-up mask

> **Runs after Task 6** (see `## Deviations`). Plan B's +0.571 gap does not reproduce: the cross-resolution pair measures +0.015 and the same-resolution control +0.160, so there is no resolution bias to remove until the classifier can tell a roof from bare ground. Its DoD below is measured against Task 6's output, not against the Task 2 baseline.

**Objective:** Compare both years at one common effective ground sample distance rather than at whatever smoothness each capture happens to have, and record that GSD in `indicators`, so a cross-resolution year pair cannot manufacture built-up area from the resampling alone. Measured on both the cross-resolution pair and the same-resolution control, so a gain cannot come from having destroyed the signal.

**Files:**

- **None — closed without implementation** (see `## Deviations`). Had it run it would have touched `backend/src/ptax/detection/detector.py:170`, `backend/src/ptax/imagery/reader.py:113`, `backend/src/ptax/detection/run.py:61`, `backend/src/ptax/api/imagery.py:424`, `backend/src/ptax/eval/cache.py:1` and `backend/tests/test_detector.py:1`; none were changed for this task, and `api/imagery.py` was not changed by this plan at all.

**Key Decisions / Notes:**

- Add `native_resolution_m: float` to `ParcelRaster` (`backend/src/ptax/detection/detector.py:20`), supplied by the caller: `detection/run.py` passes the `ImageryYear.resolution_m` it already reads at `backend/src/ptax/detection/run.py:87`, `api/imagery.py:424` passes the asset's, and the harness passes the cache manifest's. Defaulting it to `resolution_m` would silently reintroduce the bug for any caller that forgot, so make it required.
- Normalise inside `ClassicalDetector`, not inside `read_parcel` as Plan B's draft suggested: the classifier is what is biased, and keeping the reader unchanged leaves the tile and preview endpoints alone. Low-pass each year's bands to the coarser of the two native GSDs before `_classify`, using the box mean already available as `_box_mean` (`backend/src/ptax/detection/detector.py:166`).
- Add `effective_gsd_m`, `base_native_gsd_m` and `target_native_gsd_m` to the indicators dict so a stored run row shows what it was actually compared at. `indicators` is JSONB, so new keys need no migration — but the harness reads the same keys, so add them in one place.
- If Task 2's distribution report showed brightness or greenness rather than texture as the dominant separator, do Task 5 first and note the swap under Deviations.
- Update `tests/test_detector.py`'s `_field` / `_with_roof` builders for the new required field; leave their assertions alone here — re-baselining fixtures is Task 8, and a fixture assertion that breaks in this task is evidence to carry there, not something to loosen now.

**Definition of Done:**

- [ ] `ptax-eval score backend/eval/nw-hennepin-2010-2021.json --audit ...` shows the median gap between `target_builtup_frac` and `base_builtup_frac` no larger than it was at the end of Task 6, and the flag rate no higher. The Task 2 baseline gap of +0.015 is too small to improve on by a third, so the criterion is no-regression plus a recorded measurement — and if the gap at the end of Task 6 is still under 0.05 on both sets, record that this task had no bias left to remove rather than manufacturing an improvement.
- [ ] On the control set `backend/eval/nw-hennepin-2013-2017.json` (both years 1.0 m), recall drops by no more than 5 percentage points and PR-AUC does not drop at all — the guard against "improving" by detecting less.
- [ ] A stored result carries `effective_gsd_m`, `base_native_gsd_m` and `target_native_gsd_m` in `indicators`.
- [ ] Before and after numbers for both sets are appended to `backend/eval/README.md`.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy`

### Task 5: Radiometric normalisation between the two years

**Objective:** Stop applying absolute brightness and vegetation cutoffs to two captures with different sensors, seasons and sun angles, by normalising the target year's radiometry to the base year's before any threshold is applied. Measured on both sets, as in Task 4.

**Files:**

- Modify: `backend/src/ptax/detection/detector.py`
- Modify: `backend/src/ptax/detection/run.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `backend/eval/README.md`
- Test: `backend/tests/test_detector.py`

**Key Decisions / Notes:**

- Normalise per parcel, over valid non-nodata pixels of both years, before `_classify` derives any mask. Per parcel rather than per asset keeps it independent of how assets happen to tile, and it is the only scope the detector has — `compare` never sees more than one parcel.
- Prefer matching the target's brightness distribution to the base's by a robust linear fit (median and interquartile range, not mean and standard deviation) so a genuinely changed parcel — the positives, where a large bright roof appears — does not drag its own normalisation and erase the change it should have reported. Record the fitted gain and offset in `indicators` as `radiometric_gain` and `radiometric_offset`.
- Normalise the bands that feed the cutoffs: brightness for `BRIGHT_T`, and the NDVI/ExG inputs for `VEG_T_NDVI` / `VEG_T_EXG`. Leave `TEXTURE_T` on the Task 4 normalised scale.
- A parcel with too few valid pixels to fit robustly (under a few hundred) keeps the unnormalised values and records `radiometric_gain: null`, rather than fitting to noise.
- Add a detector unit test for the property that matters and does not depend on real imagery: a uniform brightness/NDVI shift applied to an unchanged parcel produces near-zero new built-up area, where today it produces a large one.

**Definition of Done:**

- [ ] A synthetic unchanged parcel with a uniform +40 brightness and −0.08 NDVI shift applied to the target year scores below 0.05, and the same parcel without normalisation scores far higher — the test fails against the Task 4 detector and passes against this one.
- [ ] On `nw-hennepin-2010-2021`, flag rate and precision both improve against the Task 4 numbers, or the task records that radiometry was not a material contributor with the measured evidence.
- [ ] On the control set, recall drops by no more than 5 percentage points and PR-AUC does not drop.
- [ ] `radiometric_gain` and `radiometric_offset` appear in `indicators`, null where the fit was skipped.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy`

### Task 6: Re-derive the built-up / vegetation classification

> **Runs first of Tasks 4-7** (see `## Deviations`). The Task 2 baseline shows this is where the defect is: vacant parcels read 0.949 → 1.000 built-up, real construction runs 0.677 → 0.569, and precision 0.0442 sits below the 0.0599 base rate. The harness has already answered the "re-tune or re-derive" question — brightness plus smoothness does not separate roofs from bare ground, so this task starts from re-deriving rather than from a constant sweep.

**Objective:** Make the built-up cue mean "structure" rather than "bright, smooth and not green", so that building a house raises the built-up fraction instead of lowering it. Extend the classification until it does that or until a measured ceiling says it cannot. The harness — not argument — decides which.

**Files:**

- Modify: `backend/src/ptax/detection/detector.py`
- Modify: `backend/eval/README.md`
- Test: `backend/tests/test_detector.py`

**Key Decisions / Notes:**

- The constants at `backend/src/ptax/detection/detector.py:67` (`VEG_T_NDVI`, `VEG_T_EXG`, `BRIGHT_T`, `TEXTURE_T`, `TEXTURE_WINDOW`) are not the problem and a sweep over them cannot fix it: no brightness cutoff separates a roof from a ploughed field, because both are bright, smooth and non-vegetated. Sweep them only to pick an operating point *after* the cue itself carries structure information.
- Add hand-derived cues inside `ClassicalDetector`: NIR-derived indices beyond plain NDVI (NAIP is 4-band, so the signal is already read and roofs and soil separate in NIR where they do not in the visible), and edge or shape structure — a roof is a compact region with long straight boundaries, bare soil is not. Train nothing; a learned segmentation model stays out of scope.
- **The falsifiable check that the cue now means "structure":** on the labelled set, the median `target_builtup_frac` − `base_builtup_frac` must be **positive for `positive` parcels** and near zero for `negative_old`. Today it is −0.108 for positives, which is why the detector ranks untouched lots above new houses. Get that sign right and the ranking metrics follow; leave it wrong and no threshold can help.
- Record the sweep, not just the winner: the grid searched, the metric at each point, and why the chosen point was chosen. A single set of new constants with no sweep behind it is how the current values got here.
- Report the ceiling honestly if one is hit. If the best measured classification cannot reach ≤ 2% flagged at ≥ 60% precision, the plan keeps the best measured detector, records the ceiling with its evidence in `backend/eval/README.md`, and names that as the trigger for the ML plan. Completion does not depend on reaching a number the imagery may not support.
- Any new cue must be cheap enough for a per-parcel hot path — `score_parcel` runs once per parcel for a whole county. Reuse the integral-image machinery at `backend/src/ptax/detection/detector.py:157` rather than introducing a per-pixel Python loop, and note the per-parcel cost of anything added.

**Definition of Done:**

- [ ] The built-up cue points the right way: median `target_builtup_frac` − `base_builtup_frac` is positive for the `positive` stratum (from −0.108 at baseline) and `negative_future` parcels no longer read as near-fully built-up (from 0.949 → 1.000).
- [ ] Reweighted precision exceeds the county base rate on both sets — the detector is at least better than random, which it is not at baseline (0.0442 against 0.0599).
- [ ] The parameter sweep and its metric at each point are recorded in `backend/eval/README.md`, with the selected point and the reason.
- [ ] On `nw-hennepin-2010-2021` with audited labels, either the target operating point is met at some (threshold, min_new_area_m2) pair, or the measured best is recorded alongside an explicit statement of the ceiling and what evidence establishes it.
- [ ] On the control set, PR-AUC is at least its Task 5 value.
- [ ] Per-parcel scoring time over the cached set has not grown by more than 2x against Task 5, measured and recorded.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy`

### Task 7: A score that ranks, and re-tuned defaults

**Objective:** Replace the saturating score with one whose range carries ranking information, so `threshold` discriminates across its whole 0–1 range and Plan C's queue ordering means something, then set the shipped defaults to the operating point the harness measured.

**Files:**

- Modify: `backend/src/ptax/detection/detector.py`
- Modify: `backend/src/ptax/api/runs.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `frontend/src/api/runs.ts`
- Modify: `backend/eval/README.md`
- Test: `backend/tests/test_detector.py`
- Test: `backend/tests/test_runs.py`

**Key Decisions / Notes:**

- The present `score = 1 - exp(-(new + 0.25·veg_loss) / 200)` at `backend/src/ptax/detection/detector.py:111` reaches ~1.0 by a few hundred m², so almost every real parcel lands in the flat tail. Recalibrate the shape against the observed distribution of `new_builtup_m2` on the labelled set so the scores of scored parcels spread across the range instead of piling at 1.0, and keep it monotone in `new_builtup_m2` so a bigger structure never ranks lower.
- Keep the score in 0–1 and keep `candidate = score >= threshold and new_builtup_m2 >= min_new_area_m2`. The API contract, the `runs.threshold` column and the Runs page form all assume both; this task changes the mapping, not the shape of the contract.
- `DEFAULT_THRESHOLD` and `DEFAULT_MIN_NEW_AREA_M2` are duplicated in `backend/src/ptax/api/runs.py:19` and `frontend/src/api/runs.ts:25`. Both move together to the measured operating point; leaving the frontend behind would silently start every operator run at the wrong parameters.
- Sweep `threshold` across the whole range on the labelled set and record the flag rate at each step. A score that ranks produces a smoothly falling curve; the current one produces a cliff. That curve is the evidence for this task.
- Add a detector unit test that ranking is preserved: parcels with 40, 120, 300 and 900 m² of new built-up area receive strictly increasing scores, none of them within 0.02 of 1.0.

**Definition of Done:**

- [ ] Four synthetic parcels at 40, 120, 300 and 900 m² of new built-up area produce strictly increasing scores, all below 0.98.
- [ ] Sweeping `threshold` from 0.1 to 0.9 on `nw-hennepin-2010-2021` produces at least five distinct flag rates, and the curve is recorded in `backend/eval/README.md`.
- [ ] At the new `DEFAULT_THRESHOLD` / `DEFAULT_MIN_NEW_AREA_M2`, the audited metrics meet the target operating point in `## Global Constraints`, or record the measured best with the Task 6 ceiling statement.
- [ ] `backend/src/ptax/api/runs.py` and `frontend/src/api/runs.ts` carry the same two values.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy && cd ../frontend && pnpm build && pnpm lint`

### Task 8: Re-baseline the synthetic fixtures and Plan B's tests

**Objective:** Bring the synthetic fixtures and Plan B's detector and run tests back into agreement with the new behaviour, keeping the assertions that still describe a real contract and replacing the ones that only described the old thresholds. The fixtures stay useful as fast wiring tests; they stop being the accuracy evidence.

**Files:**

- Modify: `backend/tests/test_detector.py`
- Modify: `backend/tests/test_runs.py`
- Modify: `backend/tests/fixtures/make_imagery_fixtures.py`
- Modify: `backend/tests/fixtures/imagery/naip_2021.tif`
- Modify: `backend/tests/fixtures/imagery/naip_2023.tif`
- Modify: `backend/tests/fixtures/imagery/ortho_2025_partial.tif`
- Modify: `backend/eval/README.md`

**Key Decisions / Notes:**

- The fixture generator at `backend/tests/fixtures/make_imagery_fixtures.py` renders both years at the same GSD and radiometry, so after Tasks 4 and 5 it exercises neither normalisation. Give the 2023 render a deliberate brightness and NDVI offset from 2021 so the fixture run covers the radiometric path, and keep the roof geometry (`EXISTING_ROOFS`, `NEW_ROOFS_2023`, `NEW_ROOFS_2025`) exactly as it is — `test_detector.py:167` and the E2E scenarios name those parcel numbers.
- `test_fixture_years_flag_exactly_the_planted_roofs` (`backend/tests/test_detector.py:146`) stays as the wiring contract: parcels 3, 7, 12 for 2021 → 2023 and parcel 1 for 2023 → 2025. If the new defaults no longer flag them, that is a real finding — carry it back to Task 7 rather than relaxing the assertion.
- The score-range assertions (`0.7 <= result.score <= 0.9` at `backend/tests/test_detector.py:62`, `result.score >= 0.7` at `:90`) encode the old saturating curve. Re-derive them from the new calibration and say in a comment what the expected value corresponds to in m², so the next change can tell a recalibration from a regression.
- Regenerate the three fixture COGs with `make imagery-fixtures` and keep each under the generator's 1 MB assertion.
- Note in `backend/eval/README.md` that the fixtures test wiring and the evaluation sets test accuracy, so a future contributor does not tune against the fixtures again.

**Definition of Done:**

- [ ] `make imagery-fixtures` regenerates all three COGs, each under 1 MB, and the 2023 fixture differs from 2021 in brightness and NDVI as well as in roofs.
- [ ] `test_fixture_years_flag_exactly_the_planted_roofs` passes unchanged in its expected parcel sets at the Task 7 defaults.
- [ ] Every re-derived score bound in `backend/tests/test_detector.py` carries a comment naming the area it corresponds to.
- [ ] TS-001 passes against the local stack.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy`

### Task 9: Re-run on the deployed stack and record the operating point

**Objective:** Confirm the measured improvement survives real ingest on AWS — the same reproduction Plan B's Task 10 ran, with the same tenant and the same two NAIP years — and record the operating point in the README. This cannot run in the implementing session, which has no AWS credentials.

**Owner:** User

**User Action:** With AWS credentials configured, run `cd infra && pnpm cdk deploy --all`, sign in at the ALB URL as the provisioned admin, ensure a real county parcel layer and NAIP 2010 and 2021 are ingested and `ready`, then start a `2010 → 2021` run at the shipped defaults and report the run's processed / candidate / skipped counts and the score distribution of the top 20 candidates.

**Files:**

- Modify: `README.md`
- Modify: `backend/eval/README.md`

**Key Decisions / Notes:**

- Plan B's AWS Verification section (`docs/plans/2026-09-21-imagery-and-detection.md:551`) documents the exact tenant, ALB, years and commands; reuse them so the before and after are comparable rather than merely both true.
- `README.md` gains a "Verified detector accuracy" note under the existing "Verified deployment" heading: date, county, year pair, flag rate, and the labelled-set precision with its label-noise rate beside it.
- The deployed run scores whatever parcels the tenant has, which is not the labelled AOI, so its candidate rate is a sanity check on the offline number and not a second measurement of precision. Say so in the note rather than presenting one as the other.
- The tolerance below is 5 points, not 1: the deployed run scores a different county sub-area with its own construction history, so an exact match would be luck. What the check can prove is that the ingest path did not undo the fix — a 90% flag rate after Task 7 measured 2% offline is an ingest-path difference (resampling, asset tiling, or year resolution metadata) and belongs back in Task 4, not in a README caveat.

**Definition of Done:**

- [x] The `2010 → 2021` run on the deployed stack reaches `succeeded` with a candidate rate in the single-digit percent range: **2 of 25, 8%**, 25 processed, 0 skipped. ~~and within 5 percentage points of the Task 7 reweighted flag rate~~ — **amended, criterion invalid as written.** The deployed tenant holds 25 synthetic fixture parcels; the Task 7 figure (30.2%) is reweighted to the Hennepin county base rate over a stratified 300-parcel labelled sample. The two describe different populations, so the 5-point band compares numbers that were never comparable. What the run does establish is that the ingest path agrees with the harness in direction and magnitude: 92% → 8% flagged on identical parcels and imagery.
- [x] ~~TS-002 passes.~~ **Steps 1–2 pass; step 3 is not executable and is deferred to Plan C.** The run was started and reached `succeeded` through `POST /api/runs` rather than the Runs page. Step 3 requires opening a candidate list ordered by score, and there is no per-parcel endpoint — `/api/runs/{id}/parcels` returns 404, because the review queue is Plan C's scope (`docs/plans/2026-09-21-review-workflow.md:1`). TS-002 should be re-run there, where the UI it describes exists.
- [x] `README.md` records the verified operating point with its date (2026-09-22), county (Demo County deployed; Hennepin for the labelled measurement), year pair (2010 → 2021) and the **measured 8.0% county-weighted label-noise rate**.
- [x] Verify: run `6a72d085-8441-44a3-b1d8-e1c6e6f152a5` observed `succeeded` with 25 processed / 2 candidates / 0 skipped via `GET /api/runs/{id}` on the ALB, corroborated by the worker log (`done: 25 parcels, 2 candidates, 0 skipped`). Not re-runnable now: the supplied STS credentials were deleted from the session scratchpad after use.

### Task 10: Visual labels for all 300 parcels

**Objective:** Replace `BUILD_YR` as the evaluation set's ground truth with a per-parcel visual verdict read from the image pair, because the app compares a base image against a new one and looks for improvements — a question `BUILD_YR` does not answer. Keep the existing stratified sampling and its inclusion probabilities; only the truth each parcel carries changes.

**Files:**

- Modify: `backend/src/ptax/eval/cli.py`
- ~~Modify: `backend/src/ptax/eval/chips.py`~~ — **no change needed**, see `## Deviations`
- Create: `backend/eval/visual-labels-nw-hennepin-2010-2021.json`
- Modify: `backend/eval/README.md`
- Test: `backend/tests/test_eval_metrics.py`

**Key Decisions / Notes:**

- The label is **"did a structure or improvement appear between the two captures"** — a house, garage, shed, barn, large addition or comparable built work. A teardown-rebuild showing a building in both years is **not** an improvement; nor is a field changing colour, a pond drying, or a parcel being graded without anything built on it.
- `chips` gains a batch mode rendering the whole set as numbered contact sheets with an index, because 300 individual images is not a practical read. Cell size must stay large enough to judge a small outbuilding — the earlier 8-cell sheet at ~1700 px wide was legible and is the reference.
- Record every verdict with its reason from a closed set, as Task 3 did, so the rate is computable rather than prose: `structure_added`, `addition_or_outbuilding`, `no_change`, `teardown_rebuild`, `ground_change_only`, `unclear`.
- Keep the `BUILD_YR` stratum on every parcel. It is still the sampling design and still carries the reweighting; it simply stops being the truth.

**Definition of Done:**

- [x] `backend/eval/visual-labels-nw-hennepin-2010-2021.json` carries a verdict and a closed-set reason for all 300 parcels, each recording its `BUILD_YR` stratum alongside the visual label. All 30 contact sheets were read; 99 of 300 parcels show an improvement, 3 are `unclear`.
- [x] The file records how far the two disagree, per stratum and overall: **31 of 300 (10.3%)** — `positive` 16, `negative_old` 14, `negative_future` 1. The disagreement runs in **both** directions, which the earlier 39-parcel audit's 8.0% one-directional noise rate could not express.
- [x] Verify: `cd backend && uv run pytest -q` 136 passed / 1 skipped; `ruff check` clean; `mypy` 7 pre-existing Plan B errors, none in `src/ptax/eval/`.

### Task 11: Re-measure against the visual labels

**Objective:** Re-derive every accuracy figure in this plan from the visual labels, and re-check whether the measured ceiling survives. The sampling stratum keeps supplying the reweighting; the visual verdict supplies the truth.

**Files:**

- Modify: `backend/src/ptax/eval/metrics.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `backend/src/ptax/eval/dataset.py`
- Modify: `backend/eval/README.md`
- Modify: `README.md`
- Modify: `Makefile`
- Test: `backend/tests/test_eval_metrics.py`
- Test: `backend/tests/test_eval_dataset.py`
- Test: `backend/tests/test_eval_cli.py`

**Key Decisions / Notes:**

- `ScoredParcel` must separate **sampling stratum** (weights) from **outcome label** (truth); `summarise` currently infers truth from the stratum, which is exactly the conflation that made `BUILD_YR` the ground truth by accident.
- The county base rate has to be re-estimated: the 5.99% figure is the `BUILD_YR` positive rate, and the prevalence of *visible improvement* is a different, larger number. Estimate it as the stratum-weighted mean of the visual-positive rate within each stratum — the stratified design supports exactly this.
- Re-run the Task 7 threshold curve and the 2-D operating-point sweep against the new labels. **If a usable operating point now exists, the ceiling finding is withdrawn**, not softened, and `README.md` plus `backend/eval/README.md` are corrected rather than annotated.

**Definition of Done:**

- [x] `ptax-eval score --labels <file>` reports all of them against the visual labels and prints the re-estimated base rate beside `BUILD_YR`'s. `ScoredParcel` now carries `stratum` (weight) and `positive` (truth) separately; leaving `positive` unset reproduces the old behaviour exactly.
- [x] The no-imagery baseline is re-run against the visual labels and still reported: its average precision falls from **0.4793 to 0.2578**, so most of what it "detected" was an artefact of what `BUILD_YR` records.
- [x] Both READMEs state the re-measured figures. **The ceiling is re-derived, not withdrawn** — the 2-D sweep is still empty at ≤2% flagged / ≥60% precision, and precision is exactly 0.0000 at every flag rate small enough to be a queue.
- [x] Verify: `cd backend && uv run pytest -q` 136 passed / 1 skipped; `ruff check src tests` clean; `mypy src` 7 pre-existing Plan B errors, none in `src/ptax/eval/`.

## Tasks 10-11 Results (2026-09-22)

**`BUILD_YR` was the wrong ground truth, and the measurement now says so with a number.**
All 300 parcels were read by eye from their base/target chip pairs. `BUILD_YR` disagrees
with the imagery on **31 of 300 (10.3%)**, in both directions:

- **16 `positive` parcels show no improvement.** Nearly all carry `BUILD_YR = 2021` against
  a **2021-06-18** capture: the house is recorded in the capture year but post-dates the
  photograph. Sheet 8 is ten consecutive examples of graded, platted, empty lots.
- **14 `negative_old` parcels visibly improved** — a machine shed, an outbuilding, a pool,
  a construction site finished into a house. `BUILD_YR` cannot see any of it, because a
  parcel's principal-structure year does not change when it gains a second building.

**The base rate was wrong by a factor of three.** `negative_old` holds 92% of the county's
labelled parcels, so a 14% improvement rate there dominates: the county rate of *visible
improvement* is **0.1795** against `BUILD_YR`'s **0.0599**.

**Re-measured at the shipped defaults** (threshold 0.3, 37.2 m²), vs `BUILD_YR` → vs visual
labels: base rate 0.0599 → **0.1795**; precision 0.0794 → **0.2874**; recall 0.4000 →
0.4828; AP 0.0592 → **0.2236**; no-imagery baseline AP 0.4793 → **0.2578**. Flag rate is
unchanged at 0.3016 (the detector did not change, only what it is judged against). The
detector is a **1.6× lift over chance**, up from 1.3×, and the no-imagery baseline loses
nearly half its apparent skill — most of it was predicting what the assessor records, not
what the ground looks like.

**The ceiling is re-derived, not withdrawn.** The full 2-D sweep (threshold 0.0–0.6 ×
minimum structure 37–800 m²) still contains **no point at ≤2% flagged with ≥60%
precision**; the best precision anywhere on the grid is 0.287 at a 30% flag rate. The shape
of the failure is unchanged and now stated against the right truth: at threshold 0.6 the
flag rate is 1.86% — inside the target — and **precision is exactly 0.0000**. The queue
reaches a workable size only by emptying itself of real construction, and the detector
fills the top of its own ranking with its largest false structures (precision @ top 1% =
0.0000 against the baseline's 1.0000). The trigger for the learned detector stands.

**Withdrawn:** the earlier suggestion that an ML evaluation set be matched or banded on
parcel size. Parcel size is irrelevant to the application — it must find improvements on a
parcel of any size — and banding on it would build a set that does not resemble the county.
The correct lesson is the one this task demonstrates: label the imagery, not the records.

**Verification (changes review): one finding applied.** The reviewer returned compliance
high, quality high, goal achieved, 2/2 truths verified, no `must_fix`. Fixed: `apply_audit`
in `backend/src/ptax/eval/metrics.py` rebuilt each corrected `ScoredParcel` by naming its
fields, which silently dropped the new `positive` field — so running `ptax-eval score` with
`--audit` *and* `--labels` together reverted every audit-corrected parcel to
stratum-as-truth, reinstating exactly the conflation Task 11 removed. It corrupted no
published figure (no documented command combined the two flags), but it was a real defect
in code this task touched and nothing covered it. Now `dataclasses.replace(parcel,
stratum=target)`, so every present and future field survives re-stratification, with a test
asserting the eye's verdict outlives a stratum correction. Verified at runtime: the two
flags together now hold recall at 0.4828 across the audit while flag rate and precision
move with the corrected weights, which is the expected signature.

**Deviations (Tasks 10-11):**

- Task 10 (tactical): `backend/src/ptax/eval/chips.py` needed no change — the `--all`
  contact-sheet mode and `letterbox` had already been added under Task 3's deviations, so
  the sheets this task reads were producible as shipped.
- Task 11 (tactical): the threshold curve and 2-D sweep are computed post-hoc from a single
  scored run rather than by re-scoring per grid point. `structure_m2` depends on neither
  `threshold` nor `min_new_area_m2`, so one run's `indicators` supports the whole grid
  exactly; re-scoring 300 parcels per point would have produced identical numbers.
- Task 11 (tactical): `make eval-score` now passes `--labels` by default (`Makefile`, added
  to the task's `Files:` block). Left alone it would have kept printing `BUILD_YR` figures
  from the repository's headline scoring command while both READMEs said those are not the
  truth — a stale-doc defect introduced by this very task. `EVAL_LABELS=` restores the old
  behaviour.
- Task 11 (tactical): the re-estimated base rate landed in `backend/src/ptax/eval/dataset.py`,
  not in `metrics.py` as the `Files:` block assumed. `VisualLabels`, `read_visual_labels`
  and `improvement_base_rate` read and weight the label file, which is set-shaped data;
  `metrics.py` is pure arithmetic over already-labelled parcels and importing a file reader
  into it would have inverted the existing dependency direction. `backend/tests/test_eval_dataset.py`
  covers them. Both paths added to the task's `Files:` block.
- Task 11 (tactical): added `backend/tests/test_eval_cli.py` for `_apply_visual_labels`,
  which joins a scored parcel to its by-eye outcome. It lives in `cli.py`, not in
  `metrics.py` where the plan's `Files:` block put the test, and getting the join wrong
  would silently print `BUILD_YR` figures under a visual-label heading.

## E2E Results

Executed 2026-09-22 against the local stack (PostGIS/MinIO/cognito-local via `make dev-up`,
API and worker restarted onto this plan's code, Vite on 5175) with Chrome.

| Scenario | Priority | Result | Fix Attempts | Notes |
|----------|----------|--------|--------------|-------|
| TS-001 | Critical | **PASS** | 0 | Signed in as `admin@demo.test`; `2021 → 2023` at the **shipped defaults** read `succeeded · 25 processed · 3 candidates · 0 skipped · threshold 0.3 · min 37.2 m²`. The UI showing `min 37.2 m²` where the two prior runs show `min 40 m²` is the code-identity proof. Step 4's assertion was verified from `run_parcels` rather than the UI (see below): candidates are parcels 000003 / 000007 / 000012 — exactly the planted roofs — scoring 0.4286 / 0.4286 / 0.4228 with `structure_m2` 300 / 300 / 293, above every non-candidate at 0.0000, and **no parcel at or above 0.99**. |
| TS-002 | Critical | **PARTIAL** | 0 | Steps 1–2 executed on the deployed stack via the API: `2010 → 2021` at shipped defaults reached `succeeded`, 25 processed, **2 candidates**, 0 skipped (against Plan B's 23). Step 3 not executable — see below. |

**Both scenarios' final step is blocked on the same missing surface.** TS-001 step 4 and
TS-002 step 3 both call for opening a candidate list ordered by score, and there is no
per-parcel endpoint or view: `GET /api/runs/{id}/parcels` returns 404 because the review
queue is Plan C's scope (`docs/plans/2026-09-21-review-workflow.md:1`). Plan D's own
`## Out of Scope` forbids adding it. TS-001's assertion was therefore checked against the
`run_parcels` rows directly; TS-002's could not be checked at all, since the deployed
tenant's 25 fixture parcels carry no labels. Both should be re-run under Plan C once the
queue UI exists.

## Verification Gaps

| Gap | Type | Severity | Affected Files | Fix Description |
|---|---|---|---|---|
| Ground truth answers the wrong question | correctness | **critical** | `backend/eval/*.json`, `backend/src/ptax/eval/metrics.py`, `backend/eval/README.md`, `README.md` | `BUILD_YR` records whether the assessor logged a **principal structure** in the window. The app's task is "did an improvement appear between these two images" — which includes sheds, garages, additions and outbuildings that leave `BUILD_YR` untouched, and excludes teardown-rebuilds that change it while the imagery shows a building in both years. Every accuracy figure in this plan is therefore measured against a proxy, not against the product's task. |
| Precision is an unquantified lower bound | measurement | **critical** | `backend/eval/README.md`, `README.md` | At the shipped defaults the detector flags **30 of 100** `negative_old` parcels, and that stratum is 92% of the county by weight. If a meaningful share of those flags are genuine improvements invisible to `BUILD_YR`, reported precision (0.079) is understated: at 25% it becomes 0.31, at 60% it becomes 0.63 and **meets the target**. The rate among *flagged* parcels was never measured — the 7.7% audit figure is the rate across all `negative_old`, which is a different quantity. |
| The ceiling claim may not survive | correctness | **critical** | `backend/eval/README.md`, `README.md`, `docs/plans/2026-09-22-detector-accuracy.md` | "No usable operating point" and "trigger for the learned detector" both rest on the precision figure above. They must be re-derived against visual labels rather than restated. |

## Deviations

- **Verification (user-agreed): `BUILD_YR` is replaced as ground truth by visual labels.** The user's correction: "the year built label doesn't matter to our app, the app takes a base image and a new image and compares and tries to find new improvements." All 300 parcels of the primary set are re-labelled by eye from their image pairs as improvement-appeared / no-improvement, and Tasks 2 and 5–7 are re-measured against those labels. The **sampling** design is unchanged — the 300 were drawn stratified by `BUILD_YR` with recorded inclusion probabilities, and those still carry the reweighting — but the **truth** each parcel carries becomes the visual verdict. `ptax.eval.metrics` therefore has to separate the sampling stratum (for weights) from the outcome label (for truth), which it currently conflates.
- **Verification (user-agreed): the size-matched evaluation set proposed at close-out is withdrawn.** The correct fix for the parcel-size confound was the one already applied at the user's direction — scoring the largest contiguous structure in m², which has no parcel-size term — so nothing can exploit size and the evaluation population needs no surgery. Resampling it would have distorted the set away from the real county to compensate for a defect that was already fixed. The no-imagery baseline stays as the tripwire against size re-entering a future score.

- **Verification (changes review): five findings applied.** The reviewer returned compliance high, quality high, goal achieved, 2/2 truths verified, no `must_fix`. Fixed: the `if __name__ == "__main__"` guard sat between command definitions in `backend/src/ptax/eval/cli.py`, so `python cli.py score` would have reported "no such command" (the packaged `ptax-eval` entry point was unaffected); `backend/tests/test_runs.py` now asserts the run-level radiometric fit reaches a stored `RunParcel.indicators` row, which nothing covered before — writing it exposed a real defect in the test itself, where a one-shot `ScalarResult` had already been exhausted by the preceding loop; and Task 7's DoD-required single-axis threshold curve (0.1–0.9, eight distinct flag rates) is now recorded in `backend/eval/README.md`, where only the 2-D ceiling sweep had been. Documented rather than changed: the chip audit draws 39 not 40 (an even draw across three strata), and Task 6's "within 2x of Task 5" cost criterion was made undefined by the execution reordering.

- **Task 9 (user-agreed): run by the agent, not the user, once credentials were supplied.** The task was user-owned only because this session had no AWS credentials; the user provided temporary STS credentials and asked for the deploy directly. Deployed `PtaxCompute` (image digest change only on the API and Worker task definitions — no schema, topology or data-stack change), after `make image-check` passed on the exact image, and ran 2010 → 2021 at the shipped defaults. **2 of 25 candidates against Plan B's 23 of 25** on identical parcels and imagery. A password was set on the throwaway `naip-verify@example.com` account to obtain a token; the user's own `tholmes4005@gmail.com` was not touched, and the credential and password files were deleted from the session scratchpad afterwards. Recorded in `backend/eval/README.md` and `README.md`.
- **Task 9 (tactical): the deployed run cannot measure precision.** Its tenant carries the 25-parcel synthetic fixture layer over real ground, so there is no `BUILD_YR` and no labels. It verifies that the ingest path agrees with the harness — both show the flag-rate collapse — and it confirms the Task 5 run-level fit executes in production (worker log: `radiometric fit over 25 parcels: gains [0.89, 0.933, 0.333, 1.172]`), which no unit test covers. It is not independent confirmation of the offline 7.9% precision, and the plan does not present it as such.
- **Task 7 (measured ceiling, user-agreed close-out): the classical detector has no usable operating point, and this is the trigger for the learned-detector plan.** Sweeping threshold 0.0–0.6 against minimum structure size 37–800 m², **no pair yields a queue of 6% or smaller with precision above the 0.0599 base rate**; at a 400 m² minimum the flagged set contains no positives at all. The only above-chance regime flags 30–48% of the county. Three independent lines agree: average precision is flat at 0.0369–0.0400 across a 12-point classification sweep; the 2-D operating-point sweep is empty; and **a no-imagery baseline that ranks by inverse parcel size beats every version of the detector** (AP 0.4793 against 0.0592). Recorded in `backend/eval/README.md`. The plan's mechanism repairs stand — vacant land 0.949 → 0.000 built-up, construction delta −0.108 → +0.169, precision moved from below to above the base rate — but the *sufficiency* of a classical cue on 1 m NAIP is now bounded by measurement rather than argument. Tasks 4 and 9 close out under this decision.
- **Task 7 (defect found by the user, and the reason the ceiling is only now visible): the share-based score was beaten by a ranker that reads no pixels.** It measured 1.43% flagged at 96.1% precision and appeared to meet the target. Ranking parcels by **inverse parcel size alone** scores AP 0.4793 and precision@top-1% 1.0000, against the share score's 0.4665 and 0.9629 — because in this AOI new construction sits on subdivided suburban lots (median 1 515 m²) while established parcels include rural acreage (median 8 198 m²), so anything dividing by parcel area inherits that correlation. `ptax-eval score` now prints that baseline on every run beneath the line "a detector that does not beat this has not detected anything", in `backend/src/ptax/eval/cli.py`. Any accuracy figure this plan reported before this deviation that rested on a share-derived statistic is suspect; the mechanism findings, which rest on per-stratum indicator medians, are not.
- **Task 7 (user-agreed, supersedes the share-based score below): the score is the size of the largest *contiguous* new structure, and `min_new_area_m2` becomes that structure's minimum size.** A share of the parcel is the wrong basis for a structure detector: it ties the rating of a house to the size of the lot it sits on, which is why the share-based score was recorded as missing a 300 m² house on a five acre parcel. The measurement that led there was also mis-read — total `new_builtup_m2` ranked near chance (AP 0.052) because it *sums scattered speckle*, not because absolute size is uninformative, and the largest contiguous component was never measured. `ClassicalDetector` gains connected-component labelling over the new built-up mask; `structure_m2` is the largest component's area; the score rises with it so a 300 m² house outranks a 40 m² shed and Plan C's queue keeps a size ordering; a parcel is a candidate when `structure_m2 >= min_new_area_m2`. `min_new_area_m2` keeps its API field, schema column and frontend input but changes meaning from "total new built-up area" to "largest contiguous structure", with the default moving 40 → **37.2 m² (400 ft²)**. Stored runs from before this change carry a number whose meaning has shifted; the only such run is Plan B's AWS verification, which this plan supersedes. Tasks 7 and 8 are re-opened: the 1.43% / 96.1% figures below were measured on the superseded score and must be re-measured, and the fixtures' `FIXTURE_THRESHOLD` workaround may become unnecessary, since a 300 m² planted roof is a real structure far above 37.2 m² however large its parcel.
- **Task 7 (superseded — retained for the measurement, not the decision): the score was the built-up *share* change.** The plan said to recalibrate the existing curve and keep it "monotone in `new_builtup_m2`". Measured threshold-free on the stored indicators, absolute area ranks near chance (average precision 0.052) because a large rural parcel accumulates more of it than a suburban lot gains real roof; the share change ranks at 0.467 with 96% precision in the top 1%. Monotone-in-area was therefore the wrong contract and the score is `(target_builtup_frac − base_builtup_frac) / SCORE_FULL_DELTA`. Vegetation loss no longer feeds it (0.045 on its own — it tracks season) but is still recorded. **This met the target**: 1.43% flagged at 96.1% precision on the primary set, 0.57% at 92.5% on the control. `DEFAULT_THRESHOLD` moved 0.3 → 0.7 in `backend/src/ptax/api/runs.py` and `frontend/src/api/runs.ts`; `DEFAULT_MIN_NEW_AREA_M2` stays 40, which adds precision at no cost to recall.
- **Task 7 (measured ceiling): a share-based score under-weights a small structure on a large parcel.** A 300 m² house is a 0.30 share of a quarter-acre lot but 0.013 of a five-acre one, scoring below any useful threshold. Capping the denominator to compensate was tested and is much worse (average precision 0.4665 → 0.1090), because it restores the absolute-area behaviour that ranked near chance. **On a rural county of large parcels this detector will miss houses.** Recorded in `backend/eval/README.md` as a property of the design rather than of the defaults.
- **Task 8 (tactical): the fixtures run at their own threshold, because their parcels are 5.5 acres.** A planted 20 × 15 m roof is a 0.013 share of a 150 m grid cell and scores ~0.033; reaching the shipped 0.70 there would need a 6 287 m² building. The plan said to carry a fixture failure back to Task 7 rather than relax the assertion — carried back, and the default is right: it is measured on real imagery and meets the target on both sets, while the fixture's parcel scale is unrepresentative. `FIXTURE_THRESHOLD = 0.02` in `backend/tests/test_detector.py` is used by `backend/tests/test_runs.py` too. No assertion was weakened: exactly parcels 3, 7, 12 and parcel 1 are still required, and 3 candidates end to end.
- **Task 5 (user-agreed): the radiometric fit is per *year pair*, not per parcel, and `Detector.compare` gains a `fit` argument.** The plan specified a per-parcel fit and anticipated the risk that a changed parcel would drag its own correction; measured, that risk is real and large — removing the bias cost precision 0.0745 → 0.0528 and recall 0.72 → 0.37 even with the most robust percentile choice. Fitting once across many parcels, where unchanged ground dominates, keeps precision at 0.0713 with recall 0.64 and gives both sets their best average precision so far (0.0413 and 0.0833). This widens the `Detector` seam, which `## Out of Scope` had assumed sufficient as-is: `backend/src/ptax/detection/detector.py` gains `RadiometricFit`, `fit_radiometry` and `paired_samples`, and `backend/src/ptax/detection/run.py` builds the fit from a geohash-spread sample before scoring so production runs match the harness. No schema or API change.
- **Task 5 (tactical): the plan's stated RED — a uniform brightness and NDVI offset — cannot fail against the Task 6 cue.** A contrast cue is by construction immune to a uniform offset, and measurement showed brightness and contrast barely differ between captures anyway; it is **NIR** that moves (179 → 160, and 185 → 130 on the control). The test in `backend/tests/test_detector.py` is an NIR-driven recapture of mottled ground instead, which reproduces the real failure: score 0.044 → 0.785 with zero new built-up area, all of it phantom vegetation loss.
- **Task 6 (tactical): the classification is replaced, not re-tuned, and gains a compactness requirement.** `BRIGHT_T` is gone; `CONTRAST_T`, `CONTEXT_M`, `WATER_NIR` and `MIN_STRUCTURE_M` replace it in `backend/src/ptax/detection/detector.py`, and `_classify` now takes the grid resolution so metre-scale windows convert to pixels. Beyond the plan's text, reported new built-up area must survive an opening at `MIN_STRUCTURE_M` (`_open`): without it a large rural lot accumulated more spurious area than a suburban lot gained real roof — 824 m² against 259 m² — so absolute area ranked untouched acreage above real construction. This is the "compact region" half of the shape cue the task authorised.
- **Task 6 (tactical, recorded rather than fixed): the control set's average precision fell 0.0189 → 0.0175 and its recall 0.9483 → 0.2931.** The 2013–2017 window's structures are smaller and the `MIN_STRUCTURE_M` opening drops them. Its flag rate improved 0.9334 → 0.2140 and precision 0.0240 → 0.0324, so the change is a precision-for-recall trade on the shorter window rather than a regression in signal — but the AP drop is real and is not netted out of the headline. Task 6's DoD asked for control PR-AUC at least at its Task 5 value; Task 5 has not run under the revised order, so this is stated against the Task 2 baseline instead.
- **Task 4 (user-agreed): closed without implementation.** Under the ceiling decision above, tuning stops. Its premise was already refuted at Task 2 (the cross-resolution pair's year gap measured +0.015 against the same-resolution control's +0.160), and after Task 5 the residual is +0.093 against +0.030 — a real but small effect worth at most a few points of a metric that is an order of magnitude short of target. Remediating it cannot change the conclusion, and the plan's Goal Verification explicitly allows a task that did not move its metric to say so rather than be marked done on a code change. `ParcelRaster` therefore keeps its existing fields and no `effective_gsd_m` indicator is added; a learned detector will need the resolution question re-asked on its own terms anyway.
- **Task 3 (tactical): the audit's closed reason set gains `capture_precedes_build`.** Two of the 13 audited positives carry `BUILD_YR = 2021` and show open field in both captures, because the target NAIP capture is **2021-06-18** — a structure recorded in the capture year can post-date the image. Neither `imagery_unclear` nor `other` describes that, and it is systematic rather than incidental: **15 of the 100 sampled positives sit in the boundary year**, which bounds the mode at up to 15% of the stratum. Recorded in `backend/eval/audit-nw-hennepin-2010-2021.json` and applied through `backend/src/ptax/eval/metrics.py`.
- **Task 3 (tactical): chips are scaled toward a target size rather than a fixed zoom.** Parcels in the set span a 60 px suburban lot to a 2300 px strip, so one zoom factor makes half the audit unreadable and the other half enormous. `fit()` in `backend/src/ptax/eval/chips.py` enlarges by replication and reduces by decimation, so every displayed pixel stays a measured pixel.
- **Task 2 (user-agreed): Task 6 runs before Tasks 4 and 5; the execution order is 6 → 4 → 5 → 7.** Task numbers are unchanged so every cross-reference still resolves. The baseline refuted Plan B's diagnosis, which was the premise for doing resolution normalisation first: the cross-resolution pair's median `target_builtup_frac` − `base_builtup_frac` gap measures **+0.015**, not Plan B's +0.571, and the **same-resolution control (2013 → 2017, both 1.0 m) shows +0.160** — ten times larger with no resolution mismatch to blame. What the baseline does show is that `_classify` cannot tell a roof from bare ground: vacant `negative_future` parcels read 0.949 → 1.000 built-up, and real construction runs 0.677 → **0.569** because a finished house replaces graded soil with lawn. Precision 0.0442 sits *below* the 0.0599 base rate, so the detector is worse than random and `precision @ top 1%` is 0. Normalising resolution or radiometry against a classifier pointing the wrong way would fit constants to an artefact — the failure this plan's ordering rationale existed to prevent. Full evidence in `backend/eval/baseline-nw-hennepin-2010-2021.md`.
- **Task 2 (tactical): the county base rate is 0.0599, not 0.0562**, and `county_build_year_counts` now records all three strata rather than `total`/`in_window`. Reweighting a stratified sample needs each stratum's *population* size, because the two negative strata are flagged at very different rates (0.990 and 0.400) and exist in very different numbers (387 280 and 7 760). The denominator also excludes the 27 851 county parcels with no assessor year, which are excluded from the sample too — so the rate now describes the population the sample was actually drawn from. Still within a point of the figure in `## Global Constraints`. Touches `backend/src/ptax/eval/sources.py`, `backend/src/ptax/eval/dataset.py`, `backend/tests/test_eval_dataset.py`, and both committed set files.
- **Task 1 (user-agreed): the imagery cache stores whole NAIP items, not per-parcel crops.** The DoD's equivalence check failed as designed: 10 of 30 reads disagreed with the source, ~90% of samples each, and every failure was the 0.6 m target year read at the 1.0 m comparison grid. The remote COG carries overviews `[2, 4, 8, 16, 32]` and a 96×113 crop carries none, so the crop resampled from full resolution where GDAL resamples from the `/2` overview. `ptax.imagery.cog.to_cog` builds overviews for every stored asset (verified: `[2, 4, 8]` on a 4000×4000 raster), so production reads from an overview too — the crops were feeding the detector pixels production never sees, on the year the whole plan is about. `backend/src/ptax/eval/cache.py` now clips each item to the AOI and writes it with `to_cog`; `backend/src/ptax/eval/cli.py` fetches per item and its `--verify` checks overviews plus full parcel coverage instead of comparing two reads. Four files per set rather than 600.
- **Task 1 (tactical): items are clipped to the AOI plus a 300 m buffer** (`AOI_CLIP_BUFFER_M` in `backend/src/ptax/eval/cache.py`). Parcels are selected by intersecting the AOI box, so edge parcels extend past it; clipping to the box exactly left 12 parcel-years unreadable and 50 with missing pixels inside the parcel. With the buffer both sets verify clean (0 and 0).
- **Task 1 (tactical): the cache is 235 MB for the primary set and 115 MB for the control**, not the ~25 MB the plan estimated. Both are gitignored, so this costs disk rather than repository size.
- **Task 1 (tactical): `uv run mypy` has a pre-existing baseline of 7 errors** in Plan B's untracked code — `backend/src/ptax/parcels/footprint.py:37`, `backend/src/ptax/imagery/naip.py:58`, `backend/src/ptax/api/runs.py:155`, `backend/src/ptax/detection/run.py:53` and `:58`, `backend/src/ptax/api/imagery.py:388`, `backend/src/ptax/api/tiles.py:41`. Attributed by re-running mypy with `backend/src/ptax/eval/` removed: the same 7 errors at the same lines, so this plan's code adds none. They are outside Plan D's authorized scope and are left for the user to decide on; every task's `uv run mypy` step is read against this baseline.

## Autonomous Decisions

- **Evaluation imagery comes from Planetary Computer, not requester-pays S3.** Verified on 2026-09-22 that `planetarycomputer.microsoft.com` serves the identical NAIP COGs (`mn_m_4509352_sw_15_1_20100913` and the 0.6 m 2021 item over the default AOI) under a free SAS token, with no credentials. This session has none, so it is the only way the harness can read real imagery locally. Production is untouched: `NaipStacSource` keeps using Earth Search and `naip-analytic`.
- **Cross-resolution policy: normalise, and record what was normalised to.** Task 4 compares at the coarser of the two native GSDs and writes `effective_gsd_m` into `indicators`, rather than refusing year pairs whose resolutions differ. Refusing would rule out the most valuable comparisons — an eleven-year gap almost always crosses a NAIP resolution change — and the recorded GSD lets a reviewer see what a run actually compared.
- **The harness ships inside `ptax`, behind a `ptax-eval` entry point**, rather than living in `tests/` or a loose script. It imports production reader and detector code, so it needs the package's dependency set and type checking; `make imagery-fixtures` is the precedent for a generator script, but that one imports nothing from `ptax`.
