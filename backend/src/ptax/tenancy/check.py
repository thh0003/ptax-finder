"""Is everything a tenant's config points at reachable? One result per item.

Checked: the ArcGIS secret, an OAuth token from the portal (client credentials), the
parcel service, each imagery year (a service answers `?f=json`; a file drop has at least
one object), and the optional footprint service and CAMA extract. Optional items are
reported but never make the check fail. No result ever carries a secret or a token.
"""

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError

from ptax.tenancy.config import TenantConfig
from ptax.tenancy.secrets import SecretError, arcgis_credentials

TIMEOUT_SECONDS = 30.0
Status = Literal["ok", "unreachable", "denied"]


@dataclass(frozen=True)
class CheckResult:
    item: str
    status: Status
    reason: str
    required: bool


def http_client() -> httpx.Client:
    """The client every check uses; tests replace it with a mock transport."""
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _service(http: httpx.Client, url: str, token: str | None) -> tuple[Status, str]:
    params = {"f": "json", **({"token": token} if token else {})}
    try:
        response = http.get(url, params=params)
    except httpx.HTTPError as exc:
        return "unreachable", type(exc).__name__
    if response.status_code in (401, 403):
        return "denied", f"HTTP {response.status_code}"
    if response.status_code != 200:
        return "unreachable", f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "unreachable", "not a JSON service"
    error = body.get("error") if isinstance(body, dict) else None
    if error:
        code = error.get("code")
        return ("denied" if code in (401, 403, 498, 499) else "unreachable"), f"ArcGIS error {code}"
    return "ok", "answers"


def _s3(s3: Any, uri: str, *, prefix: bool) -> tuple[Status, str]:
    parsed = urlparse(uri)
    bucket, key = parsed.netloc, parsed.path.lstrip("/")
    try:
        if prefix:
            listing = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            return (
                ("ok", "has files") if listing.get("KeyCount", 0) else ("unreachable", "no files")
            )
        s3.head_object(Bucket=bucket, Key=key)
        return "ok", "exists"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "error")
        return ("denied" if code in ("403", "AccessDenied") else "unreachable"), f"S3 {code}"
    except BotoCoreError as exc:
        return "unreachable", type(exc).__name__


def _token(
    http: httpx.Client, config: TenantConfig, secrets: Any
) -> tuple[CheckResult, CheckResult, str | None]:
    try:
        creds = arcgis_credentials(config, secrets)
    except SecretError as exc:
        return (
            CheckResult("secret", "denied", str(exc), True),
            CheckResult("portal token", "unreachable", "no credentials", True),
            None,
        )
    secret = CheckResult("secret", "ok", "readable", True)
    url = f"{str(config.portal_url).rstrip('/')}/sharing/rest/oauth2/token"
    try:
        response = http.post(
            url,
            data={
                "client_id": creds.client_id,
                "client_secret": creds.client_secret.get_secret_value(),
                "grant_type": "client_credentials",
                "f": "json",
            },
        )
        body = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        return secret, CheckResult("portal token", "unreachable", type(exc).__name__, True), None
    token = body.get("access_token") if isinstance(body, dict) else None
    if response.status_code != 200:
        return (
            secret,
            CheckResult("portal token", "unreachable", f"HTTP {response.status_code}", True),
            None,
        )
    if not token:
        code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
        return (
            secret,
            CheckResult("portal token", "denied", f"portal refused (code {code})", True),
            None,
        )
    return secret, CheckResult("portal token", "ok", "issued", True), str(token)


def check_config(config: TenantConfig, *, secrets: Any = None, s3: Any = None) -> list[CheckResult]:
    with http_client() as http:
        secret, token_result, token = _token(http, config, secrets)
        results = [secret, token_result]
        status, reason = _service(http, str(config.parcels.service_url), token)
        results.append(CheckResult("parcels", status, reason, True))
        for year, source in sorted(config.imagery.items()):
            if source.source == "service":
                status, reason = _service(http, source.url_or_s3_uri, token)
            else:
                s3 = s3 or boto3.client("s3")
                status, reason = _s3(s3, source.url_or_s3_uri, prefix=True)
            results.append(CheckResult(f"imagery {year}", status, reason, True))
        if config.footprints is not None:
            status, reason = _service(http, str(config.footprints.service_url), token)
            results.append(CheckResult("footprints", status, reason, False))
        if config.cama is not None:
            s3 = s3 or boto3.client("s3")
            status, reason = _s3(s3, config.cama.s3_uri, prefix=False)
            results.append(CheckResult("cama", status, reason, False))
    return results
