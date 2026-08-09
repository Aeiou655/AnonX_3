from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = (ROOT / "AnonX_3/core/calls.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.py").read_text(encoding="utf-8")
SAMPLE = (ROOT / "sample.env").read_text(encoding="utf-8")


def test_metadata_warm_seeds_pytgcalls_internal_input_call_cache():
    assert "def _seed_pytgcalls_input_call_cache" in CALLS
    block = CALLS[CALLS.index("def _seed_pytgcalls_input_call_cache"):CALLS.index("def _schedule_vc_metadata_warm", CALLS.index("def _seed_pytgcalls_input_call_cache"))]
    assert 'getattr(bridge, "_bind_client", None)' in block
    assert '"_cache"' in block
    assert "set_cache(int(chat_id), call_ref)" in block
    warm = CALLS[CALLS.index("def _schedule_vc_metadata_warm"):CALLS.index("def _pop_vc_native_payload", CALLS.index("def _schedule_vc_metadata_warm"))]
    assert "cache_seeded = self._seed_pytgcalls_input_call_cache" in warm
    assert "pytgcalls_input_call=%s cache_seeded=%s" in warm


def test_prepared_native_uses_warmed_call_ref_without_network_guard():
    block = CALLS[CALLS.index("async def _play_with_prepared_native_payload"):CALLS.index("async def _overlap_required_unmute", CALLS.index("async def _play_with_prepared_native_payload"))]
    assert "self._cached_vc_call_ref(call_client, int(chat_id))" in block
    assert "self._seed_pytgcalls_input_call_cache" in block
    assert "await connect_call(" in block
    assert "vc_native_payload_handoff" in block
    assert "prepared=1" in block
    assert "DIRECT_VC_DEFER_SOURCE_REFRESH" in block


def test_required_unmute_races_only_after_source_ready_play_entry():
    overlap = CALLS[CALLS.index("async def _overlap_required_unmute"):CALLS.index("async def has_active_group_call", CALLS.index("async def _overlap_required_unmute"))]
    assert "EditGroupCallParticipant" in overlap
    assert "DIRECT_VC_UNMUTE_INITIAL_DELAY_MS" in overlap
    assert "DIRECT_VC_UNMUTE_ATTEMPTS" in overlap
    slot = CALLS[CALLS.index("async def _play_with_startup_slot"):CALLS.index("async def _discard_empty_prejoin", CALLS.index("async def _play_with_startup_slot"))]
    assert "vc-unmute-overlap" in slot
    assert "await unmute_overlap_task" in slot
    assert "unmute_overlap=%s" in slot
    # This function is entered only by the source-ready play path; metadata warm
    # itself must still contain no Telegram JoinGroupCall.
    warm = CALLS[CALLS.index("def _schedule_vc_metadata_warm"):CALLS.index("def _pop_vc_native_payload", CALLS.index("def _schedule_vc_metadata_warm"))]
    assert "join_group_call" not in warm


def test_sub5_tail_controls_are_exposed():
    for name in (
        "DIRECT_VC_DEFER_SOURCE_REFRESH",
        "DIRECT_VC_UNMUTE_OVERLAP",
        "DIRECT_VC_UNMUTE_INITIAL_DELAY_MS",
        "DIRECT_VC_UNMUTE_RETRY_MS",
        "DIRECT_VC_UNMUTE_ATTEMPTS",
        "DIRECT_FIRST_PACKET_POLL_MS",
    ):
        assert name in CONFIG
        assert name in SAMPLE
