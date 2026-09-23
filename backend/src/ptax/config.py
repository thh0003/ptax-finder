from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Defaults match the docker-compose dev stack so ``make api`` works with no
    ``.env``; every value is overridden by an environment variable of the same
    name (upper-cased) in AWS.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Either a full URL, or the parts below (AWS injects the password from the
    # Aurora secret, which has no URL field, so the URL is composed at startup).
    database_url: str | None = None
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "ptax"
    database_user: str = "ptax"
    database_password: str = "ptax"

    s3_bucket: str = "ptax-uploads"
    # Set for MinIO locally; unset in AWS so boto3 talks to real S3 with the task role.
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_access_key_id: str | None = "minioadmin"
    s3_secret_access_key: str | None = "minioadmin"
    aws_region: str = "us-east-1"

    cognito_user_pool_id: str = "local_pool"
    cognito_client_id: str = "local_client"
    # Set for cognito-local; unset in AWS.
    cognito_endpoint_url: str | None = "http://localhost:9229"
    cognito_issuer: str = "http://localhost:9229/local_pool"

    # Directory of the built SPA; served at "/" only when it exists (production image).
    static_dir: str = "/app/static"

    # NAIP discovery/ingest. "fixture" serves the committed synthetic COGs so the whole
    # imagery flow runs locally with no AWS credentials; CDK sets "stac" in AWS.
    naip_source: Literal["stac", "fixture"] = "fixture"
    naip_stac_url: str = "https://earth-search.aws.element84.com/v1"
    naip_fixture_dir: str = "tests/fixtures/imagery"
    naip_max_ingest_gb: float = 60
    # The NAIP buckets are requester-pays in us-west-2 regardless of where we run.
    naip_aws_region: str = "us-west-2"

    @field_validator(
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "cognito_endpoint_url",
        mode="before",
    )
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        # ECS cannot unset an environment variable, so "" disables a local-stack default.
        return None if value == "" else value

    @model_validator(mode="after")
    def _compose_database_url(self) -> "Settings":
        if self.database_url is None:
            self.database_url = (
                f"postgresql+psycopg://{quote(self.database_user, safe='')}:"
                f"{quote(self.database_password, safe='')}@{self.database_host}:"
                f"{self.database_port}/{self.database_name}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
