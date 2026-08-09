# DECISIONS — Decision Log

> Variant: AnonX_3
> Last updated: 2026-08-09

Current source is authoritative; this workspace does not contain Git metadata.

## Audio-First Startup V4 and Independent Cold p95 Gate (2026-08-09)

- Required assistant unmute remains mandatory for audible proof but completes in
  an owned background task; real PCM submission and RTP never await it.
- `/vplay` keeps the EXTERNAL microphone live and attaches video in an owned,
  cancellable post-start task. Camera attachment is not audio evidence.
- Native call creation has one assistant/chat owner. V4 disables speculative
  `create_call()` prewarm plus reset/settle retries that raced the real join.
- Release requires 100 cold `/play` and 100 cold `/vplay` traces. Each command
  independently requires actual audible end-to-end p95 <=3000 ms; aggregation
  cannot hide a failing command. `DIRECT_STARTUP_V4=False` is the rollback.

## Presentation Cannot Roll Back Playback; First yt-dlp Bootstrap Is Single-Flight (2026-08-09)

- Queue/media/VC state changes are authoritative; Telegram status cards,
  playlist notices, and command cleanup are best-effort presentation. Their
  failures may be logged but cannot enter the playback startup rollback path.
- Cancellation remains authoritative and is never swallowed by the
  presentation boundary. A confirmed stale card is retired from progress
  ownership and its IDs are cleared from the queued media object.
- External yt-dlp plugin registration is process-global. Serialize exactly one
  successful cold `YoutubeDL` constructor, then release all later constructors
  and extraction work to normal concurrency. Do not suppress registry
  assertions or monkeypatch third-party decorators.

## Command Acknowledgement Is Not a Playback Dependency (2026-08-09)

- Telegram message-send latency must not serialize search, VC presence, or
  initial connection. A deferred status proxy is the only allowed mechanism:
  reads stay non-blocking, but edits/deletes await and target the bot-owned card.
- A provisional VC connection is permitted only after command authorization,
  assistant readiness, a fresh live-group-call proof, resource admission, and
  exclusive ownership of the same per-chat lock used by normal first playback.
- The provisional transport has one owner and one transfer. The resolved Track
  adopts its session/task/slot with `reconnect=0`; every non-adopted terminal
  path must close and release all four.
- This historical v3.4.9 target is superseded by Audio-First Startup V4 above.
  Source tests prove control flow, not external latency.

## Micro Clients Follow yt-dlp Authentication Policy (2026-08-09)

- Default micro clients are `tv_downgraded`, `web_safari`, and `android_vr`, the
  union of yt-dlp's maintained authenticated and JS-less defaults.
- The fast path initializes `YoutubeIE`, evaluates actual cookie auth, rejects
  clients that cannot accept cookies or require absent auth, and passes the
  client's default ytcfg into both player API header/context inputs.
- The path does not invent PO tokens or solve encrypted player-JS challenges.
  Any unsupported response or failed GVS proof continues on the already-running
  authoritative yt-dlp lanes.

## Resolver Winners Require Bounded Media-URL Proof (2026-08-09)

- A player-response micro candidate may use a direct URL or a wrapped URL only
  when no encrypted `s` signature challenge exists. The bot does not implement
  its own player-JS solver; ambiguous envelopes fall back to pinned yt-dlp.
- The micro lane owns one total 1450 ms budget for the API request and 200/206
  range proof. Stage timeouts are subordinate to that deadline.
- A full extractor is not a race winner merely because metadata extraction
  returned. Both foreground hedges continue independently through media proof,
  and the first validated full or micro source wins.
- This historical resolver-only SLO is superseded by the independent 100+100
  command-to-audible V4 gate above. Local tests cannot certify external latency.

## Cold Initial Direct Attach Bypasses MediaStream Inspection (2026-08-08)

- Scope remains only the cold initial YouTube `/play` and `/vplay` transaction.
  Once VC/unmute and the signed raw URL are ready, submit a raw PyTgCalls
  `Stream` in an owned task instead of constructing `MediaStream` and awaiting
  its internal `check_stream()` path.
