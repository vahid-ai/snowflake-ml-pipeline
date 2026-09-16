"""Bounded Parquet batches with Iceberg projection, filters and positional deletes.

PyIceberg's ArrowScan materializes every batch of a file before yielding it and
its dataset scanner can prefetch large, wide batches. Here compressed files are
spooled to temporary disk, then decoded in the requested batch size. Iceberg's
own field-ID, partition-value and delete helpers retain its table semantics.
The helper API is covered against the PyIceberg version pinned by uv.lock.
"""
from __future__ import annotations

import shutil
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.expressions.visitors import bind, extract_field_ids, translate_column_names
from pyiceberg.io.pyarrow import (
    _combine_positional_deletes, _get_column_projection_values, _read_all_delete_files,
    _to_requested_schema, expression_to_pyarrow, pyarrow_to_schema,
)
from pyiceberg.manifest import FileFormat
from pyiceberg.schema import prune_columns
from pyiceberg.types import ListType, MapType


def batches(table, scan, batch_size):
    projection = scan.projection()
    metadata = scan.table_metadata
    bound = bind(metadata.schema(), scan.row_filter, case_sensitive=scan.case_sensitive)
    ids = {i for i in projection.field_ids if not isinstance(projection.find_type(i), (MapType, ListType))}
    ids.update(extract_field_ids(bound))
    for task in sorted(scan.plan_files(), key=lambda task: task.file.file_path):
        if task.file.file_format != FileFormat.PARQUET:
            raise ValueError("Bounded LAMDA reader supports Parquet Iceberg files only")
        deletes = _read_all_delete_files(table.io, [task]).get(task.file.file_path)
        with tempfile.TemporaryFile() as local:
            # Sequential compressed download avoids thousands of tiny R2 range reads.
            with table.io.new_input(task.file.file_path).open() as remote:
                shutil.copyfileobj(remote, local, length=1024 * 1024)
            local.seek(0)
            with pq.ParquetFile(local, pre_buffer=False, buffer_size=0) as parquet:
                physical = parquet.schema_arrow
                file_schema = pyarrow_to_schema(physical, metadata.name_mapping(),
                    downcast_ns_timestamp_to_us=metadata.format_version <= 2, format_version=metadata.format_version)
                missing = _get_column_projection_values(task.file, projection, metadata.schema(),
                    metadata.specs().get(task.file.spec_id), file_schema.field_ids)
                selected = prune_columns(file_schema, ids, select_full_types=False)
                predicate = None
                if bound is not AlwaysTrue():
                    translated = translate_column_names(bound, file_schema, case_sensitive=scan.case_sensitive,
                                                        projected_field_values=missing)
                    predicate = expression_to_pyarrow(bind(file_schema, translated, case_sensitive=scan.case_sensitive), file_schema)
                offset = 0
                for batch in parquet.iter_batches(batch_size=batch_size, columns=[c.name for c in selected.columns], use_threads=False):
                    end = offset + len(batch)
                    if deletes:
                        batch = batch.take(_combine_positional_deletes(deletes, offset, end))
                    offset = end
                    if predicate is not None:
                        filtered = pa.Table.from_batches([batch]).filter(predicate)
                        if not len(filtered):
                            continue
                        batch = filtered.combine_chunks().to_batches()[0]
                    if len(batch):
                        yield _to_requested_schema(projection, selected, batch,
                            downcast_ns_timestamp_to_us=metadata.format_version <= 2,
                            projected_missing_fields=missing, allow_timestamp_tz_mismatch=True)
