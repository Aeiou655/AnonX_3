# AnonX_3 Playback Architecture

## Unified request entry points

`/play`, replied media, playlists and AI DJ all enrich the
existing `Media`/`Track` objects and enter the same queue/player. Requests
never expose a resolved stream URL; media resolution stays inside the canonical
playback and download pipelines.

Manual requests use priority 100, playlists 90, normal legacy items
50 and AI DJ 20. Priority only reorders waiting items; index 0 (currently
playing) is never displaced and equal-priority items stay FIFO.

## Preload and transition capability

The next queue item is warmed in the background through the existing
CDN/download path. A changed next item cancels stale work; a late task may leave
a reusable disk-cache file but cannot publish its path onto an obsolete queue
object. Playback joins matching work rather than starting a duplicate download.

The current ntgcalls integration has one active input, so overlapping crossfade
is capability-gated off. Gapless best-effort playback remains enabled: when the
next input is ready, the normal queue transition can switch immediately without
a fixed sleep.

## Source and resource adaptation

Source health tracks success, consecutive failures and latency EWMA. Repeated
failures open a cooldown circuit, followed by one half-open probe. Resource
quality bands use CPU, RAM, disk high-water and active work with hysteresis;
the VPS is not assumed to be permanently unconstrained.

Production hybrid playback pipeline (Phases A–D from `prompt.txt`).

## Flow

```text
/play → validate → cache_key
  → READY local/CDN? play immediately
  → singleflight resolve (yt-dlp ± PO token)
  → Branch A: direct stream + startup gate
  → Branch B: download .part → validate → READY
  → A fail + B ready → switch local
  → both fail → SoundCloud scored fallback
```

For a cold initial YouTube request, the direct branch overrides the generic
cache-first diagram: VC join/required unmute and raw-source resolution run in
parallel. When both are ready, the player submits a raw PyTgCalls `Stream` in an
owned task, bypassing `MediaStream.check_stream()` and its remote ffprobe. A
transparent FFmpeg PCM relay records spawn, first remote input, and first
decoded frame timestamps. The play owner records before/after and attachment;
NTgCalls outgoing-clock movement supplies first-packet evidence. Current-track
cache/download and all display/profile work are scheduled only after that
packet signal, while the existing bounded local fallback remains active.

## Modules

| Area | Path |
|---|---|
| Orchestrator / race | `AnonX_3/core/playback_orchestrator.py` |
| Cache keys/states/hub | `AnonX_3/core/cache/` |
| Singleflight | `AnonX_3/core/downloader/singleflight.py` (+ Redis optional) |
| CDN | `AnonX_3/core/cdn/` |
| Error classify / retry | `AnonX_3/core/resolver/` |
| SoundCloud fallback | `AnonX_3/core/resolver/soundcloud.py`, `fallback.py` |
| Resource manager | `AnonX_3/core/resource_manager.py` |
| Metrics / health | `AnonX_3/core/metrics.py`, `health.py` |
| Security helpers | `AnonX_3/core/security.py` |
| PO Token (optional) | `AnonX_3/core/provider/po_token.py` |

## Cache states

`MISS → RESOLVING → DOWNLOADING → READY`  
`FAILED_TEMPORARY | FAILED_PERMANENT | EXPIRED`

Key format: `source:youtube:{id}:audio:best`

## Status-card ownership

A foreground status message has one mutable owner at a time:

```text
Searching → Download progress → playback ownership barrier → play_media
```

When voice playback is ready, `update_now_playing()` closes progress ownership,
drains any Telegram progress edit already in flight, and detaches the YouTube UI
watcher without cancelling the background cache download. YouTube, Telegram,
TikTok, Facebook, initial `0%` edits, and late Cancel-markup tasks all use the
same per-message guard. Once `play_media` owns the card, a late `100%` callback
cannot replace its caption or controls.

## Config highlights

- Hybrid: `YOUTUBE_DIRECT_STREAM=True`, `YOUTUBE_DIRECT_STREAM_ONLY=False`
- Gate: `PLAY_STARTUP_GATE_SEC=6`
- Resources: `MAX_YTDLP_CONCURRENT`, `MAX_DOWNLOAD_CONCURRENT`, …
- Fallback: `FALLBACK_SOUNDCLOUD=True`, `FALLBACK_MIN_SCORE=0.85`
- Health: `HEALTH_PORT=0` (off) or e.g. `9100`
- Redis: `SINGLEFLIGHT_BACKEND=redis` + `REDIS_URL=…`
- PO Token: `PO_TOKEN_PROVIDER_ENABLED=False` by default

## Deployment

1. Single process (default): `python -m AnonX_3` / Docker `start`
2. Optional: `docker-compose.example.yml` (health port, media volume, Redis, Nginx)
3. Nginx: `nginx/media.conf` when serving `media/ready` publicly

## Limits (honest)

- No unlimited bandwidth / zero failures
- Telegram VC seamless mid-track switch often impossible
- Cloudflare does not auto-cache all large media
- Cookies not required by default

## Health endpoints

When `HEALTH_PORT>0`:

- `GET /health` — component status + metrics snapshot
- `GET /metrics` — JSON (or Prometheus text with `?format=prom`)
- `GET /ready` — liveness

Optional `HEALTH_TOKEN` as Bearer or `?token=`.

## Tests

```bash
python -m compileall -q AnonX_3 config.py
python ops/verify_structure.py
python -m pytest tests/ -q   # if pytest installed
python tests/run_unit_smoke.py
```
