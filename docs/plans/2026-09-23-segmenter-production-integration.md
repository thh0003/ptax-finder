# Segmenter Production Integration Implementation Plan

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** Production comparison runs use the gated `segmenter-v1` detector by default. The detector is chosen per run, and the run records the exact model it used. The segmenter runs on a separate CPU worker image that fetches hash-checked weights from S3, and it is deployed to `us-east-1` with measured Fargate cost. Requirements: `docs/prd/2026-09-23-segmenter-production-integration.md`.

## Out of Scope

- **Retraining, tuning or re-thresholding the model.** That includes clipping the driveway spill out of the structure mask. Any model change goes back through `ptax-eval` as a new candidate.
- **Re-scoring completed runs.** Existing runs read back as `classical`, with the scores and markup they recorded.
- **GPU compute**, and any change to the classical detector.
- **A second labelled evaluation area.** It is not a precondition here, because the user chose to make the segmenter the default now (see Autonomous Decisions).
- **Plan C's review workflow.**

## Approach

**Chosen:** The detector is chosen per run, and recorded on `runs` as the detector, model name and weights sha256.

- **Run job:** `execute_run` builds its detector from the run's recorded choice via a new torch-free `ptax.detection.model_store`. That module publishes the model card and weights to the uploads bucket under `models/`, and it downloads and hash-checks them for the worker.
- **Images:** a second `worker` target in `backend/Dockerfile` adds CPU-only torch. CDK builds it as a second image for the worker service only.

**Why:** Each run keeps provenance for the exact model it used, and the API image never carries torch. The costs are a second image build per deploy and a one-time `ptax-admin model-publish` step before segmenter runs can start.

## Global Constraints

- Detector names are exactly `classical` and `segmentation` (the keys of `DETECTORS` in `backend/src/ptax/detection/registry.py`).
- New runs default to `segmentation`; runs that existed before migration 0005 read back as `classical`.
- `DEFAULT_THRESHOLD` 0.3 and `DEFAULT_MIN_NEW_AREA_M2` 37.2 stay the defaults for both detectors.
- Model objects live at `models/<name>.pt` and `models/<name>.json` in the uploads bucket (`Settings.s3_bucket`).
- The model the API records and the worker loads is named by the setting `segmenter_model` (env `SEGMENTER_MODEL`), default `segmenter-v1`.
- The API image (`backend/Dockerfile` target `app`) must not contain torch; only the `worker` target installs `--group ml`.
- The Linux worker installs torch from `https://download.pytorch.org/whl/cpu` (torchvision resolves from PyPI with no CUDA dependency); no `nvidia-*` or `triton` package may be in the worker image.

## Context for Implementer

The segmenter was built and gated by `docs/plans/2026-09-23-learned-change-detector.md`; the evidence is in `backend/eval/README.md`, in the section "Learned detector — segmenter-v1 and the decision gate". The pieces it built:

- `SegmentationDetector` (`backend/src/ptax/detection/learned.py`) takes an injectable `predict`.
- `from_model_card(card_path)` refuses weights whose sha256 differs from the card. The segmenter ignores the radiometric `fit`.
- `ptax.learn.model.load` imports torch.

The worker and API share one database and one S3 bucket. The API must never import torch; the worker only imports it when a run's detector is `segmentation`. Tests run without a GPU and must never download real weights, so the model is always stubbed through `learned._load_predictor` and published as fake bytes with a matching card.

## Runtime Environment

- Local stack: `make dev-up` (Postgres/PostGIS, MinIO, cognito-local), `make api`, `make worker`, `make web`. The Makefile `worker` target gains `--group ml` so the local worker can run segmenter runs.
- Production: CDK app in `infra/`, `pnpm cdk deploy --all` from `infra/`; migrations run at API container start (`PTAX_RUN_MIGRATIONS=1`).

## Assumptions

