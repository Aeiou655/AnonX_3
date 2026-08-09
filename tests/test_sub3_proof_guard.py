from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "AnonX_3/plugins/sub3_proof_guard.py"
PATCH = PATCH_PATH.read_text(encoding="utf-8")
PLUGINS_INIT = (ROOT / "AnonX_3/plugins/__init__.py").read_text(encoding="utf-8")


def test_sub3_proof_guard_compiles_and_is_auto_discovered():
    compile(PATCH, str(PATCH_PATH), "exec")
    assert 'glob("*.py")' in PLUGINS_INIT
    assert 'file.name != "__init__.py"' in PLUGINS_INIT


def test_keepalive_silence_cannot_satisfy_audible_proof():
    assert 'accepted.is_set() and attached_event.is_set()' in PATCH
    assert 'direct_sub3_real_pcm_clock_armed' in PATCH
    assert 'outgoing_time > baseline' in PATCH
    assert 'ntgcalls_outgoing_clock_advanced_after_real_pcm' in PATCH
    assert 'real_pcm=1' in PATCH


def test_non_external_paths_keep_stock_observer():
    assert 'original = TgCall._observe_initial_direct_media' in PATCH
    assert 'return await original(' in PATCH
    assert 'DIRECT_SUB3_REAL_PCM_PROOF' in PATCH
