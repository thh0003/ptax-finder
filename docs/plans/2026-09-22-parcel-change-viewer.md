# Parcel Change Viewer Implementation Plan

Created: 2026-09-22
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** A reviewer opens a parcel from a run and sees the base-year image, the target-year image, and the target-year image with the new structures the run detected drawn on it — so the score they act on is backed by a picture of *where* the change is.

## Out of Scope

- **Confirm / dismiss / notes / review history** — Plan C (`docs/plans/2026-09-21-review-workflow.md`) owns them and composes onto this same panel.
- **The review queue and map** — Plan C. Task 6 ships a plain score-ordered list purely as the way in; it has no statuses, filters, or actions.
- **Editing or correcting the markup** — v1 shows what the detector produced.
- **A vegetation-loss overlay** — `veg_loss_m2` is shown as a number; only new built-up area is drawn.
- **Backfilling markup onto runs completed before this ships** — they keep their scores and show no markup. Re-deriving it with today's detector would produce a picture that disagrees with the score recorded next to it.

## Approach

**Chosen:** Store the detected masks as EPSG:4326 polygons on `run_parcels`, and render them with the same `rasterize` step `parcel_preview` already uses for the parcel boundary (`backend/src/ptax/api/imagery.py:431-447`).

**Why:** Polygons are resolution-independent, so the markup registers correctly on a display image rendered at a different grid from the detection grid (`comparison_resolution`, `backend/src/ptax/detection/run.py:70`) — and PostGIS already stores parcel geometry this way. Storing a raster mask instead would pin the markup to the detection grid and need resampling at every display size. The cost is a polygonisation step per scored parcel and two nullable geometry columns at county scale; Task 2 bounds both.

## Global Constraints

- All three images for one (run, parcel) must be rendered on **byte-identical extents**. Extracting the computation (Task 3) is necessary but not sufficient — it only guarantees identical output for identical inputs. The overlay route therefore takes `size`, `buffer` and `outline` query parameters with **the same names, types, ranges and defaults** as `parcel_preview` (`size=512`, `buffer=0.25`, `outline=False`; `backend/src/ptax/api/imagery.py:401-403`), and the frontend builds all three image URLs from **one shared parameter object**, never three independently constructed calls.
- Three colours must be mutually distinguishable, not merely distinct from the background: the scoring structure, the remaining new built-up area, and the parcel boundary's `OUTLINE_RGB = (255, 255, 0)` (`backend/src/ptax/api/imagery.py:31`).
- A `run_parcels` row with NULL geometry renders **no markup**, never a recomputed one — this is what keeps an old run's picture consistent with its recorded score.
- New API routes carry the existing bearer-token auth and tenant scoping used by `_get_run` (`backend/src/ptax/api/runs.py:92`).

## Context for Implementer

Detection and display use different grids. `ClassicalDetector.compare` works on a grid whose resolution comes from both imagery years and the parcel's extent, with no surrounding context. `parcel_preview` renders a square image of a requested pixel `size` with a `buffer` of surrounding context, on a UTM grid derived from the parcel. The same ground therefore has two different pixel geometries, which is the whole reason the mask is stored as polygons rather than pixels.

`run_parcels` is written in committed batches of 200 (`flush_batch`, `backend/src/ptax/detection/run.py:196`) during runs that cover tens of thousands to a few hundred thousand parcels, so anything added per parcel is paid at that scale.

## Runtime Environment

- **Start:** `make dev-up` (PostGIS :5432, MinIO :9000, cognito-local :9229), `make seed`, then `make api` (:8000), `make worker`, `make web` (:5173)
- **Health:** `GET /api/health` → `{"status":"ok","db":"ok"}`
- **Migrations:** `cd backend && uv run alembic upgrade head`
- **Demo login:** `admin@demo.test` / `Password1!`

## File Structure

