# Learned Change Detector — Prototype and Decision Gate Implementation Plan

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** A learned building-segmentation detector runs behind the existing `Detector` seam in the evaluation harness, and `ptax-eval score --detector segmentation` reports — against the 300 visual labels — whether it beats the classical detector and the no-imagery baseline, with the result recorded as a ship / no-ship decision for production integration.

Requirements: `docs/prd/2026-09-23-learned-change-detector.md`.

## Out of Scope

- **Production integration** — a per-run detector column, migration, API/Runs-page selection, the model in the worker image, and GPU compute. By decision (2026-09-23) this is a follow-up plan, written only if this plan's gate passes.
- **Training on the evaluation set.** The 300 visual labels and the `nw-hennepin` AOI are never used for training or for choosing hyperparameters.
- **Retuning the classical detector** — Plan D recorded its ceiling; it stays as the comparison.
- **A zero-shot SAM candidate** — considered and not chosen for the first candidate.

## Approach

**Chosen:** Train a small building segmenter (U-Net, ImageNet ResNet-34 encoder, `segmentation-models-pytorch`) on NAIP with Microsoft building footprints as labels; run it on each year and score the new building area, behind a `SegmentationDetector` that implements the existing `Detector` protocol (`backend/src/ptax/detection/detector.py`).
**Why:** A model trained to see buildings separates roofs from graded soil — the classical detector's measured failure — and needs no change labels, which this project does not have. The cost is a label-noise risk from footprints and a torch dependency, contained to an optional `ml` group so production is untouched. torchgeo's only NAIP-pretrained weights are a large Swin-V2-B backbone (`Swin_V2_B_Weights.NAIP_RGB_SI_SATLAS`), so a small ImageNet-encoder U-Net is the CPU-cheap choice; the Swin backbone is the fallback if it misses.

## Global Constraints

- The evaluation AOI `nw-hennepin` = `(-93.60, 45.16, -93.57, 45.19)` (`backend/src/ptax/eval/dataset.py`) and `backend/eval/visual-labels-nw-hennepin-2010-2021.json` are **never** used for training, validation, or hyperparameter choice. Training AOIs must lie at least 1 km outside it, enforced in code.
- ML dependencies live only in `[dependency-groups] ml` in `backend/pyproject.toml`. The production image build (`uv sync --frozen --no-dev`, `backend/Dockerfile`) must install nothing new.
- Modules that import `torch` are imported lazily, only when the segmentation detector is selected; `ptax.api`, `ptax.worker` and `ptax-eval` with `--detector classical` must import and run without the `ml` group.
- `--detector classical` is the default and reproduces current `ptax-eval score` and `chips` output exactly.
- The learned detector scores with the same curve as the classical one: `score = structure_m2 / (structure_m2 + 400)`, so `threshold` and `min_new_area_m2` mean the same thing for both.
- Footprint labels come from `https://minedbuildings.z5.web.core.windows.net/legacy/usbuildings-v2/Minnesota.geojson.zip` (verified live 2026-09-23, 100,888,148 bytes).

## Context for Implementer

The detector must read two captures of different vintage — `nw-hennepin-2010-2021` pairs 2010 NAIP (1.0 m) with 2021 NAIP (0.6 m), both read onto the run job's 1.0 m comparison grid. A segmenter trained only on recent imagery risks under-detecting roofs in older captures, and then every existing house looks "new". So training draws on **several NAIP years over neighbourhoods built out long before 2010**, where the same footprints are correct in every year. That single choice handles both the footprints' imagery-date uncertainty and the year-to-year sensor shift. Training and validation use chips drawn only from those training AOIs, never from the evaluation AOI.

## Runtime Environment

- CLI only, no service. `cd backend && uv sync --group ml` installs the ML stack locally.
- Training and inference run on the dev machine (Apple M4 Max) via PyTorch's `mps` backend, falling back to `cpu`.
- NAIP and footprints download without AWS credentials (Planetary Computer SAS tokens, public HTTPS), as `ptax-eval fetch` already does.

## File Structure

