# Parcel Improvement Detection — Foundations and Reconciliation Implementation Plan

Created: 2026-09-24
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** PRD Phases 0 and 1 (`docs/prd/2026-09-24-parcel-improvement-detection.md`):
- an operator can store, validate and check a county's pipeline configuration;
- the pipeline's PostGIS tables exist, isolated per tenant by row-level security;
- the AWS stacks provide KMS-encrypted per-tenant storage and per-tenant ArcGIS secrets;
- CI runs on every push;
- a tested reconciliation engine turns Year A and Year B detections into classified changes and a parcel-level status with estimated new square footage.

## Out of Scope

- **PRD Phases 2–7:** ingest and preprocess, inference, publish and feedback, Step Functions orchestration, training, and enrichment. Each gets its own plan, in that order, after this one.
- **Row-level security on the existing app tables** (parcels, imagery, runs, users). The user chose RLS on the new `pipeline` schema first; the app tables keep their `tenant_id` filtering until a later plan.
- **Per-tenant IAM conditions on S3 (ABAC).** They need per-stage task roles, which arrive with orchestration (Phase 5). This plan provides the per-tenant prefix convention, which code enforces.
- **Writing reconciliation results to PostGIS.** Phase 1 is pure logic over synthetic inputs. The writer arrives with the end-to-end inference run (Phase 3).
- **Replacing the county profile files** (`backend/counties/*.json`) and the existing classical/inventory runs. They stay until the ingest plan (Phase 2) moves ingest onto tenant configuration.

## Approach

**Chosen:**
- A new `pipeline` PostgreSQL schema beside the app's tables. Every pipeline table carries `tenant_id` under RLS, enforced by switching to a non-login role, `ptax_tenant`, inside `tenant_scope()`.
- Tenant configuration as a pydantic model stored in `pipeline.tenant_configs` and managed through `ptax-admin`.
- A pure `ptax.reconcile` package.

**Why:** Keeping the PRD's table names in their own schema avoids colliding with the existing `runs`/`parcels` tables and leaves the running app untouched. The role switch makes RLS hold even for the owner and superuser connections the app and tests use. The cost is one more role and a scoped session helper that every pipeline code path must use.

## Global Constraints

- PostgreSQL schema: `pipeline`. Tenant role: `ptax_tenant`. Session setting: `app.tenant_id`.
- Improvement classes (default, in config): `primary_structure`, `garage_outbuilding`, `shed`, `pool`, `deck_patio`, `driveway_paved`, `solar_array`, `ag_building`.
- Areas are reported in square feet, computed in the tenant's projected CRS (`crs_epsg`).
- Geometries are stored in EPSG:4326, as in the existing `parcels` table.
- Per-tenant object keys start with `tenants/<tenant_id>/`.
- Per-tenant ArcGIS secret name: `ptax/tenants/<tenant_id>/arcgis`.
- County credentials never appear in logs, CLI output, exceptions or stored configuration.

## Context for Implementer

The app connects to Postgres as the owner of its tables: `ptax`, which is a superuser locally and the RDS master user on Aurora. Owners and superusers bypass RLS, so RLS on its own would silently not apply. `tenant_scope(session, tenant_id)` therefore runs `SET LOCAL ROLE ptax_tenant` and `set_config('app.tenant_id', …, true)` inside the current transaction, and explicitly resets both on exit (Task 1). Every `pipeline` table grants its privileges to `ptax_tenant`, and its policy reads `app.tenant_id`. Pipeline code that touches tenant data runs inside `tenant_scope`. Unscoped owner access is reserved for migrations and cross-tenant operator maintenance.

## Runtime Environment

