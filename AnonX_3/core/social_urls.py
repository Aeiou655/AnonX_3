# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


"""Canonicalise the TikTok and Facebook links people actually paste.

yt-dlp accepts only a narrow set of URL shapes for these two providers. Checked
against the pinned yt-dlp (2026.07.04) extractor patterns, several very common
Telegram pastes match *no* extractor at all, so ``/play`` and ``/vplay`` failed
before a single byte was fetched:

* ``tiktok.com/@user/story/<id>``   — story items; only ``/video/`` is known
* ``tiktok.com/@user/photo/<id>``   — photo / slideshow posts
* ``tiktok.com/@user/video/<id>``   — the item pattern hardcodes ``www.``
* ``m.tiktok.com/v/<id>.html``      — legacy mobile share
* ``facebook.com/share/v/<token>/`` — today's Facebook share button
* ``facebook.com/share/r/<token>/`` — reel share
* ``fb.watch/<code>/``              — Facebook's shortener

Two passes cover all of them:

1. :func:`canonical_tiktok_url` / :func:`canonical_facebook_url` rewrite the
   shapes whose canonical form is derivable from the URL text alone. Pure,
   synchronous, offline.
2. :func:`canonical_tiktok` / :func:`canonical_facebook` additionally follow
   share and shortener links, whose destination only the server knows. Results
   are TTL-cached and single-flighted, so the same link pasted in ten groups
   costs one request.

Both passes also drop share-sheet tracking parameters. Those never select the
media but they do change the URL string, which would otherwise split the
download singleflight and the CDN cache key across copies of one video.

Everything degrades to its input. When a link cannot be improved — or the
network hop fails, or ``aiohttp`` is missing — the original string comes back
and the provider behaves exactly as it did before.
"""

from __future__ import annotations

import html as html_lib
import re
import time
from collections.abc import Iterator
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from AnonX_3 import config, logger
from AnonX_3.core.downloader.singleflight import SingleFlight

_social_url_flight = SingleFlight("social-url-resolve")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Read at most this much HTML when a share page has to be scraped for its
#: canonical URL. og:url lives in <head>, far inside this bound.
_HTML_SCAN_BYTES = 262_144

#: Attribution keys Facebook appends to share links.
_FB_DROP_PARAMS = frozenset(
    {
        "mibextid", "fs", "s", "rdid", "share_url", "extid", "eav", "sfnsn",
        "idorvanity", "paipv", "_rdr", "_rdc", "hc_ref", "ref", "refsrc",
        "fref", "acontext", "notif_id", "notif_t", "wtsid", "checkpoint_src",
        "app", "locale2",
    }
)

#: The same idea for TikTok's share sheet.
_TT_DROP_PARAMS = frozenset(
    {
        "_r", "_d", "_t", "checksum", "is_from_webapp", "sender_device",
        "sender_web_id", "web_id", "share_app_id", "share_link_id",
        "share_item_id", "share_author_id", "share_iid", "social_share_type",
        "tt_from", "source", "enter_from", "u_code", "preview_pb", "language",
        "timestamp", "user_id", "sec_user_id", "sec_uid", "iid",
        "app_language", "region", "ug_btm",
    }
)

_TT_SHORT_HOSTS = frozenset({"vm.tiktok.com", "vt.tiktok.com"})
_FB_SHORT_HOSTS = frozenset({"fb.watch", "fb.gg"})

#: ``/@user/video|story|photo|v/<id>`` and the handle-less variants.
_TT_ITEM = re.compile(
    r"^/(?:@(?P<user>[^/]+)/)?(?:video|story|photo|v)/(?P<id>\d+)",
    re.IGNORECASE,
)
_TT_SHORT_PATH = re.compile(r"^/t/[\w-]+", re.IGNORECASE)
#: yt-dlp's own handle character class. A handle outside it would not match
#: even after the rewrite, so those fall back to the handle-free form.
_TT_USER_OK = re.compile(r"^[\w.-]+$")

_FB_SHARE = re.compile(
    r"^/share/(?:v|r|p|g|video|reel|post)/(?P<token>[\w-]+)", re.IGNORECASE
)
_FB_REEL = re.compile(r"^/reel/(?P<id>\d+)", re.IGNORECASE)

