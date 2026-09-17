"""Model-independent orchestration: stage, track, fit, select, evaluate, persist."""
from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.metrics import average_precision_score

from scripts.lamda.data import ROOT, IcebergInput, SplitPolicy, binary_matrix, cached, digest, stage, write_json
from scripts.lamda.evaluation import choose_threshold, metrics
from scripts.lamda.models import create_adapter
from scripts.lamda.models.base import FittedModel, ModelAdapter, TrainingData
from scripts.lamda.tracking import TrackingConfig, TrackingSession


# Capture code, contract, dependency, and checkout provenance for model replay.
def environment_manifest():
    # Treat unavailable Git metadata as missing provenance rather than a training failure.
    def git(*args):
        result = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", *args],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    canonical_yaml = [p for p in ROOT.glob("feature-platform/**/*.yaml")
                      if not p.is_relative_to(ROOT / "feature-platform/generated")]
    files = [*ROOT.glob("scripts/**/*.py"), *canonical_yaml,
             ROOT / "pyproject.toml", ROOT / "uv.lock"]
    packages = {}
    for name in ("scikit-learn", "scipy", "numpy", "pyarrow", "pyiceberg", "joblib", "pyyaml",
                 "mlflow", "torch", "lightning"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"git_commit": git("rev-parse", "HEAD"), "git_status": git("status", "--porcelain"),
            "source_sha256": {str(p.relative_to(ROOT)): digest(p) for p in sorted(files)},
            "packages": packages}


# Preserve shard row order while collecting metadata and validated model scores.
def score(model: FittedModel, directory: Path, stems):
    metadata, predictions = [], []
    for matrix, meta in cached(directory, stems):
        predictions.append(model.scores(matrix))
        metadata.append(meta)
    return pa.concat_tables(metadata), np.concatenate(predictions)


