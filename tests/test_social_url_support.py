# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

"""Social share-link support: TikTok stories, Facebook watch/reel/share.

The bug these tests pin down is not routing — ``TikTok.valid()`` and
``Facebook.valid()`` always matched the samples. It is that yt-dlp 2026.07.04
has no extractor for the URL *shapes* people paste, so ``/play`` died before
any download started. The fix is a canonicalisation layer, and the contract is:

* every unsupported shape is rewritten into a supported one,
* every already-supported shape survives untouched,
* tracking noise never splits one video into several cache keys,
* the network hop is bounded, cached, single-flighted, and optional,
* anything unrecognised comes back byte-identical.

Standalone by project convention: ``python tests/test_social_url_support.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import types
from pathlib import Path

os.environ.setdefault("AnonX_TESTING", "1")

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

MODULE_PATH = ROOT / "AnonX_3" / "core" / "social_urls.py"

DEFAULT_CFG = {
    "SOCIAL_URL_RESOLVE_ENABLED": True,
    "SOCIAL_URL_RESOLVE_TIMEOUT_SEC": 8.0,
    "SOCIAL_URL_RESOLVE_MAX_HOPS": 5,
    "SOCIAL_URL_CACHE_TTL_SEC": 3600,
    "SOCIAL_URL_NEGATIVE_TTL_SEC": 60,
}

# The two links the user actually reported.
SAMPLE_SHARE = "https://www.facebook.com/share/v/19J3VuPhYZ/"
SAMPLE_REEL = (
    "https://www.facebook.com/reel/995756753386001/"
    "?mibextid=9drbnH&s=yWDuG2&fs=e"
)

_MODULE_SEQ = 0


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _FakeSingleFlight:
    """Records keys so dedup can be asserted without a real event-loop task."""

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self.keys: list[str] = []

    async def do(self, key, factory, *, timeout=None):
        self.keys.append(key)
        return await factory()

    async def shutdown(self) -> None:
        return None


def _install_stubs(**overrides):
    """Inject a fake AnonX_3 package so the module imports without the app."""
    saved = {
        name: sys.modules.get(name)
        for name in (
            "AnonX_3",
            "AnonX_3.config",
            "AnonX_3.core",
            "AnonX_3.core.downloader",
            "AnonX_3.core.downloader.singleflight",
        )
    }

    cfg = types.SimpleNamespace(**{**DEFAULT_CFG, **overrides})

    class _Logger:
        def _noop(self, *a, **k):
            return None

        debug = info = warning = error = exception = _noop

    pkg = types.ModuleType("AnonX_3")
    pkg.__path__ = [str(ROOT / "AnonX_3")]
    pkg.config = cfg
    pkg.logger = _Logger()

    core = types.ModuleType("AnonX_3.core")
    core.__path__ = [str(ROOT / "AnonX_3" / "core")]

    downloader = types.ModuleType("AnonX_3.core.downloader")
    downloader.__path__ = [str(ROOT / "AnonX_3" / "core" / "downloader")]

    singleflight = types.ModuleType("AnonX_3.core.downloader.singleflight")
    singleflight.SingleFlight = _FakeSingleFlight

    sys.modules["AnonX_3"] = pkg
    sys.modules["AnonX_3.config"] = cfg
    sys.modules["AnonX_3.core"] = core
    sys.modules["AnonX_3.core.downloader"] = downloader
    sys.modules["AnonX_3.core.downloader.singleflight"] = singleflight
    return cfg, saved


def _restore_stubs(saved) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_module():
    global _MODULE_SEQ
    _MODULE_SEQ += 1
    spec = importlib.util.spec_from_file_location(
        f"_social_urls_under_test_{_MODULE_SEQ}", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(coro, *, timeout: float = 10.0):
    """Run a scenario under a hard timeout.

    A regression in the bounded-network path shows up as a hang, so an
    unguarded run would stall the suite instead of failing it.
    """

    async def _guard():
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(_guard())


class _FakeResponse:
    def __init__(self, url, *, content_type="text/html", body=b""):
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _FakeSession:
    def __init__(self, plan, calls, *, timeout=None, headers=None):
        self._plan = plan
        self._calls = calls
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, *, allow_redirects=True, max_redirects=5):
        self._calls.append(
            {"url": url, "redirects": allow_redirects, "hops": max_redirects}
        )
        outcome = self._plan(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_fake_aiohttp(module, plan):
    """Give the module under test a scripted aiohttp. Returns the call log."""
    calls: list[dict] = []
    fake = types.ModuleType("aiohttp")

    class _Timeout:
        def __init__(self, total=None):
            self.total = total

    fake.ClientTimeout = _Timeout
    fake.ClientSession = lambda timeout=None, headers=None: _FakeSession(
        plan, calls, timeout=timeout, headers=headers
    )
    sys.modules["aiohttp"] = fake
    return calls


# ---------------------------------------------------------------------------
# 1. offline rewrite — TikTok
# ---------------------------------------------------------------------------


def test_tiktok_story_becomes_a_video_url():
    """The headline gap: /story/<id> matches no extractor, /video/<id> does."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert (
            m.canonical_tiktok_url("https://www.tiktok.com/@user/story/7300000000000000001")
            == "https://www.tiktok.com/@user/video/7300000000000000001"
        )
        # A story link straight off the mobile app: wrong host *and* wrong path.
        assert (
            m.canonical_tiktok_url(
                "https://m.tiktok.com/@some.user/story/7300000000000000002?_t=8x&_r=1"
            )
            == "https://www.tiktok.com/@some.user/video/7300000000000000002"
        )
    finally:
        _restore_stubs(saved)


