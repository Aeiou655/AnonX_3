#!/usr/bin/env python3
"""Executable regression tests for the bounded <=1.5s resolver lane."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


PLAYER = _load(
    ROOT / "AnonX_3/core/resolver/player_response.py",
    "anonx_player_response_test",
)
REPORT = _load(
    ROOT / "ops/resolver_latency_report.py",
    "anonx_resolver_latency_report_test",
)


def test_safe_signature_envelope_is_recovered_without_js_solver() -> None:
    wrapped_url = "https://rr.example.googlevideo.com/videoplayback?id=abc&itag=18"
    cipher = urlencode({"url": wrapped_url, "sp": "sig", "sig": "signed-value"})
    normalized, recovered = PLAYER.normalize_unciphered_player_format(
        {"itag": 18, "signatureCipher": cipher}
    )
    assert recovered is True
    assert normalized is not None
    query = parse_qs(urlparse(normalized["url"]).query)
    assert query["sig"] == ["signed-value"]
    assert query["itag"] == ["18"]


def test_encrypted_signature_and_unsafe_urls_fail_closed() -> None:
    encrypted = urlencode(
        {
            "url": "https://rr.example.googlevideo.com/videoplayback?itag=18",
            "s": "encrypted-player-js-challenge",
        }
    )
    assert PLAYER.normalize_unciphered_player_format(
        {"signatureCipher": encrypted}
    ) == (None, False)
    assert PLAYER.normalize_unciphered_player_format(
        {"url": "file:///etc/passwd"}
    ) == (None, False)


def test_live_trace_parser_uses_exact_search_to_schedule_window() -> None:
    line = (
        "INFO playback_trace command=play video=0 total_ms=2300 "
        "ack=100ms search=500ms play_task_scheduled=1800ms "
        "first_telegram_audio_packet=2200ms ready=2300ms"
    )
    sample = REPORT.parse_trace_line(line)
    assert sample is not None
    assert sample.command == "play"
    assert sample.resolver_to_scheduled_ms == 1300
    assert sample.scheduled_to_packet_ms == 400
    assert sample.end_to_end_ms == 2200


def test_nearest_rank_p95_and_sample_summary() -> None:
    samples = [
        REPORT.ResolverSample("play", float(value), 100.0)
        for value in range(1000, 1200, 10)
    ]
    report = REPORT.summarize(samples, target_ms=1500.0)
    assert report["samples"] == 20
    assert report["p95_ms"] == 1180.0
    assert report["pass"] is True


def test_source_wires_bounded_validated_first_race() -> None:
    youtube = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
    config = (ROOT / "config.py").read_text(encoding="utf-8")
    assert "normalize_unciphered_player_format" in youtube
    assert "DIRECT_RESOLVER_PARALLEL_MICRO" in youtube
    assert "DIRECT_MICRO_TOTAL_BUDGET_SEC" in youtube
    assert "micro_deadline" in youtube
    assert "_validate_prestarted_candidate" in youtube
    assert "validation_complete=1" in youtube
    assert "DIRECT_MICRO_TOTAL_BUDGET_SEC" in config
    assert "min(\n                1.45," in config


def main() -> int:
    tests = [
        test_safe_signature_envelope_is_recovered_without_js_solver,
        test_encrypted_signature_and_unsafe_urls_fail_closed,
        test_live_trace_parser_uses_exact_search_to_schedule_window,
        test_nearest_rank_p95_and_sample_summary,
        test_source_wires_bounded_validated_first_race,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