- Local stack: `make dev-up` (PostGIS on 5432, MinIO on 9000, cognito-local). Test database `ptax_test` (see `backend/tests/conftest.py:40`).
- Entry points in this plan are `ptax-admin` commands. There are no HTTP routes and no UI.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| RLS passes its tests locally but a code path queries pipeline tables without `tenant_scope` and sees every tenant | Medium | High | Tests run as the superuser test connection and assert isolation only through `tenant_scope`. A test also asserts that the `ptax_tenant` role with no `app.tenant_id` set sees zero rows (Task 1). |
| A credential leaks through a validation error or CLI output | Medium | High | Secrets are held in Secrets Manager only; config stores the ARN. The loader returns `SecretStr` values. A test asserts that no secret value appears in any exception text or CLI output (Task 3). |
| CI cannot start MinIO as a GitHub Actions service (services cannot pass a command) | Medium | Medium | CI starts MinIO with `docker run` in a step and polls its health endpoint, with a bounded timeout, before the tests (Task 5). |

## Progress Tracking

- [x] Task 1: `pipeline` schema with row-level security and `tenant_scope`
- [x] Task 2: Tenant configuration model, storage and `ptax-admin tenant-config set/show`
- [x] Task 3: ArcGIS credentials from Secrets Manager and `ptax-admin tenant-config check`
- [x] Task 4: KMS-encrypted pipeline bucket, per-tenant prefixes and secret access in the CDK stacks
- [x] Task 5: CI workflow and a clean mypy baseline
- [x] Task 6: Reconciliation geometry: vectorise masks, keep structures by centroid, area in square feet
- [x] Task 7: Reconciliation matching, per-detection classification and CAMA suppression
- [x] Task 8: Parcel status, change-model agreement, new-square-footage estimate, and docs

## File Structure

- `backend/alembic/versions/0008_pipeline_schema.py` (create) — the `pipeline` schema, its tables, GiST indexes, the `ptax_tenant` role, grants, and RLS policies.
- `backend/src/ptax/pipeline/__init__.py` (create) — package marker.
- `backend/src/ptax/pipeline/models.py` (create) — SQLAlchemy models for the `pipeline` tables.
- `backend/src/ptax/db/tenancy.py` (create) — `tenant_scope(session, tenant_id)`.
- `backend/src/ptax/tenancy/__init__.py` (create) — package marker.
- `backend/src/ptax/tenancy/config.py` (create) — pydantic `TenantConfig` and its parts (`ImagerySource`, `ParcelSource`, `Thresholds`, …).
- `backend/src/ptax/tenancy/store.py` (create) — read and write `pipeline.tenant_configs` under `tenant_scope`.
- `backend/src/ptax/tenancy/secrets.py` (create) — `arcgis_credentials(config)` from Secrets Manager.
- `backend/src/ptax/tenancy/check.py` (create) — reachability checks for a tenant config.
- `backend/tenants/peoria-il.example.json` (create) — an example tenant config, with no secrets.
- `backend/src/ptax/reconcile/__init__.py` (create) — package marker.
- `backend/src/ptax/reconcile/types.py` (create) — `Detection`, `CamaRecord`, `ClassifiedDetection`, `ParcelResult`.
- `backend/src/ptax/reconcile/geometry.py` (create) — mask vectorisation, centroid rule, square-foot area.
- `backend/src/ptax/reconcile/matching.py` (create) — A↔B matching, classification, CAMA suppression.
- `backend/src/ptax/reconcile/parcel.py` (create) — `reconcile_parcel(...)`: parcel status and estimate.
- `backend/src/ptax/cli.py`, `backend/src/ptax/config.py`, `backend/src/ptax/storage.py` (modify).
- `infra/lib/data-stack.ts`, `infra/lib/compute-stack.ts`, `infra/test/stacks.test.ts`, `docker-compose.yml` (modify).
- `.github/workflows/ci.yml` (create).
- Tests under `backend/tests/` as listed per task.

## Deviations

