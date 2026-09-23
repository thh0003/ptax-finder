"""Microsoft's open US building footprints, as per-pixel building labels.

The segmenter learns what a building looks like from these: each training chip is paired
with the footprints rasterised onto that chip's own grid. The footprints are one snapshot,
from imagery dated roughly 2014-2021, so they say "a building is here" and nothing about
when it appeared -- which is why training draws on neighbourhoods built out long before
any capture it uses (see `ptax.eval.training_data`).

The Minnesota file is a 100 MB zip of one GeoJSON. It is fetched once into the evaluation
cache and read in place through GDAL's ``/vsizip/``, filtered by bounding box, so no step
ever unpacks or loads the whole state.
"""

import zipfile
from collections.abc import Sequence
from pathlib import Path

import httpx
import numpy as np
import pyogrio
from affine import Affine
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.features import rasterize
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

FOOTPRINTS_URL = (
    "https://minedbuildings.z5.web.core.windows.net/legacy/usbuildings-v2/"
    "Minnesota.geojson.zip"
)
#: Byte length of the file at FOOTPRINTS_URL, checked 2026-09-23. A cached file of any
#: other length is a partial download and is fetched again.
FOOTPRINTS_BYTES = 100_888_148
FOOTPRINTS_PATH = Path("eval") / "cache" / "footprints" / "Minnesota.geojson.zip"


def ensure_footprints(
    path: Path = FOOTPRINTS_PATH,
    *,
    url: str = FOOTPRINTS_URL,
    expected_bytes: int = FOOTPRINTS_BYTES,
    client: httpx.Client | None = None,
) -> Path:
    """The footprint zip on local disk, downloading it only when it is missing or partial.

    The download streams into a sibling ``.part`` file and is renamed into place once
    complete, so an interrupted fetch can never be mistaken for the real file.
    """
    if path.exists() and path.stat().st_size == expected_bytes:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    owned = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
    try:
        with http.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
    finally:
        if owned:
            http.close()
    size = partial.stat().st_size
    if size != expected_bytes:
        partial.unlink()
        raise RuntimeError(f"{url} returned {size} bytes, expected {expected_bytes}")
    partial.replace(path)
    return path


def _member(zip_path: Path) -> str:
    """The GeoJSON inside the zip; the archive holds exactly one."""
    with zipfile.ZipFile(zip_path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith((".geojson", ".json"))]
    if len(names) != 1:
        raise ValueError(f"expected one GeoJSON in {zip_path}, found {names}")
    return names[0]


def read_footprints(
    zip_path: Path, bbox: tuple[float, float, float, float]
) -> list[BaseGeometry]:
    """Footprints (EPSG:4326) that intersect ``bbox`` (west, south, east, north).

    GDAL's bbox filter matches on each feature's envelope, so an L-shaped building whose
    envelope reaches into the box would come back without touching it. The exact
    intersection test afterwards removes those.
    """
    frame = pyogrio.read_dataframe(f"/vsizip/{zip_path}/{_member(zip_path)}", bbox=bbox)
    query = box(*bbox)
    return [g for g in frame.geometry if g is not None and g.intersects(query)]


def rasterise(
    footprints: Sequence[BaseGeometry],
    transform: Affine,
    shape: tuple[int, int],
    crs: CRS | int | str,
) -> np.ndarray:
    """Burn EPSG:4326 footprints onto a grid, as a boolean building mask.

    A pixel counts as building when its centre falls inside a footprint -- the default
    rasterisation rule, which neither grows nor shrinks a footprint on average, so a
    10 m x 10 m building at 1 m covers about 100 pixels rather than the 121 an
    all-touched rule would give.
    """
    if not footprints:
        return np.zeros(shape, dtype=bool)
    to_grid = Transformer.from_crs(4326, CRS.from_user_input(crs), always_xy=True).transform
    projected = [shapely_transform(to_grid, g) for g in footprints]
    burned = rasterize(
        ((g, 1) for g in projected),
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    )
    return np.asarray(burned, dtype=bool)
