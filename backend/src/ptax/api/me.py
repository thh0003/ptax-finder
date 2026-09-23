import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ptax.auth.deps import CurrentUser, get_current_user
from ptax.db.models import Tenant
from ptax.db.session import get_db

router = APIRouter()


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    state: str
    fips: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    tenant: TenantOut


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> MeOut:
    tenant = db.get_one(Tenant, user.tenant_id)
    return MeOut(
        id=user.id,
        email=user.email,
        role=user.role.value,
        tenant=TenantOut(id=tenant.id, name=tenant.name, state=tenant.state, fips=tenant.fips),
    )
