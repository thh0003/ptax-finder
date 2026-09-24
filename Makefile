.PHONY: dev-up dev-down seed api worker web imagery-fixtures eval-fetch eval-score test test-backend test-frontend test-infra

## Local dev stack (PostGIS, MinIO, cognito-local)
dev-up:
	docker compose up -d --wait db minio cognito-local
	docker compose run --rm minio-init
	# The postgis image restarts once after first-run init; wait until it stays ready.
	@for i in $$(seq 1 30); do docker compose exec -T db pg_isready -q -U ptax && sleep 2 && docker compose exec -T db pg_isready -q -U ptax && break; sleep 1; done

dev-down:
	docker compose down -v

## Local user pool + schema + demo tenants/users (idempotent)
seed:
	cd backend && uv run ptax-admin bootstrap-local-cognito --backend-env .env
	cd backend && uv run alembic upgrade head
	cd backend && uv run ptax-admin seed-local

## Run services on the host
api:
	cd backend && uv run uvicorn ptax.main:app --reload --port 8000

worker:
	cd backend && uv run python -m ptax.worker

web:
	cd frontend && pnpm dev

## Regenerate the synthetic imagery fixtures (also the local NAIP "fixture" source)
imagery-fixtures:
	cd backend && uv run python tests/fixtures/make_imagery_fixtures.py

## Detector evaluation on real imagery (needs network, no AWS credentials; see backend/eval/README.md)
EVAL_SET ?= eval/nw-hennepin-2010-2021.json

eval-fetch:
	cd backend && uv run ptax-eval fetch $(EVAL_SET) --verify

# Scored against the visual labels, which are the truth: BUILD_YR records when a principal
# structure was registered, not whether the two captures differ. Override EVAL_LABELS with
# an empty value to see the older BUILD_YR figures.
EVAL_LABELS ?= --labels eval/visual-labels-nw-hennepin-2010-2021.json
eval-score:
	cd backend && uv run ptax-eval score $(EVAL_SET) $(EVAL_LABELS)

## Tests
test: test-backend test-frontend

test-backend:
	cd backend && uv run pytest -q

test-frontend:
	cd frontend && pnpm test

test-infra:
	cd infra && pnpm test && pnpm cdk synth --quiet

## Production image (same image serves the API+SPA and, with a different command, the worker)
# `--target app`: the Dockerfile's last stage is the worker, which is Docker's default.
image:
	docker build --target app -f backend/Dockerfile -t ptax-finder:local .

# The raster stack loads shared libraries the slim base image does not ship (GDAL needs
# libexpat), and an import failure only shows up as a crashlooping container. Check the
# built image before deploying it.
image-check: image
	docker run --rm --entrypoint python ptax-finder:local -c \
		"import rasterio, rio_tiler, rio_cogeo, pyogrio, geopandas; import ptax.main; import importlib.util as u; assert u.find_spec('torch') is None, 'torch in the API image'; print('image imports ok')"

# Runs the built image on the compose network against the dev database.
image-run:
	docker run --rm -p 8080:8000 --network ptax-finder_default \
		-e DATABASE_HOST=db -e S3_ENDPOINT_URL=http://minio:9000 -e COGNITO_ENDPOINT_URL=http://cognito-local:9229 \
		-e PTAX_RUN_MIGRATIONS=1 ptax-finder:local
