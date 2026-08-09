# Broadcast Function Documentation

ဒီ project ထဲက broadcast feature အားလုံးကို `AnonX_3/plugins/broadcast.py` မှာထားထားသည်။ Command တွေကို sudo user တွေပဲသုံးနိုင်ပြီး replied message တစ်ခုကို served groups/users ဆီ forward သို့ copy လုပ်ပေးသည်။

## Source Files

- `AnonX_3/plugins/broadcast.py` - main broadcast command handlers
- `AnonX_3/core/mongo.py` - served chats/users list ကို MongoDB cache/collections မှယူသည်
- `AnonX_3/plugins/start.py` - `/start` နှင့် bot added event မှ served chat/user IDs သိမ်းသည်
- `AnonX_3/core/bot.py` - `OWNER_ID`, `LOGGER_ID`, `sudoers` filter, logger group setup
- `AnonX_3/locales/*.json` - broadcast response/help messages

## Access Control

- `/broadcast`, `/stop_gcast`, `/stop_broadcast` ကို `app.sudoers` filter ဖြင့်ကာထားသည်။
- `app.sudoers` သည် startup မှာ owner (`OWNER_ID`) နှင့် database ထဲရှိ sudo users များကို load လုပ်သည်။
- Logger group ကို `LOGGER_ID` env var မှယူပြီး bot boot တက်ချိန်တွင် bot သည် logger group admin ဖြစ်ရမည်။

## Global State

`broadcasting = False`

- Broadcast တစ်ခု run နေ/မနေကို process memory ထဲမှာသိမ်းထားသော global boolean ဖြစ်သည်။
- တစ်ကြိမ်မှာ broadcast တစ်ခုသာ run ခွင့်ပေးသည်။
- Bot restart ဖြစ်လျှင် state ပြန် `False` ဖြစ်သည်။

## `_broadcast(_, message)`

Command:

```text
/broadcast [reply to message] [-nochat] [-user] [-copy] [-pin]
```

Decorator:

```python
@app.on_message(filters.command(["broadcast"]) & app.sudoers)
@lang.language()
```

လုပ်ဆောင်ပုံ:

1. Command message သည် message တစ်ခုကို reply မလုပ်ထားလျှင် `gcast_usage` ပြန်ပို့ပြီးရပ်သည်။
2. `broadcasting` already `True` ဖြစ်နေလျှင် `gcast_active` ပြန်ပို့ပြီးရပ်သည်။
3. Reply လုပ်ထားသော message ကို broadcast payload (`msg`) အဖြစ်သတ်မှတ်သည်။
4. `-nochat` မပါလျှင် bot ၏ live dialogs ထဲရှိ group/supergroup IDs များကို scan လုပ်ပြီး MongoDB `chats` collection ထဲ auto-save လုပ်သည်။ ထို့နောက် `db.get_chats()` မှ served group/chat IDs ယူသည်။
5. `-user` ပါလျှင် bot ၏ live dialogs ထဲရှိ private user IDs များကို scan လုပ်ပြီး MongoDB `users` collection ထဲ auto-save လုပ်သည်။ ထို့နောက် `db.get_users()` မှ served private user IDs ယူသည်။
6. `groups + users` ကို target `chats` list အဖြစ်ပေါင်းသည်။
7. `broadcasting = True` ပြောင်းပြီး target loop စတင်သည်။
8. Payload message ကို logger group သို့ forward လုပ်သည်။
9. Broadcast command metadata ကို logger group သို့ပို့ပြီး pin လုပ်သည်။
10. Target တစ်ခုချင်းဆီကို concurrency limit ဖြင့်ပို့သည်။
11. `-copy` ပါလျှင် `msg.copy(chat, reply_markup=msg.reply_markup)` သုံးသည်။
12. `-copy` မပါလျှင် `msg.forward(chat)` သုံးသည်။
13. `-pin` ပါပြီး target သည် group/supergroup ဖြစ်လျှင် ပို့ပြီးသား message ကို notification မပါဘဲ pin လုပ်သည်။ Pin မအောင်မြင်လျှင် delivery ကိုမရပ်ဘဲ failed count/report ထဲထည့်သည်။
14. Target ID သည် `groups` ထဲပါလျှင် group count (`count`) တိုးသည်။ မပါလျှင် user count (`ucount`) တိုးသည်။ Private users များကို မည်သည့်အခြေအနေတွင်မဆို pin မလုပ်ပါ။
15. `FloodWait` ဖြစ်လျှင် Telegram သတ်မှတ်သည့် wait time အတိုင်းစောင့်ပြီး retry လုပ်သည်။
16. အခြား exception ဖြစ်သော target များကို `failed` string ထဲ `chat_id - error` format ဖြင့်စုသည်။
17. Failed list ရှိလျှင် `errors.txt` ဖန်တီးပြီး command caller ထံ document အဖြစ်ပို့သည်၊ ပြီးလျှင် local file ကိုဖျက်သည်။
18. ပြီးဆုံးချိန်တွင် `broadcasting = False` ပြောင်းပြီး status message ကို `gcast_end` ဖြင့် edit လုပ်သည်။ `-pin` ပါလျှင် pin အောင်မြင်/မအောင်မြင် အရေအတွက်ကိုပါ ပြသည်။

