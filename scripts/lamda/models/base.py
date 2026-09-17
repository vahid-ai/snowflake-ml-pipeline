"""Model extension contract. Fit adapters receive training shards only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from scipy import sparse

from scripts.lamda.data import cached
from scripts.lamda.tracking import TrackedRun


# Expose only training shards and training-derived counts to model fitting code.
@dataclass(frozen=True)
class TrainingData:
    directory: Path
    shards: tuple[str, ...]
    class_counts: tuple[int, int]
    n_features: int

    # Stream the permitted shards together with labels and row metadata.
    def batches(self):
        return cached(self.directory, list(self.shards))


# Require a score-producing object that can be persisted independently of training
# orchestration.
class Predictor(Protocol):
    # Emit one score per row; larger scores must indicate greater malware likelihood.
    def predict_scores(self, matrix: sparse.csr_matrix) -> np.ndarray: ...


# Pair a predictor with the semantics needed for evaluation and thresholding.
@dataclass
class FittedModel:
    predictor: Predictor
    score_kind: str  # probability or anomaly; larger always means more malicious

    # Validate score shape, finiteness, and probability bounds at the adapter boundary.
    def scores(self, matrix):
        values = np.asarray(self.predictor.predict_scores(matrix), dtype=np.float64)
        if values.shape != (matrix.shape[0],) or not np.isfinite(values).all():
            raise ValueError("Model must emit one finite score per input row")
        if self.score_kind == "probability" and ((values < 0).any() or (values > 1).any()):
            raise ValueError("Probability scores must be between 0 and 1")
        if self.score_kind not in ("probability", "anomaly"):
            raise ValueError("Unsupported model score kind")
        return values


# Define candidate enumeration and fitting so new backends share the same audit and evaluation
# flow.
class ModelAdapter(Protocol):
    name: str
    backend: str

    # Return the parameter combinations the orchestrator should compare on validation data.
    def candidates(self) -> list[dict]: ...
    # Fit using the supplied training partition and log progress through the shared tracked run.
    def fit(self, data: TrainingData, parameters: dict, *, epochs: int, seed: int,
            run: TrackedRun, directory: Path) -> FittedModel: ...