def test_tiktok_photo_and_legacy_shapes_are_rewritten():
    """Photo posts and the legacy /v/<id>.html share both reach an item URL."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert (
            m.canonical_tiktok_url("https://www.tiktok.com/@u/photo/7300000000000000003")
            == "https://www.tiktok.com/@u/video/7300000000000000003"
        )
        # No handle in the URL, so the handle-free /embed/<id> form is used.
        assert (
            m.canonical_tiktok_url("https://m.tiktok.com/v/7300000000000000004.html")
            == "https://www.tiktok.com/embed/7300000000000000004"
        )
    finally:
        _restore_stubs(saved)


def test_tiktok_host_is_forced_to_www():
    """TikTok's item pattern hardcodes www., so a bare host never matched."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        for host in ("tiktok.com", "m.tiktok.com", "www.tiktok.com"):
            assert (
                m.canonical_tiktok_url(f"https://{host}/@u/video/7300000000000000005")
                == "https://www.tiktok.com/@u/video/7300000000000000005"
            )
        # /t/ short links match a pattern that also hardcodes www.
        assert (
            m.canonical_tiktok_url("http://tiktok.com/t/ZTabcdef1/")
            == "https://www.tiktok.com/t/ZTabcdef1/"
        )
    finally:
        _restore_stubs(saved)


def test_tiktok_handle_outside_the_extractor_charset_falls_back_to_embed():
    """A handle yt-dlp's own class rejects would not match after a rewrite."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert (
            m.canonical_tiktok_url("https://www.tiktok.com/@sør key/video/7300000000000000006")
            == "https://www.tiktok.com/embed/7300000000000000006"
        )
    finally:
        _restore_stubs(saved)


def test_supported_tiktok_urls_survive_unchanged():
    """A working link must not be disturbed by the new layer."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        good = "https://www.tiktok.com/@user/video/7300000000000000007"
        assert m.canonical_tiktok_url(good) == good
        assert m.canonical_tiktok_url("https://vm.tiktok.com/ZM8abcdef/") == (
            "https://vm.tiktok.com/ZM8abcdef/"
        )
    finally:
        _restore_stubs(saved)


