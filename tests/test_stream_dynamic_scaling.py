#!/usr/bin/env python3
"""Stream dynamic scaling tests (no pytest required).

Run from the AnonX deploy root:
  python tests/test_stream_dynamic_scaling.py

Contract under test — ``MAX_ACTIVE_STREAMS`` is the BASELINE, not the cap:
  * baseline stays 20 and ``DYNAMIC_STREAMS_CEILING`` stays 0 (derive, not 50)
  * the safe ceiling is derived at runtime from CPU, RAM, event-loop lag,
    active streams and waiting (refused) stream requests
  * filling the baseline grows capacity as far as the box can carry
  * rising load throttles only NEW admissions; live VC sessions keep their slot
  * a slot is reserved before the VC starts and released if the start fails
  * direct, local and cache playback share one admission point
  * /health reports baseline, capacity, auto ceiling, active streams, reason
"""

from __future__ import annotations

import ast
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

DYN_PATH = ROOT / "AnonX_3" / "core" / "dynamic_capacity.py"
RESOURCE_PATH = ROOT / "AnonX_3" / "core" / "resource_manager.py"
CALLS_PATH = ROOT / "AnonX_3" / "core" / "calls.py"
HEALTH_PATH = ROOT / "AnonX_3" / "core" / "health.py"
CONFIG_PATH = ROOT / "config.py"
SAMPLE_ENV_PATH = ROOT / "sample.env"
EN_LOCALE_PATH = ROOT / "AnonX_3" / "locales" / "en.json"

BASELINE = 20

DEFAULT_CFG = {
    "MAX_YTDLP_CONCURRENT": 2,
    "MAX_DOWNLOAD_CONCURRENT": 2,
    "MAX_VIDEO_JOBS": 1,
    "MAX_FFMPEG_CONCURRENT": 3,
    "MAX_ACTIVE_STREAMS": BASELINE,
    "DYNAMIC_RESOURCE_CONTROL": True,
    "DYNAMIC_CAPACITY_MAX_MULTIPLIER": 4.0,
    "DYNAMIC_PRESSURE_GROW_BELOW": 0.55,
    "DYNAMIC_PRESSURE_RELIEF": 0.85,
    "DYNAMIC_LOOP_LAG_HIGH_MS": 250,
    "DYNAMIC_LOAD_PER_CORE_HIGH": 1.5,
    "DYNAMIC_RECOMPUTE_INTERVAL_SEC": 2,
    "DYNAMIC_BACKGROUND_PAUSE": True,
    "DYNAMIC_FOREGROUND_RESERVE": 1,
    "DYNAMIC_STREAM_RAM_MB": 24.0,
    "DYNAMIC_STREAMS_PER_CORE": 16,
    "DYNAMIC_STREAM_RAM_RESERVE_MB": 512.0,
    "DYNAMIC_STREAM_DEMAND_WINDOW_SEC": 20.0,
    "DYNAMIC_STREAM_HEADROOM": 2,
    "ADAPTIVE_CPU_HIGH": 70,
    "ADAPTIVE_RAM_HIGH": 88,
    # resource_manager reads these too
    "DISK_HIGH_WATER_PCT": 85,
    "DOWNLOAD_DIR": str(ROOT / "downloads"),
}

_LANE_OVERRIDES = [
    f"DYNAMIC_{lane}_{kind}"
    for lane in ("YTDLP", "DOWNLOADS", "VIDEO", "FFMPEG", "STREAMS")
    for kind in ("CEILING", "FLOOR")
]


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _FakeVM:
    def __init__(self, percent: float, total_mb: float) -> None:
        self.percent = percent
        self.total = int(total_mb * 1024 * 1024)
        self.available = int(self.total * (1.0 - percent / 100.0))


class _FakePsutil:
    """Deterministic CPU/RAM so scaling never depends on the host."""

    def __init__(self, cpu: float, ram: float, total_mb: float, cores: int) -> None:
        self.cpu = cpu
        self.ram = ram
        self.total_mb = total_mb
        self.cores = cores

    def cpu_percent(self, interval=None):  # noqa: D401 - psutil signature
        return self.cpu

    def virtual_memory(self):
        return _FakeVM(self.ram, self.total_mb)

    def cpu_count(self, logical=True):
        return self.cores

    def disk_usage(self, path):
        return types.SimpleNamespace(total=100, used=10, free=90, percent=10.0)


_SEQ = 0


