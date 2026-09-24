"""/api/tiles/{year_id}/{z}/{x}/{y}.png: WebMercator raster tiles of an imagery year.

Authenticated like every other route; browsers attach the bearer token through fetch
(thumbnails) or MapLibre's ``transformRequest`` (Plan C's map).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from morecantile import Tile
from sqlalchemy.orm import Session

from ptax.api.imagery import get_year
from ptax.auth.deps import CurrentUser, get_current_user
from ptax.db.session import get_db
from ptax.imagery.reader import MIN_ZOOM, TMS, assets_intersecting, read_tile

router = APIRouter(prefix="/tiles")
MAX_ZOOM = 22
CACHE_CONTROL = "private, max-age=3600"


@router.get("/{year_id}/{z}/{x}/{y}.png", response_class=Response)
def tile(
    year_id: uuid.UUID,
    z: int,
    x: int,
    y: int,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    if not MIN_ZOOM <= z <= MAX_ZOOM:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"zoom must be {MIN_ZOOM}-{MAX_ZOOM}"
        )
    year = get_year(db, user, year_id)
    if year.status != "ready":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "imagery year is not ready")
    bounds = TMS.bounds(Tile(x, y, z))
    assets = assets_intersecting(
        db, user.tenant_id, year.id, (bounds.left, bounds.bottom, bounds.right, bounds.top)
    )
    img = read_tile(request.app.state.settings, assets, z, x, y)
    if img is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(
        img.render(img_format="PNG"),
        media_type="image/png",
        headers={"Cache-Control": CACHE_CONTROL},
    )
