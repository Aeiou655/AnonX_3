# AnonX_3 v3.4.8 Final Release Runbook

## Deploy

1. Preserve the host's credentials, Pyrogram sessions, cookies, downloads,
   cache, and media directories.
2. Extract `AnonX_3-v3.4.8-final.zip` over the source tree. The archive's
   `.env` is a secret-free template; retain the host's real credential values.
3. Keep the official PO-token provider healthy and start through `bash start`.
4. Confirm startup logs show two foreground resolver slots and three bounded
   micro clients.

## Required live acceptance

Collect at least 20 fresh uncached samples for each command. The release target
is the exact cumulative trace window from `search` to `play_task_scheduled`:

```bash
python -B ops/resolver_latency_report.py log.txt \
  --command play --min-samples 20 --target-ms 1500
python -B ops/resolver_latency_report.py log.txt \
  --command vplay --min-samples 20 --target-ms 1500
```

Both commands must report `pass=1`. Also confirm:

- a micro winner completes player-response fetch plus 200/206 proof within the
  logged 1450 ms total budget;
- full lanes show `validation_race=1` and the winner shows
  `validation_complete=1`;
- `/vplay` existing-call source replacement remains `reconnect=0`;
- encrypted `s` signatures never enter the micro path and continue through the
  maintained full yt-dlp fallback.

This gate measures resolver-to-schedule latency. It does not relabel search,
Telegram VC connection, or remote audio rendering as a local resolver result.

## Rollback

- Set `DIRECT_RESOLVER_PARALLEL_MICRO=False` to disable all speculative
  player-response lanes.
- Set `DIRECT_FOREGROUND_RESOLVER_SLOTS=1` to serialize full extraction hedges.
- If either command regresses or 403 rate rises, restore the previous v3.4.7
  archive and restart through `bash start`.
