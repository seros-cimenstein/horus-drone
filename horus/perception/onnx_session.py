"""ONNX Runtime session loading with hardware-accelerator preference and CPU fallback."""

from __future__ import annotations

from typing import Any

_PREFERRED_PROVIDERS = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
)


def load_onnx_session(
    model_path: str,
    *,
    purpose: str,
    intra_op_num_threads: int,
    inter_op_num_threads: int,
) -> tuple[Any, str]:
    """Open ``model_path`` using the fastest available provider; return the session and input name."""
    try:
        import onnxruntime
    except ImportError as exc:
        raise RuntimeError(
            f"onnxruntime is required for {purpose} ONNX inference; install horus-drone[perception]"
        ) from exc
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = intra_op_num_threads
    options.inter_op_num_threads = inter_op_num_threads
    available = set(onnxruntime.get_available_providers())
    providers = [p for p in _PREFERRED_PROVIDERS if p in available] or ["CPUExecutionProvider"]
    session = onnxruntime.InferenceSession(
        model_path,
        sess_options=options,
        providers=providers,
    )
    inputs = session.get_inputs()
    if not inputs:
        raise RuntimeError(f"{purpose} ONNX model has no inputs: {model_path}")
    return session, inputs[0].name
