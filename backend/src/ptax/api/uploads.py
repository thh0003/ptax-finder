import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from ptax.auth.deps import CurrentUser, require_role
from ptax.db.models import UserRole
from ptax.storage import PRESIGN_EXPIRES_SECONDS, presign_put, upload_key

router = APIRouter(prefix="/uploads")
admin_only = require_role(UserRole.admin)


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    purpose: Literal["parcel_layer", "imagery"]


class UploadOut(BaseModel):
    upload_id: uuid.UUID
    key: str
    url: str
    expires_in: int


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
def create_upload(
    body: UploadIn,
    request: Request,
    user: CurrentUser = Depends(admin_only),
) -> UploadOut:
    """Issue a presigned PUT so the browser writes straight to S3/MinIO.

    The tenant prefix comes from the authenticated user, never from the request body.
    """
    upload_id = uuid.uuid4()
    key = upload_key(user.tenant_id, body.purpose, upload_id, body.filename)
    url = presign_put(request.app.state.settings, key, body.content_type)
    return UploadOut(upload_id=upload_id, key=key, url=url, expires_in=PRESIGN_EXPIRES_SECONDS)
