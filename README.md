# AnonX_3 Music Bot

Production-oriented Telegram voice-chat music bot with YouTube, Telegram,
TikTok, Facebook, playlists, queues, autoplay, and audio/video playback.

## Runtime

- Python 3.13
- Kurigram/Pyrogram and PyTgCalls/ntgcalls
- MongoDB
- yt-dlp and FFmpeg/FFprobe
- Optional CDN origin, Nginx, Redis singleflight, and PO-token provider

Entrypoint:

```bash
python -m AnonX_3
```

## Setup

1. Install Python dependencies:

   ```bash
   python -m pip install --requirement requirements.txt
   python -m pip check
   ```

2. Install FFmpeg so both `ffmpeg` and `ffprobe` are on `PATH`.
3. Copy `sample.env` to `.env` and fill in your own credentials.
4. Start MongoDB, then run the entrypoint above.

Never commit or distribute `.env`, session files, cookies, logs, downloads, or
the runtime media database.

## Fast-path design

- `/play` acknowledges a valid request before Mongo/admin permission lookups.
- YouTube text search races API, py-yt, and yt-dlp providers.
- Repeated normal and deep searches use bounded TTL caches and singleflight.
- YouTube API calls reuse one connection pool instead of reconnecting per call.
- Direct YouTube URLs cap optional metadata lookup at 350 ms.
- Direct VC playback races a local fallback download.
- Duplicate downloads are coalesced by media ID and quality.
- Completed yt-dlp artifacts skip an unnecessary fixed settle delay.
- FFmpeg/FFprobe validation runs outside the asyncio event loop.

Cache-hit and coalesced paths can be more than 10x faster than a fresh provider
request. Cold YouTube and Telegram speed still depends on upstream response,
network throughput, VPS CPU/disk, media size, and platform rate limits.

## Inline video search

Enable inline mode once through `@BotFather` with `/setinline`. Users can then
type:

```text
@BotUsername song name
```

In the bot's private chat, or a group where the bot is present, selecting a
result sends a bot-addressed `/vsong` command with the canonical YouTube URL.
The existing video download, thumbnail, cache, progress, and cancellation
pipeline then handles the request without a second button press.

## Validation

```bash
python -B ops/release_gate.py
```

Run the release gate from a fresh Python 3.13 virtual environment. It checks the
locked dependency graph, compiles the source, runs both test suites, verifies
structure and secrets, imports the Downloader API, builds the archive twice,
proves deterministic output, and verifies the manifest and SHA-256 sidecar.

See [ops/release_v3.4.10_runbook.md](ops/release_v3.4.10_runbook.md) for the
release and rollback procedure.

See [ARCHITECTURE.md](ARCHITECTURE.md) and
[RELEASE_NOTES.md](RELEASE_NOTES.md) for details.
