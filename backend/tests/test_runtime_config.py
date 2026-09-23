from pathlib import Path

from fastapi.testclient import TestClient

from ptax.config import Settings
from ptax.main import create_app


def test_config_endpoint_is_public_and_exposes_only_client_settings(
    client: TestClient, settings: Settings
) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json() == {
        "cognito_client_id": settings.cognito_client_id,
        "cognito_endpoint_url": settings.cognito_endpoint_url,
        "aws_region": settings.aws_region,
    }


def test_database_url_is_composed_from_parts_when_not_given() -> None:
    settings = Settings(
        database_url=None,
        database_host="db.internal",
        database_port=5432,
        database_name="ptax",
        database_user="ptax",
        database_password="s3cr3t/with:odd@chars",
    )
    assert settings.database_url == (
        "postgresql+psycopg://ptax:s3cr3t%2Fwith%3Aodd%40chars@db.internal:5432/ptax"
    )


def test_empty_endpoint_overrides_mean_unset() -> None:
    """ECS cannot unset an env var, so CDK passes "" to disable the local-stack defaults."""
    settings = Settings(
        s3_endpoint_url="",
        s3_access_key_id="",
        s3_secret_access_key="",
        cognito_endpoint_url="",
    )
    assert settings.s3_endpoint_url is None
    assert settings.s3_access_key_id is None
    assert settings.s3_secret_access_key is None
    assert settings.cognito_endpoint_url is None


def test_explicit_database_url_wins() -> None:
    settings = Settings(database_url="postgresql+psycopg://a:b@c:1/d", database_host="ignored")
    assert settings.database_url == "postgresql+psycopg://a:b@c:1/d"


def test_spa_is_served_with_fallback_when_static_dir_exists(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "index.html").write_text("<html><body>spa</body></html>")
    app = create_app(settings.model_copy(update={"static_dir": str(tmp_path)}))

    with TestClient(app) as c:
        assert c.get("/").text == "<html><body>spa</body></html>"
        assert c.get("/parcels").text == "<html><body>spa</body></html>"
        assert c.get("/assets/app.js").text == "console.log('hi')"
        # API paths never fall back to the SPA shell.
        assert c.get("/api/does-not-exist").status_code == 404
        assert c.get("/api/health").status_code in (200, 503)


def test_no_static_dir_means_no_spa_routes(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings.model_copy(update={"static_dir": str(tmp_path / "missing")}))
    with TestClient(app) as c:
        assert c.get("/").status_code == 404
