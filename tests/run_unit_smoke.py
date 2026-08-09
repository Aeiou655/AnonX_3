#!/usr/bin/env python3
"""Lightweight unit smoke tests (no pytest required).

Run from AnonX deploy root:
  python tests/run_unit_smoke.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# The suite intentionally exercises warning/error paths. Keep those synthetic
# records out of the deployed bot's log.txt.
os.environ.setdefault("AnonX_TESTING", "1")

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    # Runtime now fails closed when an authenticated YouTube operation is
    # started without the configured Netscape cookie file. The smoke suite uses
    # mocked yt-dlp calls, so provide a harmless local test fixture at the same
    # resolved path as the VPS configuration.
    _SMOKE_YOUTUBE_COOKIE = Path("/root/youtube-cookies.txt")
    _SMOKE_YOUTUBE_COOKIE.parent.mkdir(parents=True, exist_ok=True)
    if not _SMOKE_YOUTUBE_COOKIE.exists():
        _SMOKE_YOUTUBE_COOKIE.write_text(
            "# Netscape HTTP Cookie File\n",
            encoding="utf-8",
        )
except Exception:
    pass


_DIRECT_MODULE_SEQUENCE = 0


def _direct_load_module(path: Path, label: str):
    """Load one source file without importing its parent package."""
    import importlib.util

    global _DIRECT_MODULE_SEQUENCE
    _DIRECT_MODULE_SEQUENCE += 1
    module_name = (
        f"_anonx_smoke_{label}_{os.getpid()}_{_DIRECT_MODULE_SEQUENCE}"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses and other stdlib helpers resolve annotations through
    # sys.modules while executing the module.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _exec_source_definitions(
    path: Path,
    names: set[str],
    namespace: dict,
) -> dict:
    """Execute selected top-level definitions without module side effects."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in names
    ]
    found = {node.name for node in selected}
    assert found == names, f"missing definitions in {path}: {sorted(names - found)}"
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def test_cache_keys_states():
    from AnonX_3.core.cache.keys import make_cache_key, parse_cache_key
    from AnonX_3.core.cache.states import CacheState, can_transition

    k = make_cache_key(source="youtube", source_id="dQw4w9wgk", video=False)
    assert k == "source:youtube:dQw4w9wgk:audio:best"
    assert parse_cache_key(k)["video"] is False
    assert can_transition(CacheState.MISS, CacheState.RESOLVING)
    assert not can_transition(CacheState.FAILED_PERMANENT, CacheState.READY)


def test_runner_anchors_working_directory():
    assert Path.cwd().resolve() == ROOT.resolve()


def test_matcher():
    from AnonX_3.core.resolver.matcher import (
        is_safe_query_title_rescue,
        pick_best,
        score_candidate,
    )
    from AnonX_3.helpers import Track

    s = score_candidate(
        seed_title="Hello",
        seed_artist="Adele",
        seed_duration_sec=295,
        cand_title="Hello",
        cand_artist="Adele",
        cand_duration_sec=296,
    )
    assert s.total >= 0.85
    cands = [
        Track(id="a", title="Hello", channel_name="Adele", duration_sec=295),
        Track(id="b", title="Hello Live Cover", channel_name="X", duration_sec=400),
    ]
    best, sc = pick_best(
        cands,
        seed_title="Hello",
        seed_artist="Adele",
        seed_duration_sec=295,
    )
    assert best and best.id == "a"
    rescue_args = {
        "query": "တောက်တီးတောက်တဲ့",
        "candidate_title": (
            "တောက်တီးတောက်တဲ့ & Remix Version ( MOE RMX ).mp3"
        ),
        "seed_duration_sec": 220,
        "candidate_duration_sec": 229.532,
    }
    assert is_safe_query_title_rescue(**rescue_args)
    assert is_safe_query_title_rescue(
        **{
            **rescue_args,
            "candidate_title": (
                "တောက်တီးတောက်တဲ့\u200d Remix Version"
            ),
        }
    )
    assert not is_safe_query_title_rescue(
        **{
            **rescue_args,
            "candidate_title": "တိက်တုတိက်တ Remix Version",
        }
    )
    assert not is_safe_query_title_rescue(
        **{**rescue_args, "candidate_duration_sec": 230.1}
    )
    assert not is_safe_query_title_rescue(
        **{**rescue_args, "candidate_duration_sec": float("nan")}
    )
    assert not is_safe_query_title_rescue(
        **{**rescue_args, "query": "Love"}
    )
    assert not is_safe_query_title_rescue(
        **{
            **rescue_args,
            "candidate_title": (
                "MOERMX တောက်တီးတောက်တဲ့ Remix Version"
            ),
        }
    )
    assert not is_safe_query_title_rescue(
        **{
            **rescue_args,
            "candidate_title": (
                "တောက်တီးတောက်တဲ့ Another Song"
            ),
        }
    )


def test_singleflight():
    from AnonX_3.core.downloader.singleflight import SingleFlight

    sf = SingleFlight("test")
    counter = {"n": 0}

    async def work():
        counter["n"] += 1
        await asyncio.sleep(0.05)
        return "ok"

    async def run():
        results = await asyncio.gather(
            sf.do("k", work),
            sf.do("k", work),
            sf.do("k", work),
        )
        assert results == ["ok", "ok", "ok"]
        assert counter["n"] == 1

        release = asyncio.Event()
        survivor_started = asyncio.Event()

        async def cancellable_work():
            survivor_started.set()
            await release.wait()
            return "survived"

        cancelled_waiter = asyncio.create_task(sf.do("shielded", cancellable_work))
        surviving_waiter = asyncio.create_task(sf.do("shielded", cancellable_work))
        await survivor_started.wait()
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass
        release.set()
        assert await surviving_waiter == "survived"
        assert not sf.is_running("shielded")

    asyncio.run(run())


def test_singleflight_shutdown_cancels_factory_ownership():
    """Shutdown owns the shielded factory, not only its outer flight task."""
    from AnonX_3.core.downloader.singleflight import SingleFlight

    async def verify():
        sf = SingleFlight("shutdown-test")
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        waiter = asyncio.create_task(sf.do("owned", work))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        assert len(sf._factory_tasks) == 1
        await asyncio.wait_for(sf.shutdown(), timeout=1.0)
        assert finalized.is_set()
        assert sf._inflight == {}
        assert sf._factory_tasks == set()
        assert sf._locks == {}
        assert sf._owner == {}
        assert sf._started_at == {}
        assert sf._closing is True
        assert sf._shutdown_complete is True

        await sf.shutdown()
        try:
            await sf.do("late", work)
        except RuntimeError as ex:
            assert "shutting down" in str(ex)
        else:
            raise AssertionError("SingleFlight accepted work after shutdown")

    asyncio.run(verify())


def test_provider_shutdown_owns_cache_and_download_tasks():
    """Each external provider drains its explicit registries and flight."""

    providers = [
        ("telegram.py", "Telegram", "_telegram_download_flight", "active_tasks"),
        ("tiktok.py", "TikTok", "_tiktok_download_flight", "_active_tasks"),
        ("facebook.py", "Facebook", "_facebook_download_flight", "_active_tasks"),
    ]

    async def verify_provider(filename, class_name, flight_name, active_name):
        path = ROOT / "AnonX_3" / "core" / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        provider_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        shutdown = next(
            node
            for node in provider_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "shutdown"
        )
        start_cache = next(
            node
            for node in provider_class.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "start_current_cache"
        )
        start_source = ast.get_source_segment(source, start_cache)
        assert "if self._shutting_down:" in start_source
        assert "asyncio.all_tasks(" not in source

        harness = ast.ClassDef(
            name="ProviderShutdownHarness",
            bases=[],
            keywords=[],
            body=[shutdown],
            decorator_list=[],
        )
        module = ast.Module(body=[harness], type_ignores=[])
        ast.fix_missing_locations(module)

        class FailingFlight:
            def __init__(self):
                self.calls = 0

            async def shutdown(self):
                self.calls += 1
                raise RuntimeError("injected flight close failure")

        flight = FailingFlight()
        namespace = {"asyncio": asyncio, flight_name: flight}
        exec(compile(module, str(path), "exec"), namespace)
        provider = namespace["ProviderShutdownHarness"]()
        provider._shutdown_lock = asyncio.Lock()
        provider._shutting_down = False
        provider._shutdown_complete = False
        provider.current_cache = {}
        setattr(provider, active_name, {})
        if class_name == "Telegram":
            provider.active = [1]
            provider.events = {1: asyncio.Event()}
            provider.last_edit = {1: object()}

        finalized: list[str] = []

        def task_for(label: str) -> asyncio.Task:
            async def work():
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.append(label)

            return asyncio.create_task(work(), name=f"provider-test:{label}")

        cache_task = task_for(f"{class_name}:cache")
        active_task = task_for(f"{class_name}:active")
        await asyncio.sleep(0)
        provider.current_cache[1] = (object(), cache_task)
        getattr(provider, active_name)[2] = active_task
        event = provider.events[1] if class_name == "Telegram" else None

        results = await asyncio.gather(
            provider.shutdown(),
            provider.shutdown(),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], ExceptionGroup)
        assert flight.calls == 1
        assert provider.current_cache == {}
        assert getattr(provider, active_name) == {}
        assert cache_task.done() and active_task.done()
        assert sorted(finalized) == sorted(
            [f"{class_name}:cache", f"{class_name}:active"]
        )
        assert provider._shutting_down is True
        assert provider._shutdown_complete is True
        if class_name == "Telegram":
            assert event.is_set()
            assert provider.events == {}
            assert provider.active == []
            assert provider.last_edit == {}

        await provider.shutdown()
        assert flight.calls == 1

    async def verify():
        for provider in providers:
            await verify_provider(*provider)

    asyncio.run(verify())


def test_security():
    from AnonX_3.core.security import (
        clamp_duration_sec,
        sanitize_filename,
        validate_http_url,
        is_safe_relative_path,
        redact_secrets,
    )

    assert sanitize_filename("../etc/passwd") == "passwd" or "passwd" in sanitize_filename(
        "../etc/passwd"
    )
    ok, reason = validate_http_url("https://example.com/a")
    assert ok, reason
    ok, reason = validate_http_url("http://127.0.0.1/secret")
    assert not ok
    assert clamp_duration_sec(100, 3600)
    assert not clamp_duration_sec(99999, 3600)
    red = redact_secrets("api_key=supersecret mongodb://user:pass@host/db")
    assert "supersecret" not in red
    assert "mongodb://***" in red or "***" in red


def test_metrics_race():
    from AnonX_3.core.metrics import metrics, mark_cache_hit, mark_direct_ok
    from AnonX_3.core.playback_orchestrator import decide_race, RaceDecision

    mark_cache_hit()
    mark_direct_ok(0.5)
    snap = metrics.snapshot()
    assert "cache_hit" in snap["counters"]
    r = decide_race(direct_ok=False, local_path=None, local_pending=True)
    assert r.decision == RaceDecision.WAIT_LOCAL


def test_race_matrix_complete():
    from AnonX_3.core.playback_orchestrator import decide_race, RaceDecision

    assert decide_race(direct_ok=True, local_path=None, local_pending=True).decision == RaceDecision.USE_DIRECT
    assert decide_race(direct_ok=False, local_path=None, local_pending=True).decision == RaceDecision.WAIT_LOCAL
    assert decide_race(direct_ok=False, local_path=None, local_pending=False).decision == RaceDecision.FALLBACK
    assert decide_race(direct_ok=None, local_path=None, local_pending=True).decision == RaceDecision.WAIT_LOCAL


def test_quality_plan():
    from AnonX_3.core.resource_manager import resource_manager

    plan = resource_manager.select_quality_plan("good")
    assert plan.tier in {"poor", "normal", "good"}
    assert plan.video_max_height in {360, 480, 720}
    assert plan.max_parallel_downloads >= 1


