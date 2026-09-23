from fastapi.testclient import TestClient

from ptax.config import Settings
from ptax.main import create_app


def test_health_ok_with_database(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_health_reports_database_error() -> None:
    # Port 1 is never a Postgres; connect_timeout keeps the failure fast.
    settings = Settings(
        database_url="postgresql+psycopg://ptax:ptax@127.0.0.1:1/ptax?connect_timeout=1"
    )
    app = create_app(settings)
    with TestClient(app) as bad_client:
        response = bad_client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["db"] == "error"