- `backend/src/ptax/detection/geometry.py` (create) — pure: boolean mask + affine transform + CRS → EPSG:4326 MultiPolygon. No I/O, no DB.
- `backend/src/ptax/imagery/preview.py` (create) — pure: the parcel→(extent, resolution, UTM geometry) computation shared by both image endpoints, plus the band-colouring helper.
- `backend/src/ptax/api/runs.py` (modify) — run-parcel list, detail, and overlay image routes.
- `frontend/src/pages/ParcelViewerPage.tsx` (create) — the three-image viewer.
- `frontend/src/api/runs.ts` (modify) — run-parcel list/detail fetchers and authed image-blob URLs.

## Assumptions

- Every parcel a reviewer opens belongs to the run's pinned layer, so `parcels.id` resolves — Tasks 3–5 depend on it. Runs are pinned to `run.layer_id` (`backend/src/ptax/detection/run.py:241`).

> Not an assumption: `rasterio.features.shapes` was checked during planning against the installed rasterio 1.5.1 — it polygonises a boolean mask with exact areas and needs no new dependency.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Polygonising every parcel materially slows a county-sized run | Medium | High | Task 2 skips the conversion entirely when the mask is empty (the common case) and simplifies to half the detection pixel size; Task 2's DoD measures per-parcel scoring time against the same fixture run before the change and requires no more than a 20% increase |
| Stored geometry inflates the database at county scale | Medium | Medium | Only non-empty masks are stored; both columns are nullable and hold simplified polygons. Task 2's DoD **fails** above 2 KB average per non-NULL row and requires the projected total for a 200 000-parcel run to be stated |
| The overlay and the two preview images drift out of registration, so the markup points at the wrong ground | Medium | High | Extraction alone cannot catch this — the three images are three separate HTTP calls. Task 3 pins the overlay's `size`/`buffer` contract to `parcel_preview`'s, Task 5 builds all three URLs from one shared parameter object, and Task 5's DoD asserts the three requests the page actually issues carry identical values |
| The unfiltered score-ordered list sorts hundreds of thousands of rows on every page | Medium | High | `run_parcels_queue_idx` cannot serve it (see Task 4); Task 4 adds a matching index and its DoD requires an `EXPLAIN` showing no sort node |
| The markup disagrees with the score displayed beside it | Low | High | Task 2 stores the exact masks `compare` scored from, in the same transaction as the score; Task 1's DoD ties polygon area back to the reported `structure_m2` |

## Goal Verification

### Truths

1. The markup a reviewer sees is the area the run recorded when it scored that parcel — it does not change when the detector's code changes, and a run scored before this feature existed shows no markup rather than a recomputed one.
2. The base image, the target image, and the marked-up target image for one parcel cover the same ground at the same scale, so a feature at a given position in one is at that position in all three.

## E2E Test Scenarios

### TS-001: Reviewer inspects a flagged parcel and checks the markup
**Priority:** Critical
**Preconditions:** `make dev-up` + `make seed`; signed in as `admin@demo.test`; Demo County fixture layer ingested; fixture imagery years 2021 and 2023 ready; a completed `2021 → 2023` run.
**Mapped Tasks:** Task 3, Task 4, Task 5, Task 6

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `/runs` and open the finished `2021 → 2023` run | A score-ordered parcel list appears, highest score first |
| 2 | Click the top-ranked parcel | Navigates to `/runs/{runId}/parcels/{parcelId}`; three image panes render |
| 3 | Read the three panes | Left is 2021, middle is 2023, right is 2023 with a marked area; all three show the same ground at the same scale |
| 4 | Read the measurements beside the images | Score, candidate flag, `structure_m2`, `new_builtup_m2`, `veg_loss_m2` and the comparison resolution are shown |
| 5 | Click the markup toggle | The marked area disappears from the third pane, leaving bare 2023 imagery; the other two panes are unchanged |
| 6 | Click the toggle again | The marked area returns, in the same position |

