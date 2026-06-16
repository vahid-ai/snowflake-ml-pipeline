from __future__ import annotations

import json
import platform
import threading
import time
import tracemalloc
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil
from pyinstrument import Profiler
from pyinstrument.renderers.html import HTMLRenderer


@dataclass
class StageMetric:
    name: str
    wall_seconds: float
    process_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkMetric:
    scenario: str
    run_id: str
    status: str
    total_wall_seconds: float
    total_process_seconds: float
    peak_rss_bytes: int
    start_rss_bytes: int
    end_rss_bytes: int
    peak_tracemalloc_bytes: int
    stages: list[StageMetric]
    counters: dict[str, Any]
    artifacts: dict[str, str]
    environment: dict[str, Any]
    error: str | None = None


class MemorySampler:
    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self.process = psutil.Process()
        self.start_rss = self.process.memory_info().rss
        self.peak_rss = self.start_rss
        self.end_rss = self.start_rss
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def __enter__(self) -> MemorySampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.end_rss = self.process.memory_info().rss
        self.peak_rss = max(self.peak_rss, self.end_rss)

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
            except psutil.Error:
                return
            self._stop.wait(self.interval_seconds)


class BenchmarkContext:
    def __init__(self, scenario: str, artifact_dir: Path, run_id: str) -> None:
        self.scenario = scenario
        self.artifact_dir = artifact_dir
        self.run_id = run_id
        self.stages: list[StageMetric] = []
        self.counters: dict[str, Any] = {}
        self.artifacts: dict[str, str] = {}

    @contextmanager
    def stage(self, name: str, **metadata: Any):
        wall_start = time.perf_counter()
        process_start = time.process_time()
        try:
            yield
        finally:
            self.stages.append(
                StageMetric(
                    name=name,
                    wall_seconds=time.perf_counter() - wall_start,
                    process_seconds=time.process_time() - process_start,
                    metadata=metadata,
                )
            )

    def set_counter(self, name: str, value: Any) -> None:
        self.counters[name] = value

    def add_artifact(self, name: str, path: Path) -> None:
        self.artifacts[name] = str(path)


ScenarioFn = Callable[[BenchmarkContext, dict[str, Any]], None]


def run_scenario(
    name: str,
    scenario_fn: ScenarioFn,
    artifact_root: Path,
    options: dict[str, Any],
    profile: bool = True,
    sample_interval_seconds: float = 0.05,
) -> BenchmarkMetric:
    run_id = time.strftime("%Y%m%dT%H%M%S")
    artifact_dir = artifact_root / name / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    context = BenchmarkContext(name, artifact_dir, run_id)

    profiler = Profiler(interval=0.001, async_mode="enabled") if profile else None
    tracemalloc.start()
    wall_start = time.perf_counter()
    process_start = time.process_time()
    status = "passed"
    error = None

    try:
        with MemorySampler(interval_seconds=sample_interval_seconds) as sampler:
            if profiler is not None:
                profiler.start()
            try:
                scenario_fn(context, options)
            except Exception as exc:
                status = "failed"
                error = repr(exc)
                raise
            finally:
                if profiler is not None:
                    profiler.stop()

        peak_rss = sampler.peak_rss
        start_rss = sampler.start_rss
        end_rss = sampler.end_rss
    finally:
        current_alloc, peak_alloc = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del current_alloc

    total_wall = time.perf_counter() - wall_start
    total_process = time.process_time() - process_start

    if profiler is not None:
        profile_path = artifact_dir / "flamegraph.html"
        profile_path.write_text(HTMLRenderer().render(profiler.last_session), encoding="utf-8")
        context.add_artifact("flamegraph_html", profile_path)

    metric = BenchmarkMetric(
        scenario=name,
        run_id=run_id,
        status=status,
        total_wall_seconds=total_wall,
        total_process_seconds=total_process,
        peak_rss_bytes=peak_rss,
        start_rss_bytes=start_rss,
        end_rss_bytes=end_rss,
        peak_tracemalloc_bytes=peak_alloc,
        stages=context.stages,
        counters=context.counters,
        artifacts=context.artifacts,
        environment={
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "cpu_count": psutil.cpu_count(logical=True),
            "memory_total_bytes": psutil.virtual_memory().total,
        },
        error=error,
    )

    metrics_path = artifact_dir / "metrics.json"
    metrics_path.write_text(json.dumps(asdict(metric), indent=2), encoding="utf-8")
    metric.artifacts["metrics_json"] = str(metrics_path)
    metrics_path.write_text(json.dumps(asdict(metric), indent=2), encoding="utf-8")
    return metric


def write_summary(metrics: list[BenchmarkMetric], artifact_root: Path) -> Path:
    summary_path = artifact_root / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps([asdict(metric) for metric in metrics], indent=2),
        encoding="utf-8",
    )
    return summary_path