- Preserve the local URL shape/SSRF boundary. Remove only startup-gating remote
  probe, FFmpeg capability scan, reconnect delay, prebuffer, retry sleep, and
  synchronous play completion from this path. Established-call and legacy
  paths retain their current validation and reconnect behavior.
- Use a transparent FFmpeg PCM relay for spawn/input/decode timestamps. Record
  play-call before/after and attachment in the owner task; use NTgCalls outgoing
  clock advancement after decoded PCM as the locally observable first-packet
  signal. This is operational evidence, not a claim that Telegram acknowledged
  or rendered the packet.
- Do not schedule current-track cache/download, UI/profile hydration, metadata,
  or prefetch until the first-packet signal. These tasks remain owned and fully
  asynchronous. A play exception or early fatal stream event retains the
  existing bounded local fallback.

## Initial YouTube Join and Resolve Run in Parallel (2026-08-08)

- Scope is only a cold initial YouTube `/play` or `/vplay`. Queue auto-next,
  `/skip`, replay/seek, forced paths, established calls, and other providers do
  not opt into this startup transaction.
- Start empty-VC join plus assistant self/admin unmute concurrently with raw
  direct-source resolution. A usable unmuted call and a validated raw source
  are the only two foreground readiness conditions.
- Assistant unmute is mandatory for this transaction. If both self-unmute and
  bot-admin unmute fail, leave the empty call, release its stream reservation,
  return a localized error, and let initial admission roll back. Do not attach
  media or begin download/cache work.
- After both branches are ready, schedule PyTgCalls `play(stream)` immediately
  and observe it asynchronously. Network probes, live profile sampling,
  metadata, thumbnails, now-playing updates, cache/download, and prefetch are
  post-packet background work and cannot gate first audio.
- Keep URL shape and SSRF validation in the foreground. Preserve bounded join
  retries, FloodWait assistant rotation, stream admission, and cleanup.
- PyTgCalls task completion records stream attachment. Actual outgoing-clock
  movement records the first-packet milestone. `DIRECT_START_PROOF_SEC` remains
  an asynchronous early-fatal monitor; a fatal signal may consume exactly one
  direct-watchdog local fallback.

## YouTube /play Uses Direct-first VC Startup (2026-08-03)

- Cold YouTube `/play` and `/vplay` try direct VC streaming before any
  current-track local/cache/CDN path, even when a complete local READY file is
  already present.
- Search warmup may hydrate metadata and hand off the resolved Track, but it
  must not start a current-track `warm-local-after-search` download while
  direct-first playback is enabled.
- For non-parallel legacy paths, direct success retains the original blocking
  proof behavior. The 2026-08-08 initial-start decision supersedes that rule
  only for cold initial YouTube playback.
- yt-dlp direct extraction must produce a raw HTTP media URL with an audio
  codec. YouTube watch/search URLs, metadata objects, and local paths are not
  valid direct audio sources.
- Preserve yt-dlp HTTP headers and proxy into PyTgCalls/FFmpeg. The player must
  use the installed PyTgCalls `play(chat_id, stream, config)`/`MediaStream`
  signature at runtime instead of assuming an older API.
- `NoAudioSourceFound` and direct startup errors are logged with runtime
  diagnostics and fall through to the deferred local fallback; they must not
  crash playback.
- Current-track local acquisition starts only after direct URL resolution,
  startup, proof, or early remote playback failure. Queued-track prefetch
  remains allowed.

## Cache-first /play Uses One yt-dlp Owner (2026-07-30)

- Superseded for active YouTube current-track starts by the 2026-08-03
  direct-first VC startup decision.
- Resolve a validated local/catalog asset by source ID or normalized user
  query/title before provider metadata search and before yt-dlp.
- Freeze one audio/video download identity for a new playback request. Direct
  VC startup, foreground warmup, queued prefetch, and CDN publication must join
  that owner; none may open a hidden quality/recovery extraction ladder.
