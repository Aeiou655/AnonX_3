# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import json
from functools import wraps
from pathlib import Path

from pyrogram import errors
from pymongo.errors import ServerSelectionTimeoutError

from AnonX_3 import bot_api, db, logger

def _short_error(ex: Exception, limit: int = 220) -> str:
    text = str(ex or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_transient_telegram_timeout(ex: Exception) -> bool:
    err = str(ex)
    return "after 10 retries" in err and any(
        query in err
        for query in (
            "channels.GetMessages",
            "messages.GetMessages",
            "messages.GetStickerSet",
            "messages.SendMessage",
            "messages.EditMessage",
            "messages.GetDialogs",
        )
    )


lang_codes = {
    "ar": "العربية",
    "de": "Deutsch",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "hi": "हिन्दी",
    "ja": "日本語",
    "my": "မြန်မာဘာသာ",
    "pa": "ਪੰਜਾਬੀ",
    "pt": "Português",
    "ru": "Русский",
    "tr": "Türkçe",
    "zh": "中文"
}


class Language:
    """
    Language class for managing multilingual support using JSON language files.
    """

    def __init__(self):
        self.lang_codes = lang_codes
        self.lang_dir = Path("AnonX_3/locales")
        self.languages = self.load_files()

    def load_files(self):
        languages = {}
        lang_files = {file.stem: file for file in self.lang_dir.glob("*.json")}
        if "en" not in lang_files:
            logger.warning("Reference language file en.json is missing.")
            return languages

        with open(lang_files["en"], "r", encoding="utf-8-sig") as file:
            reference = json.load(file)

        for lang_code, lang_file in lang_files.items():
            with open(lang_file, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                logger.warning(f"Skipping non-dict locale file: {lang_file.name}")
                continue
            languages[lang_code] = {**reference, **data}
        logger.info(f"Loaded languages: {', '.join(languages.keys())}")
        return languages

    async def get_lang(self, chat_id: int) -> dict:
        lang_code = await db.get_lang(chat_id)
        return self.languages.get(lang_code) or self.languages.get("en", {})

    def get_languages(self) -> dict:
        files = {f.stem for f in self.lang_dir.glob("*.json")}
        return {code: self.lang_codes.get(code, code) for code in sorted(files) if code in self.languages}

    def language(self):
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                fallen = next(
                    (
                        arg
                        for arg in args
                        if hasattr(arg, "chat") or hasattr(arg, "message")
                    ),
                    None,
                )

                if not fallen.from_user:
                    return

                if hasattr(fallen, "chat"):
                    chat = fallen.chat
                elif hasattr(fallen, "message"):
                    chat = fallen.message.chat

                if not chat: return

                try:
                    if chat.id in db.blacklisted:
                        logger.info(f"Chat {chat.id} is blacklisted, leaving...")
                        return await chat.leave()

                    lang_code = await db.get_lang(chat.id)
                    lang_dict = self.languages.get(lang_code) or self.languages.get("en", {})
                    setattr(fallen, "lang", lang_dict)
                    return await func(*args, **kwargs)
                except (
                    errors.ChannelPrivate, errors.MessageIdInvalid, errors.MessageNotModified,
                    errors.FloodWait,
                ):
                    return
                except (
                    errors.Forbidden, errors.exceptions.Forbidden,
                    errors.ChatWriteForbidden, errors.exceptions.ChatWriteForbidden,
                    bot_api.ChatForbidden,
                ):
                    return
                except ServerSelectionTimeoutError as ex:
                    logger.warning("Handler skipped because MongoDB is temporarily unavailable: %s", _short_error(ex))
                    return
                except TimeoutError as ex:
                    if _is_transient_telegram_timeout(ex):
                        logger.warning("Handler skipped because Telegram transport timed out: %s", _short_error(ex))
                        return
                    raise

            return wrapper

        return decorator


