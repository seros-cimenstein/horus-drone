"""Loopback HTTP service for text-grounded object detection."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any, Sequence
from urllib.parse import urlsplit

import cv2
import numpy as np

from horus.grounder.backends import MODEL_IDS, create_backend

_LOG = logging.getLogger(__name__)


class GrounderService:
    """Shared inference gate and response builder around a grounding backend."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._inference_lock = Lock()

    @property
    def model(self) -> str:
        return str(getattr(self.backend, "model_id", getattr(self.backend, "model", "unknown")))

    def detect(
        self,
        image_bgr: np.ndarray,
        prompts: Sequence[str],
        *,
        box_threshold: float = 0.30,
        text_threshold: float = 0.25,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        with self._inference_lock:
            boxes = self.backend.detect(
                image_bgr,
                prompts,
                box_threshold=box_threshold,
                text_threshold=text_threshold,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        return {
            "boxes": [
                {
                    "bbox": [float(value) for value in box.bbox],
                    "score": float(box.confidence),
                    "label": box.label,
                }
                for box in boxes
            ],
            "model": self.model,
            "latency_ms": latency_ms,
        }

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model": self.model}


class GrounderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: GrounderService) -> None:
        self.service = service
        super().__init__(address, GrounderRequestHandler)


class GrounderRequestHandler(BaseHTTPRequestHandler):
    server: GrounderHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/health":
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, self.server.service.health())

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/detect":
            self._send_json(404, {"error": "not found"})
            return
        try:
            request = self._read_json()
            image = self._decode_image(request)
            prompts = self._prompts(request)
            box_threshold = self._threshold(request, "box_threshold", 0.30)
            text_threshold = self._threshold(request, "text_threshold", 0.25)
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        try:
            response = self.server.service.detect(
                image,
                prompts,
                box_threshold=box_threshold,
                text_threshold=text_threshold,
            )
        except Exception as exc:
            _LOG.exception("Grounder inference failed")
            self._send_json(503, {"error": f"grounder inference failed: {exc}"})
            return
        self._send_json(200, response)

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.info("%s - %s", self.address_string(), format % args)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > 32 * 1024 * 1024:
            raise ValueError("request body must be between 1 byte and 32 MiB")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("request body must be a JSON object")
        return value

    @staticmethod
    def _decode_image(request: dict[str, Any]) -> np.ndarray:
        encoded = request["image_jpeg_b64"]
        if not isinstance(encoded, str):
            raise TypeError("image_jpeg_b64 must be a string")
        try:
            jpeg = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image_jpeg_b64 is not valid base64") from exc
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("image_jpeg_b64 does not contain a decodable image")
        return image

    @staticmethod
    def _prompts(request: dict[str, Any]) -> list[str]:
        prompts = request["prompts"]
        if not isinstance(prompts, list) or any(not isinstance(term, str) for term in prompts):
            raise TypeError("prompts must be a list of strings")
        return prompts

    @staticmethod
    def _threshold(request: dict[str, Any], name: str, default: float) -> float:
        value = request.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return value

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        finally:
            self.close_connection = True


def make_server(host: str, port: int, backend: Any) -> GrounderHTTPServer:
    return GrounderHTTPServer((host, port), GrounderService(backend))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--model", choices=tuple(MODEL_IDS), default="grounding-dino-tiny")
    parser.add_argument("--threads", type=int, default=6)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("--threads must be a positive integer")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend = create_backend(args.model, args.threads)
    _LOG.info("Loading %s on CPU with %d Torch threads", backend.model_id, args.threads)
    backend.load(warmup=True)
    server = make_server(args.host, args.port, backend)
    _LOG.info("Grounder ready at http://%s:%d using %s", args.host, args.port, backend.model_id)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOG.info("Stopping grounder")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
