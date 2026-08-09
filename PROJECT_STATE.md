- 09-Aug-2026: **Audio-first startup V4 implemented**: required unmute no longer
  blocks EXTERNAL real PCM/RTP, `/vplay` camera attach is background-owned, and
  proof requires real PCM plus post-submit outgoing-clock advance plus confirmed
  unmute without depending on video attach. Speculative native-call precreation
  and connection reset/settle retries are bypassed under `DIRECT_STARTUP_V4`.
  Resolver micro budgets are 1.20s total / 0.95s lane / 0.20s HTTP proof. The
  live release gate now requires 100 cold `/play` and 100 cold `/vplay` samples,
  independently p95 <=3000ms. Live canary evidence remains required.
- 09-Aug-2026: **v3.4.10 playback/UI isolation and yt-dlp bootstrap safety**:
  Telegram status-card edits and other presentation-only work no longer share
  the queue/media/VC rollback boundary. A deleted queued card keeps the track
  admitted and clears dead message IDs. Every in-process `YoutubeDL`
  constructor now passes through a process-wide gate that serializes only the
  first plugin-loading constructor, then restores full concurrency. The exact
  pinned yt-dlp/bgutil six-thread failure was reproduced before the change and
  completed with zero duplicate-registration errors afterward. Dependency-free
  stale-card and constructor-concurrency regressions pass; live Telegram restart
  verification remains required.

- 09-Aug-2026: **v3.4.9 command-to-packet sub-1.5 architecture**:
  YouTube search/direct warm, status acknowledgement, language lookup, and VC
  presence now overlap from command arrival. Authorized initial requests hold a
  per-chat admission lease and start an EXTERNAL capture before search returns;
  the final Track adopts it without reconnecting. Authenticated micro lanes use
  yt-dlp's maintained `tv_downgraded`/`web_safari` defaults and correct client
  ytcfg, while unauthenticated JS-less fallback retains `android_vr`. `/vplay`
  proves an audio packet before its same-call raw A/V swap. Local regression and
  compilation pass; live <=1.5s p95 remains gated on 20 fresh `/play` plus 20
  `/vplay` samples for resolver, packet tail, and full command-to-packet E2E.

- 09-Aug-2026: **v3.4.8 bounded validated resolver race**:
  player-response fast lanes now recover only safe signed cipher envelopes,
  never encrypted `s` challenges; player-response fetch plus GVS proof owns a
  1450 ms total budget. Both foreground full-extractor hedges now race through
  independent 200/206 validation, while robust mweb/POT, 403 remint/local
  fallback, JIT transport, and reconnect-free `/vplay` source swap remain.
  Executable regressions and a real-log `search` -> `play_task_scheduled` p95
  gate were added. Production <=1.5s acceptance remains pending fresh uncached
  `/play` and `/vplay` samples.

- 08-Aug-2026: **Cold PyTgCalls direct-attach latency path optimized**:
  initial YouTube `/play` and `/vplay` now bypass PyTgCalls 2.2.11
  `MediaStream.check_stream()` by supplying an observed raw `Stream` after VC
  and URL readiness. `client.play()` is submitted as an owned asynchronous task
  with exact before/after/attachment timestamps. The PCM relay logs FFmpeg
  spawn, first raw-input bytes, and first decoded frame; NTgCalls outgoing-clock
  movement supplies first Telegram-packet evidence. Cold FFmpeg startup removes
  reconnect sleeps and uses low-buffer/low-analysis input flags. Cache/download,
  metadata, UI/profile work, and prefetch start only after first-packet evidence.
  URL/SSRF validation, admission cleanup, early-fatal monitoring, and local
  fallback remain intact; established/non-initial paths are unchanged.
  Validation: touched files compile PASS; targeted cold-start/raw-relay/unmute
  tests PASS; installed raw-Stream bypass and FFmpeg option compatibility
  confirmed; full smoke **86/87**, with only the unchanged pre-existing
  `test_one_shot_download_publishes_stream_to_concurrent_waiter_once` failure.
  Live Telegram timestamp comparison remains required to prove the ~12 s
  audible target under production network conditions. Confidence: 94%, tier: A.

- 08-Aug-2026: **Initial YouTube VC/source parallel startup implemented**:
  cold initial `/play` and `/vplay` now prepare VC join plus mandatory assistant
  unmute concurrently with raw direct-source resolution, attach immediately
  when both are ready, and move startup proof, network/ffmpeg diagnostics,
  profile refresh, metadata, thumbnails, now-playing, cache/download, and
  prefetch behind stream acceptance. Unmute failure leaves the empty call and
  releases its reservation before playback. Early fatal proof signals retain a
  one-shot local fallback. Scope excludes queued/seek/replay/skip/non-YouTube
  paths. Validation: package/tests compile PASS; focused parallel/unmute/proof
  regressions PASS; dynamic stream scaling **28/28** PASS; dynamic resource
  control **26/26** PASS; full smoke **85/86**, with only the unchanged
  pre-existing `test_one_shot_download_publishes_stream_to_concurrent_waiter_once`
  failure (also present in the 82/83 baseline); recursion, structure, locale
  JSON, and secret checks PASS. A live Telegram audible-start check remains a
  deployment validation. Confidence: 93%, tier: A.

- 04-Aug-2026: **v3.3.1-stable deep fix release**:
  Production errors fixed and error-handling hardened across the Bot API
  layer, error monitor, playback services, and TikTok/Facebook download paths.
  Full changelog below. Validation: `python -m compileall -q AnonX_3` PASS,
  `python -B tests/run_unit_smoke.py` **83/83 PASS**, `test_recursion_fix.py`
  PASS, `ops/verify_structure.py` STRUCTURE OK, `ops/secret_scan.py` OK.
  Restart required. Confidence: 99%, tier: A.

  **Fixes applied (7 total):**

  1. **`message to be replied not found` → `MessageToReplyNotFound` exception**
     (`bot_api.py`, `_utilities.py`, `error_monitor.py`):
     Stale-reply errors (user deleted message before bot replied) now raise a
     specific exception caught gracefully by callers instead of falling through
     to generic RuntimeError + owner error report.

  2. **`NameError: name 'message_id' is not defined` — TikTok fallback crash**
     (`calls.py:2264`):
     Changed bare `message_id` → `message.id`. The `play_media` function
     parameter is `message: Message`; `message_id` was never defined in scope.

  3. **CRITICAL: Facebook queued-track block used TikTok functions**
     (`calls.py:3380-3403`):
     Copy-paste from the TikTok block: the Facebook path called
     `config.TIKTOK_DIRECT_CACHE_BG`, `tiktok.start_current_cache()`, and
     `tiktok.await_current_cache_or_download()`. Fixed to use `config.FACEBOOK_*`
     and `facebook.*` counterparts.

  4. **Unused TikTok download result in soft prefetch**
     (`calls.py:3378-3379`):
     `download_path` was assigned but never stored on `media.file_path`.
     Fixed to store the result directly and use defensive `getattr()`.

  5. **Bare `media.id`/`media.video` access → defensive `getattr()`**
     (`calls.py:2271-2272, 2476`):
     Three locations used bare attribute access inconsistent with the
     surrounding defensive `getattr(media, ...)` pattern. Hardened.

  6. **`_request_form` error-classification gaps**
     (`bot_api.py`):
     Added missing `_is_stale_reply_error` → `MessageToReplyNotFound` and
     `FileTooLarge` checks to the multipart/form-data path (used by
     `sendPhoto`/`editMessageMedia`), matching the JSON `_request` path.

  7. **Error monitor benign-filter hardening**
     (`error_monitor.py`):
     Broadened `_is_bot_api_benign_edit_text` to match all Bot API method
     prefixes (not just `editMessage`). Added `_is_bot_api_benign_general`
     for 11 common non-actionable Bot API errors (query too old, can't parse
     entities, button data invalid, etc.). Added missing chat-forbidden
     substrings to `_is_chat_forbidden_error`. All filters integrated into
     both `TransientRuntimeNoiseFilter` and `DeepSeekErrorMonitor`.

- 04-Aug-2026: **Bot token rotation applied (PimPimPomPomMusicBot)**:
  Operator reported BotFather token revoke for `@PimPimPomPomMusicBot`.
  Local `AnonX_3/.env` `BOT_TOKEN` already matches the replacement token.
  Live `getMe` validation succeeded (`ok=true`, id `8863773212`, username
  `PimPimPomPomMusicBot`). No source-code change required. Restart any
  running bot process (local or VPS/Docker) so the process reloads env.
  Do not commit `.env` or paste the raw token into docs/commits.
  Confidence: 99%, tier: A.

- 03-Aug-2026: **Deep identity rename to AnonX_3**:
  Renamed the active checkout and Python package, migrated imports, runtime
  paths, environment/sample configuration, deployment/release metadata,
  documentation, tests, and the non-sensitive branded asset. Protected
  runtime files were preserved. Validation: compile/import checks, smoke,
  recursion, structure, residual-reference, and release-manifest audits.
  Restart required. Confidence: 99%, tier: A.

- 03-Aug-2026: **Broadcast pin and HTML rendering fix**:
  `/broadcast -pin` now silently pins successfully delivered group and
  supergroup messages only, continues after pin permission/API failures,
  reports pin success/failure counts, and never pins private-user deliveries.
  Shared status-message edits now preserve explicit parse modes and default to
  HTML so formatted locale errors render correctly. Validation: compileall,
  `python tests/run_unit_smoke.py` **71/71 PASS**, recursion checks, and
  structure verification. Restart required.
  Confidence: 99%, tier: A.

- 03-Aug-2026: **Deep project identity rename completed**:
  The active project root and Python package now use `AnonX_3`; imports,
  entry points, configuration/environment key names, cache namespaces,
  deployment identities, release metadata, tests, and documentation were
  updated consistently. Existing logs, media/database files, downloads,
  caches, bytecode, and session data were preserved. Validation:
  `python -m compileall -q AnonX_3`, package and entry-point imports,
  `python tests/run_unit_smoke.py` **69/69 PASS**, recursion checks,
  structure verification, and zero residual legacy references in the
  inspectable project scope. Restart required.
  Confidence: 99%, tier: A.

- 03-Aug-2026: **Auto-learn mixed datetime DB warning deep fix**:
  Teach-by-reply candidate storage now normalizes persisted datetimes, numeric
  timestamps, and ISO strings to aware UTC before comparing or re-saving them.
  This fixes the production warning `can't compare offset-naive and
  offset-aware datetimes` when pruning `auto_reply_candidates` containing older
  Mongo/PyMongo timestamp shapes. Validation: `python tests/run_unit_smoke.py`
  **69/69 PASS**. Restart required.
  Confidence: 99%, tier: A.

- 03-Aug-2026: **Bot API stale markup edit error-report deep fix**:
  Production report `e895529a44` was a stale Telegram UI edit, not a playback
  failure: `editMessageReplyMarkup` returned `Bad Request: message can't be
  edited`. The Bot API wrapper and utility classifier now treat
  `message can't/cannot be edited` as stale edit wording, and the error monitor
  suppresses this benign Bot API edit text before owner reporting. DeepSeek
  being disabled no longer adds noise for this case. Validation:
  `python tests/run_unit_smoke.py` **69/69 PASS**. Restart required.
  Confidence: 99%, tier: A.

- 03-Aug-2026: **YouTube direct stream NoAudioSourceFound hardening**:
  Direct resolution now returns a structured `DirectStreamSource` containing
  the raw media URL, yt-dlp HTTP headers, proxy, host, and audio format. The VC
  path validates the URL as media, probes FFmpeg audio input open, creates the
  PyTgCalls `MediaStream` with headers/ffmpeg parameters, and only marks direct
  successful after `client.play()` plus the observable proof window. Rich
  diagnostics are logged for `NoAudioSourceFound`/startup failures, and local
  download remains deferred until direct playback actually fails. Validation:
  `python tests/run_unit_smoke.py` **69/69 PASS**. Restart required.
  Confidence: 98%, tier: A.

- 03-Aug-2026: **YouTube direct-first VC playback switch**:
  `.env` now keeps YouTube current-track cache background disabled and PO token
  provider disabled. Cold YouTube `/play`/`/vplay` resolves and proves the
  direct VC stream before any current-track local/cache/CDN acquisition;
  `StreamEnded` during the proof window fails the direct attempt, and early
  remote death after proof starts the deferred local fallback. Warm search still
  hands off metadata but no longer starts `warm-local-after-search` while
  direct-first is enabled. Restart required to load env/config changes.

- 30-Jul-2026: **VC inactive queue-stall and cache-first acquisition deep fix**:
  `/play`, `/vplay`, and force variants reject an inactive Telegram group call
  with localized `error_no_call` before queue admission. A per-chat first-play
  transaction rechecks live VC state under a lifecycle lock and exposes queued
  only after the first request commits in `db.active_calls`; cancellation, late
  no-VC startup, and unexpected failure roll back the exact immutable request
  ID. `stop()` always performs logical cleanup and only `video_chat_ended`
  triggers VC cleanup. In the playback path, valid local/catalog alias hits
  resolve before provider work; one media-scoped yt-dlp owner is shared by
  direct start, foreground warmup, prefetch, and CDN publication. Complete local
  files are durable catalog assets, whereas partial output is never promoted.
  Status-card cancellation detaches its observer without canceling shared work;
  a one-shot failure is terminal only for that request scope. Per-video auth
  circuits no longer disappear merely because the global circuit is idle.
  Validation: `python -m compileall -q AnonX_3` PASS; targeted cache/auth/VC
  regressions PASS; `python -B tests/run_unit_smoke.py` **61/61 PASS**.
  Manual Telegram VC exercise and process restart remain deployment checks.
  Confidence: 99%, tier: A.

- 31-Jul-2026: **Private AI degraded-mode deep fix**: A rejected DeepSeek
  credential previously made every private message return the same generic
  apology. The private assistant now preserves its auth circuit and uses a
  high-confidence local intent layer for greetings/help, music search/download,
  cached-result download, owner/status, active-music, and top-song requests,
  delegating actions to existing verified tools. Unknown Myanmar input receives
  an honest clarification instead of fabricated data. Validation: compile PASS,
  focused assistant/hardening PASS, and `python tests/run_unit_smoke.py`
  **64/64 PASS**. A valid `DEEPSEEK_API_KEY` and process restart remain required
  for open-ended AI conversation. Confidence: 98%, tier: A.

- 31-Jul-2026: **Private assistant exception and query-normalization hardening**:
  Unexpected AI-agent and degraded-tool exceptions are now contained at the
  private-message boundary while cancellation remains re-raisable. The local
  fallback parser removes common Burmese/English request scaffolding such as
  `သီချင်းနာမည်`, `a song`, and `named` before calling verified music tools.
  Existing invalid-key degraded behavior remains intact. Validation: compile,
  import, FFmpeg/dependency, provider-search preflight, and full smoke **66/66**
  PASS. Confidence: 98%, tier: A.

- 31-Jul-2026: **Observed Burmese informal-spelling intent fix**: Added bounded
  fallback recognition for the production-shaped status phrase
  `ဘာတေဖစ်နေကျတာလဲဗျ` and typoed music forms `သိချင်း`/`သချင်း`.
  The exact variants are stripped from search queries, while standard greetings
  and unknown Myanmar messages retain their existing behavior. Direct observed
  case assertions and full smoke **66/66** pass. Confidence: 98%, tier: A.

- 31-Jul-2026: **Playback error truthfulness and startup lifecycle deep fix**:
  `/play` no longer maps resolver timeouts or provider transport/circuit failures
  to `play_not_found`. `PLAY_RESOLVE_TIMEOUT_SEC` defaults to 18 seconds,
  fallback metadata is preserved, and retryable failures receive localized
  messages; genuine no-match behavior is unchanged. Startup now wraps the
  complete one-run sequence in an unconditional `stop()` boundary, and
  explicit `SystemExit` configuration failures exit for an external supervisor
  instead of repeating forever. Validation: compile PASS, locale JSON PASS,
  targeted resolver/lifecycle regressions PASS, and full smoke **66/66 PASS**.
  No real bot process was started; VPS restart and live Telegram/provider checks
  remain deployment steps. Confidence: 98%, tier: A.

