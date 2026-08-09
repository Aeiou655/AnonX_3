# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import glob
import os

from pyrogram import filters, types

from AnonX_3 import app, logger, yt


def _is_json_payload(data: str) -> bool:
    text = (data or "").strip()
    return text.startswith("[") or text.startswith("{")


@app.on_message(
    filters.command(["addcookie", "addcookies"])
    & (filters.private | filters.chat(app.logger))
    & app.sudoers,
    group=-1,
)
async def add_cookie(_, message: types.Message):
    if yt.cookie_free_mode():
        return await message.reply_text(
            "<b>Cookie-free mode is active.</b> Cookie uploads are disabled."
        )
    target_message = message.reply_to_message or message
    doc = target_message.document
    if not doc:
        return await message.reply_text(
            "Reply to or send a cookie <code>.txt</code> / <code>.json</code> file with this command."
        )

    sent = await message.reply_text("Processing cookie file...")

    try:
        temp_path = f"{yt.cookie_dir}/temp_cookie_upload"
        await target_message.download(file_name=temp_path)

        with open(temp_path, "rb") as f:
            raw_bytes = f.read()
        raw_text = yt.decode_cookie_bytes(raw_bytes)
        original_name = (doc.file_name or "").lower()
        is_json = original_name.endswith(".json") or _is_json_payload(raw_text)

        txt_path = f"{yt.cookie_dir}/{yt.cookie_txt_name}"
        if is_json:
            json_path = f"{yt.cookie_dir}/{yt.cookie_json_name}"
            with open(json_path, "wb") as f:
                f.write(raw_text.encode("utf-8"))
            netscape = yt.json_cookie_to_netscape(raw_text)
            with open(txt_path, "wb") as f:
                f.write(netscape.encode("utf-8"))
            yt.strip_cookie_bom(txt_path)
            yt.protect_cookie_file(txt_path)
            file_name = f"{yt.cookie_json_name} -> {yt.cookie_txt_name}"
        else:
            with open(txt_path, "wb") as f:
                f.write(raw_bytes)
            yt.strip_cookie_bom(txt_path)
            yt.protect_cookie_file(txt_path)
            file_name = yt.cookie_txt_name

        # Clean up temp file if still exists
        if os.path.exists(temp_path):
            os.remove(temp_path)

        # Remove any other .txt cookies to avoid conflicts (keep canonical cookies.txt only)
        for old in glob.glob(f"{yt.cookie_dir}/*.txt"):
            if os.path.abspath(old) != os.path.abspath(txt_path):
                os.remove(old)

        # Reset yt-dlp cookie cache so the new file is picked up immediately
        yt.cookies.clear()
        yt.checked = False

        await sent.edit_text(f"Cookie file saved as <code>{file_name}</code>.")
        logger.info("Cookie file updated by sudo user %s: %s", message.from_user.id, file_name)
    except Exception as ex:
        logger.exception("Failed to save cookie file: %s", ex)
        await sent.edit_text(f"Failed to save cookie file: <code>{type(ex).__name__}</code>")


@app.on_message(
    filters.command(["cookies"])
    & (filters.private | filters.chat(app.logger))
    & app.sudoers,
    group=-1,
)
async def check_cookies(_, message: types.Message):
    if yt.cookie_free_mode():
        return await message.reply_text(
            "<b>Cookie-free mode is active.</b> No cookie file or browser "
            "session is used."
        )
    import time as _time
    from datetime import datetime, timezone

    txt_path = f"{yt.cookie_dir}/{yt.cookie_txt_name}"
    # Auto-convert cookies.json -> cookies.txt when json exists but txt is missing
    if not os.path.isfile(txt_path):
        yt.get_cookies()
    if not os.path.isfile(txt_path):
        return await message.reply_text("<b>No cookies.txt found.</b>")

    text = ""
    try:
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                with open(txt_path, "r", encoding=enc) as fh:
                    text = fh.read()
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
    except Exception as ex:
        return await message.reply_text(
            f"Failed to read cookies.txt: <code>{type(ex).__name__}</code>"
        )

    if not text:
        return await message.reply_text("<b>cookies.txt is empty or unreadable.</b>")

    now = int(_time.time())
    valid: list[tuple[str, str, int]] = []
    expired: list[tuple[str, str, int]] = []

    for line in text.splitlines():
        row = line.strip()
        if not row or row.startswith("#"):
            continue
        parts = row.split("\t")
        if len(parts) < 7:
            continue
        name = parts[5]
        try:
            expires_ts = int(parts[4])
        except ValueError:
            continue
        entry = (name, parts[0].replace("#HttpOnly_", ""), expires_ts)
        if expires_ts > now:
            valid.append(entry)
        else:
            expired.append(entry)

    def _fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def _delta(ts: int) -> str:
        diff = ts - now
        days = abs(diff) // 86400
        if diff >= 0:
            return f"{days}d left"
        return f"{days}d ago"

    lines = [f"<b>Cookies Status</b>  |  <code>{yt.cookie_txt_name}</code>\n"]
    lines.append(f"<b>Valid ({len(valid)}):</b>")
    for name, domain, ts in valid:
        lines.append(f"  • <code>{name}</code> — expires {_fmt_ts(ts)} ({_delta(ts)})")
    lines.append(f"\n<b>Expired ({len(expired)}):</b>")
    if expired:
        for name, domain, ts in expired:
            lines.append(f"  • <code>{name}</code> — expired {_fmt_ts(ts)} ({_delta(ts)})")
    else:
        lines.append("  • None")

    try:
        await message.reply_text("\n".join(lines))
    except Exception as ex:
        logger.warning("Failed to send /cookies report: %s", ex)


