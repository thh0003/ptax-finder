# Parcel Improvement Detection

Created: 2026-09-24
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Final
Research: Quick

Supersedes: `docs/prd/2026-09-23-imagery-sources-and-local-vlm-scanner.md` and its plan `docs/plans/2026-09-23-county-arcgis-structure-inventory.md`.

## Problem Statement

County assessors using ArcGIS need to find parcels whose **improvements are new or expanded** between two imagery years, so they can assess them. Missing one is lost revenue. Reviewing a false one costs assessor time. Today ptax-finder can load a county's ArcGIS parcels and imagery, but it cannot find structures reliably. Its classical colour-and-texture detector and a learned segmenter both fell short, and so did the chat-and-vision models and ground-level object detectors tried on 2026-09-24. This product turns ptax-finder into a multi-tenant AWS pipeline built on models trained for overhead imagery. For each county run it:

1. identifies the improvements on every parcel in the **Year A** (base) imagery;
2. identifies them in the **Year B** (new) imagery;
3. flags the parcels with new or expanded improvements, with estimated square footage and confidence.

The results are written back into the county's own ArcGIS portal, where assessors review them.

## Core User Flows

### Flow 1: Onboard a county (operator)
1. The operator records the tenant's configuration:
   - its portal (ArcGIS Online or Enterprise), with OAuth app credentials held in Secrets Manager;
   - the Year A and Year B imagery, as an image service, a tiled imagery layer, or files dropped in S3;
   - the parcel service with its PIN field and filter;
   - optionally, a building-footprint layer and a CAMA extract;
   - the CRS, the thresholds, and the publish target.
2. The system validates the configuration and the credentials, and reports anything unreachable.

### Flow 2: Run a county (operator)
1. The operator starts a run for a tenant and two imagery years, with one command or API call.
2. The pipeline:
   - ingests the imagery and parcels;
   - preprocesses them: COGs, a common CRS and GSD, Year B co-registered and radiometrically normalised to Year A, and per-parcel chips;
   - runs segmentation on both years, and the change model when one is available;
   - reconciles the detections parcel by parcel;
   - optionally writes a short note per flagged parcel;
   - publishes the results to the county portal.
3. The operator sees the run's status and metrics: parcels processed, flags by status, runtime, cost, and failed tiles or batches. A partial failure still publishes everything else.

### Flow 3: Review in ArcGIS (assessor)
1. The assessor opens the Experience Builder review app in their own portal. It shows a Year A / Year B swipe, a filtered list of flagged parcels, and a review form.
2. For each flagged parcel they see the new or expanded detections, their square footage and confidence, and optionally a note. They set `review_status` (accepted or rejected), `reviewer` and `review_comment`.
3. Re-publishing a run never overwrites those review fields.

### Flow 4: Feedback and retraining (operator / ML)
1. A scheduled sync copies review edits from the county portal into PostGIS.
2. Accepted and rejected reviews feed the parcel-level metrics and the next training round.
3. A fine-tuned model is registered, and a tenant can pin a model version.

## Scope

### In Scope
Built in the spec's phase order. Each phase ends with passing tests and a README update.

- **Phase 0 — Foundations:**
  - tenant configuration with validation (pydantic), including optional footprints, CAMA and thresholds;
  - schema migrations for the data model below;
  - the AWS additions the pipeline needs: KMS-encrypted per-tenant S3 prefixes, Secrets Manager per-tenant ArcGIS credentials, PostGIS;
  - CI: lint, type check, tests.
- **Phase 1 — Reconciliation (no ML).** Covers vectorising, clipping to the parcel (centroid inside), area in square feet, and A↔B matching by IoU with a misregistration tolerance. Each Year B detection is classified as `new`, `expanded`, `unchanged`, `removed` or `uncertain`. When a tenant has CAMA, `new` and `expanded` detections it already records are marked `already_assessed`. Each parcel gets a status (`high_confidence`, `needs_review` or `no_change`) and a new-square-footage estimate. All thresholds come from tenant config, and unit tests cover every branch with synthetic polygons.
- **Phase 2 — Ingest and preprocess:**
  - file mode first, then service mode, with concurrency limits, retries and backoff;
  - parcels stored as GeoParquet in the tenant CRS;
  - COG conversion, a common CRS and GSD, co-registration with per-tile residual shift and QC flags, per-tile radiometric normalisation, and a chip manifest (Parquet);
  - optional inputs (footprints, CAMA) never fail a run.