def test_probe_soft_403_does_not_block():
    """Soft probe: 403 from CDN must still allow try-direct (parallel local)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from AnonX_3.core.playback_orchestrator import probe_direct_url
    from AnonX_3 import config as cfg

    old = getattr(cfg, "DIRECT_URL_PROBE", "off")
    cfg.DIRECT_URL_PROBE = "soft"

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.content = MagicMock()
            self.content.read = AsyncMock(return_value=b"")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def head(self, *a, **k):
            return _Resp(403)

        def get(self, *a, **k):
            return _Resp(403)

    async def _run():
        with patch("aiohttp.ClientSession", _Session):
            ok, reason = await probe_direct_url(
                "https://googlevideo.com/videoplayback?id=1"
            )
        assert ok is True, reason
        assert "soft" in reason or reason.startswith("soft_")
        # off mode: no HTTP, immediate pass
        cfg.DIRECT_URL_PROBE = "off"
        ok2, reason2 = await probe_direct_url(
            "https://googlevideo.com/videoplayback?id=1"
        )
        assert ok2 is True
        assert reason2 == "probe_off"

    try:
        asyncio.run(_run())
    finally:
        cfg.DIRECT_URL_PROBE = old


def test_prefetch_same_media_and_pick_ready():
    """Parallel path helpers: id match + disk READY pick."""
    import tempfile
    from pathlib import Path

    from AnonX_3.core.prefetch import PrefetchManager
    from AnonX_3.core.youtube import YouTube

    pm = PrefetchManager()

    class A:
        id = "VSwTexQ5dHY"
        video = False

    class B:
        id = "VSwTexQ5dHY"
        video = False

    assert pm._same_media(A(), B()) is True
    assert pm._same_media(A(), None) is False

    # complete file ready
    d = Path("downloads")
    d.mkdir(exist_ok=True)
    p = d / "VSwTexQ5dHY.webm"
    try:
        p.write_bytes(b"x" * (70 * 1024))
        assert YouTube.is_complete_media_file(str(p), min_bytes=64 * 1024)
    finally:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def test_completed_current_cache_survives_until_playback_handoff():
    """A 100%-complete cache owner remains available to play_media.

    The local download can finish in the event-loop turn between the direct
    stream fallback and its await_current_cache_or_download() call.  Retaining
    that terminal owner prevents the one-shot guard from discarding the ready
    artifact and leaving the status card at 100%.
    """
    from unittest.mock import AsyncMock, patch

    from AnonX_3 import yt
    from AnonX_3.core.prefetch import PrefetchManager
    from AnonX_3.helpers import Media

    async def verify():
        manager = PrefetchManager()
        chat_id = -100778899
        path = "downloads/CompletedHandoff.m4a"
        owner = Media(id="CompletedHandoff", source="youtube", video=False)
        owner.local_path = path

        async def completed_download():
            return path

        task = asyncio.create_task(completed_download())
        manager._bind_current_owner(chat_id, owner, task)
        assert await task == path
        await asyncio.sleep(0)  # Run done callbacks before playback joins.
        assert manager.current_cache[chat_id][1] is task

        playback_media = Media(
            id="CompletedHandoff", source="youtube", video=False
        )
        with (
            patch.object(
                yt,
                "is_complete_media_file",
                side_effect=lambda candidate, **_kwargs: candidate == path,
            ),
            patch.object(yt, "render_completed_download_progress", AsyncMock()),
        ):
            result = await manager.await_current_cache_or_download(
                chat_id, playback_media
            )

        assert result == path
        assert playback_media.local_path == path
        assert chat_id not in manager.current_cache

    asyncio.run(verify())


def test_security_ssrf():
    from AnonX_3.core.security import validate_http_url, clamp_duration_sec, sanitize_filename
    from AnonX_3.core.playback_orchestrator import validate_direct_url

    assert validate_http_url("https://example.com/a.mp3")[0]
    assert not validate_http_url("http://127.0.0.1/x")[0]
    assert not validate_http_url("file:///etc/passwd")[0]
    assert validate_direct_url("https://example.com/a.mp3") == (True, "validated")
    assert not validate_direct_url("http://127.0.0.1/x")[0]
    assert clamp_duration_sec(100, 3600)
    assert not clamp_duration_sec(99999, 100)
    assert "passwd" in sanitize_filename("../etc/passwd")


def test_gate_fatal_event():
    from AnonX_3.core.playback_orchestrator import PlaySession, PlaybackStartupGate

    async def _run():
        gate = PlaybackStartupGate()
        # fake media-like
        class M:
            id = "abcdefghijk"
            video = False

        s = gate.begin(123, M())
        # Gate window only after direct start is attempted (not refcount-only begin)
        assert gate.in_gate_window(123) is False
        s.direct_attempted = True
        assert gate.in_gate_window(123) is True
        s.signal_fatal("test_fatal")
        assert s.fatal_event.is_set()
        assert s.fatal_reason == "test_fatal"
        gate.end(123)

        async def ok_play():
            return None

        ok = gate.begin(124, M())
        result = await gate.confirm_direct_start(
            124, play_coro=ok_play(), proof_sec=0.01
        )
        assert result.ok is True
        assert ok.direct_ok is True
        gate.end(124)

        async def fatal_play():
            asyncio.get_running_loop().call_soon(
                gate.signal_fatal, 125, "stream_ended_in_gate"
            )
            return None

        failed = gate.begin(125, M())
        result = await gate.confirm_direct_start(
            125, play_coro=fatal_play(), proof_sec=0.05
        )
        assert result.ok is False
        assert result.fatal is True
        assert result.reason == "stream_ended_in_gate"
        assert failed.direct_ok is False
        gate.end(125)

        # The cold-start contract returns after play acceptance while proof is
        # still pending; an early fatal event wakes the background monitor.
        accepted = gate.begin(126, M())
        result = await gate.accept_direct_start(126, play_coro=ok_play())
        assert result.ok is True
        assert accepted.direct_ok is False
        assert accepted.proof_complete is False
        assert gate.in_gate_window(126) is True
        monitor = asyncio.create_task(
            gate.monitor_direct_proof(126, proof_sec=1.0)
        )
        await asyncio.sleep(0)
        gate.signal_fatal(126, "async_stream_ended")
        proved = await monitor
        assert proved.ok is False
        assert proved.fatal is True
        assert proved.reason == "async_stream_ended"
        assert accepted.proof_complete is True
        assert gate.in_gate_window(126) is False
        gate.end(126)

    asyncio.run(_run())


def test_parallel_initial_readiness():
    from AnonX_3.core.playback_orchestrator import await_parallel_ready

    async def _run():
        join_started = asyncio.Event()
        source_started = asyncio.Event()
        release = asyncio.Event()

        async def join():
            join_started.set()
            await release.wait()
            return "joined"

        async def source():
            source_started.set()
            await release.wait()
            return "raw"

        task = asyncio.create_task(await_parallel_ready(join(), source()))
        await asyncio.wait_for(join_started.wait(), timeout=0.5)
        await asyncio.wait_for(source_started.wait(), timeout=0.5)
        assert task.done() is False
        release.set()
        assert await task == ("joined", "raw")

        source_cancelled = asyncio.Event()
        second_source_started = asyncio.Event()

        async def failed_join():
            await second_source_started.wait()
            raise RuntimeError("join failed")

        async def pending_source():
            second_source_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                source_cancelled.set()

        try:
            await await_parallel_ready(failed_join(), pending_source())
        except RuntimeError as ex:
            assert str(ex) == "join failed"
        else:
            raise AssertionError("join failure did not propagate")
        assert source_cancelled.is_set()

    asyncio.run(_run())


def test_youtube_client_ladder():
    from AnonX_3.core.resolver.youtube_clients import build_extract_attempts, player_clients

    assert len(player_clients()) >= 1
    attempts = build_extract_attempts({"format": "bestaudio"}, use_po=True)
    assert len(attempts) >= 2


def test_direct_watchdog():
    from AnonX_3.core.stream_watch import DirectWatchdog

    w = DirectWatchdog()

    class M:
        id = "abcdefghijk"
        video = False
        local_path = None
        time = 0
        stream_url = "https://example.com/x"
        source = "youtube_remote"
        duration_sec = 200

    w.arm(1, M(), source="youtube_remote")
    assert w.get(1) is not None
    w.update_local(1, "/tmp/a.webm")
    assert w.get(1).local_path.endswith("a.webm")
    # Early death on remote → failover candidate
    assert w.should_failover_on_stream_end(1, M()) is True
    ent = w.consume_failover(1)
    assert ent is not None
    assert w.get(1) is None


def test_stream_end_natural_vs_gate():
    """Natural song end must advance/leave; local refcount gate must not block."""
    import time as _time

    from AnonX_3.core.playback_orchestrator import PlaybackStartupGate
    from AnonX_3.core.stream_watch import DirectWatchdog

    class Media:
        def __init__(self, **kw):
            self.id = kw.get("id", "abcdefghijk")
            self.video = False
            self.local_path = kw.get("local_path")
            self.time = 0
            self.stream_url = kw.get("stream_url", "https://example.com/x")
            self.source = kw.get("source", "youtube_remote")
            self.duration_sec = kw.get("duration_sec", 180)

    gate = PlaybackStartupGate()
    # Local/CDN path: begin() only for refcount — NOT a gate window
    gate.begin(42, Media(id="localtrack1"))
    assert gate.in_gate_window(42) is False

    # Active direct confirm: in window
    sess = gate.begin(43, Media(id="direct1"))
    sess.direct_attempted = True
    assert gate.in_gate_window(43) is True
    sess.direct_ok = True
    sess.proof_complete = True
    assert gate.in_gate_window(43) is False

    w = DirectWatchdog()
    m = Media(duration_sec=20, stream_url="https://example.com/x")
    w.arm(7, m, source="youtube_remote")
    # Simulate near-end of a short track (natural completion inside 45s window)
    ent = w.get(7)
    assert ent is not None
    ent.started_at = _time.time() - 18.0  # 18s of 20s track
    assert w.should_failover_on_stream_end(7, m) is False

    # Early death of long track still fails over
    w2 = DirectWatchdog()
    m2 = Media(duration_sec=240, stream_url="https://example.com/y")
    w2.arm(8, m2, source="youtube_remote")
    assert w2.should_failover_on_stream_end(8, m2) is True

    # Local file path must not be treated as remote direct
    w3 = DirectWatchdog()
    m3 = Media(
        duration_sec=120,
        stream_url="/data/media/ready/a.webm",
        source="youtube_local",
    )
    w3.arm(9, m3, source="youtube_remote")
    assert w3.should_failover_on_stream_end(9, m3) is False


def test_store():
    from AnonX_3.core.cdn.store import MediaStore

    td = tempfile.mkdtemp()
    store = MediaStore(Path(td) / "media.db")
    store.upsert_ready(
        key="source:youtube:abc:audio:best",
        media_id="abc",
        video=False,
        quality_tier="",
        filename="a.webm",
        ready_path=str(Path(td) / "a.webm"),
        size_bytes=9999,
        title="T",
    )
    row = store.get("source:youtube:abc:audio:best")
    assert row and row.status == "ready"
    store.set_status(row.key, "downloading")
    assert store.get(row.key).status == "downloading"

    durable = store.upsert_ready(
        key="source:youtube:durable:audio:best",
        media_id="durable",
        video=False,
        quality_tier="",
        filename="durable.m4a",
        ready_path=str(Path(td) / "durable.m4a"),
        size_bytes=9999,
        title="Durable local track",
        ttl_hours=1,
        local_durable=True,
    )
    assert durable.local_durable is True
    assert durable.expires_at == 0
    # A later CDN metadata update must not accidentally reintroduce a TTL.
    updated = store.upsert_ready(
        key=durable.key,
        media_id="durable",
        video=False,
        quality_tier="",
        filename="durable.m4a",
        ready_path=str(Path(td) / "durable.m4a"),
        size_bytes=9999,
        ttl_hours=1,
    )
    assert updated.local_durable is True
    assert updated.expires_at == 0
    store.touch(durable.key, extend_ttl_hours=1)
    assert store.get(durable.key).expires_at == 0
    with store._connect() as conn:
        conn.execute(
            "UPDATE media_assets SET last_access = 0 WHERE key = ?", (durable.key,)
        )
        conn.commit()
    assert durable.key not in {asset.key for asset in store.expired(1)}


def test_normalized_title_cache_lookup_returns_valid_local_entry():
    """Exact normalized title/query hits stay local and reject near misses."""
    from unittest.mock import patch

    from AnonX_3.core.cache.hub import cache_hub
    from AnonX_3.core.cache.keys import make_cache_key
    from AnonX_3.core.cdn.store import MediaStore

    class ClosingMediaStore(MediaStore):
        """MediaStore uses sqlite context managers that commit but do not close."""

        def __init__(self, db_path):
            self._test_connections = []
            super().__init__(db_path)

        def _connect(self):
            connection = super()._connect()
            self._test_connections.append(connection)
            return connection

        def close_for_test(self):
            for connection in self._test_connections:
                try:
                    connection.close()
                except Exception:
                    pass

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        ready_path = temp_root / "cached-track.m4a"
        ready_path.write_bytes(b"x" * (128 * 1024))
        store = ClosingMediaStore(temp_root / "media.db")
        media_id = "AbCdEfGhI12"
        key = make_cache_key(source="youtube", source_id=media_id, video=False)
        try:
            store.upsert_ready(
                key=key,
                media_id=media_id,
                video=False,
                quality_tier="",
                filename=ready_path.name,
                ready_path=str(ready_path),
                size_bytes=ready_path.stat().st_size,
                query="  Cache\tFirst\nSong  ",
                title="Cache First Song",
                artist="Local Artist",
                duration=123,
            )

            with (
                patch.object(cache_hub, "store", return_value=store),
                patch(
                    "AnonX_3.core.cache.hub.validate_ready_file",
                    return_value=(True, "ok"),
                ),
                patch(
                    "AnonX_3.core.cache.hub.matches_exact_playback_mode",
                    return_value=True,
                ),
            ):
                hit = cache_hub.lookup_text("cache   first song", video=False)
                miss = cache_hub.lookup_text("cache first song remix", video=False)

            assert hit is not None
            assert hit.media_id == media_id
            assert hit.local_path == str(ready_path)
            assert hit.title == "Cache First Song"
            assert hit.query == "  Cache\tFirst\nSong  "
            assert miss is None
        finally:
            store.close_for_test()



def test_resolve_source_prefers_cache_before_search_or_extraction():
    """A verified cache result must skip all provider and extractor paths."""
    from unittest.mock import AsyncMock, patch

    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Track

    async def verify():
        service = YouTube()
        cached = Track(
            id="AbCdEfGhI12",
            title="Cached Track",
            file_path="cache/cached-track.m4a",
            message_id=901,
            video=False,
            source="youtube_local",
        )
        search = AsyncMock(side_effect=AssertionError("search must not run"))
        deep_search = AsyncMock(
            side_effect=AssertionError("deep search must not run")
        )
        with (
            patch.object(service, "resolve_cached_source", return_value=cached) as lookup,
            patch.object(service, "search", search),
            patch.object(service, "deep_search", deep_search),
            patch.object(
                service,
                "resolve_direct_stream",
                side_effect=AssertionError("yt-dlp extraction must not run"),
            ),
        ):
            resolved, queued, error = await service.resolve_source(
                query="cache first song",
                message_id=901,
                video=False,
            )

        assert resolved is cached
        assert queued == []
        assert error is None
        lookup.assert_called_once_with("cache first song", 901, video=False)
        search.assert_not_awaited()
        deep_search.assert_not_awaited()

    asyncio.run(verify())


def test_resolve_source_classifies_provider_failures_separately():
    """Provider outages must remain retryable; genuine misses stay not-found."""
    from unittest.mock import AsyncMock, patch

    from AnonX_3.core.youtube import YouTube

    async def verify():
        service = YouTube()
        with (
            patch.object(service, "resolve_cached_source", return_value=None),
            patch.object(service, "search", new_callable=AsyncMock, return_value=None),
            patch.object(service, "deep_search", new_callable=AsyncMock, return_value=[]),
            patch.object(
                service,
                "_ytdlp_search_tracks",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with patch(
                "AnonX_3.core.resolver.fallback.find_fallback_track",
                new=AsyncMock(
                    return_value=(None, {"reason": "source_unavailable"})
                ),
            ):
                _, _, error = await service.resolve_source(
                    query="provider outage", message_id=902
                )
            assert error == "play_source_unavailable"

            with patch(
                "AnonX_3.core.resolver.fallback.find_fallback_track",
                new=AsyncMock(return_value=(None, {"reason": "no_match"})),
            ):
                _, _, error = await service.resolve_source(
                    query="genuine miss", message_id=903
                )
            assert error == "play_not_found"

            with patch(
                "AnonX_3.core.resolver.fallback.find_fallback_track",
                new=AsyncMock(side_effect=RuntimeError("transport down")),
            ):
                _, _, error = await service.resolve_source(
                    query="fallback exception", message_id=904
                )
            assert error == "play_source_unavailable"

    asyncio.run(verify())


def test_search_deadline_allows_slow_valid_pyyt_result():
    """A valid provider response must outlive the internal four-second budget."""
    from unittest.mock import patch

    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Track

    async def verify():
        service = YouTube()

        async def slow_provider(*_args, **_kwargs):
            await asyncio.sleep(1.8)
            return [
                Track(
                    id="Sl0wReslt01",
                    channel_name="YouTube",
                    duration="3:00",
                    duration_sec=180,
                    message_id=905,
                    title="Slow but valid result",
                    thumbnail="https://i.ytimg.com/vi/Sl0wReslt01/hqdefault.jpg",
                    url="https://www.youtube.com/watch?v=Sl0wReslt01",
                    view_count="1K",
                    video=False,
                )
            ]

        try:
            with patch.object(service, "_pyyt_search_tracks", slow_provider):
                with patch.object(service, "_live_proxy", return_value=None):
                    with patch.object(service, "_refresh_api_key_if_due", return_value=""):
                        track = await service.search("slow valid result", 905)
            assert track is not None
            assert track.id == "Sl0wReslt01"
        finally:
            await service.close()

    asyncio.run(verify())


def test_resolve_source_uses_ytdlp_search_after_provider_miss():
    """A provider outage must still allow a bounded YouTube metadata fallback."""
    from unittest.mock import AsyncMock, patch

    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Track

    async def verify():
        service = YouTube()
        fallback = Track(
            id="YtDlPReslt1",
            channel_name="YouTube",
            duration="3:00",
            duration_sec=180,
            message_id=906,
            title="Provider fallback result",
            thumbnail="https://i.ytimg.com/vi/YtDlPReslt1/hqdefault.jpg",
            url="https://www.youtube.com/watch?v=YtDlPReslt1",
            view_count="",
            video=False,
        )
        try:
            with (
                patch.object(service, "resolve_cached_source", return_value=None),
                patch.object(service, "search", new_callable=AsyncMock, return_value=None),
                patch.object(service, "deep_search", new_callable=AsyncMock, return_value=[]),
                patch.object(
                    service,
                    "_ytdlp_search_tracks",
                    new_callable=AsyncMock,
                    return_value=[fallback],
                ) as ytdlp_search,
            ):
                resolved, queued, error = await service.resolve_source(
                    query="provider fallback song",
                    message_id=906,
                    video=False,
                )
            assert resolved is not None
            assert resolved.id == fallback.id
            assert queued == []
            assert error is None
            ytdlp_search.assert_awaited_once()
        finally:
            await service.close()

    asyncio.run(verify())


def test_play_and_startup_failure_boundaries():
    """Startup is one process lifecycle, never a same-interpreter retry loop."""
    play_source = (ROOT / "AnonX_3" / "plugins" / "play.py").read_text(
        encoding="utf-8"
    )
    main_source = (ROOT / "AnonX_3" / "__main__.py").read_text(encoding="utf-8")
    assert "PLAY_RESOLVE_TIMEOUT_SEC" in play_source
    assert "play_source_timeout" in play_source
    assert "play_source_unavailable" in play_source
    assert "timeout=12.0" not in play_source

    tree = ast.parse(main_source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert "_run_once" in functions
    assert "main" in functions
    main_node = functions["main"]
    assert any(isinstance(node, ast.Try) and node.finalbody for node in main_node.body)

    run_process = functions.get("_run_process")
    assert run_process is not None
    asyncio_run_calls = [
        node
        for node in ast.walk(run_process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "run"
    ]
    assert len(asyncio_run_calls) == 1
    assert isinstance(asyncio_run_calls[0].args[0], ast.Call)
    assert isinstance(asyncio_run_calls[0].args[0].func, ast.Name)
    assert asyncio_run_calls[0].args[0].func.id == "main"
    assert not any(isinstance(node, ast.While) for node in ast.walk(run_process))
    assert not any(isinstance(node, ast.While) for node in tree.body)

    entrypoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )
    process_calls = [
        node
        for node in ast.walk(entrypoint)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_process"
    ]
    assert len(process_calls) == 1


def test_one_shot_download_publishes_stream_to_concurrent_waiter_once():
    """One cold playback owner exposes its selected URL without a second ytdlp."""
    from unittest.mock import patch

    from AnonX_3.core import youtube as youtube_module
    from AnonX_3.core.provider import po_token as po_token_module
    from AnonX_3.core.youtube import YouTube

    async def verify():
        service = YouTube()
        video_id = "OneShot0001"
        release_download = threading.Event()

        class FakeYoutubeDL:
            calls = 0

            def __init__(self, opts):
                self.opts = dict(opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def download(self, _urls):
                type(self).calls += 1
                hook = self.opts["progress_hooks"][0]
                info = {
                    "id": video_id,
                    "title": "One-shot source",
                    "duration": 61,
                    "url": "https://media.example/one-shot",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                }
                hook({"status": "downloading", "info_dict": info})
                assert release_download.wait(3), "test did not release fake download"
                output = Path("downloads") / f"{video_id}.m4a"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"x" * (128 * 1024))
                hook(
                    {
                        "status": "finished",
                        "filename": str(output),
                        "info_dict": info,
                    }
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                with (
                    patch.object(service, "get_cookies", return_value=None),
                    patch.object(service, "_browser_cookie_spec", return_value=None),
                    patch.object(
                        po_token_module.po_token_provider,
                        "enabled",
                        return_value=False,
                    ),
                    patch.object(
                        youtube_module.resource_manager,
                        "allow_new_heavy_job",
                        return_value=True,
                    ),
                    patch.object(
                        youtube_module.yt_dlp,
                        "YoutubeDL",
                        FakeYoutubeDL,
                    ),
                ):
                    owner = asyncio.create_task(
                        service.download(
                            video_id,
                            stream_for_playback=True,
                            one_shot=True,
                        )
                    )
                    try:
                        remote, local_hint = await asyncio.wait_for(
                            service.await_download_stream_source(
                                video_id,
                                video=False,
                                owner_task=owner,
                            ),
                            timeout=2,
                        )
                        assert remote == "https://media.example/one-shot"
                        assert local_hint.endswith(f"{video_id}.m4a")
                        assert FakeYoutubeDL.calls == 1
                    finally:
                        release_download.set()
                    result = await asyncio.wait_for(owner, timeout=3)
                    assert result is not None
                    assert result.endswith(f"{video_id}.m4a")
                    assert FakeYoutubeDL.calls == 1
            finally:
                os.chdir(old_cwd)

    asyncio.run(verify())


def test_one_shot_download_failure_does_not_retry_ytdlp():
    """The interactive one-shot contract forbids recovery-ladder retries."""
    from unittest.mock import patch

    from AnonX_3 import config
    from AnonX_3.core import youtube as youtube_module
    from AnonX_3.core.youtube import YouTube

    async def verify():
        service = YouTube()
        video_id = "FailOnce001"

        class FailingYoutubeDL:
            calls = 0

            def __init__(self, _opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def download(self, _urls):
                type(self).calls += 1
                raise youtube_module.yt_dlp.utils.DownloadError("synthetic failure")

        with (
            patch.object(config, "YTDLP_MAX_RETRIES", 9),
            patch.object(service, "get_cookies", return_value=None),
            patch.object(service, "_browser_cookie_spec", return_value=None),
            patch.object(
                youtube_module.resource_manager,
                "allow_new_heavy_job",
                return_value=True,
            ),
            patch.object(
                youtube_module.yt_dlp,
                "YoutubeDL",
                FailingYoutubeDL,
            ),
        ):
            result = await service.download(
                video_id,
                stream_for_playback=True,
                one_shot=True,
            )

        assert result is None
        assert FailingYoutubeDL.calls == 1

    asyncio.run(verify())


def test_download_stream_singleflight_keys_partition_audio_and_video():
    from AnonX_3.core.youtube import YouTube

    service = YouTube()
    video_id = "AbCdEfGhI12"
    audio_key = service._download_stream_key(
        video_id, video=False, quality_tier="good"
    )
    video_key = service._download_stream_key(
        video_id, video=True, quality_tier="good"
    )
    assert audio_key != video_key
    assert audio_key == (video_id, False, None)
    assert video_key == (video_id, True, None)


def test_sudo_filter_and_startup_order():
    from types import SimpleNamespace

    from AnonX_3 import app, db

    persisted_user_id = 9_876_543_210
    denied_user_id = 9_876_543_211
    original_get_sudoers = db.get_sudoers
    original_get_owners = db.get_owners

    async def fake_get_sudoers():
        return [persisted_user_id]

    async def fake_get_owners():
        return []

    async def run():
        app._sudo_ids.discard(persisted_user_id)
        app._sudo_ids.discard(denied_user_id)
        db.get_sudoers = fake_get_sudoers
        db.get_owners = fake_get_owners
        try:
            persisted_message = SimpleNamespace(
                from_user=SimpleNamespace(id=persisted_user_id)
            )
            denied_message = SimpleNamespace(
                from_user=SimpleNamespace(id=denied_user_id)
            )
            assert await app.sudoers(app, persisted_message)
            assert persisted_user_id in app._sudo_ids
            assert not await app.sudoers(app, denied_message)
        finally:
            db.get_sudoers = original_get_sudoers
            db.get_owners = original_get_owners
            app._sudo_ids.discard(persisted_user_id)
            app._sudo_ids.discard(denied_user_id)

    asyncio.run(run())

    main_source = (ROOT / "AnonX_3" / "__main__.py").read_text(encoding="utf-8")
    auth_load = main_source.index("sudoers = await db.get_sudoers()")
    plugin_load = main_source.index("for module in all_modules:")
    bot_start = main_source.index("await app.boot()")
    assistant_start = main_source.index("await userbot.boot()")
    assert auth_load < bot_start
    assert plugin_load < bot_start
    assert bot_start < assistant_start


def test_assistant_startup_race_waits_for_readiness():
    from AnonX_3 import anon, db

    chat_id = -1009876543210
    ready_client = object()
    original_clients = anon.clients
    original_ready = anon._ready
    original_assignment = db.assistant.get(chat_id)

    async def publish_ready_client():
        await asyncio.sleep(0)
        anon.clients = [ready_client]
        anon._ready.set()

    async def run():
        anon.clients = []
        anon._ready = asyncio.Event()
        db.assistant[chat_id] = 1
        publisher = asyncio.create_task(publish_ready_client())
        try:
            assert await db.get_assistant(chat_id) is ready_client
            await publisher
        finally:
            if not publisher.done():
                publisher.cancel()
            anon.clients = original_clients
            anon._ready = original_ready
            if original_assignment is None:
                db.assistant.pop(chat_id, None)
            else:
                db.assistant[chat_id] = original_assignment

    asyncio.run(run())

    mongo_source = (ROOT / "AnonX_3" / "core" / "mongo.py").read_text(
        encoding="utf-8"
    )
    assistant_methods = mongo_source[mongo_source.index("# ASSISTANT METHODS") :]
    assert "raise SystemExit" not in assistant_methods


def test_log_regression_guards():
    youtube_source = (ROOT / "AnonX_3" / "core" / "youtube.py").read_text(
        encoding="utf-8"
    )
    misc_source = (ROOT / "AnonX_3" / "plugins" / "misc.py").read_text(
        encoding="utf-8"
    )
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    mongo_source = (ROOT / "AnonX_3" / "core" / "mongo.py").read_text(
        encoding="utf-8"
    )
    main_source = (ROOT / "AnonX_3" / "__main__.py").read_text(encoding="utf-8")
    restart_source = (ROOT / "AnonX_3" / "plugins" / "restart.py").read_text(
        encoding="utf-8"
    )
    filter_source = (ROOT / "AnonX_3" / "plugins" / "filter.py").read_text(
        encoding="utf-8"
    )
    plugins_source = (ROOT / "AnonX_3" / "plugins" / "__init__.py").read_text(
        encoding="utf-8"
    )

    # py-yt-search builds may advertise **kwargs yet still reject proxy= at
    # runtime.  Preserve the bounded retry that removes only the proxy option.
    proxy_add = youtube_source.index('search_kwargs["proxy"] = proxy')
    first_search = youtube_source.index(
        "searcher = VideosSearch(query, **search_kwargs)",
        proxy_add,
    )
    type_error = youtube_source.index("except TypeError:", first_search)
    proxy_guard = youtube_source.index(
        'if "proxy" in search_kwargs:',
        type_error,
    )
    proxy_drop = youtube_source.index(
        'search_kwargs.pop("proxy", None)',
        proxy_guard,
    )
    direct_retry = youtube_source.index(
        "searcher = VideosSearch(query, **search_kwargs)",
        proxy_drop,
    )
    assert proxy_add < first_search < type_error
    assert type_error < proxy_guard < proxy_drop < direct_retry

    # Import-time background tasks must not touch Telegram before both clients boot.
    barrier = misc_source.index(
        'while not getattr(app, "is_connected", False) or not userbot.clients:'
    )
    sync_read = misc_source.index("await db.get_users()", barrier)
    assert barrier < sync_read

    # A CDN promotion may move the old downloads/ path before a waiter settles.
    not_ready = youtube_source.index("if settled is None:")
    relocated = youtube_source.index("settled = self._local_ready_path(", not_ready)
    warning = youtube_source.index(
        '"Download finished but file not ready video_id=%s path=%s"', relocated
    )
    assert not_ready < relocated < warning

    # The optional API must be opt-in consistently at config and startup call sites.
    assert '_bool_env("DOWNLOADER_API_ENABLED", False)' in config_source
    assert 'getattr(config, "DOWNLOADER_API_ENABLED", False)' in main_source

    # Command dispatch must be deterministic, and the broad text observer must
    # not consume /logs before its sudo/owner handler acknowledges the request.
    assert "all_modules = tuple(sorted(_list_modules()))" in plugins_source
    assert "frozenset(sorted(_list_modules()))" not in plugins_source
    assert 'filters.command(["logs"]), group=-1' in restart_source
    assert 'filters.command(["logs"]) & app.sudoers' not in restart_source
    assert "group=25," in filter_source
    assert "if not m.from_user:" in restart_source
    assert "app._sudo_ids.add(uid)" in restart_source


def test_vc_auto_unmute_guards():
    calls_source = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(
        encoding="utf-8"
    )

    # The event-driven hook runs only after client.play() has joined the VC and
    # stays outside the critical first-play path.
    play_call = calls_source.index("await client.play(")
    schedule = calls_source.index(
        "self._schedule_assistant_unmute(client, chat_id)", play_call
    )
    assert play_call < schedule
    assert "asyncio.sleep(" not in calls_source[
        calls_source.index("async def _ensure_assistant_unmuted"):
        calls_source.index("def _schedule_assistant_unmute")
    ]

    # Self-unmute is preferred; the admin fallback is restricted to the active
    # assistant peer and never edits another VC participant.
    self_unmute = calls_source.index("participant=raw.types.InputPeerSelf()")
    assistant_peer = calls_source.index(
        "participant = await app.resolve_peer(assistant_id)", self_unmute
    )
    admin_unmute = calls_source.index(
        "participant=participant,", assistant_peer
    )
    assert self_unmute < assistant_peer < admin_unmute
    # Required-unmute overlap adds one self-only attempt; the normal self and
    # admin fallback remain the other two bounded call sites.
    assert calls_source.count(
        "raw.functions.phone.EditGroupCallParticipant("
    ) == 3
    assert "async def _overlap_required_unmute(" in calls_source
    assert 'unmute_mode: str = "background"' in calls_source
    assert 'if unmute_mode == "required":' in calls_source
    assert 'stream=None,' in calls_source
    assert 'unmute_mode="required",' in calls_source
    assert "propagate_floodwait=True" in calls_source
    assert "raise AssistantUnmuteError(" in calls_source


def test_required_unmute_rolls_back_empty_call():
    from types import SimpleNamespace

    calls_path = ROOT / "AnonX_3" / "core" / "calls.py"
    source = calls_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(calls_path))
    tgcall = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TgCall"
    )
    method = next(
        node
        for node in tgcall.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_play_with_startup_slot"
    )
    harness = ast.ClassDef(
        name="UnmuteHarness",
        bases=[],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    module = ast.Module(body=[harness], type_ignores=[])
    ast.fix_missing_locations(module)

    class AssistantUnmuteError(RuntimeError):
        pass

    class StreamCapacityError(RuntimeError):
        pass

    class Slot:
        admitted = True

        def __init__(self):
            self.released = 0

        def release(self):
            self.released += 1

    slot = Slot()
    resource_manager = SimpleNamespace(reserve_stream=lambda _chat_id: slot)
    namespace = {
        "asyncio": asyncio,
        "types": SimpleNamespace(
            GroupCallConfig=lambda **kwargs: SimpleNamespace(**kwargs)
        ),
        "resource_manager": resource_manager,
        "AssistantUnmuteError": AssistantUnmuteError,
        "StreamCapacityError": StreamCapacityError,
        "logger": SimpleNamespace(debug=lambda *_args, **_kwargs: None),
    }
    exec(compile(module, str(calls_path), "exec"), namespace)

    async def _run():
        service = namespace["UnmuteHarness"]()
        service._startup_semaphore = asyncio.Semaphore(1)
        service._ensure_assistant_unmuted = lambda *_args, **_kwargs: _false()
        played = []
        left = []

        async def play(**kwargs):
            played.append(kwargs)

        async def leave_call(*args, **kwargs):
            left.append((args, kwargs))

        client = SimpleNamespace(play=play, leave_call=leave_call)
        try:
            await service._play_with_startup_slot(
                client,
                chat_id=77,
                stream=None,
                unmute_mode="required",
            )
        except AssistantUnmuteError:
            pass
        else:
            raise AssertionError("required unmute failure did not abort")
        assert len(played) == 1
        assert played[0]["stream"] is None
        assert left == [((77,), {"close": False})]
        assert slot.released == 1

    async def _false():
        return False

    asyncio.run(_run())


def test_parallel_initial_youtube_start_guards():
    calls_source = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(
        encoding="utf-8"
    )
    play_source = (ROOT / "AnonX_3" / "helpers" / "_play.py").read_text(
        encoding="utf-8"
    )
    orchestrator_source = (
        ROOT / "AnonX_3" / "core" / "playback_orchestrator.py"
    ).read_text(encoding="utf-8")

    assert "initial_start: bool = False" in calls_source
    assert "initial_start=initial_start," in play_source
    gate_start = calls_source.index("initial_parallel_direct = bool(")
    gate_source = calls_source[gate_start : calls_source.index(")", gate_start)]
    assert "initial_start" in gate_source
    assert "direct_first_youtube" in gate_source
    # One ShellError disables the raw transport process-wide without disabling
    # the independent direct-first/initial-start gate itself.
    raw_gate_start = calls_source.index("use_raw_cold_path = bool(")
    raw_gate_source = calls_source[
        raw_gate_start : calls_source.index("\n        )", raw_gate_start)
    ]
    assert "not self._raw_direct_disabled_reason" in raw_gate_source
    assert 'self._raw_direct_disabled_reason: str = ""' in calls_source
    assert "self.stream_profile.cached_or_default(chat_id)" in calls_source
    # Initial direct startup is late-join: resolve/build the real stream before
    # entering VC; no empty prejoin or parallel join/resolver transaction.
    assert "await await_parallel_ready(" not in calls_source
    assert 'trace.set_meta(mode="new-direct-late-join")' in calls_source
    assert "begin_initial_direct_preconnect" in calls_source
    assert "direct_command_preconnect_adopted" in calls_source
    assert '"required" if initial_parallel_direct else "background"' in calls_source
    assert "validate_direct_url(remote_url)" in calls_source
    assert "self._build_initial_direct_raw_stream(" in calls_source
    assert "types.raw.Stream(" in calls_source
    assert "startup_gate.mark_direct_dispatched(chat_id)" in calls_source
    assert "self._monitor_initial_direct_play(" in calls_source
    assert "async def _recover_initial_direct_with_mediastream(" in calls_source
    assert "youtube_raw_direct_startup_failed" in calls_source
    assert "youtube_direct_mediastream_recovery_started" in calls_source
    assert "raw_shell_recovery" in calls_source
    assert "self._observe_initial_direct_media(" in calls_source
    assert "self._schedule_direct_startup_proof(chat_id, media)" in calls_source
    assert "self._schedule_direct_post_start_background(" in calls_source
    assert "async def await_parallel_ready(" in orchestrator_source
    assert "async def monitor_direct_proof(" in orchestrator_source
    assert "def mark_direct_dispatched(" in orchestrator_source
    assert "def mark_direct_attached(" in orchestrator_source
    assert "def mark_direct_start_failed(" in orchestrator_source

    hot_comment = calls_source.index("# Direct source is ready; VC is intentionally still")
    initial_start = calls_source.rindex("if initial_parallel_direct:", 0, hot_comment)
    initial_return = calls_source.index("                        return", hot_comment)
    initial_hot_path = calls_source[initial_start:initial_return]
    assert "await probe_direct_url(" not in initial_hot_path
    assert "await self._probe_direct_audio_open(" not in initial_hot_path
    assert "await self._start_youtube_direct_background_cache(" not in initial_hot_path
    assert "await update_now_playing(" not in initial_hot_path
    assert "self._schedule_direct_post_start_background(" not in initial_hot_path
    assert initial_hot_path.index("asyncio.create_task(") < initial_hot_path.index(
        "await db.add_call(chat_id)"
    )

    cold_args_start = calls_source.index("def _cold_direct_input_args(")
    cold_args_end = calls_source.index("def _direct_event_path(", cold_args_start)
    cold_args_source = calls_source[cold_args_start:cold_args_end]
    assert '"-fflags",' in cold_args_source
    assert '"nobuffer",' in cold_args_source
    assert '"-analyzeduration",' in cold_args_source
    assert '"-probesize",' in cold_args_source
    assert "if value in retry_options:" in cold_args_source
    for retry_option in (
        "-reconnect",
        "-reconnect_at_eof",
        "-reconnect_streamed",
        "-reconnect_delay_max",
    ):
        assert f'"{retry_option}"' in cold_args_source

    observer_start = calls_source.index("async def _observe_initial_direct_media(")
    observer_end = calls_source.index(
        "async def _monitor_initial_direct_play(", observer_start
    )
    observer_source = calls_source[observer_start:observer_end]
    packet_index = observer_source.index('"first_telegram_audio_packet_sent"')
    background_index = observer_source.index(
        "self._schedule_direct_post_start_background("
    )
    assert packet_index < background_index
    assert 'play_state.get("observer_done")' in observer_source
    for event in (
        "ffmpeg_spawned",
        "raw_url_first_bytes",
        "first_decoded_audio_frame",
    ):
        assert event in observer_source


def test_ffmpeg_observer_emits_cold_start_milestones():
    helper = ROOT / "AnonX_3" / "core" / "ffmpeg_observer.py"
    with tempfile.TemporaryDirectory() as temp_dir:
        event_file = Path(temp_dir) / "events.jsonl"
        child = (
            "import sys; "
            "sys.stderr.write(\"header='HTTP/1.1 206 Partial Content'\\n\"); "
            "sys.stderr.flush(); "
            "sys.stdout.buffer.write(b'abcdefgh'); "
            "sys.stdout.buffer.flush()"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--event-file",
                str(event_file),
                "--chat-id",
                "77",
                "--media-id",
                "cold-start-test",
                "--frame-bytes",
                "4",
                "--keep-event-file",
                "--",
                sys.executable,
                "-c",
                child,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert result.stdout == b"abcdefgh"
        events = [
            json.loads(line)
            for line in event_file.read_text(encoding="utf-8").splitlines()
        ]
        names = [event["event"] for event in events]
        assert names[0] == "ffmpeg_spawned"
        assert names.index("raw_url_first_bytes") < names.index(
            "first_decoded_audio_frame"
        )
        assert names[-1] == "ffmpeg_exited"
        for event in events:
            assert event["chat_id"] == 77
            assert event["media_id"] == "cold-start-test"
            assert event["wall"]
            assert isinstance(event["wall_time_ns"], int)
            assert isinstance(event["monotonic_ns"], int)


def test_callback_feedback_uses_non_modal_banners():
    package_root = ROOT / "AnonX_3"
    callbacks_source = (
        package_root / "plugins" / "callbacks.py"
    ).read_text(encoding="utf-8")
    modal_alerts = []
    banner_answers = 0

    for source_path in package_root.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if re.search(r"show_alert\s*=\s*True", source):
            modal_alerts.append(str(source_path.relative_to(ROOT)))
        banner_answers += len(re.findall(r"show_alert\s*=\s*False", source))

    assert not modal_alerts, f"Modal callback alerts remain: {modal_alerts}"
    assert banner_answers >= 17
    assert 'query.lang["processing"]' not in callbacks_source
    for status_key in ("paused", "playing", "skipped", "replayed", "stopped"):
        assert (
            f'query.answer(query.lang["{status_key}"], show_alert=False)'
            in callbacks_source
        )
    assert "f\"{query.lang['cmd_delete']}: {'ON' if _delete else 'OFF'}\"" in callbacks_source
    assert "f\"{query.lang['play_mode']}: {'ON' if _admin else 'OFF'}\"" in callbacks_source
    toggle = callbacks_source.index("_admin = not _admin")
    persisted = callbacks_source.index("await db.set_play_mode(chat_id, _admin)", toggle)
    assert toggle < persisted


def test_all_sudo_commands_use_early_dispatch_group():
    plugin_dir = ROOT / "AnonX_3" / "plugins"
    protected_handlers = 0

    for plugin_path in plugin_dir.glob("*.py"):
        source = plugin_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(plugin_path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                if not (
                    isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "on_message"
                ):
                    continue

                filter_source = ast.get_source_segment(source, decorator.args[0]) or ""
                if "app.sudoers" not in filter_source and "app.owners" not in filter_source:
                    continue

                protected_handlers += 1
                group_keyword = next(
                    (item for item in decorator.keywords if item.arg == "group"),
                    None,
                )
                assert group_keyword is not None, (
                    f"{plugin_path.name}:{node.lineno} protected command has no "
                    "dedicated early dispatch group"
                )
                assert ast.literal_eval(group_keyword.value) == -1, (
                    f"{plugin_path.name}:{node.lineno} protected command must use "
                    "group=-1"
                )

    assert protected_handlers >= 20

    restart_source = (plugin_dir / "restart.py").read_text(encoding="utf-8")
    sudoers_source = (plugin_dir / "sudoers.py").read_text(encoding="utf-8")
    assert 'filters.command(["logs"]), group=-1' in restart_source
    assert 'filters.command(["musiclog"])' in restart_source
    assert 'filters.command(["listsudo", "sudolist"])' in sudoers_source
    assert "& filters.chat(app.logger)" in sudoers_source
    assert "& app.sudoers" in sudoers_source


def test_simple_delete_filter_guards():
    filter_source = (ROOT / "AnonX_3" / "plugins" / "filter.py").read_text(
        encoding="utf-8"
    )
    restart_source = (ROOT / "AnonX_3" / "plugins" / "restart.py").read_text(
        encoding="utf-8"
    )

    # One direct keyword command replaces the old keyword->reply-text UX while
    # existing stored dictionary keys remain readable.
    assert 'filters.command(["filter", "filtter"])' in filter_source
    assert 'keyword_args = args[1:] if sub == "add" else args' in filter_source
    assert "for keyword in raw_data" in filter_source
    assert "data[kw] = True" in filter_source
    assert "/filter add keyword reply text" not in filter_source
    assert "/filter settext keyword new text" not in filter_source

    # Matching user messages are deleted first; the one global warning comes
    # from /settext and is itself temporary.
    delete_index = filter_source.index("await m.delete()")
    warning_index = filter_source.index(
        'await db.get_custom_text_for_chat(',
        delete_index,
    )
    assert delete_index < warning_index
    assert '"filter_warning"' in filter_source
    assert "await utils.send_formatted(" in filter_source
    assert "_schedule_delete(sent, delay=5.0)" in filter_source

    # `/settext filter` is a supported alias and can be reset to the default.
    assert '"filter": "filter_warning"' in restart_source
    assert '"filter_warning": {' in restart_source
    assert '"filter_warning",' in restart_source


def test_filter_strike_moderation_guards():
    from AnonX_3.helpers import buttons

    filter_source = (ROOT / "AnonX_3" / "plugins" / "filter.py").read_text(
        encoding="utf-8"
    )
    restart_source = (ROOT / "AnonX_3" / "plugins" / "restart.py").read_text(
        encoding="utf-8"
    )

    mute_markup = buttons.filter_moderation(123, -100456, muted=False)
    mute_button = mute_markup["inline_keyboard"][0][0]
    assert mute_button["text"] == "Mute"
    assert mute_button["style"] == "danger"
    assert mute_button["callback_data"] == "filtermod mute 123 -100456"

    unmute_markup = buttons.filter_moderation(123, -100456, muted=True)
    unmute_button = unmute_markup["inline_keyboard"][0][0]
    assert unmute_button["text"] == "Unmute"
    assert unmute_button["style"] == "success"
    assert unmute_button["callback_data"] == "filtermod unmute 123 -100456"

    assert "FILTER_STRIKE_LIMIT = 3" in filter_source
    assert 'f"filter_strikes_{chat_id}"' in filter_source
    assert "count = min(_FILTER_STRIKES[key] + 1, FILTER_STRIKE_LIMIT)" in filter_source
    assert "if strike >= FILTER_STRIKE_LIMIT:" in filter_source
    assert "await _set_muted(chat_id, m.from_user.id, True)" in filter_source
    assert "if muted and await _check_permission(chat_id, user_id):" in filter_source
    assert 'filters.regex(r"^filtermod (mute|unmute) \\d+ -?\\d+$")' in filter_source
    assert "not await _check_permission(chat_id, actor_id)" in filter_source
    assert "message_chat_id != chat_id" in filter_source
    assert "await _reset_strikes(chat_id, user_id)" in filter_source

    for placeholder in ("<code>{{0}}</code>", "<code>{{1}}</code>", "<code>{{2}}</code>", "<code>{{3}}</code>"):
        assert placeholder in restart_source


def test_fast_first_play_guards():
    youtube_source = (ROOT / "AnonX_3" / "core" / "youtube.py").read_text(
        encoding="utf-8"
    )
    play_source = (ROOT / "AnonX_3" / "helpers" / "_play.py").read_text(
        encoding="utf-8"
    )
    plugin_play_source = (ROOT / "AnonX_3" / "plugins" / "play.py").read_text(
        encoding="utf-8"
    )
    calls_source = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(
        encoding="utf-8"
    )
    prefetch_source = (ROOT / "AnonX_3" / "core" / "prefetch.py").read_text(
        encoding="utf-8"
    )
    orchestrator_source = (
        ROOT / "AnonX_3" / "core" / "playback_orchestrator.py"
    ).read_text(encoding="utf-8")
    playback_source = (ROOT / "AnonX_3" / "core" / "playback.py").read_text(
        encoding="utf-8"
    )
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")

    # Audio warm-up and actual play must share one extraction and select the
    # call-compatible M4A/AAC ladder instead of broad WebM/Opus bestaudio.
    assert "if video else None" in youtube_source
    assert 'fast_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"' in youtube_source
    assert "socket_timeout=4" in youtube_source

    # Search stays provider-only; yt-dlp is reserved for the chosen media's
    # single playback acquisition.
    assert "race_deadline  = max(pyyt_deadline, api_deadline)" in youtube_source
    assert "allow_ytdlp=False" in youtube_source
    assert "def _ytdlp_fast_search" not in play_source

    # Warm search hands the selected media into playback and starts the direct
    # singleflight before the warm-search task returns. The local cache worker
    # still stays behind successful direct playback.
    assert "_prefers_direct_start(result)" not in play_source
    assert "YouTube direct-first warm search: direct resolver prewarmed" in play_source
    assert play_source.index("YouTube direct-first warm search") < play_source.index(
        'name=f"warm-local-after-search:{m.id}"'
    )
    assert "result = await yt.search(warm_query, m.id, video=video)" in play_source
    assert "yt.warm_direct_stream_source(" in play_source
    assert "asyncio.wait_for(\n                        asyncio.shield(warm_task), timeout=4.0" in play_source
    assert 'setattr(m, "_warm_search_media", warmed_search)' in play_source
    assert 'getattr(m, "_warm_search_media", None)' in plugin_play_source
    assert 'elif warmed_search is not None:' in plugin_play_source
    assert play_source.index('setattr(m, "_warm_search_media", warmed_search)') < play_source.index(
        "return await play("
    )
    assert "chat_id in db.active_calls" in play_source
    assert "or queue.get_current(chat_id) is not None" in play_source

    # Playback dispatch is strict YouTube direct-first: raw direct starts before
    # any foreground local admission, then a silent one-owner background cache
    # fill makes the next request a local/cache hit when complete.
    assert "not _prefers_direct_start(media)" in play_source
    assert "utils.play_log(log_msg" in play_source
    assert "asyncio.create_task(" in play_source
    assert calls_source.count("await self._await_parallel_local(") == 2
    assert "After YouTube direct miss/fail: start/join the deferred local fallback." in calls_source
    assert "direct failover: starting deferred local fallback" in calls_source
    assert "prefer_remote=True" in calls_source
    assert "resolve_direct_stream_source" in calls_source
    assert "_probe_direct_audio_open" in calls_source
    assert "_build_direct_media_stream" in calls_source
    assert "headers" in calls_source
    assert "ffmpeg_parameters" in calls_source
    assert "_no_audio_source_error" in calls_source
    assert "NoAudioSourceFound" in calls_source
    assert "direct_audio_source_created=True" in calls_source
    assert 'direct_player_start="ok"' in calls_source
    assert "pytgcalls_stream_started=True" in calls_source
    assert "telegram_audio_packets_sending=True" in calls_source
    assert "vc_audio_audible_gate_ok=True" in calls_source
    assert "local_download_started=False" in calls_source
    assert 'playback_source="raw_direct"' in calls_source
    assert "await self.prefetch_manager.await_current_stream_source(" not in calls_source
    assert "Parallel local download started" not in calls_source
    assert (
        "YouTube direct-first active; foreground cache/local admission "
        in calls_source
    )
    assert "await self._start_youtube_direct_background_cache(" in calls_source
    assert "youtube_direct_background_cache_started" in calls_source
    assert "youtube_direct_background_cache_ready" in calls_source
    assert "duplicate_guard=current_cache" in calls_source
    assert 'setattr(owner, "_direct_background_cache", True)' in calls_source
    assert "progress_message=download_progress_message" in calls_source
    assert "local_path=None" in calls_source
    assert "_now_playing_thumb_task" in calls_source
    assert "_now_playing_thumb_task" in playback_source

    # Readiness comes from real tasks/events, not hardcoded startup seconds.
    assert "PLAY_STARTUP_GATE_SEC" not in config_source
    assert "PLAY_JOIN_TIMEOUT_SEC" not in config_source
    assert "DIRECT_START_PROOF_SEC" in config_source
    assert "await asyncio.shield(task)" in prefetch_source
    assert "await_local_ready" not in calls_source
    assert "await_local_ready" not in orchestrator_source
    assert "session.fatal_event.wait()" in orchestrator_source
    assert "timeout=proof_sec" in orchestrator_source
    assert "client.play() completion is the join-ready event" not in calls_source
    assert "direct succeeds only when ``client.play()``" in orchestrator_source
    assert "DirectStreamSource" in youtube_source
    assert "_direct_stream_headers" in youtube_source
    assert "_is_direct_media_url" in youtube_source


def test_local_cookie_agent_guards():
    from unittest.mock import patch

    from AnonX_3 import config as app_config
    from AnonX_3.core.youtube import YouTube

    youtube_source = (ROOT / "AnonX_3" / "core" / "youtube.py").read_text(
        encoding="utf-8"
    )
    main_source = (ROOT / "AnonX_3" / "__main__.py").read_text(encoding="utf-8")
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    docker_source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose_source = (ROOT / "docker-compose.example.yml").read_text(
        encoding="utf-8"
    )
    dockerignore_source = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    sample_env_source = (ROOT / "sample.env").read_text(encoding="utf-8")
    cookie_plugin_source = (
        ROOT / "AnonX_3" / "plugins" / "cookies.py"
    ).read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as temp_dir:
        cookie_path = Path(temp_dir) / "cookies.txt"
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\ttest-value\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\t"
            "__Secure-3PSID\ttest-http-only-value\n",
            encoding="utf-8",
        )
        health = YouTube._cookie_file_health(str(cookie_path))
        assert health["valid"] is True
        assert health["usable"] == 2
        assert health["authenticated"] == 2

        empty_cookie_path = Path(temp_dir) / "empty-cookies.txt"
        empty_cookie_path.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\t\n",
            encoding="utf-8",
        )
        empty_health = YouTube._cookie_file_health(str(empty_cookie_path))
        assert empty_health["valid"] is False
        assert empty_health["usable"] == 0
        assert empty_health["authenticated"] == 0

        cookie_free_root = Path(temp_dir) / "must-not-be-created"
        original_cookie_free = app_config.COOKIE_FREE_MODE
        try:
            app_config.COOKIE_FREE_MODE = True
            agent = YouTube()
            agent.cookie_dir = str(cookie_free_root)
            assert agent.get_cookies() is None
            assert agent._browser_cookie_spec() is None
            assert asyncio.run(
                agent.refresh_local_cookies(reason="cookie-free-test")
            ) is None
            asyncio.run(agent.save_cookies(["https://example.invalid/cookies"]))
            assert not cookie_free_root.exists()
        finally:
            app_config.COOKIE_FREE_MODE = original_cookie_free

        profile_root = Path(temp_dir) / "firefox-profile"
        profile_root.mkdir()
        recovery_root = Path(temp_dir) / "auth-recovery"
        export_calls = {"count": 0}

        def export_signed_in_cookie(*, auth_recovery=False):
            assert auth_recovery is True
            export_calls["count"] += 1
            recovery_root.mkdir(exist_ok=True)
            (recovery_root / "cookies.txt").write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\ttest-value\n",
                encoding="utf-8",
            )
            return True

        async def verify_trigger_only_singleflight():
            agent = YouTube()
            agent.cookie_dir = str(recovery_root)
            agent.cookie_txt_name = "cookies.txt"
            agent._try_export_browser_cookies = export_signed_in_cookie
            # Normal requests remain strictly cookie-free.
            assert agent._browser_cookie_spec() is None
            assert agent._browser_cookie_spec(auth_recovery=True) == (
                "firefox",
                str(profile_root),
                None,
                None,
            )
            first, second = await asyncio.gather(
                agent.refresh_local_cookies(
                    force=True,
                    reason="auth-test-a",
                    auth_recovery=True,
                ),
                agent.refresh_local_cookies(
                    force=True,
                    reason="auth-test-b",
                    auth_recovery=True,
                ),
            )
            assert first is not None and second is not None
            assert Path(first) == Path(second) == recovery_root / "cookies.txt"
            assert export_calls["count"] == 1

        with (
            patch.object(app_config, "COOKIE_FREE_MODE", True),
            patch.object(app_config, "COOKIE_AUTH_RECOVERY_ENABLED", True),
            patch.object(app_config, "AUTO_COOKIE_ENABLED", True),
            patch.object(app_config, "COOKIE_BROWSER", "firefox"),
            patch.object(
                app_config,
                "COOKIE_BROWSER_PROFILE",
                str(profile_root),
            ),
        ):
            assert YouTube.auth_cookie_recovery_enabled() is True
            with patch.object(app_config, "COOKIE_BROWSER_PROFILE", ""):
                assert YouTube.auth_cookie_recovery_enabled() is False
            asyncio.run(verify_trigger_only_singleflight())

    assert "BROWSER_CANDIDATES" in youtube_source
    assert "def _browser_cookie_spec(self, *, auth_recovery: bool = False):" in youtube_source
    assert 'base_opts["cookiesfrombrowser"] = browser_spec' not in youtube_source
    assert 'opts["cookiesfrombrowser"] = browser_spec' not in youtube_source
    assert "build_ytdlp_api_opts(" in youtube_source
    assert "build_ytdlp_cli_args(" in youtube_source
    assert 'opts["js_runtimes"] = js_runtimes' not in youtube_source
    assert 'opts["js_runtimes"] = getattr' not in youtube_source
    assert "(self.cookie_browser, None, None)" not in youtube_source
    assert "_warm_browser_cookie_session(auth_recovery=auth_recovery)" in youtube_source
    assert '"https://www.youtube.com/feed/subscriptions"' not in youtube_source
    assert "--disable-background-networking" not in youtube_source
    assert "stdout=subprocess.DEVNULL" in youtube_source
    assert "stderr=subprocess.DEVNULL" in youtube_source
    assert "ydl.cookiejar.save(" not in youtube_source
    assert "sync_callback=_sync_browser_cookie_profile" in main_source
    assert "auth_markers=%s" in youtube_source
    assert "ydl.extract_info(\"https://www.youtube.com\"" not in youtube_source
    assert "os.replace(next_path, txt_path)" not in youtube_source
    assert "os.chmod(cookie_path, 0o600)" in youtube_source
    assert "async def refresh_local_cookies(" in youtube_source
    assert "await self.refresh_local_cookies(" not in youtube_source
    assert "youtube_runtime_args_ready=True youtube_path=exact_cli" in youtube_source
    assert "youtube_authenticated_runtime_failed=True action=direct" in youtube_source
    assert 'await yt.refresh_local_cookies(reason="startup")' in main_source
    assert 'getattr(config, "COOKIE_REFRESH_SEC", 21600)' in main_source
    assert '_bool_env("AUTO_COOKIE_ENABLED", True)' in config_source
    assert '_bool_env("COOKIE_FREE_MODE", False)' in config_source
    assert '_bool_env(\n            "COOKIE_AUTH_RECOVERY_ENABLED", True' in config_source
    assert '"COOKIE_BROWSER_WARMUP", True' in config_source
    assert '"COOKIE_BROWSER_TIMEOUT_SEC", 30' in config_source
    assert "ARG WITH_CHROMIUM=0" in docker_source
    assert "ARG WITH_FIREFOX=1" in docker_source
    assert "apt-get install -y --no-install-recommends firefox-esr" in docker_source
    assert "/root/firefox-profile" in compose_source
    assert "COOKIE_BROWSER_PROFILE: /root/firefox-profile" in compose_source
    assert "**/cookies/*.txt" in dockerignore_source
    assert "firefox-profile/" in dockerignore_source
    assert "COOKIE_BROWSER=firefox" in sample_env_source
    assert "COOKIE_BROWSER_PROFILE=/root/firefox-profile" in sample_env_source
    assert "YOUTUBE_COOKIE_FILE=/root/youtube-cookies.txt" in sample_env_source
    assert "YTDLP_JS_RUNTIME=deno:/usr/local/bin/deno" in sample_env_source
    assert "if self.cookie_free_mode():" in youtube_source
    assert "Cookie-free mode ignored COOKIES_URL input" in youtube_source
    assert 'logger.debug("po_token inject skipped (%s): %s", action, ex)' in youtube_source
    assert youtube_source.count(
        "await po_token_provider.apply_to_ydl_opts("
    ) == 1
    assert (
        "Cookie-free mode active: browser sessions and cookie files are disabled"
        in main_source
    )
    assert (
        'supervisor.spawn("periodic_cookie_refresh", _periodic_cookie_refresh)'
        in main_source
    )
    assert main_source.index(
        'if getattr(config, "COOKIE_FREE_MODE", True):'
    ) < main_source.index(
        'supervisor.spawn("periodic_cookie_refresh", _periodic_cookie_refresh)'
    )
    assert cookie_plugin_source.count("if yt.cookie_free_mode():") == 2
    assert "Cookie uploads are disabled" in cookie_plugin_source
    assert "youtube_authenticated_runtime_failed=True " in youtube_source
    assert '"action=download video_id=%s class=%s msg=%s"' in youtube_source
    assert "auth_recovery=True" not in youtube_source
    assert '"/best[acodec!=none]"' in youtube_source
    assert "if last_classified.cls == ErrorClass.FORMAT:" in youtube_source
    assert not (ROOT / "AnonX_3" / "plugins" / "api_access.py").exists()
    assert not (
        ROOT / "AnonX_3" / "downloader_api" / "security" / "key_store.py"
    ).exists()


def test_added_to_queue_card_guards():
    package_root = ROOT / "AnonX_3"
    play_source = (package_root / "helpers" / "_play.py").read_text(
        encoding="utf-8"
    )
    inline_source = (package_root / "helpers" / "_inline.py").read_text(
        encoding="utf-8"
    )
    restart_source = (package_root / "plugins" / "restart.py").read_text(
        encoding="utf-8"
    )

    # A queued request edits the live search status into the result card, uses
    # the real queue position, and offers a non-destructive green Close action.
    assert "position = queue.add(chat_id, media)" in play_source
    assert "format_play_queued_template(" in play_source
    assert play_source.count(
        'reply_markup=buttons.play_queued(chat_id, _lang["close"])'
    ) == 2
    assert "def play_queued(self, chat_id: int, _text: str)" in inline_source
    assert 'key="play_queued_close"' in inline_source
    assert 'callback_data=f"controls close {chat_id}"' in inline_source
    assert 'self._style("play_queued_close", "success")' in inline_source
    assert "play_queued_force" not in inline_source
    assert '"play_queued_close": message.lang.get("close", "Close")' in restart_source
    queue_branch = play_source.index("if position != 0:")
    # The inactive-start transaction may now return through an exact rollback
    # before the healthy queued-card path. Require the prefetch in that healthy
    # path rather than assuming its first return is the card return.
    queue_prefetch = play_source.index("_schedule_queued_prefetch(chat_id)", queue_branch)
    assert queue_prefetch > queue_branch
    assert '"prefetch-queued-next:{chat_id}"' in play_source

    # Warm-search UI must not overwrite an active request's queued card with a
    # stale Downloading state. The main handler owns the queue admission/card.
    active_gate = play_source.index(
        "if not force and (",
        play_source.index("async def _on_search_found"),
    )
    downloading_edit = play_source.index(
        '"play_downloading", _download_progress_template(m.lang)',
        active_gate,
    )
    assert active_gate < downloading_edit
    assert "or queue.get_current(chat_id) is not None" in play_source[
        active_gate:downloading_edit
    ]

    for locale_path in (package_root / "locales").glob("*.json"):
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        template = locale["play_queued"]
        assert "Added To Queue At #{0}" in template
        assert "▶ Title :" in template
        assert "▶ Duration :" in template
        assert "▶ Requested By :" in template
        assert "<b>Link:</b>" not in template


def test_play_not_found_closes_progress_before_edit():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import AnonX_3.plugins.play as play_module

    async def verify():
        events = []
        message = SimpleNamespace(
            id=990007,
            chat=SimpleNamespace(id=-100990007),
        )
        language = {"play_not_found": "NOT FOUND"}

        async def invalidate(_message):
            events.append("invalidate")

        async def get_template(*args):
            events.append("template")
            return args[-1]

        async def edit(*_args, **_kwargs):
            events.append("edit")
            return message

        with (
            patch.object(play_module, "_invalidate_play_request", invalidate),
            patch.object(
                play_module.db,
                "get_custom_text_for_chat",
                AsyncMock(side_effect=get_template),
            ),
            patch.object(play_module.utils, "edit_formatted", edit),
        ):
            await play_module._edit_play_not_found(
                message,
                message.chat.id,
                language,
            )

        assert events == ["invalidate", "template", "edit"]

    asyncio.run(verify())


def test_now_playing_deletes_orphaned_download_progress_card():
    """A progress card that is not the now-playing card must not survive.

    Closing the card only revokes write ownership, so when play_media lands on a
    different message (direct→local failover, or the send fallback after a failed
    edit) the download card used to stay on screen frozen at "DOWNLOADING 100%"
    with a live Cancel button.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    import AnonX_3.core.playback as playback_module

    def make_media(progress_id):
        return SimpleNamespace(
            download_progress_message=SimpleNamespace(
                id=progress_id,
                chat=SimpleNamespace(id=-100550011),
            ),
        )

    async def verify():
        deleted: list[tuple[int, int]] = []

        async def delete_messages(chat_id, message_id):
            deleted.append((chat_id, message_id))

        async def delete_message(chat_id, message_id):
            deleted.append((chat_id, message_id))

        with (
            patch.object(playback_module.app, "delete_messages", delete_messages),
            patch.object(playback_module.bot_api, "delete_message", delete_message),
        ):
            # Orphan: the now-playing card is a different message.
            orphaned = make_media(4101)
            await playback_module._drop_orphaned_progress_card(
                -100550011, orphaned, 4207
            )
            assert deleted == [(-100550011, 4101), (-100550011, 4101)], deleted
            assert orphaned.download_progress_message is None

            # Same message became the now-playing card: must be kept.
            deleted.clear()
            reused = make_media(4207)
            await playback_module._drop_orphaned_progress_card(
                -100550011, reused, 4207
            )
            assert deleted == [], deleted
            assert reused.download_progress_message is not None

            # No progress card at all is a no-op, not an error.
            deleted.clear()
            await playback_module._drop_orphaned_progress_card(
                -100550011, SimpleNamespace(), 4207
            )
            assert deleted == [], deleted

    asyncio.run(verify())

    playback_source = (ROOT / "AnonX_3" / "core" / "playback.py").read_text(
        encoding="utf-8"
    )
    # One cleanup after the edit/send branches converge on media.message_id.
    assert playback_source.count("await _drop_orphaned_progress_card(") == 1

    calls_source = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(
        encoding="utf-8"
    )
    failover = calls_source[calls_source.index("direct→local failover chat_id=%s") :]
    failover = failover[: failover.index("async def decorators(")]
    # A same-track failover must reuse the visible card, not post the
    # queue-advance "play_next" text.
    assert '"play_next"' not in failover
    assert 'getattr(media, "status_message_id", 0)' in failover
    assert 'getattr(progress, "id", 0)' in failover
    assert 'getattr(candidate, "empty", False)' in failover
    assert "force_now_playing=True" in failover
    assert "force_now_playing: bool = False" in calls_source
    assert "force_now_playing or not seek_time" in calls_source