- Task 1 (tactical): `pipeline.detections` also has `already_assessed boolean NOT NULL DEFAULT false` (`backend/alembic/versions/0008_pipeline_schema.py`), so the Phase 3 writer can store Task 7's CAMA suppression without another migration.
- Task 4 (tactical): the compute stack receives the pipeline bucket and key through its props, so `infra/lib/app.ts` also changes to pass them.
- Task 5 (tactical): PyYAML is not installed in the backend environment, so the verify command parses the workflow with `uv run --with pyyaml`. `actionlint` is not installed; the workflow was checked by parsing it and running each of its commands locally.
- Task 6 (tactical): the plan's expected area for a 100 × 100 US-survey-foot square (10000 sq ft) was wrong. A US survey foot is 0.3048006 m, so the square is 10000.04 international sq ft, and the DoD now says so. A centroid exactly on the parcel line counts as outside (`contains` is strict).
- Task 7 (tactical): `ClassifiedDetection` also carries `would_be`, the type an `uncertain` detection would have had, because Task 8's status rule needs "an uncertain detection that would otherwise be new or expanded". `geometry.py` (`backend/src/ptax/reconcile/geometry.py`) gains a public `feet_per_unit(crs_epsg)` for the match tolerance. The Year A/B lists are the first two parameters of `classify(...)`; `year_a` is the Year A calendar year for CAMA. `delta_sqft` is the full area for an unmatched detection and minus the Year A area for a removed one.
- Task 8 (tactical): the plan did not define `change_model_agrees` for a parcel with change polygons but nothing flagged. It is True when the change polygons cover less than `min_new_area_sqft` of the parcel (both signals see no change), and False otherwise. `ParcelResult` also carries `pin`.

## Implementation Tasks

### Task 1: `pipeline` schema with row-level security and `tenant_scope`

**Objective:** Add migration 0008, creating the `pipeline` schema with the PRD's data model and RLS on every table. Add SQLAlchemy models for it, and `tenant_scope()`, the one way tenant code reaches those tables. A tenant sees and writes only its own rows; the tenant role with no tenant set sees nothing.

**Files:**

- Create: `backend/alembic/versions/0008_pipeline_schema.py`
- Create: `backend/src/ptax/pipeline/__init__.py`
- Create: `backend/src/ptax/pipeline/models.py`
- Create: `backend/src/ptax/db/tenancy.py`
- Test: `backend/tests/test_pipeline_schema.py`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- Tables, each with `tenant_id uuid NOT NULL REFERENCES tenants(id)`:
  - `tenant_configs(tenant_id PK, config jsonb, version int, updated_at)`;
  - `runs(run_id uuid PK, year_a, year_b, model_version, status CHECK IN ('queued','running','succeeded','failed','cancelled'), started_at, finished_at, config_snapshot jsonb, created_at)`;
  - `parcels(run_id, pin, geom, attrs jsonb; PK(run_id, pin))`;
  - `detections(id uuid PK, run_id, pin, year, class, score, area_sqft, change_type NULL, geom)`;
  - `detection_matches(run_id, a_id, b_id, iou, area_delta_sqft)`;
  - `parcel_changes(run_id, pin, status CHECK IN ('high_confidence','needs_review','no_change'), new_sqft_est, classes_added text[], change_model_agrees bool NULL, note, geom; PK(run_id, pin))`;
  - `reviews(run_id, pin, review_status CHECK IN ('pending','accepted','rejected'), reviewer, comment, synced_at)`;
  - `tile_qc(run_id, tile_id, reg_shift_ft, flagged)`.
