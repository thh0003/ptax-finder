import httpx
from fastapi.testclient import TestClient

from ptax.config import Settings
from ptax.storage import get_s3_client, s3_client_kwargs


def test_presigned_put_stores_object_under_tenant_prefix(
    client: TestClient, settings: Settings, tenant_with_admin
) -> None:
    response = client.post(
        "/api/uploads",
        json={
            "filename": "my parcels (2024).zip",
            "content_type": "application/zip",
            "purpose": "parcel_layer",
        },
        headers=tenant_with_admin["admin_headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    tenant_id = str(tenant_with_admin["tenant"].id)
    assert body["key"].startswith(f"tenants/{tenant_id}/parcel_layer/{body['upload_id']}/")
    assert body["key"].endswith("/my_parcels_2024.zip")
    assert body["expires_in"] == 900

    put = httpx.put(
        body["url"], content=b"PK\x03\x04fake", headers={"Content-Type": "application/zip"}
    )
    assert put.status_code == 200, put.text

    head = get_s3_client(settings).head_object(Bucket=settings.s3_bucket, Key=body["key"])
    assert head["ContentLength"] == 8


def test_unknown_purpose_is_422(client: TestClient, tenant_with_admin) -> None:
    response = client.post(
        "/api/uploads",
        json={"filename": "x.zip", "content_type": "application/zip", "purpose": "malware"},
        headers=tenant_with_admin["admin_headers"],
    )
    assert response.status_code == 422


def test_reviewer_cannot_upload(client: TestClient, tenant_with_admin) -> None:
    response = client.post(
        "/api/uploads",
        json={"filename": "x.zip", "content_type": "application/zip", "purpose": "parcel_layer"},
        headers=tenant_with_admin["reviewer_headers"],
    )
    assert response.status_code == 403


def test_s3_client_kwargs_only_use_static_credentials_for_custom_endpoint(
    settings: Settings,
) -> None:
    local = s3_client_kwargs(settings)
    assert local["endpoint_url"] == settings.s3_endpoint_url
    assert local["aws_access_key_id"] == settings.s3_access_key_id

    aws = s3_client_kwargs(settings.model_copy(update={"s3_endpoint_url": None}))
    # In AWS the task role signs requests; the MinIO defaults must not leak in.
    assert "endpoint_url" not in aws
    assert "aws_access_key_id" not in aws
    assert "aws_secret_access_key" not in aws
