# County ArcGIS Structure Inventory Implementation Plan

> **Superseded 2026-09-24** by `docs/prd/2026-09-24-parcel-improvement-detection.md` (user decision). Kept as the record of what was built and measured.

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: PENDING
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** In the locally running app, a structure inventory run lists the structures `qwen3-vl` sees on each Richwoods Township parcel in Peoria's 2015 orthophoto. The user reviews it in the run list and parcel viewer, and it is measured against 200 parcels the user labelled by eye, against the accepted accuracy bar. Requirements: `docs/prd/2026-09-23-imagery-sources-and-local-vlm-scanner.md`.

## Out of Scope

- **Change detection.** No 2015→2019 comparison; that is the next PRD.
- **Production.** No production or AWS change: nothing is deployed, and the production default detector stays `segmentation`.
- **New UI for county setup.** County setup uses `ptax-admin` operator commands; the Parcels and Imagery pages only display the results.
- **Confirm/dismiss in the app** (Plan C). Labels come from contact sheets.
- **Other counties' profiles.** The loader is county-agnostic, but only Peoria's profile is written and tested.

## Approach

**Chosen:** County data flows into the app's existing storage, and a new single-year run kind uses the vision model.

- A county profile file drives two operator commands. One pulls a township's parcels from the county's ArcGIS feature service, keeping only the mapped fields, into the existing parcel-layer ingest (`ptax.parcels.ingest`). The other mosaics the county's ArcGIS ortho tiles into COGs through the existing `store_cog` and `compute_coverage`.
- A new `inventory` run kind reuses `runs` and `run_parcels`, the run job, the run list and the parcel viewer. Its per-parcel engine is a client for the user's OpenAI-compatible `qwen3-vl` endpoint.

**Why:** Everything downstream already works on stored COGs and `run_parcels`: previews, overlays, paging, resumable batches. Reusing it keeps the new code to the ArcGIS readers, the model client and the inventory logic. The cost is a migration that widens `runs` to allow a single-year run.

## Global Constraints

- The county profile lives at `backend/counties/peoria-il.json`; the area is Richwoods, `POL_TWP_NAME = 'RICHWOODS'`; the imagery year is 2015.
- Only the profile's mapped parcel fields are ever stored or written: `PIN`, `year_built`, `eff_year_built`, `total_living_area`, `gar_area`, `det_gar_area`, `PropClass`. Owner and address fields never reach the database, a file, or the repository.
- Model settings are `VISION_BASE_URL` (default `http://pge-hermes-00:4000/v1`), `VISION_MODEL` (default `qwen3-vl`) and `VISION_API_KEY`, which has no default and comes only from the environment. `access-qwen.md` is never committed.
- Structure kinds are exactly `house`, `garage`, `shed`, `pool` and `other`.
- The accepted accuracy bar (per parcel, presence of each kind):
  - `house`: precision and recall ≥ 0.95;
  - `garage` (detached garage or outbuilding): ≥ 0.85;
  - `shed`: ≥ 0.70;
  - `pool`: ≥ 0.80;
  - "no structures" correct: ≥ 0.95.
- The label sample is 200 Richwoods parcels, stratified by county record.

## Context for Implementer

Peoria's orthophotos come from an Esri tile cache, not a GeoTIFF.

- The service at `https://gis.peoriacounty.gov/arcgis/rest/services/RL/Orthos2015/MapServer` has cache levels 0–10. Level 10 is 0.14929 m per pixel, tiles are 256 px, the origin is (-20037508.342787, 20037508.342787), and it is in EPSG:3857. Tiles come from `/tile/{level}/{row}/{col}`, and their format is "Mixed" (JPEG or PNG).
- GDAL cannot open this service directly: `MapServer?f=json` returns HTTP 400. So the ingest computes the tile range for a bounding box, fetches the tiles, and writes one georeferenced GeoTIFF in EPSG:3857 before `to_cog`.
- Richwoods is 4 separate pieces, about 8.4 km² in total, but their union bounding box is about 58 km². Imagery is clipped per piece, not per union.
- The model at `VISION_BASE_URL` is an OpenAI-compatible LiteLLM proxy, backed by Ollama, reached over Tailscale. It takes about 45 s on a cold first call and about 7 s per image after that.

