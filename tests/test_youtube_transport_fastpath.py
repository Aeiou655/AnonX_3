from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "AnonX_3/plugins/youtube_transport_fastpath.py"
SOURCE = PATCH.read_text(encoding="utf-8")
PLUGINS_INIT = (ROOT / "AnonX_3/plugins/__init__.py").read_text(encoding="utf-8")


def test_transport_fastpath_compiles_and_is_auto_discovered():
    compile(SOURCE, str(PATCH), "exec")
    discovery = PLUGINS_INIT
    assert 'glob("*.py")' in discovery
    assert 'file.name != "__init__.py"' in discovery


def test_auto_proxy_forces_explicit_direct_ytdlp_transport():
    assert 'YOUTUBE_PROXY_MODE' in SOURCE
    assert 'opts["proxy"] = ""' in SOURCE
    assert 'cleaned.extend(["--proxy", ""])' in SOURCE
    assert 'override=explicit_empty' in SOURCE
    assert 'search_proxy_retained=1' in SOURCE


def test_sub3_removes_proven_losing_parallel_lanes_only():
    assert 'DIRECT_SUB3_MODE' in SOURCE
    assert 'DIRECT_RESOLVER_PARALLEL_MICRO' in SOURCE
    assert 'DIRECT_AUDIO_ESCAPE_RACE' in SOURCE
    assert 'direct_sub3_lane_suppressed lane=video_lightweight' in SOURCE
    assert 'robust_fallback_retained=1' in SOURCE
    assert 'background140' in SOURCE


def test_sub3_prewarm_matches_foreground_worker_slots():
    assert 'audio-foreground-fast' in SOURCE
    assert 'video-escape-fast' in SOURCE
    assert 'profiles.append(("audio-foreground-fast", audio_fast, 0))' in SOURCE
    assert 'profiles.append(("video-escape-fast", video_escape, 1))' in SOURCE
    assert 'direct_sub3_pinned_warm' in SOURCE
    assert 'self._persistent_direct_prepare' in SOURCE


def test_sub3_reduces_external_pcm_prime_to_two_frames():
    assert 'DIRECT_EXTERNAL_PREBUFFER_FRAMES' in SOURCE
    assert 'min(current_frames, 2)' in SOURCE
    assert 'prebuffer_frames=%s' in SOURCE
