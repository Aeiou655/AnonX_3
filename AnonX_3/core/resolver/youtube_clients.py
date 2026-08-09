# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""YouTube yt-dlp client / PO token retry ladder."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from AnonX_3 import config


def player_clients() -> list[str]:
    raw = (getattr(config, "YTDLP_PLAYER_CLIENTS", "") or "").strip()
    if not raw:
        return ["default"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or ["default"]


def build_extract_attempts(
    base_opts: dict,
    *,
    use_po: bool = False,
    po_client: str = "web",
) -> list[dict]:
    """
    Return ordered yt-dlp option dicts for extract/download ladder:
      1) each player client without PO
      2) same with PO extractor_args when use_po
      3) drop cookies variants are handled by caller
    """
    attempts: list[dict] = []
    clients = player_clients()

    def _with_client(opts: dict, client: str) -> dict:
        o = deepcopy(opts)
        if client and client != "default":
            ea = dict(o.get("extractor_args") or {})
            yt = dict(ea.get("youtube") or {})
            yt["player_client"] = [client]
            ea["youtube"] = yt
            o["extractor_args"] = ea
        return o

    for client in clients:
        attempts.append(_with_client(base_opts, client))

    if use_po:
        for client in clients:
            o = _with_client(base_opts, client if client != "default" else po_client)
            # PO injection done by po_token_provider.apply_to_ydl_opts async;
            # mark for caller
            o["_want_po"] = True
            attempts.append(o)

    # Dedupe by repr of extractor_args + format
    seen: set[str] = set()
    unique: list[dict] = []
    for a in attempts:
        key = str(a.get("extractor_args")) + "|" + str(a.get("format")) + "|" + str(
            a.get("_want_po")
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique or [deepcopy(base_opts)]


async def apply_po_if_requested(opts: dict, video_id: str | None = None) -> dict:
    if not opts.pop("_want_po", False):
        return opts
    try:
        from AnonX_3.core.provider.po_token import po_token_provider

        if po_token_provider.enabled():
            return await po_token_provider.apply_to_ydl_opts(opts, video_id=video_id)
    except Exception:
        pass
    return opts
