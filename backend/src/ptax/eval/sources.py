"""Public data the evaluation harness reads: county parcels and NAIP imagery.

All network access for ``ptax.eval`` lives here.

Parcels come from Hennepin County's open ArcGIS service, which carries ``BUILD_YR`` —
the assessor's year-built for a parcel's principal structure, and the label source for
the whole harness. The query restricts ``outFields`` to the four attributes allowed in a
committed set file, so owner and address fields never enter the repository.

Imagery comes from Microsoft's Planetary Computer rather than the requester-pays
``naip-analytic`` bucket that production uses. The COGs are the same NAIP products, but
Planetary Computer signs them with a free short-lived token, so the harness runs on a
developer machine with no AWS credentials. Nothing in ``ptax`` outside this package reads
a Planetary Computer URL; ``ptax.imagery.naip.NaipStacSource`` remains the production path.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx

PARCEL_SERVICE = (
    "https://gis.hennepin.us/arcgis/rest/services"
    "/HennepinData/LAND_PROPERTY/MapServer/1/query"
)
STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/naip"

#: The service caps a single response at 2000 features.
PAGE_SIZE = 2000
MAX_PAGES = 50
TIMEOUT_SECONDS = 120.0

#: Only these attributes are requested, so only these can ever be committed.
OUT_FIELDS = "PID,BUILD_YR,PARCEL_AREA,STATE_CD"


class SourceError(Exception):
    """A public data source could not be queried."""


@dataclass(frozen=True)
class NaipItem:
    """One NAIP quarter-quad from the catalog."""

    id: str
    year: int
    href: str
    gsd_m: float
    epsg: int
    bbox: tuple[float, float, float, float]


def _get_json(client: httpx.Client, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"{url}: {exc or type(exc).__name__}") from exc


def fetch_parcels(bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Every parcel intersecting ``bbox``, as flat dicts with a ``geometry`` key.

    Paged on ``resultOffset`` and ordered by ``OBJECTID``; without an explicit order the
    service does not promise page-to-page stability, which would make the built set
    depend on request timing.
    """
    envelope = (
        f'{{"xmin":{bbox[0]},"ymin":{bbox[1]},"xmax":{bbox[2]},"ymax":{bbox[3]},'
        f'"spatialReference":{{"wkid":4326}}}}'
    )
    parcels: list[dict[str, Any]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for page in range(MAX_PAGES):
            payload = _get_json(
                client,
                PARCEL_SERVICE,
                params={
                    "geometry": envelope,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": 4326,
                    "outSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": OUT_FIELDS,
                    "orderByFields": "OBJECTID",
                    "returnGeometry": "true",
                    "resultOffset": page * PAGE_SIZE,
                    "resultRecordCount": PAGE_SIZE,
                    "f": "geojson",
                },
            )
            features = payload.get("features") or []
            for feature in features:
                properties = feature.get("properties") or {}
                parcels.append({**properties, "geometry": feature.get("geometry")})
            if len(features) < PAGE_SIZE:
                return parcels
    raise SourceError(f"parcel query exceeded {MAX_PAGES} pages")


def county_build_year_counts(base_year: int, target_year: int) -> dict[str, int]:
    """County-wide parcel counts per stratum: the population every rate is weighted to.

    The sample draws a fixed quota from each stratum, so carrying a rate back to the county
    needs each stratum's *population* size, not just the overall positive share — the two
    negative strata are flagged at different rates and are present in very different
    numbers.

    ``BUILD_YR`` is a *text* column, so every bound is quoted: a numeric comparison matches
    nothing on this service and would silently report a base rate of zero.
    """
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:

        def count(where: str) -> int:
            payload = _get_json(
                client,
                PARCEL_SERVICE,
                params={"where": where, "returnCountOnly": "true", "f": "json"},
            )
            if "count" not in payload:
                raise SourceError(f"count query returned no count for {where!r}")
            return int(payload["count"])

        return {
            "total": count("1=1"),
            "positive": count(f"BUILD_YR > '{base_year}' AND BUILD_YR <= '{target_year}'"),
            "negative_old": count(f"BUILD_YR > '0000' AND BUILD_YR <= '{base_year}'"),
            "negative_future": count(f"BUILD_YR > '{target_year}'"),
            "no_year": count("BUILD_YR = '0000' OR BUILD_YR IS NULL"),
            "base_year": base_year,
            "target_year": target_year,
        }


def naip_items(bbox: tuple[float, float, float, float], year: int) -> list[NaipItem]:
    """NAIP items covering ``bbox`` for one year, newest catalog entry first."""
    body = {
        "collections": ["naip"],
        "bbox": list(bbox),
        "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
        "limit": 100,
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.post(STAC_SEARCH, json=body)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"{STAC_SEARCH}: {exc or type(exc).__name__}") from exc

    items: list[NaipItem] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        image = feature.get("assets", {}).get("image", {})
        try:
            items.append(
                NaipItem(
                    id=feature["id"],
                    year=int(properties["naip:year"]),
                    href=image["href"],
                    gsd_m=float(properties["gsd"]),
                    epsg=int(properties["proj:epsg"]),
                    bbox=tuple(feature["bbox"]),  # type: ignore[arg-type]
                )
            )
        except (KeyError, TypeError, ValueError):
            # An item missing the fields we need cannot be read; skip it rather than fail
            # discovery for the whole AOI.
            continue
    return sorted((i for i in items if i.year == year), key=lambda i: i.id)


class SasSigner:
    """Signs Planetary Computer blob URLs, refreshing the token before it expires.

    Tokens last about an hour, which a full fetch over a few hundred parcels can outrun.
    """

    #: Refresh this long before the stated expiry rather than racing it.
    REFRESH_MARGIN_SECONDS = 300.0

    def __init__(self, ttl_seconds: float = 3000.0) -> None:
        self._token: str | None = None
        self._expires_at = 0.0
        self._ttl = ttl_seconds

    def sign(self, href: str) -> str:
        if self._token is None or time.monotonic() >= self._expires_at:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                payload = _get_json(client, SAS_TOKEN)
            token = payload.get("token")
            if not token:
                raise SourceError("SAS token response carried no token")
            self._token = token
            self._expires_at = time.monotonic() + self._ttl - self.REFRESH_MARGIN_SECONDS
        separator = "&" if "?" in href else "?"
        return f"{href}{separator}{self._token}"
