# Audio-First Startup V4 Canary

## Rollout

Deploy the candidate with `DIRECT_STARTUP_V4=True`. Keep the prior artifact
available; setting `DIRECT_STARTUP_V4=False` restores the legacy binding retry
behavior after restart.

Use genuinely cold requests: clear the bot's direct resolver/media cache between
runs, do not reuse a playing VC, and alternate media IDs to avoid YouTube/CDN
warm-cache bias. Collect at least 100 trace attempts for each command. Failed or
timeout attempts remain part of the operational result and must not be silently
discarded.

## Gate

```bash
python ops/resolver_latency_report.py log.txt \
  --command both --metric end-to-end --min-samples 100 --target-ms 3000
```

Promotion requires all of the following:

- `/play` has at least 100 cold samples and audible p95 <=3000ms.
- `/vplay` has at least 100 cold samples and audible p95 <=3000ms.
- Proof logs show real PCM accepted, a subsequent outgoing-clock advance, and
  confirmed unmute. Silence/JIT packets and camera attachment are not proof.
- `overlap_wait_ms=0` and `unmute_blocked_audio_ms=0` on V4 starts.
- No V4 trace contains `failed_binding_reset`, `overlap_retry`, or
  `native_settle_retry`.

The end-to-end metric reads the truthful `audible=` milestone, not the earlier
packet timestamp. The report exits `2` when either command lacks 100 audible
samples and `1` when a p95 gate fails. It exits `0` only when both independent
command gates pass.
