# AnonX_3 v3.4.10 Final — Playback/UI Isolation + yt-dlp Bootstrap Safety (2026-08-09)

- A missing or deleted Telegram SEARCHING/Queued card is now a noncritical
  presentation failure. Queue admission and confirmed playback remain intact,
  cancellation still propagates, and dead status message IDs are not retained.
- Custom queued-card templates, playlist notices, download-status updates, and
  post-start command cleanup no longer share the media/VC rollback boundary.
  Their failures are logged without being mislabeled as startup failures.
- Added a process-wide single-flight gate around only the first `YoutubeDL`
  constructor. This prevents concurrent cold resolver workers from executing
  bgutil PO-token registration decorators more than once; all constructors and
  extraction work after the first bootstrap remain parallel.
- Routed YouTube, SoundCloud, TikTok, Facebook, and Downloader API yt-dlp
  constructors through the same gate, preserving mocked constructors and public
  service interfaces.
- Added dependency-free regression coverage for a stale queued status card and
  for first-constructor serialization followed by concurrent steady-state work.

Restart is required so the current process discards any already-corrupted
yt-dlp plugin registry. See `ops/release_v3.4.10_runbook.md`.

---

# AnonX_3 v3.4.9 Final — Command-to-Packet Sub-1.5 Architecture (2026-08-09)

- Removed the Telegram SEARCHING-card RPC from the playback critical path.
  Search/direct warmup, the status send, language lookup, and live-VC presence
  check now begin concurrently. A deferred status proxy forwards every later
  mutation only to the bot-owned card, preserving cancellation and error UI.
- Added an authorized per-chat initial-playback lease. Cold YouTube `/play` and
  `/vplay` can start the EXTERNAL capture connection before search completes;
  the resolved Track adopts the exact client/session/stream reservation with
  `reconnect=0`. Failure, cancellation, capacity refusal, and no-result paths
  roll back the provisional call and release the lease.
- `/vplay` now sends a real audio lead through that EXTERNAL capture, waits up
  to 400 ms for outgoing-clock evidence, and then swaps raw A/V onto the same
  call. This removes the old one-second raw-video clock tail from first-audio
  measurement without weakening video handoff or fallback behavior.
- Fixed authenticated micro-player selection and request context. Defaults are
  the union of yt-dlp's maintained authenticated and JS-less clients:
  `tv_downgraded`, `web_safari`, and `android_vr`. Cookie-incompatible and
  auth-required clients fail closed, while the selected client's default
  Innertube config now drives API headers/context instead of empty dictionaries.
- Added secret-free player-response diagnostics and extended the live log gate
  to independently enforce resolver, scheduled-to-packet, and command-to-first-
  packet p95 at 1500 ms with a 100% sample pass requirement.

Local compile and deterministic regression checks validate the architecture and
rollback contracts. External latency is not fabricated: production acceptance
still requires at least 20 fresh uncached `/play` and 20 `/vplay` traces, with
all three p95 metrics at or below 1500 ms. See
`ops/release_v3.4.9_runbook.md`.

---

# AnonX_3 v3.4.8 Final — Bounded Validated Resolver Race (2026-08-09)

- Fixed the player-response micro normalizer so it accepts a safely wrapped
  `signatureCipher` URL when no encrypted `s` challenge exists. Plain
  `sig`/`signature` values are attached through `sp`; encrypted signatures and
  non-HTTP(S) URLs remain fail-closed on the maintained full yt-dlp path.
- Added a 1450 ms total micro budget covering player-response fetch and the
  one-byte GVS 200/206 proof. The existing per-stage timeouts can no longer add
  serially beyond the target fast-lane window.
- Full resolver hedges now race through validation, not extraction alone. Both
  fast lanes perform their GVS proof concurrently while micro candidates remain
  eligible, eliminating the extract-complete -> serial-preflight tail.
- Detached executor-backed race tasks are strongly owned and their results are
  consumed, preventing unhandled task-exception noise during first-valid wins.
- Added executable cipher, budget, validated-race, and trace-parser regressions,
  plus a real-log acceptance gate for `search` -> `play_task_scheduled` p95.

The implementation is locally validated, but the <=1.5s production SLO is not
claimed without at least 20 fresh uncached `/play` and 20 `/vplay` traces. Use
`ops/resolver_latency_report.py` after deployment.