def test_auto_learn_lifecycle():
    from datetime import datetime, timedelta, timezone

    from AnonX_3.core.mongo import MongoDB, select_stale_auto_reply_keys

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    rules = {
        "unused": {"text": "a"},
        "popular": {"text": "b"},
        "fresh": {"text": "c"},
        "manual": {"text": "d"},
        "legacy": {"text": "e"},
    }
    metadata = {
        "unused": {
            "source": "auto",
            "created_at": (now - timedelta(hours=30)).replace(tzinfo=None),
            "use_count": 0,
        },
        "popular": {
            "source": "auto",
            "last_used_at": now - timedelta(hours=25),
            "use_count": 9,
        },
        "fresh": {
            "source": "auto",
            "last_used_at": now - timedelta(minutes=10),
            "use_count": 0,
        },
        "manual": {
            "source": "manual",
            "updated_at": now - timedelta(days=30),
        },
    }
    assert select_stale_auto_reply_keys(rules, metadata, cutoff, limit=10) == [
        "unused",
        "popular",
    ]
    assert select_stale_auto_reply_keys(rules, metadata, cutoff, limit=1) == [
        "unused"
    ]

    class FakeCursor:
        def __init__(self, docs):
            self.docs = list(docs)
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index >= len(self.docs):
                raise StopAsyncIteration
            item = self.docs[self.index]
            self.index += 1
            return item

    class FakeChats:
        def __init__(self):
            self.docs = {}

        async def find_one(self, query, projection=None):
            doc = self.docs.get(query.get("_id"))
            return dict(doc) if doc else None

        async def update_one(self, query, update, upsert=False):
            chat_id = query["_id"]
            doc = self.docs.setdefault(chat_id, {"_id": chat_id})
            doc.update(update.get("$set", {}))

        def find(self, query, projection=None):
            docs = [
                dict(doc)
                for doc in self.docs.values()
                if "auto_reply_meta" in doc
            ]
            return FakeCursor(docs)

    async def verify_storage_lifecycle():
        fake = MongoDB.__new__(MongoDB)
        fake.chatsdb = FakeChats()
        await fake.set_auto_reply_rule(
            7, "manual", {"text": "keep", "entities": []}
        )
        blocked_count, blocked_ready = await fake.observe_auto_reply_candidate(
            7, "manual", {"text": "must not replace", "entities": []}
        )
        assert (blocked_count, blocked_ready) == (0, False)
        naive_seen = (now - timedelta(days=3)).replace(tzinfo=None)
        aware_seen = now - timedelta(days=2)
        fake.chatsdb.docs[7]["auto_reply_candidates"] = {
            f"old-{idx}": [
                {
                    "text": "stored",
                    "entities": [],
                    "observations": 1,
                    "first_seen_at": naive_seen,
                    "last_seen_at": naive_seen if idx % 2 else aware_seen,
                }
            ]
            for idx in range(201)
        }
        mixed_count, mixed_ready = await fake.observe_auto_reply_candidate(
            7, "mixed-datetime", {"text": "ok", "entities": []}
        )
        assert (mixed_count, mixed_ready) == (1, False)
        mixed_entry = fake.chatsdb.docs[7]["auto_reply_candidates"][
            "mixed-datetime"
        ][0]
        assert mixed_entry["first_seen_at"].tzinfo is not None
        assert mixed_entry["last_seen_at"].tzinfo is not None
        assert len(fake.chatsdb.docs[7]["auto_reply_candidates"]) <= 200
        first_count, first_ready = await fake.observe_auto_reply_candidate(
            7, "learned", {"text": "expire", "entities": []}
        )
        assert (first_count, first_ready) == (1, False)
        second_count, second_ready = await fake.observe_auto_reply_candidate(
            7, "learned", {"text": "expire", "entities": []}
        )
        assert (second_count, second_ready) == (2, True)
        assert "learned" in fake.chatsdb.docs[7]["auto_reply_candidates"]
        await fake.clear_auto_reply_candidate(7, "learned", "expire")
        assert "learned" not in fake.chatsdb.docs[7]["auto_reply_candidates"]
        await fake.append_auto_reply_variant(
            7, "learned", {"text": "expire", "entities": []}
        )
        doc = fake.chatsdb.docs[7]
        assert doc["auto_reply_meta"]["manual"]["source"] == "manual"
        assert doc["auto_reply_meta"]["learned"]["source"] == "auto"
        doc["auto_reply_meta"]["learned"]["created_at"] = (
            now - timedelta(hours=25)
        )
        doc["auto_reply_meta"]["learned"]["last_learned_at"] = (
            now - timedelta(hours=25)
        )
        removed = await fake.cleanup_stale_auto_reply_rules(
            max_idle_seconds=24 * 3600,
            limit=10,
            now=now,
        )
        assert removed == [(7, "learned")]
        assert "learned" not in doc["auto_reply_rules"]
        assert "manual" in doc["auto_reply_rules"]

    asyncio.run(verify_storage_lifecycle())

    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    mongo_source = (ROOT / "AnonX_3" / "core" / "mongo.py").read_text(
        encoding="utf-8"
    )
    main_source = (ROOT / "AnonX_3" / "__main__.py").read_text(encoding="utf-8")
    reply_source = (
        ROOT / "AnonX_3" / "plugins" / "auto_reply.py"
    ).read_text(encoding="utf-8")
    assert '"AUTO_LEARN_TTL_HOURS", "24"' in config_source
    assert 'supervisor.spawn("auto_reply_cleanup"' in main_source
    assert "cleanup_stale_auto_reply_rules(" in main_source
    assert "touch_auto_reply_rule(" in reply_source
    assert "observe_auto_reply_candidate(" in reply_source
    assert "_auto_reply_candidate_activity_time(" in mongo_source
    assert "AUTO_LEARN_CONFIRMATIONS" in config_source


def test_auto_learn_confirmation_gate():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import AnonX_3.plugins.auto_reply as reply_module

    async def verify_watcher_learning():
        observations = 0

        async def observe(*args, **kwargs):
            nonlocal observations
            observations += 1
            return observations, observations >= 2

        message = SimpleNamespace(
            chat=SimpleNamespace(id=77),
            from_user=SimpleNamespace(id=5, is_bot=False, is_self=False),
            text="ဟုတ်ကဲ့",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=SimpleNamespace(
                text="မင်္ဂလာပါ",
                caption=None,
                entities=[],
                caption_entities=[],
            ),
        )

        with (
            patch.object(reply_module, "_is_bot_or_status_message", return_value=False),
            patch.object(reply_module, "_auto_learn_allowed", return_value=True),
            patch.object(reply_module.db, "observe_auto_reply_candidate", side_effect=observe),
            patch.object(
                reply_module.db,
                "append_auto_reply_variant",
                new=AsyncMock(return_value=(1, True)),
            ) as append,
            patch.object(
                reply_module.db,
                "clear_auto_reply_candidate",
                new=AsyncMock(),
            ) as clear,
            patch.object(reply_module.db, "set_auto_reply", new=AsyncMock()) as enable,
        ):
            assert await reply_module._try_auto_learn(message) is True
            append.assert_not_awaited()
            enable.assert_not_awaited()
            assert await reply_module._try_auto_learn(message) is True
            append.assert_awaited_once()
            clear.assert_awaited_once()
            enable.assert_awaited_once_with(77, True)

    asyncio.run(verify_watcher_learning())


def test_aidj_mode_and_autoplay_persistence():
    from AnonX_3.core.autoplay import StrictAutoplaySelector
    from AnonX_3.core.mongo import MongoDB

    class SilentLogger:
        def info(self, *args, **kwargs):
            return None

    selector = StrictAutoplaySelector(SilentLogger())
    normal = selector._build_queries(
        "Example Song", "Example Artist", ["example"], "similar"
    )
    chill = selector._build_queries(
        "Example Song", "Example Artist", ["example"], "chill"
    )
    assert not any("chill" in query.casefold() for query, _ in normal)
    assert any("chill" in query.casefold() for query, _ in chill)

    class FakeChats:
        def __init__(self):
            self.docs = {
                77: {"_id": 77, "autoplay": True, "aidj_mode": "study"}
            }

        async def find_one(self, query, projection=None):
            doc = self.docs.get(query["_id"])
            return dict(doc) if doc else None

        async def update_one(self, query, update, upsert=False):
            doc = self.docs.setdefault(query["_id"], {"_id": query["_id"]})
            doc.update(update.get("$set", {}))

    async def verify():
        fake = MongoDB.__new__(MongoDB)
        fake.chatsdb = FakeChats()
        fake.autoplay = []
        fake.autoplay_loaded = set()
        fake.aidj_mode = {}

        assert await fake.get_autoplay(77) is True
        assert await fake.get_aidj_mode(77) == "study"
        await fake.set_aidj_mode(77, "party")
        assert await fake.get_aidj_mode(77) == "party"
        await fake.set_autoplay(77, False)
        assert await fake.get_autoplay(77) is False
        assert fake.chatsdb.docs[77]["autoplay"] is False
        assert fake.chatsdb.docs[77]["aidj_mode"] == "party"

    asyncio.run(verify())


def test_unified_request_context_and_priority_queue():
    from AnonX_3.core.request_context import enrich_request, normalize_query
    from AnonX_3.helpers import Track
    from AnonX_3.helpers._queue import Queue

    assert normalize_query("  hello\u200b   world  ") == "hello world"

    q = Queue()
    current = Track(id="aaaaaaaaaaa", title="current")
    ai = enrich_request(
        Track(id="bbbbbbbbbbb", title="ai"),
        chat_id=7,
        request_source="aidj",
        priority=20,
    )
    manual = enrich_request(
        Track(id="ccccccccccc", title="manual"),
        chat_id=7,
        user_id=42,
        query="  manual  ",
        request_source="manual",
        priority=100,
    )
    q.add(7, current)
    q.add(7, ai)
    assert q.add(7, manual) == 1
    assert [item.id for item in q.get_queue(7)] == [
        "aaaaaaaaaaa",
        "ccccccccccc",
        "bbbbbbbbbbb",
    ]
    assert manual.normalized_query == "manual"
    assert manual.request_source == "manual"


def test_source_circuit_and_transition_capability():
    from AnonX_3 import config as cfg
    from AnonX_3.core.source_health import SourceHealthRegistry
    from AnonX_3.core.transition_policy import select_transition_plan

    old_threshold = cfg.SOURCE_FAILURE_THRESHOLD
    old_crossfade = cfg.CROSSFADE_ENABLED
    try:
        cfg.SOURCE_FAILURE_THRESHOLD = 2
        registry = SourceHealthRegistry()
        registry.failure("test", reason="one")
        assert registry.allow("test")
        registry.failure("test", reason="two")
        assert not registry.allow("test")
        assert registry.snapshot()["test"]["circuit"] == "open"
        registry.success("test", latency_sec=0.1)
        assert registry.allow("test")

        cfg.CROSSFADE_ENABLED = True
        safe = select_transition_plan(next_ready=True, overlap_capable=False)
        assert safe.gapless is True
        assert safe.crossfade is False
        assert safe.reason == "voice_engine_no_overlap_capability"
        capable = select_transition_plan(next_ready=True, overlap_capable=True)
        assert capable.crossfade is True
    finally:
        cfg.SOURCE_FAILURE_THRESHOLD = old_threshold
        cfg.CROSSFADE_ENABLED = old_crossfade


def test_custom_template_and_prefetch_guards():
    from AnonX_3.helpers import utils

    prefetch_source = (
        ROOT / "AnonX_3" / "core" / "prefetch.py"
    ).read_text(encoding="utf-8")

    premium_template = {
        "text": "⭐ {0}",
        "entities": [
            {
                "type": "custom_emoji",
                "offset": 0,
                "length": 1,
                "custom_emoji_id": "5372981976804366741",
            }
        ],
    }
    rendered = utils.format_template(premium_template, "Track")
    assert rendered["text"] == "⭐ Track"
    assert rendered["entities"][0]["type"] == "custom_emoji"
    assert rendered["entities"][0]["custom_emoji_id"] == "5372981976804366741"
    pyrogram_entity = utils.pyrogram_entities(rendered["entities"])[0]
    assert pyrogram_entity.custom_emoji_id == 5372981976804366741

    escaped_text = (
        "🎵 {{0}}\n"
        "⏱ DURATION: {{1}}\n"
        "📺 CHANNEL: {{2}}\n"
        "🎧 TYPE: {{3}}\n"
        "▶ USING {{4}}."
    )
    escaped_entities = []
    search_from = 0
    for index, icon in enumerate(("🎵", "⏱", "📺", "🎧", "▶")):
        icon_start = escaped_text.index(icon, search_from)
        escaped_entities.append(
            {
                "type": "custom_emoji",
                "offset": utils.to_utf16_offset(escaped_text, icon_start),
                "length": utils.utf16_length(icon),
                "custom_emoji_id": str(5372981976804366741 + index),
            }
        )
        search_from = icon_start + len(icon)
    escaped_template = {
        "text": escaped_text,
        "entities": escaped_entities,
    }
    escaped_rendered = utils.format_template(
        escaped_template,
        "Title",
        "3:46",
        "UG Entertainment",
        "Audio",
        "Ba Na Na FM",
    )
    assert escaped_rendered["text"] == (
        "🎵 Title\n"
        "⏱ DURATION: 3:46\n"
        "📺 CHANNEL: UG Entertainment\n"
        "🎧 TYPE: Audio\n"
        "▶ USING Ba Na Na FM."
    )
    assert "{" not in escaped_rendered["text"]
    assert "}" not in escaped_rendered["text"]
    assert [
        entity["custom_emoji_id"] for entity in escaped_rendered["entities"]
    ] == [
        "5372981976804366741",
        "5372981976804366742",
        "5372981976804366743",
        "5372981976804366744",
        "5372981976804366745",
    ]
    rendered_icon_offsets = [
        utils.to_utf16_offset(
            escaped_rendered["text"],
            escaped_rendered["text"].index(icon),
        )
        for icon in ("🎵", "⏱", "📺", "🎧", "▶")
    ]
    assert [
        entity["offset"] for entity in escaped_rendered["entities"]
    ] == rendered_icon_offsets
    assert all(
        isinstance(entity.custom_emoji_id, int)
        for entity in utils.pyrogram_entities(escaped_rendered["entities"])
    )

    assert "prefetch_discarded_stale" in prefetch_source
    assert "self.secondary" in prefetch_source


