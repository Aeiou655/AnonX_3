# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import os
import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import filters, types

from AnonX_3 import (
    LOG_FILE_PATH,
    PROCESS_STOP_TIMEOUT_SEC,
    app,
    config,
    db,
    ensure_log_file,
    flush_log_handlers,
    lang,
    logger,
    reset_runtime_dirs,
    stop,
    write_log_snapshot_marker,
)
from AnonX_3.core.lifecycle import exec_fresh_process, resolve_package_name
from AnonX_3.helpers import utils


RUNTIME_TEXT_ALIASES = {
    "filter": "filter_warning",
    "play_pause": "play_paused",
    "play_resume": "play_resumed",
}


NAME_CHECKER_DEFAULT_TEXT = (
    "🔎 Name changed\n"
    "{2} ({3}) -> {5} ({6})\n"
    "ID: <code>{4}</code>\n"
    "Group: {0}"
)


RUNTIME_TEXT_CONFIG = {
    "filter_warning": {
        "fallback_value": (
            "🚫 Filter <code>{0}</code> ပါဝင်သဖြင့် {1} ရဲ့ message ကို "
            "ဖျက်လိုက်ပါပြီ။\n"
            "⚠️ Strike: <b>{2}/3</b>\n"
            "{3}"
        ),
        "usage_fallback": (
            "<b>Usage:</b>\n\nWarning message ကို reply လုပ်ပြီး "
            "<code>/{cmd} {key}</code> သုံးပါ။\n\n"
            "<b>Placeholders:</b>\n"
            "<code>{{0}}</code> = matched keyword\n"
            "<code>{{1}}</code> = user\n"
            "<code>{{2}}</code> = strike count\n"
            "<code>{{3}}</code> = mute status"
        ),
        "show_fallback": "<b>Current filter warning:</b>",
        "invalid_fallback": "Reply to a valid warning text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Filter Warning Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Filter Warning Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Filter Warning Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_media": {
        "fallback_lang_key": "play_media",
        "usage_lang_key": "playmedia_set_usage",
        "show_lang_key": "playmedia_set_show",
        "invalid_lang_key": "playmedia_set_invalid",
        "done_lang_key": "playmedia_set_done",
        "log_show_lang_key": "playmedia_set_log_show",
        "log_done_lang_key": "playmedia_set_log_done",
        "log_error_lang_key": "playmedia_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = media URL\n<code>{{1}}</code> = title\n"
            "<code>{{2}}</code> = duration\n<code>{{3}}</code> = requester"
        ),
        "show_fallback": "<b>Current started-streaming template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Media Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Media Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Media Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_queued": {
        "fallback_lang_key": "play_queued",
        "usage_lang_key": "playqueued_set_usage",
        "show_lang_key": "playqueued_set_show",
        "invalid_lang_key": "playqueued_set_invalid",
        "done_lang_key": "playqueued_set_done",
        "log_show_lang_key": "playqueued_set_log_show",
        "log_done_lang_key": "playqueued_set_log_done",
        "log_error_lang_key": "playqueued_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = queue position\n<code>{{1}}</code> = media URL\n"
            "<code>{{2}}</code> = title\n<code>{{3}}</code> = duration\n"
            "<code>{{4}}</code> = requester"
        ),
        "show_fallback": "<b>Current added-to-queue template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Queue Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Queue Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Queue Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "playlist_queued": {
        "fallback_lang_key": "playlist_queued",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n<code>{{0}}</code> = playlist track count"
        ),
        "show_fallback": "<b>Current playlist-queued template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Playlist Queue Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Playlist Queue Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Playlist Queue Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "queue_curr": {
        "fallback_lang_key": "queue_curr",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = media URL\n<code>{{1}}</code> = title\n"
            "<code>{{2}}</code> = duration\n<code>{{3}}</code> = requester"
        ),
        "show_fallback": "<b>Current queue-current template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Queue Current Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Queue Current Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Queue Current Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "queue_item": {
        "fallback_lang_key": "queue_item",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = queue position\n<code>{{1}}</code> = title\n"
            "<code>{{2}}</code> = duration"
        ),
        "show_fallback": "<b>Current queue-item template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Queue Item Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Queue Item Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Queue Item Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_next": {
        "fallback_lang_key": "play_next",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current next-in-queue template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Next Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Next Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Next Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_searching": {
        "fallback_lang_key": "play_searching",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current searching template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Searching Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Searching Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Searching Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_downloading": {
        "fallback_lang_key": "play_downloading",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current downloading template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Downloading Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Downloading Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Downloading Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_not_found": {
        "fallback_lang_key": "play_not_found",
        "usage_lang_key": "playnotfound_set_usage",
        "show_lang_key": "playnotfound_set_show",
        "invalid_lang_key": "playnotfound_set_invalid",
        "done_lang_key": "playnotfound_set_done",
        "log_show_lang_key": "playnotfound_set_log_show",
        "log_done_lang_key": "playnotfound_set_log_done",
        "log_error_lang_key": "playnotfound_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = support chat URL"
        ),
        "show_fallback": "<b>Current play-not-found template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Not Found Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Not Found Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Not Found Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "error_no_file": {
        "fallback_lang_key": "error_no_file",
        "usage_lang_key": "errornofile_set_usage",
        "show_lang_key": "errornofile_set_show",
        "invalid_lang_key": "errornofile_set_invalid",
        "done_lang_key": "errornofile_set_done",
        "log_show_lang_key": "errornofile_set_log_show",
        "log_done_lang_key": "errornofile_set_log_done",
        "log_error_lang_key": "errornofile_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = support chat URL"
        ),
        "show_fallback": "<b>Current error-no-file template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Error No File Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Error No File Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Error No File Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_paused": {
        "fallback_lang_key": "play_paused",
        "usage_lang_key": "playpause_set_usage",
        "show_lang_key": "playpause_set_show",
        "invalid_lang_key": "playpause_set_invalid",
        "done_lang_key": "playpause_set_done",
        "log_show_lang_key": "playpause_set_log_show",
        "log_done_lang_key": "playpause_set_log_done",
        "log_error_lang_key": "playpause_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention"
        ),
        "show_fallback": "<b>Current paused template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Paused Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Paused Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Paused Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_resumed": {
        "fallback_lang_key": "play_resumed",
        "usage_lang_key": "playresumed_set_usage",
        "show_lang_key": "playresumed_set_show",
        "invalid_lang_key": "playresumed_set_invalid",
        "done_lang_key": "playresumed_set_done",
        "log_show_lang_key": "playresumed_set_log_show",
        "log_done_lang_key": "playresumed_set_log_done",
        "log_error_lang_key": "playresumed_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention"
        ),
        "show_fallback": "<b>Current resumed template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Resumed Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Resumed Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Resumed Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "paused_status": {
        "fallback_lang_key": "paused",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current paused status text:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Paused Status Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Paused Status Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Paused Status Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "playing_status": {
        "fallback_lang_key": "playing",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current playing status text:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Playing Status Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Playing Status Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Playing Status Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_skipped": {
        "fallback_lang_key": "play_skipped",
        "usage_lang_key": "playskipped_set_usage",
        "show_lang_key": "playskipped_set_show",
        "invalid_lang_key": "playskipped_set_invalid",
        "done_lang_key": "playskipped_set_done",
        "log_show_lang_key": "playskipped_set_log_show",
        "log_done_lang_key": "playskipped_set_log_done",
        "log_error_lang_key": "playskipped_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention"
        ),
        "show_fallback": "<b>Current skipped template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Skipped Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Skipped Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Skipped Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_stopped": {
        "fallback_lang_key": "play_stopped",
        "usage_lang_key": "playstopped_set_usage",
        "show_lang_key": "playstopped_set_show",
        "invalid_lang_key": "playstopped_set_invalid",
        "done_lang_key": "playstopped_set_done",
        "log_show_lang_key": "playstopped_set_log_show",
        "log_done_lang_key": "playstopped_set_log_done",
        "log_error_lang_key": "playstopped_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention"
        ),
        "show_fallback": "<b>Current stopped template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Stopped Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Stopped Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Stopped Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "play_replayed": {
        "fallback_lang_key": "play_replayed",
        "usage_lang_key": "playreplayed_set_usage",
        "show_lang_key": "playreplayed_set_show",
        "invalid_lang_key": "playreplayed_set_invalid",
        "done_lang_key": "playreplayed_set_done",
        "log_show_lang_key": "playreplayed_set_log_show",
        "log_done_lang_key": "playreplayed_set_log_done",
        "log_error_lang_key": "playreplayed_set_log_error",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention"
        ),
        "show_fallback": "<b>Current replayed template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Play Replayed Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Play Replayed Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Play Replayed Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "autoplay_on": {
        "fallback_lang_key": "autoplay_on",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current autoplay-on template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Autoplay ON Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Autoplay ON Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Autoplay ON Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "autoplay_off": {
        "fallback_lang_key": "autoplay_off",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current autoplay-off template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Autoplay OFF Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Autoplay OFF Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Autoplay OFF Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "autoplay_no_match": {
        "fallback_lang_key": "autoplay_no_match",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current autoplay no-match template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Autoplay No-Match Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Autoplay No-Match Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Autoplay No-Match Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "autoremove_deleted_notice": {
        "fallback_lang_key": "autoremove_deleted_notice",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user mention\n<code>{{1}}</code> = matched keyword"
        ),
        "show_fallback": "<b>Current auto-remove delete notice template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Auto-Remove Delete Notice Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Auto-Remove Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Auto-Remove Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "link_deleted_notice": {
        "fallback_lang_key": "link_deleted_notice",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = bot name\n<code>{{1}}</code> = user mention"
        ),
        "show_fallback": "<b>Current link-delete notice template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Link Delete Notice Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Link Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Link Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "forward_deleted_notice": {
        "fallback_lang_key": "forward_deleted_notice",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = bot name\n<code>{{1}}</code> = user mention"
        ),
        "show_fallback": "<b>Current forward-delete notice template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Forward Delete Notice Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Forward Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Forward Delete Notice Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "error_flood_wait": {
        "fallback_lang_key": "error_flood_wait",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = wait seconds"
        ),
        "show_fallback": "<b>Current flood-wait template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Flood Wait Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Flood Wait Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Flood Wait Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "sudo_list": {
        "fallback_lang_key": "sudo_list",
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = owner mention\n<code>{{1}}</code> = sudo list lines"
        ),
        "show_fallback": "<b>Current sudo-list template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Sudo List Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Sudo List Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Sudo List Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "name_checker": {
        "fallback_value": NAME_CHECKER_DEFAULT_TEXT,
        "usage_fallback": (
            "<b>Usage:</b>\n\nReply to a text message with <code>/{cmd} {key}</code>."
            "\n\n<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = group title\n"
            "<code>{{1}}</code> = group ID\n"
            "<code>{{2}}</code> = old name\n"
            "<code>{{3}}</code> = old username\n"
            "<code>{{4}}</code> = user ID\n"
            "<code>{{5}}</code> = new name\n"
            "<code>{{6}}</code> = new username\n"
            "<code>{{7}}</code> = user ID"
        ),
        "show_fallback": "<b>Current name-checker template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>Name Checker Template</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>Name Checker Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>Name Checker Update</b></u>\n\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_user": {
        "fallback_lang_key": "id_user",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>.\n"
            "\n"
            "<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user ID\n"
            "<code>{{1}}</code> = username\n"
            "<code>{{2}}</code> = name\n"
            "<code>{{3}}</code> = group"
        ),
        "show_fallback": "<b>Current /id user template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID User Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID User Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID User Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_chat": {
        "fallback_lang_key": "id_chat",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>.\n"
            "\n"
            "<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = chat ID\n"
            "<code>{{1}}</code> = username\n"
            "<code>{{2}}</code> = title"
        ),
        "show_fallback": "<b>Current /id chat template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID Chat Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID Chat Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID Chat Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_self": {
        "fallback_lang_key": "id_self",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>.\n"
            "\n"
            "<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = user ID\n"
            "<code>{{1}}</code> = username\n"
            "<code>{{2}}</code> = name/mention"
        ),
        "show_fallback": "<b>Current /id self template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID Self Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID Self Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID Self Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_this_chat": {
        "fallback_lang_key": "id_this_chat",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>.\n"
            "\n"
            "<b>Supported placeholders:</b>\n"
            "<code>{{0}}</code> = chat ID\n"
            "<code>{{1}}</code> = chat type"
        ),
        "show_fallback": "<b>Current /id this-chat template:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID This Chat Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID This Chat Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID This Chat Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_usage": {
        "fallback_lang_key": "id_usage",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current /id usage text:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID Usage Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID Usage Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID Usage Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
    "id_no_target": {
        "fallback_lang_key": "id_no_target",
        "usage_fallback": (
            "<b>Usage:</b>\n"
            "\n"
            "Reply to a text message with <code>/{cmd} {key}</code>."
        ),
        "show_fallback": "<b>Current /id no-target text:</b>",
        "invalid_fallback": "Reply to a valid text message.",
        "done_fallback": "Updated <code>/{0} {1}</code> successfully.",
        "log_show_fallback": (
            "<u><b>ID No Target Template</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Shown"
        ),
        "log_done_fallback": (
            "<u><b>ID No Target Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Success"
        ),
        "log_error_fallback": (
            "<u><b>ID No Target Update</b></u>\n"
            "\n"
            "<b>User:</b> <code>{0}</code> | {1}\n"
            "<b>Command:</b> <code>/{2}</code>\n"
            "<b>Status:</b> Error\n"
            "<b>Reason:</b> <code>{3}</code>"
        ),
    },
}

