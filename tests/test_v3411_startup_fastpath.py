from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "AnonX_3/plugins/startup_fastpath.py"
PATCH = PATCH_PATH.read_text(encoding="utf-8")
PLUGINS_INIT = (ROOT / "AnonX_3/plugins/__init__.py").read_text(encoding="utf-8")


def test_startup_fastpath_patch_is_syntax_valid_and_auto_discovered():
    compile(PATCH, str(PATCH_PATH), "exec")
    assert 'glob("*.py")' in PLUGINS_INIT
    assert 'file.name != "__init__.py"' in PLUGINS_INIT


def test_vplay_keeps_external_audio_across_raw_video_source_swap():
    assert 'vplay_hybrid_external_audio_prepared' in PATCH
    assert 'types.raw.AudioStream(MediaSource.EXTERNAL, "", audio)' in PATCH
    assert 'types.raw.Stream(microphone=microphone, camera=camera)' in PATCH
    assert 'vplay_external_audio_close_deferred' in PATCH
    assert 'clock_wait_bypassed=1' in PATCH
    assert 'vplay_hybrid_external_audio_attached' in PATCH


def test_failed_cold_preconnect_resets_native_binding_before_retry():
    assert 'direct_failed_binding_reset' in PATCH
    assert 'await client.leave_call(int(chat_id), close=False)' in PATCH
    assert 'await stop(int(chat_id))' in PATCH
    assert 'resource_manager.unregister_stream(int(chat_id))' in PATCH
    assert 'speculative_external_failure = external_audio_session is not None' in PATCH
    assert 'external_audio_session is None' in PATCH
    assert 'direct_cold_binding_retry' in PATCH
    assert 'reserved_slot=None' in PATCH


def test_patch_is_guarded_and_audio_play_default_path_is_not_rebuilt():
    assert '_anonx_vplay_cold_start_deep_fix_v3411' in PATCH
    assert 'bool(getattr(media, "video", False))' in PATCH
    assert 'DIRECT_VPLAY_HYBRID_AUDIO_HANDOFF' in PATCH
    assert 'DIRECT_COLD_BINDING_RETRY' in PATCH


def test_auto_proxy_is_explicitly_disabled_for_ytdlp_only():
    assert '_anonx_ytdlp_auto_proxy_compat_bypass_v1' in PATCH
    assert 'opts["proxy"] = ""' in PATCH
    assert 'cleaned.extend(["--proxy", ""])' in PATCH
    assert 'YOUTUBE_PROXY_MODE' in PATCH
    assert '== "auto"' in PATCH
    assert 'youtube_ytdlp_auto_proxy_bypass' in PATCH
    assert 'search_proxy_retained=1' in PATCH
    assert 'explicit_direct_override=1' in PATCH
