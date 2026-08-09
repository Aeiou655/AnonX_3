from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_required_core_cache_modules_exist_and_compile():
    required = [
        ROOT / "AnonX_3/core/cache/keys.py",
        ROOT / "AnonX_3/core/cache/states.py",
        ROOT / "AnonX_3/core/cache/hub.py",
    ]
    for path in required:
        assert path.is_file(), str(path)
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_cache_key_roundtrip():
    import importlib.util

    path = ROOT / "AnonX_3/core/cache/keys.py"
    spec = importlib.util.spec_from_file_location("_anonx_cache_keys_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    key = module.make_cache_key(
        source="youtube",
        source_id="dQw4w9WgXcQ",
        video=False,
        quality="best",
    )
    assert key == "source:youtube:dQw4w9WgXcQ:audio:best"
    parsed = module.parse_cache_key(key)
    assert parsed["media_id"] == "dQw4w9WgXcQ"
    assert parsed["video"] is False
    assert parsed["quality"] == "best"


def test_playback_orchestrator_dependencies_are_present():
    source = (ROOT / "AnonX_3/core/playback_orchestrator.py").read_text(encoding="utf-8")
    assert "from AnonX_3.core.cache.hub import CacheEntry, cache_hub" in source
    assert "from AnonX_3.core.cache.keys import detect_source, make_cache_key" in source
    assert "from AnonX_3.core.cache.states import CacheState" in source