Deep-fix guard များ:

- Broadcast run စစချင်း stale `errors.txt` ရှိနေလျှင် ဖျက်သည်။
- Handler ထဲ exception ဖြစ်သော်လည်း `finally` ဖြင့် `broadcasting = False` ပြန်ထားသည်။
- `errors.txt` ကို document အဖြစ်ပို့ရာတွင် error ဖြစ်လည်း local file ကို cleanup လုပ်သည်။
- Final status message ကို edit မလုပ်နိုင်လျှင် fallback အဖြစ် reply text ပို့သည်။
- Live dialog group auto-detect မအောင်မြင်လျှင် broadcast ကိုမရပ်ဘဲ MongoDB ထဲရှိ existing `chats` list ဖြင့် ဆက်လုပ်သည်။
- Live dialog private-user auto-detect မအောင်မြင်လျှင် broadcast ကိုမရပ်ဘဲ MongoDB ထဲရှိ existing `users` list ဖြင့် ဆက်လုပ်သည်။

### Flags

| Flag | Meaning | Effect |
| --- | --- | --- |
| `-nochat` | groups/chats မပို့ရန် | `db.get_chats()` ကို skip လုပ်သည် |
| `-user` | private users ပါထည့်ရန် | live private dialogs ကို MongoDB ထဲ auto-save လုပ်ပြီး `db.get_users()` မှ users ထည့်သည် |
| `-copy` | forwarded tag မပြရန် | forward မလုပ်ဘဲ copy လုပ်သည် |
| `-pin` | recipient groups ကို pin လုပ်ရန် | အောင်မြင်စွာပို့ပြီးသော group/supergroup messages များကို အသံမထွက်ဘဲ pin လုပ်သည်။ Pin error ဖြစ်လည်း broadcast ဆက်လုပ်ပြီး summary/error report ထဲဖော်ပြသည်။ Private users များကို pin မလုပ်ပါ။ |

Examples:

```text
/broadcast
/broadcast -user
/broadcast -nochat -user
/broadcast -user -copy
/broadcast -pin
/broadcast -pin -user -copy
```

Behavior note:

- `/broadcast` default သည် groups/chats သို့ပို့ပြီး users မပါ။
- Users-only broadcast လိုချင်လျှင် `/broadcast -nochat -user` သုံးရမည်။
- `-pin` မပါလျှင် recipient message များကို pin မလုပ်ပါ။ Logger metadata pin ကတော့ အရင်အတိုင်း ဆက်ရှိသည်။
- `-pin` သည် group/supergroup recipients များအတွက်သာဖြစ်ပြီး private-user delivery များကို မ pin ပါ။ Broadcast တစ်ကြိမ်စီ၏ pin အသစ်များကို မဖယ်ရှားဘဲ ဆက် pin ထားသည်။
- Command flags များကို `message.command` မှ case-insensitive အဖြစ်ဖတ်သည်။

## `_stop_gcast(_, message)`

Commands:

```text
/stop_gcast
/stop_broadcast
```

Decorator:

```python
@app.on_message(filters.command(["stop_gcast", "stop_broadcast"]) & app.sudoers)
@lang.language()
```

လုပ်ဆောင်ပုံ:

1. `broadcasting` သည် `False` ဖြစ်လျှင် `gcast_inactive` ပြန်ပို့ပြီးရပ်သည်။
2. Active broadcast ရှိလျှင် `broadcasting = False` ပြောင်းသည်။
3. Stop action ကို logger group သို့ပို့ပြီး pin လုပ်သည်။
4. Command caller ထံ `gcast_stop` ပြန်ပို့သည်။
5. Running `_broadcast` loop သည် နောက် target iteration တွင် `not broadcasting` ကိုတွေ့ပြီး `gcast_stopped` status ဖြင့်ရပ်သည်။

