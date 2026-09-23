# Parcel Structure Change Detection — Plan A: Platform Foundation

Created: 2026-09-21
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: VERIFIED
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** A county admin can sign in to their own tenant, upload the county's parcel layer, pick the parcel-ID field, and see it ingested into PostGIS; an admin can invite reviewers; the whole stack runs locally via docker-compose and deploys to AWS (ECS Fargate + ALB + Aurora Serverless v2 + Cognito + S3) from CDK.

This is the first of three linked plans implementing `docs/prd/2026-09-21-parcel-structure-change-detection.md`:

- **Plan A (this file):** scaffold, local dev stack, schema, Cognito auth + tenancy, operator provisioning, users API, job runner, presigned uploads, parcel ingestion, frontend shell + parcels + users pages, CDK.
- **Plan B:** `docs/plans/2026-09-21-imagery-and-detection.md` — NAIP discovery/ingest, imagery upload, tile serving, comparison runs, classical detector.
- **Plan C:** `docs/plans/2026-09-21-review-workflow.md` — review queue, parcel viewer, map, notes/audit trail, CSV export.
- **Plan D:** `docs/plans/2026-09-22-detector-accuracy.md` — detector accuracy on real imagery, added after Plan B's AWS verification measured a 92% flag rate on a real cross-resolution year pair.

## Out of Scope

- Operator web UI — tenants and first admins are provisioned with the `ptax-admin` CLI (PRD: operator-provisioned, no self-serve). A UI for the operator role is not built.
- Imagery of any kind, comparison runs, review decisions, map, export — Plans B and C.
- Custom domain / TLS — the ALB listens on HTTP:80 by default; an ACM certificate ARN can be supplied as CDK context to add an HTTPS listener, but obtaining a domain and certificate is the user's action and not verified here.
- Password reset / forgot-password UI — Cognito's hosted flows are not wired; the operator resets via `ptax-admin set-password`. Deferred to a later plan.
- Deleting tenants or users — not needed for v1 flows; disable via Cognito console if required.

## Approach

**Chosen:** Single monorepo (`backend/` Python FastAPI + worker + CLI, `frontend/` React/Vite/TS, `infra/` CDK TS) with a DB-backed job queue and presigned-URL uploads to S3/MinIO; Cognito user pool for identity with `cognito-local` as the offline emulator.
**Why:** One Postgres-only job queue and a single backend image (API and worker are the same image with different commands) keep local dev to three containers and AWS to two Fargate services, at the cost of a queue that scales with the DB rather than SQS — acceptable for per-county batch work. Presigned uploads are established now so Plan B's multi-GB imagery uploads reuse the same path.

## Global Constraints

- Python `3.13` pinned in `backend/.python-version` (uv-managed); Node `26`, `pnpm` for `frontend/` and `infra/`.
- Database: PostgreSQL `16` + PostGIS `3.4` (`postgis/postgis:16-3.4` locally; Aurora PostgreSQL 16 on AWS). All geometry stored as `geometry(MultiPolygon, 4326)`.
- API routes are prefixed `/api`; the backend serves the built SPA at `/` with an SPA fallback for non-`/api` paths.
- Auth: Cognito `USER_PASSWORD_AUTH` flow only (the only flow `cognito-local` supports); the backend validates the Cognito **access token** (`token_use == "access"`, `client_id == COGNITO_CLIENT_ID`, issuer `COGNITO_ISSUER`, JWKS at `{COGNITO_ISSUER}/.well-known/jwks.json`).
- Roles are exactly `admin` | `reviewer` (Postgres enum `user_role`).
- `parcel_layers.status` values: `uploaded` | `inspecting` | `awaiting_field` | `ingesting` | `ready` | `failed`.
- `jobs.status` values: `queued` | `running` | `succeeded` | `failed`; job type strings: `parcel_layer.inspect`, `parcel_layer.ingest`.
- Backend env vars: `DATABASE_URL`, `S3_BUCKET`, `S3_ENDPOINT_URL` (unset in AWS), `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_ENDPOINT_URL` (unset in AWS), `COGNITO_ISSUER`.
- Frontend runtime settings come from `GET /api/config` (no build-time `VITE_*` variables; see Deviations, Task 11).
- Local ports: Postgres `5432`, MinIO `9000` (console `9001`), cognito-local `9229`, API `8000`, Vite `5173` (proxies `/api` → `8000`).

## Context for Implementer

Tenant isolation is enforced in one place: `get_current_user` (Task 3) resolves the Cognito `sub` to a `users` row and returns a `CurrentUser` carrying `tenant_id` and `role`. Every query in every router and job handler filters by that `tenant_id`; there is no cross-tenant read path and no "operator" web identity. The operator acts only through `ptax-admin`, which talks to the DB and Cognito directly and never through the API. Plans B and C inherit this: run, imagery, and review tables all carry `tenant_id` and go through the same dependency.

Parcel identity across re-uploads is the county's `parcel_ref`, never `parcels.id`: each layer upload creates fresh `parcels` rows, so anything in Plans B or C that must survive a re-ingest (review history, run results shown against a newer layer) joins on `(tenant_id, parcel_ref)`. The county footprint Plan B needs for NAIP discovery and coverage checks is derived, not stored — `ST_Union(geom)` over the current layer's parcels — so no tenant-level geometry column is added here.

`cognito-local` differs from real Cognito in ways the code must tolerate: only `USER_PASSWORD_AUTH`; its issuer is `http://localhost:9229/{poolId}` (so `COGNITO_ISSUER` is configuration, never derived from region); `AdminCreateUser` is partial and confirmation codes are printed to the container log instead of emailed. Anything that works locally must also work against real Cognito with only env changes.

## Runtime Environment

- **Start:** `make dev-up` (docker-compose: `db`, `minio`, `minio-init`, `cognito-local`), then `make seed` (creates local user pool + client, writes `backend/.env`, creates tenant `Demo County` with admin `admin@demo.test` / `Password1!` and reviewer `reviewer@demo.test` / `Password1!`, plus a second tenant `Other County` with `admin@other.test` / `Password1!`), then `make api` (uvicorn on 8000) and `make worker`, and `make web` (Vite on 5173).
- **Health:** `GET http://localhost:8000/api/health` → `{"status":"ok","db":"ok"}`.
- **Restart:** `make dev-down && make dev-up` resets Postgres, MinIO, and cognito-local volumes; re-run `make seed`.

## File Structure

