# Hardening: gate, failover, Redis, PO

## Startup gate (process-aware)

- `client.play()` must succeed.
- Then wait `DIRECT_START_PROOF_SEC` for `StreamEnded` / fatal signal.
- If fatal during window → treat as direct fail → race to local.
- True packet counters are not available from ntgcalls public API. The current
  observable gate logs packet/audible success only after FFmpeg audio input
  opens, PyTgCalls accepts the stream, and the proof window finishes cleanly.
- YouTube direct input must be a raw media URL with audio; yt-dlp headers/proxy
  are forwarded into PyTgCalls/FFmpeg. `NoAudioSourceFound` is logged with
  PyTgCalls version, play signature, input type, URL host, audio format, URL
  presence, and FFmpeg open result, then local fallback starts.

```env
DIRECT_START_PROOF_SEC=3
DIRECT_URL_PROBE=off
```

`DIRECT_URL_PROBE`: `off` (default, fastest) | `soft` (403 soft-pass) | `strict` (hard-fail 4xx).

## Direct→local failover

- After direct OK, `DirectWatchdog` is armed for `DIRECT_FAILOVER_WINDOW_SEC`.
- On early `StreamEnded`, bot restarts from local READY (seek if `media.time` known).
- **Not seamless** mid-track without restart — Telegram limitation.

```env
DIRECT_FAILOVER_WINDOW_SEC=45
DIRECT_MIDSTREAM_FAILOVER=True
```

## Parallel Branch B → CDN READY

- After direct resolve, background task runs `cdn.ensure_ready` (atomic publish).
- READY only after validate + atomic rename.

## PO Token + client ladder

```env
YTDLP_PLAYER_CLIENTS=default
PO_TOKEN_PROVIDER_ENABLED=True
PO_TOKEN_PROVIDER_URL=http://127.0.0.1:4416
PO_TOKEN_CLIENT=mweb
PO_TOKEN_CACHE_SEC=300
```

Provider: official `bgutil-ytdlp-pot-provider` plugin plus its pinned long-lived
HTTP sidecar; bot never starts a token-generation process per `/play`.

## Redis singleflight

```env
SINGLEFLIGHT_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_LOCK_TTL_SEC=120
REDIS_RESULT_TTL_SEC=300
```

Lock + heartbeat + Lua unlock + result key/publish. Falls back to memory if Redis down.

## Tests / secrets

```bash
python tests/run_unit_smoke.py
python ops/secret_scan.py
```
