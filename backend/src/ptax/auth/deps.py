import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.auth.jwt import JWKSCache, TokenError, verify_access_token
from ptax.db.models import User, UserRole
from ptax.db.session import get_db

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: UserRole


def get_jwks(request: Request) -> JWKSCache:
    return request.app.state.jwks


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    jwks: JWKSCache = Depends(get_jwks),
) -> CurrentUser:
    """Resolve the bearer token to a provisioned user; this is the tenant boundary."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = verify_access_token(credentials.credentials, request.app.state.settings, jwks)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = db.execute(select(User).where(User.cognito_sub == claims["sub"])).scalar_one_or_none()
    if user is None:
        # A real Cognito identity that no tenant has provisioned must not get in.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user not provisioned")
    return CurrentUser(id=user.id, tenant_id=user.tenant_id, email=user.email, role=user.role)


def require_role(*roles: UserRole | str) -> Callable[..., CurrentUser]:
    allowed = {UserRole(r) for r in roles}

    def _dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return _dependency