def test_po_token_video_binding_and_403_rotation():
    from unittest.mock import patch

    from AnonX_3 import config as app_config
    from AnonX_3.core.provider.po_token import PoTokenProvider

    provider = PoTokenProvider()
    provider.enabled = lambda: True
    provider.plugin_available = lambda: True

    with (
        patch.object(app_config, "PO_TOKEN_PROVIDER_URL", "http://127.0.0.1:4416"),
        patch.object(app_config, "PO_TOKEN_CLIENT", "mweb"),
    ):
        configured = asyncio.run(
            provider.apply_to_ydl_opts(
                {"extractor_args": {"youtube": {"skip": ["translated_subs"]}}},
                video_id="aaaaaaaaaaa",
            )
        )
    assert configured["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://127.0.0.1:4416"]
    }
    assert configured["extractor_args"]["youtube"] == {
        "skip": ["translated_subs"],
        "player_client": ["mweb"],
    }
    assert "po_token" not in configured["extractor_args"]["youtube"]

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    compose_path = ROOT / "docker-compose.yml"
    if not compose_path.exists():
        compose_path = ROOT / "docker-compose.example.yml"
    compose = compose_path.read_text(encoding="utf-8")
    assert "bgutil-ytdlp-pot-provider==1.3.1" in requirements
    assert "brainicism/bgutil-ytdlp-pot-provider:1.3.1" in compose
    assert "provider/po_stub.conf" not in compose
    assert "http://po-provider:4416" in compose

    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    assert "def _po_token_provider_url" in config_source
    assert 'return "http://127.0.0.1:4416"' in config_source
    assert 'missing.append("PO_TOKEN_PROVIDER_URL")' not in config_source

    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    assert "po_token_provider.invalidate(" not in youtube_source
    assert '"web_safari"' in youtube_source
    assert '"-android_vr"' in youtube_source
    assert '"-android_sdkless"' in youtube_source
    assert '"tv"' in youtube_source
    assert 'f"{url}/ping"' in youtube_source
    assert "_purge_partial_outputs()" in youtube_source
    assert 'metrics.inc("youtube_403_recovery")' in youtube_source


def test_cancel_lifecycle_and_platform_recovery_guards():
    from AnonX_3.helpers._play import (
        cancel_play_request,
        track_play_request_task,
    )

    async def verify_registry():
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        track_play_request_task(987654321, task)
        await started.wait()
        assert await cancel_play_request(987654321) is True
        assert task.cancelled()
        assert await cancel_play_request(987654321) is False

    asyncio.run(verify_registry())

    callbacks_source = (
        ROOT / "AnonX_3" / "plugins" / "callbacks.py"
    ).read_text(encoding="utf-8")
    telegram_source = (
        ROOT / "AnonX_3" / "core" / "telegram.py"
    ).read_text(encoding="utf-8")
    play_source = (
        ROOT / "AnonX_3" / "helpers" / "_play.py"
    ).read_text(encoding="utf-8")
    calls_source = (
        ROOT / "AnonX_3" / "core" / "calls.py"
    ).read_text(encoding="utf-8")

    assert "await query.message.delete()" in callbacks_source
    assert "cancel_play_request(msg_id)" in callbacks_source
    assert "tiktok.cancel(msg_id)" in callbacks_source
    assert "facebook.cancel(msg_id)" in callbacks_source
    assert "dl_not_found" not in callbacks_source.split(
        "async def cancel_dl", 1
    )[1].split("@app.on_callback_query", 1)[0]
    assert "return bool(event or task)" in telegram_source
    assert "track_play_request_task(request_message_id, request_task)" in play_source
    assert "except asyncio.CancelledError:" in play_source
    assert '"telegram_remote",' in calls_source
    assert "except exceptions.NoVideoSourceFound:" in calls_source
    assert "except call_exceptions.NoVideoSourceFound:" in play_source
    assert 'video=is_video,' in telegram_source
    assert '_lang.get("error_no_file", _lang["play_error"])' in play_source
    assert "if media.video" in calls_source


def test_tiktok_audio_artifact_guards():
    import threading
    import time
    from unittest.mock import patch

    from AnonX_3.core.tiktok import TikTok

    assert TikTok._has_stream("downloads/does-not-exist.part", "audio") is False

    state = {"downloads": 0, "done": False}
    started = threading.Event()

    class FakeYDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            state["downloads"] += 1
            started.set()
            time.sleep(0.08)
            state["done"] = True

    async def verify_tiktok_singleflight():
        service = TikTok()
        owner = asyncio.create_task(
            service.download(
                url="https://www.tiktok.com/@test/video/123",
                media_id="tt_singleflight_guard",
                video=True,
                message_id=456789,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert await service.cancel(456789) is True
        try:
            await owner
        except asyncio.CancelledError:
            pass
        survivor = await service.download(
            url="https://www.tiktok.com/@test/video/123",
            media_id="tt_singleflight_guard",
            video=True,
        )
        assert survivor == "downloads/tt_singleflight_guard.mp4"
        assert state["downloads"] == 1

    with (
        patch("AnonX_3.core.tiktok.yt_dlp.YoutubeDL", FakeYDL),
        patch.object(
            TikTok,
            "_is_playable",
            side_effect=lambda _path, *, video: state["done"],
        ),
        patch.object(
            TikTok,
            "_artifact_candidates",
            side_effect=lambda _media_id: (
                [Path("downloads/tt_singleflight_guard.mp4")]
                if state["done"]
                else []
            ),
        ),
        patch.object(TikTok, "_purge_bad_artifacts"),
    ):
        asyncio.run(verify_tiktok_singleflight())

    tiktok_source = (
        ROOT / "AnonX_3" / "core" / "tiktok.py"
    ).read_text(encoding="utf-8")
    play_source = (
        ROOT / "AnonX_3" / "plugins" / "play.py"
    ).read_text(encoding="utf-8")
    calls_source = (
        ROOT / "AnonX_3" / "core" / "calls.py"
    ).read_text(encoding="utf-8")

    assert '"bestaudio[acodec!=none]/best[acodec!=none]"' in tiktok_source
    assert '"preferredcodec": "m4a"' in tiktok_source
    assert 'ext = "mp4" if video else "m4a"' in tiktok_source
    assert 'or ".part" in target.name.lower()' in tiktok_source
    assert "await asyncio.to_thread(\n                self._is_playable," in tiktok_source
    assert "self._purge_bad_artifacts(media_id, video=video)" in tiktok_source
    assert '_tiktok_download_flight = SingleFlight("tiktok-download")' in tiktok_source
    assert '_tiktok_download_flight.do(' in tiktok_source
    assert 'f"tiktok:{media_id}"' in tiktok_source
    download_body = tiktok_source.split("async def download(", 1)[1]
    pre_owner = download_body.split("def _download_owned", 1)[0]
    assert "_purge_bad_artifacts" not in pre_owner
    owned_body = download_body.split("def _download_owned", 1)[1]
    assert "_purge_bad_artifacts" in owned_body.split(
        "async def _run_download", 1
    )[0]
    assert "self._active_tasks[message_id] = waiter" in tiktok_source
    assert "'m4a'" in play_source
    assert "await tiktok.await_current_cache_or_download(" in calls_source


def test_parallel_external_source_guards():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    from AnonX_3 import bot_api, yt
    from AnonX_3.core.prefetch import PrefetchManager
    from AnonX_3.core.telegram import Telegram
    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Media, utils

    assert Telegram._duration_label(0) == "00:00"
    assert Telegram._duration_label(65) == "01:05"
    progress_card = utils.render_download_progress(
        {
            "text": "CUSTOM DOWNLOAD",
            "entities": [{"type": "bold", "offset": 0, "length": 6}],
        },
        current=50,
        total=100,
        speed=25,
        eta_seconds=2,
    )
    assert progress_card["text"].startswith("CUSTOM DOWNLOAD")
    assert "██████░░░░░░" in progress_card["text"]
    assert "50.0%" in progress_card["text"]
    assert "📥" not in progress_card["text"]
    assert "Unknown" not in progress_card["text"]
    assert "0 B" not in progress_card["text"]
    assert "⚡" not in progress_card["text"]
    assert "⏳" not in progress_card["text"]
    assert progress_card["entities"][0]["type"] == "bold"

    initial_progress_card = utils.render_initial_download_progress(
        {
            "text": "CUSTOM DOWNLOAD",
            "entities": [{"type": "bold", "offset": 0, "length": 6}],
        }
    )
    assert initial_progress_card["text"].startswith("CUSTOM DOWNLOAD")
    assert "░░░░░░░░░░░░" in initial_progress_card["text"]
    assert "0.0%" in initial_progress_card["text"]
    assert initial_progress_card["entities"][0]["type"] == "bold"

    async def verify_large_telegram_route():
        service = Telegram()
        media = Media(
            id="large-telegram-video",
            telegram_file_id="large-file-id",
            telegram_file_size=3 * 1024 * 1024 * 1024,
            local_path="downloads/tg_large-file-id.mp4",
            video=True,
            source="telegram_remote",
        )
        request = AsyncMock()
        with patch.object(bot_api, "_request", request):
            remote, local = await service.resolve_direct_stream(media=media)
        assert remote is None
        assert local == "downloads/tg_large-file-id.mp4"
        request.assert_not_awaited()

        request = AsyncMock(side_effect=bot_api.FileTooLarge("file is too big"))
        media.telegram_file_size = 0
        with patch.object(bot_api, "_request", request):
            remote, local = await service.resolve_direct_stream(media=media)
        assert remote is None
        assert local == "downloads/tg_large-file-id.mp4"
        request.assert_awaited_once()

    asyncio.run(verify_large_telegram_route())

    async def verify_live_telegram_progress():
        service = Telegram()
        progress_message = SimpleNamespace(
            id=778811,
            chat=SimpleNamespace(id=-100778811),
            reply_markup=None,
        )
        media = Media(
            id="live-progress-telegram-video",
            telegram_file_id="live-progress-file-id",
            telegram_file_size=3 * 1024 * 1024 * 1024,
            local_path="downloads/tg_live-progress-file-id.mp4",
            video=True,
            source="telegram_remote",
        )
        setattr(media, "telegram_message", SimpleNamespace())
        setattr(media, "download_progress_message", progress_message)
        setattr(media, "download_progress_template", "CUSTOM DOWNLOAD")
        setattr(media, "download_progress_lang", {"cancel": "Cancel"})
        setattr(media, "download_progress_cancel_label", "Cancel")

        async def fake_download(_message, path, progress):
            await progress(50, 100)
            return path

        edit_progress = AsyncMock()
        with (
            patch.object(service, "_download_bytes_to_path", side_effect=fake_download),
            patch(
                "AnonX_3.core.telegram.is_playable_media",
                side_effect=[False, False, True],
            ),
            patch("AnonX_3.core.telegram.utils.edit_formatted", edit_progress),
        ):
            result = await service.ensure_local_file(media)

        assert result == media.local_path
        edit_progress.assert_awaited_once()
        rendered = edit_progress.await_args.args[1]
        assert rendered["text"].startswith("CUSTOM DOWNLOAD")
        assert "50.0%" in rendered["text"]
        assert getattr(media, "download_progress_started", False) is True

    asyncio.run(verify_live_telegram_progress())

    async def verify_ytdlp_progress_bridge():
        media = Media(id="live-progress-worker-provider", source="tiktok_remote")
        setattr(
            media,
            "download_progress_message",
            SimpleNamespace(
                id=778812,
                chat=SimpleNamespace(id=-100778812),
                reply_markup=None,
            ),
        )
        setattr(media, "download_progress_template", "CUSTOM PROVIDER DOWNLOAD")
        setattr(media, "download_progress_lang", {"cancel": "Cancel"})
        edit_progress = AsyncMock()
        with patch.object(utils, "edit_formatted", edit_progress):
            hook = utils.make_download_progress_hook(media, throttle=2.0)
            assert hook is not None
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 25,
                    "total_bytes": 100,
                    "speed": 10,
                    "eta": 7,
                }
            )
            await asyncio.sleep(0.05)

        edit_progress.assert_awaited_once()
        rendered = edit_progress.await_args.args[1]
        assert rendered["text"].startswith("CUSTOM PROVIDER DOWNLOAD")
        assert "25.0%" in rendered["text"]
        assert getattr(media, "download_progress_started", False) is True

    asyncio.run(verify_ytdlp_progress_bridge())

    async def verify_real_download_watcher_attachment():
        service = YouTube()
        release = asyncio.Event()

        async def worker():
            await release.wait()

        task = asyncio.create_task(worker())
        media = Media(id="live-progress-youtube", source="youtube")
        message = SimpleNamespace(
            id=778813,
            chat=SimpleNamespace(id=-100778813),
            reply_markup=None,
        )
        service._inflight_downloads[media.id] = {(False, None): task}
        assert service.attach_download_watcher(
            media.id,
            progress_message=message,
            progress_lang={"cancel": "Cancel"},
            progress_media=media,
        )
        assert service._active_tasks[message.id] is task
        assert service._download_watchers[task][message.id]["media"] is media
        release.set()
        await task

    asyncio.run(verify_real_download_watcher_attachment())

    async def verify_parallel_youtube_progress_context_merge():
        manager = PrefetchManager()
        release = asyncio.Event()
        started = asyncio.Event()
        existing_media = Media(
            id="liveRace123",
            source=None,
            video=False,
        )
        warm_media = Media(
            id="liveRace123",
            source=None,
            video=False,
        )
        progress_message = SimpleNamespace(
            id=778814,
            chat=SimpleNamespace(id=-100778814),
            reply_markup=None,
        )
        setattr(warm_media, "download_progress_message", progress_message)
        setattr(warm_media, "download_progress_template", "CUSTOM DOWNLOAD")
        setattr(warm_media, "download_progress_lang", {"cancel": "Cancel"})

        async def cache_worker():
            started.set()
            await release.wait()

        cache_task = asyncio.create_task(cache_worker())
        manager.current_cache[-100778814] = (existing_media, cache_task)
        attach = Mock(return_value=True)
        with patch.object(yt, "attach_download_watcher", attach):
            await manager.start_current_cache(-100778814, warm_media, force=True)

        assert getattr(existing_media, "download_progress_message") is progress_message
        assert getattr(existing_media, "download_progress_template") == "CUSTOM DOWNLOAD"
        attach.assert_called_once()
        release.set()
        await cache_task

    asyncio.run(verify_parallel_youtube_progress_context_merge())

    telegram_source = (
        ROOT / "AnonX_3" / "core" / "telegram.py"
    ).read_text(encoding="utf-8")
    tiktok_source = (
        ROOT / "AnonX_3" / "core" / "tiktok.py"
    ).read_text(encoding="utf-8")
    facebook_source = (
        ROOT / "AnonX_3" / "core" / "facebook.py"
    ).read_text(encoding="utf-8")
    helper_source = (
        ROOT / "AnonX_3" / "helpers" / "_play.py"
    ).read_text(encoding="utf-8")
    plugin_source = (
        ROOT / "AnonX_3" / "plugins" / "play.py"
    ).read_text(encoding="utf-8")
    calls_source = (
        ROOT / "AnonX_3" / "core" / "calls.py"
    ).read_text(encoding="utf-8")
    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    prefetch_source = (
        ROOT / "AnonX_3" / "core" / "prefetch.py"
    ).read_text(encoding="utf-8")
    validation_source = (
        ROOT / "AnonX_3" / "core" / "downloader" / "validation.py"
    ).read_text(encoding="utf-8")
    soundcloud_source = (
        ROOT / "AnonX_3" / "core" / "resolver" / "soundcloud.py"
    ).read_text(encoding="utf-8")

    # Telegram metadata no longer crashes and local fallback can use the
    # original message/assistant for large or private media.
    assert "def _duration_label(" in telegram_source
    assert 'SingleFlight("telegram-download")' in telegram_source
    assert 'setattr(result, "telegram_message", msg)' in telegram_source
    assert 'setattr(result, "telegram_story", story)' in telegram_source
    assert "for client in clients:" in telegram_source
    assert "if local_path and await asyncio.to_thread(" in telegram_source
    assert "is_playable_media,\n            local_path," in telegram_source
    assert "telegram_file_size=file_size" in telegram_source
    assert "file_size > _BOT_API_DIRECT_MAX_BYTES" in telegram_source
    assert "switching to assistant MTProto" in telegram_source
    assert "render_download_progress(" in telegram_source
    assert "await utils.edit_download_progress(" in telegram_source
    assert "media=media" in telegram_source
    assert "now - progress_last_edit < 2.0" in telegram_source

    # Facebook downloads are cancel-safe, media-ID single-flight, audio-safe,
    # and ffprobe validated before playback.
    assert 'SingleFlight("facebook-download")' in facebook_source
    assert 'f"facebook:{media_id}"' in facebook_source
    assert '"preferredcodec": "m4a"' in facebook_source
    assert "if await asyncio.to_thread(\n                is_playable_media," in facebook_source
    assert "make_download_progress_hook(progress_media)" in facebook_source
    assert "make_download_progress_hook(progress_media)" in tiktok_source
    assert '"progress_hooks"] = [progress_hook]' in facebook_source
    assert '"progress_hooks"] = [progress_hook]' in tiktok_source

    # Telegram/TikTok/Facebook resolve while assistant readiness runs, then
    # Calls races their direct URL against a verified local fallback.
    assert "warm_external_task = _create_play_request_task(" in helper_source
    assert "def _create_play_request_task(" in helper_source
    assert "External warm-up failed; continuing with normal resolver" in helper_source
    assert 'setattr(m, "_warm_external_media", warmed_external)' in helper_source
    assert '"download_progress_template"' in helper_source
    assert '"download_progress_started"' in helper_source
    assert helper_source.count(
        "utils.render_initial_download_progress(downloading_tpl)"
    ) >= 3
    assert 'getattr(m, "_warm_external_media", None)' in plugin_source
    assert '"download_progress_template"' in plugin_source
    assert "is_youtube_download = (" in plugin_source
    assert "file_source in external_download_sources or is_youtube_download" in plugin_source
    assert "render_download_progress(" in youtube_source
    assert 'ydl_opts["progress_hooks"] = [_progress_hook]' in youtube_source
    assert "self._download_progress_state[task]" in youtube_source
    assert '{"status": "finished", "filename": result}' in youtube_source
    assert "def attach_download_watcher(" in youtube_source
    assert helper_source.index("Publish the base Downloading state") < helper_source.index(
        'name=f"warm-local-after-search:{m.id}"'
    )
    assert '"download_progress_message"' in prefetch_source
    assert "progress_throttle=1.0" in prefetch_source
    assert "yt.attach_download_watcher(" in prefetch_source
    assert "_DOWNLOAD_PROGRESS_ATTRS" in prefetch_source
    assert '"tiktok_remote", "facebook_remote", "telegram_remote"' in calls_source
    assert "await tiktok.start_current_cache(chat_id, media)" in calls_source
    assert "await facebook.start_current_cache(chat_id, media)" in calls_source
    assert "await tg.start_current_cache(chat_id, media)" in calls_source
    assert "video=bool(getattr(media, \"video\", False))" in calls_source
    # Dead uploads are remembered across parallel workers, then a different
    # YouTube upload is preferred before crossing to SoundCloud.
    assert "def _remember_permanent_failure(" in youtube_source
    assert "def is_permanently_unavailable(" in youtube_source
    assert "async def alternate_track(" in youtube_source
    assert "YouTube alternate recovered" in calls_source
    # Direct SoundCloud fallback must explicitly suppress inherited proxy
    # environment variables, and only follows a proxy transport failure.
    assert 'opts["proxy"] = proxy or ""' in soundcloud_source
    assert "def _is_proxy_transport_failure(" in soundcloud_source
    assert "soundcloud proxy transport failed; retrying direct" in soundcloud_source
    assert '"extract_flat"] = "in_playlist"' in soundcloud_source
    assert '"ignoreerrors": True' in soundcloud_source
    assert '"retries": 0' in soundcloud_source
    assert "class SoundCloudTransportError(" in soundcloud_source

    # /vplay requires a real video stream; a container name or command flag
    # alone cannot turn an audio-only source into video.
    assert '"stream=codec_type"' in validation_source
    assert 'stream in _probe_media_stream_types(' in validation_source
    assert 'if not has_media_stream(path, "audio")' in validation_source
    assert 'return not video or has_media_stream(path, "video")' in validation_source


def test_youtube_live_progress_pipeline():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    from AnonX_3 import yt
    from AnonX_3.core.prefetch import PrefetchManager
    from AnonX_3.core.resource_manager import resource_manager
    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Media, utils

    async def verify_real_hook_sequence_and_cache_completion():
        service = YouTube()
        progress_events = {
            "10.0%": threading.Event(),
            "50.0%": threading.Event(),
            "100.0%": threading.Event(),
        }
        rendered_texts: list[str] = []
        message = SimpleNamespace(
            id=778815,
            chat=SimpleNamespace(id=-100778815),
            reply_markup=None,
            lang={"cancel": "Cancel"},
        )
        media = Media(id="LiveBar1234", source="youtube", video=False)
        setattr(media, "download_progress_template", "CUSTOM DOWNLOAD")
        setattr(media, "download_progress_cancel_label", "Cancel")

        async def capture_progress(_message, rendered, **_kwargs):
            text = rendered["text"]
            rendered_texts.append(text)
            for label, event in progress_events.items():
                if label in text:
                    event.set()
            return _message

        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def download(self, _urls):
                hook = self.opts["progress_hooks"][0]
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 100,
                        "total_bytes": 1000,
                        "speed": 100,
                        "eta": 9,
                    }
                )
                assert progress_events["10.0%"].wait(2)
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 500,
                        "total_bytes": 1000,
                        "speed": 100,
                        "eta": 5,
                    }
                )
                assert progress_events["50.0%"].wait(2)
                output = Path("downloads/LiveBar1234.m4a")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"x" * (128 * 1024))
                hook(
                    {
                        "status": "finished",
                        "downloaded_bytes": 1000,
                        "total_bytes": 1000,
                        "filename": str(output),
                    }
                )

        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                output_path = "downloads/LiveBar1234.m4a"
                with (
                    patch("AnonX_3.core.youtube.yt_dlp.YoutubeDL", FakeYoutubeDL),
                    patch.object(service, "get_cookies", return_value=None),
                    patch.object(service, "_browser_cookie_spec", return_value=None),
                    patch.object(
                        service,
                        "wait_media_file_ready",
                        AsyncMock(return_value=output_path),
                    ),
                    patch.object(
                        resource_manager, "allow_new_heavy_job", return_value=True
                    ),
                    patch.object(utils, "edit_formatted", side_effect=capture_progress),
                ):
                    result = await service.download(
                        media.id,
                        message_id=message.id,
                        progress_message=message,
                        progress_lang={"cancel": "Cancel"},
                        progress_media=media,
                    )
                    assert result == output_path, (
                        f"download result={result!r}, rendered={rendered_texts!r}"
                    )
                    assert progress_events["100.0%"].is_set()
                    assert any("10.0%" in text for text in rendered_texts)
                    assert any("50.0%" in text for text in rendered_texts)
                    assert any("100.0%" in text for text in rendered_texts)

                    # A repeated/cached play has no byte transfer to observe, so
                    # it must truthfully finalize the same UI at 100%, not 0%.
                    rendered_texts.clear()
                    cached = await service.download(
                        media.id,
                        message_id=message.id,
                        progress_message=message,
                        progress_lang={"cancel": "Cancel"},
                        progress_media=media,
                    )
                    assert cached == output_path
                    assert any("100.0%" in text for text in rendered_texts)
            finally:
                os.chdir(original_cwd)

    asyncio.run(verify_real_hook_sequence_and_cache_completion())

    async def verify_progress_to_playback_ownership_handoff():
        service = YouTube()
        message = SimpleNamespace(
            id=778817,
            chat=SimpleNamespace(id=-100778817),
            reply_markup=None,
            lang={"cancel": "Cancel"},
        )
        media = Media(id="Ownership123", source="youtube", video=False)
        setattr(media, "download_progress_message", message)
        setattr(media, "download_progress_template", "CUSTOM DOWNLOAD")
        setattr(media, "download_progress_cancel_label", "Cancel")

        release_download = asyncio.Event()

        async def background_download():
            await release_download.wait()
            return "downloads/Ownership123.m4a"

        download_task = asyncio.create_task(background_download())
        service._active_tasks[message.id] = download_task
        service._download_watchers[download_task] = {
            message.id: {
                "message": message,
                "lang": {"cancel": "Cancel"},
                "media": media,
            }
        }

        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        events: list[str] = []

        async def blocked_progress_edit(_message, rendered, **_kwargs):
            events.append(rendered["text"])
            edit_started.set()
            await release_edit.wait()
            return _message

        with patch.object(
            utils, "edit_formatted", side_effect=blocked_progress_edit
        ):
            in_flight = asyncio.create_task(
                service._edit_download_progress_card(
                    progress_message=message,
                    progress_lang={"cancel": "Cancel"},
                    progress_media=media,
                    current=100,
                    total=100,
                )
            )
            await edit_started.wait()

            # Playback publishes the closed marker before waiting for the
            # already-running Telegram edit to drain.
            handoff = asyncio.create_task(
                utils.close_download_progress(message, media)
            )
            await asyncio.sleep(0)
            assert utils.is_download_progress_closed(message)
            assert not handoff.done()

            release_edit.set()
            await in_flight
            await handoff
            assert service.detach_download_progress(message) is True
            assert message.id not in service._active_tasks
            assert all(
                message.id not in watchers
                for watchers in service._download_watchers.values()
            )
            assert not download_task.cancelled()
            assert not download_task.done()

            # This sentinel represents update_now_playing's play_media edit.
            # Every callback after the handoff must be rejected.
            events.append("PLAY_MEDIA")
            late = await service._edit_download_progress_card(
                progress_message=message,
                progress_lang={"cancel": "Cancel"},
                progress_media=media,
                current=100,
                total=100,
            )
            assert late is None
            assert events[-1] == "PLAY_MEDIA"
            assert not any(
                "100.0%" in item for item in events[events.index("PLAY_MEDIA") + 1 :]
            )

        release_download.set()
        assert await download_task == "downloads/Ownership123.m4a"

    asyncio.run(verify_progress_to_playback_ownership_handoff())

    async def verify_prefetch_inherits_foreground_status():
        manager = PrefetchManager()
        message = SimpleNamespace(
            id=778816,
            chat=SimpleNamespace(id=-100778816),
            reply_markup=None,
        )
        media = Media(id="Fallback123", source="youtube", video=False)
        setattr(media, "download_progress_message", message)
        setattr(media, "download_progress_lang", {"cancel": "Cancel"})
        download = AsyncMock(return_value=None)
        with (
            patch.object(yt, "is_complete_media_file", return_value=False),
            patch.object(yt, "download", download),
        ):
            result = await manager.await_current_cache_or_download(
                -100778816, media
            )
        assert result is None
        assert download.await_args.kwargs["progress_message"] is message
        assert download.await_args.kwargs["progress_lang"] == {"cancel": "Cancel"}
        assert download.await_args.kwargs["progress_media"] is media

    asyncio.run(verify_prefetch_inherits_foreground_status())

    calls_source = (
        ROOT / "AnonX_3" / "core" / "calls.py"
    ).read_text(encoding="utf-8")
    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    playback_source = (
        ROOT / "AnonX_3" / "core" / "playback.py"
    ).read_text(encoding="utf-8")
    assert "progress_message=(message if media.video else None)" not in calls_source
    assert calls_source.count("progress_message=download_progress_message") >= 4
    assert "Coalesce worker-thread hooks and serialize Telegram edits." in youtube_source
    assert "await self.render_completed_download_progress(" in youtube_source
    assert "_download_last_edit" not in youtube_source
    assert "await _claim_now_playing_card(message, media)" in playback_source
    assert "yt.detach_download_progress(candidate)" in playback_source


def test_runtime_log_isolation_and_media_progress_edit():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    from AnonX_3 import LOG_FILE_PATH, bot_api, db, logger
    from AnonX_3.helpers import utils

    assert LOG_FILE_PATH == os.devnull

    async def verify():
        markup = {"inline_keyboard": [[{"text": "Cancel", "callback_data": "cancel"}]]}
        rendered = {
            "text": "DOWNLOADING\n50.0%",
            "entities": [{"type": "bold", "offset": 0, "length": 11}],
        }
        media_message = SimpleNamespace(
            id=880001,
            chat=SimpleNamespace(id=-100880001),
            photo=object(),
            caption="DOWNLOADING",
        )
        edit_text = AsyncMock()
        edit_caption = AsyncMock(return_value=media_message)
        with (
            patch.object(db, "get_lang", AsyncMock(return_value="en")),
            patch.object(utils, "edit_text", edit_text),
            patch.object(utils, "edit_caption", edit_caption),
        ):
            result = await utils.edit_formatted(
                media_message,
                rendered,
                reply_markup=markup,
                ignore_stale=True,
            )
        assert result is media_message
        edit_text.assert_not_awaited()
        edit_caption.assert_awaited_once_with(
            media_message,
            caption=rendered["text"],
            caption_entities=rendered["entities"],
            reply_markup=markup,
            ignore_stale=True,
        )

        # A stale Message object may not expose its newly-added media fields.
        # Telegram's NoTextToEdit response must still switch to caption editing
        # without emitting a traceback.
        stale_message = SimpleNamespace(
            id=880002,
            chat=SimpleNamespace(id=-100880002),
        )
        # Use the real typed Bot API exception with deliberately different
        # wording, proving the fallback does not depend only on an error string.
        no_text = bot_api.NoTextToEdit("Bad Request: media message has no text")
        edit_text = AsyncMock(side_effect=no_text)
        edit_caption = AsyncMock(return_value=stale_message)
        log_exception = Mock()
        with (
            patch.object(db, "get_lang", AsyncMock(return_value="en")),
            patch.object(utils, "edit_text", edit_text),
            patch.object(utils, "edit_caption", edit_caption),
            patch.object(logger, "exception", log_exception),
        ):
            result = await utils.edit_formatted(
                stale_message,
                rendered,
                reply_markup=markup,
                ignore_stale=True,
            )
        assert result is stale_message
        edit_text.assert_awaited_once_with(
            stale_message,
            rendered["text"],
            entities=rendered["entities"],
            reply_markup=markup,
            ignore_stale=True,
        )
        edit_caption.assert_awaited_once_with(
            stale_message,
            caption=rendered["text"],
            caption_entities=rendered["entities"],
            reply_markup=markup,
            ignore_stale=True,
        )
        log_exception.assert_not_called()

        # Exercise the real Utilities -> BotAPI path. A styled button keeps
        # the markup on the Bot API branch instead of converting to Pyrogram.
        styled_markup = {
            "inline_keyboard": [[{
                "text": "Cancel",
                "callback_data": "cancel",
                "style": "danger",
            }]]
        }
        bot_text = AsyncMock(
            side_effect=bot_api.NoTextToEdit("Bad Request: media has no text")
        )
        bot_caption = AsyncMock(return_value=stale_message)
        log_exception = Mock()
        with (
            patch.object(db, "get_lang", AsyncMock(return_value="en")),
            patch.object(bot_api, "edit_message_text", bot_text),
            patch.object(bot_api, "edit_message_caption", bot_caption),
            patch.object(logger, "exception", log_exception),
        ):
            result = await utils.edit_formatted(
                stale_message,
                rendered,
                reply_markup=styled_markup,
                ignore_stale=True,
            )
        assert result is stale_message
        bot_text.assert_awaited_once_with(
            chat_id=stale_message.chat.id,
            message_id=stale_message.id,
            text=rendered["text"],
            entities=rendered["entities"],
            reply_markup=styled_markup,
        )
        bot_caption.assert_awaited_once_with(
            chat_id=stale_message.chat.id,
            message_id=stale_message.id,
            caption=rendered["text"],
            caption_entities=rendered["entities"],
            reply_markup=styled_markup,
        )
        log_exception.assert_not_called()

    asyncio.run(verify())


