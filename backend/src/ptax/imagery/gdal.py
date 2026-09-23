"""GDAL/rasterio environment for reading COGs over ``/vsis3/``.

rio-tiler and rasterio open ``s3://bucket/key`` through GDAL, not boto3, so every read
of a stored asset must run inside ``rasterio.Env(**gdal_env(settings))``. Locally that
points GDAL at MinIO; in AWS it is empty and the task role signs. NAIP reads use
``naip_env`` instead: the buckets are requester-pays and live in us-west-2.

rasterio only accepts AWS credentials/endpoints through an ``AWSSession`` (passed as
``session``); the remaining ``AWS_*`` switches are ordinary config options.
"""

from typing import Any

from rasterio.session import AWSSession

from ptax.config import Settings

# Skip the directory listing GDAL would otherwise issue before every open, and never
# probe sidecar files (.aux.xml, .ovr) that do not exist for our COGs.
_COMMON = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
}


def gdal_env(settings: Settings) -> dict[str, Any]:
    env: dict[str, Any] = dict(_COMMON)
    if settings.s3_endpoint_url:
        env["session"] = AWSSession(
            aws_access_key_id=settings.s3_access_key_id or "",
            aws_secret_access_key=settings.s3_secret_access_key or "",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.aws_region,
        )
        env["AWS_VIRTUAL_HOSTING"] = "FALSE"
        env["AWS_HTTPS"] = "YES" if settings.s3_endpoint_url.startswith("https") else "NO"
    return env


def naip_env(settings: Settings) -> dict[str, Any]:
    return {
        **_COMMON,
        "session": AWSSession(requester_pays=True, region_name=settings.naip_aws_region),
    }


def s3_uri(settings: Settings, key: str) -> str:
    return f"s3://{settings.s3_bucket}/{key}"
