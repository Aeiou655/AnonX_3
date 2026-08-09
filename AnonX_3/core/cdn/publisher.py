# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Atomic publish: downloads/tmp → ready/."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def safe_filename(media_id: str, ext: str, quality_tier: str | None = None, video: bool = False) -> str:
    try:
        from AnonX_3.core.security import sanitize_filename

        mid = sanitize_filename(str(media_id or "unknown"), default="unknown")
        ext_clean = sanitize_filename((ext or "mp4").lstrip("."), default="mp4", max_len=16)
    except Exception:
        mid = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(media_id or "unknown"))
        mid = mid.strip("._") or "unknown"
        ext_clean = (ext or "mp4").lstrip(".").lower() or "mp4"
    ext_clean = ext_clean.lstrip(".").lower() or "mp4"
    if video and quality_tier:
        try:
            from AnonX_3.core.security import sanitize_filename

            tier = sanitize_filename(str(quality_tier), default="auto", max_len=32)
        except Exception:
            tier = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(quality_tier))
        return f"{mid}.{tier}.{ext_clean}"
    return f"{mid}.{ext_clean}"


def atomic_publish(src: Path, dest: Path) -> Path:
    """
    Atomically place src at dest.

    Uses a same-directory temp then os.replace so readers never see a partial file.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.exists():
        raise FileNotFoundError(f"CDN publish source missing: {src}")

    # Path jail: destination basename only under dest.parent
    dest = dest.parent / Path(dest.name).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Same-volume replace when possible.
    if src.resolve() == dest.resolve():
        return dest

    tmp_dest = dest.with_suffix(dest.suffix + ".publishing")
    try:
        if tmp_dest.exists():
            tmp_dest.unlink()
    except Exception:
        pass

    try:
        # Prefer hardlink/move across same filesystem; fall back to copy+replace.
        try:
            os.replace(str(src), str(tmp_dest))
        except OSError:
            shutil.copy2(str(src), str(tmp_dest))
            try:
                src.unlink()
            except Exception:
                pass
        os.replace(str(tmp_dest), str(dest))
    except Exception:
        try:
            if tmp_dest.exists():
                tmp_dest.unlink()
        except Exception:
            pass
        raise
    return dest