---

# AnonX_3 v3.4.7 Final — Resolver Singleflight + Foreground Race Isolation (2026-08-09)

This release fixes the v3.4.6 production regression captured in `log (28)` without redesigning the working NTgCalls/JIT transport.

- `/vplay` now joins any in-flight prewarm for the same YouTube video across adaptive quality-tier changes (`poor`/`normal`/`good`). A tier change can no longer launch a duplicate 2–5 second full resolver.
- `/play` `foreground_fast` and `audio_escape_fast` now use a dedicated, globally bounded foreground resolver semaphore sized to the sticky resolver workers. Dynamic/background `ytdlp=1` throttling no longer serializes the two latency-critical hedge lanes.
- Command-level assistant readiness starts VC metadata/native-payload warming while provider/direct resolution is still running. `play_media()` reuses the warmed call ref + payload instead of racing `create_call()` against early connect.
- Existing `/vplay` EXTERNAL placeholder → existing-call raw source swap remains reconnect-free. Existing JIT capture, first-real-frame proof, fallback, cleanup, daily restart, watchdog, and secret-safe release packaging are preserved.
- Adds regression coverage for cross-tier singleflight, independent foreground race capacity, sticky slot separation, command-level VC warm reuse, and reconnect-free source swap.

Production acceptance remains evidence-based: confirm `/play connect_to_real_ms < 1000` p95 and `/vplay` first-real-media/capture tail `< 1000 ms` p95 over fresh runs.

---

# AnonX_3 v3.4.6 Final — Sub-1s Post-VC Activation Architecture (2026-08-09)

- `/play`: races two sticky full resolver lanes (primary + lightweight audio escape) beside bounded `android_vr`, `web_embedded`, and `mweb` micro-player lanes.
- `/vplay`: preserves early EXTERNAL placeholder connect, direct adaptive A/V pair support, and existing-call raw source swap (`reconnect=0`).
- EXTERNAL/JIT audio uses a four-frame runway and records exact `vc_connected_external_capture` -> `first_external_audio_frame_accepted` / `connect_to_real_ms` telemetry.
- Stop/track-switch cleanup owns the live EXTERNAL session before `leave_call()`, suppressing expected post-stop `ConnectionNotFound` noise.
- Daily 00:00 Asia/Yangon fresh-process restart preserves reusable media; `bash start` now supervises an event-loop heartbeat and restarts hard-stalled children.
- Target: post-VC activation p95 < 1000 ms for `/play` and `/vplay`. This release does not claim that cold command/search/network end-to-end startup is <1 second; production p95 proof remains required.

## Raw launcher ≤5s deep fix — 2026-08-08

- Fixed the NTgCalls 2.1.0 `MediaSource.SHELL` cold-start launcher failure seen as Boost.Process `default_launcher: No such file or directory`.
- Raw direct audio now gives Boost a PATH-safe launcher token while the selected FFmpeg binary is executed by absolute path (`env -- /absolute/ffmpeg ...`).
- The exact launcher chain is locally probed once during voice-service boot; if `env --` is unsupported, the selected FFmpeg directory is pinned to PATH and the verified basename is used instead.
- Added `FFMPEG_BINARY` and `DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC` configuration knobs.
- A launcher failure is detected before the first VC join and falls back to MediaStream safely; the existing late-join rule remains unchanged.
- Regression: 37/37 direct/VC/YouTube critical tests and 59/59 social/dynamic-stream tests passed. Full pytest remains unsuitable as a release gate in this sandbox because a pre-existing recursion test can hang and the legacy dynamic-resource test has a known source-string assertion mismatch.

# AnonX_3 v3.3.1 - Final Stable Release

Release date: 2026-07-31

## Changes since v3.3.0

- Hardened YouTube search with a provider-budget-aligned deadline and one
  bounded yt-dlp metadata fallback before `play_not_found`.
- Hardened automatic replies with persisted confidence-gated candidates:
  repeated keyword/answer observations are required before activation, while
  explicit `/reply` rules remain immediate and authoritative.
- Corrected authenticated YouTube recovery so a failed provider-injected client
  is not reused as a stale cookie-recovery binding unless a concrete PO token is
  present; provider-bound direct options remain preserved.
