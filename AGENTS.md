## Always-On Skill (kimi-codex)

Before any task in this variant, invoke:

```python
Skill("kimi-codex")
```

This rule applies to code changes, debugging, exploration, documentation, and Q&A. Do not skip.

# မဂ်လာပါ မြန်မာ 🇲🇲 Agent Guide

## Project Overview
မဂ်လာပါ မြန်မာ 🇲🇲 is an async Telegram music and video chat bot written in Python. It streams audio or video into Telegram group video chats using Pyrogram for Telegram clients, PyTgCalls/ntgcalls for voice chat playback, MongoDB for persistent chat state, `yt-dlp` for media downloads, and `py-yt-search` for YouTube search and playlist lookup.

The actual Python application package is `AnonX_3/`. The process entrypoint is `python -m AnonX_3`, which executes `AnonX_3/__main__.py`.

There is no `pyproject.toml`, `package.json`, `Cargo.toml`, `setup.py`, or `pytest.ini` in this repository. Dependency and deployment metadata live in these root files instead:

- `requirements.txt`: Python dependencies.
- `config.py`: environment-variable loader and required setting validation.
- `Dockerfile`: container build.
- `Procfile`: Heroku worker command.
- `heroku.yml`: Heroku container deployment descriptor.
- `app.json`: Heroku app metadata and required env vars.
- `setup`: interactive Linux/VPS bootstrap script.
- `start`: thin startup wrapper that runs `python3 -m AnonX_3`.
- `sample.env`: minimal required environment variables.

## Technology Stack
- Python 3 application runtime. The Docker image uses `python:3.13-slim`.
- `kurigram` / Pyrogram-compatible Telegram client API for the bot and assistant userbots.
- `py-tgcalls` and `ntgcalls` for Telegram voice chat streaming.
- `pymongo` async client (`AsyncMongoClient`) for persistence.
- `yt-dlp` for downloading playable audio/video files.
- `py-yt-search` for search and playlist metadata.
- `aiohttp` for cookie downloads.
- `pillow` for thumbnail generation.
- `psutil` for runtime statistics.
- External system dependency: `ffmpeg` must exist in `PATH` before startup. `AnonX_3/core/dir.py` hard-fails if it is missing.

## Runtime Architecture
Application-wide singletons are created in `AnonX_3/__init__.py` and imported throughout the codebase:

- `config`: validated settings from `.env`.
- `app`: main bot client (`AnonX_3/core/bot.py`).
- `userbot`: one to three assistant userbot clients from `SESSION`, `SESSION2`, `SESSION3` (`AnonX_3/core/userbot.py`).
- `db`: MongoDB service with cache and state helpers (`AnonX_3/core/mongo.py`).
- `lang`: translation loader and decorator (`AnonX_3/core/lang.py`).
- `tg`: Telegram file download helper (`AnonX_3/core/telegram.py`).
- `yt`: YouTube/search/download helper (`AnonX_3/core/youtube.py`).
- `queue`: in-memory per-chat playback queue (`AnonX_3/helpers/_queue.py`).
- `thumb`: thumbnail generation helper.
- `anon`: PyTgCalls wrapper for playback control (`AnonX_3/core/calls.py`).
- `tasks`: background task registry for graceful shutdown.

Boot order in `AnonX_3/__main__.py`:

1. Connect to MongoDB and warm caches.
2. Start the bot client.
3. Start assistant userbot clients.
4. Start PyTgCalls clients for each assistant.
5. Start thumbnail helper.
6. Dynamically import every module in `AnonX_3/plugins/`.
7. Optionally fetch cookie files from `COOKIES_URL`.
8. Load sudoers and blacklist state into memory.
9. Wait for process signals, then stop all tasks and clients cleanly.

Important runtime behavior:

- Playback state is split between MongoDB and process memory.
- Chat/user/language/auth/sudo/logger preferences are stored in MongoDB.
- Queue contents and active stream timers are in memory only and are cleared on restart.
- `downloads/` stores downloaded media files.
- `cache/` is used for generated thumbnail assets.
- `/restart` deletes both `cache/` and `downloads/` before restarting the process.
- **Assistant FLOOD_WAIT auto-rotation**: when an assistant hits `errors.FloodWait` during `phone.JoinGroupCall` (voice chat join), the bot silently rotates to the next assistant via `db.rotate_assistant()`. The user only sees an error when all available assistants have been exhausted.
- **Bot-based log group notification**: on startup/restart, `boot_client()` logs "Assistant X started as @username" to `log.txt` first, then uses the bot (`app.send_message`) to send "Assistant Started @username" to the Telegram logger group. Log group notification is non-fatal (warning only on failure), and falls back through assistant→auto-invite if the bot cannot send.

