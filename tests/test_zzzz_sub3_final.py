from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "AnonX_3/plugins/zzzz_sub3_final.py"
PATCH = PATCH_PATH.read_text(encoding="utf-8")
PLUGINS_INIT = (ROOT / "AnonX_3/plugins/__init__.py").read_text(encoding="utf-8")


def test_final_sub3_patch_compiles_and_loads_last():
    compile(PATCH, str(PATCH_PATH), "exec")
    assert 'tuple(sorted(_list_modules()))' in PLUGINS_INIT
    assert "zzzz_sub3_final" > "zz_sub3_tail_recovery"
    assert "zzzz_sub3_final" > "youtube_transport_fastpath"


def test_foreground_progressive_strips_pot_but_exact_140_is_retained():
    assert '_PROVIDER_KEYS = ("youtubepot-bgutilhttp", "youtubepot-bgutilscript")' in PATCH
    assert 'if fmt == "140":' in PATCH
    assert "_strip_provider_opts(opts)" in PATCH
    assert "foreground_provider=off exact140_provider=retained" in PATCH
    assert 'if action == "download":' in PATCH


def test_authenticated_mweb_micro_is_single_bounded_hedge():
    assert 'DIRECT_RESOLVER_PARALLEL_MICRO", True' in PATCH
    assert 'DIRECT_MICRO_PLAYER_CLIENTS", ("mweb",)' in PATCH
    assert 'DIRECT_MICRO_TOTAL_BUDGET_SEC", 1.30' in PATCH
    assert 'DIRECT_MICRO_LANE_TIMEOUT_SEC", 1.05' in PATCH
    assert 'DIRECT_MICRO_PROBE_TIMEOUT_SEC", 0.25' in PATCH
    assert 'DIRECT_SUB3_CLI_ESCAPE"] = "0"' in PATCH


def test_explicit_youtube_url_has_zero_metadata_search_gate():
    assert "youtube_path=direct_id_zero_search" in PATCH
    assert "self.warm_direct_stream_source(" in PATCH
    assert 'title="YouTube Video"' in PATCH
    branch = PATCH.split("async def _zero_lookup_direct_url", 1)[1]
    branch = branch.split("YouTube._authoritative_pot_opts", 1)[0]
    assert "_pyyt_search_tracks" not in branch


def test_botguard_spam_sources_are_off_normal_path():
    assert 'DIRECT_BACKGROUND_140_ENABLED", False' in PATCH
    assert 'download_pot=0' in PATCH
    assert 'foreground_pot=0' in PATCH


def test_stale_delete_is_idempotent_not_error():
    assert 'method != "deleteMessage"' in PATCH
    assert '"message to delete not found" in normalized' in PATCH
    assert "stale cleanup ignored" in PATCH
    assert "return await original_request(self, method, payload)" in PATCH
