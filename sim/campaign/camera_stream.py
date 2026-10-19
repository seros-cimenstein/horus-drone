"""Enable the Gazebo camera's RTP stream and read its runtime CameraInfo.

Standard library only, so the container's system ``python3`` runs it too:
``python3 -m sim.campaign.camera_stream [--budget-s SECONDS]``.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import socket
import subprocess
import sys
import time
from typing import Any

from sim.campaign.gz_topics import parse_camera_info

STREAM_SUFFIX = "/enable_streaming"
RTP_PORT = 5600
# Generated 1.2 km worlds render their first frames well after MCP connects, so the stream is
# re-enabled for a time budget rather than a fixed number of attempts.
DEFAULT_BUDGET_S = 180.0
TOPIC_WAIT_S = 60.0
CAMERA_INFO_WAIT_S = 30.0


def list_topics() -> list[str]:
    result = subprocess.run(["gz", "topic", "-l"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10.0, check=False)
    return [line.strip() for line in result.stdout.splitlines()]


def find_stream_topic(*, prefer: str | None = None, wait_s: float = TOPIC_WAIT_S) -> str:
    """The camera's ``enable_streaming`` topic, waiting for the sensor to advertise it."""
    deadline = time.monotonic() + wait_s
    while True:
        topics = [topic for topic in list_topics()
                  if "/camera/image/" in topic and topic.endswith(STREAM_SUFFIX)]
        if topics:
            preferred = [topic for topic in topics if prefer and prefer in topic]
            return (preferred or topics)[0]
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out locating the camera enable_streaming topic")
        time.sleep(0.5)


def rtp_flowing(port: int = RTP_PORT, timeout_s: float = 1.5) -> bool:
    """True once an RTP packet arrives; SO_REUSEADDR shares the port with the MCP camera reader."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
            probe.settimeout(timeout_s)
            probe.recv(65535)
    except OSError:  # the receive timeout, or a port that cannot be bound yet
        return False
    return True


def enable_camera_stream(*, budget_s: float = DEFAULT_BUDGET_S, prefer: str | None = None,
                         log: Callable[[str], Any] = print) -> str:
    """Re-publish ``enable_streaming`` until RTP arrives on UDP 5600 or ``budget_s`` expires.

    A single publish can be lost before discovery connects it to the camera plugin (slower over
    an Ethernet GZ_IP). Call this after MCP has connected to SITL: the lockstep world, and so
    the camera, only runs once a MAVLink client is attached. Returns the stream topic.
    """
    topic = find_stream_topic(prefer=prefer)
    deadline = time.monotonic() + budget_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        subprocess.run(["gz", "topic", "-t", topic, "-m", "gz.msgs.Boolean", "-p", "data: 1"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0, check=False)
        if rtp_flowing():
            log(f"Enabled camera stream: {topic} (RTP flowing after {attempt} attempt(s))")
            return topic
    raise RuntimeError(f"camera stream did not start after {attempt} enable attempts "
                       f"in {budget_s:g} s: {topic}")


def read_camera_info(stream_topic: str, *, wait_s: float = CAMERA_INFO_WAIT_S) -> dict[str, Any]:
    """Runtime CameraInfo of the camera whose stream ``stream_topic`` enables."""
    camera_base = stream_topic.removesuffix("/image" + STREAM_SUFFIX)
    camera_info_topic = None
    deadline = time.monotonic() + wait_s
    last_error = "camera_info topic not yet advertised"
    while time.monotonic() < deadline:
        topics = list_topics()
        matches = [topic for topic in topics if topic.endswith("/camera_info") and topic.startswith(camera_base)]
        if not matches:
            matches = [topic for topic in topics if topic.endswith("/camera_info")]
        camera_info_topic = matches[0] if matches else None
        if camera_info_topic:
            try:
                message = subprocess.run(
                    ["gz", "topic", "-e", "-n", "1", "-t", camera_info_topic],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
                if message.returncode == 0:
                    return parse_camera_info(message.stdout, camera_info_topic)
                last_error = f"gz topic exited with status {message.returncode}"
            except (subprocess.TimeoutExpired, ValueError) as exc:
                last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"could not read runtime Gazebo CameraInfo from {camera_info_topic}: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S,
                        help="seconds to keep re-enabling the stream (default: 180)")
    parser.add_argument("--prefer", help="prefer a stream topic containing this text")
    args = parser.parse_args(argv)
    try:
        enable_camera_stream(budget_s=args.budget_s, prefer=args.prefer,
                             log=lambda line: print(line, flush=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
