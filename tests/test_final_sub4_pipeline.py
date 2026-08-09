from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
CALLS = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")
SAMPLE = (ROOT / "sample.env").read_text(encoding="utf-8")


def _block(text: str, start: str, end: str) -> str:
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def test_persistent_ydl_profile_ignores_mutable_cookie_values():
    fp = _block(
        YOUTUBE,
        "def _direct_resolver_opts_fingerprint",
        "def _get_persistent_direct_ydl",
    )
    assert "cookie_identity" not in fp
    assert "_direct_cookie_semantic_fingerprint" not in fp
    runtime = _block(
        YOUTUBE,
        "def _get_persistent_direct_ydl",
        "def _persistent_direct_prepare",
    )
    assert "cookie_identity_by_key" in runtime
    assert "load(ignore_discard=True, ignore_expires=True)" in runtime
    assert "direct_ydl_rebuild reason=cookie_reload_failed" in runtime


def test_micro_player_lane_is_multi_client_bounded_and_raced_with_safe_fallback():
    assert "self._direct_micro_executors = [" in YOUTUBE
    micro = _block(
        YOUTUBE,
        "def _persistent_direct_player_candidate",
        "async def _run_persistent_direct_player_candidate",
    )
    assert 'get_ie("Youtube")' in micro
    assert '"_extract_player_response"' in micro
    assert "client," in micro
    assert "_direct_progressive_info" in micro
    normalize = _block(
        YOUTUBE,
        "def _direct_progressive_info",
        "def _direct_player18_info",
    )
    assert "normalize_unciphered_player_format" in normalize
    assert '"_micro_cipher_recovered"' in normalize
    resolver = _block(
        YOUTUBE,
        "async def _resolve_direct_stream_source_uncached",
        "async def _pyyt_search_tracks",
    )
    assert "DIRECT_MICRO_PLAYER_CLIENTS" in resolver
    assert "direct_resolver_micro_race_started" in resolver
    assert "foreground_micro_adaptive_audio" in resolver
    assert "video_micro_adaptive_pair" in resolver

def test_external_pcm_prebuffer_starts_before_vc_play_and_uses_external_source():
    prepare = _block(
        CALLS,
        "async def _prepare_initial_direct_external_stream",
        "async def _start_initial_direct_external_decoder",
    )
    decoder = _block(
        CALLS,
        "async def _start_initial_direct_external_decoder",
        "async def _build_initial_direct_external_stream",
    )
    assert "MediaSource.EXTERNAL" in prepare
    assert "// 100" in prepare
    assert "placeholder_only" in prepare
    assert "asyncio.create_subprocess_exec" in decoder
    assert '"-f", "s16le"' in decoder
    assert "direct_external_prebuffer_started" in decoder
    assert "direct_vc_resolver_overlap_started" in CALLS
    assert "resolver_overlapped_external_connect" in CALLS

def test_external_pcm_is_injected_immediately_after_connect():
    activate = _block(
        CALLS,
        "async def _activate_direct_external_audio",
        "@staticmethod\n    def _event_timestamp",
    )
    assert "send_external_frame" in activate
    assert "StreamDevice.MICROPHONE" in activate
    assert "FrameData(" in activate
    assert "direct_external_first_real_frame_sent" in activate
    assert "connect_to_real_ms" in activate
    assert '"first_external_audio_frame_accepted"' in activate

    slot = _block(
        CALLS,
        "async def _play_with_startup_slot",
        "async def _discard_empty_prejoin",
    )
    play_pos = slot.index("await client.play(")
    activate_pos = slot.index("await self._activate_direct_external_audio")
    unmute_pos = slot.index('if unmute_mode == "required"')
    assert play_pos < activate_pos < unmute_pos
    assert "external_prebuffer_native" in slot


def test_external_pump_has_eof_recovery_and_owned_cleanup():
    pump = _block(
        CALLS,
        "async def _pump_direct_external_audio",
        "async def _activate_direct_external_audio",
    )
    assert "external_audio_eof_in_gate" in pump
    assert "_try_direct_local_failover" in pump
    assert "await self.play_next(chat_id)" in pump
    assert "_close_direct_external_audio" in pump
    assert "_direct_external_audio_tasks" in CALLS


def test_sub4_controls_are_exposed():
    for name in (
        "DIRECT_MWEB_MICRO_PLAYER",
        "DIRECT_EXTERNAL_PREBUFFER_AUDIO",
        "DIRECT_EXTERNAL_PREBUFFER_FRAMES",
        "DIRECT_EXTERNAL_PREBUFFER_READY_TIMEOUT_SEC",
        "DIRECT_MICRO_TOTAL_BUDGET_SEC",
    ):
        assert name in CONFIG
        assert name in SAMPLE