### TS-002: The marked area distinguishes the scoring structure from other detections
**Priority:** High
**Preconditions:** As TS-001, on a parcel whose run recorded both a scoring structure and additional new built-up area.
**Mapped Tasks:** Task 1, Task 2, Task 3

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open that parcel's viewer | The third pane shows two visually distinct markup styles |
| 2 | Compare the emphasised area against the displayed `structure_m2` | The emphasised area is the one the score rests on; the secondary style covers the remaining new built-up area |
| 3 | Confirm both differ from the parcel boundary colour | Boundary remains distinguishable from both markup styles |

### TS-003: A skipped parcel explains itself
**Priority:** High
**Preconditions:** A run containing a parcel with `skipped_reason` set.
**Mapped Tasks:** Task 4, Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open the skipped parcel from the run's list | The viewer loads without error |
| 2 | Read the panes | The skip reason is stated in words; whichever year's imagery exists is still shown; no empty or misleading pane, and no markup |

### TS-004: A run completed before this feature shows no markup
**Priority:** Medium
**Preconditions:** A `run_parcels` row with a score but NULL geometry columns.
**Mapped Tasks:** Task 2, Task 3, Task 5

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open that parcel's viewer | Base and target images render; the score and measurements are shown |
| 2 | Look for the markup pane and toggle | The view states that this run recorded no markup, and the toggle is absent or disabled rather than showing an empty overlay |

## Progress Tracking

- [x] Task 1: Detector returns the masks it scored from, and a mask→polygon converter
- [x] Task 2: Persist both masks per scored parcel (migration + run job)
- [x] Task 3: Shared preview extent + the run-scoped overlay image endpoint
- [x] Task 4: Run-parcel list and detail API
- [x] Task 5: Parcel viewer page — three images, toggle, measurements, skip reasons
- [x] Task 6: Score-ordered parcel list on the run, and route wiring

## Implementation Tasks

### Task 1: Detector returns the masks it scored from, and a mask→polygon converter

**Objective:** `ClassicalDetector.compare` already computes the new built-up mask and picks the largest compact component from it, then throws both away and reports only scalars. Return them instead, and add a pure converter that turns a boolean mask on a projected grid into an EPSG:4326 MultiPolygon. This is the raster-to-geometry half of the feature, with no database and no HTTP in it.

**Files:**

- Create: `backend/src/ptax/detection/geometry.py`
- Modify: `backend/src/ptax/detection/detector.py`
- Test: `backend/tests/test_detection_geometry.py`
- Test: `backend/tests/test_detector.py`

**Key Decisions / Notes:**

- `ChangeResult` (`backend/src/ptax/detection/detector.py:35`) gains `new_builtup_mask` and `structure_mask`, both `np.ndarray | None`. `_largest_structure` (`detector.py:252`) currently returns `(px, fill)`; it must also return the chosen component's mask so the two never disagree about which blob scored.
- `mask_to_multipolygon(mask, transform, crs, *, simplify_m)` uses `rasterio.features.shapes` — the inverse of the `rasterize` already imported at `backend/src/ptax/api/imagery.py:11` — then reprojects with the `Transformer` + `shapely_transform` pair used at `imagery.py:415-416`. Returns `None` for an all-False mask so callers can skip storage entirely.
- Simplify at half the detection pixel size: enough to drop per-pixel staircase vertices, too little to move a boundary by a visible amount.
- Keep `compare` free of CRS work — it holds `ParcelRaster.transform` and `.crs` but geometry conversion belongs in the new module so it can be tested without imagery.

**Definition of Done:**

- [ ] `compare` on a fixture pair with a planted roof returns a `structure_mask` whose True-pixel count times the pixel area equals the reported `structure_m2` indicator.
- [ ] `structure_mask` is a subset of `new_builtup_mask` on every fixture pair that produces one.
- [ ] `mask_to_multipolygon` on a known rectangle of set pixels returns a polygon whose EPSG:4326 area, reprojected back to the source CRS, is within 2% of the rectangle's true area.
- [ ] `mask_to_multipolygon` returns `None` for an all-False mask.
- [ ] Verify: `cd backend && uv run pytest tests/test_detection_geometry.py tests/test_detector.py -q`