_CANONICAL_TAG = re.compile(r"<(?:meta|link)\b[^>]*>", re.IGNORECASE)
_TAG_KEY = re.compile(
    r'(?:property|rel)\s*=\s*["\']?(?:og:url|canonical)["\']?', re.IGNORECASE
)
_TAG_VAL = re.compile(r'(?:content|href)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

#: Last-resort scrapes when the page carries no canonical tag.
_FB_ID_IN_HTML = (
    re.compile(r"facebook\.com\\?/reel\\?/(\d{6,})", re.IGNORECASE),
    re.compile(r'"video_id"\s*:\s*"(\d{6,})"', re.IGNORECASE),
    re.compile(r'"videoId"\s*:\s*"(\d{6,})"', re.IGNORECASE),
    re.compile(r"[?&]v=(\d{6,})", re.IGNORECASE),
)
_TT_ID_IN_HTML = (
    re.compile(
        r"tiktok\.com\\?/@[\w.-]+\\?/(?:video|photo|story)\\?/(\d{6,})",
        re.IGNORECASE,
    ),
    re.compile(r'"itemId"\s*:\s*"(\d{6,})"', re.IGNORECASE),
    re.compile(r'"aweme_id"\s*:\s*"(\d{6,})"', re.IGNORECASE),
)


def _cfg(name: str, default):
    """Read a config knob, tolerating a missing or None value."""
    try:
        value = getattr(config, name, default)
    except Exception:
        return default
    return default if value is None else value


def _split(url: str) -> SplitResult | None:
    """Parse a URL, returning None for anything that is not plain http(s)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return parts


def _host(parts: SplitResult) -> str:
    return parts.netloc.lower().split("@")[-1].split(":")[0]


def _rebuild(
    parts: SplitResult,
    *,
    netloc: str | None = None,
    path: str | None = None,
    query: str | None = None,
) -> str:
    """Reassemble a URL over https, dropping the fragment."""
    return urlunsplit(
        (
            "https",
            netloc if netloc is not None else parts.netloc,
            path if path is not None else parts.path,
            query if query is not None else parts.query,
            "",
        )
    )


def _strip_params(query: str, drop: frozenset[str]) -> str:
    """Remove attribution keys, keeping order and every other parameter."""
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [
        (key, value)
        for key, value in pairs
        if key.lower() not in drop and not key.lower().startswith("utm_")
    ]
    if len(kept) == len(pairs):
        # Nothing to drop: hand back the original text so signed values are
        # never re-quoted.
        return query
    return urlencode(kept)


def _is_tiktok(host: str) -> bool:
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _is_facebook(host: str) -> bool:
    return host in ("facebook.com", "fb.com", "fb.me") or host.endswith(
        (".facebook.com", ".fb.com")
    )


def canonical_tiktok_url(url: str) -> str:
    """Rewrite a TikTok link into a shape yt-dlp's extractor accepts.

    Offline and total: an unrecognised link comes back unchanged.
    """
    parts = _split(url)
    if parts is None:
        return url
    host = _host(parts)
    if not _is_tiktok(host):
        return url
    # Short links carry no item id; only the server knows the target. Keep the
    # vm/vt host (yt-dlp matches it directly) and shed the share noise.
    if host in _TT_SHORT_HOSTS:
        return _rebuild(parts, netloc=host, query="")
    if _TT_SHORT_PATH.match(parts.path):
        # The /t/ pattern hardcodes www.
        return _rebuild(parts, netloc="www.tiktok.com", query="")
    item = _TT_ITEM.match(parts.path)
    if item:
        item_id = item.group("id")
        user = (item.group("user") or "").strip()
        if user and _TT_USER_OK.match(user):
            path = f"/@{user}/video/{item_id}"
        else:
            # No usable handle: /embed/<id> is the handle-free canonical form.
            path = f"/embed/{item_id}"
        return _rebuild(parts, netloc="www.tiktok.com", path=path, query="")
    # Unknown shape: still normalise the host, since every TikTok item pattern
    # hardcodes www., and drop the share noise.
    return _rebuild(
        parts,
        netloc="www.tiktok.com",
        query=_strip_params(parts.query, _TT_DROP_PARAMS),
    )


def canonical_facebook_url(url: str) -> str:
    """Rewrite a Facebook link into a shape yt-dlp's extractor accepts."""
    parts = _split(url)
    if parts is None:
        return url
    host = _host(parts)
    if host in _FB_SHORT_HOSTS:
        # An opaque shortener code: only the share noise can go.
        return _rebuild(parts, netloc=host, query="")
    if not _is_facebook(host):
        return url
    # l.facebook.com/l.php?u=<encoded> wraps an ordinary link. Unwrap it here
    # instead of paying a redirect, but only when it points back at Facebook —
    # handing an arbitrary third-party URL to this provider is not our job.
    if parts.path.rstrip("/") == "/l.php":
        inner = dict(parse_qsl(parts.query, keep_blank_values=True)).get("u", "")
        unwrapped = unquote(inner) if inner else ""
        inner_parts = _split(unwrapped)
        if inner_parts is not None and _is_facebook(_host(inner_parts)):
            return canonical_facebook_url(unwrapped)
    reel = _FB_REEL.match(parts.path)
    if reel:
        return _rebuild(
            parts,
            netloc="www.facebook.com",
            path=f"/reel/{reel.group('id')}",
            query="",
        )
    # Everything else keeps its path; only the host and the tracking noise
    # change. Mobile hosts redirect to www anyway, and www is the shape the
    # extractor is exercised against.
    return _rebuild(
        parts,
        netloc="www.facebook.com",
        query=_strip_params(parts.query, _FB_DROP_PARAMS),
    )


def tiktok_needs_lookup(url: str) -> bool:
    """True when only TikTok's server can say which item a link points at."""
    parts = _split(url)
    if parts is None:
        return False
    host = _host(parts)
    if host in _TT_SHORT_HOSTS:
        return True
    return bool(_is_tiktok(host) and _TT_SHORT_PATH.match(parts.path))


def facebook_needs_lookup(url: str) -> bool:
    """True when only Facebook's server can say which video a link points at."""
    parts = _split(url)
    if parts is None:
        return False
    host = _host(parts)
    if host in _FB_SHORT_HOSTS:
        return True
    return bool(_is_facebook(host) and _FB_SHARE.match(parts.path))


# ---------------------------------------------------------------------------
# Bounded network lookup for share / shortener links
# ---------------------------------------------------------------------------

_CACHE_MAX = 512
_cache: dict[str, tuple[float, str]] = {}


def _cache_get(key: str) -> str | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    expires, value = hit
    if expires <= time.monotonic():
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: str, *, ttl: float | None = None) -> None:
    if ttl is None:
        ttl = float(_cfg("SOCIAL_URL_CACHE_TTL_SEC", 3600))
    ttl = max(5.0, ttl)
    if len(_cache) >= _CACHE_MAX:
        # Bounded eviction: shed the quarter closest to expiry.
        for stale in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 4]:
            _cache.pop(stale, None)
    _cache[key] = (time.monotonic() + ttl, value)