def _fresh_env(**overrides):
    """Load ``dynamic_capacity`` (and optionally ``resource_manager``) standalone.

    Both modules import ``AnonX_3.config`` lazily/at import time only, so a stub
    package is enough — no Pyrogram, no Mongo, no PyTgCalls.
    """
    global _SEQ
    _SEQ += 1
    tag = f"{os.getpid()}_{_SEQ}"

    saved = {
        name: sys.modules.get(name)
        for name in (
            "AnonX_3",
            "AnonX_3.config",
            "AnonX_3.core",
            "AnonX_3.core.dynamic_capacity",
        )
    }

    pkg = types.ModuleType("AnonX_3")
    pkg.__path__ = []
    core = types.ModuleType("AnonX_3.core")
    core.__path__ = []
    cfg = types.ModuleType("AnonX_3.config")

    values = dict(DEFAULT_CFG)
    for name in _LANE_OVERRIDES:
        values.setdefault(name, 0)
    values.update(overrides)
    for key, value in values.items():
        setattr(cfg, key, value)

    pkg.config = cfg
    pkg.core = core
    sys.modules["AnonX_3"] = pkg
    sys.modules["AnonX_3.config"] = cfg
    sys.modules["AnonX_3.core"] = core

    dyn = _load(f"_dyn_{tag}", DYN_PATH)
    sys.modules["AnonX_3.core.dynamic_capacity"] = dyn
    core.dynamic_capacity = dyn
    return dyn, cfg, saved


def _restore(saved: dict) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _pin(dyn, *, cpu=0.0, ram=0.0, cores=4, total_mb=8192.0, lag_ms=0.0):
    """Pin every machine signal the planner reads, then return the singleton."""
    dyn.psutil = _FakePsutil(cpu, ram, total_mb, cores)
    dyn._cpu_count = lambda: cores
    dyn._total_ram_mb = lambda: total_mb
    # loadavg is host-dependent; keep it out of the pressure calculation.
    dyn.os = types.SimpleNamespace(
        getloadavg=None, environ=os.environ, cpu_count=lambda: cores
    )
    ctl = dyn.dynamic_capacity
    ctl._cpu_cached = cpu
    ctl._ram_cached = ram
    ctl._lag.observe_external(lag_ms)
    ctl._ensure_lanes()
    ctl.recompute(force=True)
    return ctl


def _load_resource_manager(dyn, tag: str):
    """Load ``resource_manager`` bound to the already-pinned controller."""
    mod = _load(f"_rm_{tag}", RESOURCE_PATH)
    assert mod.dynamic_capacity is dyn.dynamic_capacity
    return mod


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. baseline / ceiling contract  (20 baseline, never a fixed 50)
# ---------------------------------------------------------------------------


def test_baseline_is_twenty_and_ceiling_stays_derive():
    src = _source(CONFIG_PATH)
    assert '_int_env("MAX_ACTIVE_STREAMS", 20, 1)' in src
    assert '_int_env("DYNAMIC_STREAMS_CEILING", 0, 0)' in src
    env = _source(SAMPLE_ENV_PATH)
    assert "# DYNAMIC_STREAMS_CEILING=0" in env


def test_no_fixed_stream_cap_is_hardcoded():
    """The ceiling must be derived from the machine, never a magic total."""
    dyn_src = _source(DYN_PATH)
    tree = ast.parse(dyn_src)
    functions = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in ("_stream_ceiling", "_stream_target")
    }
    assert set(functions) == {"_stream_ceiling", "_stream_target"}

    for name, fn in functions.items():
        literals = {
            n.value
            for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
        }
        # A fixed 50 (or any other absolute stream total) is exactly the thing
        # this feature must not reintroduce. Per-core / per-stream unit
        # defaults are fine — they scale with the box.
        assert 50 not in literals, name
        assert not any(
            isinstance(v, int) and v > 20 and v != BASELINE for v in literals
        ), f"{name} carries an absolute capacity constant: {literals}"

    ceiling_src = ast.unparse(functions["_stream_ceiling"])
    for token in ("DYNAMIC_STREAMS_CEILING", "cores", "ram_mb", "mult", "base"):
        assert token in ceiling_src, token


