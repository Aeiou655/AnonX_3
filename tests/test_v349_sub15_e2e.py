#!/usr/bin/env python3
"""Executable regressions for the v3.4.9 <=1.5s critical-path changes."""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlencode


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


DEFERRED = _load(
    ROOT / "AnonX_3/core/deferred_status.py",
    "anonx_deferred_status_test",
)
PLAYER = _load(
    ROOT / "AnonX_3/core/resolver/player_response.py",
    "anonx_player_response_v349_test",
)
REPORT = _load(
    ROOT / "ops/resolver_latency_report.py",
    "anonx_latency_report_v349_test",
)


class _Chat:
    id = -100123


class _SourceMessage:
    id = 77
    chat = _Chat()
    link = "https://t.me/c/1/77"
    reply_markup = None


class _StatusMessage:
    id = 88
    chat = _Chat()
    link = "https://t.me/c/1/88"
    reply_markup = "cancel"

    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


def test_deferred_status_does_not_block_reads_and_forwards_mutations() -> None:
    async def _case() -> None:
        proxy = DEFERRED.DeferredStatusMessage(_SourceMessage())
        assert proxy.id == 0
        assert proxy.chat.id == -100123
        assert proxy.reply_markup is not None
        edit = asyncio.create_task(proxy.edit_text("ready", disable_web_page_preview=True))
        await asyncio.sleep(0)
        assert not edit.done()
        status = _StatusMessage()
        proxy.lang = {"ok": "OK"}
        proxy.bind(status)
        assert await edit is status
        assert status.edits == [("ready", {"disable_web_page_preview": True})]
        assert status.lang == {"ok": "OK"}
        assert proxy.id == 88

    asyncio.run(_case())


def test_player_response_summary_is_actionable_and_secret_free() -> None:
    direct = "https://r1.googlevideo.com/videoplayback?itag=18&pot=secret"
    safe_cipher = urlencode(
        {"url": "https://r2.googlevideo.com/videoplayback?itag=140", "sig": "x"}
    )
    encrypted = urlencode(
        {
            "url": "https://r3.googlevideo.com/videoplayback?itag=251",
            "s": "encrypted",
        }
    )
    summary = PLAYER.summarize_player_response(
        {
            "playabilityStatus": {"status": "OK"},
            "streamingData": {
                "formats": [{"itag": 18, "url": direct}],
                "adaptiveFormats": [
                    {"itag": 140, "signatureCipher": safe_cipher},
                    {"itag": 251, "signatureCipher": encrypted},
                ],
            },
        }
    )
    assert summary == {
        "status": "ok",
        "formats": 1,
        "adaptive": 2,
        "usable": 2,
        "safe_cipher": 1,
        "encrypted_cipher": 1,
    }
    assert "secret" not in repr(summary)


