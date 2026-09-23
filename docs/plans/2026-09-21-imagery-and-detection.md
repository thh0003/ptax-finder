# Parcel Structure Change Detection — Plan B: Imagery and Detection

Created: 2026-09-21
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** A county admin can see which NAIP years cover their county and ingest them, upload GeoTIFF/COG orthoimagery for newer years and see coverage gaps, start a base-year vs target-year comparison run, watch it progress, and get a per-parcel change score and candidate flag for every parcel with imagery in both years (PRD Flows 3 and 4).

Second of three linked plans for `docs/prd/2026-09-21-parcel-structure-change-detection.md`. Builds on Plan A (`docs/plans/2026-09-21-parcel-structure-change-detection.md`, VERIFIED): tenancy, `jobs` runner, presigned uploads, `parcels`. Plan C (`docs/plans/2026-09-21-review-workflow.md`) consumes this plan's `runs`, `run_parcels`, tile, and preview endpoints.

## Out of Scope

- Direct provider API adapters (Nearmap, EagleView, Vexcel, Vantor, Planet) — the `ImagerySource` protocol leaves room for them; v1 has NAIP and upload only.
- ML detector — v1 is classical raster change detection (decided during Plan A); a learned detector is a later plan behind the same `Detector` interface.
- Oblique/3D/DSM inputs; detecting demolitions, pools, or solar.
- Deleting imagery years or runs — retained indefinitely in v1; a failed year can be retried in place.
- Sub-pixel co-registration between years — both years are resampled onto one grid and small offsets are absorbed by morphological cleaning of the change masks; a registration step is a Deferred Idea.
- Recovering a job whose worker was SIGKILLed mid-item (run batch, NAIP item, or upload asset) — all three jobs resume from their last commit when re-queued and re-queue themselves on a graceful stop, but detecting a job left `running` by a hard kill is a Plan A worker concern, not added here.

## Approach

**Chosen:** Imagery years are tenant-scoped sets of Cloud Optimized GeoTIFFs in the existing uploads bucket (`imagery_assets` rows with 4326 bounds), produced by two worker jobs behind one `ImagerySource` seam — `NaipStacSource` (Earth Search STAC discovery + requester-pays `naip-analytic` reads) and `FixtureNaipSource` (committed synthetic COGs for local dev/tests) — plus an upload job that validates and converts county orthos. rio-tiler reads those COGs for map tiles, per-parcel previews, and the comparison run, which resamples both years onto a common UTM grid per parcel and scores it with a numpy `ClassicalDetector`.
**Why:** One COG store backs tiles, previews, and detection, so each year is copied once (clipped to the county) instead of paying NAIP cross-region egress per run or per tile view; the cost is an up-front ingest per year and a fixture source so the whole flow runs locally without AWS credentials.

## Global Constraints

- All Plan A Global Constraints still apply (Python 3.13/uv, PostGIS 16-3.4, `/api` prefix, Cognito access-token auth, `admin` | `reviewer` roles, local ports).
- New backend deps: `rasterio>=1.5`, `rio-tiler>=9`, `rio-cogeo>=7`, `numpy>=2`. No scipy, no PIL, no TiTiler.
- `imagery_years.source` values: `naip` | `upload`. `imagery_years.status` values: `queued` | `processing` | `ready` | `failed`.
- `imagery_assets.status` values: `pending` | `ready` | `failed`.
- `runs.status` values: `queued` | `running` | `succeeded` | `failed` | `cancelled`.
- Job type strings: `imagery.ingest_naip`, `imagery.ingest_upload`, `imagery.recompute_coverage`, `run.execute`.
- Graceful worker stop: long handlers check `stop_requested()` at their commit boundaries and raise `JobInterrupted`; the worker re-queues such a job with `requeue(db, job)`, which does **not** count against `MAX_ATTEMPTS`.
- Upload purpose strings (`storage.UPLOAD_PURPOSES`): `parcel_layer` | `imagery`. Imagery upload keys: `tenants/{tenant_id}/imagery/{upload_id}/{filename}`.
- Stored imagery objects: `tenants/{tenant_id}/imagery/{year_id}/{asset_id}.tif` — COG, `uint8`, 3 (RGB) or 4 (RGB+NIR) bands, DEFLATE, 512 px blocks, overviews, nodata `0`.
- Upload acceptance: `.tif` / `.tiff`; ≤ 5 GB per file (single presigned PUT); must carry a CRS; 3 or 4 bands (extra bands beyond 4 are dropped, fewer than 3 rejected); `uint8` or `uint16` (16-bit is stretched to 8-bit by 2–98 percentile per band); ground resolution ≤ 1.0 m/px.
- NAIP: STAC `NAIP_STAC_URL` default `https://earth-search.aws.element84.com/v1`, collection `naip`; assets read from `s3://naip-analytic/...` with `AWS_REQUEST_PAYER=requester` in region `us-west-2`; ingest is refused when the estimate exceeds `NAIP_MAX_INGEST_GB` (default `60`).
- New backend env vars: `NAIP_SOURCE` (`stac` | `fixture`; local default `fixture`, CDK sets `stac`), `NAIP_STAC_URL`, `NAIP_FIXTURE_DIR` (default `tests/fixtures/imagery`), `NAIP_MAX_INGEST_GB`, `NAIP_AWS_REGION` (default `us-west-2`).
- Tiles: WebMercatorQuad, 256 px PNG, `GET /api/tiles/{year_id}/{z}/{x}/{y}.png`; `204` when no asset intersects the tile.
- Detector defaults: `threshold = 0.3`, `min_new_area_m2 = 40`, comparison resolution `max(base_res, target_res, 0.5)` m coarsened so a parcel window never exceeds 4,000,000 pixels, `SCORE_SCALE_M2 = 200`.
- Run batching: 200 parcels per committed batch, ordered by `ST_GeoHash(ST_Centroid(geom))` for block-cache locality.

## Context for Implementer