def cache_clear() -> None:
    """Drop every memoised resolution. Used by tests and /reload paths."""
    _cache.clear()


def _cookie_header(cookie_file: str | None, host: str) -> str:
    """Build a Cookie header for ``host`` from a Netscape cookie file.

    Share links sometimes get a login interstitial instead of a redirect. The
    providers already keep a cookie jar for yt-dlp, so reusing it here turns
    those interstitials back into redirects.

    Cookies are matched against the target host, so the shared cookie directory
    can hold a YouTube jar without ever sending it to Facebook.
    """
    if not cookie_file or not host:
        return ""
    try:
        from http.cookiejar import MozillaCookieJar

        jar = MozillaCookieJar()
        jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
    except Exception as ex:
        logger.debug("social url: cookie jar unusable: %s", type(ex).__name__)
        return ""
    pairs = []
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if not domain or not (host == domain or host.endswith(f".{domain}")):
            continue
        if cookie.name and cookie.value:
            pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs)


async def _fetch_final_url(
    url: str, *, timeout: float, max_hops: int, cookie_file: str | None = None
) -> tuple[str, str]:
    """Follow redirects and return ``(final_url, html_head)``.

    Never raises. ``html_head`` is empty unless an HTML body was read, and the
    read is capped at :data:`_HTML_SCAN_BYTES`.
    """
    try:
        import aiohttp
    except ImportError:
        logger.debug("social url: aiohttp missing, skipping redirect lookup")
        return url, ""

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    parts = _split(url)
    cookies = _cookie_header(cookie_file, _host(parts)) if parts else ""
    if cookies:
        headers["Cookie"] = cookies
    timeout_cfg = aiohttp.ClientTimeout(total=max(1.0, float(timeout)))
    try:
        async with aiohttp.ClientSession(
            timeout=timeout_cfg, headers=headers
        ) as session:
            async with session.get(
                url, allow_redirects=True, max_redirects=max(1, int(max_hops))
            ) as response:
                final = str(response.url)
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "html" not in content_type:
                    return final, ""
                body = await response.content.read(_HTML_SCAN_BYTES)
                return final, body.decode("utf-8", "replace")
    except Exception as ex:
        logger.debug("social url lookup failed url=%s: %s", url[:80], ex)
        return url, ""


