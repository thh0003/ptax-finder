"""Parcel layer jobs: inspect an uploaded file, then ingest it into ``parcels``.

Both handlers own the layer's status transitions. A failure of any kind leaves the
layer ``failed`` with a human-readable ``error`` (committed), then re-raises so the
worker records the job as failed too.
"""

import math
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pyogrio
import shapely
from psycopg.types.json import Jsonb
from sqlalchemy.orm import Session

from ptax.config import get_settings
from ptax.db.models import Job, ParcelLayer, Tenant
from ptax.jobs.queue import enqueue
from ptax.jobs.registry import job_handler
from ptax.storage import get_s3_client

CHUNK_SIZE = 5000
SAMPLE_ROWS = 5
SAMPLES_PER_FIELD = 3
DATASET_SUFFIXES = (".geojson", ".json")


class LayerError(Exception):
    """A problem with the uploaded file that the admin needs to see."""


def _download(key: str, into: Path) -> Path:
    settings = get_settings()
    target = into / Path(key).name
    get_s3_client(settings).download_file(settings.s3_bucket, key, str(target))
    return target


def resolve_dataset(path: Path) -> Path:
    """Return the readable dataset: a .geojson/.json file, or the single .shp inside a zip."""
    suffix = path.suffix.lower()
    if suffix in DATASET_SUFFIXES:
        return path
    if suffix != ".zip":
        raise LayerError(f"unsupported file type {suffix or '(none)'}: upload .zip or .geojson")

    extract_dir = path.parent / "extracted"
    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.namelist() if not m.startswith("__MACOSX/")]
        zf.extractall(extract_dir, members=members)
    shapefiles = sorted(extract_dir.rglob("*.shp"))
    geojsons = sorted(p for p in extract_dir.rglob("*") if p.suffix.lower() in DATASET_SUFFIXES)
    if len(shapefiles) == 1:
        return shapefiles[0]
    if len(shapefiles) > 1:
        raise LayerError(f"multiple shapefiles in archive: {[p.name for p in shapefiles]}")
    if len(geojsons) == 1:
        return geojsons[0]
    raise LayerError("no shapefile or GeoJSON found in archive")


def _fail_layer(db: Session, layer: ParcelLayer, message: str) -> None:
    db.rollback()
    layer.status = "failed"
    layer.error = message[:2000]
    db.commit()


def _to_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    if isinstance(value, int | float | str | bool):
        return value
    return str(value)


@job_handler("parcel_layer.inspect")
def inspect_layer(db: Session, job: Job) -> None:
    layer = db.get_one(ParcelLayer, uuid.UUID(job.payload["layer_id"]))
    layer.status = "inspecting"
    db.commit()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = resolve_dataset(_download(layer.s3_key, Path(tmp)))
            info = pyogrio.read_info(dataset)
            sample = pyogrio.read_dataframe(dataset, max_features=SAMPLE_ROWS)
            fields = []
            for name, dtype in zip(info["fields"], info["dtypes"], strict=True):
                samples = [
                    str(_to_json_safe(v))
                    for v in sample[name].head(SAMPLES_PER_FIELD)
                    if _to_json_safe(v) is not None
                ]
                fields.append({"name": str(name), "dtype": str(dtype), "samples": samples})
            layer.fields = fields
            layer.source_crs = str(info["crs"]) if info["crs"] else None
            layer.feature_count = int(info["features"])
            layer.status = "awaiting_field"
            # A county layer arrives with its id field already chosen: ingest it directly.
            if layer.parcel_id_field is not None:
                if layer.parcel_id_field not in {f["name"] for f in fields}:
                    raise LayerError(f"field {layer.parcel_id_field!r} not found in layer")
                layer.status = "ingesting"
                enqueue(db, "parcel_layer.ingest", layer.tenant_id, {"layer_id": str(layer.id)})
            db.commit()
    except LayerError as exc:
        _fail_layer(db, layer, str(exc))
        raise
    except Exception as exc:
        _fail_layer(db, layer, f"could not read file: {exc}")
        raise


@job_handler("parcel_layer.ingest")
def ingest_layer(db: Session, job: Job) -> None:
    layer = db.get_one(ParcelLayer, uuid.UUID(job.payload["layer_id"]))
    field = layer.parcel_id_field
    if not field:
        _fail_layer(db, layer, "no parcel id field selected")
        raise LayerError("no parcel id field selected")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = resolve_dataset(_download(layer.s3_key, Path(tmp)))
            info = pyogrio.read_info(dataset)
            if not info["crs"]:
                raise LayerError("layer has no coordinate reference system")
            total = int(info["features"])
            skipped = _copy_parcels(db, layer, dataset, field, total)
        layer.skipped_count = skipped
        layer.feature_count = total
        layer.status = "ready"
        tenant = db.get_one(Tenant, layer.tenant_id)
        tenant.current_parcel_layer_id = layer.id
        # Imagery coverage stats describe the current layer; re-derive them for the new one.
        enqueue(db, "imagery.recompute_coverage", tenant.id, {"tenant_id": str(tenant.id)})
        db.commit()
    except LayerError as exc:
        _fail_layer(db, layer, str(exc))
        raise
    except Exception as exc:
        _fail_layer(db, layer, f"ingest failed: {exc}")
        raise


def _copy_parcels(db: Session, layer: ParcelLayer, dataset: Path, field: str, total: int) -> int:
    """Stream the dataset into ``parcels`` via COPY in chunks; return the skipped-row count."""
    seen_refs: set[str] = set()
    skipped = 0
    raw = db.connection().connection.driver_connection  # psycopg Connection
    assert raw is not None
    copy_sql = "COPY parcels (id, tenant_id, layer_id, parcel_ref, geom, attributes) FROM STDIN"
    for offset in range(0, max(total, 1), CHUNK_SIZE):
        gdf = pyogrio.read_dataframe(dataset, skip_features=offset, max_features=CHUNK_SIZE)
        if gdf.empty:
            break
        if field not in gdf.columns:
            raise LayerError(f"field {field!r} not found in layer")
        gdf = gdf.to_crs(4326)
        attrs = gdf.drop(columns="geometry")
        with raw.cursor() as cur, cur.copy(copy_sql) as copy:
            for idx, geom in zip(gdf.index, gdf.geometry, strict=True):
                ref = attrs.at[idx, field]
                ref = (
                    None
                    if ref is None or (isinstance(ref, float) and math.isnan(ref))
                    else str(ref).strip()
                )
                if not ref or ref in seen_refs:
                    skipped += 1
                    continue
                if (
                    geom is None
                    or geom.is_empty
                    or geom.geom_type not in ("Polygon", "MultiPolygon")
                ):
                    skipped += 1
                    continue
                seen_refs.add(ref)
                if geom.geom_type == "Polygon":
                    geom = shapely.multipolygons([geom])
                geom = shapely.set_srid(geom, 4326)
                attributes = {k: _to_json_safe(v) for k, v in attrs.loc[idx].items()}
                copy.write_row(
                    (
                        uuid.uuid4(),
                        layer.tenant_id,
                        layer.id,
                        ref,
                        shapely.to_wkb(geom, hex=True, include_srid=True),
                        Jsonb(attributes),
                    )
                )
    return skipped
