"""Compatibility facade for the modular LAMDA pipeline.

New adapters live under scripts.lamda.models; existing data/evaluation imports remain.
"""
from scripts.lamda.data import (  # noqa: F401
    ROOT, METADATA, SPLITS, IcebergInput, SplitPolicy, binary_matrix, cached,
    digest, load_contract, month, stage, write_json,
)
from scripts.lamda.evaluation import choose_threshold, metrics  # noqa: F401
from scripts.lamda.pipeline import environment_manifest, predict, score, train  # noqa: F401
