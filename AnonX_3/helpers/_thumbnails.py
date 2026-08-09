# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import os
import re
import shutil
import subprocess
import hashlib
import aiohttp
from PIL import (Image, ImageDraw, ImageEnhance,
                 ImageFilter, ImageFont, ImageOps, features)
from PIL import UnidentifiedImageError

from AnonX_3 import app, config, db, logger
from AnonX_3.core.dynamic_capacity import background_scope, dynamic_capacity
from AnonX_3.core.resource_budget import thumb_cheap, thumb_size
from AnonX_3.helpers import Track


class Thumbnail:
    def __init__(self):
        self.rect = (914, 514)
        self.fill = (255, 255, 255)
        self.mask = Image.new("L", self.rect, 0)
        self.font1 = self._load_font(30, bold=True)
        self.font2 = self._load_font(30)
        self.session: aiohttp.ClientSession | None = None
        self._emoji_font_warned = False
        self._render_queue: asyncio.Queue = asyncio.Queue()
        self._render_inflight: dict[tuple[str, tuple[int, int]], asyncio.Future] = {}
        self._worker_task: asyncio.Task | None = None
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12)
            )
        self._ensure_worker()

    async def close(self) -> None:
        self._closing = True
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    def _ensure_worker(self) -> None:
        if self._closing:
            return
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._render_worker(),
            name="critical:thumbnail-renderer",
        )
        try:
            from AnonX_3 import tasks

            tasks.append(self._worker_task)
        except Exception:
            pass

        def _restart(done: asyncio.Task) -> None:
            if self._worker_task is done:
                self._worker_task = None
            if not self._closing:
                try:
                    asyncio.get_running_loop().call_soon(self._ensure_worker)
                except RuntimeError:
                    pass

        self._worker_task.add_done_callback(_restart)

    async def _render_worker(self) -> None:
        while True:
            item = await self._render_queue.get()
            # Support both legacy 4-tuple and cheap-mode 5-tuple jobs.
            if len(item) == 5:
                key, song, size, cheap, future = item
            else:
                key, song, size, future = item
                cheap = False
            try:
                # A PIL render competes with the event loop for the GIL, which
                # is exactly what makes active VC audio stutter. Hold it back
                # (bounded) while a /play or /vplay request is waiting for a
                # lane permit, then render at background priority.
                await dynamic_capacity.defer_background(timeout=2.0)
                with background_scope():
                    result = await self._generate_now(song, size=size, cheap=cheap)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as ex:
                logger.exception("Thumbnail worker failed for key=%s: %s", key[0], ex)
                if not future.done():
                    future.set_result(
                        await db.get_bot_image("start_img")
                        or await db.get_bot_image("default_thumb")
                    )
            finally:
                self._render_inflight.pop(key, None)
                self._render_queue.task_done()

    def _load_font(self, size: int, bold: bool = False, prefer_myanmar: bool = False):
        helper_dir = os.path.dirname(__file__)
        latin_candidates = [
            os.path.join(helper_dir, "Raleway-Bold.ttf") if bold else os.path.join(helper_dir, "Inter-Light.ttf"),
        ]
        myanmar_candidates = [
            os.path.join(helper_dir, "fonts", "Pyidaungsu-Bold.ttf") if bold else os.path.join(helper_dir, "fonts", "Pyidaungsu-Regular.ttf"),
            os.path.join(helper_dir, "fonts", "Pyidaungsu-Regular.ttf"),
            "C:/Windows/Fonts/mmrtextb.ttf" if bold else "C:/Windows/Fonts/mmrtext.ttf",
            "/usr/share/fonts/noto/NotoSansMyanmar-Bold.ttf" if bold else "/usr/share/fonts/noto/NotoSansMyanmar-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansMyanmar[wdth,wght].ttf",
            "/usr/share/fonts/opentype/noto/NotoSansMyanmar-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansMyanmar-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansMyanmarUI-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansMyanmarUI-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMyanmarUI-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansMyanmarUI-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMyanmar[wdth,wght].ttf",
            "/usr/share/fonts/truetype/padauk/Padauk-Bold.ttf" if bold else "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf",
        ]
        fallback_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        candidates = (
            myanmar_candidates + latin_candidates + fallback_candidates
            if prefer_myanmar
            else latin_candidates + myanmar_candidates + fallback_candidates
        )
        for path in candidates:
            if path and os.path.exists(path):
                try:
                    layout_engine = getattr(getattr(ImageFont, "Layout", None), "RAQM", None)
                    if layout_engine is not None and features.check("raqm"):
                        return ImageFont.truetype(path, size, layout_engine=layout_engine)
                except Exception:
                    pass
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()

    def _load_emoji_font(self, size: int):
        candidates = [
            "C:/Windows/Fonts/seguiemj.ttf",
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
            "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
            "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/opentype/noto/NotoEmoji-Regular.ttf",
            "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
            "/usr/share/fonts/truetype/ttf-ancient-scripts/Symbola_hint.ttf",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
        if not self._emoji_font_warned:
            self._emoji_font_warned = True
            logger.warning(
                "No dedicated emoji font found for thumbnail rendering; THUMB_BOT_NAME emojis may degrade on this host."
            )
        return self._load_font(size, bold=True)

    def _bot_name(self) -> str:
        # Dynamic: THUMB_BOT_NAME env → bot profile → config fallback
        label = str(getattr(config, "THUMB_BOT_NAME", "") or "").strip()
        if label:
            return label[:40]

        me = getattr(app, "me", None)
        first_name = getattr(me, "first_name", None)
        last_name = getattr(me, "last_name", None)
        label = " ".join(part for part in (first_name, last_name) if part).strip()
        if label:
            return label[:40]
        # Final fallback — still dynamic via config default
        return str(getattr(config, "THUMB_BOT_NAME", "") or "AnonX_3").strip()[:40]

    def _thumb_signature(self) -> str:
        payload = "|".join(
            [
                self._bot_name(),
                str(getattr(config, "THUMB_TOP_TEXT", "") or ""),
                str(getattr(config, "THUMB_CREDIT_TEXT", "") or ""),
                "warm-split-player-v16",
            ]
        ).encode("utf-8", "ignore")
        return hashlib.md5(payload).hexdigest()[:8]

    def _cache_key(self, value) -> str:
        raw = str(value or "").strip()
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")
        if safe:
            return safe[:80]
        return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:12]

    def _has_myanmar_text(self, text: str) -> bool:
        return any("\u1000" <= char <= "\u109f" or "\uaa60" <= char <= "\uaa7f" for char in text)

    def _is_variation_selector(self, char: str) -> bool:
        code = ord(char)
        return 0xfe00 <= code <= 0xfe0f or 0xe0100 <= code <= 0xe01ef

    def _is_emoji_modifier(self, char: str) -> bool:
        code = ord(char)
        return 0x1f3fb <= code <= 0x1f3ff

    def _is_regional_indicator(self, char: str) -> bool:
        code = ord(char)
        return 0x1f1e6 <= code <= 0x1f1ff

    def _is_emoji_char(self, char: str) -> bool:
        code = ord(char)
        return (
            0x1f000 <= code <= 0x1faff
            or 0x2600 <= code <= 0x27bf
            or 0x2300 <= code <= 0x23ff
            or 0x2b00 <= code <= 0x2bff
            or self._is_regional_indicator(char)
            or self._is_variation_selector(char)
            or self._is_emoji_modifier(char)
            or code == 0x200d
            or code == 0x20e3
        )

    def _bot_name_segments(self, text: str) -> list[tuple[str, bool]]:
        segments = []
        clusters: list[tuple[str, bool]] = []
        current_cluster = ""
        current_is_emoji = False
        join_next = False

        for char in text:
            is_connector = (
                self._is_variation_selector(char)
                or self._is_emoji_modifier(char)
                or char == "\u200d"
                or ord(char) == 0x20e3
            )
            continues_flag = (
                self._is_regional_indicator(char)
                and current_cluster
                and self._is_regional_indicator(current_cluster[-1])
            )
            is_emoji = self._is_emoji_char(char)
            if (
                current_cluster
                and (is_connector or join_next or continues_flag)
            ):
                current_cluster += char
                current_is_emoji = current_is_emoji or is_emoji
            else:
                if current_cluster:
                    clusters.append((current_cluster, current_is_emoji))
                current_cluster = char
                current_is_emoji = is_emoji
            join_next = char == "\u200d"

        if current_cluster:
            clusters.append((current_cluster, current_is_emoji))

        current = ""
        current_is_emoji = False
        for cluster, is_emoji in clusters:
            if current and is_emoji != current_is_emoji:
                segments.append((current, current_is_emoji))
                current = ""
            current += cluster
            current_is_emoji = is_emoji
        if current:
            segments.append((current, current_is_emoji))
        return segments

    def _segments_width(self, draw: ImageDraw.ImageDraw, segments, text_font, emoji_font) -> int:
        width = 0
        for segment, is_emoji in segments:
            font = emoji_font if is_emoji else text_font
            try:
                bbox = draw.textbbox((0, 0), segment, font=font)
                segment_width = bbox[2] - bbox[0]
            except Exception:
                segment_width = 0
            if segment_width <= 0:
                segment_width = max(1, int(len(segment) * getattr(font, "size", 24) * 0.65))
            width += segment_width
        return width

    def _safe_text_width(self, draw: ImageDraw.ImageDraw, text: str, font, size: int) -> int:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            width = bbox[2] - bbox[0]
        except Exception:
            width = 0
        if width <= 0 and text:
            width = max(1, int(len(text) * size * 0.65))
        return width

    def _segments_left_bearing(self, draw: ImageDraw.ImageDraw, segments, text_font, emoji_font) -> int:
        if not segments:
            return 0
        segment, is_emoji = segments[0]
        font = emoji_font if is_emoji else text_font
        try:
            bbox = draw.textbbox((0, 0), segment, font=font)
            return bbox[0]
        except Exception:
            return 0

    def _draw_segments(self, draw: ImageDraw.ImageDraw, xy, segments, text_font, emoji_font, fill) -> None:
        x, y = xy
        for segment, is_emoji in segments:
            font = emoji_font if is_emoji else text_font
            kwargs = {"embedded_color": True} if is_emoji else {"fill": fill}
            try:
                draw.text((x, y), segment, font=font, **kwargs)
            except TypeError:
                draw.text((x, y), segment, font=font, fill=fill)
            except Exception:
                draw.text((x, y), segment, font=text_font, fill=fill)
            try:
                bbox = draw.textbbox((x, y), segment, font=font)
                segment_width = bbox[2] - bbox[0]
            except Exception:
                segment_width = 0
            if segment_width <= 0:
                segment_width = max(1, int(len(segment) * getattr(font, "size", 24) * 0.65))
            x += segment_width

    def _draw_bot_name(self, draw: ImageDraw.ImageDraw) -> None:
        text = self._bot_name()
        x, y = 50, 56
        prefer_myanmar = self._has_myanmar_text(text)
        size = 26
        font = self._load_font(size, bold=True, prefer_myanmar=prefer_myanmar)
        emoji_font = self._load_emoji_font(size)
        segments = self._bot_name_segments(text)
        max_width = 840
        while size > 18:
            if self._segments_width(draw, segments, font, emoji_font) <= max_width:
                break
            size -= 2
            font = self._load_font(size, bold=True, prefer_myanmar=prefer_myanmar)
            emoji_font = self._load_emoji_font(size)
        x -= self._segments_left_bearing(draw, segments, font, emoji_font)
        self._draw_segments(draw, (x + 2, y + 2), segments, font, emoji_font, (0, 0, 0, 220))
        self._draw_segments(draw, (x, y), segments, font, emoji_font, self.fill)

    def _draw_text_fit(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        max_width: int,
        size: int,
        *,
        bold: bool = False,
    ) -> None:
        text = str(text or "").strip()
        if not text:
            return
        prefer_myanmar = self._has_myanmar_text(text)
        font = self._load_font(size, bold=bold, prefer_myanmar=prefer_myanmar)
        while size > 18 and self._safe_text_width(draw, text, font, size) > max_width:
            size -= 2
            font = self._load_font(size, bold=bold, prefer_myanmar=prefer_myanmar)
        while len(text) > 1 and self._safe_text_width(draw, text, font, size) > max_width:
            text = text[:-2].rstrip() + "..."
        draw.text(xy, text, font=font, fill=self.fill)

    def _configured_top_text(self) -> str:
        return str(
            getattr(config, "THUMB_TOP_TEXT", "")
            or "If you want to create your own music bot, please contact @khantpainghtet"
        ).strip()

    def _configured_credit_text(self) -> str:
        return str(
            getattr(config, "THUMB_CREDIT_TEXT", "")
            or "Credit by@khantpainghtet"
        ).strip()

    def _text_bbox(self, draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
        try:
            return draw.textbbox((0, 0), text, font=font)
        except Exception:
            size = getattr(font, "size", 24)
            return (0, 0, int(len(text) * size * 0.58), size)

    def _draw_centered_text(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        text: str,
        max_width: int,
        size: int,
        *,
        bold: bool = False,
        fill: tuple[int, int, int] = (255, 255, 255),
    ) -> None:
        text = str(text or "").strip()
        if not text:
            return
        font = self._load_font(size, bold=bold, prefer_myanmar=self._has_myanmar_text(text))
        while size > 18 and self._safe_text_width(draw, text, font, size) > max_width:
            size -= 2
            font = self._load_font(size, bold=bold, prefer_myanmar=self._has_myanmar_text(text))
        bbox = self._text_bbox(draw, text, font)
        x = (1280 - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), text, font=font, fill=fill)

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font,
        max_width: int,
        max_lines: int,
    ) -> list[str]:
        words = str(text or "Unknown").strip().split()
        if not words:
            return ["Unknown"]
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and self._safe_text_width(draw, candidate, font, getattr(font, "size", 32)) > max_width:
                lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        if words and len(lines) == max_lines:
            joined = " ".join(lines)
            if len(joined) < len(" ".join(words)):
                while lines[-1] and self._safe_text_width(draw, lines[-1] + "...", font, getattr(font, "size", 32)) > max_width:
                    lines[-1] = lines[-1][:-1].rstrip()
                lines[-1] = lines[-1].rstrip() + "..."
        return lines

    def _draw_multiline_title(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        max_width: int,
        max_lines: int,
        size: int,
    ) -> None:
        font = self._load_font(size, bold=True, prefer_myanmar=self._has_myanmar_text(text))
        while size > 30:
            lines = self._wrap_text(draw, text, font, max_width, max_lines)
            if all(self._safe_text_width(draw, line, font, size) <= max_width for line in lines):
                break
            size -= 2
            font = self._load_font(size, bold=True, prefer_myanmar=self._has_myanmar_text(text))
        x, y = xy
        for line in lines:
            draw.text((x, y), line, font=font, fill=(255, 255, 255))
            y += int(size * 1.35)

    @staticmethod
    def _time_to_seconds(value) -> int:
        if isinstance(value, (int, float)):
            return max(0, int(value))
        text = str(value or "").strip()
        if not text:
            return 0
        parts = text.split(":")
        try:
            total = 0
            for part in parts:
                total = total * 60 + int(part)
            return max(0, total)
        except Exception:
            return 0

    @staticmethod
    def _format_clock(seconds: int) -> str:
        seconds = max(0, int(seconds))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{sec:02d}"
        return f"{minutes}:{sec:02d}"

    def _progress_values(self, song: Track) -> tuple[str, str, float]:
        position = max(1, self._time_to_seconds(getattr(song, "time", 0)))
        duration_seconds = self._time_to_seconds(getattr(song, "duration_sec", 0))
        duration_text = str(getattr(song, "duration", "") or "0:00").strip()
        if duration_seconds <= 0:
            duration_seconds = self._time_to_seconds(duration_text)
        if duration_seconds <= 0:
            duration_seconds = max(position, 1)
            duration_text = self._format_clock(duration_seconds)
        ratio = min(max(position / max(duration_seconds, 1), 0.46), 1.0)
        return self._format_clock(position), duration_text, ratio

    def _local_default_thumb(self) -> str:
        configured = str(getattr(config, "DEFAULT_THUMB", "") or "").strip()
        if configured and os.path.exists(configured):
            return configured
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "plugins", "img", "welcome.jpg")
        )

    def _write_placeholder_thumb(self, output_path: str) -> str:
        image = Image.new("RGB", self.rect, (31, 35, 45))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (0, 0, self.rect[0] - 1, self.rect[1] - 1),
            radius=15,
            outline=(95, 103, 118),
            width=4,
        )
        image.save(output_path, "JPEG", quality=90)
        return output_path

    async def _save_first_available_thumb(self, output_path: str, sources: list[str | None]) -> str:
        last_error: Exception | None = None
        seen: set[str] = set()
        for source in sources:
            source = str(source or "").strip()
            if not source or source in seen:
                continue
            seen.add(source)
            try:
                return await self.save_thumb(output_path, source)
            except Exception as ex:
                last_error = ex
                logger.warning(
                    "Thumbnail source failed source=%r; trying next fallback: %s",
                    source,
                    ex,
                )
        logger.warning("No usable thumbnail source found; rendering placeholder card: %s", last_error)
        return self._write_placeholder_thumb(output_path)

    async def save_thumb(self, output_path: str, source: str) -> str:
        source = str(source or "").strip()
        if not source:
            raise ValueError("Empty thumbnail source")
        if os.path.exists(source):
            shutil.copyfile(source, output_path)
            return output_path
        if source.startswith(("http://", "https://")):
            if not self.session:
                self.session = aiohttp.ClientSession()
            async with self.session.get(source) as resp:
                resp.raise_for_status()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(
                        f"Remote thumbnail is not an image: {content_type}"
                    )
                with open(output_path, "wb") as f:
                    f.write(await resp.read())
            try:
                with Image.open(output_path) as image:
                    image.verify()
            except (UnidentifiedImageError, OSError) as ex:
                try:
                    os.remove(output_path)
                except Exception:
                    pass
                raise ValueError(f"Invalid thumbnail image from {source}") from ex
            return output_path
        # Telegram file_id / file reference fallback.
        await app.download_media(source, file_name=output_path)
        return output_path

    async def generate_video_thumb(self, song: Track) -> str | None:
        try:
            sig = self._thumb_signature()
            media_id = self._cache_key(getattr(song, "id", None))
            output = f"cache/{media_id}_{sig}_video_thumb_v16.jpg"
            if os.path.exists(output):
                return output

            source = await self.generate(song)
            if not source or not os.path.exists(source):
                return None

            image = Image.open(source).convert("RGB")
            image.thumbnail((320, 320), Image.Resampling.LANCZOS)

            for quality in (85, 75, 65, 55, 45, 35):
                image.save(output, "JPEG", quality=quality, optimize=True)
                if os.path.getsize(output) < 190 * 1024:
                    return output

            return output
        except Exception as ex:
            logger.warning(
                "Thumbnail video thumb generation failed for media_id=%s: %s",
                getattr(song, "id", None),
                ex,
            )
            return None

    def _render_card_sync(
        self,
        temp: str,
        output: str,
        song: Track,
        size: tuple,
        cheap: bool = False,
    ) -> str:
        canvas_w, canvas_h = size
        thumb = Image.open(temp).convert("RGBA")
        bg = ImageOps.fit(thumb, size, method=Image.Resampling.LANCZOS, centering=(0.54, 0.48))
        # Under poor tier, skip the heaviest blur/glow work so thumbnails stay
        # available without blocking CPU needed for voice/video.
        if cheap:
            bg = bg.filter(ImageFilter.GaussianBlur(12))
            bg = ImageEnhance.Color(bg).enhance(1.35)
            bg = ImageEnhance.Brightness(bg).enhance(0.34)
        else:
            bg = bg.filter(ImageFilter.GaussianBlur(36))
            bg = ImageEnhance.Color(bg).enhance(1.75)
            bg = ImageEnhance.Brightness(bg).enhance(0.30)
        image = Image.new("RGBA", size, (70, 27, 0, 255))
        image.alpha_composite(bg)
        overlay = Image.new("RGBA", size, (73, 27, 0, 126))
        image.alpha_composite(overlay)

        if not cheap:
            glow = Image.new("RGBA", size, (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            glow_draw.ellipse((650, 95, 1240, 675), fill=(150, 87, 30, 68))
            glow_draw.ellipse((810, 55, 1130, 340), fill=(205, 144, 70, 34))
            glow_draw.rectangle((0, 0, 95, canvas_h), fill=(105, 43, 0, 34))
            glow_draw.rectangle((1185, 0, canvas_w, canvas_h), fill=(105, 43, 0, 34))
            image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(58)))

        artwork_box = (102, 102, 620, 620)
        artwork = ImageOps.fit(
            thumb,
            (artwork_box[2] - artwork_box[0], artwork_box[3] - artwork_box[1]),
            method=Image.Resampling.LANCZOS,
            centering=(0.47, 0.5),
        )
        artwork = ImageEnhance.Color(artwork).enhance(1.08)
        artwork = ImageEnhance.Brightness(artwork).enhance(1.02)

        mask = Image.new("L", artwork.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, artwork.size[0] - 1, artwork.size[1] - 1), radius=31, fill=255)
        bordered = Image.new("RGBA", (artwork.size[0] + 10, artwork.size[1] + 10), (0, 0, 0, 0))
        border_mask = Image.new("L", bordered.size, 0)
        ImageDraw.Draw(border_mask).rounded_rectangle((0, 0, bordered.size[0] - 1, bordered.size[1] - 1), radius=37, fill=255)
        border = Image.new("RGBA", bordered.size, (255, 255, 255, 255))
        bordered.paste(border, (0, 0), border_mask)
        bordered.paste(artwork, (5, 5), mask)
        image.paste(bordered, (artwork_box[0] - 5, artwork_box[1] - 5), bordered)

        draw = ImageDraw.Draw(image)
        self._draw_centered_text(
            draw,
            38,
            self._configured_top_text(),
            1040,
            31,
            fill=(255, 255, 255),
        )

        label_font = self._load_font(34)
        draw.text((660, 176), "Now Playing", font=label_font, fill=(255, 207, 88))
        self._draw_multiline_title(
            draw,
            (660, 242),
            getattr(song, "title", None) or "Unknown",
            510,
            2,
            42,
        )

        position_text, duration_text, ratio = self._progress_values(song)
        track_x1, track_y, track_x2 = 660, 487, 1162
        draw.line((track_x1, track_y, track_x2, track_y), fill=(224, 224, 224), width=7)
        progress_x = int(track_x1 + (track_x2 - track_x1) * ratio)
        draw.line((track_x1, track_y, progress_x, track_y), fill=(255, 206, 68), width=7)
        draw.ellipse((progress_x - 4, track_y - 4, progress_x + 4, track_y + 4), fill=(255, 206, 68))

        time_font = self._load_font(30)
        draw.text((660, 512), position_text, font=time_font, fill=(255, 255, 255))
        duration_bbox = self._text_bbox(draw, duration_text, time_font)
        draw.text((1162 - (duration_bbox[2] - duration_bbox[0]), 512), duration_text, font=time_font, fill=(255, 255, 255))

        control_font = self._load_font(44, bold=True)
        draw.text((735, 586), "<<", font=control_font, fill=(255, 255, 255))
        draw.text((898, 585), "II", font=control_font, fill=(255, 255, 255))
        draw.text((1042, 586), ">>", font=control_font, fill=(255, 255, 255))

        self._draw_centered_text(
            draw,
            684,
            self._configured_credit_text(),
            760,
            30,
            fill=(255, 255, 255),
        )

        image.convert("RGB").save(output)
        return output

    async def generate(
        self,
        song: Track,
        size: tuple[int, int] | None = None,
        quality_tier: str | None = None,
    ) -> str:
        cheap = thumb_cheap(quality_tier)
        if size is None:
            size = thumb_size(quality_tier)
        media_id = self._cache_key(getattr(song, "id", None))
        normalized_size = (int(size[0]), int(size[1]))
        mode = "c" if cheap else "f"
        output = f"cache/{media_id}_{self._thumb_signature()}_v16{mode}_{normalized_size[0]}x{normalized_size[1]}.png"
        if os.path.exists(output):
            return output

        self._ensure_worker()
        key = (media_id, normalized_size, mode)
        existing = self._render_inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        future = asyncio.get_running_loop().create_future()
        self._render_inflight[key] = future
        await self._render_queue.put((key, song, normalized_size, cheap, future))
        return await asyncio.shield(future)

    async def _generate_now(
        self,
        song: Track,
        size=(1280, 720),
        cheap: bool = False,
    ) -> str:
        try:
            media_id = self._cache_key(getattr(song, "id", None))
            temp = f"cache/temp_{media_id}.jpg"
            sig = self._thumb_signature()
            mode = "c" if cheap else "f"
            normalized_size = (int(size[0]), int(size[1]))
            output = f"cache/{media_id}_{sig}_v16{mode}_{normalized_size[0]}x{normalized_size[1]}.png"
            if os.path.exists(output):
                return output

            thumb_source = getattr(song, "thumbnail", None)
            # Prefer stream art, then /setstart (start_img), then legacy default.
            # ffmpeg may take several seconds on a remote/large artifact. Keep
            # it off the event loop so commands and callbacks stay responsive.
            extracted_thumb = await asyncio.to_thread(
                self._extract_video_thumb_frame,
                getattr(song, "file_path", None),
                media_id,
            )
            if extracted_thumb:
                thumb_source = extracted_thumb
            await self._save_first_available_thumb(
                temp,
                [
                    thumb_source,
                    await db.get_bot_image("start_img"),
                    await db.get_bot_image("default_thumb"),
                    self._local_default_thumb(),
                ],
            )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._render_card_sync(temp, output, song, size, cheap=cheap),
            )

            try: os.remove(temp)
            except Exception: pass
            if extracted_thumb:
                try: os.remove(extracted_thumb)
                except Exception: pass
            return output
        except Exception as ex:
            logger.warning(
                "Thumbnail card generation failed for media_id=%s: %s",
                getattr(song, "id", None),
                ex,
            )
            return (
                await db.get_bot_image("start_img")
                or await db.get_bot_image("default_thumb")
            )

    def _extract_video_thumb_frame(self, file_path: str, media_id: str) -> str | None:
        if not file_path or not os.path.exists(file_path):
            return None
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".mpeg", ".mpg", ".m4v"}:
            return None

        output = f"cache/temp_{media_id}_frame.jpg"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            file_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            output,
        ]
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )
            if os.path.exists(output) and os.path.getsize(output) > 0:
                return output
        except Exception:
            return None
        return None
