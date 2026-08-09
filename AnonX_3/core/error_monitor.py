# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import hashlib
import html
import logging
import re
import traceback
from time import time

import aiohttp

from AnonX_3 import app, config


_INSTALLED = False
_RUNTIME_GUARDS_INSTALLED = False
_PYROGRAM_BAD_MSG_PATCHED = False


def _patch_pyrogram_bad_msg_notification() -> None:
    global _PYROGRAM_BAD_MSG_PATCHED
    if _PYROGRAM_BAD_MSG_PATCHED:
        return
    try:
        from pyrogram.client import Client
    except Exception:
        return

    original = getattr(Client, "handle_updates", None)
    if original is None:
        return
    if getattr(original, "_AnonX_bad_msg_guard", False):
        _PYROGRAM_BAD_MSG_PATCHED = True
        return

    async def guarded_handle_updates(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except AttributeError as ex:
            if _is_pyrogram_bad_msg_notification_update(ex):
                logging.getLogger("pyrogram.client").debug(
                    "Suppressed Pyrogram BadMsgNotification update without users."
                )
                return None
            raise

    guarded_handle_updates._AnonX_bad_msg_guard = True
    guarded_handle_updates._AnonX_original_handle_updates = original
    Client.handle_updates = guarded_handle_updates
    _PYROGRAM_BAD_MSG_PATCHED = True

_TRANSIENT_TIMEOUT_QUERIES = (
    "updates.GetChannelDifference",
    "channels.GetMessages",
    "messages.GetStickerSet",
    "messages.GetMessages",
    "messages.SendMessage",
    "messages.EditMessage",
    "messages.GetDialogs",
)


def _is_transient_timeout_text(text: str) -> bool:
    return "after 10 retries" in text and any(
        query in text for query in _TRANSIENT_TIMEOUT_QUERIES
    )


def _is_tcp_connection_reset_text(text: str) -> bool:
    return "ConnectionResetError" in text and "Connection lost" in text


def _is_pymongo_server_selection_text(text: str) -> bool:
    return (
        "ServerSelectionTimeoutError" in text
        or "serverselectiontimeouterror" in text.lower()
        or "localhost:27017: timed out" in text
    )


def _is_ntgcalls_future(context: dict) -> bool:
    exc = context.get("exception")
    msg = str(context.get("message", ""))
    return (
        "Future exception was never retrieved" in msg
        and exc is not None
        and type(exc).__name__ == "TelegramServerError"
    )


def _is_pyrogram_closed_sqlite_peer_update(exc: BaseException | None) -> bool:
    if exc is None or type(exc).__name__ != "ProgrammingError":
        return False
    if "cannot operate on a closed database" not in str(exc).lower():
        return False
    frames = traceback.extract_tb(exc.__traceback__)
    frame_text = "\n".join(f"{frame.filename}:{frame.name}" for frame in frames)
    return (
        "pyrogram" in frame_text
        and "handle_updates" in frame_text
        and "fetch_peers" in frame_text
        and "update_peers" in frame_text
    )


def _is_pyrogram_bad_msg_notification_update(exc: BaseException | None) -> bool:
    if exc is None or type(exc).__name__ != "AttributeError":
        return False
    if "BadMsgNotification" not in str(exc) or "users" not in str(exc):
        return False
    frames = traceback.extract_tb(exc.__traceback__)
    frame_text = "\n".join(f"{frame.filename}:{frame.name}:{frame.lineno}" for frame in frames)
    return "pyrogram" in frame_text and "handle_updates" in frame_text


def _is_pyrogram_get_channels_resolve_timeout(exc: BaseException | None) -> bool:
    if exc is None or type(exc).__name__ != "TimeoutError":
        return False
    text = str(exc)
    if (
        'Failed to invoke "channels.GetChannels" after 10 retries' not in text
    ):
        return False
    frames = traceback.extract_tb(exc.__traceback__)
    frame_text = "\n".join(f"{frame.filename}:{frame.name}" for frame in frames)
    return (
        "pyrogram" in frame_text
        and "handle_updates" in frame_text
        and "resolve_peer" in frame_text
    )


def _is_transient_asyncio_context(context: dict) -> bool:
    exc = context.get("exception")
    text = f"{context.get('message', '')} {exc or ''}"
    return (
        _is_ntgcalls_future(context)
        or _is_transient_timeout_text(text)
        or _is_pyrogram_closed_sqlite_peer_update(exc)
        or _is_pyrogram_bad_msg_notification_update(exc)
        or _is_pyrogram_get_channels_resolve_timeout(exc)
    )


def _is_bot_api_network_noise_text(text: str) -> bool:
    lower = (text or "").lower()
    if "connection timeout to host" in lower and "api.telegram.org" in lower:
        return True
    if "failed to send formatted" in lower and (
        "connection timeout" in lower or "bot api" in lower and "timeout" in lower
    ):
        return True
    return False


def _is_bot_api_benign_edit_text(text: str) -> bool:
    """Suppress stale-edit / stale-message errors from any Bot API method."""
    lower = (text or "").lower()
    if "bot api" not in lower:
        return False
    return (
        "message can't be edited" in lower
        or "message cannot be edited" in lower
        or "message to edit not found" in lower
        or "message_id_invalid" in lower
        or "message id invalid" in lower
        or "message was deleted" in lower
        or "message not found" in lower
        or "message to forward not found" in lower
        or "message can't be deleted" in lower
        or "message cant be deleted" in lower
        or "message can't be deleted for everyone" in lower
        or "message to pin not found" in lower
    )


def _is_bot_api_benign_reply_text(text: str) -> bool:
    """Suppress stale-reply errors where the original message was deleted."""
    lower = (text or "").lower()
    return "message to be replied not found" in lower


def _is_bot_api_benign_general(text: str) -> bool:
    """Suppress common benign Bot API / Telegram errors that are not actionable."""
    lower = (text or "").lower()
    return (
        "query is too old" in lower
        or "query id is invalid" in lower
        or "bot can't send messages to bots" in lower
        or "webpage can't be previewed" in lower
        or "message author required" in lower
        or "can't parse entities" in lower
        or "can't parse message entities" in lower
        or "entity_bounds_invalid" in lower
        or "button_data_invalid" in lower
        or "wrong file identifier/http url specified" in lower
        or "failed to get http url content" in lower
    )


class TransientRuntimeNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text = f"{text} {record.exc_info[1]}"
        # Suppress CancelledError noise (normal during shutdown)
        if "CancelledError" in text or "cancelled" in text.lower():
            if record.levelno < logging.WARNING:
                return False
        if record.name.startswith("pyrogram.") and _is_transient_timeout_text(text):
            return False
        if record.name == "pyrogram.dispatcher" and _is_pymongo_server_selection_text(text):
            return False
        if (
            record.name == "pyrogram.connection.transport.tcp.tcp"
            and _is_tcp_connection_reset_text(text)
        ):
            return False
        if record.name in {"asyncio", "asyncio.unhandled"} and (
            "TelegramServerError" in text
            or _is_transient_timeout_text(text)
            or _is_tcp_connection_reset_text(text)
            or ("BadMsgNotification" in text and "users" in text)
        ):
            return False
        # Suppress ERROR-level traceback spam for exhausted Bot API HTTPS timeouts.
        if record.levelno >= logging.ERROR and _is_bot_api_network_noise_text(text):
            return False
        if record.levelno >= logging.ERROR and _is_bot_api_benign_edit_text(text):
            return False
        if record.levelno >= logging.ERROR and _is_bot_api_benign_reply_text(text):
            return False
        if record.levelno >= logging.ERROR and _is_bot_api_benign_general(text):
            return False
        return True

def _short(value: object, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class DeepSeekErrorMonitor(logging.Handler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__(getattr(logging, config.DEEPSEEK_ERROR_MIN_LEVEL, logging.ERROR))
        self.loop = loop
        self._last_seen: dict[str, float] = {}
        self._secret_patterns = self._build_secret_patterns()
        # After DeepSeek network/timeout failures, skip analyze for a while
        # so error reports stay useful without TimeoutError spam.
        self._deepseek_skip_until: float = 0.0
        self._deepseek_skip_sec: float = 300.0

    @staticmethod
    def _build_secret_patterns() -> list[tuple[re.Pattern, str]]:
        values = [
            getattr(config, "BOT_TOKEN", None),
            getattr(config, "MONGO_URL", None),
            getattr(config, "API_HASH", None),
            getattr(config, "DEEPSEEK_API_KEY", None),
        ]
        values.extend(getattr(config, "ASSISTANT_SESSIONS", []) or [])
        patterns: list[tuple[re.Pattern, str]] = []
        for value in values:
            if not value or len(str(value)) < 8:
                continue
            patterns.append((re.compile(re.escape(str(value))), "[redacted]"))
        patterns.append((re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"), "[bot-token]"))
        patterns.append((re.compile(r"mongodb(?:\+srv)?://[^\s<]+"), "[mongo-url]"))
        patterns.append((re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[api-key]"))
        return patterns

    def _sanitize(self, text: str) -> str:
        result = text
        for pattern, repl in self._secret_patterns:
            result = pattern.sub(repl, result)
        return result

    def _format_record(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return self._sanitize(message.strip())

    @staticmethod
    def _fingerprint(record: logging.LogRecord, text: str) -> str:
        payload = f"{record.name}:{record.levelname}:{text[:600]}"
        return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _is_transient_bot_api_network(text: str) -> bool:
        lower = (text or "").lower()
        if "connection timeout to host" in lower and "api.telegram.org" in lower:
            return True
        if "bot api" in lower and "failed after" in lower and (
            "timeout" in lower or "network" in lower or "connection" in lower
        ):
            return True
        if "quiet send_formatted" in lower or "quiet reply_formatted" in lower:
            return True
        return False

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("pyrogram.") or record.name.startswith("aiohttp."):
            return
        try:
            text = self._format_record(record)
            if not text:
                return
            if self._is_transient_bot_api_network(text):
                return
            if _is_bot_api_benign_edit_text(text):
                return
            if _is_bot_api_benign_reply_text(text):
                return
            if _is_bot_api_benign_general(text):
                return
            fingerprint = self._fingerprint(record, text)
            now = time()
            cooldown = max(0, int(getattr(config, "DEEPSEEK_ERROR_COOLDOWN_SEC", 60)))
            if now - self._last_seen.get(fingerprint, 0) < cooldown:
                return
            self._last_seen[fingerprint] = now
            self.loop.create_task(self._report(record, text, fingerprint[:10]))
        except Exception:
            pass

    def _mark_deepseek_unavailable(self) -> None:
        self._deepseek_skip_until = time() + float(self._deepseek_skip_sec)

    def _deepseek_temporarily_skipped(self) -> bool:
        return time() < float(getattr(self, "_deepseek_skip_until", 0.0) or 0.0)

    @staticmethod
    def _is_deepseek_network_failure(ex: BaseException) -> bool:
        name = type(ex).__name__
        if name in {
            "TimeoutError",
            "CancelledError",
            "ConnectionTimeoutError",
            "ServerTimeoutError",
            "ClientConnectorError",
            "ClientOSError",
            "ClientConnectionError",
            "ClientConnectorDNSError",
            "ClientPayloadError",
        }:
            return True
        if isinstance(ex, (asyncio.TimeoutError, TimeoutError)):
            return True
        try:
            if isinstance(ex, aiohttp.ClientError):
                return True
        except Exception:
            pass
        text = str(ex).lower()
        return (
            "timeout" in text
            or "connection" in text
            or "deepseek.com" in text
            or "cannot connect" in text
        )

    async def _deepseek_analysis(self, text: str) -> str:
        api_key = (getattr(config, "DEEPSEEK_API_KEY", "") or "").strip()
        if not api_key:
            return "DeepSeek: disabled (DEEPSEEK_API_KEY is empty)."

        if self._deepseek_temporarily_skipped():
            return (
                "DeepSeek: temporarily skipped after recent network/timeout failures. "
                "Raw error is shown above."
            )

        total = max(3, int(getattr(config, "DEEPSEEK_ERROR_TIMEOUT_SEC", 12)))
        timeout = aiohttp.ClientTimeout(
            total=total,
            connect=min(8, total),
            sock_connect=min(8, total),
            sock_read=total,
        )
        prompt = (
            "You are debugging a Python Telegram music bot runtime error. "
            "Reply in Myanmar language. Return concise analysis only: root cause, immediate action, "
            "and likely code/file area. "
            "Do not include secrets.\n\n"
            f"Error log:\n{text[:3500]}"
        )
        payload = {
            "model": getattr(config, "DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "messages": [
                {"role": "system", "content": "You are a concise senior Python debugging assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://api.deepseek.com/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        # 5xx / rate limits are transient; open a short circuit.
                        if resp.status >= 500 or resp.status == 429:
                            self._mark_deepseek_unavailable()
                        return (
                            f"DeepSeek request failed: HTTP {resp.status} "
                            f"{self._sanitize(body[:120])}"
                        )
                    data = await resp.json()
        except Exception as ex:
            if self._is_deepseek_network_failure(ex):
                self._mark_deepseek_unavailable()
                logger = logging.getLogger(__name__)
                logger.warning(
                    "DeepSeek analysis skipped (network/timeout): %s",
                    type(ex).__name__,
                )
                return (
                    "DeepSeek: network/timeout (api.deepseek.com unreachable or slow). "
                    "Raw error is shown above. Check host outbound HTTPS / DNS."
                )
            logger = logging.getLogger(__name__)
            logger.warning(
                "DeepSeek analysis skipped (%s): %s",
                type(ex).__name__,
                str(ex)[:160],
            )
            return f"DeepSeek: skipped ({type(ex).__name__}). Raw error is shown above."

        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            return "DeepSeek response parse failed."
        return self._sanitize(str(content).strip()) or "DeepSeek returned empty analysis."

    async def _report(self, record: logging.LogRecord, text: str, event_id: str) -> None:
        max_chars = max(800, int(getattr(config, "DEEPSEEK_ERROR_MAX_CHARS", 3500)))
        error_text = text[:max_chars]
        analysis = "DeepSeek: skipped."
        if getattr(config, "DEEPSEEK_ERROR_ANALYZE", True):
            try:
                analysis = await self._deepseek_analysis(error_text)
            except Exception as ex:
                # Never surface "DeepSeek analysis failed: TimeoutError:" to owners.
                if self._is_deepseek_network_failure(ex):
                    self._mark_deepseek_unavailable()
                    analysis = (
                        "DeepSeek: network/timeout. Raw error is shown above. "
                        "Check host access to api.deepseek.com."
                    )
                else:
                    analysis = (
                        f"DeepSeek: skipped ({type(ex).__name__}). "
                        "Raw error is shown above."
                    )

        # If analysis itself is only a timeout notice, keep the report short.
        show_analysis = bool(analysis) and not analysis.startswith(
            "DeepSeek analysis failed"
        )

        message = (
            f"<b>⚠️ Error Report</b> <code>{event_id}</code>\n"
            f"<b>အဆင့်:</b> <code>{html.escape(record.levelname)}</code>\n"
            f"<b>နေရာ:</b> <code>{html.escape(record.name)}</code>\n\n"
            f"<b>ဖြစ်သွားတဲ့ error:</b>\n<pre>{html.escape(error_text)}</pre>\n"
        )
        if show_analysis:
            message += (
                f"\n<b>မြန်မာလိုရှင်းပြချက် / ပြင်ရန်:</b>\n"
                f"<pre>{html.escape(analysis[:1800])}</pre>"
            )
        try:
            await app.send_message(app.logger, message, disable_web_page_preview=True)
        except Exception:
            pass


async def report_music_status(
    chat_id: int,
    reason: str,
    *,
    detail: str | None = None,
    source: str | None = None,
) -> None:
    try:
        from AnonX_3 import db, queue

        if not await db.get_music_status_report():
            return

        chat_title = str(chat_id)
        try:
            chat = await app.get_chat(chat_id)
            chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat_id)
        except Exception:
            pass

        media = queue.get_current(chat_id)
        queue_len = len(queue.get_queue(chat_id))
        active = await db.get_call(chat_id)
        playing = await db.playing(chat_id) if active else False
        title = _short(getattr(media, "title", None) if media else None) or "မရှိပါ"
        user = _short(getattr(media, "user", None) if media else None) or "မသိ"
        position = getattr(media, "time", 0) if media else 0
        duration = getattr(media, "duration", None) if media else None
        duration_text = f"{position}s / {duration}" if duration else f"{position}s"

        lines = [
            "<b>🎧 Music Bot Status</b>",
            f"<b>အခြေအနေ:</b> {html.escape(reason)}",
            f"<b>Group:</b> {html.escape(chat_title)}",
            f"<b>Group ID:</b> <code>{chat_id}</code>",
            f"<b>Active:</b> <code>{bool(active)}</code> | <b>Playing:</b> <code>{bool(playing)}</code>",
            f"<b>Queue:</b> <code>{queue_len}</code>",
            f"<b>လက်ရှိသီချင်း:</b> {html.escape(title)}",
            f"<b>တောင်းဆိုသူ:</b> {html.escape(user)}",
            f"<b>Played:</b> <code>{html.escape(duration_text)}</code>",
        ]
        if source:
            lines.append(f"<b>Source:</b> <code>{html.escape(source)}</code>")
        if detail:
            lines.append(f"<b>အသေးစိတ်:</b> {html.escape(_short(detail, 700))}")
        lines.append("")
        lines.append("ဆိုလိုတာ: bot ဘာကြောင့်ရပ်/ပြောင်းသွားလဲကို live မှတ်တမ်းတင်ထားတာပါ။")
        await app.send_message(app.logger, "\n".join(lines), disable_web_page_preview=True)
    except Exception:
        pass


def _install_runtime_guards(loop: asyncio.AbstractEventLoop) -> None:
    global _RUNTIME_GUARDS_INSTALLED
    if _RUNTIME_GUARDS_INSTALLED:
        return
    transient_filter = TransientRuntimeNoiseFilter()
    for logger_name in (
        "pyrogram.dispatcher",
        "pyrogram.connection.transport.tcp.tcp",
        "asyncio",
        "asyncio.unhandled",
    ):
        target_logger = logging.getLogger(logger_name)
        if not any(isinstance(f, TransientRuntimeNoiseFilter) for f in target_logger.filters):
            target_logger.addFilter(transient_filter)

    previous_handler = loop.get_exception_handler()

    def _loop_exception_handler(loop_obj, context):
        if _is_transient_asyncio_context(context):
            return
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio exception")
        exc_info = (type(exc), exc, exc.__traceback__) if exc else None
        
        # Log but never let the default handler kill the process
        try:
            error_logger = logging.getLogger("asyncio.unhandled")
            if exc_info:
                error_logger.error(msg, exc_info=exc_info)
            else:
                error_logger.error("asyncio unhandled: %s", msg)
        except Exception:
            pass
        
        # Call previous handler only — NEVER call default_exception_handler
        # which can os._exit() on some platforms
        if previous_handler:
            try:
                previous_handler(loop_obj, context)
            except Exception:
                pass

    loop.set_exception_handler(_loop_exception_handler)
    _RUNTIME_GUARDS_INSTALLED = True


def install_error_monitor(loop: asyncio.AbstractEventLoop | None = None) -> None:
    global _INSTALLED
    _patch_pyrogram_bad_msg_notification()
    loop = loop or asyncio.get_running_loop()
    _install_runtime_guards(loop)
    if _INSTALLED or not getattr(config, "DEEPSEEK_ERROR_MONITOR", True):
        return

    handler = DeepSeekErrorMonitor(loop)
    logging.getLogger().addHandler(handler)
    _INSTALLED = True