- 28-Jul-2026: **Complete `AnonX_3` identity migration**: Renamed the deploy root and Python package from the previous numbered identity, then migrated active imports, entrypoints, test environment symbols, Docker/Compose users and mounts, Mongo/default paths, operations scripts, release metadata, and durable documentation to `AnonX_3`. The digit-safe rewrite preserves unrelated numbered identities and the generic family regexes used by structure validation and legacy environment migration. Forced compile/import checks, full smoke, recursion regression, Compose parsing, release metadata, structure, secret, stale-reference, and filesystem checks pass; `dist/` remains absent. Restart under the new root with `python3 -m AnonX_3`. Confidence: 99%, tier: A.
- 28-Jul-2026: **Assistant startup race deep fix**: A `/play` update could reach the already-started bot while userbot/PyTgCalls startup was still in progress. Mongo assistant selection raised `SystemExit` during that temporary empty-client window, terminating the entire process with `No assistant call clients are available.` Userbot and PyTgCalls now expose bounded readiness barriers; assistant selectors wait for startup and use recoverable `RuntimeError` failures instead of process-exiting exceptions. Real missing/invalid session configuration still fails closed in the main startup path. Validation: compile OK; executable startup-race regression and full smoke 50/50 pass; structure and secret scans pass; `dist/` remains absent. VPS restart required. Confidence: 98%, tier: A.
- 28-Jul-2026: **YouTube authenticated-client and dead-upload deep fix**: Removed the stale forced `android,web` yt-dlp client override from direct audio/video extraction and local audio downloads so yt-dlp 2026.07.04 can select its maintained defaults. Authenticated cookie recovery now strips a forced player client unless a client-bound PO token is present, preserving `mweb` provider compatibility. This prevents account cookies from being retried through clients that do not support them and allows a genuinely unavailable selected upload to be classified normally so the existing alternate-YouTube-upload path runs before SoundCloud. The exact reported ID `6X0xsWZ40FU` is independently unavailable while a public control video extracts successfully. Validation: compile OK; smoke 49/49; recursion regression, structure, and secret scan OK; `dist/` remains absent. Local environment only: stale non-project `motor 3.6.0` conflicts with installed PyMongo, and FastAPI is declared but not installed, so `pip check` and the optional Downloader API import are not clean on this workstation. Restart required. Confidence: 97%, tier: A.
- 28-Jul-2026: **Bounded inline search restored as direct `/vsong` handoff**: added auto-loaded `plugins/inline_search.py` so `@BotUsername song name` returns up to eight YouTube results. In the bot's private chat or a group where it is present, selection sends a bot-addressed `/vsong@BotUsername <canonical URL>` message and the existing video-download pipeline processes it. Keystroke debounce, unique-query stale suppression, six-search concurrency, a seven-second timeout, result deduplication, blacklist enforcement, and personal 30-second result caching bound load and failure impact. The old Premium-emoji/token/template transport subsystem remains removed. Validation: compile OK; smoke 49/49. Operations: enable `@BotFather` `/setinline` once and restart. Confidence: 97%, tier: A.
- 28-Jul-2026: **Asyncio RecursionError deep fix (990+ task cancellation chain)**: Added `asyncio.shield()` around singleflight factory execution and explicit `CancelledError` boundaries in prefetch `_runner()` to break deep task cancellation chains. Root cause: cancelling a top-level task recursively propagated through 990+ nested children (prefetch → CDN → singleflight → download → retry → ...), exceeding Python's 1000 recursion limit. Shield prevents cancellation from propagating while allowing graceful cleanup; waiter shields (lines 65/74/102) remain unchanged. Regression test verifies 100-level chain cancellation completes without RecursionError. Validation: compile OK; targeted recursion test passed; structure OK. Restart required. Confidence: 96%, tier: A.
- 27-Jul-2026: **v3.2.0 final-stable closure**: preserved the executable py-yt-search proxy-first/`TypeError`/proxy-free retry behavior while replacing its obsolete signature-inspection source guard; corrected the distributable variant identity and local Mongo database in `sample.env`. All four synchronized variants pass 47/47 smoke, full compile, structure, secret scan, eight-file Compose YAML lint, deterministic release build, and manifest verification. Runtime credentials, cookies, browser profiles, and `.env` remain excluded. VPS deploy/restart and one legitimate dedicated-profile YouTube sign-in remain operational steps. Confidence: 99%, tier: A.
- 25-Jul-2026: **v3.2.0 final performance/stability release**: `/play` now acknowledges before database/admin work, caches the common Mongo play-mode default, and avoids a duplicate Telegram keyboard edit. YouTube deep search has bounded TTL caching and singleflight; normal-search misses have a short negative cache; API calls reuse one HTTP session; losing provider-race tasks are cancelled and awaited. Direct metadata waiting is capped at 0.35s, complete yt-dlp artifacts skip fixed settle polling, and FFmpeg/FFprobe work is offloaded from the asyncio event loop. Graceful shutdown closes YouTube HTTP resources. Validation: compile OK; smoke 41/41; deterministic cached deep-search benchmark 4430.7x; structure OK; secret scan OK. Final release builder and manifest verifier added; inline-search remains intentionally removed. Confidence: 97%, tier: A.
- 25-Jul-2026: **Telegram inline-search subsystem removed**: Operator-requested scoped removal deleted the auto-loaded `plugins/iquery.py` handler, `core/inline_tokens.py`, Bot API answer method, inline query configuration, signed `/start iq_*` bridge, inline-only caption/button customization keys and diagnostics, dedicated Mongo refresh helper, switch-inline conversion branches, metrics/test guards, and active architecture references. Ordinary bot-message inline keyboards remain because controls/settings/moderation use them; `/play`, `/song`, `/vsong`, queues, playlists, AI DJ, and `yt_copy`/`yt_open` are preserved. Legacy Mongo overrides are ignored rather than destructively purged. Compile OK; smoke 40/40; structure OK; targeted source/config/test scan found zero inline-search subsystem references. Confidence: 98%, tier: A.
- 24-Jul-2026: **Strict inline Premium-emoji exact-ID pass-through**: `CUSTOM_EMOJI_FORCE_BOT_API=True` now has an explicit `forced_exact_ids` policy for user-sent inline results. It disables the previous local Unicode downgrade and sends the exact persisted `inline_result` custom-emoji entities plus all effective `inline_play`, `inline_song`, `inline_vsong`, `inline_copy`/`yt_copy`, and `inline_youtube`/`yt_open` icon IDs through the MTProto `messages.SetInlineBotResults` request. Turning the force flag off retains the entitlement-aware Unicode fallback. `/setbt` and `/settext inline_result` status now distinguish `forced_exact_ids`, `entitled_exact_ids`, and `unicode_fallback`. Raw serialization regression proves one caption document ID and five distinct button icon IDs survive as 64-bit integers while actions remain unchanged. Compile OK; smoke 40/40; structure OK. Telegram remains the final renderer: actual animation in a user-sent `via @BotUsername` result still requires an active Fragment collectible additional username, and a fresh result must be sent after restart. Confidence: 98%, tier: A.
- 24-Jul-2026: **Entitlement-aware inline Premium-emoji deep fix**: The recorded result is a user-sent `via @BaNaNanMusic_Bot` inline card, so Telegram's owner-Premium direct-send exception cannot authorize its custom emoji. Inline queries now detect an active collectible Fragment username from the bot identity, keep custom caption entities/button icons only when eligible, and otherwise preserve visible Unicode emoji instead of producing blank icons. Button fallback removes only the selected entity's exact UTF-16 span; unrelated flags/headphones remain. Effective `yt_copy → inline_copy` and `yt_open → inline_youtube` overrides now participate in customization state/revision, button overrides are read on every query, and `/setbt`/`/settext inline_result` report the detected mode. All seven requested keys retain their original URL/copy/play actions. Compile OK; entitlement/action/fallback regressions + smoke 40/40; structure OK. Restart and a newly sent inline result required; animated inline emoji still requires Telegram's active Fragment entitlement. Confidence: 98%, tier: A.
- 24-Jul-2026: **Inline Premium-emoji final diagnosis + fresh customization delivery**: End-to-end Kurigram 2.2.19 serialization proves both `MessageEntityCustomEmoji.document_id` and `KeyboardButtonStyle.icon` reach a wire-ready `messages.SetInlineBotResults` request as 64-bit integers. The remaining preview-versus-inline difference is Telegram entitlement: owner Premium covers messages directly sent by the bot, while a user-sent inline result requires an active collectible additional bot username purchased on Fragment. New inline answers use customization-revision result IDs, `cache_time=0`, fresh Mongo caption reads, and a two-second button-style refresh so another worker or Telegram cache cannot serve an old `/settext` or `/setbt` value. Admin guidance now states the correct Fragment requirement; Kurigram is pinned to `>=2.2.19`. Compile OK; raw combined caption/button wire regression + smoke 40/40; structure OK. Restart and a newly sent inline result required; live Premium rendering additionally requires the Fragment entitlement. Confidence: 98%, tier: A.
- 24-Jul-2026: **Inline Premium-caption UTF-16 + MTProto entity fix**: `/settext inline_result` previews could normalize escaped `{{0}}` placeholders correctly while the sent inline card showed stray `}`, `{3}`, or `{4}` fragments because brace collapsing shifted Telegram custom-emoji entities with Python code-point offsets instead of Telegram UTF-16 offsets. Dict templates now recognize canonical, escaped, and legacy-extra-close placeholders in one entity-aware substitution pass. At the Pyrogram transport boundary, persisted digit-string custom emoji IDs are converted to MTProto 64-bit integers. Regression coverage verifies exact caption text, all five relocated Premium-emoji offsets, and integer transport IDs. Compile OK; smoke 40/40; structure OK. Restart and a newly sent inline result required. Confidence: 98%, tier: A.
- 24-Jul-2026: **Inline Premium-button MTProto transport fix**: Video inspection showed `/setbt <key>` was only being used in preview mode, and the actual inline search still travelled through HTTP Bot API. Inline results now use Pyrogram MTProto `query.answer`; button dictionaries are converted without dropping `icon_custom_emoji_id`, digit IDs become raw 64-bit integers, styles become `ButtonStyle` enums, and `KeyboardButtonStyle.icon` reaches the final wire object. Caption entities and Copy/URL actions remain intact, with compatibility downgrade limited to old Pyrogram presentation fields. Telegram still applies server-side entitlement: inline-result rendering requires a Fragment collectible additional username. Compile OK; executable raw-icon regression + smoke 40/40; structure OK. Restart and a freshly sent inline result required. Confidence: 97%, tier: A.
- 24-Jul-2026: **Inline `/setbt` compatibility + Premium-icon diagnosis**: `/setbt yt_copy` and `/setbt yt_open` now act as backward-compatible fallbacks for inline-result keys `inline_copy` and `inline_youtube`; explicit `inline_*` overrides still win. The admin response now explains that existing inline cards are immutable and that inline-mode custom-emoji button icons require a Fragment-purchased additional bot username under Telegram's Bot API restriction. Compile OK; smoke 40/40; structure OK. Restart and send a fresh inline result required. Confidence: 97%, tier: A.
- 24-Jul-2026: **YouTube explicit auth-challenge retry-storm deep fix**: The VPS log's `Sign in to confirm you're not a bot` response is now classified separately from an ordinary retryable HTTP 403/PO failure. A blocked request performs at most one materially different recovery when a fresh configured PO token or refreshed local cookie is actually available; otherwise a short global circuit stops equivalent direct/local, audio/video, and quality-tier retries while existing READY cache remains usable and source fallback remains eligible. Added configurable `YOUTUBE_AUTH_CHALLENGE_COOLDOWN_SEC` (default 180s), metrics/logging, and regression guards across both prefetch loops and final playback retries. Compile OK; smoke 36/36; structure and secret scans clean. Restart required. Confidence: 97%, tier: A.
- 24-Jul-2026: **End-to-end live progress delivery deep fix**: The second recording proved the custom Downloading card still stayed at `0.0%` even after the Track-context race fix. The remaining causes were the post-direct-failure audio path dropping the foreground message context, READY/cache exits never terminal-rendering, and untracked concurrent Telegram edit tasks allowing hook updates to replace/cancel one another. All local fallback paths now inherit the same progress card/lang/media, READY paths render a truthful file-size-backed `100%`, and one serialized coalescing worker consumes real yt-dlp byte hooks without fake increments or fixed progress delays. Deterministic threaded regression observes `10% → 50% → 100%`, including fallback inheritance and cache completion. Compile OK; smoke 35/35; restart required. Confidence: 98%, tier: A.
- 24-Jul-2026: **YouTube live-progress race deep fix**: Frame inspection confirmed the custom Downloading card rendered but stayed at `0.0%` until playback. Warm search and canonical resolve can produce distinct `Track` objects for one ID; the resolver could win local-cache ownership without the status context, then the warm request deduplicated against that unobserved worker. The canonical `/play` track now receives progress context before cache dispatch, and same-media cache deduplication merges late context into the worker-owned object plus attaches to the real yt-dlp task. Progress remains real-byte event-driven—no fake increments, polling, or delay. Compile OK; smoke 34/34; structure and secret scans clean; sanitized deploy archive refreshed. Restart required. Confidence: 98%, tier: A.
- 24-Jul-2026: **Minimal live download progress UI**: Simplified the generated block beneath custom `play_downloading` text to only the 12-cell real byte-driven bar and percentage. Removed the download icon, transferred/total size, `Unknown`, speed, and ETA from both initial and live edits while retaining Cancel, custom/Premium entities, provider hooks, and one-message updates. Confidence: 98%, tier: A.
- 24-Jul-2026: **Immediate custom download progress bar fix**: Video inspection confirmed the idle first-play status could remain on the customized `play_downloading` header for the entire observed 11-second interval because the bar was only created after a provider emitted usable byte totals. Every foreground Downloading transition now renders the custom header plus a real 0% 12-cell progress block immediately; existing byte hooks then update that same card with live percentage, size, speed, and ETA. The composed UI is never persisted back into `/settext`, so `play_downloading` remains header-only. Compile OK; smoke 34/34. Restart required. Confidence: 98%, tier: A.
- 24-Jul-2026: **Customizable Premium-emoji inline result cards**: Added `/settext inline_result` with six entity-aware placeholders and `/setbt` keys `inline_play`, `inline_song`, `inline_vsong`, `inline_copy`, and `inline_youtube`. Inline captions now emit `caption_entities` so eligible bots preserve Premium emoji; Telegram requires a Fragment collectible additional username for user-sent inline results. The main signed group link reuses canonical play-or-queue behavior; private signed actions reuse `/song` and `/vsong`. Compile OK; smoke 34/34. Restart required. Confidence: 97%, tier: A.
- 24-Jul-2026: **Custom live provider download progress**: Foreground YouTube, TikTok, Facebook, and Telegram/MTProto downloads now keep the per-chat `play_downloading` custom text and append a live 12-cell progress bar with percentage, transferred/total bytes, speed, and ETA. Updates reuse the same Cancel-enabled card, are capped at one edit per two seconds, remain silent for queued prefetch, and cannot be overwritten by a late metadata callback after byte progress starts. Compile OK; executable smoke 34/34. Restart required. Confidence: 97%, tier: A.
- 24-Jul-2026: **Telegram 3GB `getFile` limit deep fix**: Added file-size-aware routing so known media above 18MiB bypasses official Bot API direct URLs and uses the parallel assistant MTProto download; unknown-size `file is too big` responses now switch routes without terminal/error logging. Added explicit media-size metadata, documented `DOWNLOAD_LIMIT_GB=3`, and executable 3GiB/unknown-size fallback tests. Compile OK; smoke 34/34; structure OK; secret scan OK. Restart required; live 3GB time depends on VPS disk/network. Confidence: 97%, tier: A.
- 24-Jul-2026: **Telegram/TikTok/Facebook YouTube-style parallel playback deep fix**: Fixed the live Telegram `_duration_label` crash; preserved Telegram message/story context for assistant-based local fallback; warmed all external metadata alongside assistant readiness; overlapped direct streaming with verified local downloads; added media-ID singleflight for Telegram/Facebook; changed external audio artifacts to M4A; and required `ffprobe` audio/video capability before `/play`/`/vplay`. Warm-up failure now retries through the normal resolver instead of producing a generic terminal error. Compile OK; smoke 34/34; structure OK; secret scan OK. Restart required; live provider URLs remain runtime validation. Confidence: 97%, tier: A.
- 24-Jul-2026: **TikTok concurrent `.part` rename race deep fix**: Reproduced the log path where cancellation ended the asyncio wrapper but its yt-dlp thread kept writing; a new request then purged the active `.part`. Added media-ID-wide process-local singleflight across audio/video/CDN, moved cleanup inside the owner, shielded worker ownership from waiter cancellation, and protected shared CDN/singleflight tasks. Added an executable cancellation-survivor regression proving one underlying download. Compile OK; smoke 33/33. Restart required. Confidence: 97%, tier: A.
- 24-Jul-2026: **TikTok `NoAudioSourceFound` deep fix**: TikTok audio now selects audio-bearing formats, converts to M4A/AAC, refuses partial/stale/video-only cache artifacts, validates required streams with `ffprobe`, purges only the current media ID's invalid outputs, and retries through a bounded format ladder. Updated playback metadata to use `.m4a`. Compile OK; smoke 33/33; real generated M4A probe OK. Restart required. Confidence: 96%, tier: A.
- 24-Jul-2026: **Unified Cancel + Telegram/Facebook playback recovery**: Cancel now silently acknowledges, auto-deletes the transient card, and cancels every message-bound request branch without a duplicate “already cancelled” toast. Fixed Telegram cancel return semantics, non-YouTube prefetch routing, Telegram audio/video capability labeling, Facebook audio stream flags, `NoVideoSourceFound` handling, and generic terminal error replacement. Compile/JSON OK; smoke 32/32; structure OK; secret scan OK. Restart required. Confidence: 96%, tier: A.
- 24-Jul-2026: **YouTube HTTP 403 / PO-token recovery deep fix**: Migrated PO injection to current `CLIENT.CONTEXT+TOKEN` syntax, defaulted to `mweb`, isolated cache entries per video, invalidated failed video-bound tokens, aligned legacy provider prefixes with their player client, purged only incomplete artifacts, and added bounded cookie-free client rotation (`default/web_safari`, then `tv/web_embedded`) instead of repeating poisoned media URLs. Added regression coverage for token binding, normalization, invalidation, and recovery guards. Compile OK; smoke 31/31; structure OK; secret scan OK. Restart required. Confidence: 96%, tier: A.
- 23-Jul-2026: **Event-driven `/play` startup**: `Searching` is sent first; search completion immediately edits to `Downloading` and launches local fallback while direct extraction and assistant preparation remain parallel. Direct URL/local-download task completion—not fixed sleeps—dispatches playback; `client.play()` completion marks voice ready. Removed startup gate/join-second settings and polling waits. Compile OK; smoke 18/18; secret scan OK. Restart required. Confidence: 96%, tier: A.
- 23-Jul-2026: **Sudo command startup reliability fix**: Loaded persisted owners/sudoers/blacklist and registered all plugins before `app.boot()`, closing the post-“Bot/Assistant Started” window where `/restart` and `/logs` could be silently dispatched without handlers. `app.sudoers` now refreshes MongoDB on an in-memory miss. `/restart` has replay/in-progress guards; `/logs` returns a safe error for all send failures. Compile OK; smoke 16/16. Restart required. Confidence: 96%, tier: A.
- 23-Jul-2026: **Deep fix (fast /play)**: Synced from AnonX_10 — hybrid direct, CDN cold skip, local race, join 12s, m4a audio, env DIRECT=True. Smoke 15/15. Restart required. Confidence: 92%, tier: B.
- 23-Jul-2026: **Deep Scan (D5)**: Workspace `AnonX_3`+`AnonX_3`+`AnonX_10`. This variant: compileall OK, STRUCTURE OK, smoke 15/15, secret_scan OK. Package tree 183 py path-identical. Mongo `AnonX_3`. Comment-only “AnonX_3” residuals in 3 core files. Failover window env 15s. No git. Scan: `../.kimi-codex/scan/`. Confidence: 96%, tier: A.
- 21-Jul-2026: **Spawn AnonX_10**: Fresh copy with package rename; `core/cache` included. Entry `python -m AnonX_10`. Confidence: 96%, tier: A.
- 21-Jul-2026: **Spawn variants**: Fresh copies `AnonX_3` + `AnonX_3` created from this AnonX_3 tree (package rename, Mongo isolation, STRUCTURE OK, compileall OK). Each needs unique BOT_TOKEN/SESSION in `.env`. Confidence: 95%, tier: A.
- 21-Jul-2026: **Auto-learn / auto-reply anti-spam**: Silent learn (no 📚 chat). Learn limits: 3s/user, 12s/same keyword, max 4/min/user. Reply fire limits: 8s/chat, 20s/keyword, 12s/user; skip reply-threads (conversation). Duplicate answer text still not stored. File: `plugins/auto_reply.py`. Restart required. Confidence: 94%, tier: A.
- 21-Jul-2026: **D3 deep fix — parallel direct+local (audio+video)**: (1) Video no longer starts a *second* yt-dlp job in `await_current_cache` — joins the parallel task (id match). (2) Parallel kick uses same video quality tier as profile. (3) `DIRECT_URL_PROBE=off` default + gate 2.5s for immediate VC try. (4) `_pick_ready_local` / `_await_parallel_local` / disk poll so fallback uses already-warming file. (5) StreamEnded leave-VC fix retained. Files: `calls.py`, `prefetch.py`, `playback_orchestrator.py`, `config.py`. Smoke 15/15. Restart required. Confidence: 91%, tier: B.
- 21-Jul-2026: **Parallel direct+local start (audio+video)**: Soft probe default (`DIRECT_URL_PROBE=soft`) so googlevideo 403 no longer cancels VC direct while `start_current_cache(force,immediate,local_only)` races into `downloads/`. Screenshot log (`get:403` → 11s local wait) was the waste. Same path for `/play` and `/vplay` (`video=0|1`). Smoke 14/14. Restart required. Confidence: 92%, tier: B.
- 21-Jul-2026: **StreamEnded stuck-in-VC fix (AnonX_3 restore)**: After song end assistant stayed in VC and next `/play` did nothing. Root cause: local/CDN path called `startup_gate.begin()` for refcount but `in_gate_window()` treated non-`direct_attempted` sessions as forever-in-gate, so StreamEnded only signalled fatal and never `play_next`→`stop`→`leave_call`. Also midstream failover treated natural short-track completion as early death. Fix: gate window only during active direct confirm; failover skips natural end (≥85% duration / local path); StreamEnded again advances/leaves like AnonX_3. Files: `playback_orchestrator.py`, `stream_watch.py`, `calls.py`, smoke tests. Validation: smoke 13/13. Restart required. Confidence: 93%, tier: B.
- 21-Jul-2026: **Deep Scan (D3) multi-project**: Workspace = `AnonX_3` (primary) + sibling `AnonX_3` (baseline). AnonX_3: compileall OK, STRUCTURE OK, smoke 12/12, secret_scan OK, no local `.git`. AnonX_3: compileall + STRUCTURE OK, no smoke/secret_scan. AnonX_3 ahead by cache/downloader/resolver/orchestrator/security/metrics (~25 extra py modules). Doc stubs still reference missing `AnonX_3/`. Recommend AnonX_3 as active target. Confidence: 94%, tier: A static + unit smoke.
- 21-Jul-2026: **Hardening A–F**: Process-aware startup gate (fatal Event + StreamEnded in window); `DirectWatchdog` early direct→local failover with seek; Branch B CDN READY race after direct; YouTube client/PO ladder; Redis singleflight Lua unlock + heartbeat + result publish; `ops/secret_scan.py`; docs `hardening-gate-failover-redis.md`. Tests 12/12. Restart required. Confidence: 88%, tier: B.
- 21-Jul-2026: **prompt.txt 100% close-out (achievable DoD)**: URL probe before direct play; cache refcount release on stop/play_next; `QualityPlan` unified load algorithm; duration cap via `MAX_MEDIA_DURATION_SEC`; youtube download SSRF check; `docker-compose.yml` profiles (redis/nginx/po); Dockerfile optional Deno; `docs/prompt-100-compliance.md` + smoke checklist; unit smoke expanded (race/quality/security). Validation: compileall + smoke. Restart required. Confidence: 90%, tier: B.
- 21-Jul-2026: **Phase D (P3) ops**: Metrics registry + optional health HTTP (`/health` `/metrics` `/ready`); Redis singleflight optional; `nginx/media.conf` + `docker-compose.example.yml`; `security.py` (SSRF/path/redact); publisher path jail; unit smoke `tests/run_unit_smoke.py` (6/6); docs `docs/playback-architecture.md` + ARCHITECTURE.md. Config: `HEALTH_*`, `REDIS_*`. Validation: compileall + STRUCTURE OK + 6 unit tests. Restart required. Confidence: 93%, tier: B.
- 21-Jul-2026: **Phase C (P2) fallback + PO**: SoundCloud resolver + matcher (title 45% / artist 30% / duration 20% / version 5%); `find_fallback_track` on YouTube miss and download death; SC play path (direct→download); optional PO Token Provider client (`PO_TOKEN_PROVIDER_*=off` default) injects yt-dlp extractor_args; `provider/README.md`. Config: `FALLBACK_*`, `PO_TOKEN_*`. Validation: compileall + STRUCTURE OK + matcher/SC unit smoke. Restart required. Confidence: 92%, tier: B.
- 21-Jul-2026: **Phase B (P1) resilience**: Error classifier + bounded exp-backoff retries (`core/resolver/*`); `resource_manager` env limits + load band; `resource_budget` uses live load; YouTube direct/download use RM semaphores, READY skip re-extract/re-download, classified retries (403/429/5xx/permanent); CDN GC popularity TTL + orphan `.part` + disk high-water LRU; TikTok shares download semaphore. Config: `MAX_*`, `YTDLP_MAX_RETRIES`, `DISK_*`, `CDN_*_TTL`. Validation: compileall + STRUCTURE OK + classifier/RM/store smoke. Restart required. Confidence: 93%, tier: B.
- 21-Jul-2026: **Phase A (P0) playback architecture from prompt.txt**: Canonical cache keys `source:youtube:id:audio:best`; cache state machine (MISS→RESOLVING→DOWNLOADING→READY/FAILED_*); `core/cache/*` hub with verify-on-HIT + refcount; process-local `downloader/singleflight`; extended SQLite CDN store metadata; `playback_orchestrator` race matrix + startup success gate wired into YouTube direct path in `calls.py`; CDN manager uses singleflight + validation; GC skips active refcounts. Config: `PLAY_STARTUP_GATE_SEC`, `SINGLEFLIGHT_BACKEND`. Validation: compileall + verify_structure STRUCTURE OK + unit smoke (keys/states/race/store). Restart required. Confidence: 94%, tier: B.
- 20-Jul-2026: **Auto-learn fixed in watcher group=29**: Learn+reply same handler; plain reply teaches everyone; `/reply Answer` while replying to keyword (no separate keyword inject); confirm 📚; EN/MY/punct. Validation: compileall + match tests OK. Confidence: 95%, tier: A.
- 20-Jul-2026: **Auto-learn always ON, no /autolearn command**: Removed toggle command; everyone (admin+normal) teaches by reply; handler group=25; no DB gate; confirm via utils.reply_text. Validation: compileall OK. Confidence: 96%, tier: A.
- 20-Jul-2026: **Auto-learn open to all members**: Teach-by-reply no longer admin-only — sudo/owner/admin/**normal users** can learn; 2s per-user cooldown; `/autolearn` still admin toggle. Validation: compileall OK. Confidence: 97%, tier: A.
- 20-Jul-2026: **Auto-learn by reply**: Admin replies to a short keyword message (e.g. နယူး) with answer → bot learns group rule + multi-variants (Premium emoji); `/autolearn on|off` (default ON); `/replies` shows variants + learn status; `append_auto_reply_variant` in mongo. Validation: compileall OK. Confidence: 95%, tier: A.
- 20-Jul-2026: **/unreply deletes learned message fully**: Clears group-local + global (sudo) scopes so keyword stops auto-replying; NFC key match; hint when only global exists. Fixes screenshot “No auto-reply found” while replies still fired. Validation: compileall + key resolve tests OK. Confidence: 96%, tier: A.
- 20-Jul-2026: **Auto-reply Premium emoji + dump fix**: `_normalize_rule` recovers multi-variant `str(list)` dumps (no more raw entities dump in chat); preserves Telegram Premium `custom_emoji` entities on store/send; blocks re-saving dump messages; `_send_auto_reply` uses entity path. Validation: compileall + dump/premium unit tests OK. Confidence: 96%, tier: A.
- 20-Jul-2026: **Auto-reply upgraded** (group-scoped): word/phrase match (not substring), longest-one reply only, `hi?`/`Hi` work, group rules preferred over global, `/reply` auto-enables auto-reply for scope, `/replies` shows previews+status, `/unreply keyword`, bot commands registered, en/my usage strings updated. Validation: compileall + match unit tests OK. Confidence: 95%, tier: A.
- 20-Jul-2026: **True parallel local + auto-play on direct fail**. Log showed `No audio source found` on googlevideo then local `downloads/*.webm` ready. Now: kick `local_only` yt-dlp into `downloads/` *before* assistant/direct resolve; on direct fail immediately use ready file or await parallel task then auto-play; audio direct format prefers m4a to reduce ntgcalls "No audio source". Validation: `compileall` OK. Restart required. Confidence: 95%, tier: A.
- 20-Jul-2026: **Hybrid direct→local hardened** for `/play` + `/vplay` YouTube path. Early parallel local cache (`force=True`, `immediate=True`) runs with remote start; on resolve/play failure silently awaits local path before CDN/download; `prefetch.start_current_cache` stores filesystem `local_path` for VC fallback. Config: `YOUTUBE_DIRECT_STREAM=True`, `ONLY=False`, `CACHE_BG=True`. Validation: `compileall` OK. Restart bot to load. Confidence: 96%, tier: A.
- 20-Jul-2026: **YOUTUBE_DIRECT_STREAM=True** in local `.env` (was False). Hybrid fallback kept: `YOUTUBE_DIRECT_STREAM_ONLY=False`, `YOUTUBE_DIRECT_CACHE_BG=True`. TikTok direct stream already True. Restart bot to load. Confidence: 99%, tier: A.
- 20-Jul-2026: **Deep Scan (D3)**: Full audit of AnonX_3 checkout. Compileall OK; `ops/verify_structure.py` STRUCTURE OK; Python 3.13.13 available. Git detached on `origin/master` with ~152 dirty paths (staged anony→AnonX_3 rename + untracked CDN/supervisor/name_checker). No tests/CI. Secrets gitignored; local `.env` + cookies present. Doc stubs still point at missing `AnonX_3/`. Confidence: 95%, tier: A static.
- 20-Jul-2026: **Integrated CDN pipeline**: Added `AnonX_3/core/cdn/` (manager, publisher, SQLite store, TTL cleaner, optional aiohttp origin). Flow: READY hit / await inflight / yt-dlp download → `tmp/*.part` → atomic publish `ready/` → hybrid play (CDN public URL or local path) with local fallback on remote stream failure. Config keys `CDN_*` in `config.py` / `sample.env`. Boot starts GC loop + optional origin. Prefetch uses CDN when enabled. Validation: `compileall` + publish/store smoke tests passed. Confidence: 96%, tier: B.
- 20-Jul-2026: **Package rename `AnonX_3` → `AnonX_3`**: Renamed Python package directory `AnonX_3/` to `AnonX_3/`, rewrote imports/paths/process name/startup (`python -m AnonX_3`), config asset defaults, Mongo default DB name, docs, and ops scripts (~850+ text replacements across 80+ files). Restored pre-existing locale corruption in en/de/fr `play_user_invalid` back to Anonymous/Anonymer/Anonyme. Validation: workspace scan has zero remaining `AnonX_3` tokens; `python -m compileall -q AnonX_3 config.py` passed. Confidence: 98%, tier: A.
- 01-Jun-2026: **Autoplay default OFF — all 7 variants**: Removed MongoDB auto-load from `get_autoplay()` in all 7 `mongo.py` variants. Autoplay now defaults to OFF for every chat on every bot session. Users must explicitly run `/autoplay on` to enable autoplay (persists in-memory only until restart). Previously, chats with `autoplay: True` in MongoDB would auto-load as enabled on startup without any user action. Validation: `py_compile` passed for all 7 `mongo.py` files. Confidence: 99%, tier: A.
- 31-May-2026: **`/setwelcome` placeholder robustness fix in `AnonX_3`**: Fixed stray trailing `}` in `/start` welcome output (e.g., `MoeLaMin}` / `2.0}`) by hardening template parsing in `helpers/_utilities.py::format_template()` to also handle malformed named placeholders like `username}` / `botname}` (missing opening brace), and adding string-template fallback replacements in `plugins/start.py`. Validation: `python -m py_compile` passed for touched `_utilities.py` and `start.py`. Confidence: 97%, tier: A.
- 31-May-2026: **FloodWait UX change — single cooldown message + background retry in `AnonX_3`**: Updated `core/calls.py` all-assistants FloodWait path to send `error_flood_wait` once (no per-second live edits), then sleep for `wait_seconds` and retry current track in background. Keeps retry behavior while reducing message churn. Validation: `python -m py_compile AnonX_3/AnonX_3/core/calls.py` passed. Confidence: 99%, tier: A.
- 31-May-2026: **Log-driven deep fix — stale FloodWait countdown edits suppressed in `AnonX_3`**: New `log.txt` showed repeated `pyrogram.errors...MessageIdInvalid` during all-assistants `FloodWait` retry windows. Root cause: `core/calls.py` was updating the same progress message once per second while waiting, and stale/deleted messages triggered tracebacks through `helpers/_utilities.py` `edit_formatted()` → `edit_text()`. Added `ignore_stale` support to `helpers/_utilities.py` `edit_text()` / `edit_caption()` for both Pyrogram and Bot API edit paths, and enabled `ignore_stale=True` for the `error_flood_wait` countdown loop in `core/calls.py`. Expected impact: no traceback spam when the countdown message disappears mid-wait; FloodWait retry behavior remains intact. Validation: `py_compile` passed for touched `helpers/_utilities.py` and `core/calls.py`. Confidence: 97%, tier: A.
- 31-May-2026: **Deep fix — `/start` premium emoji preservation in `AnonX_3`**: `plugins/start.py` now forces `/start` rich-text messages with `custom_emoji` entities onto the Pyrogram send path by converting the start keyboard to Pyrogram markup before send. Root cause: styled Bot API keyboards kept `/start` on the Bot API path, and any caption/text `ENTITY_TEXT_INVALID` retry would strip premium emoji entities. Expected impact: premium/custom emoji in `/start` text and photo captions stay visible instead of silently degrading. Validation: `py_compile` passed for touched `plugins/start.py`. Confidence: 95%, tier: A.
- 31-May-2026: **Log-driven deep fix — file descriptor pressure hardening in `AnonX_3`**: Analyzed fresh `log.txt` burst showing repeated `OSError: [Errno 24] Too many open files` and `ntgcalls.ShellError: create_pipe: Too many open files` during concurrent `/play` startup. Added a shared VC startup semaphore + retry guard in `AnonX_3/core/calls.py` to serialize `client.play()` bursts, reduced `AnonX_3/core/youtube.py` concurrent yt-dlp/ffmpeg worker limit to `2`, and added the same bounded download semaphore to `AnonX_3/core/tiktok.py`. Expected impact: fewer ffmpeg pipe failures and less cascading Bot API/socket starvation during peak multi-chat startup load. Validation: `py_compile` passed for touched `core/calls.py`, `core/youtube.py`, and `core/tiktok.py`. Confidence: 94%, tier: A.
- 31-May-2026: **Moderation inline action polish — `ban`/`mute` now expose localized `Unban`/`Unmute` buttons in `AnonX_3`**: Synced moderation result messages to use locale-driven inline buttons instead of hardcoded text, and callback handlers now use localized completion messages, clear the inline keyboard after click, and support a new `unban_done` locale string. Validation: locale JSON parse passed and `python -m py_compile` equivalent passed for `AnonX_3/plugins/moderation.py` and `AnonX_3/plugins/callbacks.py`. Confidence: 99%, tier: A.
- 31-May-2026: **VPS startup crash fix — `ntgcalls` exception rename in `AnonX_3`**: `AnonX_3/core/calls.py` imported/caught `SignalError`, but `ntgcalls==2.1.0` exports `SignalingError`. Updated both import and exception handler to `SignalingError`, matching the VPS traceback `ImportError: cannot import name 'SignalError' ... Did you mean: 'SignalingError'?`. Validation: `python -m py_compile AnonX_3/core/calls.py` passed. Confidence: 99%, tier: A.
- 30-May-2026: **Syntax hotfix — `plugins/moderation.py` header restored**: Re-added missing `#` comment markers on lines 2-3, fixing `IndentationError: unexpected indent` during `python -m compileall -q AnonX_3/AnonX_3`. Workspace compile status for this variant is now green. Confidence: 99%, tier: A.
- 29-May-2026: **VPS startup crash fix (`python3 -m AnonX_3`)**: Repaired cross-variant package contamination inside `AnonX_3/` caused by global token rewrite. Restored `AnonX_3` self-import/runtime paths (e.g., `AnonX_3/__init__.py`, `AnonX_3/__main__.py`, `core/*`, `helpers/*`, `plugins/*`, `config.py`, and variant docs/scripts) from incorrect `AnonX_3` back to `AnonX_3`. Root cause of `ModuleNotFoundError: No module named 'AnonX_3'` at boot. Validation: `python -m compileall -q AnonX_3/AnonX_3` passed; scoped scan `rg "\\bAnonX\\b" AnonX_3/AnonX_3 AnonX_3/config.py` returned clean. Confidence: 99%, tier: A.
- 29-May-2026: **Auto-reply startup import fix**: `AnonX_3/plugins/auto_reply.py` now imports `utils` from `AnonX_3.helpers` instead of `AnonX_3.helpers._utilities`. Root cause from live startup traceback: `ImportError: cannot import name 'utils' from '...helpers._utilities'` because the shared utility instance is exported in `helpers/__init__.py`. Confidence: 99%, tier: B.
- 28-May-2026: Browser cookie auto-export — `youtube.py` new `_try_export_browser_cookies()` extracts cookies from installed browser → `cookies.txt`. Download retry tries fresh export before no-cookie fallback. All 5 variants in this workspace applied (confidence: 93%, tier: B).
- 28-May-2026: Log-driven deep fix #1 — YouTube API silent exception: `youtube.py` API search/playlist `except Exception` now logs actual error. All 5 variants in this workspace applied (confidence: 98%, tier: B).
- 28-May-2026: Log-driven deep fix #2 — Invite failure diagnostic logging: `_play.py` invite flow now logs warnings for banned/admin_required/invite link failures. All 5 variants in this workspace applied (confidence: 98%, tier: B).
- 28-May-2026: Log-driven deep fix #3 — Cookie expiry validation: `youtube.py` `_is_cookie_file_valid()` now skips fully-expired cookie files. All 5 variants in this workspace applied (confidence: 97%, tier: B).
- 28-May-2026: Cookie directory OSError hardening — `youtube.py` `get_cookies()` now wraps `os.listdir()` in try/except OSError, auto-creates the directory on failure, and continues without cookies instead of crashing. `dir.py` `ensure_dirs()` now creates the `cookies/` directory at startup. Root cause of "Failed to read cookies.txt: OSError" and downstream "Telegram server error" when download starts with missing cookies dir (confidence: 96%, tier: B).
- 28-May-2026: VoIP error retry — `calls.py` `play_media()` now retries `ConnectionError`/`ConnectionNotFound`/`TelegramServerError` up to 3 times (1.5s delay) for ALL source types before showing "Telegram server error". Previously only retried for telegram_remote/tiktok_remote sources; YouTube local files would fail instantly on transient Telegram VoIP errors. All 5 variants in this workspace applied (confidence: 96%, tier: B).
- 27-May-2026: Auto-create `log.txt` at startup — `__main__.py` `main()` now opens `log.txt` in append mode before `db.connect()`, ensuring the log file exists from boot even if deleted or missing. All 5 variants in this workspace applied (confidence: 98%, tier: B).
- 27-May-2026: File ID caching for `/song` `/vsong` — `mongo.py` now has `media_cache` collection with `get_cached_file()` / `set_cached_file()` methods. `song.py` checks cache before download: if a `file_id` was previously cached for the same `video_id`, it sends via `send_audio`/`send_video` directly using the cached `file_id`, skipping download+upload entirely. After first upload, `file_id` is saved to MongoDB. Cache key: `{video_id}:{audio|video}`. Repeat `/song` requests now near-instant. All 5 variants in this workspace applied (confidence: 98%, tier: B). — `_play.py` now retries `app.get_chat_member()` up to 3x with 5s sleep when FloodWait hits, before falling through to assistant rotation. This was the root cause of "Failed to invite assistant" — the bot's own API call was rate-limited, bypassing the entire invite flow. Combined with earlier `client.join_chat` FloodWait retry, both bot and assistant API calls now have retry coverage. All 5 variants in this workspace fixed (confidence: 98%, tier: B).
- 27-May-2026: FLOOD_WAIT invite retry — `_play.py` now catches `errors.FloodWait` during `client.join_chat(invite_link)`, sleeps 5s, then auto-retries the invite once before falling through to assistant rotation. Prevents "Failed to invite assistant" when invite hits rate limit. All 5 variants in this workspace fixed (confidence: 98%, tier: B).
- 27-May-2026: PEER_ID_INVALID assistant invite fix — `_play.py` now catches `errors.PeerIdInvalid` alongside `UserNotParticipant` in the `get_chat_member` handler, so the assistant invite flow triggers instead of showing "Failed to invite assistant" when the bot doesn't know the assistant's peer yet. All 5 variants in this workspace fixed (confidence: 98%, tier: B).
- 27-May-2026: MongoDB database isolation — `mongo.py` now uses `get_default_database(default="AnonX_3")` per-version so each bot writes to its own database based on MONGO_URL. Previously some versions hardcoded `self.mongo.Anon` which shared the same "Anon" database across bots. All 5 variants fixed: AnonX_3, AnonX_3, AnonX_3, AnonX_3, AnonX_3 (confidence: 99%).
- 27-May-2026: AUTO_LEAVE=False applied to all 5 workspace .env files for stable operation (confidence: 99%).
- 27-May-2026: Branding/docs cleanup — all .md files per version now use only their own bot name (မဂ်လာပါ မြန်မာ 🇲🇲, Heaven Music 🇲🇲, Minako Music 🇲🇲, SYN MUSIC 🇲🇲, Honey Music 🇲🇲) and package name. No cross-version contamination in any .md or .py file (confidence: 99%).
- 27-May-2026: v4/master feature port — TikTok support (core/tiktok.py), /song /vsong commands (plugins/song.py), /gp command (plugins/active.py), Telegram message-link support (core/telegram.py), youtube.py resolve_source/resolve_direct_stream synced from v1. Full source parity with v1 across all critical modules (confidence: 98%).
- 27-May-2026: v4/master source sync — youtube.py, prefetch.py, playback.py, _thumbnails.py, _dataclass.py, callbacks.py, queue.py, __main__.py synced from v1 with correct package names. userbot.py client name fixed (AnonXUB). config.py added TikTok/YouTube/Telegram direct stream vars (confidence: 98%).
- 27-May-2026: Bot-based log group notification — `boot_client()` now uses `app.send_message` (bot) to send "Assistant Started" to Telegram logger group, avoiding assistant FLOOD_WAIT on restart. Falls back to assistant→auto-invite if bot send fails. `app` imported in `userbot.py`. All 5 variants in this workspace applied (confidence: 98%, tier: B).
- 27-May-2026: Assistant startup logging hardened — `boot_client()` now logs to `log.txt` BEFORE attempting Telegram log group notification. Log group message failure is now non-fatal (warning only); assistant still fully starts and appears in `log.txt`. `boot()` now catches general `Exception` instead of only `SystemExit`. All 5 variants in this workspace applied (confidence: 98%, tier: B).
- 27-May-2026: FLOOD_WAIT assistant auto-rotation — `AnonX_3/core/calls.py` `play_media()` now auto-rotates to next assistant when `errors.FloodWait` hits during `client.play()` (phone.JoinGroupCall). Only shows error to user when all assistants exhausted. Tracks tried assistants per-chat via `_flood_tried` dict. All 5 variants (AnonX_3, AnonX_3, AnonX_3, AnonX_3, AnonX_3) applied (confidence: 98%, validation tier: B).
- 26-May-2026: Cookie auto-conversion hardening across all versions — `_json_cookie_to_netscape` now adds `#HttpOnly_` prefix for httpOnly cookies in Netscape format output; `_generate_cookie_txt_from_json_files` now auto-cleans extra `.json` files (keep only `cookies.json`) and logs conversion with cookie count. All 5 variants (AnonX_3, AnonX_3, AnonX_3, AnonX_3, AnonX_3) applied. Existing cookies.json → cookies.txt regenerated (13 cookies each, confidence: 98%).
- 25-May-2026: Added `AnonX_3/stream.py` standalone stream-debug tool for TikTok/YouTube links. It extracts media info via `yt-dlp`, prints candidate format data, outputs selected direct stream URL, and (when `ffprobe` is available) validates audio/video stream presence to troubleshoot `Stream failed` and auto-download fallback cases (confidence: 96%, validation tier: B).
- 24-May-2026: Bot display/brand name updated to `မဂ်လာပါ မြန်မာ 🇲🇲` across app metadata, Pyrogram client name, thumbnail defaults, sample env, docs, and source headers (confidence: 98%).
- 24-May-2026: Project/package rename to `AnonX_3` completed across active source, imports, startup commands, runtime asset paths, `.env` MongoDB database name, docs, and parent project folder (confidence: 97%).
- 24-May-2026: Telegram remote-stream fallback restored in `AnonX_3/core/calls.py` — when `source="telegram_remote"` fails to join VC (timeout/connection errors), bot now auto-waits cache/download and retries once with local file before hard-failing (confidence: 94%).
- 24-May-2026: Telegram media video detection hardened in `AnonX_3/core/telegram.py` (now checks `msg.video`, mime-type, and common video filename extensions), so more replied document-videos use stream-first path instead of immediate full download (confidence: 95%).
- 24-May-2026: `/vplay` safe-hybrid restored in `AnonX_3/core/calls.py::play_media()` — tries YouTube direct stream first, then auto-falls back to local download/cache on direct startup failure (no hard-stop on remote URL failure unless strict-only enabled) (confidence: 95%).
- 24-May-2026: `YOUTUBE_DIRECT_STREAM` switched back to `True` in local `.env` and `sample.env` to enable stream-first behavior with guarded fallback (confidence: 98%).
- 24-May-2026: Current `AnonX_3` variant aligned on stable auto+download-first streaming config to minimize `/vplay` stream-fail events (confidence: 96%).
## Recent Changes (24-May-2026)
- `/vplay` stream-fail stabilization synced to `AnonX_3` behavior: `AnonX_3/core/calls.py::play_media()` now uses download-first playback path and no longer starts YouTube direct-remote stream (`source="youtube_remote"` branch removed).
- 24-May-2026: Local `.env` updated to `YOUTUBE_DIRECT_STREAM=False` for stable download-first `/vplay` behavior (confidence: 98%).
- Removed remote-stream fallback recursion and direct-cache startup calls from `play_media()` to match `AnonX_3` runtime behavior and avoid remote URL join failures in group calls.
- `sample.env` default switched to `YOUTUBE_DIRECT_STREAM=False` (stream-only style behavior not used by default).
- Validation tier: Tier B (static verification only; Python runtime unavailable in this shell).
## Recent Changes (22-May-2026)
- Added YouTube direct-then-cache hybrid playback path: direct stream starts first, then background local cache download begins, with no mid-play auto switch.
- AnonX_3/core/youtube.py added 
esolve_direct_stream(...) using yt-dlp metadata extraction to produce a direct remote media URL plus cache target path.
- AnonX_3/core/calls.py now supports source="youtube_remote" startup, background cache trigger, and remote-failure fallback to cached/local download unless strict direct-only mode is enabled.
- AnonX_3/core/prefetch.py now tracks current-playing cache tasks (start_current_cache, wait_current_cache_or_download) while preserving existing next-track prefetch flow.
- Added new env/config toggles: YOUTUBE_DIRECT_STREAM, YOUTUBE_DIRECT_STREAM_ONLY, YOUTUBE_DIRECT_CACHE_BG, YOUTUBE_DIRECT_CACHE_TIMEOUT_SEC and synced docs in sample.env and pp.json.
- Local .env was set to hybrid defaults: direct stream on, strict-only off, background cache on.
- Validation tier: Tier B (static verification only in current shell).
## Recent Changes (22-May-2026)
- Hybrid Telegram reply-video streaming implemented: attempt direct Telegram file URL playback first, then auto-fallback to local download on playback failure.
- AnonX_3/core/telegram.py now builds Bot API file URLs via getFile, tags Media.source="telegram_remote", and stores fallback metadata (	elegram_file_id, local_path).
- AnonX_3/core/calls.py::play_media() now retries once with local downloaded file when remote Telegram playback fails.
- AnonX_3/helpers/_dataclass.py Media includes lightweight source/fallback fields for runtime routing.
- Scope intentionally limited to reply video flow; audio/voice/non-video behavior remains unchanged.
- Validation tier: Tier B (static verification only in current shell).
## Recent Changes (22-May-2026)
- Telegram/YouTube responsibility hard split implemented without intended user-facing behavior changes.
- AnonX_3/core/youtube.py now exposes resolve_source(...) for non-Telegram source routing (m3u8 URL, playlist URL, direct URL/query search) and returns (file, tracks, err) for play orchestration.
- m3u8 stream Media creation moved out of AnonX_3/core/telegram.py; telegram.py now remains Telegram-only (message media detection, Telegram download/cancel/progress, Telegram-origin Media metadata).
- AnonX_3/plugins/play.py now routes non-reply source resolution through yt.resolve_source(...) instead of mixing tg.process_m3u8, yt.playlist, yt.search, and yt.deep_search branches inline.
- Public cookie helper wrappers were added to YouTube (decode_cookie_bytes, json_cookie_to_netscape, strip_cookie_bom) and AnonX_3/plugins/cookies.py now uses these wrappers instead of private-like underscore methods.
- Validation tier: Tier B (static only). Python runtime checks could not be executed in this shell environment.
## Recent Changes (22-May-2026)
- Telegram /play and /vplay now generate YouTube-style now-playing cards from actual Telegram video content frames when local video files are present.
- AnonX_3/helpers/_thumbnails.py added ffmpeg-based frame extraction (_extract_video_thumb_frame) and now prefers extracted frame images over Telegram thumbs file IDs for video media.
- THUMB_BOT_NAME overlay remains applied on generated cards, so Telegram-file video cards now show the same top bot-name style as YouTube cards.
- AnonX_3/core/playback.py and AnonX_3/plugins/queue.py now allow card generation for Media items when video=True and a local file_path exists.
- Validation tier: Tier B (static inspection only); runtime test pending because Python executable is unavailable in current shell.
# PROJECT_STATE