- Updated release identity, version assertions, lock metadata, and deterministic
  archive timestamp for the `v3.3.1` final artifact.

## Post-release source update: `AnonX_3` identity migration

- Renamed the deploy root and Python package from the previous numbered identity
  to `AnonX_3`.
- Migrated imports, startup, Docker/Compose, environment defaults, media paths,
  release tooling, tests, operations scripts, and documentation.
- New start command: `python3 -m AnonX_3`; release archive prefix:
  `AnonX_3-v3.3.1-final`.
- Forced compile, package/API import, Compose parsing, recursion regression,
  structure/secret scans, and all 69 smoke tests pass.

## Post-release source update: official PO token provider

- Enabled the official yt-dlp PO-token provider framework for video-specific
  requests using the pinned `bgutil-ytdlp-pot-provider==1.3.1` plugin.
- Replaced the nonfunctional empty-token Nginx stub with the matching pinned
  long-lived provider sidecar on port `4416`.
- Added startup, health, dependency, Compose, client-binding, and retry guards;
  token contents are neither logged nor manually cached by the bot.
- Verified the pinned plugin load and Compose documents in an isolated
  dependency sandbox; compileall, recursion regression, and all 50 smoke tests
  pass.

## Post-release source update: YouTube authenticated defaults

- Removed stale forced `android,web` extraction clients and restored yt-dlp's
  maintained default selection for direct streams and local downloads.
- Refreshed account cookies now use yt-dlp's authenticated defaults; a
  client-bound PO token still preserves its provider-selected client.
- Correct permanent classification keeps alternate YouTube uploads ahead of
  SoundCloud when the selected search result is dead.
- Deployment requires replacing the changed source and restarting the bot.

## Post-release source update: inline `/vsong` search

- Restored `@BotUsername song name` search with eight-result, concurrency,
  timeout, debounce, deduplication, and blacklist bounds.
- Selecting a result sends `/vsong@BotUsername <canonical YouTube URL>` and
  enters the existing video download pipeline immediately.
- The previously removed Premium-emoji, signed-token, and custom inline
  transport subsystems remain removed.
- Deployment requires one-time inline-mode enablement through `@BotFather`
  `/setinline`, followed by a bot restart.

## Release integrity

- Canonicalized active branding, sample MongoDB identity, setup output, and
  environment migration to the current `AnonX_3` deploy root.
- Replaced open-ended dependency ranges with a tested Python 3.13 lock while
  retaining `requirements.in` as the direct dependency source.
- Made shell and Docker dependency installation fail on install conflicts.
- Centralized release identity in `ops/release_meta.py`.
- Added one release gate that compiles, tests, scans, imports the Downloader
  API, runs `pip check`, builds twice, verifies deterministic bytes, and checks
  the embedded manifest and SHA-256 sidecar.
- Environment merging now touches only the current deploy root and writes
  atomically after verifying that existing credential values are preserved.

## Bug fixes (2026-07-28)

- **Cookie watcher `FileNotFoundError`**: `_extract_cookies_to_netscape` wrote
  a temp database into `AnonX_3/cookies/` before ensuring the directory existed.
  Three-layer defense: `ensure_dirs()` creates the directory at startup, the
  watcher `start()` method creates it before launching the watch loop, and the
  extraction method itself creates it immediately before `shutil.copy2` plus
  removes any stale temp file from a previous crash.  All `__pycache__/`
  directories cleared to prevent stale bytecode from masking the fix.
- **`RecursionError` on task cancellation**: Python 3.12 `Task.cancel()`
  recursively walks `_children`, and deep task trees from supervisor restarts
  overflowed the C stack (990+ frames).  `stop()` now collects tasks iteratively
  with `_collect_all_tasks()`, clears `_children` on every task before calling
  `cancel()`, and wraps each cancel in a try/except.  `supervisor._runner`
  clears its own children when catching `CancelledError` before re-raising,
  preventing accumulation across restarts.

## Provider behavior

- SoundCloud and YouTube transport failures remain bounded, sanitized, and
  nonfatal to the bot process.
- No release can guarantee that third-party providers, proxies, restricted
  media, or network paths will always be available.

## Artifact

- `dist/AnonX_3-v3.2.1-final.zip`
- `dist/AnonX_3-v3.2.1-final.zip.sha256`

## Verified