### Task 2: Persist both masks per scored parcel (migration + run job)

**Objective:** Add two nullable geometry columns to `run_parcels` and write them in the same batch that writes the score, so the markup and the score it explains are committed together. Empty masks store NULL, which is both the common case and what makes a pre-existing run render no markup.

**Files:**

- Create: `backend/alembic/versions/0003_run_parcel_geometry.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/detection/run.py`
- Test: `backend/tests/test_runs.py`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- Two columns on `RunParcel` (`backend/src/ptax/db/models.py:273`): `new_builtup_geom` and `structure_geom`, both `Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)`, nullable — matching the existing pattern at `models.py:119`. No spatial index: these are read by primary key from the viewer, never searched spatially.
- Migration `0003` follows `0002` (`down_revision = "0002"`). Both columns are nullable with no default and no backfill, so it applies to a populated table without rewriting rows and existing runs keep their scores with NULL markup.
- `score_parcel` (`backend/src/ptax/detection/run.py:76`) converts each mask with Task 1's helper using `base_raster.transform` / `.crs`, and leaves the column NULL when the helper returns `None`.
- Hot path: this runs once per parcel inside the batch loop. Skip the conversion entirely when the mask has no True pixels before calling the converter — most parcels in a county detect nothing, so the common case must cost one `.any()` call.

**Definition of Done:**

- [ ] `uv run alembic upgrade head` then `downgrade -1` both succeed against the compose PostGIS, and `upgrade head` applied to a `run_parcels` table containing rows leaves those rows' existing `score` and `indicators` unchanged.
- [ ] After a fixture run, a parcel with a planted roof has non-NULL `structure_geom` whose area reprojected to the run's UTM zone is within 5% of its stored `indicators["structure_m2"]`.
- [ ] A parcel the run detected nothing on has NULL in both columns.
- [ ] A parcel with `skipped_reason` set has NULL in both columns.
- [ ] Per-parcel scoring time over the fixture layer increases by no more than 20% against the same run measured without the geometry write, and the measurement is recorded in the task's completion note.
- [ ] Average stored geometry is **under 2 KB per non-NULL row** on the fixture run (`SELECT avg(pg_column_size(new_builtup_geom) + pg_column_size(structure_geom))`), and the completion note states the projected total for a 200 000-parcel run at the fixture's observed non-NULL rate. Over 2 KB fails this task — it means the simplification in Task 1 is too weak.
- [ ] Verify: `cd backend && uv run pytest tests/test_runs.py tests/test_migrations.py -q`

### Task 3: Shared preview extent + the run-scoped overlay image endpoint

**Objective:** Extract the extent computation out of `parcel_preview` so one function decides the ground a parcel's images cover, then add `GET /api/runs/{run_id}/parcels/{parcel_id}/overlay.png` that renders the target year through that same function with the stored masks drawn on top. Extraction first is what guarantees the three images register; a second endpoint that recomputes the extent would drift.

**Files:**

- Create: `backend/src/ptax/imagery/preview.py`
- Modify: `backend/src/ptax/api/imagery.py`
- Modify: `backend/src/ptax/api/runs.py`
- Test: `backend/tests/test_run_overlay.py`
- Test: `backend/tests/test_tiles.py`

**Key Decisions / Notes:**