- `backend/src/ptax/detection/registry.py` (create) — `get_detector(name) -> Detector`; names `classical`, `segmentation`; lazy import of the learned module.
- `backend/src/ptax/detection/learned.py` (create) — `SegmentationDetector`: two segmentations → new-building masks → `ChangeResult`. Takes an injectable `predict(rgb) -> probability` so its logic is testable without torch.
- `backend/src/ptax/learn/__init__.py`, `backend/src/ptax/learn/model.py` (create) — torch-only: U-Net construction, weight loading, `predict`.
- `backend/src/ptax/learn/train.py` (create) — torch-only training loop, validation IoU, model card.
- `backend/src/ptax/eval/footprints.py` (create) — download/cache the Minnesota footprint zip; read by bbox; rasterise onto a grid.
- `backend/src/ptax/eval/training_data.py` (create) — training AOIs, disjointness guard, chip tiling into `.npz` shards.
- `backend/src/ptax/eval/cli.py` (modify) — `--detector` on `score`/`chips`; `train-data` and `train` commands.
- `backend/pyproject.toml`, `backend/uv.lock` (modify) — `ml` dependency group.
- `.gitignore` (modify) — add `backend/eval/models/*.pt` only. `backend/eval/cache/` (footprints, training shards) is already ignored, and the committed `.json` model card must stay tracked.
- `backend/eval/models/segmenter-v1.json` (create) — committed model card: config, data hashes, validation IoU, weights sha256, and the freeze timestamp.
- `backend/src/ptax/eval/metrics.py` (modify) — a stratified bootstrap confidence interval for average precision.
- `backend/eval/README.md`, `README.md` (modify) — the measured result and decision.

## Assumptions

- `torch` and `segmentation-models-pytorch` install for Python 3.13 on macOS arm64 via `uv` — Task 2 depends on this, and its first DoD bullet proves it.
- Planetary Computer serves NAIP for the chosen training AOIs in at least three years among 2010–2021 — Task 3 depends on this, and checks it before tiling.
- **Deliberate substitution, verified 2026-09-23:** the user chose "a small U-Net on NAIP-pretrained torchgeo weights", but torchgeo has no small NAIP-pretrained model — its only NAIP weights (`Swin_V2_B_Weights.NAIP_RGB_SI_SATLAS`) sit on a large Swin-V2-B backbone. The small, CPU-cheap intent is kept with an ImageNet ResNet-34 U-Net; the NAIP Swin backbone is the fallback.
- A ResNet-34 U-Net inferring on a single parcel's comparison-grid raster (no surrounding buffer, as the run job reads it) still segments buildings at the parcel edge well enough — Task 5 depends on it; Task 6 measures the consequence.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The evaluation set leaks into training, making the result self-graded | Medium | High | Task 3's training AOIs pass a coded disjointness guard (≥1 km from `nw-hennepin`); Task 4 picks hyperparameters on the training validation split only and **freezes** the model card (weights sha256 + timestamp) before any evaluation; Task 6 gates only that frozen candidate — any retrain after seeing a score is a new, separately recorded candidate (`segmenter-v2`), never a replacement |
| Footprint labels miss buildings built after the footprints' source imagery | Medium | Medium | Training AOIs are neighbourhoods built out before 2000; Task 3 reports per-tile footprint coverage and excludes tiles where labelled building fraction deviates beyond a stated bound from the tile median |
| The segmenter reads 2010 imagery worse than 2021, inventing "new" buildings | Medium | High | Multi-year training (≥3 NAIP years); Task 4 reports validation IoU **per year**, and Task 6 reports the detector's base-year building fraction on unchanged parcels |
| The model does not beat the baselines | Medium | Medium | That is a valid outcome of a decision gate: Task 6 records the measured best and the named failures, as Plan D recorded the classical ceiling |
| The model trains on 256 px chips with context but infers on parcel-only crops, some smaller than 32 px | Medium | Medium | Task 4 augments with random crop sizes down to 24 px, zero-padded, matching the no-buffer parcel crops the run job produces; Task 6 reports results split by parcel size |
| CPU inference is too slow for county scale | Medium | Medium | Task 6 measures per-parcel CPU inference time and projects a 200 000-parcel run; the figure goes into the gate decision |

## Goal Verification

### Truths

1. The candidate that the gate judges was frozen — weights hashed, settings fixed on the training validation split — before its first score against the visual labels, and no training code path reads the `nw-hennepin` AOI or the visual-label file. The reported score is therefore an independent measurement of that exact candidate.

## Progress Tracking

