#!/usr/bin/env python3
"""Dynamic Resource Control tests (no pytest required).

Run from the AnonX deploy root:
  python tests/test_dynamic_resource_control.py

Covers the four scenarios the feature was specified against:
  * multi-group playback   — many groups /play at once ⇒ capacity grows, bounded
  * queue auto-next        — prefetch runs background, promoted when joined
  * fallback               — any internal failure ⇒ exactly the fixed MAX_* limits
  * CPU/RAM pressure       — high pressure ⇒ shrink, without cutting live jobs
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("AnonX_TESTING", "1")

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / "AnonX_3" / "core" / "dynamic_capacity.py"
PREFETCH_PATH = ROOT / "AnonX_3" / "core" / "prefetch.py"
YOUTUBE_PATH = ROOT / "AnonX_3" / "core" / "youtube.py"
HEALTH_PATH = ROOT / "AnonX_3" / "core" / "health.py"
RESOURCE_PATH = ROOT / "AnonX_3" / "core" / "resource_manager.py"
THUMB_PATH = ROOT / "AnonX_3" / "helpers" / "_thumbnails.py"

DEFAULT_CFG = {
    "MAX_YTDLP_CONCURRENT": 2,
    "MAX_DOWNLOAD_CONCURRENT": 2,
    "MAX_VIDEO_JOBS": 1,
    "MAX_FFMPEG_CONCURRENT": 3,
    "MAX_ACTIVE_STREAMS": 20,
    "DYNAMIC_RESOURCE_CONTROL": True,
    "DYNAMIC_CAPACITY_MAX_MULTIPLIER": 4.0,
    "DYNAMIC_PRESSURE_GROW_BELOW": 0.55,
    "DYNAMIC_PRESSURE_RELIEF": 0.85,
    "DYNAMIC_LOOP_LAG_HIGH_MS": 250,
    "DYNAMIC_LOAD_PER_CORE_HIGH": 1.5,
    "DYNAMIC_RECOMPUTE_INTERVAL_SEC": 2,
    "DYNAMIC_BACKGROUND_PAUSE": True,
    "DYNAMIC_FOREGROUND_RESERVE": 1,
    "ADAPTIVE_CPU_HIGH": 70,
    "ADAPTIVE_RAM_HIGH": 88,
}

_LANE_OVERRIDES = [
    f"DYNAMIC_{lane}_{kind}"
    for lane in ("YTDLP", "DOWNLOADS", "VIDEO", "FFMPEG", "STREAMS")
    for kind in ("CEILING", "FLOOR")
]


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _install_config_stub(**overrides):
    """Provide a minimal ``AnonX_3.config`` so the module loads standalone.

    ``dynamic_capacity`` imports config lazily inside ``_cfg`` precisely so it
    stays testable without booting Pyrogram/Mongo.
    """
    saved = {
        name: sys.modules.get(name) for name in ("AnonX_3", "AnonX_3.config")
    }
    pkg = types.ModuleType("AnonX_3")
    pkg.__path__ = []  # namespace-ish; nothing else is imported from it
    cfg = types.ModuleType("AnonX_3.config")
    values = dict(DEFAULT_CFG)
    for name in _LANE_OVERRIDES:
        values.setdefault(name, 0)
    values.update(overrides)
    for key, value in values.items():
        setattr(cfg, key, value)
    pkg.config = cfg
    sys.modules["AnonX_3"] = pkg
    sys.modules["AnonX_3.config"] = cfg
    return cfg, saved


def _restore_config_stub(saved: dict) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


_SEQ = 0


def _load_module():
    global _SEQ
    _SEQ += 1
    name = f"_dyncap_test_{os.getpid()}_{_SEQ}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _FakeVM:
    def __init__(self, percent: float, total_mb: float = 8192.0) -> None:
        self.percent = percent
        self.total = int(total_mb * 1024 * 1024)
        self.available = int(self.total * (1.0 - percent / 100.0))


class _FakePsutil:
    """Deterministic CPU/RAM so pressure tests do not depend on the host."""

    def __init__(self, cpu: float, ram: float, total_mb: float = 8192.0) -> None:
        self.cpu = cpu
        self.ram = ram
        self.total_mb = total_mb

    def cpu_percent(self, interval=None):  # noqa: D401 - psutil signature
        return self.cpu

    def virtual_memory(self):
        return _FakeVM(self.ram, self.total_mb)

    def cpu_count(self, logical=True):
        return 4


def _make_controller(module, *, cpu=0.0, ram=0.0, cores=4, lag_ms=0.0):
    """Fresh controller with pinned machine signals."""
    module.psutil = _FakePsutil(cpu, ram, 8192.0)
    module._cpu_count = lambda: cores
    module._total_ram_mb = lambda: 8192.0
    # loadavg is host-dependent; pin it out of the pressure calculation.
    module.os = types.SimpleNamespace(
        getloadavg=None,
        environ=os.environ,
        cpu_count=lambda: cores,
    )
    ctl = module.DynamicCapacityController()
    ctl._cpu_cached = cpu
    ctl._ram_cached = ram
    ctl._lag.observe_external(lag_ms)
    ctl._ensure_lanes()
    return ctl


def _run(coro, *, timeout: float = 10.0):
    """Run a scenario under a hard timeout.

    A capacity regression shows up as a permit that is never granted, so an
    unguarded run would hang the suite instead of failing it.
    """

    async def _guard():
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(_guard())


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. fallback contract
# ---------------------------------------------------------------------------


def test_disabled_controller_uses_exactly_the_fixed_limits():
    _, saved = _install_config_stub(DYNAMIC_RESOURCE_CONTROL=False)
    try:
        module = _load_module()
        ctl = _make_controller(module)
        assert ctl.enabled() is False
        assert ctl.capacity("ytdlp") == 2
        assert ctl.capacity("downloads") == 2
        assert ctl.capacity("video") == 1
        assert ctl.capacity("ffmpeg") == 3
        assert ctl.capacity("streams") == 20
        assert ctl.snapshot()["mode"] == "fixed"
    finally:
        _restore_config_stub(saved)


def test_internal_failure_latches_back_to_fixed_limits():
    """Any error inside the dynamic layer ⇒ permanent fixed-limit fallback."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=5.0, ram=20.0)

        # Grow first so the degrade is observable as a real change.
        ctl._lanes["downloads"].capacity = 6
        assert ctl.enabled() is True

        # Simulate a planning bug.
        ctl._plan = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        assert ctl.recompute(force=True) is None

        assert ctl.degraded is True
        assert ctl.enabled() is False
        assert "recompute_failed" in ctl.degraded_reason
        for lane, expected in (
            ("ytdlp", 2), ("downloads", 2), ("video", 1), ("ffmpeg", 3), ("streams", 20)
        ):
            assert ctl.capacity(lane) == expected, lane
            assert ctl._lanes[lane].capacity == expected, lane

        snap = ctl.snapshot()
        assert snap["mode"] == "fixed"
        assert snap["degraded"] is True

        # Acquire/release must still work after the latch (never a hard failure).
        async def scenario():
            assert await ctl.acquire("downloads") is True
            ctl.release("downloads")

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_dynamic_semaphore_falls_back_to_a_real_bounded_semaphore():
    """Fallback is a real limit, never "unlimited"."""
    _, saved = _install_config_stub(DYNAMIC_RESOURCE_CONTROL=False)
    try:
        module = _load_module()
        ctl = _make_controller(module)
        sem = module.DynamicSemaphore("downloads", ctl)

        async def scenario():
            await sem.acquire()
            await sem.acquire()
            assert sem._fallback_size == 2  # == MAX_DOWNLOAD_CONCURRENT
            third = asyncio.create_task(sem.acquire())
            await asyncio.sleep(0.05)
            assert not third.done(), "fallback semaphore must block past its size"
            sem.release()
            await asyncio.wait_for(third, timeout=1.0)
            sem.release()
            sem.release()

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_dynamic_semaphore_release_matches_its_acquire_mode():
    """A mid-flight degrade must not mismatch acquire/release accounting."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=5.0, ram=20.0)
        sem = module.DynamicSemaphore("downloads", ctl)

        async def scenario():
            await sem.acquire()  # dynamic mode
            assert ctl._lanes["downloads"].active == 1
            ctl._degrade("test_midflight")
            sem.release()  # must release the dynamic permit, not the fallback
            assert ctl._lanes["downloads"].active == 0
            assert sem._fallback is None
            # Post-degrade acquisitions use the bounded fallback.
            await sem.acquire()
            assert sem._fallback is not None
            sem.release()

        _run(scenario())
    finally:
        _restore_config_stub(saved)


# ---------------------------------------------------------------------------
# 2. multi-group playback
# ---------------------------------------------------------------------------


def test_multi_group_playback_scales_up_but_stays_bounded():
    """Many groups /play at once on an idle box ⇒ capacity grows, never unlimited."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=4.0, ram=15.0, cores=4)
        start = ctl.capacity("downloads")
        assert start == 2, "must start at the configured limit, not above it"

        async def scenario():
            held = []
            # 10 groups hit /play simultaneously.
            tasks = [
                asyncio.create_task(ctl.acquire("downloads"))
                for _ in range(10)
            ]
            await asyncio.sleep(0.15)
            grown = ctl.capacity("downloads")
            assert grown > start, f"idle box + demand must scale up ({grown} <= {start})"
            assert grown <= ctl._lanes["downloads"].ceiling
            assert ctl._lanes["downloads"].ceiling <= 8, "4.0x of 2 is the hard bound"

            admitted = sum(1 for t in tasks if t.done())
            assert admitted == grown, (
                f"admitted {admitted} but capacity is {grown}"
            )
            # Drain: every queued group is eventually served (no starvation).
            for _ in range(len(tasks)):
                pending = [t for t in tasks if not t.done()]
                if not pending:
                    break
                ctl.release("downloads")
                await asyncio.sleep(0)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
            held.extend(tasks)
            assert all(t.result() is True for t in held)

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_quiet_bot_stays_cheap():
    """No demand ⇒ never above the configured limits (light CPU/RAM when idle)."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=2.0, ram=10.0)
        plan = ctl.recompute(force=True)
        assert plan is not None
        assert plan.limits["ytdlp"] == 2
        assert plan.limits["downloads"] == 2
        assert plan.limits["video"] == 1
        assert plan.limits["ffmpeg"] == 3
    finally:
        _restore_config_stub(saved)


def test_no_unlimited_workers_even_with_absurd_config():
    _, saved = _install_config_stub(
        DYNAMIC_CAPACITY_MAX_MULTIPLIER=9999.0,
        MAX_DOWNLOAD_CONCURRENT=2,
    )
    try:
        module = _load_module()
        ctl = _make_controller(module, cores=4)
        fixed = ctl._fixed_limits()
        ceilings = ctl._ceilings(fixed)
        for lane, value in ceilings.items():
            assert isinstance(value, int)
            assert value >= fixed[lane]
            # multiplier is clamped to 8.0 and further bounded by cores/RAM
            assert value <= max(fixed[lane], fixed[lane] * 8), lane
            assert value < 10_000, lane
    finally:
        _restore_config_stub(saved)


# ---------------------------------------------------------------------------
# 3. CPU / RAM pressure
# ---------------------------------------------------------------------------


def test_high_cpu_pressure_scales_down_to_the_floor():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=98.0, ram=30.0)
        plan = ctl.recompute(force=True)
        assert plan is not None
        assert plan.pressure >= 0.85, plan.reason
        assert plan.limits["ytdlp"] == 1
        assert plan.limits["downloads"] == 1
        assert plan.limits["video"] == 1
        assert plan.limits["ffmpeg"] == 1
        # Live VC sessions are never evicted by a shrink.
        assert plan.limits["streams"] == 20
    finally:
        _restore_config_stub(saved)


def test_high_ram_pressure_scales_down():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=5.0, ram=99.0)
        plan = ctl.recompute(force=True)
        assert plan is not None
        assert "ram" in plan.reason
        assert plan.limits["ffmpeg"] == 1
    finally:
        _restore_config_stub(saved)


def test_event_loop_lag_alone_scales_down():
    """Lag is the VC-stutter predictor CPU% misses."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=1.0, ram=5.0, lag_ms=800.0)
        plan = ctl.recompute(force=True)
        assert plan is not None
        assert "lag" in plan.reason, plan.reason
        assert plan.pressure >= 0.85
        assert plan.limits["downloads"] == 1
    finally:
        _restore_config_stub(saved)