RESETTABLE_RUNTIME_TEXT_KEYS = (
    "error_flood_wait",
    "filter_warning",
    "play_media",
    "play_queued",
    "playlist_queued",
    "queue_curr",
    "queue_item",
    "play_next",
    "play_searching",
    "play_downloading",
    "play_paused",
    "play_resumed",
    "paused_status",
    "playing_status",
    "play_skipped",
    "play_stopped",
    "play_replayed",
    "autoplay_on",
    "autoplay_off",
    "autoplay_no_match",
    "link_deleted_notice",
    "forward_deleted_notice",
    "autoremove_deleted_notice",
    "sudo_list",
    "name_checker",
    "id_user",
    "id_chat",
    "id_self",
    "id_this_chat",
    "id_usage",
    "id_no_target",
)


def resolve_runtime_text_key(key: str) -> str:
    return RUNTIME_TEXT_ALIASES.get(key, key)


def supported_runtime_text_keys() -> tuple[str, ...]:
    return tuple(RUNTIME_TEXT_CONFIG.keys()) + tuple(RUNTIME_TEXT_ALIASES.keys())


def supported_template_text_keys() -> tuple[str, ...]:
    keys = ["start_pm"]
    for item in supported_runtime_text_keys():
        if item not in keys:
            keys.append(item)
    return tuple(keys)


