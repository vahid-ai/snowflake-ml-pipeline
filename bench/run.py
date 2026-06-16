from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from bench.harness import run_scenario, write_summary
from bench.lamda_scenarios import get_scenario, scenario_names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark and compare dlt pipeline methods against local destinations."
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=["all", *scenario_names()],
        default=None,
        help="Scenario to run. Repeat to compare several scenarios. Defaults to all.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("bench/artifacts"),
        help="Directory for metrics, flame graphs, and benchmark DuckDB files.",
    )
    parser.add_argument(
        "--limit-per-file",
        type=int,
        default=10,
        help="Rows per Parquet file for the manifest_parquet_duckdb scenario. Omit with --full.",
    )
    parser.add_argument(
        "--limit-per-split",
        type=int,
        default=100,
        help="Rows per Hugging Face split for the streaming_dlt_duckdb scenario.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=4,
        help="Max Parquet files for smoke benchmarks. Omit with --full.",
    )
    parser.add_argument(
        "--max-splits",
        type=int,
        default=4,
        help="Max Hugging Face splits for streaming smoke benchmarks. Omit with --full.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1_000,
        help="Streaming dlt batch size.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full Parquet materialization instead of the default smoke-size benchmark.",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="Disable pyinstrument HTML flame graph generation.",
    )
    parser.add_argument(
        "--no-source-byte-probe",
        action="store_true",
        help="Skip HEAD requests used to estimate source ingress bytes.",
    )
    return parser.parse_args()


def selected_scenarios(raw: list[str] | None) -> list[str]:
    if raw is None or "all" in raw:
        return scenario_names()
    return raw


def main() -> None:
    args = parse_args()
    options = {
        "limit_per_file": None if args.full else args.limit_per_file,
        "limit_per_split": args.limit_per_split,
        "max_files": None if args.full else args.max_files,
        "max_splits": None if args.full else args.max_splits,
        "batch_size": args.batch_size,
        "probe_remote_bytes": not args.no_source_byte_probe,
    }

    metrics = []
    for scenario_name in selected_scenarios(args.scenario):
        metric = run_scenario(
            scenario_name,
            get_scenario(scenario_name),
            artifact_root=args.artifact_dir,
            options=options,
            profile=not args.no_profile,
        )
        metrics.append(metric)
        print(json.dumps(asdict(metric), indent=2))

    summary_path = write_summary(metrics, args.artifact_dir)
    print(f"Wrote benchmark summary to {summary_path}")


if __name__ == "__main__":
    main()
