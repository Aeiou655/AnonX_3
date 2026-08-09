# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import re
import random
import math

from pyrogram import enums, types

from AnonX_3 import app, config, lang
from AnonX_3.core.lang import lang_codes


_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

_STARTGROUP_SEQUENCE: list[str] | None = None
_STARTGROUP_INDEX = 0


def _scaled_weight_counts(weights: list[float]) -> list[int]:
    positive = [max(0.0, float(weight)) for weight in weights]
    if not positive or not any(weight > 0 for weight in positive):
        return []

    counts = [int(round(weight)) for weight in positive]
    if any(count > 0 for count in counts):
        non_zero = [count for count in counts if count > 0]
        divisor = math.gcd(*non_zero) if non_zero else 1
        return [max(0, count // divisor) for count in counts]

    scaled = [int(round(weight * 100)) for weight in positive]
    non_zero = [count for count in scaled if count > 0]
    if not non_zero:
        return []
    divisor = math.gcd(*non_zero)
    return [max(0, count // divisor) for count in scaled]


def _build_startgroup_sequence() -> list[str]:
    urls = list(config.STARTGROUP_URLS or [])
    if not urls:
        return []

    weights = list(config.STARTGROUP_WEIGHTS or [])
    if len(weights) != len(urls):
        return urls

    counts = _scaled_weight_counts(weights)
    if not counts or len(counts) != len(urls):
        return urls

    sequence: list[str] = []
    for url, count in zip(urls, counts):
        if count <= 0:
            continue
        sequence.extend([url] * count)
    return sequence or urls


def _next_startgroup_url(default_url: str) -> str:
    global _STARTGROUP_SEQUENCE, _STARTGROUP_INDEX

    if not config.STARTGROUP_URLS:
        return default_url

    if _STARTGROUP_SEQUENCE is None:
        _STARTGROUP_SEQUENCE = _build_startgroup_sequence()
        if not _STARTGROUP_SEQUENCE:
            _STARTGROUP_SEQUENCE = list(config.STARTGROUP_URLS)
        if config.STARTGROUP_WEIGHTS:
            random.shuffle(_STARTGROUP_SEQUENCE)

    if not _STARTGROUP_SEQUENCE:
        return default_url

    url = _STARTGROUP_SEQUENCE[_STARTGROUP_INDEX % len(_STARTGROUP_SEQUENCE)]
    _STARTGROUP_INDEX = (_STARTGROUP_INDEX + 1) % len(_STARTGROUP_SEQUENCE)
    return url


def _strip_unicode_emoji(text: str) -> str:
    cleaned = _UNICODE_EMOJI_RE.sub("", text)
    cleaned = cleaned.replace("\ufe0f", "").replace("\ufe0e", "").replace("\u200d", "")
    return " ".join(cleaned.split()).strip()


def _remove_utf16_span(text: str, offset: int, length: int) -> str | None:
    """Remove one Telegram entity span without disturbing unrelated emoji."""
    if not isinstance(offset, int) or not isinstance(length, int):
        return None
    if offset < 0 or length <= 0:
        return None
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    if start > len(encoded) or end > len(encoded):
        return None
    try:
        cleaned = (encoded[:start] + encoded[end:]).decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    return " ".join(cleaned.split()).strip()


class Inline:
    TEXT_KEYS = (
        "cancel_dl",
        "controls_status",
        "controls_resume",
        "controls_pause",
        "controls_replay",
        "controls_skip",
        "controls_stop",
        "controls_close",
        "help_back",
        "help_close",
        "help_item_0",
        "help_item_1",
        "help_item_2",
        "help_item_3",
        "help_item_4",
        "help_item_5",
        "help_item_6",
        "help_item_7",
        "help_item_8",
        "help_item_9",
        "filter_mute",
        "filter_unmute",
        "lang_active",
        "lang_inactive",
        "ping_support",
        "play_queued_close",
        "queue_toggle_pause",
        "queue_toggle_resume",
        "settings_play_mode_label",
        "settings_play_mode_value",
        "settings_cmd_delete_label",
        "settings_cmd_delete_value",
        "settings_autoplay_label",
        "settings_autoplay_value",
        "settings_language_label",
        "settings_language_value",
        "start_add_me",
        "start_help",
        "start_support",
        "start_channel",
        "start_source",
        "start_language",
        "yt_copy",
        "yt_open",
    )

    def __init__(self):
        self.ikm = types.InlineKeyboardMarkup
        self.ikb = types.InlineKeyboardButton
        self._styles = {}
        self._texts = {}

    async def load_styles(self, db):
        doc = await db.cache.find_one({"_id": "button_styles"}) or {}
        self._styles = doc.get("values", {})
        text_doc = await db.cache.find_one({"_id": "button_texts"}) or {}
        values = text_doc.get("values", {})
        self._texts = values if isinstance(values, dict) else {}

    @property
    def text_keys(self) -> tuple[str, ...]:
        return self.TEXT_KEYS

    @property
    def style_supported(self) -> bool:
        return True

    def _text_override(self, key: str | None):
        if not key:
            return None, None
        override = self._texts.get(key)
        source_key = key if override is not None else None
        return override, source_key

    def _style(self, key: str, default: str) -> str:
        return self._styles.get(key) or default

    def _button(
        self,
        text: str,
        style: str = None,
        key: str | None = None,
        **kwargs,
    ):
        fallback_text = _strip_unicode_emoji(str(text))
        resolved_text = str(text)
        has_override = False
        icon_custom_emoji_id = None
        icon_entity_span = None
        override, _ = self._text_override(key)
        if isinstance(override, str) and override.strip():
            has_override = True
            resolved_text = override.strip()
        elif isinstance(override, dict):
            has_override = True
            value_text = override.get("text")
            if isinstance(value_text, str) and value_text.strip():
                resolved_text = value_text.strip()
            icon_id = override.get("icon_custom_emoji_id")
            if isinstance(icon_id, int):
                icon_custom_emoji_id = str(icon_id)
            elif isinstance(icon_id, str) and icon_id.isdigit():
                icon_custom_emoji_id = icon_id
            for entity in override.get("entities") or []:
                if (
                    not isinstance(entity, dict)
                    or entity.get("type") != "custom_emoji"
                    or not entity.get("custom_emoji_id")
                ):
                    continue
                eid = entity["custom_emoji_id"]
                if isinstance(eid, int):
                    entity_icon_id = str(eid)
                elif isinstance(eid, str) and eid.isdigit():
                    entity_icon_id = eid
                else:
                    continue
                if not icon_custom_emoji_id:
                    icon_custom_emoji_id = entity_icon_id
                if entity_icon_id == icon_custom_emoji_id:
                    icon_entity_span = (
                        entity.get("offset"),
                        entity.get("length"),
                    )
                    break

        if has_override and icon_custom_emoji_id:
            sanitized_text = None
            if icon_entity_span is not None:
                sanitized_text = _remove_utf16_span(
                    resolved_text,
                    icon_entity_span[0],
                    icon_entity_span[1],
                )
            if sanitized_text is None:
                sanitized_text = _strip_unicode_emoji(resolved_text)
            if sanitized_text:
                resolved_text = sanitized_text
            elif fallback_text:
                resolved_text = fallback_text
        elif has_override and not resolved_text and fallback_text:
            resolved_text = fallback_text

        button = {"text": resolved_text}
        if style in {"primary", "success", "danger"}:
            button["style"] = style
        if icon_custom_emoji_id:
            button["icon_custom_emoji_id"] = icon_custom_emoji_id
        copy_text = kwargs.pop("copy_text", None)
        if copy_text is not None:
            button["copy_text"] = (
                copy_text if isinstance(copy_text, dict) else {"text": copy_text}
            )
        button.update({k: v for k, v in kwargs.items() if v is not None})
        return button

    def _markup(self, rows):
        return {"inline_keyboard": rows}

    def to_pyrogram_markup(self, markup: dict | None):
        if not markup:
            return None
        rows = []
        for row in markup.get("inline_keyboard", []):
            buttons = []
            for button in row:
                data = dict(button)
                icon_id = data.get("icon_custom_emoji_id")
                if isinstance(icon_id, str) and icon_id.isdigit():
                    data["icon_custom_emoji_id"] = int(icon_id)
                elif not isinstance(icon_id, int):
                    data.pop("icon_custom_emoji_id", None)
                style = data.get("style")
                if isinstance(style, str):
                    style_value = {
                        "primary": enums.ButtonStyle.PRIMARY,
                        "success": enums.ButtonStyle.SUCCESS,
                        "danger": enums.ButtonStyle.DANGER,
                    }.get(style.lower())
                    if style_value is None:
                        data.pop("style", None)
                    else:
                        data["style"] = style_value
                copy_text = data.get("copy_text")
                if isinstance(copy_text, dict):
                    data["copy_text"] = copy_text.get("text")
                try:
                    buttons.append(self.ikb(**data))
                    continue
                except TypeError:
                    pass
                # Older Pyrogram builds may lack optional presentation fields.
                # Keep the action intact while downgrading only icon/style.
                for pop_key in ("icon_custom_emoji_id", "style"):
                    data.pop(pop_key, None)
                    try:
                        buttons.append(self.ikb(**data))
                        break
                    except TypeError:
                        continue
                else:
                    # Do not emit a dead Copy button if its action is unsupported.
                    if "copy_text" not in data and len(data) > 1:
                        buttons.append(self.ikb(**data))
            if buttons:
                rows.append(buttons)
        return self.ikm(rows)

    def cancel_dl(self, text) -> dict:
        """Bot-API dict markup (styled danger when supported)."""
        label = (str(text or "").strip() or "Cancel")
        return self._markup(
            [
                [
                    self._button(
                        text=label,
                        key="cancel_dl",
                        style=self._style("cancel_dl", "danger"),
                        callback_data="cancel_dl",
                    )
                ]
            ]
        )

    def cancel_dl_pyrogram(self, text: str | None = None):
        """Always-visible Pyrogram inline Cancel (no Bot-API-only fields)."""
        label = (str(text or "").strip() or "Cancel")
        return self.ikm([[self.ikb(text=label, callback_data="cancel_dl")]])

    def support_button(self, text: str | None = None) -> dict:
        """Green (success-style) inline button linking to SUPPORT_CHAT."""
        label = (str(text or "").strip() or "Support")
        return self._markup(
            [
                [
                    self._button(
                        text=label,
                        key="support_chat",
                        style=self._style("support_chat", "success"),
                        url=config.SUPPORT_CHAT,
                    )
                ]
            ]
        )

    def filter_moderation(
        self,
        target_id: int,
        chat_id: int,
        *,
        muted: bool,
    ) -> dict:
        """Styled moderation control for keyword-filter warnings."""
        action = "unmute" if muted else "mute"
        key = f"filter_{action}"
        return self._markup(
            [
                [
                    self._button(
                        text="Unmute" if muted else "Mute",
                        key=key,
                        style=self._style(
                            key,
                            "success" if muted else "danger",
                        ),
                        callback_data=f"filtermod {action} {target_id} {chat_id}",
                    )
                ]
            ]
        )

    def controls(
        self,
        chat_id: int,
        status: str = None,
        timer: str = None,
        remove: bool = False,
        status_style: str | None = None,
    ) -> dict:
        keyboard = []
        if status:
            style_key = f"controls_status_{status_style}" if status_style else "controls_status"
            default_style = status_style or "primary"
            keyboard.append(
                [self._button(text=status, key="controls_status", style=self._style(style_key, default_style), callback_data=f"controls status {chat_id}")]
            )
        elif timer:
            keyboard.append(
                [self._button(text=timer, key="controls_status", style=self._style("controls_status", "primary"), callback_data=f"controls status {chat_id}")]
            )

        if not remove:
            keyboard.append(
                [
                    self._button(text="▷", key="controls_resume", style=self._style("controls_resume", "success"), callback_data=f"controls resume {chat_id}"),
                    self._button(text="II", key="controls_pause", style=self._style("controls_pause", "primary"), callback_data=f"controls pause {chat_id}"),
                    self._button(text="⥁", key="controls_replay", style=self._style("controls_replay", "primary"), callback_data=f"controls replay {chat_id}"),
                    self._button(text="‣‣I", key="controls_skip", style=self._style("controls_skip", "primary"), callback_data=f"controls skip {chat_id}"),
                    self._button(text="▢", key="controls_stop", style=self._style("controls_stop", "danger"), callback_data=f"controls stop {chat_id}"),
                ]
            )
            keyboard.append(
                [
                    self._button(
                        text="Close",
                        key="controls_close",
                        style=self._style("controls_close", "danger"),
                        callback_data=f"controls close {chat_id}",
                    )
                ]
            )
        return self._markup(keyboard)

    def help_markup(
        self, _lang: dict, back: bool = False
    ) -> dict:
        if back:
            rows = [
                [
                    self._button(text=_lang["back"], key="help_back", style=self._style("help_back", "primary"), callback_data="help back"),
                    self._button(text=_lang["close"], key="help_close", style=self._style("help_close", "danger"), callback_data="help close"),
                ]
            ]
        else:
            entries = [
                ("help_0", "help_item_0", "admins"),
                ("help_1", "help_item_1", "auth"),
                ("help_2", "help_item_2", "blist"),
                ("help_3", "help_item_3", "lang"),
                ("help_4", "help_item_4", "ping"),
                ("help_5", "help_item_5", "play"),
                ("help_6", "help_item_6", "queue"),
                ("help_7", "help_item_7", "stats"),
                ("help_8", "help_item_8", "sudo"),
            ]
            cycle = ("primary", "success", "danger")
            buttons = []
            for idx, (lang_key, item_key, cb) in enumerate(entries):
                default_style = self._style("help_item", cycle[idx % len(cycle)])
                style = self._style(item_key, default_style)
                buttons.append(
                    self._button(
                        text=_lang[lang_key],
                        key=item_key,
                        style=style,
                        callback_data=f"help {cb}",
                    )
                )
            rows = [buttons[i: i + 3] for i in range(0, len(buttons), 3)]

        return self._markup(rows)

    def lang_markup(self, _lang: str) -> dict:
        langs = lang.get_languages()

        buttons = [
            self._button(
                text=f"{name} ({code}) {'✔️' if code == _lang else ''}",
                key="lang_active" if code == _lang else "lang_inactive",
                style=self._style("lang_active", "success") if code == _lang else self._style("lang_inactive", "primary"),
                callback_data=f"lang_change {code}",
            )
            for code, name in langs.items()
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        return self._markup(rows)

    def ping_markup(self, text: str) -> dict:
        return self._markup([[self._button(text=text, key="ping_support", style=self._style("ping_support", "primary"), url=config.SUPPORT_CHAT)]])

    def play_queued(self, chat_id: int, _text: str) -> dict:
        return self._markup(
            [
                [
                    self._button(
                        text=_text,
                        key="play_queued_close",
                        callback_data=f"controls close {chat_id}",
                        style=self._style("play_queued_close", "success"),
                    )
                ]
            ]
        )

    def queue_markup(
        self, chat_id: int, _text: str, playing: bool
    ) -> dict:
        _action = "pause" if playing else "resume"
        default_style = "success" if not playing else "primary"
        style_key = "queue_toggle_pause" if playing else "queue_toggle_resume"
        return self._markup(
            [[self._button(text=_text, key=style_key, style=self._style(style_key, default_style), callback_data=f"controls {_action} {chat_id} q")]]
        )

    def settings_markup(
        self,
        lang: dict,
        admin_only: bool,
        cmd_delete: bool,
        autoplay: bool,
        language: str,
        chat_id: int,
    ) -> dict:
        return self._markup(
            [
                [
                    self._button(
                        text=lang["play_mode"] + " ➜",
                        key="settings_play_mode_label",
                        style=self._style("settings_label", "primary"),
                        callback_data="settings",
                    ),
                    self._button(text=admin_only, key="settings_play_mode_value", style=self._style("settings_play_mode", "success"), callback_data="settings play"),
                ],
                [
                    self._button(
                        text=lang["cmd_delete"] + " ➜",
                        key="settings_cmd_delete_label",
                        style=self._style("settings_label", "primary"),
                        callback_data="settings",
                    ),
                    self._button(text=cmd_delete, key="settings_cmd_delete_value", style=self._style("settings_delete_on", "danger") if str(cmd_delete).lower() in ("true", "on", "yes", "enabled") else self._style("settings_delete_off", "success"), callback_data="settings delete"),
                ],
                [
                    self._button(
                        text=lang.get("autoplay", "Autoplay") + " ➜",
                        key="settings_autoplay_label",
                        style=self._style("settings_label", "primary"),
                        callback_data="settings",
                    ),
                    self._button(
                        text="ON" if autoplay else "OFF",
                        key="settings_autoplay_value",
                        style=self._style("settings_autoplay_on", "success") if autoplay else self._style("settings_autoplay_off", "danger"),
                        callback_data="settings autoplay",
                    ),
                ],
                [
                    self._button(
                        text=lang["language"] + " ➜",
                        key="settings_language_label",
                        style=self._style("settings_label", "primary"),
                        callback_data="settings",
                    ),
                    self._button(text=lang_codes[language], key="settings_language_value", style=self._style("settings_language", "success"), callback_data="language"),
                ],
            ]
        )

    def start_key(
        self, lang: dict, private: bool = False
    ) -> dict:
        add_me_url = f"https://t.me/{app.username}?startgroup=true"
        add_me_url = _next_startgroup_url(add_me_url)

        rows = [
            [
                self._button(
                    text=lang["add_me"],
                    key="start_add_me",
                    style=self._style("start_add_me", "primary"),
                    url=add_me_url,
                )
            ],
            [self._button(text=lang["help"], key="start_help", style=self._style("start_help", None), callback_data="help")],
            [
                self._button(text=lang["support"], key="start_support", style=self._style("start_support", None), url=config.SUPPORT_CHAT),
                self._button(text=lang["channel"], key="start_channel", style=self._style("start_channel", None), url=config.SUPPORT_CHANNEL),
            ],
        ]
        if private:
            rows += [
                [
                    self._button(
                        text=lang["source"],
                        key="start_source",
                        style=self._style("start_source", "danger"),
                        url=f"https://t.me/{config.OWNER_USERNAME}",
                    )
                ]
            ]
        else:
            rows += [[self._button(text=lang["language"], key="start_language", style=self._style("start_language", "success"), callback_data="language")]]
        return self._markup(rows)

    def yt_key(self, link: str) -> dict:
        return self._markup(
            [
                [
                    self._button(text="❐", key="yt_copy", style=self._style("yt_copy", "primary"), copy_text=link),
                    self._button(text="Youtube", key="yt_open", style=self._style("yt_open", "danger"), url=link),
                ],
            ]
        )
