"""A tenant's ArcGIS OAuth app credentials, from Secrets Manager.

The secret (named `ptax/tenants/<tenant_id>/arcgis`) holds
`{"client_id": ..., "client_secret": ...}`. The secret value is a `SecretStr` so it
never renders in a repr, log line or CLI output, and every error names the secret's ARN,
never its contents.
"""

import json
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, SecretStr, ValidationError

from ptax.tenancy.config import TenantConfig


class SecretError(Exception):
    """The tenant's ArcGIS secret could not be read or is not in the expected shape."""


class ArcgisCredentials(BaseModel):
    client_id: str
    client_secret: SecretStr


def _region(arn: str) -> str:
    return arn.split(":")[3]


def arcgis_credentials(config: TenantConfig, client: Any = None) -> ArcgisCredentials:
    arn = config.secret_arn
    client = client or boto3.client("secretsmanager", region_name=_region(arn))
    try:
        value = client.get_secret_value(SecretId=arn)["SecretString"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "error")
        raise SecretError(f"secret {arn}: {code}") from None
    except BotoCoreError as exc:
        raise SecretError(f"secret {arn}: {type(exc).__name__}") from None
    try:
        return ArcgisCredentials.model_validate(json.loads(value))
    except (ValueError, ValidationError):
        # `from None`: both errors can quote the secret's text.
        raise SecretError(f"secret {arn} is not JSON with client_id and client_secret") from None