- Move the extent block at `backend/src/ptax/api/imagery.py:412-426` (UTM reprojection, `long_side`, `buffer_m`, `resolution_m`, asset lookup, `read_parcel`) into `preview.py` and have `parcel_preview` call it. Its observable output must not change — `tests/test_tiles.py:176` already covers it and must pass untouched.
- Also move the rasterize-and-colour block at `imagery.py:431-447` into a helper taking geometry, colour and width, so the boundary and the two markup layers all draw through one code path.
- Draw order in the overlay: other new built-up first, then the scoring structure over it, then the parcel boundary last so it stays readable. Three mutually distinguishable colours per `## Global Constraints`.
- **The overlay route declares `size`, `buffer` and `outline` with the same names, types, ranges and defaults as `parcel_preview` (`backend/src/ptax/api/imagery.py:401-403`).** Sharing the extent function is not enough on its own: these are three separate HTTP calls, so a differing default would silently misregister the markup while every backend test still passed.
- The overlay reads `run_parcels.new_builtup_geom` / `structure_geom` for `(run_id, parcel_id)` and renders the target year of `run.target_year_id`. NULL geometry renders the plain target image — the caller distinguishes the two through Task 4's detail response, not by inspecting pixels.
- Tenancy and auth via `_get_run` (`backend/src/ptax/api/runs.py:92`); a parcel outside the caller's tenant is a 404, not a 403.

**Definition of Done:**

- [ ] `GET /api/imagery/years/{year_id}/parcels/{parcel_id}/preview.png` returns the same bytes and the same `X-Bounds` header as before the extraction, for a fixture parcel.
- [ ] For one (run, parcel), the base preview, the target preview and the overlay return **identical** `X-Bounds` header values.
- [ ] The overlay of a parcel with a stored `structure_geom` differs in pixels from the plain target preview of the same parcel; the overlay of a parcel with NULL geometry is byte-identical to it.
- [ ] On a fixture parcel whose `new_builtup_geom` extends beyond its `structure_geom`, sampling the rendered PNG inside the structure region and inside the remaining new-built-up region yields **two different colour values**, and both differ from `OUTLINE_RGB`. Asserted on pixel values, not by eye — an implementation that draws both layers in one colour must fail here.
- [ ] The overlay route's `size` and `buffer` query parameters have the same defaults and validation ranges as `parcel_preview`'s, asserted from the OpenAPI schema so a later edit to either cannot silently diverge.
- [ ] A request for a run or parcel belonging to another tenant returns 404.
- [ ] Verify: `cd backend && uv run pytest tests/test_run_overlay.py tests/test_tiles.py -q`

### Task 4: Run-parcel list and detail API

**Objective:** Expose the per-parcel results a run already stores. The list is score-ordered and paginated so the viewer can be reached highest-score-first; the detail carries everything the viewer shows beside the images, including whether markup exists at all.

**Files:**

- Create: `backend/alembic/versions/0004_run_parcels_score_idx.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/src/ptax/api/runs.py`
- Test: `backend/tests/test_runs.py`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- `GET /api/runs/{run_id}/parcels` — `score DESC NULLS LAST`, then `parcel_ref` for a stable order under ties. Cursor or offset paging with a bounded page size. An optional `candidate` filter is permitted; status filters are Plan C's.
- ⛔ **The existing `run_parcels_queue_idx` cannot serve this query.** It is `(run_id, candidate, score DESC)` (`backend/src/ptax/db/models.py:277`), and `candidate` sits *between* the equality column and the sort column. With no predicate on `candidate` — which is the default list, and the one Task 6 and TS-001 use — a B-tree stores `candidate=false` rows and `candidate=true` rows as two separately-ordered groups, and PostgreSQL has no loose/skip index scan to merge them, so it falls back to sorting the whole run. At a few hundred thousand rows that is a full sort on every page.
- This task therefore adds migration `0004` (`down_revision = "0003"`) with a matching index on `(run_id, score DESC NULLS LAST, parcel_ref)`. It belongs to this task, not Task 2's migration, so each task's migration lands with the code that needs it.
- `GET /api/runs/{run_id}/parcels/{parcel_id}` — `score`, `candidate`, `skipped_reason`, `indicators`, `parcel_ref`, and a boolean `has_markup` derived from whether either geometry column is non-NULL. Return `has_markup` rather than the geometry itself: the viewer draws markup through the rendered overlay, so shipping polygons to the browser would be a second unused representation.
- Rows are returned for skipped parcels too — TS-003 depends on opening one.
- Both routes reuse `_get_run` for tenancy.