GDAL, not boto3, reads the COGs: rio-tiler/rasterio open `s3://{bucket}/{key}` through `/vsis3/`, so every read runs inside `rasterio.Env(**gdal_env(settings))` (Task 1's `ptax/imagery/gdal.py`). Locally that env points GDAL at MinIO (`AWS_S3_ENDPOINT=localhost:9000`, `AWS_HTTPS=NO`, `AWS_VIRTUAL_HOSTING=FALSE`, static keys); in AWS it is empty and the task role signs. NAIP reads use a second env (`naip_env`) that adds `AWS_REQUEST_PAYER=requester` and `AWS_REGION=NAIP_AWS_REGION`. Forgetting the env is the #1 way a test passes locally and fails in AWS or vice versa.

Plan A left the county footprint derived (`ST_Union` over the current layer's parcels) with no stored geometry. This plan persists it as `parcel_layers.footprint`, filled on first use by `footprint_for(db, tenant)` (Task 1) and thereafter read — a 100k-parcel union takes seconds and is needed by discovery, both ingest jobs, and the coverage report. Everything spatial in this plan is EPSG:4326 in the database and a local UTM zone (`32600 + zone`, from the footprint centroid's longitude) for anything measured in metres.

Tiles and previews are authenticated like every other route (Bearer token). Browsers cannot attach headers to `<img>` requests, so Plan B's thumbnail fetches with `apiFetchBlob` (Task 8) and Plan C's MapLibre raster source must use `transformRequest` to add the header; there are no signed or public tile URLs.

A run is pinned to `runs.layer_id`, the tenant's current layer at start time, so a later parcel re-upload neither changes nor breaks a retained run. `run_parcels.parcel_ref` is stored alongside `parcel_id` so Plan C can join review history across re-ingests by `(tenant_id, parcel_ref)` as Plan A's context note requires.

## Runtime Environment

- **Start:** as Plan A — `make dev-up`, `make seed`, `make api`, `make worker`, `make web`. No new containers; `NAIP_SOURCE` defaults to `fixture`, so the Imagery page lists the fixture years (2021, 2023) with no AWS credentials.
- **E2E precondition:** the Plan A fixture parcel layer is ingested (Plan A TS-002: upload `backend/tests/fixtures/parcels_small_26915.zip`, field `PIN`); the imagery fixtures cover exactly that grid.
- **Health:** `GET http://localhost:8000/api/health` → `{"status":"ok","db":"ok"}`.
- **Restart:** `make dev-down && make dev-up && make seed`.

## File Structure

- `backend/pyproject.toml` (modify) — add rasterio, rio-tiler, rio-cogeo, numpy.
- `backend/uv.lock` (modify) — lockfile for the above.
- `backend/alembic/versions/0002_imagery_and_runs.py` (create) — `parcel_layers.footprint`, `imagery_years`, `imagery_assets`, `runs`, `run_parcels`.
- `backend/src/ptax/db/models.py` (modify) — `ImageryYear`, `ImageryAsset`, `Run`, `RunParcel` ORM models; `footprint` on `ParcelLayer`; status tuples.
- `backend/src/ptax/config.py` (modify) — NAIP settings.
- `backend/src/ptax/parcels/footprint.py` (create) — `footprint_for(db, tenant) -> shapely geometry`, `utm_epsg_for(geom) -> int`.
- `backend/src/ptax/imagery/__init__.py` (create).
- `backend/src/ptax/imagery/gdal.py` (create) — `gdal_env(settings)`, `naip_env(settings)`, `s3_uri(settings, key)`.
- `backend/src/ptax/imagery/sources.py` (create) — `NaipItem`, `NaipYear` dataclasses, `ImagerySource` protocol, `get_naip_source(settings)`.
- `backend/src/ptax/imagery/naip.py` (create) — `NaipStacSource`: STAC paging, grouping by year, size estimate.
- `backend/src/ptax/imagery/fixture.py` (create) — `FixtureNaipSource` over `NAIP_FIXTURE_DIR`.
- `backend/src/ptax/imagery/cog.py` (create) — `to_cog(src_path, dst_path, *, clip_bounds=None)`, `is_cog(path)`, `RasterInfo`/`inspect_raster(path)`, 16→8-bit stretch.
- `backend/src/ptax/imagery/ingest.py` (create) — `imagery.ingest_naip` and `imagery.ingest_upload` job handlers, `register_asset`, `compute_coverage`.
- `backend/src/ptax/imagery/reader.py` (create) — `assets_intersecting(db, year_id, bounds)`, `read_parcel(...)`, `read_tile(...)`, `read_bounds_preview(...)` over rio-tiler mosaics.
- `backend/src/ptax/detection/__init__.py` (create).
- `backend/src/ptax/detection/detector.py` (create) — `Detector` protocol, `ParcelRaster`, `ChangeResult`, `ClassicalDetector`.
- `backend/src/ptax/detection/run.py` (create) — `run.execute` job handler (batches, resume, cancel, summary).
- `backend/src/ptax/api/imagery.py` (create) — `/api/imagery/...` routes (NAIP discovery/ingest, upload years, assets, finalize, thumbnail, preview).
- `backend/src/ptax/api/tiles.py` (create) — `/api/tiles/{year_id}/{z}/{x}/{y}.png`.
- `backend/src/ptax/api/runs.py` (create) — `/api/runs` create/list/get/cancel.
- `backend/src/ptax/api/uploads.py` (modify) — accept purpose `imagery`.
- `backend/src/ptax/storage.py` (modify) — `UPLOAD_PURPOSES`, `imagery_asset_key`.
- `backend/src/ptax/main.py` (modify) — mount new routers; import handler modules.
- `backend/src/ptax/worker.py` (modify) — import new handler modules; `stop_requested()`; `JobInterrupted` handling.
- `backend/src/ptax/jobs/queue.py` (modify) — `JobInterrupted`, `requeue(db, job)`.
- `backend/src/ptax/parcels/ingest.py` (modify) — enqueue `imagery.recompute_coverage` after a layer becomes current.
- `backend/tests/fixtures/make_imagery_fixtures.py` (create) — generates the three rasters below deterministically.
- `backend/tests/fixtures/imagery/naip_2021.tif`, `naip_2023.tif`, `ortho_2025_partial.tif` (create) — committed synthetic COGs (< 1 MB each).
- `backend/tests/fixtures/stac_naip_pages.json` (create) — two trimmed Earth Search response pages for the fixture bbox (first page has a `next` link).
- `backend/tests/conftest.py` (modify) — `ingested_layer` fixture (parcel fixture ingested through the real jobs); `settings` fixture points `NAIP_FIXTURE_DIR` at the imagery fixtures.
- `backend/tests/test_migrations.py` (modify) — 0002 up/down.
- `backend/tests/test_footprint.py`, `test_imagery_naip.py`, `test_imagery_ingest.py`, `test_imagery_upload.py`, `test_tiles.py`, `test_detector.py`, `test_runs.py` (create).
- `frontend/src/api/uploads.ts` (create) — `putPresigned(file, purpose, contentType, onProgress)` shared by parcel and imagery uploads; `apiFetchBlob`.
- `frontend/src/api/parcelLayers.ts` (modify) — use `putPresigned`.
- `frontend/src/api/imagery.ts` (create) — types and calls for imagery years, NAIP, uploads, thumbnails.
- `frontend/src/api/runs.ts` (create) — types and calls for runs.
- `frontend/src/components/UploadDropzone.tsx` (modify) — `accept`, `multiple`, `label` props.
- `frontend/src/components/StatusChip.tsx` (create) — generic status chip (imagery years, runs); `LayerStatus.tsx` becomes a thin wrapper.
- `frontend/src/components/LayerStatus.tsx` (modify) — delegate to `StatusChip`.
- `frontend/src/pages/ImageryPage.tsx` (create), `frontend/src/pages/RunsPage.tsx` (create).
- `frontend/src/components/Layout.tsx`, `frontend/src/App.tsx` (modify) — routes and nav.
- `frontend/src/api/imagery.test.ts` (create) — vitest for the sequential multi-file upload and finalize ordering.
- `infra/lib/compute-stack.ts` (modify) — NAIP read policy, `NAIP_SOURCE=stac`, `NAIP_AWS_REGION`.
- `infra/test/stacks.test.ts` (modify) — assertions for the above.
- `README.md` (modify) — imagery flow, fixture source, NAIP cost note, new env vars.
- `Makefile` (modify) — `imagery-fixtures` target.

## Assumptions

- Earth Search's `naip` collection keeps serving items whose `assets.image.href` is an `s3://naip-analytic/.../rgbir_cog/*.tif` and whose properties include `naip:year`, `gsd`, `proj:shape`, `proj:epsg` (observed 2026-09-21 for the fixture bbox, years up to 2023). — Tasks 2, 3 depend on this; `NAIP_STAC_URL` is configurable if the catalog moves.
- rasterio 1.5.1 `cp313` wheels (macOS arm64, manylinux_2_28 x86_64) run on `python:3.13-slim` without apt GDAL, as pyogrio's do today. — Task 1 depends on this.
- A Fargate worker with 4 GB memory and the default 20 GB ephemeral disk holds one clipped DOQQ temp COG (≤ 1 GB) at a time. — Task 3 depends on this.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| NAIP lives in `us-west-2` and the stack in `us-east-1`; a county year is tens of GB of requester-pays egress (~$0.02/GB) plus long transfer time | Certain | Medium | Task 2 shows an estimate per year before ingest; Task 3 refuses ingests over `NAIP_MAX_INGEST_GB` with a `422` naming the cap, clips each tile to the footprint bbox, and streams block-by-block so memory stays flat. |
| Real NAIP ingest cannot be exercised in this session (no AWS credentials) | Certain | High | Every NAIP path is tested against the fixture source and a recorded STAC transport; Task 10 is a user-owned run against the deployed stack with exact commands, as Plan A did for deploy. |
| A full-county run or NAIP ingest takes hours; Fargate stops the worker with SIGTERM + 30 s, and repeated deploys would exhaust the shared 3-attempt job budget | High | High | Task 3 adds `JobInterrupted`/`requeue` (no attempt charged); Task 7 commits every 200 parcels and resumes by skipping parcels that already have `run_parcels` rows, Tasks 3/4 resume per item/asset; a cancelled run stops at the next batch boundary; a run whose job fails for any other reason is marked `failed` so the UI never polls a dead run. |
| Classical thresholds tuned on synthetic fixtures under- or over-flag real imagery | High | Medium | Thresholds are run parameters with defaults (`threshold`, `min_new_area_m2`), indicators are stored per parcel so Plan C's queue can be re-sorted without re-running, and the score is a saturating function of new built-up *area* (not parcel fraction) so rural parcel size does not hide a house. |
| Uploaded orthos arrive as hundreds of tiles per year and multi-GB files | High | Medium | Task 4 accepts many assets per year, uploads them sequentially with per-file progress, and processes them in one resumable job; the 5 GB single-PUT limit is stated in the UI. |

## E2E Test Scenarios

> Driver notes carry over from Plan A: Vite's port is whatever `make web` prints; inputs are React-controlled (type with keyboard events); refs go stale on the 2 s poll — re-find right before clicking. Preconditions for every scenario: `make seed` has run, API + worker + web are up, and the Plan A fixture parcel layer is `ready` (Plan A TS-002).

### TS-001: Admin sees NAIP years and ingests one
**Priority:** Critical
**Preconditions:** Signed in as `admin@demo.test`; no imagery years yet.
**Mapped Tasks:** Task 2, Task 3, Task 5, Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Click **Imagery** in the nav | `/imagery` shows "No imagery years yet" and a **NAIP on AWS** section listing `2021` and `2023`, each with coverage `100%`, `1 tile`, resolution `1.0 m`, and an estimated size |
| 2 | Click **Ingest** on `2021`, confirm the dialog showing the estimate | Row `2021 · NAIP` appears in **Imagery years** with status `queued`, then `processing`, then `ready` within 20 s |
| 3 | Read the `2021` row | Shows `4 bands`, `1.0 m`, coverage `100%`, `0 parcels without coverage`, and a thumbnail image renders (non-blank) |
| 4 | In the NAIP section, `2021` | Shows `ingested` and its **Ingest** button is disabled |

### TS-002: Admin uploads a partial ortho year and sees the gap report
**Priority:** Critical
**Preconditions:** TS-001 completed.
**Mapped Tasks:** Task 4, Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | In **Upload imagery**, type year `2025`, provider `Nearmap`, click **Create year** | Row `2025 · upload (Nearmap)` appears with status `queued` and a dropzone labelled "GeoTIFF/COG files for 2025" |
| 2 | Upload `backend/tests/fixtures/imagery/ortho_2025_partial.tif` | Per-file progress bar completes; the file is listed under the year as `pending` |
| 3 | Click **Finish upload** | Status `processing` then `ready` within 20 s |
| 4 | Read the `2025` row | `3 bands`, `0.5 m`, coverage `60%`, `10 parcels without coverage`, thumbnail renders |

### TS-003: Invalid imagery file fails with a reason naming the file
**Priority:** High
**Preconditions:** Signed in as `admin@demo.test`.
**Mapped Tasks:** Task 4, Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Create year `2026`, provider blank; upload a text file renamed `bogus.tif`; click **Finish upload** | Status `failed`; message contains `bogus.tif` and "not a readable GeoTIFF" |
| 2 | Upload `naip_2023.tif` to the same year and click **Finish upload** again | Status `ready`; the failed asset stays listed as `failed`, the new one as `ready` |
| 3 | Try to drop `parcels_small.geojson` on the imagery dropzone | Rejected client-side: "is not a GeoTIFF (.tif/.tiff)" |

### TS-004: Admin starts a run and watches it complete
**Priority:** Critical
**Preconditions:** TS-001 done; additionally ingest NAIP `2023` (same steps) so `2021`, `2023`, `2025` are `ready`.
**Mapped Tasks:** Task 6, Task 7, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Click **Runs** in the nav | `/runs` shows a form with **Base year** and **Target year** selects listing only ready years, and "No runs yet" |
| 2 | Select base `2021`, target `2023`, click **Start run** | A run row appears: `2021 → 2023`, status `queued` then `running` with a progress bar `n / 25`, then `succeeded` within 30 s |
| 3 | Read the finished row | `25 processed · 3 candidates · 0 skipped` |
| 4 | Start `2023 → 2025` | Finishes `succeeded` with `25 processed · 1 candidate · 10 skipped` (processed counts scored + skipped) |
| 5 | Select base `2023`, target `2021`, click **Start run** | Inline error "Target year must be later than base year"; no run created |
| 6 | Reload the page | Both runs persist in the table, newest first |

### TS-005: Reviewer sees imagery and runs read-only
**Priority:** High
**Preconditions:** TS-004 completed; signed in as `reviewer@demo.test`.
**Mapped Tasks:** Task 8, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `/imagery` | Years table and NAIP availability are visible; no **Ingest**, **Create year**, dropzone, or **Finish upload** controls |
| 2 | Navigate to `/runs` | Runs table visible with both runs; no start form or **Cancel** buttons |

### TS-006: Tenants are isolated for imagery and runs
**Priority:** Critical
**Preconditions:** TS-004 completed.
**Mapped Tasks:** Task 2, Task 4, Task 7

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Sign in as `admin@other.test` | Header `Other County` |
| 2 | Navigate to `/imagery` | "No parcel layer yet — upload parcels before imagery"; no years listed |
| 3 | Navigate to `/runs` | "No runs yet"; the start form explains a parcel layer and two ready years are needed |

## Progress Tracking

- [x] Task 1: Imagery and run schema, models, footprint helper, raster deps
- [x] Task 2: `ImagerySource` seam, NAIP STAC discovery, fixture source, availability API
- [x] Task 3: NAIP ingest job with cost guard and coverage
- [x] Task 4: Upload imagery years: validation, COG conversion, coverage gap report
- [x] Task 5: Raster reader, tile endpoint, thumbnails, per-parcel previews
- [x] Task 6: `Detector` interface and `ClassicalDetector`
- [x] Task 7: Runs API and resumable run job
- [x] Task 8: Frontend Imagery page
- [x] Task 9: Frontend Runs page
- [x] Task 10: Verify real NAIP ingest against the deployed stack (user)

## Implementation Tasks

### Task 1: Imagery and run schema, models, footprint helper, raster deps

**Objective:** Add the tables this plan and Plan C persist through — `imagery_years`, `imagery_assets`, `runs`, `run_parcels` — plus the persisted county footprint on `parcel_layers`, the ORM models, the settings for NAIP, the GDAL environment helper, and the raster dependencies. After this task the schema is at `0002`, `footprint_for()` returns the county polygon for a tenant, and `uv run python -c "import rasterio, rio_tiler, rio_cogeo"` works.

**Files:**

- Create: `backend/alembic/versions/0002_imagery_and_runs.py`
- Create: `backend/src/ptax/parcels/footprint.py`
- Create: `backend/src/ptax/imagery/__init__.py`
- Create: `backend/src/ptax/imagery/gdal.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/config.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_migrations.py`
- Create: `backend/tests/__init__.py`
- Test: `backend/tests/test_footprint.py`

**Key Decisions / Notes:**

- Tables (UUID PKs, `created_at timestamptz` via `clock_timestamp()` as in `backend/alembic/versions/0001_initial.py:22`):
  - `parcel_layers` gains `footprint geometry(MultiPolygon,4326) NULL` (Plan A's tables are otherwise untouched).
  - `imagery_years(id, tenant_id FK, year int, source text, provider text NULL, status text, created_by FK users, band_count int NULL, resolution_m float NULL, coverage_pct float NULL, parcels_uncovered int NULL, bounds geometry(Polygon,4326) NULL, error text NULL, created_at)` with `UNIQUE(tenant_id, year, source)` and CHECK constraints on `source`/`status` per Global Constraints.
  - `imagery_assets(id, tenant_id FK, year_id FK, status text, s3_key text, original_filename text NULL, source_ref text NULL, bounds geometry(Polygon,4326) NULL, epsg int NULL, width int NULL, height int NULL, band_count int NULL, resolution_m float NULL, size_bytes bigint NULL, error text NULL, created_at)` with a GiST index on `bounds` and an index on `year_id`. `s3_key` is the upload key while `pending`, the stored COG key once `ready`.
  - `runs(id, tenant_id FK, layer_id FK parcel_layers, base_year_id FK imagery_years, target_year_id FK imagery_years, status text, threshold float, min_new_area_m2 float, parcels_total int, parcels_processed int default 0, candidates int default 0, parcels_skipped int default 0, created_by FK users, error text NULL, created_at, started_at NULL, finished_at NULL)` with an index on `(tenant_id, created_at)`.
  - `run_parcels(run_id FK, parcel_id FK parcels, parcel_ref text, score float NULL, candidate bool NOT NULL default false, skipped_reason text NULL, indicators jsonb NULL, PRIMARY KEY(run_id, parcel_id))` with index `run_parcels_queue_idx (run_id, candidate, score DESC)` for Plan C's queue.
- `footprint_for(db, tenant) -> shapely.MultiPolygon`: reads the current layer's `footprint`; when `NULL`, computes `ST_Multi(ST_Buffer(ST_Union(geom), 0))` over that layer's parcels, stores it, returns it. Raises `NoParcelLayer` (a `LookupError` subclass) when the tenant has no current layer — API routes map it to `409 "no parcel layer"`. `utm_epsg_for(geom)` returns `32600 + int((lon + 180) // 6) + 1` from the centroid (CONUS is northern hemisphere).
- `gdal.py`: `gdal_env(settings) -> dict[str, str]` is empty when `s3_endpoint_url` is unset; otherwise `AWS_S3_ENDPOINT` (host:port without scheme), `AWS_HTTPS`, `AWS_VIRTUAL_HOSTING="FALSE"`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, plus `GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"` and `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"` in both cases. `naip_env(settings)` = `{"AWS_REQUEST_PAYER": "requester", "AWS_REGION": settings.naip_aws_region}`. `s3_uri(settings, key)` → `s3://{bucket}/{key}`.
- `Settings` gains `naip_source: Literal["stac","fixture"] = "fixture"`, `naip_stac_url`, `naip_fixture_dir: str = "tests/fixtures/imagery"`, `naip_max_ingest_gb: float = 60`, `naip_aws_region: str = "us-west-2"` (values per Global Constraints).
- `conftest.py` gains `ingested_layer(client, db, settings, tenant_with_admin)`: uploads `parcels_small.geojson` to MinIO, creates the layer, drains the queue, ingests with `PIN`, drains again, returns the layer dict (pattern: `backend/tests/test_parcel_ingest.py:62`).
- Downgrade drops the four tables and the column in reverse order; the GiST index is dropped explicitly before its table like `parcels_geom_idx` in 0001.

**Definition of Done:**

- [ ] `alembic upgrade head` on a fresh test DB creates the four tables, `parcel_layers.footprint`, `imagery_assets_bounds_idx`, and `run_parcels_queue_idx`; `alembic downgrade 0001` removes them all.
- [ ] Inserting a second `imagery_years` row with the same `(tenant_id, year, source)` raises `IntegrityError`; a `status` outside the allowed set fails the CHECK.
- [ ] `footprint_for` on the ingested fixture layer returns a MultiPolygon whose area equals the union of the 25 fixture rectangles within 1e-9 deg², persists it on the layer row, and a second call issues no `ST_Union` (asserted with a SQLAlchemy event counter); a tenant without a layer raises `NoParcelLayer`; `utm_epsg_for` on the fixture footprint returns `32615`.
- [ ] Verify: `cd backend && uv run pytest tests/test_migrations.py tests/test_footprint.py -q && uv run python -c "import rasterio, rio_tiler, rio_cogeo, numpy" && uv run ruff check .`

### Task 2: `ImagerySource` seam, NAIP STAC discovery, fixture source, availability API

**Objective:** Define the imagery-source boundary the PRD requires and implement its two v1 members: `NaipStacSource`, which queries Earth Search for the NAIP items covering the county footprint and groups them by year with a coverage percentage and a size estimate, and `FixtureNaipSource`, which lists synthetic years from a directory so the flow works with no AWS credentials. Expose the result as `GET /api/imagery/naip/available` and generate the committed imagery fixtures every later task tests against. Verified by TS-001 step 1.

**Files:**

- Create: `backend/src/ptax/imagery/sources.py`
- Create: `backend/src/ptax/imagery/naip.py`
- Create: `backend/src/ptax/imagery/fixture.py`
- Create: `backend/src/ptax/api/imagery.py`
- Create: `backend/tests/fixtures/make_imagery_fixtures.py`
- Create: `backend/tests/fixtures/imagery/naip_2021.tif`
- Create: `backend/tests/fixtures/imagery/naip_2023.tif`
- Create: `backend/tests/fixtures/imagery/ortho_2025_partial.tif`
- Create: `backend/tests/fixtures/stac_naip_pages.json`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/tests/conftest.py`
- Modify: `Makefile`
- Test: `backend/tests/test_imagery_naip.py`

**Key Decisions / Notes:**

- `sources.py`: `NaipItem(id, year, href, geometry: shapely Polygon (4326), gsd_m, epsg, width, height, estimated_bytes)`, `NaipYear(year, items: list[NaipItem], coverage_pct, estimated_bytes, gsd_m)`, and `class ImagerySource(Protocol)` with `list_years(footprint) -> list[NaipYear]`, `items_for(year, footprint) -> list[NaipItem]`, and `gdal_env() -> dict[str, str]` (the env needed to open `href`). `get_naip_source(settings)` picks by `settings.naip_source`. Coverage = `area(footprint ∩ union(item geometries)) / area(footprint)` computed in the UTM CRS from `utm_epsg_for` (Task 1) via `pyproj`/shapely transform; rounded to an integer percent.
- `NaipStacSource(stac_url, transport=None)`: `GET {stac_url}/collections/naip/items?bbox=<footprint bounds>&limit=200` with httpx (30 s timeout), following `links[rel=next]` until exhausted; keeps items whose geometry intersects the footprint; maps `properties["naip:year"]` → int, `gsd`, `proj:epsg`, `proj:shape` → `(height, width)`, `assets["image"]["href"]`; `estimated_bytes = height * width * 4 * 0.5` (8-bit DEFLATE ≈ 50%). The optional `transport` is an `httpx.BaseTransport` so tests inject `MockTransport` exactly as `backend/tests/conftest.py:96` does for JWKS. `gdal_env()` returns `naip_env(settings)`.
- `FixtureNaipSource(directory)`: one item per `naip_<year>.tif` in the directory, geometry from the raster's bounds transformed to 4326 with rasterio, `href` = absolute path, `gdal_env() == {}`.
- `make_imagery_fixtures.py` writes, deterministically (no RNG), over the Plan A parcel grid (`backend/tests/fixtures/make_fixtures.py:20` origin `-93.70, 45.05`, `CELL_DEG 0.0015`, 5×5 cells) with a 40 m margin:
  - `naip_2021.tif`: EPSG:26915, 1.0 m/px, 4 bands uint8 (RGB+NIR), DEFLATE COG. Background "field" `(70,120,60,180)` with a ±4 checker texture; "existing buildings" as 20×15 m smooth roofs `(150,150,150,90)` on parcels `27-053-000005` and `27-053-000010`.
  - `naip_2023.tif`: identical plus new roofs on `27-053-000003`, `27-053-000007`, `27-053-000012`.
  - `ortho_2025_partial.tif`: EPSG:3857, 0.5 m/px, 3 bands uint8, DEFLATE COG, covering only grid columns 0–2 (west 60%); content = 2023 scene plus a new roof on `27-053-000001`.
  - Each file must be < 1 MB (asserted by the generator); `make imagery-fixtures` regenerates them.
- `stac_naip_pages.json`: two pages captured from Earth Search for bbox `-93.70,45.05,-93.6925,45.0575` with items trimmed to the properties above (4 items: 2021 ×2, 2023 ×2); page 1 carries a `next` link to page 2. The test transport serves page 2 only when the `next` URL is requested, proving pagination.
- `GET /api/imagery/naip/available` (any role) → `[{year, item_count, coverage_pct, estimated_gb, gsd_m, existing: {id, status} | null}]` sorted by year; `existing` joins `imagery_years` for `(tenant, year, "naip")`. `409` when `NoParcelLayer`; `502 "NAIP catalog unavailable: <reason>"` on STAC transport/HTTP errors.
- `conftest.py`: `settings` fixture sets `naip_fixture_dir` to the absolute fixtures path so tests are cwd-independent.

**Definition of Done:**

- [x] With the mock STAC transport, `NaipStacSource.list_years(fixture_footprint)` returns years `[2021, 2023]`, each with exactly the SW quarter-quad (the recorded SE quad lies east of the fixture parcels and is filtered out by geometry), `coverage_pct == 100`, and `estimated_bytes == sum(h*w*2)` of its items; the transport records exactly two requests (the second to the `next` URL).
- [ ] `FixtureNaipSource.list_years` on `tests/fixtures/imagery` returns `[2021, 2023]` with coverage `100` and item hrefs that exist on disk; `ortho_2025_partial.tif` is not listed (name pattern).
- [ ] `GET /api/imagery/naip/available` as the fixture tenant returns both years with `existing: null`; a tenant with no layer → `409`; a transport that raises → `502`.
- [ ] The three fixture rasters are valid COGs (`rio_cogeo.cog_validate`) under 1 MB, `naip_*` have 4 bands at 1.0 m, `ortho_2025_partial.tif` has 3 bands at 0.5 m in EPSG:3857.
- [ ] Verify: `cd backend && uv run pytest tests/test_imagery_naip.py -q`

### Task 3: NAIP ingest job with cost guard and coverage

**Objective:** Let an admin ingest a NAIP year: `POST /api/imagery/naip/ingest` creates the year row and queues `imagery.ingest_naip`, which copies each intersecting item — clipped to the footprint bounding box — into the tenant's imagery prefix as a COG, registers an `imagery_assets` row with its bounds, computes coverage and the uncovered-parcel count, and marks the year `ready`. Also grants the AWS task role read access to the NAIP bucket. Verified by TS-001 steps 2–4.

**Files:**

- Create: `backend/src/ptax/imagery/cog.py`
- Create: `backend/src/ptax/imagery/ingest.py`
- Modify: `backend/src/ptax/api/imagery.py`
- Modify: `backend/src/ptax/storage.py`
- Modify: `backend/src/ptax/jobs/queue.py`
- Modify: `backend/src/ptax/worker.py`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/tests/test_jobs.py`
- Modify: `infra/lib/compute-stack.ts`
- Modify: `infra/test/stacks.test.ts`
- Test: `backend/tests/test_imagery_ingest.py`

**Key Decisions / Notes:**

- `POST /api/imagery/naip/ingest {year}` (admin): resolves the footprint, calls `items_for(year, footprint)`; `404` if no items; `422 "estimated {gb:.1f} GB exceeds NAIP_MAX_INGEST_GB={cap}"` when over the cap; `409` if a `(tenant, year, "naip")` row exists in `queued|processing|ready`; a `failed` row is reset to `queued` (error cleared, its failed assets deleted) and re-enqueued. Otherwise inserts `imagery_years(status="queued", source="naip", created_by=user)` and enqueues `imagery.ingest_naip {year_id}`. Returns `201` with the year.
- `cog.py`: `inspect_raster(path, env) -> RasterInfo(crs, epsg, width, height, band_count, dtype, resolution_m, bounds_4326, is_cog)` (resolution in metres = pixel size converted through the UTM CRS when the source is geographic); `to_cog(src_path, dst_path, *, env, clip_bounds=None, bands=(1,2,3[,4]))` opens the source inside `rasterio.Env(**env)`, wraps it in a `WarpedVRT` restricted to `clip_bounds` (source CRS; same CRS as the source so this is a pure clip), stretches `uint16` to `uint8` by 2–98 percentiles sampled from the smallest overview, and writes with `rio_cogeo.cog_translate(vrt, dst_path, cog_profiles.get("deflate"), nodata=0, in_memory=False, quiet=True)`. `is_cog(path)` wraps `cog_validate`.
- `ingest.py` job `imagery.ingest_naip`: sets `processing`; for each item not already registered (`source_ref == item.id` and `ready` — resume on retry): `to_cog` into a temp dir → `put_object` to `imagery_asset_key(tenant_id, year_id, asset_id)` → `register_asset(db, year, key, info, source_ref=item.id)` commits the `ready` asset row with `bounds`, `epsg`, `width`, `height`, `band_count`, `resolution_m`, `size_bytes`. Then `compute_coverage(db, year, footprint)`: `coverage_pct` = area of `footprint ∩ ST_Union(asset bounds)` over footprint area in UTM; `parcels_uncovered` = count of current-layer parcels not `ST_CoveredBy` the union; `bounds` = envelope of the union; `band_count`/`resolution_m` = min band count / max resolution over assets. Status `ready`. Any exception → `_fail_year` (status `failed`, `error` naming the item id) then re-raise, mirroring `backend/src/ptax/parcels/ingest.py:65`.
- Clipping uses `footprint.bounds` (bbox, not the polygon) so parcels at the county edge keep context; the whole item is used when it lies entirely inside the bbox.
- `storage.py`: `UPLOAD_PURPOSES = ("parcel_layer", "imagery")`; `imagery_asset_key(tenant_id, year_id, asset_id) -> "tenants/{tenant_id}/imagery/{year_id}/{asset_id}.tif"`.
- Graceful stop (also used by Tasks 4 and 7): `queue.py` gains `class JobInterrupted(Exception)` and `requeue(db, job)` — sets `status="queued"`, `error="interrupted: worker stopping"`, `attempts = attempts − 1` (the interrupted attempt is not charged against `MAX_ATTEMPTS`, `backend/src/ptax/jobs/queue.py:16`), commits. `worker.py` sets a module-level `_stop` flag in `_request_stop`, exposes `stop_requested() -> bool`, and `run_once` catches `JobInterrupted` before the generic handler, rolls back, and calls `requeue`. `imagery.ingest_naip` checks `stop_requested()` after each item's asset row commits and raises `JobInterrupted`; on the next claim it resumes at the first unregistered item. `test_jobs.py` gains: a handler raising `JobInterrupted` leaves the job `queued` with `attempts` unchanged and `error` set, and a job interrupted 5 times still runs to `succeeded`.
- `worker.py` and `main.py` import `ptax.imagery.ingest` next to `ptax.parcels.ingest` to register handlers.
- `compute-stack.ts`: task role gets `s3:GetObject` on `arn:aws:s3:::naip-analytic/*` and `s3:ListBucket` on `arn:aws:s3:::naip-analytic`; `environment` gains `NAIP_SOURCE: "stac"` and `NAIP_AWS_REGION: "us-west-2"`. Assertion test: the policy contains those actions/resources and both task definitions carry `NAIP_SOURCE=stac`.

**Definition of Done:**

- [ ] With the fixture source and MinIO, ingesting `2021` for the ingested fixture tenant ends `ready` with one asset whose `s3_key` is under `tenants/{tenant}/imagery/{year}/`, the object in MinIO is a valid COG with 4 bands, asset `bounds` contains every fixture parcel, `coverage_pct == 100`, `parcels_uncovered == 0`, `band_count == 4`, `resolution_m == 1.0`.
- [ ] Ingesting again while `ready` → `409`; a `NAIP_MAX_INGEST_GB` of `0.0001` → `422` and no year row; a source whose `items_for` returns an item with an unreadable `href` leaves the year `failed` with the item id in `error`, and a retry `POST` resets it to `queued` and succeeds once the href is fixed.
- [ ] A fixture source yielding two items, with `stop_requested()` patched true after the first, leaves the job `queued` (attempts unchanged) and the year `processing` with one asset; draining again finishes the year `ready` with two assets and no duplicate `source_ref`.
- [ ] `cd infra && pnpm test` passes the new NAIP policy and env assertions.
- [ ] Verify: `cd backend && uv run pytest tests/test_imagery_ingest.py tests/test_jobs.py -q && cd ../infra && pnpm test`

### Task 4: Upload imagery years: validation, COG conversion, coverage gap report

**Objective:** Implement PRD Flow 3 step 3: an admin creates an upload year (year + optional provider), registers one or more uploaded GeoTIFFs against it, and finalizes; a job validates each file (readable, CRS, bands, dtype, resolution), converts it to a stored COG, and computes coverage against the county footprint with the number of parcels left uncovered. Also exposes the year listing the Imagery page reads. Verified by TS-002 and TS-003.

**Files:**

- Modify: `backend/src/ptax/api/imagery.py`
- Modify: `backend/src/ptax/api/uploads.py`
- Modify: `backend/src/ptax/imagery/ingest.py`
- Modify: `backend/src/ptax/imagery/cog.py`
- Modify: `backend/src/ptax/parcels/ingest.py`
- Modify: `backend/tests/test_uploads.py`
- Modify: `backend/tests/test_parcel_ingest.py`
- Test: `backend/tests/test_imagery_upload.py`

**Key Decisions / Notes:**

- Routes (admin unless noted): `POST /api/imagery/years {year (1990–2100), provider?}` → `201` year with `source="upload"`, `status="queued"`; `409` on the unique key. `POST /api/imagery/years/{id}/assets {upload_key, original_filename}` → `201` pending asset; `422` unless `upload_key` starts with `tenants/{tenant_id}/imagery/` (pattern: `backend/src/ptax/api/parcel_layers.py:74`); `409` unless the year is `queued|failed|ready`. `POST /api/imagery/years/{id}/finalize` → `202`; `409` if `processing` or if no `pending` asset exists; sets `processing` and enqueues `imagery.ingest_upload {year_id}`. `GET /api/imagery/years` (any role) → years newest-first with `assets: [{id, status, original_filename, error}]`, `GET /api/imagery/years/{id}` (any role).
- `uploads.py`: `purpose: Literal["parcel_layer", "imagery"]`; `imagery` accepts content types `image/tiff` and `application/octet-stream`.
- Job `imagery.ingest_upload`: for each `pending` asset in creation order: download to temp, `inspect_raster`; reject with `LayerError`-style `ImageryError` messages that include `original_filename`: "not a readable GeoTIFF", "has no coordinate reference system", "has {n} band(s); 3 or 4 required", "{dtype} pixels are not supported (uint8/uint16 only)", "resolution {r:.2f} m/px is coarser than 1 m"; on rejection the asset is `failed` with `error`, and processing continues with the next asset. Otherwise `to_cog` (skipped when `is_cog` and already `uint8` with ≤ 4 bands — the file is uploaded as-is), `put_object`, `register_asset`, delete the original upload object. After all assets: if none is `ready`, year `failed` with "no valid imagery files: " + the first asset error; else `compute_coverage` and `ready`. Assets that failed in an earlier finalize stay `failed`; a re-finalize processes only `pending`.
- Uploaded assets keep their native CRS (rio-tiler reprojects on read); `bounds` is always stored in 4326.
- `compute_coverage` is shared with Task 3 unchanged; for upload years `resolution_m` is the coarsest asset resolution and `band_count` the minimum.
- The job checks `stop_requested()` after each asset commits and raises `JobInterrupted` (Task 3); a re-claimed job resumes with the remaining `pending` assets.
- Coverage stats follow the current parcel layer: `parcel_layer.ingest` (`backend/src/ptax/parcels/ingest.py:133`, right after `current_parcel_layer_id` is set) enqueues `imagery.recompute_coverage {tenant_id}`; that handler (in `imagery/ingest.py`) calls `compute_coverage` for every `ready` year of the tenant against the new footprint, so `coverage_pct` / `parcels_uncovered` never describe a superseded layer. The enqueue is the only change to Plan A's handler.

**Definition of Done:**

- [ ] Registering `ortho_2025_partial.tif` (uploaded to MinIO under the tenant's imagery prefix) on year 2025 and finalizing yields `ready`, one `ready` asset with `epsg == 3857`, `band_count == 3`, `resolution_m == 0.5`, `coverage_pct == 60`, `parcels_uncovered == 10`, and the original upload object is gone.
- [ ] A text file registered as `bogus.tif` → asset `failed` with error containing `bogus.tif` and "not a readable GeoTIFF"; the year is `failed` with "no valid imagery files"; adding `naip_2023.tif` and finalizing again → year `ready`, the bogus asset still `failed`.
- [ ] A 1-band GeoTIFF and a `float32` GeoTIFF (built in-test with rasterio) are rejected with the band and dtype messages; a `uint16` 3-band file becomes a `uint8` COG whose 98th-percentile pixel is ≥ 250.
- [ ] `POST /api/uploads` with `purpose: "imagery"` returns a key under `tenants/{tenant}/imagery/`; a reviewer calling any admin route → `403`; another tenant's year id → `404`.
- [ ] After year 2025 is `ready` (`parcels_uncovered == 10`), ingesting a new parcel layer holding only the first 10 fixture parcels and draining the queue leaves year 2025 with `parcels_uncovered == 4` and `coverage_pct == 60` (grid columns 0–2 of rows 0–1), proving `imagery.recompute_coverage` ran.
- [ ] Verify: `cd backend && uv run pytest tests/test_imagery_upload.py tests/test_uploads.py tests/test_parcel_ingest.py -q`

### Task 5: Raster reader, tile endpoint, thumbnails, per-parcel previews

**Objective:** Serve the stored COGs back: a shared reader that mosaics the assets intersecting a request, a WebMercator tile endpoint for Plan C's map, a per-year thumbnail for the Imagery page, and a per-parcel clipped preview (with optional boundary outline) that Plan C's queue and viewer will use and Task 7's run reads through. Verified by TS-001 step 3 (thumbnail) and Task 7's tests (parcel reads).

**Files:**

- Create: `backend/src/ptax/imagery/reader.py`
- Create: `backend/src/ptax/api/tiles.py`
- Modify: `backend/src/ptax/api/imagery.py`
- Modify: `backend/src/ptax/main.py`
- Test: `backend/tests/test_tiles.py`

**Key Decisions / Notes:**

- `reader.py`: `assets_intersecting(db, tenant_id, year_id, bounds_4326) -> list[ImageryAsset]` (`ready` only, `ST_Intersects` on `bounds`); `read_tile(settings, assets, z, x, y) -> ImageData | None` using `rio_tiler.mosaic.mosaic_reader(uris, tiler, x, y, z)` where `tiler` opens `Reader(uri)` and returns `src.tile(x, y, z, indexes=(1,2,3))`; `read_parcel(settings, assets, geom_4326, *, resolution_m, buffer_m=0) -> ParcelRaster | None` computes the UTM bbox of `geom` (+buffer), width/height from `resolution_m`, and calls `mosaic_reader` with `src.part(bbox, bounds_crs=utm, dst_crs=utm, width=w, height=h)`; returns `ParcelRaster(data: uint8[bands,h,w], mask: bool[h,w] (valid pixels), transform, crs, resolution_m, parcel_mask: bool[h,w])` where `parcel_mask` is `rasterio.features.rasterize` of the parcel polygon on that grid; `read_bounds_preview(settings, assets, bounds_4326, max_size=256)` for thumbnails. All reads run inside `rasterio.Env(**gdal_env(settings))` and use 3 bands for images, all bands for `read_parcel`.
- `GET /api/tiles/{year_id}/{z}/{x}/{y}.png` (any role): `404` unless the year belongs to the caller's tenant and is `ready`; `204` when no asset intersects the tile bounds (`morecantile.tms.get("WebMercatorQuad").bounds(Tile(x,y,z))`); else `image/png` from `img.render(img_format="PNG")` with `Cache-Control: private, max-age=3600`. `z` outside 8–22 → `422`.
- `GET /api/imagery/years/{id}/thumbnail.png` → preview of `year.bounds` at ≤ 256 px; `GET /api/imagery/years/{year_id}/parcels/{parcel_id}/preview.png?size=512&buffer=0.25&outline=1` → the parcel bbox expanded by `buffer` × its larger side, rendered north-up at `size` px on the long edge; `outline=1` paints the parcel boundary (rasterized as a 2 px line in `#ffff00`) onto the RGB array before rendering; response header `X-Bounds: minx,miny,maxx,maxy` (4326) so Plan C can overlay the boundary client-side instead. `404` when the parcel is not in the caller's tenant; `204` when the parcel has no imagery in that year.
- Also add `bounds` and `min_zoom`/`max_zoom` (from the assets' resolution: `max_zoom = 22 − ceil(log2(resolution_m / 0.037))`, `min_zoom = 8`) to the year responses so Plan C can configure MapLibre without a TileJSON endpoint.

**Definition of Done:**

- [ ] For the ingested fixture year 2023, the z=17 tile containing the grid centroid returns `200 image/png` 256×256 with more than one distinct colour; a tile in the Pacific returns `204`; another tenant's year → `404`; `z=3` → `422`.
- [ ] `read_parcel` for `27-053-000003` against 2023 at 1.0 m returns arrays of 4 bands, `parcel_mask.sum()` within 5% of the parcel's area in m², and `mask.all()`; the same parcel against `ortho_2025_partial` returns `mask` all `False` for the uncovered columns and `read_parcel` on a parcel entirely outside returns `None`.
- [ ] The thumbnail for year 2023 is a PNG ≤ 256 px on its long side; the preview for `27-053-000003` with `outline=1` contains yellow pixels and the `X-Bounds` header parses to a box containing the parcel.
- [ ] Verify: `cd backend && uv run pytest tests/test_tiles.py -q`

### Task 6: `Detector` interface and `ClassicalDetector`

**Objective:** Define the per-parcel detector contract and implement the v1 classical detector: from two co-gridded parcel rasters it derives per-pixel built-up and vegetation masks, cleans them morphologically, measures new built-up area and vegetation loss inside the parcel, and returns a score in [0,1] with a candidate flag and the indicators that produced it. Pure numpy, no I/O, so it is unit-tested on arrays and on the fixture rasters read through Task 5.

**Files:**

- Create: `backend/src/ptax/detection/__init__.py`
- Create: `backend/src/ptax/detection/detector.py`
- Test: `backend/tests/test_detector.py`

**Key Decisions / Notes:**

- `ChangeResult(score: float, candidate: bool, indicators: dict)` with indicators `new_builtup_m2`, `new_builtup_frac`, `veg_loss_m2`, `veg_loss_frac`, `base_builtup_frac`, `target_builtup_frac`, `nodata_frac`, `resolution_m`; `class Detector(Protocol)` with `compare(base: ParcelRaster, target: ParcelRaster, *, threshold: float, min_new_area_m2: float) -> ChangeResult`.
- `ClassicalDetector` per pixel, on `float32` 0–255 bands: greenness `G` = NDVI `(nir − r)/(nir + r + 1)` when 4 bands else ExG `(2g − r − b)/255`; brightness `B` = mean(r,g,b); local texture `T` = std-dev over a 5×5 window (computed with cumulative sums, no scipy). `vegetated = G > VEG_T` (`0.2` NDVI / `0.08` ExG); `builtup = ~vegetated & (B > 80) & (T < 12)`. Masks are opened then closed with a 3×3 structuring element (numpy slicing) to drop speckle and 1-px misalignment slivers. `new_builtup = builtup_target & ~builtup_base & parcel_mask & valid`; `veg_loss = vegetated_base & ~vegetated_target & parcel_mask & valid`; areas = pixel counts × `resolution_m²`.
- `score = 1 − exp(−(new_builtup_m2 + 0.25 · veg_loss_m2) / SCORE_SCALE_M2)`; `candidate = score ≥ threshold and new_builtup_m2 ≥ min_new_area_m2`. Constants live on the class (`VEG_T_NDVI`, `VEG_T_EXG`, `BRIGHT_T`, `TEXTURE_T`, `SCORE_SCALE_M2`) so a later tuning pass changes one place.
- A parcel whose `nodata_frac` (invalid or outside-parcel pixels within the parcel mask) exceeds `0.5` in either year is not scored: `compare` raises `InsufficientCoverage(which="base"|"target")`; Task 7 maps that to `skipped_reason`.
- Fixture expectations (from Task 2's generator): 2021→2023 flags exactly `000003`, `000007`, `000012` (each new roof 300 m² → score ≈ 0.78); parcels `000005`/`000010` (roofs present in both years) score < 0.05; 2023→2025 flags exactly `000001`.

**Definition of Done:**

- [ ] On synthetic arrays: a 20×15 m roof added on a 100×100 m field at 1 m/px → `score` in [0.7, 0.9], `candidate` true, `new_builtup_m2` within 15% of 300; an unchanged field → `score < 0.02`; a target with a 1-px global shift of the same roof → not a candidate; a 3-band (ExG) variant of the added-roof case is also a candidate; a target with 60% nodata raises `InsufficientCoverage("target")`.
- [ ] On fixture rasters via `read_parcel`: the 25-parcel 2021→2023 candidate set is exactly `{000003, 000007, 000012}` and 2023→2025 over the 15 covered parcels is exactly `{000001}`.
- [ ] Verify: `cd backend && uv run pytest tests/test_detector.py -q`

### Task 7: Runs API and resumable run job

**Objective:** Implement PRD Flow 4: an admin starts a run between two ready years, the worker scores every current-layer parcel in committed batches with live progress, parcels lacking imagery in either year are counted as skipped, the run can be cancelled, survives a worker restart, and ends with the summary counts Plan C reads. Verified by TS-004 and TS-006.

**Files:**

- Create: `backend/src/ptax/api/runs.py`
- Create: `backend/src/ptax/detection/run.py`
- Modify: `backend/src/ptax/worker.py`
- Modify: `backend/src/ptax/main.py`
- Test: `backend/tests/test_runs.py`

**Key Decisions / Notes:**

- `POST /api/runs {base_year_id, target_year_id, threshold?, min_new_area_m2?}` (admin): both years must belong to the tenant and be `ready` (`422 "year is not ready"`), `base.year < target.year` (`422 "Target year must be later than base year"`), a current layer must exist (`409`); inserts `runs(status="queued", layer_id=current, parcels_total=count of layer parcels, threshold, min_new_area_m2)` and enqueues `run.execute {run_id}`; `201`. `GET /api/runs` (any role) newest-first with year labels (`{year, source, provider}`) and counts; `GET /api/runs/{id}`; `POST /api/runs/{id}/cancel` (admin) → `409` unless `queued|running`, sets `cancelled` (a queued run's job then exits immediately).
- `run.py` job `run.execute`: sets `running`/`started_at`; loads both years' `ready` assets once; iterates layer parcels ordered by `ST_GeoHash(ST_Centroid(geom), 8)` in batches of 200, skipping ids already in `run_parcels` (resume); per parcel: `read_parcel` (Task 5) for base and target at `resolution = max(base.resolution_m, target.resolution_m, 0.5)`, coarsened so `w*h ≤ 4e6`; `None` or `InsufficientCoverage` → row with `skipped_reason` (`no_coverage_base` | `no_coverage_target` | `partial_coverage`); else `ClassicalDetector().compare(...)` → row with `score`, `candidate`, `indicators`. After each batch: insert rows, update `parcels_processed`, `candidates`, `parcels_skipped`, commit; then re-read `runs.status` — `cancelled` → return; `stop_requested()` → raise `JobInterrupted` so the worker re-queues it without charging an attempt (Task 3's `requeue`). On completion `succeeded`/`finished_at`; on any other exception the batch is rolled back and the run is set `failed` with `error` before re-raising, so a job that exhausts `MAX_ATTEMPTS` never leaves the run `running`.
- `worker.py` and `main.py` import `ptax.detection.run` to register the handler.
- `parcels_processed` counts scored + skipped; a re-queued run continues its counts from the persisted rows (recomputed from `run_parcels` on start, not trusted from the row).

**Definition of Done:**

- [ ] Run 2021→2023 over the ingested fixture tenant ends `succeeded` with `parcels_total == 25`, `parcels_processed == 25`, `candidates == 3`, `parcels_skipped == 0`, and `run_parcels` candidate refs exactly `{000003, 000007, 000012}` with stored `indicators`; run 2023→2025 ends with `parcels_processed == 25`, `candidates == 1`, `parcels_skipped == 10`, skipped rows carrying `skipped_reason == "no_coverage_target"`.
- [ ] Cancelling a `running` run (status flipped in the test between batches with batch size patched to 5) stops it with `parcels_processed < 25` and status `cancelled`; a run started with `base.year > target.year` → `422`; a reviewer `POST /api/runs` → `403`; another tenant sees `[]` from `GET /api/runs` and `404` for the run id.
- [ ] Simulating a worker stop (`stop_requested()` patched true after the first batch) leaves the job `queued` with `attempts` unchanged and the run `running` with 5 rows; draining again completes the run with 25 rows and no duplicate `run_parcels` primary keys. A detector patched to raise on the second batch leaves the run `failed` with a non-empty `error` and the job `failed`.
- [ ] Verify: `cd backend && uv run pytest tests/test_runs.py -q`

### Task 8: Frontend Imagery page

**Objective:** Build `/imagery` (PRD Flow 3): a years table with status, source/provider, bands, resolution, coverage, uncovered parcels, error, and thumbnail; a NAIP section listing available years with coverage, tile count, and estimated size, with a confirm-then-ingest action; and an upload section that creates a year, uploads several GeoTIFFs sequentially with per-file progress, and finalizes. Reviewers see everything read-only. Verified by TS-001, TS-002, TS-003, TS-005, TS-006.

**Files:**

- Create: `frontend/src/api/uploads.ts`
- Create: `frontend/src/api/imagery.ts`
- Create: `frontend/src/pages/ImageryPage.tsx`
- Create: `frontend/src/components/StatusChip.tsx`
- Create: `frontend/src/api/imagery.test.ts`
- Modify: `frontend/src/api/parcelLayers.ts`
- Modify: `frontend/src/components/UploadDropzone.tsx`
- Modify: `frontend/src/components/LayerStatus.tsx`
- Modify: `frontend/src/components/Layout.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/ParcelsPage.tsx`
- Create: `frontend/src/pages/RunsPage.tsx`
- Modify: `README.md`

**Key Decisions / Notes:**

- `README.md`: document the Imagery and Runs pages in the local-development section, the fixture NAIP source and how to switch a laptop to `NAIP_SOURCE=stac` with `aws login` for a real discovery test, that NAIP is requester-pays in `us-west-2` behind `NAIP_MAX_INGEST_GB`, the upload constraints from Global Constraints, and the new env vars.
- `uploads.ts`: `putPresigned(file, purpose, contentType, onProgress) -> UploadTicket` extracts the XHR sequence from `frontend/src/api/parcelLayers.ts:57`; `parcelLayers.ts` calls it. `apiFetchBlob(path) -> Promise<string>` returns an object URL for authenticated images (thumbnails); callers revoke it on unmount.
- `imagery.ts`: types mirroring the API (`ImageryYear`, `ImageryAsset`, `NaipAvailability`), `listYears`, `naipAvailable`, `ingestNaip(year)`, `createUploadYear`, `registerAsset`, `finalizeYear`, and `uploadImageryFiles(yearId, files, onFileProgress)` which uploads files one at a time (`putPresigned` with purpose `imagery`, content type `image/tiff`) and registers each before starting the next; `imagery.test.ts` asserts the sequential order and that a failed file rejects without registering later ones.
- `UploadDropzone` gains `accept: string[]`, `multiple?: boolean`, `label: string`, and calls `onFiles(File[])`; the parcels page passes the old values. Client-side rejection message: "`{name}` is not a GeoTIFF (.tif/.tiff)".
- Page layout: heading; if the tenant has no current layer show "No parcel layer yet — upload parcels before imagery" and nothing else (TS-006). Otherwise: **Imagery years** table (rows: `{year} · {source}` with provider in parentheses, `StatusChip`, bands, resolution `x.x m`, coverage `NN%`, `N parcels without coverage`, error text, thumbnail via `apiFetchBlob` when `ready`); **NAIP on AWS** section (loading state while `naipAvailable` runs; rows with coverage, `{n} tiles`, `{gsd} m`, `~{gb} GB`, and for admins an **Ingest** button that opens a confirm showing the estimate; `existing` → status text and disabled button); **Upload imagery** section (admin): year number input, provider text input, **Create year**; each `queued`/`failed` upload year shows a dropzone labelled "GeoTIFF/COG files for {year}", per-file progress, the asset list with `StatusChip`, and **Finish upload** enabled when a `pending` asset exists. Poll `listYears` every 2 s while any year is `queued`/`processing` (pattern: `frontend/src/pages/ParcelsPage.tsx:38`).
- `StatusChip({status})` colours: `queued`/`pending` slate, `processing`/`running`/`inspecting`/`ingesting`/`uploaded` blue, `ready`/`succeeded` green, `failed` red, `cancelled` amber; `LayerStatus` renders `StatusChip`.
- Nav: **Imagery** and **Runs** links for all roles after **Parcels**; routes `/imagery`, `/runs`.

**Definition of Done:**

- [ ] TS-001, TS-002, TS-003, TS-005 step 1, and TS-006 step 2 pass against the local stack.
- [ ] Verify: `cd frontend && pnpm test && pnpm build && pnpm lint`

### Task 9: Frontend Runs page

**Objective:** Build `/runs` (PRD Flow 4): a start form with base/target selects over ready years and an advanced threshold field, and a runs table with status chip, progress bar, summary counts, and cancel, polling while any run is active. Reviewers see the table only. Verified by TS-004, TS-005, TS-006.

**Files:**

- Create: `frontend/src/api/runs.ts`
- Modify: `frontend/src/pages/RunsPage.tsx`
- Modify: `frontend/src/App.tsx`

**Key Decisions / Notes:**

- `runs.ts`: `Run` type, `listRuns`, `createRun({base_year_id, target_year_id, threshold?, min_new_area_m2?})`, `cancelRun(id)`.
- Form (admin): base and target `<select>` over `listYears` filtered to `ready`, labelled `{year} · {source}`; **Advanced** disclosure with `threshold` (default 0.3) and `min_new_area_m2` (default 40); **Start run** disabled until both years are chosen; a `422` from the API is shown inline verbatim (TS-004 step 5). When there is no current layer or fewer than two ready years, the form is replaced by "A parcel layer and two ready imagery years are needed to start a run."
- Table: newest first; columns `{base} → {target}`, `StatusChip`, progress `<progress value max>` plus `n / total` while `running`, summary `{processed} processed · {candidates} candidates · {skipped} skipped` once finished, started/finished times, error text, and an admin-only **Cancel** button while `queued|running`. Poll `listRuns` every 2 s while any run is `queued|running`.

**Definition of Done:**

- [ ] TS-004, TS-005 step 2, and TS-006 step 3 pass against the local stack.
- [ ] Verify: `cd frontend && pnpm build && pnpm lint`

### Task 10: Verify real NAIP ingest against the deployed stack (user)

**Objective:** Prove the STAC + requester-pays path with real credentials: deploy this plan's CDK changes and image, ingest one small NAIP year for a real county footprint, and confirm cost and timing match the estimate. This cannot run in the implementing session (no AWS credentials on this machine, as in Plan A); the fixture source covers everything else.

**Owner:** User

**User Action:** With AWS credentials configured, run `cd infra && pnpm cdk deploy --all`, sign in at the ALB URL as the provisioned admin, ingest the parcel layer of a small county (or the fixture layer), open **Imagery**, and click **Ingest** on the oldest listed NAIP year; then report the year's final status, coverage, and the S3 request/egress line on the AWS bill for that day.

**Files:**

- Modify: `README.md`

**Key Decisions / Notes:**

- `README.md` gains a "Verified NAIP ingest" note (date, state, year, GB, wall time) under the existing "Verified deployment" heading once the user confirms; the surrounding documentation was written in Task 8.
- If the estimate is refused by the cap, raise `NAIP_MAX_INGEST_GB` on the worker task definition rather than picking a larger county; the point is one real end-to-end ingest, not volume.

**Definition of Done:**

- [x] The chosen NAIP year reaches `ready` on the deployed stack with `coverage_pct ≥ 95` for the county and the Imagery page thumbnail renders real imagery. **Satisfied by Plan D's Task 9**, which ran this exact reproduction rather than a substitute: `us-east-1`, tenant `Demo County`, the fixture layer this task's `User Action` explicitly permits. NAIP **2010 and 2021 both reached `ready` at 100% coverage with 0 uncovered parcels**, in ~10 s each, read from the requester-pays `naip-analytic` bucket in `us-west-2`; thumbnails and per-parcel previews rendered real imagery. Recorded under "Verified NAIP ingest" in `README.md` — the note this task's Key Decisions asked for, with its date, region, years, GB estimate and wall time.
- [x] Verify: observed through `GET /api/imagery/years` on the ALB during that run. **Not re-runnable now** — the STS credentials were supplied for that session only and were deleted from the scratchpad afterwards, so this closes on the recorded evidence rather than a fresh call.

## Autonomous Decisions

Resolved from the draft's open questions without a user round-trip; each is reversible behind the seam named:

- **Discovery via STAC, not index shapefiles.** Earth Search's `naip` collection answers bbox queries with per-item year, GSD, shape, and requester-pays hrefs, so no manifest download or shapefile parsing is needed; `NAIP_STAC_URL` swaps catalogs. The bucket-layout facts (`naip-analytic/<state>/<year>/<res>cm/rgbir_cog/<quad>/*.tif`, us-west-2, requester pays) match the STAC hrefs.
- **4-band `naip-analytic` COGs rather than 3-band `naip-visualization`.** NIR makes vegetation-loss detection an NDVI threshold instead of a visible-band heuristic; the visualization JPEGs would be smaller but lossy.
- **Cost guard = estimate before confirm + hard cap env var**, no per-county area rule; the estimate is derived from STAC `proj:shape`.
- **Thresholds are run parameters with defaults**, stored on the run, not tenant settings; a settings UI is unnecessary until real-imagery tuning shows what needs to vary per county.
- **A fixture NAIP source is a first-class `ImagerySource`**, not a test double, so local dev and E2E cover the full ingest → tile → run flow without credentials.

## E2E Results

Executed during implementation (Tasks 8–9) against the local stack with the new code (API + worker restarted, dev DB migrated to 0002, Demo County's fixture layer already `ready`), Chrome via Claude in Chrome, Vite on port 5175.

| Scenario | Priority | Result | Fix Attempts | Notes |
|----------|----------|--------|--------------|-------|
| TS-001   | Critical | PASS   | 0            | NAIP section lists 2021/2023 (coverage 100%, 1 tile, 1.0 m, ~0.001 GB); confirm dialog shows the estimate; row reached `ready` with `4 bands · 1.0 m · coverage 100% · 0 parcels without coverage`, thumbnail renders, NAIP row shows `ingested` with Ingest disabled. The fixture ingest finishes in ~1 s, so `queued` was observed on the second ingest (2023) and `processing` was not captured on screen. |
| TS-002   | Critical | PASS   | 0            | `2025 · upload (Nearmap)` created `queued`; file listed `pending`; `processing` observed after Finish upload; `ready` with `3 bands · 0.5 m · coverage 60% · 10 parcels without coverage`, thumbnail renders |
| TS-003   | High     | PASS   | 0            | `bogus.tif` → year `failed` "no valid imagery files: bogus.tif is not a readable GeoTIFF"; adding `naip_2023.tif` → `ready`, bogus stays `failed`; GeoJSON rejected client-side "parcels_small.geojson is not a GeoTIFF (.tif/.tiff)" |
| TS-004   | Critical | PASS   | 0            | Selects list only ready years; `2021 → 2023` showed `queued · 0 / 25` with Cancel, then `succeeded · 25 processed · 3 candidates · 0 skipped`; `2023 → 2025` → `25 processed · 1 candidate · 10 skipped`; `2023 → 2021` → inline "Target year must be later than base year", no run; both runs persist after reload. Selects were driven with `form_input` (native popup swallowed arrow keys). |
| TS-005   | High     | PASS   | 0            | Reviewer sees years, NAIP availability and both runs; interactive elements are only nav links and Sign out |
| TS-006   | Critical | PASS   | 0            | `Other County`: Imagery shows "No parcel layer yet — upload parcels before imagery."; Runs shows "No runs yet." and "A parcel layer and two ready imagery years are needed to start a run." |

## AWS Verification (Task 10)

Executed 2026-09-22 against the deployed stack in `us-east-1` (account `525390918028`, ALB `PtaxCo-ApiLB-XNbHhnzCM4z6-1909514806`), tenant `Demo County` (FIPS 27053), with the 25-parcel fixture layer uploaded through the real presigned-upload → inspect → ingest flow.

| Step | Result |
|------|--------|
| Parcel layer on AWS | `ready`, 25 features, 0 skipped, `EPSG:4326` |
| `GET /api/imagery/naip/available` (live Earth Search STAC) | 7 years 2010–2023, each 1 tile, coverage 100%, gsd 1.0 m → 0.6 m → 0.3 m, estimates 0.085–0.94 GB |
| NAIP 2010 ingest (requester-pays `naip-analytic`) | `ready` in ~10 s; 4 bands, 1.0 m, coverage 100%, 0 parcels uncovered; asset `source_ref = mn_m_4509359_sw_15_1_20100912` |
| NAIP 2021 ingest | `ready` in ~11 s; 4 bands, 0.6 m, coverage 100% |
| Thumbnail / per-parcel preview | `200 image/png`, 26,060 distinct colours (mean 131.5, σ 30.6) — real aerial imagery, not a blank frame |
| Run 2010 → 2021 | `succeeded`, 25/25 processed, 0 skipped |

⛔ **Detector finding — the v1 thresholds do not survive real cross-resolution imagery.** The 2010→2021 run flagged **23 of 25 parcels**, and tightening the run parameters barely moved it (`threshold 0.6 / 200 m²` → 21; `threshold 0.9 / 500 m²` → 19), because the score saturates once a few hundred m² are detected. The stored indicators show the cause: `target_builtup_frac` median **0.937** (max 1.000) against `base_builtup_frac` median **0.366**, with `new_builtup_m2` median 3351. `ClassicalDetector._classify` calls a pixel built-up when it is non-vegetated, bright and **locally smooth** (`texture < TEXTURE_T`), but a 0.6 m raster resampled onto the 1.0 m comparison grid is far smoother than a natively 1.0 m raster, so the *resolution mismatch alone* pushes the target year's built-up mask toward 1.0 and manufactures new built-up area. The only parcels that did not flag were those already ~99% built-up in the base year. This is a detector defect, not just tuning: comparing years of differing native resolution needs the texture measure normalised to a common effective resolution (e.g. low-pass both years to the coarser native GSD before classifying), and the thresholds then re-tuned against real imagery. The `Detector` seam, the per-parcel stored `indicators`, and run-level parameters all work as designed, so this is fixable behind the existing interface without schema or API change.

**Owned by Plan D** (`docs/plans/2026-09-22-detector-accuracy.md`), which carries the measured evidence, the evaluation-harness approach, and the open question of labelled truth. Plan B's scope — imagery in, runs out — is unchanged by it.

## Deviations

- Task 10 (tactical, found by deploying): rasterio's wheel bundles GDAL but GDAL links the system libexpat, which `python:3.13-slim` does not ship, so the API and worker crashed on `import rasterio` with `ImportError: libexpat.so.1`. Plan A never hit it because pyogrio's wheel does not need it, so Task 1's assumption ("rasterio wheels run on slim without apt GDAL") holds for GDAL itself but not its dependencies. `backend/Dockerfile` now installs `libexpat1`, and `make image-check` builds the image and imports the raster stack plus `ptax.main` so this fails locally instead of as a crashlooping container.
- Task 10 (operational, found by deploying): migrations run at API container start, so the first (broken) deploy applied `0002` successfully and *then* failed to import. The CloudFormation rollback to the Plan A image could never stabilise — that image's `alembic upgrade head` exits with `Can't locate revision identified by '0002'` — leaving the stack stuck in `UPDATE_ROLLBACK_IN_PROGRESS`. **A failed deploy after a successful migration is not rollback-able under the current `SHORTCUT:`**; the recovery is to roll forward, or (as here, with an empty greenfield database) to return the schema to the previous revision before letting the rollback finish. This strengthens the existing upgrade trigger: migrations belong in a one-off task run before the service update, and the API service needs an ECS deployment circuit breaker so a bad image fails in minutes instead of hanging.

- Task 8/9 (tactical): E2E scenarios were executed once after both frontend tasks were coded (they share one browser session and TS-004/005 need both pages); `RunsPage.tsx` was created as a stub in Task 8 so the build passed, then implemented in Task 9. The dropzone prop change (`onFiles`, `accept`, `label`, `inputLabel`, `rejectMessage`, `multiple`, `progressLabel`) touched `frontend/src/pages/ParcelsPage.tsx` (call-site update only).
- Task 7 (tactical): a parcel whose valid-pixel fraction is under 5% in a year is skipped as `no_coverage_<year>` rather than `partial_coverage` (`NO_COVERAGE_VALID_FRAC` in `detection/run.py`); reprojected asset bounds overlap neighbouring parcels by a pixel or two along an edge, which is not coverage. Job handlers live in `ptax/detection/run.py` as planned; `flush_batch` is the per-batch commit seam the tests hook to cancel/stop mid-run.
- Task 6 (tactical): `ParcelRaster` is defined in `ptax/detection/detector.py` (the detector stays free of rio-tiler imports) and `ptax/imagery/reader.py` imports it from there. The texture test excludes a `TEXTURE_WINDOW // 2` rim around every roof (edges are high-contrast), so `ClassicalDetector` grows the smooth core back by that many pixels with a geodesic dilation bounded to bright non-vegetated pixels; without it a 20×15 m roof measured 176 m².
- Task 5 (tactical): rasterio refuses raw `AWS_*` credential options in `rasterio.Env`, so `gdal_env`/`naip_env` return a `session=AWSSession(...)` (MinIO keys + endpoint, or `requester_pays=True` + `us-west-2`) alongside the remaining config switches; and because `rasterio.Env` is thread-local while `mosaic_reader` reads in a thread pool, the reader callables enter the env themselves. The Context-for-Implementer wording "`rasterio.Env(**gdal_env(settings))`" still holds — the dict now carries the session.
- Task 4 (tactical): creating an upload year requires a current parcel layer (`409 "no parcel layer"`), matching NAIP ingest and the Imagery page's empty state; `test_footprint.py` clears the stored footprint before its ST_Union count because the recompute job now derives it during every parcel ingest.
- Task 3 (tactical): `rio_cogeo.cog_translate` has no rescale option and its `dtype` is a bare cast, so `to_cog` streams the `WarpedVRT` window-by-window into a tiled uint8 GeoTIFF itself (applying the 2–98 percentile stretch for uint16 sources) and then runs `cog_translate` on that file; the clip window is snapped to whole source pixels so the stored resolution stays exact. `register_asset` takes the `ImageryAsset` row to update (`store_cog` uploads + registers), rather than creating rows itself, so Task 4's pending upload assets reuse it.
- Task 2 (tactical): the recorded Earth Search pages show only the SW quarter-quad intersecting the fixture parcels (the SE quad's west edge is ~60 m east of the grid), so discovery yields one item per year, not two; DoD updated. Real pages also always carry a `next` link, and passing `params={}` to httpx on a follow-up request strips the link's own query string — `NaipStacSource` passes `None` and caps paging at `MAX_PAGES = 50` so a misbehaving catalog raises `CatalogError` instead of looping.
- Task 2 (tactical): a 4-band uint8 GeoTIFF written without `photometric="RGB"` gets its NIR band tagged as alpha, which rio-tiler would use as a mask; the fixture generator sets the photometric and `ColorInterp.undefined` on band 4. Task 3's `to_cog` must do the same for real NAIP.
- Task 1 (tactical): tests need to import shared helpers (`ingest_parcels`, `drain_queue`, `upload_object`) from `conftest`, which requires `tests` to be a package → added `backend/tests/__init__.py`. `test_footprint.py` and later tests import `from tests.conftest import ...`.

## Deferred Ideas

- ECS deployment circuit breaker on the API service, plus `minHealthyPercent: 100` with two tasks, so a bad image fails fast instead of hanging a deploy and so every deploy stops returning 503 for its swap window.
- Run migrations as a one-off ECS task before the service update (the `SHORTCUT:` upgrade trigger), which also makes a failed deploy rollback-able.
- Sub-pixel co-registration (phase correlation) between years before differencing.
- Caching NAIP discovery results per tenant (the STAC round-trips run on every Imagery page load).
- Stale-`running` job recovery in the worker (heartbeat + reclaim) for SIGKILLed runs.
- Parallel run batches across multiple worker tasks once the worker service scales beyond one.
- Per-tenant detector defaults and a threshold-tuning view over stored indicators (detector accuracy itself is Plan D, `docs/plans/2026-09-22-detector-accuracy.md`).
