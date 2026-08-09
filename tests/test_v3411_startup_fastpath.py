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


def test_speculative_preconnect_retry_stays_inside_resolver_overlap():
    assert 'DIRECT_PRECONNECT_OVERLAP_RETRY' in PATCH
    assert 'direct_preconnect_session_close_deferred' in PATCH
    assert 'direct_preconnect_overlap_retry' in PATCH
    assert 'external_session_reused=1' in PATCH
    assert 'reconnect_on_critical_path=0' in PATCH
    assert 'reserved_slot=None' in PATCH
    assert 'external_audio_session=external_audio_session' in PATCH
    assert 'direct_failed_binding_reset' in PATCH


def test_postconnect_silence_keeps_rtp_clock_hot_until_real_pcm():
    assert 'DIRECT_EXTERNAL_POSTCONNECT_RTP_KEEPALIVE' in PATCH
    assert 'direct_external_rtp_keepalive_started' in PATCH
    assert 'direct_external_rtp_keepalive_stopped' in PATCH
    assert 'StreamDevice.MICROPHONE' in PATCH
    assert 'first_frame_accepted' in PATCH
    assert 'session["send_lock"]' in PATCH
    assert 'TgCall._jit_prime_external_capture = jit_prime_external_capture' in PATCH


def test_existing_clean_raw_retry_and_cleanup_remain():
    assert 'DIRECT_COLD_BINDING_RETRY' in PATCH
    assert 'speculative_external_failure = external_audio_session is not None' in PATCH
    assert 'external_audio_session is None' in PATCH
    assert 'direct_cold_binding_retry' in PATCH
    assert 'await client.leave_call(int(chat_id), close=False)' in PATCH
    assert 'resource_manager.unregister_stream(int(chat_id))' in PATCH