def settext_usage_text(command_name: str) -> str:
    runtime_keys = ", ".join(f"<code>{key}</code>" for key in supported_runtime_text_keys())
    all_keys = ", ".join(f"<code>{key}</code>" for key in supported_template_text_keys())
    return (
        f"<b>Usage:</b> <code>/{command_name} [key]</code>\n\n"
        f"<b>Runtime text keys:</b>\n{runtime_keys}\n\n"
        f"<b>All template keys:</b>\n{all_keys}"
    )


def format_runtime_text_preview(
    message: types.Message,
    show_text: str,
    source: str,
) -> str:
    source_name = message.lang.get(
        f"settext_source_{source}",
        "Custom override" if source == "custom" else "Locale default",
    )
    source_text = message.lang.get(
        "settext_source", "<b>Source:</b> {0}"
    ).format(source_name)
    return f"{show_text}\n{source_text}"


def format_button_text_preview(
    message: types.Message,
    key: str,
    source: str,
) -> str:
    return format_runtime_text_preview(
        message,
        f"<b>Current button text for <code>{key}</code>:</b>",
        source,
    )


def default_button_text_preview(message: types.Message, key: str) -> str:
    if key.startswith("help_item_"):
        index = key.removeprefix("help_item_")
        return message.lang.get(f"help_{index}", key)

    defaults = {
        "cancel_dl": message.lang.get("cancel", "Cancel"),
        "controls_status": message.lang.get("playing", "Playing"),
        "controls_resume": "▷",
        "controls_pause": "II",
        "controls_replay": "⥁",
        "controls_skip": "‣‣I",
        "controls_stop": "▢",
        "filter_mute": "Mute",
        "filter_unmute": "Unmute",
        "help_back": message.lang.get("back", "Back"),
        "help_close": message.lang.get("close", "Close"),
        "lang_active": f'{message.lang.get("language", "Language")} (en) ✔️',
        "lang_inactive": f'{message.lang.get("language", "Language")} (en)',
        "ping_support": message.lang.get("support", "Support"),
        "play_queued_close": message.lang.get("close", "Close"),
        "queue_toggle_pause": message.lang.get("playing", "Playing"),
        "queue_toggle_resume": message.lang.get("paused", "Paused"),
        "settings_play_mode_label": f'{message.lang.get("play_mode", "Admin only play")} ➜',
        "settings_play_mode_value": "False",
        "settings_cmd_delete_label": f'{message.lang.get("cmd_delete", "Command delete")} ➜',
        "settings_cmd_delete_value": "False",
        "settings_autoplay_label": f'{message.lang.get("autoplay", "Autoplay")} ➜',
        "settings_autoplay_value": "OFF",
        "settings_language_label": f'{message.lang.get("language", "Language")} ➜',
        "settings_language_value": "English",
        "start_add_me": message.lang.get("add_me", "Add me to your group"),
        "start_help": message.lang.get("help", "Help"),
        "start_support": message.lang.get("support", "Support"),
        "start_channel": message.lang.get("channel", "Channel"),
        "start_source": message.lang.get("source", "Owner"),
        "start_language": message.lang.get("language", "Language"),
        "yt_copy": "❐",
        "yt_open": "Youtube",
    }
    return defaults.get(key, key)


