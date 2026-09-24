"""Read one area's parcels from a county's ArcGIS feature service.

The result is a GeoJSON FeatureCollection holding only the profile's mapped fields. It is
uploaded as an ordinary parcel layer, so storage goes through ``ptax.parcels.ingest`` like
any admin upload.
"""

import json
from typing import Any

import httpx
import shapely
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import Polygon, orient

from ptax.county.profile import CountyArea, CountyProfile

#: Feature services commonly cap one response at 2000 features.
PAGE_SIZE = 2000
MAX_PAGES = 500
TIMEOUT_SECONDS = 120.0


class CountySourceError(Exception):
    """A county ArcGIS service could not be read."""


def http_client() -> httpx.Client:
    """The client every county request uses; tests replace it with a mock transport."""
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def arcgis_json(response: httpx.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CountySourceError(f"{response.request.url}: {exc}") from exc
    # ArcGIS reports query errors with HTTP 200 and an ``error`` body.
    if "error" in payload:
        raise CountySourceError(f"{response.request.url}: {payload['error']}")
    return payload


def fetch_area(client: httpx.Client, area: CountyArea) -> BaseGeometry:
    """The area's boundary in EPSG:4326, unioned across all its features."""
    payload = arcgis_json(
        client.get(
            f"{area.url}/query",
            params={
                "where": area.where,
                "outFields": "",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "geojson",
            },
        )
    )
    shapes = [shape(f["geometry"]) for f in payload.get("features") or [] if f.get("geometry")]
    if not shapes:
        raise CountySourceError(f"{area.url}: no features match {area.where!r}")
    return shapely.union_all(shapes)


def _esri_polygon(geometry: BaseGeometry) -> str:
    """An Esri JSON polygon: exterior rings clockwise, holes counter-clockwise."""
    polygons: list[Polygon] = (
        [geometry] if isinstance(geometry, Polygon) else list(getattr(geometry, "geoms", []))
    )
    rings: list[list[list[float]]] = []
    for polygon in polygons:
        oriented = orient(polygon, sign=-1.0)
        rings.append([list(c) for c in oriented.exterior.coords])
        rings.extend([list(c) for c in hole.coords] for hole in oriented.interiors)
    return json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})


def fetch_parcels(
    client: httpx.Client, profile: CountyProfile, area: BaseGeometry
) -> list[dict[str, Any]]:
    """Every parcel intersecting ``area``, as GeoJSON features holding only mapped fields.

    The area geometry is POSTed: a township boundary is far too long for a query string.
    Paging is ordered by the parcel number so pages are stable. The service's own
    intersection is re-checked locally without the service's tolerance, which drops
    parcels that only touch the area's edge from outside.

    A county may store one parcel as several features sharing a parcel number (a lot split
    by a road); those pieces are merged into one feature, since the parcel ingest keeps
    only the first feature per parcel number.
    """
    shapely.prepare(area)
    request = {
        "where": "1=1",
        "geometry": _esri_polygon(area),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(profile.fields),
        "orderByFields": profile.id_field,
        "returnGeometry": "true",
        "f": "geojson",
    }
    features: list[dict[str, Any]] = []
    pieces: dict[str, list[BaseGeometry]] = {}
    for page in range(MAX_PAGES):
        payload = arcgis_json(
            client.post(
                f"{profile.parcel_url}/query",
                data={
                    **request,
                    "resultOffset": page * PAGE_SIZE,
                    "resultRecordCount": PAGE_SIZE,
                },
            )
        )
        batch = payload.get("features") or []
        for feature in batch:
            geometry = feature.get("geometry")
            if not geometry or not area.intersects(parcel := shape(geometry)):
                continue
            properties = feature.get("properties") or {}
            parcel_number = properties.get(profile.id_field)
            if parcel_number is not None and parcel_number in pieces:
                pieces[parcel_number].append(parcel)
                continue
            if parcel_number is not None:
                pieces[parcel_number] = [parcel]
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        stored: properties.get(county) for county, stored in profile.fields.items()
                    },
                }
            )
        if len(batch) < PAGE_SIZE:
            break
    else:
        raise CountySourceError(f"parcel query exceeded {MAX_PAGES} pages")
    stored_id = profile.stored_id_field
    for feature in features:
        split = pieces.get(feature["properties"][stored_id], [])
        if len(split) > 1:
            feature["geometry"] = mapping(shapely.union_all(split))
    return features


def area_parcels_geojson(profile: CountyProfile, area_key: str) -> tuple[bytes, int]:
    """The area's parcels as GeoJSON bytes, and how many there are."""
    with http_client() as client:
        area = fetch_area(client, profile.areas[area_key])
        features = fetch_parcels(client, profile, area)
    collection = {"type": "FeatureCollection", "features": features}
    return json.dumps(collection).encode(), len(features)