# ---------------------------------------------------------------------------
# 2. offline rewrite — Facebook
# ---------------------------------------------------------------------------


def test_facebook_reel_sample_loses_only_its_tracking_noise():
    """The user's second sample: already extractable, just noisy."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert (
            m.canonical_facebook_url(SAMPLE_REEL)
            == "https://www.facebook.com/reel/995756753386001"
        )
        # Every mobile/basic host collapses onto the same canonical string, so
        # one reel is one media_id no matter which app produced the link.
        variants = [
            "https://m.facebook.com/reel/995756753386001/?mibextid=9drbnH",
            "https://web.facebook.com/reel/995756753386001",
            "http://mbasic.facebook.com/reel/995756753386001/?s=abc&fs=e",
        ]
        assert {m.canonical_facebook_url(v) for v in variants} == {
            "https://www.facebook.com/reel/995756753386001"
        }
    finally:
        _restore_stubs(saved)


def test_facebook_watch_urls_keep_the_video_id():
    """The id-bearing params must survive while attribution is dropped."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        got = m.canonical_facebook_url(
            "https://web.facebook.com/watch/?v=1234567890&mibextid=zz&ref=sharing"
        )
        assert got == "https://www.facebook.com/watch/?v=1234567890"
        assert m.canonical_facebook_url(
            "https://www.facebook.com/somepage/videos/998877665544/?fs=e&s=x"
        ) == "https://www.facebook.com/somepage/videos/998877665544/"
        assert "story_fbid=" in m.canonical_facebook_url(
            "https://m.facebook.com/story.php?story_fbid=112233&id=445566&sfnsn=mo"
        )
    finally:
        _restore_stubs(saved)


def test_facebook_link_shim_is_unwrapped_offline():
    """l.facebook.com/l.php?u= wraps a real link; unwrap without a request."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert (
            m.canonical_facebook_url(
                "https://l.facebook.com/l.php"
                "?u=https%3A%2F%2Fwww.facebook.com%2Freel%2F995756753386001%2F&h=AT1"
            )
            == "https://www.facebook.com/reel/995756753386001"
        )
        # A shim pointing off-site is left alone: re-targeting this provider at
        # an arbitrary third-party host is not its job.
        offsite = "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fa"
        assert m.canonical_facebook_url(offsite).startswith(
            "https://www.facebook.com/l.php"
        )
    finally:
        _restore_stubs(saved)


def test_share_and_shortener_links_are_flagged_for_lookup():
    """Only the server knows these targets, so the text pass must say so."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert m.facebook_needs_lookup(SAMPLE_SHARE) is True
        assert m.facebook_needs_lookup("https://www.facebook.com/share/r/AbC-1/") is True
        assert m.facebook_needs_lookup("https://fb.watch/xyz123AbC/") is True
        assert m.tiktok_needs_lookup("https://vt.tiktok.com/ZSabcdef/") is True
        assert m.tiktok_needs_lookup("https://www.tiktok.com/t/ZTabcdef/") is True

        # Resolvable shapes must NOT trigger a request.
        assert m.facebook_needs_lookup(SAMPLE_REEL) is False
        assert m.facebook_needs_lookup("https://www.facebook.com/watch/?v=1") is False
        assert m.tiktok_needs_lookup("https://www.tiktok.com/@u/story/73000000001") is False
        assert m.facebook_needs_lookup("https://example.com/share/v/abc/") is False
    finally:
        _restore_stubs(saved)


