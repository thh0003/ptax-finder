"""The marked-up target image, and the registration guarantee it depends on.

Three images make this view work: base year, target year, and target year with the run's
detections drawn on it. They are three separate HTTP calls, so "they cover the same
ground" is not something the shared extent function can guarantee by itself — the calls
have to agree on `size` and `buffer` too. That is what most of this file checks.
"""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ptax.api.imagery import OUTLINE_RGB
from ptax.db.models import Parcel, RunParcel
from tests.conftest import drain_queue
from tests.test_detector import FIXTURE_THRESHOLD

# `years` is a pytest fixture shared from test_runs; pytest resolves it by name, which
# ruff reads as a redefinition at each use site.
from tests.test_runs import _other_tenant_sub, _start, years  # noqa: F401
from tests.test_tiles import _parcel, _png_array


@pytest.fixture
def scored_run(client: TestClient, db: Session, tenant_with_admin, years) -> dict:  # noqa: F811
    """A completed 2021 -> 2023 fixture run, which flags three planted roofs."""
    headers = tenant_with_admin["admin_headers"]
    run = _start(
        client, headers, years[2021]["id"], years[2023]["id"], threshold=FIXTURE_THRESHOLD
    ).json()
    drain_queue(db)
    return {"run": run, "headers": headers, "years": years}


def _flagged_parcel_id(db: Session, run_id: str) -> uuid.UUID:
    return db.execute(
        select(RunParcel.parcel_id).where(
            RunParcel.run_id == uuid.UUID(run_id), RunParcel.structure_geom.is_not(None)
        )
    ).scalars().first()


# --- The extraction must not change what `parcel_preview` already returns ---------------
#
# Hashes captured from the endpoint BEFORE the extent computation was extracted. If the
# refactor changes a single pixel of an existing preview, these fail.

PRE_EXTRACTION_PREVIEWS = {
    "size=256&outline=1": "4ddbdd84b43cba51",
    "": "f960c558d2eacd82",
    "size=512&buffer=0.5&outline=1": "2ea14eb18f46c1b5",
}


def test_parcel_preview_is_byte_identical_after_the_extraction(
    client: TestClient, db: Session, tenant_with_admin, ingested_layer, years  # noqa: F811
) -> None:
    headers = tenant_with_admin["admin_headers"]
    parcel = _parcel(db, ingested_layer["id"], "27-053-000003")
    for query, expected in PRE_EXTRACTION_PREVIEWS.items():
        response = client.get(
            f"/api/imagery/years/{years[2023]['id']}/parcels/{parcel.id}/preview.png?{query}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        digest = hashlib.sha256(response.content).hexdigest()[:16]
        assert digest == expected, f"preview changed for {query or 'defaults'}"


# --- Registration -----------------------------------------------------------------------


def test_all_three_images_cover_exactly_the_same_ground(
    client: TestClient, db: Session, scored_run
) -> None:
    headers, years_ = scored_run["headers"], scored_run["years"]
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)
    assert parcel_id is not None

    base = client.get(
        f"/api/imagery/years/{years_[2021]['id']}/parcels/{parcel_id}/preview.png", headers=headers
    )
    target = client.get(
        f"/api/imagery/years/{years_[2023]['id']}/parcels/{parcel_id}/preview.png", headers=headers
    )
    overlay = client.get(
        f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=headers
    )
    for response in (base, target, overlay):
        assert response.status_code == 200, response.text

    assert base.headers["x-bounds"] == target.headers["x-bounds"] == overlay.headers["x-bounds"]


def test_the_overlay_takes_the_same_size_and_buffer_contract_as_the_preview(client) -> None:
    """A differing default would misregister the markup while every other test passed."""
    schema = client.get("/api/openapi.json").json()

    def params(path: str) -> dict:
        for method in schema["paths"][path].values():
            return {
                p["name"]: (p["schema"].get("default"), p["schema"].get("type"))
                for p in method.get("parameters", [])
                if p["in"] == "query"
            }
        raise AssertionError(f"no method for {path}")

    preview = params("/api/imagery/years/{year_id}/parcels/{parcel_id}/preview.png")
    overlay = params("/api/runs/{run_id}/parcels/{parcel_id}/overlay.png")
    for name in ("size", "buffer", "outline"):
        assert name in overlay, f"overlay is missing the {name} parameter"
        assert overlay[name] == preview[name], f"{name} differs from parcel_preview"


