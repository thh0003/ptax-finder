import uuid

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.auth.cognito import CognitoAdmin
from ptax.db.models import Tenant, User, UserRole


def test_admin_lists_only_own_tenant(client: TestClient, db: Session, tenant_with_admin) -> None:
    other = Tenant(name="Other County", state="MN", fips="27001")
    db.add(other)
    db.flush()
    db.add(
        User(
            tenant_id=other.id,
            cognito_sub=str(uuid.uuid4()),
            email="admin@other.test",
            role=UserRole.admin,
        )
    )
    db.flush()

    response = client.get("/api/users", headers=tenant_with_admin["admin_headers"])

    assert response.status_code == 200
    emails = [u["email"] for u in response.json()]
    assert emails == ["admin@demo.test", "reviewer@demo.test"]


def test_reviewer_cannot_manage_users(client: TestClient, tenant_with_admin) -> None:
    headers = tenant_with_admin["reviewer_headers"]
    assert client.get("/api/users", headers=headers).status_code == 403
    body = {"email": "x@demo.test", "role": "reviewer"}
    assert client.post("/api/users", json=body, headers=headers).status_code == 403


def test_invite_creates_cognito_user_and_row(
    client: TestClient, db: Session, tenant_with_admin, cognito_admin: CognitoAdmin
) -> None:
    response = client.post(
        "/api/users",
        json={"email": "new.reviewer@demo.test", "role": "reviewer"},
        headers=tenant_with_admin["admin_headers"],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "new.reviewer@demo.test"
    assert body["role"] == "reviewer"

    row = db.execute(select(User).where(User.email == "new.reviewer@demo.test")).scalar_one()
    assert row.tenant_id == tenant_with_admin["tenant"].id
    assert cognito_admin.get_user("new.reviewer@demo.test")["sub"] == row.cognito_sub


def test_invite_duplicate_email_is_409_without_cognito_user(
    client: TestClient, tenant_with_admin, cognito_admin: CognitoAdmin
) -> None:
    response = client.post(
        "/api/users",
        json={"email": "reviewer@demo.test", "role": "reviewer"},
        headers=tenant_with_admin["admin_headers"],
    )

    assert response.status_code == 409
    with pytest.raises(ClientError):
        cognito_admin.get_user("reviewer@demo.test")


def test_invite_rejects_bad_email_and_role(client: TestClient, tenant_with_admin) -> None:
    headers = tenant_with_admin["admin_headers"]
    assert (
        client.post(
            "/api/users", json={"email": "nope", "role": "admin"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/users", json={"email": "ok@demo.test", "role": "owner"}, headers=headers
        ).status_code
        == 422
    )


def test_invite_cognito_failure_is_502(
    client: TestClient, tenant_with_admin, cognito_admin: CognitoAdmin, monkeypatch
) -> None:
    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalErrorException", "Message": "cognito is down"}},
            "AdminCreateUser",
        )

    monkeypatch.setattr(cognito_admin, "admin_create_user", boom)
    response = client.post(
        "/api/users",
        json={"email": "unlucky@demo.test", "role": "reviewer"},
        headers=tenant_with_admin["admin_headers"],
    )
    assert response.status_code == 502
    assert "cognito is down" in response.json()["detail"]