def test_non_provider_and_malformed_input_is_returned_byte_identical():
    """Total degradation: if we cannot improve it, we do not touch it."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        for raw in (
            "https://youtu.be/dQw4w9WgXcQ",
            "not a url at all",
            "",
            "ftp://tiktok.com/@u/video/1",
            "javascript:alert(1)",
            "file:///etc/passwd",
        ):
            assert m.canonical_tiktok_url(raw) == raw
            assert m.canonical_facebook_url(raw) == raw
    finally:
        _restore_stubs(saved)


# ---------------------------------------------------------------------------
# 3. bounded network lookup
# ---------------------------------------------------------------------------


def test_facebook_share_sample_resolves_through_a_redirect():
    """The user's first sample, end to end, over a scripted redirect."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        calls = _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/995756753386001/?mibextid=9drbnH",
                content_type="text/html; charset=utf-8",
                body=b"<html></html>",
            ),
        )

        got = _run(m.canonical_facebook(SAMPLE_SHARE))
        assert got == "https://www.facebook.com/reel/995756753386001", got
        assert len(calls) == 1, "exactly one bounded request"
        assert calls[0]["redirects"] is True
        assert calls[0]["hops"] == 5
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_fb_watch_shortener_resolves_to_a_watch_url():
    _, saved = _install_stubs()
    try:
        m = _load_module()
        _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/watch/?v=778899001122&ref=sharing",
                body=b"<html></html>",
            ),
        )
        assert _run(m.canonical_facebook("https://fb.watch/AbC123xyZ/")) == (
            "https://www.facebook.com/watch/?v=778899001122"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_a_shortened_tiktok_story_is_resolved_then_rewritten():
    """Both passes must compose: unshorten, *then* /story/ -> /video/.

    yt-dlp resolves vm/vt links itself, but it lands on the /story/ URL it
    cannot extract. Doing the hop here is what makes shortened stories work.
    """
    _, saved = _install_stubs()
    try:
        m = _load_module()
        _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.tiktok.com/@some.user/story/7300000000000000008?_t=8ab&_r=1",
                body=b"<html></html>",
            ),
        )
        assert _run(m.canonical_tiktok("https://vt.tiktok.com/ZSabcdef/")) == (
            "https://www.tiktok.com/@some.user/video/7300000000000000008"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_interstitial_page_is_scraped_for_its_canonical_url():
    """Facebook sometimes serves HTML instead of a Location header."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        page = (
            b"<html><head>"
            b'<meta property="og:title" content="something" />'
            b'<meta content="https://www.facebook.com/reel/424242424242/" '
            b'property="og:url" />'
            b"</head></html>"
        )
        _install_fake_aiohttp(
            m, lambda url: _FakeResponse(SAMPLE_SHARE, body=page)
        )
        assert _run(m.canonical_facebook(SAMPLE_SHARE)) == (
            "https://www.facebook.com/reel/424242424242"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_embedded_video_id_is_the_last_resort():
    """No Location, no canonical tag — the id is still in the payload."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        page = b'{"video_id":"556677889900","other":1}'
        _install_fake_aiohttp(
            m, lambda url: _FakeResponse(SAMPLE_SHARE, body=page)
        )
        assert _run(m.canonical_facebook(SAMPLE_SHARE)) == (
            "https://www.facebook.com/watch/?v=556677889900"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_lookup_failure_falls_back_to_the_rewritten_url():
    """A dead network must never turn into a raised exception."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        _install_fake_aiohttp(m, lambda url: OSError("connection reset"))
        assert _run(m.canonical_facebook(SAMPLE_SHARE)) == SAMPLE_SHARE
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_missing_aiohttp_degrades_to_the_offline_pass():
    """The dependency is imported lazily and its absence is not fatal."""
    _, saved = _install_stubs()
    saved_aiohttp = sys.modules.get("aiohttp")
    try:
        m = _load_module()

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "aiohttp":
                    raise ImportError("blocked for test")
                return None

        sys.modules.pop("aiohttp", None)
        sys.meta_path.insert(0, _Blocker())
        try:
            assert _run(m.canonical_facebook(SAMPLE_SHARE)) == SAMPLE_SHARE
            # The offline pass still runs, so a reel link is still cleaned.
            assert _run(m.canonical_facebook(SAMPLE_REEL)) == (
                "https://www.facebook.com/reel/995756753386001"
            )
        finally:
            sys.meta_path.pop(0)
    finally:
        if saved_aiohttp is not None:
            sys.modules["aiohttp"] = saved_aiohttp
        _restore_stubs(saved)


def test_non_html_response_is_not_scraped():
    """A binary body must not be decoded and pattern-matched."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/313131313131/",
                content_type="video/mp4",
                body=b"\x00\x01\x02never-read",
            ),
        )
        assert _run(m.canonical_facebook(SAMPLE_SHARE)) == (
            "https://www.facebook.com/reel/313131313131"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_html_read_is_capped():
    """A hostile or huge page cannot be pulled into memory unbounded."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert m._HTML_SCAN_BYTES <= 512 * 1024

        seen: list[int] = []

        class _Recording(_FakeContent):
            async def read(self, limit: int) -> bytes:
                seen.append(limit)
                return await super().read(limit)

        response = _FakeResponse(SAMPLE_SHARE, body=b"x" * (4 * 1024 * 1024))
        response.content = _Recording(b"x" * (4 * 1024 * 1024))
        _install_fake_aiohttp(m, lambda url: response)

        _run(m.canonical_facebook(SAMPLE_SHARE))
        assert seen == [m._HTML_SCAN_BYTES]
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


# ---------------------------------------------------------------------------
# 4. cost control
# ---------------------------------------------------------------------------


def test_a_resolved_share_link_is_cached():
    """The same link in ten groups must cost one request, not ten."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        calls = _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/121212121212/", body=b"<html></html>"
            ),
        )

        async def scenario():
            first = await m.canonical_facebook(SAMPLE_SHARE)
            for _ in range(9):
                assert await m.canonical_facebook(SAMPLE_SHARE) == first
            return first

        assert _run(scenario()) == "https://www.facebook.com/reel/121212121212"
        assert len(calls) == 1, f"expected 1 request, made {len(calls)}"

        m.cache_clear()
        _run(m.canonical_facebook(SAMPLE_SHARE))
        assert len(calls) == 2, "cache_clear must actually clear"
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_concurrent_requests_for_one_link_are_single_flighted():
    """Ten groups hitting a cold link at once still resolve it once."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        assert isinstance(m._social_url_flight, _FakeSingleFlight)
        _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/343434343434/", body=b"<html></html>"
            ),
        )

        async def scenario():
            results = await asyncio.gather(
                *(m.canonical_facebook(SAMPLE_SHARE) for _ in range(10))
            )
            assert len(set(results)) == 1, results
            return results[0]

        assert _run(scenario()) == "https://www.facebook.com/reel/343434343434"
        keys = set(m._social_url_flight.keys)
        assert len(keys) == 1, keys
        assert keys == {f"facebook:{SAMPLE_SHARE}"}
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_an_unreachable_provider_is_not_retried_on_every_play():
    """A dead provider must cost one timeout, not one per /play.

    Facebook is reachable from the VPS but not from every dev box, and the
    foreground path is exactly where an 8s stall hurts, so a failed hop is
    remembered briefly.
    """
    _, saved = _install_stubs(SOCIAL_URL_NEGATIVE_TTL_SEC=300)
    try:
        m = _load_module()
        calls = _install_fake_aiohttp(m, lambda url: TimeoutError("unreachable"))

        async def scenario():
            for _ in range(5):
                assert await m.canonical_facebook(SAMPLE_SHARE) == SAMPLE_SHARE

        _run(scenario())
        assert len(calls) == 1, f"expected 1 attempt, made {len(calls)}"

        # The negative entry must be short-lived, not the full hour, so a
        # provider that comes back is picked up again.
        expires, value = m._cache[SAMPLE_SHARE]
        assert value == SAMPLE_SHARE
        import time as _time

        assert expires - _time.monotonic() <= 300 + 1
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_provider_cookies_are_sent_only_to_their_own_host():
    """A login interstitial needs cookies; the wrong host must never get them.

    The cookie directory is shared, so a YouTube jar can be the file the
    Facebook provider hands over. Domain matching is what keeps those
    credentials off Facebook's wire.
    """
    _, saved = _install_stubs()
    jar = ROOT / "tests" / "_tmp_social_cookies.txt"
    try:
        m = _load_module()
        jar.write_text(
            "# Netscape HTTP Cookie File\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\tfb-value\n"
            ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tfb-secret\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tyt-secret\n",
            encoding="utf-8",
        )

        header = m._cookie_header(str(jar), "www.facebook.com")
        assert "c_user=fb-value" in header
        assert "xs=fb-secret" in header
        assert "yt-secret" not in header, "YouTube cookies must not reach Facebook"

        # A different registrable domain gets nothing at all.
        assert m._cookie_header(str(jar), "fb.watch") == ""
        assert m._cookie_header(str(jar), "www.tiktok.com") == ""
        # No jar, unreadable jar, or no host: silent empty, never an exception.
        assert m._cookie_header(None, "www.facebook.com") == ""
        assert m._cookie_header(str(jar / "nope"), "www.facebook.com") == ""
        assert m._cookie_header(str(jar), "") == ""

        captured: list[dict] = []

        class _Recorder(_FakeSession):
            def get(self, url, *, allow_redirects=True, max_redirects=5):
                captured.append(dict(self.headers))
                return super().get(
                    url, allow_redirects=allow_redirects, max_redirects=max_redirects
                )

        fake = types.ModuleType("aiohttp")
        fake.ClientTimeout = lambda total=None: types.SimpleNamespace(total=total)
        fake.ClientSession = lambda timeout=None, headers=None: _Recorder(
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/878787878787/", body=b"<html></html>"
            ),
            [],
            headers=headers,
        )
        sys.modules["aiohttp"] = fake

        got = _run(m.canonical_facebook(SAMPLE_SHARE, cookie_file=str(jar)))
        assert got == "https://www.facebook.com/reel/878787878787"
        assert captured and "Cookie" in captured[0]
        assert "c_user=fb-value" in captured[0]["Cookie"]
    finally:
        jar.unlink(missing_ok=True)
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_no_cookie_header_without_a_jar():
    """The anonymous path must not invent an empty Cookie header."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        captured: list[dict] = []

        class _Recorder(_FakeSession):
            def get(self, url, *, allow_redirects=True, max_redirects=5):
                captured.append(dict(self.headers))
                return super().get(
                    url, allow_redirects=allow_redirects, max_redirects=max_redirects
                )

        fake = types.ModuleType("aiohttp")
        fake.ClientTimeout = lambda total=None: types.SimpleNamespace(total=total)
        fake.ClientSession = lambda timeout=None, headers=None: _Recorder(
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/919191919191/", body=b"<html></html>"
            ),
            [],
            headers=headers,
        )
        sys.modules["aiohttp"] = fake

        _run(m.canonical_facebook(SAMPLE_SHARE))
        assert captured and "Cookie" not in captured[0]
        assert captured[0]["User-Agent"].startswith("Mozilla/5.0")
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_cache_is_bounded():
    """An unbounded map keyed by user input is a memory leak."""
    _, saved = _install_stubs()
    try:
        m = _load_module()
        for index in range(m._CACHE_MAX * 2):
            m._cache_put(f"https://fb.watch/k{index}/", f"https://x/{index}")
            assert len(m._cache) <= m._CACHE_MAX
        assert m._cache, "eviction must not empty the cache"
    finally:
        _restore_stubs(saved)


