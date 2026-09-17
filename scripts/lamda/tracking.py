"""Explicit, mandatory MLflow tracking shared by every model adapter.

No environment dump, source rows, or data cache is sent to the tracking store.
The pipeline owns run lifetimes, including failures in staging and evaluation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time

from mlflow import MlflowClient
from mlflow.entities import Metric, Param

from scripts.lamda.data import ROOT


# Carry explicit tracking destinations and names without relying on a global active MLflow run.
@dataclass(frozen=True)
class TrackingConfig:
    uri: str | None = None
    experiment: str = "lamda-malware"
    run_name: str | None = None


# Bind logging operations to one run ID, including nested candidate runs.
class TrackedRun:
    # Keep the client and run identity together to avoid logging to a different active run.
    def __init__(self, client: MlflowClient, run_id: str):
        self.client, self.run_id = client, run_id

    # Serialize structured parameters and send bounded batches to the tracking service.
    def params(self, values: dict):
        params = [Param(str(k), json.dumps(v) if isinstance(v, (dict, list, tuple)) else str(v))
                  for k, v in values.items()]
        for offset in range(0, len(params), 100):
            self.client.log_batch(self.run_id, params=params[offset:offset + 100])

    def metrics(self, values: dict, step: int = 0):
        """Flatten finite scalar metrics; confusion matrices stay in JSON artifacts."""
        flat = {}
        # Flatten nested metric dictionaries; skip arrays and non-finite values.
        def visit(prefix, value):
            if isinstance(value, dict):
                for key, item in value.items():
                    visit(f"{prefix}.{key}" if prefix else str(key), item)
            elif isinstance(value, (int, float)) and math.isfinite(value):
                flat[prefix] = float(value)
        visit("", values)
        now = int(time.time() * 1000)
        items = [Metric(k, v, now, step) for k, v in flat.items()]
        for offset in range(0, len(items), 100):
            self.client.log_batch(self.run_id, metrics=items[offset:offset + 100])

    # Upload a selected file to an optional artifact subdirectory for this run.
    def artifact(self, path: Path, artifact_path: str | None = None):
        self.client.log_artifact(self.run_id, str(path), artifact_path)

    # Attach searchable descriptive metadata to this run.
    def tag(self, key: str, value: str):
        self.client.set_tag(self.run_id, key, value)


# Own experiment selection and run lifetimes for a complete pipeline invocation.
class TrackingSession:
    # Use an explicit or environment tracking URI, falling back to local SQLite and local
    # artifacts.
    def __init__(self, config: TrackingConfig):
        # SQLite works without a tracking server and supports the MLflow UI.
        default = ROOT / "data/mlflow"
        uri = config.uri or os.getenv("MLFLOW_TRACKING_URI")
        local_default = uri is None
        if local_default:
            default.mkdir(parents=True, exist_ok=True)
            uri = "sqlite:///" + (default / "mlflow.db").as_posix()
        self.client = MlflowClient(tracking_uri=uri)
        experiment = self.client.get_experiment_by_name(config.experiment)
        if experiment is None:
            artifact_location = (default / "artifacts").as_uri() if local_default else None
            self.experiment_id = self.client.create_experiment(config.experiment, artifact_location)
        else:
            if experiment.lifecycle_stage != "active":
                raise ValueError("The configured MLflow experiment is deleted")
            self.experiment_id = experiment.experiment_id

    # Create a parent or child run and terminate it correctly on success, failure, or
    # interruption.
    @contextmanager
    def run(self, name: str, *, parent: TrackedRun | None = None, tags: dict | None = None):
        run_tags = {"mlflow.runName": name, **(tags or {})}
        if parent:
            run_tags["mlflow.parentRunId"] = parent.run_id
        info = self.client.create_run(self.experiment_id, tags=run_tags)
        run = TrackedRun(self.client, info.info.run_id)
        try:
            yield run
        except BaseException as exc:
            # Exception messages can contain signed URLs or credentials. Record type only.
            try:
                run.tag("failure.type", type(exc).__name__)
                self.client.set_terminated(run.run_id, "KILLED" if isinstance(exc, KeyboardInterrupt) else "FAILED")
            except Exception:
                pass  # Preserve the original failure if the tracking service is down.
            raise
        else:
            self.client.set_terminated(run.run_id, "FINISHED")