def test_soundcloud_proxy_and_video_guards():
    from unittest.mock import AsyncMock, patch

    from AnonX_3.core.resolver import fallback as fallback_module
    from AnonX_3.core.resolver.soundcloud import (
        SoundCloudResolver,
        SoundCloudTransportError,
        soundcloud,
    )
    from AnonX_3.helpers import Track

    calls: list[str] = []

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = dict(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            proxy = self.opts.get("proxy")
            calls.append(proxy)
            if proxy:
                raise OSError("Tunnel connection failed: 502 Bad Gateway")
            return None

    async def verify():
        service = SoundCloudResolver()
        proxy = "http://127.0.0.1:40000"
        with (
            patch.object(service, "enabled", return_value=True),
            patch("config.get_youtube_proxy", return_value=proxy),
            patch(
                "AnonX_3.core.resolver.soundcloud.yt_dlp.YoutubeDL",
                FakeYoutubeDL,
            ),
            patch(
                "AnonX_3.core.resolver.soundcloud.logger.warning"
            ) as warning,
        ):
            assert await service.search("no result", limit=1) == []
            assert await service.resolve_url(
                "https://soundcloud.com/example/missing"
            ) is None
        # The proxy fails at the transport boundary, so both search and direct
        # URL resolution perform exactly one explicit direct retry.
        assert calls == [proxy, "", proxy, ""]
        warning.assert_not_called()
        wrapped_proxy_error = RuntimeError("extract failed")
        wrapped_proxy_error.__cause__ = OSError(
            "407 Proxy Authentication Required (SOCKS5)"
        )
        assert service._is_proxy_transport_failure(wrapped_proxy_error)

        # Empty/opaque proxy results must not terminate download or direct
        # stream resolution before the explicit direct attempt.
        media_calls: list[tuple[bool, str]] = []

        class EmptyProxyYoutubeDL:
            def __init__(self, opts):
                self.opts = dict(opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=False):
                selected_proxy = self.opts.get("proxy")
                media_calls.append((bool(download), selected_proxy))
                if selected_proxy:
                    return None
                if download:
                    return {
                        "requested_downloads": [
                            {"filepath": "downloads/sc_direct.m4a"}
                        ]
                    }
                return {
                    "url": "https://media.example/sc-direct",
                    "acodec": "opus",
                }

        with (
            patch.object(service, "enabled", return_value=True),
            patch("config.get_youtube_proxy", return_value=proxy),
            patch(
                "AnonX_3.core.resolver.soundcloud.yt_dlp.YoutubeDL",
                EmptyProxyYoutubeDL,
            ),
        ):
            assert await service.download(
                "https://soundcloud.com/example/track",
                media_id="sc_direct",
            ) == "downloads/sc_direct.m4a"
            assert await service.resolve_direct_stream(
                "https://soundcloud.com/example/track"
            ) == ("https://media.example/sc-direct", None)
        assert media_calls == [
            (True, proxy),
            (True, ""),
            (False, proxy),
            (False, ""),
        ]

        # Explicit DRM metadata is terminal. Do not retry the same protected
        # item without the proxy or emit it as a transport failure.
        drm_stream_calls: list[str] = []

        class DrmStreamYoutubeDL:
            def __init__(self, opts):
                self.opts = dict(opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=False):
                assert download is False
                drm_stream_calls.append(self.opts.get("proxy"))
                return {
                    "id": "protected",
                    "title": "Protected track",
                    "_has_drm": True,
                }

        with (
            patch.object(service, "enabled", return_value=True),
            patch("config.get_youtube_proxy", return_value=proxy),
            patch(
                "AnonX_3.core.resolver.soundcloud.yt_dlp.YoutubeDL",
                DrmStreamYoutubeDL,
            ),
            patch(
                "AnonX_3.core.resolver.soundcloud.logger.warning"
            ) as warning,
        ):
            assert await service.resolve_direct_stream(
                "https://soundcloud.com/example/protected"
            ) == (None, None)
        assert drm_stream_calls == [proxy]
        warning.assert_not_called()

        # scsearch must stay flat and skip an explicitly marked DRM entry
        # without resolving/aborting the complete result list.
        search_opts: list[dict] = []

        class FlatSearchYoutubeDL:
            def __init__(self, opts):
                self.opts = dict(opts)
                search_opts.append(self.opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                assert url.startswith("scsearch")
                assert download is False
                return {
                    "_type": "playlist",
                    "entries": [
                        {
                            "id": "drm",
                            "title": "Exact Song",
                            "webpage_url": "https://soundcloud.com/example/drm",
                            "_has_drm": True,
                        },
                        {
                            "id": "playable",
                            "title": "Exact Song",
                            "webpage_url": (
                                "https://soundcloud.com/example/playable"
                            ),
                        },
                    ],
                }

        with (
            patch.object(service, "enabled", return_value=True),
            patch("config.get_youtube_proxy", return_value=proxy),
            patch(
                "AnonX_3.core.resolver.soundcloud.yt_dlp.YoutubeDL",
                FlatSearchYoutubeDL,
            ),
        ):
            flat_tracks = await service.search("Exact Song", limit=2)
        assert [track.id for track in flat_tracks] == ["sc_playable"]
        assert len(search_opts) == 1
        assert search_opts[0]["extract_flat"] == "in_playlist"
        assert search_opts[0]["ignoreerrors"] is True
        assert search_opts[0]["retries"] == 0
        assert search_opts[0]["fragment_retries"] == 0

        # A late fallback must preserve the user's original Burmese query.
        # Cross-source uploader/version names may differ, so the exact query
        # can rescue only a title-contained candidate with a close duration.
        media = Track(
            id="youtube-id",
            title="တောက်တီးတောက်တဲ့",
            channel_name="Chan Myae Aung Official",
            duration="03:40",
            duration_sec=220,
            url="https://www.youtube.com/watch?v=youtube-id",
        )
        media.original_query = "တောက်တီးတောက်တဲ့"
        media.normalized_query = "တောက်တီးတောက်တဲ့"
        candidate = Track(
            id="2124078384",
            title="တောက်တီးတောက်တဲ့ & Remix Version ( MOE RMX ).mp3",
            channel_name="MOERMX",
            duration="03:49",
            duration_sec=229.532,
            url=(
                "https://soundcloud.com/k-m-710989858/"
                "remix-version-moe-rmx-mp3-2"
            ),
        )
        assert fallback_module._query_candidates(media)[:2] == [
            "တောက်တီးတောက်တဲ့",
            "Chan Myae Aung Official တောက်တီးတောက်တဲ့",
        ]
        query_search = AsyncMock(return_value=[candidate])
        query_probe = AsyncMock(return_value=(candidate, "ok"))
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(fallback_module.source_health, "allow", return_value=True),
            patch.object(fallback_module.source_health, "success"),
            patch.object(fallback_module.soundcloud, "search", query_search),
            patch.object(
                fallback_module.soundcloud,
                "resolve_url_status",
                query_probe,
            ),
        ):
            track, meta = await fallback_module.find_fallback_track(
                media=media,
                video=False,
            )
        assert track is candidate
        assert meta and meta["reason"] == "ok"
        assert meta["query_rescue"] is True
        assert meta["match_mode"] == "normalized_query_containment"
        assert meta["score"]["total"] < fallback_module.min_score()
        assert [call.args[0] for call in query_search.await_args_list] == [
            "တောက်တီးတောက်တဲ့",
        ]
        query_probe.assert_awaited_once_with(
            candidate.url,
            message_id=0,
            video=False,
        )
        ambiguous = Track(
            id="also-eligible",
            title=candidate.title,
            channel_name=candidate.channel_name,
            duration_sec=candidate.duration_sec,
            url="https://soundcloud.com/example/also-eligible",
        )
        assert fallback_module._pick_strict_query_rescue(
            [candidate, ambiguous],
            query="တောက်တီးတောက်တဲ့",
            seed_artist=media.channel_name,
            seed_duration_sec=media.duration_sec,
        ) == (None, None)

        # A highly scored DRM/private candidate is removed after the bounded
        # probe and the next equally valid playable candidate is selected.
        drm_candidate = Track(
            id="drm-candidate",
            title=media.title,
            channel_name=media.channel_name,
            duration=media.duration,
            duration_sec=media.duration_sec,
            url="https://soundcloud.com/example/drm-candidate",
        )
        playable_candidate = Track(
            id="playable-candidate",
            title=media.title,
            channel_name=media.channel_name,
            duration=media.duration,
            duration_sec=media.duration_sec,
            url="https://soundcloud.com/example/playable-candidate",
        )
        ranked_search = AsyncMock(
            return_value=[drm_candidate, playable_candidate]
        )
        ranked_probe = AsyncMock(
            side_effect=[
                (None, "unplayable"),
                (playable_candidate, "ok"),
            ]
        )
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(fallback_module.source_health, "allow", return_value=True),
            patch.object(fallback_module.source_health, "success") as success,
            patch.object(fallback_module.soundcloud, "search", ranked_search),
            patch.object(
                fallback_module.soundcloud,
                "resolve_url_status",
                ranked_probe,
            ),
        ):
            ranked_track, ranked_meta = await fallback_module.find_fallback_track(
                media=media,
                video=False,
            )
        assert ranked_track is playable_candidate
        assert ranked_meta and ranked_meta["reason"] == "ok"
        assert [call.args[0] for call in ranked_probe.await_args_list] == [
            drm_candidate.url,
            playable_candidate.url,
        ]
        success.assert_called_once()

        # A transport outage is not a normal empty search. It stops the query
        # ladder and records source failure exactly once.
        transport_search = AsyncMock(
            side_effect=SoundCloudTransportError(
                "SoundCloud search unavailable (RETRYABLE)"
            )
        )
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(fallback_module.source_health, "allow", return_value=True),
            patch.object(fallback_module.source_health, "success") as success,
            patch.object(fallback_module.source_health, "failure") as failure,
            patch.object(
                fallback_module.soundcloud,
                "search",
                transport_search,
            ),
        ):
            unavailable_track, unavailable_meta = (
                await fallback_module.find_fallback_track(
                    media=media,
                    video=False,
                )
            )
        assert unavailable_track is None
        assert unavailable_meta == {
            "reason": "source_unavailable",
            "source": "soundcloud",
        }
        assert transport_search.await_count == 1
        success.assert_not_called()
        failure.assert_called_once_with("soundcloud", reason="transport")

        derived_search = AsyncMock(side_effect=[[], [candidate]])
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(fallback_module.source_health, "allow", return_value=True),
            patch.object(fallback_module.source_health, "success"),
            patch.object(
                fallback_module.soundcloud,
                "search",
                derived_search,
            ),
        ):
            derived_track, derived_meta = (
                await fallback_module.find_fallback_track(
                    media=media,
                    video=False,
                )
            )
        assert derived_track is None
        assert derived_meta and derived_meta["reason"] == "no_match"
        media.original_query = "https://youtu.be/youtube-id"
        media.normalized_query = "https://youtu.be/youtube-id"
        assert fallback_module._query_candidates(media)[0] == (
            "Chan Myae Aung Official တောက်တီးတောက်တဲ့"
        )

        # SoundCloud cannot provide a real video stream, so /vplay must not
        # search or download it as a fallback.
        search = AsyncMock()
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(soundcloud, "search", search),
        ):
            track, meta = await fallback_module.find_fallback_track(
                query="video request",
                video=True,
            )
        assert track is None
        assert meta == {"reason": "video_unsupported", "source": "soundcloud"}
        search.assert_not_awaited()
        assert await service.search("video request", video=True) == []
        assert (
            await service.download(
                "https://soundcloud.com/example/track",
                video=True,
            )
            is None
        )

        # A normal query miss proves the provider is reachable. It must close
        # or preserve the source circuit, never increment its failure count.
        with (
            patch.object(fallback_module, "fallback_enabled", return_value=True),
            patch.object(fallback_module.source_health, "allow", return_value=True),
            patch.object(
                fallback_module.soundcloud,
                "search",
                AsyncMock(return_value=[]),
            ),
            patch.object(fallback_module.source_health, "success") as success,
            patch.object(fallback_module.source_health, "failure") as failure,
        ):
            track, meta = await fallback_module.find_fallback_track(
                query="ordinary missing song",
                video=False,
            )
        assert track is None
        assert meta == {
            "reason": "no_candidates",
            "query": "ordinary missing song",
        }
        success.assert_called_once()
        failure.assert_not_called()

    asyncio.run(verify())


def test_direct_stream_codec_and_pyyt_proxy_compatibility():
    from unittest.mock import patch

    from AnonX_3.core.provider.po_token import po_token_provider
    from AnonX_3.core.youtube import YouTube

    class FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            return {
                "url": "https://cdn.example/video-only",
                "acodec": "none",
                "vcodec": "avc1",
                "http_headers": {"User-Agent": "top-agent"},
                "formats": [
                    {
                        "url": "https://cdn.example/audio-ok",
                        "acodec": "mp4a.40.2",
                        "vcodec": "none",
                        "ext": "m4a",
                        "format_id": "140",
                        "protocol": "https",
                        "abr": 128,
                        "http_headers": {"X-Test": "format-header"},
                    }
                ],
            }

    constructor_calls: list[dict] = []

    class MisleadingVideosSearch:
        def __init__(self, _query, **kwargs):
            constructor_calls.append(dict(kwargs))
            if "proxy" in kwargs:
                raise TypeError("unexpected keyword argument 'proxy'")

        async def next(self):
            return {
                "result": [
                    {
                        "id": "AbCdEfGhI12",
                        "title": "Proxy compatible result",
                        "link": "https://www.youtube.com/watch?v=AbCdEfGhI12",
                        "duration": "03:00",
                        "channel": {"name": "Channel"},
                        "thumbnails": [],
                        "viewCount": {"short": "1"},
                    }
                ]
            }

    async def verify():
        service = YouTube()
        with (
            patch.object(service, "_local_ready_path", return_value=None),
            patch.object(service, "get_cookies", return_value=None),
            patch.object(service, "_browser_cookie_spec", return_value=None),
            patch.object(service, "_live_proxy", return_value=None),
            patch.object(po_token_provider, "enabled", return_value=False),
            patch("AnonX_3.core.youtube.yt_dlp.YoutubeDL", FakeYoutubeDL),
        ):
            source = await service.resolve_direct_stream_source(
                "AbCdEfGhI12",
                video=False,
                prefer_remote=True,
            )
            remote, _local = await service._resolve_direct_stream_uncached(
                "AbCdEfGhI12",
                video=False,
            )
        assert source.url == "https://cdn.example/audio-ok"
        assert source.host == "cdn.example"
        assert source.audio_format == "m4a/mp4a.40.2"
        assert source.headers["User-Agent"] == "top-agent"
        assert source.headers["X-Test"] == "format-header"
        assert source.proxy == ""
        assert remote == "https://cdn.example/audio-ok"

        with (
            patch.object(
                service,
                "_live_proxy",
                return_value="http://127.0.0.1:40000",
            ),
            patch(
                "AnonX_3.core.youtube.VideosSearch",
                MisleadingVideosSearch,
            ),
        ):
            tracks = await service._pyyt_search_tracks(
                "proxy compatibility",
                99,
                limit=1,
            )
        assert len(tracks) == 1
        assert len(constructor_calls) == 2
        assert "proxy" in constructor_calls[0]
        assert "proxy" not in constructor_calls[1]

    asyncio.run(verify())


def test_stop_is_idempotent_and_benign_leave_is_terminal():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    import AnonX_3.core.calls as calls_module
    from AnonX_3.core.calls import TgCall

    async def run_stop(service, client, *operations):
        with (
            patch.object(
                service,
                "_delete_now_playing",
                AsyncMock(return_value=None),
            ),
            patch.object(
                calls_module.db,
                "get_assistant",
                AsyncMock(return_value=client),
            ),
            patch.object(
                calls_module.db,
                "remove_call",
                AsyncMock(return_value=None),
            ),
            patch.object(
                calls_module.db,
                "set_loop",
                AsyncMock(return_value=None),
            ),
            patch.object(calls_module.queue, "clear", Mock()),
            patch.object(service.prefetch_manager, "cancel", Mock()),
            patch.object(service.stream_profile, "clear", Mock()),
            patch.object(calls_module.startup_gate, "end", Mock()),
            patch.object(calls_module.direct_watchdog, "disarm", Mock()),
            patch.object(
                calls_module.resource_manager,
                "unregister_stream",
                Mock(),
            ),
        ):
            await asyncio.gather(*operations)

    async def verify():
        chat_id = -100990001
        service = TgCall()
        leave = AsyncMock(return_value=None)
        client = SimpleNamespace(leave_call=leave)
        await run_stop(
            service,
            client,
            service.stop(chat_id),
            service.stop(chat_id),
        )
        leave.assert_awaited_once_with(chat_id, close=False)

        class AlreadyLeft(Exception):
            pass

        chat_id = -100990002
        service = TgCall()
        leave = AsyncMock(side_effect=AlreadyLeft("No active group call"))
        client = SimpleNamespace(leave_call=leave)
        await run_stop(service, client, service.stop(chat_id))
        leave.assert_awaited_once_with(chat_id, close=False)
        assert chat_id in service._stopped_chats

    asyncio.run(verify())


def test_vc_preflight_and_initial_playback_lock_guards():
    from unittest.mock import AsyncMock, patch

    from AnonX_3.core.calls import TgCall

    async def verify():
        service = TgCall()
        chat_id = -100990003

        with patch.object(
            service,
            "_get_input_group_call",
            AsyncMock(side_effect=ValueError("No active group call")),
        ):
            assert await service.has_active_group_call(chat_id) is False

        with patch.object(
            service,
            "_get_input_group_call",
            AsyncMock(return_value=object()),
        ):
            assert await service.has_active_group_call(chat_id) is True

        with patch.object(
            service,
            "_get_input_group_call",
            AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
        ):
            try:
                await service.has_active_group_call(chat_id)
            except RuntimeError as ex:
                assert str(ex) == "Telegram unavailable"
            else:
                raise AssertionError("preflight must not hide operational errors")

        entered: list[str] = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first_request():
            async with service.initial_playback_lock(chat_id):
                entered.append("first")
                first_entered.set()
                await release_first.wait()

        async def second_request():
            async with service.initial_playback_lock(chat_id):
                entered.append("second")

        first = asyncio.create_task(first_request())
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        second = asyncio.create_task(second_request())
        await asyncio.sleep(0)
        assert entered == ["first"]
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        assert entered == ["first", "second"]

    asyncio.run(verify())


def test_stop_recleans_queue_after_no_vc_cleanup():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    import AnonX_3.core.calls as calls_module
    from AnonX_3.core.calls import TgCall

    class NoActiveGroupCall(Exception):
        pass

    async def verify():
        chat_id = -100990004
        service = TgCall()
        leave = AsyncMock(side_effect=NoActiveGroupCall("No active group call"))
        client = SimpleNamespace(leave_call=leave)
        calls_module.queue.clear(chat_id)
        try:
            with (
                patch.object(service, "_delete_now_playing", AsyncMock()),
                patch.object(
                    calls_module.db,
                    "get_assistant",
                    AsyncMock(return_value=client),
                ),
                patch.object(
                    calls_module.db,
                    "remove_call",
                    AsyncMock(return_value=None),
                ) as remove_call,
                patch.object(
                    calls_module.db,
                    "set_loop",
                    AsyncMock(return_value=None),
                ),
                patch.object(service.prefetch_manager, "cancel", Mock()),
                patch.object(service.stream_profile, "clear", Mock()),
                patch.object(calls_module.startup_gate, "end", Mock()),
                patch.object(calls_module.direct_watchdog, "disarm", Mock()),
                patch.object(
                    calls_module.resource_manager,
                    "unregister_stream",
                    Mock(),
                ),
            ):
                # First terminal no-VC cleanup records that leave_call has
                # already completed. A later failed request can still have
                # placed fresh queue state before its own cleanup runs.
                await service.stop(chat_id)
                assert chat_id in service._stopped_chats
                calls_module.queue.add(
                    chat_id,
                    SimpleNamespace(id="late-no-vc-item", priority=50),
                )
                assert calls_module.queue.get_current(chat_id) is not None

                await service.stop(chat_id)
                assert calls_module.queue.get_current(chat_id) is None
                assert calls_module.queue.get_queue(chat_id) == []
                assert remove_call.await_count == 2

            leave.assert_awaited_once_with(chat_id, close=False)
        finally:
            calls_module.queue.clear(chat_id)

    asyncio.run(verify())


def test_vc_watcher_only_cleans_up_on_video_chat_end():
    package_name = "AnonX_3"
    source = (ROOT / package_name / "plugins" / "misc.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    watcher = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_watcher_vc"
    )

    def mentions_filter(decorator, name: str) -> bool:
        return any(
            isinstance(node, ast.Attribute) and node.attr == name
            for node in ast.walk(decorator)
        )

    assert any(
        mentions_filter(decorator, "video_chat_ended")
        for decorator in watcher.decorator_list
    )
    assert not any(
        mentions_filter(decorator, "video_chat_started")
        for decorator in watcher.decorator_list
    )
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "anon"
        and node.value.func.attr == "stop"
        for node in ast.walk(watcher)
    )


def test_queue_remove_request_is_exact_for_duplicate_media_ids():
    from AnonX_3.helpers._dataclass import Media
    from AnonX_3.helpers._queue import Queue

    chat_id = -100990005
    queue_service = Queue()
    active = Media(id="same-video", request_id="active-request")
    cancelled = Media(id="same-video", request_id="cancelled-request")
    later = Media(id="same-video", request_id="later-request")

    queue_service.add(chat_id, active)
    queue_service.add(chat_id, cancelled)
    queue_service.add(chat_id, later)

    assert queue_service.remove_request(chat_id, "cancelled-request") is True
    assert [item.request_id for item in queue_service.get_queue(chat_id)] == [
        "active-request",
        "later-request",
    ]
    assert queue_service.get_current(chat_id) is active
    assert queue_service.remove_request(chat_id, "cancelled-request") is False


def test_play_request_scope_blocks_late_background_work():
    from types import SimpleNamespace

    from AnonX_3.helpers._play import (
        _create_play_request_task,
        _invalidate_play_request,
        _play_request_is_live,
    )

    async def verify():
        message_id = 990006
        started = asyncio.Event()

        async def stale_worker():
            started.set()
            await asyncio.Event().wait()

        task = _create_play_request_task(
            message_id,
            stale_worker(),
            name="test-stale-play-worker",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert _play_request_is_live(message_id) is True
        await _invalidate_play_request(SimpleNamespace(id=message_id))
        assert task.cancelled()
        assert _play_request_is_live(message_id) is False

    asyncio.run(verify())


def test_stream_media_vc_admission_transaction_guards():
    """Exercise no-VC, retry, concurrent-start, and Cancel rollback invariants."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    import AnonX_3.helpers._play as play_module
    from AnonX_3.core.bot_api import BotAPI
    from AnonX_3.helpers._dataclass import Media

    chat_id = -100990006
    no_call_text = "NO ACTIVE VC"
    language = {
        "error_no_call": no_call_text,
        "play_duration_limit": "limit {}",
        "play_downloading": "Downloading",
        "play_queued": "queued {position}",
        "playlist_queued": "playlist {}",
        "close": "Close",
        "cancel": "Cancel",
        "error_no_file": "missing",
        "play_error": "error",
        "error_no_video": "no video",
    }

    async def verify():
        active = False
        lock = asyncio.Lock()

        @asynccontextmanager
        async def initial_lock(_chat_id):
            async with lock:
                yield

        async def get_call(_chat_id):
            return active

        edit = AsyncMock(return_value=None)
        has_vc = AsyncMock(return_value=False)
        ready_media = lambda item_id: Media(
            id=item_id,
            title=item_id,
            duration="03:00",
            file_path="downloads/ready.m4a",
        )
        sent = SimpleNamespace(id=0, link=None, reply_markup=None)

        with (
            patch.object(play_module.db, "get_lang", AsyncMock(return_value="en")),
            patch.object(play_module.db, "is_logger", AsyncMock(return_value=False)),
            patch.object(play_module.db, "get_call", AsyncMock(side_effect=get_call)),
            patch.object(play_module.anon, "initial_playback_lock", initial_lock),
            patch.object(play_module.anon, "has_active_group_call", has_vc),
            patch.object(play_module.anon, "play_media", AsyncMock()),
            patch.object(play_module.utils, "edit_text", edit),
            patch.object(
                play_module,
                "_auto_delete_play_command",
                AsyncMock(return_value=None),
            ),
            patch.object(play_module, "_schedule_queued_prefetch", Mock()),
        ):
            # Two no-VC commands are terminal errors and never leave queue state.
            await play_module.stream_media(chat_id, sent, ready_media("no-vc-1"), _lang=language)
            await play_module.stream_media(chat_id, sent, ready_media("no-vc-2"), _lang=language)
            assert play_module.queue.get_queue(chat_id) == []
            assert has_vc.await_count == 2
            assert [call.args[1] for call in edit.await_args_list] == [
                no_call_text,
                no_call_text,
            ]

            # After a user starts the VC, the next request becomes position zero
            # and starts directly; it must not render a queued result card.
            has_vc.reset_mock()
            edit.reset_mock()
            has_vc.return_value = True

            async def commit_play(**_kwargs):
                nonlocal active
                active = True

            with patch.object(
                play_module.anon, "play_media", AsyncMock(side_effect=commit_play)
            ) as play_media:
                started = ready_media("fresh-vc-start")
                await play_module.stream_media(chat_id, sent, started, _lang=language)
                assert play_module.queue.get_current(chat_id) is started
                play_media.assert_awaited_once()
                edit.assert_not_awaited()

            # Telegram can end the VC after preflight but before PyTgCalls
            # confirms playback. That late no-VC path is terminal and leaves
            # neither a queue head nor a partially-started call behind.
            play_module.queue.clear(chat_id)
            active = False
            has_vc.reset_mock()
            has_vc.return_value = True
            late_playlist_tail = ready_media("late-playlist-tail")
            with (
                patch.object(play_module.anon, "play_media", AsyncMock()),
                patch.object(play_module.anon, "stop", AsyncMock()) as stop,
            ):
                await play_module.stream_media(
                    chat_id,
                    sent,
                    ready_media("vc-closed-late"),
                    tracks=[late_playlist_tail],
                    _lang=language,
                )
                assert play_module.queue.get_queue(chat_id) == []
                stop.assert_awaited_once_with(chat_id)

            # Two simultaneous first-play commands serialize. The second is
            # admitted only after the first commits an active bot call.
            play_module.queue.clear(chat_id)
            active = False
            has_vc.reset_mock()
            has_vc.return_value = True
            edit.reset_mock()
            first_play_entered = asyncio.Event()
            release_first_play = asyncio.Event()

            async def delayed_commit(**_kwargs):
                nonlocal active
                first_play_entered.set()
                await release_first_play.wait()
                active = True

            with (
                patch.object(
                    play_module.anon,
                    "play_media",
                    AsyncMock(side_effect=delayed_commit),
                ) as play_media,
                patch.object(
                    play_module.db,
                    "get_custom_text",
                    AsyncMock(return_value="queued {position}"),
                ),
                patch.object(
                    play_module.utils,
                    "normalize_template_entities",
                    AsyncMock(return_value="queued {position}"),
                ),
            ):
                first = asyncio.create_task(
                    play_module.stream_media(
                        chat_id, sent, ready_media("first"), _lang=language
                    )
                )
                await asyncio.wait_for(first_play_entered.wait(), timeout=1)
                second_media = ready_media("second")
                second = asyncio.create_task(
                    play_module.stream_media(chat_id, sent, second_media, _lang=language)
                )
                await asyncio.sleep(0)
                assert len(play_module.queue.get_queue(chat_id)) == 1
                release_first_play.set()
                await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
                assert play_media.await_count == 1
                assert play_module.queue.get_current(chat_id).id == "first"
                assert play_module.queue.get_queue(chat_id)[1] is second_media
                assert all(
                    call.args[1] != no_call_text for call in edit.await_args_list
                )

            # A user (or another status owner) may delete the SEARCHING card
            # after queue admission but before the Queued edit. Presentation
            # loss must not be reclassified as media startup failure or remove
            # the already-admitted track.
            play_module.queue.clear(chat_id)
            current_media = ready_media("already-playing")
            stale_media = ready_media("stale-card-queued")
            play_module.queue.add(chat_id, current_media)
            stale_sent = SimpleNamespace(
                id=990008,
                link=None,
                reply_markup=None,
                chat=SimpleNamespace(id=chat_id),
            )
            edit.reset_mock()
            edit.side_effect = BotAPI.MessageToEditNotFound(
                "Bad Request: message to edit not found"
            )
            try:
                with (
                    patch.object(
                        play_module.db,
                        "get_custom_text",
                        AsyncMock(return_value="queued {position}"),
                    ),
                    patch.object(
                        play_module.utils,
                        "normalize_template_entities",
                        AsyncMock(return_value="queued {position}"),
                    ),
                ):
                    await play_module._admit_and_stream_media(
                        chat_id,
                        stale_sent,
                        stale_media,
                        tracks=[],
                        force=False,
                        _lang=language,
                        log_msg=None,
                        trace=None,
                        lang_code="en",
                        initial_start=False,
                    )
            finally:
                edit.side_effect = None
            assert play_module.queue.get_queue(chat_id) == [
                current_media,
                stale_media,
            ]
            assert stale_media.message_id == 0
            assert getattr(stale_media, "status_message_id", 0) == 0

            # Cancellation while the initial Downloading card is awaiting an
            # edit removes only this provisional request, before play_media.
            play_module.queue.clear(chat_id)
            active = False
            has_vc.return_value = True
            progress_entered = asyncio.Event()
            never_finish_progress = asyncio.Event()

            async def block_progress(*_args, **_kwargs):
                progress_entered.set()
                await never_finish_progress.wait()

            with (
                patch.object(
                    play_module.db,
                    "get_custom_text",
                    AsyncMock(return_value="Downloading"),
                ),
                patch.object(
                    play_module.utils,
                    "edit_download_progress",
                    AsyncMock(side_effect=block_progress),
                ),
                patch.object(play_module.anon, "play_media", AsyncMock()) as play_media,
            ):
                cancelling_media = Media(id="cancel-me", title="cancel-me")
                cancelled = asyncio.create_task(
                    play_module.stream_media(
                        chat_id, sent, cancelling_media, _lang=language
                    )
                )
                await asyncio.wait_for(progress_entered.wait(), timeout=1)
                cancelled.cancel()
                try:
                    await cancelled
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("startup cancellation must propagate")
                assert play_module.queue.get_queue(chat_id) == []
                play_media.assert_not_awaited()
        play_module.queue.clear(chat_id)

    asyncio.run(verify())


def test_ytdlp_first_constructor_is_singleflight():
    """Only cold global plugin registration is serialized; later work overlaps."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    ytdlp_runtime = _direct_load_module(
        ROOT / "AnonX_3" / "core" / "ytdlp_runtime.py",
        "ytdlp_runtime",
    )

    initial_ready = ytdlp_runtime._first_constructor_ready.is_set()
    ytdlp_runtime._first_constructor_ready.clear()
    entered = 0
    entered_lock = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    start_barrier = threading.Barrier(6)
    later_barrier = threading.Barrier(5)

    def fake_constructor(options):
        nonlocal entered
        with entered_lock:
            entered += 1
            ordinal = entered
        if ordinal == 1:
            first_entered.set()
            if not release_first.wait(timeout=2):
                raise AssertionError("first constructor release timed out")
        else:
            # Every post-bootstrap constructor must be able to enter together;
            # the gate must not serialize extraction runtimes permanently.
            later_barrier.wait(timeout=2)
        return options["slot"]

    def construct(slot: int):
        start_barrier.wait(timeout=2)
        return ytdlp_runtime.create_youtube_dl(
            {"slot": slot}, fake_constructor
        )

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(construct, slot) for slot in range(6)]
            assert first_entered.wait(timeout=2)
            time.sleep(0.05)
            assert entered == 1
            release_first.set()
            assert sorted(future.result(timeout=2) for future in futures) == list(
                range(6)
            )
        assert entered == 6
        assert ytdlp_runtime._first_constructor_ready.is_set()
    finally:
        release_first.set()
        if initial_ready:
            ytdlp_runtime._first_constructor_ready.set()
        else:
            ytdlp_runtime._first_constructor_ready.clear()


