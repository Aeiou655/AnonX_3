from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
CALLS = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
PLAY = (ROOT / "AnonX_3/helpers/_play.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")
ENV = (ROOT / ".env").read_text(encoding="utf-8")
SAMPLE = (ROOT / "sample.env").read_text(encoding="utf-8")


def _block(text: str, start: str, end: str) -> str:
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def test_vplay_cross_tier_prewarm_is_singleflight():
    resolve = _block(
        YOUTUBE,
        "async def resolve_direct_stream_source",
        "async def _validated_direct_source",
    )
    warm = _block(
        YOUTUBE,
        "def warm_direct_stream_source",
        "async def _resolve_direct_stream_source_uncached",
    )
    assert "prewarm_cross_tier_join" in resolve
    assert "prewarm_cross_tier_result_reused" in resolve
    assert "inflight_cross_tier_join" in resolve
    assert "duplicate_extract_avoided=1" in resolve
    assert "prewarm_cross_tier_reused" in warm
    assert "prewarm_inflight_cross_tier_reused" in warm
    # Warm task cleanup must not delete a newer replacement for the same key.
    assert "if self._direct_warm_tasks.get(_key) is done:" in warm


def test_v4_micro_lane_gets_a_bounded_head_start_before_one_full_hedge():
    init = _block(YOUTUBE, "def __init__", "@staticmethod\n    def cookie_free_mode")
    resolver = _block(
        YOUTUBE,
        "async def _resolve_direct_stream_source_uncached",
        "def _metadata_from_direct_info",
    )
    assert "_direct_foreground_resolver_semaphore" in init
    assert "DIRECT_FOREGROUND_RESOLVER_SLOTS" in CONFIG
    assert "DIRECT_FOREGROUND_RESOLVER_SLOTS=2" in SAMPLE
    prestarted = _block(
        resolver,
        "async def _run_prestarted_extract",
        "for idx, (profile, extract_opts, require_140) in enumerate(profiles[:2])",
    )
    assert "async with self._direct_foreground_resolver_semaphore" in prestarted
    assert "resource_manager.extract_semaphore()" not in prestarted
    assert "resolver_slot_hint=slot_hint" in prestarted
    assert "foreground_slots=%s" in resolver
    assert "dynamic_ytdlp=%s" in resolver
    assert "if not bool(getattr(config, \"DIRECT_STARTUP_V4\", True))" in resolver
    assert "DIRECT_V4_AUTHORITATIVE_HEDGE_DELAY_SEC" in resolver
    assert "_run_delayed_authoritative_hedge" in resolver
    assert "authoritative_lanes=1 fallback_serial_wait=0" in resolver


def test_audio_and_video_fast_lanes_still_use_distinct_sticky_workers():
    resolver = _block(
        YOUTUBE,
        "async def _resolve_direct_stream_source_uncached",
        "def _metadata_from_direct_info",
    )
    assert 'profiles.append(("foreground_fast", fast_opts, False))' in resolver
    assert 'profiles.append(("audio_escape_fast", escape_opts, False))' in resolver
    assert 'profiles.append(("video_escape_fast", escape, False))' in resolver
    assert "0 if idx == 0 else 1" in resolver


def test_command_layer_starts_vc_native_warm_before_play_media():
    ready = _block(PLAY, "if not reason:", "logger.warning(\n                    \"Assistant invite attempt failed")
    assert "anon._schedule_vc_metadata_warm" in ready
    assert "vc_command_overlap_warm_ready" in ready
    warm = _block(CALLS, "def _schedule_vc_metadata_warm", "def _pop_vc_native_payload")
    assert "vc_join_metadata_warm_cache_hit" in warm
    assert "call_ref=1 native_payload=1 joined=0" in warm


def test_vplay_existing_call_swap_stays_reconnect_free():
    assert "vplay_source_swap_before" in CALLS
    assert "direct_video_background_source_swap" in CALLS
    assert "audio_wait_ms=0" in CALLS
    assert "reconnect=0" in CALLS
