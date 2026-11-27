"""Object-grounding backends: zero-install Cloud VLM and optional CPU Transformers."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Sequence

import cv2
import httpx
import numpy as np

from horus.perception.types import GroundingBox

MODEL_IDS = {
    "grounding-dino-tiny": "IDEA-Research/grounding-dino-tiny",
    "owlv2-base-patch16-ensemble": "google/owlv2-base-patch16-ensemble",
    "cloud-vlm": "cloud-vlm",
}

_BACKEND_ALIASES = {
    "grounding-dino": "grounding-dino-tiny",
    "owlv2": "owlv2-base-patch16-ensemble",
}

_CLOUD_PROVIDERS = (
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4.1-mini"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemini-2.5-flash"),
)


def resolve_cloud_vlm_config(settings_store: Any = None) -> tuple[str, str, str] | None:
    """Resolve ``(api_key, base_url, model)`` for Cloud VLM grounding or return ``None``."""
    env_key = os.environ.get("HORUS_GROUNDER_API_KEY", "").strip()
    env_base = os.environ.get("HORUS_GROUNDER_BASE_URL", "").strip()
    env_model = os.environ.get("HORUS_GROUNDER_MODEL", "").strip()
    if env_key and env_base and env_model:
        return env_key, env_base.rstrip("/"), env_model

    for secret_name, default_base, default_model in _CLOUD_PROVIDERS:
        key = None
        if settings_store is not None:
            try:
                key = settings_store.get_secret(secret_name)
            except Exception:
                key = None
        if not key:
            val = os.environ.get(secret_name, "").strip()
            key = val or None
        if key:
            return (
                env_key or key,
                (env_base or default_base).rstrip("/"),
                env_model or default_model,
            )
    return None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


class CloudVLMGrounder:
    """OpenAI-compatible multimodal VLM grounder requiring zero PyTorch dependencies."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._explicit_api_key = api_key
        self._explicit_base_url = base_url
        self._explicit_model = model
        self._client = client
        self._timeout_s = timeout_s
        self._api_key: str | None = None
        self._base_url: str | None = None
        self._resolved_model: str | None = model

    @property
    def model_id(self) -> str:
        return f"cloud-vlm:{self._resolved_model or 'auto'}"

    def _ensure_config(self) -> tuple[str, str, str]:
        if self._api_key and self._base_url and self._resolved_model:
            return self._api_key, self._base_url, self._resolved_model
        if self._explicit_api_key and self._explicit_base_url:
            self._api_key = self._explicit_api_key
            self._base_url = self._explicit_base_url.rstrip("/")
            self._resolved_model = self._explicit_model or "gemini-2.5-flash"
            return self._api_key, self._base_url, self._resolved_model

        resolved = resolve_cloud_vlm_config()
        if resolved is None:
            raise RuntimeError(
                "Cloud VLM grounder requires GEMINI_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY."
            )
        cfg_key, cfg_base, cfg_model = resolved
        self._api_key = self._explicit_api_key or cfg_key
        self._base_url = (self._explicit_base_url or cfg_base).rstrip("/")
        self._resolved_model = self._explicit_model or cfg_model
        return self._api_key, self._base_url, self._resolved_model

    def load(self, *, warmup: bool = True) -> None:
        self._ensure_config()

    def detect(
        self,
        image_bgr: np.ndarray,
        prompts: Sequence[str],
        *,
        box_threshold: float,
        text_threshold: float,
    ) -> list[GroundingBox]:
        del text_threshold
        if not prompts:
            return []
        api_key, base_url, model = self._ensure_config()
        height, width = image_bgr.shape[:2]
        ok, encoded = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("Failed to encode image frame for Cloud VLM grounder")
        b64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        prompt_list = ", ".join(repr(p) for p in prompts)
        instruction = (
            f"Locate all instances matching any of these target labels: [{prompt_list}]. "
            'Return ONLY valid JSON with key "detections": an array of objects '
            '{"label": "<matching label>", "score": <float 0..1>, '
            '"box_2d": [ymin, xmin, ymax, xmax]} where coordinates are integers in [0, 1000] '
            "normalized to the image height and width. Return {\"detections\": []} if nothing matches."
        )
        body = {
            "model": model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout_s)
        try:
            resp = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if owns_client:
                client.close()

        choices = payload.get("choices") or []
        if not choices:
            return []
        content = ((choices[0] or {}).get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        parsed = _extract_json_object(str(content))
        detections = parsed.get("detections")
        if not isinstance(detections, list):
            return []

        boxes: list[GroundingBox] = []
        for item in detections:
            if not isinstance(item, dict):
                continue
            coords = item.get("box_2d")
            if not isinstance(coords, (list, tuple)) or len(coords) != 4:
                continue
            try:
                ymin, xmin, ymax, xmax = (float(c) for c in coords)
                score = float(item.get("score", 0.85))
            except (TypeError, ValueError):
                continue
            if score < box_threshold:
                continue
            x1 = max(0.0, min(float(width), xmin / 1000.0 * width))
            y1 = max(0.0, min(float(height), ymin / 1000.0 * height))
            x2 = max(0.0, min(float(width), xmax / 1000.0 * width))
            y2 = max(0.0, min(float(height), ymax / 1000.0 * height))
            if x2 <= x1 or y2 <= y1:
                continue
            label = str(item.get("label") or prompts[0]).strip() or prompts[0]
            boxes.append(GroundingBox(bbox=(x1, y1, x2, y2), confidence=max(0.0, min(1.0, score)), label=label))
        return boxes


class TransformersGrounder:
    """One Transformers object-detection model; Torch is imported only when loaded."""

    def __init__(self, model: str, torch_threads: int = 6) -> None:
        if model not in MODEL_IDS or model == "cloud-vlm":
            raise ValueError(f"Unknown grounding model: {model}")
        self.model = model
        self.model_id = MODEL_IDS[model]
        self.torch_threads = torch_threads
        self._torch: Any = None
        self._processor: Any = None
        self._model: Any = None

    def load(self, *, warmup: bool = True) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoProcessor

        torch.set_num_threads(self.torch_threads)
        if self.model == "grounding-dino-tiny":
            from transformers import AutoModelForZeroShotObjectDetection

            model_class = AutoModelForZeroShotObjectDetection
        else:
            from transformers import Owlv2ForObjectDetection

            model_class = Owlv2ForObjectDetection

        self._processor = AutoProcessor.from_pretrained(self.model_id)
        if self.model == "grounding-dino-tiny":
            # Avoid the default 800 px upscaling on 640x480 CPU frames.
            self._processor.image_processor.size = {
                "shortest_edge": 320,
                "longest_edge": 533,
            }
        self._model = model_class.from_pretrained(self.model_id).to("cpu").eval()
        self._torch = torch
        if warmup:
            self.detect(
                np.zeros((480, 640, 3), dtype=np.uint8),
                ["person"],
                box_threshold=0.99,
                text_threshold=0.99,
            )

    def detect(
        self,
        image_bgr: np.ndarray,
        prompts: Sequence[str],
        *,
        box_threshold: float,
        text_threshold: float,
    ) -> list[GroundingBox]:
        if self._model is None or self._processor is None or self._torch is None:
            raise RuntimeError("Grounding model has not been loaded")
        if not prompts:
            return []
        height, width = image_bgr.shape[:2]
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        from PIL import Image

        image = Image.fromarray(image_rgb)
        if self.model == "grounding-dino-tiny":
            inputs = self._processor(
                images=image,
                text=" . ".join(prompts) + " .",
                return_tensors="pt",
            )
            with self._torch.inference_mode():
                outputs = self._model(**inputs)
            results = self._processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                box_threshold=box_threshold,
                text_threshold=text_threshold,
                target_sizes=[(height, width)],
            )[0]
            labels = results.get("text_labels", results.get("labels", []))
        else:
            inputs = self._processor(
                images=image,
                text=[list(prompts)],
                return_tensors="pt",
            )
            with self._torch.inference_mode():
                outputs = self._model(**inputs)
            results = self._processor.post_process_object_detection(
                outputs=outputs,
                threshold=box_threshold,
                target_sizes=[(height, width)],
            )[0]
            labels = [
                prompts[int(index)]
                for index in results["labels"].tolist()
            ]

        boxes = results["boxes"].tolist()
        scores = results["scores"].tolist()
        if self.model == "grounding-dino-tiny" and labels and isinstance(labels[0], int):
            labels = [prompts[index] if 0 <= index < len(prompts) else str(index) for index in labels]
        return [
            GroundingBox(
                bbox=tuple(float(value) for value in bbox),
                confidence=float(score),
                label=str(label),
            )
            for bbox, score, label in zip(boxes, scores, labels)
        ]


def create_backend(model: str, torch_threads: int = 6) -> CloudVLMGrounder | TransformersGrounder:
    """Create a grounding backend without importing Torch until ``load()``."""
    canonical = _BACKEND_ALIASES.get(model, model)
    if canonical == "cloud-vlm":
        return CloudVLMGrounder()
    return TransformersGrounder(canonical, torch_threads)