def _unescape(value: str) -> str:
    return html_lib.unescape(value.replace("\\/", "/")).strip()


def _html_candidates(html: str, *, kind: str) -> Iterator[str]:
    """Yield URLs the page claims to be, most trustworthy first."""
    if not html:
        return
    for tag in _CANONICAL_TAG.findall(html):
        if not _TAG_KEY.search(tag):
            continue
        value = _TAG_VAL.search(tag)
        if value:
            yield _unescape(value.group(1))
    patterns = _TT_ID_IN_HTML if kind == "tiktok" else _FB_ID_IN_HTML
    for pattern in patterns:
        found = pattern.search(html)
        if not found:
            continue
        item_id = found.group(1)
        if kind == "tiktok":
            yield f"https://www.tiktok.com/embed/{item_id}"
        else:
            yield f"https://www.facebook.com/watch/?v={item_id}"


def _rewriter(kind: str):
    return canonical_tiktok_url if kind == "tiktok" else canonical_facebook_url


def _needs_lookup(kind: str):
    return tiktok_needs_lookup if kind == "tiktok" else facebook_needs_lookup


async def _lookup(url: str, *, kind: str, cookie_file: str | None = None) -> str:
    """Resolve one share/shortener link to a canonical URL."""
    rewrite = _rewriter(kind)
    needs = _needs_lookup(kind)
    final, html = await _fetch_final_url(
        url,
        timeout=float(_cfg("SOCIAL_URL_RESOLVE_TIMEOUT_SEC", 8)),
        max_hops=int(_cfg("SOCIAL_URL_RESOLVE_MAX_HOPS", 5)),
        cookie_file=cookie_file,
    )
    candidate = rewrite(final)
    if not needs(candidate):
        return candidate
    # Still a share link: the destination is in the page instead of a Location
    # header, which is what Facebook does when it serves an interstitial.
    for raw in _html_candidates(html, kind=kind):
        guess = rewrite(raw)
        if guess and not needs(guess):
            return guess
    return candidate


async def _canonicalise(url: str, kind: str, cookie_file: str | None = None) -> str:
    rewrite = _rewriter(kind)
    needs = _needs_lookup(kind)
    try:
        local = rewrite(url)
    except Exception as ex:
        logger.warning("%s url rewrite failed url=%s: %s", kind, (url or "")[:80], ex)
        return url
    if not needs(local) or not bool(_cfg("SOCIAL_URL_RESOLVE_ENABLED", True)):
        return local
    cached = _cache_get(local)
    if cached:
        return cached
    try:
        # Keyed on the URL alone: the same link resolves to the same target
        # regardless of which caller's cookie jar got there first.
        resolved = await _social_url_flight.do(
            f"{kind}:{local}",
            lambda: _lookup(local, kind=kind, cookie_file=cookie_file),
        )
    except Exception as ex:
        logger.warning("%s url lookup failed url=%s: %s", kind, local[:80], ex)
        return local
    final = resolved or local
    if final != local:
        _cache_put(local, final)
        logger.info("%s share link resolved %s -> %s", kind, local[:80], final[:80])
    else:
        # The hop produced nothing — usually an unreachable provider or a
        # login wall. Remember that briefly so a retried /play does not pay
        # the full timeout again on the foreground path.
        _cache_put(
            local, local, ttl=float(_cfg("SOCIAL_URL_NEGATIVE_TTL_SEC", 60))
        )
    return final


async def canonical_tiktok(url: str, *, cookie_file: str | None = None) -> str:
    """Canonical TikTok URL, following share links when the text is not enough."""
    return await _canonicalise(url, "tiktok", cookie_file)


async def canonical_facebook(url: str, *, cookie_file: str | None = None) -> str:
    """Canonical Facebook URL, following share/fb.watch links when needed."""
    return await _canonicalise(url, "facebook", cookie_file)
