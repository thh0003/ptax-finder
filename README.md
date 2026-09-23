# ptax-finder

Multi-county SaaS that compares aerial imagery of every parcel in a county across two
years, flags parcels that appear to have gained a structure, and gives assessor staff a
review queue with an audit trail. Requirements: `docs/prd/2026-09-21-parcel-structure-change-detection.md`.

## Layout

| Path        | What                                                                 |
| ----------- | -------------------------------------------------------------------- |
| `backend/`  | Python 3.13 (uv): FastAPI API, background worker, `ptax-admin` CLI   |
| `frontend/` | React + Vite + TypeScript SPA                                        |
| `infra/`    | AWS CDK (TypeScript): VPC, Aurora Serverless v2, S3, Cognito, ECS    |
| `docs/`     | PRD and implementation plans                                         |

## Local development

Prerequisites: Docker, `uv`, Node 26 + `pnpm`.

```sh
make dev-up      # PostGIS :5432, MinIO :9000 (console :9001), cognito-local :9229
make seed        # local user pool + client, migrations, demo tenants/users (idempotent)
make api         # FastAPI on :8000  (GET /api/health)
make worker      # background job runner
make web         # Vite on :5173 (proxies /api -> :8000)
```

`make seed` writes `backend/.env` with the local pool/client ids (the SPA reads them from
`GET /api/config`) and creates two tenants. All demo users share the password `Password1!`:

| Tenant       | User                  | Role     |
| ------------ | --------------------- | -------- |
| Demo County  | `admin@demo.test`     | admin    |
| Demo County  | `reviewer@demo.test`  | reviewer |
| Other County | `admin@other.test`    | admin    |

Operator provisioning (any environment) uses the `ptax-admin` CLI:

```sh
cd backend
uv run ptax-admin create-tenant --name "Hennepin County" --state MN --fips 27053
uv run ptax-admin create-user --tenant-fips 27053 --email a@county.gov --role admin
uv run ptax-admin set-password a@county.gov --permanent 'NewPassword1!'
```

If port 5173 is busy Vite picks the next free port; MinIO's CORS allow-list already
includes 5174.

Tests:

```sh
make test-backend   # pytest against the compose PostGIS
make test-frontend  # vitest
```

Backend configuration is read from environment variables (or `backend/.env`); the
defaults in `backend/src/ptax/config.py` match the compose stack. The SPA fetches the
Cognito client id and region from `GET /api/config` at runtime, so one build works in
every environment.

### Imagery and runs

After a parcel layer is ingested (Parcels page), the **Imagery** page lists the NAIP years
that cover the county with an ingest size estimate, lets an admin ingest them, and lets an
admin create an upload year (year + optional provider), add GeoTIFF/COG files to it and
finish the upload; each year shows its status, band count, resolution, coverage of the
county footprint and the number of parcels without coverage. The **Runs** page compares a
base year with a later target year over every parcel and reports processed / candidate /
skipped counts. A finished run lists its parcels highest score first; opening one shows the
**parcel change viewer** at `/runs/{run}/parcels/{parcel}` — the base year, the target year,
and the target year with the new structures the run detected drawn on it, alongside the
score and the measurements behind it. The scoring structure and the rest of the detected
new built-up area are drawn in different colours, and the markup can be toggled off to
check it against the bare imagery. A parcel the run skipped says why instead of showing an
empty pane. Uploaded imagery must be a GeoTIFF or COG with a CRS, 3 or 4 bands
(RGB or RGB+NIR), `uint8` or `uint16` pixels, 1 m/px or finer, and at most 5 GB per file;
everything is stored as a uint8 COG under `tenants/<tenant>/imagery/`.

A run records **where** it found the change, not just how much: the detected new built-up
area and the single structure the score rests on are stored as polygons on `run_parcels`
when the parcel is scored. The viewer draws what the run recorded, never a fresh
detection — so a decision keeps matching the picture it was made from while the detector
changes underneath. Runs completed before this shipped keep their scores and show no
markup rather than one re-derived by today's code.

Locally `NAIP_SOURCE` defaults to `fixture`: the NAIP list comes from the small synthetic
years in `backend/tests/fixtures/imagery/` (`make imagery-fixtures` regenerates them), so
the whole ingest -> run flow works with no AWS credentials. To exercise real NAIP
discovery on a laptop, set `NAIP_SOURCE=stac` in `backend/.env`; discovery needs no
credentials, but ingesting a year reads the requester-pays `naip-analytic` bucket in
`us-west-2` (`aws login` first). Ingest is refused above `NAIP_MAX_INGEST_GB` (default 60).

| Variable             | Default (local)                              | Meaning                                    |
| -------------------- | -------------------------------------------- | ------------------------------------------ |
| `NAIP_SOURCE`        | `fixture`                                    | `stac` (Earth Search) or `fixture`         |
| `NAIP_STAC_URL`      | `https://earth-search.aws.element84.com/v1`  | STAC API root for the `naip` collection    |
| `NAIP_FIXTURE_DIR`   | `tests/fixtures/imagery`                     | Directory of `naip_<year>.tif` fixtures    |
| `NAIP_MAX_INGEST_GB` | `60`                                         | Refuse NAIP ingests estimated above this   |
| `NAIP_AWS_REGION`    | `us-west-2`                                  | Region of the NAIP buckets                 |

