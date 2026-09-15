"""Incremental logistic-regression baseline."""
from dataclasses import dataclass
import numpy as np
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import log_loss

from scripts.lamda.data import cached
from scripts.lamda.models.base import FittedModel, TrainingData


@dataclass
class SGDPredictor:
    estimator: SGDClassifier

    def predict_scores(self, matrix):
        return self.estimator.predict_proba(matrix)[:, 1]


class SGDAdapter:
    name = "sgd"
    backend = "lamda_iceberg_scipy_v1"

    def __init__(self, *, alphas=(0.0001, 0.001)):
        if not alphas or any(not np.isfinite(a) or a <= 0 for a in alphas):
            raise ValueError("alphas must be positive and finite")
        self.alphas = tuple(alphas)

    def candidates(self):
        return [{"alpha": a} for a in self.alphas]

    def fit(self, data: TrainingData, parameters, *, epochs, seed, run, directory):
        count = np.asarray(data.class_counts)
        weights = count.sum() / (2.0 * count)
        run.params({"class_weights": weights.tolist(), "fit_population": "all_training_rows"})
        model = SGDClassifier(loss="log_loss", alpha=parameters["alpha"], random_state=seed,
                              average=True, shuffle=True, learning_rate="optimal")
        rng = np.random.default_rng(seed)
        for epoch in range(epochs):
            loss, rows = 0.0, 0
            for matrix, meta in cached(data.directory, rng.permutation(data.shards).tolist()):
                labels = meta["label"].to_numpy()
                model.partial_fit(matrix, labels, classes=np.array([0, 1]),
                                  sample_weight=weights[labels].astype(np.float32))
                loss += log_loss(labels, model.predict_proba(matrix), labels=[0, 1]) * len(labels)
                rows += len(labels)
            run.metrics({"train.loss": loss / rows, "train.rows": rows}, step=epoch)
            print(f"sgd alpha={parameters['alpha']}: epoch {epoch + 1}/{epochs}", flush=True)
        return FittedModel(SGDPredictor(model), "probability")
