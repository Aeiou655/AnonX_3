from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = (ROOT / 'AnonX_3/core/calls.py').read_text(encoding='utf-8')
CONFIG = (ROOT / 'config.py').read_text(encoding='utf-8')
SAMPLE = (ROOT / 'sample.env').read_text(encoding='utf-8')


def test_prevalidated_audio_raw_attach_is_default():
    assert 'DIRECT_PREVALIDATED_RAW_AUDIO' in CONFIG
    assert '"DIRECT_PREVALIDATED_RAW_AUDIO", True' in CONFIG
    assert 'and not bool(getattr(media, "video", False))' in CALLS
    assert 'and getattr(config, "DIRECT_PREVALIDATED_RAW_AUDIO", True)' in CALLS
    assert '"external_audio" if prevalidated_raw_audio and use_raw_cold_path else' in CALLS
    assert "direct_vc_resolver_overlap_started" in CALLS


def test_raw_attach_bypasses_media_stream_check_and_keeps_recovery():
    raw = CALLS[CALLS.index('def _build_initial_direct_raw_stream('):CALLS.index('def _event_timestamp(', CALLS.index('def _build_initial_direct_raw_stream('))]
    assert 'types.raw.AudioStream(' in raw
    assert 'MediaSource.SHELL' in raw
    assert 'types.raw.Stream(microphone=microphone' in raw
    monitor = CALLS[CALLS.index('async def _monitor_initial_direct_play('):CALLS.index('def _cancel_startup_proof(', CALLS.index('async def _monitor_initial_direct_play('))]
    assert 'raw_shell_error = shell_error and playback_source == "raw_direct"' in monitor
    assert '_recover_initial_direct_with_mediastream(' in monitor


def test_vc_metadata_is_warmed_without_joining():
    block = CALLS[CALLS.index('def _schedule_vc_metadata_warm('):CALLS.index('async def has_active_group_call', CALLS.index('def _schedule_vc_metadata_warm('))]
    assert 'get_input_call' in block
    assert '_get_input_group_call' in block
    assert 'join_group_call' not in block
    assert 'joined=0' in block
    assert 'vc_join_metadata_warm_started' in CALLS
    assert 'vc_join_metadata_warm_ready' in CALLS


def test_unmute_reuses_warmed_call_reference():
    block = CALLS[CALLS.index('async def _ensure_assistant_unmuted('):CALLS.index('def _schedule_assistant_unmute', CALLS.index('async def _ensure_assistant_unmuted('))]
    assert 'self._cached_vc_call_ref(call_client, chat_id)' in block
    assert 'call_ref_cache_hit=%s' in block


def test_raw_observer_is_off_by_default_for_latency():
    assert '"DIRECT_RAW_OBSERVER", False' in CONFIG
    assert 'DIRECT_RAW_OBSERVER=False' in SAMPLE


def test_native_payload_prewarm_is_local_only_and_consumed_after_source_ready():
    assert '"DIRECT_VC_NATIVE_PREWARM", True' in CONFIG
    warm = CALLS[CALLS.index('def _schedule_vc_metadata_warm('):CALLS.index('def _pop_vc_native_payload', CALLS.index('def _schedule_vc_metadata_warm('))]
    assert 'create_call(int(chat_id))' in warm
    assert 'join_group_call' not in warm
    helper = CALLS[CALLS.index('async def _play_with_prepared_native_payload('):CALLS.index('async def has_active_group_call', CALLS.index('async def _play_with_prepared_native_payload('))]
    assert 'await connect_call(' in helper
    assert 'types.GroupCallConfig(auto_start=False)' in helper
    assert 'StreamParams.get_stream_params(stream)' in helper


def test_fast_attach_timing_logs_raw_and_native_paths():
    assert 'prewarmed_native_raw' in CALLS
    assert 'raw_no_media_probe' in CALLS
    assert 'mediastream_check_stream' in CALLS
    assert 'vc_fast_attach timing' in CALLS


def test_raw_launcher_execs_absolute_ffmpeg_behind_boost_safe_token():
    block = CALLS[
        CALLS.index('def _boost_shell_safe_prefix('):
        CALLS.index('def _build_initial_direct_raw_stream(', CALLS.index('def _boost_shell_safe_prefix('))
    ]
    assert 'shutil.which("env")' in block
    assert 'return (env_token, "--", executable_abs), "env_absolute_exec"' in block
    assert 'TgCall._same_executable(resolved_env, env_abs)' in block
    assert 'os.path.samefile' in CALLS
    assert 'subprocess.run(' in block
    assert '[launcher_abs, *prefix[1:], "-version"]' in block
    assert 'raw_audio_launcher_ready' in block


def test_raw_command_never_uses_absolute_ffmpeg_as_boost_argv0():
    raw = CALLS[
        CALLS.index('def _build_initial_direct_raw_stream('):
        CALLS.index('def _event_timestamp(', CALLS.index('def _build_initial_direct_raw_stream('))
    ]
    assert 'self._prepare_raw_ffmpeg_launcher()' in raw
    assert 'audio_command = [\n            *launcher_prefix,' in raw
    assert 'video_command = [\n                *launcher_prefix,' in raw
    assert 'ffmpeg = shutil.which("ffmpeg") or "ffmpeg"' not in raw
    assert 'self._raw_shell_command(observed_audio_command)' in raw
    assert 'self._raw_shell_command(video_command)' in raw


def test_raw_launcher_is_preflighted_at_voice_boot():
    boot = CALLS[CALLS.index('async def boot(self) -> None:'):]
    assert 'await asyncio.to_thread(self._prepare_raw_ffmpeg_launcher)' in boot
    assert 'launcher_preflight:' in boot
    assert 'Raw direct launcher disabled at boot; MediaStream fallback' in boot


def test_raw_launcher_has_env_probe_failure_basename_fallback():
    block = CALLS[
        CALLS.index('def _prepare_raw_ffmpeg_launcher('):
        CALLS.index('def _raw_shell_command(', CALLS.index('def _prepare_raw_ffmpeg_launcher('))
    ]
    assert 'mode != "env_absolute_exec"' in block
    assert 'path_pinned_basename_after_env_probe_failure' in block
    assert 'os.environ["PATH"] = os.pathsep.join([directory, *pieces])' in block
    assert 'self._same_executable(resolved, ffmpeg_abs)' in block


def test_raw_launcher_config_exposes_ffmpeg_binary_and_probe_timeout():
    config_src = (ROOT / 'config.py').read_text(encoding='utf-8')
    sample = (ROOT / 'sample.env').read_text(encoding='utf-8')
    assert 'self.FFMPEG_BINARY:' in config_src
    assert 'DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC' in config_src
    assert '# FFMPEG_BINARY=ffmpeg' in sample
    assert '# DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC=3' in sample