### Measuring detector accuracy

Detector changes are judged on real imagery over real parcels, not on the synthetic
fixtures — the fixtures test wiring, `ptax-eval` tests accuracy:

```bash
cd backend
uv run ptax-eval build --aoi nw-hennepin --base-year 2010 --target-year 2021
make eval-fetch      # cache the AOI's NAIP items through the production COG writer
make eval-score      # precision / recall / flag rate, reweighted to the county base rate

# judge the detector on what the imagery shows rather than on assessor build years
uv run ptax-eval score eval/nw-hennepin-2010-2021.json \
  --labels eval/visual-labels-nw-hennepin-2010-2021.json

# the learned detector (optional `ml` dependency group; production never installs it)
uv sync --group ml
uv run --group ml ptax-eval score eval/nw-hennepin-2010-2021.json \
  --labels eval/visual-labels-nw-hennepin-2010-2021.json --detector segmentation
```

Needs network but **no AWS credentials**. Every run also prints a no-imagery baseline that
ranks parcels by size alone; a detector that does not beat it has not detected anything.
See [`backend/eval/README.md`](backend/eval/README.md) for the labelled sets, the visual
labels that replaced `BUILD_YR` as truth, and the measured ceiling.

## Production image

```sh
make image       # docker build -f backend/Dockerfile .  (multi-stage: SPA + backend)
make image-run   # run it on :8080 against the compose stack
```

The API container runs Alembic at start only when `PTAX_RUN_MIGRATIONS=1` (under a
Postgres advisory lock); the worker runs the same image with `python -m ptax.worker`.

## Deploying to AWS

`infra/` is an AWS CDK (TypeScript) app with four stacks: `PtaxNetwork` (VPC),
`PtaxData` (Aurora Serverless v2 PostgreSQL 16, uploads bucket), `PtaxAuth` (Cognito
user pool + client), `PtaxCompute` (ECS Fargate API behind an ALB, worker service). The
compute stack sets `NAIP_SOURCE=stac` and grants the task role read access to the
requester-pays `naip-analytic` bucket; NAIP lives in `us-west-2`, so a stack in another
region pays cross-region transfer (~$0.02/GB) once per ingested year.

```sh
cd infra && pnpm install
pnpm test                                  # assertion tests
pnpm cdk synth --quiet                     # no credentials needed
aws login                                  # or any other credential setup
pnpm cdk bootstrap                         # once per account/region
pnpm cdk deploy --all                      # optional: -c certificateArn=arn:aws:acm:... for HTTPS
```

The deploy prints `PtaxCompute.AlbDnsName` (open it in a browser), plus `ClusterName`,
`WorkerTaskDefinitionArn`, `PrivateSubnetIds`, and `TaskSecurityGroupId`.

### Provisioning tenants in AWS

Aurora is private to the VPC, so run `ptax-admin` as a one-off ECS task using the worker
task definition (it already has the database secret and Cognito permissions):

```sh
CLUSTER=<ClusterName>; TASKDEF=<WorkerTaskDefinitionArn>
SUBNETS=<PrivateSubnetIds>; SG=<TaskSecurityGroupId>

aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"worker","command":["ptax-admin","create-tenant","--name","Hennepin County","--state","MN","--fips","27053"]}]}'

aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"worker","command":["ptax-admin","create-user","--tenant-fips","27053","--email","you@county.gov","--role","admin"]}]}'
```

Task output is in the `worker` CloudWatch log group. Cognito emails the temporary
password; the first sign-in asks for a permanent one.

### Verified deployment

2026-09-21, `us-east-1`: all four stacks deployed from this repository; `/api/health`
returned `{"status":"ok","db":"ok"}` on the ALB, a tenant and users were provisioned with
the `run-task` commands above, and a user signed in through the SPA served by the ALB.

### Verified NAIP ingest

2026-09-22, `us-east-1`: with the 25-parcel fixture layer ingested for the demo tenant, the
Imagery page listed seven real NAIP years (2010–2023, 1 tile each, 100% coverage, 1.0 m →
0.3 m). Ingesting 2010 (~0.085 GB estimated) and 2021 (~0.24 GB) each completed in about ten
seconds, reaching `ready` with 100% coverage and 0 uncovered parcels, from the requester-pays
`naip-analytic` bucket in `us-west-2`; thumbnails and per-parcel previews render real imagery.
Total requester-pays egress for the two years was well under a dollar at this footprint size —
a full county is tens of GB per year, which is what `NAIP_MAX_INGEST_GB` guards.

A comparison run over that imagery completes, but the v1 classical detector flagged 23 of
25 parcels comparing 1.0 m 2010 against 0.6 m 2021. See the Detector finding in
`docs/plans/2026-09-21-imagery-and-detection.md` — and note that its stated cause
(resampling the finer year under the texture threshold) was later **refuted by
measurement**: Plan D found the same-resolution control showed a *larger* year gap, and the
real defect was that bare ground classified as built-up.

