#!/usr/bin/env python3
"""Measure live resolver, packet-tail, and command-to-audible SLOs from logs.

The input must contain real ``playback_trace`` lines emitted by the bot. This
tool performs no synthetic timing and exits non-zero when the sample floor or
the configured p95 target is not met.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path


TRACE_MARKER = "playback_trace "
COMMAND_RE = re.compile(r"\bcommand=([^\s]+)")
PHASE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)=(-?[0-9]+(?:\.[0-9]+)?)ms\b")


@dataclass(frozen=True)
class ResolverSample:
    command: str
    resolver_to_scheduled_ms: float
    scheduled_to_packet_ms: float | None
    end_to_end_ms: float | None = None


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    """Return the deterministic nearest-rank percentile."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def parse_trace_line(line: str) -> ResolverSample | None:
    if TRACE_MARKER not in line:
        return None
    command_match = COMMAND_RE.search(line)
    command = command_match.group(1).strip().lower() if command_match else "unknown"
    phases = {name: float(value) for name, value in PHASE_RE.findall(line)}
    search_ms = phases.get("search")
    scheduled_ms = phases.get("play_task_scheduled")
    if search_ms is None or scheduled_ms is None or scheduled_ms < search_ms:
        return None
    packet_ms = phases.get("first_telegram_audio_packet")
    audible_ms = phases.get("audible")
    packet_tail = None
    if packet_ms is not None and packet_ms >= scheduled_ms:
        packet_tail = packet_ms - scheduled_ms
    return ResolverSample(
        command=command,
        resolver_to_scheduled_ms=scheduled_ms - search_ms,
        scheduled_to_packet_ms=packet_tail,
        # A packet is not necessarily audible while assistant unmute is still
        # pending. Only the trace's truthful audible milestone can satisfy the
        # end-to-end production gate.
        end_to_end_ms=audible_ms,
    )


def read_samples(paths: list[Path], command: str = "all") -> list[ResolverSample]:
    samples: list[ResolverSample] = []
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                sample = parse_trace_line(line)
                if sample is None:
                    continue
                if command != "all" and sample.command != command:
                    continue
                samples.append(sample)
    return samples


def summarize(samples: list[ResolverSample], target_ms: float) -> dict:
    values = [sample.resolver_to_scheduled_ms for sample in samples]
    if not values:
        return {
            "samples": 0,
            "target_ms": target_ms,
            "pass": False,
        }
    packet_values = [
        sample.scheduled_to_packet_ms
        for sample in samples
        if sample.scheduled_to_packet_ms is not None
    ]
    end_to_end_values = [
        sample.end_to_end_ms
        for sample in samples
        if sample.end_to_end_ms is not None
    ]
    result = {
        "samples": len(values),
        "target_ms": target_ms,
        "average_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "p95_ms": percentile_nearest_rank(values, 95),
        "worst_ms": max(values),
        "pass_count": sum(value <= target_ms for value in values),
    }
    result["pass_rate_pct"] = 100.0 * result["pass_count"] / len(values)
    result["all_samples_pass"] = result["pass_count"] == len(values)
    result["pass"] = result["p95_ms"] <= target_ms
    if packet_values:
        packet_pass_count = sum(value <= target_ms for value in packet_values)
        result["scheduled_to_packet"] = {
            "samples": len(packet_values),
            "average_ms": statistics.fmean(packet_values),
            "p95_ms": percentile_nearest_rank(packet_values, 95),
            "worst_ms": max(packet_values),
            "pass_count": packet_pass_count,
            "pass_rate_pct": 100.0 * packet_pass_count / len(packet_values),
            "all_samples_pass": packet_pass_count == len(packet_values),
            "pass": percentile_nearest_rank(packet_values, 95) <= target_ms,
        }
    if end_to_end_values:
        end_to_end_pass_count = sum(
            value <= target_ms for value in end_to_end_values
        )
        result["end_to_end"] = {
            "samples": len(end_to_end_values),
            "average_ms": statistics.fmean(end_to_end_values),
            "median_ms": statistics.median(end_to_end_values),
            "p95_ms": percentile_nearest_rank(end_to_end_values, 95),
            "worst_ms": max(end_to_end_values),
            "pass_count": end_to_end_pass_count,
            "pass_rate_pct": (
                100.0 * end_to_end_pass_count / len(end_to_end_values)
            ),
            "all_samples_pass": end_to_end_pass_count == len(end_to_end_values),
            "pass": percentile_nearest_rank(end_to_end_values, 95) <= target_ms,
        }
    return result