def setbuttontext_usage_text(command_name: str, key: str, supported: str) -> str:
    return (
        f"<b>Usage:</b> <code>/{command_name} {key}</code> (reply to text)\n"
        f"<b>Reset:</b> <code>/{command_name} {key} default</code>\n\n"
        f"<b>Supported keys:</b> {supported}"
    )


def extract_reply_text_value(
    reply: types.Message,
) -> tuple[str | None, list[dict]]:
    text = getattr(reply, "text", None) or getattr(reply, "caption", None)
    if not isinstance(text, str) or not text.strip():
        return None, []
    raw_entities = (
        getattr(reply, "entities", None)
        if getattr(reply, "text", None)
        else getattr(reply, "caption_entities", None)
    )
    entities = utils.serialize_entities(raw_entities or [])
    return text, entities


async def reset_named_runtime_text(
    message: types.Message,
    key: str,
    display_key: str | None = None,
) -> None:
    display_key = display_key or key
    if key not in RESETTABLE_RUNTIME_TEXT_KEYS:
        supported = ", ".join(
            f"<code>{item}</code>" for item in RESETTABLE_RUNTIME_TEXT_KEYS
        )
        return await message.reply_text(
            message.lang.get(
                "settext_reset_unsupported",
                "Reset is only supported for: {0}.",
            ).format(supported)
        )

    if not await db.has_custom_text(key):
        return await message.reply_text(
            message.lang.get(
                "settext_reset_missing",
                "Repo default template is already active for <code>{0}</code>.",
            ).format(display_key)
        )

    await db.delete_custom_text(key)
    await message.reply_text(
        message.lang.get(
            "settext_reset_done",
            "Cleared custom override for <code>{0}</code>. Repo default template is active again.",
        ).format(display_key)
    )


@app.on_message(filters.command(["logs"]), group=-1)
@lang.language()
async def _logs(_, m: types.Message):
    if not m.from_user:
        return

    uid = m.from_user.id
    authorized = uid == config.OWNER_ID or uid in app._sudo_ids
    if not authorized:
        try:
            db_sudoers = await db.get_sudoers()
            db_owners = await db.get_owners()
            authorized = uid in db_sudoers or uid in db_owners
        except Exception as ex:
            logger.warning("Failed to refresh /logs authorization for user_id=%s: %s", uid, ex)

    if not authorized:
        await m.reply_text("⛔ Owner / Sudo users only.")
        return

    app._sudo_ids.add(uid)

    sent = await m.reply_text(m.lang["log_fetch"])
    flush_log_handlers()
    log_path = _resolve_log_file_path()
    if not log_path or not os.path.exists(log_path):
        return await sent.edit_text(m.lang["log_not_found"])
    try:
        file_size = os.path.getsize(log_path)
    except OSError:
        return await sent.edit_text(m.lang["log_not_found"])
    if file_size == 0:
        try:
            log_path = write_log_snapshot_marker(
                log_path,
                reason="Log snapshot requested while file was empty.",
            )
            flush_log_handlers()
            file_size = os.path.getsize(log_path)
        except OSError:
            return await sent.edit_text(m.lang.get("log_empty", "Log file is empty."))
        if file_size == 0:
            return await sent.edit_text(m.lang.get("log_empty", "Log file is empty."))
    try:
        await sent.edit_media(
            media=types.InputMediaDocument(
                media=log_path,
                caption=m.lang["log_sent"].format(app.name),
            )
        )
    except Exception as ex:
        logger.warning(
            "Failed to send log file to chat_id=%s user_id=%s: %s",
            m.chat.id,
            m.from_user.id if m.from_user else 0,
            ex,
        )
        await sent.edit_text(
            m.lang.get("log_empty", "Log file is empty or inaccessible.")
        )


@app.on_message(filters.command(["logger"]) & app.sudoers, group=-1)
@lang.language()
async def _logger(_, m: types.Message):
    if m.from_user:
        uid = m.from_user.id
        if uid != config.OWNER_ID and uid not in app._sudo_ids:
            db_sudoers = await db.get_sudoers()
            if uid not in db_sudoers and uid not in (await db.get_owners()):
                await m.reply_text("⛔ Owner / Sudo users only.")
                return

    if len(m.command) < 2:
        return await m.reply_text(m.lang["logger_usage"].format(m.command[0]))
    if m.command[1] not in ("on", "off"):
        return await m.reply_text(m.lang["logger_usage"].format(m.command[0]))

    if m.command[1] == "on":
        await db.set_logger(True)
        await m.reply_text(m.lang["logger_on"])
    else:
        await db.set_logger(False)
        await m.reply_text(m.lang["logger_off"])


