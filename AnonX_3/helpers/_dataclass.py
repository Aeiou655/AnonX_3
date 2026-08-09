# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from dataclasses import dataclass, field
from time import time as unix_time
from uuid import uuid4


def new_request_id() -> str:
    return uuid4().hex


@dataclass
class Media:
    id: str
    duration: str = "00:00"
    duration_sec: int = 0
    file_path: str = None
    message_id: int = 0
    title: str = None
    url: str = None
    time: int = 0
    user: str = None
    thumbnail: str = None
    video: bool = False
    source: str = None
    telegram_file_id: str = None
    local_path: str = None
    request_id: str = field(default_factory=new_request_id)
    chat_id: int = 0
    user_id: int = 0
    original_query: str = None
    normalized_query: str = None
    request_source: str = "command"
    priority: int = 50
    requested_at: float = field(default_factory=unix_time)
    candidate_sources: list[str] = field(default_factory=list)
    selected_source: str = None
    backup_source: str = None
    cache_key: str = None
    retry_count: int = 0
    cancelled: bool = False
    feature_flags: dict = field(default_factory=dict)
    # Appended to preserve the positional constructor order used by older
    # plugins while carrying Bot API versus MTProto routing metadata.
    telegram_file_size: int = 0


@dataclass
class Track:
    id: str
    channel_name: str = None
    duration: str = "00:00"
    duration_sec: int = 0
    title: str = None
    url: str = None
    file_path: str = None
    message_id: int = 0
    time: int = 0
    thumbnail: str = None
    user: str = None
    view_count: str = None
    video: bool = False
    source: str = None
    request_id: str = field(default_factory=new_request_id)
    chat_id: int = 0
    user_id: int = 0
    original_query: str = None
    normalized_query: str = None
    request_source: str = "command"
    priority: int = 50
    requested_at: float = field(default_factory=unix_time)
    candidate_sources: list[str] = field(default_factory=list)
    selected_source: str = None
    backup_source: str = None
    cache_key: str = None
    retry_count: int = 0
    cancelled: bool = False
    feature_flags: dict = field(default_factory=dict)