# --- What the markup draws ---------------------------------------------------------------


def test_a_recorded_detection_changes_the_picture(
    client: TestClient, db: Session, scored_run
) -> None:
    headers, years_ = scored_run["headers"], scored_run["years"]
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)

    plain = client.get(
        f"/api/imagery/years/{years_[2023]['id']}/parcels/{parcel_id}/preview.png", headers=headers
    )
    overlay = client.get(f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=headers)

    assert overlay.status_code == 200
    assert overlay.content != plain.content


def test_the_scoring_structure_and_the_rest_are_drawn_differently(
    client: TestClient, db: Session, scored_run
) -> None:
    """The user asked for two styles: the area that scored, and everything else detected.

    Drawn in one colour this view still "works" by every other check here, and silently
    loses the distinction between what produced the score and what did not.
    """
    from ptax.api.runs import NEW_BUILTUP_RGB, STRUCTURE_RGB

    headers = scored_run["headers"]
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)

    # Force the separation rather than hoping the fixture happens to have it: the scoring
    # structure is a small square, the wider detection a strictly larger square around it.
    # Both must then appear in the rendered image, in their own colour.
    centre = to_shape(db.get(Parcel, parcel_id).geom).centroid
    structure = centre.buffer(0.00012, quad_segs=1)
    db.execute(
        update(RunParcel)
        .where(RunParcel.run_id == uuid.UUID(run_id), RunParcel.parcel_id == parcel_id)
        .values(
            structure_geom=from_shape(MultiPolygon([structure]), srid=4326),
            new_builtup_geom=from_shape(
                MultiPolygon([centre.buffer(0.00030, quad_segs=1)]), srid=4326
            ),
        )
    )
    db.commit()

    overlay = client.get(f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=headers)
    rgb = _png_array(overlay.content)[:3]
    colours = {
        tuple(int(c) for c in rgb[:, y, x])
        for y in range(rgb.shape[1])
        for x in range(rgb.shape[2])
    }

    # Both layers must be *rendered*. Comparing the two constants to each other would pass
    # even if the endpoint painted only one of them.
    assert STRUCTURE_RGB in colours, "the scoring structure is not drawn"
    assert NEW_BUILTUP_RGB in colours, "the wider new built-up area is not drawn"
    assert OUTLINE_RGB not in (STRUCTURE_RGB, NEW_BUILTUP_RGB)


def test_a_run_that_recorded_no_markup_renders_the_plain_target_image(
    client: TestClient, db: Session, scored_run
) -> None:
    """Runs scored before this feature carry NULL geometry and must not be re-derived."""
    headers, years_ = scored_run["headers"], scored_run["years"]
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)

    db.execute(
        update(RunParcel)
        .where(RunParcel.run_id == uuid.UUID(run_id), RunParcel.parcel_id == parcel_id)
        .values(new_builtup_geom=None, structure_geom=None)
    )
    db.commit()

    plain = client.get(
        f"/api/imagery/years/{years_[2023]['id']}/parcels/{parcel_id}/preview.png", headers=headers
    )
    overlay = client.get(f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=headers)

    assert overlay.status_code == 200
    assert overlay.content == plain.content


# --- Access -------------------------------------------------------------------------------


def test_another_tenants_run_is_not_found(
    client: TestClient, db: Session, scored_run, make_token
) -> None:
    headers = scored_run["headers"]
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)

    assert (
        client.get(
            f"/api/runs/{uuid.uuid4()}/parcels/{uuid.uuid4()}/overlay.png", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/runs/{run_id}/parcels/{uuid.uuid4()}/overlay.png", headers=headers
        ).status_code
        == 404
    )
    # The imagery this route serves is the point: a second tenant must be refused the
    # run and parcel that genuinely exist, not merely ones that do not.
    other_headers = {"Authorization": f"Bearer {make_token(_other_tenant_sub(db))}"}
    assert (
        client.get(
            f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png", headers=other_headers
        ).status_code
        == 404
    )


def test_the_overlay_requires_a_token(client: TestClient, db: Session, scored_run) -> None:
    run_id = scored_run["run"]["id"]
    parcel_id = _flagged_parcel_id(db, run_id)
    assert client.get(f"/api/runs/{run_id}/parcels/{parcel_id}/overlay.png").status_code == 401
