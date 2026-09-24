"""A county's pipeline configuration: where its imagery, parcels and optional footprints
and CAMA live, its portal, CRS, detection thresholds and publish targets.

Stored per tenant in `pipeline.tenant_configs` (see `ptax.tenancy.store`). Credentials are
never part of it: `secret_arn` names the Secrets Manager secret that holds them.
"""

import re
from datetime import date
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

DEFAULT_CLASSES = (
    "primary_structure",
    "garage_outbuilding",
    "shed",
    "pool",
    "deck_patio",
    "driveway_paved",
    "solar_array",
    "ag_building",
)

_SECRET_ARN = re.compile(r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _https(url: AnyHttpUrl) -> AnyHttpUrl:
    if url.scheme != "https":
        raise ValueError("must use https")
    return url


class ImagerySource(_Model):
    source: Literal["service", "files"]
    url_or_s3_uri: str
    year: int = Field(ge=1900, le=2100)
    capture_date: date | None = None
    gsd_ft: float = Field(gt=0)
    bands: list[str] = Field(min_length=3)

    @model_validator(mode="after")
    def _location_matches_source(self) -> "ImagerySource":
        if self.source == "service" and not self.url_or_s3_uri.startswith("https://"):
            raise ValueError("a service source needs an https URL")
        if self.source == "files" and not self.url_or_s3_uri.startswith("s3://"):
            raise ValueError("a files source needs an s3:// URI")
        return self


class ParcelSource(_Model):
    service_url: AnyHttpUrl
    pin_field: str = Field(min_length=1)
    where_clause: str = "1=1"

    _https_url = field_validator("service_url")(_https)


class FootprintSource(_Model):
    service_url: AnyHttpUrl

    _https_url = field_validator("service_url")(_https)


class CamaSource(_Model):
    s3_uri: str = Field(pattern=r"^s3://.+")
    pin_field: str = Field(min_length=1)
    #: CAMA column -> reconciliation field (`cls`, `area_sqft`, `year`).
    field_mapping: dict[str, str]


class Thresholds(_Model):
    """Detection and reconciliation thresholds; every value tunable per tenant."""

    min_new_area_sqft: float = Field(default=100, gt=0)
    expansion_min_sqft: float = Field(default=100, gt=0)
    expansion_min_pct: float = Field(default=0.10, gt=0, le=1)
    iou_match: float = Field(default=0.3, gt=0, le=1)
    match_tolerance_ft: float = Field(default=3.0, gt=0)
    min_score: float = Field(default=0.5, gt=0, le=1)
    high_confidence_score: float = Field(default=0.8, gt=0, le=1)
    change_agreement_frac: float = Field(default=0.5, gt=0, le=1)
    cama_area_tolerance_pct: float = Field(default=0.25, gt=0, le=1)
    reg_shift_tolerance_ft: float = Field(default=2.0, gt=0)
    chip_buffer_ft: float = Field(default=10.0, gt=0)


class PublishTarget(_Model):
    folder: str = Field(min_length=1)
    detections_item: str = Field(min_length=1)
    parcel_changes_item: str = Field(min_length=1)
    #: Publish every parcel, not only flagged parcels and their detections.
    publish_all: bool = False


class TenantConfig(_Model):
    county_name: str = Field(min_length=1)
    state: str = Field(min_length=2, max_length=2)
    portal_type: Literal["agol", "enterprise"]
    portal_url: AnyHttpUrl
    secret_arn: str
    imagery: dict[int, ImagerySource]
    parcels: ParcelSource
    footprints: FootprintSource | None = None
    cama: CamaSource | None = None
    crs_epsg: int
    classes: list[str] = Field(default_factory=lambda: list(DEFAULT_CLASSES), min_length=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    publish: PublishTarget

    _https_url = field_validator("portal_url")(_https)

    @field_validator("secret_arn")
    @classmethod
    def _is_a_secret_arn(cls, value: str) -> str:
        if not _SECRET_ARN.match(value):
            # Never echo the value: someone may have pasted a credential here.
            raise ValueError("must be a Secrets Manager secret ARN")
        return value

    @field_validator("crs_epsg")
    @classmethod
    def _is_projected(cls, value: int) -> int:
        try:
            crs = CRS.from_epsg(value)
        except CRSError as exc:
            raise ValueError(f"EPSG:{value} is not a known CRS") from exc
        if not crs.is_projected:
            raise ValueError(f"EPSG:{value} is not projected; areas need a projected CRS")
        return value

    @field_validator("imagery")
    @classmethod
    def _two_consistent_years(cls, value: dict[int, ImagerySource]) -> dict[int, ImagerySource]:
        if len(value) < 2:
            raise ValueError("configure at least two imagery years")
        for year, source in value.items():
            if source.year != year:
                raise ValueError(f"imagery[{year}] describes year {source.year}")
        return value