- Every `run_id` is a foreign key to `pipeline.runs`. Every `geom` has a GiST index, following `backend/alembic/versions/0002_imagery_and_runs.py:1`.
- RLS on every table: `ENABLE ROW LEVEL SECURITY`, and one policy `USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (same)`. `NULLIF` makes an unset or cleared setting match no rows instead of failing the `uuid` cast. Role `ptax_tenant` is `NOLOGIN`, granted to `current_user`, with `USAGE` on the schema and DML on its tables. `FORCE` is not set, so the owner (migrations, operator maintenance) is unaffected. See Context for Implementer.
- `tenant_scope` is a context manager. On entry it runs `SET LOCAL ROLE ptax_tenant` and `set_config('app.tenant_id', str(tenant_id), true)`. On exit it **explicitly** runs `RESET ROLE` and `set_config('app.tenant_id', '', true)`, in a `finally` block.
- The explicit reset is needed because `SET LOCAL` reverts only at a real COMMIT or ROLLBACK. The test fixture's `session.commit()` is a savepoint release (`backend/tests/conftest.py:77`), and a request may mix scoped and unscoped work in one transaction; relying on a commit would leak the tenant role.
- Nesting a different tenant's scope inside an open scope raises instead of silently switching.
- The downgrade drops the schema and the role. Pipeline data exists only in this schema, so that is the whole downgrade.

**Definition of Done:**

- [x] Within `tenant_scope(A)`, rows inserted for tenant A are visible; tenant B's rows are not. An insert with `tenant_id = B` inside `tenant_scope(A)` is rejected.
- [x] As `ptax_tenant` with no `app.tenant_id`, every pipeline table returns zero rows. The superuser test connection outside any scope still sees all rows, so migrations work.
- [x] Using the standard `db` fixture: enter `tenant_scope(A)`, write, exit, then `session.commit()` (a savepoint release). An unscoped query on the same session afterwards sees tenant B's rows again, and `current_user` is no longer `ptax_tenant`. The same holds when the block exits by exception. Nesting `tenant_scope(B)` inside `tenant_scope(A)` raises.
- [x] Migration 0008 upgrades and downgrades cleanly and leaves the existing tables untouched.
- [x] Verify: `cd backend && uv run pytest tests/test_pipeline_schema.py tests/test_migrations.py -q`

### Task 2: Tenant configuration model, storage and `ptax-admin tenant-config set/show`

**Objective:** Add the tenant configuration (PRD Core User Flows > Flow 1, and Scope > Phase 0 — Foundations) as a validated pydantic model with defaults, stored per tenant in `pipeline.tenant_configs` under `tenant_scope`. Operators set it from a JSON file and read it back with `ptax-admin`. Invalid configurations are refused with messages naming each bad field.

**Files:**

- Create: `backend/src/ptax/tenancy/__init__.py`
- Create: `backend/src/ptax/tenancy/config.py`
- Create: `backend/src/ptax/tenancy/store.py`
- Create: `backend/tenants/peoria-il.example.json`
- Modify: `backend/src/ptax/cli.py`
- Test: `backend/tests/test_tenant_config.py`

**Key Decisions / Notes:**

- `TenantConfig` fields:
  - `county_name`, `state`;
  - `portal_type` (`agol` or `enterprise`), `portal_url` (https), `secret_arn` (an ARN only; never secret values);
  - `imagery: dict[int, ImagerySource]`, where `ImagerySource` has `source` (`service` or `files`), `url_or_s3_uri` (https for services, `s3://` for files), `year`, `capture_date`, `gsd_ft`, `bands`;
  - `parcels: ParcelSource` (`service_url`, `pin_field`, `where_clause="1=1"`);
  - optional `footprints` (`service_url`) and `cama` (`s3_uri`, `pin_field`, `field_mapping`);
  - `crs_epsg`, which must be a projected CRS according to pyproj;
  - `classes` (default as in Global Constraints);
  - `thresholds: Thresholds`;
  - `publish` (`folder`, `detections_item`, `parcel_changes_item`, `publish_all=False`).
- `Thresholds` defaults are tunable per tenant and all must be positive, with fractions in (0, 1]:
  - `min_new_area_sqft=100`, `expansion_min_sqft=100`, `expansion_min_pct=0.10`;
  - `iou_match=0.3`, `match_tolerance_ft=3.0`;
  - `min_score=0.5`, `high_confidence_score=0.8`, `change_agreement_frac=0.5`;
  - `cama_area_tolerance_pct=0.25`;
  - `reg_shift_tolerance_ft=2.0`, `chip_buffer_ft=10.0`.
