#!/usr/bin/env python3
"""Pull frames from horus_capture.asi while horus_capture_selftest.exe runs and check their pixels.

Each frame must be the selftest's full 640x360 back buffer cleared to one palette colour (so the copy
finished, the channel order is right and the format conversion is exact within rounding), and the
frames must cycle through several colours (so the plugin serves fresh frames, not one stale copy).

The camera fields of the header are checked too. Without ``--camera`` nothing writes HorusGta's
camera block, so every frame must say camera_active 0 and script age "never". With ``--camera``
(selftest started with ``--camera``) the selftest writes the block before each Present of an even
palette colour and stops writing during odd colours, like a paused game whose block still says
active 1. Even-colour frames must then be active with that frame's pose seq. Odd-colour frames must
be inactive, apart from the first presents after the pause (script age <= 2) that still carry the
last tick of the previous colour.

With ``--shared`` (selftest started with ``--shared``) the selftest alternates drone frames (palette
colour, 8x8 black corner marker) and player frames (grey, white marker) with the block in shared
view. Every frame served must then be a drone frame: palette colour outside the marker, black
marker, camera_active 1, script age 0, an even pose seq naming the frame's colour. The frames must
arrive at ``--min-hz`` or faster (the plugin publishes at most ``max_hz``, 15 by default).

The checker reads every frame (``require_active=False``), active or not. ``--legacy-header``
accepts the 40-byte header of plugins built before the camera fields (their frames carry no camera
fields to check); HORUS itself never accepts it.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import time


def _load_gta_stream():
    # Load by file path: putting horus/perception on sys.path would shadow the stdlib `types`
    # module with horus/perception/types.py, and importing the horus package pulls in its extras.
    path = Path(__file__).resolve().parents[3] / "horus" / "perception" / "gta_stream.py"
    spec = importlib.util.spec_from_file_location("gta_stream", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gta_stream = _load_gta_stream()
GtaFrameStream = _gta_stream.GtaFrameStream
SCRIPT_AGE_NEVER = _gta_stream.SCRIPT_AGE_NEVER

PALETTE = [(230, 30, 20), (20, 200, 40), (40, 60, 220), (240, 220, 30), (200, 40, 210), (30, 210, 220)]
FRAMES_PER_COLOUR = 10  # selftest.cpp kFramesPerColour
MAX_SCRIPT_AGE = 2  # camera_block.hpp kMaxScriptAgePresents
MARKER_PIXELS = 8  # selftest.cpp kMarkerPixels
TOLERANCE = 2


def connect(host: str, port: int, deadline_s: float, legacy_header: bool) -> GtaFrameStream:
    end = time.monotonic() + deadline_s
    while True:
        try:
            return GtaFrameStream(
                host, port, timeout_s=5.0, require_active=False, allow_legacy_header=legacy_header
            )
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.2)


def shared_error(colour: int, header) -> str | None:
    """Why this shared-view frame's camera fields are wrong, or None."""
    seq = header.pose_seq
    if header.camera_active != 1 or header.script_age_presents != 0:
        return f"camera_active {header.camera_active}, script age {header.script_age_presents}"
    if seq % 2 != 0:
        return f"pose_seq {seq} is a player frame"
    if header.game_frame != seq or (seq // FRAMES_PER_COLOUR) % len(PALETTE) != colour:
        return f"colour {colour} with pose_seq {seq}, game_frame {header.game_frame}"
    return None


def camera_error(colour: int, header, camera: bool) -> str | None:
    """Why this frame's camera fields are wrong, or None."""
    active, age, seq = header.camera_active, header.script_age_presents, header.pose_seq
    if age is None:  # legacy 40-byte header (--legacy-header): no camera fields
        return None
    if not camera:
        if active or age != SCRIPT_AGE_NEVER:
            return f"camera_active {active}, script age {age} without a camera block writer"
        return None
    seq_colour = (seq // FRAMES_PER_COLOUR) % len(PALETTE)
    if colour % 2 == 0:
        if not active or age != 0 or seq_colour != colour or header.game_frame != seq:
            return f"ticking colour {colour}: active {active}, age {age}, pose_seq {seq}, game_frame {header.game_frame}"
        return None
    if age == 0:
        return f"paused colour {colour}: script age 0"
    if active and not (
        age <= MAX_SCRIPT_AGE and seq % FRAMES_PER_COLOUR == FRAMES_PER_COLOUR - 1 and seq_colour == colour - 1
    ):
        return f"paused colour {colour}: still active at script age {age}, pose_seq {seq}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5610)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--camera", action="store_true", help="the selftest runs with --camera")
    parser.add_argument("--shared", action="store_true", help="the selftest runs with --shared")
    parser.add_argument("--min-hz", type=float, default=10.0, help="--shared: lowest acceptable frame rate")
    parser.add_argument(
        "--legacy-header", action="store_true", help="accept a plugin built before the camera fields"
    )
    args = parser.parse_args()

    stream = connect(args.host, args.port, 30.0, args.legacy_header)
    seen: set[int] = set()
    indices: list[int] = []
    seqs: list[int] = []
    active_frames = paused_inactive_frames = 0
    stamps: list[int] = []
    # Shared view: the marker corner is black and excluded from the uniformity check.
    body = (slice(None), slice(None))
    try:
        for n in range(args.frames):
            ok, image = stream.read()
            header = stream.last_header
            assert ok and header is not None
            if image.shape != (360, 640, 3):
                print(f"FAIL frame {n}: shape {image.shape}")
                return 1
            if args.shared:
                marker = int(image[:MARKER_PIXELS, :MARKER_PIXELS].max())
                if marker > TOLERANCE:
                    print(f"FAIL frame {n}: marker corner is not black (max {marker}); a player frame")
                    return 1
                body = (slice(MARKER_PIXELS, None), slice(None))
            pixels = image[body].reshape(-1, 3)
            spread = pixels.max(axis=0).astype(int) - pixels.min(axis=0)
            if spread.max() > TOLERANCE:
                print(f"FAIL frame {n}: not uniform, per-channel spread {spread.tolist()}")
                return 1
            b, g, r = (int(v) for v in image[image.shape[0] - 1, image.shape[1] - 1])
            match = [
                i for i, colour in enumerate(PALETTE)
                if max(abs(r - colour[0]), abs(g - colour[1]), abs(b - colour[2])) <= TOLERANCE
            ]
            if not match:
                print(f"FAIL frame {n}: RGB {(r, g, b)} is not a palette colour")
                return 1
            error = shared_error(match[0], header) if args.shared else camera_error(match[0], header, args.camera)
            if error:
                print(f"FAIL frame {n}: {error}")
                return 1
            active_frames += header.camera_active
            paused_inactive_frames += match[0] % 2 == 1 and not header.camera_active
            seen.add(match[0])
            indices.append(header.present_index)
            seqs.append(header.pose_seq)
            stamps.append(header.capture_unix_ns)
    finally:
        stream.release()
    if len(seen) < 3:
        print(f"FAIL: only {len(seen)} distinct colours in {args.frames} frames; frames are stale")
        return 1
    if any(b <= a for a, b in zip(indices, indices[1:])):
        print(f"FAIL: present indices not increasing: {indices}")
        return 1
    camera = ""
    if args.camera:
        if active_frames == 0 or paused_inactive_frames == 0:
            print(f"FAIL: {active_frames} active and {paused_inactive_frames} paused-inactive frames; need both")
            return 1
        if any(b < a for a, b in zip(seqs, seqs[1:])):
            print(f"FAIL: pose seq went backwards: {seqs}")
            return 1
        camera = f", camera: {active_frames} active, {paused_inactive_frames} paused inactive, seq {seqs[0]}..{seqs[-1]}"
    if args.shared:
        rate = (len(stamps) - 1) / max((stamps[-1] - stamps[0]) / 1e9, 1e-9)
        if rate < args.min_hz:
            print(f"FAIL: drone frames at {rate:.1f} Hz, below {args.min_hz} Hz")
            return 1
        camera = f", shared view: all drone frames, {rate:.1f} Hz, seq {seqs[0]}..{seqs[-1]}"
    print(f"OK {args.frames} frames, {len(seen)} colours, present index {indices[0]}..{indices[-1]}{camera}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