@app.on_message(
    filters.command(["musiclog"])
    & (filters.private | filters.chat(app.logger))
    & ~app.bl_users,
    group=-1,
)
async def _musiclog(_, m: types.Message):
    if not m.from_user or m.from_user.id != app.owner:
        return await m.reply_text("Owner only.")
    if len(m.command) < 2 or m.command[1] not in ("on", "off"):
        state = "on" if await db.get_music_status_report() else "off"
        return await m.reply_text(
            "<b>Usage:</b> <code>/musiclog on</code> or <code>/musiclog off</code>\n"
            f"<b>Current:</b> <code>{state}</code>"
        )

    enabled = m.command[1] == "on"
    await db.set_music_status_report(enabled)
    text = (
        "Music status log reports ဖွင့်ထားပါပြီ။"
        if enabled
        else "Music status log reports ပိတ်ထားပါပြီ။"
    )
    await m.reply_text(text)


async def set_bot_image(message: types.Message, key: str) -> None:
    defaults = {
        "default_thumb": config.DEFAULT_THUMB,
        "ping_img": config.PING_IMG,
        "start_img": config.START_IMG,
    }
    if key == "start_img" and len(message.command) >= 2:
        mode = message.command[1].strip().lower()
        if mode in {"off", "disable", "disabled"}:
            await db.set_bot_image(key, "__disabled__")
            return await message.reply_text(
                f"{message.lang['image_set_done'].format(message.command[0])}\n"
                "<code>start image: off</code>"
            )
        if mode in {"on", "enable", "enabled"}:
            default_value = defaults.get(key) or ""
            await db.set_bot_image(key, default_value)
            return await message.reply_text(
                f"{message.lang['image_set_done'].format(message.command[0])}\n"
                "<code>start image: on</code>"
            )
    if not message.reply_to_message:
        default_value = defaults.get(key)
        if default_value:
            await db.set_bot_image(key, default_value)
            return await message.reply_text(
                f"{message.lang['image_set_done'].format(message.command[0])}\n"
                f"<code>{default_value}</code>"
            )
        return await message.reply_text(
            message.lang["image_set_usage"].format(message.command[0])
        )

    photo = getattr(message.reply_to_message, "photo", None)
    if not photo:
        return await message.reply_text(message.lang["image_set_invalid"])

    await db.set_bot_image(key, photo.file_id)
    await message.reply_text(
        message.lang["image_set_done"].format(message.command[0])
    )


async def set_custom_text(message: types.Message, key: str) -> None:
    lang_code = await db.get_lang(message.chat.id)
    if len(message.command) >= 2 and message.command[1].strip().lower() in {"default", "reset"}:
        return await reset_named_runtime_text(message, key, key)

    if not message.reply_to_message:
        template = await db.get_custom_text(key, message.lang["start_pm"], lang_code)
        preview_template = await utils.preview_template(
            template, key, lang_code=lang_code
        )
        await message.reply_text(message.lang["welcome_set_show"])
        if isinstance(preview_template, dict):
            await utils.reply_text(
                message,
                text=preview_template["text"],
                entities=preview_template.get("entities"),
                disable_web_page_preview=False,
                link_preview_options={"is_disabled": False, "show_above_text": True},
            )
        else:
            await utils.reply_text(
                message,
                text=preview_template,
                disable_web_page_preview=False,
                link_preview_options={"is_disabled": False, "show_above_text": True},
            )
        try:
            await app.send_message(
                chat_id=message.from_user.id,
                text=message.lang["welcome_set_log_show"].format(
                    message.from_user.id,
                    message.from_user.mention,
                    message.command[0],
                ),
            )
        except Exception:
            pass
        return

    reply = message.reply_to_message
    text, serialized_entities = extract_reply_text_value(reply)
    if text is None:
        await message.reply_text(message.lang["welcome_set_invalid"])
        try:
            await app.send_message(
                chat_id=message.from_user.id,
                text=message.lang["welcome_set_log_error"].format(
                    message.from_user.id,
                    message.from_user.mention,
                    message.command[0],
                    "invalid_text",
                ),
            )
        except Exception:
            pass
        return

    if serialized_entities:
        value = {
            "text": text,
            "entities": serialized_entities,
        }
    else:
        value = text
    # /setwelcome controls the global private /start caption. Save it as the
    # canonical template so stale per-language copies cannot keep old text.
    await db.set_custom_text(key, value)
    if key == "start_pm" and getattr(reply, "photo", None):
        await db.set_bot_image("start_img", reply.photo.file_id)
    await message.reply_text(
        message.lang["welcome_set_done"].format(message.command[0])
    )
    try:
        await app.send_message(
            chat_id=message.from_user.id,
            text=message.lang["welcome_set_log_done"].format(
                message.from_user.id,
                message.from_user.mention,
                message.command[0],
            ),
        )
    except Exception:
        pass


