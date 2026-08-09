# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import ast
import json
import os
import re
import unicodedata

from pyrogram import StopPropagation, enums, errors, filters, types
from pymongo.errors import ServerSelectionTimeoutError

from AnonX_3 import app, config, db, lang, logger
from AnonX_3.helpers import utils

_REPLY_FILE = os.path.join(os.path.dirname(__file__), "..", "locales", "reply.json")
_AUTOREMOVE_FILE = os.path.join(os.path.dirname(__file__), "..", "locales", "autoremove.json")
_LEGACY_REPLY_MIGRATED = False


def _short_db_error(ex: Exception, limit: int = 220) -> str:
    text = str(ex or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _load_replies() -> dict:
    if not os.path.isfile(_REPLY_FILE):
        return {}
    try:
        with open(_REPLY_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_autoremove_keywords() -> set[str]:
    if not os.path.isfile(_AUTOREMOVE_FILE):
        return set()
    try:
        with open(_AUTOREMOVE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return set()
    if isinstance(payload, dict):
        payload = payload.get("keywords", [])
    if not isinstance(payload, list):
        return set()
    return {str(item).lower().strip() for item in payload if str(item).strip()}


def _save_autoremove_keywords(keywords: set[str]) -> None:
    with open(_AUTOREMOVE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(keywords), f, ensure_ascii=False, indent=2)


async def _is_group_admin(chat_id: int, user_id: int) -> bool:
    if user_id in app._sudo_ids:
        return True
    if user_id in await db.get_admins(chat_id):
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    }


async def _auto_reply_global_scope(m: types.Message) -> bool | None:
    if not m.from_user:
        await m.reply_text(m.lang["user_no_perms"])
        return None
    if m.chat.id == app.logger and m.from_user.id in app._sudo_ids:
        return True
    if await _is_group_admin(m.chat.id, m.from_user.id):
        return False
    await m.reply_text(m.lang["user_no_perms"])
    return None


def _scope_label(m: types.Message, global_scope: bool) -> str:
    key = "auto_reply_scope_global" if global_scope else "auto_reply_scope_group"
    fallback = "all groups" if global_scope else "this group"
    return m.lang.get(key, fallback)


def _parse_state_arg(arg: str) -> bool | None:
    states = {
        "on": True,
        "off": False,
        "enable": True,
        "disable": False,
        "true": True,
        "false": False,
        "yes": True,
        "no": False,
        "1": True,
        "0": False,
    }
    return states.get(arg)


def _looks_like_variant_dump(text: str) -> bool:
    """True when text is a str()'d list of {text, entities} dicts (bad legacy store)."""
    raw = str(text or "").strip()
    if not raw.startswith("["):
        return False
    return ("'text'" in raw or '"text"' in raw) and (
        "entities" in raw or "custom_emoji" in raw
    )


def _try_parse_variant_payload(value):
    """Parse multi-variant reply payload from list / JSON / Python repr."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return None


def _coerce_rule_dict(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text", "")
    if not isinstance(text, str):
        if isinstance(text, list):
            # Nested dump inside text — expand later
            return None
        text = str(text or "")
    entities = item.get("entities", [])
    if not isinstance(entities, list):
        entities = []
    return {"text": text, "entities": entities}


def _list_variants(rule) -> list[dict]:
    """Expand stored rule into ordered variants (oldest → newest as learned)."""
    if isinstance(rule, str) and _looks_like_variant_dump(rule):
        parsed = _try_parse_variant_payload(rule)
        if parsed is not None:
            rule = parsed
    if isinstance(rule, list):
        out = []
        for item in rule:
            coerced = _coerce_rule_dict(item)
            if coerced and coerced["text"] and not _looks_like_variant_dump(coerced["text"]):
                out.append(coerced)
        return out
    if isinstance(rule, dict):
        text = rule.get("text", "")
        if isinstance(text, list):
            return _list_variants(text)
        if isinstance(text, str) and _looks_like_variant_dump(text):
            parsed = _try_parse_variant_payload(text)
            if parsed is not None:
                return _list_variants(parsed)
        coerced = _coerce_rule_dict(rule)
        return [coerced] if coerced and coerced["text"] else []
    if isinstance(rule, str) and rule.strip():
        return [{"text": rule, "entities": []}]
    return []


# Per-chat+keyword loop index — newest-first round-robin, one reply each time.
_variant_loop_idx: dict[tuple[int, str], int] = {}


def _pick_loop_variant(chat_id: int, keyword: str, rule) -> dict:
    """
    Loop learned answers: newest first, then older, then wrap.
    Exactly ONE variant per trigger (no multi-send).
    """
    variants = _list_variants(rule)
    if not variants:
        return {"text": "", "entities": []}
    if len(variants) == 1:
        return _finalize_rule_dict(variants[0])

    # Storage is oldest→newest; loop order is newest→oldest
    ordered = list(reversed(variants))
    loop_key = (int(chat_id), _normalize_keyword(keyword))
    idx = _variant_loop_idx.get(loop_key, 0) % len(ordered)
    chosen = ordered[idx]
    _variant_loop_idx[loop_key] = idx + 1
    return _finalize_rule_dict(chosen)


def _finalize_rule_dict(rule: dict) -> dict:
    text = str((rule or {}).get("text") or "")
    raw_entities = (rule or {}).get("entities", [])
    if not isinstance(raw_entities, list):
        raw_entities = []
    _, _, normalized_entities = utils.deserialize_entities(raw_entities)
    return {"text": text, "entities": normalized_entities}


def _normalize_rule(rule) -> tuple[dict, bool]:
    """
    Normalize one auto-reply rule for send/storage.

    Supports:
    - {text, entities} with Telegram Premium custom_emoji entities
    - list of variants (first/newest resolved by caller via _pick_loop_variant)
    - accidental str(list) dumps from older bugs (recover instead of echoing dump)
    """
    changed = False

    if isinstance(rule, str) and _looks_like_variant_dump(rule):
        parsed = _try_parse_variant_payload(rule)
        if parsed is not None:
            rule = parsed
            changed = True

    if isinstance(rule, list):
        variants = _list_variants(rule)
        if not variants:
            return {"text": "", "entities": []}, True
        # Default single pick without chat context: newest only
        return _finalize_rule_dict(variants[-1]), True

    if not isinstance(rule, dict):
        text = str(rule or "")
        if _looks_like_variant_dump(text):
            parsed = _try_parse_variant_payload(text)
            if parsed is not None:
                return _normalize_rule(parsed)
        return {"text": text, "entities": []}, True

    text = rule.get("text", "")
    if isinstance(text, list):
        return _normalize_rule(text)
    if not isinstance(text, str):
        text = str(text or "")
        changed = True
    if _looks_like_variant_dump(text):
        parsed = _try_parse_variant_payload(text)
        if parsed is not None:
            return _normalize_rule(parsed)

    raw_entities = rule.get("entities", [])
    if not isinstance(raw_entities, list):
        raw_entities = []
        changed = True

    _, entities_changed, normalized_entities = utils.deserialize_entities(raw_entities)
    normalized = {"text": text, "entities": normalized_entities}
    changed = changed or entities_changed or normalized != rule
    return normalized, changed


def _normalize_rules(rules: dict) -> dict:
    """Normalize for storage: keep single {text,entities} per keyword (first clean)."""
    normalized = {}
    for keyword, rule in rules.items():
        key = _normalize_keyword(keyword)
        if not key:
            continue
        # For storage migration prefer first variant, not random
        payload = rule
        if isinstance(rule, list):
            for item in rule:
                coerced = _coerce_rule_dict(item)
                if coerced and coerced["text"] and not _looks_like_variant_dump(coerced["text"]):
                    payload = coerced
                    break
        elif isinstance(rule, str) and _looks_like_variant_dump(rule):
            parsed = _try_parse_variant_payload(rule)
            if isinstance(parsed, list):
                for item in parsed:
                    coerced = _coerce_rule_dict(item)
                    if coerced and coerced["text"]:
                        payload = coerced
                        break
        normalized[key] = _normalize_rule(payload)[0]
    return normalized


async def _send_auto_reply(m: types.Message, response: dict) -> None:
    """Send auto-reply text with Premium custom emoji entities preserved."""
    text = _clean_reply_text(str(response.get("text") or ""))
    if not text or _looks_like_variant_dump(text):
        # Never send a raw dump to chat
        response, _ = _normalize_rule(response if not _looks_like_variant_dump(text) else text)
        text = _clean_reply_text(str(response.get("text") or ""))
    if not text or _looks_like_variant_dump(text):
        logger.warning(
            "Auto-reply skipped: invalid stored text chat_id=%s",
            getattr(m.chat, "id", None),
        )
        return
    if _looks_corrupted_myanmar(text):
        logger.warning(
            "Auto-reply skipped: corrupted Myanmar text chat_id=%s text=%r",
            getattr(m.chat, "id", None),
            text[:80],
        )
        return

    entities = response.get("entities") or []
    if not isinstance(entities, list):
        entities = []
    try:
        entities = utils.sanitize_entities_for_text(text, entities) or []
    except Exception:
        entities = []

    # Pure Myanmar without premium emoji → plain text (avoids bad UTF-16 offsets)
    has_custom = any(
        str((e.get("type") if isinstance(e, dict) else getattr(e, "type", "")) or "")
        .lower()
        .replace("messageentitytype.", "")
        in {"custom_emoji", "customemoji"}
        for e in entities
    )
    my_ratio = sum(1 for c in text if "\u1000" <= c <= "\u109f") / max(1, len(text))
    if my_ratio > 0.4 and not has_custom:
        entities = []

    try:
        await utils.reply_text(
            m,
            text,
            entities=entities if entities else None,
        )
    except Exception as ex:
        # Last resort: plain text (fixes "စာမထွက်" from entity errors)
        logger.warning(
            "Auto-reply entity send failed; plain retry chat_id=%s: %s",
            getattr(m.chat, "id", None),
            ex,
        )
        await utils.reply_text(m, text, entities=None)


def _normalize_keyword(keyword: str) -> str:
    """Normalize keyword for storage/match (NFC + EN casefold; MY preserved)."""
    text = unicodedata.normalize("NFC", str(keyword or "")).strip()
    if not text:
        return ""
    # casefold for Latin; Myanmar/Unicode letters stay meaningful
    return text.casefold().strip()


def _resolve_stored_key(rules: dict, keyword: str) -> str | None:
    """Find the actual map key for a keyword (handles NFC / case / spacing)."""
    if not isinstance(rules, dict) or not rules:
        return None
    want = _normalize_keyword(keyword)
    if not want:
        return None
    if want in rules:
        return want
    for existing in rules.keys():
        if _normalize_keyword(existing) == want:
            return str(existing)
    # Also match via match-text (punctuation-stripped) for soft equality
    want_match = _normalize_match_text(want)
    for existing in rules.keys():
        if _normalize_match_text(str(existing)) == want_match:
            return str(existing)
    return None


def _normalize_match_text(text: str) -> str:
    """Strip punctuation so 'Hi?' / 'hi!!!' match keyword 'hi' (EN + MY)."""
    raw = str(text or "").casefold().strip()
    if not raw:
        return ""
    # Keep letters/numbers/Myanmar blocks; turn other punct into spaces
    cleaned = re.sub(
        r"[^\w\s\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+",
        " ",
        raw,
        flags=re.UNICODE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _keyword_matches(keyword: str, text_norm: str) -> bool:
    """Whole-phrase match (not raw substring) so 'hi' does not hit 'this'."""
    kw = _normalize_match_text(keyword)
    if not kw or not text_norm:
        return False
    if text_norm == kw:
        return True
    # Word/phrase boundaries using spaces after punct strip
    padded = f" {text_norm} "
    needle = f" {kw} "
    return needle in padded


def _find_best_reply(
    rules: dict,
    message_text: str,
    *,
    chat_id: int | None = None,
) -> tuple[str, dict] | None:
    """
    Pick at most ONE auto-reply: longest matching keyword wins.
    Multi-variant packs loop newest→oldest (one message only, no stack).
    """
    text_norm = _normalize_match_text(message_text)
    if not text_norm or not rules:
        return None
    best_key = None
    best_rule = None
    best_len = -1
    for keyword, response in rules.items():
        key = str(keyword or "").strip()
        if not key:
            continue
        if not _keyword_matches(key, text_norm):
            continue
        score = len(_normalize_match_text(key))
        if score > best_len:
            best_len = score
            best_key = key
            best_rule = response
    if best_key is None or best_rule is None:
        return None
    if chat_id is not None:
        return best_key, _pick_loop_variant(chat_id, best_key, best_rule)
    return best_key, _normalize_rule(best_rule)[0]


def _preview_text(rule, limit: int = 60) -> str:
    if isinstance(rule, list):
        n = len([x for x in rule if isinstance(x, dict)])
        first = next((x for x in rule if isinstance(x, dict)), None)
        base = _preview_text(first, limit=max(20, limit - 12)) if first else "…"
        return f"{base} (+{n} variants)" if n > 1 else base
    if not isinstance(rule, dict):
        text = str(rule or "").replace("\n", " ").strip()
    else:
        text = str((rule or {}).get("text") or "").replace("\n", " ").strip()
    if _looks_like_variant_dump(text):
        return "(multi-variant pack)"
    if len(text) <= limit:
        return text or "…"
    return text[: limit - 1] + "…"


async def _ensure_legacy_replies_migrated() -> None:
    global _LEGACY_REPLY_MIGRATED
    if _LEGACY_REPLY_MIGRATED:
        return
    legacy_rules = _normalize_rules(_load_replies())
    if legacy_rules:
        await db.migrate_auto_reply_rules(legacy_rules)
    _LEGACY_REPLY_MIGRATED = True


@app.on_message(filters.command(["autoreply"]) & filters.group & ~app.bl_users)
@lang.language()
async def _auto_reply_toggle(_, m: types.Message):
    await _ensure_legacy_replies_migrated()
    global_scope = await _auto_reply_global_scope(m)
    if global_scope is None:
        return

    raw_arg = " ".join(m.command[1:]).strip().lower()
    arg = re.split(r"\s+", raw_arg, maxsplit=1)[0] if raw_arg else ""
    enabled = (
        await db.get_auto_reply_global()
        if global_scope
        else await db.get_auto_reply(m.chat.id)
    )

    if len(m.command) == 1:
        new_state = not enabled
    else:
        new_state = _parse_state_arg(arg)
        if new_state is None:
            return await m.reply_text(m.lang["auto_reply_usage"].format(m.command[0]))

    if global_scope:
        await db.set_auto_reply_all(new_state)
    else:
        await db.set_auto_reply(m.chat.id, new_state)

    key = "auto_reply_on" if new_state else "auto_reply_off"
    fallback = "Auto-reply is now ON for {0}." if new_state else "Auto-reply is now OFF for {0}."
    await m.reply_text(m.lang.get(key, fallback).format(_scope_label(m, global_scope)))


def _is_learnable_keyword(text: str) -> bool:
    """Keyword from a replied message must stay short (teach-by-reply)."""
    raw = unicodedata.normalize("NFC", str(text or "")).strip()
    if not raw or raw.startswith("/"):
        return False
    if "\n" in raw or "\r" in raw:
        return False
    if len(raw) > 64:
        return False
    if re.match(r"https?://|t\.me/", raw, re.I):
        return False
    # Avoid learning huge sentences as keywords
    if len(raw.split()) > 8:
        return False
    return True


_BOT_STATUS_SNIPPETS = (
    # EN play/stream status
    "download ready",
    "joining voice",
    "processing file",
    "voice chat",
    "now playing",
    "stream failed",
    "download completed",
    "download failed",
    "searching",
    "searching...",
    "wait",
    "wait •",
    "• searching",
    "retry",
    "queue",
    "paused",
    "resumed",
    "loading",
    "buffering",
    "connecting",
    "fetching",
    "preparing",
    "cancel",
    # MY play/stream status (from locales)
    "ဒေါင်းလုဒ်",
    "ချိတ်ဆက်နေ",
    "ဖိုင်ကို စီမံ",
    "တန်းစီ",
    "ယခုဖွင့်နေ",
    "ဗီဒီယိုချတ်",
    "voice chat",
    "assistant",
    "floodwait",
    "flood wait",
    "ရှာနေတယ်",
    "စောင့်ပါ",
    "ခဏစောင့်",
)


def _clean_reply_text(text: str | None) -> str:
    """NFC + strip invisible junk that breaks Myanmar display."""
    raw = unicodedata.normalize("NFC", str(text or ""))
    # BOM / zero-width / bidi controls (keep normal spaces)
    raw = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]", "", raw)
    # Normalize newlines
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return raw.strip()


def _looks_corrupted_myanmar(text: str) -> bool:
    """Heuristic: too many asat/virama vs consonants → likely bad entity/Zawgyi mess."""
    if not text:
        return False
    my = [c for c in text if "\u1000" <= c <= "\u109f"]
    if len(my) < 4:
        return False
    asat = sum(1 for c in my if c == "\u103a")
    consonants = sum(1 for c in my if "\u1000" <= c <= "\u1021")
    if consonants <= 0:
        return asat >= 2
    # Healthy MY rarely has asat near or above consonant count
    if asat >= max(3, consonants):
        return True
    # Many isolated asats in a short string
    if asat >= 3 and asat / max(1, len(my)) > 0.35:
        return True
    return False


def _looks_like_bot_status(text: str) -> bool:
    low = (text or "").casefold()
    if not low:
        return False
    return any(s in low for s in _BOT_STATUS_SNIPPETS)


def _is_human_user_message(msg: types.Message | None) -> bool:
    """True only when message is from a real human user (not bot/channel/status)."""
    if msg is None:
        return False
    # Service / empty system
    if getattr(msg, "service", None) or getattr(msg, "empty", False):
        return False
    # Channel / anonymous admin as primary sender → never keyword for learn
    if getattr(msg, "sender_chat", None) is not None and getattr(msg, "from_user", None) is None:
        return False
    user = getattr(msg, "from_user", None)
    if user is None:
        return False
    if getattr(user, "is_bot", False):
        return False
    if getattr(user, "is_self", False):
        return False
    try:
        if app.me and int(user.id) == int(app.me.id):
            return False
    except Exception:
        pass
    if getattr(msg, "via_bot", None) is not None:
        return False
    return True


def _is_bot_or_status_message(msg: types.Message | None) -> bool:
    """
    True when the replied-to message must NOT be used as auto-learn keyword.

    HARD RULE: only human-to-human teach-by-reply.
    Reply under bot / status / Now Playing / download cards → never learn.
    """
    if msg is None:
        return True

    # Primary gate: keyword message must be from a human user
    if not _is_human_user_message(msg):
        return True

    text = _clean_reply_text(msg.text or msg.caption or "")
    if _looks_like_bot_status(text):
        return True

    # Inline keyboards (play controls) → bot card even if somehow attributed wrong
    if getattr(msg, "reply_markup", None) is not None:
        return True

    # Photo/video "now playing" style cards as keyword — skip
    if getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "document", None):
        # Users rarely teach with media-only keyword; captions handled above
        if not text or _looks_like_bot_status(text):
            return True

    return False


def _is_safe_learn_answer(text: str) -> bool:
    clean = _clean_reply_text(text)
    if not clean or _looks_like_variant_dump(clean):
        return False
    if _looks_like_bot_status(clean):
        return False
    if _looks_corrupted_myanmar(clean):
        return False
    if len(clean) > 1000:
        return False
    return True


def _rule_from_message(msg: types.Message) -> dict | None:
    text = _clean_reply_text(msg.text or msg.caption or "")
    if not text or _looks_like_variant_dump(text):
        return None
    if not _is_safe_learn_answer(text):
        return None
    entities = list(msg.entities or msg.caption_entities or [])
    # Sanitize offsets against cleaned text (UTF-16 safe)
    serialized = utils.serialize_entities(entities)
    try:
        safe = utils.sanitize_entities_for_text(text, serialized) or []
    except Exception:
        safe = []
    # If entities would corrupt Myanmar display, keep plain text only
    if _looks_corrupted_myanmar(text):
        safe = []
    # Drop entities when none are custom_emoji and text is pure Myanmar
    # (broken offsets are the #1 cause of "စာမထွက်")
    has_custom = any(
        (e.get("type") or "").lower() in {"custom_emoji", "customemoji"}
        for e in safe
        if isinstance(e, dict)
    )
    my_ratio = sum(1 for c in text if "\u1000" <= c <= "\u109f") / max(1, len(text))
    if my_ratio > 0.4 and not has_custom:
        safe = []
    return {
        "text": text,
        "entities": safe if safe else [],
    }


# --- Anti-spam: learn + auto-reply fire (no chat confirm spam) ---
# Learn: silent DB only. Reply: throttled so bot does not flood groups.
_AUTO_LEARN_COOLDOWN_SEC = config.AUTO_LEARN_USER_COOLDOWN_SEC
_AUTO_LEARN_KEYWORD_COOLDOWN_SEC = config.AUTO_LEARN_KEYWORD_COOLDOWN_SEC
_AUTO_LEARN_USER_BURST = config.AUTO_LEARN_USER_BURST
_AUTO_LEARN_USER_BURST_WINDOW = config.AUTO_LEARN_USER_BURST_WINDOW_SEC
_AUTO_LEARN_CONFIRMATIONS = config.AUTO_LEARN_CONFIRMATIONS

_AUTO_REPLY_CHAT_COOLDOWN_SEC = 8.0  # any auto-reply in this chat
_AUTO_REPLY_KEYWORD_COOLDOWN_SEC = 20.0  # same keyword again
_AUTO_REPLY_USER_COOLDOWN_SEC = 12.0  # same user again

_auto_learn_last: dict[tuple[int, int], float] = {}
_auto_learn_keyword_last: dict[tuple[int, str], float] = {}
_auto_learn_burst: dict[tuple[int, int], list[float]] = {}

_auto_reply_chat_last: dict[int, float] = {}
_auto_reply_keyword_last: dict[tuple[int, str], float] = {}
_auto_reply_user_last: dict[tuple[int, int], float] = {}


def _prune_timestamps(times: list[float], now: float, window: float) -> list[float]:
    cut = now - window
    return [t for t in times if t >= cut]


def _auto_reply_allowed(chat_id: int, user_id: int, keyword: str) -> bool:
    """Rate-limit outbound auto-replies (main spam surface after learn)."""
    import time as _time

    now = _time.monotonic()
    chat_id = int(chat_id)
    user_id = int(user_id)
    kw = str(keyword or "").casefold().strip()

    last_chat = _auto_reply_chat_last.get(chat_id, 0.0)
    if now - last_chat < _AUTO_REPLY_CHAT_COOLDOWN_SEC:
        return False
    last_kw = _auto_reply_keyword_last.get((chat_id, kw), 0.0)
    if kw and now - last_kw < _AUTO_REPLY_KEYWORD_COOLDOWN_SEC:
        return False
    last_user = _auto_reply_user_last.get((chat_id, user_id), 0.0)
    if now - last_user < _AUTO_REPLY_USER_COOLDOWN_SEC:
        return False

    _auto_reply_chat_last[chat_id] = now
    if kw:
        _auto_reply_keyword_last[(chat_id, kw)] = now
    _auto_reply_user_last[(chat_id, user_id)] = now
    return True


def _auto_learn_allowed(chat_id: int, user_id: int, keyword: str) -> bool:
    """Rate-limit learning so flood-replies cannot fill rules DB."""
    import time as _time

    now = _time.monotonic()
    chat_id = int(chat_id)
    user_id = int(user_id)
    kw = str(keyword or "").casefold().strip()
    cool_key = (chat_id, user_id)

    if now - _auto_learn_last.get(cool_key, 0.0) < _AUTO_LEARN_COOLDOWN_SEC:
        return False
    if kw and now - _auto_learn_keyword_last.get((chat_id, kw), 0.0) < _AUTO_LEARN_KEYWORD_COOLDOWN_SEC:
        return False

    burst_key = cool_key
    times = _prune_timestamps(
        _auto_learn_burst.get(burst_key, []), now, _AUTO_LEARN_USER_BURST_WINDOW
    )
    if len(times) >= _AUTO_LEARN_USER_BURST:
        return False

    _auto_learn_last[cool_key] = now
    if kw:
        _auto_learn_keyword_last[(chat_id, kw)] = now
    times.append(now)
    _auto_learn_burst[burst_key] = times
    return True


async def _try_auto_learn(m: types.Message) -> bool:
    """
    Teach-by-reply (everyone), but ONLY human → human:

      User A: hi / မောနင်း
      User B: (reply to that) Hello!

    NEVER learn when users reply under bot/status messages
    (Download ready, Now Playing, searching, buttons, etc.).
    Silent learn only — no chat confirm (anti-spam).
    """
    user = getattr(m, "from_user", None)
    if not user or getattr(user, "is_bot", False):
        return False

    replied = getattr(m, "reply_to_message", None)
    if not replied:
        return False

    # Never teach using our own bot messages as the answer
    if getattr(m.from_user, "is_self", False):
        return False
    try:
        if m.from_user and app.me and m.from_user.id == app.me.id:
            return False
    except Exception:
        pass

    # HARD RULE: bot/status/control message → do not learn (any user reply)
    if _is_bot_or_status_message(replied):
        logger.info(
            "Auto-learn SKIP bot/status reply chat_id=%s user=%s",
            getattr(m.chat, "id", None),
            user.id,
        )
        return False

    raw_text = _clean_reply_text(m.text or m.caption or "")
    if not raw_text:
        return False
    # Skip management commands used as the "answer"
    if raw_text.startswith("/"):
        cmd0 = raw_text.split(None, 1)[0].lower().lstrip("/")
        cmd0 = cmd0.split("@", 1)[0]
        if cmd0 in {
            "reply",
            "unreply",
            "replies",
            "autoreply",
            "autolearn",
            "start",
            "help",
            "restart",
        }:
            return False

    if not _is_safe_learn_answer(raw_text):
        logger.debug(
            "Auto-learn rejected unsafe answer chat_id=%s user=%s",
            getattr(m.chat, "id", None),
            user.id,
        )
        return False

    keyword_src = _clean_reply_text(replied.text or replied.caption or "")
    if not _is_learnable_keyword(keyword_src):
        return False
    # Don't learn bot status lines as keywords either (belt + suspenders)
    if _looks_like_bot_status(keyword_src):
        return False

    variant = _rule_from_message(m)
    if not variant:
        return False

    key = _normalize_keyword(keyword_src)
    if not key:
        return False

    # Anti-spam gates (silent skip — never notify chat)
    if not _auto_learn_allowed(int(m.chat.id), int(user.id), key):
        logger.debug(
            "Auto-learn rate-limited chat_id=%s user=%s key=%s",
            m.chat.id,
            user.id,
            key,
        )
        return False

    try:
        observations, confirmed = await db.observe_auto_reply_candidate(
            m.chat.id,
            key,
            variant,
            confirmations=_AUTO_LEARN_CONFIRMATIONS,
        )
    except Exception as ex:
        logger.warning(
            "Auto-learn candidate DB error chat_id=%s key=%s user=%s: %s",
            m.chat.id,
            key,
            user.id,
            ex,
        )
        return False

    if observations <= 0:
        # Manual and legacy rules are intentionally never changed by the
        # automatic learner.
        return False
    if not confirmed:
        logger.info(
            "Auto-learn candidate chat=%s key=%s observations=%s/%s user=%s",
            m.chat.id,
            key,
            observations,
            _AUTO_LEARN_CONFIRMATIONS,
            user.id,
        )
        return True

    try:
        count, is_new = await db.append_auto_reply_variant(
            m.chat.id, key, variant, global_scope=False
        )
    except Exception as ex:
        logger.warning(
            "Auto-learn DB error chat_id=%s key=%s user=%s: %s",
            m.chat.id,
            key,
            user.id,
            ex,
        )
        return False

    if count <= 0:
        # Duplicate answer text — not an error, not spam
        try:
            await db.clear_auto_reply_candidate(m.chat.id, key, variant["text"])
        except Exception:
            pass
        return False

    try:
        await db.clear_auto_reply_candidate(m.chat.id, key, variant["text"])
    except Exception as ex:
        logger.warning(
            "Auto-learn candidate cleanup failed chat_id=%s key=%s: %s",
            m.chat.id,
            key,
            ex,
        )

    try:
        await db.set_auto_reply(m.chat.id, True)
    except Exception:
        pass

    # Silent learn — no 📚 / no chat message (anti-spam). Log only.
    logger.info(
        "Auto-learn OK chat=%s key=%s variants=%s new=%s user=%s",
        m.chat.id,
        key,
        count,
        is_new,
        user.id,
    )
    return True


@app.on_message(filters.command(["reply"]) & filters.group & ~app.bl_users)
@lang.language()
async def _set_reply(_, m: types.Message):
    """
    Two ways (admin):
    1) Reply to keyword message + `/reply Your answer text`
       → keyword = that message, answer = command text
    2) Reply to answer message + `/reply keyword`
       → classic mode (answer keeps Premium entities)
    Or: plain reply to keyword (no command) = auto-learn for everyone.
    """
    await _ensure_legacy_replies_migrated()
    global_scope = await _auto_reply_global_scope(m)
    if global_scope is None:
        return

    replied = m.reply_to_message
    if not replied or not (replied.text or replied.caption):
        await m.reply_text(m.lang["reply_usage"])
        return

    replied_text = (replied.text or replied.caption or "").strip()
    args = " ".join(m.command[1:]).strip() if len(m.command) > 1 else ""

    # Mode 1: reply to short keyword + `/reply Your answer text`
    # (keyword comes from the message you reply to — no separate keyword inject)
    use_mode1 = bool(args) and _is_learnable_keyword(replied_text) and (
        not _is_learnable_keyword(args) or len(args) > len(replied_text)
    )
    if use_mode1:
        keyword = replied_text
        key = _normalize_keyword(keyword)
        rule, _ = _normalize_rule({"text": args, "entities": []})
        await db.set_auto_reply_rule(m.chat.id, key, rule, global_scope=global_scope)
        if global_scope:
            await db.set_auto_reply_all(True)
        else:
            await db.set_auto_reply(m.chat.id, True)
        scope = _scope_label(m, global_scope)
        await m.reply_text(
            m.lang.get(
                "reply_added_scope",
                "Auto-reply for <b>{0}</b> has been added for <b>{1}</b>.",
            ).format(keyword, scope)
        )
        return

    # Mode 2 classic: reply to answer message + `/reply keyword`
    keyword = args
    if not keyword:
        await m.reply_text(
            m.lang.get(
                "reply_usage_simple",
                "Easy ways:\n"
                "• Reply to <code>hi</code> with your answer (auto-learn, anyone)\n"
                "• Or reply to <code>hi</code> then: <code>/reply Your answer</code>\n"
                "• Or reply to your answer then: <code>/reply hi</code>",
            )
        )
        return

    text = replied_text
    entities = list(replied.entities or replied.caption_entities or [])
    key = _normalize_keyword(keyword)
    if not key:
        await m.reply_text(m.lang["reply_usage"])
        return

    if _looks_like_variant_dump(text):
        await m.reply_text(
            m.lang.get(
                "reply_invalid_source",
                "That message looks like a broken dump. Use a normal message.",
            )
        )
        return

    rules = await db.get_auto_reply_rules_for_scope(m.chat.id, global_scope=global_scope)
    was_update = key in rules or any(
        _normalize_keyword(k) == key for k in rules.keys()
    )
    rule = {
        "text": text,
        "entities": utils.serialize_entities(entities),
    }
    rule, _ = _normalize_rule(rule)
    await db.set_auto_reply_rule(m.chat.id, key, rule, global_scope=global_scope)

    if global_scope:
        if not await db.get_auto_reply_global():
            await db.set_auto_reply_all(True)
    else:
        if not await db.get_auto_reply(m.chat.id):
            await db.set_auto_reply(m.chat.id, True)

    scope = _scope_label(m, global_scope)
    if was_update:
        await m.reply_text(
            m.lang.get(
                "reply_updated_scope",
                "Auto-reply for <b>{0}</b> has been updated for <b>{1}</b>.",
            ).format(keyword, scope)
        )
    else:
        await m.reply_text(
            m.lang.get(
                "reply_added_scope",
                "Auto-reply for <b>{0}</b> has been added for <b>{1}</b>.\n"
                "Anyone who says this keyword will get the reply (one match only).",
            ).format(keyword, scope)
        )


@app.on_message(filters.command(["unreply"]) & filters.group & ~app.bl_users)
@lang.language()
async def _del_reply(_, m: types.Message):
    """Remove learned keyword reply so auto-reply stops for that keyword.

    Deletes from this group's rules always (when present). Sudo / log-group
    owner also deletes the global learned rule — watcher merges both scopes,
    so clearing only one left replies still firing (screenshot bug).
    """
    await _ensure_legacy_replies_migrated()
    global_scope = await _auto_reply_global_scope(m)
    if global_scope is None:
        return

    keyword = " ".join(m.command[1:]).strip() if len(m.command) > 1 else ""
    if not keyword:
        await m.reply_text(m.lang["reply_usage"])
        return

    can_edit_global = bool(global_scope) or (
        m.from_user and m.from_user.id in app._sudo_ids
    )
    removed_from: list[str] = []

    # 1) This chat's group-local learned messages
    group_rules = await db.get_auto_reply_rules_for_scope(
        m.chat.id, global_scope=False
    )
    group_key = _resolve_stored_key(group_rules, keyword)
    if group_key is not None:
        await db.del_auto_reply_rule(m.chat.id, group_key, global_scope=False)
        removed_from.append(
            m.lang.get("auto_reply_scope_group", "this group")
        )

    # 2) Global learned messages (sudo / log-group owner)
    if can_edit_global:
        global_rules = await db.get_auto_reply_rules_global()
        global_key = _resolve_stored_key(global_rules, keyword)
        if global_key is not None:
            await db.del_auto_reply_rule(
                m.chat.id, global_key, global_scope=True
            )
            removed_from.append(
                m.lang.get("auto_reply_scope_global", "all groups")
            )

    if not removed_from:
        # Hint when keyword only exists in the other scope
        other = await db.get_auto_reply_rules_global()
        if not can_edit_global and _resolve_stored_key(other, keyword):
            await m.reply_text(
                m.lang.get(
                    "reply_not_found_global_hint",
                    "No auto-reply for <b>{0}</b> in this group.\n"
                    "It exists as a <b>global</b> rule — remove it from the "
                    "log group as owner/sudo: <code>/unreply {0}</code>",
                ).format(keyword)
            )
            return
        await m.reply_text(m.lang["reply_not_found"].format(keyword))
        return

    scope_txt = " + ".join(removed_from)
    await m.reply_text(
        m.lang.get(
            "reply_removed_scope",
            "Auto-reply for <b>{0}</b> removed ({1}).\n"
            "Learned message deleted — this keyword will no longer auto-reply.",
        ).format(keyword, scope_txt)
    )


@app.on_message(filters.command(["replies"]) & filters.group & ~app.bl_users)
@lang.language()
async def _list_replies(_, m: types.Message):
    """List keywords that fire here: group auto-learn + global (both)."""
    await _ensure_legacy_replies_migrated()
    # Any group admin or log-group sudo may list (same gate as other reply cmds)
    global_scope = await _auto_reply_global_scope(m)
    if global_scope is None:
        return

    group_rules = await db.get_auto_reply_rules_for_scope(
        m.chat.id, global_scope=False
    )
    global_rules = await db.get_auto_reply_rules_global()
    if not isinstance(group_rules, dict):
        group_rules = {}
    if not isinstance(global_rules, dict):
        global_rules = {}

    if not group_rules and not global_rules:
        await m.reply_text(m.lang["reply_empty"])
        return

    enabled = await db.get_auto_reply(m.chat.id)
    status = "ON" if enabled else "OFF"

    def _lines(rules: dict, tag: str) -> list[str]:
        out = []
        for key in sorted(rules.keys(), key=lambda k: str(k).casefold()):
            n = len(_list_variants(rules[key]))
            preview = _preview_text(rules[key])
            if n > 1:
                out.append(f"• <code>{key}</code> [{tag}×{n}] → {preview}")
            else:
                out.append(f"• <code>{key}</code> [{tag}] → {preview}")
        return out

    parts: list[str] = [
        m.lang.get(
            "reply_list_header_v2",
            "<u><b>Auto-Reply Keywords</b></u> — reply: <b>{0}</b> · learn: <b>ALWAYS</b>",
        ).format(status)
    ]

    if group_rules:
        parts.append(
            m.lang.get(
                "reply_list_group_section",
                "\n<b>This group</b> (auto-learn):",
            )
        )
        parts.append("\n".join(_lines(group_rules, "group")))

    if global_rules:
        parts.append(
            m.lang.get(
                "reply_list_global_section",
                "\n<b>All groups</b> (global):",
            )
        )
        parts.append("\n".join(_lines(global_rules, "global")))

    body = "\n".join(parts)
    # Telegram message limit safety
    if len(body) > 4000:
        body = body[:3990] + "\n…"
    await m.reply_text(body)


@app.on_message(
    filters.command(["autoremove"]) & app.sudoers & filters.group,
    group=-1,
)
@lang.language()
async def _set_autoremove(_, m: types.Message):
    keyword = " ".join(m.command[1:]).strip() if len(m.command) > 1 else ""
    if not keyword:
        await m.reply_text(m.lang["autoremove_usage"])
        return

    keywords = _load_autoremove_keywords()
    if keyword.lower() == "list":
        if not keywords:
            await m.reply_text(m.lang["autoremove_empty"])
            return
        await m.reply_text(m.lang["autoremove_list"].format("\n".join(f"  {k}" for k in sorted(keywords))))
        return

    key = keyword.lower().strip()
    if key in keywords:
        await m.reply_text(m.lang["autoremove_exists"].format(keyword))
        return

    keywords.add(key)
    _save_autoremove_keywords(keywords)
    await m.reply_text(m.lang["autoremove_added"].format(keyword))


@app.on_message(
    filters.command(["unautoremove"]) & app.sudoers & filters.group,
    group=-1,
)
@lang.language()
async def _del_autoremove(_, m: types.Message):
    keyword = " ".join(m.command[1:]).strip() if len(m.command) > 1 else ""
    if not keyword:
        await m.reply_text(m.lang["autoremove_usage"])
        return

    keywords = _load_autoremove_keywords()
    key = keyword.lower().strip()
    if key not in keywords:
        await m.reply_text(m.lang["autoremove_not_found"].format(keyword))
        return

    keywords.remove(key)
    _save_autoremove_keywords(keywords)
    await m.reply_text(m.lang["autoremove_removed"].format(keyword))


@app.on_message(filters.group & ~filters.service & ~app.bl_users, group=26)
@lang.language()
async def _auto_remove_watcher(_, m: types.Message):
    if not m.from_user or m.from_user.is_bot:
        return
    if await _is_group_admin(m.chat.id, m.from_user.id):
        return

    text = m.text or m.caption or ""
    if not text:
        return

    text_lower = text.lower()
    for keyword in _load_autoremove_keywords():
        if keyword and keyword in text_lower:
            try:
                await m.delete()
            except (
                errors.Forbidden,
                errors.ChatAdminRequired,
                errors.MessageDeleteForbidden,
                errors.MessageIdInvalid,
            ):
                return
            try:
                template = await db.get_custom_text_for_chat(
                    m.chat.id,
                    "autoremove_deleted_notice",
                    m.lang["autoremove_deleted_notice"],
                )
                await utils.send_formatted(
                    m.chat.id,
                    template,
                    m.from_user.mention,
                    keyword,
                    template_key="autoremove_deleted_notice",
                )
            except Exception:
                pass
            raise StopPropagation


@app.on_message(
    filters.group
    & ~app.bl_users
    & (filters.text | filters.caption)
    & ~filters.service,
    group=29,
)
async def _auto_reply_watcher(_, m: types.Message):
    """
    1) Auto-learn: if message is a reply to a short keyword → store answer
       (everyone: admin + normal users). Always on.
    2) Auto-reply: match keyword → send one best answer (group rules first).
    """
    text = m.text or m.caption or ""
    if not text:
        return

    user = getattr(m, "from_user", None)
    if user is None or getattr(user, "is_bot", False):
        return

    # --- Auto-learn (before command skip so plain replies always teach) ---
    learned = False
    if getattr(m, "reply_to_message", None) and not text.lstrip().startswith("/"):
        try:
            learned = await _try_auto_learn(m)
        except Exception as ex:
            logger.warning("Auto-learn unexpected error: %s", ex)

    # After learning, do not also fire auto-reply on the teaching message
    if learned:
        return

    if text.lstrip().startswith("/"):
        return

    # Conversation replies: do not interrupt with auto-reply spam
    # (plain keyword messages still match; reply-threads are human chat).
    if getattr(m, "reply_to_message", None) is not None:
        return

    hit_global = False
    try:
        if not await db.get_auto_reply(m.chat.id):
            return

        await _ensure_legacy_replies_migrated()
        group_rules = await db.get_auto_reply_rules_for_scope(
            m.chat.id, global_scope=False
        )
        hit = _find_best_reply(group_rules, text, chat_id=m.chat.id)
        if hit is None:
            global_rules = await db.get_auto_reply_rules_global()
            hit = _find_best_reply(global_rules, text, chat_id=m.chat.id)
            hit_global = hit is not None
    except ServerSelectionTimeoutError as ex:
        logger.warning(
            "Auto-reply skipped because MongoDB is temporarily unavailable: %s",
            _short_db_error(ex),
        )
        return
    if hit is None:
        return

    _keyword, response = hit
    if not _auto_reply_allowed(int(m.chat.id), int(user.id), _keyword):
        logger.debug(
            "Auto-reply rate-limited chat_id=%s user=%s key=%s",
            m.chat.id,
            user.id,
            _keyword,
        )
        return
    try:
        await _send_auto_reply(m, response)
        if not hit_global:
            await db.touch_auto_reply_rule(
                m.chat.id,
                _keyword,
                used=True,
            )
    except Exception as ex:
        logger.warning(
            "Auto-reply send failed chat_id=%s keyword=%s: %s",
            m.chat.id,
            _keyword,
            ex,
        )
