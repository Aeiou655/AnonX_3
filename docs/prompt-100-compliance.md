# prompt.txt → AnonX_3 100% compliance map

Source: `C:\Users\HP\Downloads\prompt.txt`  
Codebase: AnonX_3 (in-place architecture)

## Definition of 100%

All **achievable** prompt behaviors are code-wired, configurable, documented, and smoke-testable.  
Does **not** claim: unlimited bandwidth, zero failures, perfect seamless VC switch, guaranteed YouTube.

## Objective checklist

| # | Objective | Status | Evidence |
|---|---|---|---|
| 1 | Local cache first | Done | `cache/hub.py`, CDN READY, `prepare_cache_hit` |
| 2 | CDN/public cache | Done | `core/cdn/*`, optional origin/Nginx |
| 3 | YouTube resolve | Done | `youtube.py` |
| 4 | yt-dlp + Deno/EJS/PO when needed | Done* | PO client + compose stub + Dockerfile `WITH_DENO` |
| 5 | Direct play ASAP | Done | hybrid direct path + URL probe |
| 6 | Parallel original download | Done | prefetch/CDN/local race |
| 7 | Failover local/CDN | Done | `decide_race` + calls.py |
| 8 | SoundCloud fallback | Done | `resolver/fallback.py` |
| 9 | No duplicate downloads | Done | singleflight memory; Redis optional |
| 10 | Reduce VPS load | Done | `resource_manager.select_quality_plan` |
| 11 | Auto clean cache | Done | CDN GC TTL + high-water + refcount |
| 12 | Minimal manual config | Done | auto CDN/quality defaults |
| 13 | Optional AI DJ modes | Done | `/aidj` reuses strict autoplay with persisted per-group mood |
| 14 | Unified request metadata | Done | additive `request_context.py`; canonical `Media`/`Track`/`Queue` retained |
| 15 | Inline search → group play/queue | Removed | Operator-requested removal on 25-Jul-2026; ordinary `/play`, `/song`, `/vsong`, and queues remain |
| 16 | Source health/circuit breaker | Done | `source_health.py`; SoundCloud fallback is circuit-aware |
| 17 | Adaptive VPS quality | Done | CPU, RAM, disk and active-work pressure bands with hysteresis |
| 18 | Next-track preload/gapless | Done* | identity-safe prefetch, stale cancellation, READY metrics, immediate prepared switch |
| 19 | Manual request priority over AI DJ | Done | stable priority insertion leaves current item intact |
| 20 | Observable readiness stages | Done | prefetch, source, cache, direct/local and AI DJ counters/timings |

AI DJ is deterministic and does not require a paid API. Supported modes are
`chill`, `party`, `study`, `workout`, `myanmar`, and `romantic`; manual queue
items retain priority because the existing queue and empty-queue path are reused.
AI DJ no longer pre-enqueues a recommendation merely when the setting is
enabled; selection happens at the real empty-queue boundary.

\*PO/Deno: **optional sidecars** — enable via env/compose; not required for lab mode.
\*Crossfade: config and capability policy exist, but overlapping crossfade stays
off on the current one-input ntgcalls engine. Calling a hard switch a crossfade
would be incorrect. Gapless best-effort uses the already resolved/downloaded
next item without a fixed timer.

## Explicit non-goals (documented limits)

- Perfect mid-track seamless remote→local handoff (Telegram/ntgcalls)
- Overlapping crossfade without a voice engine that supports concurrent inputs
- Cloudflare auto-cache of all large media
- Cookies as default path
- Separate microservices on day-1 low-cost VPS (in-process workers accepted)

## Ops artifacts

| Artifact | Path |
|---|---|
| Compose | `docker-compose.yml` (PO provider by default; optional Redis/Nginx profiles) |
| Nginx | `nginx/media.conf` |
| PO provider | `brainicism/bgutil-ytdlp-pot-provider:1.3.1` + `provider/README.md` |
| Architecture | `docs/playback-architecture.md` |
| Smoke tests | `tests/run_unit_smoke.py` |
| Manual checklist | `ops/smoke_playback_checklist.md` |

## Sign-off commands

```bash
python -m compileall -q AnonX_3 config.py tests ops
python ops/verify_structure.py
python tests/run_unit_smoke.py
python ops/secret_scan.py
```