def test_stale_queued_status_is_noncritical_without_runtime_deps():
    """Execute the real queue-admission branch with dependency-free fakes."""
    from types import SimpleNamespace

    class StaleEdit(RuntimeError):
        pass

    class FakeQueue:
        def __init__(self):
            self.items = [SimpleNamespace(request_id="current")]

        def add(self, _chat_id, media):
            self.items.append(media)
            return len(self.items) - 1

        def get_current(self, _chat_id):
            return self.items[0] if self.items else None

        def get_next(self, _chat_id, *, check=False):
            return self.items[1] if len(self.items) > 1 else None

    class FakeUtils:
        def __init__(self):
            self.closed = []

        @staticmethod
        def is_stale_edit_error(ex):
            return isinstance(ex, StaleEdit)

        @staticmethod
        def is_quiet_edit_error(ex):
            return isinstance(ex, StaleEdit)

        @staticmethod
        async def normalize_template_entities(_key, template, **_kwargs):
            return template

        @staticmethod
        async def edit_text(*_args, **_kwargs):
            raise StaleEdit("Bad Request: message to edit not found")

        async def close_download_progress(self, message, media):
            self.closed.append((message.id, media.request_id))

    class FakeDB:
        @staticmethod
        async def get_custom_text(_key, default, _lang_code):
            return default

    class NoVideoSourceFound(Exception):
        pass

    queue = FakeQueue()
    utils = FakeUtils()
    detached = []
    scheduled = []
    namespace = {
        "asyncio": asyncio,
        "types": SimpleNamespace(Message=object),
        "PlaybackTrace": object,
        "queue": queue,
        "utils": utils,
        "db": FakeDB(),
        "yt": SimpleNamespace(
            detach_download_progress=lambda message: detached.append(message.id)
        ),
        "buttons": SimpleNamespace(
            play_queued=lambda *_args: {"inline_keyboard": []},
            support_button=lambda: None,
            cancel_dl=lambda _label: None,
        ),
        "logger": SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            exception=lambda *_args, **_kwargs: None,
        ),
        "config": SimpleNamespace(AUTO_DELETE_PLAY_QUEUED=False),
        "call_exceptions": SimpleNamespace(
            NoVideoSourceFound=NoVideoSourceFound
        ),
        "format_play_queued_template": lambda *_args, **_kwargs: "queued",
        "_schedule_queued_prefetch": lambda chat_id: scheduled.append(chat_id),
        "_append_playlist_notice": lambda *_args, **_kwargs: asyncio.sleep(0),
        "_auto_delete_play_command": lambda *_args, **_kwargs: asyncio.sleep(0),
        "_rollback_admitted_media": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale presentation must not roll back queue state")
        ),
        "_invalidate_play_request": lambda *_args, **_kwargs: asyncio.sleep(0),
        "_prefers_direct_start": lambda _media: True,
    }
    _exec_source_definitions(
        ROOT / "AnonX_3" / "helpers" / "_play.py",
        {
            "_run_noncritical_play_operation",
            "_edit_queued_status_card",
            "_retire_stale_play_status",
            "_request_id_for",
            "_admit_and_stream_media",
        },
        namespace,
    )

    media = SimpleNamespace(
        request_id="stale-queued",
        title="Queued",
        url="https://example.invalid/media",
        message_id=991234,
        file_path="ready.m4a",
        source=None,
    )
    sent = SimpleNamespace(
        id=991234,
        chat=SimpleNamespace(id=-100991234),
        reply_markup=None,
    )

    async def verify():
        await namespace["_admit_and_stream_media"](
            -100991234,
            sent,
            media,
            tracks=[],
            force=False,
            _lang={
                "play_queued": "queued {position}",
                "close": "Close",
                "error_no_file": "missing",
                "play_error": "error",
                "error_no_video": "no video",
            },
            log_msg=None,
            trace=None,
            lang_code="en",
            initial_start=False,
        )

    asyncio.run(verify())
    assert queue.items[-1] is media
    assert media.message_id == 0
    assert media.status_message_id == 0
    assert utils.closed == [(sent.id, media.request_id)]
    assert detached == [sent.id]
    assert scheduled == [-100991234]


