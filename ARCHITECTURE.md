# ARCHITECTURE — AnonX_3 Music Bot

> Last updated: 2026-08-09  
> Package: `AnonX_3` · Entry: `python -m AnonX_3`

## Audio-first startup V4 (2026-08-09)

Initial direct playback overlaps search/resolution, VC connection, assistant
unmute, and FFmpeg PCM startup. One assistant/chat binding lock owns native call
submission; metadata warming never creates a competing native call. The first
real PCM frame is submitted immediately after connection, and outgoing-clock
advance is measured against a baseline captured before that submission.

Audible proof is `max(real-PCM-backed outgoing clock advance, confirmed unmute)`.
For `/vplay`, the EXTERNAL microphone remains continuous while raw video is
attached by a per-chat post-start task. Stop, skip, replacement, disconnect, and
shutdown cancel that task through the existing post-start ownership registry.

## Stack

- Python 3.13, kurigram/Pyrogram, PyTgCalls/ntgcalls
- MongoDB (chat state), yt-dlp (media), optional aiohttp origin
- SQLite CDN catalog under `media/media.db`

## Runtime layout

```text
Telegram → plugins/play → helpers/_play → calls.play_media
                              ↓
              playback_orchestrator (race + startup gate)
                              ↓
         cache hub / CDN ←→ resolver (YouTube, SC fallback)
                              ↓
              downloader singleflight + resource_manager
                              ↓
              media/tmp → ready · optional Nginx/origin
```

## Playback pipeline (A–D)

1. **Parallel cold YouTube start** — initial `/play` and `/vplay` prepare the VC
   (join plus required self/admin unmute) while resolving the raw source. Once
   both are ready, the source is attached immediately.  
2. **Cache/fallback** — validated local READY assets remain durable fallback
   targets, but they do not preempt direct-first YouTube playback.  
3. **Async proof/failover** — direct startup proof observes the attached stream
   without blocking first audio; one early fatal event triggers one local fallback.
   SoundCloud scored fallback remains the final audio-only provider fallback.  
4. **Resources** — dynamic per-lane capacity driven by CPU/RAM/load/event-loop
   lag, with the env concurrency limits as the fixed fallback contract  
5. **Ops** — metrics, optional health HTTP, optional Redis singleflight, Nginx example  

## Playback presentation and yt-dlp bootstrap boundary (v3.4.10)

- Queue admission, VC ownership, and confirmed media start form the playback
  transaction. Telegram status-card edits, download/queued presentation,
  playlist notices, and command-card cleanup are outside that transaction.
- `helpers/_play.py` propagates cancellation but converts presentation failures
  into bounded logs. A typed stale edit closes progress ownership, detaches
  late download UI writers, and stores no dead status message ID.
- Every service passes `YoutubeDL` construction through
  `core/ytdlp_runtime.py`. Its process-wide event/lock serializes only the first
  successful constructor, where yt-dlp discovers and registers external
  plugins. After readiness is published, constructors and network extraction
  proceed concurrently on the existing worker pools.

## Command-to-packet overlap (v3.4.9)

- The SEARCHING card is asynchronous. `DeferredStatusMessage` exposes chat
  metadata immediately, blocks only UI mutations, and binds cancellation work
  to the real status ID when Telegram returns it.
- Read-only search/direct warmup, language lookup, and group-call presence run
  concurrently. A fresh presence result is handed into queue admission so the
  same Telegram lookup is not repeated after source readiness.
- After authorization and assistant readiness, an initial request may acquire
  the normal per-chat admission lock as an `InitialPlaybackLease`, reserve one
  stream, and connect an EXTERNAL capture before search finishes. The media
  Track later adopts the transport dictionary; no second connection or stream
  reservation is created.
- Audio attaches its decoder to the connected capture. Video does the same for
  a bounded first-audio lead, observes the outgoing clock, then replaces the
  sources with raw A/V on the existing call (`reconnect=0`).
- Any failure before adoption cancels the owned connect task, closes FFmpeg/
  EXTERNAL state, leaves the provisional call, releases stream capacity, and
  releases the admission lease.

## Request and search fast paths