def train(source: IcebergInput, output: Path, *, policy: SplitPolicy = SplitPolicy(),
          batch_size=4096, epochs=3, alphas=(0.0001, 0.001), model="sgd", model_options=None,
          adapter: ModelAdapter | None = None, tracking: TrackingConfig | None = None,
          observations_root=None, publish_catalog=None):
    """Every adapter uses the same split audit, MLflow lifecycle and held-out evaluation.

    Supply an adapter to extend the pipeline without editing orchestration, or choose
    a registered model. The legacy alphas argument is retained for SGD callers.
    """
    if batch_size < 1 or epochs < 1:
        raise ValueError("batch_size and epochs must be positive")
    if adapter is None:
        options = dict(model_options or {})
        if model == "sgd":
            options.setdefault("alphas", alphas)
        adapter = create_adapter(model, **options)
    configurations = adapter.candidates()
    if not configurations:
        raise ValueError("Model adapter returned no candidates")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "status.json", {"status": "running"})
    tracking = tracking or TrackingConfig()
    parent = None
    try:
        session = TrackingSession(tracking)
        with session.run(tracking.run_name or f"{adapter.name}-{output.name}", tags={
                "model.type": adapter.name, "feature_set": source.contract["id"],
                "source.snapshot_id": str(source.snapshot_id), "backend": adapter.backend}) as parent:
            run_info = {"run_id": parent.run_id, "experiment_id": session.experiment_id}
            write_json(output / "tracking.json", run_info)
            environment = environment_manifest()
            parent.params({"model": adapter.name, "epochs": epochs, "seed": policy.seed,
                           "staging_batch_size": batch_size, "feature_count": len(source.contract["columns"]),
                           "snapshot_id": source.snapshot_id, "split": policy.manifest()})
            parent.tag("mlflow.source.git.commit", environment["git_commit"] or "unknown")
            for name, value in (("input_contract.json", source.contract), ("source.json", source.manifest),
                                ("environment.json", environment)):
                write_json(output / name, value)
                parent.artifact(output / name)
            directory = output / "cache"
            try:
                data = stage(source, directory, policy, batch_size, model=adapter.name,
                             observations_root=observations_root, publish_catalog=publish_catalog)
            finally:
                for name in ("report.json", "report.html", "diagnostics.txt"):
                    path = output / "cache_audit" / name
                    if path.exists():
                        parent.artifact(path, "audit")
            parent.artifact(directory / "manifest.json", "data_manifest")
            parent.metrics({"data": {split: {"benign": count[0], "malware": count[1]}
                                     for split, count in data["counts"].items()}})
            # Release only certified training shards to adapters; held-out partitions stay with
            # the orchestrator.
            training = TrainingData(directory, tuple(data["shards"]["train"]),
                                    tuple(data["counts"]["train"]), len(source.contract["columns"]))
            best_model, best_ap, best_validation = None, -1.0, None
            selected_parameters, selected_path, selected_run_id = None, None, None
            candidates = []
            # Fit and track each candidate separately, then select using validation average
            # precision.
            for index, parameters in enumerate(configurations):
                candidate_dir = output / "candidates" / str(index)
                candidate_dir.mkdir(parents=True)
                with session.run(f"{adapter.name}-{index}", parent=parent, tags={"model.type": adapter.name}) as run:
                    run.params({**parameters, "epochs": epochs, "seed": policy.seed,
                                "feature_count": training.n_features})
                    fitted = adapter.fit(training, parameters, epochs=epochs, seed=policy.seed,
                                         run=run, directory=candidate_dir)
                    validation, scores = score(fitted, directory, data["shards"]["validation"])
                    ap = float(average_precision_score(validation["label"].to_numpy(), scores))
                    run.metrics({"validation.average_precision": ap})
                    run.tag("score.kind", fitted.score_kind)
                    candidate = {"parameters": parameters, "validation_average_precision": ap}
                    candidates.append(candidate)
                    write_json(candidate_dir / "selection.json", candidate)
                    artifact = {"format_version": 2, "model_type": adapter.name,
                                "model": fitted.predictor, "score_kind": fitted.score_kind,
                                "threshold": choose_threshold(validation["label"].to_numpy(), scores),
                                "contract": source.contract, "source": source.manifest,
                                "split": policy.manifest(), "environment": environment,
                                "parameters": parameters, "tracking": {**run_info, "candidate_run_id": run.run_id}}
                    joblib.dump(artifact, candidate_dir / "model.joblib")
                    run.artifact(candidate_dir / "model.joblib")
                    run.artifact(candidate_dir / "selection.json")
                    if ap > best_ap:
                        best_model, best_ap, best_validation = fitted, ap, (validation, scores)
                        selected_parameters, selected_path = parameters, candidate_dir / "model.joblib"
                        selected_run_id = run.run_id
            validation, val_scores = best_validation
            threshold = choose_threshold(validation["label"].to_numpy(), val_scores)
            # Use the test partition only after model and threshold selection are complete.
            test, test_scores = score(best_model, directory, data["shards"]["test"])
            test_y = test["label"].to_numpy()
            years = np.array([m[:4] for m in test["year_month"].to_pylist()])
            count = np.asarray(data["counts"]["train"])
            kind = best_model.score_kind
            report = {
                "model": adapter.name, "score_kind": kind, "selection": candidates,
                "selected_parameters": selected_parameters,
                "threshold_policy": "Maximum validation F1, higher threshold wins ties",
                "validation": metrics(validation["label"].to_numpy(), val_scores, threshold, score_kind=kind),
                "test": metrics(test_y, test_scores, threshold, score_kind=kind),
                "test_by_year": {year: metrics(test_y[years == year], test_scores[years == year], threshold, score_kind=kind)
                                 for year in sorted(set(years))},
                "training_prior_baseline": metrics(test_y, np.full(len(test_y), count[1] / count.sum()), 0.5),
            }
            if kind == "probability":
                report["test_at_0_5"] = metrics(test_y, test_scores, 0.5)
            if adapter.name == "sgd":
                report["selected_alpha"] = selected_parameters["alpha"]
            # Promote the selected artifact and record predictions, metrics, hashes, and
            # provenance for replay.
            model_path = output / "model.joblib"
            shutil.copyfile(selected_path, model_path)
            score_column = "malware_probability" if kind == "probability" else "anomaly_score"
            scored = test.append_column(score_column, pa.array(test_scores))
            pq.write_table(scored.append_column("prediction", pa.array((test_scores >= threshold).astype(np.int8))),
                           output / "test_predictions.parquet")
            write_json(output / "metrics.json", report)
            manifest = {
                "status": "complete", "created_at": datetime.now(timezone.utc).isoformat(),
                "experiment": "lamda.model_comparison@1", "feature_set": source.contract["id"],
                "source": source.manifest, "split": policy.manifest(), "class_counts": data["counts"],
                "backend": adapter.backend, "environment": environment,
                "training": {"model": adapter.name, "epochs": epochs, "batch_size": batch_size,
                             "parameters": selected_parameters, "seed": policy.seed},
                "score_kind": kind, "tracking": {**run_info, "selected_candidate_run_id": selected_run_id},
                "fitted_artifact": {"path": "model.joblib", "sha256": digest(model_path)},
                "input_contract_sha256": digest(output / "input_contract.json"),
            }
            write_json(output / "manifest.json", manifest)
            parent.tag("selected_candidate_run_id", selected_run_id)
            parent.tag("score.kind", kind)
            parent.params({"selected_parameters": selected_parameters})
            parent.metrics({key: value for key, value in report.items() if key != "selection"})
            for name in ("model.joblib", "metrics.json", "manifest.json", "tracking.json"):
                parent.artifact(output / name)
        write_json(output / "status.json", {"status": "complete"})
        print(f"MLflow run: {parent.run_id}", flush=True)
        return report
    except BaseException:
        write_json(output / "status.json", {"status": "failed"})
        if parent is not None:
            try:
                parent.artifact(output / "status.json")
            except Exception:
                pass
        raise


# Apply the artifact-frozen input contract and threshold, including the legacy artifact
# compatibility path.
def predict(artifact, batch):
    version = artifact.get("format_version")
    if version not in (1, 2):
        raise ValueError("Unsupported model artifact version")
    contract = artifact["contract"]
    for name in ("config_name", "dataset_id"):
        if name not in batch.schema.names or any(v != contract[name] for v in batch[name].to_pylist()):
            raise ValueError(f"Inference requires matching {name} on every row")
    if "definitions" in contract:
        from scripts.lamda.audit import Issues
        from scripts.lamda.contracts import Plan
        issues = Issues()
        matrix = Plan(contract).execute(batch, issues, model=artifact.get("model_type"))
        if matrix is None or issues.error_count:
            raise ValueError("Inference input audit failed: " + "; ".join(
                f"{i['code']} {i['feature']}: {i['message']}" for i in issues.records()[:10]))
    else:
        matrix = binary_matrix(batch, contract["columns"])
    kind = artifact.get("score_kind", "probability")
    if version == 1:
        scores = artifact["model"].predict_proba(matrix)[:, 1]
    else:
        scores = FittedModel(artifact["model"], kind).scores(matrix)
    column = "malware_probability" if kind == "probability" else "anomaly_score"
    result = pa.table({column: scores, "prediction": (scores >= artifact["threshold"]).astype(np.int8)})
    if "hash" in batch.schema.names:
        result = result.append_column("hash", batch["hash"])
    return result
