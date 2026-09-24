"""The `pipeline` schema (migration 0008): one run of parcel improvement detection per
tenant and year pair, and everything it produces.

Every table is under row-level security keyed on `tenant_id`; read and write them inside
`ptax.db.tenancy.tenant_scope`. Outside a scope the owner connection sees every tenant,
which is for migrations and operator maintenance only.
"""

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ptax.db.models import Base

SCHEMA = "pipeline"
_ARGS = {"schema": SCHEMA}


def _tenant() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)


def _run() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )


def _geom(nullable: bool = False) -> Mapped[Any]:
    return mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=nullable
    )


class TenantConfigRow(Base):
    __tablename__ = "tenant_configs"
    __table_args__ = _ARGS

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PipelineRun(Base):
    __tablename__ = "runs"
    __table_args__ = _ARGS

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    year_a: Mapped[int] = mapped_column(Integer, nullable=False)
    year_b: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PipelineParcel(Base):
    __tablename__ = "parcels"
    __table_args__ = _ARGS

    run_id: Mapped[uuid.UUID] = _run()
    pin: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    geom: Mapped[Any] = _geom()
    attrs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Detection(Base):
    __tablename__ = "detections"
    __table_args__ = _ARGS

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    pin: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    cls: Mapped[str] = mapped_column("class", Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    area_sqft: Mapped[float] = mapped_column(Float, nullable=False)
    change_type: Mapped[str | None] = mapped_column(Text)
    already_assessed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    geom: Mapped[Any] = _geom()


class DetectionMatch(Base):
    __tablename__ = "detection_matches"
    __table_args__ = _ARGS

    run_id: Mapped[uuid.UUID] = _run()
    a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.detections.id"), primary_key=True
    )
    b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.detections.id"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    iou: Mapped[float] = mapped_column(Float, nullable=False)
    area_delta_sqft: Mapped[float] = mapped_column(Float, nullable=False)


class ParcelChange(Base):
    __tablename__ = "parcel_changes"
    __table_args__ = _ARGS

    run_id: Mapped[uuid.UUID] = _run()
    pin: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    status: Mapped[str] = mapped_column(Text, nullable=False)
    new_sqft_est: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    classes_added: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    change_model_agrees: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    geom: Mapped[Any | None] = _geom(nullable=True)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = _ARGS

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = _tenant()
    pin: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class TileQC(Base):
    __tablename__ = "tile_qc"
    __table_args__ = _ARGS

    run_id: Mapped[uuid.UUID] = _run()
    tile_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    reg_shift_ft: Mapped[float] = mapped_column(Float, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False)


PIPELINE_TABLES: tuple[Table, ...] = tuple(
    Base.metadata.tables[f"{SCHEMA}.{name}"]
    for name in (
        "tenant_configs",
        "runs",
        "parcels",
        "detections",
        "detection_matches",
        "parcel_changes",
        "reviews",
        "tile_qc",
    )
)