def evaluate_gate(
    samples: list[ResolverSample],
    *,
    target_ms: float,
    min_samples: int,
    metric: str,
    command: str = "all",
) -> dict:
    """Apply the sample floor and selected p95 release requirement."""

    report = summarize(samples, target_ms=target_ms)
    report["command"] = command
    minimum = max(1, int(min_samples))
    report["minimum_samples"] = minimum
    resolver_floor = len(samples) >= minimum
    packet_report = report.get("scheduled_to_packet") or {}
    end_to_end_report = report.get("end_to_end") or {}
    packet_floor = int(packet_report.get("samples", 0)) >= minimum
    end_to_end_floor = int(end_to_end_report.get("samples", 0)) >= minimum
    selected = {
        "resolver": bool(
            report.get("pass")
            and resolver_floor
        ),
        "packet-tail": bool(
            packet_report.get("pass")
            and packet_floor
        ),
        "end-to-end": bool(
            end_to_end_report.get("pass")
            and end_to_end_floor
        ),
    }
    if metric == "all":
        report["sample_floor_met"] = bool(
            resolver_floor and packet_floor and end_to_end_floor
        )
        report["pass"] = all(selected.values())
    elif metric in selected:
        report["sample_floor_met"] = {
            "resolver": resolver_floor,
            "packet-tail": packet_floor,
            "end-to-end": end_to_end_floor,
        }[metric]
        report["pass"] = selected[metric]
    else:
        raise ValueError(f"unsupported metric: {metric}")
    report["metric"] = metric
    report["metric_results"] = selected
    return report


def evaluate_command_gates(
    samples: list[ResolverSample],
    *,
    target_ms: float,
    min_samples: int,
    metric: str,
) -> dict:
    """Require independent `/play` and `/vplay` p95 gates.

    Aggregating the commands can hide a slow command behind a faster one. The
    production release contract therefore requires a complete sample floor and
    a passing p95 for each command separately.
    """
    commands = {}
    for command in ("play", "vplay"):
        command_samples = [sample for sample in samples if sample.command == command]
        commands[command] = evaluate_gate(
            command_samples,
            target_ms=target_ms,
            min_samples=min_samples,
            metric=metric,
            command=command,
        )
    return {
        "commands": commands,
        "target_ms": target_ms,
        "minimum_samples_per_command": max(1, int(min_samples)),
        "metric": metric,
        "sample_floor_met": all(
            report["sample_floor_met"] for report in commands.values()
        ),
        "pass": all(report["pass"] for report in commands.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gate live playback_trace resolver, packet-tail, and end-to-end "
            "latency at p95 <= target."
        )
    )
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--target-ms", type=float, default=3000.0)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument(
        "--command",
        choices=("both", "all", "play", "vplay"),
        default="both",
        help="'both' independently gates /play and /vplay (production default).",
    )
    parser.add_argument(
        "--metric",
        choices=("all", "resolver", "packet-tail", "end-to-end"),
        default="all",
        help="Metric to gate; 'all' requires every measured p95 to pass.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    read_command = "all" if args.command == "both" else args.command
    samples = read_samples(args.logs, command=read_command)
    if args.command == "both":
        report = evaluate_command_gates(
            samples,
            target_ms=args.target_ms,
            min_samples=args.min_samples,
            metric=args.metric,
        )
    else:
        report = evaluate_gate(
            samples,
            target_ms=args.target_ms,
            min_samples=args.min_samples,
            metric=args.metric,
            command=args.command,
        )
    if args.command == "both":
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            fields = [
                f"metric={args.metric}",
                f"target_ms={args.target_ms:.1f}",
                f"minimum_each={args.min_samples}",
            ]
            for command in ("play", "vplay"):
                command_report = report["commands"][command]
                selected = (
                    command_report.get("end_to_end", {})
                    if args.metric in {"all", "end-to-end"}
                    else command_report.get("scheduled_to_packet", {})
                    if args.metric == "packet-tail"
                    else command_report
                )
                fields.extend(
                    (
                        f"{command}_samples={command_report.get('samples', 0)}",
                        f"{command}_p95_ms={float(selected.get('p95_ms', -1)):.1f}",
                        f"{command}_pass={int(command_report['pass'])}",
                    )
                )
            fields.append(f"pass={int(report['pass'])}")
            print("PLAYBACK LATENCY " + " ".join(fields))
        if not report["sample_floor_met"]:
            return 2
        return 0 if report["pass"] else 1

    packet_report = report.get("scheduled_to_packet") or {}
    end_to_end_report = report.get("end_to_end") or {}

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if samples:
            fields = [
                f"metric={args.metric}",
                f"samples={len(samples)}",
                f"resolver_p95_ms={report['p95_ms']:.1f}",
                f"resolver_pass_rate={report['pass_rate_pct']:.1f}%",
            ]
            if packet_report:
                fields.append(
                    f"packet_tail_p95_ms={packet_report['p95_ms']:.1f}"
                )
                fields.append(
                    f"packet_tail_pass_rate={packet_report['pass_rate_pct']:.1f}%"
                )
            if end_to_end_report:
                fields.append(
                    f"end_to_end_p95_ms={end_to_end_report['p95_ms']:.1f}"
                )
                fields.append(
                    f"end_to_end_pass_rate={end_to_end_report['pass_rate_pct']:.1f}%"
                )
            fields.extend(
                (f"target_ms={args.target_ms:.1f}", f"pass={int(report['pass'])}")
            )
            print("PLAYBACK LATENCY " + " ".join(fields))
        else:
            print("PLAYBACK LATENCY samples=0 pass=0")

    if not report["sample_floor_met"]:
        return 2
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
