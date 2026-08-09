# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import inspect
import os
from pathlib import Path

import pyrogram
from pyrogram import errors

from AnonX_3 import config, logger


DEPLOY_ROOT = Path(__file__).resolve().parents[2]


async def shutdown_pyrogram_client(client: pyrogram.Client) -> list[Exception]:
    """Close any Pyrogram startup state exactly once.

    ``Client.start()`` can fail while ``storage.open()`` has already created a
    SQLite connection but before ``is_connected`` becomes true.  Calling
    ``Client.stop()`` in that state raises ``Client is already terminated`` and
    leaves the storage handle alive.  Tear down each state transition directly
    so normal, partial, and repeated shutdowns are all safe.
    """
    failures: list[Exception] = []
    storage = getattr(client, "storage", None)
    was_connected = bool(getattr(client, "is_connected", False))

    if bool(getattr(client, "is_initialized", False)):
        try:
            await client.terminate()
        except Exception as ex:
            failures.append(ex)

    session = getattr(client, "session", None)
    if session is not None:
        try:
            result = session.stop()
            if inspect.isawaitable(result):
                await result
        except Exception as ex:
            failures.append(ex)
        finally:
            try:
                client.session = None
            except Exception:
                pass

    # A failed storage.open() leaves ``conn`` populated while the client never
    # reaches the connected state.  A connected in-memory client has no ``conn``
    # attribute, but its storage still receives one close call.
    if storage is not None:
        connection = getattr(storage, "conn", None)
        if was_connected or connection is not None:
            try:
                result = storage.close()
                if inspect.isawaitable(result):
                    await result
            except Exception as ex:
                failures.append(ex)
            else:
                # Pyrogram's SQLiteStorage.close() does not clear this reference.
                try:
                    storage.conn = None
                except Exception:
                    pass

    try:
        client.is_connected = False
    except Exception:
        pass

    return failures


