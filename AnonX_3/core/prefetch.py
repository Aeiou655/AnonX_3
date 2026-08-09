# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import os
import time

from AnonX_3 import config, queue, yt
from AnonX_3.core.cdn import cdn
from AnonX_3.core.dynamic_capacity import background_scope, dynamic_capacity
from AnonX_3.core.metrics import metrics
from AnonX_3.core.resource_budget import (
    allow_current_cache,
    allow_prefetch_next,
    allow_prefetch_video,
)
from AnonX_3.helpers import Media, Track


class PrefetchManager:
    _DOWNLOAD_PROGRESS_ATTRS = (
        "download_progress_message",
        "download_progress_lang",
        "download_progress_template",
        "download_progress_cancel_label",
        "download_progress_started",
    )
    _CACHE_CONTEXT_ATTRS = (
        "original_query",
        "normalized_query",
        "title",
        "channel_name",
        "thumbnail",
        "duration",
        "duration_sec",
        "user",
        "chat_id",
        "user_id",
        "request_source",
        "priority",
        "_play_request_scope",
    )
    _TERMINAL_OUTCOME_TTL_SEC = 180.0

    def __init__(self):
        self.prefetch: dict[int, tuple[Media | Track, asyncio.Task]] = {}
        self.secondary: dict[int, tuple[Media | Track, asyncio.Task]] = {}
        self.current_cache: dict[int, tuple[Media | Track, asyncio.Task]] = {}
        # A warm search owner can finish before play_hndlr creates its final
        # Track object. Keep its terminal one-shot result briefly by the
        # status-card scope so the handoff cannot start a second extractor.
        self._terminal_outcomes: dict[
            tuple[int, str, bool], tuple[Media | Track, float]
        ] = {}

    @staticmethod
    def _scope_key(media: Media | Track | None) -> tuple[int, str, bool] | None:
        if media is None:
            return None
        scope = int(getattr(media, "_play_request_scope", 0) or 0)
        media_id = str(getattr(media, "id", "") or "")
        if not scope or not media_id:
            return None
        return (scope, media_id, bool(getattr(media, "video", False)))

    def _prune_terminal_outcomes(self) -> None:
        now = time.monotonic()
        for key, (_media, expires) in list(self._terminal_outcomes.items()):
            if expires <= now:
                self._terminal_outcomes.pop(key, None)

    def _remember_terminal_outcome(self, media: Media | Track) -> None:
        key = self._scope_key(media)
        if key is None:
            return
        self._prune_terminal_outcomes()
        self._terminal_outcomes[key] = (
            media,
            time.monotonic() + self._TERMINAL_OUTCOME_TTL_SEC,
        )

    def _adopt_terminal_outcome(self, media: Media | Track) -> bool:
        key = self._scope_key(media)
        if key is None:
            return False
        self._prune_terminal_outcomes()
        entry = self._terminal_outcomes.get(key)
        if not entry:
            return False
        target, _expires = entry
        if not self._same_media(target, media):
            return False
        self._merge_owner_context(target, media)
        self._copy_owner_result(target, media)
        setattr(media, "_cache_one_shot", True)
        setattr(media, "_cache_one_shot_attempted", True)
        setattr(media, "_cache_one_shot_complete", True)
        return True

    @staticmethod
    def _owner_tier(media: Media | Track, fallback: str | None) -> str | None:
        tier = getattr(media, "_cache_quality_tier", None)
        return tier if tier is not None else fallback

    def _merge_owner_context(self, target: Media | Track, incoming: Media | Track) -> None:
        """Merge request/UI context onto the one physical cache owner."""
        for attr in self._DOWNLOAD_PROGRESS_ATTRS + self._CACHE_CONTEXT_ATTRS:
            value = getattr(incoming, attr, None)
            if value not in (None, "", 0):
                setattr(target, attr, value)
        owner_tier = self._owner_tier(target, None)
        if owner_tier is not None:
            setattr(incoming, "_cache_quality_tier", owner_tier)
        setattr(target, "_cache_one_shot", True)
        setattr(incoming, "_cache_one_shot", True)

    @staticmethod
    def _copy_owner_result(target: Media | Track, incoming: Media | Track) -> None:
        for attr in (
            "local_path",
            "file_path",
            "cdn_play_url",
            "_cache_one_shot_attempted",
            "_cache_one_shot_complete",
            "_cache_one_shot_succeeded",
        ):
            value = getattr(target, attr, None)
            if value not in (None, ""):
                setattr(incoming, attr, value)

    def _active_owner(
        self, chat_id: int, media: Media | Track
    ) -> tuple[Media | Track, asyncio.Task] | None:
        for mapping in (self.current_cache, self.prefetch, self.secondary):
            candidate = mapping.get(chat_id)
            if (
                candidate
                and not candidate[1].done()
                and self._same_media(candidate[0], media)
            ):
                return candidate
        return None

    def _bind_current_owner(
        self, chat_id: int, media: Media | Track, task: asyncio.Task
    ) -> None:
        self.current_cache[chat_id] = (media, task)
        # Keep a completed owner until the foreground playback path consumes
        # it.  Removing it in a done callback creates a race: yt-dlp can
        # publish 100%, finish, and erase the only result between the direct
        # stream fallback and await_current_cache_or_download().  The latter
        # then sees no owner, treats the request's one-shot download as spent,
        # and cannot hand the finished file to play_media.  A newer owner for
        # this chat replaces this entry, and await_current_cache_or_download()
        # / cancel() consume it, so this retains at most one terminal owner
        # per chat.

    def task(self, chat_id: int):
        data = self.prefetch.get(chat_id)
        return data[1] if data else None

    def current_task(self, chat_id: int, media: Media | Track | None = None):
        """Return the current local-cache task, optionally matched by media ID."""
        current = self.current_cache.get(chat_id)
        if not current:
            return None
        if media is not None and not self._same_media(current[0], media):
            return None
        return current[1]

    def cancel(self, chat_id: int) -> None:
        task = self.task(chat_id)
        if task and not task.done():
            task.cancel()
        self.prefetch.pop(chat_id, None)
        second = self.secondary.pop(chat_id, None)
        if second and not second[1].done():
            second[1].cancel()
        current = self.current_cache.get(chat_id)
        if current and not current[1].done():
            current[1].cancel()
        self.current_cache.pop(chat_id, None)

    async def start_current_cache(
        self,
        chat_id: int,
        media: Media | Track,
        quality_tier: str | None = None,
        *,
        force: bool = False,
        immediate: bool = False,
        local_only: bool = False,
    ) -> None:
        """Warm local storage for the track currently starting.

        force: start even on poor tier — used as the direct-stream safety net.
        immediate: skip the short delay so download races with remote start.
        local_only: skip CDN and download straight into downloads/ (true parallel
        safety net while direct stream is attempted).
        Always stores a filesystem path on media.local_path for VC fallback.
        """
        if not force and not allow_current_cache(quality_tier):
            return
        if not media or not getattr(media, "id", None):
            return
        video = bool(getattr(media, "video", False))
        owner_tier = yt.resolve_download_quality_tier(quality_tier, video=video)
        setattr(media, "_cache_quality_tier", owner_tier)
        # Current playback gets one coordinated yt-dlp owner.  If direct VC
        # startup later fails, the same request joins this owner or reports
        # failure; it must not open a second recovery ladder underneath it.
        setattr(media, "_cache_one_shot", True)

        # A warm owner may have completed before the final resolver Track
        # reaches this method. Its outcome is still the sole acquisition for
        # this status-card request.
        if self._adopt_terminal_outcome(media):
            return

        existing = self._active_owner(chat_id, media)
        if existing:
            target, task = existing
            self._merge_owner_context(target, media)
            self._copy_owner_result(target, media)
            self._bind_current_owner(chat_id, target, task)
            progress_message = getattr(target, "download_progress_message", None)
            if progress_message is not None:
                try:
                    yt.attach_download_watcher(
                        str(target.id),
                        progress_message=progress_message,
                        progress_lang=getattr(target, "download_progress_lang", None),
                        progress_throttle=1.0,
                        progress_media=target,
                    )
                except Exception:
                    pass
            return

        # A prior one-shot failure on this exact request object is terminal.
        # A later user command has a fresh scope and can acquire again.
        if getattr(media, "_cache_one_shot_attempted", False):
            return

        try:
            yt.prepare_download_stream_source(
                str(media.id), video=video, quality_tier=owner_tier
            )
        except Exception:
            pass

        # Already on disk — only skip when COMPLETE (not yt-dlp partial).
        try:
            expected = yt._local_ready_path(
                str(media.id), video=video, quality_tier=owner_tier
            )
            min_b = 512 * 1024 if video else 64 * 1024
            if yt.is_complete_media_file(expected, min_bytes=min_b):
                setattr(media, "local_path", expected)
                # Treat a validated disk hit as this request's terminal
                # acquisition result too.  If the file vanishes later in the
                # same lifecycle, fallback must not silently open a new
                # extraction ladder behind the original status card.
                setattr(media, "_cache_one_shot_attempted", True)
                setattr(media, "_cache_one_shot_complete", True)
                setattr(media, "_cache_one_shot_succeeded", True)
                self._remember_terminal_outcome(media)
                await yt.render_completed_download_progress(
                    expected,
                    progress_message=getattr(
                        media, "download_progress_message", None
                    ),
                    progress_lang=getattr(media, "download_progress_lang", None),
                    progress_media=media,
                )
                return
        except Exception:
            pass

        async def _runner(target: Media | Track) -> None:
            # Deliberately foreground priority. Despite the "background cache"
            # name this owns the download for the track that is starting *now*,
            # and await_current_cache_or_download() joins it with an unbounded
            # asyncio.shield(). Tagging it background would let another chat's
            # foreground request pause it while foreground playback is already
            # blocked on it — a priority inversion that stalls VC. Load shedding
            # for this path stays where it belongs: allow_current_cache().
            try:
                # A cold CDN job used to hide a retry ladder below this path.
                # Local yt-dlp is the one owner; CDN can publish the completed
                # file later without extracting it again.
                setattr(target, "_cache_one_shot_attempted", True)
                local_path = await yt.download(
                    target.id,
                    video=target.video,
                    quality_tier=owner_tier,
                    message_id=getattr(
                        getattr(target, "download_progress_message", None),
                        "id",
                        None,
                    ),
                    progress_message=getattr(
                        target, "download_progress_message", None
                    ),
                    progress_lang=getattr(target, "download_progress_lang", None),
                    progress_throttle=1.0,
                    progress_media=target,
                    stream_for_playback=True,
                    one_shot=True,
                    quality_tier_resolved=True,
                )
                min_b = 512 * 1024 if video else 64 * 1024
                if yt.is_complete_media_file(local_path, min_bytes=min_b):
                    setattr(target, "local_path", local_path)
                    setattr(target, "_cache_one_shot_succeeded", True)
            except asyncio.CancelledError:
                # Explicit cancellation boundary: don't propagate further down
                # the task chain. This breaks 990+ recursive cancel() calls.
                return
            except Exception:
                pass
            finally:
                setattr(target, "_cache_one_shot_complete", True)
                self._remember_terminal_outcome(target)

        task = asyncio.create_task(_runner(media))
        self._bind_current_owner(chat_id, media, task)

    async def await_current_stream_source(
        self,
        chat_id: int,
        media: Media | Track,
        *,
        quality_tier: str | None = None,
    ) -> tuple[str | None, str]:
        """Join the current cache owner until it exposes a direct URL/file."""
        current = self.current_cache.get(chat_id)
        owner = (
            current[0]
            if current and self._same_media(current[0], media)
            else media
        )
        owner_tier = self._owner_tier(owner, quality_tier)
        return await yt.await_download_stream_source(
            str(getattr(media, "id", "") or ""),
            video=bool(getattr(media, "video", False)),
            quality_tier=owner_tier,
            owner_task=self.current_task(chat_id, media),
        )

    def _same_media(self, a, b) -> bool:
        if a is b:
            return True
        if a is None or b is None:
            return False
        return (
            str(getattr(a, "id", "") or "")
            == str(getattr(b, "id", "") or "")
            and bool(getattr(a, "video", False))
            == bool(getattr(b, "video", False))
        )

    async def await_current_cache_or_download(
        self,
        chat_id: int,
        media: Media | Track,
        quality_tier: str | None = None,
        ping: float | None = None,
        progress_message=None,
        progress_lang: dict | None = None,
        progress_throttle: float = 5.0,
    ) -> str | None:
        """Join the parallel local task first — never start a second download for video."""
        if progress_message is None:
            progress_message = getattr(media, "download_progress_message", None)
        if progress_lang is None:
            progress_lang = getattr(media, "download_progress_lang", None)
        min_b = 512 * 1024 if getattr(media, "video", False) else 64 * 1024
        self._adopt_terminal_outcome(media)
        current = self.current_cache.get(chat_id)
        if current and self._same_media(current[0], media):
            target = current[0]
            owner_tier = self._owner_tier(target, quality_tier)
            task = current[1]
            # Attach progress watcher onto the *existing* download when possible
            if (
                progress_message
                and not task.done()
            ):
                try:
                    mid = getattr(progress_message, "id", None)
                    if mid is not None:
                        yt.attach_download_watcher(
                            str(media.id),
                            progress_message=progress_message,
                            progress_lang=progress_lang,
                            progress_throttle=progress_throttle,
                            progress_media=media,
                        )
                except Exception:
                    pass
            if not task.done():
                try:
                    # Completion of the actual download task is the readiness
                    # event.  Do not approximate it with a fixed wait window.
                    await asyncio.shield(task)
                except Exception:
                    pass
            self.current_cache.pop(chat_id, None)
            self._copy_owner_result(target, media)
            local_path = getattr(media, "local_path", None) or getattr(
                target, "local_path", None
            )
            if yt.is_complete_media_file(local_path, min_bytes=min_b):
                await yt.render_completed_download_progress(
                    local_path,
                    progress_message=progress_message,
                    progress_lang=progress_lang,
                    progress_media=media,
                )
                return local_path
            # Parallel task finished — check expected disk paths before new download
            try:
                for cand in (
                    yt._local_ready_path(
                        str(media.id),
                        video=bool(media.video),
                        quality_tier=owner_tier,
                    ),
                    yt.get_download_filename(
                        str(media.id),
                        video=bool(media.video),
                        quality_tier=None,
                    ),
                    f"downloads/{media.id}.webm",
                    f"downloads/{media.id}.mp4",
                    f"downloads/{media.id}.m4a",
                ):
                    if yt.is_complete_media_file(cand, min_bytes=min_b):
                        setattr(media, "local_path", cand)
                        await yt.render_completed_download_progress(
                            cand,
                            progress_message=progress_message,
                            progress_lang=progress_lang,
                            progress_media=media,
                        )
                        return cand
            except Exception:
                pass

        local_path = getattr(media, "local_path", None)
        if yt.is_complete_media_file(local_path, min_bytes=min_b):
            await yt.render_completed_download_progress(
                local_path,
                progress_message=progress_message,
                progress_lang=progress_lang,
                progress_media=media,
            )
            return local_path
        if getattr(media, "_cache_one_shot", False):
            # The first playback acquisition already completed/failed. Do not
            # turn its fallback into another yt-dlp operation for this request.
            return None
        local_path = await yt.download(
            media.id,
            video=media.video,
            quality_tier=yt.resolve_download_quality_tier(
                quality_tier, video=bool(getattr(media, "video", False))
            ),
            message_id=getattr(progress_message, "id", None) if progress_message else None,
            progress_message=progress_message,
            progress_lang=progress_lang,
            progress_throttle=progress_throttle,
            progress_media=media,
            one_shot=True,
            quality_tier_resolved=True,
        )
        if not yt.is_complete_media_file(local_path, min_bytes=min_b):
            local_path = None
        if yt.is_complete_media_file(local_path, min_bytes=min_b):
            setattr(media, "local_path", local_path)
            return local_path
        return None

    async def start_next(self, chat_id: int, quality_tier: str | None = None) -> None:
        if not allow_prefetch_next(quality_tier):
            return
        waiting = queue.get_queue(chat_id)[1 : 1 + int(config.PRELOAD_DEPTH)]
        unresolved = [item for item in waiting if not item.file_path]
        media = unresolved[0] if unresolved else None
        if not media:
            return
        if not allow_prefetch_video(quality_tier, bool(getattr(media, "video", False))):
            return

        video = bool(getattr(media, "video", False))
        owner_tier = yt.resolve_download_quality_tier(quality_tier, video=video)
        setattr(media, "_cache_quality_tier", owner_tier)
        setattr(media, "_cache_one_shot", True)

        # A current-playback owner can already be acquiring this media (for
        # example after a force/skip races the prefetch scheduler).  Reuse that
        # physical acquisition instead of creating a second wrapper task.
        active = self._active_owner(chat_id, media)
        if active:
            owner, _task = active
            self._merge_owner_context(owner, media)
            self._copy_owner_result(owner, media)
            return

        existing = self.prefetch.get(chat_id)
        reuse_existing = False
        if existing and not existing[1].done():
            if self._same_media(existing[0], media):
                reuse_existing = True
            else:
                existing[1].cancel()
                metrics.inc("prefetch_cancelled_stale")
        if not reuse_existing:
            self.prefetch.pop(chat_id, None)

        async def _prefetch_runner(target: Media | Track) -> None:
            try:
                # Never route non-YouTube sources through yt-dlp prefetch.
                target_src = str(getattr(target, "source", "") or "").lower()
                if target_src in {
                    "telegram_remote", "telegram_local",
                    "tiktok_remote", "tiktok_local",
                    "facebook_remote", "facebook_local",
                    "soundcloud", "soundcloud_remote", "soundcloud_local",
                }:
                    return
                metrics.inc("prefetch_started")
                target_video = bool(getattr(target, "video", False))
                # The secondary prefetch may have a different media type from
                # the first queued item, so freeze its own canonical tier
                # instead of inheriting the first item's resolved tier.
                target_tier = self._owner_tier(
                    target,
                    yt.resolve_download_quality_tier(
                        quality_tier, video=target_video
                    ),
                )
                setattr(target, "_cache_one_shot", True)

                # A completed/failed one-shot has a terminal result for this
                # queue item.  Do not revive it through CDN or a quality ladder.
                if getattr(target, "_cache_one_shot_attempted", False):
                    return

                min_b = 512 * 1024 if target_video else 64 * 1024
                local_path = yt._local_ready_path(
                    str(target.id), video=target_video, quality_tier=target_tier
                )
                setattr(target, "_cache_one_shot_attempted", True)
                if not yt.is_complete_media_file(local_path, min_bytes=min_b):
                    local_path = await yt.download(
                        target.id,
                        video=target_video,
                        quality_tier=target_tier,
                        progress_media=target,
                        one_shot=True,
                        quality_tier_resolved=True,
                    )

                if not yt.is_complete_media_file(local_path, min_bytes=min_b):
                    return

                setattr(target, "local_path", local_path)
                setattr(target, "file_path", local_path)
                setattr(target, "_cache_one_shot_succeeded", True)

                # CDN publication consumes the validated local result above;
                # it is never allowed to become a second extractor owner.
                if getattr(config, "CDN_ENABLED", False):
                    asset = await cdn.ensure_ready(target, quality_tier=target_tier)
                    if asset:
                        setattr(target, "local_path", asset.local_path)
                        setattr(target, "file_path", asset.play_url or asset.local_path)
                # A skip/force operation may have changed "next" while this
                # task was downloading.  The file remains a valid disk-cache
                # hit, but must not be published onto the stale queue object.
                current_targets = queue.get_queue(chat_id)[
                    1 : 1 + int(config.PRELOAD_DEPTH)
                ]
                if not any(self._same_media(item, target) for item in current_targets):
                    metrics.inc("prefetch_discarded_stale")
                    return
                metrics.inc("prefetch_ready")
            except asyncio.CancelledError:
                metrics.inc("prefetch_cancelled")
                # Explicit cancellation boundary: don't propagate further
                pass
            except Exception:
                metrics.inc("prefetch_failed")
            finally:
                setattr(target, "_cache_one_shot_complete", True)
                self._remember_terminal_outcome(target)

        async def _runner(target: Media | Track) -> None:
            # Warming a *future* queue item is background work: it yields lane
            # permits to foreground /play and /vplay and pauses while any
            # foreground request is waiting.  It is only ever delayed, never
            # cancelled, and join_or_download() joins it under
            # PREFETCH_JOIN_TIMEOUT before downloading itself at foreground
            # priority — so a paused prefetch can never hold playback back.
            with background_scope():
                await _prefetch_runner(target)

        if not reuse_existing:
            task = asyncio.create_task(_runner(media))
            self.prefetch[chat_id] = (media, task)

            def _cleanup(_):
                current = self.prefetch.get(chat_id)
                if current and current[1] is task:
                    self.prefetch.pop(chat_id, None)

            task.add_done_callback(_cleanup)

        # On capable resource tiers, optionally warm one additional waiting
        # item.  It uses the same yt/CDN singleflight layer, so repeated calls
        # cannot create duplicate downloads.
        second_media = unresolved[1] if len(unresolved) > 1 else None
        if (
            second_media
            and not second_media.file_path
            and allow_prefetch_video(
                quality_tier, bool(getattr(second_media, "video", False))
            )
        ):
            old_second = self.secondary.get(chat_id)
            if old_second and not old_second[1].done():
                if self._same_media(old_second[0], second_media):
                    return
                old_second[1].cancel()
                metrics.inc("prefetch_cancelled_stale")
            second_task = asyncio.create_task(_runner(second_media))
            self.secondary[chat_id] = (second_media, second_task)

            def _cleanup_second(_):
                current = self.secondary.get(chat_id)
                if current and current[1] is second_task:
                    self.secondary.pop(chat_id, None)

            second_task.add_done_callback(_cleanup_second)

    async def join_or_download(
        self,
        chat_id: int,
        media: Media | Track,
        quality_tier: str | None = None,
    ) -> str | None:
        # Never route non-YouTube sources through yt-dlp extraction.
        src = str(getattr(media, "source", "") or "").lower()
        if src in {
            "telegram_remote", "telegram_local",
            "tiktok_remote", "tiktok_local",
            "facebook_remote", "facebook_local",
            "soundcloud", "soundcloud_remote", "soundcloud_local",
        }:
            return None
        self._adopt_terminal_outcome(media)
        min_b = 512 * 1024 if bool(getattr(media, "video", False)) else 64 * 1024
        existing_path = getattr(media, "local_path", None) or getattr(
            media, "file_path", None
        )
        if yt.is_complete_media_file(existing_path, min_bytes=min_b):
            media.file_path = existing_path
            return existing_path

        prefetched = self.prefetch.get(chat_id)
        source_map = self.prefetch
        if not prefetched or not self._same_media(prefetched[0], media):
            prefetched = self.secondary.get(chat_id)
            source_map = self.secondary
        if prefetched and self._same_media(prefetched[0], media):
            owner, task = prefetched
            self._merge_owner_context(owner, media)
            task = prefetched[1]
            if not task.done():
                # Queue auto-next joining its own warm task: the prefetch runs
                # at background priority, so lift its queued permits to
                # foreground now that playback is actually waiting on it.
                dynamic_capacity.promote_if_foreground(task)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=max(1.0, float(config.PREFETCH_JOIN_TIMEOUT)),
                    )
                except Exception:
                    pass
            if task.done():
                source_map.pop(chat_id, None)
            self._copy_owner_result(owner, media)
            existing_path = getattr(media, "local_path", None) or getattr(
                media, "file_path", None
            )
            if yt.is_complete_media_file(existing_path, min_bytes=min_b):
                media.file_path = existing_path
                return existing_path

        # The queue/preload owner already made its one extraction attempt.  A
        # failed one is terminal for this queue entry; the next user command is
        # a new request and may try again.
        if getattr(media, "_cache_one_shot_attempted", False):
            return None

        video = bool(getattr(media, "video", False))
        owner_tier = yt.resolve_download_quality_tier(quality_tier, video=video)
        setattr(media, "_cache_quality_tier", owner_tier)
        setattr(media, "_cache_one_shot", True)
        setattr(media, "_cache_one_shot_attempted", True)
        media.file_path = await yt.download(
            media.id,
            video=video,
            quality_tier=owner_tier,
            progress_media=media,
            one_shot=True,
            quality_tier_resolved=True,
        )
        setattr(media, "_cache_one_shot_complete", True)
        if yt.is_complete_media_file(media.file_path, min_bytes=min_b):
            media.local_path = media.file_path
            setattr(media, "_cache_one_shot_succeeded", True)
            self._remember_terminal_outcome(media)
            return media.file_path
        self._remember_terminal_outcome(media)
        return None