- `uv` can pin torch to the PyTorch CPU index for Linux only while macOS keeps PyPI wheels with MPS, via `[tool.uv.sources]` markers — **checked 2026-09-23 in a scratch copy:** the lock resolves `torch 2.14.0+cpu` from the CPU index for Linux and `torch 2.14.0` from PyPI otherwise, and drops every `nvidia-*` package and `triton`; `torchvision 0.29.0` stays on PyPI for both, with no CUDA dependency left. Task 4 depends on this; its DoD re-checks it in the built image.
- CPU inference on amd64 gives the same candidate flags as the MPS inference the gate measured. Task 7 measures this. The gate's result only carries over to production if it holds.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The segmenter is the default but the model is not published or its hash is wrong, so every default run fails | Medium | High | Run creation returns 409 when `models/<segmenter_model>.json` is absent (Task 2). The worker verifies the weights against the sha256 the run recorded before scoring any parcel, and fails the run with that message (Task 3). |
| The default Linux torch wheel drags several GB of CUDA libraries into the worker image | High | Medium | CPU index pinned for Linux (Task 4); DoD asserts `torch.version.cuda is None` and no `nvidia-*` distribution in the image |
| CPU inference disagrees with the MPS numbers the gate reported | Low | High | Task 7 compares CPU and MPS per-parcel results on all 300 evaluation parcels before deploy |
| The Docker build context ships the 2.2 GB evaluation cache into the image | Certain (today) | Medium | `.dockerignore` excludes `backend/eval/cache`, `backend/eval/out` and `backend/eval/models/*.pt` (Task 4) |
| Fargate is too slow or too small for county runs | Medium | Medium | Task 9 logs per-parcel time and peak RSS from a real deployed run and records a 200 000-parcel projection; worker size changes only on that evidence |

## Autonomous Decisions

