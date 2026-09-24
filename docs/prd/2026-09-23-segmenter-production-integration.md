# Segmenter Production Integration

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Delivered 2026-09-23 (docs/plans/2026-09-23-segmenter-production-integration.md); segmenter is the default detector
Research: Light — repository and deployment configuration read; Fargate measured after delivery (see Outcome)

## Problem Statement

The learned detector works, but only on a developer's laptop. `segmenter-v1` — a building segmenter that flags a parcel when a building appears in the target capture that is absent from the base — passed its decision gate on 2026-09-23 (`docs/plans/2026-09-23-learned-change-detector.md`, evidence in `backend/eval/README.md`). Against the 300 visually labelled Hennepin County parcels it ranks at **average precision 0.588 (95% interval 0.422–0.772)**, against 0.224 for the classical detector that production runs today. **19 of its top 20 parcels** really gained a structure, and every structure it marks there is a roof. The classical detector's top 13 were all graded lots and bare ground.

An assessor running a comparison today still gets the classical detector's queue: 30% of parcels flagged at 29% precision, with the worst mistakes at the top. The segmenter exists only in the offline evaluation harness (`ptax-eval score --detector segmentation`). Its dependencies (torch, segmentation-models-pytorch) are deliberately excluded from the production image, and its weights live on one machine. None of the queue's value reaches a user until a run can use it.

## Core User Flows

### Flow 1: Admin starts a run with the segmenter
1. On the Runs page the admin picks base and target years, as today, and chooses a detector — **Segmenter** or **Classical**.
2. The run job scores every parcel with that detector, resumably, in batches of 200, as today.
3. The run's record names the detector and, for the segmenter, the exact frozen model (name and weights hash). A completed run can always say what produced its scores.

### Flow 2: Reviewer works the run's queue
1. The reviewer opens the run's flagged parcels, highest score first, as today.
2. The parcel viewer draws the segmenter's markup — red for the new building the score rests on, blue for the rest of the new building area — using the masks the run stored, as today.
3. The parcel page shows which detector found the change.

### Flow 3: Team ships a new model version
1. A retrained candidate (`segmenter-v2`) passes the same gate in `ptax-eval`.
2. The team publishes it. New runs can use it; completed runs keep the model they recorded.

## Scope

### In Scope
- **Per-run detector choice**: a detector field on runs, with a migration; the run-creation API accepts it and validates it; the Runs page offers it.
- **Recording provenance** on each run: detector name and, for the segmenter, the model card name and weights sha256. The run refuses to start if the deployed weights do not match the card.
- **Worker packaging**: the worker image carries CPU-only torch, segmentation-models-pytorch and the frozen weights (or fetches them at start with the sha256 check). The API image must not grow to carry them if that can be avoided.
- **Running the segmenter in the run job**: `backend/src/ptax/detection/run.py` builds its detector from the registry (`ptax.detection.registry.get_detector`) instead of constructing `ClassicalDetector()`; the run-level radiometric fit is skipped for the segmenter, which ignores it.
- **Fargate measurement**: per-parcel time and memory on the real worker task (2 vCPU, 4 GB today). Also a county-scale projection, and a worker size change if the numbers call for one.
- **Showing the detector** on the run and parcel pages.
- **Deploying** to `us-east-1` and a verified comparison run with the segmenter over real parcels.

### Explicitly Out of Scope
- **Retraining or tuning the model.** `segmenter-v1` is the gated candidate; a new model goes back through `ptax-eval` first.
- **Re-scoring completed runs.** A run keeps the scores and markup it recorded.
- **GPU compute.** The measured CPU cost (~74 ms per parcel on one M4 core) does not call for it. Revisit only if the Fargate measurement disagrees by an order of magnitude.
- **The review workflow** (confirm/dismiss, notes, history) — Plan C's.
- **Removing the classical detector.** It stays selectable as the fallback and the comparison.

## Success Criteria

| Bar | Criterion |
|---|---|
| **Must** | A run created with the segmenter completes on the deployed stack over real parcels. Its scores match `ptax-eval`'s for the same parcels, imagery and grid |
| **Must** | Every run records its detector, and segmenter runs record the model's weights sha256; a hash mismatch stops the run before it scores anything |
| **Must** | Runs created without a detector choice behave exactly as today, and existing runs read back unchanged |
| **Must** | Per-parcel time and peak memory are measured on the Fargate worker, with a 200 000-parcel projection recorded |
| **Should** | The API image's size is unchanged, or the growth is stated and justified |
| **Decision** | Whether the segmenter becomes the default detector — see Open Question 1 |

## Technical Context

