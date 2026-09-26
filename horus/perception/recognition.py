"""A small CPU multinomial logistic head for labeled frozen ReID embeddings."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


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
        normalized = [_normalize(embedding) for embedding in embeddings]
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
        vector = np.asarray(_normalize(embedding), dtype=np.float64)
        if vector.shape[0] != self._weights.shape[1]:
            raise ValueError("query embedding dimension differs from the trained head")
        logits = self._weights @ vector + self._bias
        logits -= logits.max()
        scores = np.exp(logits)
        scores /= scores.sum()
        index = int(np.argmax(scores))
        return self.labels[index], float(scores[index])


def _normalize(embedding: Sequence[float]) -> tuple[float, ...]:
    try:
        values = tuple(float(value) for value in embedding)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("embedding values must be finite numbers") from exc
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding values must be a non-empty finite vector")
    scale = max(abs(value) for value in values)
    if scale == 0:
        raise ValueError("embedding vector must have non-zero magnitude")
    scaled = tuple(value / scale for value in values)
    norm = math.sqrt(sum(value * value for value in scaled))
    if scale <= 1e-12 / norm:
        raise ValueError("embedding vector must have non-zero magnitude")
    return tuple(value / norm for value in scaled)
