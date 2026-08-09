# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Process-local structured metrics for playback / cache / extraction."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, list[float]] = defaultdict(list)
        self._timing_cap = 200
        self.started_at = time.time()

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = int(self._counters.get(name, 0)) + int(value)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            bucket = self._timings[name]
            bucket.append(float(seconds))
            if len(bucket) > self._timing_cap:
                del bucket[: len(bucket) - self._timing_cap]

    def _percentile(self, values: list[float], p: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
        return ordered[idx]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            timings_raw = {k: list(v) for k, v in self._timings.items()}

        timings: dict[str, Any] = {}
        for name, values in timings_raw.items():
            if not values:
                continue
            timings[name] = {
                "count": len(values),
                "avg": round(sum(values) / len(values), 4),
                "p50": round(self._percentile(values, 50) or 0, 4),
                "p95": round(self._percentile(values, 95) or 0, 4),
                "max": round(max(values), 4),
            }

        hit = counters.get("cache_hit", 0)
        miss = counters.get("cache_miss", 0)
        total_cm = hit + miss
        ffmpeg_ok = counters.get("ffmpeg_ok", 0)
        ffmpeg_fail = counters.get("ffmpeg_fail", 0)
        extract_ok = counters.get("extract_ok", 0)
        extract_fail = counters.get("extract_fail", 0)
        download_ok = counters.get("download_ok", 0)
        download_fail = counters.get("download_fail", 0)
        return {
            "uptime_sec": round(time.time() - self.started_at, 1),
            "counters": counters,
            "gauges": gauges,
            "timings": timings,
            "derived": {
                "cache_hit_rate": round(hit / total_cm, 4) if total_cm else None,
                "direct_success_rate": _rate(
                    counters.get("direct_play_ok", 0),
                    counters.get("direct_play_ok", 0) + counters.get("direct_play_fail", 0),
                ),
                "local_failover_rate": _rate(
                    counters.get("local_failover", 0),
                    counters.get("direct_play_fail", 0) or 1,
                ),
                "ffmpeg_success_rate": _rate(ffmpeg_ok, ffmpeg_ok + ffmpeg_fail),
                "extract_success_rate": _rate(extract_ok, extract_ok + extract_fail),
                "download_success_rate": _rate(download_ok, download_ok + download_fail),
                "fallback_usage_rate": _rate(
                    counters.get("fallback_used", 0),
                    counters.get("play_request", 0) or 1,
                ),
            },
        }

    def prometheus_text(self) -> str:
        snap = self.snapshot()
        lines = [
            "# HELP AnonX_uptime_seconds Bot uptime",
            "# TYPE AnonX_uptime_seconds gauge",
            f"AnonX_uptime_seconds {snap['uptime_sec']}",
        ]
        for name, val in sorted(snap["counters"].items()):
            metric = f"AnonX_{name}_total"
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {val}")
        for name, val in sorted(snap["gauges"].items()):
            metric = f"AnonX_{name}"
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric} {val}")
        return "\n".join(lines) + "\n"


def _rate(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round(num / den, 4)


metrics = MetricsRegistry()


# Convenience helpers used across modules
def mark_cache_hit() -> None:
    metrics.inc("cache_hit")


def mark_cache_miss() -> None:
    metrics.inc("cache_miss")


def mark_direct_ok(elapsed: float | None = None) -> None:
    metrics.inc("direct_play_ok")
    if elapsed is not None:
        metrics.observe("startup_delay_sec", elapsed)


def mark_direct_fail() -> None:
    metrics.inc("direct_play_fail")


def mark_local_failover() -> None:
    metrics.inc("local_failover")


def mark_extract_fail() -> None:
    metrics.inc("extract_fail")


def mark_fallback_used() -> None:
    metrics.inc("fallback_used")


def mark_po_token_used() -> None:
    metrics.inc("po_token_used")


def mark_request() -> None:
    metrics.inc("play_request")


def mark_ffmpeg_fail() -> None:
    metrics.inc("ffmpeg_fail")


def mark_ffmpeg_ok() -> None:
    metrics.inc("ffmpeg_ok")


def mark_download_ok(elapsed: float | None = None) -> None:
    metrics.inc("download_ok")
    if elapsed is not None:
        metrics.observe("download_duration_sec", elapsed)


def mark_download_fail() -> None:
    metrics.inc("download_fail")


def mark_extract_ok(elapsed: float | None = None) -> None:
    metrics.inc("extract_ok")
    if elapsed is not None:
        metrics.observe("extract_duration_sec", elapsed)


def set_queue_length(n: int) -> None:
    metrics.set_gauge("queue_length", float(n))


def set_active_streams(n: int) -> None:
    metrics.set_gauge("active_streams", float(n))


def set_active_downloads(n: int) -> None:
    metrics.set_gauge("active_downloads", float(n))


def set_active_extracts(n: int) -> None:
    metrics.set_gauge("active_extracts", float(n))


def observe_disk_usage_pct(pct: float | None) -> None:
    if pct is not None:
        metrics.set_gauge("disk_usage_pct", float(pct))


def observe_cpu_pct(pct: float | None) -> None:
    if pct is not None:
        metrics.set_gauge("cpu_percent", float(pct))


def observe_ram_pct(pct: float | None) -> None:
    if pct is not None:
        metrics.set_gauge("ram_percent", float(pct))