async def set_runtime_text(
    message: types.Message,
    key: str,
    fallback: str,
    usage_text: str,
    show_text: str,
    invalid_text: str,
    done_text: str,
    log_show_text: str,
    log_done_text: str,
    log_error_text: str,
    command_label: str | None = None,
    storage_key: str | None = None,
    display_key: str | None = None,
) -> None:
    storage_key = storage_key or key
    display_key = display_key or key
    lang_code = await db.get_lang(message.chat.id)
    if command_label is None:
        command_label = message.command[0]

    if not message.reply_to_message:
        template, source = await db.get_custom_text_state(
            storage_key, fallback, lang_code
        )
        preview_template = await utils.preview_template(
            template, storage_key, lang_code=lang_code
        )
        await message.reply_text(format_runtime_text_preview(message, show_text, source))
        if isinstance(preview_template, dict):
            await utils.reply_text(
                message,
                text=preview_template["text"],
                entities=preview_template.get("entities"),
            )
        else:
            await message.reply_text(preview_template)
        await message.reply_text(usage_text)
        try:
            await app.send_message(
                chat_id=message.from_user.id,
                text=log_show_text.format(
                    message.from_user.id,
                    message.from_user.mention,
                    command_label,
                ),
            )
        except Exception:
            pass
        return

    reply = message.reply_to_message
    text, serialized_entities = extract_reply_text_value(reply)
    if text is None:
        await message.reply_text(invalid_text)
        try:
            await app.send_message(
                chat_id=message.from_user.id,
                text=log_error_text.format(
                    message.from_user.id,
                    message.from_user.mention,
                    command_label,
                    "invalid_text",
                ),
            )
        except Exception:
            pass
        return

    if serialized_entities:
        value = {
            "text": text,
            "entities": serialized_entities,
        }
    else:
        value = text
    await db.set_custom_text(storage_key, value, lang_code=lang_code)
    await message.reply_text(done_text.format(message.command[0], display_key))
    try:
        await app.send_message(
            chat_id=message.from_user.id,
            text=log_done_text.format(
                message.from_user.id,
                message.from_user.mention,
                command_label,
            ),
        )
    except Exception:
        pass


def get_runtime_text_config(m: types.Message, key: str, command_name: str) -> dict | None:
    canonical_key = resolve_runtime_text_key(key)
    config = RUNTIME_TEXT_CONFIG.get(canonical_key)
    if not config:
        return None

    if "fallback_value" in config:
        fallback = config["fallback_value"]
    else:
        fallback = m.lang[config["fallback_lang_key"]]
    usage_text = m.lang.get(
        config.get("usage_lang_key", ""),
        config["usage_fallback"],
    ).format(cmd=command_name, key=key)
    show_text = m.lang.get(config.get("show_lang_key", ""), config["show_fallback"])
    invalid_text = m.lang.get(
        config.get("invalid_lang_key", ""), config["invalid_fallback"]
    )
    done_text = m.lang.get(config.get("done_lang_key", ""), config["done_fallback"])
    log_show_text = m.lang.get(
        config.get("log_show_lang_key", ""), config["log_show_fallback"]
    )
    log_done_text = m.lang.get(
        config.get("log_done_lang_key", ""), config["log_done_fallback"]
    )
    log_error_text = m.lang.get(
        config.get("log_error_lang_key", ""), config["log_error_fallback"]
    )

    return {
        "storage_key": canonical_key,
        "display_key": key,
        "fallback": fallback,
        "usage_text": usage_text,
        "show_text": show_text,
        "invalid_text": invalid_text,
        "done_text": done_text,
        "log_show_text": log_show_text,
        "log_done_text": log_done_text,
        "log_error_text": log_error_text,
    }


async def set_named_runtime_text(message: types.Message, key: str, command_name: str) -> None:
    config = get_runtime_text_config(message, key, command_name)
    if not config:
        keys = ", ".join(supported_runtime_text_keys())
        return await message.reply_text(
            f"<b>Usage:</b> <code>/{command_name} [key]</code>\n\n"
            f"<b>Supported keys:</b> <code>{keys}</code>"
        )

    await set_runtime_text(
        message=message,
        key=key,
        fallback=config["fallback"],
        usage_text=config["usage_text"],
        show_text=config["show_text"],
        invalid_text=config["invalid_text"],
        done_text=config["done_text"],
        log_show_text=config["log_show_text"],
        log_done_text=config["log_done_text"],
        log_error_text=config["log_error_text"],
        command_label=f"{command_name} {key}",
        storage_key=config["storage_key"],
        display_key=config["display_key"],
    )


@app.on_message(filters.command(["setthumb"]) & app.sudoers, group=-1)
@lang.language()
async def _setthumb(_, m: types.Message):
    await set_bot_image(m, "default_thumb")


@app.on_message(filters.command(["setping"]) & app.sudoers, group=-1)
@lang.language()
async def _setping(_, m: types.Message):
    await set_bot_image(m, "ping_img")


@app.on_message(filters.command(["setstart"]) & app.sudoers, group=-1)
@lang.language()
async def _setstart(_, m: types.Message):
    await set_bot_image(m, "start_img")


@app.on_message(filters.command(["setwelcome"]) & app.sudoers, group=-1)
@lang.language()
async def _setwelcome(_, m: types.Message):
    await set_custom_text(m, "start_pm")


@app.on_message(filters.command(["setplaymedia"]) & app.sudoers, group=-1)
@lang.language()
async def _setplaymedia(_, m: types.Message):
    await set_named_runtime_text(m, "play_media", m.command[0])


@app.on_message(filters.command(["setplayqueued"]) & app.sudoers, group=-1)
@lang.language()
async def _setplayqueued(_, m: types.Message):
    await set_named_runtime_text(m, "play_queued", m.command[0])


@app.on_message(filters.command(["setplaynotfound"]) & app.sudoers, group=-1)
@lang.language()
async def _setplaynotfound(_, m: types.Message):
    await set_named_runtime_text(m, "play_not_found", m.command[0])


@app.on_message(filters.command(["seterrornofile"]) & app.sudoers, group=-1)
@lang.language()
async def _seterrornofile(_, m: types.Message):
    await set_named_runtime_text(m, "error_no_file", m.command[0])


