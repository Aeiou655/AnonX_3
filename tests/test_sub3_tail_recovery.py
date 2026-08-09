from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "AnonX_3/plugins/zz_sub3_tail_recovery.py"
PATCH = PATCH_PATH.read_text(encoding="utf-8")
PLUGINS_INIT = (ROOT / "AnonX_3/plugins/__init__.py").read_text(encoding="utf-8")


def test_tail_recovery_compiles_and_loads_after_existing_fastpaths():
    compile(PATCH, str(PATCH_PATH), "exec")
    assert 'tuple(sorted(_list_modules()))' in PLUGINS_INIT
    assert "zz_sub3_tail_recovery" > "youtube_transport_fastpath"
    assert "zz_sub3_tail_recovery" > "startup_fastpath"


def test_cli_escape_is_independent_bounded_and_206_proven():
    assert "DIRECT_SUB3_CLI_ESCAPE" in PATCH
    assert "asyncio.create_subprocess_exec" in PATCH
    assert "DIRECT_SUB3_CLI_ESCAPE_EXTRACT_TIMEOUT_SEC" in PATCH
    assert '"--extractor-retries",' in PATCH
    assert '"--retries",' in PATCH
    assert "DIRECT_SUB3_RESOLVER_HEDGE_WINDOW_SEC" in PATCH
    assert "_probe_direct_source_status" in PATCH
    assert "status not in (200, 206)" in PATCH
    assert "direct_sub3_cli_escape ready" in PATCH
    assert "direct_sub3_resolver_winner" in PATCH
    assert "action=await_authoritative" in PATCH


def test_cli_escape_keeps_exact_140_background_out_of_foreground_race():
    assert "or exact_audio140" in PATCH
    assert "exact_audio140=exact_audio140" in PATCH
    assert 'fmt = "18/bestaudio[ext=m4a]/bestaudio/best"' in PATCH
    assert "fast_progressive=True" in PATCH


def test_preconnect_recovery_is_bounded_and_decoderless_only():
    assert "DIRECT_SUB3_NATIVE_SETTLE_RETRY" in PATCH
    assert "DIRECT_SUB3_NATIVE_SETTLE_FIRST_SEC" in PATCH
    assert "DIRECT_SUB3_NATIVE_SETTLE_SECOND_SEC" in PATCH
    assert 'session.get("process") is not None' in PATCH
    assert 'session.get("activated")' in PATCH
    assert 'session["closed"] = False' in PATCH
    assert "direct_preconnect_native_settle_retry" in PATCH
    assert "direct_preconnect_native_settle_connected" in PATCH
    assert "reserved_slot=None" in PATCH


def test_escape_cancellation_kills_child_and_authoritative_fallback_survives():
    assert "await _kill_process(process)" in PATCH
    assert "for loser in pending:" in PATCH
    assert "loser.cancel()" in PATCH
    assert "return await primary" in PATCH