class Bot(pyrogram.Client):
    def __init__(self):
        super().__init__(
            name="AnonX_3",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            # Persist MTProto update state across process restarts.  With an
            # in-memory bot session, a restart can replay already handled
            # commands (including /restart and /play).
            in_memory=False,
            # Keep the persistent session beside the deployment, independent
            # of the launcher's current working directory.
            workdir=str(DEPLOY_ROOT),
            parse_mode=pyrogram.enums.ParseMode.HTML,
            max_concurrent_transmissions=7,
            link_preview_options=pyrogram.types.LinkPreviewOptions(is_disabled=True),
        )
        self.owner = config.OWNER_ID
        self.logger = config.LOGGER_ID
        # Custom filter: use the warmed in-memory set first, then refresh from
        # MongoDB on a miss. This keeps persisted sudo users working even if a
        # command arrives during cache refresh or another process changed the
        # sudo list.
        self.owners = pyrogram.filters.user(self.owner)
        self.bl_users = pyrogram.filters.user()
        self._sudo_ids: set[int] = {self.owner}
        self._exit_lock = asyncio.Lock()
        self._exit_complete = False

        async def sudo_filter(_, __, message):
            user_id = (
                message.from_user.id
                if message is not None and message.from_user is not None
                else 0
            )
            if not user_id:
                return False
            if user_id in self._sudo_ids:
                return True

            try:
                from AnonX_3 import db

                persisted_ids = set(await db.get_sudoers())
                persisted_ids.update(await db.get_owners())
            except Exception as ex:
                logger.warning(
                    "Sudo authorization refresh failed for user_id=%s: %s",
                    user_id,
                    ex,
                )
                return False

            if user_id in persisted_ids:
                self._sudo_ids.add(user_id)
                return True
            return False

        self.sudoers = pyrogram.filters.create(
            sudo_filter,
            "SudoFilter",
        )

    async def _set_command_menu(self):
        all_commands = [
            pyrogram.types.BotCommand("alive", "Check if the bot is alive"),
            pyrogram.types.BotCommand("start", "Start the bot"),
            pyrogram.types.BotCommand("play", "Play your desired music"),
            pyrogram.types.BotCommand("song", "Download audio from YouTube or TikTok"),
            pyrogram.types.BotCommand("vsong", "Download video with a thumbnail"),
            pyrogram.types.BotCommand("kick", "Kick a user from the group"),
            pyrogram.types.BotCommand("ban", "Ban a user from the group"),
            pyrogram.types.BotCommand("mute", "Mute a user in the group"),
            pyrogram.types.BotCommand("unmute", "Unmute a user in the group"),
            pyrogram.types.BotCommand("skip", "Skip the current music"),
            pyrogram.types.BotCommand("pause", "Pause the current playing stream"),
            pyrogram.types.BotCommand("resume", "Resume the paused stream"),
            pyrogram.types.BotCommand("end", "Clear the queue and end the stream"),
            pyrogram.types.BotCommand("autoplay", "Toggle auto-play of similar songs"),
            pyrogram.types.BotCommand("aidj", "Smart autoplay with selectable moods"),
            pyrogram.types.BotCommand("autoreact", "Toggle automatic message reactions"),
            pyrogram.types.BotCommand("autoreply", "Toggle automatic keyword replies"),
            pyrogram.types.BotCommand("reply", "Set keyword auto-reply (reply to message)"),
            pyrogram.types.BotCommand("unreply", "Remove a keyword auto-reply"),
            pyrogram.types.BotCommand("replies", "List auto-reply keywords"),
            pyrogram.types.BotCommand("check", "Toggle or check name changes"),
            pyrogram.types.BotCommand("logger", "Toggle playback logging"),
            pyrogram.types.BotCommand("musiclog", "Toggle music status log reports"),
            pyrogram.types.BotCommand("queue", "See the list of music in queue"),
            pyrogram.types.BotCommand("auth", "Add a user to the auth list"),
            pyrogram.types.BotCommand("unauth", "Remove a user from the auth list"),
            pyrogram.types.BotCommand("authusers", "Show the list of auth users"),
            pyrogram.types.BotCommand("afk", "Set your status as AFK"),
            pyrogram.types.BotCommand("help", "Get the help menu with explanations"),
            pyrogram.types.BotCommand("filter", "Manage keyword filters (admin)"),
            pyrogram.types.BotCommand("id", "🔭 ID CHECKER — username & ID (reply)"),
        ]
        private_commands = [
            pyrogram.types.BotCommand("alive", "Check if the bot is alive"),
            pyrogram.types.BotCommand("start", "Start the bot"),
            pyrogram.types.BotCommand("song", "Download audio from YouTube or TikTok"),
            pyrogram.types.BotCommand("vsong", "Download video with a thumbnail"),
            pyrogram.types.BotCommand("afk", "Set your status as AFK"),
            pyrogram.types.BotCommand("help", "Get the help menu with explanations"),
            pyrogram.types.BotCommand("musiclog", "Toggle music status log reports"),
            pyrogram.types.BotCommand("filter", "Manage keyword filters"),
            pyrogram.types.BotCommand("id", "🔭 ID CHECKER — username & ID (reply)"),
        ]
        group_commands = [
            pyrogram.types.BotCommand("alive", "Check if the bot is alive"),
            pyrogram.types.BotCommand("start", "Start the bot"),
            pyrogram.types.BotCommand("play", "Play your desired music"),
            pyrogram.types.BotCommand("song", "Download audio from YouTube or TikTok"),
            pyrogram.types.BotCommand("vsong", "Download video with a thumbnail"),
            pyrogram.types.BotCommand("kick", "Kick a user from the group"),
            pyrogram.types.BotCommand("ban", "Ban a user from the group"),
            pyrogram.types.BotCommand("mute", "Mute a user in the group"),
            pyrogram.types.BotCommand("unmute", "Unmute a user in the group"),
            pyrogram.types.BotCommand("skip", "Skip the current music"),
            pyrogram.types.BotCommand("pause", "Pause the current playing stream"),
            pyrogram.types.BotCommand("resume", "Resume the paused stream"),
            pyrogram.types.BotCommand("end", "Clear the queue and end the stream"),
            pyrogram.types.BotCommand("autoplay", "Toggle auto-play of similar songs"),
            pyrogram.types.BotCommand("aidj", "Smart autoplay with selectable moods"),
            pyrogram.types.BotCommand("autoreact", "Toggle automatic message reactions"),
            pyrogram.types.BotCommand("autoreply", "Toggle automatic keyword replies"),
            pyrogram.types.BotCommand("reply", "Set keyword auto-reply (reply to message)"),
            pyrogram.types.BotCommand("unreply", "Remove a keyword auto-reply"),
            pyrogram.types.BotCommand("replies", "List auto-reply keywords"),
            pyrogram.types.BotCommand("check", "Toggle or check name changes"),
            pyrogram.types.BotCommand("logger", "Toggle playback logging"),
            pyrogram.types.BotCommand("queue", "See the list of music in queue"),
            pyrogram.types.BotCommand("afk", "Set your status as AFK"),
            pyrogram.types.BotCommand("help", "Get the help menu with explanations"),
            pyrogram.types.BotCommand("filter", "Manage keyword filters (admin)"),
            pyrogram.types.BotCommand("id", "🔭 ID CHECKER — username & ID (reply)"),
        ]
        admin_commands = [
            pyrogram.types.BotCommand("auth", "Add a user to the auth list"),
            pyrogram.types.BotCommand("unauth", "Remove a user from the auth list"),
            pyrogram.types.BotCommand("authusers", "Show the list of auth users"),
            pyrogram.types.BotCommand("kick", "Kick a user from the group"),
            pyrogram.types.BotCommand("ban", "Ban a user from the group"),
            pyrogram.types.BotCommand("mute", "Mute a user in the group"),
            pyrogram.types.BotCommand("unmute", "Unmute a user in the group"),
            pyrogram.types.BotCommand("autoplay", "Toggle auto-play of similar songs"),
            pyrogram.types.BotCommand("aidj", "Smart autoplay with selectable moods"),
            pyrogram.types.BotCommand("autoreact", "Toggle automatic message reactions"),
            pyrogram.types.BotCommand("autoreply", "Toggle automatic keyword replies"),
            pyrogram.types.BotCommand("check", "Toggle or check name changes"),
            pyrogram.types.BotCommand("logger", "Toggle playback logging"),
            pyrogram.types.BotCommand("musiclog", "Toggle music status log reports"),
            pyrogram.types.BotCommand("filter", "Manage keyword filters"),
            pyrogram.types.BotCommand("id", "🔭 ID CHECKER — username & ID (reply)"),
        ]

        await self.set_bot_commands(all_commands)
        await self.set_bot_commands(
            private_commands,
            scope=pyrogram.types.BotCommandScopeAllPrivateChats(),
        )
        await self.set_bot_commands(
            group_commands,
            scope=pyrogram.types.BotCommandScopeAllGroupChats(),
        )
        await self.set_bot_commands(
            admin_commands,
            scope=pyrogram.types.BotCommandScopeAllChatAdministrators(),
        )

    @staticmethod
    def _logger_admin_statuses() -> set:
        statuses = {
            pyrogram.enums.ChatMemberStatus.ADMINISTRATOR,
            pyrogram.enums.ChatMemberStatus.OWNER,
        }
        # Backward compatibility with old enum naming, if present.
        creator = getattr(pyrogram.enums.ChatMemberStatus, "CREATOR", None)
        if creator is not None:
            statuses.add(creator)
        return statuses

    @staticmethod
    def _is_channel_invalid(ex: Exception) -> bool:
        text = str(ex).upper()
        return "CHANNEL_INVALID" in text or "PEER_ID_INVALID" in text

    async def _recover_logger_peer(self) -> None:
        """
        Best-effort logger peer recovery for MTProto cache misses.

        Bots can access chat_id via Bot API while MTProto may still fail to resolve
        a private supergroup/channel peer. We warm up peer data via invite link.
        """
        from AnonX_3 import bot_api, logger

        try:
            chat = await bot_api._request("getChat", {"chat_id": self.logger})
        except Exception as ex:
            logger.warning("Logger recovery getChat failed for %s: %s", self.logger, ex)
            return

        invite_link = None
        if isinstance(chat, dict):
            invite_link = chat.get("invite_link")
        if not isinstance(invite_link, str) or not invite_link:
            return

        try:
            await self.join_chat(invite_link)
        except errors.UserAlreadyParticipant:
            pass
        except Exception as ex:
            logger.warning("Logger recovery join_chat failed for %s: %s", self.logger, ex)

    @staticmethod
    def _consume_daily_restart_notice() -> str | None:
        restart_at = os.environ.pop("ANONX_DAILY_RESTART_AT", "").strip()
        restart_tz = os.environ.pop("ANONX_DAILY_RESTART_TZ", "").strip()
        if not restart_at:
            return None
        return f"Daily auto-restart started at {restart_at} ({restart_tz or 'Asia/Yangon'})."

    async def _check_logger_access(self):
        """
        Validate log-group access with MTProto, with recovery fallback for CHANNEL_INVALID.
        """
        from AnonX_3 import bot_api

        restart_notice = self._consume_daily_restart_notice()

        async def _send_startup_messages():
            if restart_notice:
                await self.send_message(self.logger, restart_notice)
            await self.send_message(self.logger, "Bot Started")
            return await self.get_chat_member(self.logger, self.id)

        try:
            return await _send_startup_messages()
        except Exception as ex:
            if self._is_channel_invalid(ex):
                await self._recover_logger_peer()
                try:
                    return await _send_startup_messages()
                except Exception:
                    pass
            # Final fallback: verify via Bot API so startup doesn't fail on MTProto-only peer issues.
            try:
                result = await bot_api._request(
                    "getChatMember",
                    {"chat_id": self.logger, "user_id": self.id},
                )
            except Exception:
                raise ex
            status = (result or {}).get("status")
            if status in {"administrator", "creator"}:
                return None
            raise ex

    async def boot(self):
        """
        Starts the bot and performs initial setup.

        Raises:
            SystemExit: If the bot fails to access the log group or is not an administrator in the logger group.
        """
        await super().start()
        self.id = self.me.id
        self.name = self.me.first_name
        self.username = self.me.username
        self.mention = self.me.mention

        try:
            member = await self._check_logger_access()
        except Exception as ex:
            raise SystemExit(f"Bot has failed to access the log group: {self.logger}\nReason: {ex}")

        if member is not None and member.status not in self._logger_admin_statuses():
            raise SystemExit("Please promote the bot as an admin in logger group.")
        try:
            await self._set_command_menu()
        except Exception as ex:
            logger.warning("Failed to set bot command menu: %s", ex)
        logger.info(f"Bot started as @{self.username}")

    async def exit(self):
        """
        Asynchronously stops the bot.
        """
        async with self._exit_lock:
            if self._exit_complete:
                return
            self._exit_complete = True
            failures = await shutdown_pyrogram_client(self)
            for ex in failures:
                logger.warning("Bot shutdown step failed: %s", ex)
            if failures:
                raise ExceptionGroup("Bot shutdown failed", failures)
            logger.info("Bot stopped.")