def test_shrink_never_cuts_running_jobs():
    """Capacity shrink removes future permits only."""
    # Configured limit of 6 so five concurrent FFmpeg jobs are legitimately
    # admitted on an idle box before the pressure spike.
    _, saved = _install_config_stub(MAX_FFMPEG_CONCURRENT=6)
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=3.0, ram=10.0)

        async def scenario():
            # Five live FFmpeg jobs, admitted through the real acquire path.
            for _ in range(5):
                assert await ctl.acquire("ffmpeg") is True
            assert ctl._lanes["ffmpeg"].active == 5

            # The box now hurts badly.
            module.psutil.cpu = 99.0
            ctl._cpu_cached = 99.0
            ctl._last_cpu_sample = 0.0
            plan = ctl.recompute(force=True)
            assert plan is not None
            assert ctl.capacity("ffmpeg") == 1, "must shrink"
            assert ctl._lanes["ffmpeg"].active == 5, "running jobs kept their permit"
            assert ctl._lanes["ffmpeg"].free() == 0

            # They exit normally, one by one, with no accounting damage.
            for _ in range(5):
                ctl.release("ffmpeg")
            assert ctl._lanes["ffmpeg"].active == 0

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_stream_registration_floor_protects_live_sessions():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=99.0, ram=99.0, lag_ms=2000.0)
        fixed = ctl._fixed_limits()
        floors = ctl._floors(fixed)
        assert floors["streams"] == fixed["streams"] == 20
        ctl.recompute(force=True)
        assert ctl.capacity("streams") >= 20
        assert ctl.try_admit("streams") is True
    finally:
        _restore_config_stub(saved)


