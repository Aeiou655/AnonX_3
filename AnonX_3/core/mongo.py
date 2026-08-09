# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import re
from time import time
from datetime import datetime, timedelta, timezone

import aiohttp
from zoneinfo import ZoneInfo

from pymongo import AsyncMongoClient
from pymongo.errors import ConfigurationError, DuplicateKeyError, ServerSelectionTimeoutError

from AnonX_3 import config, logger, userbot


def _coerce_utc_datetime(
    value,
    default: datetime | None = None,
) -> datetime | None:
    """Convert persisted timestamp shapes to offset-aware UTC datetimes."""
    result: datetime | None = None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        try:
            result = datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            result = None
    elif isinstance(value, str):
        text = value.strip()
        if text:
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                result = datetime.fromisoformat(text)
            except ValueError:
                result = None

    if result is None:
        result = default
    if result is None:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _auto_reply_activity_time(meta: dict) -> datetime | None:
    """Return the newest valid UTC activity timestamp from rule metadata."""
    values: list[datetime] = []
    for field in ("last_used_at", "last_learned_at", "created_at"):
        value = _coerce_utc_datetime(meta.get(field))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _auto_reply_candidate_activity_time(entries) -> datetime:
    """Return the newest valid candidate observation time as aware UTC."""
    values: list[datetime] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = _coerce_utc_datetime(entry.get("last_seen_at"))
            if value is None:
                value = _coerce_utc_datetime(entry.get("first_seen_at"))
            if value is not None:
                values.append(value)
    return max(values, default=datetime.min.replace(tzinfo=timezone.utc))


def select_stale_auto_reply_keys(
    rules: dict,
    metadata: dict,
    cutoff: datetime,
    limit: int = 100,
) -> list[str]:
    """Select expired learned-only rules, least-used and oldest first."""
    if not isinstance(rules, dict) or not isinstance(metadata, dict):
        return []
    cutoff = _coerce_utc_datetime(cutoff, datetime.now(timezone.utc))
    candidates: list[tuple[int, datetime, str]] = []
    for keyword, raw_meta in metadata.items():
        if keyword not in rules or not isinstance(raw_meta, dict):
            continue
        if raw_meta.get("source") != "auto":
            continue
        activity = _auto_reply_activity_time(raw_meta)
        if activity is None or activity > cutoff:
            continue
        try:
            usage = max(0, int(raw_meta.get("use_count", 0) or 0))
        except (TypeError, ValueError):
            usage = 0
        candidates.append((usage, activity, str(keyword)))
    candidates.sort(key=lambda item: (item[0], item[1], item[2].casefold()))
    return [item[2] for item in candidates[: max(1, int(limit or 1))]]


