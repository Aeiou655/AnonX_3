# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import utils


@app.on_message(
    filters.command(["addowner", "delowner", "rmowner"]) & app.owners,
    group=-1,
)
@lang.language()
async def _owner(_, m: types.Message):
    global o_mention
    user = await utils.extract_user(m)
    if not user:
        return await m.reply_text(m.lang["user_not_found"])

    if m.command[0] == "addowner":
        if user.id in app.owners:
            return await m.reply_text(f"{user.mention} is already an owner.")

        app.owners.add(user.id)
        app._sudo_ids.add(user.id)
        await db.add_owner(user.id)
        o_mention = None
        await m.reply_text(f"Added {user.mention} to the owner list.")
    else:
        if user.id == app.owner:
            return await m.reply_text("The main OWNER_ID cannot be removed by command.")
        if user.id not in app.owners:
            return await m.reply_text(f"{user.mention} is not an owner.")

        app.owners.discard(user.id)
        app._sudo_ids.discard(user.id)
        await db.del_owner(user.id)
        o_mention = None
        await m.reply_text(f"Removed {user.mention} from the owner list.")


@app.on_message(
    filters.command(["addsudo", "delsudo", "rmsudo"]) & app.owners,
    group=-1,
)
@lang.language()
async def _sudo(_, m: types.Message):
    user = await utils.extract_user(m)
    if not user:
        return await m.reply_text(m.lang["user_not_found"])

    if m.command[0] == "addsudo":
        if user.id in app._sudo_ids:
            return await m.reply_text(m.lang["sudo_already"].format(user.mention))

        app._sudo_ids.add(user.id)
        await db.add_sudo(user.id)
        await m.reply_text(m.lang["sudo_added"].format(user.mention))
    else:
        if user.id not in app._sudo_ids:
            return await m.reply_text(m.lang["sudo_not"].format(user.mention))

        app._sudo_ids.discard(user.id)
        await db.del_sudo(user.id)
        await m.reply_text(m.lang["sudo_removed"].format(user.mention))


o_mention = None


def _format_sudo_lines(mentions: list[str], empty_text: str) -> str:
    if not mentions:
        return empty_text

    lines = []
    last_index = len(mentions) - 1
    for index, mention in enumerate(mentions):
        prefix = "└" if index == last_index else "├"
        lines.append(f"{prefix} {mention}")
    return "\n".join(lines)

@app.on_message(
    filters.command(["listsudo", "sudolist"])
    & filters.chat(app.logger)
    & app.sudoers,
    group=-1,
)
@lang.language()
async def _listsudo(_, m: types.Message):
    global o_mention
    sent = await m.reply_text(m.lang["sudo_fetching"])

    if not o_mention:
        owner_mentions = []
        owner_ids = [app.owner, *await db.get_owners()]
        for user_id in dict.fromkeys(owner_ids):
            try:
                owner_mentions.append((await app.get_users(user_id)).mention)
            except Exception:
                owner_mentions.append(f"<code>{user_id}</code>")
        o_mention = _format_sudo_lines(owner_mentions, f"<code>{app.owner}</code>")

    sudoers = await db.get_sudoers()
    mentions = []
    for user_id in sudoers:
        try:
            mentions.append((await app.get_users(user_id)).mention)
        except Exception:
            continue

    template = await db.get_custom_text_for_chat(
        m.chat.id,
        "sudo_list",
        m.lang["sudo_list"],
    )
    sudo_lines = _format_sudo_lines(mentions, m.lang["sudo_list_empty"])
    await utils.edit_formatted(
        sent,
        template,
        o_mention,
        sudo_lines,
        template_key="sudo_list",
    )
