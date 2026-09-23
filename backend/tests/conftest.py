"""Shared fixtures.

Tests run against the docker-compose PostGIS (``make dev-up``); each test gets a
session bound to an outer transaction that is rolled back at teardown, so the
database is left as it was found.
"""

import json
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import boto3
import httpx
import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ptax.auth.cognito import CognitoAdmin, get_cognito
from ptax.auth.jwt import JWKSCache
from ptax.config import Settings
from ptax.db.models import Tenant, User, UserRole
from ptax.db.session import get_db, get_engine
from ptax.main import create_app
from ptax.storage import get_s3_client

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_KID = "test-kid"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Base settings, but pointed at a dedicated ``<db>_test`` database.

    The dev database holds ``make seed`` data (fixed FIPS codes, demo users) that
    would collide with test rows, so tests get their own database on the same server.
    """
    base = Settings()
    url = make_url(base.database_url)
    test_url = url.set(database=f"{url.database}_test").render_as_string(hide_password=False)
    return base.model_copy(
        update={
            "database_url": test_url,
            # Absolute so the fixture NAIP source works whatever the cwd is.
            "naip_fixture_dir": str(Path(__file__).parent / "fixtures" / "imagery"),
        }
    )


@pytest.fixture(scope="session", autouse=True)
def migrated(settings: Settings) -> None:
    """Create the test database if needed and bring it to head once per session."""
    url = make_url(settings.database_url)
    admin_engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        ).first()
        if exists is None:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin_engine.dispose()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@pytest.fixture
def db(settings: Settings) -> Iterator[Session]:
    engine = get_engine(settings.database_url)
    connection: Connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks_document(rsa_key: rsa.RSAPrivateKey) -> dict:
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    public_jwk.update({"kid": TEST_KID, "alg": "RS256", "use": "sig"})
    return {"keys": [public_jwk]}


@pytest.fixture
def jwks(settings: Settings, jwks_document: dict) -> JWKSCache:
    """A JWKS cache whose HTTP fetches are answered by the test keypair."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json=jwks_document)
        return httpx.Response(404)

    return JWKSCache(settings.cognito_issuer, transport=httpx.MockTransport(handler))


@pytest.fixture
def make_token(settings: Settings, rsa_key: rsa.RSAPrivateKey) -> Callable[..., str]:
    """Mint Cognito-shaped access tokens signed with the test key."""

    def _make(
        sub: str,
        *,
        client_id: str | None = None,
        issuer: str | None = None,
        token_use: str = "access",
        expires_in: int = 3600,
        kid: str = TEST_KID,
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": sub,
            "iss": issuer or settings.cognito_issuer,
            "client_id": client_id or settings.cognito_client_id,
            "token_use": token_use,
            "iat": now,
            "exp": now + expires_in,
            "username": sub,
        }
        return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})

    return _make


@pytest.fixture
def app(settings: Settings, db: Session, jwks: JWKSCache):
    application = create_app(settings)
    application.state.jwks = jwks

    def _override_db() -> Iterator[Session]:
        yield db

    application.dependency_overrides[get_db] = _override_db
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def cognito_settings(settings: Settings) -> Iterator[Settings]:
    """Settings pointing at a moto-backed user pool (no local emulator involved)."""
    with mock_aws():
        idp = boto3.client("cognito-idp", region_name="us-east-1")
        pool_id = idp.create_user_pool(PoolName="test")["UserPool"]["Id"]
        client_id = idp.create_user_pool_client(UserPoolId=pool_id, ClientName="web")[
            "UserPoolClient"
        ]["ClientId"]
        yield settings.model_copy(
            update={
                "cognito_endpoint_url": None,
                "cognito_user_pool_id": pool_id,
                "cognito_client_id": client_id,
                "aws_region": "us-east-1",
            }
        )


@pytest.fixture
def cognito_admin(app, cognito_settings: Settings) -> CognitoAdmin:
    """Route the API's Cognito admin calls at the moto pool."""
    admin = CognitoAdmin(cognito_settings)
    app.dependency_overrides[get_cognito] = lambda: admin
    return admin


