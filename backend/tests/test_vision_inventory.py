import base64
import io
import json
import socket

import httpx
import numpy as np
import pytest
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box

from ptax.config import Settings
from ptax.detection.detector import ParcelRaster
from ptax.vision.client import VisionClient, VisionNotConfigured, VisionUnavailable
from ptax.vision.inventory import InventoryError, inventory_parcel

# A 100 x 100 px image at 1 m/px in UTM 15N; the parcel is the square 10 m in from the edge.
X0, Y0, PX = 500_000.0, 4_500_000.0, 100
PARCEL = box(X0 + 10, Y0 - 90, X0 + 90, Y0 - 10)


def _raster() -> ParcelRaster:
    transform = from_origin(X0, Y0, 1.0, 1.0)
    return ParcelRaster(
        data=np.full((3, PX, PX), 90, dtype=np.uint8),
        mask=np.ones((PX, PX), dtype=bool),
        parcel_mask=np.ones((PX, PX), dtype=bool),
        transform=transform,
        crs=CRS.from_epsg(32615),
        resolution_m=1.0,
        bounds=(X0, Y0 - PX, X0 + PX, Y0),
    )


class FakeModel:
    """An OpenAI-compatible endpoint answering with the queued replies in order."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    def client(self) -> VisionClient:
        return VisionClient(
            "http://model.test/v1", "qwen3-vl", "test-key", transport=httpx.MockTransport(self)
        )


def _reply(*structures: tuple[str, list[int], float], summary: str = "A house.") -> str:
    return json.dumps(
        {
            "structures": [
                {"kind": kind, "box": box_, "confidence": confidence}
                for kind, box_, confidence in structures
            ],
            "summary": summary,
        }
    )


def test_structures_land_on_the_pixels_their_boxes_name() -> None:
    # Boxes are in Qwen-VL's 0-1000 coordinates over the whole image. Fenced JSON after a
    # thinking block is still read.
    reply = _reply(("house", [200, 200, 400, 400], 0.9), ("shed", [600, 700, 650, 750], 0.6))
    model = FakeModel(f"<think>looking</think>\n```json\n{reply}\n```")

    result = inventory_parcel(model.client(), _raster(), PARCEL)

    house, shed = result.structures
    assert (house.kind, house.confidence) == ("house", 0.9)
    # Pixels 20-40 from the top-left corner, at 1 m/px.
    assert house.polygon.bounds == pytest.approx((X0 + 20, Y0 - 40, X0 + 40, Y0 - 20))
    assert shed.polygon.bounds == pytest.approx((X0 + 60, Y0 - 75, X0 + 65, Y0 - 70))
    assert result.summary == "A house."
    assert result.counts == {"house": 1, "shed": 1}
    assert result.kinds == ["house", "shed"]

    # One request, deterministic, carrying the rendered parcel image.
    (request,) = model.requests
    assert request["model"] == "qwen3-vl" and request["temperature"] == 0
    image_url = next(
        part["image_url"]["url"]
        for part in request["messages"][-1]["content"]
        if part["type"] == "image_url"
    )
    png = base64.b64decode(image_url.removeprefix("data:image/png;base64,"))
    assert Image.open(io.BytesIO(png)).size == (PX, PX)


def test_a_structure_is_clipped_to_the_parcel_and_one_outside_it_is_dropped() -> None:
    # Straddles the parcel's west edge (x = 10 m); and one wholly in the buffer outside.
    reply = _reply(("garage", [0, 500, 200, 600], 0.8), ("house", [0, 0, 80, 80], 0.7))
    result = inventory_parcel(FakeModel(reply).client(), _raster(), PARCEL)
    (garage,) = result.structures
    assert garage.polygon.bounds == pytest.approx((X0 + 10, Y0 - 60, X0 + 20, Y0 - 50))


def test_no_structures_is_a_valid_inventory() -> None:
    result = inventory_parcel(FakeModel(_reply(summary="Empty lot.")).client(), _raster(), PARCEL)
    assert result.structures == [] and result.counts == {} and result.summary == "Empty lot."


@pytest.mark.parametrize(
    "bad",
    [
        _reply(("barn-ish", [100, 100, 200, 200], 0.5)),  # not one of the kinds
        _reply(("house", [100, 100, 1200, 200], 0.5)),  # outside the image
        _reply(("house", [300, 100, 200, 200], 0.5)),  # inverted
        _reply(("house", [100, 100, 200, 200], 1.5)),  # confidence out of range
        "not json at all",
    ],
)
def test_an_invalid_reply_is_retried_once_with_the_error_quoted(bad: str) -> None:
    model = FakeModel(bad, _reply(("house", [200, 200, 400, 400], 0.9)))
    result = inventory_parcel(model.client(), _raster(), PARCEL)
    assert [s.kind for s in result.structures] == ["house"]
    assert len(model.requests) == 2
    correction = model.requests[1]["messages"][-1]["content"]
    assert "invalid" in correction.lower()
    assert model.requests[1]["messages"][-2] == {"role": "assistant", "content": bad}


def test_two_invalid_replies_raise_with_the_raw_reply() -> None:
    model = FakeModel("{oops", '{"structures": "none"}')
    with pytest.raises(InventoryError) as caught:
        inventory_parcel(model.client(), _raster(), PARCEL)
    assert caught.value.raw == '{"structures": "none"}'
    assert len(model.requests) == 2


def test_a_refused_connection_is_unavailable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = VisionClient(f"http://127.0.0.1:{port}/v1", "qwen3-vl", "test-key")
    with pytest.raises(VisionUnavailable):
        inventory_parcel(client, _raster(), PARCEL)


def test_a_server_error_is_unavailable() -> None:
    client = VisionClient(
        "http://model.test/v1",
        "qwen3-vl",
        "test-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    with pytest.raises(VisionUnavailable):
        inventory_parcel(client, _raster(), PARCEL)


def test_no_key_configured_fails_before_any_request(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made without a key")

    unkeyed = settings.model_copy(update={"vision_api_key": None})
    with pytest.raises(VisionNotConfigured, match="VISION_API_KEY"):
        VisionClient.from_settings(unkeyed, transport=httpx.MockTransport(refuse))
