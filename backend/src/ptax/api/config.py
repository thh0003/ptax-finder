from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class ClientConfig(BaseModel):
    """Public settings the SPA needs before anyone signs in (none are secrets)."""

    cognito_client_id: str
    cognito_endpoint_url: str | None
    aws_region: str


@router.get("/config", response_model=ClientConfig)
def client_config(request: Request) -> ClientConfig:
    settings = request.app.state.settings
    return ClientConfig(
        cognito_client_id=settings.cognito_client_id,
        cognito_endpoint_url=settings.cognito_endpoint_url,
        aws_region=settings.aws_region,
    )
