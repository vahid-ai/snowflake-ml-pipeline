from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import dlt
import duckdb
from datasets import get_dataset_config_names
from dlt.destinations import duckdb as dlt_duckdb
from huggingface_hub import hf_hub_url, list_repo_files


DATASET_ID = "IQSeC-Lab/LAMDA"
DEFAULT_DB_PATH = Path("data/lamda.duckdb")
DEFAULT_DATASET_NAME = "raw_lamda"


def parquet_manifest() -> list[dict]:
    configs = set(get_dataset_config_names(DATASET_ID))
    files = []

    for repo_path in list_repo_files(DATASET_ID, repo_type="dataset"):
        if not repo_path.endswith(".parquet"):
            continue

        config_name = repo_path.split("/", 1)[0]
        if config_name not in configs:
            continue

        file_name = Path(repo_path).name
        if file_name.endswith("_train.parquet"):
            split_name = "train"
        elif file_name.endswith("_test.parquet"):
            split_name = "test"
        else:
            continue

        files.append(
            {
                "dataset_id": DATASET_ID,
                "config_name": config_name,
                "split_name": split_name,
                "repo_path": repo_path,
                "url": hf_hub_url(DATASET_ID, repo_path, repo_type="dataset"),
            }
        )

    return sorted(files, key=lambda row: (row["config_name"], row["repo_path"]))


@dlt.resource(name="lamda_files", write_disposition="replace")
def lamda_files() -> Iterator[dict]:
    yield from parquet_manifest()


def quote_sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def materialize_samples(
    db_path: Path,
    files: list[dict],
    limit_per_file: int | None = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    urls = "[" + ", ".join(quote_sql(file["url"]) for file in files) + "]"
    con = duckdb.connect(str(db_path))
    con.execute("set preserve_insertion_order = false")
    con.execute("set threads = 2")
    con.execute(f"create schema if not exists {DEFAULT_DATASET_NAME}")

    if limit_per_file is not None:
        materialize_limited_samples(con, files, limit_per_file)
        con.close()
        return

    urls = "[" + ", ".join(quote_sql(file["url"]) for file in files) + "]"
    if limit_per_file is None:
        row_number_expression = "0::bigint as row_number"
    else:
        row_number_expression = (
            "row_number() over (partition by source_file order by hash) - 1 as row_number"
        )

    con.execute(
        f"""
        create or replace table {DEFAULT_DATASET_NAME}.lamda_samples as
        with scanned as (
            select
                *,
                filename as source_file,
                regexp_extract(filename, '/(Baseline|var_thresh_0\\.01)/', 1) as config_name,
                regexp_extract(filename, '_(train|test)\\.parquet', 1) as split_name
            from read_parquet({urls}, union_by_name = true, filename = true)
        ),
        numbered as (
            select
                {quote_sql(DATASET_ID)} as dataset_id,
                config_name,
                split_name,
                {row_number_expression},
                source_file,
                * exclude (filename, source_file, config_name, split_name)
            from scanned
        )
        select *
        from numbered
        """
    )
    con.close()


def materialize_limited_samples(
    con: duckdb.DuckDBPyConnection,
    files: list[dict],
    limit_per_file: int,
) -> None:
    first_url = quote_sql(files[0]["url"])
    con.execute(
        f"""
        create or replace table {DEFAULT_DATASET_NAME}.lamda_samples as
        select
            {quote_sql(DATASET_ID)} as dataset_id,
            {quote_sql(files[0]["config_name"])} as config_name,
            {quote_sql(files[0]["split_name"])} as split_name,
            0::bigint as row_number,
            {quote_sql(files[0]["url"])} as source_file,
            *
        from read_parquet({first_url})
        limit 0
        """
    )

    for file in files:
        con.execute(
            f"""
            insert into {DEFAULT_DATASET_NAME}.lamda_samples by name
            select
                {quote_sql(file["dataset_id"])} as dataset_id,
                {quote_sql(file["config_name"])} as config_name,
                {quote_sql(file["split_name"])} as split_name,
                row_number() over (order by hash) - 1 as row_number,
                {quote_sql(file["url"])} as source_file,
                *
            from (
                select *
                from read_parquet({quote_sql(file["url"])})
                limit {limit_per_file}
            )
            """
        )


def run_pipeline(db_path: Path, limit_per_file: int | None = None) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    files = parquet_manifest()

    pipeline = dlt.pipeline(
        pipeline_name="lamda_huggingface",
        destination=dlt_duckdb(credentials=str(db_path)),
        dataset_name=DEFAULT_DATASET_NAME,
    )
    load_info = pipeline.run(lamda_files())
    print(load_info)

    materialize_samples(db_path=db_path, files=files, limit_per_file=limit_per_file)
    print(f"Materialized {len(files)} Parquet files into {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load the IQSeC-Lab/LAMDA Hugging Face dataset into DuckDB."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="DuckDB database path. Defaults to data/lamda.duckdb.",
    )
    parser.add_argument(
        "--limit-per-file",
        type=int,
        default=None,
        help="Optional row limit per Parquet file, useful for smoke tests.",
    )
    args = parser.parse_args()

    run_pipeline(db_path=args.db_path, limit_per_file=args.limit_per_file)


if __name__ == "__main__":
    main()
