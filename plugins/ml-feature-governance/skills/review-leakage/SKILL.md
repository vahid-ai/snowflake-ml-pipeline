---
name: review-leakage
description: Independently review ML feature and training pipelines for temporal leakage, split contamination, fitted-state leakage, target leakage, late data, and train-serve skew.
---

# Leakage and Reproducibility Review

Review without modifying files. Trace event time versus availability time, point-in-time joins, current-event inclusion,
label-derived features, split boundaries, stateful-transform fitting scope, resampling scope, late data and backfills,
materialization time, caches, and dataset snapshot pins.

Treat fitting state on validation or test data as blocking unless the experiment explicitly studies that behavior and
cannot be mistaken for unbiased evaluation. Return findings ordered by severity with affected paths and durable fixes.