- `README.md` (create) — repo overview, local dev quickstart, deploy steps.
- `Makefile` (create) — `dev-up`, `dev-down`, `seed`, `api`, `worker`, `web`, `test`, `test-backend`, `test-frontend`, `test-infra`.
- `docker-compose.yml` (create) — `db` (postgis), `minio`, `minio-init` (creates bucket + CORS), `cognito-local`.
- `.gitignore` (create) — Python, Node, CDK outputs, `.env*`, `.cognito/`.
- `backend/pyproject.toml` (create) — uv project; deps: fastapi, uvicorn, sqlalchemy 2, geoalchemy2, alembic, psycopg[binary], pydantic-settings, boto3, PyJWT[crypto], httpx, geopandas, pyogrio, shapely, typer; dev: pytest, pytest-asyncio, moto, ruff, mypy.
- `backend/.python-version` (create) — `3.13`.
- `backend/Dockerfile` (create) — multi-stage: build frontend, install backend, copy `frontend/dist` into image; entrypoint runs `alembic upgrade head` then the command.
- `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/0001_initial.py` (create) — migrations.
- `backend/src/ptax/__init__.py` (create).
- `backend/src/ptax/config.py` (create) — `Settings` (pydantic-settings) for all env vars in Global Constraints.
- `backend/src/ptax/main.py` (create) — FastAPI app factory `create_app()`, router mounting under `/api`, SPA static serving.
- `backend/src/ptax/db/session.py` (create) — engine, `SessionLocal`, `get_db` dependency.
- `backend/src/ptax/db/models.py` (create) — `Tenant`, `User`, `ParcelLayer`, `Parcel`, `Job` ORM models.
- `backend/src/ptax/auth/jwt.py` (create) — JWKS fetch/cache and access-token verification.
- `backend/src/ptax/auth/deps.py` (create) — `CurrentUser`, `get_current_user`, `require_role`.
- `backend/src/ptax/auth/cognito.py` (create) — thin boto3 `cognito-idp` client wrapper (`admin_create_user`, `admin_set_user_password`) honouring `COGNITO_ENDPOINT_URL`.
- `backend/src/ptax/api/health.py` (create) — `/api/health`.
- `backend/src/ptax/api/me.py` (create) — `/api/me` (current user + tenant).
- `backend/src/ptax/api/users.py` (create) — `/api/users` list/invite (admin).
- `backend/src/ptax/api/uploads.py` (create) — `/api/uploads` presigned PUT issuance.
- `backend/src/ptax/api/parcel_layers.py` (create) — `/api/parcel-layers` create/list/get/ingest.
- `backend/src/ptax/api/parcels.py` (create) — `/api/parcels` bbox query + lookup by parcel ref.
- `backend/src/ptax/storage.py` (create) — S3 client factory (`S3_ENDPOINT_URL` aware), key helpers, presign.
- `backend/src/ptax/jobs/queue.py` (create) — `enqueue(db, type, tenant_id, payload)`, `claim_next(db)`, `mark_succeeded/failed`.
- `backend/src/ptax/jobs/registry.py` (create) — `@job_handler("type")` registry.
- `backend/src/ptax/worker.py` (create) — `python -m ptax.worker` poll loop with graceful shutdown.
- `backend/src/ptax/parcels/ingest.py` (create) — inspect and ingest job handlers (pyogrio/geopandas → PostGIS).
- `backend/src/ptax/cli.py` (create) — `ptax-admin` Typer app: `create-tenant`, `create-user`, `set-password`, `bootstrap-local-cognito`.
- `backend/tests/conftest.py` (create) — DB fixture (transaction-per-test against compose Postgres), RSA test keypair + JWKS mock, token factory, MinIO fixture.
- `backend/tests/fixtures/parcels_small.geojson`, `backend/tests/fixtures/parcels_small_26915.zip` (create) — 25 synthetic parcels (GeoJSON in 4326; zipped shapefile in EPSG:26915), generated by `backend/tests/fixtures/make_fixtures.py`.
- `backend/tests/test_migrations.py`, `test_auth.py`, `test_users_api.py`, `test_jobs.py`, `test_uploads.py`, `test_parcel_ingest.py`, `test_cli.py` (create).
- `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/index.html` (create) — Vite React TS app; dev proxy `/api` → `http://localhost:8000`.
- `frontend/src/main.tsx`, `frontend/src/App.tsx` (create) — router, providers, protected layout.
- `frontend/src/auth/cognito.ts` (create) — `signIn`, `respondNewPassword`, `refresh`, `signOut` over `@aws-sdk/client-cognito-identity-provider` (endpoint override for local).
- `frontend/src/auth/AuthContext.tsx` (create) — session state, token persistence (sessionStorage), `useAuth`.
- `frontend/src/api/client.ts` (create) — fetch wrapper adding `Authorization: Bearer`, 401 → sign-out.
- `frontend/src/pages/LoginPage.tsx`, `ParcelsPage.tsx`, `UsersPage.tsx` (create).
- `frontend/src/components/Layout.tsx`, `UploadDropzone.tsx`, `LayerStatus.tsx` (create).
- `frontend/src/auth/cognito.test.ts` (create) — vitest for challenge handling and token refresh.
- `infra/package.json`, `infra/cdk.json`, `infra/tsconfig.json`, `infra/bin/ptax.ts` (create) — CDK app.
- `infra/lib/network-stack.ts`, `infra/lib/data-stack.ts`, `infra/lib/auth-stack.ts`, `infra/lib/compute-stack.ts` (create).
- `infra/test/stacks.test.ts` (create) — `Template.fromStack` assertions.

## Assumptions

- `cognito-local` supports the `NEW_PASSWORD_REQUIRED` challenge in `RespondToAuthChallenge` (listed as partial support). — Tasks 4, 8, 10 depend on this. **Confirmed during Task 10:** the invited-user first-login flow (temporary password → challenge → permanent password) completed against the emulator in the browser.
- Prebuilt wheels for `pyogrio`, `shapely`, and `geopandas` exist for CPython 3.13 on macOS arm64 and linux/amd64 (the Docker base). — Tasks 1, 7 depend on this.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| No AWS credentials on this machine, so `cdk deploy` cannot be exercised during implementation | Certain | Medium | Task 11 proves the CDK app with `cdk synth` + assertion tests and a local `docker build`/run of the production image; Task 12 is user-owned deploy with exact commands. |
| Emulator/real-Cognito divergence (issuer, admin API partials) breaks in AWS what passed locally | Medium | High | Issuer and endpoints are pure config (Global Constraints); Task 3 tests validate tokens against a self-generated JWKS, independent of either service; Task 12 DoD includes a real login against the deployed stack. |
| Alembic at container start races when the API and worker tasks (or multiple API tasks) start together on a deploy | Medium | Medium | Task 11: only the API task sets `PTAX_RUN_MIGRATIONS=1`, so the worker never migrates; the migration step holds `pg_advisory_lock` so any accidental concurrency serializes; `desiredCount: 1` for the API service with a `SHORTCUT:` naming the upgrade trigger (one-off migration task before scaling out). |
| Operator provisioning against the deployed stack — Aurora is private to the VPC, so a laptop `ptax-admin` cannot connect | Certain | High | Task 11 documents and Task 12 uses `aws ecs run-task` with a command override on the worker task definition to run `ptax-admin` inside the VPC with the task's own DB secret and Cognito permissions; no bastion or public DB. |
| Large parcel layers (100k+ features) make ingest slow or memory-heavy | Medium | Medium | Task 7 ingests in chunks of 5,000 features via `COPY`-style bulk insert and is verified with a generated 50k-feature layer under 60 s locally. |

## E2E Test Scenarios

> Driver notes: the app's port is whatever Vite prints (`make web`; 5173 is often taken by another project on this machine). Inputs are React-controlled — type with keyboard events (click the field, then type); setting a value programmatically does not update React state (for the `<select>`, focus it and use arrow keys + Enter). Element refs go stale after the 2 s poll re-renders the page — click by coordinates or re-find right before clicking.