- **Default detector = `segmentation`** (user's choice, 2026-09-23). The PRD's Must row "runs created without a detector choice behave exactly as today" becomes "existing runs read back as `classical`, and a run can still choose `classical`". Task 9 edits the PRD to match.
- **Driveway spill is accepted as-is.** Changing the markup means changing the detector, which is out of scope.
- **Model version switch = `SEGMENTER_MODEL` setting**, not a mutable pointer object in S3. Publishing `segmenter-v2` never changes production until a deploy says so.
- **Operator path `ptax-admin start-run`** lets a deployed run be started through ECS `run-task`, as tenants and users already are, without anyone entering a password.

## File Structure

- `backend/src/ptax/detection/model_store.py` (create): torch-free publishing, reading and fetching of model cards and weights in S3.
- `backend/alembic/versions/0005_run_detector.py` (create): the `runs.detector`, `model_name` and `model_sha256` columns, with the backfill and the check constraint.
- `backend/src/ptax/db/models.py`, `backend/src/ptax/api/runs.py`, `backend/src/ptax/config.py` (modify): the per-run detector field and the `segmenter_model` setting.
- `backend/src/ptax/detection/run.py` (modify): builds the run's detector, skips the radiometric fit for the segmenter, and logs timing and memory.
- `backend/src/ptax/learn/model.py` (modify): a `PTAX_TORCH_DEVICE` override for choosing the device.
- `backend/src/ptax/cli.py` (modify): the `model-publish` and `start-run` commands.
- `backend/Dockerfile`, `.dockerignore`, `backend/pyproject.toml`, `backend/uv.lock`, `Makefile` (modify): the `worker` image target, CPU torch, and the build-context exclusions.
- `infra/lib/compute-stack.ts`, `infra/test/stacks.test.ts` (modify): the worker image asset and the `SEGMENTER_MODEL` environment variable.
- `frontend/src/api/runs.ts`, `frontend/src/pages/RunsPage.tsx`, `frontend/src/pages/ParcelViewerPage.tsx`, `frontend/src/api/runs.test.ts` (modify): the detector choice and its display.
- `README.md`, `backend/eval/README.md`, `docs/prd/2026-09-23-segmenter-production-integration.md` (modify): the deployment record and the measured Fargate cost.

## E2E Test Scenarios

Local stack (`make dev-up`, `make api`, `make worker`, `make web`), 25-parcel fixture layer ingested, NAIP fixture years 2021 and 2023 ready, `segmenter-v1` published to MinIO with `ptax-admin model-publish eval/models/segmenter-v1.json`.

### TS-001: Start a run with the default detector
**Priority:** Critical
**Preconditions:** Signed in as the demo admin; model published
**Mapped Tasks:** Task 2, Task 3, Task 6

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to the Runs page | The start-run form shows a Detector select with "Segmenter" selected |
| 2 | Choose base 2021, target 2023; click Start | A new run appears labelled "Segmenter · segmenter-v1" with status queued/running |
| 3 | Wait for the run to finish (reload the page) | Status succeeded; 25 processed; a candidate count is shown |

### TS-002: A segmenter parcel shows who found the change
**Priority:** High
**Preconditions:** TS-001's run succeeded
**Mapped Tasks:** Task 6

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open the run's highest-scoring parcel in the viewer | The header shows "Detected by Segmenter (segmenter-v1)" |
| 2 | Read the indicators | "Largest new structure", "New building area", "Base-year building share" and "Compared at" are shown; no "Vegetation loss" row |

### TS-003: A classical run is still available and labelled
**Priority:** High
**Preconditions:** Signed in as the demo admin
**Mapped Tasks:** Task 2, Task 6

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | On the Runs page choose Detector "Classical", base 2021, target 2023; click Start | A run labelled "Classical" appears and completes |
| 2 | Open one of its parcels | The header shows "Detected by Classical"; the indicators include "Vegetation loss" |

### TS-004: Starting a segmenter run without a published model is refused
**Priority:** Medium
**Preconditions:** `models/segmenter-v1.json` removed from MinIO (re-published after the scenario)
**Mapped Tasks:** Task 2, Task 6

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start a run with Detector "Segmenter" | The form shows the error "segmenter model segmenter-v1 is not published"; no run is added |

## Deviations

- Task 4 (tactical): the scratch probe's reading that "torchvision can stay on PyPI" was wrong for Linux: PyPI torchvision is ABI-incompatible with `torch+cpu` (`operator torchvision::nms does not exist` in the built worker image). uv applies `[tool.uv.sources]` only to direct dependencies, so `backend/pyproject.toml` now declares `torchvision` in the `ml` group and pins it to the CPU index like torch; `backend/uv.lock` resolves `torchvision 0.29.0+cpu` for Linux. The Global Constraint line about torchvision is superseded by this entry.
- Task 2 (tactical): the shared `published_segmenter` fixture (a fake model published under a unique name, selected for the app) lives in `backend/tests/conftest.py`, used by Task 2 and Task 3 tests.
- Task 3 (tactical): `backend/src/ptax/detection/learned.py` now uses `ptax.detection.model_store.sha256_file` instead of its own copy of the hash helper (the duplication the previous plan's review flagged); `backend/src/ptax/learn/train.py` keeps its own, being outside this plan's files.
- Task 6 (tactical): the E2E scenarios run on an isolated stack (API :8001, Vite :5174 via a temporary `frontend/vite.e2e.local.config.ts`, deleted afterwards) against the compose database, because :8000 and :5173 were occupied by processes this session did not start; the dev database was migrated to 0005 and `segmenter-v1` published to local MinIO for them.

## E2E Results

Executed 2026-09-23 on an isolated local stack from this branch: API on :8001, the `ml`-group worker, and Vite on :5174. All three used the compose PostGIS and MinIO, with the dev database migrated to 0005 and the real `segmenter-v1` published to MinIO. The user signed in; the agent entered no password. The pre-existing :8000 and :5173 processes were left alone.

| Scenario | Priority | Result | Fix Attempts | Notes |
|----------|----------|--------|--------------|-------|
| TS-001 | Critical | PASS | 0 | Detector select defaults to "Segmenter"; the two pre-0005 runs read "Classical". The new 2021→2023 run is labelled "Segmenter · segmenter-v1" and succeeded over 25/25 with 0 skipped. It has 0 candidates: the real model finds no roofs in the synthetic fixture shapes, which is expected. The worker logged `mean_parcel_ms 64.8`. |
| TS-002 | High | PASS | 0 | Header "2021 → 2023 · Detected by Segmenter · segmenter-v1". Indicators: Largest new structure, New building area, Base-year building share (0.0%) and Compared at, with no Vegetation loss row. The header wording uses the list's "·" label rather than the plan's parenthesised form. |
| TS-003 | High | PASS | 0 | A Classical run, recorded with no model and no hash, succeeded with 3 candidates, the same as the earlier classical run. Its parcel page reads "Detected by Classical", and its indicators include Vegetation loss (300 m²). |
| TS-004 | Medium | PASS | 0 | The API was restarted with `SEGMENTER_MODEL=segmenter-unpublished` rather than deleting the published card. The form shows "segmenter model segmenter-unpublished is not published", and no run was added (still 4). |

Found while running TS-001: the worker's `peak_rss_mb` read 845392 on macOS, where `ru_maxrss` is bytes rather than Linux's KiB. Fixed in `backend/src/ptax/detection/run.py`, with a regression test in `backend/tests/test_runs.py`.

## Progress Tracking

- [x] Task 1: Model store and `ptax-admin model-publish`
- [x] Task 2: Runs record their detector and model
- [x] Task 3: The run job runs the recorded detector
- [x] Task 4: Worker image with CPU-only torch
- [x] Task 5: CDK builds and runs the worker image
- [x] Task 6: Detector choice and display in the SPA
- [x] Task 7: CPU and MPS inference agree on the evaluation set
- [x] Task 8: Provide AWS deploy credentials
- [x] Task 9: Deploy, run on Fargate, measure and record

## Implementation Tasks

### Task 1: Model store and `ptax-admin model-publish`

**Objective:** Add a new torch-free module, `ptax.detection.model_store`. It puts a frozen model card and its weights in the uploads bucket, reads a published card, and downloads weights to a local cache after checking their sha256. An operator command, `ptax-admin model-publish <card>`, does the publishing, so the API and worker can refer to a model by name.

**Files:**

- Create: `backend/src/ptax/detection/model_store.py`
- Modify: `backend/src/ptax/cli.py`
- Test: `backend/tests/test_model_store.py`

**Key Decisions / Notes:**

- `publish(settings, card_path)` checks the card's `weights_sha256` against the local `.pt` beside it, uploads `models/<name>.pt` first and `models/<name>.json` last, so a visible card implies its weights are present; re-publishing identical bytes is a no-op; a card already published with a *different* sha256 raises (a frozen name is never overwritten — mirrors the rule in `backend/src/ptax/learn/train.py:212`).
- `published_card(settings, name) -> dict | None` and `fetch_weights(settings, name, expected_sha256, cache_dir) -> Path` (cache file `<name>-<sha256[:12]>.pt`, re-downloaded when missing or when its hash differs; raises `ModelMismatch` naming both hashes).
- S3 access through `ptax.storage.get_s3_client` (`backend/src/ptax/storage.py:37`); tests use the same MinIO bucket as `upload_object` in `backend/tests/conftest.py:227`.
- Model names are validated against `^[a-z0-9][a-z0-9.-]*$` before they become S3 keys.

**Definition of Done:**

- [x] Publishing a card + weights makes `published_card` return the card and `fetch_weights` return a file whose sha256 matches.
- [x] Publishing a card whose weights do not match its `weights_sha256` is refused before anything is uploaded.
- [x] Re-publishing the same name with different weights is refused; re-publishing identical bytes succeeds.
- [x] `fetch_weights` with a wrong expected hash raises `ModelMismatch` and leaves no cached file.
- [x] Verify: `cd backend && uv run pytest tests/test_model_store.py -q && uv run ruff check src tests && uv run mypy src`

### Task 2: Runs record their detector and model

**Objective:** Store the detector each run uses. Migration 0005 adds `runs.detector` (existing rows backfilled `classical`) plus `model_name` and `model_sha256`, which only segmenter runs carry. The runs API accepts `detector`, defaulting to `segmentation`, records the published model's name and hash at creation, and returns 409 when that model is not published.

**Files:**

- Create: `backend/alembic/versions/0005_run_detector.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/api/runs.py`
- Modify: `backend/src/ptax/config.py`
- Test: `backend/tests/test_runs.py`
- Test: `backend/tests/test_migrations.py`
- Test: `backend/tests/conftest.py`

**Key Decisions / Notes:**

- The migration adds `detector TEXT NOT NULL DEFAULT 'classical'`, which backfills existing rows, then drops the server default so every new run states its detector. It adds `model_name TEXT NULL` and `model_sha256 TEXT NULL`, with check constraints `detector IN ('classical','segmentation')` and `(detector = 'segmentation') = (model_sha256 IS NOT NULL)`. The migration style follows `backend/alembic/versions/0004_run_parcels_score_idx.py:1`.
- `RunIn.detector: Literal["classical", "segmentation"] = "segmentation"`; `RunOut` gains `detector` and `model_name`.
- `create_run` with `segmentation` reads `model_store.published_card(settings, settings.segmenter_model)`. Missing → 409 `segmenter model <name> is not published`. The API imports only `model_store`, never `learned` or torch.
- Existing tests in `backend/tests/test_runs.py` exercise the classical detector against fixture imagery; their `_start` helper passes `detector="classical"` so they keep testing what they tested.

**Definition of Done:**

- [x] `POST /api/runs` without `detector` creates a `segmentation` run recording `model_name` `segmenter-v1` and the published card's `weights_sha256`.
- [x] `detector: "classical"` creates a classical run with null model fields; an unknown detector returns 422.
- [x] With no published card, a segmentation run is refused with 409 and no run or job row is created.
- [x] After upgrading a database holding a pre-0005 run, that run reads back as `classical`; the migration downgrades cleanly.
- [x] Verify: `cd backend && uv run pytest tests/test_runs.py tests/test_migrations.py -q && uv run ruff check src tests && uv run mypy src`

### Task 3: The run job runs the recorded detector

**Objective:** Make `execute_run` build the detector the run recorded instead of hard-coding `ClassicalDetector()`. For a segmenter run it fetches and verifies the weights by the run's recorded sha256 before scoring any parcel, and skips the radiometric pre-pass the segmenter ignores. At the end it logs per-parcel time and peak memory so Fargate cost can be read from the worker log. Add `ptax-admin start-run` so an operator can start a run without the SPA.

**Files:**

- Modify: `backend/src/ptax/detection/run.py`
- Modify: `backend/src/ptax/detection/learned.py`
- Modify: `backend/src/ptax/cli.py`
- Modify: `Makefile`
- Test: `backend/tests/test_runs.py`
- Test: `backend/tests/test_detection_learned.py`

**Key Decisions / Notes:**

- A new `_detector_for(settings, run)` in `run.py`: `classical` → `ClassicalDetector()`; `segmentation` → `model_store.fetch_weights(...)` + `learned.from_model_card(card_path)`. The card is written to the same cache directory, so the existing sha256 refusal in `learned.py:141` runs as well. The weights are loaded once per run, not per parcel. Both happen before the batch loop (`run.py:283`), so a mismatch fails the run with zero `run_parcels` rows.
- `_fit_for_run` (`run.py:159`) is called only for `classical`.
- The completion log adds `mean_parcel_ms` and `peak_rss_mb` (`resource.getrusage(RUSAGE_SELF).ru_maxrss`, which is kilobytes on Linux).
- `learned.py` gains a `cache_dir`-aware loader only if `from_model_card` needs it. Tests stub `learned._load_predictor` with a red-band stub like `backend/tests/test_detection_learned.py:52`, and publish fake weight bytes with a matching card.
- `ptax-admin start-run --tenant <name> --base <year> --target <year> [--detector ...]` builds the run through the same code path as `create_run`, so its validation cannot drift from the API. The Makefile `worker` target runs with `--group ml`.

**Definition of Done:**

- [x] A segmentation run over the fixture layer succeeds with a stubbed predictor: every parcel gets a row, flagged rows store `structure_geom`, and `indicators["model"]` is the run's `model_name`.
- [x] A segmentation run whose recorded sha256 differs from the published weights ends `failed` with an error naming the hashes, and has zero `run_parcels` rows.
- [x] A classical run scores exactly as before (the existing `test_run_scores_every_parcel_and_summarises` passes unchanged apart from the explicit detector).
- [x] A segmentation run does not call `_fit_for_run`, asserted by monkeypatching it to raise.
- [x] `ptax-admin start-run` creates a queued run and job for the named tenant and years.
- [x] Verify: `cd backend && uv run --group ml pytest tests/test_runs.py tests/test_detection_learned.py -q && uv run ruff check src tests && uv run mypy src`

### Task 4: Worker image with CPU-only torch

**Objective:** Add a `worker` target to `backend/Dockerfile` that installs the `ml` group with CPU-only torch, so production workers can run the segmenter while the API image stays free of torch. Stop the build context shipping the local evaluation cache and model weights into every image.

**Files:**

- Modify: `backend/Dockerfile`
- Modify: `.dockerignore`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `Makefile`

**Key Decisions / Notes:**

- `pyproject.toml`: add an explicit `[[tool.uv.index]] name = "pytorch-cpu"` (url per Global Constraints) and `[tool.uv.sources] torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]`, so macOS keeps PyPI torch with MPS. This exact config was resolved in a scratch copy on 2026-09-23 (see Assumptions).
- `Dockerfile`: `FROM app AS worker` runs `uv sync --frozen --no-dev --group ml`, with `CMD ["python", "-m", "ptax.worker"]`. **Because `worker` must follow `app`, it becomes Docker's default (last) target, so every API build names `app` explicitly.** The Makefile `image` and `image-check` recipes gain `--target app`, and Task 5 sets `target: "app"` on the API asset. The `app` stage itself is unchanged apart from the smaller context.
- `.dockerignore`: add `backend/eval/cache`, `backend/eval/out` and `backend/eval/models/*.pt`. The committed card JSON may stay, since the worker reads cards from S3.
- `Makefile`: add `image-worker` = `docker build --platform linux/amd64 --target worker -f backend/Dockerfile -t ptax-finder-worker:local .` and `image-check-worker`, which runs that amd64 image and imports `torch, segmentation_models_pytorch, ptax.learn.model, ptax.detection.run`, asserting `torch.version.cuda is None`. An explicit amd64 platform means the check covers the architecture Fargate runs, not the Mac's arm64. The Python stages build under QEMU emulation: slow but correct. Only the Node `web` stage needed `$BUILDPLATFORM` (`backend/Dockerfile:5`), and `worker` inherits from `app`, not `web`.

**Definition of Done:**

- [x] `uv lock` resolves Linux torch from the PyTorch CPU index (`+cpu`) with no `nvidia-*` or `triton` package in the lock, and `uv sync --group ml` on macOS still reports `torch.backends.mps.is_available()` True.
- [x] `make image-check-worker` passes for `linux/amd64`, and `pip list` in the worker image shows no `nvidia-*` distribution.
- [x] In the API image built with `--target app` (`make image-check`), `import torch` fails, and the image is smaller than today's `ptax-finder:local`. Both image sizes are recorded in Task 9's README entry.
- [x] Verify: `make image-check && make image-check-worker`

### Task 5: CDK builds and runs the worker image

**Objective:** Build the `worker` Dockerfile target as a second image asset in `infra/lib/compute-stack.ts` and run the worker service on it, with `SEGMENTER_MODEL` set for both services. The API keeps the torch-free image, and the worker can fetch the published model with its existing bucket permissions.

**Files:**

- Modify: `infra/lib/compute-stack.ts`
- Test: `infra/test/stacks.test.ts`

**Key Decisions / Notes:**

- The existing API `DockerImageAsset` (`compute-stack.ts:34`) gains `target: "app"`. It must be explicit, because the Dockerfile's last stage is now `worker`. A second `DockerImageAsset` with `target: "worker"` and the same `directory`, `file` and `platform` is used by the worker container at `compute-stack.ts:144`.
- `environment` gains `SEGMENTER_MODEL: "segmenter-v1"` for both services. `grantReadWrite` on the uploads bucket (`compute-stack.ts:72`) already covers `models/*`; no new IAM.
- Worker CPU and memory stay at 2048 / 4096 unless Task 9's measurement says otherwise.

**Definition of Done:**

- [x] In the cloud assembly's asset manifest, the API image asset builds `dockerBuildTarget` `app` and the worker asset builds `worker`. Both are `linux/amd64`, the API and worker task definitions use different image URIs, and both carry `SEGMENTER_MODEL`.
- [x] Verify: `make test-infra`

### Task 6: Detector choice and display in the SPA

**Objective:** The Runs page offers a Detector select (Segmenter by default, Classical as the alternative). The run list labels each run with its detector and model. The parcel viewer states which detector found the change and shows the indicators that detector actually produces. Verified by TS-001 to TS-004.

**Files:**

- Modify: `frontend/src/api/runs.ts`
- Modify: `frontend/src/pages/RunsPage.tsx`
- Modify: `frontend/src/pages/ParcelViewerPage.tsx`
- Test: `frontend/src/api/runs.test.ts`

**Key Decisions / Notes:**

- `runs.ts`: `Detector = "classical" | "segmentation"`, `DETECTOR_LABELS`, and `Run` gains `detector` and `model_name`. `createRun` sends `detector`. `RunParcelDetail.indicators` values become `number | string | null`, since the segmenter's `model` indicator is a string.
- `ParcelViewerPage.tsx`: replace the single `INDICATORS` list (`ParcelViewerPage.tsx:23`) with one list per detector. Segmentation uses `structure_m2`, `new_builtup_m2` (labelled "New building area"), `base_building_frac` (labelled "Base-year building share") and `resolution_m`.
- `RunsPage.tsx`: a labelled `<select>` next to the year selects (`RunsPage.tsx:229`). A 409 message from the API shows in the form's existing error slot. The run summary line (`RunsPage.tsx:359`) adds the detector label and model.
- `runs.test.ts`: `createRun` sends the detector; label helpers map both names.

**Definition of Done:**

- [x] The Runs page's Detector select defaults to Segmenter, and a started run sends `detector` in its request body.
- [x] Each run in the list shows "Segmenter · segmenter-v1" or "Classical".
- [x] The parcel viewer shows "Detected by …" and only the indicators the run's detector produces.
- [x] Verify: `make test-frontend` and TS-001–TS-004 pass in the browser against the local stack

### Task 7: CPU and MPS inference agree on the evaluation set

**Objective:** Prove the CPU path production will run reproduces the gated result. Add a `PTAX_TORCH_DEVICE` override to `pick_device`, then score the 300-parcel evaluation set with `segmenter-v1` on `cpu` and on `mps`, and compare them parcel by parcel.

**Files:**

- Modify: `backend/src/ptax/learn/model.py`
- Test: `backend/tests/test_learn_model.py`
- Modify: `backend/eval/README.md`

**Key Decisions / Notes:**

- `pick_device(preferred)`: an explicit argument wins, then the env var `PTAX_TORCH_DEVICE`, then MPS, then CPU. This is a small extension of `backend/src/ptax/learn/model.py:36`.
- Comparison: `PTAX_TORCH_DEVICE=cpu` and `=mps` runs of `ptax-eval score --detector segmentation --out …`, with the two per-parcel JSONs diffed. Both are runs of the already-frozen model; they are recorded as evaluation runs 4 and 5 in `backend/eval/README.md` and change nothing.

**Definition of Done:**

- [x] `PTAX_TORCH_DEVICE=cpu` makes `pick_device()` return `cpu`, covered by a test in `test_learn_model.py`.
- [x] On all 300 parcels the CPU and MPS runs agree on every candidate flag, and every score differs by at most 0.01. AP matches to 3 decimals. Any disagreement is reported in `backend/eval/README.md` rather than hidden.
- [x] Verify: `cd backend && PTAX_TORCH_DEVICE=cpu uv run --group ml ptax-eval score eval/nw-hennepin-2010-2021.json --labels eval/visual-labels-nw-hennepin-2010-2021.json --detector segmentation`

### Task 8: Provide AWS deploy credentials

**Objective:** Deploying to `us-east-1` needs AWS credentials with deploy rights, and so does publishing the model to the production bucket. Only the user can provide them, and they must not be pasted into the chat. The earlier STS keys are expired and should be revoked.

**Owner:** User

**User Action:** Sign in to a named AWS profile with deploy rights for the ptax account in `us-east-1` (for example `! aws sso login --profile <name>`), then reply `done` and give the profile name.

**Files:**

- Modify: `README.md`

**Key Decisions / Notes:**

- The agent uses `--profile <name>` / `AWS_PROFILE` on every AWS command and never writes credentials to disk or into the repository.
- `README.md` is listed because Task 9 records the deployment there, not because this task edits it.

**Definition of Done:**

- [x] `aws sts get-caller-identity --profile <name>` returns the ptax account.

### Task 9: Deploy, run on Fargate, measure and record

**Objective:** Publish `segmenter-v1` to the production bucket and deploy both images with `cdk deploy`, letting migration 0005 run at API start. Start a default (segmenter) run over the demo tenant's real NAIP 2010→2021 imagery through `ptax-admin start-run` on ECS. Read its timing and memory from the worker log, and record the deployment, the Fargate cost and the 200 000-parcel projection.

**Files:**

- Modify: `README.md`
- Modify: `backend/eval/README.md`
- Modify: `docs/prd/2026-09-23-segmenter-production-integration.md`

**Key Decisions / Notes:**

- The order matters, because the segmenter is now the default:
  1. `model-publish` against the production bucket.
  2. `cdk diff`, which must show only the two image assets, the new environment variable and the task definitions.
  3. `cdk deploy --all`.
  4. Health and migration log check.
  5. `start-run` through ECS `run-task`, following the tenant-provisioning `run-task` pattern in `README.md:137`.
  6. Worker log read.
- The deployed run's per-parcel scores are listed through the API and compared with Task 7's CPU numbers where the parcels overlap, which they don't: the demo tenant's 25 parcels are not the evaluation set. So parity rests on Task 7, and this run proves wiring, cost and memory.
- The PRD's Status and Success Criteria are updated to the default-detector decision and the measured results.

**Definition of Done:**

- [x] `/api/health` returns ok after deploy, and the API log shows `Running upgrade 0004 -> 0005`.
- [x] A default run on the deployed stack records `detector` `segmentation` and model `segmenter-v1`, and succeeds over all 25 demo parcels.
- [x] The worker log's `mean_parcel_ms` and `peak_rss_mb` are recorded in `README.md`, with the 200 000-parcel projection and the two image sizes. Worker size is changed only if peak RSS exceeds 75% of 4 GB.
- [x] Verify: `aws logs tail /aws/ecs/<worker log group> --profile <name> --since 1h | grep "mean_parcel_ms"`