def test_youtube_auth_challenge_short_circuit():
    from unittest.mock import AsyncMock, patch

    from AnonX_3 import config
    from AnonX_3.core import youtube as youtube_module
    from AnonX_3.core.resolver.error_classifier import (
        ErrorClass,
        classify_error,
        should_fallback_source,
        should_retry,
    )
    from AnonX_3.core.youtube import YouTube

    message = (
        "ERROR: [youtube] ApPj1Oprm70: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    classified = classify_error(message)
    assert classified.cls == ErrorClass.AUTH_CHALLENGE
    assert classified.retryable is False
    assert should_retry(classified, attempt=1, max_attempts=3) is False
    assert should_fallback_source(classified) is True

    format_error = classify_error(
        "ERROR: [youtube] Requested format is not available."
    )
    assert format_error.cls == ErrorClass.FORMAT
    assert format_error.retryable is True
    assert classify_error("No video formats found").cls == ErrorClass.FORMAT

    service = YouTube()
    forced_clients = {
        "format": "bestaudio",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["translated_subs"],
            }
        },
    }
    authenticated = service._authenticated_default_client_opts(forced_clients)
    assert authenticated["extractor_args"]["youtube"] == {
        "skip": ["translated_subs"]
    }
    assert forced_clients["extractor_args"]["youtube"]["player_client"] == [
        "android",
        "web",
    ]
    token_bound = service._authenticated_default_client_opts(
        {
            "extractor_args": {
                "youtube": {
                    "player_client": ["mweb"],
                    "po_token": ["mweb.gvs+opaque-test-token"],
                }
            }
        }
    )
    assert token_bound["extractor_args"]["youtube"]["player_client"] == ["mweb"]
    provider_bound = service._authenticated_default_client_opts(
        {
            "extractor_args": {
                "youtubepot-bgutilhttp": {
                    "base_url": ["http://127.0.0.1:4416"]
                },
                "youtube": {"player_client": ["mweb"]},
            }
        }
    )
    assert provider_bound["extractor_args"]["youtube"]["player_client"] == [
        "mweb"
    ]

    with (
        patch.object(config, "YOUTUBE_AUTH_CHALLENGE_COOLDOWN_SEC", 180),
        patch("AnonX_3.core.youtube.time.monotonic", return_value=1000.0),
    ):
        service._remember_auth_challenge(message)
        assert service.auth_challenge_active() is True
    with patch("AnonX_3.core.youtube.time.monotonic", return_value=1181.0):
        assert service.auth_challenge_active() is False

    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    prefetch_source = (
        ROOT / "AnonX_3" / "core" / "prefetch.py"
    ).read_text(encoding="utf-8")
    calls_source = (
        ROOT / "AnonX_3" / "core" / "calls.py"
    ).read_text(encoding="utf-8")
    assert "youtube auth challenge circuit skip" in youtube_source
    assert "youtube_authenticated_runtime_failed=True " in youtube_source
    assert '"action=download video_id=%s class=%s msg=%s"' in youtube_source
    assert "async def _execute_download_strategy(opts: dict)" in youtube_source
    assert "await _execute_download_strategy(" in youtube_source
    assert 'reason="download-auth-challenge"' not in youtube_source
    assert "recovery=browser_authenticated" not in youtube_source
    assert youtube_source.count("_remember_auth_challenge(") >= 2
    # A one-shot failure is terminal for the request instead of opening the
    # historical prefetch/quality retry ladders.
    assert "_cache_one_shot_attempted" in prefetch_source
    assert "one_shot=True" in prefetch_source
    assert calls_source.count("yt.auth_challenge_active()") >= 1

    async def verify_auth_challenge_stops_duplicate_downloads():
        auth_error = (
            "ERROR: [youtube] transport1: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )

        class FakeYoutubeDL:
            calls: list[dict] = []

            def __init__(self, opts):
                self.opts = dict(opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def download(self, _urls):
                type(self).calls.append(self.opts)
                raise RuntimeError(auth_error)

        recovery_service = YouTube()
        refresh = AsyncMock(return_value=None)
        with (
            patch.object(config, "YTDLP_MAX_RETRIES", 3),
            patch.object(recovery_service, "get_cookies", return_value=None),
            patch.object(
                recovery_service, "_browser_cookie_spec", return_value=None
            ),
            patch.object(
                recovery_service, "cookie_free_mode", return_value=False
            ),
            patch.object(
                recovery_service,
                "auth_cookie_recovery_enabled",
                return_value=True,
            ),
            patch.object(
                recovery_service, "refresh_local_cookies", refresh
            ),
            patch.object(
                youtube_module.resource_manager,
                "allow_new_heavy_job",
                return_value=True,
            ),
            patch.object(
                youtube_module.yt_dlp, "YoutubeDL", FakeYoutubeDL
            ),
        ):
            result = await recovery_service.download("transport1")

        assert result is None
        assert len(FakeYoutubeDL.calls) == 1
        assert FakeYoutubeDL.calls[0].get("cookiefile") == "/root/youtube-cookies.txt"
        assert recovery_service.auth_challenge_for("transport1") is True
        refresh.assert_not_awaited()

    asyncio.run(verify_auth_challenge_stops_duplicate_downloads())


def test_direct_youtube_metadata_propagation():
    """Direct /play cards reuse metadata without a pre-download extraction."""
    from unittest.mock import patch

    from AnonX_3.core.youtube import YouTube

    service = YouTube()
    video_id = "pBLm7qJ8410"
    metadata = service._metadata_from_direct_info(
        {
            "title": "Detected YouTube Title",
            "duration": 185.9,
            "channel": "Detected Channel",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "view_count": 1234,
        },
        video_id,
    )
    assert metadata is not None
    assert metadata["duration"] == "3:05"
    assert metadata["duration_sec"] == 185

    with patch("AnonX_3.core.youtube.time.monotonic", return_value=100.0):
        service._direct_metadata_cache[video_id] = (200.0, metadata)
        audio = service._track_from_direct_metadata(video_id, 77, video=False)
        video = service._track_from_direct_metadata(video_id, 78, video=True)

    for track, expected_video in ((audio, False), (video, True)):
        assert track is not None
        assert track.title == "Detected YouTube Title"
        assert track.duration == "3:05"
        assert track.duration_sec == 185
        assert track.video is expected_video
        assert track.url.endswith(video_id)

    async def verify_direct_search_handoff():
        handoff = YouTube()

        async def no_lightweight_metadata(*_args, **_kwargs):
            return []

        handoff._direct_metadata_cache[video_id] = (float("inf"), metadata)

        with (
            patch.object(
                handoff,
                "_pyyt_search_tracks",
                side_effect=no_lightweight_metadata,
            ),
            patch.object(
                handoff,
                "resolve_direct_stream",
                side_effect=AssertionError("direct extraction must not run"),
            ),
        ):
            resolved = await handoff._search_uncached(
                f"https://www.youtube.com/watch?v={video_id}",
                91,
                False,
            )
        assert resolved is not None
        assert resolved.title == "Detected YouTube Title"
        assert resolved.duration == "3:05"
        assert resolved.duration_sec == 185

    asyncio.run(verify_direct_search_handoff())

    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    direct_search_source = youtube_source[
        youtube_source.index("async def _search_uncached(") : youtube_source.index(
            "# ── TEXT SEARCH",
            youtube_source.index("async def _search_uncached("),
        )
    ]
    assert "await self.resolve_direct_stream(" not in direct_search_source
    assert "youtube_path=direct_metadata" in youtube_source
    assert "timeout=0.35" in youtube_source


def test_fast_release_hot_paths():
    """Regression coverage for search, response and download hot paths."""
    from time import perf_counter
    from unittest.mock import patch

    from AnonX_3.core.mongo import MongoDB
    from AnonX_3.core.youtube import YouTube
    from AnonX_3.helpers import Track

    class FakeChats:
        def __init__(self):
            self.find_calls = 0
            self.update_calls = 0

        async def find_one(self, _query):
            self.find_calls += 1
            return None

        async def update_one(self, *_args, **_kwargs):
            self.update_calls += 1

    async def verify_mongo_negative_cache():
        service = MongoDB.__new__(MongoDB)
        service.admin_play = []
        service._admin_play_known = set()
        service.chatsdb = FakeChats()
        assert await service.get_play_mode(-1001) is False
        assert await service.get_play_mode(-1001) is False
        assert service.chatsdb.find_calls == 1
        await service.set_play_mode(-1001)
        assert await service.get_play_mode(-1001) is True
        assert service.chatsdb.find_calls == 1

    async def verify_search_coalescing_and_cache():
        service = YouTube()
        calls = {"count": 0}

        async def provider(
            _query, _m_id, video=False, limit=5, *, allow_ytdlp=True
        ):
            calls["count"] += 1
            await asyncio.sleep(0.05)
            return [
                Track(
                    id="AbCdEfGhI12",
                    title="Fast result",
                    channel_name="Test",
                    duration="1:00",
                    duration_sec=60,
                    message_id=0,
                    thumbnail="",
                    url="https://www.youtube.com/watch?v=AbCdEfGhI12",
                    view_count="",
                    video=video,
                )
            ][:limit]

        service._deep_search_uncached = provider
        cold_start = perf_counter()
        cold = await asyncio.gather(
            *[
                service.deep_search("same query", index, limit=5)
                for index in range(1, 9)
            ]
        )
        cold_elapsed = perf_counter() - cold_start
        assert calls["count"] == 1
        assert [batch[0].message_id for batch in cold] == list(range(1, 9))

        warm_start = perf_counter()
        for index in range(20, 40):
            cached = await service.deep_search("same query", index, limit=5)
            assert cached[0].message_id == index
        warm_per_call = (perf_counter() - warm_start) / 20
        ratio = cold_elapsed / max(warm_per_call, 0.000001)
        assert ratio >= 10.0, ratio
        print(f"BENCH deep_search_cache_speedup={ratio:.1f}x")

        miss_calls = {"count": 0}

        async def miss(_query, _m_id, _video):
            miss_calls["count"] += 1
            return None

        service._search_uncached = miss
        assert await service.search("missing query", 1) is None
        assert await service.search("missing query", 2) is None
        assert miss_calls["count"] == 1

    async def verify_api_session_reuse():
        service = YouTube()
        created = []

        class FakeSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                created.append(self)

            async def close(self):
                self.closed = True

        with (
            patch.object(service, "_is_socks_proxy", return_value=False),
            patch("AnonX_3.core.youtube.aiohttp.ClientSession", FakeSession),
        ):
            first = await service._youtube_client_session()
            second = await service._youtube_client_session()
            assert first is second
            assert len(created) == 1
            await service.close()
            assert first.closed is True

    asyncio.run(verify_mongo_negative_cache())
    asyncio.run(verify_search_coalescing_and_cache())
    asyncio.run(verify_api_session_reuse())

    play_helper = (
        ROOT / "AnonX_3" / "helpers" / "_play.py"
    ).read_text(encoding="utf-8")
    play_plugin = (
        ROOT / "AnonX_3" / "plugins" / "play.py"
    ).read_text(encoding="utf-8")
    youtube_source = (
        ROOT / "AnonX_3" / "core" / "youtube.py"
    ).read_text(encoding="utf-8")
    init_source = (
        ROOT / "AnonX_3" / "__init__.py"
    ).read_text(encoding="utf-8")
    thumbnail_source = (
        ROOT / "AnonX_3" / "helpers" / "_thumbnails.py"
    ).read_text(encoding="utf-8")
    telegram_source = (
        ROOT / "AnonX_3" / "core" / "telegram.py"
    ).read_text(encoding="utf-8")
    assert play_helper.index('trace.mark("ack")') < play_helper.index(
        "play_mode = await db.get_play_mode(chat_id)"
    )
    assert 'if getattr(sent, "reply_markup", None) is None:' in play_plugin
    assert "timeout=0.35" in youtube_source
    assert "if self.is_complete_media_file(result, min_bytes=min_bytes)" in youtube_source
    assert '("yt.close", yt.close)' in init_source
    assert "extracted_thumb = await asyncio.to_thread(" in thumbnail_source
    assert "if local_path and await asyncio.to_thread(" in telegram_source


def test_ai_assistant_accuracy_agent_guards():
    assistant_source = (
        ROOT / "AnonX_3" / "plugins" / "bot_assistant.py"
    ).read_text(encoding="utf-8")
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")

    # A second, low-temperature agent reviews substantive replies without tools.
    assert "async def _run_accuracy_agent(" in assistant_source
    assert '"temperature": 0.15' in assistant_source
    assert '"tools": BOT_TOOLS' in assistant_source
    assert "verified_tool_results" in assistant_source
    assert "_review_preserves_literals(draft, reviewed)" in assistant_source
    assert "_CASUAL_ONLY_RE.fullmatch(question) is None" in assistant_source

    # The primary reply must not wait for the second model round trip.
    ask_source = assistant_source.split(
        "async def _ask_deepseek_dynamic(", 1
    )[1].split("# ── Fallback", 1)[0]
    assert "await _run_accuracy_agent(" not in ask_source
    assert "verified_context_results + tool_results" in ask_source
    assert "async def _review_answer_in_background(" in assistant_source
    assert "def _schedule_accuracy_review(" in assistant_source
    assert "asyncio.create_task(" in assistant_source
    assert "_MAX_ACCURACY_TASKS = 32" in assistant_source
    assert assistant_source.index("sent_message = await message.reply_text(") < (
        assistant_source.index("_schedule_accuracy_review(", assistant_source.index(
            "sent_message = await message.reply_text("
        ))
    )

    # Primary and background reviewer each use their own bounded session.
    assert assistant_source.count(
        "aiohttp.ClientSession(timeout=timeout)"
    ) == 2
    assert "DEEPSEEK_ASSISTANT_TIMEOUT_SEC" in config_source
    assert "DEEPSEEK_REVIEW_ENABLED" in config_source
    assert "DEEPSEEK_REVIEW_MODEL" in config_source
    assert "DEEPSEEK_REVIEW_TIMEOUT_SEC" in config_source
    assert "DEEPSEEK_FAST_TIMEOUT_SEC" in config_source
    assert "DEEPSEEK_FAST_MODEL" in config_source
    assert '"deepseek-v4-flash"' in config_source
    assert '"thinking": {"type": "disabled"}' in assistant_source

    # Every turn is a semantic streaming agent; no keyword gate selects tools/roles.
    assert "def _build_fast_system_prompt(" in assistant_source
    assert "async def _stream_fast_answer(" in assistant_source
    assert 'stream_payload["stream"] = True' in assistant_source
    assert "AI streaming first content delivered" in assistant_source
    assert "streamed_tool_calls" in assistant_source
    assert 'delta_payload.get("tool_calls")' in assistant_source
    assert "if sent_message is None:" in assistant_source
    assert '"tools": BOT_TOOLS' in ask_source
    assert '"tool_choice": "auto"' in ask_source
    assert 'model = getattr(config, "DEEPSEEK_FAST_MODEL"' in ask_source
    assert "*ctx[-8:]" in ask_source
    assert "_BOT_TOOL_INTENT_RE" not in assistant_source
    assert "_message_needs_bot_tools" not in assistant_source
    assert "_OWNER_INTENT_RE" not in assistant_source
    assert "never a keyword table" in assistant_source
    assert "USER-LED DYNAMIC ROLE" in assistant_source
    assert 'await _execute_bot_tool("get_owner_info", {}, message)' in assistant_source
    assert "PRIVATE VERIFIED OWNER CONTEXT" in assistant_source
    assert '{"role": "assistant", "content": "", "tool_calls": tool_calls}' in ask_source
    assert "*tool_results" in ask_source
    assert "VERIFIED LIVE OWNER PROFILE (public fields only)" in assistant_source
    assert "owner.first_name, owner.last_name" in assistant_source
    assert "https://t.me/{owner_username}" in assistant_source
    assert "phone number, or other private details" in assistant_source
    assert "Determine owner tone afresh" in assistant_source
    assert "Do not automatically praise, defend, criticize, or flatter the owner" in assistant_source
    assert "If verified facts genuinely support praise" in assistant_source
    assert "not a command to praise or defend the owner" in assistant_source
    assert "Reply warmly and positively in the user's language" not in assistant_source
    assert "group=21" in assistant_source
    handler_source = assistant_source.split(
        "async def _bot_assistant(", 1
    )[1]
    assert "send_chat_action" not in handler_source

    # Conversation memory cannot grow forever or survive indefinitely.
    assert "_MAX_CONTEXT_USERS = 500" in assistant_source
    assert "_CONTEXT_TTL_SEC = 6 * 60 * 60" in assistant_source
    assert "_USER_CONTEXTS.popitem(last=False)" in assistant_source

    # Runtime logs/status remain inaccessible to ordinary AI-chat users.
    assert "Denied AI diagnostic tool access" in assistant_source
    assert "Diagnostic status is available only to bot administrators." in assistant_source


def test_log_error_hardening_guards():
    """Regression coverage for the recurring failures found in log.txt."""
    from AnonX_3.core.bot_api import BotAPI
    from AnonX_3.core.cookie_watcher import ChromiumCookieWatcher
    from AnonX_3.core import error_monitor
    from AnonX_3.helpers import utils
    import AnonX_3.helpers._play as play_module
    import AnonX_3.plugins.bot_assistant as assistant_module

    assert play_module._download_progress_template({}) == "Downloading"
    assert play_module._download_progress_template({"play_downloading": "Downloading"}) == "Downloading"

    with tempfile.TemporaryDirectory() as temp_dir:
        watcher = ChromiumCookieWatcher(temp_dir, str(Path(temp_dir) / "cookies.txt"))
        asyncio.run(watcher._sync_once())
        asyncio.run(watcher._sync_once())
        assert watcher._missing_db_reported is True

    assert utils.is_chat_forbidden_error(
        BotAPI.ChatForbidden("CHAT_SEND_PLAIN_FORBIDDEN")
    )
    stale_markup_error = (
        "Bot API editMessageReplyMarkup failed: {'ok': False, "
        "'error_code': 400, 'description': \"Bad Request: message can't be edited\"}"
    )
    assert BotAPI._is_stale_edit_error("Bad Request: message can't be edited")
    assert not BotAPI._is_stale_edit_error("Bad Request: chat not found")
    assert BotAPI._is_chat_forbidden_error("Bad Request: chat not found")
    assert utils.is_chat_forbidden_error(
        BotAPI.ChatForbidden("Bad Request: chat not found")
    )
    assert utils.is_stale_edit_error(
        BotAPI.MessageToEditNotFound("Bad Request: message can't be edited")
    )
    bot_api_source = (ROOT / "AnonX_3" / "core" / "bot_api.py").read_text(encoding="utf-8")
    assert "if self._is_chat_forbidden_error(desc):" in bot_api_source
    assert bot_api_source.index("if self._is_chat_forbidden_error(desc):") < bot_api_source.index(
        "if self._is_stale_edit_error(desc):"
    )
    assert 'or "not found" in text' not in bot_api_source
    utilities_source = (ROOT / "AnonX_3" / "helpers" / "_utilities.py").read_text(encoding="utf-8")
    assert "if self.is_chat_forbidden_error(ex):\n                    raise" in utilities_source
    assert utilities_source.index("if self.is_chat_forbidden_error(ex):") < utilities_source.index(
        'logger.exception("Failed to send formatted reply with entities: %s", ex)'
    )
    assert error_monitor._is_bot_api_benign_edit_text(stale_markup_error)
    assert utils.sanitize_entities_for_text(
        "abc", [{"type": "bold", "offset": 0, "length": 99}]
    )[0]["length"] == 3

    original_key = getattr(assistant_module.config, "DEEPSEEK_API_KEY", "")
    original_cooldown = getattr(
        assistant_module.config, "DEEPSEEK_AUTH_FAILURE_COOLDOWN_SEC", 3600
    )
    original_auth_key = assistant_module._AI_AUTH_KEY
    original_until = assistant_module._AI_AUTH_DISABLED_UNTIL
    original_warning = assistant_module._AI_AUTH_WARNING_EMITTED
    try:
        assistant_module.config.DEEPSEEK_API_KEY = "invalid-test-key"
        assistant_module.config.DEEPSEEK_AUTH_FAILURE_COOLDOWN_SEC = 300
        assistant_module._AI_AUTH_KEY = None
        assistant_module._AI_AUTH_WARNING_EMITTED = False
        assert assistant_module._ai_auth_available() is True
        assistant_module._disable_ai_for_auth_failure()
        assert assistant_module._ai_auth_available() is False
    finally:
        assistant_module.config.DEEPSEEK_API_KEY = original_key
        assistant_module.config.DEEPSEEK_AUTH_FAILURE_COOLDOWN_SEC = original_cooldown
        assistant_module._AI_AUTH_KEY = original_auth_key
        assistant_module._AI_AUTH_DISABLED_UNTIL = original_until
        assistant_module._AI_AUTH_WARNING_EMITTED = original_warning


def test_ai_degraded_mode_guards():
    """AI outages must not turn every private message into the same dead end."""
    import AnonX_3.plugins.bot_assistant as assistant_module
    from unittest.mock import patch

    assert assistant_module._fallback_intent("မင်္ဂလာပါ") == ("greeting", {})
    assert assistant_module._fallback_intent("bot မှာ ဘာ error ဖြစ်နေလဲ") == (
        "get_realtime_status",
        {},
    )
    assert assistant_module._fallback_intent("ဘာတေဖစ်နေကျတာလဲဗျ") == (
        "get_realtime_status",
        {},
    )
    assert assistant_module._fallback_intent("ကြင်ဖော်ကြင်ဖက် သီချင်း ရှာပေး") == (
        "search_music",
        {"query": "ကြင်ဖော်ကြင်ဖက်", "video": False},
    )
    assert assistant_module._fallback_intent("သီချင်းနာမည် Adele Hello ရှာပေး") == (
        "search_music",
        {"query": "Adele Hello", "video": False},
    )
    assert assistant_module._fallback_intent("သိချင်းနာမည် Adele Hello ရှာပေး") == (
        "search_music",
        {"query": "Adele Hello", "video": False},
    )
    assert assistant_module._fallback_intent("please download a song named Adele Hello") == (
        "search_download_song",
        {"query": "Adele Hello", "video": False},
    )
    assert assistant_module._fallback_intent("Hello") == ("greeting", {})
    assert assistant_module._fallback_intent("ကြင်ဖော်ကြင်ဖက် သီချင်း ဒေါင်းပေး") == (
        "search_download_song",
        {"query": "ကြင်ဖော်ကြင်ဖက်", "video": False},
    )
    assert assistant_module._fallback_intent("ဒုတိယတစ်ပုဒ် mp3 ဒေါင်းပေး") == (
        "download_cached_result",
        {"index": 2, "video": False},
    )
    assert assistant_module._fallback_intent("သီချင်း ရှာပေး") == (
        "music_clarify",
        {},
    )
    assert "AI service" in assistant_module._fallback_answer("အခြားမေးခွန်း")

    calls = []

    async def fake_tool(name, args, message):
        calls.append((name, args))
        return "verified local result"

    with patch.object(assistant_module, "_execute_bot_tool", fake_tool):
        answer, tool_results = asyncio.run(
            assistant_module._run_local_degraded_path(
                "ကြင်ဖော်ကြင်ဖက် သီချင်း ဒေါင်းပေး", object()
            )
        )
    assert calls == [
        (
            "search_download_song",
            {"query": "ကြင်ဖော်ကြင်ဖက်", "video": False},
        )
    ]
    assert answer == "verified local result"
    assert tool_results == [{"content": "verified local result"}]

    async def failing_tool(name, args, message):
        raise RuntimeError("provider unavailable")

    with patch.object(assistant_module, "_execute_bot_tool", failing_tool):
        answer, tool_results = asyncio.run(
            assistant_module._run_local_degraded_path(
                "ကြင်ဖော်ကြင်ဖက် သီချင်း ဒေါင်းပေး", object()
            )
        )
    assert answer is None
    assert tool_results == []


def test_process_instance_lock_cross_process():
    """Only one OS process owns the persistent Pyrogram session at a time."""
    import subprocess
    import textwrap
    import time
    from datetime import datetime

    lifecycle_path = ROOT / "AnonX_3" / "core" / "lifecycle.py"
    child_source = textwrap.dedent(
        """
        import importlib.util
        import json
        import os
        import sys
        from pathlib import Path

        source_path = Path(sys.argv[1])
        deploy_root = Path(sys.argv[2])
        mode = sys.argv[3]
        ready_path = Path(sys.argv[4])
        startup_path = Path(sys.argv[5])
        module_name = f"_anonx_lifecycle_child_{os.getpid()}"
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        identity = module.resolve_runtime_identity("AnonX_3", deploy_root)
        lock = module.ProcessInstanceLock(identity)
        try:
            lock.acquire()
        except module.ProcessInstanceAlreadyRunning as error:
            ready_path.write_text(
                json.dumps(error.owner_metadata),
                encoding="utf-8",
            )
            raise SystemExit(module.DUPLICATE_INSTANCE_EXIT_CODE)

        # This marker stands in for the first service startup operation.  A
        # rejected duplicate must exit before it can be created.
        startup_path.write_text("started", encoding="utf-8")
        metadata = dict(lock.metadata)
        ready_path.write_text(json.dumps(metadata), encoding="utf-8")
        if mode == "owner":
            sys.stdin.readline()
        lock.release()
        """
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        owner_ready = temp_root / "owner.ready"
        owner_started = temp_root / "owner.started"
        owner = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                child_source,
                str(lifecycle_path),
                str(temp_root),
                "owner",
                str(owner_ready),
                str(owner_started),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while not owner_ready.exists() and time.monotonic() < deadline:
                if owner.poll() is not None:
                    error = owner.stderr.read() if owner.stderr else ""
                    raise AssertionError(f"lock owner exited early: {error}")
                time.sleep(0.01)
            assert owner_ready.exists(), "lock owner did not become ready"
            assert owner_started.exists()

            lock_path = temp_root / "AnonX_3.instance.lock"
            # Windows denies a second open of the byte-range-locked file, so
            # inspect the exact payload reported by its owning process.  The
            # on-disk bytes are checked immediately after release below.
            metadata = json.loads(owner_ready.read_text(encoding="utf-8"))
            assert set(metadata) == {"pid", "started_at"}
            assert metadata["pid"] == owner.pid
            assert isinstance(metadata["started_at"], str)
            datetime.fromisoformat(metadata["started_at"])

            duplicate_ready = temp_root / "duplicate.ready"
            duplicate_started = temp_root / "duplicate.started"
            duplicate = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    child_source,
                    str(lifecycle_path),
                    str(temp_root),
                    "probe",
                    str(duplicate_ready),
                    str(duplicate_started),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert duplicate.returncode == 75, duplicate.stderr
            assert not duplicate_started.exists()
            assert duplicate_ready.exists()
            duplicate_metadata = json.loads(
                duplicate_ready.read_text(encoding="utf-8")
            )
            assert duplicate_metadata == metadata
            assert set(duplicate_metadata) == {"pid", "started_at"}

            owner_stdout, owner_stderr = owner.communicate("release\n", timeout=5)
            assert owner.returncode == 0, owner_stderr or owner_stdout
            assert lock_path.is_file(), "stable lock file must never be unlinked"
            released_metadata = json.loads(lock_path.read_text(encoding="utf-8"))
            assert released_metadata == metadata

            reacquired_ready = temp_root / "reacquired.ready"
            reacquired_started = temp_root / "reacquired.started"
            reacquired = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    child_source,
                    str(lifecycle_path),
                    str(temp_root),
                    "probe",
                    str(reacquired_ready),
                    str(reacquired_started),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert reacquired.returncode == 0, reacquired.stderr
            assert reacquired_started.exists()
            assert reacquired_ready.exists()
            assert lock_path.is_file()
            reacquired_metadata = json.loads(
                reacquired_ready.read_text(encoding="utf-8")
            )
            assert set(reacquired_metadata) == {"pid", "started_at"}
            assert reacquired_metadata["pid"] != owner.pid
        finally:
            if owner.poll() is None:
                try:
                    owner.communicate("release\n", timeout=2)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.wait(timeout=2)


def test_crash_restart_backoff_contract():
    lifecycle = _direct_load_module(
        ROOT / "AnonX_3" / "core" / "lifecycle.py",
        "backoff",
    )
    key = lifecycle.RESTART_ATTEMPT_ENV
    env: dict[str, str] = {}
    plans = [
        lifecycle.plan_crash_restart(0, env=env, jitter_fraction=0.0)
        for _ in range(8)
    ]
    assert [plan.base_delay_seconds for plan in plans] == [
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        60.0,
        60.0,
        60.0,
    ]
    assert [plan.delay_seconds for plan in plans] == [
        plan.base_delay_seconds for plan in plans
    ]
    assert env == {key: "8"}

    upper = lifecycle.plan_crash_restart(
        0,
        env={},
        jitter_fraction=0.10,
    )
    lower = lifecycle.plan_crash_restart(
        0,
        env={},
        jitter_fraction=-0.10,
    )
    assert abs(upper.delay_seconds - 2.2) < 1e-12
    assert abs(lower.delay_seconds - 1.8) < 1e-12

    pre_reset = {key: "5"}
    reset = lifecycle.plan_crash_restart(
        120.0,
        env=pre_reset,
        jitter_fraction=0.0,
    )
    assert reset.reset_after_stable_runtime is True
    assert reset.attempt == 1
    assert reset.base_delay_seconds == 2.0
    assert pre_reset == {key: "1"}

    previous = os.environ.get(key)
    try:
        os.environ[key] = "9"
        lifecycle.clear_crash_restart_state()
        assert key not in os.environ
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def test_bootstrap_restart_guard_contract():
    """Direct package-launch import failures use one locked fresh exec."""
    import contextlib
    import io
    from types import SimpleNamespace

    lifecycle = _direct_load_module(
        ROOT / "AnonX_3" / "core" / "lifecycle.py",
        "bootstrap_guard",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        identity = lifecycle.resolve_runtime_identity("AnonX_3", temp_dir)
        instance_lock = lifecycle.ProcessInstanceLock(identity)
        instance_lock.acquire()
        calls = {
            "cleanup": 0,
            "plan": 0,
            "sleep": 0,
            "exec": 0,
            "delegate": [],
        }

        original_hook = sys.excepthook
        original_plan = lifecycle.plan_crash_restart
        original_sleep = lifecycle.time.sleep
        original_exec = lifecycle.exec_fresh_process

        def delegated(exc_type, exc_value, exc_tb):
            calls["delegate"].append((exc_type, exc_value, exc_tb))

        def cleanup():
            calls["cleanup"] += 1
            assert instance_lock.acquired
            raise RuntimeError("cleanup failure must not block fresh exec")

        def plan(runtime_seconds):
            calls["plan"] += 1
            assert runtime_seconds >= 0
            assert instance_lock.acquired
            return SimpleNamespace(
                attempt=3,
                base_delay_seconds=8.0,
                delay_seconds=0.0,
            )

        def sleep(delay):
            calls["sleep"] += 1
            assert delay == 0.0
            assert instance_lock.acquired

        def blocked_exec(package_name, *, reason):
            calls["exec"] += 1
            assert package_name == "AnonX_3"
            assert reason == "bootstrap-crash-attempt-3"
            assert instance_lock.acquired
            raise RuntimeError("exec intercepted")

        try:
            sys.excepthook = delegated
            lifecycle.plan_crash_restart = plan
            lifecycle.time.sleep = sleep
            lifecycle.exec_fresh_process = blocked_exec

            guard = lifecycle.BootstrapRestartGuard(identity, instance_lock)
            assert guard.install([sys.executable, "-m", "AnonX_3"])
            assert guard.active
            assert sys.excepthook is not delegated
            guard.set_cleanup(cleanup)

            failure = RuntimeError("bootstrap exploded")
            with contextlib.redirect_stderr(io.StringIO()):
                sys.excepthook(type(failure), failure, failure.__traceback__)

            assert calls["cleanup"] == 1
            assert calls["plan"] == 1
            assert calls["sleep"] == 1
            assert calls["exec"] == 1
            assert len(calls["delegate"]) == 1
            assert calls["delegate"][0][1] is failure
            assert not guard.active
            assert not instance_lock.acquired

            # Ordinary imports and other -m targets must never auto-restart.
            sys.excepthook = delegated
            passive = lifecycle.BootstrapRestartGuard(identity, instance_lock)
            assert not passive.install([sys.executable, "-m", "pytest"])
            assert sys.excepthook is delegated

            # Intentional terminal exceptions delegate without entering the
            # crash-restart policy or surrendering the incumbent's lock.
            instance_lock.acquire()
            terminal = lifecycle.BootstrapRestartGuard(identity, instance_lock)
            assert terminal.install([sys.executable, "-m", "AnonX_3"])
            interrupted = KeyboardInterrupt()
            with contextlib.redirect_stderr(io.StringIO()):
                sys.excepthook(
                    type(interrupted),
                    interrupted,
                    interrupted.__traceback__,
                )
            assert instance_lock.acquired
            assert calls["plan"] == 1
            terminal.complete()
            instance_lock.release()
        finally:
            instance_lock.release()
            lifecycle.plan_crash_restart = original_plan
            lifecycle.time.sleep = original_sleep
            lifecycle.exec_fresh_process = original_exec
            sys.excepthook = original_hook


def test_package_bootstrap_lock_precedes_service_construction():
    """Package import claims the session before Config, Bot, or Mongo exist."""
    package_path = ROOT / "AnonX_3" / "__init__.py"
    package_tree = ast.parse(
        package_path.read_text(encoding="utf-8"),
        filename=str(package_path),
    )

    assignments = {
        target.id: node
        for node in package_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "runtime_identity" in assignments
    assert "process_instance_lock" in assignments
    assert "bootstrap_restart_guard" in assignments
    lock_assignment = assignments["process_instance_lock"]
    assert isinstance(lock_assignment.value, ast.Call)
    assert isinstance(lock_assignment.value.func, ast.Name)
    assert lock_assignment.value.func.id == "ProcessInstanceLock"

    acquire_call = next(
        node
        for node in ast.walk(package_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "process_instance_lock"
        and node.func.attr == "acquire"
    )
    config_import = next(
        node
        for node in package_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "config"
    )
    constructors = {
        name: next(
            node
            for node in ast.walk(package_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )
        for name in ("Config", "Bot", "MongoDB")
    }
    guard_assignment = assignments["bootstrap_restart_guard"]
    guard_install = next(
        node
        for node in ast.walk(package_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "bootstrap_restart_guard"
        and node.func.attr == "install"
    )
    guard_cleanup = next(
        node
        for node in ast.walk(package_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "bootstrap_restart_guard"
        and node.func.attr == "set_cleanup"
    )
    assert (
        lock_assignment.lineno
        < acquire_call.lineno
        < guard_assignment.lineno
        < guard_install.lineno
        < config_import.lineno
    )
    assert all(guard_install.lineno < node.lineno for node in constructors.values())
    assert all(node.lineno < guard_cleanup.lineno for node in constructors.values())

    main_path = ROOT / "AnonX_3" / "__main__.py"
    main_source = main_path.read_text(encoding="utf-8")
    main_tree = ast.parse(main_source, filename=str(main_path))
    run_process = next(
        node
        for node in main_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_process"
    )
    run_source = ast.get_source_segment(main_source, run_process)
    assert "instance_lock = process_instance_lock" in run_source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProcessInstanceLock"
        for node in ast.walk(run_process)
    )
    idempotent_acquires = [
        node
        for node in ast.walk(run_process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "instance_lock"
        and node.func.attr == "acquire"
    ]
    assert len(idempotent_acquires) == 1
    complete_call = next(
        node
        for node in ast.walk(run_process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "bootstrap_restart_guard"
        and node.func.attr == "complete"
    )
    lifecycle_run = next(
        node
        for node in ast.walk(run_process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "run"
    )
    assert complete_call.lineno < lifecycle_run.lineno


def test_run_process_crash_and_terminal_outcomes():
    """SQLite startup crashes re-exec once; terminal outcomes never restart."""
    import contextlib
    import io
    import sqlite3
    import time as time_module
    import traceback
    from types import SimpleNamespace
    from unittest.mock import patch

    main_path = ROOT / "AnonX_3" / "__main__.py"
    main_source = main_path.read_text(encoding="utf-8")
    assert "InvalidOperation" not in main_source
    lifecycle_path = ROOT / "AnonX_3" / "core" / "lifecycle.py"
    lifecycle_source = lifecycle_path.read_text(encoding="utf-8")
    lifecycle_tree = ast.parse(lifecycle_source, filename=str(lifecycle_path))
    exec_helper = next(
        node
        for node in lifecycle_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "exec_fresh_process"
    )
    exec_source = ast.get_source_segment(lifecycle_source, exec_helper)
    assert exec_source.index("stream.flush()") < exec_source.index(
        "release_active_process_lock()"
    ) < exec_source.index("os.execvpe(")

    class QuietLogger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    def run_case(outcome: str):
        lifecycle = _direct_load_module(
            ROOT / "AnonX_3" / "core" / "lifecycle.py",
            f"run_process_{outcome}",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            identity = lifecycle.resolve_runtime_identity("AnonX_3", temp_dir)
            instance_lock = lifecycle.ProcessInstanceLock(identity)
            instance_lock.acquire()
            bootstrap_handle = instance_lock._handle
            bootstrap_metadata = dict(instance_lock.metadata)
            counts = {
                "acquire": 0,
                "guard_complete": 0,
                "run_once": 0,
                "connect": 0,
                "start": 0,
                "stop": 0,
                "plan": 0,
                "sleep": 0,
                "exec_helper": 0,
                "os_exec": 0,
            }

            original_acquire = instance_lock.acquire

            def idempotent_acquire():
                counts["acquire"] += 1
                assert instance_lock.acquired
                assert instance_lock._handle is bootstrap_handle
                result = original_acquire()
                assert result is instance_lock
                assert instance_lock._handle is bootstrap_handle
                assert instance_lock.metadata == bootstrap_metadata
                return result

            class BootstrapGuard:
                def complete(self):
                    counts["guard_complete"] += 1

            # __main__ must reuse this exported bootstrap object.  Its acquire
            # call is allowed only because ProcessInstanceLock.acquire() is
            # idempotent and therefore cannot touch the OS lock again.
            instance_lock.acquire = idempotent_acquire

            async def connect():
                counts["connect"] += 1
                if outcome == "connect_locked":
                    raise sqlite3.OperationalError("database is locked")

            async def boot():
                counts["start"] += 1
                if outcome in {"boot_locked", "delay_interrupted", "plan_failure"}:
                    raise sqlite3.OperationalError("database is locked")

            async def run_once():
                counts["run_once"] += 1
                await connect()
                await boot()
                if outcome == "system_exit":
                    raise SystemExit(23)
                if outcome == "keyboard_interrupt":
                    raise KeyboardInterrupt

            async def stop():
                counts["stop"] += 1

            def plan(_runtime_seconds):
                counts["plan"] += 1
                assert instance_lock.acquired
                if outcome == "plan_failure":
                    raise RuntimeError("restart planning failed")
                return SimpleNamespace(
                    attempt=1,
                    base_delay_seconds=2.0,
                    delay_seconds=0.0,
                    reset_after_stable_runtime=False,
                )

            def sleep(_delay):
                counts["sleep"] += 1
                assert instance_lock.acquired
                if outcome == "delay_interrupted":
                    raise KeyboardInterrupt

            def blocked_os_exec(executable, argv, child_env):
                counts["os_exec"] += 1
                assert executable == sys.executable
                assert argv == [sys.executable, "-m", "AnonX_3"]
                assert isinstance(child_env, dict)
                # The shared helper must release at the last responsible
                # moment: after backoff, immediately before the OS exec.
                assert not instance_lock.acquired
                raise RuntimeError("exec blocked by lifecycle smoke test")

            def exec_fresh_process(*args, **kwargs):
                counts["exec_helper"] += 1
                assert instance_lock.acquired
                return lifecycle.exec_fresh_process(*args, **kwargs)

            namespace = {
                "__package__": "AnonX_3",
                "asyncio": asyncio,
                "logger": QuietLogger(),
                "os": os,
                "sys": sys,
                "time": SimpleNamespace(
                    monotonic=time_module.monotonic,
                    sleep=sleep,
                ),
                "traceback": traceback,
                "DUPLICATE_INSTANCE_EXIT_CODE": (
                    lifecycle.DUPLICATE_INSTANCE_EXIT_CODE
                ),
                "ProcessInstanceAlreadyRunning": (
                    lifecycle.ProcessInstanceAlreadyRunning
                ),
                "bootstrap_restart_guard": BootstrapGuard(),
                "process_instance_lock": instance_lock,
                "resolve_runtime_identity": lambda _hint: identity,
                "plan_crash_restart": plan,
                "exec_fresh_process": exec_fresh_process,
                "_failure_phase": None,
                "_lifecycle_phase": "initialization",
                "_run_once": run_once,
                "stop": stop,
            }
            _exec_source_definitions(
                main_path,
                {"_set_lifecycle_phase", "main", "_system_exit_code", "_run_process"},
                namespace,
            )
            quiet_logger = type(
                "QuietLifecycleLogger",
                (),
                {"warning": lambda *_args, **_kwargs: None},
            )()
            with (
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(lifecycle.os, "execvpe", new=blocked_os_exec),
                patch.object(
                    lifecycle.logging,
                    "getLogger",
                    return_value=quiet_logger,
                ),
            ):
                result = namespace["_run_process"]()

            assert not instance_lock.acquired
            probe = lifecycle.ProcessInstanceLock(identity)
            probe.acquire()
            probe.release()
            return result, counts

    for failure in ("connect_locked", "boot_locked"):
        result, counts = run_case(failure)
        assert result == 1
        assert counts["acquire"] == 1
        assert counts["guard_complete"] == 1
        assert counts["run_once"] == 1
        assert counts["connect"] == 1
        assert counts["start"] == (0 if failure == "connect_locked" else 1)
        assert counts["stop"] == 1
        assert counts["plan"] == 1
        assert counts["sleep"] == 1
        assert counts["exec_helper"] == 1
        assert counts["os_exec"] == 1

    normal_result, normal = run_case("normal")
    assert normal_result == 0
    assert normal["acquire"] == 1
    assert normal["guard_complete"] == 1
    assert normal["run_once"] == normal["connect"] == normal["start"] == 1
    assert normal["stop"] == 1
    assert normal["plan"] == normal["sleep"] == 0
    assert normal["exec_helper"] == normal["os_exec"] == 0

    exit_result, exited = run_case("system_exit")
    assert exit_result == 23
    assert exited["acquire"] == 1
    assert exited["guard_complete"] == 1
    assert exited["run_once"] == exited["connect"] == exited["start"] == 1
    assert exited["stop"] == 1
    assert exited["plan"] == exited["sleep"] == 0
    assert exited["exec_helper"] == exited["os_exec"] == 0

    interrupt_result, interrupted = run_case("keyboard_interrupt")
    assert interrupt_result == 130
    assert interrupted["acquire"] == 1
    assert interrupted["guard_complete"] == 1
    assert interrupted["run_once"] == interrupted["stop"] == 1
    assert interrupted["plan"] == interrupted["sleep"] == 0
    assert interrupted["exec_helper"] == interrupted["os_exec"] == 0

    delay_result, delay_interrupted = run_case("delay_interrupted")
    assert delay_result == 130
    assert delay_interrupted["acquire"] == 1
    assert delay_interrupted["guard_complete"] == 1
    assert delay_interrupted["run_once"] == delay_interrupted["stop"] == 1
    assert delay_interrupted["plan"] == delay_interrupted["sleep"] == 1
    assert delay_interrupted["exec_helper"] == 0
    assert delay_interrupted["os_exec"] == 0

    plan_result, plan_failed = run_case("plan_failure")
    assert plan_result == 1
    assert plan_failed["acquire"] == 1
    assert plan_failed["guard_complete"] == 1
    assert plan_failed["run_once"] == plan_failed["stop"] == 1
    assert plan_failed["plan"] == 1
    assert plan_failed["sleep"] == 0
    assert plan_failed["exec_helper"] == plan_failed["os_exec"] == 0

    lifecycle = _direct_load_module(
        ROOT / "AnonX_3" / "core" / "lifecycle.py",
        "duplicate_branch",
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        identity = lifecycle.resolve_runtime_identity("AnonX_3", temp_dir)
        duplicate_counts = {"run_once": 0, "stop": 0, "plan": 0, "exec": 0}

        class DuplicateLock:
            def acquire(self):
                raise lifecycle.ProcessInstanceAlreadyRunning(
                    identity.lock_path,
                    {"pid": 9876, "started_at": "2026-08-03T00:00:00+00:00"},
                )

        async def should_not_run():
            duplicate_counts["run_once"] += 1

        async def should_not_stop():
            duplicate_counts["stop"] += 1

        def should_not_plan(_runtime):
            duplicate_counts["plan"] += 1
            raise AssertionError("duplicate instance planned a restart")

        def should_not_exec(*_args, **_kwargs):
            duplicate_counts["exec"] += 1

        namespace = {
            "__package__": "AnonX_3",
            "asyncio": asyncio,
            "logger": QuietLogger(),
            "os": os,
            "sys": sys,
            "time": SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _x: None),
            "traceback": traceback,
            "DUPLICATE_INSTANCE_EXIT_CODE": 75,
            "ProcessInstanceAlreadyRunning": lifecycle.ProcessInstanceAlreadyRunning,
            "bootstrap_restart_guard": SimpleNamespace(
                complete=lambda: (_ for _ in ()).throw(
                    AssertionError("duplicate instance completed bootstrap guard")
                )
            ),
            "process_instance_lock": DuplicateLock(),
            "resolve_runtime_identity": lambda _hint: identity,
            "plan_crash_restart": should_not_plan,
            "exec_fresh_process": should_not_exec,
            "_failure_phase": None,
            "_lifecycle_phase": "initialization",
            "_run_once": should_not_run,
            "stop": should_not_stop,
        }
        _exec_source_definitions(
            main_path,
            {"_set_lifecycle_phase", "main", "_system_exit_code", "_run_process"},
            namespace,
        )
        with contextlib.redirect_stderr(io.StringIO()):
            assert namespace["_run_process"]() == 75
        assert duplicate_counts == {
            "run_once": 0,
            "stop": 0,
            "plan": 0,
            "exec": 0,
        }


def test_global_stop_is_concurrent_ordered_and_failure_tolerant():
    """Concurrent stop callers execute the owned teardown graph only once."""
    from types import SimpleNamespace

    package_path = ROOT / "AnonX_3" / "__init__.py"
    package_source = package_path.read_text(encoding="utf-8")
    assert "asyncio.all_tasks(" not in package_source

    class RecordingLogger:
        def __init__(self):
            self.records = []

        @staticmethod
        def _format(message, args):
            try:
                return message % args if args else str(message)
            except Exception:
                return str(message)

        def info(self, message, *args, **_kwargs):
            self.records.append(("info", self._format(message, args)))

        def warning(self, message, *args, **_kwargs):
            self.records.append(("warning", self._format(message, args)))

        def error(self, message, *args, **_kwargs):
            self.records.append(("error", self._format(message, args)))

    async def verify():
        order: list[str] = []
        finalized: list[str] = []
        logger = RecordingLogger()
        registered_tasks: list[asyncio.Task] = []
        http_media_tasks: list[asyncio.Task] = []

        async def owned_work(label: str):
            try:
                await asyncio.Event().wait()
            finally:
                finalized.append(label)

        owned_task = asyncio.create_task(
            owned_work("owned"), name="owned-shutdown-test"
        )
        http_task = asyncio.create_task(
            owned_work("http"), name="http-media-shutdown-test"
        )
        registered_tasks.append(owned_task)
        http_media_tasks.append(http_task)
        await asyncio.sleep(0)

        def closer(label: str, *, fail: bool = False):
            async def close():
                order.append(label)
                if fail:
                    raise RuntimeError(f"{label} injected failure")

            return close

        supervisor = SimpleNamespace(shutdown=closer("supervisor"))
        yt = SimpleNamespace(
            _cookie_watcher=None,
            _cookie_refresh_task=None,
            close=closer("yt"),
        )
        tg = SimpleNamespace(shutdown=closer("tg"))
        tiktok = SimpleNamespace(shutdown=closer("tiktok"))
        facebook = SimpleNamespace(shutdown=closer("facebook"))
        namespace = {
            "asyncio": asyncio,
            "logger": logger,
            "tasks": registered_tasks,
            "http_media_tasks": http_media_tasks,
            "yt": yt,
            "tg": tg,
            "tiktok": tiktok,
            "facebook": facebook,
            "anon": SimpleNamespace(shutdown=closer("anon", fail=True)),
            "userbot": SimpleNamespace(exit=closer("userbot")),
            "app": SimpleNamespace(exit=closer("app")),
            "thumb": SimpleNamespace(close=closer("thumb")),
            "bot_api": SimpleNamespace(close=closer("bot_api")),
            "db": SimpleNamespace(close=closer("db")),
            "shutdown_singleflights": closer("singleflights"),
            "PROCESS_STOP_TIMEOUT_SEC": 5.0,
            "TASK_CANCEL_TIMEOUT_SEC": 2.0,
            "CLIENT_STOP_TIMEOUT_SEC": 2.0,
            "_shutdown_lock": asyncio.Lock(),
            "_shutdown_state": "running",
        }
        _exec_source_definitions(
            package_path,
            {
                "_collect_all_tasks",
                "_cancel_owned_task_registry",
                "_cancel_registered_tasks",
                "_cancel_http_media_tasks",
                "_stop_background_watchers",
                "_run_shutdown_step",
                "stop",
            },
            namespace,
        )

        real_cancel = namespace["_cancel_registered_tasks"]
        real_http_cancel = namespace["_cancel_http_media_tasks"]

        async def tracked_watchers():
            order.append("watchers")

        async def tracked_cancel():
            order.append("tasks")
            await real_cancel()
            assert owned_task.done()
            assert not http_task.done()

        async def tracked_http_cancel():
            order.append("http_media")
            assert order[-2] == "app"
            await real_http_cancel()
            assert http_task.done()

        namespace["_stop_background_watchers"] = tracked_watchers
        namespace["_cancel_registered_tasks"] = tracked_cancel
        namespace["_cancel_http_media_tasks"] = tracked_http_cancel

        supervisor_module = SimpleNamespace(supervisor=supervisor)
        module_name = "AnonX_3.core.supervisor"
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = supervisor_module
        try:
            await asyncio.gather(
                namespace["stop"](),
                namespace["stop"](),
                namespace["stop"](),
            )
            await namespace["stop"]()
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous

        assert order == [
            "supervisor",
            "watchers",
            "tasks",
            "anon",
            "userbot",
            "app",
            "http_media",
            "tg",
            "tiktok",
            "facebook",
            "singleflights",
            "thumb",
            "yt",
            "bot_api",
            "db",
        ]
        assert sorted(finalized) == ["http", "owned"]
        assert owned_task.done()
        assert http_task.done()
        assert registered_tasks == []
        assert http_media_tasks == []
        assert namespace["_shutdown_state"] == "stopped"
        assert any(
            level == "error" and "Shutdown incomplete" in message
            for level, message in logger.records
        )
        assert not any(
            level == "info" and message.strip() == "Stopped."
            for level, message in logger.records
        )

    asyncio.run(verify())


def test_supervisor_shutdown_owns_and_forgets_tasks():
    from types import SimpleNamespace

    supervisor_path = ROOT / "AnonX_3" / "core" / "supervisor.py"
    source = supervisor_path.read_text(encoding="utf-8")
    assert "asyncio.all_tasks(" not in source

    class QuietLogger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    async def verify():
        global_tasks: list[asyncio.Task] = []
        namespace = {
            "asyncio": asyncio,
            "time": __import__("time"),
            "CoroutineFactory": object,
            "logger": QuietLogger(),
            "tasks": global_tasks,
        }
        _exec_source_definitions(
            supervisor_path,
            {"_collect_owned_task_tree", "_clear_own_children", "CriticalTaskSupervisor"},
            namespace,
        )
        service = namespace["CriticalTaskSupervisor"]()
        started = {"one": asyncio.Event(), "two": asyncio.Event()}
        finalized: list[str] = []
        calls = {"one": 0, "two": 0}

        def factory(name: str):
            async def work():
                calls[name] += 1
                started[name].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.append(name)

            return work

        roots = [
            service.spawn("one", factory("one")),
            service.spawn("two", factory("two")),
        ]
        await asyncio.gather(*(event.wait() for event in started.values()))
        assert all(root in global_tasks for root in roots)

        await asyncio.gather(service.shutdown(), service.shutdown())
        assert service._stopping is True
        assert service._shutdown_complete is True
        assert service._tasks == {}
        assert service._restarts == {}
        assert global_tasks == []
        assert all(task.done() for task in roots)
        assert sorted(finalized) == ["one", "two"]
        assert calls == {"one": 1, "two": 1}

        try:
            service.spawn("resurrect", factory("one"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("supervisor accepted work after shutdown")
        await service.shutdown()
        await asyncio.sleep(0)
        assert calls == {"one": 1, "two": 1}

    asyncio.run(verify())


def test_tgcall_shutdown_cleans_all_owned_state():
    """TgCall cleanup awaits every owned task and every partial client."""
    import inspect
    from types import SimpleNamespace

    calls_path = ROOT / "AnonX_3" / "core" / "calls.py"
    source = calls_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(calls_path))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "all_tasks"
        for node in ast.walk(tree)
    )
    tgcall = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TgCall"
    )
    method_names = {
        "_flatten_owned_tasks",
        "_detach_prefetch_tasks",
        "_stop_call_client",
        "shutdown",
    }
    methods = [
        node
        for node in tgcall.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ]
    assert {node.name for node in methods} == method_names
    harness = ast.ClassDef(
        name="TgCallShutdownHarness",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.Module(body=[harness], type_ignores=[])
    ast.fix_missing_locations(module)

    class QuietLogger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    async def verify():
        direct_watchdog = SimpleNamespace(_watches={1: object(), 2: object()})
        namespace = {
            "asyncio": asyncio,
            "inspect": inspect,
            "direct_watchdog": direct_watchdog,
            "logger": QuietLogger(),
        }
        exec(compile(module, str(calls_path), "exec"), namespace)
        service = namespace["TgCallShutdownHarness"]()
        service._shutdown_lock = asyncio.Lock()
        service._shutdown_complete = False
        service._shutting_down = False
        service._startup_error = RuntimeError("partial boot")
        service._ready = asyncio.Event()

        finalized: list[str] = []

        def owned_task(name: str) -> asyncio.Task:
            async def work():
                try:
                    await asyncio.Event().wait()
                finally:
                    finalized.append(name)

            return asyncio.create_task(work(), name=f"tgcall-test:{name}")

        task_names = [
            "owned",
            "unmute",
            "direct",
            "thumbnail",
            "post_start",
            "proof",
            "prefetch",
            "secondary",
            "current_cache",
        ]
        task_map = {name: owned_task(name) for name in task_names}
        await asyncio.sleep(0)
        service._owned_tasks = {
            task_map["owned"],
            task_map["unmute"],
            task_map["direct"],
            task_map["thumbnail"],
            task_map["post_start"],
            task_map["proof"],
        }
        service._vc_unmute_tasks = {task_map["unmute"]}
        service._direct_cache_tasks = {task_map["direct"]}
        service._thumbnail_tasks = {task_map["thumbnail"]}
        service._post_start_tasks = {task_map["post_start"]}
        service._post_start_by_chat = {1: {task_map["post_start"]}}
        service._startup_proof_tasks = {1: task_map["proof"]}
        service.prefetch_manager = SimpleNamespace(
            prefetch={1: (object(), task_map["prefetch"])},
            secondary={2: (object(), task_map["secondary"])},
            current_cache={3: (object(), task_map["current_cache"])},
            _terminal_outcomes={1: object()},
        )

        class PartialClient:
            def __init__(self, name: str, fail: bool = False):
                self.name = name
                self.fail = fail
                self.calls = 0
                self._is_running = True
                self.executor = None

            async def shutdown(self):
                self.calls += 1
                if self.fail:
                    raise RuntimeError(f"{self.name} close failed")

        partial_clients = [
            PartialClient("first"),
            PartialClient("broken", fail=True),
            PartialClient("last"),
        ]
        main_thread_id = threading.get_ident()

        class RecordingExecutor:
            def __init__(self):
                self.calls = []

            def shutdown(self, *, wait, cancel_futures=True):
                import time

                time.sleep(0.02)
                self.calls.append(
                    (wait, cancel_futures, threading.get_ident())
                )

        class ExecutorOnlyClient:
            def __init__(self):
                self.calls = {}
                self._is_running = True
                self.executor = RecordingExecutor()

        executor_client = ExecutorOnlyClient()
        clients = [*partial_clients, executor_client]
        service.clients = list(clients)

        outcomes = await asyncio.gather(
            service.shutdown(),
            service.shutdown(),
            return_exceptions=True,
        )
        failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], ExceptionGroup)
        assert [client.calls for client in partial_clients] == [1, 1, 1]
        assert all(client._is_running is False for client in clients)
        assert len(executor_client.executor.calls) == 1
        wait, cancel_futures, worker_thread_id = executor_client.executor.calls[0]
        assert wait is True
        assert cancel_futures is True
        assert worker_thread_id != main_thread_id
        assert service.clients == []
        assert service._owned_tasks == set()
        assert service._vc_unmute_tasks == set()
        assert service._direct_cache_tasks == set()
        assert service._thumbnail_tasks == set()
        assert service._post_start_tasks == set()
        assert service._post_start_by_chat == {}
        assert service._startup_proof_tasks == {}
        assert service.prefetch_manager.prefetch == {}
        assert service.prefetch_manager.secondary == {}
        assert service.prefetch_manager.current_cache == {}
        assert service.prefetch_manager._terminal_outcomes == {}
        assert direct_watchdog._watches == {}
        assert sorted(finalized) == sorted(task_names)
        assert all(task.done() for task in task_map.values())
        assert service._startup_error is None
        assert service._ready.is_set()
        assert service._shutting_down is True
        assert service._shutdown_complete is True

        await service.shutdown()
        assert [client.calls for client in partial_clients] == [1, 1, 1]
        assert len(executor_client.executor.calls) == 1

    asyncio.run(verify())


def test_partial_pyrogram_storage_cleanup_and_session_invariants():
    """A failed storage-open is closed without calling terminated Client.stop."""
    import inspect
    from types import SimpleNamespace

    bot_path = ROOT / "AnonX_3" / "core" / "bot.py"
    userbot_path = ROOT / "AnonX_3" / "core" / "userbot.py"
    namespace = {
        "asyncio": asyncio,
        "inspect": inspect,
        "pyrogram": SimpleNamespace(Client=object),
    }
    _exec_source_definitions(
        bot_path,
        {"shutdown_pyrogram_client"},
        namespace,
    )
    shutdown_client = namespace["shutdown_pyrogram_client"]

    async def verify_cleanup():
        class Storage:
            def __init__(self):
                self.conn = object()
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1

        class Session:
            def __init__(self):
                self.stop_calls = 0

            async def stop(self):
                self.stop_calls += 1

        class PartialClient:
            def __init__(self):
                self.storage = Storage()
                self.session = Session()
                self.is_initialized = False
                self.is_connected = False
                self.stop_calls = 0
                self.terminate_calls = 0

            async def stop(self):
                self.stop_calls += 1
                raise RuntimeError("Client is already terminated")

            async def terminate(self):
                self.terminate_calls += 1

        client = PartialClient()
        session = client.session
        assert await shutdown_client(client) == []
        assert await shutdown_client(client) == []
        assert client.storage.close_calls == 1
        assert client.storage.conn is None
        assert session.stop_calls == 1
        assert client.session is None
        assert client.stop_calls == 0
        assert client.terminate_calls == 0
        assert client.is_connected is False

    asyncio.run(verify_cleanup())

    bot_source = bot_path.read_text(encoding="utf-8")
    bot_tree = ast.parse(bot_source)
    bot_class = next(
        node
        for node in bot_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Bot"
    )
    bot_init = next(
        node
        for node in bot_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    super_init = next(
        node
        for node in ast.walk(bot_init)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__init__"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    )
    bot_keywords = {keyword.arg: keyword.value for keyword in super_init.keywords}
    assert ast.literal_eval(bot_keywords["name"]) == "AnonX_3"
    assert ast.literal_eval(bot_keywords["in_memory"]) is False
    workdir = bot_keywords["workdir"]
    assert isinstance(workdir, ast.Call)
    assert isinstance(workdir.func, ast.Name) and workdir.func.id == "str"
    assert isinstance(workdir.args[0], ast.Name)
    assert workdir.args[0].id == "DEPLOY_ROOT"
    assert bot_path.resolve().parents[2] == ROOT.resolve()

    lifecycle = _direct_load_module(
        ROOT / "AnonX_3" / "core" / "lifecycle.py",
        "session_invariant",
    )
    identity = lifecycle.resolve_runtime_identity("AnonX_3", ROOT)
    assert identity.session_path == ROOT / "AnonX_3.session"

    userbot_tree = ast.parse(userbot_path.read_text(encoding="utf-8"))
    userbot_class = next(
        node
        for node in userbot_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Userbot"
    )
    userbot_init = next(
        node
        for node in userbot_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assistant_client = next(
        node
        for node in ast.walk(userbot_init)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Client"
    )
    assistant_keywords = {
        keyword.arg: keyword.value for keyword in assistant_client.keywords
    }
    assert ast.literal_eval(assistant_keywords["in_memory"]) is True
    assert isinstance(assistant_keywords["session_string"], ast.Name)
    assert assistant_keywords["session_string"].id == "session"


def test_manual_restart_detaches_shutdown_from_handler():
    from unittest.mock import patch

    package_name = "AnonX_3"
    restart_path = ROOT / package_name / "plugins" / "restart.py"
    restart_source = (
        restart_path
    ).read_text(encoding="utf-8")
    tree = ast.parse(restart_source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    handler_source = ast.get_source_segment(restart_source, functions["_restart"])
    worker_source = ast.get_source_segment(
        restart_source,
        functions["_finish_manual_restart"],
    )

    assert "await asyncio.wait_for(stop()" not in handler_source
    assert "_restart_lock = asyncio.Lock()" in restart_source
    assert "async with _restart_lock:" in handler_source
    assert handler_source.index("_restart_in_progress = True") < handler_source.index(
        'claimed = await db.claim_command_once("restart"'
    )
    assert "if not claimed:" in handler_source
    assert "_restart_in_progress = False" in handler_source
    assert "Could not send manual restart acknowledgement" in handler_source
    assert "Could not update manual restart acknowledgement" in handler_source
    assert "_restart_task = asyncio.create_task(" in handler_source
    assert "_finish_manual_restart(resolve_package_name(__package__))" in handler_source
    assert "await asyncio.sleep(0)" in worker_source
    assert "await asyncio.wait_for(stop()" in worker_source
    assert "exec_fresh_process(" in worker_source
    assert 'reason="manual-restart"' in worker_source
    assert "clear_crash_state=True" in worker_source
    assert 'name="manual-restart"' in handler_source

    calls = {"stop": 0, "reset": 0, "exec": []}

    async def fake_stop():
        calls["stop"] += 1

    def fake_reset():
        calls["reset"] += 1
        raise RuntimeError("reset failure is shielded")

    def fake_exec(package, **kwargs):
        # Patched NoReturn seam: record rather than replacing this interpreter.
        calls["exec"].append((package, kwargs))

    namespace = {
        "asyncio": asyncio,
        "stop": fake_stop,
        "PROCESS_STOP_TIMEOUT_SEC": 2.0,
        "logger": type(
            "QuietLogger",
            (),
            {"warning": lambda *_args, **_kwargs: None},
        )(),
        "reset_runtime_dirs": fake_reset,
        "exec_fresh_process": fake_exec,
    }
    _exec_source_definitions(
        restart_path,
        {"_finish_manual_restart"},
        namespace,
    )
    asyncio.run(namespace["_finish_manual_restart"](package_name))
    assert calls == {
        "stop": 1,
        "reset": 1,
        "exec": [
            (
                package_name,
                {"reason": "manual-restart", "clear_crash_state": True},
            )
        ],
    }

    lifecycle = _direct_load_module(
        ROOT / package_name / "core" / "lifecycle.py",
        "manual_restart_exec",
    )
    key = lifecycle.RESTART_ATTEMPT_ENV
    previous = os.environ.get(key)
    captured = {}

    def blocked_exec(executable, argv, child_env):
        captured.update(
            executable=executable,
            argv=list(argv),
            child_env=dict(child_env),
        )
        raise RuntimeError("exec blocked by smoke test")

    try:
        os.environ[key] = "6"
        child_env = {key: "6", "SAFE_SENTINEL": "kept"}
        quiet_logger = type(
            "QuietLifecycleLogger",
            (),
            {"warning": lambda *_args, **_kwargs: None},
        )()
        with (
            patch.object(lifecycle.os, "execvpe", new=blocked_exec),
            patch.object(lifecycle.logging, "getLogger", return_value=quiet_logger),
        ):
            try:
                lifecycle.exec_fresh_process(
                    package_name,
                    reason="manual-restart-test",
                    clear_crash_state=True,
                    env=child_env,
                )
            except RuntimeError as ex:
                assert str(ex) == "exec blocked by smoke test"
            else:
                raise AssertionError("patched exec unexpectedly returned")
        # An explicit child environment is sanitized without mutating the
        # parent process; the inherited/default path is covered by the
        # backoff test's clear_crash_restart_state() assertion.
        assert os.environ[key] == "6"
        assert captured["executable"] == sys.executable
        assert captured["argv"] == [sys.executable, "-m", package_name]
        assert key not in captured["child_env"]
        assert captured["child_env"]["SAFE_SENTINEL"] == "kept"
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def test_song_command_pipeline_guards():
    package_name = "AnonX_3"
    song_path = ROOT / package_name / "plugins" / "song.py"
    source = song_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parser_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_parse_song_tokens"
    )
    cache_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_media_cache_id"
    )
    namespace = {
        "_VIDEO_FLAGS": {"-v", "--video"},
        "_VIDEO_CACHE_REVISION": "thumb-v1",
    }
    exec(
        compile(
            ast.Module(body=[parser_node, cache_node], type_ignores=[]),
            str(song_path),
            "exec",
        ),
        namespace,
    )
    parse = namespace["_parse_song_tokens"]
    cache_id = namespace["_media_cache_id"]

    assert parse(["song", "attention"], None) == (False, "attention")
    assert parse(["song", "-v", "attention"], None) == (False, "attention")
    assert parse(["vsong", "attention"], None) == (True, "attention")
    assert parse(
        ["song"],
        "/song@MusicBot --video attention please",
    ) == (False, "attention please")
    assert parse(
        ["vsong"],
        "/vsong@MusicBot attention please",
    ) == (True, "attention please")
    assert cache_id("media-id", False) == "media-id"
    assert cache_id("media-id", True) == "media-id:thumb-v1"
    assert 'filters.command(["song", "vsong"]) & ~app.bl_users, group=-1' in source
    assert 'video = command_name == "vsong"' in source
    assert 'return f"{media_id}:{_VIDEO_CACHE_REVISION}"' in source
    assert "await thumb.generate_video_thumb(track)" in source
    assert '"thumb": video_thumb' in source
    assert "cancel_kb = buttons.cancel_dl(" in source
    assert "cancel_kb = buttons.cancel_dl_pyrogram(" not in source
    assert "sent = await utils.reply_formatted(" in source
    assert 'template_key="song_searching"' in source
    assert "if sent is None:" in source
    assert 'm.lang["song_downloading"]' not in source
    assert "downloading_sent" not in source
    assert "await sent.edit_text(m.lang[\"song_downloading\"])" not in source
    button_factory = __import__(
        f"{package_name}.helpers", fromlist=["buttons"]
    ).buttons
    cancel_markup = button_factory.cancel_dl("Cancel")
    assert isinstance(cancel_markup, dict)
    cancel_button = cancel_markup["inline_keyboard"][0][0]
    assert cancel_button["style"] == "danger"
    assert cancel_button["callback_data"] == "cancel_dl"
    assert "message_id=m.id" in source
    assert "progress_message=sent" in source
    assert "url = _first_url(query) or (utils.get_url(m) if not query else None)" in source
    assert 'partial = target.with_name(f"{target.stem}.part{target.suffix}")' in source
    assert "os.replace(partial, target)" in source

    bot_source = (ROOT / package_name / "core" / "bot.py").read_text(
        encoding="utf-8"
    )
    assert 'BotCommand("song", "Download audio' in bot_source
    assert 'BotCommand("vsong", "Download video with a thumbnail")' in bot_source


def test_inline_vsong_search_guards():
    from types import SimpleNamespace

    from pyrogram import enums, types

    package_name = "AnonX_3"
    inline_path = ROOT / package_name / "plugins" / "inline_search.py"
    source = inline_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    command_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_vsong_command"
    )
    builder_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_build_inline_result"
    )
    namespace = {
        "_YOUTUBE_VIDEO_ID_RE": re.compile(r"^[A-Za-z0-9_-]{11}$"),
        "_BOT_USERNAME_RE": re.compile(r"^[A-Za-z0-9_]{5,32}$"),
        "enums": enums,
        "types": types,
    }
    exec(
        compile(
            ast.Module(body=[command_node, builder_node], type_ignores=[]),
            str(inline_path),
            "exec",
        ),
        namespace,
    )
    command = namespace["_vsong_command"]

    assert command("dQw4w9WgXcQ", "MusicBot") == (
        "/vsong@MusicBot https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        len("/vsong@MusicBot"),
    )
    assert command("dQw4w9WgXcQ", None) == (
        "/vsong https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        len("/vsong"),
    )
    assert command("invalid id", "MusicBot") is None

    article = namespace["_build_inline_result"](
        SimpleNamespace(
            id="dQw4w9WgXcQ",
            title="Inline Result",
            channel_name="Channel",
            duration="03:32",
            thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        ),
        "MusicBot",
    )
    assert isinstance(article, types.InlineQueryResultArticle)
    content = article.input_message_content
    assert content.message_text == (
        "/vsong@MusicBot https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert content.link_preview_options.is_disabled is True
    assert len(content.entities) == 1
    assert content.entities[0].type == enums.MessageEntityType.BOT_COMMAND
    assert content.entities[0].offset == 0
    assert content.entities[0].length == len("/vsong@MusicBot")

    assert "@app.on_inline_query(group=-1)" in source
    assert "await asyncio.sleep(INLINE_DEBOUNCE_SEC)" in source
    assert "async with _INLINE_SEARCH_LIMITER" in source
    assert "await asyncio.wait_for(" in source
    assert "yt.deep_search(" in source
    assert "video=True" in source
    assert "limit=INLINE_RESULT_LIMIT" in source
    assert "types.InlineQueryResultArticle(" in source
    assert "types.InputTextMessageContent(" in source
    assert "enums.MessageEntityType.BOT_COMMAND" in source
    assert "types.LinkPreviewOptions(is_disabled=True)" in source
    assert "is_personal=True" in source
    assert "user_id in app.bl_users" in source
    assert "len(results) >= INLINE_RESULT_LIMIT" in source


def test_global_silent_kick_watchlist_guards():
    package_name = "AnonX_3"
    plugin_source = (
        ROOT / package_name / "plugins" / "global_kick.py"
    ).read_text(encoding="utf-8")
    mongo_source = (ROOT / package_name / "core" / "mongo.py").read_text(
        encoding="utf-8"
    )
    moderation_source = (
        ROOT / package_name / "plugins" / "moderation.py"
    ).read_text(encoding="utf-8")

    for command in ('filters.command(["kick"])', 'filters.command(["unkick"])',
                    'filters.command(["kicklist"])',
                    'filters.command(["sudolists"])'):
        assert command in plugin_source
    assert plugin_source.count("filters.chat(app.logger)") == 4
    assert plugin_source.count("& app.sudoers") == 4
    assert plugin_source.count("group=-1") >= 4
    assert "await _silent_delete(m)" in plugin_source
    assert "await db.add_global_kick(" in plugin_source
    assert "await db.del_global_kick(user_id)" in plugin_source
    assert "entries = await db.get_global_kicks()" in plugin_source
    assert "await app.ban_chat_member(chat_id, user_id)" in plugin_source
    assert "await app.unban_chat_member(chat_id, user_id)" in plugin_source
    assert "filters.new_chat_members & filters.group" in plugin_source
    assert "enforce_global_kick_on_message" in plugin_source
    assert "self.global_kicksdb = self.db.global_kicks" in mongo_source
    assert "async def get_global_kick_ids" in mongo_source
    assert "& ~filters.chat(app.logger)" in moderation_source


def test_shared_edit_text_defaults_to_html():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pyrogram import enums

    from AnonX_3.helpers import utils

    async def verify():
        message = SimpleNamespace(
            id=991001,
            chat=SimpleNamespace(id=-100991001),
        )
        edit = AsyncMock(return_value=message)
        message.edit_text = edit

        await utils.edit_text(message, "<b>formatted</b>")
        assert edit.await_args.kwargs["parse_mode"] == enums.ParseMode.HTML

        edit.reset_mock()
        await utils.edit_text(
            message,
            "<b>literal</b>",
            parse_mode=enums.ParseMode.DISABLED,
        )
        assert edit.await_args.kwargs["parse_mode"] == enums.ParseMode.DISABLED

    asyncio.run(verify())


def test_broadcast_pin_contract():
    import importlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    broadcast_source = (
        ROOT / "AnonX_3" / "plugins" / "broadcast.py"
    ).read_text(encoding="utf-8")
    docs_source = (ROOT / "broadcast.md").read_text(encoding="utf-8")
    en_locale = json.loads(
        (ROOT / "AnonX_3" / "locales" / "en.json").read_text(encoding="utf-8")
    )
    my_locale = json.loads(
        (ROOT / "AnonX_3" / "locales" / "my.json").read_text(encoding="utf-8")
    )

    assert 'pin_requested = "-pin" in options' in broadcast_source
    assert "if pin_requested and chat in group_set:" in broadcast_source
    assert 'return ("user_ok", chat, None, None)' in broadcast_source
    assert "disable_notification=True" in broadcast_source
    assert "pin_failures += 1" in broadcast_source
    assert "gcast_pin_summary" in broadcast_source
    assert "[-pin]" in docs_source
    assert "Private users" in docs_source
    assert "gcast_pin_summary" in en_locale
    assert "gcast_pin_summary" in my_locale
    assert "-pin" in en_locale["help_sudo"]
    assert "-pin" in my_locale["help_sudo"]

    broadcast_module = importlib.import_module("AnonX_3.plugins.broadcast")

    async def verify():
        delivered = SimpleNamespace(pin=AsyncMock())
        assert await broadcast_module._pin_broadcast_message(delivered, -100991002) is None
        delivered.pin.assert_awaited_once_with(disable_notification=True)

        failing = SimpleNamespace(
            pin=AsyncMock(side_effect=RuntimeError("not admin"))
        )
        error = await broadcast_module._pin_broadcast_message(failing, -100991003)
        assert error and "pin failed" in error

    asyncio.run(verify())


def test_release_identity_and_packaging_guards():
    variant = (ROOT / "VARIANT.txt").read_text(encoding="utf-8")
    sample = (ROOT / "sample.env").read_text(encoding="utf-8")
    setup = (ROOT / "setup").read_text(encoding="utf-8")
    utilities = (
        ROOT / "AnonX_3" / "helpers" / "_utilities.py"
    ).read_text(encoding="utf-8")
    merge_env = (ROOT / "ops" / "merge_env_full.py").read_text(encoding="utf-8")
    build = (ROOT / "ops" / "build_release.py").read_text(encoding="utf-8")
    structure = (ROOT / "ops" / "verify_structure.py").read_text(
        encoding="utf-8"
    )
    gate = (ROOT / "ops" / "release_gate.py").read_text(encoding="utf-8")

    assert "VARIANT=AnonX" in variant
    assert "COPIED_FROM" not in variant
    assert "# AnonX_3 MINIMAL .env" in sample
    assert "MONGO_URL=mongodb://localhost:27017/AnonX_3" in sample
    assert "AnonX_3 Music Installation Setup" in setup
    assert "python3 -m pip check" in setup
    assert "continuing" not in setup.lower()
    assert 'user.mention if user else "Anonymous"' in utilities

    assert 'ROOT = Path(__file__).resolve().parents[1]' in merge_env
    assert "project_identity()" in merge_env
    assert "WORKSPACE" not in merge_env
    assert "AnonX_10" not in merge_env

    source_lines = [
        line.strip()
        for line in (ROOT / "requirements.in").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.startswith("#")
    ]
    lock_lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert source_lines
    assert lock_lines
    assert all("==" in line for line in lock_lines)

    assert "from release_meta import" in build
    assert '".instance.lock"' in build
    assert '"lifecycle.py"' in structure
    assert 'PROJECT = "AnonX_3"' not in build
    assert 'VERSION = "3.2.1"' not in build
    assert "dependency consistency" in gate
    assert "release build 2" in gate
    assert "Release archive is not deterministic" in gate

    sys.path.insert(0, str(ROOT / "ops"))
    try:
        import release_meta

        assert release_meta.PROJECT == "AnonX_3"
        assert release_meta.VERSION == "3.4.10"
        assert release_meta.RELEASE_DATE == "2026-08-09"
        assert release_meta.ARCHIVE_NAME == "AnonX_3-v3.4.10-final.zip"
    finally:
        sys.path.pop(0)


def test_youtube_exact_cli_runtime_builder_guards():
    """Focused guards for authenticated YouTube playable-media integration."""
    from unittest.mock import patch

    from AnonX_3 import config as app_config
    from AnonX_3.core.youtube import YouTube, YouTubeRuntimeConfigError

    youtube_source = (ROOT / "AnonX_3" / "core" / "youtube.py").read_text(
        encoding="utf-8"
    )
    prefetch_source = (ROOT / "AnonX_3" / "core" / "prefetch.py").read_text(
        encoding="utf-8"
    )
    calls_source = (ROOT / "AnonX_3" / "core" / "calls.py").read_text(
        encoding="utf-8"
    )
    direct_body = youtube_source[
        youtube_source.index("async def resolve_direct_stream_source(") :
        youtube_source.index("    def _metadata_from_direct_info(")
    ]
    download_body = youtube_source[
        youtube_source.index("async def download(") :
        youtube_source.index("    def attach_download_watcher(")
    ]
    pyyt_body = youtube_source[
        youtube_source.index("async def _pyyt_search_tracks(") :
        youtube_source.index("    def _ytdlp_proxy_attempts(")
    ]
    assert "build_ytdlp_api_opts(" in direct_body
    assert 'action="direct"' in direct_body
    assert "create_youtube_dl(opts, yt_dlp.YoutubeDL)" in direct_body
    assert direct_body.index("build_ytdlp_api_opts(") < direct_body.index(
        "create_youtube_dl(opts, yt_dlp.YoutubeDL)"
    )
    assert "build_ytdlp_api_opts(" in download_body
    assert 'action="download"' in download_body
    assert "create_youtube_dl(opts, yt_dlp.YoutubeDL)" in download_body
    assert download_body.index("build_ytdlp_api_opts(") < download_body.index(
        "create_youtube_dl(opts, yt_dlp.YoutubeDL)"
    )
    assert "yt.download(" in prefetch_source
    assert "yt_dlp.YoutubeDL(" not in prefetch_source
    assert "build_ytdlp_api_opts(" in download_body
    assert "Local fallback" in calls_source
    assert "yt.download(" in calls_source
    assert "yt_dlp.YoutubeDL(" not in calls_source
    assert "yt_dlp.YoutubeDL(" not in pyyt_body
    assert "resolve_direct_stream" not in pyyt_body
    assert "youtube_authenticated_runtime_failed=True " in youtube_source
    assert '"action=download video_id=%s class=%s msg=%s"' in youtube_source
    assert "youtube_authenticated_runtime_failed=True action=direct" in youtube_source
    assert 'reason="download-auth-challenge"' not in youtube_source
    assert 'reason="direct-bot-check"' not in youtube_source

    with tempfile.TemporaryDirectory() as td:
        cookie_path = Path(td) / "youtube-cookies.txt"
        cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        with (
            patch.object(app_config, "YTDLP_BINARY", "/usr/local/bin/yt-dlp"),
            patch.object(app_config, "YOUTUBE_COOKIE_FILE", str(cookie_path)),
            patch.object(app_config, "YTDLP_JS_RUNTIME", "deno:/usr/local/bin/deno"),
            patch.object(app_config, "YTDLP_REMOTE_COMPONENTS", "ejs:github"),
            patch.object(app_config, "PO_TOKEN_PROVIDER_ENABLED", True),
            patch.object(app_config, "PO_TOKEN_PROVIDER_URL", "http://127.0.0.1:4416"),
            patch.object(app_config, "POT_PROVIDER_URL", "http://127.0.0.1:4416"),
            patch.object(
                app_config,
                "YTDLP_POT_SERVER_HOME",
                "/root/bgutil-ytdlp-pot-provider/server",
            ),
            patch.object(app_config, "COOKIE_FREE_MODE", False),
        ):
            yt_service = YouTube()
            opts = yt_service.build_ytdlp_api_opts(
                action="direct",
                video_id="dQw4w9WgXcQ",
                validate_cookie=True,
            )
            cli_args = yt_service.build_ytdlp_cli_args(
                action="direct",
                video_id="dQw4w9WgXcQ",
                validate_cookie=True,
            )
        assert opts["ignoreconfig"] is True
        assert opts["cookiefile"] == str(cookie_path)
        assert opts["remote_components"] == ["ejs:github"]
        assert opts["js_runtimes"] == {
            "deno": {"path": "/usr/local/bin/deno"}
        }
        assert not isinstance(opts["js_runtimes"], str)
        assert "--js-runtimes" in cli_args
        assert cli_args[cli_args.index("--js-runtimes") + 1] == (
            "deno:/usr/local/bin/deno"
        )
        assert opts["extractor_args"]["youtube"]["player_client"] == ["mweb"]
        assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
            "http://127.0.0.1:4416"
        ]
        assert opts["extractor_args"]["youtubepot-bgutilscript"]["server_home"] == [
            "/root/bgutil-ytdlp-pot-provider/server"
        ]

        missing_path = Path(td) / "missing.txt"
        with (
            patch.object(app_config, "YOUTUBE_COOKIE_FILE", str(missing_path)),
            patch.object(app_config, "COOKIE_FREE_MODE", False),
        ):
            try:
                YouTube().build_ytdlp_api_opts(
                    action="download",
                    video_id="dQw4w9WgXcQ",
                    validate_cookie=True,
                )
            except YouTubeRuntimeConfigError:
                pass
            else:
                raise AssertionError("missing cookie file must fail closed")


def main() -> int:
    tests = [
        test_runner_anchors_working_directory,
        test_cache_keys_states,
        test_matcher,
        test_singleflight,
        test_singleflight_shutdown_cancels_factory_ownership,
        test_provider_shutdown_owns_cache_and_download_tasks,
        test_security,
        test_metrics_race,
        test_race_matrix_complete,
        test_quality_plan,
        test_probe_soft_403_does_not_block,
        test_prefetch_same_media_and_pick_ready,
        test_completed_current_cache_survives_until_playback_handoff,
        test_security_ssrf,
        test_gate_fatal_event,
        test_parallel_initial_readiness,
        test_youtube_client_ladder,
        test_direct_watchdog,
        test_stream_end_natural_vs_gate,
        test_store,
        test_normalized_title_cache_lookup_returns_valid_local_entry,
        test_resolve_source_prefers_cache_before_search_or_extraction,
        test_resolve_source_classifies_provider_failures_separately,
        test_search_deadline_allows_slow_valid_pyyt_result,
        test_resolve_source_uses_ytdlp_search_after_provider_miss,
        test_play_and_startup_failure_boundaries,
        test_process_instance_lock_cross_process,
        test_crash_restart_backoff_contract,
        test_bootstrap_restart_guard_contract,
        test_package_bootstrap_lock_precedes_service_construction,
        test_run_process_crash_and_terminal_outcomes,
        test_global_stop_is_concurrent_ordered_and_failure_tolerant,
        test_supervisor_shutdown_owns_and_forgets_tasks,
        test_tgcall_shutdown_cleans_all_owned_state,
        test_partial_pyrogram_storage_cleanup_and_session_invariants,
        test_one_shot_download_publishes_stream_to_concurrent_waiter_once,
        test_one_shot_download_failure_does_not_retry_ytdlp,
        test_download_stream_singleflight_keys_partition_audio_and_video,
        test_sudo_filter_and_startup_order,
        test_assistant_startup_race_waits_for_readiness,
        test_log_regression_guards,
        test_vc_auto_unmute_guards,
        test_required_unmute_rolls_back_empty_call,
        test_parallel_initial_youtube_start_guards,
        test_ffmpeg_observer_emits_cold_start_milestones,
        test_callback_feedback_uses_non_modal_banners,
        test_all_sudo_commands_use_early_dispatch_group,
        test_simple_delete_filter_guards,
        test_filter_strike_moderation_guards,
        test_fast_first_play_guards,
        test_play_not_found_closes_progress_before_edit,
        test_now_playing_deletes_orphaned_download_progress_card,
        test_local_cookie_agent_guards,
        test_added_to_queue_card_guards,
        test_auto_learn_lifecycle,
        test_auto_learn_confirmation_gate,
        test_aidj_mode_and_autoplay_persistence,
        test_unified_request_context_and_priority_queue,
        test_source_circuit_and_transition_capability,
        test_custom_template_and_prefetch_guards,
        test_po_token_video_binding_and_403_rotation,
        test_cancel_lifecycle_and_platform_recovery_guards,
        test_tiktok_audio_artifact_guards,
        test_parallel_external_source_guards,
        test_youtube_live_progress_pipeline,
        test_runtime_log_isolation_and_media_progress_edit,
        test_soundcloud_proxy_and_video_guards,
        test_direct_stream_codec_and_pyyt_proxy_compatibility,
        test_stop_is_idempotent_and_benign_leave_is_terminal,
        test_vc_preflight_and_initial_playback_lock_guards,
        test_stop_recleans_queue_after_no_vc_cleanup,
        test_vc_watcher_only_cleans_up_on_video_chat_end,
        test_queue_remove_request_is_exact_for_duplicate_media_ids,
        test_play_request_scope_blocks_late_background_work,
        test_stream_media_vc_admission_transaction_guards,
        test_ytdlp_first_constructor_is_singleflight,
        test_stale_queued_status_is_noncritical_without_runtime_deps,
        test_log_error_hardening_guards,
        test_ai_degraded_mode_guards,
        test_youtube_auth_challenge_short_circuit,
        test_direct_youtube_metadata_propagation,
        test_fast_release_hot_paths,
        test_ai_assistant_accuracy_agent_guards,
        test_manual_restart_detaches_shutdown_from_handler,
        test_song_command_pipeline_guards,
        test_inline_vsong_search_guards,
        test_global_silent_kick_watchlist_guards,
        test_shared_edit_text_defaults_to_html,
        test_broadcast_pin_contract,
        test_release_identity_and_packaging_guards,
        test_youtube_exact_cli_runtime_builder_guards,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as ex:
            failed += 1
            print(f"FAIL {fn.__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
