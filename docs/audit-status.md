# Live audit status (corrected) — AnonX_3

Generated against current codebase. **Not** the outdated “0/9 tests” row.

| Item | Status | Notes |
|---|---|---|
| Local cache lookup | **Done** | `cache_hub`, CDN READY, `_local_ready_path` |
| Cache state machine | **Done** | MISS→…→READY/FAILED/EXPIRED in store + hub |
| Parallel direct + download | **Done** | Prefetch + Branch B CDN publish after direct |
| Direct startup verification | **Done*** | play() + stable window + StreamEnded fatal (*not raw packet counters) |
| Early/mid-stream failover | **Done*** | `DirectWatchdog` + local restart/seek (*not seamless) |
| Atomic CDN publication | **Done** | `tmp/.part` → `atomic_publish` → READY |
| Distributed duplicate prevention | **Partial** | Memory singleflight always; Redis optional (lock+HB+result) |
| yt-dlp retry classifier | **Done** | `error_classifier` + backoff + client ladder |
| Deno | **Optional/off** | Dockerfile `WITH_DENO=1` |
| EJS challenge | **Optional** | Via yt-dlp/Deno when installed; not separate service |
| Real PO Token Provider | **Partial** | HTTP client + ladder + cache; compose stub only |
| SoundCloud matching | **Done** | 45/30/20/5 formula |
| SoundCloud threshold | **Done** | Hard ≥0.85; soft auto **off** (`FALLBACK_SOFT_AUTO=False`) |
| Dynamic quality | **Done** | `QualityPlan` CPU/RAM + band + concurrency |
| FFmpeg/process limits | **Partial** | Counters + MAX_*; ntgcalls owns real FFmpeg PIDs |
| Nginx static serving | **Partial** | `nginx/media.conf` + compose profile |
| Real CDN/Cloudflare automation | **N/A** | Documented; not automated |
| Cleanup/TTL/LRU | **Done** | GC TTL + popular + high-water + refcount skip |
| Active playback protection | **Done** | refcount acquire gate + GC skip + stream register |
| Security hardening | **Partial** | SSRF helpers, path jail, secret_scan; ongoing |
| Metrics/health | **Partial** | metrics + health server when `HEALTH_PORT>0` |
| Automated tests | **12/12 pass** | `python tests/run_unit_smoke.py` |
| Production deployment proof | **Partial** | compose + checklist; no hosted VPS proof in-repo |

## Commands

```bash
python tests/run_unit_smoke.py   # 12/12
python ops/secret_scan.py
python ops/verify_structure.py
```