class MongoDB:
    def __init__(self):
        """
        Initialize the MongoDB connection with retryable operations enabled.
        """
        try:
            self.mongo = AsyncMongoClient(
                config.MONGO_URL,
                serverSelectionTimeoutMS=config.MONGO_TIMEOUT_MS,
                retryWrites=True,
                retryReads=True,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
                maxPoolSize=20,
                minPoolSize=2,
                maxIdleTimeMS=60000,
            )
        except ConfigurationError as e:
            raise SystemExit("Database configuration invalid: check MONGO_URL format.") from e
        self.db = self.mongo.get_default_database(default="AnonX_3")

        self.admin_list = {}
        self.active_calls = {}
        self.admin_play = []
        # Cache both enabled and disabled play-mode reads.  Previously only
        # enabled chats were cached, so the common disabled/default case hit
        # MongoDB on every /play command.
        self._admin_play_known: set[int] = set()
        self.blacklisted = []
        self.cmd_delete = []
        self.autoplay = []
        self.autoplay_loaded = set()
        self.aidj_mode = {}
        self.auto_react = {}
        self.auto_react_global = None
        self.auto_reply = {}
        self.auto_reply_global = None
        self.auto_learn = {}
        self.loop = {}
        self.notified = []
        self.cache = self.db.cache
        self.vcstats = self.db.vcstats
        self.media_cache = self.db.media_cache
        self.logger = False
        self.music_status_report = True

        self.assistant = {}
        self.assistantdb = self.db.assistant

        self.auth = {}
        self.authdb = self.db.auth

        self.afk = {}

        self.chats = set()
        self.chatsdb = self.db.chats
        self.global_kicksdb = self.db.global_kicks

        self.lang = {}
        self.langdb = self.db.lang

        self.users = set()
        self.usersdb = self.db.users
        self.name_profilesdb = self.db.name_profiles
        self.audiencedb = self.db.audience
        self.bot_images = {}
        self.custom_text = {}
        self.custom_text_loaded = False
        self._auto_reply_rules = None
        self.button_styles = {}
        self.button_texts = {}
        self._custom_text_translation_lock = {}
        self.logger_groups = {}
        self.name_checker = {}
        self.logger_groupsdb = self.db.logger_groups

    async def connect(self) -> None:
        """Check if we can connect to the database.

        Raises:
            SystemExit: If the connection to the database fails.
        """
        try:
            start = time()
            await self.mongo.admin.command("ping")
            logger.info(f"Database connection successful. ({time() - start:.2f}s)")
            await self.load_cache()
        except ServerSelectionTimeoutError as e:
            raise SystemExit(
                "Database connection failed: ServerSelectionTimeoutError. "
                "Check MONGO_URL, MongoDB Atlas Network Access/IP whitelist, "
                "VPS DNS/firewall, and cluster status."
            ) from e
        except Exception as e:
            raise SystemExit(f"Database connection failed: {type(e).__name__}") from e

    async def close(self) -> None:
        """Close the connection to the database."""
        await self.mongo.close()
        logger.info("Database connection closed.")

    @staticmethod
    def _short(value: object, limit: int = 120) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    async def _retry_operation(
        operation,
        *,
        max_retries: int = 3,
        base_delay: float = 0.5,
        label: str = "db_op",
    ):
        """Retry a MongoDB operation with exponential backoff on transient errors."""
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                return await operation()
            except Exception as ex:
                last_exc = ex
                exc_name = type(ex).__name__.lower()
                transient = any(tag in exc_name for tag in (
                    "timeout", "connection", "network", "notwritableprimary",
                    "serverselection", "replica", "socket", "reset",
                ))
                if not transient or attempt >= max_retries:
                    break
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "DB %s transient error (attempt %s/%s): %s — retrying in %.1fs",
                    label,
                    attempt,
                    max_retries,
                    MongoDB._short(ex, 100),
                    delay,
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    # CACHE
    async def get_call(self, chat_id: int) -> bool:
        return chat_id in self.active_calls

    async def add_call(self, chat_id: int) -> None:
        self.active_calls[chat_id] = 1

    async def remove_call(self, chat_id: int) -> None:
        self.active_calls.pop(chat_id, None)

    async def claim_command_once(
        self, command: str, chat_id: int, message_id: int
    ) -> bool:
        """Atomically claim a command update so a restart cannot replay it."""
        claim_id = f"command_claim:{command}:{chat_id}:{message_id}"
        try:
            await self.cache.insert_one(
                {
                    "_id": claim_id,
                    "command": command,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "created_at": datetime.now(timezone.utc),
                }
            )
        except DuplicateKeyError:
            return False
        return True

    async def playing(self, chat_id: int, paused: bool = None) -> bool | None:
        if paused is not None:
            self.active_calls[chat_id] = int(not paused)
        return bool(self.active_calls.get(chat_id, 0))

    async def get_admins(self, chat_id: int, reload: bool = False) -> list[int]:
        from AnonX_3.helpers._admins import reload_admins

        if chat_id not in self.admin_list or reload:
            self.admin_list[chat_id] = await reload_admins(chat_id)
        return self.admin_list[chat_id]

    async def get_loop(self, chat_id: int) -> int:
        return self.loop.get(chat_id, 0)

    async def set_loop(self, chat_id: int, count: int) -> None:
        self.loop[chat_id] = count

    # AUTH METHODS
    async def _get_auth(self, chat_id: int) -> set[int]:
        if chat_id not in self.auth:
            doc = await self.authdb.find_one({"_id": chat_id}) or {}
            self.auth[chat_id] = set(doc.get("user_ids", []))
        return self.auth[chat_id]

    async def is_auth(self, chat_id: int, user_id: int) -> bool:
        return user_id in await self._get_auth(chat_id)

    async def add_auth(self, chat_id: int, user_id: int) -> None:
        users = await self._get_auth(chat_id)
        if user_id not in users:
            users.add(user_id)
            await self.authdb.update_one(
                {"_id": chat_id}, {"$addToSet": {"user_ids": user_id}}, upsert=True
            )

    async def rm_auth(self, chat_id: int, user_id: int) -> None:
        users = await self._get_auth(chat_id)
        if user_id in users:
            users.discard(user_id)
            await self.authdb.update_one(
                {"_id": chat_id}, {"$pull": {"user_ids": user_id}}
            )

    # AFK METHODS
    async def get_afk(self, user_id: int) -> dict | None:
        if user_id not in self.afk:
            doc = await self.usersdb.find_one({"_id": user_id}, {"afk": 1}) or {}
            data = doc.get("afk")
            self.afk[user_id] = data if isinstance(data, dict) and data.get("since") else None
        return self.afk[user_id]

    async def set_afk(self, user_id: int, reason: str = "") -> dict:
        data = {"since": int(time()), "reason": reason.strip()}
        self.afk[user_id] = data
        await self.usersdb.update_one(
            {"_id": user_id},
            {"$set": {"afk": data}},
            upsert=True,
        )
        return data

    async def clear_afk(self, user_id: int) -> None:
        self.afk[user_id] = None
        await self.usersdb.update_one(
            {"_id": user_id},
            {"$unset": {"afk": ""}},
            upsert=True,
        )

    # ASSISTANT METHODS
    async def set_assistant(self, chat_id: int, num: int = 1) -> int:
        await userbot.wait_until_ready()
        total = len(userbot.clients)
        try:
            num = int(num)
        except Exception:
            num = 1
        if num < 1 or num > total:
            num = 1
        await self.assistantdb.update_one(
            {"_id": chat_id},
            {"$set": {"num": num}},
            upsert=True,
        )
        self.assistant[chat_id] = num
        return num

    async def rotate_assistant(self, chat_id: int) -> int:
        await userbot.wait_until_ready()
        current = int(self.assistant.get(chat_id) or 1)
        total = len(userbot.clients)
        nxt = (current % total) + 1
        return await self.set_assistant(chat_id, nxt)

    async def get_assistant(self, chat_id: int):
        from AnonX_3 import anon

        if chat_id not in self.assistant:
            doc = await self.assistantdb.find_one({"_id": chat_id})
            num = doc["num"] if doc else await self.set_assistant(chat_id)
            self.assistant[chat_id] = num

        await anon.wait_until_ready()

        num = int(self.assistant.get(chat_id) or 0)
        if num < 1 or num > len(anon.clients):
            logger.warning(
                "Assistant index out of range for chat_id=%s (num=%s, available=%s). Reassigning.",
                chat_id,
                num,
                len(anon.clients),
            )
            num = await self.set_assistant(chat_id)
            self.assistant[chat_id] = num

        return anon.clients[num - 1]

    async def get_client(self, chat_id: int):
        if chat_id not in self.assistant:
            await self.get_assistant(chat_id)
        await userbot.wait_until_ready()

        num = int(self.assistant.get(chat_id) or 0)
        if num < 1 or num > len(userbot.clients):
            logger.warning(
                "Assistant userbot index out of range for chat_id=%s (num=%s, available=%s). Reassigning.",
                chat_id,
                num,
                len(userbot.clients),
            )
            num = await self.set_assistant(chat_id)
            self.assistant[chat_id] = num
        return userbot.clients[num - 1]

    # BLACKLIST METHODS
    async def add_blacklist(self, chat_id: int) -> None:
        if str(chat_id).startswith("-"):
            self.blacklisted.append(chat_id)
            return await self.cache.update_one(
                {"_id": "bl_chats"}, {"$addToSet": {"chat_ids": chat_id}}, upsert=True
            )
        await self.cache.update_one(
            {"_id": "bl_users"}, {"$addToSet": {"user_ids": chat_id}}, upsert=True
        )

    async def del_blacklist(self, chat_id: int) -> None:
        if str(chat_id).startswith("-"):
            self.blacklisted.remove(chat_id)
            return await self.cache.update_one(
                {"_id": "bl_chats"},
                {"$pull": {"chat_ids": chat_id}},
            )
        await self.cache.update_one(
            {"_id": "bl_users"},
            {"$pull": {"user_ids": chat_id}},
        )

    async def get_blacklisted(self, chat: bool = False) -> list[int]:
        if chat:
            if not self.blacklisted:
                doc = await self.cache.find_one({"_id": "bl_chats"})
                self.blacklisted.extend(doc.get("chat_ids", []) if doc else [])
            return self.blacklisted
        doc = await self.cache.find_one({"_id": "bl_users"})
        return doc.get("user_ids", []) if doc else []

    # CHAT METHODS
    async def is_chat(self, chat_id: int) -> bool:
        return chat_id in self.chats

    async def add_chat(self, chat_id: int) -> None:
        if not await self.is_chat(chat_id):
            self.chats.add(chat_id)
            try:
                await self.chatsdb.insert_one({"_id": chat_id})
            except DuplicateKeyError:
                pass
        await self.touch_audience(
            peer_id=chat_id,
            peer_type="group",
            source="add_chat",
            is_active=True,
        )

    async def rm_chat(self, chat_id: int) -> None:
        if await self.is_chat(chat_id):
            self.chats.discard(chat_id)
            await self.chatsdb.delete_one({"_id": chat_id})

    async def get_chats(self) -> list:
        if not self.chats:
            self.chats.update([chat["_id"] async for chat in self.chatsdb.find()])
        return list(self.chats)

    # GLOBAL SILENT KICK WATCHLIST
    async def add_global_kick(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        added_by: int | None = None,
    ) -> None:
        normalized_username = (username or "").lstrip("@")
        await self.global_kicksdb.update_one(
            {"_id": int(user_id)},
            {
                "$set": {
                    "username": normalized_username,
                    "username_key": normalized_username.casefold(),
                    "first_name": first_name or "",
                    "added_by": added_by,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )

    async def del_global_kick(self, user_id: int) -> bool:
        result = await self.global_kicksdb.delete_one({"_id": int(user_id)})
        return bool(result.deleted_count)

    async def get_global_kicks(self) -> list[dict]:
        cursor = self.global_kicksdb.find({}).sort("_id", 1)
        return [entry async for entry in cursor]

    async def get_global_kick_ids(self) -> set[int]:
        return {
            int(entry["_id"])
            async for entry in self.global_kicksdb.find({}, {"_id": 1})
        }

    async def find_global_kick_by_username(self, username: str) -> dict | None:
        username_key = username.strip().lstrip("@").casefold()
        if not username_key:
            return None
        return await self.global_kicksdb.find_one({"username_key": username_key})

    # COMMAND DELETE
    async def get_cmd_delete(self, chat_id: int) -> bool:
        if chat_id not in self.cmd_delete:
            doc = await self.chatsdb.find_one({"_id": chat_id})
            if doc and doc.get("cmd_delete"):
                self.cmd_delete.append(chat_id)
        return chat_id in self.cmd_delete

    async def set_cmd_delete(self, chat_id: int, delete: bool = False) -> None:
        if delete:
            self.cmd_delete.append(chat_id)
        else:
            self.cmd_delete.remove(chat_id)
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"cmd_delete": delete}},
            upsert=True,
        )

    async def get_autoplay(self, chat_id: int) -> bool:
        if chat_id not in self.autoplay_loaded:
            doc = await self.chatsdb.find_one(
                {"_id": chat_id},
                {"autoplay": 1, "aidj_mode": 1},
            )
            if doc and doc.get("autoplay") and chat_id not in self.autoplay:
                self.autoplay.append(chat_id)
            if doc and isinstance(doc.get("aidj_mode"), str):
                self.aidj_mode[chat_id] = doc["aidj_mode"]
            self.autoplay_loaded.add(chat_id)
        return chat_id in self.autoplay

    async def set_autoplay(self, chat_id: int, enabled: bool = False) -> None:
        self.autoplay_loaded.add(chat_id)
        if enabled:
            if chat_id not in self.autoplay:
                self.autoplay.append(chat_id)
        elif chat_id in self.autoplay:
            self.autoplay.remove(chat_id)
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"autoplay": enabled}},
            upsert=True,
        )

    async def get_auto_react(self, chat_id: int) -> bool:
        if chat_id not in self.auto_react:
            doc = await self.chatsdb.find_one({"_id": chat_id}, {"auto_react": 1})
            if doc and "auto_react" in doc:
                self.auto_react[chat_id] = bool(doc["auto_react"])
            else:
                self.auto_react[chat_id] = await self.get_auto_react_global()
        return self.auto_react[chat_id]

    async def set_auto_react(self, chat_id: int, enabled: bool = False) -> None:
        self.auto_react[chat_id] = bool(enabled)
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_react": enabled}},
            upsert=True,
        )

    async def get_auto_react_global(self) -> bool:
        if self.auto_react_global is None:
            doc = await self.cache.find_one({"_id": "auto_react_global"}, {"enabled": 1})
            self.auto_react_global = bool(doc.get("enabled", True)) if doc else True
        return self.auto_react_global

    async def set_auto_react_all(self, enabled: bool = False) -> None:
        enabled = bool(enabled)
        self.auto_react_global = enabled
        await self.cache.update_one(
            {"_id": "auto_react_global"},
            {"$set": {"enabled": enabled}},
            upsert=True,
        )
        await self.chatsdb.update_many({}, {"$set": {"auto_react": enabled}})
        for chat_id in await self.get_chats():
            self.auto_react[chat_id] = enabled

    async def get_auto_reply(self, chat_id: int) -> bool:
        if chat_id not in self.auto_reply:
            doc = await self.chatsdb.find_one({"_id": chat_id}, {"auto_reply": 1})
            if doc and "auto_reply" in doc:
                self.auto_reply[chat_id] = bool(doc["auto_reply"])
            else:
                self.auto_reply[chat_id] = await self.get_auto_reply_global()
        return self.auto_reply[chat_id]

    async def set_auto_reply(self, chat_id: int, enabled: bool = False) -> None:
        enabled = bool(enabled)
        self.auto_reply[chat_id] = enabled
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_reply": enabled}},
            upsert=True,
        )

    async def get_auto_learn(self, chat_id: int) -> bool:
        """Teach-by-reply: admin reply to a keyword message stores auto-reply."""
        if not hasattr(self, "auto_learn") or self.auto_learn is None:
            self.auto_learn = {}
        if chat_id not in self.auto_learn:
            doc = await self.chatsdb.find_one({"_id": chat_id}, {"auto_learn": 1})
            # Default ON — reply-to-message teaches the bot (user request)
            if doc and "auto_learn" in doc:
                self.auto_learn[chat_id] = bool(doc["auto_learn"])
            else:
                self.auto_learn[chat_id] = True
        return self.auto_learn[chat_id]

    async def set_auto_learn(self, chat_id: int, enabled: bool = True) -> None:
        if not hasattr(self, "auto_learn") or self.auto_learn is None:
            self.auto_learn = {}
        enabled = bool(enabled)
        self.auto_learn[chat_id] = enabled
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_learn": enabled}},
            upsert=True,
        )

    async def get_auto_reply_global(self) -> bool:
        if self.auto_reply_global is None:
            doc = await self.cache.find_one({"_id": "auto_reply_global"}, {"enabled": 1})
            self.auto_reply_global = bool(doc.get("enabled", True)) if doc else True
        return self.auto_reply_global

    async def set_auto_reply_all(self, enabled: bool = False) -> None:
        enabled = bool(enabled)
        self.auto_reply_global = enabled
        await self.cache.update_one(
            {"_id": "auto_reply_global"},
            {"$set": {"enabled": enabled}},
            upsert=True,
        )
        await self.chatsdb.update_many({}, {"$set": {"auto_reply": enabled}})
        for chat_id in await self.get_chats():
            self.auto_reply[chat_id] = enabled

    async def get_auto_reply_rules_global(self) -> dict:
        doc = await self.cache.find_one({"_id": "auto_reply_global"}, {"rules": 1})
        rules = doc.get("rules", {}) if doc else {}
        return rules if isinstance(rules, dict) else {}

    async def get_auto_reply_rules_for_scope(
        self, chat_id: int, global_scope: bool = False
    ) -> dict:
        if global_scope:
            return await self.get_auto_reply_rules_global()
        doc = await self.chatsdb.find_one({"_id": chat_id}, {"auto_reply_rules": 1})
        rules = doc.get("auto_reply_rules", {}) if doc else {}
        return rules if isinstance(rules, dict) else {}

    async def get_auto_reply_rules(self, chat_id: int) -> dict:
        rules = dict(await self.get_auto_reply_rules_global())
        group_rules = await self.get_auto_reply_rules_for_scope(chat_id)
        rules.update(group_rules)
        return rules

    async def get_auto_reply_metadata(self, chat_id: int) -> dict:
        doc = await self.chatsdb.find_one({"_id": chat_id}, {"auto_reply_meta": 1})
        metadata = doc.get("auto_reply_meta", {}) if doc else {}
        return metadata if isinstance(metadata, dict) else {}

    async def get_auto_reply_candidates(self, chat_id: int) -> dict:
        """Return unconfirmed teach-by-reply candidates for one group."""
        doc = await self.chatsdb.find_one(
            {"_id": chat_id}, {"auto_reply_candidates": 1}
        )
        candidates = doc.get("auto_reply_candidates", {}) if doc else {}
        return candidates if isinstance(candidates, dict) else {}

    async def observe_auto_reply_candidate(
        self,
        chat_id: int,
        keyword: str,
        variant: dict,
        *,
        confirmations: int = 2,
    ) -> tuple[int, bool]:
        """Record one automatic teach observation without activating it.

        A candidate becomes eligible only after the same keyword/answer pair
        is observed repeatedly. Existing manual or legacy rules are never
        modified by automatic learning.
        """
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        text = unicodedata.normalize(
            "NFC", str((variant or {}).get("text") or "")
        ).strip()
        if not key or not text or not isinstance(variant, dict):
            return 0, False

        rules = await self.get_auto_reply_rules_for_scope(chat_id)
        metadata = await self.get_auto_reply_metadata(chat_id)
        existing_key = next(
            (
                old
                for old in rules
                if unicodedata.normalize("NFC", str(old or "")).casefold().strip()
                == key
            ),
            None,
        )
        if existing_key is not None:
            existing_meta = metadata.get(existing_key, {})
            if not isinstance(existing_meta, dict) or existing_meta.get("source") != "auto":
                return 0, False

        candidates = await self.get_auto_reply_candidates(chat_id)
        entries = candidates.get(key, [])
        if not isinstance(entries, list):
            entries = []
        else:
            normalised_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                first_seen_at = _coerce_utc_datetime(entry.get("first_seen_at"))
                last_seen_at = _coerce_utc_datetime(
                    entry.get("last_seen_at"),
                    default=first_seen_at,
                )
                if first_seen_at is not None:
                    entry["first_seen_at"] = first_seen_at
                if last_seen_at is not None:
                    entry["last_seen_at"] = last_seen_at
                normalised_entries.append(entry)
            entries = normalised_entries
        now = datetime.now(timezone.utc)
        match = next(
            (
                item
                for item in entries
                if isinstance(item, dict) and item.get("text") == text
            ),
            None,
        )
        if match is None:
            match = {
                "text": text,
                "entities": variant.get("entities")
                if isinstance(variant.get("entities"), list)
                else [],
                "observations": 0,
                "first_seen_at": now,
            }
            entries.append(match)
        match["observations"] = min(
            100, max(0, int(match.get("observations", 0) or 0)) + 1
        )
        match["last_seen_at"] = now
        # Keep the candidate collection bounded even if a group teaches many
        # different phrases before any of them is confirmed.
        candidates[key] = entries[-5:]
        if len(candidates) > 200:
            oldest = sorted(
                candidates,
                key=lambda item: _auto_reply_candidate_activity_time(
                    candidates.get(item)
                ),
            )
            for old_key in oldest[: len(candidates) - 200]:
                candidates.pop(old_key, None)

        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_reply_candidates": candidates}},
            upsert=True,
        )
        required = max(2, int(confirmations or 2))
        return int(match["observations"]), int(match["observations"]) >= required

    async def clear_auto_reply_candidate(
        self, chat_id: int, keyword: str, text: str | None = None
    ) -> None:
        """Remove one promoted candidate, or all candidates for a keyword."""
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        if not key:
            return
        candidates = await self.get_auto_reply_candidates(chat_id)
        entries = candidates.get(key, [])
        if text is None:
            candidates.pop(key, None)
        elif isinstance(entries, list):
            wanted = unicodedata.normalize("NFC", str(text or "")).strip()
            entries = [
                item
                for item in entries
                if not isinstance(item, dict) or item.get("text") != wanted
            ]
            if entries:
                candidates[key] = entries
            else:
                candidates.pop(key, None)
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_reply_candidates": candidates}},
            upsert=True,
        )

    async def set_auto_reply_rule(
        self,
        chat_id: int,
        keyword: str,
        rule: dict | list,
        global_scope: bool = False,
        source: str = "manual",
        allow_auto_create: bool = False,
    ) -> None:
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        if not key:
            return
        if global_scope:
            rules = await self.get_auto_reply_rules_global()
            # Drop any mixed-case duplicates of the same keyword
            for old in list(rules.keys()):
                old_n = unicodedata.normalize("NFC", str(old or "")).casefold().strip()
                if old_n == key and old != key:
                    rules.pop(old, None)
            rules[key] = rule
            await self.cache.update_one(
                {"_id": "auto_reply_global"},
                {"$set": {"rules": rules}},
                upsert=True,
            )
            return
        rules = await self.get_auto_reply_rules_for_scope(chat_id)
        metadata = await self.get_auto_reply_metadata(chat_id)
        candidates = await self.get_auto_reply_candidates(chat_id)
        candidates.pop(key, None)
        for old in list(rules.keys()):
            old_n = unicodedata.normalize("NFC", str(old or "")).casefold().strip()
            if old_n == key and old != key:
                rules.pop(old, None)
                if old in metadata and key not in metadata:
                    metadata[key] = metadata.pop(old)
        rules[key] = rule
        now = datetime.now(timezone.utc)
        previous = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
        previous_source = previous.get("source")
        if source == "manual":
            metadata[key] = {
                **previous,
                "source": "manual",
                "updated_at": now,
            }
        elif source == "auto" and (
            previous_source == "auto" or (allow_auto_create and not previous_source)
        ):
            metadata[key] = {
                **previous,
                "source": "auto",
                "created_at": _coerce_utc_datetime(
                    previous.get("created_at"),
                    now,
                ),
                "last_learned_at": now,
                "use_count": max(0, int(previous.get("use_count", 0) or 0)),
            }
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {
                "$set": {
                    "auto_reply_rules": rules,
                    "auto_reply_meta": metadata,
                    "auto_reply_candidates": candidates,
                }
            },
            upsert=True,
        )

    async def append_auto_reply_variant(
        self,
        chat_id: int,
        keyword: str,
        variant: dict,
        global_scope: bool = False,
    ) -> tuple[int, bool]:
        """Add a reply variant (multi-learn). Returns (variant_count, is_new_keyword)."""
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        if not key or not isinstance(variant, dict):
            return 0, False
        text = unicodedata.normalize("NFC", str(variant.get("text") or "")).strip()
        # Strip zero-width / bidi junk that breaks Myanmar rendering
        import re as _re

        text = _re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]", "", text)
        if not text:
            return 0, False

        rules = (
            await self.get_auto_reply_rules_global()
            if global_scope
            else await self.get_auto_reply_rules_for_scope(chat_id)
        )
        if not isinstance(rules, dict):
            rules = {}

        existing_key = key
        for old in list(rules.keys()):
            old_n = unicodedata.normalize("NFC", str(old or "")).casefold().strip()
            if old_n == key:
                existing_key = old
                break

        current = rules.get(existing_key)
        is_new = current is None
        variants: list = []
        if isinstance(current, list):
            variants = [v for v in current if isinstance(v, dict)]
        elif isinstance(current, dict) and current.get("text"):
            variants = [current]
        elif isinstance(current, str) and current.strip():
            variants = [{"text": current, "entities": []}]

        # Dedupe by exact text — no new learn
        if any(str(v.get("text") or "") == text for v in variants):
            if not global_scope:
                await self.touch_auto_reply_rule(
                    chat_id, key, learned=True, allow_auto_create=is_new
                )
            return 0, False

        variants.append(
            {
                "text": text,
                "entities": variant.get("entities")
                if isinstance(variant.get("entities"), list)
                else [],
            }
        )
        # Cap variants to avoid unbounded growth
        if len(variants) > 20:
            variants = variants[-20:]

        payload: dict | list = variants[0] if len(variants) == 1 else variants
        await self.set_auto_reply_rule(
            chat_id,
            key,
            payload,
            global_scope=global_scope,
            source="auto",
            allow_auto_create=is_new,
        )
        return len(variants), is_new

    async def touch_auto_reply_rule(
        self,
        chat_id: int,
        keyword: str,
        *,
        learned: bool = False,
        used: bool = False,
        allow_auto_create: bool = False,
    ) -> None:
        """Refresh learned-rule activity without converting manual rules."""
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        if not key:
            return
        metadata = await self.get_auto_reply_metadata(chat_id)
        current = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
        source = current.get("source")
        if source != "auto":
            if not allow_auto_create or source == "manual":
                return
            current = {"source": "auto", "created_at": datetime.now(timezone.utc)}
        now = datetime.now(timezone.utc)
        if learned:
            current["last_learned_at"] = now
        if used:
            current["last_used_at"] = now
            current["use_count"] = max(
                0, int(current.get("use_count", 0) or 0)
            ) + 1
        metadata[key] = current
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"auto_reply_meta": metadata}},
            upsert=True,
        )

    async def get_aidj_mode(self, chat_id: int) -> str:
        await self.get_autoplay(chat_id)
        mode = self.aidj_mode.get(chat_id, "similar")
        return mode if isinstance(mode, str) and mode else "similar"

    async def set_aidj_mode(self, chat_id: int, mode: str = "similar") -> None:
        clean = str(mode or "similar").strip().lower()
        self.aidj_mode[chat_id] = clean
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"aidj_mode": clean}},
            upsert=True,
        )

    async def cleanup_stale_auto_reply_rules(
        self,
        *,
        max_idle_seconds: float,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[tuple[int, str]]:
        """Remove learned-only rules inactive past the configured TTL."""
        current = _coerce_utc_datetime(now, datetime.now(timezone.utc))
        cutoff = current - timedelta(seconds=max(1.0, float(max_idle_seconds)))
        remaining = max(1, int(limit or 1))
        removed: list[tuple[int, str]] = []
        cursor = self.chatsdb.find(
            {"auto_reply_meta": {"$exists": True}},
            {"auto_reply_rules": 1, "auto_reply_meta": 1},
        )
        async for doc in cursor:
            if remaining <= 0:
                break
            rules = doc.get("auto_reply_rules", {})
            metadata = doc.get("auto_reply_meta", {})
            stale = select_stale_auto_reply_keys(
                rules, metadata, cutoff, limit=remaining
            )
            if not stale:
                continue
            for key in stale:
                rules.pop(key, None)
                metadata.pop(key, None)
                removed.append((int(doc["_id"]), key))
            await self.chatsdb.update_one(
                {"_id": doc["_id"]},
                {"$set": {"auto_reply_rules": rules, "auto_reply_meta": metadata}},
            )
            remaining -= len(stale)
        return removed

    async def del_auto_reply_rule(
        self, chat_id: int, keyword: str, global_scope: bool = False
    ) -> bool:
        """Delete a learned keyword rule. Returns True if at least one key was removed."""
        import unicodedata

        key = unicodedata.normalize("NFC", str(keyword or "")).casefold().strip()
        if not key:
            return False

        def _pop_matches(rules: dict) -> bool:
            removed = False
            for old in list(rules.keys()):
                old_norm = unicodedata.normalize(
                    "NFC", str(old or "")
                ).casefold().strip()
                if old_norm == key or str(old) == keyword:
                    rules.pop(old, None)
                    removed = True
            return removed

        if global_scope:
            rules = await self.get_auto_reply_rules_global()
            if not isinstance(rules, dict):
                rules = {}
            removed = _pop_matches(rules)
            if removed:
                await self.cache.update_one(
                    {"_id": "auto_reply_global"},
                    {"$set": {"rules": rules}},
                    upsert=True,
                )
            return removed

        rules = await self.get_auto_reply_rules_for_scope(chat_id)
        metadata = await self.get_auto_reply_metadata(chat_id)
        if not isinstance(rules, dict):
            rules = {}
        removed = _pop_matches(rules)
        if removed:
            candidates = await self.get_auto_reply_candidates(chat_id)
            for old in list(candidates.keys()):
                old_norm = unicodedata.normalize(
                    "NFC", str(old or "")
                ).casefold().strip()
                if old_norm == key or str(old) == keyword:
                    candidates.pop(old, None)
            for old in list(metadata.keys()):
                old_norm = unicodedata.normalize(
                    "NFC", str(old or "")
                ).casefold().strip()
                if old_norm == key or str(old) == keyword:
                    metadata.pop(old, None)
            await self.chatsdb.update_one(
                {"_id": chat_id},
                {
                    "$set": {
                        "auto_reply_rules": rules,
                        "auto_reply_meta": metadata,
                        "auto_reply_candidates": candidates,
                    }
                },
                upsert=True,
            )
        return removed

    async def migrate_auto_reply_rules(self, rules: dict) -> None:
        if not isinstance(rules, dict) or not rules:
            return
        doc = await self.cache.find_one(
            {"_id": "auto_reply_global"},
            {"rules": 1, "reply_json_migrated": 1},
        ) or {}
        if doc.get("reply_json_migrated"):
            return
        merged = doc.get("rules", {}) if isinstance(doc.get("rules", {}), dict) else {}
        for keyword, rule in rules.items():
            merged.setdefault(keyword, rule)
        await self.cache.update_one(
            {"_id": "auto_reply_global"},
            {"$set": {"rules": merged, "reply_json_migrated": True}},
            upsert=True,
        )

    # LANGUAGE METHODS
    async def set_lang(self, chat_id: int, lang_code: str):
        await self.langdb.update_one(
            {"_id": chat_id},
            {"$set": {"lang": lang_code}},
            upsert=True,
        )
        self.lang[chat_id] = lang_code

    async def get_lang(self, chat_id: int) -> str:
        if chat_id not in self.lang:
            doc = await self.langdb.find_one({"_id": chat_id})
            self.lang[chat_id] = doc["lang"] if doc else config.LANG_CODE
        return self.lang[chat_id]

    # LOGGER METHODS
    async def is_logger(self) -> bool:
        return self.logger

    async def get_logger(self) -> bool:
        doc = await self.cache.find_one({"_id": "logger"})
        if doc:
            self.logger = doc["status"]
        return self.logger

    async def set_logger(self, status: bool, chat_id: int | None = None) -> None:
        if chat_id is None:
            self.logger = status
            self.logger_groups = {}
            await self.cache.update_one(
                {"_id": "logger"},
                {"$set": {"status": status}},
                upsert=True,
            )
            await self.logger_groupsdb.delete_many({})
            return

        self.logger_groups[chat_id] = bool(status)
        await self.logger_groupsdb.update_one(
            {"_id": chat_id},
            {"$set": {"status": bool(status)}},
            upsert=True,
        )

    async def is_name_checker_for_chat(self, chat_id: int) -> bool:
        if chat_id in self.name_checker:
            return bool(self.name_checker[chat_id])
        doc = await self.chatsdb.find_one({"_id": chat_id}, {"name_checker": 1})
        if doc and "name_checker" in doc:
            self.name_checker[chat_id] = bool(doc["name_checker"])
        else:
            self.name_checker[chat_id] = True
        return bool(self.name_checker[chat_id])

    async def set_name_checker(self, chat_id: int, enabled: bool = False) -> None:
        enabled = bool(enabled)
        self.name_checker[chat_id] = enabled
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"name_checker": enabled}},
            upsert=True,
        )

    async def get_music_status_report(self) -> bool:
        doc = await self.cache.find_one({"_id": "music_status_report"})
        if doc and "status" in doc:
            self.music_status_report = bool(doc["status"])
        return self.music_status_report

    async def set_music_status_report(self, status: bool) -> None:
        self.music_status_report = bool(status)
        await self.cache.update_one(
            {"_id": "music_status_report"},
            {"$set": {"status": self.music_status_report}},
            upsert=True,
        )

    # ACTIVE VC STATS
    def _activevc_now(self) -> datetime:
        try:
            return datetime.now(ZoneInfo(config.ACTIVEVC_TIMEZONE))
        except Exception:
            return datetime.now(timezone.utc)

    async def add_activevc_sample(self) -> None:
        now_local = self._activevc_now()
        ts_utc = int(datetime.now(timezone.utc).timestamp())
        date_local = now_local.strftime("%Y-%m-%d")
        hour_local = now_local.hour
        active_groups = len(self.active_calls)
        _id = f"{date_local}:{hour_local:02d}"
        await self.vcstats.update_one(
            {"_id": _id},
            {
                "$setOnInsert": {
                    "date_local": date_local,
                    "hour_local": hour_local,
                },
                "$set": {
                    "last_ts_utc": ts_utc,
                },
                "$max": {
                    "max_active_groups": active_groups,
                },
                "$inc": {
                    "sample_count": 1,
                },
            },
            upsert=True,
        )

    async def get_today_peak_hour(self) -> dict | None:
        today = self._activevc_now().strftime("%Y-%m-%d")
        doc = await self.vcstats.find_one(
            {"date_local": today},
            sort=[("max_active_groups", -1), ("hour_local", 1)],
        )
        if not doc:
            return None
        return {
            "date": doc["date_local"],
            "hour": int(doc["hour_local"]),
            "groups": int(doc.get("max_active_groups", 0)),
        }

    async def get_last_day_peak_hour(self) -> dict | None:
        today = self._activevc_now().strftime("%Y-%m-%d")
        date_doc = await self.vcstats.find_one(
            {"date_local": {"$lt": today}},
            sort=[("date_local", -1)],
        )
        if not date_doc:
            return None
        target_day = date_doc["date_local"]
        doc = await self.vcstats.find_one(
            {"date_local": target_day},
            sort=[("max_active_groups", -1), ("hour_local", 1)],
        )
        if not doc:
            return None
        return {
            "date": doc["date_local"],
            "hour": int(doc["hour_local"]),
            "groups": int(doc.get("max_active_groups", 0)),
        }

    # PLAY MODE METHODS
    async def get_play_mode(self, chat_id: int) -> bool:
        if chat_id not in self._admin_play_known:
            doc = await self.chatsdb.find_one({"_id": chat_id})
            if doc and doc.get("admin_play"):
                self.admin_play.append(chat_id)
            self._admin_play_known.add(chat_id)
        return chat_id in self.admin_play

    async def set_play_mode(self, chat_id: int, remove: bool = False) -> None:
        if remove and chat_id in self.admin_play:
            self.admin_play.remove(chat_id)
        elif not remove and chat_id not in self.admin_play:
            self.admin_play.append(chat_id)
        self._admin_play_known.add(chat_id)
        await self.chatsdb.update_one(
            {"_id": chat_id},
            {"$set": {"admin_play": not remove}},
            upsert=True,
        )

    # SUDO METHODS
    async def add_sudo(self, user_id: int) -> None:
        await self.cache.update_one(
            {"_id": "sudoers"}, {"$addToSet": {"user_ids": user_id}}, upsert=True
        )

    async def del_sudo(self, user_id: int) -> None:
        await self.cache.update_one(
            {"_id": "sudoers"}, {"$pull": {"user_ids": user_id}}
        )

    async def get_sudoers(self) -> list[int]:
        doc = await self.cache.find_one({"_id": "sudoers"})
        return doc.get("user_ids", []) if doc else []

    # OWNER METHODS
    async def add_owner(self, user_id: int) -> None:
        await self.cache.update_one(
            {"_id": "owners"}, {"$addToSet": {"user_ids": user_id}}, upsert=True
        )

    async def del_owner(self, user_id: int) -> None:
        await self.cache.update_one(
            {"_id": "owners"}, {"$pull": {"user_ids": user_id}}
        )

    async def get_owners(self) -> list[int]:
        doc = await self.cache.find_one({"_id": "owners"})
        return doc.get("user_ids", []) if doc else []

    # BOT IMAGE METHODS
    async def get_bot_image(self, key: str) -> str:
        from AnonX_3 import config

        defaults = {
            "default_thumb": config.DEFAULT_THUMB,
            "ping_img": config.PING_IMG,
            "start_img": config.START_IMG,
        }
        if key not in defaults:
            raise KeyError(f"Unknown bot image key: {key}")

        if not self.bot_images:
            doc = await self.cache.find_one({"_id": "bot_images"}) or {}
            self.bot_images = doc.get("images", {})

        value = self.bot_images.get(key)
        if key == "start_img" and value == "__disabled__":
            return ""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return defaults[key]

    async def set_bot_image(self, key: str, value: str) -> None:
        if key not in {"default_thumb", "ping_img", "start_img"}:
            raise KeyError(f"Unknown bot image key: {key}")

        self.bot_images[key] = value
        await self.cache.update_one(
            {"_id": "bot_images"},
            {"$set": {f"images.{key}": value}},
            upsert=True,
        )

    # MEDIA FILE ID CACHE
    async def get_cached_file(self, video_id: str, video: bool = False) -> str | None:
        kind = "video" if video else "audio"
        doc = await self.media_cache.find_one({"_id": f"{video_id}:{kind}"})
        if doc:
            return doc.get("file_id")
        return None

    async def set_cached_file(self, video_id: str, file_id: str, video: bool = False) -> None:
        kind = "video" if video else "audio"
        try:
            await self.media_cache.update_one(
                {"_id": f"{video_id}:{kind}"},
                {"$set": {"file_id": file_id, "cached_at": int(time())}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Failed to cache file_id for %s:%s: %s", video_id, kind, e)

    async def get_top_songs(self, limit: int = 10) -> list[dict]:
        try:
            cursor = (
                self.cache.find(
                    {"_id": {"$regex": "^play:"}},
                    {"title": 1, "url": 1, "count": 1, "last_played": 1, "_id": 0},
                )
                .sort("count", -1)
                .limit(max(1, min(limit, 25)))
            )
            results = []
            async for doc in cursor:
                results.append(
                    {
                        "title": doc.get("title", "Unknown"),
                        "url": doc.get("url", ""),
                        "count": doc.get("count", 0),
                        "last_played": doc.get("last_played", 0),
                    }
                )
            return results
        except Exception as ex:
            logger.warning("Failed to fetch top songs: %s", ex)
            return []

    # CUSTOM TEXT METHODS
    async def _ensure_custom_text_loaded(self) -> None:
        if self.custom_text_loaded:
            return

        doc = await self.cache.find_one({"_id": "custom_text"}) or {}
        values = doc.get("values", {})
        self.custom_text = values if isinstance(values, dict) else {}
        self.custom_text_loaded = True

    @staticmethod
    def is_valid_custom_text(value: str | dict | None) -> bool:
        if isinstance(value, str):
            return bool(value.strip())

        if isinstance(value, dict):
            text = value.get("text")
            entities = value.get("entities")
            return (
                isinstance(text, str)
                and bool(text.strip())
                and (entities is None or isinstance(entities, list))
            )

        return False

    @staticmethod
    def is_localized_custom_text(value) -> bool:
        return (
            isinstance(value, dict)
            and value.get("_localized") is True
            and isinstance(value.get("values"), dict)
        )

    @classmethod
    def is_valid_custom_text_value(cls, value) -> bool:
        if cls.is_localized_custom_text(value):
            return any(
                cls.is_valid_custom_text(item)
                for item in value.get("values", {}).values()
            )
        return cls.is_valid_custom_text(value)

    @staticmethod
    def _make_localized_custom_text(value: str | dict, source_lang: str) -> dict:
        return {
            "_localized": True,
            "source_lang": source_lang,
            "values": {source_lang: value},
        }

    @classmethod
    def _first_localized_custom_text(
        cls,
        localized: dict,
    ) -> tuple[str | None, str | dict | None]:
        values = localized.get("values", {})
        source_lang = localized.get("source_lang")
        if source_lang and cls.is_valid_custom_text(values.get(source_lang)):
            return source_lang, values[source_lang]

        for lang_code, value in values.items():
            if cls.is_valid_custom_text(value):
                return lang_code, value

        return None, None

    def _get_custom_text_lock(self, key: str, lang_code: str):
        lock_key = f"{key}:{lang_code}"
        lock = self._custom_text_translation_lock.get(lock_key)
        if lock is None:
            import asyncio

            lock = asyncio.Lock()
            self._custom_text_translation_lock[lock_key] = lock
        return lock

    @staticmethod
    def _utf16_length(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _entity_to_markup(text: str, entities: list[dict] | None) -> str:
        if not entities:
            return text

        opening: dict[int, list[str]] = {}
        closing: dict[int, list[str]] = {}

        def add_tag(start: int, end: int, open_tag: str, close_tag: str) -> None:
            opening.setdefault(start, []).append(open_tag)
            closing.setdefault(end, []).insert(0, close_tag)

        for entity in sorted(entities, key=lambda item: (item["offset"], -item["length"])):
            start = entity.get("offset")
            length = entity.get("length")
            if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
                continue

            end = start + length
            etype = entity.get("type")
            if etype == "bold":
                add_tag(start, end, "<b>", "</b>")
            elif etype == "italic":
                add_tag(start, end, "<i>", "</i>")
            elif etype == "underline":
                add_tag(start, end, "<u>", "</u>")
            elif etype == "strike":
                add_tag(start, end, "<s>", "</s>")
            elif etype == "code":
                add_tag(start, end, "<code>", "</code>")
            elif etype == "pre":
                language = entity.get("language")
                language_attr = f' language="{language}"' if language else ""
                add_tag(start, end, f"<pre{language_attr}>", "</pre>")
            elif etype == "text_link" and entity.get("url"):
                add_tag(start, end, f'<a href="{entity["url"]}">', "</a>")
            elif etype == "custom_emoji" and entity.get("custom_emoji_id"):
                add_tag(
                    start,
                    end,
                    f'<emoji id="{entity["custom_emoji_id"]}">',
                    "</emoji>",
                )

        result = []
        utf16_offset = 0
        if utf16_offset in opening:
            result.extend(opening[utf16_offset])
        for char in text:
            result.append(char)
            utf16_offset += MongoDB._utf16_length(char)
            if utf16_offset in closing:
                result.extend(closing[utf16_offset])
            if utf16_offset in opening:
                result.extend(opening[utf16_offset])

        return "".join(result)

    @classmethod
    def _template_to_markup(cls, template: str | dict) -> str:
        if isinstance(template, str):
            return template
        return cls._entity_to_markup(template["text"], template.get("entities"))

    @classmethod
    def _custom_emoji_signature(cls, value: str | dict | None) -> tuple[str, ...]:
        if not cls.is_valid_custom_text(value):
            return ()

        if isinstance(value, dict):
            entities = value.get("entities") or []
            signature = [
                str(entity.get("custom_emoji_id"))
                for entity in entities
                if isinstance(entity, dict)
                and entity.get("type") == "custom_emoji"
                and entity.get("custom_emoji_id")
            ]
            if signature:
                return tuple(signature)

        markup = cls._template_to_markup(value)
        return tuple(
            match.group(1)
            for match in re.finditer(
                r'<emoji\b[^>]*\bid=["\']?(\d+)["\']?[^>]*>.*?</emoji>',
                markup,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    @classmethod
    def _custom_emoji_needs_refresh(
        cls,
        source_value: str | dict | None,
        target_value: str | dict | None,
    ) -> bool:
        source_signature = cls._custom_emoji_signature(source_value)
        if not source_signature:
            return False
        return cls._custom_emoji_signature(target_value) != source_signature

    @staticmethod
    def _protect_template_tokens(text: str) -> tuple[str, dict[str, str]]:
        tokens: dict[str, str] = {}
        index = 0

        def replace(match):
            nonlocal index
            token = f"ZXTXTOKEN{index}Q"
            tokens[token] = match.group(0)
            index += 1
            return token

        # Preserve the entire custom emoji block so the translation step
        # cannot alter or drop the placeholder character inside it.
        protected = re.sub(
            r"<emoji\b[^>]*>.*?</emoji>",
            replace,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        protected = re.sub(r"</?[^>]+>", replace, protected)
        protected = re.sub(r"\{\{|\}\}|\{[a-zA-Z0-9_]+\}", replace, protected)
        return protected, tokens

    @staticmethod
    def _restore_template_tokens(text: str, tokens: dict[str, str]) -> str:
        for token, original in tokens.items():
            text = text.replace(token, original)
        return text

    @staticmethod
    def _protect_plain_text_tokens(text: str) -> tuple[str, dict[str, str]]:
        tokens: dict[str, str] = {}
        index = 0

        def replace(match):
            nonlocal index
            token = f"ZXTTEXTTOKEN{index}Q"
            tokens[token] = match.group(0)
            index += 1
            return token

        protected = re.sub(r"\{\{|\}\}|\{[a-zA-Z0-9_]+\}", replace, text)
        return protected, tokens

    @classmethod
    def _utf16_offset_map(cls, text: str) -> dict[int, int]:
        offset_map = {}
        offset = 0
        for index, char in enumerate(text):
            offset_map[offset] = index
            offset += cls._utf16_length(char)
        offset_map[offset] = len(text)
        return offset_map

    async def _translate_entity_template_value(
        self,
        value: dict,
        target_lang: str,
        source_lang: str,
    ) -> str | dict | None:
        text = value.get("text")
        entities = value.get("entities") or []
        if not isinstance(text, str):
            return None
        if not entities:
            protected, tokens = self._protect_plain_text_tokens(text)
            translated = await self._translate_text(protected, target_lang, source_lang)
            if translated is None:
                return None
            return self._restore_template_tokens(translated, tokens)

        boundaries = {0, self._utf16_length(text)}
        normalized_entities = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            start = entity.get("offset")
            length = entity.get("length")
            if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
                continue
            end = start + length
            boundaries.add(start)
            boundaries.add(end)
            normalized_entities.append(dict(entity))

        offset_map = self._utf16_offset_map(text)
        if not all(boundary in offset_map for boundary in boundaries):
            return None

        translated_parts = []
        segment_map = []
        new_offset = 0
        ordered_boundaries = sorted(boundaries)
        for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
            if end <= start:
                continue
            start_index = offset_map[start]
            end_index = offset_map[end]
            segment_text = text[start_index:end_index]
            active_indexes = [
                idx
                for idx, entity in enumerate(normalized_entities)
                if entity["offset"] <= start and entity["offset"] + entity["length"] >= end
            ]
            if any(normalized_entities[idx].get("type") == "custom_emoji" for idx in active_indexes):
                translated_segment = segment_text
            else:
                protected, tokens = self._protect_plain_text_tokens(segment_text)
                translated_segment = await self._translate_text(
                    protected,
                    target_lang,
                    source_lang,
                )
                if translated_segment is None:
                    return None
                translated_segment = self._restore_template_tokens(translated_segment, tokens)

            translated_parts.append(translated_segment)
            segment_map.append({
                "original_start": start,
                "original_end": end,
                "new_start": new_offset,
                "new_end": new_offset + self._utf16_length(translated_segment),
                "active_indexes": active_indexes,
            })
            new_offset += self._utf16_length(translated_segment)

        translated_text = "".join(translated_parts)
        rebuilt_entities = []
        for idx, entity in enumerate(normalized_entities):
            segments = [
                item for item in segment_map if idx in item["active_indexes"]
            ]
            if not segments:
                continue
            new_entity = dict(entity)
            new_entity["offset"] = segments[0]["new_start"]
            new_entity["length"] = segments[-1]["new_end"] - segments[0]["new_start"]
            if new_entity["length"] > 0:
                rebuilt_entities.append(new_entity)

        return {
            "text": translated_text,
            "entities": rebuilt_entities,
        }

    @classmethod
    def _translation_keeps_custom_emojis(
        cls,
        source_value: str | dict,
        translated_value: str | dict | None,
    ) -> bool:
        if translated_value is None:
            return False
        return not cls._custom_emoji_needs_refresh(source_value, translated_value)

    @staticmethod
    async def _translate_text(
        text: str,
        target_lang: str,
        source_lang: str = "auto",
    ) -> str | None:
        if not text.strip():
            return text

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={
                        "client": "gtx",
                        "sl": source_lang or "auto",
                        "tl": target_lang,
                        "dt": "t",
                        "q": text,
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except Exception as ex:
            logger.warning("Custom text translation failed: %s", ex)
            return None

        try:
            return "".join(item[0] for item in data[0] if item and item[0])
        except Exception:
            return None

    async def _translate_custom_text_value(
        self,
        value: str | dict,
        target_lang: str,
        source_lang: str,
    ) -> str | dict | None:
        if isinstance(value, dict):
            translated_value = await self._translate_entity_template_value(
                value,
                target_lang,
                source_lang,
            )
        else:
            markup = self._template_to_markup(value)
            protected, tokens = self._protect_template_tokens(markup)
            translated = await self._translate_text(protected, target_lang, source_lang)
            if not translated:
                return None

            restored = self._restore_template_tokens(translated, tokens)
            from AnonX_3.helpers import utils

            translated_value = utils.preview_template_text(restored)
        if not self.is_valid_custom_text(translated_value):
            return None
        if not self._translation_keeps_custom_emojis(value, translated_value):
            logger.warning(
                "Skipping localized custom text because premium emoji entities changed."
            )
            return None
        return translated_value

    async def _persist_custom_text_value(self, key: str, value) -> None:
        self.custom_text[key] = value
        await self.cache.update_one(
            {"_id": "custom_text"},
            {"$set": {f"values.{key}": value}},
            upsert=True,
        )

    async def ensure_custom_text_language(self, lang_code: str) -> None:
        await self._ensure_custom_text_loaded()
        for key, value in list(self.custom_text.items()):
            if not self.is_localized_custom_text(value):
                if self.is_valid_custom_text(value):
                    value = self._make_localized_custom_text(value, "auto")
                    await self._persist_custom_text_value(key, value)
                else:
                    continue

            source_lang, source_value = self._first_localized_custom_text(value)
            if not source_value:
                continue

            force_refresh = (
                lang_code != source_lang
                and bool(self._custom_emoji_signature(source_value))
            )
            existing_value = value.get("values", {}).get(lang_code)
            if (
                not force_refresh
                and self.is_valid_custom_text(existing_value)
                and not self._custom_emoji_needs_refresh(
                    source_value,
                    existing_value,
                )
            ):
                continue

            lock = self._get_custom_text_lock(key, lang_code)
            async with lock:
                latest = self.custom_text.get(key, value)
                if self.is_localized_custom_text(latest):
                    existing = latest.get("values", {}).get(lang_code)
                    source_lang, source_value = self._first_localized_custom_text(latest)
                    force_refresh = (
                        lang_code != source_lang
                        and bool(self._custom_emoji_signature(source_value))
                    )
                    if (
                        not force_refresh
                        and self.is_valid_custom_text(existing)
                        and not self._custom_emoji_needs_refresh(
                            source_value,
                            existing,
                        )
                    ):
                        continue
                if not source_value:
                    continue

                translated = await self._translate_custom_text_value(
                    source_value,
                    lang_code,
                    source_lang or "auto",
                )
                if not self.is_valid_custom_text(translated):
                    continue

                latest["values"][lang_code] = translated
                await self._persist_custom_text_value(key, latest)

    async def has_custom_text(self, key: str) -> bool:
        await self._ensure_custom_text_loaded()
        return self.is_valid_custom_text_value(self.custom_text.get(key))

    async def get_custom_text_state(
        self,
        key: str,
        default: str | dict,
        lang_code: str | None = None,
    ) -> tuple[str | dict, str]:
        await self._ensure_custom_text_loaded()
        value = self.custom_text.get(key)
        if self.is_valid_custom_text(value):
            return value, "custom"
        if self.is_localized_custom_text(value):
            requested = value.get("values", {}).get(lang_code) if lang_code else None
            source_lang, source_value = self._first_localized_custom_text(value)
            if not source_value:
                return default, "default"

            if self.is_valid_custom_text(requested) and not self._custom_emoji_needs_refresh(
                source_value,
                requested,
            ):
                return requested, "custom"

            if lang_code:
                lock = self._get_custom_text_lock(key, lang_code)
                async with lock:
                    latest = self.custom_text.get(key, value)
                    if self.is_localized_custom_text(latest):
                        requested = latest.get("values", {}).get(lang_code)
                        source_lang, source_value = self._first_localized_custom_text(latest)
                        if self.is_valid_custom_text(requested) and not self._custom_emoji_needs_refresh(
                            source_value,
                            requested,
                        ):
                            return requested, "custom"
                    if source_value:
                        translated = await self._translate_custom_text_value(
                            source_value,
                            lang_code,
                            source_lang or "auto",
                        )
                        if self.is_valid_custom_text(translated):
                            latest["values"][lang_code] = translated
                            await self._persist_custom_text_value(key, latest)
                            return translated, "custom"

            return source_value, "custom"

        # Fallback to MongoDB directly in case the cache is stale
        # (e.g., another worker/instance updated the value after cache load).
        doc = await self.cache.find_one({"_id": "custom_text"}) or {}
        values = doc.get("values", {})
        if isinstance(values, dict):
            value = values.get(key)
            if self.is_valid_custom_text(value):
                self.custom_text[key] = value
                return value, "custom"
            if self.is_localized_custom_text(value):
                self.custom_text[key] = value
                return await self.get_custom_text_state(key, default, lang_code)

        return default, "default"

    async def get_custom_text(
        self,
        key: str,
        default: str | dict,
        lang_code: str | None = None,
    ) -> str | dict:
        value, _ = await self.get_custom_text_state(key, default, lang_code)
        return value

    async def get_custom_text_for_chat(
        self,
        chat_id: int,
        key: str,
        default: str | dict,
    ) -> str | dict:
        lang_code = await self.get_lang(chat_id)
        return await self.get_custom_text(key, default, lang_code)

    async def set_custom_text(
        self,
        key: str,
        value: str | dict,
        lang_code: str | None = None,
    ) -> None:
        await self._ensure_custom_text_loaded()
        current = self.custom_text.get(key)

        if lang_code:
            if self.is_localized_custom_text(current):
                localized = current
            elif self.is_valid_custom_text(current):
                localized = self._make_localized_custom_text(current, "auto")
            else:
                localized = self._make_localized_custom_text(value, lang_code)

            localized["source_lang"] = lang_code
            localized.setdefault("values", {})[lang_code] = value
            await self._persist_custom_text_value(key, localized)
            return

        await self._persist_custom_text_value(key, value)

    async def delete_custom_text(self, key: str) -> None:
        await self._ensure_custom_text_loaded()
        self.custom_text.pop(key, None)
        await self.cache.update_one(
            {"_id": "custom_text"},
            {"$unset": {f"values.{key}": ""}},
        )

    async def _ensure_auto_reply_loaded(self) -> None:
        if self._auto_reply_rules is None:
            doc = await self.cache.find_one({"_id": "auto_reply"}) or {}
            self._auto_reply_rules: dict[str, dict] = doc.get("rules", {})

    async def set_legacy_auto_reply(self, keyword: str, response: dict) -> None:
        await self._ensure_auto_reply_loaded()
        keyword_lower = keyword.lower().strip()
        self._auto_reply_rules[keyword_lower] = response
        await self.cache.update_one(
            {"_id": "auto_reply"},
            {"$set": {f"rules.{keyword_lower}": response}},
            upsert=True,
        )

    async def get_legacy_auto_reply(self, keyword: str) -> dict | None:
        await self._ensure_auto_reply_loaded()
        return self._auto_reply_rules.get(keyword.lower().strip())

    async def delete_auto_reply(self, keyword: str) -> bool:
        await self._ensure_auto_reply_loaded()
        keyword_lower = keyword.lower().strip()
        if keyword_lower not in self._auto_reply_rules:
            return False
        del self._auto_reply_rules[keyword_lower]
        await self.cache.update_one(
            {"_id": "auto_reply"},
            {"$unset": {f"rules.{keyword_lower}": ""}},
        )
        return True

    async def get_all_auto_replies(self) -> dict[str, dict]:
        await self._ensure_auto_reply_loaded()
        return dict(self._auto_reply_rules)

    async def get_button_style(self, key: str, default: str) -> str:
        if key not in self.button_styles:
            doc = await self.cache.find_one({"_id": "button_styles"}) or {}
            self.button_styles = doc.get("values", {})
        return self.button_styles.get(key) or default

    async def set_button_style(self, key: str, style: str) -> None:
        self.button_styles[key] = style
        await self.cache.update_one(
            {"_id": "button_styles"},
            {"$set": {f"values.{key}": style}},
            upsert=True,
        )

    async def get_button_text(self, key: str, default: str | dict | None = None):
        if key not in self.button_texts:
            doc = await self.cache.find_one({"_id": "button_texts"}) or {}
            self.button_texts = doc.get("values", {})
        return self.button_texts.get(key, default)

    async def get_button_texts(self) -> dict:
        doc = await self.cache.find_one({"_id": "button_texts"}) or {}
        values = doc.get("values", {})
        self.button_texts = values if isinstance(values, dict) else {}
        return dict(self.button_texts)

    async def set_button_text(self, key: str, value: str | dict) -> None:
        self.button_texts[key] = value
        await self.cache.update_one(
            {"_id": "button_texts"},
            {"$set": {f"values.{key}": value}},
            upsert=True,
        )

    async def delete_button_text(self, key: str) -> None:
        self.button_texts.pop(key, None)
        await self.cache.update_one(
            {"_id": "button_texts"},
            {"$unset": {f"values.{key}": ""}},
            upsert=True,
        )

    # USER METHODS
    async def is_user(self, user_id: int) -> bool:
        return user_id in self.users

    async def add_user(self, user_id: int) -> None:
        if not await self.is_user(user_id):
            self.users.add(user_id)
            try:
                await self.usersdb.insert_one({"_id": user_id})
            except DuplicateKeyError:
                pass
        await self.touch_audience(
            peer_id=user_id,
            peer_type="user",
            source="add_user",
            blocked=False,
            is_active=True,
        )

    async def rm_user(self, user_id: int) -> None:
        if await self.is_user(user_id):
            self.users.discard(user_id)
            await self.usersdb.delete_one({"_id": user_id})

    async def get_users(self) -> list:
        if not self.users:
            self.users.update([user["_id"] async for user in self.usersdb.find()])
        return list(self.users)

    async def update_name_profile(self, profile: dict) -> dict | None:
        user_id = profile.get("user_id")
        if not isinstance(user_id, int):
            return None

        current = dict(profile)
        current["_id"] = user_id
        current["updated_at"] = int(time())

        previous = await self.name_profilesdb.find_one({"_id": user_id})
        if previous:
            comparable = {
                "first_name": previous.get("first_name") or "",
                "last_name": previous.get("last_name") or "",
                "full_name": previous.get("full_name") or "",
                "username": previous.get("username") or "",
            }
            incoming = {
                "first_name": current.get("first_name") or "",
                "last_name": current.get("last_name") or "",
                "full_name": current.get("full_name") or "",
                "username": current.get("username") or "",
            }
            if comparable == incoming:
                return None

        await self.name_profilesdb.update_one(
            {"_id": user_id},
            {"$set": current},
            upsert=True,
        )
        return previous

    async def touch_audience(
        self,
        peer_id: int,
        peer_type: str,
        *,
        source: str = "",
        blocked: bool | None = None,
        is_admin: bool | None = None,
        is_active: bool | None = None,
    ) -> None:
        if peer_type not in {"user", "group"}:
            return
        update_set = {
            "peer_id": peer_id,
            "peer_type": peer_type,
            "last_seen_ts": int(time()),
            "source": source or "unknown",
        }
        if blocked is not None:
            update_set["blocked"] = bool(blocked)
        if is_admin is not None:
            update_set["is_admin"] = bool(is_admin)
        if is_active is not None:
            update_set["is_active"] = bool(is_active)
        await self.audiencedb.update_one(
            {"_id": f"{peer_type}:{peer_id}"},
            {"$set": update_set},
            upsert=True,
        )


    async def migrate_coll(self) -> None:
        logger.info("Migrating users and chats from old collections...")

        users, musers, mchats = [], [], []
        seen_chats, seen_users = set(), set()
        users.extend([user async for user in self.usersdb.find()])
        users.extend([user async for user in self.db.tgusersdb.find()])

        for user in users:
            _id = user.get("_id")
            if isinstance(_id, int):
                user_id = _id
            else:
                user_id = int(user.get("user_id"))

            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            musers.append({"_id": user_id})

        await self.usersdb.drop()
        await self.db.tgusersdb.drop()
        if musers:
            await self.usersdb.insert_many(musers)

        async for chat in self.chatsdb.find():
            _id = chat.get("_id")
            if isinstance(_id, int):
                chat_id = _id
            else:
                chat_id = int(chat.get("chat_id"))

            if chat_id in seen_chats:
                continue
            seen_chats.add(chat_id)
            mchats.append({"_id": chat_id})

        await self.chatsdb.drop()
        if mchats:
            await self.chatsdb.insert_many(mchats)

        await self.cache.insert_one({"_id": "migrated"})
        logger.info("Migration completed successfully.")

    async def load_cache(self) -> None:
        doc = await self.cache.find_one({"_id": "migrated"})
        if not doc:
            await self.migrate_coll()

        await self.get_chats()
        await self.get_users()
        await self.get_blacklisted(True)
        await self.get_logger()
        logger.info("Database cache loaded.")