### TS-001: Admin signs in and sees tenant
**Priority:** Critical
**Preconditions:** `make seed` has run; API, worker, and web are up.
**Mapped Tasks:** Task 3, Task 4, Task 8

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `http://localhost:5173/` | Redirected to `/login` |
| 2 | Fill email `admin@demo.test`, password `Password1!`, click **Sign in** | Redirected to `/parcels`; header shows `Demo County` and `admin@demo.test` |
| 3 | Click **Sign out** | Redirected to `/login`; navigating to `/parcels` redirects back to `/login` |

### TS-002: Admin uploads a parcel layer and ingests it
**Priority:** Critical
**Preconditions:** Signed in as `admin@demo.test`; no parcel layer yet.
**Mapped Tasks:** Task 6, Task 7, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `/parcels` | Empty state: "No parcel layer yet" with an upload dropzone |
| 2 | Upload `backend/tests/fixtures/parcels_small_26915.zip` | Progress bar completes; status chip cycles `uploaded` → `inspecting` → `awaiting_field` within 10 s |
| 3 | In the field picker, select `PIN`, click **Ingest** | Status `ingesting` then `ready`; summary shows `25 parcels`, `0 skipped`, CRS `EPSG:26915 → 4326` |
| 4 | Reload the page | Same summary persists; layer appears in **Layer history** as current |

### TS-003: Invalid parcel file is rejected with a reason
**Priority:** High
**Preconditions:** Signed in as `admin@demo.test`.
**Mapped Tasks:** Task 7, Task 9

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | On `/parcels`, upload a `.zip` containing only a text file | Status becomes `failed`; message contains "no shapefile or GeoJSON found" |
| 2 | Upload `parcels_small.geojson` | Reaches `awaiting_field`; the previous failed layer remains listed in history as `failed` |

### TS-004: Admin invites a reviewer; reviewer has restricted navigation
**Priority:** High
**Preconditions:** Signed in as `admin@demo.test`.
**Mapped Tasks:** Task 5, Task 8, Task 10

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to `/users` | Table lists `admin@demo.test (admin)` and `reviewer@demo.test (reviewer)` |
| 2 | Fill email `new.reviewer@demo.test`, role `reviewer`, click **Invite** | Row `new.reviewer@demo.test (reviewer)` appears; a toast confirms the invitation |
| 3 | Sign out; sign in as `reviewer@demo.test` / `Password1!` | Lands on `/parcels`; no **Users** link in the nav; navigating to `/users` shows "Admins only" |
| 4 | Sign out; read the temporary password from `docker compose logs cognito-local` (the "Confirmation Code Delivery" box for `new.reviewer@demo.test`); sign in with it | "Set a new password" form appears |
| 5 | Enter `Password1!` and submit | Lands on `/parcels` with header `Demo County`, email `new.reviewer@demo.test`, role `reviewer`. (Alternative when the log is unavailable: `ptax-admin set-password new.reviewer@demo.test --permanent Password1!` then sign in normally.) |

### TS-005: Tenants are isolated
**Priority:** Critical
**Preconditions:** TS-002 completed for `Demo County`.
**Mapped Tasks:** Task 3, Task 5, Task 7

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Sign in as `admin@other.test` / `Password1!` | Header shows `Other County` |
| 2 | Navigate to `/parcels` | Empty state — the Demo County layer is not visible |
| 3 | Navigate to `/users` | Only `admin@other.test` is listed |

## Progress Tracking

- [x] Task 1: Monorepo scaffold, local dev stack, health endpoint
- [x] Task 2: Database schema and migrations
- [x] Task 3: Cognito token verification, tenant context, operator CLI
- [x] Task 4: Local Cognito bootstrap and dev seed
- [x] Task 5: Users API (list, invite) with role enforcement
- [x] Task 6: Job runner and presigned uploads
- [x] Task 7: Parcel layer inspect/ingest pipeline and parcels API
- [x] Task 8: Frontend shell with Cognito sign-in
- [x] Task 9: Frontend parcels page
- [x] Task 10: Frontend users page
- [x] Task 11: Production image and CDK stacks
- [x] Task 12: Deploy to AWS (user)

## Implementation Tasks

### Task 1: Monorepo scaffold, local dev stack, health endpoint

**Objective:** Create the three-project monorepo, the docker-compose dev stack (PostGIS, MinIO with an initialised bucket, cognito-local), the uv-managed backend with a FastAPI app factory and `/api/health`, the Vite React TS frontend skeleton, and the Makefile that ties them together. This is the substrate every later task builds on; after it, `make dev-up && make api` yields a healthy API against a real PostGIS.

**Files:**

- Create: `README.md`
- Create: `Makefile`
- Create: `docker-compose.yml`
- Create: `.gitignore`
- Create: `backend/pyproject.toml`
- Create: `backend/.python-version`
- Create: `backend/src/ptax/__init__.py`
- Create: `backend/src/ptax/config.py`
- Create: `backend/src/ptax/main.py`
- Create: `backend/src/ptax/db/session.py`
- Create: `backend/src/ptax/api/health.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/eslint.config.js`
- Create: `frontend/pnpm-workspace.yaml`

**Key Decisions / Notes:**

