from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from horus.mcp_server.gz_image import GazeboImagePublisher


class _DecodedImage:
    shape = (2, 3, 3)

    def tobytes(self) -> bytes:
        return bytes(range(1, 19))


class _Numpy:
    uint8 = object()

    def frombuffer(self, data: bytes, *, dtype: object) -> bytes:
        assert dtype is self.uint8
        return data


class _OpenCV:
    IMREAD_COLOR = 1

    def imdecode(self, encoded: bytes, mode: int) -> _DecodedImage:
        assert encoded == b"rendered-jpeg"
        assert mode == self.IMREAD_COLOR
        return _DecodedImage()


class _ImageMessage:
    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.step = 0
        self.pixel_format_type = 0
        self.data = b""


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[_ImageMessage] = []
        self.closed = False

    def publish(self, message: _ImageMessage) -> bool:
        self.messages.append(message)
        return True

    def close(self) -> None:
        self.closed = True


class _Node:
    def __init__(self, publisher: _Publisher) -> None:
        self.publisher = publisher
        self.topic: str | None = None
        self.message_type: object | None = None
        self.closed = False

    def advertise(self, topic: str, message_type: object) -> _Publisher:
        self.topic = topic
        self.message_type = message_type
        return self.publisher

    def close(self) -> None:
        self.closed = True


def test_publisher_converts_jpeg_to_gazebo_bgr_image_invariants():
    publisher = _Publisher()
    node = _Node(publisher)
    adapter = GazeboImagePublisher(
        node_factory=lambda: node,
        image_type=_ImageMessage,
        bgr_int8=8,
        cv2_module=_OpenCV(),
        numpy_module=_Numpy(),
    )

    adapter.publish(SimpleNamespace(image_jpeg=b"rendered-jpeg"))

    assert node.topic == "/horus/som_image"
    assert node.message_type is _ImageMessage
    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert (message.width, message.height) == (3, 2)
    assert message.step == message.width * 3
    assert message.pixel_format_type == 8
    assert len(message.data) == message.step * message.height
    assert message.data == bytes(range(1, 19))

    adapter.close()
    assert publisher.closed
    assert node.closed


def test_unavailable_gazebo_dependencies_leave_publisher_inert(monkeypatch):
    import horus.mcp_server.gz_image as gz_image

    def unavailable():
        raise ImportError("Gazebo bindings are not installed")

    monkeypatch.setattr(gz_image, "_gazebo_dependencies", unavailable)
    adapter = GazeboImagePublisher()

    adapter.publish(SimpleNamespace(image_jpeg=b"ignored"))
    adapter.close()


def test_importing_mcp_package_does_not_load_optional_vision_or_gazebo_modules():
    code = """
import sys
import horus.mcp_server
assert 'cv2' not in sys.modules
assert 'gz' not in sys.modules
assert not any(name.startswith('horus.perception') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)