## Recent Changes (19-May-2026)
- Converted `AnonX_3/cookies/cookies.json` (JSON array export) to Netscape cookie format at `AnonX_3/cookies/cookies.txt` for yt-dlp compatible cookie loading.
- Conversion mapping used: `domain`, `hostOnly`/leading-dot -> include-subdomains flag, `path`, `secure`, `expirationDate` (floored Unix epoch), `name`, `value`, plus `#HttpOnly_` prefix for HttpOnly cookies.
- Validation: generated file contains 16 cookie records (tab-delimited Netscape rows).

## Recent Changes (12-May-2026)
- Project rename `AnonX_3` -> `AnonX_3` completed (13-May-2026):
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Repo-wide exact token references were updated (`AnonX_3` -> `AnonX_3`) in source, startup scripts, config defaults, and docs.
  - Verification: exact standalone `AnonX_3` token no longer appears in source/docs search.
- Project rename `AnonX_3` -> `AnonX_3` completed (13-May-2026):
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Repo-wide exact token references were updated (`AnonX_3` -> `AnonX_3`) in source, startup scripts, config defaults, and docs.
  - Verification: exact standalone `AnonX_3` token no longer appears in source/docs search.
- Project rename `AnonX_3` -> `AnonX_3` completed (13-May-2026):
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Repo-wide exact token references were updated (`AnonX_3` -> `AnonX_3`) for imports, module entrypoints, startup commands, runtime asset paths, and docs.
  - Verification: exact standalone `AnonX_3` token no longer appears in source/docs search.
- `.env` cleanup for deprecated broadcast tuning keys:
  - Removed unused runtime keys from local `.env`: `BROADCAST_WORKERS`, `BROADCAST_MAX_RETRIES`, `BROADCAST_RATE_PER_SEC`, `BROADCAST_WORKERS_BUSY`, `BROADCAST_RATE_PER_SEC_BUSY`.
  - Verified active code no longer reads these keys (matches prior broadcast simplification state); change is config hygiene only and does not alter current runtime behavior.
- Project rename `AnonX_3` -> `AnonX_3` completed:
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Import/module entrypoint references now target `AnonX_3` (`from AnonX_3 ...`, `python -m AnonX_3`, restart `os.execl(..., "-m", "AnonX_3")`).
  - Runtime default asset/cookie/locale paths were moved to `AnonX_3/...` (for `config.py`, `core/lang.py`, `core/youtube.py`, `sample.env`, and docs/scripts).
  - Brand/client labels that depended on the package token were aligned (`AnonX_3*` -> `AnonX_3*`, `AnonX_3` -> `မဂ်လာပါ မြန်မာ 🇲🇲`) while historical identity strings like `AnonX_3*` were intentionally preserved.
- Auto video-quality + auto-FPS alignment:
  - Runtime `.env` now uses `VIDEO_QUALITY=auto` with `STREAM_ADAPTIVE=True`, so video tier selection follows the same adaptive path as audio (`poor` / `normal` / `good`).
  - `AnonX_3/core/youtube.py` now applies tier-aware FPS caps in auto mode (`poor` -> 24fps, `normal`/`good` -> 30fps, still bounded by `VIDEO_MAX_FPS`) so `/vplay` quality and fps adapt together instead of using one fixed fps across all tiers.
  - `AnonX_3/core/youtube.py` auto mode now also applies tier-aware width caps (`poor` -> 640, `normal` -> 854, `good` -> 1280), bounded by `VIDEO_MAX_WIDTH`, so width/height/fps all adapt together like audio auto tiers.
- Desktop `/vplay` freeze-mitigation hardening:
  - Added `VIDEO_STRICT_AVC` (default `True`): manual-quality `/vplay` now avoids VP9/AV1 fallback and stays on AVC/H.264-oriented format selection for better Telegram mobile playback stability.
  - `AnonX_3/core/youtube.py` manual quality mode now prioritizes AVC/H.264 video (and AAC when available) before generic codec fallbacks, reducing heavy AV1/VP9 decode pressure that can stall desktop group-call rendering.
  - Manual-quality video cache files are now quality-tagged (`downloads/<video_id>.<VIDEO_QUALITY>.mp4`) so older cached manual encodes are not silently reused after quality/codec logic changes.
  - `config.py` added `PREFETCH_VIDEO` (default `False`) and `AnonX_3/core/prefetch.py` now skips video prefetch unless explicitly enabled, reducing bandwidth spikes during active video streams.
  - `sample.env` now documents `PREFETCH_VIDEO=False` with a freeze-risk note for weaker desktop/VPS links.
- Project rename `AnonX_3` -> `AnonX_3` completed:
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Repo-wide exact token references updated (`AnonX_3` -> `AnonX_3`) for imports, entrypoint commands, runtime asset paths, and docs.
  - Startup commands now target `python -m AnonX_3` (including `start` wrapper).
  - Verification: runtime/source references were updated to `AnonX_3` (remaining `AnonX_3` mentions are historical notes only).
- `/vplay` adaptive-quality cache fix:
  - `AnonX_3/core/youtube.py` now uses tier-aware video cache filenames in auto mode (`downloads/<video_id>.<tier>.mp4`) so a prior low-tier download no longer pins later playback to poor quality.
  - `youtube.download()` now scopes inflight task reuse by exact `(video, quality_tier)` instead of reusing any existing video task across tiers.
  - `AnonX_3/helpers/_play.py` no longer hardcodes `downloads/<id>.mp4` pre-check, letting `yt.download()` resolve the correct tier-specific cached file path.
## Architecture
- `AnonX_3/plugins/autoplay.py` handles `/autoplay` command parsing and toggle persistence.
- `AnonX_3/core/calls.py` executes queue-end autoplay via `db.get_autoplay()` + `yt.autoplay_track()` when queue is empty.

## Recent Changes (09-May-2026)
- FloodWait stabilization for Pyrogram send/edit paths:
  - `AnonX_3/helpers/_utilities.py` added `retry_flood_wait()` with wait-and-retry policy (`errors.FloodWait`), bounded retries, and throttled warning logs.
  - Applied flood retry wrapper across Pyrogram text/photo send/edit methods (`reply_text`, `send_message`, `edit_text`, `edit_caption`, `reply_photo`, `send_photo`).
  - Added throttled warning dedupe for invalid entity drops and entity fallback retry logs to reduce log spam bursts.
- `/play` flow hardening:
  - `AnonX_3/plugins/play.py` now guards initial `utils.reply_formatted(...)`; on send failure after retries, handler exits gracefully without dispatcher traceback.
  - `AnonX_3/helpers/_play.py` switched direct message sends/edits to utility wrappers where relevant and added guarded `anon.play_media(...)` failure handling to avoid handler-level crashes.

## Recent Changes
- Project rename `AnonX_3` -> `AnonX_3` completed (08-May-2026):
  - Python package directory renamed from `AnonX_3/` to `AnonX_3/`.
  - Import/module entrypoint references updated to `AnonX_3` (including `python -m` startup strings and `os.execl(..., "-m", "AnonX_3")` restart paths).
  - Config/runtime path defaults updated for package-relative assets (`AnonX_3/plugins/...`, `AnonX_3/locales`, `AnonX_3/cookies`).
  - Bot/userbot client names updated for runtime session naming consistency (`name="မဂ်လာပါ မြန်မာ 🇲🇲"` and `AnonX_3*`).
  - Root docs/scripts were synchronized where they referenced package path or entrypoint (`AGENTS.md`, `README.md`, `start`, `ops/*`, etc.).
  - Historical identity strings like `AnonX_3*` were intentionally preserved.
- Midnight auto-restart log reset added (08-May-2026):
  - `AnonX_3/__main__.py` now resets `log.txt` during `_daily_auto_restart()` before process re-exec.
  - Added `_reset_log_file_for_restart()` helper:
    - First tries `os.remove("log.txt")`.
    - Falls back to truncating with write mode if remove fails (e.g., file handle lock behavior on some platforms).
  - Existing cache/downloads cleanup behavior is unchanged.
  - Scope is midnight auto-restart only; manual `/restart` flow in `plugins/restart.py` remains as-is.
- Log-driven error-noise hardening (08-May-2026):
  - `AnonX_3/core/bot_api.py` now classifies `"there is no text in the message to edit"` as `BotAPI.NoTextToEdit` in both JSON and multipart request paths, preventing false `ERROR` logs when callback edits intentionally fall back from text to caption.
  - `AnonX_3/plugins/start.py` now catches `bot_api.ChatForbidden` during `/start` photo send attempts and text fallback sends, logging warning-level skip messages instead of traceback-level error spam for expected permission/member-state failures.
  - `AnonX_3/__init__.py` now adds a `pyrogram.dispatcher` log filter to suppress known upstream `PEER_ID_INVALID` parser noise records.
  - This pass targets unresolved `log.txt` errors at lines 459, 1503, 2243, 2256, and 2284 plus dispatcher `PEER_ID_INVALID` noise at line 1160.
  - Verification limited to static review in current shell (no `python` executable available to run runtime checks).
- Bundled Myanmar thumbnail font fallback (02-May-2026):
  - Added `AnonX_3/helpers/fonts/NotoSansMyanmar-Regular.ttf` and `NotoSansMyanmar-Bold.ttf` from the official Noto Fonts repository (SIL OFL 1.1) so generated thumbnails no longer depend solely on host-installed Myanmar fonts.
  - `AnonX_3/helpers/_thumbnails.py` now tries bundled Noto Sans Myanmar first for Myanmar text, then Windows Myanmar Text, then broader Linux Noto paths including `/usr/share/fonts/noto/` and variable-font filenames.
  - Generated thumbnail cache filenames now use `_v8` / `_video_thumb_v8` so screenshots do not reuse stale `_v7` cards with the blank grey label bar.
  - Screenshot symptom explained: the grey bar means the label background drew successfully, but the active runtime font did not render Myanmar glyphs.
