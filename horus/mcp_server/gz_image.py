"""Optional Gazebo Transport publisher for rendered set-of-mark images."""

from __future__ import annotations

import logging
from typing import Any


_LOGGER = logging.getLogger(__name__)
IMAGE_TOPIC = "/horus/som_image"


def _gazebo_dependencies() -> tuple[Any, Any, int, Any, Any]:
    """Load container-only Gazebo, OpenCV, and NumPy dependencies on demand."""
    import cv2
    import numpy as np
    from gz.msgs10.image_pb2 import BGR_INT8, Image
    from gz.transport13 import Node

    return Node, Image, BGR_INT8, cv2, np


class GazeboImagePublisher:
    """Publish JPEG payloads as raw BGR ``gz.msgs.Image`` messages when available."""

    def __init__(
        self,
        topic: str = IMAGE_TOPIC,
        *,
        node_factory: Any = None,
        image_type: Any = None,
        bgr_int8: int | None = None,
        cv2_module: Any = None,
        numpy_module: Any = None,
    ) -> None:
        self._node: Any = None
        self._publisher: Any = None
        self._image_type: Any = None
        self._bgr_int8: int | None = None
        self._cv2: Any = None
        self._numpy: Any = None

        try:
            if all(value is None for value in (
                node_factory, image_type, bgr_int8, cv2_module, numpy_module
            )):
                node_factory, image_type, bgr_int8, cv2_module, numpy_module = (
                    _gazebo_dependencies()
                )
            elif any(value is None for value in (
                node_factory, image_type, bgr_int8, cv2_module, numpy_module
            )):
                raise ValueError("all image publisher dependencies must be injected together")

            self._node = node_factory()
            self._publisher = self._node.advertise(topic, image_type)
            if self._publisher is None or self._publisher is False:
                raise RuntimeError(f"Gazebo could not advertise {topic}")
            self._image_type = image_type
            self._bgr_int8 = int(bgr_int8)
            self._cv2 = cv2_module
            self._numpy = numpy_module
        except Exception as exc:
            _LOGGER.warning("Gazebo set-of-mark image publishing is unavailable: %s", exc)
            self._release()

    def publish(self, payload: Any) -> None:
        """Decode and publish one rendered JPEG; disable this optional path on failure."""
        if self._publisher is None:
            return
        try:
            image = self._image_message(payload.image_jpeg)
            result = self._publisher.publish(image)
            if result is False:
                raise RuntimeError("Gazebo image publisher rejected a frame")
        except Exception as exc:
            _LOGGER.warning("Disabling Gazebo set-of-mark image publishing: %s", exc)
            self._release()

    def _image_message(self, image_jpeg: bytes) -> Any:
        encoded = self._numpy.frombuffer(image_jpeg, dtype=self._numpy.uint8)
        decoded = self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("OpenCV could not decode the set-of-mark JPEG")
        shape = decoded.shape
        if len(shape) != 3 or int(shape[2]) != 3:
            raise ValueError("decoded set-of-mark image must have three BGR channels")
        height, width = int(shape[0]), int(shape[1])
        if width < 1 or height < 1:
            raise ValueError("decoded set-of-mark image must have positive dimensions")

        message = self._image_type()
        message.width = width
        message.height = height
        message.step = width * 3
        message.pixel_format_type = self._bgr_int8
        message.data = decoded.tobytes()
        return message

    def close(self) -> None:
        """Release the optional publisher and transport node without propagating errors."""
        self._release()

    def _release(self) -> None:
        publisher, self._publisher = self._publisher, None
        node, self._node = self._node, None
        for resource in (publisher, node):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    _LOGGER.debug("Error closing Gazebo image resource: %s", exc)
