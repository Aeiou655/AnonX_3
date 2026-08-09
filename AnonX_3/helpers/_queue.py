# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from collections import defaultdict, deque
from typing import Union

from ._dataclass import Media, Track

MediaItem = Union[Media, Track]


class Queue:
    def __init__(self):
        self.queues: dict[int, deque[MediaItem]] = defaultdict(deque)

    def add(self, chat_id: int, item: MediaItem) -> int:
        """Add an item, preserving FIFO while manual requests outrank AI DJ.

        Index 0 is the currently playing item and is never displaced.  A higher
        priority request is inserted before lower-priority waiting items only.
        """
        items = self.queues[chat_id]
        incoming_priority = int(getattr(item, "priority", 50) or 50)
        if items:
            for index in range(1, len(items)):
                queued_priority = int(getattr(items[index], "priority", 50) or 50)
                if incoming_priority > queued_priority:
                    items.insert(index, item)
                    return index
        items.append(item)
        return len(items) - 1

    def check_item(self, chat_id: int, item_id: str) -> tuple[int, MediaItem | None]:
        """Return (internal 0-based index, item) for the first matching ID."""
        pos, track = next(
            (
                (i, track)
                for i, track in enumerate(list(self.queues[chat_id]))
                if track.id == item_id
            ),
            (-1, None),
        )
        return pos, track

    def remove_request(self, chat_id: int, request_id: str) -> bool:
        """Remove exactly one queued item by its immutable request ID.

        Media IDs identify a source and can legitimately repeat in a queue.  A
        request ID instead belongs to one command invocation, so cancellation
        and failed-start rollback can safely remove only their own item.
        """
        if not request_id:
            return False
        items = self.queues.get(chat_id)
        if not items:
            return False
        for index, item in enumerate(items):
            if str(getattr(item, "request_id", "") or "") == str(request_id):
                del items[index]
                return True
        return False

    def force_add(
        self, chat_id: int, item: MediaItem, remove: int | bool = False
    ) -> None:
        """Replace the currently playing item with a new one."""
        self.remove_current(chat_id)
        self.queues[chat_id].appendleft(item)
        if remove and isinstance(remove, int) and remove > 0:
            self.queues[chat_id].rotate(-remove)
            self.queues[chat_id].popleft()
            self.queues[chat_id].rotate(remove)

    def get_current(self, chat_id: int) -> MediaItem | None:
        """Return the currently playing item (first in queue), if any."""
        return self.queues[chat_id][0] if self.queues[chat_id] else None

    def get_next(self, chat_id: int, check: bool = False) -> MediaItem | None:
        """Remove current item and return the next one, or None if empty."""
        if not self.queues[chat_id]:
            return None
        if check:
            return self.queues[chat_id][1] if len(self.queues[chat_id]) > 1 else None

        self.queues[chat_id].popleft()
        return self.queues[chat_id][0] if self.queues[chat_id] else None

    def get_queue(self, chat_id: int) -> list[MediaItem]:
        """Return the full queue including the currently playing item."""
        return list(self.queues[chat_id])

    def remove_current(self, chat_id: int) -> None:
        """Remove the currently playing item only (if exists)."""
        if self.queues[chat_id]:
            self.queues[chat_id].popleft()

    def clear(self, chat_id: int) -> None:
        """Clear the entire queue."""
        self.queues[chat_id].clear()
