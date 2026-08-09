# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""A status-message handoff that does not serialize playback on Telegram I/O."""

from __future__ import annotations

import asyncio
from typing import Any


class DeferredStatusMessage:
    """Proxy a command status card while its Telegram send is still in flight.

    Reading cheap message metadata never blocks the playback path.  Mutating
    operations wait for the bot-owned status message and are then forwarded to
    it, so an early failure cannot accidentally try to edit the user's command.
    """

    _PENDING_MARKUP = object()

    def __init__(self, source_message: Any):
        object.__setattr__(self, "_source_message", source_message)
        object.__setattr__(self, "_message", None)
        object.__setattr__(self, "_failure", None)
        object.__setattr__(self, "_pending_attributes", {})
        object.__setattr__(self, "_ready", asyncio.get_running_loop().create_future())

    @property
    def id(self) -> int:
        message = object.__getattribute__(self, "_message")
        return int(getattr(message, "id", 0) or 0) if message is not None else 0

    @property
    def chat(self):
        message = object.__getattribute__(self, "_message")
        if message is not None:
            return message.chat
        return object.__getattribute__(self, "_source_message").chat

    @property
    def link(self):
        message = object.__getattribute__(self, "_message")
        if message is not None:
            return getattr(message, "link", None)
        return getattr(object.__getattribute__(self, "_source_message"), "link", None)

    @property
    def reply_markup(self):
        message = object.__getattribute__(self, "_message")
        if message is not None:
            return getattr(message, "reply_markup", None)
        # The acknowledgement coroutine already sends the Cancel keyboard.
        # A non-None sentinel prevents a redundant markup edit while pending.
        return self._PENDING_MARKUP

    @property
    def is_bound(self) -> bool:
        return object.__getattribute__(self, "_message") is not None

    def bind(self, message: Any) -> None:
        if message is None:
            raise ValueError("deferred_status_missing_message")
        if object.__getattribute__(self, "_message") is not None:
            return
        object.__setattr__(self, "_message", message)
        pending = object.__getattribute__(self, "_pending_attributes")
        for name, value in tuple(pending.items()):
            try:
                setattr(message, name, value)
            except Exception:
                pass
        pending.clear()
        ready = object.__getattribute__(self, "_ready")
        if not ready.done():
            ready.set_result(True)

    def fail(self, error: BaseException) -> None:
        if object.__getattribute__(self, "_message") is not None:
            return
        object.__setattr__(self, "_failure", error)
        ready = object.__getattribute__(self, "_ready")
        if not ready.done():
            # Resolve normally and retain the exception ourselves.  This avoids
            # an un-retrieved Future exception when no UI mutation was needed.
            ready.set_result(False)

    async def wait(self):
        await asyncio.shield(object.__getattribute__(self, "_ready"))
        message = object.__getattribute__(self, "_message")
        if message is not None:
            return message
        failure = object.__getattribute__(self, "_failure")
        raise RuntimeError("play_status_ack_failed") from failure

    async def _forward(self, method: str, *args, **kwargs):
        message = await self.wait()
        callback = getattr(message, method)
        result = callback(*args, **kwargs)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            return await result
        return result

    async def edit_text(self, *args, **kwargs):
        return await self._forward("edit_text", *args, **kwargs)

    async def edit_caption(self, *args, **kwargs):
        return await self._forward("edit_caption", *args, **kwargs)

    async def edit_reply_markup(self, *args, **kwargs):
        return await self._forward("edit_reply_markup", *args, **kwargs)

    async def reply_text(self, *args, **kwargs):
        return await self._forward("reply_text", *args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._forward("delete", *args, **kwargs)

    def __getattr__(self, name: str):
        pending = object.__getattribute__(self, "_pending_attributes")
        if name in pending:
            return pending[name]
        message = object.__getattribute__(self, "_message")
        if message is not None:
            return getattr(message, name)
        return getattr(object.__getattribute__(self, "_source_message"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        message = object.__getattribute__(self, "_message")
        if message is not None:
            setattr(message, name, value)
            return
        object.__getattribute__(self, "_pending_attributes")[name] = value