## Code Organization
`AnonX_3/` is divided by responsibility:

- `AnonX_3/core/`: long-lived services and integrations.
  - `bot.py`: main bot client startup checks.
  - `userbot.py`: assistant userbot startup.
  - `calls.py`: playback, pause/resume/stop, stream-end handling.
  - `mongo.py`: persistence and cache layer.
  - `telegram.py`: Telegram media download/cancel flow.
  - `youtube.py`: search, playlist parsing, cookie handling, yt-dlp download.
  - `lang.py`: locale loading plus `@lang.language()` decorator.
  - `dir.py`: startup dependency and directory checks.
- `AnonX_3/helpers/`: reusable helpers, decorators, inline keyboards, dataclasses, queue, thumbnail generation, misc utilities.
- `AnonX_3/plugins/`: command and callback handlers. Every `*.py` file here is auto-imported at startup.
- `AnonX_3/locales/`: translation JSON files. `en.json` is the reference language.
- `AnonX_3/cookies/`: cookie files used by `yt-dlp`. `COOKIES_URL` only accepts `batbin.me` URLs in `config.py`.

Plugin layout is command-family based. Examples:

- `play.py`, `pause.py`, `resume.py`, `skip.py`, `stop.py`, `seek.py`, `queue.py`, `loop.py`: playback operations.
- `callbacks.py`: callback query handlers for ordinary bot keyboards.
- `start.py`, `misc.py`, `ping.py`, `stats.py`: user-facing utility and background behavior.
- `auth.py`, `blacklist.py`, `sudoers.py`, `broadcast.py`, `restart.py`: privileged administration.
- `language.py`: per-chat language settings.

## Build, Setup, and Run Commands
Local dependency install:

```powershell
pip install -r requirements.txt
```

Local startup on Windows:

```powershell
python -m AnonX_3
```

Local startup on Linux/macOS:

```bash
python3 -m AnonX_3
```

Wrapper script:

```bash
bash start
```

Interactive Linux/VPS bootstrap:

```bash
bash setup
```

Container build:

```bash
docker build -t မဂ်လာပါ မြန်မာ 🇲🇲 .
docker run --env-file .env မဂ်လာပါ မြန်မာ 🇲🇲
```

Deployment notes from the repository:

- `Procfile` runs `bash start`, so Heroku uses a worker dyno.
- `heroku.yml` points Heroku to `Dockerfile`.
- `app.json` defines the required variables for one-click Heroku deploys.
- `Dockerfile` installs `ffmpeg`, then installs `requirements.txt` and starts `bash start`.

## Environment and Configuration
`config.py` validates these variables as required:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `MONGO_URL`
- `LOGGER_ID`
- `OWNER_ID`
- `SESSION` (loaded internally as `SESSION1`)

Optional variables currently used by the code:

- `SESSION2`, `SESSION3`
- `DURATION_LIMIT`
- `QUEUE_LIMIT`
- `PLAYLIST_LIMIT`
- `SUPPORT_CHANNEL`
- `SUPPORT_CHAT`
- `AUTO_LEAVE`
- `AUTO_END`
- `THUMB_GEN`
- `VIDEO_PLAY`
- `AUDIO_QUALITY`
- `VIDEO_QUALITY`
- `VIDEO_STRICT_AVC`
- `VIDEO_MAX_HEIGHT`
- `VIDEO_MAX_WIDTH`
- `VIDEO_MAX_FPS`
- `PREFETCH_NEXT`
- `PREFETCH_VIDEO`
- `LANG_CODE`
- `COOKIES_URL`
- `DEFAULT_THUMB`
- `PING_IMG`
- `START_IMG`
- `THUMB_BOT_NAME`

Operational requirements inferred from code:

- The bot must be able to send messages to `LOGGER_ID`.
- The bot must be an administrator in the logger group.
- At least one assistant session must be valid.
- Assistant accounts may need to join target groups before playback can start.

