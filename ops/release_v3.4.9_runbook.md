# AnonX_3 v3.4.9 Final Release Runbook

## Deploy

1. Back up the target host's credential-bearing `.env`, cookie files, and any
   deployment-specific Compose override.
2. Extract `AnonX_3-v3.4.9-final.zip` over the source tree. The archive contains
   only a secret-free `.env` template; merge new tuning keys without replacing
   live credentials.
3. Install the pinned `requirements.txt`, restart the bot process, and verify
   that startup reports the v3.4.9 package version with no dependency conflict.
4. Confirm a group voice chat is already active, then use uncached YouTube
   requests. The bot intentionally does not create the group call.

## Expected critical-path evidence

- `direct_command_preconnect_started` occurs after authorization/assistant
  readiness and before search/direct resolution finishes.
- `direct_command_preconnect_adopted ... reconnect=0` appears once per accepted
  initial request; no second VC connection is created.
- A micro win reports `direct_micro_player timing` with status 200/206. A miss
  includes only URL-free response counts and continues on the already-running
  authoritative lanes.
- `/vplay` reports `vplay_audio_lead_packet_ready` before
  `vplay_source_swap_before`; the final swap still reports `reconnect=0`.
- Cancellation, no-result, capacity refusal, or resolver failure reports
  `direct_command_preconnect_rolled_back` and leaves no silent assistant call or
  stream reservation.

## Mandatory live acceptance

Collect at least 20 fresh uncached `/play` traces and at least 20 `/vplay`
traces after this version is deployed. Do not mix v3.4.8/pre-change lines into
the sample set.

```bash
python -B ops/resolver_latency_report.py fresh.log \
  --command play --metric all --target-ms 1500 --min-samples 20

python -B ops/resolver_latency_report.py fresh.log \
  --command vplay --metric all --target-ms 1500 --min-samples 20
```

Both commands must exit 0. For each command, resolver p95,
scheduled-to-packet p95, and command-to-first-packet p95 must all be <=1500 ms;
the sample floor must be met, giving a 100% gate result. Any failure is a real
release blocker—retain the logs and inspect the first slow phase instead of
raising timeouts or relabeling milestones.

## Local verification

```bash
python -m compileall -q AnonX_3 config.py ops tests
python -B tests/test_v348_sub15_resolver.py
python -B tests/test_v349_sub15_e2e.py
python -B ops/secret_scan.py
python -B ops/verify_structure.py
python -B ops/build_release.py
python -B ops/verify_release.py
```