- Cross-field checks: every `imagery` key equals its source's `year`, and at least two years are configured.
- `store.put_config` and `store.get_config` run under `tenant_scope`. `put` increments `version`.
- CLI: `ptax-admin tenant-config set <file> --tenant-fips <fips>` (validate, store, print the version) and `ptax-admin tenant-config show --tenant-fips <fips>` (print JSON). Follow the command pattern at `backend/src/ptax/cli.py:153`. Validation errors exit 1 with one line per field.
- The example config is Peoria's services from `backend/counties/peoria-il.json`, with a placeholder secret ARN.

**Definition of Done:**

- [x] The example config validates. Removing the second imagery year, using a geographic `crs_epsg` (4326), or putting an `http://` portal URL each fails with a message naming the field.
- [x] `tenant-config set` then `show` round-trips the config, and a second `set` bumps `version` to 2. Tenant B cannot read tenant A's config, because `get_config` runs under B's scope.
- [x] Verify: `cd backend && uv run pytest tests/test_tenant_config.py -q`

### Task 3: ArcGIS credentials from Secrets Manager and `ptax-admin tenant-config check`

**Objective:** Load a tenant's ArcGIS OAuth app credentials from Secrets Manager, never exposing them. Add `ptax-admin tenant-config check`, which reports each configured endpoint as reachable or not, as PRD Flow 1 requires:
- the secret itself;
- an OAuth token from the portal;
- the parcel service;
- each imagery service;
- the optional footprint and CAMA sources.

**Files:**

- Create: `backend/src/ptax/tenancy/secrets.py`
- Create: `backend/src/ptax/tenancy/check.py`
- Modify: `backend/src/ptax/cli.py`
- Test: `backend/tests/test_tenant_check.py`

**Key Decisions / Notes:**

- The secret JSON is `{"client_id", "client_secret"}`. `arcgis_credentials(config)` returns them with `client_secret: SecretStr`. Errors name the secret ARN, never its value.
- Token check: POST `{portal_url}/sharing/rest/oauth2/token` with `grant_type=client_credentials`. For services, GET `<url>?f=json`. For S3 sources, a HEAD on the object or prefix. All use an injectable `httpx.Client` factory, as in `backend/src/ptax/county/parcels.py:29`.
- `check` returns one result per item (`ok`, `unreachable` or `denied`, with a short reason). The CLI prints one line per item and exits 1 if any required item fails. Optional items (footprints, CAMA) are reported but never fail it.
- Tests use moto's Secrets Manager mock and `httpx.MockTransport`.

**Definition of Done:**

- [x] With a mocked secret and mocked services all answering, `check` reports every item `ok` and exits 0. With the parcel service returning 500, it reports that item `unreachable` and exits 1. With a missing footprints service, it reports it but still exits 0.
- [x] No secret value appears in any CLI output or exception message, including when the portal rejects the token.
- [x] Verify: `cd backend && uv run pytest tests/test_tenant_check.py -q`

### Task 4: KMS-encrypted pipeline bucket, per-tenant prefixes and secret access in the CDK stacks

**Objective:** Add the pipeline's storage to the AWS stacks:
- a customer-managed KMS key with rotation;
- a KMS-encrypted pipeline bucket, private, SSL-only, versioned and retained;
- task-role access to that bucket and key, and to the per-tenant ArcGIS secrets;
- `PIPELINE_BUCKET` passed to both services.

The backend gets a `pipeline_bucket` setting and a helper that builds a tenant's object keys and refuses keys outside the tenant's prefix. The local MinIO gets the matching bucket.

**Files:**