@app.on_message(filters.command(["setbuttonstyle"]) & app.sudoers, group=-1)
@lang.language()
async def _setbuttonstyle(_, m: types.Message):
    if len(m.command) == 1:
        doc = await db.cache.find_one({"_id": "button_styles"}) or {}
        styles = doc.get("values", {})
        active = {k: v for k, v in styles.items() if v}
        if not active:
            return await m.reply_text(m.lang["buttonstyle_empty"])
        lines = ""
        for k, v in active.items():
            lines += f"<code>{k}</code> ➜ <b>{v}</b>\n"
        return await m.reply_text(m.lang["buttonstyle_list"].format(lines))

    if len(m.command) < 3:
        return await m.reply_text(m.lang["buttonstyle_usage"])

    key, style = m.command[1], m.command[2]
    if style not in ("primary", "success", "danger", "default"):
        return await m.reply_text(m.lang["buttonstyle_invalid"])

    from AnonX_3.helpers import buttons
    await db.set_button_style(key, style if style != "default" else "")
    await buttons.load_styles(db)
    reply = m.lang["buttonstyle_done"].format(key, style)
    if not buttons.style_supported:
        reply += "\n\n" + m.lang["buttonstyle_nosupport"]
    await m.reply_text(reply)


@app.on_message(
    filters.command(["setbt", "setbuttontext"]) & app.sudoers,
    group=-1,
)
@lang.language()
async def _setbuttontext(_, m: types.Message):
    from AnonX_3.helpers import buttons

    supported = ", ".join(f"<code>{key}</code>" for key in buttons.text_keys)

    if len(m.command) == 1:
        active = await db.get_button_texts()
        if not active:
            return await m.reply_text(
                "<b>No custom button text overrides yet.</b>\n\n"
                f"<b>Usage:</b> <code>/{m.command[0]} [key]</code> (reply to text)\n"
                f"<b>Reset:</b> <code>/{m.command[0]} [key] default</code>\n\n"
                f"<b>Supported keys:</b> {supported}"
            )
        lines = []
        for key, value in active.items():
            if isinstance(value, dict):
                text = value.get("text", "")
                icon = value.get("icon_custom_emoji_id")
                lines.append(
                    f"<code>{key}</code> -> <code>{text}</code>"
                    + (f" (icon: <code>{icon}</code>)" if icon else "")
                )
            else:
                lines.append(f"<code>{key}</code> -> <code>{value}</code>")
        return await m.reply_text(
            "<b>Active button text overrides:</b>\n\n"
            + "\n".join(lines)
            + "\n\n"
            + f"<b>Supported keys:</b> {supported}"
        )

    key = m.command[1].strip().lower()
    if key not in buttons.text_keys:
        return await m.reply_text(
            f"<b>Usage:</b> <code>/{m.command[0]} [key]</code> (reply to text)\n"
            f"<b>Reset:</b> <code>/{m.command[0]} [key] default</code>\n\n"
            f"<b>Supported keys:</b> {supported}"
        )

    if len(m.command) >= 3 and m.command[2].strip().lower() == "default":
        await db.delete_button_text(key)
        await buttons.load_styles(db)
        return await m.reply_text(
            f"Reset button text for <code>{key}</code> to default."
        )

    async def _send_preview(text: str, entities: list[dict] | None = None):
        if entities:
            try:
                return await utils.reply_text(m, text=text, entities=entities)
            except Exception:
                pass
        return await m.reply_text(text)

    if not m.reply_to_message:
        active = await db.get_button_texts()
        current = active.get(key)
        source = "custom" if current else "default"
        await m.reply_text(format_button_text_preview(m, key, source))
        if isinstance(current, dict):
            preview_text = current.get("text", "")
            preview_entities = current.get("entities")
            if preview_text:
                await _send_preview(preview_text, preview_entities)
        elif isinstance(current, str):
            await m.reply_text(current)
        else:
            await m.reply_text(default_button_text_preview(m, key))
        usage = setbuttontext_usage_text(
            command_name=m.command[0],
            key=key,
            supported=supported,
        )
        return await m.reply_text(usage)

    reply = m.reply_to_message
    text, serialized = extract_reply_text_value(reply)
    if text is None:
        return await m.reply_text("Reply must contain text.")

    icon_custom_emoji_id = None
    for entity in serialized:
        if entity.get("type") != "custom_emoji":
            continue
        emoji_id = entity.get("custom_emoji_id")
        if isinstance(emoji_id, int):
            icon_custom_emoji_id = str(emoji_id)
            break
        if isinstance(emoji_id, str) and emoji_id.isdigit():
            icon_custom_emoji_id = emoji_id
            break

    value: str | dict = text
    if serialized or icon_custom_emoji_id:
        value = {"text": text}
        if serialized:
            value["entities"] = serialized
        if icon_custom_emoji_id:
            value["icon_custom_emoji_id"] = icon_custom_emoji_id

    await db.set_button_text(key, value)
    await buttons.load_styles(db)

    if isinstance(value, dict):
        await _send_preview(value.get("text", ""), value.get("entities"))
    else:
        await m.reply_text(value)
    await m.reply_text(f"Updated <code>{key}</code>.")


@app.on_message(
    filters.command(["settext", "gettext"]) & app.sudoers,
    group=-1,
)
@lang.language()
async def _settext(_, m: types.Message):
    if len(m.command) < 2:
        return await m.reply_text(settext_usage_text(m.command[0]))

    key = m.command[1].strip().lower()
    config = get_runtime_text_config(m, key, m.command[0])
    if m.command[0] == "settext" and len(m.command) >= 3:
        if m.command[2].strip().lower() == "default":
            if not config:
                return await m.reply_text(settext_usage_text(m.command[0]))
            return await reset_named_runtime_text(
                m,
                config["storage_key"],
                config["display_key"],
            )

    if not config:
        return await m.reply_text(settext_usage_text(m.command[0]))

    if config["storage_key"] in RESETTABLE_RUNTIME_TEXT_KEYS:
        reset_note = m.lang.get(
            "settext_reset_note",
            "<b>Reset:</b> <code>/settext {0} default</code>",
        ).format(config["display_key"])
        config["usage_text"] = f'{config["usage_text"]}\n\n{reset_note}'

    await set_runtime_text(
        message=m,
        key=config["display_key"],
        fallback=config["fallback"],
        usage_text=config["usage_text"],
        show_text=config["show_text"],
        invalid_text=config["invalid_text"],
        done_text=config["done_text"],
        log_show_text=config["log_show_text"],
        log_done_text=config["log_done_text"],
        log_error_text=config["log_error_text"],
        command_label=f'{m.command[0]} {config["display_key"]}',
        storage_key=config["storage_key"],
        display_key=config["display_key"],
    )