- A one-shot attempt has a request-scoped terminal outcome. Cancellation
  detaches only the status-card observer; it must not stop a shared physical
  download or allow stale UI work to mutate playback.
- Persist a complete locally downloaded asset as a durable SQLite catalog row
  with normalized aliases. TTL cleanup must not erase it, while ordinary bounded
  LRU/capacity cleanup may still reclaim it.
- Per-video YouTube auth challenges remain independent of the global circuit;
  an idle global circuit must not clear another video's active challenge.

## Delegate YouTube Client Selection to yt-dlp (2026-07-28)

- Do not pin the normal extraction path to a client list that ages separately
  from yt-dlp's YouTube extractor.
- Authenticated cookies use yt-dlp's maintained account-compatible defaults.
- Preserve an explicit client only when a PO token is bound to it.
- Keep recovery bounded: direct and configured-proxy cookie attempts may run
  once, after which the auth circuit and provider fallback control amplification.

## Restore Inline Search as a `/vsong` Handoff (2026-07-28)

- Restore only the small `@BotUsername query` search surface; do not restore the
  removed signed-token, Premium-emoji, runtime-template, or custom transport
  subsystem.
- Reuse `yt.deep_search()` with debounce, timeout, concurrency, result, and
  cache bounds.
- Selecting a result sends `/vsong@BotUsername <canonical YouTube URL>`.
  In the bot's private chat or a group where it is present, Telegram delivers
  the sent-via-bot message; the explicit command address also satisfies group
  privacy mode.
- Keep `plugins/song.py` authoritative for resolve, download, thumbnail, cache,
  progress, cancellation, and upload behavior.

## Separate Song and Video-Song Commands (2026-07-26)

- `/song` always downloads audio; legacy video flags no longer change its mode.
- `/vsong` is the single explicit video-download command and uses a generated Telegram thumbnail.
- Version the video file-id cache when thumbnail behavior changes so legacy thumbnail-less cached uploads do not mask the fix.
- Use `cancel_dl_pyrogram()` at direct Pyrogram message boundaries; reserve the styled `cancel_dl()` dictionary for Bot API-aware Utilities paths.
- Route `/song` and `/vsong` Searching through `utils.reply_formatted()` so they can safely use the same danger-colored `cancel_dl()` button as `/play`.
- Do not emit the standalone `song_downloading` transition; retain the original Cancel-enabled card and let only real provider progress update that same message.

## Performance Release v3.2.0 (2026-07-25)

- Optimize measured hot-path waste rather than promise a universal network
  multiplier.
- Acknowledge valid play requests before slow permission lookups.
- Cache both true and false play-mode states.
- Coalesce identical search/download work and bound all new caches.
- Reuse outbound HTTP connection pools and close them during shutdown.
- Keep blocking FFmpeg/FFprobe work outside the asyncio event loop.
- Preserve defensive media validation and fallback behavior; bypass settling
  waits only when a complete artifact is already proven.

## Remove Telegram Inline Search (2026-07-25)
- Historical decision superseded by the bounded `/vsong` handoff restoration on
  2026-07-28.
- Remove the complete `@BotUsername query` subsystem after repeated platform-level custom-emoji incompatibility.
- Remove inline-only code, settings, admin keys, transport, deep-link tokens, tests, and active documentation without changing ordinary bot-message inline keyboards.
- Preserve `/play`, `/song`, `/vsong`, `yt_copy`, `yt_open`, queue, playlist, and AI DJ behavior.
- Do not purge legacy Mongo values automatically; they are harmless and external data deletion was not requested.

## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX_3 and AnonX_3 .env).

