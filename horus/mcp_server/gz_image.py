"""Optional Gazebo Transport publisher for rendered set-of-mark and live perception images."""

from __future__ import annotations

import logging
from typing import Any


_LOGGER = logging.getLogger(__name__)
IMAGE_TOPIC = "/horus/som_image"
LIVE_IMAGE_TOPIC = "/horus/perception/live"


def _gazebo_dependencies() -> tuple[Any, Any, int, Any, Any]:
    """Load container-only Gazebo, OpenCV, and NumPy dependencies on demand."""
    import cv2
    import numpy as np
    from gz.msgs10.image_pb2 import RGB_INT8, Image
    from gz.transport13 import Node

    return Node, Image, RGB_INT8, cv2, np


class GazeboImagePublisher:
    """Publish images as raw RGB ``gz.msgs.Image`` messages when available.

    gz-gui's ImageDisplay rejects ``BGR_INT8`` ("Unsupported image type"), so OpenCV BGR
    frames are converted to ``RGB_INT8`` before publishing.
    """

    def __init__(
        self,
        topic: str = IMAGE_TOPIC,
        *,
        node_factory: Any = None,
        image_type: Any = None,
        rgb_int8: int | None = None,
        cv2_module: Any = None,
        numpy_module: Any = None,
    ) -> None:
        self._node: Any = None
        self._publisher: Any = None
        self._image_type: Any = None
        self._rgb_int8: int | None = None
        self._cv2: Any = None
        self._numpy: Any = None

        try:
            if all(value is None for value in (
                node_factory, image_type, rgb_int8, cv2_module, numpy_module
            )):
                node_factory, image_type, rgb_int8, cv2_module, numpy_module = (
                    _gazebo_dependencies()
                )
            elif any(value is None for value in (
                node_factory, image_type, rgb_int8, cv2_module, numpy_module
            )):
                raise ValueError("all image publisher dependencies must be injected together")

            self._node = node_factory()
            self._publisher = self._node.advertise(topic, image_type)
            if self._publisher is None or self._publisher is False:
                raise RuntimeError(f"Gazebo could not advertise {topic}")
            self._image_type = image_type
            self._rgb_int8 = int(rgb_int8)
            self._cv2 = cv2_module
            self._numpy = numpy_module
        except Exception as exc:
            _LOGGER.warning("Gazebo image publishing on %s is unavailable: %s", topic, exc)
            self._release()

    def publish(self, payload: Any) -> None:
        """Decode and publish one rendered JPEG; disable this optional path on failure."""
        if self._publisher is None:
            return
        try:
            encoded = self._numpy.frombuffer(payload.image_jpeg, dtype=self._numpy.uint8)
            decoded = self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
            if decoded is None:
                raise ValueError("OpenCV could not decode the set-of-mark JPEG")
            self._publish_bgr(decoded)
        except Exception as exc:
            _LOGGER.warning("Disabling Gazebo image publishing: %s", exc)
            self._release()

    def publish_bgr(self, image: Any) -> None:
        """Publish one BGR image array; disable this optional path on failure."""
        if self._publisher is None:
            return
        try:
            self._publish_bgr(image)
        except Exception as exc:
            _LOGGER.warning("Disabling Gazebo image publishing: %s", exc)
            self._release()

    def _publish_bgr(self, image: Any) -> None:
        shape = image.shape
        if len(shape) != 3 or int(shape[2]) != 3:
            raise ValueError("published image must have three BGR channels")
        height, width = int(shape[0]), int(shape[1])
        if width < 1 or height < 1:
            raise ValueError("published image must have positive dimensions")

        message = self._image_type()
        message.width = width
        message.height = height
        message.step = width * 3
        message.pixel_format_type = self._rgb_int8
        message.data = self._cv2.cvtColor(image, self._cv2.COLOR_BGR2RGB).tobytes()
        if self._publisher.publish(message) is False:
            raise RuntimeError("Gazebo image publisher rejected a frame")

    @property
    def available(self) -> bool:
        """Whether Gazebo publishing is still active."""
        return self._publisher is not None

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