def test_derived_ceiling_exceeds_the_baseline_on_a_small_vps():
    """Regression: the download lane's per-job RAM unit pinned a 2 GB VPS at 20.

    ``ram_mb // 150`` gives 13 on 2 GB — below the baseline — so streams could
    never grow at all. A relayed VC stream is far cheaper than a yt-dlp job.
    """
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cores=2, total_mb=2048.0)
        fixed = ctl._fixed_limits()
        ceilings = ctl._ceilings(fixed)
        assert ceilings["streams"] > BASELINE, ceilings["streams"]
        # ...and a bigger box gets more room than a small one.
        ctl_big = _pin(dyn, cores=4, total_mb=8192.0)
        big = ctl_big._ceilings(ctl_big._fixed_limits())["streams"]
        assert big > ceilings["streams"]
    finally:
        _restore(saved)


def test_ceiling_is_bounded_even_with_an_absurd_machine():
    """No unlimited mode: still bounded by baseline * max multiplier."""
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cores=256, total_mb=1024.0 * 1024.0)
        fixed = ctl._fixed_limits()
        ceiling = ctl._ceilings(fixed)["streams"]
        assert ceiling <= BASELINE * 8
        assert ceiling < 10_000
    finally:
        _restore(saved)


def test_operator_override_is_honoured_but_still_bounded():
    dyn, _, saved = _fresh_env(DYNAMIC_STREAMS_CEILING=45)
    try:
        ctl = _pin(dyn)
        assert ctl._ceilings(ctl._fixed_limits())["streams"] == 45
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# 2. growth: filling the baseline raises capacity
# ---------------------------------------------------------------------------


def test_capacity_starts_at_the_baseline_on_a_quiet_box():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn)
        assert ctl.capacity("streams") == BASELINE
        view = ctl.stream_view()
        assert view["baseline"] == BASELINE
        assert view["active"] == 0
        assert view["capacity"] == BASELINE
    finally:
        _restore(saved)


def test_filling_the_baseline_auto_grows_on_a_healthy_box():
    """20 groups streaming ⇒ the 21st is admitted, not refused."""
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=15.0, ram=30.0)
        for i in range(BASELINE):
            assert ctl.try_admit("streams") is True, i
        assert ctl._lanes["streams"].active == BASELINE
        # The refusal→recompute→retry happens inside this one call.
        assert ctl.try_admit("streams") is True, "21st stream must be admitted"
        assert ctl.capacity("streams") > BASELINE
        assert ctl._lanes["streams"].active == BASELINE + 1
    finally:
        _restore(saved)


def test_growth_stops_at_the_runtime_safe_ceiling():
    """Growth is bounded by the *safe* ceiling, which trails the hard one.

    The hard ceiling is the machine bound; the safe ceiling is what the box
    can carry at the current pressure. Only a completely idle box may reach
    the hard bound, so an admission run must stop at the safe ceiling.
    """
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0, cores=2, total_mb=2048.0)
        hard = ctl._ceilings(ctl._fixed_limits())["streams"]
        safe = ctl.recompute(force=True).stream_safe_ceiling
        assert BASELINE < safe <= hard, (BASELINE, safe, hard)

        admitted = 0
        for _ in range(hard + 25):
            if ctl.try_admit("streams"):
                admitted += 1
            else:
                break
        assert admitted == safe, (admitted, safe, hard)
        assert ctl.try_admit("streams") is False
        assert ctl.capacity("streams") <= hard
    finally:
        _restore(saved)


def test_refused_requests_count_as_waiting_demand():
    """The streams lane never queues, so a refused /play IS the waiting request."""
    dyn, _, saved = _fresh_env(DYNAMIC_STREAMS_CEILING=BASELINE)
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        for _ in range(BASELINE):
            assert ctl.try_admit("streams") is True
        assert ctl.stream_waiting() == 0
        assert ctl.try_admit("streams") is False  # pinned at the override
        assert ctl.stream_waiting() >= 1
        assert ctl.snapshot()["counters"]["stream_refusals"] >= 1
        assert ctl.stream_view()["waiting"] >= 1
    finally:
        _restore(saved)


def test_admitting_retires_the_refusal_record():
    """Probe-then-admit must count one request, not two."""
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        for _ in range(BASELINE):
            ctl.try_admit("streams")
        assert ctl.probe_stream_admission() is True
        waiting_after_probe = ctl.stream_waiting()
        assert waiting_after_probe >= 1
        assert ctl.try_admit("streams") is True
        assert ctl.stream_waiting() < waiting_after_probe
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# 3. pressure: throttle admissions, never cut live VC
# ---------------------------------------------------------------------------


