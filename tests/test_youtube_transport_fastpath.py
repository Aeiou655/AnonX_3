from pathlib import Path


PATCH = Path("AnonX_3/plugins/youtube_transport_fastpath.py")
PLUGIN_INIT = Path("AnonX_3/plugins/__init__.py")


def test_transport_fastpath_compiles_and_is_auto_discovered():
    source = PATCH.read_text(encoding="utf-8")
    compile(source, str(PATCH), "exec")

    discovery = PLUGIN_INIT.read_text(encoding="utf-8")
    assert 'glob("*.py")' in discovery
    assert 'file.name != "__init__.py"' in discovery


def test_auto_proxy_forces_explicit_direct_ytdlp_transport():
    source = PATCH.read_text(encoding="utf-8")
    assert 'YOUTUBE_PROXY_MODE' in source
    assert '== "auto"' in source
    assert 'opts["proxy"] = ""' in source
    assert 'cleaned.extend(["--proxy", ""])' in source
    assert 'override=explicit_empty' in source
    assert 'search_proxy_retained=1' in source


def test_explicit_and_off_proxy_modes_remain_operator_controlled():
    source = PATCH.read_text(encoding="utf-8")
    assert 'YTDLP_AUTO_PROXY_COMPAT_BYPASS' in source
    assert 'def _auto_mode()' in source
    assert 'if not _auto_mode()' in source


def test_micro_resolver_uses_isolated_cookie_free_profile_only():
    source = PATCH.read_text(encoding="utf-8")
    assert 'DIRECT_MICRO_COOKIE_FREE' in source
    assert 'clean.pop("cookiefile", None)' in source
    assert 'clean.pop("cookiesfrombrowser", None)' in source
    assert 'str(key).lower() == "cookie"' in source
    assert 'YouTube._run_persistent_direct_player_candidate' in source
    assert 'YouTube._run_persistent_direct_micro_prepare' in source
    assert 'authenticated_fallback_retained=1' in source
    assert 'youtube_micro_fastpath_patch enabled' in source