- Clean Python 3.13.13 virtual-environment install and `pip check`
- 48/48 deterministic smoke checks
- Recursion regression, full compile (all AnonX_3 .py), structure, and secret scans
- Downloader API import
- Two byte-identical release builds and manifest verification
- Linux CPython 3.13 artifact availability for all 58 locked entries
- Zero stale `AnonX_3` references across all source files

# AnonX_3 v3.2.0 - Final Performance and Stability Release

Release date: 2026-07-27

## Performance

- Immediate valid `/play` acknowledgement before database/admin lookups.
- Mongo play-mode negative caching removes one database read from the common
  default-mode request path after the first lookup.
- Removed a duplicate Telegram keyboard edit from every normal `/play`.
- Added bounded TTL caching and singleflight to deep YouTube result searches.
- Added a short negative cache to prevent an immediate duplicate provider wave.
- Reused the YouTube API HTTP connection pool across searches and playlists.
- Reduced optional direct-URL metadata wait from 1.8 seconds to 0.35 seconds.
- Skipped the old fixed media settle polling delay when yt-dlp already produced
  a complete artifact.
- Moved FFmpeg thumbnail extraction and FFprobe validation off the asyncio event
  loop so downloads cannot freeze unrelated bot commands.

## Stability

- Provider-race tasks are cancelled and awaited cleanly.
- Search caches are bounded and lazily prune expired entries.
- Persistent YouTube HTTP resources close during graceful shutdown.
- Preserved queue, playlist, autoplay, `/song`, `/vsong`, `/play`, `/vplay`,
  ordinary inline keyboards, cancel behavior, and direct/local fallback.
- At the v3.2.0 release point, Telegram inline search was intentionally removed;
  the post-release v3.2.1 source update above restores the bounded `/vsong`
  handoff only.
- Challenge-only YouTube auth recovery is now wired to an optional Chromium
  image component and a dedicated compose profile mount.
- SoundCloud proxy recovery now reaches explicit direct attempts for metadata,
  downloads, and streams within bounded budgets, while late fallback preserves
  the user's original non-URL query.
- A Unicode-mark-preserving, exact-query-only rescue handles the reported
  Burmese remix/uploader alias without lowering the 0.85 global matcher; URLs,
  derived-query results, ambiguous candidates, unknown durations, and results
  outside ten seconds or 5% remain rejected.
- Corrected the distributable `sample.env` identity and local Mongo database
  name so this variant cannot inherit a sibling variant's default database.
- Replaced an obsolete source-text proxy-signature guard with assertions for
  the real bounded `TypeError` recovery order; the executable compatibility
  test still proves proxy-first construction and proxy-free retry.

## Verified

- Full package compile
- 47/47 deterministic smoke checks in each of four synchronized variants
- Structure validation
- Secret scan on distributable source
- Eight Docker Compose YAML files pass relaxed lint validation
- Sanitized, allowlisted release archive with SHA-256 manifest

The deterministic mocked deep-search cache benchmark must exceed 10x in CI/local
validation. Real cold-network speed cannot be guaranteed because it is bounded
by YouTube, Telegram, proxy quality, media size, VPS resources, and bandwidth.

## SoundCloud Fallback Hardening (2026-07-28)

- SoundCloud search now uses flat discovery so a DRM/private result cannot
  terminate the complete candidate list.
- Ranked candidates receive a bounded playability probe; protected entries are
  skipped in favor of the next valid match.
- Provider timeouts are distinct from valid empty searches, stop repeated query
  attempts, and emit sanitized logs without raw yt-dlp error duplication.
- Metadata extraction uses one resolver-owned proxy attempt and one direct
  retry with yt-dlp internal metadata retries disabled.

## Post-release source update: adaptive YouTube fast lane (2026-08-08)

- The authenticated fast lane now races one POT-free cookie client at a time
  instead of asking yt-dlp to process the whole client ladder in one extract.
- A conclusive fast-lane 403 or bot/PO gate refusal quarantines only the
  responsible client for the configured cooldown; subsequent plays rotate to
  the next candidate while `mweb` + PO-token remains the authoritative path.
- Fast-lane logs now include the actual minting client, and source rewrites keep
  that metadata so later cached-URL failures are attributed correctly.

## Post-release source update: authoritative POT fast path (2026-08-08)

