from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / 'AnonX_3/core/youtube.py').read_text(encoding='utf-8')
CONFIG = (ROOT / 'config.py').read_text(encoding='utf-8')


def test_mweb_profiles_keep_client_and_provider_path():
    assert 'def _authoritative_pot_opts' in YOUTUBE
    assert 'lightweight: bool = False' in YOUTUBE
    assert 'fast_progressive: bool = False' in YOUTUBE
    assert 'youtube_args["player_client"] = [client]' in YOUTUBE
    assert 'if "webpage" not in player_skip' in YOUTUBE
    assert 'opts["sleep_interval_requests"] = 0.0' in YOUTUBE


def test_persistent_direct_resolver_and_prewarm_exist():
    assert 'ThreadPoolExecutor' in YOUTUBE
    assert 'self._direct_resolver_tls = threading.local()' in YOUTUBE
    assert 'def _persistent_direct_extract' in YOUTUBE
    assert 'def warm_direct_stream_source' in YOUTUBE
    assert 'direct_resolver prewarm_started' in YOUTUBE
    assert 'self.warm_direct_stream_source(' in YOUTUBE


def test_config_defaults_are_safe_and_enabled():
    assert '"DIRECT_MWEB_LIGHTWEIGHT", True' in CONFIG
    assert '"DIRECT_RESOLVER_PREWARM", True' in CONFIG
    assert 'DIRECT_RESOLVER_WORKERS' in CONFIG
    assert 'DIRECT_RESOLVER_STARTUP_WARM' in CONFIG
    assert 'DIRECT_BACKGROUND_140_ENABLED' in CONFIG


def test_old_speculative_clients_stay_removed():
    forbidden = [
        'DIRECT_FAST_LANE_CLIENTS',
        'adaptive_legacy',
        'def _fast_lane(',
    ]
    for token in forbidden:
        assert token not in YOUTUBE