## Video-bound PO Token and Bounded 403 Recovery (2026-07-24)
- Use current yt-dlp `CLIENT.CONTEXT+TOKEN` syntax and default to the recommended `mweb` GVS client when a real provider is enabled.
- Never reuse video-bound PO tokens across media IDs.
- After a 403, refresh the failed binding once and then use materially different cookie-free client profiles; do not repeat the same poisoned extraction context.
- Preserve bounded retries and the existing alternate-source fallback instead of claiming guaranteed YouTube access.

## Recovery Circuits Follow the Final Error Class (2026-07-27)
- An earlier auth challenge is not sufficient to open the global circuit after authenticated recovery changes the terminal class.
- Try the authenticated original selector and one authenticated permissive selector; a final format mismatch continues through bounded format/client recovery.
- Open the YouTube auth circuit only for a final explicit auth challenge.
- Treat an empty alternate-source search as a healthy provider response, not a source-health failure.

## Challenge-Only Chromium Wiring and Original-Query Fallback (2026-07-27)

- Keep ordinary public requests cookie-free, but ship the container with
  optional Chromium and an explicit dedicated-profile mount so a classified
  YouTube auth challenge can use the existing single-flight export immediately.
- Never auto-discover a personal/default profile and never automate Google
  credentials, CAPTCHA, or account verification.
- Give SoundCloud proxy and direct metadata attempts separate bounded budgets
  inside `/play`'s outer deadline, and set `proxy=""` on every direct attempt.
- During late YouTube-download recovery, search the original non-URL user query
  before derived title/artist metadata; this preserves Burmese and other
  non-Latin input.
- Do not lower the global weighted-match threshold for cross-source uploader
  aliases. After normal rejection, allow one automatic rescue only when the
  same original text query produced exactly one distinct candidate whose
  Unicode-preserved title begins with the full query, whose suffix is a known
  version label, and whose known duration is within both ten seconds and 5%.

## 2026-07-26 — Isolate global silent kick from moderation

- Use a dedicated `global_kicks` collection and plugin.
- Require both `filters.chat(app.logger)` and `app.sudoers` for every control
  command; do not expose these commands through the public bot menu.
- Exclude the logger chat from ordinary moderation `/kick`, preventing a
  non-sudo administrator from reaching the legacy handler there.
- Protect the bot, owner, and current sudo identities from watchlist insertion.

## Final-Stable Release Gate (2026-07-27)

- Keep the release identity at `v3.2.0-final`: runtime recovery behavior is
  unchanged; this closure repairs the obsolete test contract and distributable
  sample identity.
- Guard py-yt-search compatibility by asserting the bounded operational order:
  proxy-first construction, `TypeError`, removal of only `proxy`, then one
  proxy-free retry. Do not require the retired `inspect.signature` strategy.
- Every `sample.env` must name its own variant and local Mongo database so
  sibling deployments remain isolated by default.
- Build final archives only after 47/47 smoke, compile, structure, secret, and
  Compose YAML gates pass in all four variants.

## SoundCloud Discovery Is Flat, Playability Is Probed (2026-07-28)

- Keep `scsearch` discovery flat so one DRM/private result cannot abort the
  complete search response.
- Rank metadata candidates first, then fully resolve at most three candidates
  to verify playability; reject DRM/private/unavailable entries and continue
  with the next ranked match.
- Treat a completed empty search as healthy, but raise a typed transport error
  for proxy/direct timeouts so fallback stops its query ladder and records the
  provider failure once.
- Give yt-dlp metadata attempts zero internal retries and capture its logger.
  The resolver owns the bounded proxy/direct retry and emits one sanitized
  application log instead of raw duplicate extractor errors.

## One Canonical Final Release (2026-07-28)

- Release only the active `AnonX_3` root as `v3.2.1-final`; sibling variants are
  historical and are not restored or synchronized.
- Keep direct dependency intent in `requirements.in` and publish only the exact
  Python 3.13 graph in `requirements.txt`.
- `python -B ops/release_gate.py` is the sole publish gate. It must prove clean
  dependencies, executable regressions, secret exclusions, deterministic
  archive bytes, manifest integrity, and sidecar integrity.
