import uuid
from datetime import datetime
from typing import Annotated

from botocore.exceptions import ClientError
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ptax.auth.cognito import CognitoAdmin, get_cognito
from ptax.auth.deps import CurrentUser, require_role
from ptax.db.models import User, UserRole
from ptax.db.session import get_db

router = APIRouter(prefix="/users")

admin_only = require_role(UserRole.admin)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    created_at: datetime


def _normalize_email(value: str) -> str:
    # test_environment permits reserved names such as @demo.test used by the local seed;
    # deliverability is not checked because Cognito owns delivery.
    try:
        return validate_email(value, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc


class InviteIn(BaseModel):
    email: Annotated[str, AfterValidator(_normalize_email)]
    role: UserRole


@router.get("", response_model=list[UserOut])
def list_users(
    user: CurrentUser = Depends(admin_only), db: Session = Depends(get_db)
) -> list[User]:
    stmt = select(User).where(User.tenant_id == user.tenant_id).order_by(User.email)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def invite_user(
    body: InviteIn,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
    cognito: CognitoAdmin = Depends(get_cognito),
) -> User:
    """Create the Cognito user (Cognito emails the temporary password) and bind it to the tenant."""
    email = body.email.lower()
    # Emails are globally unique because Cognito usernames are; check before touching Cognito.
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that email already has an account")

    try:
        sub = cognito.admin_create_user(email)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "UsernameExistsException":
            raise HTTPException(
                status.HTTP_409_CONFLICT, "that email already has an account"
            ) from exc
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"cognito: {error.get('Message', 'unknown error')}"
        ) from exc

    new_user = User(tenant_id=user.tenant_id, cognito_sub=sub, email=email, role=body.role)
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Leave no orphaned Cognito identity behind so a retry is clean.
        cognito.admin_delete_user(email)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "could not save user; retry"
        ) from exc
    db.refresh(new_user)
    return new_user