## Runtime Environment

- Local stack: `make dev-up` for Postgres, MinIO and cognito-local, plus `make api`, `make worker` and `make web`. For browser verification, use a free port pair as in the last plan.
- The worker needs `VISION_API_KEY` exported. The developer machine must be on Tailscale to reach `pge-hermes-00`.

## Assumptions

- Peoria's 2015 tile cache is served for Richwoods at level 10, as the 2019 and 2024 caches were. Task 2 depends on this, and its DoD checks it on real tiles.
- `qwen3-vl` can return structure boxes as pixel coordinates in JSON when asked. Task 3 depends on this, and its first real-image check confirms it. If it cannot, the per-parcel list is still stored and scored, but boxes are recorded as absent and the location score is reported as not measurable.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Owner or address fields leak into the database, files or commits | Medium | High | The loader keeps an allowlist of the profile's mapped fields only. A test asserts that `owner_name`, `ADDR1` and `prop_street` are absent from the written GeoJSON and from the stored `attributes` (Task 1). |
| The model returns malformed or partial JSON | High | Medium | Strict parse against the kinds enum, with one retry. After that the parcel is stored as `skipped_reason = "model_error"` with the raw reply, and never guessed (Task 3). |
| The model endpoint is down mid-run | Medium | Medium | `VisionUnavailable` becomes `RetryableError`. `execute_run` gains an `except RetryableError` branch that, like its `JobInterrupted` branch, rolls back and re-raises *without* setting `status = "failed"`. The run stays `running`, so the re-claimed job resumes from committed batches through `_restore_counters` (Task 4). |
| The imagery ingest hammers the county server | Medium | Medium | Tiles are fetched sequentially with a short fixed delay, and cached per piece so a resumed ingest skips finished pieces (Task 2). |
| The API key is committed | Medium | High | Its value comes only from the environment. `access-qwen.md` is added to `.gitignore` (Task 3). |

## E2E Test Scenarios

The local stack runs as in Runtime Environment, with Peoria set up by Tasks 1 and 2, and signed in as the demo admin.

### TS-001: Start a structure inventory
**Priority:** Critical
**Preconditions:** The Richwoods parcels and the 2015 imagery are ready; `VISION_API_KEY` is set for the worker.
**Mapped Tasks:** Task 4, Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open the Runs page | A "Structure inventory" section offers an imagery-year select containing 2015 |
| 2 | Choose 2015 and click "Start inventory" | A run appears labelled "Inventory · 2015 · qwen3-vl" and shows progress |
| 3 | Wait until the first batch commits, then reload | Parcels processed is above 0, and the run is running or succeeded |

### TS-002: Review a parcel's structures
**Priority:** High
**Preconditions:** TS-001 has processed some parcels.
**Mapped Tasks:** Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Show the inventory run's parcels and filter by "garage" | Only parcels whose summary includes a garage are listed |
| 2 | Open one parcel | One 2015 image pane with the parcel outline and the model's structures marked; the summary text; and the county record (year built, living area, garage and detached-garage area) |

### TS-003: The model unreachable, or no key set
**Priority:** Medium
**Preconditions:** The API process has no `VISION_API_KEY`.
**Mapped Tasks:** Task 4, Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start an inventory | The form shows "vision model is not configured (VISION_API_KEY)", and no run is created |

## Progress Tracking

- [x] Task 1: County profile and ArcGIS parcel loader
- [x] Task 2: ArcGIS ortho imagery ingest
- [x] Task 3: Vision model client and parcel structure inventory
- [x] Task 4: Inventory run kind: storage, API and job
- [x] Task 5: Inventory runs in the SPA
- [x] Task 6: Label sample, labelling sheets and inventory scoring
- [ ] Task 7: Run the Richwoods 2015 inventory and render the label sheets
- [ ] Task 8: Label the 200 sampled parcels
- [ ] Task 9: Score the inventory against the labels and record the result

## Deviations