- Do not mutate `.env` or runtime data during release validation. The merge
  utility is current-root-only and credential-preserving when explicitly run.

## Browser Cookie Integrity and Egress Recovery (2026-07-28)

- Chromium cookie values must be exported through yt-dlp's browser integration;
  raw SQLite rows are never allowed to replace the canonical jar when protected
  values cannot be decrypted.
- A non-empty known auth cookie is an auth marker, not proof that YouTube has
  accepted the session. Runtime logs use `auth_markers` accordingly.
- After a classified auth challenge and one cookie refresh, try the direct
  browser-matching egress first and the configured proxy second. Each transport
  gets only the original and permissive selectors; open the global circuit only
  after both materially distinct routes still return `AUTH_CHALLENGE`.
- Keep the circuit global for load protection but retain exact video IDs so only
  the request that opened it receives the auth-specific user message.

## Direct-Resolve Fast Lane (2026-08-08)

- The 11.1s direct resolve was the yt-dlp extract, not the VC join. `mweb` sets
  `REQUIRE_JS_PLAYER` and needs a GVS PO token, so every cold play paid a player
  JS fetch, an n-signature solve through Deno, and a fresh bgutil mint
  (`pot_age_ms=0` on every trace).
- Race JS-free, player-token-exempt clients (`ios,android`) concurrently against
  the authenticated `mweb` lane and keep the first usable audio URL. Concurrency
  is the contract: a fast-lane miss must cost zero, so no sequential fast-first
  path is acceptable.
- The winner is proven by the existing 403 probe, not by trust. Only a
  conclusive probe verdict moves the breaker; an inconclusive probe proves
  nothing about the minting client.
- Park the fast lane for a cooldown after `DIRECT_FAST_LANE_MAX_403` consecutive
  403s. A client whose URLs Google refuses must stop being raced rather than
  cost a 403 probe on every play.
- A fast-lane win does not clear the auth challenge state. A JS-free client
  succeeding is not evidence that the authenticated path is healthy.
- Only clients that the installed yt-dlp's `INNERTUBE_CLIENTS` table reports as
  `REQUIRE_JS_PLAYER=False` may race; unknown clients are dropped and an import
  failure disables the lane. `android_vr` stays excluded — it now requires a GVS
  PO token and `_recovery_client_opts` already rejects it.
- `vc_join` was marked only after the serial resolve returned, so extract time
  read as slow VC join. Mark `direct_resolved` separately and log
  `direct_parallel_skipped` with its reason when the overlap path is bypassed.


## Authenticated Fast-Lane Revision (2026-08-08)

- The anonymous `ios,android` fast lane is superseded for deployments that use
  account cookies. Current YouTube/yt-dlp behavior can bot-gate those clients on
  datacenter IPs, and those clients do not support account cookies, so stripping
  auth cannot make that path reliable.
- With cookies enabled, `DIRECT_FAST_LANE_CLIENTS=default` now means: preserve
  the cookie file, source-address binding, Deno and EJS runtime, remove only the
  forced bgutil PO-provider/mweb override, and let the installed yt-dlp choose
  its authenticated default clients. This lane still races the authoritative
  mweb + PO-token lane, so a miss adds no serial latency.
- A persisted legacy `DIRECT_FAST_LANE_CLIENTS=ios,android` is remapped to the
  authenticated default selection when cookie mode is active. Cookie-free mode
  retains the anonymous JS-free behavior and uses `android_vr` for the `default`
  sentinel.
- Authenticated fast-lane extraction must not force-skip webpage/configs because
  yt-dlp may need ytcfg/session data to build cookie authorization headers. It
  may still skip subtitles/HLS/DASH work for the direct M4A audio path.
- Fast-lane logging identifies `lane=authenticated_fast` versus
  `lane=anonymous_fast`; raw cookie values and PO tokens remain forbidden in
  logs.

## Fast-Lane Gate Refusals And yt-dlp Console Output (2026-08-08)