def test_resolution_can_be_switched_off():
    """Operators keep an escape hatch; the offline pass keeps working."""
    _, saved = _install_stubs(SOCIAL_URL_RESOLVE_ENABLED=False)
    try:
        m = _load_module()
        calls = _install_fake_aiohttp(
            m, lambda url: _FakeResponse("https://www.facebook.com/reel/1/")
        )
        assert _run(m.canonical_facebook(SAMPLE_SHARE)) == SAMPLE_SHARE
        assert calls == [], "disabled must mean no request at all"
        assert _run(m.canonical_facebook(SAMPLE_REEL)) == (
            "https://www.facebook.com/reel/995756753386001"
        )
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


def test_timeout_and_hop_limits_come_from_config():
    _, saved = _install_stubs(
        SOCIAL_URL_RESOLVE_TIMEOUT_SEC=3.5, SOCIAL_URL_RESOLVE_MAX_HOPS=2
    )
    try:
        m = _load_module()
        calls = _install_fake_aiohttp(
            m,
            lambda url: _FakeResponse(
                "https://www.facebook.com/reel/565656565656/", body=b"<html></html>"
            ),
        )
        _run(m.canonical_facebook(SAMPLE_SHARE))
        assert calls[0]["hops"] == 2
    finally:
        sys.modules.pop("aiohttp", None)
        _restore_stubs(saved)


