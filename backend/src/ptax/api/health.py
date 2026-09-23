from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ptax.db.session import get_engine

router = APIRouter()


@router.get("/health")
def health(request: Request) -> JSONResponse:
    engine = get_engine(request.app.state.settings.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "degraded", "db": "error"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})
