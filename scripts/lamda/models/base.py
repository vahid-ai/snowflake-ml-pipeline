"""Model extension contract. Fit adapters receive training shards only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from scipy import sparse

from scripts.lamda.data import cached
from scripts.lamda.tracking import TrackedRun


@dataclass(frozen=True)
class TrainingData:
    directory: Path
    shards: tuple[str, ...]
    class_counts: tuple[int, int]
    n_features: int

    def batches(self):
        return cached(self.directory, list(self.shards))


class Predictor(Protocol):
    def predict_scores(self, matrix: sparse.csr_matrix) -> np.ndarray: ...


@dataclass
class FittedModel:
    predictor: Predictor
    score_kind: str  # probability or anomaly; larger always means more malicious

    def scores(self, matrix):
        values = np.asarray(self.predictor.predict_scores(matrix), dtype=np.float64)
        if values.shape != (matrix.shape[0],) or not np.isfinite(values).all():
            raise ValueError("Model must emit one finite score per input row")
        if self.score_kind == "probability" and ((values < 0).any() or (values > 1).any()):
            raise ValueError("Probability scores must be between 0 and 1")
        if self.score_kind not in ("probability", "anomaly"):
            raise ValueError("Unsupported model score kind")
        return values


class ModelAdapter(Protocol):
    name: str
    backend: str

    def candidates(self) -> list[dict]: ...
    def fit(self, data: TrainingData, parameters: dict, *, epochs: int, seed: int,
            run: TrackedRun, directory: Path) -> FittedModel: ...
