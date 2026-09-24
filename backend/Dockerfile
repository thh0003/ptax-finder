# Build context is the repository root:  docker build -f backend/Dockerfile .
# Stage 1: build the SPA. Stage 2: install the backend and serve the SPA from it.

# Built on the host's native platform: it only emits static files, and Node under QEMU
# (amd64 image built on an arm64 Mac) crashes with a libuv assertion.
FROM --platform=$BUILDPLATFORM node:26-alpine AS web
WORKDIR /web
RUN npm install -g pnpm@11
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# rasterio's wheel bundles GDAL, but GDAL links against the system libexpat, which the
# slim image does not ship (pyogrio's wheel does not need it, hence only rasterio fails).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so source edits do not invalidate the layer.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

COPY --from=web /web/dist /app/static
RUN chmod +x /app/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" STATIC_DIR=/app/static PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# The worker service overrides this with: python -m ptax.worker
CMD ["uvicorn", "ptax.main:app", "--host", "0.0.0.0", "--port", "8000"]
