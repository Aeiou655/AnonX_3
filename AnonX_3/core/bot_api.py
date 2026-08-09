# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import json
import os
import re

import aiohttp
from pyrogram import errors, types

from AnonX_3 import config, logger


class BotAPI:
    class MessageToEditNotFound(RuntimeError):
        pass

    class MessageToReplyNotFound(RuntimeError):
        """The message to reply to was not found (deleted or inaccessible)."""

    class ChatForbidden(RuntimeError):
        pass

    class NoTextToEdit(RuntimeError):
        pass

    class NetworkError(RuntimeError):
        """Transient Telegram Bot API connectivity failure after retries."""

    class RateLimited(RuntimeError):
        """Telegram deferred a send; callers should not block for a long cooldown."""

        def __init__(self, message: str, retry_after: int) -> None:
            super().__init__(message)
            self.retry_after = retry_after

    class FileTooLarge(RuntimeError):
        """Official Bot API cannot expose this file; use an MTProto user client."""

    # Network exceptions that are expected under flaky host/Telegram connectivity.
    _NETWORK_ERRORS = (
        asyncio.TimeoutError,
        TimeoutError,
        aiohttp.ClientOSError,
        aiohttp.ClientConnectionError,
        aiohttp.ServerTimeoutError,
        aiohttp.ClientConnectorError,
    )

    def __init__(self) -> None:
        self.base_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            # Bound connect/total waits so stuck DNS/TLS cannot hang the bot forever.
            timeout = aiohttp.ClientTimeout(total=35, connect=15, sock_connect=15, sock_read=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _safe_error_text(value) -> str:
        text = str(value or "")
        token = getattr(config, "BOT_TOKEN", None)
        if token:
            text = text.replace(str(token), "[bot-token]")
        return re.sub(
            r"https://api\.telegram\.org/bot[^/\s]+",
            "https://api.telegram.org/bot[bot-token]",
            text,
        )

    @staticmethod
    def _serialize_form_value(value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return json.dumps(value)
        return str(value)

    @staticmethod
    def _sanitize_reply_markup(reply_markup: dict | None):
        if not isinstance(reply_markup, dict):
            return None
        keyboard = reply_markup.get("inline_keyboard")
        if not isinstance(keyboard, list):
            logger.warning("Dropping invalid reply_markup payload: missing inline_keyboard list")
            return None
        cleaned_rows = []
        for row in keyboard:
            if not isinstance(row, list):
                continue
            cleaned_buttons = []
            for button in row:
                if isinstance(button, dict) and "text" in button:
                    cleaned_buttons.append(button)
            if cleaned_buttons:
                cleaned_rows.append(cleaned_buttons)
        if not cleaned_rows:
            logger.warning("Dropping invalid reply_markup payload: no valid buttons")
            return None
        return {"inline_keyboard": cleaned_rows}

    @staticmethod
    def _with_reply_markup(reply_markup: dict | None) -> dict:
        if reply_markup is None:
            return {}
        return {"reply_markup": reply_markup}

    @staticmethod
    def _extract_retry_after(data: dict | None, description: str | None = None) -> int | None:
        if isinstance(data, dict):
            parameters = data.get("parameters")
            if isinstance(parameters, dict):
                retry_after = parameters.get("retry_after")
                if isinstance(retry_after, int) and retry_after > 0:
                    return retry_after
                if isinstance(retry_after, str) and retry_after.isdigit():
                    return int(retry_after)

        text = (description or "").lower()
        match = re.search(r"retry after (\d+)", text)
        if match:
            try:
                value = int(match.group(1))
                if value > 0:
                    return value
            except Exception:
                return None
        return None

    @staticmethod
    def _resolve_media_path(path: str) -> str:
        """Resolve a relative media path against the project root if it doesn't exist as-is."""
        if not isinstance(path, str) or os.path.isabs(path) or os.path.exists(path):
            return path
# bot_api.py -> core -> AnonX (package) -> project_root
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        resolved = os.path.join(project_root, path)
        if os.path.exists(resolved):
            return resolved
        return path

    @staticmethod
    def _is_stale_edit_error(description: str | None) -> bool:
        text = (description or "").lower()
        return (
            "message to edit not found" in text
            or "message_id_invalid" in text
            or "message id invalid" in text
            or "message was deleted" in text
            or "message can't be edited" in text
            or "message cannot be edited" in text
        )

    @staticmethod
    def _is_stale_reply_error(description: str | None) -> bool:
        text = (description or "").lower()
        return "message to be replied not found" in text

    @staticmethod
    def _is_chat_forbidden_error(description: str | None) -> bool:
        text = (description or "").lower()
        return (
            "not enough rights" in text
            or "bot was kicked" in text
            or "chat not found" in text
            or "user is deactivated" in text
            or "group chat was upgraded" in text
            or "have no write access to the chat" in text
            or "need to be admin" in text
            or "chat admin required" in text
            or "chat write forbidden" in text
            or "forbidden" in text
        )

    @staticmethod
    def _is_no_text_to_edit_error(description: str | None) -> bool:
        return "there is no text in the message to edit" in (description or "").lower()

    @staticmethod
    def _is_gateway_error(error_code: int | None, description: str | None) -> bool:
        if error_code in (502, 503, 504):
            return True
        text = (description or "").lower()
        return any(
            phrase in text
            for phrase in ("bad gateway", "service unavailable", "gateway timeout", "server error")
        )

    @staticmethod
    def _utf16_length(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _utf16_boundaries(text: str) -> set[int]:
        boundaries = {0}
        offset = 0
        for char in text:
            offset += len(char.encode("utf-16-le")) // 2
            boundaries.add(offset)
        return boundaries

    @staticmethod
    def _previous_utf16_boundary(boundaries: set[int], offset: int) -> int:
        return max(boundary for boundary in boundaries if boundary <= offset)

    @classmethod
    def _sanitize_entities(cls, text: str | None, entities: list[dict] | None):
        if not entities or text is None:
            return entities
        max_len = cls._utf16_length(text)
        boundaries = cls._utf16_boundaries(text)
        cleaned = []
        dropped = 0
        for entity in entities:
            if not isinstance(entity, dict):
                dropped += 1
                continue
            offset = entity.get("offset")
            length = entity.get("length")
            if not isinstance(offset, int) or not isinstance(length, int):
                dropped += 1
                continue
            if offset < 0 or length <= 0 or offset >= max_len:
                dropped += 1
                continue
            if offset not in boundaries:
                dropped += 1
                continue
            if offset + length > max_len:
                length = max_len - offset
            end = offset + length
            if end not in boundaries:
                end = cls._previous_utf16_boundary(boundaries, end)
                length = end - offset
            if length <= 0:
                dropped += 1
                continue
            cleaned_entity = dict(entity)
            cleaned_entity["length"] = length
            if cleaned_entity.get("custom_emoji_id") is not None:
                custom_emoji_id = cleaned_entity.get("custom_emoji_id")
                if isinstance(custom_emoji_id, int):
                    cleaned_entity["custom_emoji_id"] = str(custom_emoji_id)
                elif isinstance(custom_emoji_id, str) and custom_emoji_id.isdigit():
                    cleaned_entity["custom_emoji_id"] = custom_emoji_id
                else:
                    dropped += 1
                    continue
            cleaned.append(cleaned_entity)
        cleaned.sort(key=lambda item: (item.get("offset", 0), item.get("length", 0)))
        if dropped:
            logger.warning("Dropped %s invalid Bot API entities for text length %s", dropped, max_len)
        return cleaned or None

    async def _request(self, method: str, payload: dict | None = None):
        session = await self._get_session()
        url = f"{self.base_url}/{method}"
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                async with session.post(url, json=payload or {}) as resp:
                    data = await resp.json(content_type=None)
            except self._NETWORK_ERRORS as ex:
                logger.warning(
                    "Bot API %s network error (attempt %s/%s): %s",
                    method,
                    attempt + 1,
                    max_attempts,
                    self._safe_error_text(ex),
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise self.NetworkError(
                    f"Bot API {method} failed after {max_attempts} attempts: {self._safe_error_text(ex)}"
                ) from ex

            if data.get("ok"):
                return data.get("result")

            desc = data.get("description", "")
            error_code = data.get("error_code")

            if self._is_gateway_error(error_code, desc):
                if attempt < max_attempts - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "Bot API %s gateway error %s (attempt %s/%s): %s — retrying in %ss",
                        method,
                        error_code or "unknown",
                        attempt + 1,
                        max_attempts,
                        desc or "Bad Gateway",
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    "Bot API %s gateway error after %s attempts: %s",
                    method,
                    max_attempts,
                    self._safe_error_text(data),
                )
                return None

            if self._is_chat_forbidden_error(desc):
                raise self.ChatForbidden(
                    desc or f"Bot API {method} failed"
                )
            if self._is_stale_edit_error(desc):
                raise self.MessageToEditNotFound(
                    desc or f"Bot API {method} failed"
                )
            if self._is_stale_reply_error(desc):
                raise self.MessageToReplyNotFound(
                    desc or f"Bot API {method} failed"
                )

            retry_after = self._extract_retry_after(data, desc)
            if data.get("error_code") == 429 and retry_after:
                if retry_after <= 60 and attempt < max_attempts - 1:
                    logger.warning(
                        "Bot API %s rate limited; retrying in %ss (%s/%s)",
                        method,
                        retry_after,
                        attempt + 1,
                        max_attempts - 1,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                logger.warning(
                    "Bot API %s rate limited; deferring send for %ss",
                    method,
                    retry_after,
                )
                raise self.RateLimited(
                    desc or f"Bot API {method} rate limited", retry_after
                )

            if "message is not modified" in desc.lower():
                return None
            if self._is_no_text_to_edit_error(desc):
                raise self.NoTextToEdit(desc or f"Bot API {method} failed")
            if "file is too big" in desc.lower():
                # This is an expected capability boundary for getFile, not a
                # broken request. Callers can route to an assistant/userbot.
                raise self.FileTooLarge(desc)

            logger.error("Bot API %s failed: %s", method, self._safe_error_text(data))
            raise RuntimeError(desc or f"Bot API {method} failed")

        raise RuntimeError(f"Bot API {method} failed")

    async def _request_form(
        self,
        method: str,
        payload: dict | None = None,
        files: dict[str, str] | None = None,
    ):
        session = await self._get_session()
        url = f"{self.base_url}/{method}"
        payload = payload or {}
        max_attempts = 3

        for attempt in range(max_attempts):
            form = aiohttp.FormData()
            handles = []
            data = None
            try:
                for key, value in payload.items():
                    if value is None:
                        continue
                    form.add_field(key, self._serialize_form_value(value))
                for field, path in (files or {}).items():
                    handle = open(path, "rb")
                    handles.append(handle)
                    form.add_field(field, handle, filename=os.path.basename(path))
                try:
                    async with session.post(url, data=form) as resp:
                        data = await resp.json(content_type=None)
                except self._NETWORK_ERRORS as ex:
                    logger.warning(
                        "Bot API %s network error (attempt %s/%s): %s",
                        method,
                        attempt + 1,
                        max_attempts,
                        self._safe_error_text(ex),
                    )
                    data = None
            finally:
                for handle in handles:
                    handle.close()

            if data is None:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise self.NetworkError(
                    f"Bot API {method} failed after {max_attempts} attempts (network error)"
                )

            if data and data.get("ok"):
                return data.get("result")

            reason = f"Bot API {method} failed"
            error_code = None
            if isinstance(data, dict):
                reason = data.get("description", reason)
                error_code = data.get("error_code")

            if self._is_gateway_error(error_code, reason):
                if attempt < max_attempts - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "Bot API %s gateway error %s (attempt %s/%s): %s — retrying in %ss",
                        method,
                        error_code or "unknown",
                        attempt + 1,
                        max_attempts,
                        reason or "Bad Gateway",
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    "Bot API %s gateway error after %s attempts: %s",
                    method,
                    max_attempts,
                    self._safe_error_text(data),
                )
                return None

            if self._is_chat_forbidden_error(reason):
                raise self.ChatForbidden(reason)
            if self._is_stale_edit_error(reason):
                raise self.MessageToEditNotFound(reason)
            if self._is_stale_reply_error(reason):
                raise self.MessageToReplyNotFound(reason)

            retry_after = self._extract_retry_after(data, reason)
            if (
                isinstance(data, dict)
                and data.get("error_code") == 429
                and retry_after
                and attempt < max_attempts - 1
            ):
                wait_for = min(max(retry_after, 1), 60)
                logger.warning(
                    "Bot API %s rate limited; retrying in %ss (%s/%s)",
                    method,
                    wait_for,
                    attempt + 1,
                    max_attempts - 1,
                )
                await asyncio.sleep(wait_for)
                continue

            if "message is not modified" in reason.lower():
                return None
            if self._is_no_text_to_edit_error(reason):
                raise self.NoTextToEdit(reason)
            if "file is too big" in reason.lower():
                raise self.FileTooLarge(reason)

            logger.error("Bot API %s failed: %s", method, self._safe_error_text(data))
            raise RuntimeError(reason)

        raise RuntimeError(f"Bot API {method} failed")

    @staticmethod
    def _reply_data(reply_to_message_id: int | None) -> dict:
        if not reply_to_message_id:
            return {}
        return {"reply_to_message_id": reply_to_message_id}

    @staticmethod
    def _text_payload(
        text_key: str,
        text: str,
        entities_key: str,
        entities: list[dict] | None,
    ) -> dict:
        payload = {text_key: text}
        had_entities = entities is not None
        entities = BotAPI._sanitize_entities(text, entities)
        if entities:
            payload[entities_key] = entities
        elif not had_entities and text:
            payload["parse_mode"] = "HTML"
        return payload

    async def _fetch_message(self, chat_id: int, result, message_id: int | None = None):
        from AnonX_3 import app

        def _is_fetch_timeout(ex: Exception) -> bool:
            err = str(ex)
            return "after 10 retries" in err and "messages.GetMessages" in err

        msg_id = message_id
        if isinstance(result, dict):
            result_message_id = result.get("message_id")
            if isinstance(result_message_id, int) and result_message_id > 0:
                msg_id = result_message_id
        if not isinstance(msg_id, int) or msg_id <= 0:
            return result
        try:
            return await app.get_messages(chat_id, msg_id)
        except errors.RPCError as ex:
            err = str(ex)
            if "MESSAGE_IDS_EMPTY" in err or "MESSAGE_ID_INVALID" in err:
                logger.warning(
                    "Skipping get_messages(chat_id=%s, message_id=%s) after Bot API response: %s",
                    chat_id,
                    msg_id,
                    err,
                )
                return result
            raise
        except TimeoutError as ex:
            if _is_fetch_timeout(ex):
                logger.warning(
                    "Skipping get_messages(chat_id=%s, message_id=%s) after Bot API response: %s",
                    chat_id,
                    msg_id,
                    self._safe_error_text(ex),
                )
                return result
            raise

    async def send_message(
        self,
        chat_id: int,
        text: str,
        entities: list[dict] | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
        link_preview_options: dict | None = None,
        fetch: bool = True,
    ):
        reply_markup = self._sanitize_reply_markup(reply_markup)
        payload = {
            "chat_id": chat_id,
            **self._reply_data(reply_to_message_id),
            **self._with_reply_markup(reply_markup),
            **self._text_payload("text", text, "entities", entities),
        }
        if link_preview_options is not None:
            payload["link_preview_options"] = link_preview_options
        else:
            payload["disable_web_page_preview"] = disable_web_page_preview
        result = await self._request("sendMessage", payload)
        if not fetch:
            return result
        return await self._fetch_message(chat_id, result)

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str | None = None,
        caption_entities: list[dict] | None = None,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ):
        reply_markup = self._sanitize_reply_markup(reply_markup)
        payload = {
            "chat_id": chat_id,
            **self._reply_data(reply_to_message_id),
            **self._with_reply_markup(reply_markup),
        }
        if caption is not None:
            payload.update(
                self._text_payload("caption", caption, "caption_entities", caption_entities)
            )

        if isinstance(photo, str):
            photo = self._resolve_media_path(photo)
        if isinstance(photo, str) and os.path.exists(photo):
            payload["photo"] = "attach://photo"
            result = await self._request_form(
                "sendPhoto", payload=payload, files={"photo": photo}
            )
        else:
            payload["photo"] = photo
            result = await self._request_form("sendPhoto", payload=payload)
        return await self._fetch_message(chat_id, result)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        reply_markup: dict | None = None,
    ):
        reply_markup = self._sanitize_reply_markup(reply_markup)
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            **self._with_reply_markup(reply_markup),
            **self._text_payload("text", text, "entities", entities),
        }
        result = await self._request("editMessageText", payload)
        return await self._fetch_message(chat_id, result, message_id)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Delete a message via Bot API (for cards edited/sent on Bot API path)."""
        result = await self._request(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
        return bool(result)

    async def edit_message_caption(
        self,
        chat_id: int,
        message_id: int,
        caption: str | None = None,
        caption_entities: list[dict] | None = None,
        reply_markup: dict | None = None,
    ):
        reply_markup = self._sanitize_reply_markup(reply_markup)
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            **self._with_reply_markup(reply_markup),
        }
        if caption is not None:
            payload.update(
                self._text_payload(
                    "caption", caption, "caption_entities", caption_entities
                )
            )
        result = await self._request("editMessageCaption", payload)
        return await self._fetch_message(chat_id, result, message_id)

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict | None = None,
    ):
        reply_markup = self._sanitize_reply_markup(reply_markup)
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            **self._with_reply_markup(reply_markup),
        }
        result = await self._request("editMessageReplyMarkup", payload)
        return await self._fetch_message(chat_id, result, message_id)

    async def edit_message_media(
        self,
        chat_id: int,
        message_id: int,
        media,
        reply_markup: dict | None = None,
        fetch: bool = True,
    ):
        if not isinstance(media, types.InputMediaPhoto):
            raise TypeError("Only InputMediaPhoto is supported for Bot API media edits.")
        reply_markup = self._sanitize_reply_markup(reply_markup)

        media_payload = {"type": "photo"}
        media_source = media.media
        if isinstance(media_source, str):
            media_source = self._resolve_media_path(media_source)
        files = {}
        if isinstance(media_source, str) and os.path.exists(media_source):
            media_payload["media"] = "attach://media"
            files["media"] = media_source
        else:
            media_payload["media"] = media_source

        caption = getattr(media, "caption", None)
        caption_entities = getattr(media, "caption_entities", None)
        if caption is not None:
            media_payload["caption"] = caption
            had_entities = caption_entities is not None
            caption_entities = self._sanitize_entities(caption, caption_entities)
            if caption_entities:
                media_payload["caption_entities"] = caption_entities
            elif not had_entities and caption:
                media_payload["parse_mode"] = "HTML"

        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": media_payload,
            **self._with_reply_markup(reply_markup),
        }
        result = await self._request_form(
            "editMessageMedia",
            payload=payload,
            files=files or None,
        )
        if not fetch:
            return result
        return await self._fetch_message(chat_id, result, message_id)