- `Settings` in `config.py` reads every env var named in Global Constraints with local defaults matching the compose ports, so `make api` works with no `.env` until Task 4 writes one.
- `minio-init` is a one-shot `minio/mc` container that creates `S3_BUCKET` and applies a CORS rule allowing `PUT` from `http://localhost:5173` (needed by Task 6's browser-direct uploads).
- `/api/health` runs `SELECT 1` and reports `db: "ok" | "error"`; the ALB health check in Task 11 targets it.
- Frontend at this task is a placeholder page; the dev proxy for `/api` is configured now so later tasks need no Vite changes.
- `conftest.py` provides a `db` fixture that opens a connection to `DATABASE_URL`, begins a transaction, yields a session bound to it, and rolls back — tests run against the compose Postgres, not SQLite (PostGIS is required).

**Definition of Done:**

- [ ] `make dev-up` starts `db`, `minio`, `cognito-local`; `minio-init` exits 0 having created the bucket.
- [ ] `make api` serves `GET /api/health` → `200 {"status":"ok","db":"ok"}`; with the DB stopped it returns `503` with `db: "error"`.
- [ ] `pnpm --dir frontend dev` serves the placeholder page and proxies `/api/health` to the backend.
- [ ] Verify: `cd backend && uv run pytest tests/test_health.py -q && uv run ruff check . && cd ../frontend && pnpm build`

### Task 2: Database schema and migrations

**Objective:** Define the ORM models and the initial Alembic migration for tenants, users, parcel layers, parcels, and jobs, including the PostGIS extension and a spatial index on parcel geometry. Everything later persists through these tables.

**Files:**

- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_initial.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/src/ptax/db/models.py`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_migrations.py`

**Key Decisions / Notes:**

- Tables and columns (UUID PKs, `created_at timestamptz` everywhere):
  - `tenants(id, name, state char(2), fips char(5) unique, current_parcel_layer_id nullable FK)`
  - `users(id, tenant_id FK, cognito_sub unique, email unique, role user_role, created_at)`
  - `parcel_layers(id, tenant_id FK, uploaded_by FK users, s3_key, original_filename, status, parcel_id_field nullable, fields jsonb nullable, source_crs text nullable, feature_count int nullable, skipped_count int nullable, error text nullable, created_at)`
  - `parcels(id, tenant_id FK, layer_id FK, parcel_ref text, geom geometry(MultiPolygon,4326), attributes jsonb)` with `UNIQUE(layer_id, parcel_ref)` and a GiST index on `geom`.
  - `jobs(id, tenant_id FK nullable, type text, payload jsonb, status, attempts int default 0, error text nullable, created_at, started_at, finished_at)` with an index on `(status, created_at)`.
- Migration 0001 begins with `CREATE EXTENSION IF NOT EXISTS postgis` — Aurora's master user may run this, so no manual step is needed in AWS.
- `tenants` ↔ `parcel_layers` reference each other: create `tenants` without the `current_parcel_layer_id` constraint, create `parcel_layers`, then `ALTER TABLE tenants ADD CONSTRAINT tenants_current_parcel_layer_fk FOREIGN KEY (current_parcel_layer_id) REFERENCES parcel_layers(id)`; downgrade drops the constraint first.
- `env.py` reads `DATABASE_URL` from `Settings` (Task 1), not from `alembic.ini`.
- Plans B and C add their own tables in later migrations; they do not alter these.

**Definition of Done:**

- [ ] `alembic upgrade head` on a fresh compose DB creates all five tables, the `user_role` enum, and the `parcels_geom_idx` GiST index; `alembic downgrade base` removes them.
- [ ] Inserting a parcel with a `Polygon` geometry fails the column type check (MultiPolygon required), proving the storage contract in Global Constraints.
- [ ] Verify: `cd backend && uv run pytest tests/test_migrations.py -q`

### Task 3: Cognito token verification, tenant context, operator CLI

**Objective:** Implement access-token verification against the configured JWKS, the `get_current_user` / `require_role` dependencies that establish tenant context, `/api/me`, and the `ptax-admin` CLI the operator uses to create tenants and users in both Postgres and Cognito. After this task the API can authenticate a real Cognito user and every request knows its tenant.

**Files:**

- Create: `backend/src/ptax/auth/jwt.py`
- Create: `backend/src/ptax/auth/deps.py`
- Create: `backend/src/ptax/auth/cognito.py`
- Create: `backend/src/ptax/api/me.py`
- Create: `backend/src/ptax/cli.py`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_auth.py`
- Test: `backend/tests/test_cli.py`

**Key Decisions / Notes:**

- `jwt.py` fetches `{COGNITO_ISSUER}/.well-known/jwks.json` with httpx, caches keys by `kid` for 1 h, and verifies RS256 with PyJWT; a `kid` miss triggers one refetch. Rejects tokens where `token_use != "access"`, `client_id != COGNITO_CLIENT_ID`, or `iss != COGNITO_ISSUER`.
- `get_current_user` maps `sub` → `users.cognito_sub`; an unknown `sub` is `403 user not provisioned` (a valid Cognito identity with no tenant row must not get in). `CurrentUser` is a frozen dataclass `(id, tenant_id, email, role)`.
- `require_role("admin")` returns a dependency that raises `403` otherwise.
- `cognito.py` wraps boto3 `cognito-idp` with `endpoint_url=COGNITO_ENDPOINT_URL` when set; exposes `admin_create_user(email, temporary_password=None, suppress_message=False) -> sub` and `admin_set_user_password(email, password, permanent)`.
- `ptax-admin create-tenant --name --state --fips`, `create-user --tenant-fips --email --role [--password --permanent]` (creates the Cognito user via `AdminCreateUser`, then the `users` row with the returned `sub`), `set-password --email --permanent <pw>`. Registered as a `[project.scripts]` entry point.
- Tests mint tokens with a test RSA keypair and serve its JWKS via an httpx `MockTransport`; Cognito admin calls are stubbed with `moto`'s `cognitoidp` mock.

**Definition of Done:**

- [ ] `GET /api/me` with a valid access token returns `{id, email, role, tenant: {id, name, state, fips}}`; expired token → `401`; wrong `client_id` → `401`; valid token with no `users` row → `403`.
- [ ] `ptax-admin create-tenant` then `create-user` produce a `tenants` row, a Cognito user, and a `users` row whose `cognito_sub` matches the Cognito user's `sub`; a duplicate `fips` exits non-zero with a clear message.
- [ ] Verify: `cd backend && uv run pytest tests/test_auth.py tests/test_cli.py -q`

### Task 4: Local Cognito bootstrap and dev seed

**Objective:** Make the local stack usable end to end: a CLI command that creates a user pool and app client in `cognito-local` and writes the resulting IDs into the gitignored backend and frontend env files, plus a `make seed` that provisions the two demo tenants and three users named in Runtime Environment. This is what every E2E scenario's preconditions depend on.

**Files:**

- Modify: `backend/src/ptax/cli.py`
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `docker-compose.yml`
- Create: `dev/cognito-local/config.json`
- Test: `backend/tests/test_cli.py`

**Key Decisions / Notes:**

- `ptax-admin bootstrap-local-cognito` calls `CreateUserPool` (username attribute `email`) and `CreateUserPoolClient` (`ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH","ALLOW_REFRESH_TOKEN_AUTH"]`) against `COGNITO_ENDPOINT_URL`, then writes `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_ISSUER=http://localhost:9229/<poolId>` to `backend/.env` and `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_ENDPOINT=http://localhost:9229`, `VITE_AWS_REGION=local` to `frontend/.env.local`. Idempotent: reuses a pool named `ptax-local` if present.
- `make seed` = bootstrap + `alembic upgrade head` + `create-tenant`/`create-user` calls with `--password Password1! --permanent` for the three seed users.
- Seed users and tenants are exactly those listed in Runtime Environment; E2E scenarios reference them by name.

**Definition of Done:**

- [ ] After `make dev-up && make seed`, `aws --endpoint http://localhost:9229 cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id $VITE_COGNITO_CLIENT_ID --auth-parameters USERNAME=admin@demo.test,PASSWORD=Password1!` returns an `AccessToken`.
- [ ] `curl -H "Authorization: Bearer <that token>" localhost:8000/api/me` returns `Demo County` with role `admin`.
- [ ] Running `make seed` twice does not create duplicate pools, tenants, or users.
- [ ] Verify: `cd backend && uv run pytest tests/test_cli.py -q`

### Task 5: Users API (list, invite) with role enforcement

**Objective:** Expose tenant-scoped user management so a county admin can see their staff and invite new admins or reviewers. Invitation creates the Cognito user (Cognito emails the temporary password in AWS; the emulator logs it) and the tenant-bound `users` row in one request.

**Files:**

- Create: `backend/src/ptax/api/users.py`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/src/ptax/auth/cognito.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_cli.py`
- Test: `backend/tests/test_users_api.py`

**Key Decisions / Notes:**

- `GET /api/users` (admin) → users of the caller's tenant only, ordered by email. `POST /api/users {email, role}` (admin) → `201` with the new user; `409` if the email exists in any tenant (emails are globally unique because Cognito usernames are).
- On Cognito failure after nothing was written, return `502` with the Cognito error message; on DB failure after the Cognito user was created, delete the Cognito user (`AdminDeleteUser`) before returning `500`, so a retry is clean.
- Reviewer calling either endpoint → `403` via `require_role("admin")` (Task 3).

**Definition of Done:**

- [ ] Admin of tenant A listing users never sees tenant B's users; reviewer receives `403`.
- [ ] Invite creates the Cognito user and the `users` row with matching `sub`; duplicate email → `409` with no new Cognito user.
- [ ] Verify: `cd backend && uv run pytest tests/test_users_api.py -q`

### Task 6: Job runner and presigned uploads

**Objective:** Add the background job queue (enqueue, claim with `SKIP LOCKED`, success/failure with error capture, bounded retries) and the worker process that drains it, plus the presigned-PUT upload endpoint so browsers write files straight to S3/MinIO. Parcel ingestion (Task 7) and all Plan B processing run on this.

**Files:**

- Create: `backend/src/ptax/jobs/queue.py`
- Create: `backend/src/ptax/jobs/registry.py`
- Create: `backend/src/ptax/worker.py`
- Create: `backend/src/ptax/storage.py`
- Create: `backend/src/ptax/api/uploads.py`
- Modify: `backend/src/ptax/main.py`
- Modify: `Makefile`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_jobs.py`
- Test: `backend/tests/test_uploads.py`

**Key Decisions / Notes:**

- `claim_next` runs `UPDATE jobs SET status='running', started_at=now(), attempts=attempts+1 WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`. A handler exception marks the job `failed` with the traceback's last line in `error`; jobs with `attempts < 3` and a retryable error class are re-queued, others stay `failed`.
- `worker.py` polls every 2 s when idle, handles `SIGTERM` by finishing the current job then exiting (Fargate gives 30 s by default), and logs job id/type/duration.
- `POST /api/uploads {filename, content_type, purpose: "parcel_layer"}` → `{upload_id, key, url, expires_in}`; key is `tenants/{tenant_id}/{purpose}/{upload_id}/{sanitized filename}`; presigned for 15 min. The tenant prefix is derived from `CurrentUser`, never from the request body.
- `storage.py` builds the boto3 S3 client with `endpoint_url=S3_ENDPOINT_URL` when set and `signature_version='s3v4'`; presigned URLs must be reachable from the browser, so locally the endpoint is `http://localhost:9000`.
- `make worker` = `uv run python -m ptax.worker`.

**Definition of Done:**

- [ ] Two worker processes draining 50 queued no-op jobs concurrently process each job exactly once (asserted by a per-job counter table in the test).
- [ ] A handler that raises marks the job `failed` with a non-empty `error`; a handler raising the retryable class is re-queued until `attempts == 3`.
- [ ] `PUT` to the presigned URL from the test (against MinIO) stores the object under the tenant prefix; a `purpose` outside the allowed set → `422`.
- [ ] Verify: `cd backend && uv run pytest tests/test_jobs.py tests/test_uploads.py -q`

### Task 7: Parcel layer inspect/ingest pipeline and parcels API

**Objective:** Implement PRD Flow 2: register an uploaded file as a parcel layer, inspect it in a job (detect shapefile-in-zip or GeoJSON, read CRS, field names with sample values, feature count), let the admin choose the parcel-ID field, then ingest in a job (reproject to 4326, promote Polygon→MultiPolygon, skip non-polygon or empty geometries, bulk insert) and mark the layer current. Also expose the parcels read endpoints later plans and the map need.

**Files:**

- Create: `backend/src/ptax/parcels/ingest.py`
- Create: `backend/src/ptax/api/parcel_layers.py`
- Create: `backend/src/ptax/api/parcels.py`
- Create: `backend/tests/fixtures/make_fixtures.py`
- Create: `backend/tests/fixtures/parcels_small.geojson`
- Create: `backend/tests/fixtures/parcels_small_26915.zip`
- Create: `backend/src/ptax/parcels/__init__.py`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/src/ptax/worker.py`
- Modify: `backend/src/ptax/db/models.py`
- Modify: `backend/alembic/versions/0001_initial.py`
- Modify: `backend/src/ptax/cli.py`
- Test: `backend/tests/test_parcel_ingest.py`

**Key Decisions / Notes:**

- Endpoints: `POST /api/parcel-layers {upload_key, original_filename}` (admin) → creates the row with status `uploaded`, enqueues `parcel_layer.inspect`, returns `201`. `GET /api/parcel-layers` → history newest first with `is_current`. `GET /api/parcel-layers/{id}` → full row incl. `fields` and `error`. `POST /api/parcel-layers/{id}/ingest {parcel_id_field}` (admin) → `409` unless status is `awaiting_field`; sets `ingesting`, enqueues `parcel_layer.ingest`.
- Inspect: download to a temp dir; for `.zip`, find exactly one `.shp` (else fail with "no shapefile or GeoJSON found" / "multiple shapefiles"); read with `pyogrio.read_info` + first 5 rows for samples; write `fields` (name, dtype, 3 samples), `source_crs`, `feature_count`; status `awaiting_field`. Any exception → `failed` with `error`.
- Ingest: read in chunks of 5,000 with `pyogrio.read_dataframe(..., skip_features, max_features)`, `to_crs(4326)`, `shapely.multipolygons` promotion, drop rows whose `parcel_id_field` is null/empty or geometry is not (Multi)Polygon, counting them into `skipped_count`; bulk insert with `psycopg` `COPY` of WKB; duplicate `parcel_ref` within the layer keeps the first and counts the rest as skipped; on success set `tenants.current_parcel_layer_id`, status `ready`.
- `GET /api/parcels?bbox=minx,miny,maxx,maxy&limit=` → GeoJSON FeatureCollection from the current layer (max 5,000 features, `413` if the bbox holds more); `GET /api/parcels/by-ref/{parcel_ref}` → one feature. Both tenant-scoped.
- Fixture generator writes 25 rectangles with `PIN` and `OWNER` attributes; the zip variant is in EPSG:26915 to exercise reprojection. Fixtures are committed so tests don't depend on the generator.
- Performance: the 50k-feature timing check in Risks uses `make_fixtures.py --count 50000` into a temp dir (not committed).

**Definition of Done:**

- [ ] Ingesting `parcels_small_26915.zip` with field `PIN` yields 25 `parcels` rows in EPSG:4326 whose centroids match the GeoJSON fixture within 1e-6°, `skipped_count == 0`, layer `ready`, tenant `current_parcel_layer_id` set.
- [ ] A zip with no `.shp` and no `.geojson` → layer `failed`, `error` contains "no shapefile or GeoJSON found"; a layer with 2 null-PIN rows and 1 LineString row → `skipped_count == 3`.
- [ ] Re-uploading a layer keeps the previous layer's parcels and switches `current_parcel_layer_id`; `GET /api/parcels` returns only current-layer features for the caller's tenant.
- [ ] 50,000 generated features ingest in under 60 s against the compose DB (timed in the test, skipped unless `PTAX_PERF=1`).
- [ ] Verify: `cd backend && uv run pytest tests/test_parcel_ingest.py -q`

### Task 8: Frontend shell with Cognito sign-in

**Objective:** Build the authenticated React shell: login page using `USER_PASSWORD_AUTH` (with `NEW_PASSWORD_REQUIRED` handling for invited users), session persistence and refresh, an API client that attaches the access token, protected routes, and a layout showing tenant name, user email, role-aware navigation, and sign-out. Verified by TS-001.

**Files:**

- Create: `frontend/src/auth/cognito.ts`
- Create: `frontend/src/auth/AuthContext.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/pages/LoginPage.tsx`
- Create: `frontend/src/components/Layout.tsx`
- Create: `frontend/src/auth/cognito.test.ts`
- Create: `frontend/src/pages/ParcelsPage.tsx`
- Create: `frontend/src/pages/UsersPage.tsx`
- Create: `frontend/src/index.css`
- Create: `frontend/src/vite-env.d.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/package.json`
- Modify: `frontend/pnpm-lock.yaml`
- Modify: `frontend/vite.config.ts`
- Modify: `docker-compose.yml`

**Key Decisions / Notes:**

- Use `@aws-sdk/client-cognito-identity-provider` directly (`InitiateAuthCommand` with `AuthFlow: "USER_PASSWORD_AUTH"`, `RespondToAuthChallengeCommand` for `NEW_PASSWORD_REQUIRED`, `REFRESH_TOKEN_AUTH` for refresh); pass `endpoint: import.meta.env.VITE_COGNITO_ENDPOINT` when set. No Amplify.
- Tokens live in `sessionStorage`; `client.ts` refreshes when the access token has < 60 s left and signs out on a `401` from the API.
- After sign-in, `GET /api/me` populates `AuthContext` with tenant and role; nav shows **Parcels** for everyone and **Users** only for `admin`.
- Routing: `/login`, `/parcels`, `/users`, default redirect `/` → `/parcels`. Unauthenticated → `/login`.
- Styling: Tailwind v4 via the Vite plugin; keep components unstyled-library-free.
- `cognito.test.ts` (vitest) mocks the SDK client and asserts: challenge response path, refresh path, and that `endpoint` is only set when the env var exists.

**Definition of Done:**

- [ ] TS-001 passes against the local stack.
- [ ] Signing in as an invited user whose password is temporary shows a "Set a new password" form and completes sign-in after submission (verified against real Cognito in Task 12; locally per the Assumptions note).
- [ ] Verify: `cd frontend && pnpm test && pnpm build && pnpm lint`

### Task 9: Frontend parcels page

**Objective:** Implement the admin's parcel-layer workflow in the UI: dropzone upload via presigned PUT with progress, live status polling through inspect, a field picker populated from `fields` with sample values, ingest, and a summary card (parcel count, skipped, source CRS) plus layer history. Reviewers see the read-only summary. Verified by TS-002 and TS-003.

**Files:**

- Modify: `frontend/src/pages/ParcelsPage.tsx`
- Create: `frontend/src/api/parcelLayers.ts`
- Create: `frontend/src/components/UploadDropzone.tsx`
- Create: `frontend/src/components/LayerStatus.tsx`
- Modify: `frontend/src/App.tsx`

**Key Decisions / Notes:**

- Upload sequence: `POST /api/uploads` → `PUT` file to `url` with `XMLHttpRequest` for progress events → `POST /api/parcel-layers` → poll `GET /api/parcel-layers/{id}` every 2 s until `awaiting_field`, `ready`, or `failed`.
- Field picker lists `fields` with dtype and samples; the **Ingest** button is disabled until a field is chosen; a `409` from ingest (stale status) refreshes the layer.
- Accepts `.zip` and `.geojson`/`.json`; reject others client-side with a message before uploading.
- Reviewer role hides the dropzone and ingest controls but shows summary and history.

**Definition of Done:**

- [ ] TS-002 and TS-003 pass against the local stack.
- [ ] Verify: `cd frontend && pnpm build && pnpm lint`

### Task 10: Frontend users page

**Objective:** Give admins a users table and an invite form (email + role) wired to Task 5, with reviewer access blocked at the route. Verified by TS-004 and TS-005.

**Files:**

- Modify: `frontend/src/pages/UsersPage.tsx`
- Modify: `frontend/src/App.tsx`

**Key Decisions / Notes:**

- Route guard renders "Admins only" for non-admins rather than redirecting, so the restriction is observable in TS-004.
- `409` from invite shows "That email already has an account"; `502` shows the Cognito message.

**Definition of Done:**

- [ ] TS-004 and TS-005 pass against the local stack.
- [ ] Verify: `cd frontend && pnpm build && pnpm lint`

### Task 11: Production image and CDK stacks

**Objective:** Produce the deployable artifacts: a multi-stage Dockerfile that builds the frontend and serves it from the backend image, and a CDK TypeScript app defining the VPC, Aurora Serverless v2 PostgreSQL 16 cluster, S3 uploads bucket, Cognito user pool + client, ECS cluster with the API service behind an ALB and a worker service, with secrets and env wired per Global Constraints. Proven by `cdk synth`, assertion tests, and running the built image locally — deployment itself is Task 12.

**Files:**

- Create: `backend/Dockerfile`
- Create: `infra/package.json`
- Create: `infra/cdk.json`
- Create: `infra/tsconfig.json`
- Create: `infra/bin/ptax.ts`
- Create: `infra/lib/network-stack.ts`
- Create: `infra/lib/data-stack.ts`
- Create: `infra/lib/auth-stack.ts`
- Create: `infra/lib/compute-stack.ts`
- Create: `infra/lib/app.ts`
- Create: `infra/jest.config.js`
- Create: `infra/test/stacks.test.ts`
- Create: `infra/test/fixtures/image/backend/Dockerfile`
- Create: `.dockerignore`
- Create: `backend/docker-entrypoint.sh`
- Create: `backend/src/ptax/migrate.py`
- Create: `backend/src/ptax/api/config.py`
- Create: `backend/tests/test_migrate.py`
- Create: `backend/tests/test_runtime_config.py`
- Create: `frontend/src/config.ts`
- Modify: `backend/src/ptax/main.py`
- Modify: `backend/src/ptax/config.py`
- Modify: `backend/src/ptax/cli.py`
- Modify: `backend/alembic/env.py`
- Modify: `backend/tests/test_cli.py`
- Modify: `frontend/src/auth/cognito.ts`
- Modify: `frontend/src/auth/cognito.test.ts`
- Modify: `frontend/src/auth/AuthContext.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/vite-env.d.ts`
- Modify: `Makefile`
- Modify: `README.md`

**Key Decisions / Notes:**

- Dockerfile stages: `node:26` builds `frontend/dist`; `python:3.13-slim` installs the backend with uv (`--frozen`), copies `dist` to `/app/static`; entrypoint script runs `alembic upgrade head` only when `PTAX_RUN_MIGRATIONS=1`, then `exec "$@"`; default command `uvicorn ptax.main:app --host 0.0.0.0 --port 8000`; worker service overrides the command with `python -m ptax.worker`. The migration step runs inside `SELECT pg_advisory_lock(<fixed key>)` so concurrent starts serialize. GDAL runtime libs come from the `pyogrio` wheel — no apt GDAL.
- Only the API task definition sets `PTAX_RUN_MIGRATIONS=1`; the worker task definition does not.
- Operator access in AWS: `README.md` documents `aws ecs run-task --cluster <cluster> --task-definition <worker-taskdef> --launch-type FARGATE --network-configuration <private subnets + task SG> --overrides '{"containerOverrides":[{"name":"worker","command":["ptax-admin","create-tenant", ...]}]}'` and reading the result from CloudWatch Logs; `compute-stack.ts` outputs the cluster name, worker task definition ARN, private subnet ids, and task security group id so the command can be assembled without console lookups. The task role already holds the DB secret and Cognito admin permissions.
- `main.py` mounts `/app/static` at `/` with an SPA fallback only when the directory exists, so local dev is unaffected.
- `network-stack.ts`: VPC with 2 AZs, 1 NAT gateway. `data-stack.ts`: `rds.DatabaseCluster` with `engine: auroraPostgres(VER_16_x)`, `writer: ClusterInstance.serverlessV2('writer')`, `serverlessV2MinCapacity: 0`, `serverlessV2MaxCapacity: 4`, generated master secret; S3 bucket with CORS `PUT` from the app origin, versioning on, block public access. `auth-stack.ts`: `UserPool` (sign-in by email, self-signup disabled) + `UserPoolClient` with `authFlows: {userPassword: true}`; `COGNITO_ISSUER` is computed from region + pool id. `compute-stack.ts`: `ecs_patterns.ApplicationLoadBalancedFargateService` for the API (health check `/api/health`, `desiredCount: 1`, image from `backend/Dockerfile` via `DockerImageAsset`), a plain `FargateService` for the worker, DB secret injected via `secrets`, task role granted the bucket and `cognito-idp:AdminCreateUser/AdminSetUserPassword/AdminDeleteUser` on the pool. Optional context `certificateArn` adds an HTTPS listener and redirects 80→443.
- `SHORTCUT:` migrations at API container start with `desiredCount: 1` (advisory-locked, worker excluded); upgrade trigger is scaling the API beyond one task, at which point migrations move to a one-off ECS task run before the service update.
- Backend env additions for this task: `PTAX_RUN_MIGRATIONS` (entrypoint only; not read by `Settings`).
- `make image` builds the Dockerfile; `make image-run` runs it against the compose stack with `--network host` for a local smoke test.

**Definition of Done:**

- [ ] `docker build -f backend/Dockerfile .` succeeds and the container, run against the compose stack, serves `/` (the SPA) and `/api/health` → `200`, and applies migrations on start.
- [ ] `cd infra && pnpm cdk synth` produces four stacks with no errors and no AWS credentials.
- [ ] Assertion tests confirm: Aurora cluster is `aurora-postgresql` with serverless v2 scaling min 0; the API target group health check path is `/api/health`; the user pool client enables `ALLOW_USER_PASSWORD_AUTH`; the worker task definition's command is `python -m ptax.worker`; both task definitions receive the database host/user/password from the cluster secret (the app composes `DATABASE_URL` from them); only the API task definition sets `PTAX_RUN_MIGRATIONS=1`; the stack exports cluster name, worker task definition ARN, private subnet ids, and task security group id.
- [ ] Running the built image with `PTAX_RUN_MIGRATIONS` unset does not touch the schema; two containers started simultaneously with it set both come up healthy with the schema migrated once.
- [ ] Verify: `cd infra && pnpm test && pnpm cdk synth --quiet`

### Task 12: Deploy to AWS (user)

**Objective:** Deploy the CDK app to the user's AWS account and prove a real sign-in against the deployed stack. This cannot run in the implementing session because no AWS credentials are configured on this machine; the exact commands are recorded in `README.md` by Task 11.

**Owner:** User

**User Action:** Run `aws login` (or configure credentials), then `cd infra && pnpm cdk bootstrap && pnpm cdk deploy --all`, then run `ptax-admin create-tenant` and `create-user` as one-off ECS tasks using the `aws ecs run-task` command documented in `README.md` (Task 11), and open the ALB URL printed by the deploy.

**Files:**

- Modify: `README.md`
- Modify: `infra/lib/data-stack.ts`
- Modify: `infra/lib/compute-stack.ts`
- Modify: `infra/test/stacks.test.ts`
- Modify: `backend/Dockerfile`
- Modify: `backend/src/ptax/migrate.py`
- Modify: `backend/src/ptax/worker.py`
- Modify: `backend/src/ptax/config.py`
- Test: `backend/tests/test_migrate.py`
- Test: `backend/tests/test_jobs.py`
- Test: `backend/tests/test_runtime_config.py`

**Key Decisions / Notes:**

- The API service's env is fully generated by CDK; the only manual input is the ACM `certificateArn` context if HTTPS is wanted.
- Provisioning runs inside the VPC via `aws ecs run-task` on the worker task definition — Aurora is not publicly reachable and no bastion exists.
- `README.md` gains a "Verified deployment" note (date, region) once the user confirms, so the repo records that the deploy path has been exercised.

**Definition of Done:**

- [ ] `cdk deploy --all` completes; the ALB DNS name serves `/api/health` → `200 {"status":"ok","db":"ok"}`.
- [ ] The `ptax-admin` one-off tasks exit 0 (CloudWatch Logs show the created tenant and user), and that user can sign in at the ALB URL and sees their tenant name (TS-001 against AWS).
- [ ] Verify: `curl -s http://<alb-dns>/api/health`

## E2E Results

Executed in the verification phase against the final code on a freshly reset local stack (`make dev-down && make dev-up && make seed`), Chrome via Claude in Chrome, Vite on port 5175.

| Scenario | Priority | Result | Fix Attempts | Notes |
|----------|----------|--------|--------------|-------|
| TS-001   | Critical | PASS   | 0            | Redirect to `/login`, header `Demo County` / `admin@demo.test`, sign-out bounces protected route |
| TS-002   | Critical | PASS   | 0            | zip → `awaiting_field` → `PIN` → `ingesting` → `ready`; `25 parcels · 0 skipped · CRS EPSG:26915 → 4326`; persists on reload |
| TS-003   | High     | PASS   | 0            | `junk.zip` → `failed` "no shapefile or GeoJSON found in archive"; GeoJSON reaches `awaiting_field`; failed layer stays in history |
| TS-004   | High     | PASS   | 0            | Invite creates row + toast; reviewer has no Users link, `/users` → "Admins only"; invited user's temporary password → "Set a new password" → signed in as reviewer |
| TS-005   | Critical | PASS   | 0            | `admin@other.test` sees `Other County`, empty parcels, only own user |

Also verified against the AWS deployment during Task 12: TS-001 through the ALB with a real Cognito pool (API and SPA).

## Deviations

- Task 1 (tactical): the standalone `minio/mc` Docker image is no longer published → `minio-init` in `docker-compose.yml` runs `mc` from the `minio/minio` image instead.
- Task 1 (tactical): bucket-level CORS via `mc` is unnecessary on MinIO → the allow-list is set with `MINIO_API_CORS_ALLOW_ORIGIN` on the `minio` service in `docker-compose.yml` (5173, 5174 for Vite's fallback port, 8000). Real S3 CORS is configured in Task 11's `data-stack.ts` as planned.
- Task 12 (tactical, found by deploying): Aurora rejected PostgreSQL `16.6` (withdrawn in `us-east-1`) → `infra/lib/data-stack.ts` pins `VER_16_13`. The amd64 image build crashed in the Node stage under QEMU on Apple Silicon → `backend/Dockerfile` builds the frontend stage with `--platform=$BUILDPLATFORM`.
- Task 12 (tactical, found in CloudWatch): the generated Aurora password URL-encodes to `%`-sequences that Alembic's ConfigParser rejects → `backend/src/ptax/migrate.py` escapes `%` (regression test with a real `%`/`=` password role in `backend/tests/test_migrate.py`). The worker exited when `jobs` did not exist yet → `backend/src/ptax/worker.py` `tick()` logs and retries on database errors (`backend/tests/test_jobs.py`). `S3_ENDPOINT_URL`/`COGNITO_ENDPOINT_URL` defaults (local MinIO/emulator) leaked into AWS because ECS cannot unset a variable → `Settings` treats `""` as unset and `infra/lib/compute-stack.ts` passes `""` (`backend/tests/test_runtime_config.py`).
- Task 12 (operational, agent-decided): with `PtaxCompute` stuck in `CREATE_IN_PROGRESS` waiting on a service that could never stabilise with the first image, the Aurora master password was rotated to an alphanumeric value and the secret updated in place so the running tasks could migrate; the fixed image was then redeployed. The database was empty at the time; the secret remains the single source of truth.
- Task 12 (user-agreed): the user supplied temporary AWS credentials in chat and asked the agent to deploy, so the task is executed by the agent (credentials used only as per-command environment variables, never stored); region `us-east-1` assumed. `README.md` gains the verified-deployment note as planned.
- Task 11 (tactical): Vite bakes `VITE_*` values at build time, but CDK builds the image before the Cognito client exists → the SPA now fetches `{cognito_client_id, cognito_endpoint_url, aws_region}` from a public `GET /api/config` (`backend/src/ptax/api/config.py`, `frontend/src/config.ts`) at runtime. `frontend/.env.local` and the `VITE_COGNITO_*` / `VITE_AWS_REGION` variables are gone; `ptax-admin bootstrap-local-cognito` writes only `backend/.env` (Task 4's DoD still holds for the backend env). Global Constraints' frontend env-var line is superseded by this.
- Task 11 (tactical): the Aurora master secret has no URL field, so `Settings` (`backend/src/ptax/config.py`) composes `DATABASE_URL` from `DATABASE_HOST/PORT/NAME/USER/PASSWORD` when `DATABASE_URL` is unset; CDK injects host/user/password from the secret. Task 11's DoD wording updated accordingly.
- Task 11 (tactical): granting DB access from compute-stack security groups created a cross-stack cycle → `PtaxData` owns an "app" security group pre-authorised on the DB port; `PtaxCompute` imports it immutably and adds an API-only group for ALB ingress. The `TaskSecurityGroupId` output is that app group.
- Task 11 (tactical): `alembic/env.py` now calls `fileConfig(..., disable_existing_loggers=False)` and `ptax.migrate` sets the `ptax` logger level, so the migrator's own log lines are visible in container logs.
- Task 8 (tactical): routes need targets, so minimal `frontend/src/pages/ParcelsPage.tsx` and `frontend/src/pages/UsersPage.tsx` (heading + admin guard) are created here; Tasks 9 and 10 modify them instead of creating them. Added `frontend/src/index.css` (Tailwind entry) and `frontend/src/vite-env.d.ts` (typed `import.meta.env`).
- Task 8 (tactical): Vite's dev port drifts on this machine (5173/5174 taken by other projects), so local MinIO now uses its default allow-any-origin CORS (`docker-compose.yml`) instead of a fixed origin list; real S3 CORS is still defined in Task 11.
- Task 7 (tactical): `created_at` defaults changed from `now()` to `clock_timestamp()` in `backend/alembic/versions/0001_initial.py` and `backend/src/ptax/db/models.py` (migration not yet deployed anywhere) so rows written in one transaction keep insertion order — layer history and FIFO job claiming depend on it.
- Task 7 (tactical): `ptax-admin seed-local` in `backend/src/ptax/cli.py` now re-binds a user that already exists in Cognito but not in the database (`UsernameExistsException` → look up `sub`), which happens after a database-only reset.
- Task 6 (environment, user-agreed): the Docker VM disk was 99% full and MinIO refused writes (`XMinioStorageFull`); with the user's approval removed the stopped `dowser-zeronet-1` container (28.7 GB) and pruned Docker build cache (3 GB). No project files changed.
- Task 5 (tactical): tests collided with `make seed` rows (fixed FIPS codes) in the dev database → `backend/tests/conftest.py` now creates and migrates a separate `ptax_test` database on the compose server; the dev database is untouched by the suite. `get_cognito` FastAPI dependency added to `backend/src/ptax/auth/cognito.py` so tests can route admin calls at moto.
- Task 5 (tactical): `email-validator` (via `pydantic[email]`) rejects the reserved `.test` TLD the seed users use → `InviteIn.email` validates with `test_environment=True` and no deliverability check (Cognito owns delivery).
- Task 4 (tactical): `make seed` calls an idempotent `ptax-admin seed-local` (skips existing tenants/users) instead of chaining `create-tenant`/`create-user` with `|| true`, so a second run is silent rather than a wall of "already exists" errors. Same seed data as planned.
- Task 4 (tactical): cognito-local inside Docker stamps `iss` as `http://0.0.0.0:9229/<pool>`, which never matches `COGNITO_ISSUER` → added `dev/cognito-local/config.json` (`TokenConfig.IssuerDomain = http://localhost:9229`) mounted into the container by `docker-compose.yml`.
- Task 2 (tactical): PostGIS 3.4 promotes a Polygon to MultiPolygon on the `geometry(MultiPolygon,4326)` typmod cast instead of rejecting it → `backend/tests/test_migrations.py` asserts the stored geometry type is `ST_MultiPolygon` after a Polygon insert and that a LineString is rejected, which proves the same storage contract. Added `backend/alembic/script.py.mako` (Alembic's revision template) so `alembic revision` works for Plans B/C.
- Task 1 (tactical): pnpm 11 blocks esbuild's postinstall unless allowed → added `frontend/pnpm-workspace.yaml` (`allowBuilds: esbuild: true`); `frontend/eslint.config.js` added so `pnpm lint` exists for later tasks' verify commands.

## Deferred Ideas

- Forgot-password flow via Cognito hosted UI or custom pages.
- Migrations as a one-off ECS task (see the `SHORTCUT:` in Task 11).
- SSO (Entra/Okta) via Cognito federated identity providers — a driver for having chosen Cognito.
- ML building-segmentation detector behind the Plan B `Detector` interface.
