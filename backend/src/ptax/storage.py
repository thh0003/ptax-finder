"""S3 access (MinIO locally) and presigned-upload helpers."""

import re
import uuid
from typing import Any

import boto3
from botocore.config import Config

from ptax.config import Settings

UPLOAD_PURPOSES = ("parcel_layer", "imagery")
PRESIGN_EXPIRES_SECONDS = 900


def s3_client_kwargs(settings: Settings) -> dict[str, Any]:
    """boto3 client arguments.

    Static credentials and a custom endpoint are used only for MinIO; in AWS the
    task role signs requests and the endpoint is the regional S3 service.
    """
    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
        # Path-style addressing keeps presigned URLs valid for MinIO and S3 alike.
        "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
        kwargs["aws_access_key_id"] = settings.s3_access_key_id
        kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
    return kwargs


def get_s3_client(settings: Settings):  # noqa: ANN201 - boto3 clients are untyped
    return boto3.client("s3", **s3_client_kwargs(settings))


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    """Keep the basename, replace anything outside [A-Za-z0-9._-] with underscores."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE.sub("_", base)
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = re.sub(r"_*\._*", ".", cleaned).strip("._")
    return cleaned or "upload"


def upload_key(tenant_id: uuid.UUID, purpose: str, upload_id: uuid.UUID, filename: str) -> str:
    return f"tenants/{tenant_id}/{purpose}/{upload_id}/{sanitize_filename(filename)}"


def imagery_asset_key(tenant_id: uuid.UUID, year_id: uuid.UUID, asset_id: uuid.UUID) -> str:
    """Where a stored imagery COG lives (the upload prefix has an upload id instead)."""
    return f"tenants/{tenant_id}/imagery/{year_id}/{asset_id}.tif"


def presign_put(settings: Settings, key: str, content_type: str) -> str:
    return get_s3_client(settings).generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGN_EXPIRES_SECONDS,
    )


class TenantKeyError(ValueError):
    """An object key outside the tenant's own prefix."""


def _tenant_prefix(tenant_id: uuid.UUID) -> str:
    return f"tenants/{tenant_id}/"


def assert_tenant_key(tenant_id: uuid.UUID, key: str) -> None:
    """Refuse any key that is not strictly inside ``tenants/<tenant_id>/``."""
    prefix = _tenant_prefix(tenant_id)
    parts = key.split("/")
    if not key.startswith(prefix) or key == prefix or ".." in parts or "." in parts:
        raise TenantKeyError(f"{key!r} is outside tenant {tenant_id}'s prefix")


def pipeline_key(tenant_id: uuid.UUID, *parts: str) -> str:
    """A pipeline object key under the tenant's prefix, e.g. ``tenants/<id>/raw/2015/x.tif``."""
    key = _tenant_prefix(tenant_id) + "/".join(parts)
    assert_tenant_key(tenant_id, key)
    return key