# ---------------------------------------------------------------------------
# 5. provider wiring (source-level, no Telegram/yt-dlp needed)
# ---------------------------------------------------------------------------


def test_provider_regexes_accept_every_reported_shape():
    """Routing must not regress while the rewrite layer is added."""
    tiktok_src = (ROOT / "AnonX_3" / "core" / "tiktok.py").read_text("utf-8")
    facebook_src = (ROOT / "AnonX_3" / "core" / "facebook.py").read_text("utf-8")

    tt_pattern = re.search(r'r"(\^https\?://[^"]*tiktok[^"]*)"', tiktok_src)
    assert tt_pattern, "TikTok regex not found"
    tt_regex = re.compile(tt_pattern.group(1).replace("\\\\", "\\"), re.IGNORECASE)

    fb_parts = re.findall(r'r"(\^?https?[^"]*|\(\?:[^"]*)"', facebook_src)
    assert fb_parts, "Facebook regex not found"

    for url in (
        "https://www.tiktok.com/@u/story/7300000000000000009",
        "https://tiktok.com/@u/video/7300000000000000009",
        "https://vt.tiktok.com/ZSabcdef/",
        "https://m.tiktok.com/v/7300000000000000009.html",
    ):
        assert tt_regex.match(url), url
    assert not tt_regex.match("https://www.youtube.com/watch?v=abc")

    # Facebook's pattern is split across source lines; rebuild it the same way
    # the module does.
    fb_regex = re.compile(
        r"^https?://(?:[\w-]+\.)?"
        r"(?:facebook\.com|fb\.watch|fb\.com|fb\.me|fb\.gg)/.*",
        re.IGNORECASE,
    )
    assert '(?:facebook\\.com|fb\\.watch|fb\\.com|fb\\.me|fb\\.gg)/.*' in facebook_src
    assert 'r"^https?://(?:[\\w-]+\\.)?"' in facebook_src
    for url in (
        SAMPLE_SHARE,
        SAMPLE_REEL,
        "https://fb.watch/AbC123xyZ/",
        "https://web.facebook.com/watch/?v=1234567890",
        "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Freel%2F1%2F",
        "https://mbasic.facebook.com/story.php?story_fbid=1&id=2",
    ):
        assert fb_regex.match(url), url
    assert not fb_regex.match("https://notfacebook.example/share/v/x/")


