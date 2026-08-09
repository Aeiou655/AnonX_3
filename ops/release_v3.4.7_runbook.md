# AnonX_3 v3.4.7 Final Release Runbook

## Performance acceptance

After deploy, run fresh `/play` and `/vplay` samples across multiple uncached YouTube IDs. The required production evidence is:

- `/play`: `connect_to_real_ms < 1000` at p95.
- `/vplay`: no duplicate `direct_resolver mode=... video=1` for the same request; expect `prewarm_cross_tier_join`/reuse when the adaptive tier changes.
- Direct race log must show `foreground_slots=2` even if `dynamic_ytdlp=1`.
- `/vplay` source swap remains `reconnect=0`.
- Prefer `native_payload=1` or `vc_join_metadata_warm_cache_hit ... native_payload=1`; public PyTgCalls fallback remains valid if private-layout prewarm is unavailable.

## Rollback

- Set `DIRECT_AUDIO_ESCAPE_RACE=False` to disable the audio hedge only.
- Set `DIRECT_FOREGROUND_RESOLVER_SLOTS=1` to disable parallel full-resolver hedging without changing background resource limits.

## Prior v3.4.6 deployment notes

# AnonX_3 v3.4.6 Final Release Runbook

1. Preserve the existing live credentials, Pyrogram session, `downloads/`, cache, and `media/ready/`.
2. Extract `AnonX_3.zip` over the application source, then restore/fill the included safe `.env` placeholders with deployment credentials.
3. Keep the low-latency defaults unless the host requires an override: direct YouTube transport, healthy bgutil POT provider, `mweb`, resolver workers=2, audio escape race enabled, micro clients `android_vr,web_embedded,mweb`, EXTERNAL prebuffer frames=4.
4. Start with `bash start`; do not bypass it in production because it owns hard-stall heartbeat recovery.
5. Confirm startup logs show direct transport, resolver startup warm, PyTgCalls started, heartbeat supervision, and the next 00:00 Asia/Yangon media-preserving restart.
6. Cold `/play`: confirm `direct_resolver_race_started`, `direct_resolver_micro_race_started`, `vc_connected_external_capture`, `first_external_audio_frame_accepted`, and `connect_to_real_ms`.
7. Cold `/vplay`: confirm early placeholder connect, `vplay_source_swap_after`, `direct_video_existing_call_source_swap ... reconnect=0`, and first capture/media progress.
8. Acceptance target is post-VC p95 <1000ms over at least 10 fresh `/play` and 10 fresh `/vplay` samples. Do not interpret this as a hard <1s cold command-to-media SLA; search/resolver/network time is external and production measurement is required.
9. Explicit `/stop` must close the EXTERNAL capture session before leaving the VC and return cleanly without recurring `ConnectionNotFound` warnings.
10. Roll back the new audio hedge immediately with `DIRECT_AUDIO_ESCAPE_RACE=False` if a deployment-specific resolver interaction appears.