def test_safe_ceiling_shrinks_as_pressure_rises():
    dyn, _, saved = _fresh_env()
    try:
        seen = []
        for cpu in (5.0, 40.0, 60.0, 80.0):
            ctl = _pin(dyn, cpu=cpu, ram=cpu)
            plan = ctl.recompute(force=True)
            seen.append(plan.stream_safe_ceiling)
        assert seen == sorted(seen, reverse=True), seen
        assert seen[0] > seen[-1]
        assert seen[-1] >= BASELINE, "the baseline is a floor, not a target"
    finally:
        _restore(saved)


def test_high_pressure_only_throttles_new_admissions():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=99.0, ram=99.0, lag_ms=2000.0)
        plan = ctl.recompute(force=True)
        # Pressure at/above relief ⇒ pinned to the baseline floor…
        assert plan.limits["streams"] == BASELINE
        assert "relief" in plan.stream_reason
        # …yet the baseline itself is still fully admittable, so a saturated
        # box keeps serving groups instead of refusing everything.
        assert ctl.try_admit("streams") is True
    finally:
        _restore(saved)


def test_pressure_spike_never_revokes_a_live_session():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        for _ in range(BASELINE + 6):
            assert ctl.try_admit("streams") is True
        live = ctl._lanes["streams"].active
        assert live == BASELINE + 6

        # Load spikes hard.
        dyn.psutil = _FakePsutil(99.0, 99.0, 8192.0, 4)
        ctl._cpu_cached = 99.0
        ctl._ram_cached = 99.0
        ctl._lag.observe_external(2000.0)
        ctl.recompute(force=True)

        assert ctl._lanes["streams"].active == live, "live VC lost its slot"
        assert ctl.capacity("streams") >= live, "capacity dropped below live count"
        # New admissions are what gets throttled.
        assert ctl.try_admit("streams") is False
    finally:
        _restore(saved)


def test_scaling_reason_names_every_input():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=20.0, ram=25.0)
        reason = ctl.recompute(force=True).stream_reason
        for token in (
            "active=",
            "waiting=",
            "capacity=",
            "baseline=",
            "safe_ceiling=",
            "hard_ceiling=",
            "pressure=",
        ):
            assert token in reason, (token, reason)
    finally:
        _restore(saved)


def test_disabled_controller_pins_streams_to_the_fixed_baseline():
    dyn, _, saved = _fresh_env(DYNAMIC_RESOURCE_CONTROL=False)
    try:
        ctl = _pin(dyn)
        assert ctl.capacity("streams") == BASELINE
        view = ctl.stream_view()
        assert view["capacity"] == BASELINE
        assert "MAX_ACTIVE_STREAMS=20" in view["scaling_reason"]
        # Admission must never hard-fail in fallback mode.
        assert ctl.try_admit("streams") is True
        assert ctl.probe_stream_admission() is True
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# 4. reserve before VC start, release on failure
# ---------------------------------------------------------------------------


def test_reserve_then_release_returns_the_slot():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        rm = _load_resource_manager(dyn, "res1").resource_manager

        slot = rm.reserve_stream(101)
        assert slot.admitted is True
        assert slot.created is True
        assert rm.active_stream_count() == 1
        assert ctl._lanes["streams"].active == 1

        slot.release()  # VC start failed
        assert rm.active_stream_count() == 0
        assert ctl._lanes["streams"].active == 0
        # Double release is harmless.
        slot.release()
        assert ctl._lanes["streams"].active == 0
    finally:
        _restore(saved)


def test_reservation_is_idempotent_per_chat():
    """/skip, queue auto-next and adaptive switch re-enter the same chat."""
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        rm = _load_resource_manager(dyn, "res2").resource_manager

        first = rm.reserve_stream(202)
        again = rm.reserve_stream(202)
        assert first.created is True and again.created is False
        assert ctl._lanes["streams"].active == 1

        again.release()  # must NOT free a live session's slot
        assert rm.active_stream_count() == 1
        assert ctl._lanes["streams"].active == 1

        rm.unregister_stream(202)
        assert rm.active_stream_count() == 0
        assert ctl._lanes["streams"].active == 0
    finally:
        _restore(saved)


