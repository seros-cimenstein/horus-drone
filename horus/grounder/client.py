"""HTTP client for the optional loopback text-grounding service."""

from __future__ import annotations

import base64
import json
import socket
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from horus.perception.types import GroundingBox


class GrounderUnavailable(RuntimeError):
    """The grounding service could not be reached or returned an invalid response."""


class GrounderClient:
    """Call the loopback JSON grounding service."""

    def __init__(self, url: str, timeout_s: float = 10.0) -> None:
        self._url = url.rstrip("/")
        self._timeout_s = timeout_s

    def detect(
        self,
        image_bgr: np.ndarray,
        prompts: Sequence[str],
        *,
        box_threshold: float = 0.30,
        text_threshold: float = 0.25,
    ) -> list[GroundingBox]:
        try:
            ok, encoded = cv2.imencode(".jpg", image_bgr)
        except cv2.error as exc:
            raise GrounderUnavailable(f"Could not encode grounding image: {exc}") from exc
        if not ok:
            raise GrounderUnavailable("Could not encode grounding image as JPEG")
        payload = {
            "image_jpeg_b64": base64.b64encode(encoded.tobytes()).decode("ascii"),
            "prompts": list(prompts),
            "box_threshold": box_threshold,
            "text_threshold": text_threshold,
        }
        response = self._request("POST", "/detect", payload)
        try:
            boxes = response["boxes"]
            if not isinstance(boxes, list):
                raise TypeError("boxes must be a list")
            return [
                GroundingBox(
                    bbox=tuple(float(value) for value in box["bbox"]),
                    confidence=float(box["score"]),
                    label=str(box["label"]),
                )
                for box in boxes
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise GrounderUnavailable(f"Invalid /detect response: {exc}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._url}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_s) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise GrounderUnavailable(f"Grounder request failed: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GrounderUnavailable(f"Invalid grounder JSON response: {exc}") from exc
        if not isinstance(decoded, dict):
            raise GrounderUnavailable("Grounder response must be a JSON object")
        return decoded