**Definition of Done:**

- [ ] `GET /api/runs/{id}/parcels` returns rows ordered by descending score with skipped (NULL-score) rows last, and paging returns each row exactly once across pages.
- [ ] `GET /api/runs/{id}/parcels/{parcel_id}` returns the stored `score`, `candidate`, `skipped_reason` and `indicators` for that parcel.
- [ ] `has_markup` is true for a parcel with stored geometry and false for one without.
- [ ] Both routes return 404 for a run or parcel in another tenant, and 401 without a token.
- [ ] `EXPLAIN` of the default (no `candidate` filter) list query against a `run_parcels` table seeded to at least 50 000 rows for one run shows an index scan on the new index and **no `Sort` node**; the same `EXPLAIN` taken before the migration shows the sort, so the index is demonstrably what removed it.
- [ ] `uv run alembic upgrade head` then `downgrade -1` both succeed with migration `0004` applied over `0003`.
- [ ] Verify: `cd backend && uv run pytest tests/test_runs.py tests/test_migrations.py -q`

### Task 5: Parcel viewer page — three images, toggle, measurements, skip reasons

**Objective:** The view itself, at `/runs/:runId/parcels/:parcelId`: base and target imagery side by side with the marked-up target beside them, the score and measurements that decision rests on, a control to hide and show the markup, and a plain statement of why a parcel was skipped instead of an empty pane.

**Files:**

- Create: `frontend/src/pages/ParcelViewerPage.tsx`
- Modify: `frontend/src/api/runs.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/api/runs.test.ts`

**Key Decisions / Notes:**

- **All three image URLs are built from one shared `{size, buffer, outline}` object**, not three independently constructed query strings. Per `## Global Constraints` this is what actually holds the three panes in registration; three separate call sites drift the moment someone changes one. `outline: true` on all three — the PRD requires the parcel boundary on every pane, and `parcel_preview` defaults it to `False` (`backend/src/ptax/api/imagery.py:403`).
- Images need the bearer token, which an `<img src>` cannot carry. Fetch each as a blob and use an object URL, following `frontend/src/api/uploads.ts:45-49` exactly — including revoking the URL on unmount, which that helper's callers must already do.
- The markup toggle swaps the third pane between the overlay URL and the plain target-preview URL. Fetch both up front so toggling does not re-request; they are the same parcel at the same size.
- When the detail response reports `has_markup: false`, render two panes and say the run recorded no markup — do not render a third pane that silently equals the second (TS-004).
- When `skipped_reason` is set, show it in words and render whichever year's preview returns 204 versus an image; `parcel_preview` already returns `204 No Content` when a parcel has no imagery (`backend/src/ptax/api/imagery.py:428`), so a 204 is the signal for "this year has nothing", not an error.
- Add the route under the authenticated `<Layout />` block at `frontend/src/App.tsx:16-22`, beside `/runs`.

**Definition of Done:**

> **How the rendered-UI criteria below are checked.** The frontend has `vitest` only — no `@testing-library/react`, no jsdom, and no `.tsx` test anywhere in the repo (verified during planning). This plan does **not** add a component-test stack; that is a tooling decision the PRD did not ask for. The pane, toggle and skip-reason criteria are therefore verified by **executing TS-001 and TS-003 through browser automation** against the `## Runtime Environment` stack, which is what `spec-verify` Phase B already does for a Full-profile plan. `pnpm test` covers the URL-building and fetch logic only, and is not claimed to cover the rest.