- Modify: `infra/lib/data-stack.ts`
- Modify: `infra/lib/compute-stack.ts`
- Modify: `infra/lib/app.ts`
- Modify: `infra/test/stacks.test.ts`
- Modify: `backend/src/ptax/config.py`
- Modify: `backend/src/ptax/storage.py`
- Modify: `docker-compose.yml`
- Test: `backend/tests/test_storage.py`

**Key Decisions / Notes:**

- Follow the uploads bucket at `infra/lib/data-stack.ts:49`, but with `BucketEncryption.KMS` on the new key and no CORS, since the pipeline bucket takes no browser uploads.
- Grants: `readWrite` on the bucket, `encryptDecrypt` on the key, and `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:<region>:<account>:secret:ptax/tenants/*`.
- `pipeline_key(tenant_id, *parts)` returns `tenants/<tenant_id>/...`. `assert_tenant_key(tenant_id, key)` raises for any key outside that prefix, including one that uses `..`.
- Aurora is already `storageEncrypted: true` (`infra/lib/data-stack.ts:44`), so no database change.
- The compose `minio-init` service also creates `ptax-pipeline`. The local default for `pipeline_bucket` is `ptax-pipeline`.

**Definition of Done:**

- [x] The synthesised template has a KMS key with rotation enabled and a bucket encrypted with `aws:kms` on that key. The task role can read and write the bucket and decrypt with the key. Both containers get `PIPELINE_BUCKET`. Secret access is limited to `ptax/tenants/*`.
- [x] `pipeline_key` builds `tenants/<id>/raw/2015/x.tif`; `assert_tenant_key` rejects another tenant's prefix and `tenants/<id>/../<other>/x`.
- [x] Verify: `cd infra && npx jest && cd ../backend && uv run pytest tests/test_storage.py -q`

### Task 5: CI workflow and a clean mypy baseline

**Objective:** Add a GitHub Actions workflow that runs on every push and pull request:
- backend: `ruff check`, `mypy` and `pytest`, against PostGIS and MinIO;
- frontend: `tsc`, lint and tests;
- infra: jest.

Fix the six existing mypy errors so the type-check step can fail the build. Update the README so a reader knows CI exists and what it runs.

**Files:**

- Create: `.github/workflows/ci.yml`
- Modify: `backend/src/ptax/parcels/footprint.py`
- Modify: `backend/src/ptax/imagery/naip.py`
- Modify: `backend/src/ptax/detection/run.py`
- Modify: `backend/src/ptax/api/imagery.py`
- Modify: `backend/src/ptax/api/tiles.py`
- Modify: `README.md`

**Key Decisions / Notes:**

- Backend job:
  - `postgis/postgis:16-3.4` as a service, matching `docker-compose.yml:3`;
  - MinIO started with `docker run` plus bucket creation (`mc mb`), then a poll of `/minio/health/live` for up to 60 s;
  - `uv sync`, then `uv run ruff check src tests`, `uv run mypy src` and `uv run pytest -q`.
- Frontend job: `pnpm install --frozen-lockfile`, `pnpm exec tsc -b --noEmit`, `pnpm lint`, `pnpm test`. Infra job: install, then `npx jest`.
- `ruff format --check` is not run: the repo carries formatting drift that predates this work, and reformatting it would be unrelated churn.
- The mypy fixes are type narrowings only (a None check before `to_shape`, a 4-tuple cast for bounds, a typed dict default), with no behaviour change.
- `Trivial:` the mypy fixes change types only; the existing full backend suite (`uv run pytest -q`) covers their behaviour.

**Definition of Done:**

- [x] `uv run mypy src` reports no errors, and the full backend suite still passes.
- [x] The workflow file parses (`actionlint` if installed; otherwise a YAML parse). Every command it runs has been run locally and passes.
- [x] Verify: `cd backend && uv run ruff check src tests && uv run mypy src && uv run pytest -q && uv run --with pyyaml python -c "import yaml; yaml.safe_load(open('../.github/workflows/ci.yml'))"`

