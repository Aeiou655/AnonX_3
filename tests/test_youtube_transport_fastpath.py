from pathlib import Path


PATCH = Path("AnonX_3/plugins/youtube_transport_fastpath.py")
PLUGIN_INIT = Path("AnonX_3/plugins/__init__.py")


def test_transport_fastpath_compiles_and_is_auto_discovered():
    source = PATCH.read_text(encoding="utf-8")
    compile(source, str(PATCH), "exec")

    discovery = PLUGIN_INIT.read_text(encoding="utf-8")
    assert 'glob("*.py")' in discovery
    assert 'file.name != "__init__.py"' in discovery


def test_only_auto_proxy_is_removed_from_ytdlp_options():
    source = PATCH.read_text(encoding="utf-8")
    assert 'YOUTUBE_PROXY_MODE' in source
    assert 'mode != "auto" or not proxy' in source
    assert 'opts.pop("proxy", None)' in source
    assert 'search_proxy_retained=1' in source


def test_explicit_and_off_proxy_modes_remain_operator_controlled():
    source = PATCH.read_text(encoding="utf-8")
    # The guard must return before mutation for every non-auto mode, including
    # explicit operator proxies and YOUTUBE_PROXY=off.
    guard = source.index('if mode != "auto" or not proxy:')
    mutation = source.index('opts.pop("proxy", None)')
    assert guard < mutation
    assert 'YTDLP_AUTO_PROXY_COMPAT_BYPASS' in source