- Valid `/play` requests receive their SEARCHING card before Mongo/admin checks.
- `plugins/inline_search.py` debounces Telegram keystroke queries, caps
  concurrency/results, and reuses the cached multi-provider YouTube deep search.
- Selecting an inline result emits `/vsong@BotUsername <canonical URL>` so the
  normal video-download handler remains the only download implementation.
- Default play-mode reads are negatively cached after the first Mongo lookup.
- Normal and deep YouTube searches use bounded TTL caches plus singleflight.
- Provider races cancel and await losing tasks; YouTube API calls reuse one
  connection pool.
- FFmpeg/FFprobe subprocesses run in worker threads, never on the command loop.

Detail: [`docs/playback-architecture.md`](docs/playback-architecture.md)

## Parallel initial YouTube startup (2026-08-08)

- `helpers/_play.py` still reuses the warm YouTube metadata/search result, but
  it does not start a current-track local worker while direct-first playback is
  pending.
- For the initial cold request only, `calls.play_media(initial_start=True)`
  launches empty-VC join plus required assistant unmute and raw-source resolve
  concurrently. Either join/unmute failure rolls the empty call back before a
  stream is attached. FloodWait assistant rotation remains bounded and intact.
- Once both readiness branches succeed, the code performs only foreground URL
  shape/SSRF validation, builds a PyTgCalls raw `Stream`, and schedules
  `play()` immediately on the prepared call. It does not await that task on the
  request path. This bypasses `MediaStream.check_stream()` and its remote
  ffprobe plus FFmpeg-capability scan for this cold-start transaction only.
- The raw audio shell starts FFmpeg against the resolved URL with no reconnect
  sleeps and low-startup-buffer flags (`nobuffer`, `low_delay`, zero analyzed
  duration, and a 32 KiB probe size). A transparent PCM relay records exact UTC,
  wall-clock-nanosecond, and monotonic timestamps for FFmpeg spawn, first remote
  input bytes, and first decoded audio frame without pre-reading the source.
- The detached play monitor records timestamps immediately before and after
  `PyTgCalls.play()`, treats its successful return as stream attachment, and
  preserves the existing one-shot local fallback on failure. NTgCalls outgoing
  clock advancement after the first decoded frame is the local evidence for the
  first Telegram audio packet. Cache/download, profile/UI work, metadata, and
  queued prefetch are scheduled only after that packet evidence and never gate
  transmission.
- Direct URL resolution is not success. The resolver must return a structured
  raw media source, not a YouTube watch/search URL or local path. The player
  passes yt-dlp HTTP headers, proxy, and network binding into the raw FFmpeg
  source. Established-call and non-initial direct starts retain their existing
  `MediaStream` validation/reconnect behavior. `DIRECT_START_PROOF_SEC` remains
  asynchronous; an early fatal `StreamEnded` consumes the watchdog once and
  performs exactly one local fallback attempt.
- `NoAudioSourceFound` is treated as a direct startup failure, not an unhandled
  crash. Logs include PyTgCalls/ntgcalls versions, play signature, input type,
  URL host, audio format, URL presence, and FFmpeg open result before fallback.
- CDN work is publication-only after a complete local artifact exists; partial
  yt-dlp output is never promoted as a cache hit. Locally acquired catalog rows
  are durable (not TTL-expired), though normal capacity eviction still applies.
- Canceling a status card detaches that UI observer and its background resolver
  callbacks. It does not cancel an in-flight shared media owner; a completed or
  failed owner is terminal for that request scope, while a fresh command may
  acquire the media again.
- Queued tracks, seek/replay/skip, forced paths, non-YouTube sources, and
  established-call starts keep their existing sequencing.

## Key modules

| Module | Role |
|---|---|
| `core/calls.py` | VC play, hybrid race, SC path |
| `core/youtube.py` | Search, extract, download, PO inject |
| `plugins/inline_search.py` | Bounded inline search and `/vsong` handoff |
| `core/cdn/*` | READY store, publish, GC, origin |
| `core/cache/*` | Keys, states, hub |
| `core/resolver/*` | Errors, retry, matcher, SoundCloud |
| `core/resource_manager.py` | Lane permits + load band |
| `core/dynamic_capacity.py` | Dynamic capacity, foreground priority, fallback |
| `core/social_urls.py` | TikTok/Facebook share-link canonicalisation |
| `core/metrics.py` / `health.py` | Observability |
| `core/security.py` | Path/URL/redaction helpers |
| `core/provider/po_token.py` | Official yt-dlp PO provider configuration |