### Task 6: Reconciliation geometry: vectorise masks, keep structures by centroid, area in square feet

**Objective:** Add the geometric first steps of reconciliation (PRD Scope > Phase 1 — Reconciliation (no ML)):
- turn a per-year instance mask into polygon detections, each with its class and score, simplified;
- keep a structure on a parcel only when its centroid is inside the parcel, keeping its whole outline rather than clipping it;
- compute each detection's area in square feet in the tenant's projected CRS.

**Files:**

- Create: `backend/src/ptax/reconcile/__init__.py`
- Create: `backend/src/ptax/reconcile/types.py`
- Create: `backend/src/ptax/reconcile/geometry.py`
- Test: `backend/tests/test_reconcile_geometry.py`

**Key Decisions / Notes:**

- The inputs are an integer instance-label array, a per-instance `(class, score, occluded)` table, the raster transform and the CRS. Polygonise with `rasterio.features.shapes`, as in `backend/src/ptax/detection/geometry.py:1`, then simplify with a tolerance of half a pixel.
- `Detection` holds `id`, `year` (`"A"` or `"B"`), `cls`, `score`, `occluded`, `tile_flagged`, and `geom` in the tenant CRS.
- Area in square feet is `polygon.area × (metres_per_unit / 0.3048)²`. `metres_per_unit` comes from pyproj's first axis `unit_conversion_factor`, so metres, international feet and US survey feet all come out correct.
- A structure straddling the parcel line is kept only when its centroid is inside, which avoids double-counting across neighbouring parcels.

**Definition of Done:**

- [x] A synthetic label array with two instances yields two polygons with the given classes and scores. An instance whose centroid lies outside the parcel is dropped. A straddling instance whose centroid is inside is kept whole.
- [x] A 10 m × 10 m square has area 1076.39 sq ft (±0.01) in a metre CRS (EPSG:26916). A 100 US-ft × 100 US-ft square has area 10000.04 international sq ft (±0.01) in a US-foot State Plane CRS (EPSG:3435).
- [x] Verify: `cd backend && uv run pytest tests/test_reconcile_geometry.py -q`

### Task 7: Reconciliation matching, per-detection classification and CAMA suppression

**Objective:** Match each parcel's Year A and Year B detections and classify them as PRD Scope > Phase 1 — Reconciliation (no ML) describes. Every Year B detection gets exactly one change type (`new`, `expanded`, `unchanged` or `uncertain`), and every unmatched Year A detection is `removed`. When CAMA records are supplied, `new` and `expanded` detections the county already assessed are marked `already_assessed`.

**Files:**

- Create: `backend/src/ptax/reconcile/matching.py`
- Modify: `backend/src/ptax/reconcile/types.py`
- Test: `backend/tests/test_reconcile_matching.py`

**Key Decisions / Notes:**

- **Matching:** IoU of the two polygons, each buffered by `match_tolerance_ft` in the tenant CRS, to absorb misregistration and building lean. Matching is greedy one-to-one in descending IoU, among pairs with IoU ≥ `iou_match`. It ignores class, so a shed that grew into a garage still matches.
- **Classification order for a Year B detection:**
  1. `uncertain` if `score < min_score`, `occluded`, or `tile_flagged`;
  2. otherwise `new` if unmatched and `area ≥ min_new_area_sqft`; an unmatched detection below that area is `unchanged` (too small to report);
  3. otherwise `expanded` if `delta ≥ expansion_min_sqft` and `delta / area_a ≥ expansion_min_pct`;
  4. otherwise `unchanged`.
- **CAMA:** a `CamaRecord` holds `pin`, `cls`, `area_sqft` and `year`, already mapped by the tenant's `field_mapping`. A `new` or `expanded` detection becomes `already_assessed` when an unused CAMA record for that PIN has the same class, `year ≥ year_a`, and area within `cama_area_tolerance_pct` of the detection's new area (the full area for `new`, the delta for `expanded`). Each record suppresses at most one detection.
- `ClassifiedDetection` holds `detection`, `change_type`, `area_sqft`, `delta_sqft`, `matched_a_id`, `iou` and `already_assessed`.