@pytest.fixture
def tenant_with_admin(db: Session, make_token) -> dict:
    """A tenant, its admin and reviewer users, and bearer headers for each."""
    tenant = Tenant(name="Demo County", state="MN", fips="27053")
    db.add(tenant)
    db.flush()
    admin_sub, reviewer_sub = str(uuid.uuid4()), str(uuid.uuid4())
    admin = User(
        tenant_id=tenant.id, cognito_sub=admin_sub, email="admin@demo.test", role=UserRole.admin
    )
    reviewer = User(
        tenant_id=tenant.id,
        cognito_sub=reviewer_sub,
        email="reviewer@demo.test",
        role=UserRole.reviewer,
    )
    db.add_all([admin, reviewer])
    db.flush()
    return {
        "tenant": tenant,
        "admin": admin,
        "reviewer": reviewer,
        "admin_headers": {"Authorization": f"Bearer {make_token(admin_sub)}"},
        "reviewer_headers": {"Authorization": f"Bearer {make_token(reviewer_sub)}"},
    }


FIXTURES_DIR = Path(__file__).parent / "fixtures"
PARCELS_GEOJSON = FIXTURES_DIR / "parcels_small.geojson"


def drain_queue(db: Session) -> None:
    """Run every queued job in-process (the worker loop without the sleep)."""
    from ptax.worker import run_once

    while run_once(db) is not None:
        pass


def upload_object(settings: Settings, key: str, data: bytes) -> str:
    get_s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=data)
    return key


def ingest_parcels(
    client: TestClient, db: Session, settings: Settings, tenant: Tenant, headers: dict, data: bytes
) -> dict:
    """Upload a GeoJSON parcel layer and run it through inspect + ingest with field PIN."""
    key = upload_object(
        settings, f"tenants/{tenant.id}/parcel_layer/{uuid.uuid4()}/parcels.geojson", data
    )
    response = client.post(
        "/api/parcel-layers",
        json={"upload_key": key, "original_filename": "parcels.geojson"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    layer_id = response.json()["id"]
    drain_queue(db)
    response = client.post(
        f"/api/parcel-layers/{layer_id}/ingest", json={"parcel_id_field": "PIN"}, headers=headers
    )
    assert response.status_code == 202, response.text
    drain_queue(db)
    response = client.get(f"/api/parcel-layers/{layer_id}", headers=headers)
    layer = response.json()
    assert layer["status"] == "ready", layer.get("error")
    return layer


def ingest_naip_year(client: TestClient, db: Session, headers: dict, year: int) -> dict:
    """Ingest a fixture NAIP year through the API + job and return the ready year."""
    response = client.post("/api/imagery/naip/ingest", json={"year": year}, headers=headers)
    assert response.status_code == 201, response.text
    drain_queue(db)
    year_json = client.get(f"/api/imagery/years/{response.json()['id']}", headers=headers).json()
    assert year_json["status"] == "ready", year_json.get("error")
    return year_json


def ingest_upload_year(
    client: TestClient,
    db: Session,
    settings: Settings,
    tenant: Tenant,
    headers: dict,
    year: int,
    files: dict[str, bytes],
    provider: str | None = None,
) -> dict:
    """Create an upload year, register ``files`` (name -> bytes), finalize, and drain."""
    response = client.post(
        "/api/imagery/years", json={"year": year, "provider": provider}, headers=headers
    )
    assert response.status_code == 201, response.text
    year_id = response.json()["id"]
    for name, data in files.items():
        key = upload_object(settings, f"tenants/{tenant.id}/imagery/{uuid.uuid4()}/{name}", data)
        response = client.post(
            f"/api/imagery/years/{year_id}/assets",
            json={"upload_key": key, "original_filename": name},
            headers=headers,
        )
        assert response.status_code == 201, response.text
    response = client.post(f"/api/imagery/years/{year_id}/finalize", headers=headers)
    assert response.status_code == 202, response.text
    drain_queue(db)
    return client.get(f"/api/imagery/years/{year_id}", headers=headers).json()


@pytest.fixture
def ingested_layer(
    client: TestClient, db: Session, settings: Settings, tenant_with_admin: dict
) -> dict:
    """The 25-parcel fixture layer ingested for ``tenant_with_admin`` through the real jobs."""
    return ingest_parcels(
        client,
        db,
        settings,
        tenant_with_admin["tenant"],
        tenant_with_admin["admin_headers"],
        PARCELS_GEOJSON.read_bytes(),
    )
