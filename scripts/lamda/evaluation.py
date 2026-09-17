"""Shared score evaluation; anomaly scores are not probabilities."""
import numpy as np
from sklearn.metrics import (
    average_precision_score, balanced_accuracy_score, confusion_matrix, f1_score,
    log_loss, precision_recall_curve, precision_score, recall_score, roc_auc_score,
)

def choose_threshold(labels, probabilities) -> float:
    precision, recall, thresholds = precision_recall_curve(labels, probabilities)
    f1 = np.divide(2 * precision[:-1] * recall[:-1], precision[:-1] + recall[:-1],
                   out=np.zeros(len(thresholds)), where=(precision[:-1] + recall[:-1]) > 0)
    # On ties prefer the higher threshold to avoid additional false positives.
    return float(thresholds[np.flatnonzero(f1 == f1.max())[-1]])


def metrics(labels, probabilities, threshold: float, *, score_kind="probability") -> dict:
    labels, probabilities = np.asarray(labels), np.asarray(probabilities)
    predicted = probabilities >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, predicted, labels=[0, 1]).ravel()
    both = len(np.unique(labels)) == 2
    return {
        "rows": len(labels), "threshold": threshold,
        "malware_prevalence": float(labels.mean()),
        "average_precision": float(average_precision_score(labels, probabilities)) if both else None,
        "roc_auc": float(roc_auc_score(labels, probabilities)) if both else None,
        "f1": float(f1_score(labels, predicted, zero_division=0)),
        "precision": float(precision_score(labels, predicted, zero_division=0)),
        "recall": float(recall_score(labels, predicted, zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, predicted)) if both else None,
        "false_positive_rate": float(fp / (fp + tn)) if fp + tn else None,
        "log_loss": (float(log_loss(labels, probabilities, labels=[0, 1]))
                     if score_kind == "probability" else None),
        "confusion_matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
    }
