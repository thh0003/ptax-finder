import enum
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRole(enum.StrEnum):
    admin = "admin"
    reviewer = "reviewer"


PARCEL_LAYER_STATUSES = ("uploaded", "inspecting", "awaiting_field", "ingesting", "ready", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
IMAGERY_SOURCES = ("naip", "upload", "arcgis")
IMAGERY_YEAR_STATUSES = ("queued", "processing", "ready", "failed")
IMAGERY_ASSET_STATUSES = ("pending", "ready", "failed")
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
RUN_KINDS = ("change", "inventory")
RUN_DETECTORS = ("classical", "segmentation", "vision")


def _in_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(
        "{} IN ({})".format(column, ", ".join(f"'{v}'" for v in values)), name=name
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    # clock_timestamp() (not now()) so rows created in one transaction keep insertion order.
    return mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    fips: Mapped[str] = mapped_column(String(5), nullable=False, unique=True)
    # Circular FK with parcel_layers; created by ALTER TABLE in migration 0001.
    current_parcel_layer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcel_layers.id", use_alter=True, name="tenants_current_parcel_layer_fk"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    cognito_sub: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()


class ParcelLayer(Base):
    __tablename__ = "parcel_layers"
    __table_args__ = (
        CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in PARCEL_LAYER_STATUSES)),
            name="parcel_layers_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="uploaded")
    parcel_id_field: Mapped[str | None] = mapped_column(Text, nullable=True)
    fields: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    source_crs: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skipped_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # County footprint (union of this layer's parcels); filled lazily by footprint_for().
    footprint: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class Parcel(Base):
    __tablename__ = "parcels"
    __table_args__ = (
        UniqueConstraint("layer_id", "parcel_ref", name="parcels_layer_ref_key"),
        Index("parcels_geom_idx", "geom", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    layer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parcel_layers.id"), nullable=False, index=True
    )
    parcel_ref: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in JOB_STATUSES)),
            name="jobs_status_check",
        ),
        Index("jobs_status_created_idx", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _tenant_fk(index: bool = True) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=index)


class ImageryYear(Base):
    """One year of imagery for a tenant, from NAIP, an upload or a county ArcGIS tile cache;
    a set of COG assets."""

    __tablename__ = "imagery_years"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "year", "source", name="imagery_years_tenant_year_source_key"
        ),
        _in_check("source", IMAGERY_SOURCES, "imagery_years_source_check"),
        _in_check("status", IMAGERY_YEAR_STATUSES, "imagery_years_status_check"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    band_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    parcels_uncovered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bounds: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class ImageryAsset(Base):
    """One stored COG of an imagery year (a NAIP quarter-quad or an uploaded file)."""

    __tablename__ = "imagery_assets"
    __table_args__ = (
        _in_check("status", IMAGERY_ASSET_STATUSES, "imagery_assets_status_check"),
        Index("imagery_assets_bounds_idx", "bounds", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk(index=False)
    year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("imagery_years.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # The upload key while pending, the stored COG key once ready.
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    bounds: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False), nullable=True
    )
    epsg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    band_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Run(Base):
    """A run over the parcel layer current at start time: a base-year vs target-year
    comparison (``change``), or a single-year structure ``inventory`` (0007), whose one
    year is ``base_year_id`` and whose ``target_year_id`` is NULL."""

    __tablename__ = "runs"
    __table_args__ = (
        _in_check("status", RUN_STATUSES, "runs_status_check"),
        _in_check("kind", RUN_KINDS, "runs_kind_check"),
        _in_check("detector", RUN_DETECTORS, "runs_detector_check"),
        CheckConstraint(
            "(detector = 'segmentation') = (model_sha256 IS NOT NULL)", name="runs_model_check"
        ),
        CheckConstraint(
            "(kind = 'inventory') = (target_year_id IS NULL)", name="runs_kind_target_check"
        ),
        CheckConstraint(
            "(kind = 'inventory') = (detector = 'vision')", name="runs_kind_detector_check"
        ),
        Index("runs_tenant_created_idx", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk(index=False)
    layer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parcel_layers.id"), nullable=False
    )
    base_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("imagery_years.id"), nullable=False
    )
    target_year_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("imagery_years.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="change")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    min_new_area_m2: Mapped[float] = mapped_column(Float, nullable=False)
    # Which detector scored the run (0005). A segmenter run also records the model it was
    # given and its weights' sha256. The segmentation detector has since been removed; its
    # value stays allowed so past runs keep their history, and no new run can use it. An
    # inventory run's detector is `vision`, and `model_name` is the vision model.
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    parcels_total: Mapped[int] = mapped_column(Integer, nullable=False)
    parcels_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parcels_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunParcel(Base):
    """Per-parcel result of a run; ``parcel_ref`` survives re-uploads (Plan C joins on it)."""

    __tablename__ = "run_parcels"
    __table_args__ = (
        # Candidate-filtered reads (the review queue). `candidate` sits between the
        # equality and sort columns, so this index only serves queries that pin it.
        Index("run_parcels_queue_idx", "run_id", "candidate", text("score DESC")),
        # The unfiltered score-ordered parcel list. Without this the list sorts the whole
        # run on every page, because PostgreSQL has no skip scan to merge the two
        # `candidate` groups of the index above back into one ordered stream.
        Index(
            "run_parcels_score_idx", "run_id", text("score DESC NULLS LAST"), "parcel_ref"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), primary_key=True
    )
    parcel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parcels.id"), primary_key=True
    )
    parcel_ref: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    indicators: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Where the run found the change, recorded when it scored the parcel. NULL means the
    # run detected nothing, was skipped, or predates this feature -- all three render no
    # markup, which is what keeps an older run's picture agreeing with its stored score.
    # No spatial index: these are read by primary key from the viewer, never searched.
    new_builtup_geom: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True
    )
    structure_geom: Mapped[Any | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True
    )