### Verified parcel change viewer deployment

2026-09-23, `us-east-1`: `PtaxCompute` updated to `UPDATE_COMPLETE`; API and worker both
1/1 on task definition revision `:6`. Migrations **0003 → 0004 ran in the API container at
start** against Aurora and the app came up clean:

```
migrating database to head
Running upgrade 0002 -> 0003, run_parcels: where the run found the change
Running upgrade 0003 -> 0004, run_parcels: an index the score-ordered parcel list can actually use
migrations complete
Application startup complete.
```

`/api/health` returns `{"status":"ok","db":"ok"}`, the SPA serves 200, and all three new
routes are live — `GET /api/runs/{run}/parcels`, `.../parcels/{parcel}` and
`.../parcels/{parcel}/overlay.png` each answer **401** without a token while a genuinely
absent path answers 404, so they are routed rather than swallowed by a catch-all. The
`cdk diff` beforehand touched only the two container image digests: no infrastructure,
IAM or security-group change.

Not exercised on the deployed stack: the viewer against a real signed-in session. That
needs a Cognito password, which this workflow does not enter. The four E2E scenarios ran
against a local stack carrying the same image content — see the plan's E2E Results.

### Verified detector accuracy

2026-09-22, `us-east-1`, tenant `Demo County`, over the same 25 parcels and the same NAIP
2010 → 2021 pair: **2 of 25 flagged at the shipped defaults** (threshold 0.3, minimum
structure 37.2 m² = 400 ft²), against **23 of 25** for the v1 detector. 25 processed, 0
skipped. The run-level radiometric fit ran in production over 25 sampled parcels with an
NIR gain of 1.172, matching the direction measured offline.

**The detector is not yet good enough to ship a review queue**, and the evidence for that
is recorded rather than estimated. All 300 **Hennepin County** parcels in the evaluation
set were labelled by eye from their base/target image pairs — the app compares two images
and looks for improvements, which is not what the assessor's `BUILD_YR` records. Against
those labels, at the shipped defaults: precision **28.7% against a 17.95% base rate**
(a 1.6× lift) at a 30% flag rate, and **no combination of threshold and minimum structure
size reaches the ≤2%-flagged / ≥60%-precision target**. Precision falls to exactly zero the
moment the queue shrinks to a workable size — at a 1.86% flag rate, nothing flagged had
improved. That bounds what a classical cue can do on 1 m NAIP and is the trigger for a
learned detector.

Labelling the imagery mattered: `BUILD_YR` disagrees with what the pictures show on
**31 of 300 parcels (10.3%)**, in both directions. 16 parcels recorded as built in 2021
were photographed before the house went up, and 14 parcels recorded as built before 2010
visibly gained a shed, an outbuilding or a pool that `BUILD_YR` cannot see. Because the
stratum holding those 14 is 92% of the county, the true base rate is three times the one
`BUILD_YR` implies. Every run also prints a no-imagery baseline that ranks parcels by size
alone; relabelling cut that baseline's average precision from 0.48 to 0.26, which is how
much of it was an artefact of the assessor's records rather than of the ground.

**A learned detector passes the decision gate** (2026-09-23). `segmenter-v1` — a U-Net
building segmenter trained on NAIP with open building footprints, from neighbourhoods at
least 1 km from the evaluation area and frozen before its first score — ranks the same 300
labelled parcels at **average precision 0.588 (95% CI 0.422–0.772)** against 0.224 for the
classical detector and 0.258 for the no-imagery baseline, with **19 of its top 20** parcels
genuinely improved and every top-20 structure on a roof, where the classical detector's top
13 were graded lots and bare ground. At its shipped defaults it flags 14% at 61% precision;
the ≤2% / ≥60% target is met on the point estimate (1.7% flagged, 14 of 14 correct) but
rests on too few parcels to call settled. CPU inference costs about 74 ms per parcel. It is
selectable in the evaluation harness only (`ptax-eval score --detector segmentation`, which
needs `uv sync --group ml`); production runs still use the classical detector until the
integration plan lands.

Full evidence, including the labelled sets, the visual labels, the no-imagery baseline and
the learned detector's gate, in [`backend/eval/README.md`](backend/eval/README.md).

Things learned on the way, already reflected in the code:

- Aurora withdraws older PostgreSQL 16 minors; `infra/lib/data-stack.ts` pins a version
  that `aws rds describe-db-engine-versions --engine aurora-postgresql` still lists.
- The generated master password contains characters that URL-encode to `%`; the
  migrator escapes them for Alembic's config parser.
- The frontend build stage runs on the host platform (`--platform=$BUILDPLATFORM`),
  because Node under QEMU emulation crashes when building an amd64 image on Apple Silicon.
- rasterio's wheel bundles GDAL but GDAL links the system libexpat, so the image installs
  `libexpat1`; run `make image-check` before deploying to catch that class of failure locally
  instead of as a crashlooping container.
- Migrations run at API container start, so a deploy whose image migrates successfully and
  then fails to start cannot be rolled back (the previous image cannot resolve the newer
  revision). Roll forward, or revert the schema before letting the rollback finish.
