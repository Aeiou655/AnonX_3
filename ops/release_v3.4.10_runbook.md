# AnonX_3 v3.4.10 Final Release Runbook

## Deploy

1. Back up the target host's `.env`, cookie files, session data, and any
   deployment-specific Compose override.
2. Extract `AnonX_3-v3.4.10-final.zip` over the source tree. Merge configuration
   additions without replacing live credentials.
3. Install the pinned `requirements.txt` in one environment and run
   `python -m pip check`.
4. Fully restart the bot process. A reload inside the old process is not enough
   because yt-dlp's provider registry is process-global.

## Incident verification

1. Start one track, then issue another `/play` while the first is active.
2. Delete the second command's SEARCHING card before the Queued edit completes.
3. Confirm the second track remains in the queue and no
   `Unexpected playback startup failure` traceback is emitted.
4. Restart with `DIRECT_RESOLVER_STARTUP_WARM=True`. Startup must not emit
   `PoTokenProvider ... already registered`; the final
   `direct_resolver startup_warm ready=` line should still report the expected
   warm profiles.

## Local verification

```bash
python -m compileall -q AnonX_3 config.py ops tests
python -B tests/run_unit_smoke.py
python -B tests/test_v348_sub15_resolver.py
python -B tests/test_v349_sub15_e2e.py
python -B ops/secret_scan.py
python -B ops/verify_structure.py
python -B ops/build_release.py
python -B ops/verify_release.py
```

## Performance acceptance

The first-constructor gate ends as soon as one `YoutubeDL` runtime finishes
initialization; it does not serialize extraction. The V4 candidate supersedes
the prior 20+20 gate. Collect at least 100 cold `/play` and 100 cold `/vplay`
traces, then run `ops/resolver_latency_report.py --command both --metric
end-to-end --min-samples 100 --target-ms 4000`. Both commands must independently
have command-to-ready `playback_trace total_ms` p95 <=4000 ms and retain
truthful audible proof. See `ops/audio_first_v4_runbook.md`.

## Rollback

Stop the process, restore the previous source tree and its matching dependency
lock, then start a fresh process. Do not reuse an interpreter that already
reported duplicate provider registration.
