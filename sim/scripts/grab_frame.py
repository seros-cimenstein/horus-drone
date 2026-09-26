#!/usr/bin/env python3
"""Enable a Gazebo camera stream and save one decoded frame."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path


FRAME_PATH = Path("/horus/sim/logs/s0_frame.jpg")


def run(*args: str) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=True)
    return result.stdout


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: grab_frame.py [OUTPUT_PATH]", file=sys.stderr)
        return 2
    frame_path = Path(sys.argv[1]) if len(sys.argv) == 2 else FRAME_PATH
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is missing; use the simulation container's system Python") from exc

    build_info = cv2.getBuildInformation()
    match = re.search(r"^\s*GStreamer:\s*(\S+)", build_info, re.MULTILINE)
    if match is None or match.group(1) != "YES":
        raise RuntimeError("OpenCV was not built with GStreamer support")

    topics = run("gz", "topic", "-l").splitlines()
    streaming_topics = [
        topic.strip()
        for topic in topics
        if topic.strip().endswith("/enable_streaming")
        and "/camera/image/" in topic
    ]
    if not streaming_topics:
        raise RuntimeError(
            "No camera enable_streaming topic found; check that iris_runway.sdf "
            "is running and its camera sensor loaded"
        )
    preferred = [topic for topic in streaming_topics if "/iris_with_gimbal/" in topic]
    topic = (preferred or streaming_topics)[0]
    run(
        "gz",
        "topic",
        "-t",
        topic,
        "-m",
        "gz.msgs.Boolean",
        "-p",
        "data: 1",
    )
    print(f"Enabled camera stream: {topic}", flush=True)

    pipeline = (
        'udpsrc port=5600 caps="application/x-rtp, media=(string)video, '
        'clock-rate=(int)90000, encoding-name=(string)H264" '
        "! rtph264depay ! avdec_h264 ! videoconvert "
        "! video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
    )
    capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not capture.isOpened():
        raise RuntimeError("OpenCV could not open the GStreamer UDP 5600 pipeline")

    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            ok, frame = capture.read()
            if ok and frame is not None:
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(frame_path), frame):
                    raise RuntimeError(f"OpenCV failed to write {frame_path}")
                height, width = frame.shape[:2]
                print(f"Saved frame: {frame_path} ({width}x{height})", flush=True)
                return 0
            time.sleep(0.1)
    finally:
        capture.release()
    raise TimeoutError("No decodable frame arrived from UDP port 5600 within 30 seconds")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {exc.cmd}\n{exc.stderr}", file=sys.stderr)
        raise