- **Phase 3 — Baseline inference.** Pretrained, off-the-shelf models trained on overhead imagery, behind a SageMaker Batch Transform handler: instance segmentation over the improvement classes, plus the bitemporal change model if one is ready. Done when an end-to-end run on sample data writes detections and parcel changes to PostGIS. Reconciliation works with or without change masks.
- **Phase 4 — Publish and feedback:**
  - hosted feature layers in the county portal: a detections layer, and a parcel-changes layer or table;
  - upserts keyed by `(pin, run_id)`, preserving the review fields;
  - by default only flagged parcels and their detections are published (configurable);
  - review sync into PostGIS;
  - an Experience Builder setup guide, plus item JSON where feasible.
- **Phase 5 — Orchestration.** A Step Functions state machine over all stages, with Distributed Map fan-out, per-batch retries, per-run status and cost-allocation tags. One command or API call runs a whole tenant.
- **Phase 6 — Training and evaluation:**
  - label bootstrapping from the county's footprints when available, otherwise SAM-family pre-labels;
  - correction in SageMaker Ground Truth;
  - pretraining on public aerial datasets (WHU Aerial, Inria), then per-county fine-tuning;
  - Model Registry, with tenant pinning;
  - a parcel-level evaluation report.
- **Phase 7 — Enrichment and docs.** Optional Bedrock notes per flagged parcel; a tenant onboarding guide; the Experience Builder setup guide.
- **Improvement classes (v1, in config):** `primary_structure`, `garage_outbuilding`, `shed`, `pool`, `deck_patio`, `driveway_paved`, `solar_array`, `ag_building`.

### Explicitly Out of Scope
- **Valuation or assessment calculations.** The product flags parcels; assessors assess.
- **A new standalone review UI.** Assessors review in ArcGIS Experience Builder. The existing SPA is not extended for review.
- **Counties not on ArcGIS.** Every input and output goes through an ArcGIS portal or an S3 file drop.
- **Real-time inference.** Runs are batch jobs of up to 24 hours.
- **An on-premises agent for Enterprise behind a firewall.** File mode is the fallback in v1.
- **The superseded qwen3-vl inventory work** as a product path. See Key Decisions.

## Technical Context

- **Existing codebase (ptax-finder), which this evolves:**
  - `backend/` is Python 3.13 with uv: a FastAPI API (`ptax.api`), a Postgres-backed job queue and worker (`ptax.jobs`, `ptax.worker`), and the operator CLIs `ptax-admin` and `ptax-eval`.
  - `frontend/` is a React SPA using Cognito sign-in.
  - `infra/` is AWS CDK in **TypeScript**, with stacks `PtaxNetwork`, `PtaxData` (Aurora PostgreSQL 16 with PostGIS, and an uploads bucket), `PtaxAuth` (Cognito) and `PtaxCompute` (ECS Fargate API behind an ALB, plus the worker).
  - Tenancy is a `tenant_id` column across tables, with Cognito users bound to tenants. There is no row-level security yet.
  - Production (us-east-1) runs task-definition revision `:7`, which still carries the removed segmenter until the next deploy.
- **What already exists that the pipeline builds on:**
  - **County profiles** (`backend/counties/*.json`) with ArcGIS parcel loading (`ptax.county.parcels`) and tile-cache mosaicking into COGs (`ptax.county.imagery`), on branch `feat/county-arcgis-structure-inventory`, uncommitted;
  - COG storage and asset registration (`ptax.imagery.ingest`, `ptax.imagery.cog`);
  - per-parcel raster reads (`ptax.imagery.reader`);
  - `runs` and `run_parcels` tables, with `change` and `inventory` kinds;
  - the classical detector;
  - the `ptax-eval` labelling-sheet and scoring tools.
- **Measured facts about county ArcGIS imagery (Peoria, 2026-09-23/24):**
  - `MapServer?f=json` cannot be opened by GDAL (HTTP 400).
  - The 2024 service's `export` returned a blank image while its tile cache served real tiles. Service-mode ingest therefore cannot rely on `exportImage` alone.
  - Richwoods 2015 (0.15 m) took 30.5 min through the tile cache: 16,674 tiles, fetched sequentially with a 50 ms delay.
