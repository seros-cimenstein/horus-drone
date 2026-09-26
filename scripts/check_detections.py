"""Inspect detector proposals and shirt-color attributes for a saved frame."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from horus.config import load_config
from horus.perception.attributes import classify_shirt_colour
from horus.perception.detector import DetectorConfig, YOLODetector


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the configured ONNX detector and annotate every detection."
    )
    parser.add_argument("image", type=Path, help="input image captured from the simulator")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("annotated.jpg"),
        help="annotated output image (default: annotated.jpg)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.image.is_file():
        parser.error(f"input image does not exist: {args.image}")
    if args.image.resolve() == args.out.resolve():
        parser.error("--out must not overwrite the input image")
    try:
        import cv2
    except ImportError as exc:
        parser.error(f"OpenCV is required to read and annotate images: {exc}")

    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if image is None:
        parser.error(f"could not decode input image: {args.image}")

    config = load_config()
    detector = YOLODetector(DetectorConfig(model_name=config.perception.detector_model))
    detections = detector.detect(image)
    annotated = image.copy()
    print(f"model: {detector.model_id}")
    for index, detection in enumerate(detections, 1):
        shirt_colour = None
        shirt_colour_confidence = None
        if detection.class_name.casefold() == "person":
            classification = classify_shirt_colour(image, detection.bbox)
            if classification is not None:
                shirt_colour, shirt_colour_confidence = classification
        box = tuple(round(value, 1) for value in detection.bbox)
        print(
            f"{index}: class={detection.class_name} conf={detection.confidence:.3f} "
            f"box={box} shirt_colour={shirt_colour}"
            + (
                f" shirt_colour_conf={shirt_colour_confidence:.3f}"
                if shirt_colour_confidence is not None
                else ""
            )
        )
        x1, y1, x2, y2 = (int(round(value)) for value in detection.bbox)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = f"{detection.class_name} {detection.confidence:.2f}"
        if shirt_colour is not None:
            label += f" {shirt_colour}"
        cv2.putText(
            annotated,
            label,
            (x1, max(15, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(args.out), annotated):
        parser.error(f"could not write annotated image: {args.out}")
    print(f"annotated image: {args.out}")
    print(f"detections: {len(detections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