## Development Conventions
Observed conventions in the current codebase:

- 4-space indentation and standard Python naming.
- Async handlers are the default for command and callback flows.
- Plugin handlers commonly use names ending in `_hndlr` or prefixed with `_`.
- Shared services are imported from `AnonX_3`, not re-created inside plugins.
- Permissions are enforced with helper decorators such as `@lang.language()`, `@admin_check`, `@can_manage_vc`, and `@checkUB`.
- Queue items use the `Media` and `Track` dataclasses from `AnonX_3/helpers/_dataclass.py`.
- UI text should come from locale files, not hardcoded English strings, unless the text is strictly operational/internal.
- New commands should typically live in a focused plugin file instead of expanding unrelated modules.

When changing behavior, preserve these patterns:

- Use translation keys for user-facing text.
- Reuse the singleton services from `AnonX_3`.
- Keep per-chat settings in MongoDB through `db`, and keep transient playback state in `queue` or task-local memory as the existing code does.
- If a feature changes playback controls or settings UI, update the inline keyboard helper code in `AnonX_3/helpers/_inline.py` together with the plugin handlers.

## Testing and Verification
There is no committed automated test suite, no `tests/` directory, and no CI workflow in `.github/`.

Current realistic verification strategy is manual:

1. Fill `.env` with valid Telegram and MongoDB credentials.
2. Ensure `ffmpeg` is installed and on `PATH`.
3. Start the bot with `python -m AnonX_3` or `bash start`.
4. Confirm startup messages reach the logger group.
5. Exercise the affected Telegram commands manually in a supergroup with video chat enabled.
6. For playback changes, test at least:
   - `/play` with search query
   - `/play` or `/vplay` with a direct link
   - pause/resume/skip/stop
   - queue display
   - settings/language changes
   - assistant auto-join behavior

If you add pure logic that can be tested outside Telegram, introducing a `tests/` directory would be straightforward, but the repository does not currently contain that infrastructure.

## Security and Sensitive Files
Sensitive local files currently present in the repository root:

- `.env`
- `AnonX_3.session`
- `AnonX_3.session-journal`
- `log.txt`

Agent rules for this repository:

- Never commit or expose secrets from `.env`, session files, or MongoDB URLs.
- Treat logger group IDs, owner IDs, session strings, and cookie URLs as sensitive operational data.
- Do not paste session contents, auth tokens, or downloaded cookie contents into documentation, issues, or commit messages.
- `/restart` is destructive to runtime artifacts in `cache/` and `downloads/`; do not invoke or mirror that behavior casually in maintenance scripts.

## Deployment and Operations
Supported deployment paths that exist in this checkout:

- Local machine / VPS with Python and FFmpeg installed.
- Docker via the provided `Dockerfile`.
- Heroku container deployment via `heroku.yml`, `Procfile`, and `app.json`.

Operational details worth knowing before changes:

- `setup` is Linux-specific and assumes `sudo apt`.
- The `start` script is also Linux-oriented because it calls `python3`.
- Windows developers should run `python -m AnonX_3` directly.
- The bot logs to both stdout and a rotating `log.txt`.
- MongoDB collection migration can run automatically on first startup through `MongoDB.load_cache()`.

## Durable Project Files
These files now exist at the repository root and should be kept synchronized:

- `ARCHITECTURE.md` — Playback pipeline A-D, fast paths, key modules, data stores, optional services
- `DECISIONS.md` — Design decisions log
- `ERRORS.md` — Error reference
- `PROJECT_STATE.md` — Comprehensive project state snapshot with full changelog
- `SESSION_MEMORY.md` — Session memory documentation
- `MEMORY.md` — Memory documentation
- `RELEASE_NOTES.md` — Version release notes
- `broadcast.md` — Broadcast command documentation

## Testing and Verification (Updated)
The `tests/run_unit_smoke.py` suite contains self-contained regression tests and
does not require pytest. Run with:
```bash
python -B tests/run_unit_smoke.py
```
Additional operational scripts live in `ops/`: `build_release.py`, `verify_structure.py`, `secret_scan.py`, `verify_release.py`, and deployment runbooks.




## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX_3 and AnonX_3 .env).
