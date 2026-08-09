from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
CALLS = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")


def test_foreground_audio_uses_fast_progressive_profile_not_exact_140():
    resolver = YOUTUBE[YOUTUBE.index("async def _resolve_direct_stream_source_uncached("):]
    assert 'fast_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"' in resolver
    assert 'fast_progressive=True' in resolver
    assert 'profiles.append(("foreground_fast", fast_opts, False))' in resolver
    assert 'mode=dual_stage_foreground_fast' in resolver


def test_exact_140_is_background_only_and_requires_visible_pot():
    assert 'def warm_audio140_source(' in YOUTUBE
    assert 'exact_audio140=True' in YOUTUBE
    assert 'profiles.append(("background_140", exact_light, True))' in YOUTUBE
    assert 'background_140_missing_visible_pot' in YOUTUBE
    assert 'source.format_id == "140" and source.pot_bound' in YOUTUBE


def test_background_140_has_separate_executor_pool():
    assert 'self._direct_background140_executors = [' in YOUTUBE
    assert 'thread_name_prefix=f"yt-140-bg-{idx}"' in YOUTUBE
    assert 'background140=bool(require_140)' in YOUTUBE
    assert 'DIRECT_BACKGROUND_140_WORKERS' in CONFIG


def test_exact_140_promotion_cannot_delay_initial_vc_join():
    gate = CALLS.index('gate = await startup_gate.confirm_direct_start(')
    promote = CALLS.index('yt.warm_audio140_source(', gate)
    assert gate < promote
    assert 'mode="new-direct-late-join"' in CALLS


def test_queued_next_gets_background_140_warm():
    start = CALLS.index('async def _prefetch_next(')
    end = CALLS.index('async def pause(', start)
    block = CALLS[start:end]
    assert 'queue.get_next(chat_id, check=True)' in block
    assert 'yt.warm_audio140_source(next_id)' in block


def test_promoted_140_cache_is_checked_before_foreground_prewarm_join():
    start = YOUTUBE.index('async def resolve_direct_stream_source(')
    end = YOUTUBE.index('async def _validated_direct_source(', start)
    block = YOUTUBE[start:end]
    promoted = block.index('self._direct_audio140_cache.get(video_id)')
    warm = block.index('warm_result = self._direct_warm_results.get(key)')
    assert promoted < warm
    assert 'promoted_140_cache_hit' in block


def test_cookie_worker_identity_is_semantic_not_mtime():
    start = YOUTUBE.index('def _direct_cookie_semantic_fingerprint(')
    end = YOUTUBE.index('def _get_persistent_direct_ydl(', start)
    block = YOUTUBE[start:end]
    assert '_expiry' in block
    assert 'rows.append((domain, path, secure, name, value))' in block
    assert 'st_mtime_ns' not in block


def test_dual_stage_config_defaults_enabled():
    assert '"DIRECT_BACKGROUND_140_ENABLED", True' in CONFIG
    assert '"DIRECT_BACKGROUND_140_TTL_SEC", "600"' in CONFIG
    assert '"DIRECT_BACKGROUND_140_WORKERS", "1"' in CONFIG