- Authenticated direct playback now defaults to `DIRECT_FAST_LANE_MODE=authoritative_pot`.
- The proven `mweb` + bgutil PO-token path is the only foreground authenticated direct lane; speculative `tv/tv_downgraded/web_embedded` extraction is disabled in this mode.
- Direct format 140 keeps cookies, POT, JS/runtime and source binding while skipping subtitle/HLS/DASH work.
- Fresh authoritative URLs are GVS Range-preflighted; a first 403 purges the POT cache and remints once.
- Fresh preflight success is reused by the outer validator, removing a duplicate network probe. Cached URLs still revalidate normally.

## 2026-08-08 — final merged mweb + POT-only direct resolver

- Consolidated the YouTube 403 fixes into the full project tree.
- Removed the old speculative direct fast-lane implementation and its configuration/rollback state.
- Direct foreground resolution now uses only provider-bound `mweb` + PO token, validates the fresh GVS URL with a Range preflight, reuses a successful 200/206 verdict, and remints once after an authoritative 403.
- Runtime secrets, sessions, logs, cached media, databases and Python bytecode are excluded from the distributed archive.

### mweb + POT startup latency optimization

The authoritative direct resolver now uses yt-dlp's mweb ad-playback context to avoid the mandatory pre-download wait and skips only the unneeded `initial_data` request. The PO-token provider, authenticated cookies, source-address binding, webpage/config fetches, and player JS/nsig path remain intact. Per-play extraction/preflight timing is logged for verification. Set `DIRECT_MWEB_USE_AD_PLAYBACK_CONTEXT=False` or `DIRECT_MWEB_SKIP_INITIAL_DATA=False` to roll back either optimization independently.

## Persistent mweb/POT latency path
`/play` and `/vplay` now prewarm a persistent mweb + PO-token resolver. The first profile is a lightweight Innertube path and the existing robust authoritative profile remains the fail-safe. Watch `authoritative_mweb_pot timing ... profile=lightweight ... ydl_warm=1` in logs to measure the gain on the deployment.

### Direct-start latency follow-up
- Warm YouTube search now starts direct mweb/POT resolution before handing the Track to the main play handler, allowing extraction to overlap the remaining request preparation.
- `/play` now deterministically prefers audio-only itag 140 from the returned formats before progressive itag 18.
- VC late-join behavior remains unchanged.

### Dual-stage direct audio latency
Foreground audio now uses the fastest validated mweb AAC/progressive URL and joins VC only after that source is ready. Exact 140 + POT is promoted asynchronously after audible startup and for queued-next tracks, isolated on dedicated background workers. This removes the 8–10 second exact-140 extraction from `/play -> audible` while retaining 140 for prepared/replayed queue items.

### VC sub-5 fast attach
- Prevalidated direct audio bypasses PyTgCalls MediaStream.check_stream() by attaching as a raw audio stream, eliminating a duplicate remote FFmpeg probe after the project has already proved the GVS URL.
- VC metadata and the local NTgCalls call payload are warmed while media resolves, but Telegram JoinGroupCall remains deferred until the source is ready.
- Assistant unmute reuses the warmed group-call reference; raw attach falls back to the existing MediaStream recovery path on native/ShellError failures.
- Production timing now reports vc_fast_attach play_ms/unmute_ms and whether a prepared native payload was used.

### Fresh direct sub-4 pipeline
- Foreground audio first attempts one guarded mweb player-API request for a direct itag-18 URL, then reuses the existing HTTP 200/206 GVS validation; any private-API drift, cipher-only result, missing URL, or rejected preflight falls back to the normal full yt-dlp extractor.
- Persistent YoutubeDL workers are keyed by stable resolver profile rather than mutable session-cookie values. Cookie rotations reload into the existing cookiejar in place, keeping subsequent fresh plays warm without ignoring genuine cookie-file replacement failures.
- Validated fresh audio starts an absolute-FFmpeg PCM predecoder before the late VC join. NTgCalls receives a MediaSource.EXTERNAL microphone and the first predecoded 10 ms PCM frame immediately after connect, overlapping remote-open/decode with the Telegram/WebRTC handshake instead of paying it after stream attachment.
- The external PCM pump owns EOF/failure cleanup and retains MediaStream/local recovery. Telegram JoinGroupCall still occurs only after a source has been resolved and validated.