## Data stores

- **MongoDB** — chat settings, auth, language, file_id cache  
- **SQLite** — CDN media assets + status metadata  
- **Filesystem** — `downloads/`, `media/tmp/`, `media/ready/`  
- **In-memory** — queue, singleflight, metrics  

## Optional services

| Service | When |
|---|---|
| Built-in CDN origin | `CDN_ENABLED` + no public domain |
| Nginx | `nginx/media.conf` + `CDN_PUBLIC_BASE_URL` |
| Redis singleflight | `SINGLEFLIGHT_BACKEND=redis` + `REDIS_URL` |
| Health server | `HEALTH_PORT>0` |
| PO Token provider | `PO_TOKEN_PROVIDER_ENABLED=True` + bgutil sidecar on `4416` |

## Explicit non-goals

- Unlimited bandwidth / zero YouTube failures  
- Perfect seamless Telegram remote→local switch  
- Cookies as default requirement  

## Global Silent Kick Watchlist (2026-07-26)

- `plugins/global_kick.py` owns the dedicated sudo-only control plane.
- `/kick`, `/unkick`, `/kicklist`, and `/sudolists` are accepted only in
  `LOGGER_ID`; ordinary moderation `/kick` remains available outside that chat.
- MongoDB collection `global_kicks` is isolated from blacklist, ban, mute, and
  ordinary moderation state.
- Adding an entry triggers a bounded sweep of served groups. New-member events
  force a fresh database read, and group-message checks provide a fallback.
- Enforcement is silent ban-then-unban; the bot publishes no moderation notice.

## Asyncio Cancellation Boundaries (2026-07-28)

**Problem**: Deep task chains (990+ nested `_runner` functions) caused `RecursionError` 
when cancelled, as each level caught and re-raised `asyncio.CancelledError`.

**Solution**: Explicit cancellation boundaries in all `_runner` wrapper functions:

```python
except asyncio.CancelledError:
    # Explicit cancellation boundary: don't propagate further
    pass
```

**Fixed locations**:
- `core/prefetch.py:350` - YouTube prefetch runner
- `core/tiktok.py:235` - TikTok resolver runner  
- `core/facebook.py:190` - Facebook resolver runner
- `core/telegram.py:492` - Telegram resolver runner

**Pattern**: Any `_runner` function that wraps async operations should suppress 
`CancelledError` rather than re-raising it. This breaks recursive cancellation 
chains while preserving proper cleanup and error handling for all other exceptions.

**Verification**: `tests/test_recursion_fix.py` confirms deep cancellation chains 
(100+ levels) complete without `RecursionError`.

## Social share-link canonicalisation (2026-08-05)

yt-dlp accepts only a narrow set of TikTok/Facebook URL shapes. Verified against
yt-dlp 2026.07.04, several common Telegram pastes matched **no** extractor, so
`/play` and `/vplay` failed before any download started: TikTok `/story/` and
`/photo/` items, any TikTok host without `www.`, `m.tiktok.com/v/<id>.html`,
`facebook.com/share/v|r/<token>/`, and `fb.watch/<code>/`.

`core/social_urls.py` fixes this in two passes:

1. A pure, synchronous, offline rewrite for shapes derivable from the URL text
   (`/story/` → `/video/`, host → `www.`, `l.php?u=` unwrap, tracking-param
   strip).
2. A bounded HTTP redirect follow for share and shortener links, whose target
   only the server knows. Capped hops, capped body read, TTL-cached and
   single-flighted, with a short negative TTL so an unreachable provider cannot
   stall foreground `/play` once per attempt. The provider's existing yt-dlp
   cookie jar is reused, domain-matched to the target host so a shared cookie
   directory never sends one provider's credentials to another.

