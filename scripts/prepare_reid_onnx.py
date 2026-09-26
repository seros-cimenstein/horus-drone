"""Make the pinned OSNet ONNX batch dimension flexible without installing Torch."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_reid_onnx.py SOURCE.onnx OUTPUT.onnx")
    try:
        import onnx
    except ImportError as exc:
        raise SystemExit(
            "onnx is required to prepare the ReID checkpoint; install horus-drone[model-tools]"
        ) from exc

    source, destination = map(Path, sys.argv[1:])
    model = onnx.load(str(source))
    if len(model.graph.input) != 1 or len(model.graph.output) != 1:
        raise SystemExit("expected the pinned ReID model to have one input and one output")

    input_shape = model.graph.input[0].type.tensor_type.shape.dim
    output_shape = model.graph.output[0].type.tensor_type.shape.dim
    if (
        len(input_shape) != 4
        or [dimension.dim_value for dimension in input_shape] != [16, 3, 256, 128]
        or len(output_shape) != 2
        or [dimension.dim_value for dimension in output_shape] != [16, 512]
    ):
        raise SystemExit("pinned ReID model has an unexpected input or output shape")

    for shape in (input_shape, output_shape):
        shape[0].ClearField("dim_value")
        shape[0].dim_param = "batch_size"

    onnx.checker.check_model(model)
    onnx.save(model, str(destination))


if __name__ == "__main__":
    main()