- Task 1 (tactical): the server's 3,242 intersecting features include 94 neighbours that only touch Richwoods' edge within the service's tolerance (distance 0 to 1e-7°), and 51 extra pieces of 39 parcels stored as several features under one PIN. The loader re-checks intersection locally, dropping the 94, and merges each PIN's pieces into one multipolygon. Richwoods is therefore 3,097 parcels, all ingested with none skipped.
- Task 1 (tactical): `backend/src/ptax/parcels/ingest.py` changed. A layer created with `parcel_id_field` already set (the county loader's layers) is queued for ingest directly by `parcel_layer.inspect`. Admin uploads never preset the field, so their flow is unchanged.
- Task 2 (tactical): assets are square blocks of 16 x 16 tiles (about 610 m) over the tiles touching the parcel footprint, with `source_ref = "arcgis:<service>:<level>:block<row>_<col>"`, not one asset per footprint piece. Richwoods' footprint has 199 pieces, and the largest piece's bounding box spans 64 km² in EPSG:3857, about 43 GB raw at level 10. Blocks cost only the 16,674 tiles the parcels touch (140 blocks), and the reader already mosaics across assets.
- Task 2 (tactical): `ptax.county.parcels._json` became the public `arcgis_json` so the tile reader shares it (`backend/src/ptax/county/parcels.py`). Pillow is now a declared dependency for decoding mixed JPEG/PNG tiles (`backend/pyproject.toml`, `backend/uv.lock`); it was already installed transitively.
- Task 3 (tactical): boxes use Qwen-VL's native coordinates, 0–1000 over the whole image, not pixels, so they survive any resizing by the model server. Requests ask for `response_format: json_object`, and the parser strips `<think>` blocks and code fences.
- Task 4 (tactical): an unreachable model is retried per parcel after 15 s, 60 s and 180 s before `RetryableError` re-queues the job. On the job's last attempt (`MAX_ATTEMPTS`), the run is marked `failed` with the reason instead of being left `running` with no job to finish it. Inventory runs commit every 10 parcels (`INVENTORY_BATCH_SIZE`), because a parcel costs seconds of model time. A parcel with no imagery is skipped as `no_coverage`. The overlay outlines an inventory's structures instead of filling them, so the roofs stay visible.
- Task 3 (first real request, for Task 9): parcel 1301401002, a large farm parcel read at 1.39 m/px to fit 768 px. `qwen3-vl` answered valid JSON in 8.2 s, with one house at confidence 0.9, and its box lands on the building. The county records a 360 sq ft detached garage, and several small outbuildings beside the house were not reported: at that scale they are a few pixels across. Large parcels are the expected weak spot of whole-parcel rendering.
- Task 5 (tactical): the parcel list shows an inventory parcel's summary and kinds. For that, `RunParcelOut` gains `summary` and `kinds` (`backend/src/ptax/api/runs.py`), and an inventory's parcels can be listed while it runs. An ArcGIS year's label shows its service name, not its URL (`frontend/src/api/imagery.ts`, `frontend/src/api/imagery.test.ts`).
- Task 6 (tactical): the `inventory-*` commands read the local database and object store (read-only), which the rest of `ptax-eval` never does; the module docstring says so. The sheets number each cell to match the labels file's `n`.
- Task 2 (real service): Peoria 2015 ingested in 30.5 min (140 blocks, 16,674 tiles). The year is `ready` at 0.1493 m/px, 3 bands, 100% coverage, 0 parcels uncovered, and the parcel preview renders.
- Task 5 (browser, :5180 against a :8010 API on this branch): TS-003 showed "vision model is not configured (VISION_API_KEY)" with no run created. TS-001 started "Inventory · 2015 · qwen3-vl", which showed running 10 / 3097 after a reload. TS-002's garage filter left 3 of 10 parcels; the viewer showed one pane with outlines, the structures table and the county record, and the markup toggle worked. Observations for Task 9: summaries sometimes mention a shed that is not in the structure list, and on parcel 1427305016 the model reports a garage the county does not record.
- Task 6 (tactical, found rendering the real sheets): cells are scaled smoothly to fill their square, with the parcel boundary drawn 3 px wide in the model's yellow. Whole-step letterboxing halved any parcel one pixel wider than its cell, and the 1-px outline broke up at sheet scale (`backend/src/ptax/eval/inventory.py`, `backend/tests/test_eval_inventory.py`).
- Task 7 (timing): the run started 2026-09-23 22:26 and runs at about 5.8 parcels/min (about 10 s each), so about 9 h for 3,097 parcels, not 6.3 h. Task 8's labelling is blind to the run, so it proceeds in parallel.
- Tasks 3, 7 and 9 (user-agreed, 2026-09-24): the local `qwen3-vl` is dropped as the inventory model. It is too slow (8–21 s per parcel, about 9 h for Richwoods), it hung twice under load, and the user will not upgrade its hardware. The model will be a cloud model on Amazon Bedrock, chosen by comparison and then scored on the 200 labels. The app's client reaches Bedrock's OpenAI-compatible endpoint (`bedrock-mantle`) unchanged. Models served only through Bedrock's Converse API need a small client addition, decided once a model is chosen. The Richwoods run that failed at 170 parcels is not resumed.
- Tasks 3–6, 8 and 9 (user-agreed, 2026-09-24): the inventory need not say what a structure is, only that a structure or improvement is there, and where. Kinds (house, garage, shed, pool, other) are dropped as a requirement. The per-kind accuracy bar in Global Constraints no longer applies. A replacement bar (improvement found per parcel, "no improvements" correct, box placement) and the matching changes to the prompt, the stored indicators, the kind filter, the labels template and the scoring are to be settled once prompt tuning on the seven test parcels converges.
- All tasks (user-agreed, 2026-09-24, hard constraint): structures are identified from the imagery alone. County-provided structure data, such as Peoria's `Building_Outlines` footprints, is never used: not as input, not as training labels, not as ground truth. The county parcels remain only as the property boundaries being inventoried.
- Out-of-plan change (user-directed, 2026-09-24): the segmentation detector is removed from the codebase entirely. That covers the detector, its training harness, the model store and `model-publish`, the `ml` dependency group, the infra `SEGMENTER_MODEL` setting, and its tests and docs. Change runs default to the classical detector. Migration 0005 and the `segmentation` value stay in the database checks, so past runs keep their history. Production is left as deployed until the user chooses to redeploy.

## Implementation Tasks

### Task 1: County profile and ArcGIS parcel loader

**Objective:** Add a county profile for Peoria and an operator command, `ptax-admin county-parcels backend/counties/peoria-il.json --area richwoods --tenant-fips <fips>`. The command fetches the township boundary and every parcel that intersects it from the county's ArcGIS services, keeps only the profile's mapped fields, and feeds the result into the existing parcel-layer ingest as that tenant's current layer.

**Files:**

- Create: `backend/counties/peoria-il.json`
- Create: `backend/src/ptax/county/__init__.py`
- Create: `backend/src/ptax/county/profile.py`
- Create: `backend/src/ptax/county/parcels.py`
- Modify: `backend/src/ptax/cli.py`
- Modify: `backend/src/ptax/parcels/ingest.py`
- Test: `backend/tests/test_county_parcels.py`

**Key Decisions / Notes:**

- The profile holds:
  - the parcel service URL and its id field (`PIN`);
  - the field map (county field → canonical name);
  - the areas: a service layer plus its `where` clause;
  - the imagery services, keyed by year.
- ArcGIS queries page by `resultOffset` / `resultRecordCount` with `f=geojson`, and POST the area geometry (it is too long for a URL). The paging pattern follows `ptax.eval.sources.fetch_parcels` (`backend/src/ptax/eval/sources.py:64`).
- The output is a GeoJSON uploaded to S3 as a `ParcelLayer`, with `original_filename` naming the county and area. It then goes through the existing `parcel_layer.inspect` and `parcel_layer.ingest` jobs with `parcel_id_field` set, so storage stays one code path (`backend/src/ptax/parcels/ingest.py:116`).
- The allowlist is applied before anything is written; PII rules are in Global Constraints.

**Definition of Done:**

- [x] Against a mocked ArcGIS service (httpx `MockTransport`), the command stores only parcels that intersect the area, across several result pages. Their `attributes` hold exactly the mapped fields.
- [x] `owner_name`, `ADDR1` and `prop_street` are absent from the uploaded GeoJSON and from every stored parcel's `attributes`.
- [x] Against the real services, `county-parcels … --area richwoods` ingests 3,242 parcels (± the county's live edits) — actual 3,097; see Deviations, and the layer becomes the tenant's current layer.
- [x] Verify: `cd backend && uv run pytest tests/test_county_parcels.py -q && uv run ruff check src tests && uv run mypy src`

### Task 2: ArcGIS ortho imagery ingest

**Objective:** Add an `arcgis` imagery source, and `ptax-admin county-imagery backend/counties/peoria-il.json --year 2015 --tenant-fips <fips>`. The command creates an imagery year and queues a job that turns the county's cached ortho tiles into stored COG assets, one per piece of the tenant's parcel footprint, at the service's finest level. Previews, runs and coverage then work on it exactly as they do on NAIP.

**Files:**

- Create: `backend/alembic/versions/0006_arcgis_imagery_source.py`
- Create: `backend/src/ptax/county/imagery.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/imagery/ingest.py`
- Modify: `backend/src/ptax/cli.py`
- Modify: `backend/src/ptax/county/parcels.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Test: `backend/tests/test_county_imagery.py`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- The migration widens `imagery_years_source_check` to `('naip', 'upload', 'arcgis')`, and `IMAGERY_SOURCES` in `backend/src/ptax/db/models.py:37` changes to match.
- The tile math comes from the service's `tileInfo`: the finest LOD, the origin and the tile size. A bounding box in EPSG:3857 maps to a row/column range, and the mosaic is written as a GeoTIFF with transform `from_origin(tile_x0, tile_y0, res, res)`. Missing tiles (404/204) become nodata 0. Tiles are fetched sequentially with a 50 ms delay.
- Tiles are decoded from the response bytes with the format sniffed from the content (JPEG or PNG; the cache is "Mixed"), never from a file extension or `Content-Type` alone.
- One asset per footprint piece, with `source_ref = "arcgis:<service>:<level>:piece<n>"`. Finished pieces are skipped on resume, mirroring `ingest_naip`'s `done` set (`backend/src/ptax/imagery/ingest.py:160`). Then `compute_coverage`.
- `ImageryYear.provider` records the service URL; `resolution_m` comes from `inspect_raster`.

**Definition of Done:**

- [x] From mocked tiles, the mosaic is georeferenced exactly: a pixel's centre maps to the expected EPSG:3857 coordinate within half a pixel.
- [x] A missing tile becomes nodata without failing the ingest, a mosaic mixing JPEG and PNG tiles decodes both, and a resumed job skips completed pieces.
- [x] Against the real 2015 service, `county-imagery --year 2015` leaves the year `ready`, at about 0.15 m and 100% coverage of Richwoods, and a parcel preview (`/api/imagery/years/{id}/parcels/{parcel}/preview.png`) returns imagery.
- [x] Verify: `cd backend && uv run pytest tests/test_county_imagery.py tests/test_migrations.py -q && uv run ruff check src tests && uv run mypy src`

### Task 3: Vision model client and parcel structure inventory

**Objective:** Add a client for the user's OpenAI-compatible model endpoint, plus the per-parcel inventory function. The function renders a parcel's single-year image with its outline, asks `qwen3-vl` for the structures inside the parcel as strict JSON, validates the answer, and converts each structure's pixel box into a georeferenced polygon.

**Files:**

- Create: `backend/src/ptax/vision/__init__.py`
- Create: `backend/src/ptax/vision/client.py`
- Create: `backend/src/ptax/vision/inventory.py`
- Modify: `backend/src/ptax/config.py`
- Modify: `.gitignore`
- Test: `backend/tests/test_vision_inventory.py`

**Key Decisions / Notes:**

- The settings are listed in Global Constraints. `client.py` posts `chat/completions` with a base64 PNG `image_url`, `temperature` 0, and a timeout of 180 s. Connection errors and 5xx raise a dedicated `VisionUnavailable`.
- The image is the parcel plus a small buffer, rendered with `ptax.imagery.preview.plan_view` / `paint` (`backend/src/ptax/imagery/preview.py:45`) so the outline matches the viewer's.
- The model must reply with `{"structures": [{"kind", "box": [x0, y0, x1, y1], "confidence"}], "summary"}`. Validation keeps only the Global Constraints kinds, boxes inside the image, and confidence between 0 and 1.
  - One retry, with the validation error quoted back.
  - Then `InventoryError`, carrying the raw reply.
  - Boxes are clipped to the parcel polygon and converted through the raster's transform.
- `access-qwen.md` goes into `.gitignore`.

**Definition of Done:**

- [x] With a stub HTTP server, a valid reply yields structures whose polygons land on the image pixels named by their boxes. An unknown kind or an out-of-image box is rejected, and malformed JSON is retried once, then raises `InventoryError`.
- [x] A refused connection raises `VisionUnavailable`, and no key configured raises before any request is made.
- [x] One real request to `qwen3-vl` on a Richwoods parcel returns a valid inventory. It is recorded in `backend/eval/README.md` (Task 9) as the first real check.
- [x] Verify: `cd backend && uv run pytest tests/test_vision_inventory.py -q && uv run ruff check src tests && uv run mypy src`

### Task 4: Inventory run kind: storage, API and job

**Objective:** Widen runs so that a run can be a single-year structure inventory using the `vision` detector. Add `POST /api/runs/inventory`, and a run job that stores each parcel's structures, summary, kinds and counts in `run_parcels`, with the structure polygons as its markup. The run is resumable and cancellable like any other.

**Files:**

- Create: `backend/alembic/versions/0007_inventory_runs.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/api/runs.py`
- Modify: `backend/src/ptax/detection/run.py`
- Test: `backend/tests/test_inventory_runs.py`
- Test: `backend/tests/test_runs.py`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- The migration adds `runs.kind` (`'change' | 'inventory'`; existing rows are backfilled `change`, and the server default is then dropped, as in `backend/alembic/versions/0005_run_detector.py:1`). It makes `target_year_id` nullable, and adds `vision` to the detector check. It also adds a check: `(kind = 'inventory') = (target_year_id IS NULL)`, and an inventory run's detector is `vision`, with `model_name` holding the model.
- The inventory is queued through a `queue_inventory` sibling of `queue_run` (`backend/src/ptax/api/runs.py:161`). It returns 409 when `VISION_API_KEY` is unset, and 404 or 422 for a missing or unready year.
- Per parcel, results are stored as:
  - `indicators`: `structures`, `summary`, `kinds`, `counts`, `model` and `resolution_m`;
  - `structure_geom`: the union of the structure polygons;
  - `score`: the highest structure confidence, or 0;
  - `candidate`: false.
  `InventoryError` becomes `skipped_reason = "model_error"`, and `VisionUnavailable` raises `RetryableError`.
- **Resumability:** today `execute_run` (`backend/src/ptax/detection/run.py:372`) marks the run `failed` for any exception except `JobInterrupted`, and a `failed` run is never resumed (its status guard returns early). Add an `except RetryableError` branch that mirrors `except JobInterrupted`: roll back, re-raise, and leave the status `running`.
- An inventory run's single year is stored as `base_year_id`, with `target_year_id` null. `RunOut.target_year` becomes optional (`RunYearOut | None`). `_out()` and `_years_for()` handle a null target, and `RunOut` gains `kind`. `parcel_overlay` (`backend/src/ptax/api/runs.py:399`) renders `run.target_year_id or run.base_year_id`, so an inventory overlay draws on its one year.
- `GET /api/runs/{id}/parcels` gains a `structure=<kind>` filter over `indicators->'kinds'`. `RunParcelDetailOut` gains `parcel_attributes`, the parcel's mapped county fields.

**Definition of Done:**

- [x] An inventory run over the fixture layer, with a stubbed model, stores the structures, kinds and markup for every parcel and succeeds. A malformed reply leaves that parcel `model_error` while the rest are scored.
- [x] With the model unreachable partway through, the run stays `running` (not `failed`), and a second `execute_run` on the same run completes only the remaining parcels. Cancelling stops at a batch boundary.
- [x] The kind filter returns only matching parcels. Change runs behave exactly as before; the existing `test_runs.py` passes unchanged.
- [x] Migration 0007 backfills existing runs as `change`, rejects inconsistent rows, and downgrades cleanly.
- [x] With a tenant holding both a change run and an inventory run, `GET /api/runs`, `GET /api/runs/{id}` and the inventory parcel's `overlay.png` all succeed, and the inventory run serialises with `target_year: null`.
- [x] Verify: `cd backend && uv run pytest tests/test_inventory_runs.py tests/test_runs.py tests/test_migrations.py -q && uv run ruff check src tests && uv run mypy src`

### Task 5: Inventory runs in the SPA

**Objective:** On the Runs page, start a structure inventory on one imagery year, and label inventory runs in the list. In the parcel list, filter by structure kind and show each parcel's summary. The parcel viewer shows an inventory parcel's single image with the model's structures marked, its summary and structure list, and the county's record. Verified by TS-001 to TS-003.

**Files:**

- Modify: `frontend/src/api/runs.ts`
- Modify: `frontend/src/pages/RunsPage.tsx`
- Modify: `frontend/src/pages/ParcelViewerPage.tsx`
- Modify: `frontend/src/api/imagery.ts`
- Modify: `backend/src/ptax/api/runs.py`
- Test: `frontend/src/api/runs.test.ts`
- Test: `frontend/src/api/imagery.test.ts`
- Test: `backend/tests/test_inventory_runs.py`

**Key Decisions / Notes:**

- `runs.ts` types `Run.target_year` as `RunYear | null`. Every consumer (`RunsPage.tsx` run label, `ParcelViewerPage.tsx` panes) handles the null inventory case. `runs.ts` also adds:
  - `RunKind` and `createInventory(yearId)`;
  - the `structure` query parameter on `listRunParcels`;
  - `runLabel(run)`, which gives "Inventory · 2015 · qwen3-vl" for inventories and the existing `detectorLabel` text otherwise;
  - `indicatorsFor("vision")`.
- Viewer: for `kind === "inventory"`, one pane (the run's year) with the markup toggle, plus a structures table (kind and confidence) and a county-record block from `parcel_attributes`. Change runs render exactly as now.
- The inventory start form reuses the existing form error slot for the 409 message.

**Definition of Done:**

- [x] The Runs page starts an inventory, and the run appears with its inventory label.
- [x] The parcel list filters by kind, and the viewer shows one pane with markup, the structures and the county record for an inventory parcel.
- [x] Verify: `make test-frontend` and `cd frontend && pnpm exec tsc -b --noEmit && pnpm lint`; TS-001–TS-003 pass in the browser

### Task 6: Label sample, labelling sheets and inventory scoring

**Objective:** Add three `ptax-eval` commands:
- `inventory-sample` draws the 200-parcel stratified sample from the tenant's layer;
- `inventory-sheets` renders the sample's 2015 images as contact sheets for labelling by eye, blind to the model;
- `inventory-score` compares an inventory run with the filled labels file and the county records, and reports each kind against the accepted bar.

**Files:**

- Create: `backend/src/ptax/eval/inventory.py`
- Modify: `backend/src/ptax/eval/cli.py`
- Test: `backend/tests/test_eval_inventory.py`

**Key Decisions / Notes:**

- Strata come from `parcel_attributes`: `det_gar_area > 0`; `gar_area > 0` only; no garage with a building; and vacant or no `year_built`. The draw is 50 per stratum, or all of a stratum if it has fewer, then topped up to 200 with a fixed seed.
- Sheets use the `ptax.eval.chips` layout (`backend/src/ptax/eval/chips.py:163`) with one panel and the parcel outline, and never draw the model's markup.
- The labels file, `backend/eval/inventory-labels-peoria-richwoods-2015.json`, holds only `PIN` and, per kind, the count, plus an optional note.
- Scoring reports, per kind: presence precision and recall; the exact-count rate; the "no structures" accuracy; and county agreement (recorded detached garage → garage found, and so on). It marks pass or fail against the bar in Global Constraints. Each kind's result also shows its positive-label count (N). A kind with N < 10 is reported as "too few labels to judge", not pass or fail. The county records have no shed or pool fields, so those kinds' N depends on how often they occur in the sample.

**Definition of Done:**

- [x] The sample is deterministic for a seed, holds 200 parcels, and covers every non-empty stratum.
- [x] On a synthetic run and labels, scoring reproduces hand-computed precision and recall per kind, and the pass/fail against the bar. A kind with fewer than 10 positive labels reports "too few labels to judge", with its N.
- [x] The sheets and the labels template contain no owner or address fields.
- [x] Verify: `cd backend && uv run pytest tests/test_eval_inventory.py -q && uv run ruff check src tests && uv run mypy src`

### Task 7: Run the Richwoods 2015 inventory and render the label sheets

**Objective:** On the local stack, set up Peoria with Tasks 1 and 2, then run the 2015 structure inventory over all Richwoods parcels through the app, and record throughput. Draw the 200-parcel sample and render its labelling sheets and labels template for the user.

**Files:**

- Create: `backend/eval/inventory-sample-peoria-richwoods-2015.json`
- Create: `backend/eval/inventory-labels-peoria-richwoods-2015.json`

**Key Decisions / Notes:**

- A Peoria tenant is created with `ptax-admin create-tenant`. The run is started from the SPA or with `ptax-admin start-run`, whichever Task 4 makes available for inventories. It runs in the background for about 6.3 hours.
- The sample and the empty labels template are committed. The sheets go to `backend/eval/out/`, which is gitignored.

**Definition of Done:**

- [ ] The inventory run succeeds over the Richwoods layer. The worker log's `mean_parcel_ms` and the parcels-per-minute figure are captured.
- [ ] The sample file and the labels template exist, and the sheets are rendered for all 200 parcels.
- [ ] Verify: `cd backend && uv run ptax-eval inventory-sheets --help && ls eval/out/inventory-sheets/*.png | wc -l`

### Task 8: Label the 200 sampled parcels

**Objective:** Record by eye, on the contact sheets, which structures each sampled parcel really has. These labels are the ground truth that the inventory is scored against.

**Owner:** User

**User Action:** Open the sheets in `backend/eval/out/inventory-sheets/`. For each of the 200 parcels, fill in the counts of house, garage, shed, pool and other in `backend/eval/inventory-labels-peoria-richwoods-2015.json`, then reply `done`.

**Files:**

- Modify: `backend/eval/inventory-labels-peoria-richwoods-2015.json`

**Key Decisions / Notes:**

- The labelling rules are the ones in the template header. For example: "garage" means a detached garage or outbuilding, while an attached garage counts as part of the house.

**Definition of Done:**

- [ ] All 200 parcels carry a label, and the file validates with `ptax-eval inventory-score --check-labels`.

### Task 9: Score the inventory against the labels and record the result

**Objective:** Score the Richwoods inventory run against the user's labels and the county records. Record the per-kind results, the pass or fail against the bar, throughput, and the first-real-request check in `backend/eval/README.md`. Summarise it in `README.md`, and update the PRD's status.

**Files:**

- Modify: `backend/eval/README.md`
- Modify: `README.md`
- Modify: `docs/prd/2026-09-23-imagery-sources-and-local-vlm-scanner.md`

**Key Decisions / Notes:**

- A kind that misses the bar is reported with its numbers and examples, not hidden. The next step, whether change detection or a better prompt or model, is recorded as a recommendation, not decided here.

**Definition of Done:**

- [ ] `backend/eval/README.md` records each kind's precision and recall, with N, plus count accuracy, "no structures" accuracy and county agreement, with pass or fail against the bar. It flags every kind whose N was too small to judge, and records throughput and the county-scale projection.
- [ ] Verify: `cd backend && uv run ptax-eval inventory-score --run <inventory run id> --labels eval/inventory-labels-peoria-richwoods-2015.json`