def test_both_providers_canonicalise_before_extracting():
    """The rewrite must sit at resolve(), the single Track.url producer."""
    for name, call in (
        ("tiktok", "social_urls.canonical_tiktok(url, cookie_file=cookie)"),
        ("facebook", "social_urls.canonical_facebook(url, cookie_file=cookie)"),
    ):
        src = (ROOT / "AnonX_3" / "core" / f"{name}.py").read_text("utf-8")
        assert "from AnonX_3.core import social_urls" in src, name
        head = src.split("async def resolve(")[1].split("def _extract()")[0]
        assert f"url = await {call}" in head, name
        # It has to happen before yt-dlp is handed the link, and before the
        # media_id is derived, or the cache key would key off the raw URL.
        body = src.split("async def resolve(")[1]
        assert body.index(f"url = await {call}") < body.index("extract_info("), name
        assert body.index(f"url = await {call}") < body.index("_build_media_id("), name
        # Rebinding `url` is what propagates the canonical value into Track.url
        # and from there into download / direct-stream / cache.
        assert "url=url," in body, name


def test_config_exposes_the_social_url_knobs():
    src = (ROOT / "config.py").read_text("utf-8")
    for knob in (
        "SOCIAL_URL_RESOLVE_ENABLED",
        "SOCIAL_URL_RESOLVE_TIMEOUT_SEC",
        "SOCIAL_URL_RESOLVE_MAX_HOPS",
        "SOCIAL_URL_CACHE_TTL_SEC",
        "SOCIAL_URL_NEGATIVE_TTL_SEC",
    ):
        assert f"self.{knob}" in src, knob
        assert knob in (ROOT / "sample.env").read_text("utf-8"), knob