**Definition of Done:**

- [x] Synthetic parcels cover each outcome with exact expected values:
  - an identical building is `unchanged`;
  - a building shifted 2 ft within tolerance is still matched;
  - a new 400 sq ft garage is `new`;
  - a new 50 sq ft object is not reported;
  - a house grown from 1,500 to 1,800 sq ft is `expanded` with delta 300;
  - a house grown 1,500 to 1,550 sq ft is `unchanged` (below both expansion thresholds);
  - a demolished shed is `removed`;
  - a low-score detection is `uncertain`, and so is a detection in a flagged tile.
- [x] A new 400 sq ft garage with a CAMA garage of 390 sq ft assessed after Year A is `already_assessed`. The same record with class `pool`, or with area 200, does not suppress it. One CAMA record never suppresses two detections.
- [x] Verify: `cd backend && uv run pytest tests/test_reconcile_matching.py -q`

### Task 8: Parcel status, change-model agreement, new-square-footage estimate, and docs

**Objective:** Combine one parcel's classified detections and optional change-model polygons into its parcel-level result (PRD Scope > Phase 1 — Reconciliation (no ML)), and document the reconciliation rules. The result carries:
- a status (`high_confidence`, `needs_review` or `no_change`);
- `new_sqft_est` and `classes_added`;
- `change_model_agrees`;
- the classified detections and matches.

Reconciliation works with or without change masks.

**Files:**

- Create: `backend/src/ptax/reconcile/parcel.py`
- Modify: `backend/src/ptax/reconcile/types.py`
- Modify: `README.md`
- Test: `backend/tests/test_reconcile_parcel.py`

**Key Decisions / Notes:**

- **Flagged detections** are `new` or `expanded`, not `already_assessed`, and not `uncertain`.
- **Change agreement:** the change-mask polygons cover at least `change_agreement_frac` of a flagged detection's area (its new outline for `new`; for `expanded`, the Year B area minus the matched Year A outline). With `change_polygons=None` (no change model), `change_model_agrees` is None.
- **Status rules:**
  - `high_confidence` when at least one detection is flagged, change polygons are supplied and agree for every flagged detection, and every flagged score is at least `high_confidence_score`;
  - otherwise `needs_review` when any detection is flagged, when the change polygons alone cover at least `min_new_area_sqft` inside the parcel, or when any `uncertain` detection would otherwise have been `new` or `expanded`;
  - otherwise `no_change`.
- Without a change model a flagged parcel is always `needs_review`, as the PRD says: high confidence requires both signals.
- `new_sqft_est` is the sum of flagged `new` areas plus flagged `expanded` deltas. `classes_added` is the sorted set of flagged classes.
- README: a short "Parcel improvement detection" section covering tenant config and its CLI, the `pipeline` schema and RLS, and these reconciliation rules. It links to the PRD for later phases.

**Definition of Done:**

- [x] Each status branch is reached by a synthetic parcel:
  - both signals agree with high scores → `high_confidence`;
  - both agree but one score is low → `needs_review`;
  - segmentation only, no change model → `needs_review` with `change_model_agrees` None;
  - change polygons only → `needs_review`;
  - only an `uncertain` would-be-new detection → `needs_review`;
  - only `already_assessed` → `no_change`;
  - nothing → `no_change`.
- [x] A parcel with a new 400 sq ft garage and a house expanded by 300 sq ft has `new_sqft_est` 700 and `classes_added` ["garage_outbuilding", "primary_structure"].
- [x] Verify: `cd backend && uv run pytest tests/test_reconcile_parcel.py tests/test_reconcile_matching.py tests/test_reconcile_geometry.py -q`