def test_initial_playback_lease_releases_exactly_once() -> None:
    calls_path = ROOT / "AnonX_3/core/calls.py"
    tree = ast.parse(calls_path.read_text(encoding="utf-8"), filename=str(calls_path))
    lease_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "InitialPlaybackLease"
    )
    module = ast.Module(body=[lease_node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"asyncio": asyncio}
    exec(compile(module, str(calls_path), "exec"), namespace)
    lease_type = namespace["InitialPlaybackLease"]

    async def _case() -> None:
        lock = asyncio.Lock()
        await lock.acquire()
        lease = lease_type(-100123, lock)
        assert lock.locked()
        lease.release()
        assert not lock.locked()
        assert lease.released is True
        lease.release()
        assert not lock.locked()

    asyncio.run(_case())


def test_cold_start_gate_requires_100_per_command_and_independent_p95() -> None:
    parsed = REPORT.parse_trace_line(
        "playback_trace command=play total_ms=2380 search=250ms play_task_scheduled=1200ms "
        "first_telegram_audio_packet=1800ms audible=2100ms"
    )
    assert parsed is not None
    assert parsed.scheduled_to_packet_ms == 600.0
    assert parsed.end_to_end_ms == 2380.0
    assert parsed.audible_ms == 2100.0

    samples = [
        REPORT.ResolverSample("play", 600.0, 300.0, 1200.0),
        REPORT.ResolverSample("play", 700.0, 350.0, 1300.0),
        REPORT.ResolverSample("play", 800.0, 400.0, 1400.0),
    ]
    report = REPORT.summarize(samples, target_ms=4000.0)
    assert report["p95_ms"] == 800.0
    assert report["scheduled_to_packet"]["p95_ms"] == 400.0
    assert report["end_to_end"]["p95_ms"] == 1400.0
    assert report["pass"] is True
    assert report["pass_rate_pct"] == 100.0
    assert report["all_samples_pass"] is True
    assert report["scheduled_to_packet"]["pass"] is True
    assert report["scheduled_to_packet"]["pass_rate_pct"] == 100.0
    assert report["end_to_end"]["pass"] is True
    assert report["end_to_end"]["pass_rate_pct"] == 100.0

    play = [
        REPORT.ResolverSample("play", 900.0, 150.0, 3800.0)
        for _ in range(100)
    ]
    vplay = [
        REPORT.ResolverSample("vplay", 1000.0, 180.0, 3900.0)
        for _ in range(95)
    ] + [
        REPORT.ResolverSample("vplay", 1100.0, 190.0, 4200.0)
        for _ in range(5)
    ]
    gated = REPORT.evaluate_command_gates(
        play + vplay,
        target_ms=4000.0,
        min_samples=100,
        metric="end-to-end",
    )
    assert gated["sample_floor_met"] is True
    assert gated["commands"]["play"]["pass"] is True
    assert gated["commands"]["vplay"]["end_to_end"]["p95_ms"] == 3900.0
    assert gated["commands"]["vplay"]["pass"] is True
    assert gated["pass"] is True

    too_few = REPORT.evaluate_command_gates(
        play + vplay[:-1],
        target_ms=4000.0,
        min_samples=100,
        metric="end-to-end",
    )
    assert too_few["sample_floor_met"] is False
    assert too_few["commands"]["vplay"]["pass"] is False

    slow_vplay = [
        REPORT.ResolverSample("vplay", 1000.0, 180.0, 4100.0)
        for _ in range(100)
    ]
    independent = REPORT.evaluate_command_gates(
        play + slow_vplay,
        target_ms=4000.0,
        min_samples=100,
        metric="end-to-end",
    )
    assert independent["commands"]["play"]["pass"] is True
    assert independent["commands"]["vplay"]["pass"] is False
    assert independent["pass"] is False


def test_source_wires_parallel_ack_admission_and_authenticated_micro_context() -> None:
    play = (ROOT / "AnonX_3/helpers/_play.py").read_text(encoding="utf-8")
    youtube = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
    config = (ROOT / "config.py").read_text(encoding="utf-8")
    env = (ROOT / ".env").read_text(encoding="utf-8")
    sample_env = (ROOT / "sample.env").read_text(encoding="utf-8")
    assert "DeferredStatusMessage(m)" in play
    assert play.index("async def _warm_search_and_direct") < play.index(
        'name=f"play-status-ack:'
    )
    assert "_prefetch_admission_state" in play
    assert "voice_call_verified" in play
    assert "acquire_initial_playback_lease" in play
    assert "begin_initial_direct_preconnect" in play
    calls = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
    assert "class InitialPlaybackLease" in calls
    assert "direct_command_preconnect_adopted" in calls
    assert '"adopted": False' in calls
    assert "vplay_audio_lead_packet_ready" in calls
    assert "DIRECT_VIDEO_AUDIO_LEAD_PACKET_TIMEOUT_SEC" in config
    assert "reconnect=0" in calls
    assert "vc_required_unmute_background" in calls
    assert "unmute_blocked_audio_ms=0" in calls
    assert "direct_video_background_source_swap" in calls
    proof = (ROOT / "AnonX_3/plugins/sub3_proof_guard.py").read_text(
        encoding="utf-8"
    )
    assert "video_attach_required=0" in proof
    assert 'session["unmute_confirmed"].is_set()' in proof
    assert "player_ytcfg = get_default_ytcfg(client)" in youtube
    assert "webpage_ytcfg=player_ytcfg" in youtube
    assert "auth_client_rejects_cookies" in youtube
    assert '"tv_downgraded", "web_safari", "android_vr"' in youtube
    assert "tv_downgraded,web_safari,android_vr" in config
    final_patch = (ROOT / "AnonX_3/plugins/zzzz_sub3_final.py").read_text(
        encoding="utf-8"
    )
    assert 'setattr(config, "DIRECT_MICRO_PLAYER_CLIENTS", ("mweb",))' in final_patch
    assert "direct_resolver_v4_delayed_hedge" in youtube
    assert "authoritative_lanes=1 fallback_serial_wait=0" in youtube
    assert "# DIRECT_STARTUP_V4=True" in sample_env


def main() -> int:
    tests = [
        test_deferred_status_does_not_block_reads_and_forwards_mutations,
        test_player_response_summary_is_actionable_and_secret_free,
        test_initial_playback_lease_releases_exactly_once,
        test_cold_start_gate_requires_100_per_command_and_independent_p95,
        test_source_wires_parallel_ack_admission_and_authenticated_micro_context,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
