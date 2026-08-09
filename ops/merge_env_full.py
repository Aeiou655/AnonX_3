#!/usr/bin/env python3
"""Merge missing .env keys for the current AnonX_3 deploy root.

Preserves all existing values (tokens, sessions, brand). Only adds missing keys.
"""
from __future__ import annotations

import ast
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_identity() -> str:
    values: dict[str, str] = {}
    for line in (ROOT / "VARIANT.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    project = values.get("VARIANT", "")
    if project != ROOT.name:
        raise ValueError(
            f"VARIANT.txt identity {project!r} does not match deploy root {ROOT.name!r}"
        )
    return project


def clean_default(raw: str, kind: str) -> str:
    raw = (raw or "").strip().rstrip(",")
    if raw.startswith('"') or raw.startswith("'"):
        try:
            return str(ast.literal_eval(raw))
        except Exception:
            return raw.strip("\"'")
    if raw in ("True", "False"):
        return raw
    if re.fullmatch(r"-?\d+", raw):
        return raw
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return raw
    m = re.match(r"str\((.+)\)", raw)
    if m:
        return clean_default(m.group(1), kind)
    if any(x in raw for x in ("getenv", "os.", "Path", "lambda", "config", "(")):
        return "auto" if kind == "float_auto" else ""
    try:
        return str(ast.literal_eval(raw))
    except Exception:
        return raw.strip("\"'") if raw else ""


def extract_defaults(config_text: str) -> OrderedDict:
    defaults: OrderedDict[str, str] = OrderedDict()
    patterns = [
        (r'getenv\(\s*["\']([A-Z][A-Z0-9_]+)["\']\s*,\s*([^)]+)\)', "getenv"),
        (r'_bool_env\(\s*["\']([A-Z][A-Z0-9_]+)["\']\s*,\s*([^)]+)\)', "bool"),
        (r'_int_env\(\s*["\']([A-Z][A-Z0-9_]+)["\']\s*,\s*([^,\)]+)', "int"),
        (r'_float_or_auto\(\s*["\']([A-Z][A-Z0-9_]+)["\']\s*,\s*([^)]+)\)', "float_auto"),
    ]
    for pat, kind in patterns:
        for m in re.finditer(pat, config_text):
            name = m.group(1)
            if name in defaults:
                continue
            d = clean_default(m.group(2), kind)
            if kind == "bool":
                low = str(d).lower()
                d = "True" if low in ("true", "1", "yes") else (
                    "False" if low in ("false", "0", "no") else str(d)
                )
            defaults[name] = str(d)
    return defaults


OVERRIDES = {
    "DIRECT_URL_PROBE": "off",
    "DIRECT_START_PROOF_SEC": "3",
    "YOUTUBE_DIRECT_CACHE_BG": "False",
    "CDN_ENABLED": "True",
    "CDN_PLAY_MODE": "auto",
    "CDN_MEDIA_ROOT": "auto",
    "CDN_URL_PREFIX": "auto",
    "CDN_TTL_HOURS": "auto",
    "CDN_GC_INTERVAL_SEC": "auto",
    "CDN_READY_WAIT_SEC": "auto",
    "CDN_ORIGIN_ENABLED": "auto",
    "CDN_ORIGIN_HOST": "auto",
    "CDN_ORIGIN_PORT": "auto",
    "AUTO_LEAVE": "False",
    "AUTO_END": "False",
    "THUMB_GEN": "True",
    "VIDEO_PLAY": "True",
    "DYNAMIC_QUALITY": "True",
    "AUDIO_QUALITY": "auto",
    "VIDEO_QUALITY": "auto",
    "VIDEO_MAX_HEIGHT": "auto",
    "VIDEO_MAX_WIDTH": "auto",
    "VIDEO_MAX_FPS": "auto",
    "STREAM_ADAPTIVE": "True",
    "PREFETCH_NEXT": "True",
    "PREFETCH_VIDEO": "False",
    "VIDEO_STRICT_AVC": "True",
    "ACTIVEVC_TIMEZONE": "Asia/Yangon",
    "CUSTOM_EMOJI_FORCE_BOT_API": "True",
    "TIKTOK_DIRECT_STREAM": "True",
    "TELEGRAM_DIRECT_CACHE_BG": "True",
    "HEALTH_PORT": "0",
    "SINGLEFLIGHT_BACKEND": "memory",
    "YOUTUBE_PROXY": "auto",
    "YOUTUBE_DIRECT_STREAM": "False",
    "YOUTUBE_DIRECT_STREAM_ONLY": "False",
    "TELEGRAM_DIRECT_STREAM_ONLY": "False",
    "TIKTOK_DIRECT_STREAM_ONLY": "False",
    "FALLBACK_SOUNDCLOUD": "True",
    "PLAY_GATE_REQUIRE_STABLE": "True",
    "DIRECT_MIDSTREAM_FAILOVER": "True",
    "DIRECT_FAILOVER_WINDOW_SEC": "45",
    "MONGO_TIMEOUT_MS": "12500",
}

SECTIONS = OrderedDict(
    [
        (
            "REQUIRED",
            [
                "API_ID",
                "API_HASH",
                "BOT_TOKEN",
                "MONGO_URL",
                "MONGO_TIMEOUT_MS",
                "LOGGER_ID",
                "OWNER_ID",
                "SESSION",
                "SESSION2",
                "SESSION3",
            ],
        ),
        (
            "SUPPORT",
            [
                "SUPPORT_CHANNEL",
                "SUPPORT_CHAT",
                "OWNER_USERNAME",
                "STARTGROUP_BOTS",
                "STARTGROUP_WEIGHTS",
            ],
        ),
        (
            "LIMITS",
            [
                "DURATION_LIMIT",
                "QUEUE_LIMIT",
                "PLAYLIST_LIMIT",
                "DOWNLOAD_LIMIT_GB",
                "BROADCAST_COOLDOWN_MINUTES",
                "MAX_MEDIA_DURATION_SEC",
                "MAX_DOWNLOAD_MB",
                "MAX_DOWNLOAD_CONCURRENT",
                "MAX_FFMPEG_CONCURRENT",
                "MAX_ACTIVE_STREAMS",
                "MAX_RESOLVE_CONCURRENT",
            ],
        ),
        (
            "BEHAVIOR",
            [
                "AUTO_LEAVE",
                "AUTO_END",
                "LANG_CODE",
                "THUMB_GEN",
                "VIDEO_PLAY",
                "THUMB_BOT_NAME",
                "THUMB_TOP_TEXT",
                "THUMB_CREDIT_TEXT",
                "ACTIVEVC_TIMEZONE",
                "ACTIVEVC_SAMPLE_INTERVAL_SEC",
                "CUSTOM_EMOJI_FORCE_BOT_API",
                "AUTO_DELETE_PLAY_COMMAND",
                "AUTO_DELETE_PLAY_QUEUED",
                "AUTO_DELETE_PLAY_QUEUED_SEC",
                "AUTO_DELETE_PLAY_STATUS_ON_START",
            ],
        ),
        (
            "QUALITY",
            [
                "DYNAMIC_QUALITY",
                "AUDIO_QUALITY",
                "VIDEO_QUALITY",
                "VIDEO_MAX_HEIGHT",
                "VIDEO_MAX_WIDTH",
                "VIDEO_MAX_FPS",
                "VIDEO_STRICT_AVC",
                "STREAM_ADAPTIVE",
                "PREFETCH_NEXT",
                "PREFETCH_VIDEO",
                "ADAPTIVE_CPU_HIGH",
                "ADAPTIVE_CPU_RECOVER",
                "ADAPTIVE_PING_HIGH",
                "ADAPTIVE_PING_RECOVER",
            ],
        ),
        (
            "STREAM",
            [
                "YOUTUBE_DIRECT_STREAM",
                "YOUTUBE_DIRECT_STREAM_ONLY",
                "YOUTUBE_DIRECT_CACHE_BG",
                "YOUTUBE_DIRECT_CACHE_TIMEOUT_SEC",
                "YOUTUBE_API_KEY",
                "YOUTUBE_API_RELOAD_SEC",
                "YOUTUBE_PROXY",
                "YOUTUBE_PROXY_CANDIDATES",
                "YOUTUBE_PROXY_RELOAD_SEC",
                "YOUTUBE_PROXY_VALIDATE",
                "TELEGRAM_DIRECT_STREAM_ONLY",
                "TELEGRAM_DIRECT_CACHE_BG",
                "TELEGRAM_DIRECT_CACHE_TIMEOUT_SEC",
                "TIKTOK_DIRECT_STREAM",
                "TIKTOK_DIRECT_STREAM_ONLY",
                "TIKTOK_DIRECT_CACHE_BG",
                "TIKTOK_DIRECT_CACHE_TIMEOUT_SEC",
                "DIRECT_URL_PROBE",
                "PLAY_STARTUP_GATE_SEC",
                "PLAY_JOIN_TIMEOUT_SEC",
                "PLAY_GATE_REQUIRE_STABLE",
                "DIRECT_FAILOVER_WINDOW_SEC",
                "DIRECT_MIDSTREAM_FAILOVER",
                "COOKIES_URL",
            ],
        ),
        (
            "CDN",
            [
                "CDN_ENABLED",
                "CDN_PLAY_MODE",
                "CDN_MEDIA_ROOT",
                "CDN_URL_PREFIX",
                "CDN_TTL_HOURS",
                "CDN_GC_INTERVAL_SEC",
                "CDN_READY_WAIT_SEC",
                "CDN_ORIGIN_ENABLED",
                "CDN_ORIGIN_HOST",
                "CDN_ORIGIN_PORT",
                "CDN_PUBLIC_BASE_URL",
                "CDN_POPULAR_TTL_HOURS",
                "CDN_UNCOMMON_TTL_HOURS",
                "CDN_TMP_GRACE_SEC",
            ],
        ),
        (
            "OPS",
            [
                "FALLBACK_SOUNDCLOUD",
                "FALLBACK_MIN_SCORE",
                "FALLBACK_SOFT_MIN_SCORE",
                "FALLBACK_SEARCH_LIMIT",
                "FALLBACK_SOFT_AUTO",
                "DISK_HIGH_WATER_PCT",
                "DISK_TARGET_PCT",
                "SINGLEFLIGHT_BACKEND",
                "REDIS_URL",
                "REDIS_LOCK_TTL_SEC",
                "REDIS_RESULT_TTL_SEC",
                "HEALTH_PORT",
                "HEALTH_HOST",
                "HEALTH_TOKEN",
                "PO_TOKEN_PROVIDER_ENABLED",
                "PO_TOKEN_PROVIDER_URL",
                "PO_TOKEN_CLIENT",
                "PO_TOKEN_CACHE_SEC",
                "PO_TOKEN_TIMEOUT_SEC",
                "YTDLP_MAX_RETRIES",
                "YTDLP_PLAYER_CLIENTS",
            ],
        ),
        (
            "DEEPSEEK",
            [
                "DEEPSEEK_API_KEY",
                "DEEPSEEK_MODEL",
                "DEEPSEEK_ERROR_MONITOR",
                "DEEPSEEK_ERROR_ANALYZE",
                "DEEPSEEK_ERROR_MIN_LEVEL",
                "DEEPSEEK_ERROR_COOLDOWN_SEC",
                "DEEPSEEK_ERROR_TIMEOUT_SEC",
                "DEEPSEEK_ERROR_MAX_CHARS",
            ],
        ),
        (
            "AUTOPLAY",
            [
                "AUTOPLAY_RECENT_WINDOW",
                "AUTOPLAY_MAX_ARTIST_STREAK",
                "AUTOPLAY_RECENT_TITLE_PENALTY",
                "AUTOPLAY_SAME_ARTIST_PENALTY",
                "AUTOPLAY_REPEAT_ARTIST_STREAK_PENALTY",
                "AUTOPLAY_SEED_EXACT_TITLE_PENALTY",
                "AUTOPLAY_REQUIRED_OVERLAP_MIN",
            ],
        ),
        ("PATHS", ["DEFAULT_THUMB", "PING_IMG", "START_IMG"]),
    ]
)

TITLES = {
    "REQUIRED": "=== REQUIRED ===",
    "SUPPORT": "=== SUPPORT / BRAND ===",
    "LIMITS": "=== LIMITS ===",
    "BEHAVIOR": "=== BEHAVIOR / UI ===",
    "QUALITY": "=== QUALITY / ADAPTIVE ===",
    "STREAM": "=== STREAM / DIRECT / PROXY ===",
    "CDN": "=== CDN ===",
    "OPS": "=== FALLBACK / RESOURCES / OPS ===",
    "DEEPSEEK": "=== DEEPSEEK ERROR MONITOR ===",
    "AUTOPLAY": "=== AUTOPLAY TUNING ===",
    "PATHS": "=== ASSET PATHS ===",
}

EMPTY_OK = {
    "SESSION2",
    "SESSION3",
    "COOKIES_URL",
    "CDN_PUBLIC_BASE_URL",
    "YOUTUBE_API_KEY",
    "DEEPSEEK_API_KEY",
    "REDIS_URL",
    "PO_TOKEN_PROVIDER_URL",
    "HEALTH_TOKEN",
    "SUPPORT_CHANNEL",
    "SUPPORT_CHAT",
    "OWNER_USERNAME",
    "STARTGROUP_BOTS",
    "THUMB_TOP_TEXT",
    "THUMB_CREDIT_TEXT",
    "YOUTUBE_PROXY_CANDIDATES",
    "HEALTH_HOST",
}


def parse_env(path: Path) -> OrderedDict:
    data: OrderedDict[str, str] = OrderedDict()
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if re.match(r"^[A-Z][A-Z0-9_]*$", k):
            data[k] = v
    return data


def render(
    existing: OrderedDict,
    variant: str,
    mongo_default: str,
    defaults: OrderedDict,
) -> str:
    catalog_keys: list[str] = []
    for keys in SECTIONS.values():
        catalog_keys.extend(keys)

    merged: OrderedDict[str, str] = OrderedDict()
    for k in catalog_keys:
        if k in existing:
            merged[k] = existing[k]
        elif k in defaults:
            merged[k] = defaults[k]
        elif k in EMPTY_OK:
            merged[k] = ""
        else:
            merged[k] = defaults.get(k, "")

    # preserve extras from old env
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v

    if not (merged.get("MONGO_URL") or "").strip():
        merged["MONGO_URL"] = mongo_default

    for pk in ("DEFAULT_THUMB", "PING_IMG", "START_IMG"):
        val = (merged.get(pk) or "").strip()
        if not val:
            if pk == "PING_IMG":
                merged[pk] = f"{variant}/plugins/img/ping.jpg"
            else:
                merged[pk] = f"{variant}/plugins/img/welcome.jpg"
        else:
            merged[pk] = re.sub(
                r"^AnonX_3(?:_\d+(?:_\d+)*)?/",
                f"{variant}/",
                val,
            )

    lines = [
        "# =============================================================================",
        f"# {variant} — full .env (merged; existing secrets/brand preserved)",
        "# Missing keys filled from config.py defaults + safe product overrides",
        "# =============================================================================",
        "",
    ]
    used: set[str] = set()
    for section, keys in SECTIONS.items():
        lines.append(f"# {TITLES.get(section, section)}")
        for k in keys:
            if k in merged:
                lines.append(f"{k}={merged[k]}")
                used.add(k)
        lines.append("")

    extras = [k for k in merged if k not in used]
    if extras:
        lines.append("# === EXTRA ===")
        for k in extras:
            lines.append(f"{k}={merged[k]}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        variant = project_identity()
    except (OSError, ValueError) as exc:
        print(f"identity error: {exc}")
        return 1

    config_path = ROOT / "config.py"
    if not config_path.exists():
        print("config.py not found at", config_path)
        return 1
    defaults = extract_defaults(config_path.read_text(encoding="utf-8"))
    for k, v in OVERRIDES.items():
        defaults[k] = v

    secrets = (
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "SESSION",
        "SESSION2",
        "SESSION3",
        "MONGO_URL",
        "LOGGER_ID",
        "OWNER_ID",
        "THUMB_BOT_NAME",
        "STARTGROUP_BOTS",
        "YOUTUBE_API_KEY",
        "DEEPSEEK_API_KEY",
    )

    print(f"project_root={ROOT}")
    print(f"defaults_catalog={len(defaults)}")

    mongo = f"mongodb://localhost:27017/{variant}"
    env_path = ROOT / ".env"
    backup_path = ROOT / ".env.bak_before_merge"
    temp_path = ROOT / ".env.merge.tmp"
    existing = parse_env(env_path)
    before_n = len(existing)
    if env_path.exists():
        backup_path.write_bytes(env_path.read_bytes())

    content = render(existing, variant, mongo, defaults)
    temp_path.write_text(content, encoding="utf-8", newline="\n")
    after = parse_env(temp_path)
    bad = [
        name
        for name in secrets
        if name in existing and existing[name] != after.get(name)
    ]
    if bad:
        temp_path.unlink(missing_ok=True)
        print("ERROR secret preservation failed:", ", ".join(bad))
        return 2

    temp_path.replace(env_path)
    added = sorted(set(after) - set(existing))
    print(f"{variant}: before={before_n} after={len(after)} added={len(added)}")
    if added[:12]:
        suffix = "..." if len(added) > 12 else ""
        print("  added:", ", ".join(added[:12]), suffix)
    print("  secrets/brand preserved OK")
    for required in ("API_ID", "API_HASH", "BOT_TOKEN", "SESSION", "MONGO_URL"):
        if not (after.get(required) or "").strip():
            print(f"  WARN empty required: {required}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
