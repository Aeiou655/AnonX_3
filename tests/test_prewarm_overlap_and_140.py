from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = (ROOT / 'AnonX_3/helpers/_play.py').read_text(encoding='utf-8')
YOUTUBE = (ROOT / 'AnonX_3/core/youtube.py').read_text(encoding='utf-8')


def test_warm_search_starts_direct_before_returning_result():
    search_pos = HELPER.index('result = await yt.search(warm_query, m.id, video=video)')
    prewarm_pos = HELPER.index('yt.warm_direct_stream_source(', search_pos)
    return_pos = HELPER.index('return result', prewarm_pos)
    assert search_pos < prewarm_pos < return_pos
    assert 'direct resolver prewarmed' in HELPER


def test_late_join_and_singleflight_are_still_present():
    calls = (ROOT / 'AnonX_3/core/calls.py').read_text(encoding='utf-8')
    assert 'mode="new-direct-late-join"' in calls
    assert 'vc_join_start' in calls
    assert 'self._direct_stream_inflight.get(key)' in YOUTUBE


def test_provider_winner_starts_prewarm_before_search_cleanup():
    start = YOUTUBE.index('async def _search_uncached(')
    end = YOUTUBE.index('async def search(', start)
    block = YOUTUBE[start:end]
    winner = block.index('best = self._clone_search_track(tracks[0], m_id, video)')
    prewarm = block.index('self.warm_direct_stream_source(', winner)
    cancel = block.index('# Found a result — cancel remaining providers.', prewarm)
    assert winner < prewarm < cancel


def test_foreground_joins_fully_resolved_prewarm_task():
    start = YOUTUBE.index('async def resolve_direct_stream_source(')
    end = YOUTUBE.index('async def _validated_direct_source(', start)
    block = YOUTUBE[start:end]
    assert 'warm_task = self._direct_warm_tasks.get(key)' in block
    assert 'warmed = await asyncio.shield(warm_task)' in block
    assert 'isinstance(warmed, DirectStreamSource) and warmed.url' in block
    assert 'prewarm_join_miss' in block
    assert 'fully_resolved=1' in block
    assert 'warm_result = self._direct_warm_results.get(key)' in block


def test_fast_foreground_skips_configs_while_background_140_keeps_them():
    start = YOUTUBE.index('def _authoritative_pot_opts(')
    end = YOUTUBE.index('def _direct_cookie_semantic_fingerprint(', start)
    block = YOUTUBE[start:end]
    assert 'if fast_progressive:' in block
    assert 'player_skip.append("configs")' in block
    assert 'player_skip.remove("configs")' in block
    resolver = YOUTUBE[YOUTUBE.index('async def _resolve_direct_stream_source_uncached('):]
    assert 'fast_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"' in resolver
    assert 'exact_base["format"] = "140"' in resolver


def test_startup_warm_runtime_prepares_fast_and_background_profiles():
    main = (ROOT / 'AnonX_3/__main__.py').read_text(encoding='utf-8')
    config = (ROOT / 'config.py').read_text(encoding='utf-8')
    assert 'async def warm_direct_resolver_runtime' in YOUTUBE
    assert 'audio-fast' in YOUTUBE
    assert 'audio-140-background' in YOUTUBE
    assert 'DIRECT_RESOLVER_STARTUP_WARM' in config
    assert 'yt.warm_direct_resolver_runtime()' in main
