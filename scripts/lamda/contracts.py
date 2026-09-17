"""Portable, deterministic feature DAG execution shared by audit and inference.

Definitions are frozen into the model artifact. No statistics or operations are
inferred from observed data, and no fitted operation is silently approximated.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import yaml
from scipy import sparse

OPS = {"identity", "positive_presence", "fill_null", "clip", "log1p_nonnegative", "cast"}
TYPES = {name: getattr(pa, name)() for name in
         ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "float32", "float64")}


# Canonical JSON ordering makes equivalent contract values share the same identity.
def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


# Index versioned feature definitions and retain YAML line locations for actionable audit
# errors.
def read_definitions(root: Path):
    definitions = {}
    for path in sorted((root / "feature-platform/features").glob("*.yaml")):
        # compose retains source marks for compiler-style diagnostics.
        document = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
        node = next(v for k, v in document.value if k.value == "features")
        values = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)["features"]
        for feature, mark in zip(values, node.value):
            ref = f"{feature['id']}@{feature['version']}"
            if ref in definitions:
                raise ValueError(f"Duplicate feature definition: {ref}")
            definitions[ref] = {**feature, "location": f"{path.relative_to(root).as_posix()}:{mark.start_mark.line + 1}"}
    return definitions


# Distinguish invalid feature plans from bad values encountered while executing a valid plan.
class ContractError(ValueError):
    pass


# Compile canonical feature dependencies into a reusable, deterministic execution order.
class Plan:
    # Validate supported operations and model representations before evaluating any source
    # batches.
    def __init__(self, contract):
        self.contract = contract
        self.outputs = contract["features"]
        definitions = contract.get("definitions")
        if definitions is None:  # Legacy v1/v2 artifacts and API callers.
            definitions = {ref: {"id": ref.rsplit("@", 1)[0], "version": 1, "column": column,
                               "semantic_type": "binary", "output": {"type": "int64", "nullable": True},
                               "missing": {"strategy": "error"}, "validation": {"min": 0, "max": 1},
                               "model_representation": {"kind": "scalar", "dtype": "float32"}}
                           for ref, column in zip(self.outputs, contract["columns"])}
        self.definitions, self.order, active, done = definitions, [], set(), set()

        # A depth-first traversal detects cycles and missing dependencies while ordering inputs
        # before outputs.
        def visit(ref):
            if ref in active:
                raise ContractError(f"Feature DAG cycle at {ref}")
            if ref in done:
                return
            if ref not in definitions:
                raise ContractError(f"Missing versioned dependency {ref}")
            node = definitions[ref]
            if node.get("output", {}).get("type") not in TYPES:
                raise ContractError(f"{ref}: unsupported logical type; scalar numeric features required")
            if node.get("missing", {}).get("strategy", "error") not in ("error", "propagate", "constant"):
                raise ContractError(f"{ref}: unsupported missing-value strategy")
            active.add(ref)
            inputs = node.get("inputs", [])
            if inputs:
                transform = node.get("transform", {})
                if len(inputs) != 1 or transform.get("kind") != "builtin" or transform.get("op") not in OPS:
                    raise ContractError(f"{ref}: unsupported transform; register an executable deterministic adapter")
                params = transform.get("params", {})
                op = transform["op"]
                required = {"clip": {"min", "max"}, "fill_null": {"value"}}
                if not required.get(op, set()) <= params.keys():
                    raise ContractError(f"{ref}: missing {op} parameters")
                if op == "clip" and params["min"] > params["max"]:
                    raise ContractError(f"{ref}: reversed clip bounds")
                for dep in inputs:
                    visit(dep)
            elif not node.get("column") or node.get("transform"):
                raise ContractError(f"{ref}: source column or transform dependency missing")
            active.remove(ref)
            done.add(ref)
            self.order.append(ref)

        if not self.outputs:
            raise ContractError("Model input cannot be empty")
        if len(set(self.outputs)) != len(self.outputs):
            raise ContractError("Duplicate model input features")
        if contract.get("shape") != [len(self.outputs)] or contract.get("dtype") != "float32":
            raise ContractError("Model shape/dtype must match ordered scalar float32 features")
        for ref in self.outputs:
            visit(ref)
            representation = definitions[ref].get("model_representation", {})
            if representation.get("kind") != "scalar" or representation.get("dtype") != "float32":
                raise ContractError(f"{ref}: model requires scalar float32 representation")
        self.raw = [ref for ref in self.order if not definitions[ref].get("inputs")]
        forbidden = {"label", "hash", "vt_count", "family", "year_month", "split_name", "config_name", "dataset_id", "source_file", "row_number"}
        for ref in self.raw:
            node = definitions[ref]
            if node["column"] in forbidden or node["column"].startswith("_dlt_"):
                raise ContractError(f"{ref}: label, identity and partition metadata cannot be model features")
            if "source" in contract and node.get("source") != contract["source"]:
                raise ContractError(f"{ref}: raw feature belongs to a different source")
        self.columns = list(dict.fromkeys(definitions[ref]["column"] for ref in self.raw))
        if contract["columns"] != self.columns:
            raise ContractError("Source column order differs from resolved feature DAG")
        self.digest = fingerprint({"contract": {k: v for k, v in contract.items() if k != "definitions"},
                                   "definitions": {ref: definitions[ref] for ref in self.order}})

    # Attach upstream feature chains to each model input for audit and artifact explanations.
    def lineage(self):
        # Recursively expand input references from each output back to its raw feature sources.
        def chain(ref):
            node = self.definitions[ref]
            return [*sum((chain(dep) for dep in node.get("inputs", [])), []), ref]
        return [{"index": i, "feature": ref, "dtype": "float32", "trace": chain(ref),
                 "nodes": [{"feature": dep, **{k: v for k, v in self.definitions[dep].items()
                             if k in {"column", "source", "location", "semantic_type", "output", "missing", "transform"}}}
                           for dep in chain(ref)]}
                for i, ref in enumerate(self.outputs)]

    def execute(self, batch, issues, *, offset=0, model=None):
        """Collect all independent node errors. Invalid nodes cannot produce tensors."""
        if len(batch.schema.names) != len(set(batch.schema.names)):
            issues.add("DUPLICATE_COLUMN", "schema", "input", {}, "Duplicate physical column names are ambiguous")
            return None
        values = {}
        for ref in self.order:
            node = self.definitions[ref]
            dependencies = node.get("inputs", [])
            stage = "engineered" if dependencies else "raw_contract"
            start_errors = issues.error_count
            if dependencies:
                if dependencies[0] not in values:
                    continue
                value = values[dependencies[0]]
                transform = node["transform"]
                op, params = transform["op"], transform.get("params", {})
                try:
                    if op == "positive_presence":
                        value = pc.cast(pc.greater(value, 0), TYPES[node["output"]["type"]])
                    elif op == "fill_null":
                        value = pc.fill_null(value, params["value"])
                    elif op == "clip":
                        value = pc.if_else(pc.less(value, params["min"]), params["min"], value)
                        value = pc.if_else(pc.greater(value, params["max"]), params["max"], value)
                    elif op == "log1p_nonnegative":
                        if pc.any(pc.less(value, 0)).as_py():
                            raise ValueError("log1p_nonnegative requires nonnegative input")
                        value = pc.log1p(value)
                    # Declared output conversion is explicit on every derived node.
                    value = pc.cast(value, TYPES[node["output"]["type"]], safe=True)
                except (pa.ArrowException, ValueError, OverflowError) as exc:
                    issues.add("TRANSFORM_FAILED", stage, ref, node, str(exc), count=len(batch))
                    continue
            else:
                name = node["column"]
                if name not in batch.schema.names:
                    issues.add("MISSING_COLUMN", stage, ref, node, f"Required column {name} is missing")
                    continue
                value = batch[name]
            missing = node.get("missing", {"strategy": "error"})
            if value.null_count and missing.get("strategy") == "constant":
                try:
                    value = pc.fill_null(value, missing["value"])
                except (KeyError, pa.ArrowException) as exc:
                    issues.add("NULL_POLICY", stage, ref, node, f"Invalid constant fill: {type(exc).__name__}")
            logical = TYPES[node["output"]["type"]]
            # Int32 and Int64 are compatible physical representations if every value fits.
            valid_type = (pa.types.is_integer(logical) and pa.types.is_integer(value.type) or
                          pa.types.is_floating(logical) and pa.types.is_floating(value.type))
            if not valid_type:
                issues.add("TYPE_MISMATCH", stage, ref, node, f"Expected {logical}, observed {value.type}", count=len(batch))
                continue
            try:
                pc.cast(value, logical, safe=True)
            except pa.ArrowException:
                issues.add("TYPE_OVERFLOW", stage, ref, node, f"Values do not fit {logical}", count=len(batch))
            if value.null_count and (not node["output"].get("nullable", False) or
                                     missing.get("strategy", "error") == "error" or ref in self.outputs):
                issues.mask("UNHANDLED_NULL", stage, ref, node, "Nulls reach a required feature; define an explicit fill node",
                            pc.is_null(value), batch, offset)
            valid = pc.fill_null(pc.is_finite(value), True)
            if not pc.all(valid).as_py():
                issues.mask("NON_FINITE", stage, ref, node, "NaN or infinity is not a model value",
                            pc.invert(valid), batch, offset)
            validation = node.get("validation", {})
            if node.get("semantic_type") == "binary":
                invalid = pc.invert(pc.is_in(value, value_set=pa.array([0, 1], type=value.type)))
                issues.mask("BINARY_DOMAIN", stage, ref, node, "Binary feature requires 0 or 1; register an explicit transform for counts",
                            pc.and_(pc.is_valid(value), invalid), batch, offset, value)
            for key, comparison in (("min", pc.less), ("max", pc.greater)):
                # Avoid duplicate diagnostics for the ordinary binary domain.
                if key in validation and not (node.get("semantic_type") == "binary" and validation[key] == (0 if key == "min" else 1)):
                    issues.mask("RANGE_" + key.upper(), stage, ref, node,
                                f"Value contradicts declared {key}={validation[key]}",
                                comparison(value, validation[key]), batch, offset, value)
            allowed = validation.get("allowed_values", validation.get("enum"))
            if allowed is not None:
                issues.mask("CATEGORY_DOMAIN", stage, ref, node, "Value is outside declared categories",
                            pc.and_(pc.is_valid(value), pc.invert(pc.is_in(value, value_set=pa.array(allowed, type=value.type)))),
                            batch, offset, value)
            if issues.error_count == start_errors:
                values[ref] = value
        # Do not construct a tensor if a required feature failed validation or could not be
        # computed.
        if any(ref not in values for ref in self.outputs):
            return None
        # Build sparse columns in canonical output order, checking float32 precision before
        # storing nonzeros.
        indices, offsets, data = [], [0], []
        tensor_errors = issues.error_count
        for ref in self.outputs:
            original = values[ref].to_numpy(zero_copy_only=False)
            with np.errstate(over="ignore", invalid="ignore"):
                column = original.astype(np.float32)
            if not np.isfinite(column).all():
                issues.add("TENSOR_NON_FINITE", "model_input", ref, self.definitions[ref], "float32 conversion overflows", count=len(batch))
                continue
            if np.issubdtype(original.dtype, np.integer) and self.definitions[ref].get("semantic_type") != "binary":
                try:
                    round_trip = pc.cast(pa.array(column), values[ref].type, safe=True).to_numpy()
                    exact = np.array_equal(original, round_trip)
                except pa.ArrowException:
                    exact = False
                if not exact:
                    issues.add("TENSOR_PRECISION", "model_input", ref, self.definitions[ref],
                               "Integer values lose precision in float32; define an explicit engineering transform", count=len(batch))
                    continue
            if model == "autoencoder" and ((column < 0).any() or (column > 1).any()):
                issues.add("MODEL_DOMAIN", "model_input", ref, self.definitions[ref],
                           "BCE autoencoder requires inputs in [0,1]", count=len(batch))
                continue
            rows = np.flatnonzero(column)
            indices.append(rows)
            data.append(column[rows])
            offsets.append(offsets[-1] + len(rows))
        if issues.error_count != tensor_errors:
            return None
        matrix = sparse.csc_matrix((np.concatenate(data), np.concatenate(indices), np.asarray(offsets)),
                                   shape=(len(batch), len(self.outputs))).tocsr()
        validate_tensor(matrix, self.contract, issues, len(batch))
        return matrix


# Check the final sparse tensor boundary independently of per-feature validation.
def validate_tensor(matrix, contract, issues, rows):
    if not sparse.isspmatrix_csr(matrix):
        issues.add("TENSOR_FORMAT", "model_input", "tensor", {}, "Expected CSR matrix")
    if matrix.shape != (rows, contract["shape"][0]):
        issues.add("TENSOR_SHAPE", "model_input", "tensor", {}, f"Expected ({rows}, {contract['shape'][0]}), got {matrix.shape}")
    if matrix.dtype != np.dtype(contract["dtype"]):
        issues.add("TENSOR_DTYPE", "model_input", "tensor", {}, f"Expected {contract['dtype']}, got {matrix.dtype}")
    if not np.isfinite(matrix.data).all():
        issues.add("TENSOR_NON_FINITE", "model_input", "tensor", {}, "Matrix contains NaN/infinity")
