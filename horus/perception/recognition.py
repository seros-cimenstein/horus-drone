"""Target-profile recognition: reference-embedding scoring and a tiny labeled classifier head."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
import math
from typing import Any, NamedTuple

import numpy as np

from .embedders import cosine_similarity, normalize_embedding
from .geometry import image_size, validate_box
from .types import BBox, Candidate, Embedding

# set_target_profile schema (documented on the MCP ``set_target_profile`` tool).
PROFILE_KEYS = frozenset(
    {
        "class_name",
        "class_id",
        "target_class",
        "reference_embeddings",
        "training_embeddings",
        "reference_images",
        "labels",
    }
)
EXAMPLE_KEYS = frozenset({"embedding", "image", "bbox", "label"})
RECOGNITION_MODES = ("embedding", "classifier")


class TinyEmbeddingClassifier:
    """Train a deterministic linear softmax head over precomputed appearance embeddings."""

    def __init__(self, *, epochs: int = 400, learning_rate: float = 0.15, l2: float = 1e-3):
        if epochs < 1 or not math.isfinite(learning_rate) or learning_rate <= 0:
            raise ValueError("epochs and learning_rate must be positive")
        if not math.isfinite(l2) or l2 < 0:
            raise ValueError("l2 must be finite and non-negative")
        self.epochs = int(epochs)
        self.learning_rate = float(learning_rate)
        self.l2 = float(l2)
        self.labels: tuple[str, ...] = ()
        self._weights: np.ndarray | None = None
        self._bias: np.ndarray | None = None

    @property
    def fitted(self) -> bool:
        return self._weights is not None

    def fit(self, embeddings: Sequence[Sequence[float]], labels: Sequence[str]) -> "TinyEmbeddingClassifier":
        """Fit a tiny regularized head; require repeat examples for at least two labels."""
        if len(embeddings) != len(labels):
            raise ValueError("each embedding must have exactly one label")
        if len(embeddings) < 4:
            raise ValueError("classifier head needs at least four labeled embedding examples")
        normalized = [_unit(embedding) for embedding in embeddings]
        dimensions = {len(vector) for vector in normalized}
        if len(dimensions) != 1:
            raise ValueError("all reference embeddings must have the same dimension")
        classes = tuple(dict.fromkeys(str(label) for label in labels))
        if len(classes) < 2:
            raise ValueError("classifier head needs at least two distinct labels")
        counts = {label: sum(str(item) == label for item in labels) for label in classes}
        if any(count < 2 for count in counts.values()):
            raise ValueError("classifier head needs at least two examples per label")

        matrix = np.asarray(normalized, dtype=np.float64)
        class_indices = np.asarray([classes.index(str(label)) for label in labels], dtype=np.int64)
        weights = np.zeros((len(classes), matrix.shape[1]), dtype=np.float64)
        bias = np.zeros(len(classes), dtype=np.float64)
        one_hot = np.eye(len(classes), dtype=np.float64)[class_indices]
        rate = self.learning_rate
        for _ in range(self.epochs):
            logits = matrix @ weights.T + bias
            logits -= logits.max(axis=1, keepdims=True)
            probabilities = np.exp(logits)
            probabilities /= probabilities.sum(axis=1, keepdims=True)
            error = probabilities - one_hot
            weights -= rate * (error.T @ matrix / len(matrix) + self.l2 * weights)
            bias -= rate * error.mean(axis=0)
        self.labels = classes
        self._weights = weights
        self._bias = bias
        return self

    def predict(self, embedding: Sequence[float]) -> tuple[str, float]:
        """Return the best label and its softmax confidence score."""
        if self._weights is None or self._bias is None:
            raise RuntimeError("classifier head has not been trained")
        vector = np.asarray(_unit(embedding), dtype=np.float64)
        if vector.shape[0] != self._weights.shape[1]:
            raise ValueError("query embedding dimension differs from the trained head")
        logits = self._weights @ vector + self._bias
        logits -= logits.max()
        scores = np.exp(logits)
        scores /= scores.sum()
        index = int(np.argmax(scores))
        return self.labels[index], float(scores[index])


def _unit(embedding: Sequence[float]) -> Embedding:
    vector = normalize_embedding(embedding)
    if vector is None:
        raise ValueError("embedding values must be a non-empty finite vector with non-zero magnitude")
    return vector


class TargetProfile(NamedTuple):
    """Recognition state replaced as one object so readers never see a half-applied profile."""

    class_name: Any = None
    class_id: Any = None
    target_class: Any = None
    embeddings: tuple[Embedding, ...] = ()
    classifier: TinyEmbeddingClassifier | None = None
    recognition_mode: str = "embedding"
    active: bool = False
    training_example_count: int = 0

    @classmethod
    def build(cls, profile: Any, encode: Callable[[Any, BBox], Embedding | None]) -> "TargetProfile":
        """Validate a ``set_target_profile`` mapping, encode its images and train the optional head.

        Raises ``ValueError`` for unknown keys or invalid content; a new profile starts on the
        highest recognition step it supports (``classifier`` when a head was trained).
        """
        if not isinstance(profile, Mapping):
            raise ValueError("target profile must be an object")
        unknown = sorted(str(key) for key in profile if key not in PROFILE_KEYS)
        if unknown:
            raise ValueError(
                f"unknown target profile keys {unknown}; allowed: {sorted(PROFILE_KEYS)}"
            )
        references = _reference_vectors(profile.get("reference_embeddings"))
        examples = [
            *_example_list(profile, "training_embeddings"),
            *_example_list(profile, "reference_images"),
        ]
        labels = profile.get("labels")
        if labels is not None:
            if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
                raise ValueError("profile labels must be a sequence matching the reference examples")
            if len(labels) != len(examples):
                raise ValueError("profile labels must match the number of reference examples")
        training_vectors: list[Embedding] = []
        training_labels: list[str] = []
        for index, example in enumerate(examples):
            vector, label = _encode_example(example, None if labels is None else labels[index], encode)
            if vector is None:
                continue
            references.append(vector)
            if label is not None:
                training_vectors.append(vector)
                training_labels.append(str(label))
        classifier: TinyEmbeddingClassifier | None = None
        if training_vectors:
            classifier = TinyEmbeddingClassifier().fit(training_vectors, training_labels)
        return cls(
            class_name=profile.get("class_name"),
            class_id=profile.get("class_id"),
            target_class=profile.get("target_class"),
            embeddings=tuple(references),
            classifier=classifier,
            recognition_mode="classifier" if classifier is not None else "embedding",
            active=True,
            training_example_count=len(training_vectors),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "reference_count": len(self.embeddings),
            "training_example_count": self.training_example_count,
            "classifier_trained": self.classifier is not None,
            "recognition_mode": self.recognition_mode,
            "target_class": self.target_class,
        }

    def with_mode(self, recognition_mode: str) -> "TargetProfile":
        """Select gallery (``embedding``) or trained-head (``classifier``) scoring."""
        if recognition_mode not in RECOGNITION_MODES:
            raise ValueError(f"recognition mode must be one of {RECOGNITION_MODES}")
        if recognition_mode == "classifier" and (self.classifier is None or not self.classifier.fitted):
            raise RuntimeError(
                "train a labeled tiny embedding head with set_target_profile before selecting classifier mode"
            )
        return self._replace(recognition_mode=recognition_mode)

    def _class_mismatch(self, candidate: Candidate) -> bool:
        return (self.class_name is not None and candidate.class_name != self.class_name) or (
            self.class_id is not None and candidate.class_id != self.class_id
        )

    def needs_embedding(self, candidate: Candidate) -> bool:
        """True when scoring this candidate uses its current appearance."""
        return self.classifier is not None or (
            bool(self.embeddings) and not self._class_mismatch(candidate)
        )

    def score(self, candidate: Candidate, embedding: Embedding | None) -> dict[str, Any]:
        """Recognition fields for a candidate record.

        ``recognized_class``/``recognition_confidence`` are reported whenever a head exists;
        ``target_match_confidence`` is reported once a profile is set: the head's confidence for
        ``target_class`` in classifier mode, else the best reference similarity (0.0 without
        appearance evidence), 1.0 for a class-only match, 0.0 for a class mismatch.
        """
        result: dict[str, Any] = {}
        predicted_label: str | None = None
        recognition_confidence: float | None = None
        if self.classifier is not None and embedding is not None:
            predicted_label, recognition_confidence = self.classifier.predict(embedding)
            result["recognized_class"] = predicted_label
            result["recognition_confidence"] = recognition_confidence
        if not self.active:
            return result
        scoring = self.classifier if self.recognition_mode == "classifier" else None
        target_class = self.target_class
        if target_class is None and scoring is not None and self.class_name in scoring.labels:
            target_class = self.class_name
        if target_class is not None and scoring is not None:
            match = recognition_confidence if predicted_label == str(target_class) else 0.0
        elif self._class_mismatch(candidate):
            match = 0.0
        elif scoring is None and self.embeddings:
            scores = [
                score
                for vector in self.embeddings
                if (score := cosine_similarity(vector, embedding)) is not None
            ]
            match = max(scores, default=0.0)
        elif self.class_name is not None or self.class_id is not None:
            match = 1.0
        else:
            match = None
        result["target_match_confidence"] = match
        return result


def _reference_vectors(value: Any) -> list[Embedding]:
    if value is None:
        return []
    if _is_numeric_vector(value):
        value = (value,)
    return [vector for item in value if (vector := normalize_embedding(item)) is not None]


def _example_list(profile: Mapping[str, Any], key: str) -> Sequence[Any]:
    examples = profile.get(key)
    if examples is None:
        return ()
    if isinstance(examples, Mapping):
        return (examples,)
    if isinstance(examples, (str, bytes)) or not isinstance(examples, Sequence):
        raise ValueError(f"{key} must be a sequence of examples")
    return examples


def _encode_example(
    example: Any, label: Any, encode: Callable[[Any, BBox], Embedding | None]
) -> tuple[Embedding | None, Any]:
    """An example's unit embedding and label; a bare entry is an image labeled by ``labels``."""
    image_value, bbox_value, vector = example, None, None
    if isinstance(example, Mapping):
        unknown = sorted(str(key) for key in example if key not in EXAMPLE_KEYS)
        if unknown:
            raise ValueError(f"unknown profile example keys {unknown}; allowed: {sorted(EXAMPLE_KEYS)}")
        label = example.get("label")
        image_value = example.get("image")
        bbox_value = example.get("bbox")
        if example.get("embedding") is not None:
            vector = normalize_embedding(example["embedding"])
    if vector is None and image_value is not None:
        image = _decode_reference_image(image_value)
        size = image_size(image)
        if bbox_value is not None:
            vector = encode(image, _reference_box(bbox_value))
        elif size is not None:
            vector = encode(image, (0.0, 0.0, float(size[0]), float(size[1])))
    return vector, label


def _decode_reference_image(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("data:"):
            _, separator, value = value.partition(",")
            if not separator:
                raise ValueError("reference image data URI has no base64 payload")
        try:
            value = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("reference image must be a base64-encoded image") from exc
    if isinstance(value, (bytes, bytearray, memoryview)):
        import cv2

        image = cv2.imdecode(np.frombuffer(value, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("reference image bytes are not a decodable image")
        return image
    if getattr(value, "shape", None) is None:
        raise ValueError("reference image must be an image array or base64-encoded image")
    return value


def _reference_box(value: Any) -> BBox:
    if isinstance(value, Mapping):
        value = (value.get("x1"), value.get("y1"), value.get("x2"), value.get("y2"))
    return validate_box(value)


def _is_numeric_vector(values: Any) -> bool:
    if isinstance(values, (str, bytes)) or values is None:
        return False
    try:
        if len(values) == 0:
            return False
        float(values[0])
        return True
    except (IndexError, KeyError, TypeError, ValueError):
        return False