- [x] Task 1: Detector registry and `--detector` on `ptax-eval score` / `chips`
- [x] Task 2: `ml` dependency group and the footprint label source
- [x] Task 3: Training data — training AOIs, disjointness guard, multi-year chips
- [x] Task 4: Train the building segmenter and write its model card
- [x] Task 5: `SegmentationDetector` behind the `Detector` seam
- [x] Task 6: Measure against the visual labels and record the decision

## Deviations

- Task 3 (tactical): Edina measured 0.820 of dated parcels built by 2000 (teardown-rebuilds carry post-2000 years) and was dropped; rural west-Hennepin candidates (Independence, Medina, Greenfield) measured 0.44-0.72 and were dropped too. `TRAIN_AOIS` in `backend/src/ptax/eval/training_data.py` is Richfield, Minnetonka, Brooklyn Park and Crystal (0.903-0.930). Consequence: training has little open field or bare soil — the classical detector's named false positives — which Task 6 must read for in the ranked chips.
- Task 3 (tactical): training chips are read through `read_parcel_uris` with each chip box standing in for a parcel, so training imagery is resampled exactly as the run job resamples; the label screen is an absolute bound (`SCREEN_MAX_DEVIATION` 0.15 building fraction from the AOI median) rather than a relative one, and the split holds out the trailing 20% of each AOI's tile columns.
- Task 5 (tactical): the test seam for the model load is `ptax.detection.learned._load_predictor` (which imports `ptax.learn.model.load` on call) rather than monkeypatching `ptax.learn.model.load` itself, so the registry wiring test never imports torch. The coverage check is replicated from `ClassicalDetector.compare` rather than refactored out of `backend/src/ptax/detection/detector.py`, which this plan does not modify.
- Task 1 (tactical): `ptax-eval chips --detector` other than `classical` writes under `eval/out/chips/<set>/<detector>/`, and only with `--markup`/`--ranked`; `write_index` records the detector only for detector-aware sheets, so the blind labelling index is unchanged. Files as planned (`backend/src/ptax/eval/chips.py` carries the `write_index` change and is added to Task 1's Files).

## Implementation Tasks

### Task 1: Detector registry and `--detector` on `ptax-eval score` / `chips`

**Objective:** Replace the three hard-coded `ClassicalDetector()` constructions in the evaluation harness with a registry, and add a `--detector` option to `ptax-eval score` and `ptax-eval chips`, defaulting to `classical`. This is the seam every later task plugs into, and it must change nothing for the classical path.

**Files:**

- Create: `backend/src/ptax/detection/registry.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Modify: `backend/src/ptax/eval/metrics.py`
- Modify: `backend/src/ptax/eval/chips.py`
- Test: `backend/tests/test_eval_cli.py`
- Test: `backend/tests/test_eval_chips.py`
- Test: `backend/tests/test_eval_metrics.py`

**Key Decisions / Notes:**

- `get_detector(name: str) -> Detector` in `registry.py`; unknown names raise `ValueError` listing valid names. `segmentation` is registered as a lazy import (the module is written in Task 5), so importing the registry never imports torch.
- Wire it at `backend/src/ptax/eval/cli.py:368` (`_score_set`) and `backend/src/ptax/eval/cli.py:452` (`_chip_detector`). Leave `backend/src/ptax/detection/run.py:259` alone — the run job is production, out of scope.
- Per-parcel results JSON and chips index record the detector name; a scored-output filename includes it so runs of two detectors never overwrite each other.
- `ptax-eval score` also prints a 95% confidence interval on average precision from a **stratified bootstrap** (resample within each sampling stratum, 1000 replicates, fixed seed, weights recomputed per replicate) — `bootstrap_ap_interval` in `backend/src/ptax/eval/metrics.py`. With 99 improved parcels among 300, a point estimate alone cannot say whether a margin is real.

**Definition of Done:**

- [x] `get_detector("classical")` returns a `ClassicalDetector`; `get_detector("nope")` raises `ValueError` naming the valid detectors.
- [x] Importing `ptax.detection.registry` does not import `torch` (asserted via `sys.modules` in a test).
- [x] `bootstrap_ap_interval` is deterministic for a fixed seed, its interval contains the point estimate, and it narrows as a synthetic sample grows.
- [x] `ptax-eval score eval/nw-hennepin-2010-2021.json --labels eval/visual-labels-nw-hennepin-2010-2021.json` with and without `--detector classical` prints identical metrics (precision 0.2874, AP 0.2236 at defaults).
- [x] Verify: `cd backend && uv run pytest tests/test_eval_cli.py tests/test_eval_chips.py tests/test_eval_metrics.py -q && uv run ruff check src tests && uv run mypy src`

### Task 2: `ml` dependency group and the footprint label source

**Objective:** Add the ML stack as an optional dependency group that production never installs, and a footprint source that downloads Microsoft's Minnesota building footprints once, reads them by bounding box, and rasterises them onto a given grid as a boolean building mask.

**Files:**

- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `.gitignore`
- Create: `backend/src/ptax/eval/footprints.py`
- Test: `backend/tests/test_eval_footprints.py`

**Key Decisions / Notes:**

- `[dependency-groups] ml = ["torch", "segmentation-models-pytorch"]` (versions resolved by `uv add --group ml`). Do not add `ml` to `[tool.uv] default-groups`: `uv sync --no-dev` would then install it into the image.
- Cache the zip at `backend/eval/cache/footprints/Minnesota.geojson.zip` (gitignored, like the NAIP cache). Download is skipped when the file exists with the expected byte length.
- Read with `pyogrio.read_dataframe(..., bbox=...)` through `/vsizip/`; rasterise with `rasterio.features.rasterize` onto a caller-supplied transform/shape/CRS, reprojecting from EPSG:4326 — follow the reprojection pattern in `backend/src/ptax/imagery/preview.py:45`.
- Tests use a tiny synthetic GeoJSON zipped in `tmp_path` — no network.

**Definition of Done:**

- [x] `uv sync --group ml` succeeds on the dev machine and `python -c "import torch, segmentation_models_pytorch"` runs; `torch.backends.mps.is_available()` is reported.
- [x] `uv sync --frozen --no-dev` (the image's command) installs neither `torch` nor `segmentation-models-pytorch`, checked by listing the environment.
- [x] A synthetic 10 m × 10 m footprint rasterised onto a 1 m grid covers 100 ± 4 pixels, in the right place.
- [x] Reading by bbox returns only footprints intersecting it.
- [x] Verify: `cd backend && uv run pytest tests/test_eval_footprints.py -q && uv run ruff check src tests && uv run mypy src`

### Task 3: Training data — training AOIs, disjointness guard, multi-year chips

**Objective:** Define training AOIs in long-established Hennepin neighbourhoods, prove in code that they sit at least 1 km from the evaluation AOI, fetch their NAIP for several years through the existing cache writer, and tile image + footprint-mask pairs into chips on the 1.0 m grid the run job compares at. Chips are split into train and validation by tile, never by random pixel.

**Files:**

- Create: `backend/src/ptax/eval/training_data.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Test: `backend/tests/test_eval_training_data.py`

**Key Decisions / Notes:**

- `TRAIN_AOIS` holds bboxes in neighbourhoods built out before 2000 (candidates: Richfield, Edina, Minnetonka, Brooklyn Park — the implementer confirms each is developed and NAIP-covered before committing). `assert_disjoint_from_eval(aoi)` rejects any AOI within 1 km of any entry in `AOIS` (`backend/src/ptax/eval/dataset.py:35`). Distance is **planar in EPSG:26915** (UTM 15N) between the two boxes after projection — never raw degree deltas, which at 45°N mis-scale longitude by ~30%.
- Built-out check: for each `TRAIN_AOIS` entry, fetch its parcels with `sources.fetch_parcels` and `dataset.build_year` (the same path that stratified `nw-hennepin`) and require **≥90% of dated parcels built in 2000 or earlier**. The result is written to the manifest; an AOI that fails is dropped, not argued for.
- Years: at least three of 2010/2015/2019/2021, including 2010 — the evaluation's base year vintage. Fetch with `sources.naip_items` + `cache.fetch_item` (`backend/src/ptax/eval/sources.py:138`, `backend/src/ptax/eval/cache.py:84`), read onto a 1.0 m grid.
- Chips 256 × 256 at 1.0 m, RGB only (uploads can be RGB-only; one input shape for every source). Validation = whole AOIs or whole tiles held out, so neighbouring pixels never straddle the split.
- Label-quality screen: report per-tile labelled-building fraction; exclude tiles beyond a stated bound from their AOI median (records how many were excluded and why).
- New command `ptax-eval train-data` writes `.npz` shards and a manifest (AOIs, years, tile counts, split, exclusions) under `backend/eval/cache/training/` (gitignored).

**Definition of Done:**

- [x] `assert_disjoint_from_eval` rejects an AOI overlapping `nw-hennepin`, rejects one 999 m from it, and accepts one 1 001 m away — measured in EPSG:26915.
- [x] Each `TRAIN_AOIS` entry's share of dated parcels built ≤ 2000 is recorded in the manifest and is ≥ 90%.
- [x] Every configured `TRAIN_AOIS` entry passes the guard (a test iterates them).
- [x] On a synthetic raster + footprint, a produced chip's mask lines up with its image pixel for pixel.
- [x] `ptax-eval train-data` completes, and its manifest lists ≥3 years per AOI, train/validation tile counts, and excluded tiles.
- [x] Verify: `cd backend && uv run pytest tests/test_eval_training_data.py -q && uv run ptax-eval train-data`

### Task 4: Train the building segmenter and write its model card

**Objective:** Train a U-Net (ImageNet ResNet-34 encoder) to segment buildings from the Task 3 chips on the M4 Max, choose its settings on the validation split only, and save the weights plus a committed model card that makes the run reproducible and auditable.

**Files:**

- Create: `backend/src/ptax/learn/__init__.py`
- Create: `backend/src/ptax/learn/model.py`
- Create: `backend/src/ptax/learn/train.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Create: `backend/eval/models/segmenter-v1.json`
- Test: `backend/tests/test_learn_model.py`

**Key Decisions / Notes:**

- `model.py`: `build_model()`, `load(weights_path) -> predictor`, `predict(rgb_uint8 (3,h,w)) -> float32 (h,w)` probabilities; pads to a multiple of 32 and crops back; device `mps` → `cpu` fallback.
- `train.py`: Dice + BCE loss, flips/rotations, brightness/contrast jitter (captures differ radiometrically) and random crop sizes from 24 to 256 px zero-padded to 256 (inference sees parcel-only crops), fixed seed. **Stopping rule:** at most 30 epochs, early stop when validation IoU has not improved for 5 epochs, best checkpoint kept.
- **Freeze before evaluation:** `ptax-eval train` writes the card with the weights' sha256 and a `frozen_at` timestamp; the `segmentation` detector refuses to load weights whose sha256 differs from the card. The probability cutoff is chosen to maximise validation IoU — **on the training validation split, never on the evaluation set**.
- Weights go to `backend/eval/models/segmenter-v1.pt` (gitignored). The model card records encoder, epochs, seed, data manifest hash, validation IoU **per year**, chosen cutoff, and the weights' sha256.
- Tests use `pytest.importorskip("torch")` so the default suite stays green without the `ml` group; they check `predict` shape and padding on random input, not accuracy.

**Definition of Done:**

- [x] `predict` returns an `(h, w)` float array in [0, 1] for inputs whose sides are not multiples of 32.
- [x] `ptax-eval train` completes and writes `segmenter-v1.pt` plus `backend/eval/models/segmenter-v1.json` with validation IoU per year and the chosen cutoff.
- [x] Validation building IoU is reported for every training year, 2010 included; the card states it even if weak.
- [x] The card records the epoch at which training stopped and why (cap or early stop), and `frozen_at`.
- [x] Verify: `cd backend && uv run --group ml pytest tests/test_learn_model.py -q && uv run --group ml ptax-eval train`

### Task 5: `SegmentationDetector` behind the `Detector` seam

**Objective:** Implement `SegmentationDetector.compare`, which segments buildings in the base and target rasters and returns a `ChangeResult` whose score, candidate flag and two markup masks follow the same contract as the classical detector, so the harness, the chips and — later — runs and the viewer consume it unchanged.

**Files:**

- Create: `backend/src/ptax/detection/learned.py`
- Modify: `backend/src/ptax/detection/registry.py`
- Test: `backend/tests/test_detection_learned.py`

**Key Decisions / Notes:**

- Constructor takes a `predict: Callable[[np.ndarray], np.ndarray]` and a cutoff; the registry's `segmentation` entry builds it from `ptax.learn.model.load` and the model card's cutoff. Tests inject a stub `predict`, so this module's logic is tested without torch.
- `new_builtup_mask = target_building & ~dilate(base_building, 1 px) & valid & parcel` — the dilation absorbs sub-pixel misregistration between years so an existing roof's edge isn't counted as new.
- `structure_mask` = largest connected component of `new_builtup_mask`, reusing `_largest_structure` from `backend/src/ptax/detection/detector.py` with `min_fill` 0 (the segmenter already encodes shape); score curve and candidate rule per `## Global Constraints`.
- Honour `InsufficientCoverage` exactly as `ClassicalDetector.compare` does (`backend/src/ptax/detection/detector.py:236-241`). The radiometric `fit` is accepted and ignored: the segmenter normalises each chip itself — documented in the docstring.
- `indicators`: `structure_m2`, `new_builtup_m2`, `base_building_frac`, `target_building_frac`, `resolution_m`, `model` (card name).

**Definition of Done:**

- [x] With a stub that marks a 20 × 15 px block only in the target, `compare` returns `structure_m2` = 300 at 1 m, `structure_mask` equal to that block, and the classical score curve's value.
- [x] A block present in both years produces no new area, including when shifted by one pixel between years.
- [x] `structure_mask` is a subset of `new_builtup_mask`; both match the raster grid shape.
- [x] Too little coverage raises `InsufficientCoverage` for the right year.
- [x] With a fixture model card and `ptax.learn.model.load` monkeypatched, `get_detector("segmentation")` returns a `SegmentationDetector` using the card's cutoff; a card whose `weights_sha256` does not match the weights file is refused. Tested without torch.
- [x] Verify: `cd backend && uv run pytest tests/test_detection_learned.py -q && uv run ruff check src tests && uv run mypy src`

### Task 6: Measure against the visual labels and record the decision

**Objective:** Score the segmentation detector on `nw-hennepin-2010-2021` against the visual labels with both comparisons alongside, look at what it marks at the top of its ranking, measure CPU inference cost, and record a ship / no-ship decision for production integration against the PRD's criteria.

**Files:**

- Modify: `backend/eval/README.md`
- Modify: `README.md`
- Modify: `docs/prd/2026-09-23-learned-change-detector.md`

**Key Decisions / Notes:**

- Run `ptax-eval score --detector segmentation --labels ...` and `ptax-eval chips --all --ranked --limit 20 --markup --detector segmentation`; read the ranked sheets as Plan D's chips finding was read.
- The gate uses the PRD's criteria, plus the uncertainty the PRD left open: **pass** if AP's 95% bootstrap lower bound is above both 0.2236 and 0.2578 and precision @ top 1% > 0; **inconclusive** if the point estimate clears both but the interval does not; **fail** otherwise. Report the ≤2% / ≥60% target separately.
- Gate only the frozen `segmenter-v1`; confirm its weights sha256 matches the card before the first score.
- Record every evaluation run made, not only the best — tuning against the evaluation set after seeing it is reported as such, never hidden.
- Measure per-parcel inference time on CPU (not MPS — production workers are CPU-only) over the 300 parcels and project a 200 000-parcel run.
- Report the segmenter's base-year building fraction on parcels labelled `no_change`: a high "new" rate there is the 2010-domain-shift failure named in the risks.
- Update the PRD's `Status:` and Open Questions with what was answered.

**Definition of Done:**

- [x] `backend/eval/README.md` records AP, precision, recall, flag rate and precision @ top 1% for the segmentation detector beside the classical detector and the no-imagery baseline, at the 0.1795 base rate.
- [x] The top-20 ranked markup sheet is read and its failure modes (or their absence) recorded, including whether graded lots still dominate.
- [x] Per-parcel CPU inference time and the 200 000-parcel projection are recorded.
- [x] AP is reported with its 95% bootstrap interval, and the decision is recorded as pass / inconclusive / fail against the gate above, in `backend/eval/README.md` and summarised in `README.md`.
- [x] Results are also reported split by parcel size (below and above the median), since inference on small parcel-only crops is a named risk.
- [x] Verify: `cd backend && uv run --group ml ptax-eval score eval/nw-hennepin-2010-2021.json --labels eval/visual-labels-nw-hennepin-2010-2021.json --detector segmentation`
