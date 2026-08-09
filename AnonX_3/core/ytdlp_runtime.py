# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Process-wide construction guard for yt-dlp runtimes.

yt-dlp's first ``YoutubeDL`` constructor loads external plugins into global
registries.  That bootstrap is not thread-safe: concurrent cold constructors
can execute the same provider decorators more than once.  Only the first
constructor needs serialization; all later constructors and every extraction
remain fully concurrent.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


_first_constructor_lock = threading.Lock()
_first_constructor_ready = threading.Event()


def create_youtube_dl(
    options: dict,
    constructor: Callable[[dict], Any],
):
    """Create one ``YoutubeDL`` instance after a single-flight cold bootstrap."""
    if _first_constructor_ready.is_set():
        return constructor(options)

    with _first_constructor_lock:
        if not _first_constructor_ready.is_set():
            instance = constructor(options)
            # Publish readiness only after the constructor (and therefore
            # yt-dlp's process-global plugin load) completed successfully.
            _first_constructor_ready.set()
            return instance

    # The waiting constructors leave the lock before doing their normal work,
    # preserving startup-warm parallelism after the one global bootstrap.
    return constructor(options)