# ---------------------------------------------------------------------------
# 4. foreground priority / background pause
# ---------------------------------------------------------------------------


def test_priority_default_is_foreground_and_background_opts_in():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        assert module.current_priority() == int(module.Priority.FOREGROUND)
        with module.background_scope():
            assert module.current_priority() == int(module.Priority.BACKGROUND)
            with module.foreground_scope():
                assert module.current_priority() == int(module.Priority.FOREGROUND)
            assert module.current_priority() == int(module.Priority.BACKGROUND)
        assert module.current_priority() == int(module.Priority.FOREGROUND)
    finally:
        _restore_config_stub(saved)


def test_foreground_is_served_before_background():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=90.0, ram=90.0)  # pinned at floor 1
        ctl.recompute(force=True)
        assert ctl.capacity("downloads") == 1

        order: list[str] = []

        async def scenario():
            assert await ctl.acquire("downloads") is True  # occupies the only permit

            async def bg():
                with module.background_scope():
                    await ctl.acquire("downloads")
                order.append("background")

            async def fg():
                await ctl.acquire("downloads")
                order.append("foreground")

            bg_task = asyncio.create_task(bg())
            await asyncio.sleep(0.05)      # background queues first
            fg_task = asyncio.create_task(fg())
            await asyncio.sleep(0.05)
            assert ctl._lanes["downloads"].waiting(module.Priority.BACKGROUND) == 1
            assert ctl._lanes["downloads"].waiting(module.Priority.FOREGROUND) == 1

            ctl.release("downloads")
            await asyncio.sleep(0.05)
            assert order == ["foreground"], (
                f"foreground must jump the queue, got {order}"
            )
            ctl.release("downloads")
            await asyncio.wait_for(asyncio.gather(bg_task, fg_task), timeout=2.0)
            assert order == ["foreground", "background"]
            ctl.release("downloads")

        _run(scenario())
        assert ctl._stats["foreground_preempts"] >= 1
    finally:
        _restore_config_stub(saved)


