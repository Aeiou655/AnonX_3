from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(encoding="utf-8")


def test_initial_direct_resolves_before_vc_join():
    resolver = CALLS.index('trace.set_meta(mode="new-direct-late-join")')
    resolve_call = CALLS.index('direct_source = await yt.resolve_direct_stream_source(', resolver)
    late_join = CALLS.index('trace.mark("vc_join_start")', resolve_call)
    play_call = CALLS.index('await self._play_with_startup_slot(', late_join)
    assert resolver < resolve_call < late_join < play_call


def test_initial_direct_has_no_empty_prejoin_transaction():
    hot = CALLS[CALLS.index('if can_try_direct:'):CALLS.index('if remote_url:', CALLS.index('if can_try_direct:'))]
    assert 'await await_parallel_ready(' not in hot
    assert '_prepare_initial_direct_call(' not in hot


def test_initial_direct_requires_unmute_during_real_stream_join():
    start = CALLS.index('async def _direct_play()')
    block = CALLS[start:CALLS.index('try:', CALLS.index('try:', start) + 4)]
    assert '"required" if initial_parallel_direct else "background"' in block
    assert 'stream=direct_stream' in block
