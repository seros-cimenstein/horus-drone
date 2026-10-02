"""Lazy CPU-only Transformers implementations for text grounding."""

from __future__ import annotations

from typing import Any, Sequence

import cv2
import numpy as np

from horus.perception.types import GroundingBox


MODEL_IDS = {
    "grounding-dino-tiny": "IDEA-Research/grounding-dino-tiny",
    "owlv2-base-patch16-ensemble": "google/owlv2-base-patch16-ensemble",
}


class TransformersGrounder:
    """One Transformers object-detection model; Torch is imported only when loaded."""

    def __init__(self, model: str, torch_threads: int = 6) -> None:
        if model not in MODEL_IDS:
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


def create_backend(model: str, torch_threads: int = 6) -> TransformersGrounder:
    """Create a backend without importing or loading Torch."""
    return TransformersGrounder(model, torch_threads)