Canonicalisation runs at the top of `TikTok.resolve()` / `Facebook.resolve()` —
the single producer of `Track.url` — so download, direct stream, cache key and
CDN publication all inherit one canonical link. Every function degrades to its
input: an unrecognised link, a missing `aiohttp`, or a failed hop returns the
original string and the provider behaves exactly as before. Knobs:
`SOCIAL_URL_RESOLVE_ENABLED`, `SOCIAL_URL_RESOLVE_TIMEOUT_SEC`,
`SOCIAL_URL_RESOLVE_MAX_HOPS`, `SOCIAL_URL_CACHE_TTL_SEC`,
`SOCIAL_URL_NEGATIVE_TTL_SEC`.

**Verification**: `tests/test_social_url_support.py` (31 tests).

## Stream dynamic scaling (2026-08-05)

`MAX_ACTIVE_STREAMS=20` is the **baseline**, not the cap, and
`DYNAMIC_STREAMS_CEILING=0` means *derive* — no fixed replacement ceiling.

Three defects previously made the baseline behave as a hard cap:

1. The streams ceiling was sized with the download lane's `ram_mb // 150`
   per-job unit. A relayed VC stream is one ntgcalls pipe plus its share of the
   event loop, not ~150 MB of decode buffers, so a 2 GB VPS derived 13 —
   *below* the baseline — and streams could never grow. `_stream_ceiling()`
   now uses stream units (`DYNAMIC_STREAM_RAM_MB`, `DYNAMIC_STREAMS_PER_CORE`,
   `DYNAMIC_STREAM_RAM_RESERVE_MB`), still bounded by
   `MAX_ACTIVE_STREAMS × DYNAMIC_CAPACITY_MAX_MULTIPLIER`.
2. Demand summed only the queueing lanes. The streams lane never queues, so
   waiting `/play` requests counted as zero and the 21st group was refused on
   an idle box. A refusal *is* the waiting request: `_stream_demand()` counts a
   decaying refusal window, and `try_admit("streams")` records the refusal,
   force-recomputes, and retries inside the same call — so a healthy box admits
   the 21st group immediately instead of only after the next `/play`.
3. `register_stream()` kept a private chat set and never touched the lane, so
   `lanes["streams"].active` was always 0 and `try_admit("streams")` was dead
   code. Admission now flows through the lane, with `_stream_lane_chats`
   guaranteeing symmetric release.

Runtime decision (`_stream_target`): the **safe ceiling** is the hard machine
ceiling scaled by remaining pressure headroom over CPU, RAM, load-per-core and
event-loop lag; the target is `active + waiting + DYNAMIC_STREAM_HEADROOM`
clamped into `[baseline, safe_ceiling]`. At or above `DYNAMIC_PRESSURE_RELIEF`
the target drops to the baseline floor — capacity shrink throttles **new
admissions only** and never falls below the live session count, so an active
voice chat is never cut.

`calls._play_with_startup_slot` is the single VC-start path and therefore the
single admission point: direct, local and cache playback obey one decision. It
reserves the slot **before** `client.play()` and releases it on any failure or
cancellation (`ResourceManager.reserve_stream` → `StreamReservation.release`,
idempotent per chat so `/skip` and queue auto-next never
release a live session's slot). `play_media` runs a cheap non-binding
`can_admit_stream()` pre-flight so a saturated box refuses before paying for a
resolve or download. A refusal raises `StreamCapacityError`, which reports
`play_stream_busy` and leaves the queue intact — it never calls `play_next`.

`/health` → `components.dynamic_capacity.streams` reports `baseline`,
`capacity`, `auto_ceiling`, `hard_ceiling`, `active`, `waiting` and
`scaling_reason`.

**Verification**: `tests/test_stream_dynamic_scaling.py` (28 tests).

## Release Boundary

- `ops/release_meta.py` owns release identity and deterministic archive naming.
- `requirements.in` is the direct dependency contract; `requirements.txt` is
  the exact Python 3.13 runtime graph.
- `ops/release_gate.py` is the single promotion boundary. It validates the
  environment and application before producing and verifying the archive.
- Runtime configuration and state are external to release archives. Environment
  merge operations are explicitly invoked, root-scoped, atomic, and preserve
  existing credential values.