def test_module_never_reaches_for_a_proxy():
    """TikTok and Facebook need a direct connection, as resolve() documents."""
    src = MODULE_PATH.read_text("utf-8")
    assert "proxy" not in src.lower()
    # Lazy import, matching the playback_orchestrator convention.
    assert src.count("import aiohttp") == 1
    assert "    try:\n        import aiohttp" in src


# ---------------------------------------------------------------------------


def main() -> int:
    tests = [
        test_tiktok_story_becomes_a_video_url,
        test_tiktok_photo_and_legacy_shapes_are_rewritten,
        test_tiktok_host_is_forced_to_www,
        test_tiktok_handle_outside_the_extractor_charset_falls_back_to_embed,
        test_supported_tiktok_urls_survive_unchanged,
        test_facebook_reel_sample_loses_only_its_tracking_noise,
        test_facebook_watch_urls_keep_the_video_id,
        test_facebook_link_shim_is_unwrapped_offline,
        test_share_and_shortener_links_are_flagged_for_lookup,
        test_non_provider_and_malformed_input_is_returned_byte_identical,
        test_facebook_share_sample_resolves_through_a_redirect,
        test_fb_watch_shortener_resolves_to_a_watch_url,
        test_a_shortened_tiktok_story_is_resolved_then_rewritten,
        test_interstitial_page_is_scraped_for_its_canonical_url,
        test_embedded_video_id_is_the_last_resort,
        test_lookup_failure_falls_back_to_the_rewritten_url,
        test_an_unreachable_provider_is_not_retried_on_every_play,
        test_provider_cookies_are_sent_only_to_their_own_host,
        test_no_cookie_header_without_a_jar,
        test_missing_aiohttp_degrades_to_the_offline_pass,
        test_non_html_response_is_not_scraped,
        test_html_read_is_capped,
        test_a_resolved_share_link_is_cached,
        test_concurrent_requests_for_one_link_are_single_flighted,
        test_cache_is_bounded,
        test_resolution_can_be_switched_off,
        test_timeout_and_hop_limits_come_from_config,
        test_provider_regexes_accept_every_reported_shape,
        test_both_providers_canonicalise_before_extracting,
        test_config_exposes_the_social_url_knobs,
        test_module_never_reaches_for_a_proxy,
    ]

    passed = 0
    for test in tests:
        try:
            test()
        except Exception as ex:
            print(f"FAIL {test.__name__}: {type(ex).__name__}: {ex}")
        else:
            passed += 1
            print(f"OK   {test.__name__}")

    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