- Generated thumbnail Myanmar label deep fix (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` moved `THUMB_BOT_NAME` drawing from the outer top-left edge into the main rounded thumbnail card at `(207, 56)`, avoiding Telegram/client edge cropping and the tiny blank pill symptom seen in screenshots.
  - Font loading now uses Pillow RAQM layout when available, keeps Windows Myanmar Text and Linux Noto/Padauk fallbacks, and uses safe text-width fallbacks when `textbbox()` returns unusable metrics for Myanmar glyphs.
  - Channel/title/duration drawing now routes through a shared `_draw_text_fit()` helper, so Burmese track titles prefer Myanmar-capable fonts instead of fixed Raleway/Inter Latin fonts.
  - Thumbnail cache filenames now use `_v7` / `_video_thumb_v7` so stale `_v6` images without the visible label are not reused.
  - `AGENTS.md` optional environment list now includes existing `THUMB_BOT_NAME`.
  - Local verification was limited because neither `python` nor `py` is installed in the current Windows shell; static review found the old likely failure path in `_draw_bot_name()`.
- Dynamic Myanmar-only thumbnail label update (02-May-2026):
  - `config.py` now defaults `THUMB_BOT_NAME` to `မဂ်လာပါ မြန်မာ 🇲🇲`, while still allowing `.env`/environment override.
  - `sample.env` documents `THUMB_BOT_NAME=မဂ်လာပါ မြန်မာ 🇲🇲`; `app.json` exposes optional `THUMB_BOT_NAME` for Heroku/container env configuration.
  - `AnonX_3/helpers/_thumbnails.py` now checks additional Noto Myanmar font paths under both `/usr/share/fonts/opentype/noto/` and `/usr/share/fonts/truetype/noto/`.
  - Generated thumbnail cache filenames now use `_v6` so prior `_v5` images with missing/old labels are not reused.
  - `Dockerfile` and Linux `setup` now install `fonts-noto-extra` in addition to Noto core/color-emoji packages to improve Myanmar glyph availability.
- Now-playing top label visibility hardening (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` now draws `THUMB_BOT_NAME` at top-left `x=50, y=36` with 24px starting size and a semi-transparent dark rounded background strip so the configured bot label remains visible over bright thumbnails.
  - Generated thumbnail cache filenames now use `_v5` so prior `_v4` images without the visible label are not reused.
  - `Dockerfile` installs `fonts-noto-core` and `fonts-noto-color-emoji` alongside `ffmpeg` so deployed containers have Myanmar and emoji font support.
  - Linux `setup` now installs the same Noto font packages for VPS deployments.
- `/vsong` video cover correction (02-May-2026):
  - `AnonX_3/plugins/song.py` now sends the full generated now-playing card from `thumb.generate(track)` as `video_cover=` for Kurigram video uploads, while still sending the Telegram-compatible 320px JPEG as `thumb=`.
  - The `/vsong` upload path falls back from `video_cover=` to `cover=` and then to `thumb=` only if the installed client build does not support the cover keyword, avoiding upload failure on client-version differences.
  - This targets Telegram video previews that ignored the custom thumbnail and showed a server-generated frame such as `1002.` instead of the generated card.
- Now-playing thumbnail fixed top-left label correction (02-May-2026):
  - `config.py` now defines optional `THUMB_BOT_NAME`, defaulting to `မဂ်လာပါ မြန်မာ 🇲🇲`.
  - `AnonX_3/helpers/_thumbnails.py` now prefers `config.THUMB_BOT_NAME` for the generated card overlay before falling back to the runtime Telegram bot profile name.
  - The overlay remains top-left at the same `x=50` visual column as the lower title, with Myanmar left-bearing compensation and 22px starting size.
  - Generated thumbnail cache filenames now use `_v4` so prior images without the requested label are not reused.
  - `sample.env` documents `THUMB_BOT_NAME=မဂ်လာပါ မြန်မာ 🇲🇲`.
- Now-playing thumbnail bot-name top alignment correction (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` draws the runtime Telegram bot display name near the top of the generated card at the same visual left column as the lower title (`x=50`) with a 22px starting size.
  - Myanmar font left-bearing is compensated so names such as `မဂ်လာပါမြန်မာ🇲🇲🎧` do not appear clipped off the left edge.
  - Generated thumbnail cache filenames now use a `_v3` suffix so old `_v2`/original images with the wrong placement are not reused.
- Now-playing thumbnail bot-name lower-title placement correction (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` now draws the runtime Telegram bot display name in the lower metadata/title block at `x=50`, directly above the channel/title lines.
  - Bot-name overlay starts at 22px and shrinks to 18px if needed, keeping Myanmar + emoji names such as `မဂ်လာပါမြန်မာ🇲🇲🎧` visible without using the username.
  - Channel/title text was shifted down slightly to make room for the bot-name line while preserving the existing progress bar area.
  - Generated thumbnail cache filenames now use a `_v2` suffix so old cached images without the bot-name line are not reused.
- `/vsong` video upload thumbnail added (02-May-2026):
  - `AnonX_3/plugins/song.py` now passes a generated playback-style thumbnail to `app.send_video(..., thumb=...)` for `/vsong` and `/song -v`.
  - `AnonX_3/helpers/_thumbnails.py` added `generate_video_thumb()` to convert the full now-playing card into a Telegram-compatible JPEG thumbnail capped to 320px and compressed below the documented 200KB limit target.
  - Audio `/song` upload behavior is unchanged.
- Deep check cleanup for new thumbnail helpers (02-May-2026):
  - Re-read `AnonX_3/helpers/_thumbnails.py` and `AnonX_3/plugins/song.py` after adding bot-name and `/vsong` thumbnail paths.
  - Removed an unused `font3` thumbnail attribute left from the first bot-name overlay pass.
  - Confirmed the new `/vsong` code keeps thumbnail generation best-effort: if thumbnail generation fails, upload continues with `thumb=None`.
- Deep check for corrected bot-name and `/vsong` thumbnail functions (02-May-2026):
  - Re-read `AnonX_3/helpers/_thumbnails.py` and `AnonX_3/plugins/song.py` after the visual correction request.
  - Verified bot overlay now uses `app.me.first_name`/`last_name` display name only, not `@username`.
  - Verified overlay stays in the lower-title column (`x=50`) with smaller 26px starting size.
  - Verified `/vsong` still passes a generated JPEG thumbnail to `send_video(..., thumb=...)` while leaving audio `/song` unchanged.
- Now-playing thumbnail real bot name overlay added (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` now draws the runtime bot display name, plus username when different, near the top-left of generated playback thumbnails.
  - The overlay uses white text with a small shadow and shrinks to fit a fixed width.
  - Font selection now prefers Myanmar-capable fonts when the bot name contains Myanmar text, with bundled Latin font and common Linux font fallbacks.
- Now-playing thumbnail bot-name visual correction (02-May-2026):
  - `AnonX_3/helpers/_thumbnails.py` now uses Telegram bot display name from `app.me.first_name`/`last_name` only, not the `@username`, for the top-left overlay.
  - Bot-name overlay is aligned to the same `x=50` column as the lower title metadata and uses a smaller 26px starting size.
  - Mixed Myanmar + emoji bot names render through split text/emoji font segments, using Myanmar-capable font for text and emoji-capable font for characters such as `🇲🇲🎧`.
- Bot image defaults moved to local plugin assets (02-May-2026):
  - `config.py` now defaults `DEFAULT_THUMB` and `START_IMG` to `AnonX_3/plugins/img/welcome.jpg`.
  - `config.py` now defaults `PING_IMG` to `AnonX_3/plugins/img/ping.jpg`.
  - Env overrides still work for `DEFAULT_THUMB`, `START_IMG`, and `PING_IMG`.
  - `sample.env` now documents that bot images can be file IDs, local paths, or direct image URLs.
- Full function/static deep check snapshot (02-May-2026):
  - Re-ran root structure and function readiness checks after `.github`, `.dockerignore`, and broadcast changes.
  - Source/static checks passed, but local Windows runtime still requires installing `ffmpeg` on `PATH`.
  - Locale files are incomplete relative to `en.json`, but `AnonX_3/core/lang.py` merges every locale over English at load time, so missing translated keys fall back to English.
- Root/deploy security hardening after deep structure check (02-May-2026):
  - Hardened `.dockerignore` so Docker build context excludes `.git/`, `.github/`, env files, logs, session files, runtime `cache/`/`downloads/`, Python caches, IDE metadata, and cookie text files.
  - This prevents local secrets such as `.env`, `AnonX_3.session`, `AnonX_3.session-journal`, and runtime artifacts from being copied into container images.
- `/broadcast` MongoDB private-user auto-detect added (02-May-2026):
  - Added `_sync_dialog_users_to_db()` in `AnonX_3/plugins/broadcast.py`.
  - Before any user broadcast (`-user` used), the bot now scans `app.get_dialogs()` and auto-saves missing `PRIVATE` chat IDs into MongoDB via `db.add_user(user_id)`.
  - Broadcast then reads the refreshed `db.get_users()` list, so newly detected DM users are included in the same `/broadcast -user` run.
  - If live private-dialog scanning fails, broadcast continues with existing MongoDB user IDs.
  - `broadcast.md` was updated to document the private-user auto-detect behavior.
- `/broadcast` MongoDB group auto-detect added (02-May-2026):
  - Added `_sync_dialog_groups_to_db()` in `AnonX_3/plugins/broadcast.py`.
  - Before any group broadcast (`-nochat` not used), the bot now scans `app.get_dialogs()` and auto-saves missing `GROUP`/`SUPERGROUP` chat IDs into MongoDB via `db.add_chat(chat_id)`.
  - Broadcast then reads the refreshed `db.get_chats()` list, so newly detected groups are included in the same `/broadcast` run.
  - If live dialog scanning fails, broadcast continues with existing MongoDB chat IDs.
  - `broadcast.md` was updated to document the group auto-detect behavior.
- `/broadcast` exact spec deep-check against `broadcast.md` (02-May-2026):
  - Compared `broadcast.md` line-by-line with `AnonX_3/plugins/broadcast.py`.
  - Verified all documented active behavior is present: sudo decorators, reply-required usage guard, active-broadcast guard, DB group/user target loading, `groups + users` snapshot, logger forward/log pin, 0.1s send delay, `-copy` copy mode, default forward mode, group/user counters, `FloodWait.value + 30`, failed target collection, temporary `errors.txt`, stop commands, and guarded cleanup.
  - No code change was needed in this pass.
- `/broadcast` follow-up deep recheck for current replacement (02-May-2026):
  - Re-read `broadcast.md` and `AnonX_3/plugins/broadcast.py` after the guard pass.
  - No additional code changes were needed: current active functions still match documented behavior for `-nochat`, `-user`, `-copy`, forward/copy delivery, `errors.txt`, stop handling, and final status.
- `/broadcast` deep-fix guard pass while preserving `broadcast.md` behavior (02-May-2026):
  - Added guarded stale `errors.txt` cleanup at broadcast start.
  - Wrapped active broadcast work in `try/finally` so `broadcasting` always resets to `False` after runtime/report/status failures.
  - Kept documented target and flag semantics unchanged: DB chats unless `-nochat`, DB users only with `-user`, forward by default, copy only with `-copy`, 0.1s per target, `FloodWait.value + 30`.
  - Hardened failed-target report handling so `errors.txt` is removed even if document send fails.
  - Added final status fallback: if editing the original `gcast_start` message fails, send the final status as a reply.
  - Synced `broadcast.md` with these deep-fix guards.
- `/broadcast` replaced exactly from `broadcast.md` spec (02-May-2026):
  - `AnonX_3/plugins/broadcast.py` was reduced to the documented global `broadcasting` flag plus `_broadcast` and `_stop_gcast` handlers.
  - Removed prior helper/worker/rate-limit/dialog-scan/permanent-cleanup behavior (`RateLimiter`, `_collect_broadcast_targets`, retry workers, `error.txt`, stale-ID cleanup, forward-attribution preservation).
  - Current target source is only `db.get_chats()` unless `-nochat` is passed, plus `db.get_users()` only when `-user` is passed.
  - Current delivery behavior is `msg.forward(chat)` by default, `msg.copy(chat, reply_markup=msg.reply_markup)` only when `-copy` appears in command text, with `0.1s` delay per target and `FloodWait.value + 30s` wait.
  - Failures are written to temporary `errors.txt`, sent to the command caller, then removed.
  - Removed unused broadcast tuning env/config variables from `config.py` and `sample.env` because the new documented implementation does not use worker/rate settings.
- `/broadcast` third-pass delivery reliability fix (02-May-2026):
  - `_copy_once()` now re-raises `errors.FloodWait` from `msg.forward(...)` instead of swallowing it and immediately trying `copy`, so Telegram flood limits always go through the shared retry/cooldown path.
  - The logger 5-second pause now uses `_sleep_while_broadcasting(5)`, so `/stop_gcast` can interrupt the initial logger delay.
  - Report init/final write failures now fall back to result text instead of crashing the handler or losing the final user response.
  - `broadcast.md` was synced with FloodWait-preserving forward fallback and report-write fallback behavior.
- `/broadcast` second-pass reliability hardening (02-May-2026):
  - Added `_sleep_while_broadcasting()` so FloodWait/cooldown waits can exit promptly after `/stop_gcast` instead of sleeping up to the full backoff.
  - Moved per-run report cleanup/init inside the guarded `/broadcast` `try`, so filesystem/report setup failures cannot leave `broadcasting=True` stuck.
  - `_cleanup_broadcast_error_files()` now ignores `OSError` to prevent cleanup failures from crashing the handler.
  - `/stop_gcast` now ignores logger send/pin failures and still replies to the sudo user.
  - `broadcast.md` was updated for the new interruptible sleep and hardened stop/report behavior.
- `/broadcast` deep coverage fix per `broadcast.md` (02-May-2026):
  - `AnonX_3/plugins/broadcast.py::_collect_broadcast_targets()` now targets all served DB chats/users plus best-effort live dialogs without admin-status precheck.
  - This supersedes the previous admin-only group filter because it could under-count groups where the bot is not admin but can still send messages.
  - `/broadcast` now sets `broadcasting=True` before target collection and always resets it in `finally`, preventing stuck active state after logger/report/status-edit failures.
  - Logger mirror failures no longer abort the whole broadcast; they are recorded in the per-run report.
  - `config.py` and `sample.env` removed unused `BROADCAST_STATUS_WORKERS` because broadcast no longer runs `get_chat_member` prechecks.
  - `broadcast.md` was synced to current function-level behavior, including target collection, permanent-error cleanup, `error.txt`, and safe shutdown behavior.
- Added `update.md` changelog file (01-May-2026):
  - Documented latest `/broadcast` scope expansion (all admin groups + all DM users), admin-status precheck parallelization, config additions, and deep-check outcome.
- Deep check checkpoint for new broadcast target functions (01-May-2026):
  - Re-verified `_collect_broadcast_targets()` concurrent precheck flow in `AnonX_3/plugins/broadcast.py`.
  - Confirmed behavior is preserved: target scope remains admin/owner groups + DM users, with dedup merge from DB and live dialogs.
  - No high/medium correctness bug found in current static pass.
- `/broadcast` admin-status precheck parallelization (01-May-2026):
  - `AnonX_3/plugins/broadcast.py` updated `_collect_broadcast_targets()` to validate candidate group admin status with concurrent workers instead of sequential `get_chat_member` loop.
  - Added optional env/config knob `BROADCAST_STATUS_WORKERS` (default `24`) in `config.py` and `sample.env`.
  - Behavior preserved: still only keeps groups where bot is `ADMINISTRATOR` or `OWNER`; change is startup speed optimization for large group sets.
- `/broadcast` target coverage expansion for full admin-group + DM-user delivery (01-May-2026):
  - `AnonX_3/plugins/broadcast.py` now uses `_collect_broadcast_targets()` instead of DB-only target lists.
  - Target collection now merges:
    - `db.get_chats()` + live dialogs group/supergroup IDs, then keeps only chats where bot status is `ADMINISTRATOR` or `OWNER`.
    - `db.get_users()` + live private dialogs (`ChatType.PRIVATE`) as DM recipients.
  - This removes the old limitation where `/broadcast` only reached historical "served" DB subset, so broadcast now follows "all bot-admin groups + all bot-DM users".
- Post-fix deep re-check (01-May-2026):
  - Re-verified `broadcast.py`, `song.py`, and `start.py` sender-safety + HTML-escape patches are present and consistent.
  - No new high/medium static issues found in the newly added helper/function paths during this pass.
- Deep review checkpoint for new sender-safe functions (01-May-2026):
  - Verified previous null-guard hardening remains present in `AnonX_3/plugins/broadcast.py`, `AnonX_3/plugins/song.py`, and `AnonX_3/plugins/start.py`.
  - Follow-up deep fix applied:
    - `/song` caption now escapes `track.title` and sender-chat fallback title before formatting.
    - `/broadcast` logger flow now escapes sender-chat fallback title and escaped command text in `gcast_log`.
- Deep-check hardening for AnonX_3 sender edge cases (01-May-2026):
  - `AnonX_3/plugins/broadcast.py` added `_actor_info(...)` and now uses sender-safe actor fallback (`from_user` -> `sender_chat` -> `unknown`) for broadcast start/stop logger messages.
  - Prevents `/broadcast` and `/stop_gcast` crash on AnonX_3-admin commands where `message.from_user` is `None`.
  - `AnonX_3/plugins/song.py` now builds requester caption with safe fallback (`from_user` -> `sender_chat` -> `Unknown`), preventing caption formatting crash in AnonX_3 sender-chat flows.
  - `AnonX_3/plugins/start.py` now guards the early blacklist notification check with `message.from_user` presence, preventing `/start` sender-chat null dereference.
- `error.md` documentation sync (01-May-2026):
  - Corrected broadcast fix note path from `AnonX_3/plugins/broadcast.py` to the actual active path `AnonX_3/plugins/broadcast.py`.
  - Added follow-up deep-check notes for AnonX_3 sender safety hardening in `broadcast.py`, `song.py`, and `start.py`.
- `/broadcast` error-report reset and per-run consistency fix (01-May-2026):
  - `AnonX_3/plugins/broadcast.py` added `ERROR_FILE_PATH="error.txt"` and `_write_broadcast_error_file(...)` helper to generate a fresh report with UTC timestamp and error count.
  - Each `/broadcast` run now resets report state at start, so old run lines do not leak into the current run report.
  - End-of-run report is now always generated and sent as document (including zero-error runs with `No errors in this broadcast run.`).
  - Local report file cleanup now runs in a `finally` block to avoid stale `error.txt` leftovers when send flow exits unexpectedly.
  - Added legacy cleanup guard for prior `errors.txt` filename so old artifacts do not remain on disk.
- `/start` image source stability patch (01-May-2026):
  - `AnonX_3/plugins/start.py` now validates and normalizes candidate image sources before send attempts.
  - Invalid `/start` image sources are cached in-memory and skipped on next `/start`, preventing repeated noisy `sendPhoto` failures.
  - If the invalid source came from Mongo `start_img`, the bad DB value is auto-cleared via `db.set_bot_image("start_img", "")` so fallback flow recovers automatically.
  - Fallback chain remains photo candidates -> text response, so `/start` always responds even when photo sources are broken.
  - `config.py` default `START_IMG` now falls back to `DEFAULT_THUMB` instead of the previous hardcoded Catbox URL.
  - `sample.env` now documents optional `START_IMG`/`PING_IMG` overrides.
- `update.md` feature port completed from `AnonX_3` notes into active `AnonX_3` tree (`2026-04-30`):
  - Player controls now include a red `Close` button, and `controls close` callback safely deletes active controls message.
  - Help menu now includes `Songs` section and `help_songs` content.
  - Help menu button defaults now cycle `primary/success/danger` while preserving global (`help_item`) and per-button (`help_item_*`) style overrides.
  - Added working `/song` and `/vsong` command handler in `AnonX_3/plugins/song.py`:
    - `/song` default output: MP3 (ffmpeg conversion when needed)
    - `/song -v` or `/vsong`: MP4
    - Flow: search -> download -> optional convert -> upload
  - Added `/song` to bot command menu scopes in `AnonX_3/core/bot.py`.
  - Broadcast permanent-error cleanup completed in `AnonX_3/plugins/broadcast.py`:
    - Detects permanent Telegram delivery errors (`CHANNEL_INVALID`, `PEER_ID_INVALID`, `USER_IS_BLOCKED`, etc.).
    - Auto-removes stale failed IDs from served chats/users via `db.rm_chat()` / `db.rm_user()`.
    - `errors.txt` now includes a `Cleaned stale IDs` section for visibility.
- `/start` invalid image-source hardening (30-Apr-2026):
  - `AnonX_3/plugins/start.py` now detects invalid photo source errors (`wrong file identifier/HTTP URL specified` / `PHOTO_INVALID`).
  - If DB `start_img` is invalid, it auto-tries fallback `config.START_IMG`; if still invalid, it falls back to text response cleanly.
  - Prevents repeated duplicate photo-failure traces during `/start`.
- `/broadcast` forward-attribution behavior refined to 2 modes:
  - If the replied source is a forwarded message from the same sender who runs `/broadcast` (self-source), it uses `msg.copy(...)` so `Forwarded from` is hidden.
  - If the replied source is forwarded from others, it uses `msg.forward(...)` so `Forwarded from` is preserved.
  - If Telegram blocks forwarding for a target (for example, forward restrictions), it gracefully falls back to `msg.copy(...)` so broadcast delivery still continues.
  - Logger mirror during broadcast start now follows the same mode decision.
- Locale UTF-8 BOM startup crash fix:
  - `AnonX_3/core/lang.py` now reads locale JSON files with `encoding="utf-8-sig"`, so startup survives accidental UTF-8 BOM in `en.json` or any translated locale.
  - Rewrote every `AnonX_3/locales/*.json` file as UTF-8 without BOM after Windows PowerShell had written BOM bytes, which caused Linux Python `json.load()` to crash with `JSONDecodeError: Unexpected UTF-8 BOM`.
- `/ping` latency reduction without fake values:
  - `AnonX_3/plugins/ping.py` now sends the initial ping probe through the existing `bot_api.send_message(..., fetch=False)` path instead of Pyrogram `reply_text()`, so displayed latency measures the actual Bot API `sendMessage` round trip without the extra Pyrogram message-object fetch.
  - The final ping photo update now uses `bot_api.edit_message_media(..., fetch=False)` directly, reducing handler overhead after the latency sample is collected.
  - `AnonX_3/core/bot_api.py` added optional `fetch=False` support for `send_message()` and `edit_message_media()` while preserving the previous default fetch behavior for existing callers.
- `/ping` PyTgCalls latency truthfulness fix:
  - `AnonX_3/core/calls.py::ping()` now returns `None` when PyTgCalls has no valid positive ping sample instead of reporting fake `0.0`.
  - `AnonX_3/plugins/ping.py` now formats missing PyTgCalls ping as a localized measuring state (`ping_pytgcalls_measuring`) and only appends `ms` when a real numeric sample exists.
  - `AnonX_3/core/stream_profile.py::safe_ping_value()` now ignores `None`, invalid, and non-positive ping samples so adaptive stream quality does not treat missing ping as a perfect network.
  - Locale `ping_pong` templates now accept a fully formatted PyTgCalls ping string in `{5}`, preventing stale `0.0ms` and double-unit output.
- `/play` / `/vplay` smooth-first + adaptive stutter reduction pass:
  - `AnonX_3/core/calls.py` now uses smooth-first `auto` stream policy (`audio=MEDIUM`, `video=SD_480p`) and adaptive low-tier downgrade (`audio=LOW`, `video=SD_360p`) under stress signals (CPU/ping thresholds with recovery hysteresis).
  - Added per-track stream-profile observability log (`tier`, `reason`, CPU, ping) for correlation in `log.txt`.
  - Queue transition prefetch join now uses bounded timeout via `PREFETCH_JOIN_TIMEOUT` (default `8s`) instead of long fixed wait.
  - `AnonX_3/core/youtube.py` now deduplicates concurrent downloads per `video_id` bucket to avoid duplicate heavy fetches during queue/prefetch overlap.
  - `config.py`/`sample.env` added adaptive tuning vars: `STREAM_ADAPTIVE`, `ADAPTIVE_CPU_HIGH`, `ADAPTIVE_CPU_RECOVER`, `ADAPTIVE_PING_HIGH`, `ADAPTIVE_PING_RECOVER`, `PREFETCH_JOIN_TIMEOUT`.
  - Smooth defaults updated to lower video pressure: `VIDEO_MAX_HEIGHT=480`, `VIDEO_MAX_FPS=24`.
- `/ping` latency display stabilization:
  - `AnonX_3/plugins/ping.py` now keeps a rolling window (`maxlen=12`) of raw `/ping` command latencies.
  - Displayed latency now uses window median (instead of single-shot value) to reduce jitter spikes from transient Telegram/update-queue delays while keeping command behavior unchanged.
- Crash-only deep fix pass (26-Apr-2026 log-driven):
  - `AnonX_3/core/calls.py` now catches `TimeoutError` from `client.play()` (`phone.JoinGroupCall` retry exhaustion), logs `chat_id/media_id`, stops playback state, and reports Telegram-server error via utility edit flow without bubbling to dispatcher.
  - `AnonX_3/helpers/_utilities.py` added `edit_callback_text()` safe path: try text edit first, fallback to caption edit on `there is no text in the message to edit`, preserve existing entity fallback chain, and optionally ignore stale/deleted targets.
  - `AnonX_3/plugins/language.py` now uses `utils.edit_callback_text(..., ignore_stale=True)` for both language menu and language-changed callback edits.
  - `AnonX_3/plugins/callbacks.py` force-expired callback path now uses `utils.edit_callback_text(..., ignore_stale=True)` instead of direct `query.edit_message_text(...)`.
- Added deployment-sync operational artifacts for stale-edit crash verification:
  - `ops/verify_runtime_guard.sh` checks runtime source for `_fetch_message` stale guards, entity fallback chain, and stale callback handling.
  - `ops/deploy_sync_runbook.md` defines baseline capture, Heroku container redeploy flow, and post-redeploy pass criteria.
- `/setping` discoverability update completed:
  - Kept command behavior unchanged (`/setping` remains sudo-only and reply-photo based via `set_bot_image(..., "ping_img")`).
  - Added `/setping` help text entry inside `help_sudo` for all locale files (`AnonX_3/locales/*.json`).
  - `/ping` image source path remains unchanged (`db.get_bot_image("ping_img")`).
- Deep fix pass for runtime errors found in `C:\Users\HP\Downloads\log.txt`:
  - `AnonX_3/helpers/_utilities.py` now retries `reply_text`, `send_message`, `edit_text`, and `edit_caption` on `ENTITY_TEXT_INVALID`.
  - Entity fallback order is: original rich entities, retry without `custom_emoji` entities, then retry without entities.
  - `AnonX_3/core/bot_api.py` now treats both `message to edit not found` and `MESSAGE_ID_INVALID` as stale edit errors.
  - Bot API plain-entity fallback can now pass an empty entity list without re-enabling HTML parse mode.
  - `AnonX_3/core/calls.py` now sends a replacement now-playing message when the original processing/playback message cannot be edited.
  - Replacement now-playing sends update `media.message_id`, so later controls target the newly sent message.
  - `AnonX_3/plugins/callbacks.py` skip/replay/stop status messages now use `utils.send_message` instead of direct `bot_api.send_message`, so entity fallback is applied.
  - `AnonX_3/plugins/callbacks.py` pause/resume control edits now use `utils.edit_text`/`utils.edit_caption` and ignore stale edit targets.
- Deep fix pass for missing runtime keys/functions:
  - `AnonX_3/core/lang.py` now merges every locale over `en.json` at load time, so incomplete locale files fall back to English keys instead of raising `KeyError`.
  - Invalid or unavailable chat language codes now fall back to English language data.
  - `autoplay_no_match` is registered in `AnonX_3/plugins/restart.py` runtime text config and resettable keys, so `/settext autoplay_no_match` and `/gettext autoplay_no_match` are supported.
- Fixed autoplay no-match playback control flow:
  - `autoplay_no_match` is sent only when autoplay is ON and no similar track can be selected.
  - Normal queue-end with autoplay OFF now stops silently as before and no longer falls through with `media=None`.
  - No-match message is sent through formatted template helpers so custom text/entities are preserved.
- Added autoplay no-match user feedback when queue ends:
  - `AnonX_3/core/calls.py` now sends `autoplay_no_match` message before stopping playback if autoplay cannot find a similar next track.
  - Added locale keys for this message in `AnonX_3/locales/en.json` and `AnonX_3/locales/my.json`.
- Simplified `/autoplay` UX in command handler:
  - `/autoplay` (without args) now toggles state directly (OFF->ON / ON->OFF).
  - `/autoplay on|off` is still supported for explicit control.
  - Updated fallback usage text to show both forms.
- Fixed `/autoplay` repeat-loop behavior:
  - `AnonX_3/core/youtube.py::autoplay_track()` no longer falls back to returning the same seed track when no suitable related candidate is found.
  - Added `exclude_ids` support in autoplay lookup to skip current/queued/recently-autoplayed track IDs.
  - `AnonX_3/core/calls.py` now tracks per-chat recent autoplay IDs (`maxlen=12`) and uses that history while selecting next autoplay track.
  - `AnonX_3/plugins/autoplay.py` proactive prefill path (`/autoplay on` during active call) now excludes already queued/current IDs.
- Updated `AnonX_3/plugins/autoplay.py` to accept more boolean forms for command arg parsing:
  - on/off, enable/disable, true/false, yes/no, 1/0
- Added proactive queue fill when `/autoplay on` is used during an active call:
  - If current track exists and no next track is queued, fetch one similar track and append it immediately.
- Refined `/setbt` button text override behavior:
  - `/setbt [key]` shows source + preview + usage flow similar to `/settext`.
  - Re-enabled storing first replied `custom_emoji` as optional `icon_custom_emoji_id` (`Icon + Keep Text`).
  - Inline button renderer now reads `icon_custom_emoji_id` again, with backward-compatible fallback from stored entities.
  - `/setbt` override labels are now sanitized at render time to strip Unicode emojis from button text, so premium icon slot is used instead of text emojis.
  - Existing key/text override behavior remains unchanged.
- `/autoplay` selector updated to dynamic vibe-first behavior (27-Apr-2026):
  - Removed hardcoded theme mapping behavior; selector now extracts vibe/mood tokens directly from seed title/channel text.
  - Ranking now prioritizes seed-vibe token overlap + artist relevance (artist secondary), and penalizes same-title core repeats more aggressively.
  - Title cooldown keys in `AnonX_3/core/calls.py` now use cleaned core-title tokens to reduce near-duplicate loops (same song/version churn).
- `/autoplay` diversity hardening follow-up (27-Apr-2026):
  - Candidate filter now hard-rejects same core-title as current/recent history (not only penalty), reducing "same song keeps replaying" loops.
  - Query builder now broadens dynamic vibe terms (`term`, `term song`, `term music`, Myanmar-term `term သီချင်း`) and lowers artist+term dominance for cross-artist vibe results.
  - Track title metadata used in search/deep-search/playlist increased from 25 to 80 chars to keep more vibe keywords for relevance scoring.
- `/autoplay` keyword-strict selector update (27-Apr-2026):
  - Candidate acceptance now uses strict keyword overlap gate from seed title tokens (artist-agnostic): seed keywords >=2 requires 2+ overlaps; single keyword requires 1 overlap.
  - Unrelated/generic fallback queries are disabled in strict mode; query pool is keyword-centric (keyword-only + keyword-combo + optional artist+keyword).
  - Added autoplay selector debug logging for seed keywords, required overlap, checked/rejected/accepted counters, and selected track metadata.
- `/autoplay` selector module split (27-Apr-2026):
  - Strict autoplay selection logic was moved from `AnonX_3/core/youtube.py` into `AnonX_3/core/autoplay.py`.
  - `YouTube.autoplay_track()` remains as the compatibility wrapper, delegating to `StrictAutoplaySelector` with `deep_search`.
  - `/autoplay` command handler and queue-end caller continue using `yt.autoplay_track(...)`, so public command behavior is unchanged.
- `/play` / `/vplay` playback cleanup + network-aware auto quality (27-Apr-2026):
  - Split playback responsibilities out of `AnonX_3/core/calls.py` into `AnonX_3/core/stream_profile.py`, `AnonX_3/core/prefetch.py`, and `AnonX_3/core/playback.py`.
  - `TgCall.play_media()` and `TgCall.play_next()` remain compatibility entrypoints, but now delegate stream profile selection, prefetch/download orchestration, and now-playing rendering.
  - Video downloads now accept `quality_tier` (`good`/`normal`/`poor`) so auto mode can cap downloads to network/CPU-selected quality while preserving env caps.
  - First-track and callback-force playback now let `play_media()` perform the final download, so selected stream quality can influence `/vplay` downloads.
- Log-driven crash fix pass (27-Apr-2026):
  - `AnonX_3/core/bot_api.py` now handles Bot API `429 Too Many Requests` with bounded retry (`retry_after`) for both JSON and multipart endpoints.
  - `AnonX_3/core/bot_api.py` now treats `message is not modified` as non-fatal and returns current message flow without raising handler-breaking errors.
  - `AnonX_3/plugins/start.py` now has 3-step fallback on `/start`: photo with entities -> photo without entities -> text message fallback, preventing unhandled handler crashes when photo send rights are missing.
- Updated `/settext` usage output:
  - Now shows both `Runtime text keys` and `All template keys`.
  - `All template keys` includes non-runtime template key `start_pm` plus runtime keys.
- Performance tuning for ping/stats telemetry:
  - `AnonX_3/plugins/ping.py`: switched latency timing to `time.perf_counter()`, added async gather for image + PyTgCalls ping, and added 1-second cache for CPU/RAM/Disk sampling to reduce repeated psutil overhead.
  - `AnonX_3/plugins/stats.py`: removed blocking `process.cpu_percent(interval=1.0)` and replaced with non-blocking warmup + `process.oneshot()` based snapshot.
  - `AnonX_3/core/calls.py`: hardened `anon.ping()` against empty clients and increased precision to 3 decimals.
- Playback smoothness tuning for `/play` and `/vplay`:
  - Added configurable stream quality controls in `config.py`:
    - `AUDIO_QUALITY`, `VIDEO_QUALITY`, `VIDEO_MAX_HEIGHT`, `VIDEO_MAX_WIDTH`, `VIDEO_MAX_FPS`, `PREFETCH_NEXT`.
  - `AnonX_3/core/calls.py` now maps stream qualities from config and defaults to smoother profile (`medium` audio + `sd_480p` video).
  - Added next-track prefetch task in `AnonX_3/core/calls.py` to download upcoming queue media in background and reduce transition stalls.
  - `AnonX_3/core/youtube.py` video download format now honors configured max height/width/fps caps to reduce decode/network pressure.
  - Updated `sample.env` with playback tuning variables.
- Broadcast scalability tuning:
  - Reworked `/broadcast` from sequential send loop to concurrent worker model (`BROADCAST_WORKERS=8`).
  - Added retry logic for `FloodWait` with bounded backoff and retry cap (`MAX_RETRIES=3`).
  - Removed per-message fixed delay bottleneck and made failures accumulate in `errors.txt` as before.
  - Preserved `/stop_gcast` behavior; workers stop when broadcasting flag is cleared.
  - Made broadcast tuning configurable from env:
    - `BROADCAST_WORKERS` (default `8`)
    - `BROADCAST_MAX_RETRIES` (default `3`)
    - `BROADCAST_RATE_PER_SEC` (default `12`)
    - `BROADCAST_WORKERS_BUSY` (default `3`)
    - `BROADCAST_RATE_PER_SEC_BUSY` (default `6`)
  - Added adaptive global flood cooldown + per-message rate limiter so broadcast stays inside Telegram limits while keeping throughput high.
  - Added playback-protection busy mode:
    - When active voice calls exist, broadcast auto switches to lower worker/rate profile to reduce impact on `/play` and `/vplay`.
  - Improved busy-mode to be real-time adaptive during broadcast run:
    - Rate limiter now re-evaluates active call state per-send (`normal` vs `busy` rate).
    - Worker pool now auto-gates active workers while calls are ongoing, then restores full throughput when calls end.
  - Broadcast mode policy updated per latest request:
    - Disabled playback-aware auto-throttle during broadcast.
    - Broadcast now always uses fast profile (`BROADCAST_WORKERS` + `BROADCAST_RATE_PER_SEC`) even while `/play`/`/vplay` is active.

## Files Touched
- `ops/verify_runtime_guard.sh`
- `ops/deploy_sync_runbook.md`
- `AnonX_3/locales/ar.json`
- `AnonX_3/locales/de.json`
- `AnonX_3/locales/en.json`
- `AnonX_3/locales/es.json`
- `AnonX_3/locales/fr.json`
- `AnonX_3/locales/hi.json`
- `AnonX_3/locales/ja.json`
- `AnonX_3/locales/my.json`
- `AnonX_3/locales/pa.json`
- `AnonX_3/locales/pt.json`
- `AnonX_3/locales/ru.json`
- `AnonX_3/locales/tr.json`
- `AnonX_3/locales/zh.json`
- `AnonX_3/plugins/autoplay.py`
- `AnonX_3/core/mongo.py`
- `AnonX_3/helpers/_inline.py`
- `AnonX_3/plugins/restart.py`
- `AnonX_3/core/lang.py`
- `AnonX_3/plugins/ping.py`
- `AnonX_3/plugins/stats.py`
- `AnonX_3/core/calls.py`
- `AnonX_3/core/youtube.py`
- `AnonX_3/core/autoplay.py`
- `AnonX_3/core/stream_profile.py`
- `AnonX_3/core/prefetch.py`
- `AnonX_3/core/playback.py`
- `config.py`
- `sample.env`
- `AnonX_3/plugins/broadcast.py`
- `AnonX_3/helpers/_utilities.py`
- `AnonX_3/core/bot_api.py`
- `AnonX_3/plugins/callbacks.py`
- `AnonX_3/plugins/song.py`
- `AnonX_3/helpers/_thumbnails.py`
- `AnonX_3/core/bot.py`
- `AnonX_3/locales/en.json`
- `AnonX_3/locales/my.json`
- `AnonX_3/core/youtube.py` (synced across AnonX_3, AnonX_3, AnonX_3, AnonX_3, AnonX_3)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `AnonX_3/AnonX_3/cookies/cookies.txt` (regenerated, 13 cookies)
- `PROJECT_STATE.md`
- `broadcast.md`

## Validation
- Validation for dynamic Myanmar-only thumbnail label:
  - `uv run python -m compileall -q AnonX_3 config.py`: OK.
  - `app.json` parsed with PowerShell `ConvertFrom-Json`: OK.
  - Static scan confirmed `THUMB_BOT_NAME=မဂ်လာပါ မြန်မာ 🇲🇲` defaults/docs in `config.py`, `sample.env`, and `app.json`; `_v6` thumbnail cache names; additional Noto Myanmar font lookup paths; and `fonts-noto-extra` in Docker/VPS setup.
- Validation for now-playing top label visibility hardening:
  - `uv run python -m compileall -q AnonX_3 config.py`: OK.
  - Static scan confirmed `_v5` thumbnail cache names, top label `x=50, y=36`, 24px starting size, semi-transparent dark label strip, and Noto Myanmar/emoji font packages in both `Dockerfile` and `setup`.
- Validation for `/vsong` generated card cover:
  - `uv run python -m compileall -q AnonX_3 config.py`: OK.
  - Static scan confirmed `/vsong` now passes full generated card as `video_cover=` and 320px JPEG as `thumb=`, with fallback to `cover=` then thumb-only for client-version differences.
  - `uv run --with-requirements requirements.txt python -` confirmed installed Kurigram `Client.send_video` supports `thumb`, `video_cover`, and `supports_streaming`; it does not expose a plain `cover` parameter.
- Validation for `/vsong` generated video thumbnail:
  - `uv run python -m py_compile AnonX_3\helpers\_thumbnails.py AnonX_3\plugins\song.py`: OK.
  - Inspected local Kurigram `Client.send_video` signature/docs: `thumb` is supported; docs require JPEG, under 200KB, max 320px.
  - Deep-check validation after cleanup: compile OK; installed Kurigram `Client.send_video` signature still includes `thumb` and `supports_streaming`.
- Validation for now-playing thumbnail bot-name overlay:
  - `uv run python -m py_compile AnonX_3\helpers\_thumbnails.py`: OK.
  - Local `git diff` could not run because `git` is not installed/on PATH on this host.
  - Visual correction validation: `uv run python -m py_compile AnonX_3\helpers\_thumbnails.py AnonX_3\plugins\song.py`: OK.
  - PIL smoke test with sample `မဂ်လာပါမြန်မာ🇲🇲🎧` measured successfully using Windows Myanmar Text + Segoe UI Emoji fonts.
  - Deep-check validation for corrected bot-name and `/vsong` functions:
    - `uv run python -m compileall -q AnonX_3 config.py`: OK.
    - `uv run --with-requirements requirements.txt python -c "... Client.send_video signature ..."`: OK, `thumb` and `supports_streaming` are present.
    - AST parse of `AnonX_3/helpers/_thumbnails.py` and `AnonX_3/plugins/song.py`: OK.
    - Static assertions passed for display-name source, absence of `@username` concatenation, 26px overlay sizing, `x=50` alignment, JPEG/320px/200KB thumbnail constraints, and `/vsong` `thumb=video_thumb` wiring.
    - Current tool PowerShell session still does not resolve `ffmpeg` via `where.exe ffmpeg`; the WinGet link exists, so the shell/PATH may need to be reopened/refreshed for runtime tests.
- Validation for local plugin image defaults:
  - `uv run python -m py_compile config.py AnonX_3\plugins\start.py AnonX_3\plugins\ping.py AnonX_3\core\mongo.py`: OK.
  - Confirmed `AnonX_3/plugins/img/welcome.jpg` and `AnonX_3/plugins/img/ping.jpg` exist.
- Validation for full function/static deep check:
  - `uv run python -m compileall -q AnonX_3 config.py`: OK.
  - Parsed `app.json` and every `AnonX_3/locales/*.json` with PowerShell `ConvertFrom-Json`: OK.
  - Dependency import check with `uv run --with-requirements requirements.txt python -`: OK for core requirements.
  - Locale key coverage check found missing translated keys in non-English locales, but runtime fallback to English is verified in `AnonX_3/core/lang.py`.
  - `Get-Command ffmpeg` returned no result on this Windows host; Dockerfile/setup install it for container/Linux paths.
- Validation for root/deploy deep structure check:
  - `uv run python -m compileall -q AnonX_3 config.py`: OK.
  - Parsed `app.json` and every `AnonX_3/locales/*.json` with PowerShell `ConvertFrom-Json`: OK.
  - Dependency import check with `uv run --with-requirements requirements.txt python -`: OK for `aiohttp`, `PIL`, `psutil`, `pymongo`, `pyrogram`, `pytgcalls`, `ntgcalls`, `yt_dlp`, and `dotenv`.
  - `.github` metadata scan confirmed old owner refs are absent and `MingalaparX2026` refs are present.
  - Local host still does not have `ffmpeg` on `PATH`; Dockerfile and Linux `setup` install `ffmpeg`, but Windows/local runtime needs separate host install.
- Validation for `/broadcast` MongoDB private-user auto-detect:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan confirmed `_sync_dialog_users_to_db()` calls `app.get_dialogs()`, filters `enums.ChatType.PRIVATE`, calls `db.add_user(user_id)`, and is invoked before `db.get_users()` when `-user` is present.
- Validation for `/broadcast` MongoDB group auto-detect:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan confirmed `_sync_dialog_groups_to_db()` calls `app.get_dialogs()` and is invoked before `db.get_chats()` when `-nochat` is not present.
- Validation for `/broadcast` exact spec deep-check:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan confirmed old worker/rate-limit/permanent-cleanup symbols are absent from active code/config/sample.
  - Numbered `broadcast.md` behavior checked against active source lines in `AnonX_3/plugins/broadcast.py`: OK.
- Validation for `/broadcast` follow-up deep recheck:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan confirmed no old broadcast helper/rate-limit symbols remain in active `AnonX_3`, `config.py`, or `sample.env`.
  - Function scan confirmed active broadcast module contains only `_broadcast`, `_stop_gcast`, the global `broadcasting` flag, and the `errors.txt` path constant.
- Validation for `/broadcast` deep-fix guard pass:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan of active code confirmed no old broadcast helper/rate-limit symbols remain in `AnonX_3`, `config.py`, or `sample.env`.
- Validation for `broadcast.md` replacement:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
  - Static scan of active code confirmed no `BROADCAST_*`, `_collect_broadcast_targets`, `RateLimiter`, `_copy_with_retry`, `_copy_once`, `_sleep_while_broadcasting`, `ERROR_FILE_PATH`, `LEGACY_ERROR_FILE_PATH`, `PERMANENT_BROADCAST_ERRORS`, or `error.txt` references remain in `AnonX_3`, `config.py`, or `sample.env`.
  - Direct `python -m py_compile ...` still cannot run on this host because `python` resolves to the Microsoft Store alias.
- Static verification for `/broadcast` deep coverage fix:
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK after third-pass delivery reliability fix.
  - Static scan confirmed `_copy_once()` has explicit `except errors.FloodWait: raise` before generic forward fallback.
  - Static scan confirmed logger delay uses `_sleep_while_broadcasting(5)` instead of direct `asyncio.sleep(5)`.
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK after second-pass reliability hardening.
  - Static scan confirmed no direct `await asyncio.sleep(backoff)` / `await asyncio.sleep(wait_for)` remains in `_copy_with_retry`; waits now use `_sleep_while_broadcasting()`.
  - `AnonX_3/plugins/broadcast.py` no longer references `get_chat_member`, `ADMIN_STATUSES`, or `BROADCAST_STATUS_WORKERS`.
  - `_collect_broadcast_targets()` now merges `db.get_chats()`, `db.get_users()`, and best-effort `app.get_dialogs()` without dropping non-admin served groups before send.
  - `config.py` and `sample.env` no longer expose `BROADCAST_STATUS_WORKERS`.
  - `uv run python -m py_compile AnonX_3\plugins\broadcast.py config.py`: OK.
- Historical validation note superseded:
  - The prior `BROADCAST_STATUS_WORKERS` / `get_chat_member` admin-precheck path was removed on 02-May-2026 to avoid under-counting send-capable non-admin served groups.
- `python -m py_compile AnonX_3\plugins\broadcast.py` could not run on this host because `python` resolves to Microsoft Store alias (no active Python executable on PATH).
- `python -m py_compile AnonX_3\plugins\broadcast.py config.py` could not run on this host because `python` resolves to Microsoft Store alias (no active Python executable on PATH).
- Historical validation note superseded:
  - `_collect_broadcast_targets()` still exists and is still used by `/broadcast`.
  - The previous admin-only group filtering validation is no longer current after the 02-May-2026 deep coverage fix.
  - Current behavior attempts all served DB groups plus best-effort live group dialogs; failed/non-sendable targets are reported at send stage.
- Verified `AnonX_3/plugins/broadcast.py` now writes report files through `_write_broadcast_error_file(...)` at both run start and run end, and removes `error.txt` (plus legacy `errors.txt`) via `finally` cleanup.
- Verified `AnonX_3/plugins/broadcast.py` now applies dual-mode forward behavior for `/broadcast`:
  - self-sourced forwarded input -> copy (hide forward attribution)
  - other-sourced forwarded input -> forward (preserve attribution)
  - blocked forward targets -> copy fallback
- Local environment limitation verified: `git`, `docker`, and `heroku` CLIs are not available on this host, and Heroku env credentials are not present (`HEROKU_API_KEY` missing), so remote redeploy was prepared as runbook/script steps instead of executed here.
- Verified repo currently contains runtime guards for `MESSAGE_IDS_EMPTY`/`MESSAGE_ID_INVALID` in `AnonX_3/core/bot_api.py`.
- Verified repo currently contains `ENTITY_TEXT_INVALID` retry fallback chain in `AnonX_3/helpers/_utilities.py`.
- Verified `/setping` handler remains mapped to `set_bot_image(m, "ping_img")` under `app.sudoers`.
- Verified `/ping` continues to resolve image from `db.get_bot_image("ping_img")`.
- Parsed every locale JSON after edit with PowerShell `ConvertFrom-Json`: OK.
- Verified every locale `help_sudo` now includes `/setping:` entry.
- Verified direct callback skip/replay/stop status sends no longer call `bot_api.send_message` directly; remaining direct calls are inside utility fallback wrappers.
- Parsed every `AnonX_3/locales/*.json` after `/ping` PyTgCalls latency edits with PowerShell `ConvertFrom-Json`: OK.
- Verified no locale `ping_pong` template still contains `{5}ms` or `{5}мс`; PyTgCalls ping units are now supplied by code only when numeric.
- Parsed every `AnonX_3/locales/*.json` after `/ping` Bot API fast-path edit with PowerShell `ConvertFrom-Json`: OK.
- Verified every `AnonX_3/locales/*.json` has no UTF-8 BOM after locale crash fix.
- Parsed every `AnonX_3/locales/*.json` after BOM cleanup with PowerShell `ConvertFrom-Json`: OK.
- Python compile check for `AnonX_3/core/calls.py`, `AnonX_3/core/stream_profile.py`, and `AnonX_3/plugins/ping.py` could not run because `python` resolves to the Microsoft Store alias on this host.
- Python compile check for `AnonX_3/core/bot_api.py` and `AnonX_3/plugins/ping.py` could not run because `python` resolves to the Microsoft Store alias and `py` is unavailable on this host.
- Verified callback control edit path uses utility edit wrappers instead of direct `bot_api.edit_message_text`/`edit_message_caption`.
- Verified stale edit detection now includes `MESSAGE_ID_INVALID` and `message to edit not found`.
- Verified text utility functions contain shared `ENTITY_TEXT_INVALID` fallback handling.
- Parsed every `AnonX_3/locales/*.json` with PowerShell `ConvertFrom-Json`: OK.
- Simulated English fallback merge for every locale: OK.
- Verified log tracebacks map to two handler-crash roots:
  - Bot API edit `429 retry_after` raised as unhandled `RuntimeError` in `/play` stream status edit path.
  - `/start` photo send permission error (`not enough rights to send photos`) raised twice and bubbled as unhandled exception.
- Verified `autoplay_no_match` references in playback flow, locale files, and runtime text config with `rg`.
- Python compile checks could not be completed because `python` resolves to the Microsoft Store alias instead of a working interpreter on this host.
- Verified `update.md` port status in this checkout:
  - `AnonX_3/plugins/song.py` now exists and registers `/song` + `/vsong`.
  - `AnonX_3/core/bot.py` now includes `/song` in all/private/group command scopes.
  - `AnonX_3/helpers/_inline.py` includes `controls_close` and help `Songs` menu wiring with mixed color defaults.
  - `AnonX_3/plugins/callbacks.py` includes safe `controls close` deletion branch.
  - `AnonX_3/locales/en.json` parses successfully after `help_songs` + `song_*` key additions (`ConvertFrom-Json`: OK).
- Verified `/broadcast` now has permanent-error stale-ID cleanup path:
  - Extracts permanent error tokens from exception text/class/`ID`.
  - Removes stale chat/user IDs from DB so the same invalid IDs do not repeat in future broadcast error files.
- Verified `/start` handler fallback chain now includes invalid-image detection:
  - DB `start_img` -> `config.START_IMG` -> text fallback.
  - Invalid image errors are downgraded to warning logs and do not block `/start` response.

## Open Notes
- Runtime verification still needed for the current `broadcast.md` replacement:
  - Run `/broadcast` as a sudo user while replying to a message and confirm default delivery reaches served groups from `db.get_chats()`.
  - Run `/broadcast -user`, `/broadcast -nochat -user`, and `/broadcast -user -copy` to confirm documented flag behavior.
  - Run `/stop_gcast` during an active broadcast and confirm the loop stops at the next target iteration and final status uses `gcast_stopped`.
  - Confirm failed targets produce temporary `errors.txt` for the caller and that the file is removed after sending.
- Runtime verification still needed for new `/song` flow:
  - Test `/song [query]` sends MP3 successfully after download + ffmpeg conversion.
  - Test `/song -v [query]` and `/vsong [query]` send MP4 successfully.
  - Confirm failure messages for not-found/download/convert/upload cases.
- Runtime verification still needed for controls close UX:
  - Open now-playing controls and click `Close`; confirm controls message deletes cleanly without noisy stale edit errors.
- Runtime verification still needed for log-derived runtime fixes:
  - Run `/play`, delete the bot processing/playing message before the playback update, and confirm a new now-playing message appears without a traceback.
  - Use inline skip/replay/stop buttons with custom/premium emoji text templates and confirm no `ENTITY_TEXT_INVALID` crash.
  - Run `/skip` with custom/premium emoji text templates and confirm fallback sends a valid message if Telegram rejects entities.
  - Click old/stale inline controls after deleting their message and confirm no noisy `MESSAGE_ID_INVALID` traceback.
  - Re-check `log.txt` after runtime testing for absence of `ENTITY_TEXT_INVALID`, `message to edit not found`, and `MESSAGE_ID_INVALID` stack traces.
- Runtime verification still needed in Telegram group VC:
  - Confirm `/autoplay` with no argument toggles and returns correct ON/OFF message.
  - With autoplay OFF, let a single-song queue end and confirm no `autoplay_no_match` text is sent.
  - Play one song, run `/autoplay on`, then confirm next similar song auto-continues after current track ends.
  - If no similar track is found at queue-end, confirm bot sends autoplay no-match text and then leaves/stops cleanly.
  - Run `/settext autoplay_no_match`, `/gettext autoplay_no_match`, and `/settext autoplay_no_match default` to confirm runtime customization works.
  - Switch to a non-English locale with missing repo keys and confirm common commands do not raise `KeyError`.
  - Confirm same song ID does not immediately repeat when autoplay candidate pool is narrow.
  - Keep autoplay running for 5-10 transitions and confirm recent tracks are not reselected in a short loop.
- Runtime verification still needed for `/setbt`:
  - Reply to a message that contains premium/custom emoji text and set a key like `start_add_me`.
  - Run `/setbt start_add_me` without reply and confirm source + preview + usage are shown.
  - Open a UI that renders the key and confirm premium emoji icon appears on the target button.
  - Verify Unicode emojis in custom label text are not shown on button text.
  - Re-set the same key using plain text and confirm icon is removed.
- Runtime verification still needed for `/settext` help output:
  - Run `/settext` (without args) and confirm both key sections are shown.
- Runtime verification still needed for log-driven crash fixes:
  - Trigger rapid `/play` or status edits in same chat and confirm no `RuntimeError: Too Many Requests: retry after ...` traceback in `log.txt`.
  - In a chat where bot cannot send photos, run `/start` and confirm text fallback is sent without `Unexpected exception raised in MessageHandler`.
- Runtime verification still needed for performance telemetry:
  - Compare `/ping` response delay before/after under same host/network load.
  - Confirm `/stats` responds faster and no 1-second CPU sampling block remains.
  - Note: PyTgCalls latency is network/runtime dependent and cannot be forced to a fixed floor like `0.01ms`.
- Runtime verification still needed for playback smoothness:
  - Test `/play` audio continuity over multiple tracks and confirm fewer stalls between queue transitions.
  - Test `/vplay` under constrained network/CPU and tune `VIDEO_MAX_HEIGHT`/`VIDEO_MAX_FPS` if stutter persists.
- Environment tuning applied in local `.env`:
  - `VIDEO_QUALITY=auto`
  - `VIDEO_MAX_HEIGHT=720`
  - `VIDEO_MAX_WIDTH=1280`
  - `VIDEO_MAX_FPS=24`
  - `AUDIO_QUALITY=auto`
  - `PREFETCH_NEXT=True`


## Log Deep Analysis & Fixes (04-May-2026)

### Source
- `log.txt` (467 KB, 3444 lines) covering 15-Apr-26 through 03-May-26.

### Error breakdown (verified counts)
| Pattern | Count | Root cause | Status |
|---|---|---|---|
| `not enough rights to send photos to the chat` | 66 | Bot API `sendPhoto`/`editMessageMedia` raising generic `RuntimeError`; `playback.py` fallback re-tries photo instead of text-only | **Fixed** |
| `DuplicateKeyError` on `Anon.users` | 14 | Race condition in `mongo.py` `add_user()` between `is_user()` check and `insert_one()` | **Fixed** |
| `Bad Request: not Found` | 10 | `_is_stale_edit_error()` missing `"not found"` substring; stale edit treated as fatal | **Fixed** |
| `Forbidden: bot was kicked` | 2 | Background tasks (`update_timer`, `vc_watcher`) editing markup after bot was kicked; no catch for kicked/rights errors | **Fixed** |
| `MESSAGE_ID_INVALID` | 4 | Pyrogram path in `utils.edit_reply_markup()` missing `errors.MessageIdInvalid` catch | **Fixed** |
| `MEDIA_EMPTY` | 1 | Already handled in `ping.py`; `playback.py` catches via generic fallback | No change needed |
| `wrong file identifier/HTTP URL` | 2 | Already handled in `start.py` with DB clear | No change needed |
| Rate-limit warnings | 2 | Bot API `_request` already retries 429s | No change needed |

### Files modified
1. **`AnonX_3/core/bot_api.py`**
   - Added `BotAPI.ChatForbidden(RuntimeError)` exception class.
   - Added `_is_chat_forbidden_error()` to detect `"not enough rights"`, `"bot was kicked"`, `"forbidden"`, `"chat not found"`, `"user is deactivated"`, `"group chat was upgraded"`.
   - Updated `_is_stale_edit_error()` to include `"not found"`.
   - `_request()` and `_request_form()` now raise `ChatForbidden` instead of generic `RuntimeError` for permission/rights errors.

2. **`AnonX_3/core/lang.py`**
   - Imported `bot_api`.
   - `@lang.language()` decorator now catches `bot_api.ChatForbidden` alongside Pyrogram `Forbidden`/`ChatWriteForbidden`, preventing unexpected handler crashes.

3. **`AnonX_3/core/playback.py`**
   - `_send_now_playing()` now explicitly catches `ChatSendPhotosForbidden`, `ChatSendMediaForbidden`, and `bot_api.ChatForbidden`, falling back to `utils.send_message()` (text-only with entities/keyboard) instead of retrying photo.
   - Generic fallback also catches photo-forbidden errors via `app.send_photo` → `app.send_message`.
   - `update_now_playing()` explicit exception tuple now includes `bot_api.ChatForbidden`.

4. **`AnonX_3/core/mongo.py`**
   - Imported `pymongo.errors.DuplicateKeyError`.
   - `add_user()`: wrapped `usersdb.insert_one()` in `try/except DuplicateKeyError`.
   - `add_chat()`: wrapped `chatsdb.insert_one()` in `try/except DuplicateKeyError`.

5. **`AnonX_3/helpers/_utilities.py`**
   - Imported `pyrogram.errors`.
   - `edit_reply_markup()`: Bot API path catches `bot_api.ChatForbidden` and returns `None` when `ignore_stale=True`.
   - `edit_reply_markup()`: Pyrogram path (`app.edit_message_reply_markup`) catches `errors.MessageIdInvalid`, `errors.Forbidden`, `errors.ChatWriteForbidden` and returns `None` when `ignore_stale=True`.

6. **`AnonX_3/plugins/misc.py`**
   - Imported `bot_api`.
   - `vc_watcher()` exception catch expanded to `(errors.MessageIdInvalid, errors.Forbidden, errors.ChatWriteForbidden, bot_api.ChatForbidden)` so kicked-chat scenarios do not crash the watcher loop.

### Behavioral impact
- Background timer edits (`update_timer`) that hit kicked/rights errors will now return `None`, trigger `_clear_active_message_id()`, and stop retrying instead of spamming logs every 7 seconds.
- Now-playing cards in chats where photo sending is disabled will now fall back to text-only messages instead of crashing the stream start flow.
- Concurrent `/start` PM users no longer cause `DuplicateKeyError` spikes.
- Stale `editMessageReplyMarkup` calls with `"not Found"` are now silently dropped when `ignore_stale=True`.

### Verification status
- No runtime test executed (Python not available in current Windows shell).
- Static review confirms all modified files have balanced parentheses and consistent indentation.
- Recommended live verification:
  1. Start bot, join a group where bot lacks photo-send rights, `/play` a track → expect text-only now-playing message instead of exception.
  2. Start bot, `/start` from two terminals simultaneously with a new user_id → expect no `DuplicateKeyError`.
  3. Add bot to group, start playback, kick bot → expect `vc_watcher`/`update_timer` to stop editing without traceback.

## Broadcast Group Auto-Discovery Fix (04-May-2026)

### Problem
- Bot accounts cannot call `get_dialogs()` in Pyrogram (returns `BOT_METHOD_INVALID`).
- `_sync_dialog_groups_to_db()` in `broadcast.py` silently failed, so only groups where `/start` was explicitly run were saved to MongoDB.
- Many groups where the bot was a member never received broadcast messages.

### Root cause
- `add_chat()` was only triggered by:
  1. `broadcast.py` `_sync_dialog_groups_to_db()` (bot `get_dialogs()` → always fails)
  2. `misc.py` `audience_sync()` (same bot `get_dialogs()` → fails, then loop skips forever)
  3. `start.py` when `/start` is used in a group
- No passive discovery mechanism existed for groups where the bot was added but no command was used.

### Solution
Three-layer discovery implemented:

1. **Passive message tracker** (`broadcast.py`):
   - `@app.on_message(filters.group, group=999)` → `_track_group_chat()`
   - Any message in any group where the bot is present now triggers `db.add_chat(chat_id)` if not already known.
   - Uses `db.is_chat()` (in-memory list) first to avoid redundant DB writes.

2. **Userbot dialog fallback in broadcast sync** (`broadcast.py`):
   - `_sync_dialog_groups_to_db()` now tries `app.get_dialogs()` first.
   - On `BOT_METHOD_INVALID`, falls back to scanning `userbot.clients` dialogs.
   - For each userbot-found group, verifies main bot membership via `app.get_chat_member(chat_id, app.id)` before saving.
   - Prevents saving groups where only the assistant userbot is present.

3. **Userbot dialog fallback in periodic sync** (`misc.py`):
   - `audience_sync()` no longer skips forever after detecting `BOT_METHOD_INVALID`.
   - Every 10 minutes, it now scans userbot dialogs as fallback and verifies bot membership before saving.
   - Private users also synced via userbot fallback.

### Files modified
- **`AnonX_3/plugins/broadcast.py`**
  - Imported `logger, userbot`.
  - Replaced `_sync_dialog_groups_to_db()` with bot-dialog + userbot-fallback logic.
  - Replaced `_sync_dialog_users_to_db()` with bot-dialog + userbot-fallback logic.
  - Added `_track_group_chat()` global handler at bottom.

- **`AnonX_3/plugins/misc.py`**
  - Replaced `audience_sync()` with bot-dialog + userbot-fallback logic.
  - Removed the `if dialogs_unsupported: continue` early-exit that caused the loop to do nothing after first failure.

### Admin rights requirement
- **No admin rights required** for discovery.
- The bot only needs to be a normal member of the group.
- `get_chat_member(chat_id, app.id)` works for bots even without admin rights (it just returns the bot's own membership status).

### Verification status
- No runtime test executed (Python not available in current Windows shell).
- Static review confirms balanced parentheses and correct imports.
- Recommended live verification:
  1. Add bot to a new group (do NOT run any command).
  2. Wait for any member to send a normal message.
  3. Run `/broadcast` with a reply → the new group should now appear in the broadcast list.
  4. Alternatively, start playback in the group (assistant userbot joins) → wait 10 minutes for `audience_sync()` → check if group appears in broadcast list.

## DM User Auto-Discovery & Re-activation (04-May-2026)

### Problem
- User asked whether a previously blocked user who unblocks the bot and sends `/start` after restart is auto-detected.
- Pre-existing `/start` handler already re-activated blocked users via `db.add_user()` → `touch_audience(blocked=False, is_active=True)`.
- However, if the user unblocked and sent a normal message **without** `/start`, there was no passive discovery mechanism.

### Fix
- Added `@app.on_message(filters.private, group=999)` `_track_private_user()` to `broadcast.py`.
- Any private message (command or plain text) now triggers `db.add_user(user_id)`.
- `MongoDB.add_user()` calls `touch_audience(blocked=False, is_active=True)` **unconditionally**, so previously blocked users are automatically re-activated on any interaction.

### Behavior summary
| Scenario | Before fix | After fix |
|---|---|---|
| User blocks bot, later unblocks and sends `/start` | Re-activated ✅ (already worked) | Re-activated ✅ |
| User blocks bot, later unblocks and sends plain text | Not detected ❌ | Auto-detected & re-activated ✅ |
| New user messages bot without `/start` | Not in broadcast list ❌ | Auto-detected ✅ |
| Bot restart + existing user sends any message | No state update | `last_seen_ts` + `is_active=True` updated |

### Files modified
- **`AnonX_3/plugins/broadcast.py`**: appended `_track_private_user()` handler.

### Verification status
- Static review confirms bracket balance.
- Recommended live verification: block bot from a test account, unblock, send plain text (no command), then run `/broadcast -user` → message should arrive.

## MongoDB Chat/User Cache Performance Fix (04-May-2026)

### Problem
- `MongoDB.chats` and `MongoDB.users` were stored as Python `list`.
- `is_chat()` and `is_user()` used `chat_id in self.chats` which is **O(n)**.
- With thousands of groups/users, every passive tracker call (`_track_group_chat`, `_track_private_user`) and every `add_chat`/`add_user` call performed a linear scan.

### Fix
Converted in-memory caches from `list` to `set` in `AnonX_3/core/mongo.py`:

| Method | Before | After |
|---|---|---|
| `__init__` | `self.chats = []` | `self.chats = set()` |
| `__init__` | `self.users = []` | `self.users = set()` |
| `add_chat` | `self.chats.append(chat_id)` | `self.chats.add(chat_id)` |
| `rm_chat` | `self.chats.remove(chat_id)` | `self.chats.discard(chat_id)` |
| `get_chats` | `self.chats.extend(...)` | `self.chats.update(...)` |
| `get_chats` | `return self.chats` | `return list(self.chats)` |
| `add_user` | `self.users.append(user_id)` | `self.users.add(user_id)` |
| `rm_user` | `self.users.remove(user_id)` | `self.users.discard(user_id)` |
| `get_users` | `self.users.extend(...)` | `self.users.update(...)` |
| `get_users` | `return self.users` | `return list(self.users)` |

- `discard` used instead of `remove` to avoid `KeyError` on race conditions.
- `list(self.chats)` preserves the return type contract (`-> list`) for existing callers.

### Impact
- `is_chat()` / `is_user()` complexity: **O(n) → O(1)**
- Passive tracker overhead per message: **microseconds** regardless of scale.

### Files modified
- **`AnonX_3/core/mongo.py`**

### Verification status
- Bracket balance confirmed across all 7 modified files.
- Return type contract preserved (`list` returned from `get_chats`/`get_users`).
- `discard` prevents `KeyError` if duplicate removal occurs.

## Privacy Hardening: Removed Userbot Private-Chat Fallback (04-May-2026)

### Issue discovered during deep check
- Userbot dialog fallback in `_sync_dialog_users_to_db()` and `audience_sync()` scanned ALL private chats from assistant userbot dialogs.
- Assistant userbots may have personal private conversations unrelated to the main bot.
- Adding those users to the broadcast list would be a privacy/spam violation.

### Fix
- Removed userbot private-chat fallback from `broadcast.py` `_sync_dialog_users_to_db()`.
- Removed userbot private-chat fallback from `misc.py` `audience_sync()`.
- Userbot dialog fallback is now **group-only**.
- Private user discovery now relies on:
  1. Bot `get_dialogs()` (if ever supported)
  2. `_track_private_user` passive tracker (`filters.private`)
  3. `/start` command

### Files modified
- **`AnonX_3/plugins/broadcast.py`**
- **`AnonX_3/plugins/misc.py`**

### Verification status
- Bracket balance re-confirmed across all 7 modified files.
- Group-only userbot fallback still verifies bot membership via `get_chat_member`.
- Rate-limit sleep (`0.05s`) remains active on group verification path.

## Log1.txt Deep Analysis & FloodWait Fix (04-May-2026)

### Source
- `log1.txt` (601 KB, newer log covering 03-May-26 through 04-May-26).

### Error breakdown (compared to older `log.txt`)
| Pattern | Old log | New log | Status |
|---|---|---|---|
| `not enough rights to send photos` | 66 | 66 | **Fixed in code** (bot running old build) |
| `DuplicateKeyError` users | 14 | 26 | **Fixed in code** (bot running old build) |
| `DuplicateKeyError` chats | 0 | 1 | **Fixed in code** (bot running old build) |
| `Bad Request: not Found` | 10 | 11 | **Fixed in code** (bot running old build) |
| `bot was kicked` | 2 | 2 | **Fixed in code** (bot running old build) |
| `MEDIA_EMPTY` | 1 | 1 | Already handled |
| `wrong file identifier` | 2 | 2 | Already handled |
| `FloodWait phone.JoinGroupCall` | 0 | 3 | **NEW — Fixed** |
| `Group call join timed out` | — | 1 | Already handled by `TimeoutError` catch |
| `message was deleted` | — | 1 | **Fixed in code** (stale edit detection) |

### New error discovered: FloodWait from phone.JoinGroupCall
- **Count**: 3 occurrences
- **Source**: `pyrogram.errors.exceptions.flood_420.FloodWait` caused by `"phone.JoinGroupCall"`
- **Traceback path**: `play.py` → `_play.py` `stream_media()` → `core/calls.py` `play_media()` → `client.play()` → PyTgCalls → Pyrogram `phone.JoinGroupCall`
- **Impact**: Exception propagated uncaught through dispatcher → "Unexpected exception raised in MessageHandler" crash
- **Root cause**: `core/calls.py` `play_media()` caught `TimeoutError`, `NoActiveGroupCall`, `NoAudioSourceFound`, `ConnectionError`, `RTMPStreamingUnsupported`, but **not** `errors.FloodWait`.

### Fix applied
**File: `AnonX_3/core/calls.py`**
- Imported `errors` from `pyrogram`.
- Added `except errors.FloodWait as fw:` block in `play_media()`:
  - Logs flood wait duration and chat_id.
  - Calls `self.stop(chat_id)`.
  - Sends `_lang["error_tg_server"]` error text to the chat (with generic try/except to suppress notification failures).

### "Unexpected exception" breakdown (22 total)
| Source | Count | Cause | Fix status |
|---|---|---|---|
| `start.py` `add_user()` | ~12 | `DuplicateKeyError` race condition | ✅ Fixed in `mongo.py` |
| `queue.py` `edit_media()` | ~6-7 | `RuntimeError: not enough rights` | ✅ Fixed in `bot_api.py` |
| `_play.py` `stream_media()` | ~3 | `FloodWait phone.JoinGroupCall` | ✅ Fixed in `calls.py` |

### Files modified in this pass
- **`AnonX_3/core/calls.py`**

### Total modified files across entire session
1. `AnonX_3/core/bot_api.py`
2. `AnonX_3/core/lang.py`
3. `AnonX_3/core/playback.py`
4. `AnonX_3/core/mongo.py`
5. `AnonX_3/core/calls.py`
6. `AnonX_3/helpers/_utilities.py`
7. `AnonX_3/plugins/misc.py`
8. `AnonX_3/plugins/broadcast.py`

### Verification status
- Bracket balance confirmed across all 8 modified files.
- `errors.FloodWait` catch placed before `RTMPStreamingUnsupported` and after `ConnectionError` catch block.
- `CancelledError` not swallowed (`BaseException` on Python 3.8+).








## Recent Changes (24-May-2026)
- `/vplay` YouTube fallback hardening: when initial direct-stream resolution and local download both fail, `AnonX_3/core/calls.py` now performs a second direct-stream resolution attempt before returning `error_no_file`.
- `AnonX_3/core/youtube.py::resolve_direct_stream()` now includes an additional permissive progressive format fallback (`best[acodec!=none][vcodec!=none]/best`) with and without cookies, improving direct URL extraction coverage for videos where strict filtered formats are unavailable.
- Expected behavior impact: fewer false `Download failed` responses for `/vplay` on YouTube sources when strict format matching fails.
- Validation tier: Tier B (static verification only; runtime Telegram playback test pending).
## Recent Changes (24-May-2026)
- Local `.env` tuned for stability-first YouTube playback: `YOUTUBE_DIRECT_STREAM=False` so `/vplay` now uses download-first path instead of starting with direct remote stream.
- Other YouTube direct flags were kept unchanged for easy rollback/testing.
- Validation tier: Tier B (config update only; runtime playback verification pending).
## Recent Changes (24-May-2026)
- Local `.env` switched back to hybrid auto YouTube mode: `YOUTUBE_DIRECT_STREAM=True` (with existing `YOUTUBE_DIRECT_STREAM_ONLY=False`) so runtime can attempt direct stream first and then fallback to local cache/download automatically.
- Intent: maximize `/vplay` recovery path (direct + cache/download fallback) instead of download-only mode.
- Validation tier: Tier B (config update only; runtime verification pending).

## Recent Changes (24-May-2026)
- Added TikTok URL support for /song and /vsong in AnonX_3/plugins/song.py via new AnonX_3/core/tiktok.py extractor/downloader (yt-dlp based).
- /song flow now auto-detects TikTok links, resolves metadata (title/duration/uploader/thumbnail), downloads media, and reuses existing upload + conversion pipeline.
- /vsong TikTok uploads now reuse existing thumbnail generation pipeline (thumb.generate + thumb.generate_video_thumb), with track.file_path set so frame-based card generation works like Telegram/YouTube video flows.
- Wired TikTok singleton in AnonX_3/__init__.py as tiktok for plugin reuse.
- Validation tier: Tier B (static verification only; runtime Telegram tests pending due to missing local Python runtime).

## Recent Changes (24-May-2026)
- Added TikTok URL support to /play and /vplay pipeline in AnonX_3/plugins/play.py.
- Non-reply TikTok URLs now route through core TikTok resolver/downloader, then stream as local media (file.file_path assigned before anon.play_media).
- Updated AnonX_3/helpers/_play.py URL classification so TikTok links are not misclassified as m3u8 and not rejected by YouTube-invalid guard.
- /vplay TikTok now reuses existing now-playing thumbnail generation path (same behavior family as YouTube/Telegram local media cards).
- Validation tier: Tier B (static verification only; runtime Telegram VC test pending).
## Recent Changes (24-May-2026)
- Added Telegram message-link support for /play and /vplay: bot now accepts t.me message URLs (public and /c/ style), fetches linked media, and streams it via existing Telegram media pipeline.
- Added Telegram message-link support for /song and /vsong: linked Telegram audio/video can now be downloaded and uploaded through existing song/vsong flow.
- Implemented Telegram link parser/resolver in AnonX_3/core/telegram.py (is_message_link + fetch_linked_message).
- Preserved existing thumbnail generation behavior: YouTube/TikTok/Telegram video flows continue using shared thumb.generate and thumb.generate_video_thumb paths.
- Validation tier: Tier B (static verification only; runtime Telegram tests pending).

## Recent Changes (24-May-2026)
- YouTube remote failure resilience improved for /vplay in `AnonX_3/core/calls.py`: when direct remote stream fails, local cache/download fallback is now forced even if `YOUTUBE_DIRECT_STREAM_ONLY=True`.
- Added multi-tier fallback download retry in `AnonX_3/core/prefetch.py::await_current_cache_or_download()` for video sources (`None -> normal -> poor -> good`) to reduce `Download failed` after remote-stream errors.
- Expected impact: fewer hard-stop `YouTube remote stream failed` + `Download failed` paths; bot should continue by auto cache/download more often.
- Validation tier: Tier B (static patch verification only; runtime Telegram VC test pending).

## Thumbnail Card Runtime Fix (2026-07-09)
- turn_id: 2026-07-09T13:27:27+06:30-thumbnail-card-fix
- Affected file: `AnonX_3/helpers/_thumbnails.py`.
- Fixed thumbnail card fallback masking by logging source/card generation exceptions.
- Added remote thumbnail validation: HTTP status is checked, non-image `Content-Type` is rejected, and downloaded payloads are verified by Pillow before card rendering.
- Expected impact: `/play` and `/vplay` now-playing cards should use the actual track/video thumbnail when available; only genuinely bad/missing thumbnail sources fall back to the configured default image.
- Validation: `python -m py_compile AnonX_3\helpers\_thumbnails.py AnonX_3\core\playback.py AnonX_3\plugins\play.py` passed. Runtime Telegram visual test still pending. Confidence: 96%, tier: A static.

## Thumbnail Bot Name Visual Adjustment (2026-07-09)
- turn_id: 2026-07-09T13:34:03+06:30-thumb-name-background-remove
- Affected file: `AnonX_3/helpers/_thumbnails.py`.
- Removed the `draw.rounded_rectangle(...)` background behind the top-left `THUMB_BOT_NAME` label. The label now renders directly over the blurred card background with its existing shadow.
- Validation: `python -m py_compile AnonX_3\helpers\_thumbnails.py` passed. Confidence: 99%, tier: A static.

## Thumbnail Bot Name Cache Bust (2026-07-09)
- turn_id: 2026-07-09T13:38:43+06:30-thumb-name-cache-bust
- Affected file: `AnonX_3/helpers/_thumbnails.py`.
- Updated `_thumb_signature()` with a no-background style marker and moved generated thumbnail filenames from `v10` to `v11`.
- Expected impact: old cached card files that still contain the gray rounded `THUMB_BOT_NAME` background are bypassed; newly generated `/play` and video-thumb cards render the bot name without a text backing.
- Validation: `python -m py_compile AnonX_3\helpers\_thumbnails.py` passed. Confidence: 98%, tier: A static.

## Thumbnail Bot Name Emoji Upgrade (2026-07-09)
- turn_id: 2026-07-09T13:43:38+06:30-thumb-name-emoji-upgrade
- Affected file: `AnonX_3/helpers/_thumbnails.py`.
- Added sequence-aware emoji segmentation for `THUMB_BOT_NAME`, covering flags, skin-tone modifiers, keycaps, variation selectors, and ZWJ emoji sequences while preserving mixed Myanmar/Latin text runs.
- Added a defensive draw fallback when color-emoji rendering fails on a host font, so thumbnail generation continues instead of crashing.
- Updated thumbnail cache signature marker and generated filenames to `v12` so old non-emoji-aware card files are bypassed.
- Validation: `python -m py_compile AnonX_3\helpers\_thumbnails.py` passed. Confidence: 97%, tier: A static.

## Thumbnail Bot Name Config Update (2026-07-09)
- turn_id: 2026-07-09T13:47:36+06:30-thumb-name-env-update
- Affected file: local `.env` only.
- Set `THUMB_BOT_NAME` to `မင်္ဂလာပါ မြန်မာ 🇲🇲🎧`.
- Expected impact: newly generated now-playing thumbnails use the requested Myanmar text plus flag/headphone emoji, with existing no-background styling and `v12` emoji-aware cache behavior.
- Validation: exact `.env` match count was 1; `python -m py_compile AnonX_3\helpers\_thumbnails.py config.py` passed. Confidence: 99%, tier: A static.

## Project Copy To AnonX_3 (2026-07-09)
- turn_id: 2026-07-09T13:55:30+06:30-copy-AnonX_3-to-AnonX_3
- Created sibling project copy at `C:\Users\HP\Downloads\New folder 2\AnonX_3`.
- In the copied project only, renamed package directory to `AnonX_3\AnonX_3`, updated startup commands (`start`, docs) to `python -m AnonX_3`, and rewrote source imports/references to `AnonX_3`.
- Copied runtime-sensitive local files remain in the duplicate, including `.env`; secrets were not printed in logs or response summaries.
- Validation: no remaining plain `AnonX_3` references matched `rg -P "AnonX_3(?!_\d)"` outside skipped binary/runtime/git paths; `python -m compileall -q AnonX_3 config.py` passed. Confidence: 97%, tier: A static.
- 24-May-2026: Implemented YouTube API + cookie parallel metadata flow with dynamic runtime key reload. Added `YOUTUBE_API_KEY` and `YOUTUBE_API_RELOAD_SEC` in `config.py`, `sample.env`, and `app.json`; `AnonX_3/core/youtube.py` now tries YouTube Data API first for search/playlist metadata and silently falls back to existing `py-yt-search`/`Playlist` paths on API errors (`invalid_key`, `quota_exceeded`, `network_error`, `unexpected`) while keeping yt-dlp+cookie media extraction behavior unchanged (confidence: 95%).
- 24-May-2026: Enabled all-source direct-first video startup with local-download fallback/cache behavior. YouTube direct-first remains available via env; Telegram direct reply-video path remains active; TikTok now supports `tiktok_remote` direct startup + background cache + local fallback on timeout/connection/no-audio errors. Added new env/config keys `TIKTOK_DIRECT_STREAM`, `TIKTOK_DIRECT_STREAM_ONLY`, `TIKTOK_DIRECT_CACHE_BG`, `TIKTOK_DIRECT_CACHE_TIMEOUT_SEC`; updated `config.py`, `sample.env`, `app.json`, `.env`, `AnonX_3/core/tiktok.py`, `AnonX_3/plugins/play.py`, and `AnonX_3/core/calls.py` (confidence: 93%).
- 24-May-2026: Fixed `/vplay` video-missing regression by enforcing video-mode continuity in autoplay and hardening YouTube video fallback formats against audio-only resolution. `AnonX_3/core/calls.py::play_next()` now forces `track.video` to inherit ended track mode; `AnonX_3/core/youtube.py::autoplay_track()` now forces selected track `video` from seed; permissive/retry video fallback formats removed audio-only `/best` tail and now keep video-required selectors (confidence: 95%).
- 29-May-2026: **Auto-reply crash fix — legacy/custom emoji entity handling**: `AnonX_3/plugins/auto_reply.py` now uses shared `utils.serialize_entities()` / `utils.deserialize_entities()` / `utils.reply_text()` instead of local enum casting, and auto-normalizes saved reply rules before sending. `AnonX_3/helpers/_utilities.py` `normalize_entity_type()` now accepts legacy raw-class strings like `"<class 'pyrogram.raw.types.message_entity_custom_emoji.MessageEntityCustomEmoji'>"`, so previously saved premium/custom emoji replies no longer crash with `ValueError ... is not a valid MessageEntityType` and are rewritten into normalized entity dictionaries on first use (confidence: 99%, tier: B).

- 31-May-2026: **Deep fix — `/setbt` + `/settext` premium emoji preservation in `AnonX_3`**: `plugins/restart.py` no longer stores replied text after `strip()` when saving `/settext`, `/setwelcome`, and `/setbt` overrides; validation still checks non-empty text, but original text/entities are now preserved so UTF-16 entity offsets stay aligned and premium/custom emoji entities do not break on resend. `helpers/_inline.py` also stopped stripping emoji characters from custom `/setbt` labels at render time, while still keeping `icon_custom_emoji_id` support. Expected impact: premium emojis survive both saved runtime text templates and custom button text overrides instead of degrading or disappearing. Validation: `py_compile` passed for touched `plugins/restart.py` and `helpers/_inline.py`. Confidence: 96%, tier: A.
- 31-May-2026: **Deep fix — `/start` button emoji/custom icon preservation in `AnonX_3`**: `plugins/start.py` no longer force-converts `/start` keyboards directly through `buttons.to_pyrogram_markup()`. It now delegates to `utils.maybe_convert_bot_api_markup()`, so keyboards that use Bot API-only button fields like `icon_custom_emoji_id` keep the Bot API path instead of silently dropping button icons during the `/start` premium-emoji workaround. Expected impact: `/setbt`-driven `/start` buttons keep their original Telegram/custom button icons while text-side custom emoji handling still follows the shared safe conversion rules. Validation: `py_compile` passed for touched `plugins/start.py`. Confidence: 95%, tier: A.
- 31-May-2026: **UI polish — remove normal Unicode emojis when `/setbt` already uses a Telegram/custom button icon in `AnonX_3`**: `helpers/_inline.py` now strips standard Unicode emojis from override text only when `icon_custom_emoji_id` is present, preventing duplicated emoji presentation like `📌 Add me...` beside a Telegram/custom icon. Plain text overrides without a custom icon are unchanged. Validation: `py_compile` passed for touched `helpers/_inline.py`. Confidence: 97%, tier: A.

## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX_3 and AnonX_3 .env).
- 05-Jun-2026: **YouTube DNS log amplification fix**: `core/youtube.py` now breaks API retry loops on `network_error` and filters `py_yt.core.requests` internal ERROR tracebacks while preserving bot warnings and yt-dlp fallback. Source log error was external DNS failure (`Temporary failure in name resolution`). Validation: `py_compile` passed. Confidence: 99%, tier: A static.
## Deep Scan Update (2026-07-09)
- Active project root: `AnonX_3/` inside the extracted workspace; sibling `__MACOSX/` is non-project archive residue.
- Git state: repository present and readable, latest reachable commit `1ecf118` (`Typo`), but branch-name lookup is currently unhealthy (`git branch --show-current` fails).
- Code surface observed: 29 plugin modules under `AnonX_3/plugins/` and 13 locale JSON files under `AnonX_3/locales/`.
- Deployment/runtime assets confirmed: `.env`, `Dockerfile`, `Procfile`, `heroku.yml`, `app.json`, `start`, `setup`, `ops/` scripts, `cache/`, and `downloads/`.
- Documentation debt confirmed: root docs exist but are partially polluted by copied history from other variants (`AnonX_3`, multi-variant notes, truncated logs). Future cleanup should separate current-state facts from archival changelog content.

## Import Update (2026-07-09)
- `AnonX_3` feature surface partially merged into `AnonX_3` at code level.
- New files added: `AnonX_3/core/error_monitor.py`, `AnonX_3/plugins/name_checker.py`, `AnonX_3/plugins/bot_assistant.py`.
- Existing files integrated: `AnonX_3/__main__.py`, `AnonX_3/core/mongo.py`, `AnonX_3/core/bot.py`, `AnonX_3/plugins/restart.py`, `AnonX_3/plugins/stop.py`, `config.py`, `AnonX_3/locales/en.json`, `AnonX_3/locales/my.json`.
- Validation status: static compile checks passed; runtime behavior still depends on actual Telegram/Mongo/optional DeepSeek environment.

## Logging Update (2026-07-09)
- `AnonX_3/__init__.py` logging now uses a timezone-aware formatter instead of raw server localtime.
- Effective log timezone source: env `ACTIVEVC_TIMEZONE`, default `Asia/Yangon`.

## Name Checker Compare (2026-07-09)
- `AnonX_3` name-check implementation was reviewed against current `AnonX_3`.
- Outcome: no delta worth importing; current `AnonX_3` already contains the same name-check behavior.

## Name Checker Text Import (2026-07-09)
- Imported the remaining `AnonX_3` runtime text layer for the name-check feature into `AnonX_3/plugins/restart.py`.
- Added default `name_checker` template text, runtime text config entry, resettable-key registration, and direct-fallback support for `/settext`, `/gettext`, and `/resettext`.
- Validation: `python -m py_compile AnonX_3/plugins/restart.py` passed.

## Log Path Deep Fix (2026-07-09)
- Unified runtime log-file resolution across `AnonX_3/__init__.py`, `AnonX_3/__main__.py`, `plugins/restart.py`, and `plugins/bot_assistant.py`.
- Added canonical `LOG_FILE_PATH`, startup file auto-create, handler flushing before `/logs`, and shared path use for assistant real-time error scans.
- Expected impact: `log.txt` stays real-time and `/logs` sends the same live file instead of a stale/mismatched path when cwd/layout changes.
- Validation: `python -m py_compile AnonX_3/__init__.py AnonX_3/__main__.py AnonX_3/plugins/restart.py AnonX_3/plugins/bot_assistant.py` passed.

## Silent Direct Fallback Fix (2026-07-09)
- Adjusted `helpers/_play.py` and `core/calls.py` so YouTube/TikTok direct-stream candidates do not show the initial `play_downloading` message before a direct attempt.
- Removed chat-side `Stream failed (...) Switching to auto download...` notices for TikTok direct fallback and suppressed progress-message edits during silent local fallback for direct-start candidates.
- Expected impact: when direct stream fails, fallback auto-download continues quietly and users mainly see the normal end result instead of transient failure/download messages.
- Validation: `python -m py_compile AnonX_3/core/calls.py AnonX_3/helpers/_play.py` passed.

## Log-Driven Name Checker Fix (2026-07-09)
- Reviewed workspace `log.txt` and isolated the current repeating runtime crash to `plugins/name_checker.py` premium-emoji handling.
- Added missing `Utilities.is_premium_emoji_char()` and `Utilities.has_premium_emoji()` helpers in `AnonX_3/helpers/_utilities.py` so imported name-checker code matches the expected helper API.
- Expected impact: name-check alerts no longer crash on users whose display names contain Telegram premium/custom-emoji placeholder characters.
- Validation: `python -m py_compile AnonX_3/helpers/_utilities.py AnonX_3/plugins/name_checker.py` passed.

## Empty `/logs` Fix (2026-07-09)
- Updated `plugins/restart.py` so an existing-but-empty `log.txt` no longer returns only `Log file is empty.`.
- Added `write_log_snapshot_marker()` in `AnonX_3/__init__.py`; `/logs` now seeds an empty file with a timestamped info line and sends that fresh log document.
- Validation: `python -m py_compile AnonX_3/__init__.py AnonX_3/plugins/restart.py` passed.

## Startgroup Weighted Rotation Fix (2026-07-09)
- Updated `AnonX_3/helpers/_inline.py` so `STARTGROUP_BOTS` / `STARTGROUP_WEIGHTS` no longer rely on request-time random selection.
- Configured startgroup links now build a weighted rotation sequence; `40,30,30` reduces to an exact `4,3,3` cycle (shuffled once per process, then rotated deterministically).
- Expected impact: when multiple add-me bots are configured, `@mingalapar_music_bot` no longer sticks at 100% merely because fallback/random selection keeps favoring the current bot username.
- Validation: `python -m py_compile AnonX_3/helpers/_inline.py` passed.

## Thumbnail Card Fallback Fix (2026-07-09)
- turn_id: 2026-07-09-thumbnail-card-fallback
- User-reported issue: now-playing thumbnail should look like the generated 1280x720 music card, but Telegram showed a raw/default photo above the streaming controls.
- Root cause: `core/playback.py` and `plugins/queue.py` could bypass `thumb.generate()` and send `default_thumb` directly when media metadata did not satisfy the previous `can_generate_card` gate. Generator fallback paths could also fail on unsafe media IDs or unusable thumbnail sources.
- Code changes: now-playing and queue thumbnails always call `thumb.generate()` when `THUMB_GEN=True`; `_thumbnails.py` now sanitizes cache filenames and tries track thumbnail, DB default thumb, local configured default image, then a generated placeholder before rendering the card.
- Related runtime hardening: `AnonX_3/__init__.py` timezone formatter now falls back to stdlib UTC when `zoneinfo` data is missing.
- Validation tier: A static syntax check passed with `python -m py_compile AnonX_3\__init__.py AnonX_3\helpers\_thumbnails.py AnonX_3\core\playback.py AnonX_3\plugins\queue.py`. Full Telegram visual runtime check remains pending because local Python dependencies are not installed.

## Log-Driven Runtime Warning Fixes (2026-07-23)
- Deep-scanned the supplied runtime `log.txt` and grouped repeated symptoms without persisting user/chat IDs or other log-sensitive values.
- `core/youtube.py` now passes `proxy` to `py_yt.VideosSearch` only when the installed callable supports it, eliminating the repeated version-compatibility `unexpected keyword argument 'proxy'` fallback warning.
- Download waiters now recover the CDN-promoted ready path when a concurrent publish moves the original `downloads/` file.
- `plugins/misc.py` now holds audience synchronization behind bot-and-assistant readiness, preventing import-before-start ordering from calling Telegram on an unstarted client. The expected bot `get_dialogs` limitation is informational.
- The self-hosted Downloader API is explicitly opt-in (`DOWNLOADER_API_ENABLED=False` by default); core bot deployments no longer require its optional FastAPI stack.
- Validation: `compileall` passed, smoke suite `17/17` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. Live Telegram/VPS restart verification remains required.

## First-Play Latency Deep Fix (2026-07-23)
- Video evidence showed roughly 18 seconds from `/play` submission to Started; supplied traces showed 24.5–40.0 second totals, with duplicated search/direct resolution and failed direct audio followed by local-download waits.
- YouTube audio direct resolution now uses the M4A/AAC-compatible format ladder, shares one audio inflight key across warm/play calls, keeps the provider race alive through the yt-dlp deadline, and uses a four-second extract socket timeout.
- Removed the duplicate raw yt-dlp warm search, blocking logger/status-button work, and duplicate local-fallback joins. Thumbnail rendering now overlaps resolver/join work.
- Follow-up converted startup sequencing to readiness events: search completion drives `Downloading`, direct/local task completion drives playback selection, and `client.play()` completion drives voice-ready. Startup gate/join timers and local polling windows were removed.
- Validation: `compileall` passed, smoke suite `18/18` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. Live VPS timing remains required because provider/network latency is external.

## Deterministic `/logs` Dispatch Fix (2026-07-23)
- The plural `/logs` command could be silently consumed by the broad group-text filter because both handlers were registered in Pyrogram group 0.
- Plugin discovery used `frozenset(sorted(...))`, which discarded the sorted iteration order and made the collision vary between process restarts.
- Plugin modules now load from a deterministic sorted tuple; the broad text-filter observer runs in group 25, while `/logs` runs in group -1 and performs its own owner/sudo database authorization.
- The singular `/log` remains unrelated and may be handled by another group-management bot; the supported runtime-log command is `/logs`.
- Validation: `compileall` passed, smoke suite `18/18` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. A bot restart is required to register the new handler groups.

## All Sudo Commands Early-Dispatch Fix (2026-07-23)
- Extended the `/logs` dispatch correction to every plugin handler protected by `app.sudoers` or `app.owners`.
- Sudo/owner commands now use Pyrogram handler `group=-1`, so default-group text watchers cannot silently consume `/restart`, `/broadcast`, `/logger`, `/gp`, `/activevc`, blacklist, cookie, auto-remove, runtime-text/image, or sudo-management commands.
- Owner-only `/musiclog` and public `/listsudo` were also placed in the same command-first group.
- Added an AST-based regression guard that scans every plugin and fails if a protected command is added without `group=-1`.
- Validation: `compileall` passed, smoke suite `19/19` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. Deployment restart remains required.

## Simple Delete-Only Keyword Filter (2026-07-23)
- Replaced the per-keyword reply-text workflow with direct `/filter <keyword>` registration; `/filtter` is accepted as a typo-compatible alias.
- A matching non-admin group message is deleted immediately, followed by one unquoted warning that auto-deletes after five seconds.
- The warning is globally editable through `/settext filter` (reply to the desired warning message) and resettable with `/settext filter default`.
- Existing MongoDB filter documents remain compatible: stored dictionary keys are preserved while old per-keyword reply values are normalized away on the next save.
- `/filter list`, `/filter remove <keyword>`, and `/filter clear` remain available.
- Validation: `compileall` passed, smoke suite `20/20` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. Restart is required to load the new handlers/template registry.

## Three-Strike Filter Moderation (2026-07-23)
- The shared `filter_warning` default now renders matched keyword, user mention, strike progress, and moderation status through placeholders `{0}`–`{3}`.
- Filter violations are tracked persistently per chat/user in MongoDB with an in-process fallback; the third strike attempts an automatic mute and resets the counter after success.
- Strikes one and two show a danger-style `Mute` button and retain the five-second warning auto-delete. A successful manual or automatic mute keeps a persistent message with a success-style `Unmute` button.
- Callback actions are bound to the source chat, restricted to admins/owner/sudo, and refuse to mute protected users. Unmute restores the chat's default permissions.
- Added customizable `filter_mute` and `filter_unmute` button-text keys through the existing button system.
- Validation: `compileall` passed, smoke suite `21/21` passed, and `ops/secret_scan.py` returned `SECRET SCAN OK`. Live Telegram permission/style verification requires restart.

## Local VPS Cookie Agent (2026-07-24)
- Removed the abandoned API-key/access-service direction; AnonX_3 does not expose `/apikey`, `/plan`, or `/revoke`.
- Added config-driven local browser cookie export at startup, periodically, near expiry, and after YouTube bot-check failures.
- Chromium-family profiles can receive a headless YouTube warmup before export; the profile must already contain a legitimate signed-in session.
- Cookie publication is validated and atomic, failed exports preserve the last valid file, cookie values are not logged, and generated files use owner-only permissions where supported.
- Operational setup and the non-bypass boundary are documented in `docs/local-cookie-agent.md`.
- Follow-up fixed direct/search/download yt-dlp options to preserve `COOKIE_BROWSER_PROFILE`; those paths no longer fall back to the browser's default profile after `cookies.txt` is removed.

## True Cookie-Free Mode (2026-07-24)
- Added `COOKIE_FREE_MODE=True` as the safe default for public-media playback.
- Cookie-free runtime does not inspect browser databases, read/create cookie files, consume `COOKIES_URL`, accept `/addcookie`, start periodic refresh tasks, or attach browser cookies to normal yt-dlp requests.
- Existing full cookie-agent behavior remains available with `COOKIE_FREE_MODE=False`; challenge-only recovery is separately gated by `COOKIE_AUTH_RECOVERY_ENABLED=True` plus an explicit browser/profile.

## Auth-Triggered Chromium Cookie Recovery (2026-07-25)
- Added a profile-gated, challenge-only recovery path that keeps normal public playback cookie-free, then atomically exports a legitimately signed-in Chromium-family profile after YouTube explicitly returns an auth/not-a-robot gate. The feature defaults enabled but remains inert without an explicit browser/profile.
- Direct-stream and local-download recovery share one refresh lock/generation; simultaneous callers reuse one export, and download performs at most one authenticated retry.
- The gate fails closed unless automatic cookies, an explicit supported browser ID, and a non-empty dedicated profile are configured. It never manufactures authentication or automates CAPTCHA.
- Startup logs explicitly distinguish strict cookie-free mode from configured challenge-only recovery; neither mode starts the periodic cookie task.
- Audio download now includes a progressive `best[acodec!=none]` fallback in the primary selector and continues to the permissive strategy on `FORMAT` errors before spending an outer retry.
- Final-attempt correction: Chromium authenticated recovery now owns one dedicated bounded execution slot, so an auth challenge on ordinary attempt `3/3` can still sync and retry immediately instead of refreshing a cookie with no retry budget left.
- 24-Jul-2026: **Action-specific callback banners**: Playback controls no longer show generic `Processing...`. Pause, resume, skip, replay, stop, force-play, and expired-item paths now answer once with their relevant localized status. Settings now show the selected setting plus its resulting ON/OFF state (or localized autoplay result). Also fixed settings play-mode persistence ordering, which previously saved the old value while rendering the toggled value. Validation: compileall passed, smoke suite 24/24 passed, modal scan and secret scan clean. Restart required. Confidence: 98%, tier: A static.

- 24-Jul-2026: **All callback feedback changed to top toast/banner UI**: Removed modal `show_alert=True` behavior from playback controls, settings, language selection, moderation/filter actions, download cancellation, Telegram download callbacks, and shared admin permission feedback. Every callback text now uses non-modal `show_alert=False`, so Telegram shows transient top feedback instead of an OK dialog. Added a repository-wide smoke guard that rejects any future modal callback alert. Validation: compileall passed, smoke suite 24/24 passed, secret scan clean. Restart required. Confidence: 98%, tier: A static.

- 24-Jul-2026: **Voice-chat assistant auto-unmute**: `core/calls.py` now reacts to successful `client.play()` join completion by scheduling a non-blocking active-assistant self-unmute through Telegram's group-call API. If Telegram reports an admin force-mute, the main bot attempts a permission-based fallback against that exact assistant peer only. No fixed delay or playback-path wait was added. Validation: compileall passed, smoke suite 23/23 passed, secret scan clean. Live Telegram permission verification requires restart. Confidence: 94%, tier: B.

- 24-Jul-2026: **Screenshot-style queued-track result**: User queue additions now render `Added To Queue At #N`, linked title, duration, and requester across every bundled locale. The previous force-play button was replaced with a success-style `Close` control that deletes only the card. Queue numbering remains derived from `queue.add()` and is not hardcoded. Validation: compileall passed and smoke suite 25/25 passed. Restart required. Confidence: 98%, tier: A static.

- 24-Jul-2026: **Active-queue UI race correction**: The warm-search callback now detects an active call/current queue item and leaves the status for the canonical `play_queued` renderer instead of overwriting it with `Downloading`. Queue admission also triggers the existing resource-aware, deduplicated next-track prefetch in the background. Idle first-play download UI is unchanged. Validation: compileall passed and smoke suite 25/25 passed. Restart required. Confidence: 98%, tier: A static.
- 24-Jul-2026: **Fast lifecycle-aware auto-learn**: teach-by-reply gates are now config-driven (defaults: 0.75s/user, 2s/keyword, 12 learns/minute). Newly auto-learned group rules track source, learned/used timestamps, and usage count. A supervised bounded cleanup removes only auto-sourced rules inactive for 24 hours, ordered least-used then oldest; manual `/reply` and legacy-unclassified rules are preserved, and `/unreply` removes associated metadata. Validation: compileall + smoke 26/26 + secret scan clean. Restart required. Confidence: 98%, tier: A static/unit.

- 24-Jul-2026: **Prompt-aware optional AI DJ**: Added admin-only `/aidj` with persisted per-group `chill`, `party`, `study`, `workout`, `myanmar`, and `romantic` modes. It reuses the existing strict autoplay selector, recent-track/artist exclusions, queue, resolver, and playback engine; no external AI API or duplicate queue/player was added. Existing autoplay settings now lazy-load from MongoDB after restart, fixing persistence that previously depended only on process memory. Also corrected `ops/verify_structure.py` so it detects genuinely foreign numbered sibling imports instead of flagging every valid `AnonX_3` import. Validation: compileall passed, smoke suite 27/27 passed, structure and secret scans clean. Restart required. Confidence: 98%, tier: A static/unit.

## Complete Runtime Log Reconciliation (2026-07-24)
- Reconciled all 1,139 supplied log lines into ten normalized clusters: 55 warnings, one error, one traceback, and historical/test-origin records.
- Media progress edits now select caption or text APIs correctly, preserve entities/buttons, detect typed `BotAPI.NoTextToEdit` plus legacy descriptions, retry stale wrappers through caption editing without exception logs, and detach dead progress watchers.
- YouTube requested-format failures are retryable format mismatches rather than six-hour permanent-cache entries. Direct streams require compatible codecs, and `py_yt` retries once without an unsupported proxy argument.
- SoundCloud direct retry no longer reports a stale proxy failure and is excluded from video fallback. Duplicate voice-chat stop requests are serialized and already-left calls complete idempotently.
- Known bot-account dialog scans are skipped, smoke tests use an isolated null logger, and thumbnail font loading requests RAQM only when Pillow reports it available.
- Validation: `compileall`, structure verification, and secret scan passed; executable smoke suite passed 40/40 with no warnings. The supplied forensic log remained byte-for-byte unchanged. Deployment restart remains required.
- A traceback that still identifies `_utilities.py:1938` as `edit_text()` proves an older module is loaded: in the fixed source line 1938 is the media-caption branch and text editing is later. Extraction alone is insufficient; the Python process must be fully restarted.

## Download-to-Playback Card Ownership Fix (2026-07-24)
- Fixed the screenshot state where a Now Playing thumbnail and live controls retained a `DOWNLOADING... 100%` caption.
- Root cause was a late parallel-cache callback retaining write ownership of the same message after `update_now_playing()` had converted it to the `play_media` card. The media-aware `NoTextToEdit` fallback correctly made that stale write succeed as a caption edit; the timer then restored controls, producing the mixed UI.
- All progress producers now share a per-chat/message ownership guard and edit lock. Playback closes progress ownership and drains an already-in-flight edit before rendering `play_media`; YouTube detaches only the UI watcher, so the background cache download continues.
- Initial `0%`, live YouTube, Telegram, TikTok/Facebook yt-dlp, and delayed Cancel-button edits all honor the same closed state.
- Validation: changed modules compile and the deterministic in-flight race regression passed as part of the full `40/40` smoke suite. Deployment still requires extraction plus a full process restart.

## Direct YouTube Link Metadata Propagation (2026-07-25)
- `/play <youtube-url>` and `/vplay <youtube-url>` now reuse the title, duration, channel, thumbnail, URL, and view count already returned by the parallel yt-dlp direct-stream extraction.
- The former fast-path placeholder (`YouTube Video`, empty duration rendered as `00:00`) remains only as a fail-soft result when every metadata source is unavailable.
- Direct extraction stays single-flight and stores a bounded 30-minute metadata cache, so first-stream and queued cards receive the same normalized Track without a second yt-dlp request.
- The existing 350 ms lightweight provider budget is preserved; the fix does not add a fixed wait or weaken cookie/auth recovery.

## Full AnonX_3 Namespace Migration (2026-07-25)
- Renamed the inner Python package to `AnonX_3/` and migrated imports, entrypoints, configuration, tests, operations, and documentation to the `AnonX_3` namespace.
- Built and verified `dist/AnonX_3-v3.2.0-final.zip`; obsolete inherited sibling release artifacts were removed after successful verification.
- Compile, structure, secret, and D5 full-project checks pass. Final stale-reference audit found zero old standalone or sibling namespace references and zero old package paths. Full smoke result is 41/42 with only the pre-existing `test_log_regression_guards` failure.

<!-- KIMI-CODEX:PROJECT_STATE:START -->
# Auto-Managed Durable State

## Decisions

- [turn:2026-07-26-ai-assistant-accuracy-agent] Use a primary conversation agent plus a low-temperature independent Accuracy Agent for substantive replies; bypass review for casual greetings and fall back to the primary draft if review fails.
- [turn:2026-07-26-ai-fast-response-background-review] Keep the independent Accuracy Agent but remove it from the synchronous response critical path; send the primary reply first, then perform bounded review in the background and edit only the corresponding reply when improved.
- [turn:2026-07-26-ai-stream-first-token] Ordinary AI chat now uses a compact streaming fast-model path; tool-capable requests retain the full non-stream agent path, and accuracy review remains background-only.
- [turn:2026-07-26-deepseek-v4-key-recovery] Use the current DeepSeek V4 model IDs: deepseek-v4-flash with thinking disabled for fast ordinary chat, while retaining deepseek-v4-pro for tool-capable requests.
- [turn:2026-07-26-dynamic-owner-profile] Owner identity questions must use a fresh verified Telegram public profile and a warm positive privacy-safe reply.
- [turn:2026-07-26-owner-context-no-keywords] Owner understanding must be semantic and must not depend on predefined owner keywords.
- [turn:2026-07-26-semantic-dynamic-role-agent] AI chat role and music-tool selection are inferred semantically from the full user message and conversation, never from predefined keyword routing.
- [turn:2026-07-26-dynamic-balanced-owner-tone] Owner replies must choose tone anew from each user message and conversation; no forced praise, defense, criticism, or fixed owner persona.
- [turn:2026-07-27-youtube-format-auth-recovery] Recovery circuits follow the final classified error: FORMAT continues bounded selector/client recovery, and only a final explicit AUTH_CHALLENGE opens the global YouTube circuit.
- [turn:2026-07-27-dynamic-chromium-soundcloud] Challenge-only auth recovery uses only an explicit dedicated Chromium profile; SoundCloud direct attempts clear inherited proxy state and late fallback preserves the original non-URL user query.
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] Keep the global 0.85 matcher unchanged; rescue only one unambiguous candidate from the same original query attempt when Unicode marks, a version-labelled title prefix, and known durations within ten seconds and 5% all agree.

## Recent Changes

- [turn:2026-07-26-ai-assistant-accuracy-agent] Upgraded AnonX_3 AI chat intent/accuracy prompt, bounded TTL/LRU conversation memory, shared HTTP session, protected-literal validation, sudo-only diagnostics, and dynamic review configuration.
- [turn:2026-07-26-ai-fast-response-background-review] Added background review task tracking (max 32), concurrency limit 3, dedicated 8-second review timeout, protected reply edits, context correction, and primary latency logging.
- [turn:2026-07-26-ai-stream-first-token] Moved private AI handler ahead of AFK DB work (group 21), removed blocking typing action, added local tool-intent routing, four-message fast context, 360-token fast output, deepseek-chat fast model, 15-second bound, SSE first-content delivery, throttled Telegram edits, fallback, and TTFB logging.
- [turn:2026-07-26-deepseek-v4-key-recovery] Updated the ignored runtime DEEPSEEK_API_KEY without persisting its value in documentation; set DEEPSEEK_FAST_MODEL=deepseek-v4-flash; added non-thinking request mode; updated config, sample environment, and focused guards.
- [turn:2026-07-26-dynamic-owner-profile] Expanded Myanmar and English owner/creator/developer intent routing; dynamically resolves owner display name and public username on every request; injects verified profile into the fast response path; prevents invented biography and private details.
- [turn:2026-07-26-owner-context-no-keywords] Removed owner intent regex and owner terms from operational tool routing. Every AI chat request now receives a freshly resolved public-only owner profile as trusted context; the model determines whether it is relevant from the full message.
- [turn:2026-07-26-semantic-dynamic-role-agent] Removed _BOT_TOOL_INTENT_RE and _message_needs_bot_tools; made BOT_TOOLS available every turn; added streamed tool-call accumulation; standardized V4 Flash with thinking disabled; strengthened user-led dynamic-role and human-tone instructions; isolated owner facts as private context used only when semantically relevant.
- [turn:2026-07-26-dynamic-balanced-owner-tone] Replaced forced warm-positive owner instructions with verified-fact-based dynamic tone across the full prompt, compact streaming prompt, live owner tool result, and private owner context.
- [turn:2026-07-27-youtube-format-auth-recovery] Added original-plus-permissive authenticated format recovery, final-error reclassification, and healthy zero-result handling for SoundCloud source health.
- [turn:2026-07-27-dynamic-chromium-soundcloud] Wired optional Chromium/profile mounts into container configuration; bounded SoundCloud proxy/direct metadata attempts; completed download/direct-stream fallthrough; and added original-query-first fallback.
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] Added mark-preserving NFKC query/title validation, strict duration/version/original-attempt gates, ambiguity rejection, observable rescue metadata, and exact live Burmese regression coverage.

## Markdown Notes

- [turn:2026-07-26-ai-assistant-accuracy-agent-env] Documented optional DEEPSEEK_REVIEW_ENABLED, DEEPSEEK_REVIEW_MODEL, and DEEPSEEK_ASSISTANT_TIMEOUT_SEC settings.

## Files Touched

- [turn:2026-07-26-ai-assistant-accuracy-agent] AnonX_3\plugins\bot_assistant.py; config.py; tests\run_unit_smoke.py
- [turn:2026-07-26-ai-assistant-accuracy-agent-env] sample.env
- [turn:2026-07-26-ai-fast-response-background-review] AnonX_3\plugins\bot_assistant.py; config.py; sample.env; tests\run_unit_smoke.py
- [turn:2026-07-26-deepseek-v4-key-recovery] AnonX_3\plugins\bot_assistant.py; config.py; sample.env; tests\run_unit_smoke.py; .env (secret value omitted)
- [turn:2026-07-26-dynamic-owner-profile] AnonX_3/plugins/bot_assistant.py
- [turn:2026-07-26-dynamic-owner-profile] tests/run_unit_smoke.py
- [turn:2026-07-26-dynamic-balanced-owner-tone] AnonX_3\AnonX_3\plugins\bot_assistant.py; tests\run_unit_smoke.py
- [turn:2026-07-27-youtube-format-auth-recovery] AnonX_3\core\youtube.py; AnonX_3\core\resolver\fallback.py; tests\run_unit_smoke.py; ERRORS.md; DECISIONS.md
- [turn:2026-07-27-dynamic-chromium-soundcloud] AnonX_3\core\resolver\soundcloud.py; AnonX_3\core\resolver\fallback.py; config.py; sample.env; Dockerfile; docker-compose.yml; docker-compose.example.yml; .gitignore; .dockerignore; tests\run_unit_smoke.py; docs\local-cookie-agent.md; durable Markdown
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] AnonX_3\core\resolver\matcher.py; AnonX_3\core\resolver\fallback.py; tests\run_unit_smoke.py; DECISIONS.md; ERRORS.md; SESSION_MEMORY.md; RELEASE_NOTES.md; PROJECT_STATE.md

## Resolved Issues

- [turn:2026-07-26-ai-assistant-accuracy-agent] AI replies are independently checked for factual consistency, relevance, natural language, tool evidence, and preserved URLs/commands/IDs.
- [turn:2026-07-26-ai-fast-response-background-review] The second model round trip no longer delays ordinary AI chat replies; supplied 14.7-second recording matched the previous serial-review bottleneck.
- [turn:2026-07-26-ai-stream-first-token] Normal AI replies no longer wait for the complete model response before Telegram delivery; first useful streamed content is sent immediately and then edited progressively.
- [turn:2026-07-26-deepseek-v4-key-recovery] The AI fallback was reproduced as API request failure; legacy deepseek-chat validation returned HTTP 400 after its deprecation date, while the new V4 Flash non-thinking request returned HTTP 200 with the supplied credential.
- [turn:2026-07-26-dynamic-owner-profile] Natural owner questions now bypass model tool-choice uncertainty and use verified live profile data.
- [turn:2026-07-26-owner-context-no-keywords] Owner questions using arbitrary wording can be answered dynamically without keyword matching.
- [turn:2026-07-26-semantic-dynamic-role-agent] Arbitrary natural wording can select the appropriate conversational role or music-bot action without a keyword table.
- [turn:2026-07-26-dynamic-balanced-owner-tone] Owner questions no longer force excessive praise; genuine verified positives may be expressed naturally while unsupported judgments remain neutral, constructive, and honest.
- [turn:2026-07-27-youtube-format-auth-recovery] Browser-authenticated FORMAT failures no longer poison unrelated YouTube requests, and normal SoundCloud query misses no longer open its global source circuit.
- [turn:2026-07-27-dynamic-chromium-soundcloud] Shipped runtime defaults no longer leave challenge recovery structurally inert in Docker, and Burmese queries no longer disappear when a late YouTube failure crosses to SoundCloud.
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] The exact `တောက်တီးတောက်တဲ့` SoundCloud remix/uploader alias can pass the narrow fallback guard without conflating different Myanmar diacritics or broadly weakening match safety.

## Test Results

- [turn:2026-07-26-ai-assistant-accuracy-agent] Compile passed; focused AI regression passed; full smoke 42/43 with only pre-existing test_log_regression_guards failure.
- [turn:2026-07-26-ai-fast-response-background-review] All four variants compile; focused fast-response regression 4/4 passed; normalized assistant hashes match; base full smoke 42/43 with only pre-existing test_log_regression_guards failure.
- [turn:2026-07-26-ai-stream-first-token] All four variants compile; focused AI streaming guards pass 4/4; namespace-normalized assistant hashes match; git diff check passes; base full smoke 42/43 with only the pre-existing unrelated test_log_regression_guards failure.
- [turn:2026-07-26-deepseek-v4-key-recovery] All four variants compile; focused AI tests pass 4/4; live DeepSeek V4 Flash request returned HTTP 200.
- [turn:2026-07-26-dynamic-owner-profile] Compile passed; focused owner AI regression passed. Base full smoke: 42/43, with only pre-existing test_log_regression_guards failing.
- [turn:2026-07-26-owner-context-no-keywords] Compile passed and focused dynamic-owner regression passed for all four variants.
- [turn:2026-07-26-semantic-dynamic-role-agent] All four variants compile and focused semantic-agent regressions pass; normalized source/test parity true. Base full smoke remains 42/43 with only pre-existing test_log_regression_guards failing.
- [turn:2026-07-26-dynamic-balanced-owner-tone] Compile and focused dynamic-owner regression passed for all four variants; namespace-normalized source/test parity true; base full smoke 42/43 with only pre-existing test_log_regression_guards failure.
- [turn:2026-07-27-youtube-format-auth-recovery] Exact executable regressions passed; all 188 package Python files compile; structure and secret scans pass; full smoke is 46/47 with only the pre-existing unrelated py-yt-search proxy-signature guard failing.
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] Live metadata verified `T6um_4zwoSM` at 220 seconds and exactly one direct `scsearch8` candidate at 229.532 seconds; the live AnonX_3 fallback returned `match_mode=normalized_query_containment`. All four variants compile, pass matcher/SoundCloud guards, structure, secret, and eight-file YAML validation; each full smoke suite is 46/47 with only the pre-existing unrelated py-yt-search proxy-signature guard failing.

## Next Steps

- [turn:2026-07-26-ai-assistant-accuracy-agent] Restart the deployed bot process to load the updated assistant code and optionally set DEEPSEEK_REVIEW_MODEL.
- [turn:2026-07-26-ai-fast-response-background-review] Deploy/restart each bot process and confirm AI primary reply delivered latency_ms in logs.
- [turn:2026-07-26-ai-stream-first-token] Restart each deployed bot process, then verify AI streaming first content delivered ttfb_ms in logs.
- [turn:2026-07-26-deepseek-v4-key-recovery] Deploy updated files and restart each running VPS bot process; confirm streaming first-content log and no fallback response.
- [turn:2026-07-26-dynamic-owner-profile] Restart the deployed bot process and ask: ပိုင်ရှင်က ဘယ်သူလဲ
- [turn:2026-07-26-owner-context-no-keywords] Restart the deployed bot process and test owner questions with several different natural phrasings.
- [turn:2026-07-26-semantic-dynamic-role-agent] Restart each deployed bot and test casual chat, an indirectly worded music request, a domain question, and an indirectly worded owner question.
- [turn:2026-07-26-dynamic-balanced-owner-tone] Restart the deployed bot process and test neutral identity, genuine praise, criticism bait, and unknown-character owner questions.
- [turn:2026-07-27-youtube-format-auth-recovery] Restart the deployed AnonX_3 process and confirm FORMAT recovery logs `to=format next=format_retry` without opening the auth circuit; verify later unrelated audio requests remain eligible for YouTube and SoundCloud.
- [turn:2026-07-27-dynamic-chromium-soundcloud] Sign into the dedicated Chromium profile once, close the interactive browser, rebuild/redeploy the selected variant, restart it, then verify one auth-triggered export and a sanitized SoundCloud `retrying direct` trace.
- [turn:2026-07-27-unicode-safe-soundcloud-rescue] After deploy/restart, replay `တောက်တီးတောက်တဲ့` and confirm sanitized fallback metadata reports `match_mode=normalized_query_containment` only when YouTube recovery cannot produce a playable artifact.
- [turn:2026-07-28-soundcloud-drm-timeout] Deploy and restart AnonX_3, replay the reported SoundCloud fallback query, and confirm protected candidates are skipped while transport failures produce one sanitized `source_unavailable` outcome without a repeated query ladder.

## SoundCloud DRM/Timeout Deep Fix (2026-07-28)

- Decision: flat SoundCloud discovery plus a bounded three-candidate playability
  probe replaces eager extraction of every `scsearch` result.
- Files touched: `AnonX_3/core/resolver/soundcloud.py`,
  `AnonX_3/core/resolver/fallback.py`, `config.py`, `sample.env`,
  `tests/run_unit_smoke.py`, and durable Markdown.
- Resolved: one DRM result no longer aborts search; metadata timeouts no longer
  multiply through yt-dlp internal retries and repeated fallback queries; raw
  extractor errors are captured and sanitized.
- Validation: compile PASS, focused SoundCloud regression PASS, full smoke
  47/47 PASS, recursion regression PASS, structure PASS, secret scan PASS.
<!-- KIMI-CODEX:PROJECT_STATE:END -->
- 26-Jul-2026: **Strict `/song` audio and `/vsong` video contract**: `/song` now remains audio-only even when legacy `-v`/`--video` flags are supplied; `/vsong` is the only video-download command. Fresh `/vsong` uploads use the generated 320px Telegram thumbnail, legacy video file-id cache entries are bypassed with a `thumb-v1` cache revision, and the newly uploaded thumbnail-bearing file is cached for reuse. Telegram command menus, help text, assistant command context, and smoke guards now describe the two commands explicitly. Validation: compile OK; targeted song/cache/menu/JSON guards OK; structure OK. The full smoke suite is 44/45 because the pre-existing unrelated YouTube proxy-signature log guard fails. Confidence: 96%, tier: A.
- 26-Jul-2026: **`/song` and `/vsong` Pyrogram keyboard crash fixed**: live traceback proved `buttons.cancel_dl()` returned a Bot API `dict` to direct `Message.reply_text()`, whose Kurigram serializer requires an object with `.write()`. The song handler now uses `cancel_dl_pyrogram()` at that direct transport boundary while Bot API-aware progress utilities retain the styled dictionary builder. Regression coverage executes the real builder and verifies non-dict markup, `.write()`, and `cancel_dl` callback data. Compile and targeted transport guards pass; full smoke remains 44/45 solely because of the pre-existing unrelated YouTube proxy-signature guard. Restart required. Confidence: 98%, tier: A.
- 26-Jul-2026: **Colored Cancel button for `/song` and `/vsong`**: song/video-song Searching now matches `/play` by building the Cancel keyboard with `buttons.cancel_dl()` (`style="danger"`, `callback_data="cancel_dl"`) and sending it through `utils.reply_formatted()`. This preserves Telegram button color while keeping Bot API dictionaries away from direct Pyrogram serialization, so the prior `.write()` crash cannot recur on this path. A `None` send result exits cleanly. Compile and executable colored-button/transport guards pass; full smoke is 44/45 only because of the pre-existing unrelated YouTube proxy-signature guard. Restart required. Confidence: 98%, tier: A.
- 26-Jul-2026: **Single-card `/song` and `/vsong` Downloading transition**: the Searching status now advances through `utils.edit_download_progress()` instead of a direct Pyrogram `sent.edit_text()`. Searching and Downloading therefore share one message ID and one Bot API-aware edit path, while the danger-colored Cancel keyboard remains attached during the download. Compile and focused guards pass in all variants; full smoke remains 44/45 only because of the pre-existing unrelated log/proxy guard. Restart required. Confidence: 98%, tier: A.
- 26-Jul-2026: **Plain `Downloading song...` status removed**: operator clarification superseded the explicit transition above. `/song` and `/vsong` now keep the original Cancel-enabled status card and do not publish or edit it to the standalone `song_downloading` text; real provider byte-progress may still update that same message without creating another card. Executable guards require zero `song_downloading` references in the handler while preserving Cancel and message-bound progress/cancellation. Compile and focused guards pass in all four variants. Restart required. Confidence: 99%, tier: A.
- 26-Jul-2026: **Global silent kick watchlist**: `/kick <id|username>`, `/unkick`, `/kicklist`, and `/sudolists` are sudo-only and LOGGER_ID-only. Dedicated Mongo storage remains separate from ordinary ban/mute/kick; add triggers a bounded all-group sweep, rejoin forces a fresh DB check, and message activity is a fallback. Add/remove commands and enforcement publish no bot messages. All four variants compile and their new regression guard passes; full smoke is 45/46 only because of the pre-existing unrelated log/proxy guard. Restart required. Confidence: 98%, tier: A.
- 26-Jul-2026: **Shared-root runtime artifacts removed**: the smoke runner now changes CWD to its own deploy root before imports/tests, preventing path-based invocation from creating `cache/`, `downloads/`, or `media/` in the shared parent workspace. Verified empty root cache/downloads and the test-created 20 KB root media database were removed without touching per-variant runtime directories. Compile and executable CWD guards pass for all four variants. Confidence: 99%, tier: A.
- 27-Jul-2026: **YouTube FORMAT/auth recovery deep fix synchronized and validated**: authenticated recovery now tries original and permissive selectors, follows the final error class, never opens the global auth circuit for final `FORMAT`, and treats empty SoundCloud searches as healthy provider responses. Normalized implementation and regression-test hashes match across AnonX_3, AnonX_3, AnonX_3, and AnonX_3. All four compile 188 package files and pass focused recovery, structure, and secret checks. Each full smoke suite is 46/47; the only failure is the pre-existing unrelated `test_log_regression_guards` proxy-signature assertion. Restart required. Confidence: 98%, tier: A.
- 28-Jul-2026: **AnonX_3 v3.2.1 final release implementation**: the active root is the sole canonical release, dependencies are exactly locked for Python 3.13, setup and Docker installs fail on conflicts, environment merge is root-scoped and atomic, and release identity is centralized. `ops/release_gate.py` is the publish gate and produces `AnonX_3-v3.2.1-final.zip` only after clean dependency, compile, regression, structure, secret, API import, deterministic-build, and manifest checks. Runtime credentials and data are excluded and untouched. Validation passed in a clean Python 3.13.13 environment: `pip check`, 48/48 smoke, recursion regression, compile, structure, secret scan, API import, two byte-identical builds, manifest verification, and Linux artifact availability for all 58 lock entries.
- 28-Jul-2026: **YouTube `/play` auth-challenge deep fix**: forensic `log.txt` analysis traced the screenshot failure to a Chromium-cookie integrity bug plus proxy-bound authenticated recovery. The watcher now delegates to yt-dlp's decrypted export, empty values cannot pass health checks, auth recovery rotates direct and configured-proxy egress before opening the circuit, repeated circuit skips are deduplicated, and the exact affected track gets an actionable session message. Files: `AnonX_3/core/cookie_watcher.py`, `AnonX_3/core/youtube.py`, `AnonX_3/core/calls.py`, `AnonX_3/__main__.py`, `AnonX_3/locales/en.json`, `tests/run_unit_smoke.py`, and durable Markdown. Validation: targeted auth regression PASS; full smoke 49/49 PASS; compile PASS. Deployment restart required. Confidence: 98%, tier: A.
- 28-Jul-2026: **Official PO-token provider enabled**: Replaced the nonfunctional empty-token Nginx stub/custom GET client with yt-dlp's provider framework, pinned `bgutil-ytdlp-pot-provider==1.3.1`, and the matching long-lived `brainicism/bgutil-ytdlp-pot-provider:1.3.1` Compose sidecar on port 4416. The bot configures the official provider extractor argument only for the existing bounded YouTube extraction/download paths, preserves the client-bound `mweb` client through authenticated recovery, avoids retrying a configured-but-missing plugin, validates enabled-without-URL startup state, and exposes plugin availability in health output. No token contents are logged or manually cached. Validation: pinned plugin load, provider args, and both Compose YAML files pass in an isolated dependency sandbox; compileall and recursion regression pass; full smoke 50/50, structure, and secret scans pass; `dist/` remains absent. Docker is unavailable on this workstation, so a live sidecar health check remains a deployment check. Confidence: 98%, tier: A.
- 28-Jul-2026: **Complete `AnonX_3` → `AnonX_3` identity migration**: Renamed both the deploy root and Python package to `AnonX_3`; migrated imports, dynamic patch paths, runtime entry point, test environment key, Mongo default, media/image paths, Docker user/mounts, Compose, setup, release metadata/archive name, operations scripts, tests, and active documentation. Existing credentials were preserved while exact identity values in `.env` were updated. Generic numbered-sibling detection remains intentionally capable of recognizing legacy `AnonX_N` imports and paths. Validation: forced compileall, package/API import, release metadata, both Compose YAML documents, structure and secret scans, recursion regression, and full smoke 50/50 pass. `dist/` remains absent. Restart/deploy from the new root with `python3 -m AnonX_3`. Confidence: 99%, tier: A.
- 31-Jul-2026: **Deep AnonX_3 identity migration**: Renamed the active project
  root and Python package directory to `AnonX_3`, then updated all exact
  identity references across imports, module entrypoints, startup wrappers,
  asset paths, Mongo defaults, Docker/Compose, release metadata, tests,
  documentation, `.env`, and generated scan metadata. Operational symbols now
  use `AnonX_*` where they are part of this project's environment, metrics,
  nginx, or internal guard contracts. Sibling identities and the generic
  `AnonX_N` legacy placeholder remain unchanged. Validation: exact legacy-token
  scan clean, package import PASS, compileall PASS, structure PASS, secret scan
  PASS, recursion regression PASS, and full smoke **66/66 PASS**. The complete
  release gate stopped at the existing workstation dependency conflict
  (`motor 3.6.0` requires `pymongo<4.10`, installed `pymongo 4.17.0`); release
  identity checks pass. The release archive built twice byte-identically as
  `AnonX_3-v3.3.0-final.zip` and `ops/verify_release.py` passed with 249
  members. Confidence: 99%, tier: A.
- 31-Jul-2026: **YouTube search false-miss timeout fix**: The outer py_yt
  provider-race deadline was shorter than the provider's configured
  four-second request budget, so healthy slow searches could return `None` and
  surface `play_not_found`. Search and deep-search deadlines now allow the
  provider budget while remaining bounded by the existing `/play` resolver
  timeout. Regression coverage proves a 1.8-second valid provider result is
  retained. Live `Shape of You` search returned `erd3fTm-2t8`
  (`Ed Sheeran – Shape of You (Lyrics)`). Compile and full smoke **67/67 PASS**.
  Confidence: 99%, tier: A.
- 31-Jul-2026: **YouTube search outage fallback and support-link fix**: When
  the API and py_yt providers both miss, `resolve_source` now makes one
  bounded yt-dlp metadata-search attempt before SoundCloud fallback, so a
  transient provider outage is not reported as `play_not_found`. English and
  Myanmar not-found templates now quote the support-chat href for valid
  Telegram link entities. Live `Shape of You` resolver returned
  `JGwWNGJdvx8` (`Ed Sheeran - Shape of You (Official Music Video)`). Compile,
  focused regressions, structure, secret scan, and full smoke **68/68 PASS**.
  Confidence: 99%, tier: A.
- 31-Jul-2026: **Confidence-gated auto-learn**: Plain human-to-human replies
  are stored as bounded Mongo candidates instead of becoming active answers on
  the first observation. The same keyword/answer pair must be observed twice
  by default (`AUTO_LEARN_CONFIRMATIONS=2`); manual and legacy `/reply` rules
  cannot be overwritten by automatic learning. Explicit `/reply` remains
  immediate. Candidate persistence, manual-rule protection, and promotion are
  covered by smoke tests. Compile and full smoke **69/69 PASS**. Confidence:
  99%, tier: A.
- 31-Jul-2026: **v3.3.1 final deep stable release preparation**: Promoted the
  current verified `AnonX_3` tree from stale v3.3.0 metadata to
  `AnonX_3-v3.3.1-final`, aligned package/version/lock/runbook documentation,
  and removed the unused workstation-only Motor package so the declared native
  PyMongo async graph passes dependency consistency. The full release gate
  passed with 69/69 smoke tests, recursion, structure, secret, Downloader API,
  deterministic double-build, and manifest verification; the generated
  `.sha256` sidecar is the authoritative archive digest.
- 08-Aug-2026: **YouTube fast-lane runtime-403 deep fix**: changed authenticated direct fast-lane selection from a multi-client yt-dlp ladder to one speculative client per extract, added per-client runtime quarantine/rotation for conclusive GVS 403s and gate refusals, preserved minting-client metadata through source rewrites, and upgraded diagnostics to report the actual rejected client. The authoritative `mweb` + bgutil PO-token lane remains parallel and untouched. This addresses the live trace where `tv,tv_downgraded,web_embedded` was statically POT-free yet GVS returned 403 before `mweb` succeeded with 206. Validation in this workspace: full Python compile PASS, project secret scan PASS, synthetic current-policy rotation test PASS. Live VPS verification after restart is still required because YouTube enforcement is account/egress dependent.
- 08-Aug-2026: **Authoritative mweb + POT primary fast-path deep change**: authenticated direct startup now defaults to a single provider-bound `mweb` foreground lane instead of racing the token-free speculative ladder that returned live GVS 403s. The path preserves cookies, IPv6/source-address binding, JS runtime and bgutil POT, skips manifest/subtitle work unnecessary for format 140, preflights the fresh GVS URL, reuses that fresh verdict to avoid duplicate probing, and purges/remints once on a first authoritative 403. `adaptive_legacy` remains as a rollback mode. Compile + structure validation PASS in the workspace; dependency-backed runtime tests require the deploy environment packages.

- 08-Aug-2026: **Final merged mweb + POT-only YouTube direct resolver**: legacy speculative direct fast-lane clients/config/race/quarantine/rollback logic removed. Foreground direct resolution is now provider-bound `mweb + POT` only, gated by fresh GVS Range 200/206 validation with duplicate-probe reuse and one PO-token cache purge/remint after a first 403. Existing local/download recovery remains separate. Clean distribution excludes secrets and runtime artifacts.

### Direct resolver latency architecture (2026-08-08)
Foreground YouTube direct playback uses mweb + bgutil PO-token only. Search starts a singleflight prewarm task. A dedicated two-worker persistent YoutubeDL executor reuses resolver state. The lightweight mweb profile skips watch-page/client-config discovery and request sleeps, but every result still requires a 200/206 GVS Range preflight. Robust mweb/POT is the immediate fallback, including the existing 403 token-purge/remint recovery.