- [ ] The three image URLs are built from one shared parameter object, and a unit test asserts the base, target and overlay URLs carry identical `size`, `buffer` and `outline` values — the frontend half of the registration guarantee (`frontend/src/api/runs.test.ts`).
- [ ] Score, candidate flag, `structure_m2`, `new_builtup_m2`, `veg_loss_m2` and the comparison resolution from `indicators` are all displayed — **TS-001 step 4**.
- [ ] Three panes render for a parcel with markup, two for a parcel without — **TS-001 step 3 and TS-004**.
- [ ] The toggle hides and restores the markup with no new image request after first load — **TS-001 steps 5-6**, confirmed against the browser's network panel.
- [ ] A skipped parcel shows its reason in words and still shows whichever year's imagery exists; a year returning 204 renders an explicit "no imagery for this year" pane rather than a broken image — **TS-003**.
- [ ] Verify: `cd backend && uv run pytest -q && uv run ruff check . && uv run mypy && cd ../frontend && pnpm build && pnpm lint && pnpm test`

### Task 6: Score-ordered parcel list on the run, and route wiring

**Objective:** Give the viewer a way in. The finished run on the Runs page gains a plain list of its parcels, highest score first, each linking to the viewer. This is the entry point only — no statuses, filters or actions, which are Plan C's.

**Files:**

- Modify: `frontend/src/pages/RunsPage.tsx`
- Modify: `frontend/src/api/runs.ts`
- Test: `frontend/src/api/runs.test.ts`

**Key Decisions / Notes:**

- List columns: `parcel_ref`, score, a candidate marker. Paged through Task 4's endpoint with a "load more" control rather than loading a county at once.
- Only shown for a run in a terminal state; a `queued` or `running` run has partial results and listing them invites acting on an unfinished run.
- Keep it inside `RunsPage.tsx` (267 lines) as a section on the selected run rather than a new route — the viewer is the new route, and a second list route would be the queue, which is Plan C's.

**Definition of Done:**

> Same verification split as Task 5: paging arithmetic is unit-tested; the rendered list and navigation are checked by executing TS-001 through browser automation.

- [ ] The paging helper requests each page exactly once and concatenates without duplicating or dropping a row across a page boundary, including a final short page (`frontend/src/api/runs.test.ts`).
- [ ] A finished run shows a parcel list ordered highest score first, and clicking a row navigates to that parcel's viewer — **TS-001 steps 1-2**.
- [ ] A `running` run shows no parcel list.
- [ ] Verify: `cd frontend && pnpm build && pnpm lint && pnpm test`

## Deviations

- Task 1/2 (tactical): `mask_to_multipolygon` rebuilt a `pyproj.Transformer` on every
  call, twice per parcel. Measured at **0.705 ms** against **0.2 ms** for the entire
  polygonisation it supported — 78% of the added cost, on a path that runs hundreds of
  thousands of times per run. Added an `lru_cache`d `_to_4326(crs_key)` in
  `backend/src/ptax/detection/geometry.py`, keyed on the CRS string because
  `rasterio.crs.CRS` is not reliably hashable across versions. Compare-only overhead fell
  from +60.5% to +35.6%; the DoD metric (per-parcel scoring time, which includes the COG
  reads that dominate) is **+4.7%**.

- Task 3 (tactical): removing `parcel_preview`'s inline extent block left `_expand` in
  `backend/src/ptax/api/imagery.py` with no callers — `plan_view` computes that bbox now —
  so it was deleted as code made unused by this change.
- Task 3 (tactical): `backend/tests/test_tiles.py` is listed in the task's `Files:` block
  but was **not modified**. Its existing `test_thumbnail_and_parcel_preview` already covers
  `parcel_preview`, and it passing untouched after the extraction is exactly the evidence
  wanted — editing it would have weakened that. The stronger byte-identity check (sha256 of
  three responses captured *before* the refactor) went into `backend/tests/test_run_overlay.py`
  instead, where it sits beside the registration tests it belongs with.
- Task 3 (tactical): the OpenAPI contract test reads `/api/openapi.json`, not
  `/openapi.json`; the app sets `openapi_url="/api/openapi.json"`
  (`backend/src/ptax/main.py:25`).
