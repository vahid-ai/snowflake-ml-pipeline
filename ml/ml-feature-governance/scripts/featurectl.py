#!/usr/bin/env python3
"""Small policy validator for the canonical feature-platform definitions.

The validator deliberately validates semantics that JSON Schema alone cannot express:
versioned references, DAG acyclicity, stateful-transform boundaries, deterministic hashing,
and time/leakage invariants.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

PRIMITIVES = {
    "bool", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
    "float16", "float32", "float64", "utf8", "binary", "date32", "date64"
}
SEMANTIC_TYPES = {
    "continuous", "numerical", "categorical", "binary", "embedding", "vector", "timestamp",
    "text", "identifier"
}
TRANSFORM_KINDS = {"expression", "builtin", "fitted", "model", "window_aggregate", "plugin"}
BACKEND_CODE_KEYS = {
    "sql", "spark_sql", "spark_expr", "pyspark", "python", "python_code", "polars", "duckdb_sql",
    "snowflake_sql", "cudf", "cuda", "dask", "ray", "nvtabular", "udf", "code"
}
VERSIONED_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*@[1-9][0-9]*$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
TYPE_PATTERNS = [
    re.compile(r"^timestamp\[(s|ms|us|ns)(,\s*[^\]]+)?\]$"),
    re.compile(r"^decimal(128|256)\([1-9][0-9]*,\s*[0-9]+\)$"),
    re.compile(r"^list<.+>$"),
    re.compile(r"^fixed_size_list<.+,\s*[1-9][0-9]*>$"),
    re.compile(r"^struct<.+>$"),
    re.compile(r"^map<.+,.+>$"),
]
CHANGE_STATUSES = {"proposed", "approved", "implemented", "verified", "superseded"}
CHANGE_TYPES = {
    "implementation_only", "additive_semantic", "breaking_semantic",
    "backend_support", "migration", "experiment_only"
}
COMPATIBILITY = {"backward_compatible", "breaking", "migration_required"}
VERIFICATION_KINDS = {
    "contract", "dag", "leakage", "portability", "golden_differential",
    "backward_compatibility", "migration"
}

class ValidationError(Exception):
    pass


def load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise ValidationError(
            f"{path}: YAML requires PyYAML>=6. Install PyYAML or use JSON canonical definitions. ({exc})"
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as exc:
        raise ValidationError(f"{path}: invalid YAML: {exc}")


def load_doc(path: Path) -> Any:
    if path.suffix.lower() == ".json":
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValidationError(f"{path}: invalid JSON: {exc}")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return load_yaml(path)
    return None


def docs_under(root: Path, subdir: str) -> Iterable[tuple[Path, Any]]:
    d = root / subdir
    if not d.exists():
        return []
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}:
            out.append((p, load_doc(p)))
    return out


def valid_type(t: Any) -> bool:
    if not isinstance(t, str) or not t.strip():
        return False
    t = t.strip()
    if t in PRIMITIVES:
        return True
    return any(p.match(t) for p in TYPE_PATTERNS)


def walk_keys(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def feature_key(feature: dict[str, Any]) -> str | None:
    fid = feature.get("id")
    version = feature.get("version")
    if isinstance(fid, str) and isinstance(version, int) and version > 0:
        return f"{fid}@{version}"
    return None


def positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_iceberg_reference(value: Any, prefix: str, errors: list[str], field: bool = False) -> None:
    if not isinstance(value, dict):
        errors.append(f"{prefix}: storage/dataset reference must be a mapping")
        return
    if value.get("format") != "iceberg":
        if "iceberg_field_id" in value or "iceberg_snapshot_id" in value:
            errors.append(f"{prefix}: Iceberg IDs require format: iceberg")
        return
    if not isinstance(value.get("table"), str) or not value["table"].strip():
        errors.append(f"{prefix}: Iceberg reference requires table identity")
    required_id = "iceberg_field_id" if field else "iceberg_snapshot_id"
    if not positive_id(value.get(required_id)):
        errors.append(f"{prefix}: {required_id} must be a positive integer")
    for key in ("iceberg_snapshot_id", "iceberg_field_id"):
        if key in value and not positive_id(value[key]):
            errors.append(f"{prefix}: invalid {key}")
    if "column" in value and (not isinstance(value["column"], str) or not value["column"].strip()):
        errors.append(f"{prefix}: column must be a non-empty string")
    if "doc" in value:
        doc = value["doc"]
        if not isinstance(doc, str) or not doc.strip() or doc.lstrip().startswith(("{", "[")):
            errors.append(f"{prefix}: Iceberg doc must be human-readable prose, not serialized configuration")
    if {"ml", "normalization", "transform", "tags", "governance", "metadata"}.intersection(value):
        errors.append(f"{prefix}: ML/catalog metadata belongs outside the Iceberg storage binding")


def validate_feature(path: Path, f: Any, errors: list[str]) -> str | None:
    prefix = str(path)
    if not isinstance(f, dict):
        errors.append(f"{prefix}: feature entry must be a mapping")
        return None

    fid = f.get("id")
    ver = f.get("version")
    if not isinstance(fid, str) or not ID_RE.match(fid):
        errors.append(f"{prefix}: feature id must match {ID_RE.pattern!r}; got {fid!r}")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        errors.append(f"{prefix}: feature {fid!r} version must be an integer >= 1")

    semantic = f.get("semantic_type")
    if semantic not in SEMANTIC_TYPES:
        errors.append(f"{prefix}: {fid!r} semantic_type must be one of {sorted(SEMANTIC_TYPES)}")

    output = f.get("output")
    if not isinstance(output, dict):
        errors.append(f"{prefix}: {fid!r} requires output mapping")
    else:
        if not valid_type(output.get("type")):
            errors.append(f"{prefix}: {fid!r} has invalid/ambiguous output type {output.get('type')!r}")
        if "nullable" not in output or not isinstance(output.get("nullable"), bool):
            errors.append(f"{prefix}: {fid!r} output.nullable must be explicit boolean")

    if "description" in f and (not isinstance(f["description"], str) or not f["description"].strip()):
        errors.append(f"{prefix}: description must be non-empty prose")
    if "storage" in f:
        validate_iceberg_reference(f["storage"], f"{prefix}: {fid}", errors, field=True)
    ml = f.get("ml")
    if "normalization" in f or (isinstance(ml, dict) and "normalization" in ml):
        errors.append(f"{prefix}: {fid}: normalization requires a separate versioned fitted-transform node")

    raw = "source" in f or "column" in f
    has_transform = "transform" in f
    if raw and has_transform:
        errors.append(f"{prefix}: {fid!r} cannot be both a raw source feature and transformed feature")
    if raw:
        if not isinstance(f.get("source"), str) or not isinstance(f.get("column"), str):
            errors.append(f"{prefix}: raw feature {fid!r} requires string source and column")
        if f.get("inputs"):
            errors.append(f"{prefix}: raw feature {fid!r} must not declare feature inputs")
    elif not has_transform:
        errors.append(f"{prefix}: {fid!r} must declare either source+column or transform")

    if has_transform:
        inputs = f.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append(f"{prefix}: transformed feature {fid!r} requires non-empty versioned inputs")
        else:
            for ref in inputs:
                if not isinstance(ref, str) or not VERSIONED_REF.match(ref):
                    errors.append(f"{prefix}: {fid!r} input must be versioned id@N; got {ref!r}")
        tr = f.get("transform")
        if not isinstance(tr, dict):
            errors.append(f"{prefix}: {fid!r} transform must be mapping")
        else:
            kind = tr.get("kind")
            if kind not in TRANSFORM_KINDS:
                errors.append(f"{prefix}: {fid!r} transform.kind must be one of {sorted(TRANSFORM_KINDS)}")
            forbidden = BACKEND_CODE_KEYS.intersection(set(walk_keys(tr)))
            if forbidden and kind != "plugin":
                errors.append(
                    f"{prefix}: {fid!r} canonical transform leaks backend-specific/code keys {sorted(forbidden)}; "
                    "use portable ops or a plugin implementations map"
                )
            if kind == "plugin":
                if not isinstance(tr.get("operation"), str):
                    errors.append(f"{prefix}: plugin transform {fid!r} requires operation")
                impl = tr.get("implementations")
                if not isinstance(impl, dict) or not impl:
                    errors.append(f"{prefix}: plugin transform {fid!r} requires non-empty implementations mapping")
                eq = tr.get("equivalence") or f.get("equivalence")
                if not isinstance(eq, dict) or not eq.get("mode"):
                    errors.append(f"{prefix}: plugin transform {fid!r} requires an equivalence policy")
            if kind == "fitted":
                fit = tr.get("fit")
                if not isinstance(fit, dict) or not fit.get("split"):
                    errors.append(f"{prefix}: fitted transform {fid!r} requires transform.fit.split")
                state = tr.get("state")
                if state is not None:
                    if not isinstance(state, dict):
                        errors.append(f"{prefix}: fitted transform {fid!r} transform.state must be metadata mapping")
                    else:
                        mutable_keys = {"mean", "std", "scale", "median", "vocabulary", "quantiles", "categories"}
                        leaked = mutable_keys.intersection(state.keys())
                        if leaked:
                            errors.append(
                                f"{prefix}: fitted transform {fid!r} embeds fitted values {sorted(leaked)}; "
                                "store them in an immutable artifact and reference it"
                            )
            if kind == "model":
                if not tr.get("model_ref"):
                    errors.append(f"{prefix}: model transform {fid!r} requires immutable model_ref")
            op = tr.get("op")
            if op == "stable_hash_bucket":
                params = tr.get("params")
                required = {"algorithm", "seed", "num_buckets", "encoding", "null_bucket"}
                missing = required - set(params.keys()) if isinstance(params, dict) else required
                if missing:
                    errors.append(f"{prefix}: {fid!r} stable_hash_bucket missing params {sorted(missing)}")
            if kind == "window_aggregate":
                window = tr.get("window")
                if not isinstance(window, dict):
                    errors.append(f"{prefix}: window feature {fid!r} requires transform.window")
                else:
                    for key in ("type", "duration", "time_column", "partition_by", "closed"):
                        if key not in window:
                            errors.append(f"{prefix}: window feature {fid!r} missing window.{key}")
                temporal = f.get("temporal")
                if not isinstance(temporal, dict) or temporal.get("point_in_time_required") is not True:
                    errors.append(f"{prefix}: window feature {fid!r} must set temporal.point_in_time_required: true")

    if semantic in {"embedding", "vector"}:
        model_rep = f.get("model_representation")
        if not isinstance(model_rep, dict):
            errors.append(f"{prefix}: vector/embedding {fid!r} requires model_representation")
        else:
            shape = model_rep.get("shape")
            if not isinstance(shape, list) or not shape or not all(isinstance(x, int) and x > 0 for x in shape):
                errors.append(f"{prefix}: vector/embedding {fid!r} requires positive integer model_representation.shape")
            if not valid_type(str(model_rep.get("dtype", ""))):
                errors.append(f"{prefix}: vector/embedding {fid!r} requires explicit valid model_representation.dtype")

    # Prevent engine implementation blocks at feature top level too.
    top_forbidden = BACKEND_CODE_KEYS.intersection(f.keys())
    if top_forbidden:
        errors.append(f"{prefix}: {fid!r} has backend/code fields at canonical feature level: {sorted(top_forbidden)}")

    return feature_key(f)


def validate_sources(root: Path, errors: list[str]) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for path, doc in docs_under(root, "sources"):
        entries = doc.get("sources") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'sources' list")
            continue
        for source in entries:
            if not isinstance(source, dict):
                errors.append(f"{path}: source entry must be mapping")
                continue
            source_id = source.get("id")
            if not isinstance(source_id, str) or not ID_RE.match(source_id):
                errors.append(f"{path}: source id invalid: {source_id!r}")
                continue
            if source_id in sources:
                errors.append(f"{path}: duplicate source {source_id!r}")
                continue
            columns = source.get("columns")
            if not isinstance(columns, dict) or not columns:
                errors.append(f"{path}: source {source_id!r} requires non-empty columns mapping")
                continue
            for column_name, column in columns.items():
                if not isinstance(column, dict):
                    errors.append(f"{path}: source {source_id!r} column {column_name!r} must be mapping")
                    continue
                if not valid_type(column.get("type")):
                    errors.append(
                        f"{path}: source {source_id!r} column {column_name!r} has invalid type {column.get('type')!r}"
                    )
                if not isinstance(column.get("nullable"), bool):
                    errors.append(
                        f"{path}: source {source_id!r} column {column_name!r} nullable must be explicit boolean"
                    )
            for timestamp_key in ("event_timestamp", "availability_timestamp"):
                column_name = source.get(timestamp_key)
                if column_name is None:
                    continue
                if column_name not in columns:
                    errors.append(
                        f"{path}: source {source_id!r} {timestamp_key} references unknown column {column_name!r}"
                    )
                elif not isinstance(columns[column_name], dict):
                    errors.append(
                        f"{path}: source {source_id!r} {timestamp_key} column {column_name!r} must be mapping"
                    )
                elif not str(columns[column_name].get("type", "")).startswith("timestamp["):
                    errors.append(
                        f"{path}: source {source_id!r} {timestamp_key} column {column_name!r} must be timestamp type"
                    )
            locations = source.get("locations")
            if not isinstance(locations, dict) or not locations:
                errors.append(f"{path}: source {source_id!r} requires at least one location")
            sources[source_id] = source
    if not sources:
        errors.append(f"{root / 'sources'} contains no source definitions")
    return sources


def validate_entities(root: Path, errors: list[str]) -> set[str]:
    entities: set[str] = set()
    for path, doc in docs_under(root, "entities"):
        entries = doc.get("entities") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'entities' list")
            continue
        for entity in entries:
            if not isinstance(entity, dict):
                errors.append(f"{path}: entity entry must be mapping")
                continue
            entity_id = entity.get("id")
            if not isinstance(entity_id, str) or not ID_RE.match(entity_id):
                errors.append(f"{path}: entity id invalid: {entity_id!r}")
                continue
            if entity_id in entities:
                errors.append(f"{path}: duplicate entity {entity_id!r}")
            entities.add(entity_id)
            join_keys = entity.get("join_keys")
            if not isinstance(join_keys, list) or not join_keys:
                errors.append(f"{path}: entity {entity_id!r} requires non-empty join_keys")
                continue
            for key in join_keys:
                if not isinstance(key, dict) or not isinstance(key.get("name"), str):
                    errors.append(f"{path}: entity {entity_id!r} join key requires string name")
                    continue
                if not valid_type(key.get("type")):
                    errors.append(
                        f"{path}: entity {entity_id!r} join key {key.get('name')!r} has invalid type {key.get('type')!r}"
                    )
                if not isinstance(key.get("nullable"), bool):
                    errors.append(
                        f"{path}: entity {entity_id!r} join key {key.get('name')!r} nullable must be explicit boolean"
                    )
    if not entities:
        errors.append(f"{root / 'entities'} contains no entity definitions")
    return entities


def validate_feature_bindings(
    features: dict[str, dict[str, Any]],
    origins: dict[str, Path],
    sources: dict[str, dict[str, Any]],
    entities: set[str],
    errors: list[str],
) -> None:
    for key, feature in features.items():
        entity = feature.get("entity")
        if not isinstance(entity, str) or entity not in entities:
            errors.append(f"{origins[key]}: feature {key} references unknown entity {entity!r}")
        if "source" not in feature:
            continue
        source_id = feature.get("source")
        column_name = feature.get("column")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if source is None:
            errors.append(f"{origins[key]}: raw feature {key} references unknown source {source_id!r}")
            continue
        columns = source.get("columns") if isinstance(source, dict) else None
        column = columns.get(column_name) if isinstance(columns, dict) and isinstance(column_name, str) else None
        if not isinstance(column, dict):
            errors.append(
                f"{origins[key]}: raw feature {key} references unknown source column {source_id}.{column_name}"
            )
            continue
        output = feature.get("output")
        if isinstance(output, dict):
            if output.get("type") != column.get("type"):
                errors.append(
                    f"{origins[key]}: raw feature {key} output type {output.get('type')!r} differs from "
                    f"source column type {column.get('type')!r}"
                )
            if output.get("nullable") != column.get("nullable"):
                errors.append(
                    f"{origins[key]}: raw feature {key} nullability differs from source column {source_id}.{column_name}"
                )


def validate_capabilities(
    root: Path,
    features: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    docs = list(docs_under(root, "capabilities"))
    if not docs:
        errors.append(f"{root / 'capabilities'} contains no backend capability definitions")
        return
    required_types: set[str] = set()
    for feature in features.values():
        output = feature.get("output")
        if isinstance(output, dict) and isinstance(output.get("type"), str):
            required_types.add(output["type"])
        model_rep = feature.get("model_representation")
        if isinstance(model_rep, dict) and isinstance(model_rep.get("dtype"), str):
            required_types.add(model_rep["dtype"])

    references: list[tuple[Path, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for path, doc in docs:
        backends = doc.get("backends") if isinstance(doc, dict) else None
        if not isinstance(backends, dict) or not backends:
            errors.append(f"{path}: expected non-empty 'backends' mapping")
            continue
        for name, backend in backends.items():
            if name in seen:
                errors.append(f"{path}: duplicate backend capability profile {name!r}")
            seen.add(str(name))
            if not isinstance(backend, dict):
                errors.append(f"{path}: backend {name!r} must be mapping")
                continue
            mappings = backend.get("type_mappings")
            if not isinstance(mappings, dict) or not mappings:
                errors.append(f"{path}: backend {name!r} requires non-empty type_mappings")
                continue
            for canonical_type, physical in mappings.items():
                if not valid_type(canonical_type):
                    errors.append(f"{path}: backend {name!r} has invalid canonical type mapping {canonical_type!r}")
                if not isinstance(physical, (str, dict)) or not physical:
                    errors.append(f"{path}: backend {name!r} mapping for {canonical_type!r} must be explicit")
            if backend.get("role") == "reference":
                references.append((path, str(name), backend))
    if len(references) != 1:
        errors.append(f"backend capabilities require exactly one reference backend; found {len(references)}")
    else:
        path, name, backend = references[0]
        mappings = backend.get("type_mappings", {})
        missing = required_types - set(mappings)
        if missing:
            errors.append(
                f"{path}: reference backend {name!r} lacks mappings for canonical/model types {sorted(missing)}"
            )


def validate_changes(root: Path, errors: list[str]) -> None:
    seen: set[str] = set()
    for path, doc in docs_under(root, "changes"):
        entries = doc.get("changes") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'changes' list")
            continue
        for change in entries:
            if not isinstance(change, dict):
                errors.append(f"{path}: change entry must be mapping")
                continue
            change_id, version = change.get("id"), change.get("version")
            if not isinstance(change_id, str) or not ID_RE.match(change_id):
                errors.append(f"{path}: change id invalid: {change_id!r}")
                continue
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                errors.append(f"{path}: change {change_id!r} version must be integer >= 1")
                continue
            key = f"{change_id}@{version}"
            if key in seen:
                errors.append(f"{path}: duplicate change contract {key}")
            seen.add(key)
            status = change.get("status")
            change_type = change.get("change_type")
            compatibility = change.get("compatibility")
            if status not in CHANGE_STATUSES:
                errors.append(f"{path}: change {key} status must be one of {sorted(CHANGE_STATUSES)}")
            if change_type not in CHANGE_TYPES:
                errors.append(f"{path}: change {key} change_type must be one of {sorted(CHANGE_TYPES)}")
            if compatibility not in COMPATIBILITY:
                errors.append(f"{path}: change {key} compatibility must be one of {sorted(COMPATIBILITY)}")
            if not isinstance(change.get("intent"), str) or not change["intent"].strip():
                errors.append(f"{path}: change {key} requires non-empty intent")
            for field in ("affected_features", "proposed_features", "affected_feature_sets"):
                refs = change.get(field, [])
                if not isinstance(refs, list):
                    errors.append(f"{path}: change {key} {field} must be list")
                    continue
                for ref in refs:
                    if not isinstance(ref, str) or not VERSIONED_REF.match(ref):
                        errors.append(f"{path}: change {key} {field} has unversioned reference {ref!r}")
            verification = change.get("verification")
            required = verification.get("required") if isinstance(verification, dict) else None
            if not isinstance(required, list):
                errors.append(f"{path}: change {key} verification.required must be list")
                required_checks: list[str] = []
            else:
                required_checks = [item for item in required if isinstance(item, str)]
                if len(required_checks) != len(required):
                    errors.append(f"{path}: change {key} verification.required entries must be strings")
                if len(required_checks) != len(set(required_checks)):
                    errors.append(f"{path}: change {key} verification.required contains duplicates")
            unknown_checks = set(required_checks) - VERIFICATION_KINDS
            if unknown_checks:
                errors.append(f"{path}: change {key} has unknown verification checks {sorted(unknown_checks)}")
            approval = change.get("approval")
            approval_required = approval.get("required") if isinstance(approval, dict) else None
            if not isinstance(approval_required, bool):
                errors.append(f"{path}: change {key} approval.required must be explicit boolean")
            if change_type in {"additive_semantic", "breaking_semantic"}:
                semantic_checks = {"contract", "dag", "leakage", "portability", "golden_differential"}
                missing_checks = semantic_checks - set(required_checks)
                if missing_checks:
                    errors.append(f"{path}: semantic change {key} missing verification checks {sorted(missing_checks)}")
            if compatibility in {"breaking", "migration_required"} or change_type in {"breaking_semantic", "migration"}:
                migration = change.get("migration")
                if not isinstance(migration, dict) or not isinstance(migration.get("strategy"), str) or not migration["strategy"].strip():
                    errors.append(f"{path}: breaking/migration change {key} requires migration.strategy")
                if approval_required is not True:
                    errors.append(f"{path}: breaking/migration change {key} must set approval.required: true")
                consumers = change.get("affected_consumers")
                if not isinstance(consumers, list):
                    errors.append(f"{path}: breaking/migration change {key} requires affected_consumers inventory")
                missing_checks = {"backward_compatibility", "migration"} - set(required_checks)
                if missing_checks:
                    errors.append(f"{path}: breaking/migration change {key} missing checks {sorted(missing_checks)}")
            if approval_required is True and status in {"approved", "implemented", "verified"}:
                decision = approval.get("decision") if isinstance(approval, dict) else None
                approved_by = approval.get("approved_by") if isinstance(approval, dict) else None
                approved_at = approval.get("approved_at") if isinstance(approval, dict) else None
                if decision != "approved" or not isinstance(approved_by, str) or not approved_by.strip() or not isinstance(approved_at, str) or not approved_at.strip():
                    errors.append(
                        f"{path}: change {key} status {status!r} requires recorded approval decision, approved_by, and approved_at"
                    )


def validate_feature_sets(root: Path, known: dict[str, dict[str, Any]], errors: list[str]) -> set[str]:
    set_keys: set[str] = set()
    for path, doc in docs_under(root, "feature_sets"):
        if doc is None:
            continue
        entries = doc.get("feature_sets") if isinstance(doc, dict) else None
        if entries is None and isinstance(doc, dict) and "id" in doc:
            entries = [doc]
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'feature_sets' list")
            continue
        for s in entries:
            if not isinstance(s, dict):
                errors.append(f"{path}: feature_set entry must be mapping")
                continue
            sid, ver = s.get("id"), s.get("version")
            if not isinstance(sid, str) or not ID_RE.match(sid):
                errors.append(f"{path}: feature_set id invalid: {sid!r}")
                continue
            if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
                errors.append(f"{path}: feature_set {sid!r} version must be integer >=1")
                continue
            skey = f"{sid}@{ver}"
            if skey in set_keys:
                errors.append(f"{path}: duplicate feature_set {skey}")
            set_keys.add(skey)
            refs = s.get("features")
            if not isinstance(refs, list) or not refs:
                errors.append(f"{path}: feature_set {skey} requires ordered non-empty features list")
                continue
            seen = set()
            for ref in refs:
                if not isinstance(ref, str) or not VERSIONED_REF.match(ref):
                    errors.append(f"{path}: feature_set {skey} contains unversioned feature ref {ref!r}")
                    continue
                if ref not in known:
                    errors.append(f"{path}: feature_set {skey} references unknown feature {ref}")
                if ref in seen:
                    errors.append(f"{path}: feature_set {skey} duplicates feature {ref}; ordering must be unique")
                seen.add(ref)

            layout = s.get("output_layout")
            if layout is not None:
                if not isinstance(layout, dict):
                    errors.append(f"{path}: feature_set {skey} output_layout must be mapping")
                else:
                    laid_out: set[str] = set()
                    for group_name, group in layout.items():
                        if not isinstance(group, dict):
                            errors.append(f"{path}: feature_set {skey} output_layout.{group_name} must be mapping")
                            continue
                        dtype = group.get("dtype")
                        if not valid_type(dtype):
                            errors.append(f"{path}: feature_set {skey} output_layout.{group_name}.dtype invalid: {dtype!r}")
                        group_refs = group.get("features")
                        if not isinstance(group_refs, list):
                            errors.append(f"{path}: feature_set {skey} output_layout.{group_name}.features must be list")
                            continue
                        for ref in group_refs:
                            if ref not in refs:
                                errors.append(f"{path}: feature_set {skey} layout references feature not in feature set: {ref}")
                                continue
                            if ref in laid_out:
                                errors.append(f"{path}: feature_set {skey} layout duplicates feature {ref} across groups")
                            laid_out.add(ref)
                            feat = known.get(ref)
                            if feat and valid_type(dtype):
                                model_rep = feat.get("model_representation") if isinstance(feat, dict) else None
                                expected_dtype = None
                                if isinstance(model_rep, dict):
                                    expected_dtype = model_rep.get("dtype")
                                if expected_dtype is None:
                                    output = feat.get("output") if isinstance(feat, dict) else None
                                    if isinstance(output, dict):
                                        expected_dtype = output.get("type")
                                if isinstance(expected_dtype, str) and expected_dtype != dtype:
                                    errors.append(
                                        f"{path}: feature_set {skey} layout dtype {dtype} for {ref} differs from "
                                        f"declared model/output dtype {expected_dtype}; declare model_representation explicitly"
                                    )
                    missing_layout = set(refs) - laid_out
                    if missing_layout:
                        errors.append(f"{path}: feature_set {skey} output_layout omits features {sorted(missing_layout)}")
    return set_keys


def validate_experiments(root: Path, known_sets: set[str], known_features: set[str], errors: list[str]) -> None:
    for path, doc in docs_under(root, "experiments"):
        if doc is None:
            continue
        entries = doc.get("experiments") if isinstance(doc, dict) else None
        if entries is None and isinstance(doc, dict) and "id" in doc:
            entries = [doc]
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'experiments' list")
            continue
        for e in entries:
            if not isinstance(e, dict):
                errors.append(f"{path}: experiment entry must be mapping")
                continue
            eid = e.get("id")
            base = e.get("base_feature_set")
            if not isinstance(eid, str) or not ID_RE.match(eid):
                errors.append(f"{path}: experiment id invalid: {eid!r}")
            if not isinstance(base, str) or not VERSIONED_REF.match(base):
                errors.append(f"{path}: experiment {eid!r} base_feature_set must be versioned id@N")
            elif base not in known_sets:
                errors.append(f"{path}: experiment {eid!r} references unknown feature_set {base}")
            if "dataset" in e:
                validate_iceberg_reference(e["dataset"], f"{path}: experiment {eid}", errors)
            if "datasets" in e:
                datasets = e["datasets"]
                if not isinstance(datasets, list) or not datasets:
                    errors.append(f"{path}: datasets must be a non-empty list")
                else:
                    for dataset in datasets:
                        validate_iceberg_reference(dataset, f"{path}: experiment {eid}", errors)
            variants = e.get("variants")
            if not isinstance(variants, list) or not variants:
                errors.append(f"{path}: experiment {eid!r} requires variants list")
                continue
            for v in variants:
                if not isinstance(v, dict) or not isinstance(v.get("name"), str):
                    errors.append(f"{path}: experiment {eid!r} variant requires string name")
                    continue
                for action in ("include", "exclude", "replace"):
                    refs = v.get(action, [])
                    if refs is None:
                        continue
                    if action == "replace" and isinstance(refs, dict):
                        refs_iter = list(refs.keys()) + list(refs.values())
                    elif isinstance(refs, list):
                        refs_iter = refs
                    else:
                        errors.append(f"{path}: experiment {eid!r} variant {v.get('name')} {action} has wrong shape")
                        continue
                    for ref in refs_iter:
                        if not isinstance(ref, str) or not VERSIONED_REF.match(ref):
                            errors.append(f"{path}: experiment {eid!r} variant has unversioned feature ref {ref!r}")
                        elif ref not in known_features:
                            errors.append(f"{path}: experiment {eid!r} variant references unknown feature {ref}")


def validate_dag(features: dict[str, dict[str, Any]], origins: dict[str, Path], errors: list[str]) -> None:
    indegree: dict[str, int] = {k: 0 for k in features}
    children: dict[str, list[str]] = defaultdict(list)
    for key, f in features.items():
        for dep in f.get("inputs", []) or []:
            if dep not in features:
                errors.append(f"{origins[key]}: feature {key} references unknown dependency {dep}")
                continue
            indegree[key] += 1
            children[dep].append(key)
    q = deque(k for k, d in indegree.items() if d == 0)
    visited = 0
    while q:
        cur = q.popleft()
        visited += 1
        for nxt in children[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                q.append(nxt)
    if visited != len(features):
        cyclic = sorted(k for k, d in indegree.items() if d > 0)
        errors.append("Feature DAG contains a cycle involving: " + ", ".join(cyclic))


def validate_project(project: Path) -> list[str]:
    errors: list[str] = []
    root = project / "feature-platform"
    if not root.exists():
        return [f"{root} does not exist; initialize ML Feature Governance first"]

    try:
        sources = validate_sources(root, errors)
        entities = validate_entities(root, errors)
    except ValidationError as exc:
        return [str(exc)]

    features: dict[str, dict[str, Any]] = {}
    origins: dict[str, Path] = {}
    try:
        feature_docs = list(docs_under(root, "features"))
    except ValidationError as exc:
        return [str(exc)]
    for path, doc in feature_docs:
        if doc is None:
            continue
        entries = doc.get("features") if isinstance(doc, dict) else None
        if entries is None and isinstance(doc, dict) and "id" in doc:
            entries = [doc]
        if not isinstance(entries, list):
            errors.append(f"{path}: expected 'features' list")
            continue
        for f in entries:
            key = validate_feature(path, f, errors)
            if key:
                if key in features:
                    errors.append(f"{path}: duplicate feature definition {key}; first defined in {origins[key]}")
                elif isinstance(f, dict):
                    features[key] = f
                    origins[key] = path

    if not features:
        errors.append(f"{root / 'features'} contains no feature definitions")
    else:
        validate_dag(features, origins, errors)
        validate_feature_bindings(features, origins, sources, entities, errors)

    try:
        sets = validate_feature_sets(root, features, errors)
        validate_experiments(root, sets, set(features), errors)
        validate_capabilities(root, features, errors)
        validate_changes(root, errors)
    except ValidationError as exc:
        errors.append(str(exc))

    return errors


def doctor(project: Path) -> int:
    print(f"project: {project}")
    required = {
        "feature-platform": project / "feature-platform",
        "governance config": project / ".feature-platform" / "config.toml",
        "governance lock": project / ".feature-platform" / "governance.lock.json",
        "project validator": project / ".feature-platform" / "tools" / "featurectl.py",
        "architecture policy": project / "feature-platform" / "docs" / "architecture-principles.md",
        "backend capabilities": project / "feature-platform" / "capabilities" / "backends.yaml",
        "change contracts": project / "feature-platform" / "changes",
    }
    missing = []
    for label, path in required.items():
        present = path.exists()
        print(f"{label}: {'present' if present else 'missing'}")
        if not present:
            missing.append(label)
    try:
        import yaml  # noqa: F401
        print("PyYAML: available")
    except Exception:
        print("PyYAML: missing (YAML validation unavailable; JSON still works)")
        missing.append("PyYAML")
    print("workflow dependencies: none")
    return 2 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="featurectl")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "doctor"):
        p = sub.add_parser(name)
        p.add_argument("--project-dir", default=".")
    args = parser.parse_args()
    project = Path(args.project_dir).resolve()
    if args.command == "doctor":
        return doctor(project)
    errors = validate_project(project)
    if errors:
        print(f"ML feature governance: {len(errors)} violation(s)")
        for i, e in enumerate(errors, 1):
            print(f"{i}. {e}")
        return 2
    print("ML feature governance: validation passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