- **Detector seam:** `ptax.detection.registry.get_detector(name)` already returns `classical` or `segmentation`. The segmentation entry imports torch only on demand, via `ptax.detection.learned.from_model_card`, which checks the weights' sha256 against the committed card `backend/eval/models/segmenter-v1.json`. `SegmentationDetector.compare` returns the same `ChangeResult` (score, candidate, indicators, `new_builtup_mask`, `structure_mask`) the run job and the viewer already store and draw.
- **Run job:** `execute_run` in `backend/src/ptax/detection/run.py` hard-codes `ClassicalDetector()` (line 259). It computes a run-level radiometric fit (`_fit_for_run`) and scores in batches of `BATCH_SIZE = 200`. Indicators land in `run_parcels.indicators` (JSONB), so the segmenter's different indicator keys need no schema change there.
- **Runs API:** `backend/src/ptax/api/runs.py` takes `threshold` and `min_new_area_m2` per run; the segmenter uses the same score curve, so both keep their meaning. The Runs page is `frontend/src/pages/RunsPage.tsx`, with its client in `frontend/src/api/runs.ts`. Migrations are Alembic, `backend/alembic/versions/0001`–`0004`.
- **Image:** one image, `backend/Dockerfile`, serves both the API and the worker, built for amd64 from an arm64 Mac, with `uv sync --frozen --no-dev`. The ML libraries sit in the optional `[dependency-groups] ml`. **On Linux x86_64 the default PyPI torch wheel pulls CUDA libraries (several GB)**; the CPU-only wheel from PyTorch's CPU index is far smaller. Choosing the wheel source is part of this work.
- **Compute:** `infra/lib/compute-stack.ts` runs the API at 1 vCPU / 2 GB and the worker at 2 vCPU / 4 GB on Fargate, which has no GPUs. The weights file is 93 MB. The existing S3 uploads bucket (`infra/lib/data-stack.ts`) is a candidate store if the weights are not baked into the image.
- **Measured so far (dev machine, not Fargate):** `compare` averages 73.6 ms per parcel on one thread and 46.8 ms on twelve. Reading both years costs a further ~54 ms. A 200 000-parcel run is ~4 CPU-hours of inference, ~7 with reads. A Fargate vCPU is likely slower than an M4 core.
- **Known model limits:**
  - The product target (≤2% flagged at ≥60% precision) is met only on the point estimate, from 14 sampled parcels.
  - Segmenter markup sometimes spills from a roof onto the driveway beside it, which inflates `structure_m2`.
  - The one top-20 false positive was a house the model missed in the 2010 capture.
  - The model was judged on one labelled area only.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Which model ships | The frozen `segmenter-v1`, verified by sha256 at run start | The gate judged that exact file; anything else is an ungated model |
| Selection | Per run, stored on the run | Lets the two detectors run side by side on real counties, and keeps each run's provenance |
| Completed runs | Never re-scored | A run must keep showing the picture its scores were computed from |
| Hardware | CPU on Fargate | Measured cost is ~74 ms/parcel; GPUs are not available on Fargate and are not needed at that cost |
| Classical detector | Kept | Fallback, and the comparison every future model is measured against |

## Open Questions

1. **Default detector.** Does the segmenter become the default for new runs in this plan, or only after a second labelled evaluation area confirms it generalises beyond northwest Hennepin? The ≤2% operating point rests on 14 parcels here.
2. **Image layout.** Should there be a separate worker image with the ML stack, keeping the API image as it is, or one image for both? Two images mean two builds and pushes per deploy; one image adds CPU torch (hundreds of MB) to the API's cold start.
3. **Weights delivery.** Bake the 93 MB weights into the worker image, which is simplest and versioned with the code, or fetch them from S3 at worker start with the sha256 check, which keeps them out of the image and swaps them without a rebuild?
4. **Default operating point.** Keep the classical defaults (threshold 0.3, minimum structure 37.2 m²), which give 14% flagged at 61% precision for the segmenter, or ship detector-specific defaults (for example 0.4: 7% flagged at 86%)?
5. **Driveway spill.** Accept it for now, since it moves scores but not which parcels rank highest, or clip the structure mask before storing it, since the viewer will show it to reviewers?

## Outcome (2026-09-23)

- **Default detector:** the segmenter, by the user's decision during planning. Runs created before the change read back as `classical`, and a run can still choose `classical`. This replaces the Must row "runs created without a detector choice behave exactly as today".
- **Deployed:** `us-east-1`, revision `:7`, migration 0005 applied. A default run over the demo tenant's 25 parcels (NAIP 2010→2021) recorded `segmenter-v1` and its weights hash, and succeeded.
- **Scores match `ptax-eval`:** CPU inference, which Fargate runs, reproduces the gated MPS result exactly on all 300 evaluation parcels.
- **Fargate cost:** about 543 ms per parcel with reads (classical: 131 ms) and 733 MB peak on a 2 vCPU / 4 GB worker, so a 200 000-parcel run takes about 30 hours on one worker. Worker size is unchanged; county-scale throughput needs more workers, not more memory.
- **Images:** API image torch-free at 1.85 GB; worker image 4.16 GB with CPU-only torch.
- **Answered open questions:** 1, default = segmenter; 2, separate worker image; 3, weights from S3 with a sha256 check; 4, threshold 0.3 for both; 5, driveway spill accepted.
- **Still open:** a second labelled evaluation area, and how many workers a county run should use.

