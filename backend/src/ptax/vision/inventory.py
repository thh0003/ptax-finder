"""One parcel's structure inventory from a vision-language model.

The parcel's image -- its imagery plus a little context, with the boundary drawn in
yellow as the parcel viewer draws it -- goes to the model with a fixed prompt. The model
answers strict JSON: the structures whose roofs lie inside the boundary, each with a kind,
a box and a confidence. Boxes use Qwen-VL's native convention, 0-1000 over the whole
image, which survives any resizing the model server does to the image.

A reply is used whole or not at all: one invalid structure (an unknown kind, a box off the
image, a confidence out of range) makes the reply invalid. It is retried once with the
problem quoted back; a second invalid reply raises ``InventoryError`` carrying the raw
text, and the parcel is recorded as a model error rather than guessed at.
"""

import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from ptax.detection.detector import ParcelRaster
from ptax.imagery.preview import paint
from ptax.vision.client import VisionClient, image_part

KINDS = ("house", "garage", "shed", "pool", "other")
#: The model's box coordinates run 0..BOX_SCALE across the whole image.
BOX_SCALE = 1000
#: Image side sent to the model, and context around the parcel as a fraction of its size.
IMAGE_PX = 768
BUFFER = 0.15
OUTLINE_RGB = (255, 230, 0)
OUTLINE_PX = 2

PROMPT = f"""\
This is a north-up aerial photograph of one property. Its boundary is drawn in yellow.
List every structure whose roof lies inside the yellow boundary. Ignore anything outside it.
Kinds:
- house: the main dwelling; an attached garage is part of the house.
- garage: a detached garage or outbuilding (barn, pole building, workshop).
- shed: a small detached storage shed.
- pool: a swimming pool, in-ground or above-ground.
- other: any other structure (for example a commercial building or a large tank).
Give each structure a box [x0, y0, x1, y1] in coordinates from 0 to {BOX_SCALE} over the
whole image (0,0 is the top-left corner), and your confidence from 0 to 1.
Reply with only this JSON object and nothing else:
{{"structures": [{{"kind": "house", "box": [x0, y0, x1, y1], "confidence": 0.9}}],
 "summary": "one sentence describing what is on the property"}}
If there are no structures inside the boundary, reply with an empty "structures" list."""


class InventoryError(Exception):
    """The model's reply could not be used, even after one retry."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class Structure:
    kind: str
    confidence: float
    #: As the model gave it: 0..BOX_SCALE over the image.
    box: tuple[int, int, int, int]
    #: On the raster's grid (its CRS), clipped to the parcel.
    polygon: BaseGeometry


@dataclass(frozen=True)
class Inventory:
    structures: list[Structure]
    summary: str
    raw: str
    counts: dict[str, int] = field(init=False)
    kinds: list[str] = field(init=False)

    def __post_init__(self) -> None:
        counts = dict(Counter(s.kind for s in self.structures))
        object.__setattr__(self, "counts", counts)
        object.__setattr__(self, "kinds", sorted(counts))


def render(raster: ParcelRaster, parcel: BaseGeometry) -> bytes:
    """The PNG the model sees: the imagery with the parcel boundary in yellow."""
    rgb = raster.data[:3].copy()
    rgb[:, ~raster.mask] = 0
    paint(rgb, raster, parcel, OUTLINE_RGB, width_px=OUTLINE_PX, outline_only=True)
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb.transpose(1, 2, 0))).save(buffer, format="PNG")
    return buffer.getvalue()


def _json_text(reply: str) -> str:
    """The JSON object in a reply that may carry a thinking block or a code fence."""
    text = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def parse_reply(reply: str) -> tuple[list[tuple[str, tuple[int, int, int, int], float]], str]:
    """(kind, box, confidence) per structure, and the summary; ``ValueError`` if invalid."""
    try:
        payload: Any = json.loads(_json_text(reply))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("structures"), list):
        raise ValueError('the reply must be an object with a "structures" list')
    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise ValueError('"summary" must be a string')
    structures = []
    for n, item in enumerate(payload["structures"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"structure {n} is not an object")
        kind = item.get("kind")
        if kind not in KINDS:
            raise ValueError(f"structure {n} has kind {kind!r}; use one of {', '.join(KINDS)}")
        coords = item.get("box")
        if (
            not isinstance(coords, list)
            or len(coords) != 4
            or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in coords)
        ):
            raise ValueError(f"structure {n} needs a box of four numbers")
        x0, y0, x1, y1 = (round(v) for v in coords)
        if not (0 <= x0 < x1 <= BOX_SCALE and 0 <= y0 < y1 <= BOX_SCALE):
            raise ValueError(
                f"structure {n} box {coords} is not inside 0..{BOX_SCALE} with x0<x1, y0<y1"
            )
        confidence = item.get("confidence")
        if (
            not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise ValueError(f"structure {n} confidence must be a number from 0 to 1")
        structures.append((kind, (x0, y0, x1, y1), float(confidence)))
    return structures, summary.strip()


def _polygon(raster: ParcelRaster, coords: tuple[int, int, int, int]) -> BaseGeometry:
    """A model box as a polygon on the raster's grid."""
    height, width = raster.mask.shape
    x0, y0, x1, y1 = coords
    west, north = raster.transform * (x0 * width / BOX_SCALE, y0 * height / BOX_SCALE)
    east, south = raster.transform * (x1 * width / BOX_SCALE, y1 * height / BOX_SCALE)
    return box(min(west, east), min(south, north), max(west, east), max(south, north))


def inventory_parcel(client: VisionClient, raster: ParcelRaster, parcel: BaseGeometry) -> Inventory:
    """Ask the model for the structures on ``parcel`` (in the raster's CRS) in ``raster``.

    Raises ``VisionUnavailable`` if the model cannot be reached and ``InventoryError`` if
    it twice replies with something unusable.
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": PROMPT}, image_part(render(raster, parcel))],
        }
    ]
    reply = client.complete(messages)
    try:
        parsed, summary = parse_reply(reply)
    except ValueError as first:
        messages += [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": f"That reply was invalid: {first}. "
                "Reply again with only the JSON object.",
            },
        ]
        reply = client.complete(messages)
        try:
            parsed, summary = parse_reply(reply)
        except ValueError as second:
            raise InventoryError(f"invalid reply after one retry: {second}", reply) from second

    structures = []
    for kind, coords, confidence in parsed:
        polygon = _polygon(raster, coords).intersection(parcel)
        if polygon.is_empty or polygon.area == 0:
            continue  # the model boxed something outside the boundary
        structures.append(Structure(kind, confidence, coords, polygon))
    return Inventory(structures=structures, summary=summary, raw=reply)
