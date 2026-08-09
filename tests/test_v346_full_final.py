from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
CALLS = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")
MAIN = (ROOT / "AnonX_3/__main__.py").read_text(encoding="utf-8")
DIR = (ROOT / "AnonX_3/core/dir.py").read_text(encoding="utf-8")
START = (ROOT / "start").read_text(encoding="utf-8")
ENV = (ROOT / ".env").read_text(encoding="utf-8")


def test_play_races_two_full_resolver_workers_and_three_micro_clients():
    assert 'profiles.append(("audio_escape_fast", escape_opts, False))' in YOUTUBE
    assert 'resolver_slot_hint=slot_hint' in YOUTUBE
    assert '0 if idx == 0 else 1' in YOUTUBE
    assert 'DIRECT_AUDIO_ESCAPE_RACE' in CONFIG
    assert 'DIRECT_MICRO_PLAYER_CLIENTS' in CONFIG
    assert '"tv_downgraded", "web_safari", "android_vr"' in YOUTUBE
    assert 'direct_resolver_micro_race_started' in YOUTUBE
    assert 'fastest_valid_206=1' in YOUTUBE


def test_micro_winner_is_proven_and_vplay_can_use_adaptive_pair():
    assert 'async def _probe_direct_source_status' in YOUTUBE
    assert 'audio_status, video_status = await asyncio.gather' in YOUTUBE
    assert 'micro_status not in (200, 206)' in YOUTUBE
    assert 'def _direct_adaptive_av_info' in YOUTUBE
    assert 'video_micro_adaptive_pair' in YOUTUBE
    assert 'video_url' in YOUTUBE


def test_audio_external_jit_has_exact_post_connect_proof():
    assert 'direct_external_jit_capture_ready' in CALLS
    assert 'vc_connected_external_capture' in CALLS
    assert 'first_external_audio_frame_accepted' in CALLS
    assert 'connect_to_real_ms' in CALLS
    assert 'DIRECT_EXTERNAL_PREBUFFER_FRAMES", 4' in CALLS
    assert 'DIRECT_EXTERNAL_PREBUFFER_FRAMES=4' in ENV


def test_vplay_uses_early_placeholder_and_existing_call_swap_without_reconnect():
    assert 'placeholder_only=False' in CALLS
    assert 'vplay_audio_lead_packet_ready' in CALLS
    assert 'DIRECT_VIDEO_AUDIO_LEAD_PACKET_TIMEOUT_SEC' in CONFIG
    assert 'vplay_source_swap_before' in CALLS
    assert 'vplay_source_swap_after' in CALLS
    assert 'direct_video_existing_call_source_swap' in CALLS
    assert 'reconnect=0' in CALLS
    assert 'video_url = str(getattr(source, "video_url", "") or "") or url' in CALLS


def test_external_session_cleanup_precedes_leave_and_owns_expected_shutdown():
    assert 'self._direct_external_audio_sessions: dict[int, dict] = {}' in CALLS
    assert 'external_session = self._direct_external_audio_sessions.get(int(chat_id))' in CALLS
    assert 'await self._close_direct_external_audio(external_session)' in CALLS
    assert 'except (ConnectionNotFound, ConnectionError) as ex:' in CALLS
    stop = CALLS[CALLS.index('    async def stop(self, chat_id: int) -> None:'):]
    assert stop.index('await self._close_direct_external_audio(external_session)') < stop.index('await client.leave_call(chat_id, close=False)')


def test_daily_restart_preserves_media_and_outer_watchdog_is_active():
    assert 'supervisor.spawn("runtime_heartbeat", _runtime_heartbeat)' in MAIN
    assert 'supervisor.spawn("daily_auto_restart", _daily_auto_restart)' in MAIN
    daily = MAIN[MAIN.index('async def _daily_auto_restart'):MAIN.index('async def _start_downloader_api')]
    assert 'media cache preserved' in daily
    assert 'reset_runtime_dirs' not in daily
    reset = DIR[DIR.index('def reset_runtime_dirs'):DIR.index('def runtime_storage_percent')]
    assert 'shutil.rmtree' not in reset
    assert 'mkdir' in reset
    assert 'heartbeat stale' in START
    assert 'python3 -m AnonX_3 &' in START


def test_release_env_contains_no_live_identity_credentials():
    values = {}
    for raw in ENV.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        values[k] = v
    assert values['API_ID'] == '0'
    assert values['API_HASH'] == ''
    assert values['BOT_TOKEN'] == ''
    assert values['LOGGER_ID'] == '0'
    assert values['OWNER_ID'] == '0'
    assert values['SESSION'] == ''
    assert values['DIRECT_MICRO_PLAYER_CLIENTS'] == 'tv_downgraded,web_safari,android_vr'
    assert values['DIRECT_AUDIO_ESCAPE_RACE'].lower() == 'true'
