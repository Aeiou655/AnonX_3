# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
from importlib import import_module
from pyrogram import Client

from AnonX_3 import app, config, logger
from AnonX_3.core.bot import shutdown_pyrogram_client


class Userbot(Client):
    READY_TIMEOUT_SEC = 45.0

    def __init__(self):
        """
        Initializes the userbot with multiple clients.

        This method sets up clients for the userbot using dynamic session strings.
        Each client is assigned a deterministic name by assistant index.
        """
        self.clients = []
        self.assistants = []
        self._ready = asyncio.Event()
        self._startup_error = None
        self._exit_lock = asyncio.Lock()
        self._exit_complete = False
        for num, session in enumerate(config.ASSISTANT_SESSIONS, start=1):
            self.assistants.append(
                Client(
                    name=f"AnonXUB{num}",
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    session_string=session,
                    in_memory=True,
                )
            )
        self.one = self.assistants[0] if len(self.assistants) > 0 else None
        self.two = self.assistants[1] if len(self.assistants) > 1 else None
        self.three = self.assistants[2] if len(self.assistants) > 2 else None

    async def boot_client(self, num: int, client: Client):
        """
        Boot a client and perform initial setup.
        Args:
            num (int): The 1-based client number to boot.
            client (Client): The userbot client instance.
        """
        await client.start()

        client.id = client.me.id
        client.name = client.me.first_name
        client.username = client.me.username
        client.mention = client.me.mention
        self.clients.append(client)
        logger.info("Assistant %s started as @%s", num, client.username)

        # Notify logger group via bot (reliable, avoids assistant FLOOD_WAIT).
        try:
            await app.send_message(config.LOGGER_ID, f"Assistant Started @{client.username}")
        except Exception:
            try:
                await client.send_message(config.LOGGER_ID, f"Assistant Started @{client.username}")
            except Exception as ex:
                invited = await self._auto_invite_to_log_group(num, client, ex)
                if invited:
                    try:
                        await client.send_message(config.LOGGER_ID, f"Assistant Started @{client.username}")
                    except Exception as retry_ex:
                        logger.warning(
                            "Assistant %s failed to send message in log group after auto-invite: %s",
                            num,
                            retry_ex,
                        )
                else:
                    logger.warning(
                        "Assistant %s cannot send to log group: %s",
                        num,
                        ex,
                    )

        try:
            await client.join_chat("fallenx")
        except Exception:
            pass

    async def _auto_invite_to_log_group(self, num: int, client: Client, reason: Exception) -> bool:
        logger.warning(
            "Assistant %s cannot send in log group %s. Auto-invite flow started. Reason: %s",
            num,
            config.LOGGER_ID,
            reason,
        )
        try:
            package = import_module(__name__.split(".")[0])
            app = package.app
        except Exception as ex:
            logger.warning("Assistant %s auto-invite skipped: bot app unavailable (%s)", num, ex)
            return False

        try:
            await app.add_chat_members(config.LOGGER_ID, client.me.id)
            logger.info("Assistant %s added to log group by bot.", num)
            return True
        except Exception as add_ex:
            logger.warning("Assistant %s direct add to log group failed: %s", num, add_ex)

        invite_link = None
        try:
            chat = await app.get_chat(config.LOGGER_ID)
            invite_link = getattr(chat, "invite_link", None)
        except Exception:
            invite_link = None
        if not invite_link:
            try:
                invite_link = await app.export_chat_invite_link(config.LOGGER_ID)
            except Exception as link_ex:
                logger.warning("Assistant %s cannot obtain log group invite link: %s", num, link_ex)
                return False

        try:
            await client.join_chat(invite_link)
            logger.info("Assistant %s joined log group via invite link.", num)
            return True
        except Exception as join_ex:
            logger.warning("Assistant %s failed to join log group via invite link: %s", num, join_ex)
            return False

    async def boot(self):
        """
        Asynchronously starts the assistants.
        """
        self._ready.clear()
        self._startup_error = None
        failed = []
        try:
            for num, client in enumerate(self.assistants, start=1):
                try:
                    await self.boot_client(num, client)
                except Exception as ex:
                    failed.append((num, ex))
                    logger.warning("Assistant %s skipped: %s", num, ex)

            if not self.clients:
                raise SystemExit(
                    "No assistant sessions could be started. "
                    "Please check SESSION / SESSION<n>."
                )

            if failed:
                logger.warning("Assistant startup completed with %s failure(s).", len(failed))
        finally:
            if failed:
                self._startup_error = failed[-1][1]
            self._ready.set()

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        """Wait for startup without allowing a command handler to exit the process."""
        if self.clients:
            return
        try:
            await asyncio.wait_for(
                self._ready.wait(),
                timeout=self.READY_TIMEOUT_SEC if timeout is None else timeout,
            )
        except asyncio.TimeoutError as ex:
            raise RuntimeError("Assistant clients are still starting; retry shortly.") from ex
        if not self.clients:
            raise RuntimeError(
                "No assistant userbot clients are available. "
                "Check SESSION / SESSION<n> and the preceding startup warning."
            ) from self._startup_error

    async def exit(self):
        """
        Asynchronously stops the assistants.
        """
        async with self._exit_lock:
            if self._exit_complete:
                return
            self._exit_complete = True

            # Include configured assistants that failed before boot_client()
            # appended them; storage.open() may already have allocated state.
            candidates = []
            seen: set[int] = set()
            for client in [*self.clients, *self.assistants]:
                identity = id(client)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(client)

            # Detach ownership before awaiting network cleanup.  A concurrent
            # exit therefore cannot stop any assistant twice, even on failure.
            self.clients.clear()
            self.assistants.clear()
            self.one = None
            self.two = None
            self.three = None
            self._ready.set()

            shutdown_failures: list[Exception] = []
            for idx, client in reversed(list(enumerate(candidates, start=1))):
                failures = await shutdown_pyrogram_client(client)
                for ex in failures:
                    logger.warning("Assistant %s shutdown step failed: %s", idx, ex)
                    shutdown_failures.append(ex)
            if shutdown_failures:
                raise ExceptionGroup(
                    "Assistant shutdown failed",
                    shutdown_failures,
                )
            logger.info("Assistants stopped.")
