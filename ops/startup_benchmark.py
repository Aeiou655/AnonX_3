#!/usr/bin/env python3
"""Offline, deterministic startup-latency model for the /play critical path.

WHAT THIS IS
------------
A *synthetic pipeline-overhead* simulator. It models the orchestration timeline
of ``play_media`` (ack -> search -> resolve -> vc_join -> audible -> proof ->
ready) using FIXED, documented per-phase latency assumptions and a seeded PRNG.
It computes average and p95 for the exact phase breakdown requested for three
scenarios: ``cache-hit``, ``new-direct`` and ``multi-group``, comparing the
BEFORE state (eager ffmpeg pre-open enabled) against the AFTER state
(``DIRECT_AUDIO_PROBE`` off — the redundant pre-open removed).

WHAT THIS IS NOT
----------------
It is NOT a live measurement. It never touches Telegram, a voice chat, MongoDB,
yt-dlp, ffmpeg, or the network, and it produces NO audio. The absolute numbers
are modeling assumptions (see ``PHASES`` below and ``--help``); only the
*delta* between BEFORE and AFTER is a direct consequence of the code change
under test (removing ``_probe_direct_audio_open`` from the pre-audio path).

Live per-phase numbers come from the ``playback_trace`` log line emitted by
``AnonX_3.core.performance.PlaybackTrace`` on every real /play; this tool exists
so the pipeline delta can be reproduced deterministically without a server.

Determinism: identical ``--seed`` + ``--iterations`` => byte-identical output.
No wall-clock, no ``Math.random`` equivalent beyond the seeded ``random.Random``.

Usage:
  python ops/startup_benchmark.py
  python ops/startup_benchmark.py --iterations 5000 --seed 1729
  python ops/startup_benchmark.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# SYNTHETIC latency assumptions (milliseconds). These are documented modeling
# inputs, NOT measured live values. base = typical, jitter = +/- uniform spread,
# cap = hard ceiling (models a subprocess/timeout bound). Sources noted inline.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Phase:
    base: float
    jitter: float
    cap: float | None = None

    def sample(self, rng: random.Random) -> float:
        val = self.base + rng.uniform(-self.jitter, self.jitter)
        val = max(0.0, val)
        if self.cap is not None:
            val = min(val, self.cap)
        return val


PHASES = {
    # request acknowledged (edit "searching" card)
    "ack": Phase(25, 15),
    # provider search: cache lookup vs cold YouTube search (single-flight)
    "search_cache": Phase(90, 60),
    "search_new": Phase(550, 250),
    # yt-dlp direct URL resolve (new-direct only; cache-hit has none)
    "resolve_new": Phase(480, 220),
    # redundant ffmpeg pre-open of the direct URL — BEFORE only.
    # rw_timeout 5s / 6s cap in _probe_direct_audio_open.
    "ffprobe": Phase(1600, 900, cap=6000),
    # VC/voice startup. Per the user's live report this phase dominates cold
    # startup; warm = assistant already joined, stream swap only.
    "vc_join_cold": Phase(1500, 800),
    "vc_join_warm": Phase(120, 80),
    # client.play() returns once joined (packets begin -> audible)
    "play_dispatch": Phase(90, 50),
    # post-audible proof window (DIRECT_START_PROOF_SEC=3). Direct path only.
    # Does NOT delay audio; counts toward total/ready, not audible.
    "proof": Phase(3000, 0),
    # now-playing card render (after audio; non-blocking of audio)
    "np": Phase(40, 25),
}

# Phase-order per scenario. Each entry: (trace_label, phase_key, counts_to_audible)
CACHE_HIT = [
    ("ack", "ack", True),
    ("search", "search_cache", True),
    ("vc_join", "vc_join_warm", True),
    ("audible", "play_dispatch", True),
    ("np_updated", "np", False),
]


def new_direct_timeline(before: bool) -> list[tuple[str, str, bool]]:
    steps = [
        ("ack", "ack", True),
        ("search", "search_new", True),
        ("resolve", "resolve_new", True),
    ]
    if before:
        steps.append(("ffprobe", "ffprobe", True))
    steps += [
        ("vc_join", "vc_join_cold", True),
        ("audible", "play_dispatch", True),
        ("proof", "proof", False),
        ("np_updated", "np", False),
    ]
    return steps


def simulate(timeline, rng: random.Random) -> dict[str, float]:
    """Return cumulative-at-mark ms per trace label, plus audible_ms/total_ms."""
    marks: dict[str, float] = {}
    cursor = 0.0
    audible = 0.0
    for label, key, counts_audible in timeline:
        cursor += PHASES[key].sample(rng)
        marks[label] = cursor
        if counts_audible:
            audible = cursor
    marks["audible_ms"] = audible
    marks["total_ms"] = cursor
    return marks


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def aggregate(timeline, iterations: int, seed: int) -> dict[str, dict[str, float]]:
    rng = random.Random(seed)
    series: dict[str, list[float]] = {}
    for _ in range(iterations):
        marks = simulate(timeline, rng)
        for label, value in marks.items():
            series.setdefault(label, []).append(value)
    out: dict[str, dict[str, float]] = {}
    for label, values in series.items():
        out[label] = {
            "avg_ms": sum(values) / len(values),
            "p95_ms": percentile(values, 95),
        }
    return out


def aggregate_multigroup(
    groups: int, iterations: int, seed: int
) -> dict[str, dict[str, float]]:
    """Multi-group: `groups` concurrent new-direct (AFTER) sharing the startup
    Semaphore(30). groups <= 30 => no queue wait; excess would serialize, which
    we model as additive slot wait. Aggregates audible/total across all groups."""
    rng = random.Random(seed)
    timeline = new_direct_timeline(before=False)
    slots = 30
    audible_all: list[float] = []
    total_all: list[float] = []
    for _ in range(iterations):
        finishes: list[float] = []
        for g in range(groups):
            marks = simulate(timeline, rng)
            # Startup-slot contention only when concurrency exceeds the semaphore.
            queue_wait = 0.0
            if groups > slots:
                over = g // slots
                queue_wait = over * PHASES["vc_join_cold"].base
            audible_all.append(marks["audible_ms"] + queue_wait)
            total_all.append(marks["total_ms"] + queue_wait)
            finishes.append(marks["total_ms"] + queue_wait)
    return {
        "audible_ms": {
            "avg_ms": sum(audible_all) / len(audible_all),
            "p95_ms": percentile(audible_all, 95),
        },
        "total_ms": {
            "avg_ms": sum(total_all) / len(total_all),
            "p95_ms": percentile(total_all, 95),
        },
    }


def fmt(ms: float) -> str:
    return f"{ms/1000.0:6.2f}s"


def print_block(title: str, stats: dict[str, dict[str, float]]) -> None:
    print(f"\n  {title}")
    print(f"    {'phase':<14}{'avg':>9}{'p95':>9}")
    order = [
        "ack", "search", "resolve", "ffprobe", "vc_join",
        "audible", "proof", "np_updated", "audible_ms", "total_ms",
    ]
    for label in order:
        if label not in stats:
            continue
        pretty = {"audible_ms": "AUDIBLE", "total_ms": "TOTAL"}.get(label, label)
        row = stats[label]
        print(f"    {pretty:<14}{fmt(row['avg_ms']):>9}{fmt(row['p95_ms']):>9}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic /play startup-latency pipeline model (offline).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--groups", type=int, default=8,
                        help="concurrent chats for the multi-group scenario")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="also write raw results to this JSON path")
    args = parser.parse_args()

    n = max(1, args.iterations)

    results = {
        "meta": {
            "kind": "SYNTHETIC pipeline-overhead model (NOT live audio)",
            "iterations": n,
            "seed": args.seed,
            "groups": args.groups,
            "note": "absolute values are modeling assumptions; the BEFORE/AFTER "
                    "delta is the ffprobe-removal effect. Live numbers: "
                    "playback_trace log line.",
        },
        "cache_hit": aggregate(CACHE_HIT, n, args.seed),
        "new_direct_before": aggregate(new_direct_timeline(True), n, args.seed),
        "new_direct_after": aggregate(new_direct_timeline(False), n, args.seed),
        "multi_group_after": aggregate_multigroup(args.groups, n, args.seed),
    }

    print("=" * 62)
    print(" AnonX_3 startup pipeline model  —  SYNTHETIC (not live audio)")
    print(f" iterations={n} seed={args.seed} groups={args.groups}")
    print(" phases: ack / search / resolve / vc_join / audible / proof / total")
    print("=" * 62)

    print_block("cache-hit (ffprobe N/A on this path)", results["cache_hit"])
    print_block("new-direct  BEFORE (eager ffmpeg pre-open ON)",
                results["new_direct_before"])
    print_block("new-direct  AFTER  (DIRECT_AUDIO_PROBE off)",
                results["new_direct_after"])
    print_block(f"multi-group AFTER  ({args.groups} concurrent)",
                results["multi_group_after"])

    b = results["new_direct_before"]
    a = results["new_direct_after"]
    d_aud = b["audible_ms"]["avg_ms"] - a["audible_ms"]["avg_ms"]
    d_tot = b["total_ms"]["avg_ms"] - a["total_ms"]["avg_ms"]
    d_aud95 = b["audible_ms"]["p95_ms"] - a["audible_ms"]["p95_ms"]
    print("\n  DELTA new-direct (BEFORE - AFTER, positive = faster after)")
    print(f"    audible avg  -{d_aud/1000.0:5.2f}s   p95  -{d_aud95/1000.0:5.2f}s")
    print(f"    total   avg  -{d_tot/1000.0:5.2f}s")
    print(f"\n  AFTER new-direct audible avg = {fmt(a['audible_ms']['avg_ms']).strip()}"
          f"  (target 3-6s)")
    print(f"  cache-hit total avg = "
          f"{fmt(results['cache_hit']['total_ms']['avg_ms']).strip()}"
          f"  (target 1-2s)")
    print("\n  NOTE: synthetic model. Absolute values are assumptions; the")
    print("  BEFORE/AFTER delta reflects the ffprobe-removal code change only.")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, sort_keys=True)
        print(f"\n  wrote {args.json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
