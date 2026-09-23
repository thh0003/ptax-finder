import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ptax.db.models import Tenant, User, UserRole


def _provision(db: Session, sub: str, email: str = "admin@t.test") -> Tenant:
    tenant = Tenant(name="Demo County", state="MN", fips="27001")
    db.add(tenant)
    db.flush()
    db.add(User(tenant_id=tenant.id, cognito_sub=sub, email=email, role=UserRole.admin))
    db.flush()
    return tenant


def test_me_returns_user_and_tenant(client: TestClient, db: Session, make_token) -> None:
    sub = str(uuid.uuid4())
    tenant = _provision(db, sub)

    response = client.get("/api/me", headers={"Authorization": f"Bearer {make_token(sub)}"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@t.test"
    assert body["role"] == "admin"
    assert body["tenant"] == {
        "id": str(tenant.id),
        "name": "Demo County",
        "state": "MN",
        "fips": "27001",
    }


def test_missing_token_is_401(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401


def test_expired_token_is_401(client: TestClient, db: Session, make_token) -> None:
    sub = str(uuid.uuid4())
    _provision(db, sub)
    token = make_token(sub, expires_in=-10)
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_wrong_client_id_is_401(client: TestClient, db: Session, make_token) -> None:
    sub = str(uuid.uuid4())
    _provision(db, sub)
    token = make_token(sub, client_id="someone-elses-app")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_id_token_is_rejected(client: TestClient, db: Session, make_token) -> None:
    sub = str(uuid.uuid4())
    _provision(db, sub)
    token = make_token(sub, token_use="id")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unknown_kid_is_401(client: TestClient, db: Session, make_token) -> None:
    sub = str(uuid.uuid4())
    _provision(db, sub)
    token = make_token(sub, kid="rotated-away")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_valid_token_without_user_row_is_403(client: TestClient, make_token) -> None:
    token = make_token(str(uuid.uuid4()))
    response = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["detail"] == "user not provisioned"
