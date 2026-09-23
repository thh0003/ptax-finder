"""Thin boto3 wrapper for the Cognito admin calls the app and CLI make.

Honours ``COGNITO_ENDPOINT_URL`` so the same code drives cognito-local (which
accepts any credentials) and real Cognito (task role / operator credentials).
"""

from typing import Any

import boto3
from fastapi import Request

from ptax.config import Settings


class CognitoAdmin:
    def __init__(self, settings: Settings) -> None:
        self._pool_id = settings.cognito_user_pool_id
        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.cognito_endpoint_url:
            kwargs.update(
                endpoint_url=settings.cognito_endpoint_url,
                aws_access_key_id="local",
                aws_secret_access_key="local",
            )
        self._client = boto3.client("cognito-idp", **kwargs)

    @property
    def client(self):  # noqa: ANN201 - boto3 clients are untyped
        return self._client

    def admin_create_user(
        self,
        email: str,
        *,
        temporary_password: str | None = None,
        suppress_message: bool = False,
    ) -> str:
        """Create the user and return its ``sub``."""
        params: dict[str, Any] = {
            "UserPoolId": self._pool_id,
            "Username": email,
            "UserAttributes": [
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            "DesiredDeliveryMediums": ["EMAIL"],
        }
        if temporary_password:
            params["TemporaryPassword"] = temporary_password
        if suppress_message:
            params["MessageAction"] = "SUPPRESS"
        response = self._client.admin_create_user(**params)
        attributes = {a["Name"]: a["Value"] for a in response["User"].get("Attributes", [])}
        return attributes["sub"]

    def admin_set_user_password(self, email: str, password: str, *, permanent: bool) -> None:
        self._client.admin_set_user_password(
            UserPoolId=self._pool_id, Username=email, Password=password, Permanent=permanent
        )

    def admin_delete_user(self, email: str) -> None:
        self._client.admin_delete_user(UserPoolId=self._pool_id, Username=email)

    def get_user(self, email: str) -> dict[str, Any]:
        response = self._client.admin_get_user(UserPoolId=self._pool_id, Username=email)
        attributes = {a["Name"]: a["Value"] for a in response.get("UserAttributes", [])}
        return {"sub": attributes.get("sub"), "status": response.get("UserStatus"), **attributes}


def get_cognito(request: Request) -> CognitoAdmin:
    """FastAPI dependency; one client per app instance, overridable in tests."""
    state = request.app.state
    if not hasattr(state, "cognito"):
        state.cognito = CognitoAdmin(state.settings)
    return state.cognito
