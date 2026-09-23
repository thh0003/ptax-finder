from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ptax.api import (
    config,
    health,
    imagery,
    me,
    parcel_layers,
    parcels,
    runs,
    tiles,
    uploads,
    users,
)
from ptax.auth.jwt import JWKSCache
from ptax.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="ptax-finder", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.jwks = JWKSCache(settings.cognito_issuer)

    # Importing the handler modules registers the jobs for in-process runs (tests).
    import ptax.detection.run  # noqa: F401
    import ptax.imagery.ingest  # noqa: F401
    import ptax.parcels.ingest  # noqa: F401

    for router in (
        health.router,
        config.router,
        me.router,
        users.router,
        uploads.router,
        parcel_layers.router,
        parcels.router,
        imagery.router,
        tiles.router,
        runs.router,
    ):
        app.include_router(router, prefix="/api")

    _mount_spa(app, Path(settings.static_dir))
    return app


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve the built SPA (production image only); unknown non-API paths get index.html."""
    if not static_dir.is_dir():
        return
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = static_dir / path
        if path and candidate.is_file() and candidate.resolve().is_relative_to(static_dir):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
