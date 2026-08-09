# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import re
import time
import weakref
from html.parser import HTMLParser

from pyrogram import enums, errors, types

from AnonX_3 import app, bot_api, db, config, logger


def _get_buttons():
    """Lazy import — _utilities is loaded during helpers.__init__ before the
    buttons singleton is assigned, so a top-level import would fail."""
    from AnonX_3.helpers import buttons

    return buttons


class Utilities:
    _DOWNLOAD_PROGRESS_HISTORY_LIMIT = 4096

    def __init__(self):
        self._flood_log_ttl = 30
        self._entity_log_ttl = 30
        self._log_throttle_cache: dict[str, float] = {}
        # A status message changes owners exactly once: download progress may
        # mutate it until playback claims it, then only the now-playing UI may
        # write to it.  Locks serialize an already-in-flight Telegram edit with
        # that handoff; the bounded history rejects callbacks that arrive late.
        self._download_progress_locks: weakref.WeakValueDictionary[
            tuple[int, int], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._closed_download_progress: dict[tuple[int, int], None] = {}

    def _allow_throttled_log(self, key: str, ttl: int) -> bool:
        now = time.monotonic()
        expire_at = self._log_throttle_cache.get(key, 0.0)
        if now < expire_at:
            return False
        self._log_throttle_cache[key] = now + ttl
        return True

    async def retry_flood_wait(
        self,
        label: str,
        operation,
        *,
        max_retries: int = 2,
    ):
        attempt = 0
        while True:
            try:
                return await operation()
            except errors.FloodWait as fw:
                if attempt >= max_retries:
                    logger.error(
                        "FloodWait retry budget exhausted for %s after %ss",
                        label,
                        fw.value,
                    )
                    raise
                attempt += 1
                wait_for = max(int(fw.value), 1)
                if self._allow_throttled_log(
                    f"flood:{label}:{wait_for}", self._flood_log_ttl
                ):
                    logger.warning(
                        "FloodWait on %s; retrying in %ss (%s/%s)",
                        label,
                        wait_for,
                        attempt,
                        max_retries,
                    )
                await asyncio.sleep(wait_for)

    @staticmethod
    def _is_offset_covered_by_entity(
        offset: int, length: int, entities: list[dict] | None
    ) -> bool:
        for ent in entities or []:
            if ent.get("type") != "custom_emoji":
                continue
            ent_start = ent.get("offset", 0)
            ent_end = ent_start + ent.get("length", 0)
            if offset < ent_end and offset + length > ent_start:
                return True
        return False

    def _premium_emoji_entities(
        self, text: str, existing_entities: list[dict] | None = None
    ) -> list[dict] | None:
        mapping = getattr(config, "PREMIUM_EMOJI_IDS", None) or {}
        if not mapping:
            return None
        entities = []
        offset = 0
        for char in text:
            char_len = self.utf16_length(char)
            if char in mapping and not self._is_offset_covered_by_entity(
                offset, char_len, existing_entities
            ):
                entities.append({
                    "type": "custom_emoji",
                    "offset": offset,
                    "length": char_len,
                    "custom_emoji_id": str(mapping[char]),
                })
            offset += char_len
        return entities or None

    @staticmethod
    def is_premium_emoji_char(char: str) -> bool:
        if not isinstance(char, str) or len(char) != 1:
            return False
        codepoint = ord(char)
        return (
            0xE000 <= codepoint <= 0xF8FF
            or 0xF0000 <= codepoint <= 0xFFFFD
            or 0x100000 <= codepoint <= 0x10FFFD
        )

    def has_premium_emoji(self, text: str | None) -> bool:
        if not isinstance(text, str) or not text:
            return False
        return any(self.is_premium_emoji_char(char) for char in text)

    def has_custom_emoji_entity(self, entities) -> bool:
        raw = self.raw_entities(entities)
        if not raw:
            return False
        return any(
            isinstance(entity, dict) and entity.get("type") == "custom_emoji"
            for entity in raw
        )

    def maybe_convert_bot_api_markup(self, reply_markup, entities=None):
        has_custom_emoji = self.has_custom_emoji_entity(entities)
        if has_custom_emoji:
            # Custom emojis must be sent through the Bot API path. Keep Bot
            # API dicts as-is and convert Pyrogram markups back to dicts so
            # the Bot API branch is selected downstream.
            if reply_markup is None:
                return None, False
            if self.is_bot_api_markup(reply_markup):
                return reply_markup, False
            bot_api_markup = self.pyrogram_markup_to_dict(reply_markup)
            return bot_api_markup, False

        if not self.is_bot_api_markup(reply_markup):
            return reply_markup, False

        # Without custom emojis, prefer Pyrogram markup to preserve rich
        # button presentation unless Bot API-only fields are present.
        # Keep Bot API markup when buttons use Bot API-only presentation
        # fields such as style/copy_text/icon_custom_emoji_id, otherwise
        # converting to Pyrogram strips them and button colors disappear.
        for row in reply_markup.get("inline_keyboard", []):
            if not isinstance(row, list):
                continue
            for button in row:
                if not isinstance(button, dict):
                    continue
                if any(
                    key in button
                    for key in ("style", "copy_text", "icon_custom_emoji_id")
                ):
                    return reply_markup, False

        try:
            from AnonX_3.helpers import buttons

            pyrogram_markup = buttons.to_pyrogram_markup(reply_markup)
        except Exception:
            pyrogram_markup = None

        if pyrogram_markup is None:
            return reply_markup, False
        return pyrogram_markup, True

    @staticmethod
    def is_entity_text_invalid_error(ex: Exception) -> bool:
        err = str(ex)
        return "ENTITY_TEXT_INVALID" in err or "in a middle of a utf-16 symbol" in err.lower()

    @staticmethod
    def is_no_text_to_edit_error(ex: Exception) -> bool:
        # Prefer the typed Bot API signal. The text fallback also covers
        # Pyrogram and older Bot API wrappers that expose only Telegram's
        # description string.
        no_text_type = getattr(bot_api, "NoTextToEdit", None)
        if isinstance(no_text_type, type) and isinstance(ex, no_text_type):
            return True
        err = str(ex).lower()
        return (
            "there is no text in the message to edit" in err
            or "message has no text to edit" in err
        )

    @staticmethod
    def is_stale_edit_error(ex: Exception) -> bool:
        err = str(ex).lower()
        return (
            "message to edit not found" in err
            or "message_id_invalid" in err
            or "message id invalid" in err
            or "message was deleted" in err
            or "message can't be edited" in err
            or "message cannot be edited" in err
        )

    @staticmethod
    def is_duplicate_edit_error(ex: Exception) -> bool:
        return "MESSAGE_NOT_MODIFIED" in str(ex) or "message is not modified" in str(ex).lower()

    @staticmethod
    def is_edit_transport_timeout(ex: Exception) -> bool:
        err = str(ex)
        return "after 10 retries" in err and (
            "messages.EditMessage" in err or "messages.EditInlineBotMessage" in err
        )

    @staticmethod
    def is_delivery_transport_timeout(ex: Exception) -> bool:
        err = str(ex)
        return "after 10 retries" in err and any(
            query in err
            for query in (
                "channels.GetMessages",
                "messages.GetMessages",
                "messages.SendMessage",
                "messages.SendMedia",
                "messages.SendMultiMedia",
            )
        )

    @staticmethod
    def is_bot_api_network_timeout(ex: Exception) -> bool:
        """Quiet-handle flaky HTTPS to api.telegram.org (connect/read timeouts)."""
        name = type(ex).__name__
        # BotAPI.NetworkError (and identically named wrappers)
        if name == "NetworkError":
            return True

        err = str(ex)
        err_l = err.lower()

        if name in {
            "ConnectionTimeoutError",
            "ServerTimeoutError",
            "ClientConnectorError",
            "ClientOSError",
            "ClientConnectionError",
            "ClientPayloadError",
            "ClientConnectorDNSError",
        }:
            return (
                "telegram.org" in err_l
                or "bot api" in err_l
                or "connection timeout" in err_l
                or "cannot connect" in err_l
            )

        if "connection timeout to host" in err_l:
            return True
        if "api.telegram.org" in err_l and (
            "timeout" in err_l or "connection" in err_l or "cannot connect" in err_l
        ):
            return True
        if "bot api" in err_l and (
            ("failed after" in err_l and ("timeout" in err_l or "network" in err_l or "connection" in err_l))
            or "network error" in err_l
        ):
            return True
        return False

    @staticmethod
    def is_bot_api_rate_limit(ex: Exception) -> bool:
        return type(ex).__name__ == "RateLimited"

    @staticmethod
    def is_chat_forbidden_error(ex: Exception) -> bool:
        """Identify permanent chat-write failures without relying on one client."""
        for name in ("ChatForbidden", "Forbidden", "ChatWriteForbidden"):
            error_type = getattr(bot_api, name, None) or getattr(errors, name, None)
            if isinstance(error_type, type) and isinstance(ex, error_type):
                return True
        text = str(ex).lower()
        return any(
            marker in text
            for marker in (
                "chat_send_plain_forbidden",
                "not enough rights to send text messages",
                "can't send non-media (text)",
                "bot was kicked",
                "chat not found",
                "user is deactivated",
                "group chat was upgraded",
            )
        )

    def is_quiet_send_error(self, ex: Exception) -> bool:
        return (
            self.is_delivery_transport_timeout(ex)
            or self.is_bot_api_network_timeout(ex)
            or self.is_bot_api_rate_limit(ex)
        )

    def is_quiet_edit_error(self, ex: Exception) -> bool:
        return (
            self.is_duplicate_edit_error(ex)
            or self.is_stale_edit_error(ex)
            or self.is_edit_transport_timeout(ex)
            or self.is_bot_api_network_timeout(ex)
        )
    @staticmethod
    def has_caption_or_media(msg: types.Message) -> bool:
        return (
            getattr(msg, "caption", None) is not None
            or getattr(msg, "photo", None) is not None
            or getattr(msg, "video", None) is not None
            or getattr(msg, "animation", None) is not None
            or getattr(msg, "document", None) is not None
            or getattr(msg, "audio", None) is not None
            or getattr(msg, "voice", None) is not None
        )

    def strip_custom_emoji_entities(self, entities) -> tuple[list[dict] | None, bool]:
        raw = self.raw_entities(entities)
        if not raw:
            return None, False

        filtered = [
            dict(entity)
            for entity in raw
            if not (
                isinstance(entity, dict) and entity.get("type") == "custom_emoji"
            )
        ]
        return (filtered or None), len(filtered) != len(raw)

    def entity_text_fallbacks(self, entities) -> list[tuple[list[dict] | None, str]]:
        raw = self.raw_entities(entities)
        if not raw:
            return []

        fallbacks = []
        filtered, changed = self.strip_custom_emoji_entities(raw)
        if changed:
            fallbacks.append((filtered, "without custom emoji entities"))
        fallbacks.append(([], "without entities"))
        return fallbacks

    async def retry_entity_text_invalid(
        self,
        label: str,
        operation,
        entities,
    ):
        try:
            return await operation(entities)
        except Exception as ex:
            if not self.is_entity_text_invalid_error(ex):
                raise
            last_error = ex

        for fallback_entities, fallback_label in self.entity_text_fallbacks(entities):
            if self._allow_throttled_log(
                f"entity_retry:{label}:{fallback_label}", self._entity_log_ttl
            ):
                logger.warning("Retrying %s %s: %s", label, fallback_label, last_error)
            try:
                return await operation(fallback_entities)
            except Exception as ex:
                if not self.is_entity_text_invalid_error(ex):
                    raise
                last_error = ex

        raise last_error

    def format_eta(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}:{seconds % 60:02d} min"
        else:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h}:{m:02d}:{s:02d} h"

    def format_size(self, bytes: int) -> str:
        if bytes >= 1024**3:
            return f"{bytes / 1024 ** 3:.2f} GB"
        elif bytes >= 1024**2:
            return f"{bytes / 1024 ** 2:.2f} MB"
        else:
            return f"{bytes / 1024:.2f} KB"

    def to_seconds(self, time: str) -> int:
        cleaned = (time or "").strip()
        if not cleaned:
            return 0
        try:
            parts = [int(p) for p in cleaned.split(":") if p.strip()]
            if not parts:
                return 0
            return sum(value * 60**i for i, value in enumerate(reversed(parts)))
        except (ValueError, TypeError):
            return 0


    def get_url(self, message_1: types.Message) -> str | None:
        link = None
        messages = [message_1]

        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)

        for message in messages:
            entities = message.entities or message.caption_entities or []

            for entity in entities:
                if entity.type == enums.MessageEntityType.TEXT_LINK:
                    link = entity.url
                    break
                elif entity.type == enums.MessageEntityType.URL:
                    text = message.text or message.caption
                    if not text:
                        continue
                    link = text[entity.offset: entity.offset + entity.length]
                    break

        if link:
            return link.split("&si")[0].split("?si")[0]
        return None


    async def extract_user(self, msg: types.Message) -> types.User | None:
        if msg.reply_to_message:
            return msg.reply_to_message.from_user

        if msg.entities:
            for e in msg.entities:
                if e.type == enums.MessageEntityType.TEXT_MENTION:
                    return e.user

        if msg.text:
            try:
                if m := re.search(r"@(\w{5,32})", msg.text):
                    return await app.get_users(m.group(0))
                if m := re.search(r"\b\d{6,15}\b", msg.text):
                    return await app.get_users(int(m.group(0)))
            except Exception:
                pass

        return None


    async def play_log(
        self,
        m: types.Message,
        link: str,
        title: str,
        duration: str,
    ) -> None:
        if m.chat.id == app.logger:
            return
        _text = m.lang["play_log"].format(
            app.name,
            m.chat.id,
            m.chat.title,
            m.from_user.id,
            m.from_user.mention,
            link,
            title,
            duration,
        )
        await app.send_message(chat_id=app.logger, text=_text)

    async def send_log(self, m: types.Message, chat: bool = False) -> None:
        if chat:
            user = m.from_user
            return await app.send_message(
                chat_id=app.logger,
                text=m.lang["log_chat"].format(
                    m.chat.id,
                    m.chat.title,
                    user.id if user else 0,
                    user.mention if user else "Anonymous",
                ),
            )

        await app.send_message(
            chat_id=app.logger,
            text=m.lang["log_user"].format(
                m.from_user.id,
                f"@{m.from_user.username}",
                m.from_user.mention,
            ),
        )

    @staticmethod
    def parse_html_entities(text: str) -> str | dict:
        class EntityHTMLParser(HTMLParser):
            TAG_TO_ENTITY = {
                "b": "bold",
                "strong": "bold",
                "i": "italic",
                "em": "italic",
                "u": "underline",
                "ins": "underline",
                "s": "strike",
                "strike": "strike",
                "del": "strike",
                "code": "code",
                "pre": "pre",
                "blockquote": "blockquote",
                "spoiler": "spoiler",
            }

            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.parts = []
                self.entities = []
                self.stack = []

            def _text(self) -> str:
                return "".join(self.parts)

            def _utf16_length(self) -> int:
                return len(self._text().encode("utf-16-le")) // 2

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                attrs_dict = dict(attrs)
                if tag == "a" and attrs_dict.get("href"):
                    self.stack.append({
                        "type": "text_link",
                        "offset": self._utf16_length(),
                        "url": attrs_dict["href"],
                    })
                    return
                if tag == "emoji" and attrs_dict.get("id"):
                    self.stack.append({
                        "type": "custom_emoji",
                        "offset": self._utf16_length(),
                        "custom_emoji_id": attrs_dict["id"],
                    })
                    return
                entity_type = self.TAG_TO_ENTITY.get(tag)
                if not entity_type:
                    return
                item = {
                    "type": entity_type,
                    "offset": self._utf16_length(),
                }
                if entity_type == "pre" and attrs_dict.get("language"):
                    item["language"] = attrs_dict["language"]
                self.stack.append(item)

            def handle_endtag(self, tag):
                tag = tag.lower()
                expected_types = []
                if tag == "a":
                    expected_types = ["text_link"]
                elif tag == "emoji":
                    expected_types = ["custom_emoji"]
                else:
                    entity_type = self.TAG_TO_ENTITY.get(tag)
                    if entity_type:
                        expected_types = [entity_type]
                if not expected_types:
                    return
                current_offset = self._utf16_length()
                for idx in range(len(self.stack) - 1, -1, -1):
                    if self.stack[idx]["type"] not in expected_types:
                        continue
                    item = self.stack.pop(idx)
                    length = current_offset - item["offset"]
                    if length <= 0:
                        return
                    entity = dict(item)
                    entity["length"] = length
                    self.entities.append(entity)
                    return

            def handle_data(self, data):
                self.parts.append(data)

        parser = EntityHTMLParser()
        parser.feed(text)
        parser.close()
        parsed_text = "".join(parser.parts)
        if parser.entities:
            return {"text": parsed_text, "entities": parser.entities}
        return parsed_text

    @staticmethod
    def serialize_entities(entities: list[types.MessageEntity]) -> list[dict]:
        if not entities:
            return []
        result = []
        for e in entities:
            entity_type, _ = Utilities.normalize_entity_type(getattr(e, "type", None))
            if not entity_type:
                continue
            d = {
                "type": entity_type,
                "offset": e.offset,
                "length": e.length,
            }
            if getattr(e, "url", None):
                d["url"] = e.url
            if getattr(e, "language", None):
                d["language"] = e.language
            if getattr(e, "custom_emoji_id", None):
                d["custom_emoji_id"] = e.custom_emoji_id
            result.append(d)
        return result

    @staticmethod
    def normalize_entity_type(raw_type) -> tuple[str | None, bool]:
        if isinstance(raw_type, enums.MessageEntityType):
            return raw_type.name.lower(), False

        if not isinstance(raw_type, str) or not raw_type:
            return None, False

        normalized = raw_type
        changed = False

        if raw_type.startswith("<class '") and "message_entity_" in raw_type:
            legacy_name = raw_type.lower().split("message_entity_", 1)[1].split(".", 1)[0]
            member = getattr(enums.MessageEntityType, legacy_name.upper(), None)
            if member is not None:
                return member.name.lower(), True

        if raw_type.startswith("MessageEntityType."):
            normalized = raw_type.split(".", 1)[1]
            changed = True

        normalized_name = normalized.upper()
        member = getattr(enums.MessageEntityType, normalized_name, None)
        if member is not None:
            return member.name.lower(), True

        try:
            member = enums.MessageEntityType[normalized_name]
        except KeyError:
            return None, changed

        return member.name.lower(), changed or normalized != raw_type

    @classmethod
    def deserialize_entities(
        cls, entity_dicts: list[dict]
    ) -> tuple[list[types.MessageEntity], bool, list[dict]]:
        if not entity_dicts:
            return [], False, []

        result = []
        normalized_entities = []
        changed = False
        for d in entity_dicts:
            if not isinstance(d, dict):
                changed = True
                continue

            entity_type, normalized = cls.normalize_entity_type(d.get("type"))
            if not entity_type:
                changed = True
                continue

            try:
                etype = enums.MessageEntityType[entity_type.upper()]
                offset = d["offset"]
                length = d["length"]
            except (KeyError, TypeError):
                changed = True
                continue

            cleaned = {
                "type": entity_type,
                "offset": offset,
                "length": length,
            }
            if d.get("url") is not None:
                cleaned["url"] = d["url"]
            if d.get("language") is not None:
                cleaned["language"] = d["language"]
            if d.get("custom_emoji_id") is not None:
                custom_emoji_id = d["custom_emoji_id"]
                if isinstance(custom_emoji_id, int):
                    cleaned["custom_emoji_id"] = str(custom_emoji_id)
                elif isinstance(custom_emoji_id, str) and custom_emoji_id.isdigit():
                    cleaned["custom_emoji_id"] = custom_emoji_id
                else:
                    changed = True
                    continue

            # Pyrogram expects custom_emoji_id as a string; keep the validated
            # digit string instead of converting to int so large IDs are safe.
            pyrogram_custom_emoji_id = cleaned.get("custom_emoji_id")

            e = types.MessageEntity(
                type=etype,
                offset=offset,
                length=length,
                url=cleaned.get("url"),
                language=cleaned.get("language"),
                custom_emoji_id=pyrogram_custom_emoji_id,
            )
            result.append(e)
            normalized_entities.append(cleaned)
            changed = changed or normalized or cleaned != d

        return result, changed, normalized_entities

    async def normalize_template_entities(
        self,
        key: str | None,
        template: str | dict,
        lang_code: str | None = None,
    ) -> str | dict:
        if not key or not isinstance(template, dict):
            return template

        _, changed, normalized_entities = self.deserialize_entities(
            list(template.get("entities", []))
        )
        if not changed:
            return template

        normalized_template = {
            "text": template["text"],
            "entities": normalized_entities,
        }
        await db.set_custom_text(key, normalized_template, lang_code=lang_code)
        return normalized_template

    @staticmethod
    def is_bot_api_markup(reply_markup) -> bool:
        return isinstance(reply_markup, dict) and "inline_keyboard" in reply_markup

    @staticmethod
    def pyrogram_markup_to_dict(reply_markup) -> dict | None:
        """Convert a Pyrogram InlineKeyboardMarkup to a Bot API dict.

        This is the reverse of buttons.to_pyrogram_markup. Used when custom
        emoji entities force the Bot API send path but the caller supplied a
        Pyrogram-style markup object.
        """
        if not isinstance(reply_markup, types.InlineKeyboardMarkup):
            return None
        rows = []
        for row in reply_markup.inline_keyboard:
            buttons = []
            for btn in row:
                if not isinstance(btn, types.InlineKeyboardButton):
                    continue
                data = {"text": getattr(btn, "text", "") or ""}
                if btn.callback_data is not None:
                    data["callback_data"] = btn.callback_data
                elif btn.url is not None:
                    data["url"] = btn.url
                elif getattr(btn, "callback_game", None) is not None:
                    data["callback_game"] = btn.callback_game
                elif getattr(btn, "pay", None):
                    data["pay"] = True
                elif btn.web_app is not None:
                    data["web_app"] = {"url": btn.web_app.url}
                elif btn.login_url is not None:
                    data["login_url"] = {
                        "url": btn.login_url.url,
                        "forward_text": btn.login_url.forward_text,
                        "bot_username": btn.login_url.bot_username,
                        "request_write_access": btn.login_url.request_write_access,
                    }
                elif getattr(btn, "user_id", None) is not None:
                    data["user_id"] = btn.user_id
                if data.keys() != {"text"}:
                    buttons.append(data)
            if buttons:
                rows.append(buttons)
        if not rows:
            return None
        return {"inline_keyboard": rows}

    @staticmethod
    def sanitize_reply_markup(reply_markup):
        if not isinstance(reply_markup, dict):
            return None
        keyboard = reply_markup.get("inline_keyboard")
        if not isinstance(keyboard, list):
            return None
        cleaned_rows = []
        for row in keyboard:
            if not isinstance(row, list):
                continue
            cleaned_buttons = []
            for button in row:
                if not isinstance(button, dict):
                    continue
                if "text" not in button:
                    continue
                cleaned_buttons.append(button)
            if cleaned_buttons:
                cleaned_rows.append(cleaned_buttons)
        if not cleaned_rows:
            return None
        return {"inline_keyboard": cleaned_rows}

    def sanitize_entities_for_text(self, text: str | None, entities) -> list[dict] | None:
        raw = self.raw_entities(entities)
        if not raw or text is None:
            return raw

        max_len = self.utf16_length(text)
        boundaries = self.utf16_boundaries(text)
        cleaned = []
        dropped = 0
        dropped_types: set[str] = set()
        for entity in raw:
            offset = entity.get("offset")
            length = entity.get("length")
            entity_type = entity.get("type", "unknown")
            if not isinstance(offset, int) or not isinstance(length, int):
                dropped += 1
                dropped_types.add(entity_type)
                continue
            if offset < 0 or length <= 0 or offset >= max_len:
                dropped += 1
                dropped_types.add(entity_type)
                continue
            if offset not in boundaries:
                dropped += 1
                dropped_types.add(entity_type)
                continue
            if offset + length > max_len:
                length = max_len - offset
            end = offset + length
            if end not in boundaries:
                end = self.previous_utf16_boundary(boundaries, end)
                length = end - offset
            if length <= 0:
                dropped += 1
                dropped_types.add(entity_type)
                continue
            cleaned_entity = dict(entity)
            cleaned_entity["length"] = length
            cleaned.append(cleaned_entity)
        if dropped:
            throttle_key = f"invalid_entities:{dropped}:{max_len}"
            if self._allow_throttled_log(throttle_key, self._entity_log_ttl):
                logger.warning(
                    "Dropped %s invalid message entities (types: %s) for text length %s",
                    dropped,
                    ", ".join(sorted(dropped_types)) or "unknown",
                    max_len,
                )
        return cleaned or None

    def raw_entities(self, entities) -> list[dict] | None:
        if not entities:
            return None

        if isinstance(entities, list) and all(isinstance(e, dict) for e in entities):
            _, _, normalized = self.deserialize_entities(entities)
            return normalized or None

        normalized = self.serialize_entities(entities)
        return normalized or None

    def pyrogram_entities(self, entities) -> list[types.MessageEntity] | None:
        raw = self.raw_entities(entities)
        if not raw:
            return None
        parsed, _, _ = self.deserialize_entities(raw)
        # Bot API JSON accepts custom emoji document IDs as digit strings, but
        # Pyrogram's MTProto writer serializes them as signed 64-bit integers.
        # Keeping strings in persisted templates is safe; convert only at the
        # Pyrogram transport boundary.
        for entity in parsed:
            if (
                entity.type == enums.MessageEntityType.CUSTOM_EMOJI
                and isinstance(entity.custom_emoji_id, str)
                and entity.custom_emoji_id.isdigit()
            ):
                entity.custom_emoji_id = int(entity.custom_emoji_id)
        return parsed or None

    async def reply_text(
        self,
        msg: types.Message,
        text: str,
        entities=None,
        reply_markup=None,
        quote: bool = True,
        **kwargs,
    ) -> types.Message:
        disable_web_page_preview = kwargs.pop("disable_web_page_preview", True)
        link_preview_options = kwargs.pop("link_preview_options", None)
        entities = self.sanitize_entities_for_text(text, entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, entities
        )
        if link_preview_options is not None and force_pyrogram:
            bot_api_markup = self.pyrogram_markup_to_dict(reply_markup)
            if bot_api_markup is not None:
                reply_markup = bot_api_markup
                force_pyrogram = False
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (
            self.is_bot_api_markup(reply_markup)
            or force_bot_api
            or link_preview_options is not None
        ) and not force_pyrogram:
            async def _bot_api_reply(fallback_entities):
                try:
                    return await bot_api.send_message(
                        chat_id=msg.chat.id,
                        text=text,
                        entities=fallback_entities,
                        reply_markup=reply_markup,
                        reply_to_message_id=msg.id if quote else None,
                        disable_web_page_preview=disable_web_page_preview,
                        link_preview_options=link_preview_options,
                    )
                except bot_api.MessageToEditNotFound:
                    return await bot_api.send_message(
                        chat_id=msg.chat.id,
                        text=text,
                        entities=fallback_entities,
                        reply_markup=reply_markup,
                        reply_to_message_id=None,
                        disable_web_page_preview=disable_web_page_preview,
                        link_preview_options=link_preview_options,
                    )
                except bot_api.MessageToReplyNotFound:
                    return None
            return await self.retry_entity_text_invalid(
                "text reply",
                _bot_api_reply,
                entities,
            )

        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        kwargs["quote"] = quote
        kwargs["disable_web_page_preview"] = disable_web_page_preview

        async def _reply(fallback_entities):
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                pyrogram_entities = self.pyrogram_entities(fallback_entities)
                if pyrogram_entities:
                    retry_kwargs["entities"] = pyrogram_entities
                else:
                    retry_kwargs.pop("entities", None)
                    retry_kwargs["parse_mode"] = enums.ParseMode.DISABLED
            else:
                retry_kwargs.pop("entities", None)
                retry_kwargs["parse_mode"] = enums.ParseMode.DISABLED
            return await msg.reply_text(text, **retry_kwargs)

        return await self.retry_flood_wait(
            "text reply",
            lambda: self.retry_entity_text_invalid("text reply", _reply, entities),
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        entities=None,
        reply_markup=None,
        **kwargs,
    ) -> types.Message:
        disable_web_page_preview = kwargs.pop("disable_web_page_preview", True)
        link_preview_options = kwargs.pop("link_preview_options", None)
        entities = self.sanitize_entities_for_text(text, entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, entities
        )
        if link_preview_options is not None and force_pyrogram:
            bot_api_markup = self.pyrogram_markup_to_dict(reply_markup)
            if bot_api_markup is not None:
                reply_markup = bot_api_markup
                force_pyrogram = False
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (
            self.is_bot_api_markup(reply_markup)
            or force_bot_api
            or link_preview_options is not None
        ) and not force_pyrogram:
            return await self.retry_entity_text_invalid(
                "message send",
                lambda fallback_entities: bot_api.send_message(
                    chat_id=chat_id,
                    text=text,
                    entities=fallback_entities,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    link_preview_options=link_preview_options,
                ),
                entities,
            )

        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        kwargs["disable_web_page_preview"] = disable_web_page_preview

        async def _send(fallback_entities):
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                pyrogram_entities = self.pyrogram_entities(fallback_entities)
                if pyrogram_entities:
                    retry_kwargs["entities"] = pyrogram_entities
                else:
                    retry_kwargs.pop("entities", None)
                    retry_kwargs["parse_mode"] = enums.ParseMode.DISABLED
            else:
                retry_kwargs.pop("entities", None)
                retry_kwargs["parse_mode"] = enums.ParseMode.DISABLED
            return await app.send_message(chat_id=chat_id, text=text, **retry_kwargs)

        return await self.retry_flood_wait(
            "message send",
            lambda: self.retry_entity_text_invalid("message send", _send, entities),
        )

    async def reply_photo(
        self,
        msg: types.Message,
        photo: str,
        caption: str | None = None,
        caption_entities=None,
        reply_markup=None,
        quote: bool = True,
        **kwargs,
    ) -> types.Message:
        caption_entities = self.sanitize_entities_for_text(caption, caption_entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, caption_entities
        )
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(caption_entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (self.is_bot_api_markup(reply_markup) or force_bot_api) and not force_pyrogram:
            try:
                return await bot_api.send_photo(
                    chat_id=msg.chat.id,
                    photo=photo,
                    caption=caption,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup,
                    reply_to_message_id=msg.id if quote else None,
                )
            except Exception as ex:
                fallback_entities, changed = self.strip_custom_emoji_entities(
                    caption_entities
                )
                if not changed or not self.is_entity_text_invalid_error(ex):
                    raise
                logger.warning(
                    "Retrying photo reply without custom emoji entities: %s", ex
                )
                return await bot_api.send_photo(
                    chat_id=msg.chat.id,
                    photo=photo,
                    caption=caption,
                    caption_entities=fallback_entities,
                    reply_markup=reply_markup,
                    reply_to_message_id=msg.id if quote else None,
                )

        if caption_entities is not None:
            kwargs["caption_entities"] = self.pyrogram_entities(caption_entities)
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        kwargs["quote"] = quote
        try:
            return await self.retry_flood_wait(
                "photo reply",
                lambda: msg.reply_photo(photo=photo, caption=caption, **kwargs),
            )
        except Exception as ex:
            fallback_entities, changed = self.strip_custom_emoji_entities(
                caption_entities
            )
            if not changed or not self.is_entity_text_invalid_error(ex):
                raise
            logger.warning(
                "Retrying photo reply without custom emoji entities: %s", ex
            )
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                retry_kwargs["caption_entities"] = self.pyrogram_entities(
                    fallback_entities
                )
            else:
                retry_kwargs.pop("caption_entities", None)
            return await self.retry_flood_wait(
                "photo reply",
                lambda: msg.reply_photo(photo=photo, caption=caption, **retry_kwargs),
            )

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str | None = None,
        caption_entities=None,
        reply_markup=None,
        **kwargs,
    ) -> types.Message:
        caption_entities = self.sanitize_entities_for_text(caption, caption_entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, caption_entities
        )
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(caption_entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (self.is_bot_api_markup(reply_markup) or force_bot_api) and not force_pyrogram:
            try:
                return await bot_api.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    caption_entities=caption_entities,
                    reply_markup=reply_markup,
                )
            except Exception as ex:
                fallback_entities, changed = self.strip_custom_emoji_entities(
                    caption_entities
                )
                if not changed or not self.is_entity_text_invalid_error(ex):
                    raise
                logger.warning(
                    "Retrying photo send without custom emoji entities: %s", ex
                )
                return await bot_api.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    caption_entities=fallback_entities,
                    reply_markup=reply_markup,
                )

        if caption_entities is not None:
            kwargs["caption_entities"] = self.pyrogram_entities(caption_entities)
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        try:
            return await self.retry_flood_wait(
                "photo send",
                lambda: app.send_photo(
                    chat_id=chat_id, photo=photo, caption=caption, **kwargs
                ),
            )
        except Exception as ex:
            fallback_entities, changed = self.strip_custom_emoji_entities(
                caption_entities
            )
            if not changed or not self.is_entity_text_invalid_error(ex):
                raise
            logger.warning(
                "Retrying photo send without custom emoji entities: %s", ex
            )
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                retry_kwargs["caption_entities"] = self.pyrogram_entities(
                    fallback_entities
                )
            else:
                retry_kwargs.pop("caption_entities", None)
            return await self.retry_flood_wait(
                "photo send",
                lambda: app.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    **retry_kwargs,
                ),
            )

    async def edit_text(
        self,
        msg: types.Message,
        text: str,
        entities=None,
        reply_markup=None,
        ignore_stale: bool = False,
        **kwargs,
    ) -> types.Message:
        entities = self.sanitize_entities_for_text(text, entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, entities
        )
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (self.is_bot_api_markup(reply_markup) or force_bot_api) and not force_pyrogram:
            try:
                return await self.retry_entity_text_invalid(
                    "text edit",
                    lambda fallback_entities: bot_api.edit_message_text(
                        chat_id=msg.chat.id,
                        message_id=msg.id,
                        text=text,
                        entities=fallback_entities,
                        reply_markup=reply_markup,
                    ),
                    entities,
                )
            except bot_api.MessageToEditNotFound:
                if ignore_stale:
                    return None
                raise

        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup

        async def _edit(fallback_entities):
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                pyrogram_entities = self.pyrogram_entities(fallback_entities)
                if pyrogram_entities:
                    retry_kwargs["entities"] = pyrogram_entities
                else:
                    retry_kwargs.pop("entities", None)
                    retry_kwargs.setdefault("parse_mode", enums.ParseMode.HTML)
            else:
                retry_kwargs.pop("entities", None)
                retry_kwargs.setdefault("parse_mode", enums.ParseMode.HTML)
            return await msg.edit_text(text, **retry_kwargs)

        try:
            return await self.retry_flood_wait(
                "text edit",
                lambda: self.retry_entity_text_invalid("text edit", _edit, entities),
            )
        except Exception as ex:
            if ignore_stale and self.is_stale_edit_error(ex):
                return None
            raise

    async def edit_caption(
        self,
        msg: types.Message,
        caption: str | None = None,
        caption_entities=None,
        reply_markup=None,
        ignore_stale: bool = False,
        **kwargs,
    ) -> types.Message:
        caption_entities = self.sanitize_entities_for_text(caption, caption_entities)
        reply_markup, force_pyrogram = self.maybe_convert_bot_api_markup(
            reply_markup, caption_entities
        )
        force_bot_api = (
            getattr(config, "CUSTOM_EMOJI_FORCE_BOT_API", False)
            and self.has_custom_emoji_entity(caption_entities)
        )
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if (self.is_bot_api_markup(reply_markup) or force_bot_api) and not force_pyrogram:
            try:
                return await self.retry_entity_text_invalid(
                    "caption edit",
                    lambda fallback_entities: bot_api.edit_message_caption(
                        chat_id=msg.chat.id,
                        message_id=msg.id,
                        caption=caption,
                        caption_entities=fallback_entities,
                        reply_markup=reply_markup,
                    ),
                    caption_entities,
                )
            except bot_api.MessageToEditNotFound:
                if ignore_stale:
                    return None
                raise

        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup

        async def _edit_caption(fallback_entities):
            retry_kwargs = dict(kwargs)
            if fallback_entities is not None:
                pyrogram_entities = self.pyrogram_entities(fallback_entities)
                if pyrogram_entities:
                    retry_kwargs["caption_entities"] = pyrogram_entities
                else:
                    retry_kwargs.pop("caption_entities", None)
            else:
                retry_kwargs.pop("caption_entities", None)
            return await msg.edit_caption(caption, **retry_kwargs)

        try:
            return await self.retry_flood_wait(
                "caption edit",
                lambda: self.retry_entity_text_invalid(
                    "caption edit", _edit_caption, caption_entities
                ),
            )
        except Exception as ex:
            if ignore_stale and self.is_stale_edit_error(ex):
                return None
            raise

    async def edit_callback_text(
        self,
        msg: types.Message,
        text: str,
        entities=None,
        reply_markup=None,
        ignore_stale: bool = False,
        **kwargs,
    ):
        try:
            return await self.edit_text(
                msg,
                text,
                entities=entities,
                reply_markup=reply_markup,
                **kwargs,
            )
        except bot_api.MessageToEditNotFound:
            if ignore_stale:
                return None
            raise
        except Exception as ex:
            if self.is_stale_edit_error(ex):
                if ignore_stale:
                    return None
                raise
            if not self.is_no_text_to_edit_error(ex) or not self.has_caption_or_media(msg):
                raise
            logger.warning("Retrying callback edit as caption: %s", ex)

        try:
            return await self.edit_caption(
                msg,
                caption=text,
                caption_entities=entities,
                reply_markup=reply_markup,
                **kwargs,
            )
        except bot_api.MessageToEditNotFound:
            if ignore_stale:
                return None
            raise
        except Exception as ex:
            if self.is_stale_edit_error(ex) and ignore_stale:
                return None
            raise

    async def edit_media(
        self,
        msg: types.Message,
        media,
        reply_markup=None,
        **kwargs,
    ) -> types.Message:
        reply_markup = self.sanitize_reply_markup(reply_markup) if self.is_bot_api_markup(reply_markup) else reply_markup
        if self.is_bot_api_markup(reply_markup):
            if getattr(media, "caption_entities", None):
                media.caption_entities = self.sanitize_entities_for_text(
                    getattr(media, "caption", None),
                    media.caption_entities,
                )
            return await bot_api.edit_message_media(
                chat_id=msg.chat.id,
                message_id=msg.id,
                media=media,
                reply_markup=reply_markup,
            )

        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await msg.edit_media(media=media, **kwargs)

    async def edit_reply_markup(
        self,
        message: types.Message | None = None,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup=None,
        ignore_stale: bool = False,
    ):
        if message is not None:
            chat_id = message.chat.id
            message_id = message.id
        if chat_id is None or message_id is None:
            raise ValueError("chat_id and message_id are required.")

        if self.is_bot_api_markup(reply_markup) or reply_markup is None:
            try:
                return await bot_api.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                )
            except bot_api.MessageToEditNotFound:
                if ignore_stale:
                    return None
                raise
            except bot_api.ChatForbidden:
                if ignore_stale:
                    return None
                raise

        try:
            return await app.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except (errors.MessageIdInvalid, errors.Forbidden, errors.ChatWriteForbidden):
            if ignore_stale:
                return None
            raise

    @staticmethod
    def utf16_length(text: str) -> int:
        text = Utilities.stringify_template_value(text)
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def utf16_boundaries(text: str) -> set[int]:
        text = Utilities.stringify_template_value(text)
        boundaries = {0}
        offset = 0
        for char in text:
            offset += len(char.encode("utf-16-le")) // 2
            boundaries.add(offset)
        return boundaries

    @staticmethod
    def previous_utf16_boundary(boundaries: set[int], offset: int) -> int:
        return max(boundary for boundary in boundaries if boundary <= offset)

    @classmethod
    def to_utf16_offset(cls, text: str, index: int) -> int:
        text = cls.stringify_template_value(text)
        return cls.utf16_length(text[:index])

    @classmethod
    def entities_to_utf16(cls, text: str, entities: list[dict]) -> list[dict]:
        """Convert entity offsets from Python code-points to UTF-16 code-units."""
        if not entities:
            return entities
        return [
            {
                **ent,
                "offset": cls.to_utf16_offset(text, ent["offset"]),
                "length": cls.utf16_length(text[ent["offset"]:ent["offset"] + ent["length"]]),
            }
            for ent in entities
            if isinstance(ent, dict) and "offset" in ent and "length" in ent
        ]

    @staticmethod
    def merge_html_entities(text: str, entities: list[dict]) -> tuple[str, list[dict]]:
        """Parse raw HTML <a href=...> and <emoji id=...> tags into proper entities."""
        pattern = re.compile(
            r'(?:<a\s+href=["\']?([^"\'>\s]+)["\']?>([^<]*)</a>)'
            r'|(?:<emoji\s+id=["\']?(\d+)["\']?>([^<]*)</emoji>)',
            re.IGNORECASE,
        )
        # Separate pre-existing entities from newly added ones so existing
        # entities can have their offsets corrected after HTML tag removal.
        pre_entities = list(entities)
        new_entities = []
        new_text = ""
        offset = 0
        utf16_offset = 0

        # Build a list of tag positions and their utf16 delta so we can
        # adjust pre-existing entity offsets after processing all matches.
        tag_adjustments: list[tuple[int, int]] = []

        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            segment = text[offset:start]
            new_text += segment
            segment_utf16_len = len(segment.encode("utf-16-le")) // 2
            utf16_offset += segment_utf16_len

            match_utf16_start = utf16_offset
            match_full_len_utf16 = len(match.group(0).encode("utf-16-le")) // 2

            if match.group(1) is not None:
                # <a> tag
                url = match.group(1)
                inner = match.group(2)
                new_entities.append({
                    "type": "text_link",
                    "offset": utf16_offset,
                    "length": len(inner.encode("utf-16-le")) // 2,
                    "url": url,
                })
            else:
                # <emoji> tag
                emoji_id = match.group(3)
                inner = match.group(4)
                new_entities.append({
                    "type": "custom_emoji",
                    "offset": utf16_offset,
                    "length": len(inner.encode("utf-16-le")) // 2,
                    "custom_emoji_id": emoji_id,
                })

            inner_utf16_len = len(inner.encode("utf-16-le")) // 2
            delta = inner_utf16_len - match_full_len_utf16
            tag_adjustments.append((match_utf16_start, delta))

            new_text += inner
            utf16_offset += inner_utf16_len
            offset = end

        new_text += text[offset:]

        # Adjust pre-existing entity offsets for text that was shortened
        # by HTML tag removal.  Entities positioned after a removed tag
        # must be shifted by the cumulative delta of all preceding tags.
        if tag_adjustments:
            adjusted_pre = []
            for ent in pre_entities:
                shift = 0
                ent_start = ent["offset"]
                for tag_start_utf16, tag_delta in tag_adjustments:
                    if ent_start >= tag_start_utf16:
                        shift += tag_delta
                ent = dict(ent)
                ent["offset"] += shift
                adjusted_pre.append(ent)
            return new_text, adjusted_pre + new_entities

        return new_text, pre_entities + new_entities

    @staticmethod
    def auto_link_title(text: str, entities: list[dict], title: str, url: str) -> tuple[str, list[dict]]:
        if not title or not url:
            return text, entities
        # Never create text_link entities for non-http URLs (ytsearch:, etc).
        # Telegram rejects them with ENTITY_TEXT_INVALID / bad port number.
        if not (url.startswith("http://") or url.startswith("https://")):
            return text, entities

        title_idx = text.find(title)
        if title_idx == -1:
            return text, entities

        prefix = text[:title_idx]
        title_start_utf16 = len(prefix.encode("utf-16-le")) // 2
        title_len_utf16 = len(title.encode("utf-16-le")) // 2
        title_end_utf16 = title_start_utf16 + title_len_utf16

        for ent in entities:
            if ent.get("type") == "text_link":
                es = ent["offset"]
                ee = es + ent["length"]
                if es <= title_start_utf16 and ee >= title_end_utf16:
                    return text, entities

        after = text[title_idx + len(title):]
        m = re.match(r'(\s*)' + re.escape(url), after)
        if not m:
            new_entities = list(entities)
            new_entities.append({
                "type": "text_link",
                "offset": title_start_utf16,
                "length": title_len_utf16,
                "url": url,
            })
            return text, new_entities

        ws = m.group(1)
        full_match = m.group(0)
        remove_start = title_idx + len(title)
        remove_end = remove_start + len(full_match)

        if ws:
            new_text = text[:remove_start] + " " + text[remove_end:]
            kept_ws = " "
        else:
            new_text = text[:remove_start] + text[remove_end:]
            kept_ws = ""

        removed_text = text[remove_start:remove_end]
        removed_utf16 = len(removed_text.encode("utf-16-le")) // 2
        kept_utf16 = len(kept_ws.encode("utf-16-le")) // 2
        delta = kept_utf16 - removed_utf16

        remove_start_utf16 = len(text[:remove_start].encode("utf-16-le")) // 2
        remove_end_utf16 = remove_start_utf16 + removed_utf16

        new_entities = []
        for ent in entities:
            es = ent["offset"]
            ee = es + ent["length"]
            ne = dict(ent)
            if ee <= remove_start_utf16:
                pass
            elif es >= remove_end_utf16:
                ne["offset"] += delta
            else:
                overlap_s = max(es, remove_start_utf16)
                overlap_e = min(ee, remove_end_utf16)
                ne["length"] -= (overlap_e - overlap_s)
                if es >= remove_start_utf16:
                    ne["offset"] += delta
            if ne["length"] > 0:
                new_entities.append(ne)

        new_entities.append({
            "type": "text_link",
            "offset": title_start_utf16,
            "length": title_len_utf16,
            "url": url,
        })
        return new_text, new_entities

    @staticmethod
    def stringify_template_value(value) -> str:
        if isinstance(value, str):
            return value
        text = getattr(value, "text", None)
        if isinstance(text, str):
            return text
        caption = getattr(value, "caption", None)
        if isinstance(caption, str):
            return caption
        return "" if value is None else str(value)

    def format_template(self, template: str | dict, *args, **kwargs) -> str | dict:
        if isinstance(template, str):
            template = re.sub(
                r"(\{(?:\d+|[a-zA-Z_][a-zA-Z0-9_]*)(?:![^}:]+)?(?::[^{}]+)?\})\}",
                r"\1",
                template,
            )
            formatted = template.format(*args, **kwargs)
            parsed = self.parse_html_entities(formatted)
            existing_entities = parsed.get("entities") if isinstance(parsed, dict) else None
            premium_entities = self._premium_emoji_entities(formatted, existing_entities)
            if isinstance(parsed, dict):
                if premium_entities:
                    parsed["entities"] = list(parsed["entities"]) + premium_entities
                parsed["text"], parsed["entities"] = self.merge_html_entities(
                    parsed["text"], list(parsed["entities"])
                )
                return parsed
            new_text, new_entities = self.merge_html_entities(
                formatted, premium_entities or []
            )
            if new_entities:
                return {"text": new_text, "entities": new_entities}
            return formatted

        text = self.stringify_template_value(template.get("text", ""))
        entities = [dict(ent) for ent in template.get("entities", [])]

        placeholders = []
        placeholder_key = r"\d+|[a-zA-Z_][a-zA-Z0-9_]*"
        placeholder_pattern = re.compile(
            rf"\{{\{{({placeholder_key})\}}\}}"
            rf"|\{{({placeholder_key})\}}\}}?"
        )
        for match in placeholder_pattern.finditer(text):
            key = match.group(1) or match.group(2)
            if key.isdigit():
                idx = int(key)
                replacement = (
                    self.stringify_template_value(args[idx])
                    if idx < len(args)
                    else f"{{{key}}}"
                )
            else:
                replacement = (
                    self.stringify_template_value(kwargs[key])
                    if key in kwargs
                    else f"{{{key}}}"
                )
            placeholders.append({
                "start": match.start(),
                "end": match.end(),
                "start_utf16": self.to_utf16_offset(text, match.start()),
                "end_utf16": self.to_utf16_offset(text, match.end()),
                "replacement": replacement,
                "replacement_utf16_length": self.utf16_length(replacement),
                "delta_utf16": (
                    self.utf16_length(replacement)
                    - self.utf16_length(match.group(0))
                ),
            })

        new_text = ""
        last_end = 0
        for ph in placeholders:
            new_text += text[last_end:ph["start"]]
            new_text += ph["replacement"]
            last_end = ph["end"]
        new_text += text[last_end:]

        new_entities = []
        for entity in entities:
            entity_end = entity["offset"] + entity["length"]
            offset_shift = 0
            length_shift = 0
            for ph in placeholders:
                if ph["end_utf16"] <= entity["offset"]:
                    offset_shift += ph["delta_utf16"]
                elif ph["start_utf16"] >= entity_end:
                    pass
                else:
                    overlap_start = max(ph["start_utf16"], entity["offset"])
                    overlap_end = min(ph["end_utf16"], entity_end)
                    overlapped = overlap_end - overlap_start
                    length_shift += ph["replacement_utf16_length"] - overlapped
                    if ph["start_utf16"] < entity["offset"]:
                        offset_shift += ph["delta_utf16"]
            new_entity = dict(entity)
            new_entity["offset"] = entity["offset"] + offset_shift
            new_entity["length"] = entity["length"] + length_shift
            new_entities.append(new_entity)

        new_text, new_entities = self.merge_html_entities(new_text, new_entities)
        premium_entities = self._premium_emoji_entities(new_text, new_entities)
        if premium_entities:
            new_entities = new_entities + premium_entities
        return {"text": new_text, "entities": new_entities or None}

    def format_template_text(self, template: str | dict, *args) -> str:
        result = self.format_template(template, *args)
        return result["text"] if isinstance(result, dict) else result

    @staticmethod
    def append_template_text(left: str | dict, right: str | dict) -> dict:
        """Concatenate two template values (str or {text, entities}) preserving entities."""
        if isinstance(left, str):
            left_text, left_entities = left, []
        else:
            left_text = left.get("text", "")
            left_entities = list(left.get("entities") or [])
        if isinstance(right, str):
            right_text, right_entities = right, []
        else:
            right_text = right.get("text", "")
            right_entities = list(right.get("entities") or [])
        if not left_entities and not right_entities:
            return {"text": left_text + right_text, "entities": None}
        shift = Utilities.utf16_length(left_text)
        shifted_right = [dict(ent) for ent in right_entities]
        for ent in shifted_right:
            ent["offset"] += shift
        entities = left_entities + shifted_right
        return {"text": left_text + right_text, "entities": entities or None}

    @staticmethod
    def download_progress_key(message) -> tuple[int, int] | None:
        """Return a stable chat/message identity across stale Pyrogram wrappers."""
        if message is None:
            return None
        message_id = getattr(message, "id", None)
        if message_id is None:
            return None
        chat_id = getattr(getattr(message, "chat", None), "id", 0)
        try:
            return int(chat_id or 0), int(message_id)
        except (TypeError, ValueError):
            return None

    def is_download_progress_closed(self, message) -> bool:
        key = self.download_progress_key(message)
        if key is None:
            return True
        return key in self._closed_download_progress

    def _remember_closed_download_progress(self, key: tuple[int, int]) -> None:
        # Reinsert so frequently observed late callbacks remain at the newest
        # end of the bounded insertion-ordered history.
        self._closed_download_progress.pop(key, None)
        self._closed_download_progress[key] = None
        while (
            len(self._closed_download_progress)
            > self._DOWNLOAD_PROGRESS_HISTORY_LIMIT
        ):
            oldest = next(iter(self._closed_download_progress))
            self._closed_download_progress.pop(oldest, None)

    async def close_download_progress(self, message, *media_objects) -> bool:
        """Atomically hand a status card from progress UI to playback UI.

        The close marker is published before the first await.  If a Telegram
        progress edit is already in flight, the shared per-message lock acts as
        a barrier so the caller's subsequent play_media edit is guaranteed to
        be the final writer.  The media download itself is never cancelled.
        """
        key = self.download_progress_key(message)
        if key is None:
            return False
        self._remember_closed_download_progress(key)
        for media in media_objects:
            if media is None:
                continue
            try:
                setattr(media, "download_progress_closed_key", key)
            except Exception:
                pass
        lock = self._download_progress_locks.get(key)
        if lock is not None:
            async with lock:
                pass
            if self._download_progress_locks.get(key) is lock:
                self._download_progress_locks.pop(key, None)
        return True

    async def edit_download_progress(
        self,
        message,
        rendered: str | dict,
        *,
        reply_markup=None,
        media=None,
        ignore_stale: bool = True,
    ):
        """Edit a progress card only while the download UI still owns it."""
        key = self.download_progress_key(message)
        if key is None:
            return None
        lock = self._download_progress_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.is_download_progress_closed(message):
                return None
            result = await self.edit_formatted(
                message,
                rendered,
                reply_markup=reply_markup,
                ignore_stale=ignore_stale,
            )
            if result is not None and media is not None:
                setattr(media, "download_progress_started", True)
            return result

    async def edit_download_progress_markup(
        self,
        message,
        *,
        reply_markup,
        ignore_stale: bool = True,
    ):
        """Apply Cancel markup only while the message is a progress card."""
        key = self.download_progress_key(message)
        if key is None:
            return None
        lock = self._download_progress_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.is_download_progress_closed(message):
                return None
            return await self.edit_reply_markup(
                message,
                reply_markup=reply_markup,
                ignore_stale=ignore_stale,
            )

    def render_download_progress(
        self,
        template: str | dict,
        *,
        current: int,
        total: int,
        speed: float,
        eta_seconds: int,
        width: int = 12,
    ) -> dict:
        """Append only a live bar and percentage without discarding custom entities."""
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        width = min(20, max(8, int(width or 12)))
        percent = min(100.0, (current * 100.0 / total)) if total else 0.0
        filled = min(width, max(0, round(width * percent / 100.0)))
        bar = ("█" * filled) + ("░" * (width - filled))
        base = self.format_template(template)
        details = self.format_template(
            "\n\n<code>{0}</code> <b>{1:.1f}%</b>",
            bar,
            percent,
        )
        return self.append_template_text(base, details)

    def render_initial_download_progress(
        self,
        template: str | dict,
        *,
        width: int = 12,
    ) -> dict:
        """Render the custom header with an immediately visible 0% live bar."""
        return self.render_download_progress(
            template,
            current=0,
            total=0,
            speed=0,
            eta_seconds=0,
            width=width,
        )

    def make_download_progress_hook(
        self,
        media,
        *,
        throttle: float = 2.0,
    ):
        """Bridge a worker-thread yt-dlp hook to one custom Telegram status card."""
        message = getattr(media, "download_progress_message", None)
        if message is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        template = getattr(media, "download_progress_template", "Downloading...")
        lang_map = getattr(media, "download_progress_lang", None) or {}
        cancel_label = getattr(
            media,
            "download_progress_cancel_label",
            lang_map.get("cancel", "Cancel"),
        )
        started = time.monotonic()
        last_edit = 0.0
        throttle = max(1.0, float(throttle or 2.0))

        async def _render(current: int, total: int, speed: float, eta: int) -> None:
            from AnonX_3.helpers import buttons

            if self.is_download_progress_closed(message):
                return
            rendered = self.render_download_progress(
                template,
                current=current,
                total=total,
                speed=speed,
                eta_seconds=eta,
            )
            try:
                await self.edit_download_progress(
                    message,
                    rendered,
                    reply_markup=_get_buttons().cancel_dl(cancel_label),
                    media=media,
                )
            except Exception:
                # The UI is best-effort; a deleted/stale card must not stop
                # the underlying provider download.
                pass

        def _hook(data: dict) -> None:
            nonlocal last_edit
            if self.is_download_progress_closed(message):
                return
            status = str((data or {}).get("status", "")).lower()
            if status not in {"downloading", "finished"}:
                return
            current = max(0, int((data or {}).get("downloaded_bytes") or 0))
            total = max(
                0,
                int(
                    (data or {}).get("total_bytes")
                    or (data or {}).get("total_bytes_estimate")
                    or 0
                ),
            )
            if status == "finished":
                total = max(total, current)
            if total <= 0:
                return
            now = time.monotonic()
            if status == "downloading" and now - last_edit < throttle:
                return
            last_edit = now
            setattr(media, "download_progress_started", True)
            elapsed = max(0.001, now - started)
            speed = float((data or {}).get("speed") or 0.0) or current / elapsed
            raw_eta = (data or {}).get("eta")
            try:
                eta = max(0, int(float(raw_eta)))
            except (TypeError, ValueError):
                eta = int((total - current) / speed) if speed else 0
            asyncio.run_coroutine_threadsafe(
                _render(current, total, speed, eta),
                loop,
            )

        return _hook

    def preview_template_text(self, template: str | dict) -> str | dict:
        if isinstance(template, str):
            parsed = self.parse_html_entities(template)
            if isinstance(parsed, dict):
                parsed["text"], parsed["entities"] = self.merge_html_entities(
                    parsed["text"], list(parsed["entities"])
                )
                return parsed
            new_text, new_entities = self.merge_html_entities(parsed, [])
            if new_entities:
                return {"text": new_text, "entities": new_entities}
            return parsed
        return self.format_template(template)

    async def preview_template(
        self,
        template: str | dict,
        template_key: str | None = None,
        lang_code: str | None = None,
    ) -> str | dict:
        template = await self.normalize_template_entities(
            template_key, template, lang_code=lang_code
        )
        result = self.preview_template_text(template)
        return result

    async def reply_formatted(
        self,
        msg: types.Message,
        template: str | dict,
        *args,
        template_key: str | None = None,
        **kwargs,
    ) -> types.Message:
        lang_code = await db.get_lang(msg.chat.id)
        template = await self.normalize_template_entities(
            template_key, template, lang_code=lang_code
        )
        # Auto-attach green Support button on error/not-found messages.
        # Deferred import — _utilities is loaded during helpers.__init__ before
        # the buttons singleton is assigned, so a top-level import would fail.
        if (
            template_key
            and (template_key.startswith("error_") or template_key.startswith("play_not_found"))
            and "reply_markup" not in kwargs
        ):
            from AnonX_3.helpers import buttons as _btns

            kwargs["reply_markup"] = _btns.support_button()
        result = self.format_template(template, *args)
        if isinstance(result, dict):
            try:
                return await self.reply_text(msg, result["text"], entities=result["entities"], **kwargs)
            except Exception as ex:
                if self.is_chat_forbidden_error(ex):
                    raise
                if self.is_quiet_send_error(ex):
                    logger.warning(
                        "Quiet reply_formatted entities skip (transient): %s",
                        str(ex)[:220],
                    )
                    return None
                logger.exception("Failed to send formatted reply with entities: %s", ex)
                try:
                    return await self.reply_text(msg, result["text"], **kwargs)
                except Exception as fallback_ex:
                    if self.is_quiet_send_error(fallback_ex):
                        logger.warning(
                            "Quiet reply_formatted fallback skip (transient): %s",
                            str(fallback_ex)[:220],
                        )
                        return None
                    raise
        try:
            return await self.reply_text(msg, result, **kwargs)
        except Exception as ex:
            if self.is_quiet_send_error(ex):
                logger.warning(
                    "Quiet reply_formatted skip (transient): %s",
                    str(ex)[:220],
                )
                return None
            raise

    async def edit_formatted(
        self,
        msg: types.Message,
        template: str | dict,
        *args,
        template_key: str | None = None,
        **kwargs,
    ) -> types.Message:
        lang_code = await db.get_lang(msg.chat.id)
        template = await self.normalize_template_entities(
            template_key, template, lang_code=lang_code
        )
        # Auto-attach green Support button on error/not-found messages.
        # Deferred import — _utilities is loaded during helpers.__init__ before
        # the buttons singleton is assigned, so a top-level import would fail.
        if (
            template_key
            and (template_key.startswith("error_") or template_key.startswith("play_not_found"))
            and "reply_markup" not in kwargs
        ):
            from AnonX_3.helpers import buttons as _btns

            kwargs["reply_markup"] = _btns.support_button()
        result = self.format_template(template, *args)
        if isinstance(result, dict):
            if self.has_caption_or_media(msg):
                try:
                    return await self.edit_caption(
                        msg,
                        caption=result["text"],
                        caption_entities=result["entities"],
                        **kwargs,
                    )
                except Exception as ex:
                    if self.is_quiet_edit_error(ex):
                        return None
                    raise
            try:
                return await self.edit_text(msg, result["text"], entities=result["entities"], **kwargs)
            except Exception as ex:
                if self.is_no_text_to_edit_error(ex):
                    # Progress/status cards can be replaced with media while a
                    # background watcher still holds the original Message
                    # object. Telegram then rejects editMessageText, but the
                    # same card remains editable through its caption.
                    try:
                        return await self.edit_caption(
                            msg,
                            caption=result["text"],
                            caption_entities=result["entities"],
                            **kwargs,
                        )
                    except Exception as caption_ex:
                        if self.is_quiet_edit_error(caption_ex):
                            return None
                        raise
                if self.is_quiet_edit_error(ex):
                    return None
                logger.exception("Failed to edit formatted text with entities: %s", ex)
                try:
                    return await self.edit_text(msg, result["text"], **kwargs)
                except Exception as fallback_ex:
                    if self.is_quiet_edit_error(fallback_ex):
                        return None
                    raise
        if self.has_caption_or_media(msg):
            try:
                return await self.edit_caption(msg, caption=result, **kwargs)
            except Exception as ex:
                if self.is_quiet_edit_error(ex):
                    return None
                raise
        try:
            return await self.edit_text(msg, result, **kwargs)
        except Exception as ex:
            if self.is_no_text_to_edit_error(ex):
                try:
                    return await self.edit_caption(msg, caption=result, **kwargs)
                except Exception as caption_ex:
                    if self.is_quiet_edit_error(caption_ex):
                        return None
                    raise
            if self.is_quiet_edit_error(ex):
                return None
            raise

    async def send_formatted(
        self,
        chat_id: int,
        template: str | dict,
        *args,
        template_key: str | None = None,
        **kwargs,
    ) -> types.Message:
        kwargs.pop("_quiet_permission_error", None)
        lang_code = await db.get_lang(chat_id)
        template = await self.normalize_template_entities(
            template_key, template, lang_code=lang_code
        )
        # Auto-attach green Support button on error/not-found messages.
        if (
            template_key
            and (template_key.startswith("error_") or template_key.startswith("play_not_found"))
            and "reply_markup" not in kwargs
        ):
            kwargs["reply_markup"] = _get_buttons().support_button()
        result = self.format_template(template, *args)
        if isinstance(result, dict):
            try:
                return await self.send_message(chat_id, result["text"], entities=result["entities"], **kwargs)
            except Exception as ex:
                if self.is_chat_forbidden_error(ex):
                    raise
                if self.is_quiet_send_error(ex):
                    logger.warning(
                        "Quiet send_formatted entities skip (transient): %s",
                        str(ex)[:220],
                    )
                    return None
                logger.exception("Failed to send formatted message with entities: %s", ex)
                try:
                    return await self.send_message(chat_id, result["text"], **kwargs)
                except Exception as fallback_ex:
                    if self.is_quiet_send_error(fallback_ex):
                        logger.warning(
                            "Quiet send_formatted fallback skip (transient): %s",
                            str(fallback_ex)[:220],
                        )
                        return None
                    raise
        try:
            return await self.send_message(chat_id, result, **kwargs)
        except Exception as ex:
            if self.is_quiet_send_error(ex):
                logger.warning(
                    "Quiet send_formatted skip (transient): %s",
                    str(ex)[:220],
                )
                return None
            raise
