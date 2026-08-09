from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")


def test_sticky_profile_worker_reuses_thread_local_ydl():
    assert "self._direct_resolver_executors = [" in YOUTUBE
    assert "ThreadPoolExecutor(max_workers=1" in YOUTUBE
    assert "worker_slot=%s" in YOUTUBE


def test_search_prewarm_is_not_exact_track_class_gated():
    assert "warm_id = str(getattr(file, \"id\", \"\")" in YOUTUBE
    assert "if len(warm_id) == 11:" in YOUTUBE
    assert "isinstance(file, Track) and len" not in YOUTUBE


def test_audio_quality_promotion_is_exact_140():
    assert 'exact_base["format"] = "140"' in YOUTUBE
    assert 'source.format_id == "140" and source.pot_bound' in YOUTUBE
    assert 'promoted_140_cache_hit' in YOUTUBE


def test_pot_binding_provenance_and_client_guard_exist():
    assert "pot_bound: bool = False" in YOUTUBE
    assert '"visible_url" if pot_visible' in YOUTUBE
    assert "direct_dual_stage client_binding_rejected" in YOUTUBE
    assert "pot_bound=%s pot_provenance=%s" in YOUTUBE
