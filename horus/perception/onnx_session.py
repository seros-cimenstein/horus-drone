"""ONNX Runtime CPU session loading shared by the YOLO detector and the ReID embedder."""

from __future__ import annotations

from typing import Any


def load_onnx_session(
    model_path: str,
    *,
    purpose: str,
    intra_op_num_threads: int,
    inter_op_num_threads: int,
) -> tuple[Any, str]:
    """Open ``model_path`` on the CPU provider; return the session and its first input's name."""
    try:
        import onnxruntime
    except ImportError as exc:
        raise RuntimeError(
            f"onnxruntime is required for {purpose} ONNX inference; install horus-drone[perception]"
        ) from exc
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = intra_op_num_threads
    options.inter_op_num_threads = inter_op_num_threads
    session = onnxruntime.InferenceSession(
        model_path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    inputs = session.get_inputs()
    if not inputs:
        raise RuntimeError(f"{purpose} ONNX model has no inputs: {model_path}")
    return session, inputs[0].name