def test_background_pauses_globally_while_a_foreground_request_waits():
    """A waiting /play must not sit behind a thumbnail or prefetch job."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=90.0, ram=90.0)
        ctl.recompute(force=True)
        # downloads is contended; ffmpeg is free.
        assert ctl.capacity("downloads") == 1
        ctl._lanes["ffmpeg"].capacity = 3

        async def scenario():
            await ctl.acquire("downloads")
            fg = asyncio.create_task(ctl.acquire("downloads"))
            await asyncio.sleep(0.05)
            assert ctl.foreground_waiting() == 1

            # Background work in a *different, free* lane is paused too.
            with module.background_scope():
                bg = asyncio.create_task(ctl.acquire("ffmpeg"))
            await asyncio.sleep(0.05)
            assert not bg.done(), "background must pause while foreground waits"
            assert ctl._stats["background_pauses"] >= 1

            ctl.release("downloads")             # foreground served
            await asyncio.wait_for(fg, timeout=2.0)
            await asyncio.wait_for(bg, timeout=2.0)   # background resumes
            assert ctl.foreground_waiting() == 0
            ctl.release("downloads")
            ctl.release("ffmpeg")

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_defer_background_is_bounded_and_never_starves():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module)

        async def scenario():
            assert await ctl.defer_background(timeout=0.2) is False
            ctl._foreground_waiting = 1  # a /play is queued and never clears
            loop = asyncio.get_running_loop()
            began = loop.time()
            assert await ctl.defer_background(timeout=0.3, poll=0.02) is True
            elapsed = loop.time() - began
            assert 0.2 <= elapsed < 2.0, f"must be bounded, waited {elapsed:.2f}s"

        _run(scenario())
    finally:
        _restore_config_stub(saved)


# ---------------------------------------------------------------------------
# 5. queue auto-next + same-song dedup (priority inheritance)
# ---------------------------------------------------------------------------


def test_joining_a_background_worker_promotes_it_to_foreground():
    """Single-flight join must not make foreground inherit background priority."""
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=90.0, ram=90.0)
        ctl.recompute(force=True)
        assert ctl.capacity("downloads") == 1

        async def scenario():
            await ctl.acquire("downloads")  # someone else holds the only permit

            # A prefetch owns the shared download worker at background priority.
            async def worker():
                with module.background_scope():
                    await ctl.acquire("downloads")
                return "done"

            worker_task = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            state = ctl._lanes["downloads"]
            assert state.waiting(module.Priority.BACKGROUND) == 1
            assert ctl.foreground_waiting() == 0

            # /play now joins that same worker (same song ⇒ no second yt-dlp).
            promoted = ctl.promote_if_foreground(worker_task)
            assert promoted == 1
            assert state.waiting(module.Priority.BACKGROUND) == 0
            assert state.waiting(module.Priority.FOREGROUND) == 1
            assert ctl.foreground_waiting() == 1
            assert ctl._stats["priority_promotions"] == 1

            ctl.release("downloads")
            assert await asyncio.wait_for(worker_task, timeout=2.0) == "done"
            # Bookkeeping unwound cleanly: no leaked foreground waiter.
            assert ctl.foreground_waiting() == 0
            assert ctl._waiters_by_task == {}
            ctl.release("downloads")

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_promote_is_a_noop_for_a_background_joiner():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=90.0, ram=90.0)
        ctl.recompute(force=True)

        async def scenario():
            await ctl.acquire("downloads")

            async def worker():
                with module.background_scope():
                    await ctl.acquire("downloads")

            worker_task = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            with module.background_scope():
                assert ctl.promote_if_foreground(worker_task) == 0
            assert ctl.foreground_waiting() == 0
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            ctl.release("downloads")

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_queue_auto_next_wiring_is_background_then_promoted():
    """Static contract for the prefetch/auto-next path."""
    src = _source(PREFETCH_PATH)
    assert "from AnonX_3.core.dynamic_capacity import background_scope, dynamic_capacity" in src
    # Future queue items warm at background priority ...
    assert "async def _prefetch_runner(" in src
    assert "with background_scope():" in src
    assert "await _prefetch_runner(target)" in src
    # ... and are promoted the moment playback actually joins them.
    join = src.split("async def join_or_download")[1]
    assert "dynamic_capacity.promote_if_foreground(task)" in join
    assert "asyncio.shield(task)" in join

    # The current track's owner deliberately stays foreground: playback joins it
    # with an unbounded shield, so backgrounding it would invert priority.
    current = src.split("async def start_current_cache")[1].split(
        "async def await_current_stream_source"
    )[0]
    assert "background_scope()" not in current

    tree = ast.parse(src)
    assert isinstance(tree, ast.Module)


def test_same_song_dedup_and_promotion_wiring_in_youtube():
    src = _source(YOUTUBE_PATH)
    assert "from AnonX_3.core.dynamic_capacity import" in src and "dynamic_capacity" in src
    assert "self._inflight_downloads" in src, "same-song single flight must exist"
    block = src.split("existing = inflight_bucket.get(inflight_key)")[1][:2600]
    assert "dynamic_capacity.promote_if_foreground(existing)" in block
    assert "await asyncio.shield(existing)" in block, "join, never a second yt-dlp"


def test_thumbnail_render_yields_to_waiting_playback():
    src = _source(THUMB_PATH)
    assert "from AnonX_3.core.dynamic_capacity import background_scope, dynamic_capacity" in src
    worker = src.split("async def _render_worker")[1][:1200]
    assert "await dynamic_capacity.defer_background(" in worker
    assert "with background_scope():" in worker


def test_foreground_play_is_never_rejected_by_backpressure():
    """allow_new_heavy_job may shed background work only."""
    src = _source(RESOURCE_PATH)
    block = src.split("def allow_new_heavy_job")[1].split("def disk_usage_pct")[0]
    assert "priority = current_priority()" in block
    assert "if int(priority) <= int(Priority.FOREGROUND):" in block
    assert "return True" in block
    # The fixed limits remain readable as the fallback contract.
    assert "def fixed_ytdlp(" in src
    assert "def fixed_active_streams(" in src
    assert "dynamic_capacity.capacity(lane)" in src


# ---------------------------------------------------------------------------
# 6. /health surface
# ---------------------------------------------------------------------------


def test_snapshot_exposes_capacity_active_waiting_cpu_ram_and_lag():
    _, saved = _install_config_stub()
    try:
        module = _load_module()
        ctl = _make_controller(module, cpu=42.0, ram=51.0, lag_ms=37.0)

        async def scenario():
            await ctl.acquire("downloads")
            snap = ctl.snapshot()
            for key in (
                "mode", "degraded", "pressure", "demand", "cpu_percent",
                "ram_percent", "load_per_core", "event_loop_lag_ms",
                "foreground_waiting", "background_paused", "lanes", "counters",
            ):
                assert key in snap, key
            for lane in module.LANES:
                entry = snap["lanes"][lane]
                for key in (
                    "capacity", "active", "waiting", "waiting_foreground",
                    "waiting_background", "floor", "ceiling", "fixed",
                ):
                    assert key in entry, f"{lane}.{key}"
            assert snap["lanes"]["downloads"]["active"] == 1
            assert snap["event_loop_lag_ms"] == 37.0
            assert snap["cpu_percent"] == 42.0
            assert snap["ram_percent"] == 51.0
            ctl.release("downloads")

        _run(scenario())
    finally:
        _restore_config_stub(saved)


def test_health_endpoint_reports_the_dynamic_view():
    src = _source(HEALTH_PATH)
    block = src.split('components["dynamic_capacity"] = {')[1].split("\n        }")[0]
    for key in (
        '"capacity"', '"active_jobs"', '"waiting_jobs"', '"cpu_percent"',
        '"ram_percent"', '"event_loop_lag_ms"', '"foreground_waiting"',
        '"background_paused"', '"fixed_limits"', '"mode"', '"degraded"',
    ):
        assert key in block, key
    assert "resource_manager.event_loop_lag_ms()" in src


def test_startup_and_shutdown_are_wired():
    main_src = _source(ROOT / "AnonX_3" / "__main__.py")
    assert "resource_manager.start_dynamic_control()" in main_src
    init_src = _source(ROOT / "AnonX_3" / "__init__.py")
    assert "resource_manager.stop_dynamic_control()" in init_src
    assert '("dynamic_capacity.stop", _stop_dynamic_capacity)' in init_src


def test_config_exposes_every_dynamic_knob():
    src = _source(ROOT / "config.py")
    for name in (
        "DYNAMIC_RESOURCE_CONTROL",
        "DYNAMIC_CAPACITY_MAX_MULTIPLIER",
        "DYNAMIC_PRESSURE_GROW_BELOW",
        "DYNAMIC_PRESSURE_RELIEF",
        "DYNAMIC_LOOP_LAG_HIGH_MS",
        "DYNAMIC_LOAD_PER_CORE_HIGH",
        "DYNAMIC_RECOMPUTE_INTERVAL_SEC",
        "DYNAMIC_BACKGROUND_PAUSE",
        "DYNAMIC_FOREGROUND_RESERVE",
    ):
        assert name in src, name
    for lane in ("YTDLP", "DOWNLOADS", "VIDEO", "FFMPEG", "STREAMS"):
        assert f"DYNAMIC_{lane}_CEILING" in src, lane
        assert f"DYNAMIC_{lane}_FLOOR" in src, lane


def main() -> int:
    tests = [
        # fallback contract
        test_disabled_controller_uses_exactly_the_fixed_limits,
        test_internal_failure_latches_back_to_fixed_limits,
        test_dynamic_semaphore_falls_back_to_a_real_bounded_semaphore,
        test_dynamic_semaphore_release_matches_its_acquire_mode,
        # multi-group playback
        test_multi_group_playback_scales_up_but_stays_bounded,
        test_quiet_bot_stays_cheap,
        test_no_unlimited_workers_even_with_absurd_config,
        # CPU / RAM / lag pressure
        test_high_cpu_pressure_scales_down_to_the_floor,
        test_high_ram_pressure_scales_down,
        test_event_loop_lag_alone_scales_down,
        test_shrink_never_cuts_running_jobs,
        test_stream_registration_floor_protects_live_sessions,
        # foreground priority / background pause
        test_priority_default_is_foreground_and_background_opts_in,
        test_foreground_is_served_before_background,
        test_background_pauses_globally_while_a_foreground_request_waits,
        test_defer_background_is_bounded_and_never_starves,
        # queue auto-next + same-song dedup
        test_joining_a_background_worker_promotes_it_to_foreground,
        test_promote_is_a_noop_for_a_background_joiner,
        test_queue_auto_next_wiring_is_background_then_promoted,
        test_same_song_dedup_and_promotion_wiring_in_youtube,
        test_thumbnail_render_yields_to_waiting_playback,
        test_foreground_play_is_never_rejected_by_backpressure,
        # /health + wiring
        test_snapshot_exposes_capacity_active_waiting_cpu_ram_and_lag,
        test_health_endpoint_reports_the_dynamic_view,
        test_startup_and_shutdown_are_wired,
        test_config_exposes_every_dynamic_knob,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as ex:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(ex).__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