- Task 3 (tactical): `tests/test_run_overlay.py` decodes PNGs with `test_tiles._png_array`
  (rasterio) rather than Pillow — PIL is not a dependency of this project and adding one
  for a test decoder was not warranted.

- Task 5/6 (tactical): the plan's "paging helper" landed as `appendPage(current, page)` in
  `frontend/src/api/runs.ts`, not a walk-every-page helper. The list is incremental
  ("load more"), so a helper that fetches all pages would have been written, tested and
  never called. `appendPage` is what `RunsPage` actually uses, and it additionally drops a
  parcel already on screen — rows are score-ordered, so a run re-read mid-scroll can hand
  back an overlapping page and silently push a parcel off the end.

## Measurements

- **Task 2 scoring cost:** 69.19 ms → 72.46 ms per parcel over the 25-parcel fixture layer,
  **+4.7%** against the 20% ceiling (best of 3 runs each, `_record_detected_area` patched
  to a no-op for the baseline). The compare-only figure is +35.6%, but real scoring is
  dominated by reading two COGs, so that number overstates the share by roughly an order
  of magnitude.
- **Task 2 storage:** 3 of 25 rows non-NULL, **273 B per non-NULL row** against the 2 KB
  ceiling, 820 B total. Projected for a 200 000-parcel run at the same 12% non-NULL rate:
  **~6 MB**.
- **Task 3 registration guard, regression-tested by controlled mutation:** changing the
  overlay route's `size` default from 512 to 256 fails both
  `test_the_overlay_takes_the_same_size_and_buffer_contract_as_the_preview` and
  `test_all_three_images_cover_exactly_the_same_ground`; restoring it passes both. The
  guarantee is enforced, not merely asserted.
- **Task 4 index, EXPLAIN over a 50 000-row run** (seeded parcels + run_parcels, ANALYZEd):

  ```
  WITH run_parcels_score_idx      Limit  (cost=0.41..9.82 rows=50)
                                    -> Index Scan using run_parcels_score_idx
  WITHOUT it                      Limit  (cost=3536.11..3536.23 rows=50)
                                    -> Sort  (Sort Key: score DESC NULLS LAST, parcel_ref)
                                         -> Seq Scan on run_parcels
  ```

  First-page cost falls ~360x, and the sort returns the moment the index is dropped — so
  the index is demonstrably what removed it. This confirms the spec review's finding that
  `run_parcels_queue_idx` could not serve the unfiltered list.

## E2E Results

Executed 2026-09-22 against an isolated local stack — API on :8001 and Vite on :5174,
both carrying this change, proxying to the same compose PostGIS — so the dev servers
already running on :8000/:5173 were left untouched. Code identity confirmed before
testing: the running API served all three new routes, which the :8000 instance did not.

| Scenario | Priority | Result | Fix Attempts | Notes |
|----------|----------|--------|--------------|-------|
| TS-001 | Critical | PASS | 0 | Score-ordered list (0.429/0.429/0.423 then zeros, "25 of 25"), click → `/runs/:runId/parcels/:parcelId`, three panes, all measurements, toggle off and back on with **zero `/api/` requests** after first load |
| TS-002 | High | PASS | 0 | Scoring structure red over wider new built-up blue over yellow boundary — three mutually distinguishable colours in the correct draw order |
| TS-003 | High | PASS | 0 | Skip reason in words; score renders "—"; both the partial-coverage case (imagery shown where it exists) and the 204 case ("No imagery for this year") verified on separate parcels |
| TS-004 | Medium | PASS | 0 | Flagged parcel with NULL geometry renders two panes, no toggle, and states the run recorded no markup — no silently-identical third pane, no recomputed markup |

TS-002's two-colour separation and TS-004's NULL state were set up by writing the
`run_parcels` geometry directly, because the 25-parcel fixture's detections have
`structure_m2 == new_builtup_m2` and every run in the database was scored by this code.
Both are exactly the row shapes those scenarios describe.
