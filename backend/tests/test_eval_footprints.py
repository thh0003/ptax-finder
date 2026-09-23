"""Building footprints as segmentation labels: fetched once, read by bbox, rasterised.

The labels decide what the segmenter learns a building looks like, so a footprint that
lands a few pixels off its roof teaches it the wrong thing everywhere. These tests pin the
reprojection and the rasterisation against footprints whose position and size are known.
No network: the source zip is written into ``tmp_path``.
"""

import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box, mapping
from shapely.ops import transform as shapely_transform

from ptax.eval.footprints import ensure_footprints, rasterise, read_footprints

UTM = 32615
TO_WGS84 = Transformer.from_crs(UTM, 4326, always_xy=True).transform
TO_UTM = Transformer.from_crs(4326, UTM, always_xy=True).transform
# A point in Richfield, MN, in UTM 15N metres.
ORIGIN_E, ORIGIN_N = 478_000.0, 4_970_000.0


def _wgs84(geometry: Polygon) -> Polygon:
    result: Polygon = shapely_transform(TO_WGS84, geometry)
    return result


def _zip(tmp_path: Path, polygons: list[Polygon]) -> Path:
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(p)} for p in polygons
        ],
    }
    path = tmp_path / "Minnesota.geojson.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Minnesota.geojson", json.dumps(collection))
    return path


def test_a_ten_metre_footprint_covers_a_hundred_one_metre_pixels_in_place() -> None:
    footprint = box(ORIGIN_E + 20, ORIGIN_N - 30, ORIGIN_E + 30, ORIGIN_N - 20)
    transform = from_origin(ORIGIN_E, ORIGIN_N, 1.0, 1.0)

    mask = rasterise([_wgs84(footprint)], transform, (64, 64), UTM)

    assert mask.dtype == bool
    assert abs(int(mask.sum()) - 100) <= 4
    rows, cols = np.nonzero(mask)
    # Rows count down from the north edge, columns east from the west edge.
    assert 19 <= rows.min() and rows.max() <= 30
    assert 19 <= cols.min() and cols.max() <= 30


def test_no_footprints_rasterise_to_an_empty_mask() -> None:
    mask = rasterise([], from_origin(ORIGIN_E, ORIGIN_N, 1.0, 1.0), (8, 8), UTM)

    assert mask.shape == (8, 8)
    assert not mask.any()


def test_reading_by_bbox_returns_only_footprints_that_intersect_it(tmp_path: Path) -> None:
    inside = box(ORIGIN_E + 10, ORIGIN_N + 10, ORIGIN_E + 20, ORIGIN_N + 20)
    far = box(ORIGIN_E + 2_000, ORIGIN_N + 2_000, ORIGIN_E + 2_010, ORIGIN_N + 2_010)
    # An L whose bounding box overlaps the query box but whose body does not: a bbox
    # prefilter alone would return it.
    ell = Polygon(
        [
            (ORIGIN_E - 100, ORIGIN_N - 60),
            (ORIGIN_E + 70, ORIGIN_N - 60),
            (ORIGIN_E + 70, ORIGIN_N + 200),
            (ORIGIN_E + 60, ORIGIN_N + 200),
            (ORIGIN_E + 60, ORIGIN_N - 50),
            (ORIGIN_E - 100, ORIGIN_N - 50),
        ]
    )
    source = _zip(tmp_path, [_wgs84(inside), _wgs84(far), _wgs84(ell)])
    query = _wgs84(box(ORIGIN_E, ORIGIN_N, ORIGIN_E + 50, ORIGIN_N + 50)).bounds
    assert _wgs84(ell).envelope.intersects(box(*query))

    found = read_footprints(source, query)

    assert len(found) == 1
    assert shapely_transform(TO_UTM, found[0]).equals_exact(inside, tolerance=0.01)


def _transport(body: bytes, calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def test_the_source_is_downloaded_once_and_reused(tmp_path: Path) -> None:
    body = b"x" * 1234
    calls: list[str] = []
    destination = tmp_path / "footprints" / "Minnesota.geojson.zip"
    client = httpx.Client(transport=_transport(body, calls))

    ensure_footprints(destination, url="https://example/f.zip", expected_bytes=1234, client=client)
    ensure_footprints(destination, url="https://example/f.zip", expected_bytes=1234, client=client)

    assert destination.read_bytes() == body
    assert calls == ["https://example/f.zip"]


def test_a_partial_download_is_fetched_again(tmp_path: Path) -> None:
    calls: list[str] = []
    destination = tmp_path / "Minnesota.geojson.zip"
    destination.write_bytes(b"x" * 10)
    client = httpx.Client(transport=_transport(b"y" * 50, calls))

    ensure_footprints(destination, url="https://example/f.zip", expected_bytes=50, client=client)

    assert destination.read_bytes() == b"y" * 50
    assert len(calls) == 1