def test_refused_reservation_takes_no_slot():
    dyn, _, saved = _fresh_env(DYNAMIC_STREAMS_CEILING=BASELINE)
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        rm = _load_resource_manager(dyn, "res3").resource_manager
        for chat in range(BASELINE):
            assert rm.reserve_stream(chat).admitted is True
        refused = rm.reserve_stream(9999)
        assert refused.admitted is False
        assert refused.created is False
        assert rm.active_stream_count() == BASELINE
        assert ctl._lanes["streams"].active == BASELINE
        refused.release()  # no-op, must not free someone else's slot
        assert ctl._lanes["streams"].active == BASELINE
    finally:
        _restore(saved)


def test_preflight_passes_for_a_chat_that_already_streams():
    dyn, _, saved = _fresh_env(DYNAMIC_STREAMS_CEILING=BASELINE)
    try:
        _pin(dyn, cpu=10.0, ram=20.0)
        rm = _load_resource_manager(dyn, "res4").resource_manager
        for chat in range(BASELINE):
            rm.reserve_stream(chat)
        assert rm.can_admit_stream(0) is True, "live chat must never be blocked"
        assert rm.can_admit_stream(9999) is False
    finally:
        _restore(saved)


def test_unregister_release_is_symmetric_over_many_cycles():
    """A leak here would silently shrink usable capacity over uptime."""
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn, cpu=10.0, ram=20.0)
        rm = _load_resource_manager(dyn, "res5").resource_manager
        for cycle in range(30):
            chat = 1000 + (cycle % 5)
            rm.reserve_stream(chat)
            rm.unregister_stream(chat)
        assert rm.active_stream_count() == 0
        assert ctl._lanes["streams"].active == 0
        # Unregistering an unknown chat must not go negative.
        rm.unregister_stream(424242)
        assert ctl._lanes["streams"].active == 0
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# 5. one admission point for direct / local / cache
# ---------------------------------------------------------------------------


def _calls_tree():
    return ast.parse(_source(CALLS_PATH))


def test_every_vc_start_goes_through_the_single_admission_point():
    """`client.play(...)` must exist in exactly one place: the slot wrapper."""
    tree = _calls_tree()
    owners: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "play"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "client"
            ):
                owners.append(fn.name)
    assert owners == ["_play_with_startup_slot"], owners


def test_the_admission_point_reserves_before_play_and_releases_on_failure():
    tree = _calls_tree()
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_play_with_startup_slot"
    )
    body = ast.unparse(fn)
    assert "resource_manager.reserve_stream(chat_id)" in body
    assert "raise StreamCapacityError(chat_id)" in body
    assert "slot.release()" in body
    # Reservation precedes submission, and release is on the failure path.
    assert body.index("reserve_stream") < body.index("client.play")
    handlers = [
        h
        for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler)
        and isinstance(h.type, ast.Name)
        and h.type.id == "BaseException"
    ]
    assert handlers, "cancellation must release the slot too"
    assert any(
        isinstance(stmt, ast.Raise) for stmt in ast.walk(handlers[0])
    ), "the failure must still propagate after releasing"


def test_no_playback_path_registers_a_stream_on_its_own():
    """Local/cache used to log 'MAX_ACTIVE_STREAMS reached' and play anyway."""
    src = _source(CALLS_PATH)
    assert "register_stream(chat_id)" not in src.replace(
        "unregister_stream(chat_id)", ""
    )
    assert "MAX_ACTIVE_STREAMS reached" not in src
    # Cleanup must keep releasing on stop.
    assert "resource_manager.unregister_stream(chat_id)" in src


def test_capacity_refusal_is_handled_without_burning_the_queue():
    src = _source(CALLS_PATH)
    assert "class StreamCapacityError(RuntimeError):" in src
    tree = _calls_tree()
    handlers = [
        h
        for h in ast.walk(tree)
        if isinstance(h, ast.ExceptHandler)
        and isinstance(h.type, ast.Name)
        and h.type.id == "StreamCapacityError"
    ]
    assert len(handlers) >= 6, len(handlers)
    notified = 0
    for handler in handlers:
        body = ast.unparse(handler)
        assert "play_next" not in body, "a refusal must not advance the queue"
        if "_notify_stream_busy" in body:
            notified += 1
        else:
            # The only alternative is re-raising so an outer handler answers.
            assert any(
                isinstance(node, ast.Raise) for node in ast.walk(handler)
            ), body
    assert notified >= 5, notified
    # The pre-flight keeps a saturated box from paying for a download first.
    assert "resource_manager.can_admit_stream(chat_id)" in src
    assert '"play_stream_busy"' in src
    assert '"play_stream_busy"' in _source(EN_LOCALE_PATH)