- **Measured facts about models on this imagery (seven Richwoods parcels judged by the user, 2026-09-24):**
  - **Chat-and-vision models** (Claude Sonnet 4.6, Llama 4 Maverick, Nova Pro/2 Lite, Pixtral, Mistral Large 3, Qwen3-VL 235B, local qwen3-vl) found the main building on most parcels. Their boxes were loose or misplaced; they merged buildings, missed small ones, and flagged cars and curb objects.
  - **Off-the-shelf detectors trained on ground-level photos** (Amazon Rekognition, OWLv2, Grounding DINO) found little. Rekognition labelled roofs "Airplane" and "Bird".
  - This is the evidence for the spec's choice of models trained on overhead imagery.
- **Label sources:**
  - Peoria publishes `Building_Outlines`: 143,108 footprints digitised from its spring-2019 6-inch imagery. It is usable as training labels under the rule below.
  - The user's 200-parcel Richwoods sample and its labelling sheets exist (`backend/eval/inventory-*-peoria-richwoods-2015.json`), and can seed a labelled holdout.
- **Constraints:**
  - Scale: a 600 sq mi county at 6-inch GSD (about 200 GB per year raw) completes a run within 24 hours.
  - Tenant isolation covers S3 prefixes with IAM conditions, the database, and secrets; there are no cross-tenant queries.
  - KMS at rest, TLS in transit, least-privilege IAM per stage, and no county credentials in logs.
  - Spot capacity where possible.
  - Every stage is idempotent and resumable per batch.
  - Area math is in square feet in the county's projected CRS.

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| Where it is built | **Evolve ptax-finder** (user) | Reuses its tenants, auth, ArcGIS ingest, PostGIS and infra instead of starting over. |
| Earlier structure-inventory PRD and plan | **Superseded** (user) | Their chat-and-vision approach failed the user's review; this PRD replaces it. The finished loaders are reused. |
| County building footprints | **Training labels only, when a county has them; otherwise we label ourselves** (user) | Run-time detection uses imagery alone. Footprints only speed up labelling. |
| CAMA | **Optional, after detection only** (user) | Marks already-assessed structures; never used to find structures. |
| IaC language | **Keep TypeScript CDK** (assumption) | The spec says CDK in Python, but the existing four stacks are TypeScript and evolving the repo means extending them. Confirm or override at review. |
| Python version | **Keep 3.13** (assumption) | The spec says 3.12; the codebase runs 3.13. SageMaker containers pin their own runtime either way. |
| Tenant isolation | **`tenant_id` column plus row-level security** (assumption) | The spec allows this instead of schema-per-tenant; the codebase already keys every table on `tenant_id`. |
| Existing SPA | **Operator console only** (assumption) | Tenant setup, runs and status stay in it. Assessor review moves to ArcGIS, and the SPA's parcel viewer is not extended. |
| Existing classical detector and inventory runs | **Kept until the pipeline replaces them; not extended** (assumption) | Avoids breaking the deployed app mid-migration. Whether to remove them is decided in `/spec`. |
| Run orchestration | **Step Functions for the run pipeline** (spec) | Required by the spec for fan-out and per-stage retries. Whether the existing Postgres job queue keeps its small jobs is decided in `/spec`. |
| Models | **Instance segmentation (Mask2Former or Mask R-CNN) and a bitemporal change model (ChangeFormer or BIT family), trained on overhead imagery** (spec defaults) | Consistent with the 2026-09-24 evidence: general and ground-level models do not localise structures in this imagery. |
| Improvement classes | **The spec's eight classes, in config** (spec) | Replaces the interim "presence only" rule, which was agreed for the superseded prompt-tuning work. |
| Success metrics | **Parcel-level:** recall of truly improved parcels (primary), precision of flagged parcels, metrics per status bucket, absolute error of `new_sqft_est`, and per-class detection precision and recall. Targets are set per tenant after a pilot. (spec) | Missed parcels are lost revenue; false flags are review burden. |

## Open Questions

Stubbed behind config; they do not block.

1. The mix of counties on ArcGIS Online versus Enterprise, which sets the priority of the file-drop fallback.
2. Typical imagery resolution and format, and whether both years come from the same vendor and are true orthos.
3. Whether the NIR band is available per county.
4. The CAMA extract format and field mapping per county.
5. Whether the Experience Builder app is delivered as an automated item or a manual guide.
6. The confirmation of the four assumptions above: TypeScript CDK, Python 3.13, `tenant_id` with row-level security, and the SPA as operator console only.
