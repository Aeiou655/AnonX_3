"""Regression guard for the dual-stage mweb direct path."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = (ROOT / "AnonX_3/core/youtube.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")

LEGACY_TOKENS = (
    "DIRECT_FAST_LANE_CLIENTS",
    "DIRECT_FAST_LANE_MODE",
    "DIRECT_FAST_LANE_TIMEOUT_SEC",
    "DIRECT_FAST_LANE_MAX_403",
    "DIRECT_FAST_LANE_MAX_GATE",
    "DIRECT_FAST_LANE_COOLDOWN_SEC",
    "DIRECT_FAST_LANE_SKIP_WHEN_SATURATED",
    "adaptive_legacy",
    "def _fast_lane_clients",
    "def _fast_lane_opts",
    "def _quarantine_fast_lane_client",
    "async def _fast_lane(",
)

for token in LEGACY_TOKENS:
    assert token not in YOUTUBE, f"legacy fast-lane token remains in youtube.py: {token}"
    assert token not in CONFIG, f"legacy fast-lane config remains: {token}"

assert "def _authoritative_pot_opts" in YOUTUBE
assert "direct_resolver mode=dual_stage_foreground_fast" in YOUTUBE
assert "direct_resolver mode=dual_stage_background_140" in YOUTUBE
assert "_purge_ytdlp_pot_cache()" in YOUTUBE
assert "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC" in CONFIG
assert "DIRECT_MWEB_USE_AD_PLAYBACK_CONTEXT" in CONFIG
assert "DIRECT_MWEB_SKIP_INITIAL_DATA" in CONFIG
assert 'youtube_args["use_ad_playback_context"] = ["true"]' in YOUTUBE
assert 'player_skip.append("initial_data")' in YOUTUBE
assert "direct_dual_stage timing" in YOUTUBE
assert 'fast_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"' in YOUTUBE
assert 'exact_base["format"] = "140"' in YOUTUBE
print("dual-stage mweb regression guard: PASS")