def test_resource_manager_keeps_its_fallback_contract():
    src = _source(RESOURCE_PATH)
    assert "def fixed_active_streams(" in src
    assert "def reserve_stream(" in src
    assert "def can_admit_stream(" in src
    assert "def stream_scaling(" in src
    assert 'dynamic_capacity.release("streams")' in src
    assert 'dynamic_capacity.try_admit("streams")' in src


# ---------------------------------------------------------------------------
# 6. /health visibility
# ---------------------------------------------------------------------------


def test_health_reports_baseline_capacity_ceiling_active_and_reason():
    src = _source(HEALTH_PATH)
    assert '"streams": resource_manager.stream_scaling(),' in src

    dyn, _, saved = _fresh_env()
    try:
        _pin(dyn, cpu=12.0, ram=22.0)
        rm = _load_resource_manager(dyn, "res6").resource_manager
        rm.reserve_stream(777)
        view = rm.stream_scaling()
        for key in (
            "baseline",
            "capacity",
            "auto_ceiling",
            "hard_ceiling",
            "active",
            "waiting",
            "scaling_reason",
        ):
            assert key in view, key
        assert view["baseline"] == BASELINE
        assert view["active"] == 1
        assert view["auto_ceiling"] >= view["baseline"]
        assert view["hard_ceiling"] >= view["auto_ceiling"]
        assert view["scaling_reason"]
    finally:
        _restore(saved)


def test_snapshot_exposes_the_stream_view():
    dyn, _, saved = _fresh_env()
    try:
        ctl = _pin(dyn)
        snap = ctl.snapshot()
        assert "streams" in snap
        assert snap["streams"]["baseline"] == BASELINE
        # Existing keys must survive.
        for key in ("mode", "degraded", "pressure", "demand", "lanes", "counters"):
            assert key in snap, key
        assert "streams" in snap["lanes"]
    finally:
        _restore(saved)


def test_config_and_sample_env_expose_the_stream_knobs():
    cfg_src = _source(CONFIG_PATH)
    env_src = _source(SAMPLE_ENV_PATH)
    for name in (
        "DYNAMIC_STREAM_RAM_MB",
        "DYNAMIC_STREAMS_PER_CORE",
        "DYNAMIC_STREAM_RAM_RESERVE_MB",
        "DYNAMIC_STREAM_DEMAND_WINDOW_SEC",
        "DYNAMIC_STREAM_HEADROOM",
    ):
        assert name in cfg_src, name
        assert name in env_src, name


def main() -> int:
    tests = [
        # baseline / ceiling contract
        test_baseline_is_twenty_and_ceiling_stays_derive,
        test_no_fixed_stream_cap_is_hardcoded,
        test_derived_ceiling_exceeds_the_baseline_on_a_small_vps,
        test_ceiling_is_bounded_even_with_an_absurd_machine,
        test_operator_override_is_honoured_but_still_bounded,
        # growth
        test_capacity_starts_at_the_baseline_on_a_quiet_box,
        test_filling_the_baseline_auto_grows_on_a_healthy_box,
        test_growth_stops_at_the_runtime_safe_ceiling,
        test_refused_requests_count_as_waiting_demand,
        test_admitting_retires_the_refusal_record,
        # pressure
        test_safe_ceiling_shrinks_as_pressure_rises,
        test_high_pressure_only_throttles_new_admissions,
        test_pressure_spike_never_revokes_a_live_session,
        test_scaling_reason_names_every_input,
        test_disabled_controller_pins_streams_to_the_fixed_baseline,
        # reserve / release
        test_reserve_then_release_returns_the_slot,
        test_reservation_is_idempotent_per_chat,
        test_refused_reservation_takes_no_slot,
        test_preflight_passes_for_a_chat_that_already_streams,
        test_unregister_release_is_symmetric_over_many_cycles,
        # single admission point
        test_every_vc_start_goes_through_the_single_admission_point,
        test_the_admission_point_reserves_before_play_and_releases_on_failure,
        test_no_playback_path_registers_a_stream_on_its_own,
        test_capacity_refusal_is_handled_without_burning_the_queue,
        test_resource_manager_keeps_its_fallback_contract,
        # /health
        test_health_reports_baseline_capacity_ceiling_active_and_reason,
        test_snapshot_exposes_the_stream_view,
        test_config_and_sample_env_expose_the_stream_knobs,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except Exception as ex:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(ex).__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
