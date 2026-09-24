"""A minimal client for an OpenAI-compatible ``chat/completions`` endpoint with image input.

The endpoint is the operator's own model server (a LiteLLM proxy in front of Ollama on
the local network), so failures split in two: the server being unreachable or erroring is
``VisionUnavailable`` -- worth retrying later, never a verdict on the parcel -- while a
reply that arrives but is unusable is the caller's to judge.
"""

import base64
from typing import Any

import httpx

from ptax.config import Settings

TIMEOUT_SECONDS = 180.0


class VisionNotConfigured(Exception):
    """No API key is configured for the vision model."""


class VisionUnavailable(Exception):
    """The vision model could not be reached, or failed on its side."""


def image_part(png: bytes) -> dict[str, Any]:
    """A message content part carrying a PNG."""
    encoded = base64.b64encode(png).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


class VisionClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> "VisionClient":
        if not settings.vision_api_key:
            raise VisionNotConfigured("vision model is not configured (VISION_API_KEY)")
        return cls(
            settings.vision_base_url,
            settings.vision_model,
            settings.vision_api_key,
            transport=transport,
        )

    def complete(self, messages: list[dict[str, Any]]) -> str:
        """The model's reply text for ``messages``, deterministic and JSON-only."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._http.post("chat/completions", json=body)
        except httpx.TransportError as exc:
            raise VisionUnavailable(f"{self.model}: {exc or type(exc).__name__}") from exc
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise VisionUnavailable(f"{self.model}: HTTP {response.status_code}")
        if response.status_code != 200:
            # A 4xx other than rate limiting is a configuration problem (bad key, unknown
            # model); retrying will not fix it, but it is still not a verdict on a parcel.
            raise VisionUnavailable(
                f"{self.model}: HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VisionUnavailable(f"{self.model}: malformed response envelope") from exc
        return str(content or "")

    def close(self) -> None:
        self._http.close()
