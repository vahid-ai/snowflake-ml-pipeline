"""Standalone LAMDA preflight: full raw EDA, engineered contracts and model input trace."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyiceberg.catalog import load_catalog
from scripts.lamda.audit import run_audit
from scripts.lamda.data import ROOT, IcebergInput, SplitPolicy, load_contract
from scripts.lamda.tracking import TrackingConfig, TrackingSession
from scripts.load_lamda_r2_iceberg import r2_catalog_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="raw_lamda.lamda_samples")
    parser.add_argument("--local-root", type=Path)
    parser.add_argument("--snapshot-id", type=int)
    parser.add_argument("--feature-set", default="lamda.malware_baseline@1")
    parser.add_argument("--model", choices=["sgd", "mlp", "autoencoder"], default="sgd")
    parser.add_argument("--batch-size", type=int, default=8192)
    parser.add_argument("--max-rows", type=int, help="Advisory sample only; cannot certify or update observations")
    parser.add_argument("--examples", type=int, default=3, help="Maximum provenance examples per diagnostic; 0 omits them")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-through")
    parser.add_argument("--validation-through")
    parser.add_argument("--output", type=Path, default=Path("data/lamda_audits") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--observations-root", type=Path, default=ROOT / "feature-platform/generated/raw_profiles")
    parser.add_argument("--no-publish-observations", action="store_true")
    parser.add_argument("--mlflow-tracking-uri")
    args = parser.parse_args()
    policy = SplitPolicy(args.seed, args.train_through, args.validation_through)
    if args.local_root:
        from scripts.load_lamda_local_iceberg import open_local_catalog
        catalog = open_local_catalog(args.local_root)
    else:
        catalog = load_catalog("lamda_r2", **r2_catalog_config())
    try:
        source = IcebergInput(catalog.load_table(args.table), load_contract(args.feature_set), args.snapshot_id)
        session = TrackingSession(TrackingConfig(uri=args.mlflow_tracking_uri))
        with session.run("audit-" + args.output.name, tags={"phase": "audit", "feature_set": args.feature_set}) as run:
            report = run_audit(source, args.output, policy=policy, batch_size=args.batch_size,
                               max_rows=args.max_rows, model=args.model, examples=args.examples,
                               observations_root=None if args.no_publish_observations else args.observations_root,
                               publish_catalog=None if args.no_publish_observations else catalog)
            run.params({"snapshot_id": source.snapshot_id, "contract_sha256": report["contract_sha256"]})
            run.metrics({"audit.rows": report["rows"], "audit.certified": int(report["certified"]),
                         "audit.diagnostics": len(report["issues"])})
            for name in ("report.json", "report.html", "diagnostics.txt"):
                run.artifact(args.output / name, "audit")
            if report["status"] == "failed":
                raise SystemExit(2)
        # Advisory runs finish successfully as an audit operation, with exit 3 so CI
        # cannot mistake a sample for a passing training gate.
        if not report["certified"]:
            raise SystemExit(3)
    finally:
        catalog.close()


if __name__ == "__main__":
    main()