_restart_in_progress = False
_restart_task: asyncio.Task | None = None
_restart_lock = asyncio.Lock()


async def _finish_manual_restart(package_name: str) -> None:
    """Shut down outside the Pyrogram handler task, then replace the process."""
    # Let the command handler return to the dispatcher before app.exit() stops
    # dispatcher workers. Calling stop() from that handler creates a cancellation
    # cycle when asyncio.wait_for() times out.
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(stop(), timeout=PROCESS_STOP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning(
            "Manual restart stop() timed out after %ss",
            PROCESS_STOP_TIMEOUT_SEC,
        )
    except Exception as ex:
        logger.warning("Manual restart stop() failed: %s", ex)
    finally:
        try:
            reset_runtime_dirs()
        except Exception as ex:
            logger.warning(
                "Runtime directory reset failed before manual restart: %s",
                ex,
            )
        exec_fresh_process(
            package_name,
            reason="manual-restart",
            clear_crash_state=True,
        )


@app.on_message(filters.command(["restart"]) & app.sudoers, group=-1)
@lang.language()
async def _restart(_, m: types.Message):
    global _restart_in_progress, _restart_task

    # Reliable permission check — owner + sudoers from DB + filter
    if m.from_user:
        uid = m.from_user.id
        if uid != config.OWNER_ID and uid not in app._sudo_ids:
            db_sudoers = await db.get_sudoers()
            if uid not in db_sudoers and uid not in (await db.get_owners()):
                await m.reply_text("⛔ Owner / Sudo users only.")
                return

    message_date = getattr(m, "date", None)
    if message_date is not None:
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - message_date).total_seconds()
        if age_seconds > 300:
            logger.warning(
                "Ignored stale /restart replay for chat_id=%s message_id=%s age=%ss.",
                m.chat.id, m.id, int(age_seconds),
            )
            return

    async with _restart_lock:
        already_restarting = _restart_in_progress
        if not already_restarting:
            # Claim before the first await below so two distinct authorized
            # commands cannot both pass the restart gate.
            _restart_in_progress = True
    if already_restarting:
        await m.reply_text(
            m.lang.get(
                "restart_in_progress",
                "♻️ Restart is already in progress. Please wait.",
            )
        )
        return

    restart_scheduled = False
    try:
        try:
            claimed = await db.claim_command_once("restart", m.chat.id, m.id)
        except Exception as ex:
            # Mongo availability must not prevent an authorized manual recovery.
            logger.warning(
                "Could not persist /restart replay claim for chat_id=%s "
                "message_id=%s: %s",
                m.chat.id,
                m.id,
                ex,
            )
            claimed = True
        if not claimed:
            logger.warning(
                "Ignored duplicate /restart update for chat_id=%s message_id=%s.",
                m.chat.id,
                m.id,
            )
            return

        sent = None
        try:
            sent = await m.reply_text(m.lang["restarting"])
        except Exception as ex:
            logger.warning("Could not send manual restart acknowledgement: %s", ex)
        await asyncio.sleep(0.5)
        if sent is not None:
            try:
                await sent.edit_text(m.lang["restarted"])
            except Exception as ex:
                logger.warning(
                    "Could not update manual restart acknowledgement: %s",
                    ex,
                )
        try:
            os.remove("log.txt")
        except Exception:
            pass

        _restart_task = asyncio.create_task(
            _finish_manual_restart(resolve_package_name(__package__)),
            name="manual-restart",
        )
        restart_scheduled = True
    finally:
        if not restart_scheduled:
            # Cancellation or any pre-scheduling failure must not permanently
            # wedge the command gate in this still-running interpreter.
            async with _restart_lock:
                _restart_in_progress = False


def _resolve_log_file_path() -> str | None:
    candidates: list[str] = []

    # Prefer file handlers already attached by logging.basicConfig in __init__.py.
    for holder in (logger, logging.getLogger()):
        for handler in getattr(holder, "handlers", []):
            base = getattr(handler, "baseFilename", None)
            if isinstance(base, str) and base:
                candidates.append(base)
                # If file path was deleted while process is alive, reopen stream.
                if not os.path.exists(base):
                    try:
                        if hasattr(handler, "acquire"):
                            handler.acquire()
                        stream = getattr(handler, "stream", None)
                        if stream:
                            stream.close()
                        if hasattr(handler, "_open"):
                            handler.stream = handler._open()
                    except Exception:
                        pass
                    finally:
                        if hasattr(handler, "release"):
                            handler.release()

    # Fallbacks in case no file handler exists.
    candidates.extend(
        [
            LOG_FILE_PATH,
            os.path.abspath("log.txt"),
            os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "log.txt")
            ),
        ]
    )

    for path in candidates:
        if os.path.exists(path):
            return path

    # Create fallback file so /logs always has an attachable target.
    fallback = candidates[0] if candidates else LOG_FILE_PATH
    try:
        return ensure_log_file(fallback)
    except Exception:
        return None
