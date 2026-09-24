"""County profiles: which ArcGIS services hold a county's parcels, areas and imagery.

A profile is a JSON file under ``backend/counties/``. Its parcel ``fields`` map is an
allowlist: only the county fields named there are requested from the service, and they
are stored under their mapped names. Owner and address fields are simply never mapped.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CountyArea:
    """A named part of the county: a feature-service layer plus the ``where`` selecting it."""

    url: str
    where: str


@dataclass(frozen=True)
class CountyProfile:
    name: str
    state: str
    parcel_url: str
    #: The county field holding the parcel number.
    id_field: str
    #: County field name -> stored name. The only parcel fields ever requested.
    fields: dict[str, str]
    areas: dict[str, CountyArea]
    #: Imagery year -> ArcGIS MapServer URL of that year's cached orthophoto.
    imagery: dict[int, str]

    @property
    def stored_id_field(self) -> str:
        return self.fields[self.id_field]


def load_profile(path: Path) -> CountyProfile:
    raw = json.loads(path.read_text())
    parcels = raw["parcels"]
    fields = dict(parcels["fields"])
    if parcels["id_field"] not in fields:
        raise ValueError(f"{path}: id_field {parcels['id_field']!r} is not in fields")
    return CountyProfile(
        name=raw["name"],
        state=raw["state"],
        parcel_url=parcels["url"].rstrip("/"),
        id_field=parcels["id_field"],
        fields=fields,
        areas={
            key: CountyArea(url=area["url"].rstrip("/"), where=area["where"])
            for key, area in raw["areas"].items()
        },
        imagery={int(year): url.rstrip("/") for year, url in raw["imagery"].items()},
    )