- yt-dlp printed its own coloured `ERROR:` block straight to stderr on every
  fast-lane bot-gate miss, outside the log format and with no chat, video, or
  lane context. `quiet` does not cover it: `YoutubeDL.trouble` calls
  `to_stderr` unconditionally and only checks `quiet` for informational output.
  Pass a `logger` — `to_stderr` then routes through it — and set `no_color`, so
  the escapes never reach the log even when stderr is a TTY.
- yt-dlp's exception text carries the same escapes, which is why they appeared
  inline in `direct_fast_lane miss ... err=`. Strip ANSI before any yt-dlp text
  is logged, not just at the sink.
- The fast-lane breaker only counted 403 probe verdicts, and the probe is
  reachable only when the lane produces a URL. A gate refusal kills the extract
  before that, so the streak never moved and the lane was never parked: every
  cold play paid a doomed ~1.4s extract plus a console error block, forever.
  Count extract-stage `AUTH_CHALLENGE`/`CLIENT_PO` refusals on their own streak
  (`DIRECT_FAST_LANE_MAX_GATE`) into the same park.
- Timeouts and transport errors reset that streak instead of feeding it — they
  are noise, not a verdict on whether the gate refuses these clients.
- A fast-lane gate refusal must never open the global auth-challenge circuit.
  This lane deliberately strips the cookies and PO token the authenticated lane
  carries, so its refusal says nothing about the authenticated path — which the
  logs show resolving normally while the fast lane is gated.

## Adaptive Fast-Lane Client Quarantine (2026-08-08)

- A static yt-dlp `GVS_PO_TOKEN_POLICY(required=False)` is an admission hint,
  not proof that YouTube will accept the resulting media URL for this account,
  IP, and rollout state. The observed authenticated fast ladder
  `tv,tv_downgraded,web_embedded` still produced a conclusive GVS 403 while the
  authoritative `mweb` + PO-token lane returned 206.
- Race exactly one speculative client per yt-dlp extraction. Passing a whole
  ladder makes yt-dlp perform several player requests inside the supposed fast
  lane and defeats the latency goal.
- A conclusive GVS 403, classified bot-gate/CLIENT_PO extract refusal, or a
  single-client no-usable-audio result quarantines only that client for
  `DIRECT_FAST_LANE_COOLDOWN_SEC`. The next request rotates to the next admitted
  client. The authoritative lane is never quarantined by speculative evidence.
- Keep the old global 403/gate streak only as a compatibility fallback when an
  extractor result does not expose the minting `_client` metadata.
- Preserve `DirectStreamSource.client` whenever the local-path field is replaced
  and log the actual minting client in fast-lane diagnostics; otherwise a later
  cached-URL 403 cannot be attributed correctly.

## Authoritative mweb + POT as Primary Fast Path (2026-08-08)

- The live trace proved the provider-bound `mweb` URL with a real GVS Range response (`206`) while the speculative authenticated `tv/tv_downgraded/web_embedded` ladder was rejected with `403 / gvs_rejected`.
- Authenticated direct playback therefore defaults to `DIRECT_FAST_LANE_MODE=authoritative_pot`: do not spend a foreground extract slot on the token-free speculative lane. The configured `PO_TOKEN_CLIENT` (default `mweb`) plus bgutil provider is now the foreground fast path.
- Preserve cookies, source-address binding, JS runtime, remote components, and the bgutil provider. Optimize only work irrelevant to direct format 140 (`translated_subs`, HLS, DASH).
- A freshly minted authoritative source is Range-probed before it wins. `206` (and a valid `200` server response) is accepted. The verdict is carried on the fresh source so the outer validator does not repeat the same probe; cached sources are always probed again.
- A first authoritative `403` purges yt-dlp's PO-token cache and remints once. A second conclusive rejection falls through to the existing local/download recovery path.
- `DIRECT_FAST_LANE_MODE=adaptive_legacy` remains as an explicit rollback switch. Its per-client quarantine logic is retained but is no longer the default authenticated startup path.