## Target Data Source

### Served Groups/Chats

- `db.add_chat(chat_id)` သည် group `/start` command သို့ bot added event မှ `chats` collection ထဲသိမ်းသည်။
- `/broadcast` run ချိန်တွင် `-nochat` မပါလျှင် `app.get_dialogs()` မှ bot ရောက်နေသော group/supergroup များကို scan လုပ်ပြီး missing IDs များကို `db.add_chat(chat_id)` ဖြင့် MongoDB ထဲ auto-save လုပ်သည်။
- `db.get_chats()` သည် memory cache (`self.chats`) ပထမဆုံးစစ်ပြီး cache empty ဖြစ်လျှင် MongoDB `chats` collection မှ `_id` list ကို load လုပ်သည်။

### Served Users

- `db.add_user(user_id)` သည် private `/start` မှ `users` collection ထဲသိမ်းသည်။
- `/broadcast -user` run ချိန်တွင် `app.get_dialogs()` မှ bot နှင့် DM ရှိသော private users များကို scan လုပ်ပြီး missing IDs များကို `db.add_user(user_id)` ဖြင့် MongoDB ထဲ auto-save လုပ်သည်။
- `db.get_users()` သည် memory cache (`self.users`) ပထမဆုံးစစ်ပြီး cache empty ဖြစ်လျှင် MongoDB `users` collection မှ `_id` list ကို load လုပ်သည်။

## Locale Keys Used

Broadcast handler သုံးသော response keys:

- `gcast_usage`
- `gcast_active`
- `gcast_inactive`
- `gcast_start`
- `gcast_stop`
- `gcast_stopped`
- `gcast_log`
- `gcast_stop_log`
- `gcast_end`
- `gcast_pin_summary`

Help text ထဲမှာ `/broadcast`, `-nochat`, `-user`, `-copy`, `-pin` အသုံးပြုပုံကို `help_sudo` key အောက်မှာ ထည့်ထားသည်။

## Logger Behavior

- Broadcast payload message ကို logger group ထံ forward လုပ်သည်။
- Broadcast starter user ID, mention, full command text ပါသော log message ကို logger group ထံပို့ပြီး pin လုပ်သည်။
- `-pin` ပါသော broadcast များတွင် အောင်မြင်စွာပို့ပြီးသော group/supergroup recipient message ကို notification မပါဘဲ pin လုပ်သည်။
- Stop command ကိုလည်း logger group ထံ log ပို့ပြီး pin လုပ်သည်။

## Error Handling

- `FloodWait` exception တွင် Telegram ကညွှန်သည့် wait value ထက် 30 seconds ထပ်ပေါင်းစောင့်သည်။
- အခြား send/copy/forward errors များကို skipped target အဖြစ်မှတ်ပြီး loop ဆက်သည်။
- Recipient pin errors များကို delivery failure မဖြစ်စေဘဲ ဆက်လက် broadcast လုပ်ပြီး pin summary နှင့် `errors.txt` report တွင် ထည့်သည်။
- Failed target များရှိလျှင် `errors.txt` temporary file ထုတ်ပြီး caller ထံပို့သည်။
- `errors.txt` ဖန်တီး/ပို့/ဖျက် လုပ်ငန်းစဉ်တွင် filesystem/send failure ကြောင့် broadcast state stuck မဖြစ်အောင် guarded cleanup သုံးသည်။

## Known Implementation Notes

- `broadcasting` သည် global boolean ဖြစ်သောကြောင့် parallel worker/process များတွင် shared state မဟုတ်။
- `errors.txt` သည် fixed filename ဖြစ်သောကြောင့် process ထဲတွင် broadcast တစ်ခုသာခွင့်ပြုထားခြင်းနှင့်တွဲသုံးထားသည်။
- Stop command က active loop ကိုချက်ချင်း interrupt မလုပ်ဘဲ loop next iteration တွင်ရပ်စေသည်။
- Target list သည် broadcast စတင်ချိန်တွင် snapshot ဖြစ်ပြီး loop run နေစဉ် MongoDB ထဲထပ်ဝင်သော new users/chats မပါ။




## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX_3 and AnonX_3 .env).
