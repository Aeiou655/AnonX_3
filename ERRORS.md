# ERRORS — Bug Encyclopedia

## Deleted Status Card Aborted Playback + Concurrent POT Registration (2026-08-09)

- Symptoms: `_admit_and_stream_media()` reported
  `Unexpected playback startup failure` when `editMessageText` returned
  `message to edit not found`; startup warm also printed repeated
  `PoTokenProvider BgUtilHTTP/BgUtilScriptNode already registered` assertions.
- Playback root cause: queued/status presentation ran inside the same broad
  exception transaction as queue admission and media startup. A missing card
  therefore invoked media rollback despite valid queue/provider state.
- yt-dlp root cause: pinned yt-dlp loads external plugins during the first
  `YoutubeDL` construction through process-global registries. Six cold sticky
  workers constructed runtimes concurrently, and that first-load path is not
  thread-safe. Exact pinned wheels reproduced the failure independently.
- Fix: isolate presentation-only operations from playback rollback, clear dead
  card IDs, and single-flight only the first process-wide constructor while
  keeping every later constructor/extraction concurrent.
- Prevention: retain the dependency-free stale-card and constructor-barrier
  regressions. A deploy requires a full process restart because the provider
  registry cannot be repaired safely in place.

## Acknowledgement, Search, Resolver, and VC Ran Serially (2026-08-09)

- Symptom: recent fresh traces showed search-to-scheduled at 2.093–2.846 s,
  resolver p95 2.846 s, command-to-packet p95 4.790 s, and only 1/3 under 4 s.
  All nine micro attempts returned `no_direct_progressive`.
- Root causes: the 0.636–0.704 s SEARCHING send completed before search began;
  live-VC presence was fetched after source readiness; the actual VC connection
  began only inside `play_media`; and the micro player passed empty ytcfg to
  cookie-bearing clients that did not match yt-dlp's authenticated defaults.
- Fix: overlap the acknowledgement/search/admission reads, start an authorized
  provisional EXTERNAL connection under an exclusive admission lease, adopt it
  without reconnecting, use yt-dlp-aligned auth clients/default ytcfg, and give
  `/vplay` a bounded external-audio packet lead before its raw A/V swap.
- Prevention: v3.4.9 executable tests cover deferred message forwarding,
  rollback/adoption wiring, response diagnostics, client context, and all three
  latency summaries. Live acceptance remains 20+20 fresh traces; prior logs are
  baseline evidence only.

## Micro Resolver Discarded Safe Signed URL Envelopes (2026-08-09)

- Symptom: production `android_vr`, `web_embedded`, and `mweb` micro lanes all
  reported `no_direct_progressive`, so every request waited for a 1.5–2.8s full
  yt-dlp extraction and then a serial media preflight.
- Root cause: the normalizer rejected every `signatureCipher`/`cipher` object,
  including envelopes whose URL was already signed and contained no encrypted
  `s` challenge. The race also treated full extraction completion as a reason
  to stop waiting for micro before the full URL had passed 200/206 validation.
- Fix: recover only HTTP(S) wrapped URLs with no encrypted `s`, preserve plain
  signature parameters, cap micro fetch + proof at 1450 ms, and race both full
  hedges through validation beside all micro candidates.
- Prevention: executable regression tests cover safe/unsafe envelopes and the
  validated race; `ops/resolver_latency_report.py` enforces the live p95 target
  from real `playback_trace` logs. Production win rate and latency still require
  fresh deployment evidence.

## Raw Direct Audio Stalled Inside PyTgCalls Startup (2026-08-08)

- Symptom: raw source and VC readiness completed at about 1.75 s and 3.58 s,
  while play dispatch occurred near 2.03 s but stream attachment/first audio did
  not occur until about 15.38 s. The remaining delay was therefore inside the
  play-to-media-attachment interval, not VC join or URL resolution.
- Root cause: installed PyTgCalls 2.2.11 calls `MediaStream.check_stream()`
  before installing sources. For a remote URL this performs a blocking ffprobe
  and an additional FFmpeg capability scan. Awaiting `client.play()` kept that
  inspection on the cold-start critical path.
- Fix: cold initial YouTube playback now supplies a raw PyTgCalls `Stream`,
  starts `client.play()` in an owned task, and observes attachment/failure
  asynchronously. Its FFmpeg command removes reconnect sleeps and minimizes
  input analysis/buffering. A transparent relay timestamps FFmpeg spawn, first
  remote bytes, and first PCM frame; the call owner timestamps play before/after
  and attachment; outgoing NTgCalls clock movement records first-packet evidence.
  Cache/download and other post-start work begins only after that signal.
- Fallback/safety: foreground URL shape/SSRF validation and stream admission are
  retained. Task or early-stream failure still enters the existing one-shot
  local fallback. Queued, established-call, and non-YouTube behavior is unchanged.
- Prevention: retain the raw-stream source guards and executable observer relay
  test in `tests/run_unit_smoke.py`. Deployment validation must compare the new
  `direct_startup_event` timestamps and confirm first-packet/audible timing on a
  live Telegram VC; local tests cannot prove remote network or Telegram render
  latency.

## Initial YouTube Audio Waited for Serial Startup Work (2026-08-08)

- Symptom: `/play` logs showed direct source resolution completing before VC
  join/self-unmute, followed by a blocking startup proof and cache/download
  work before the request was considered ready. First audio therefore inherited
  the sum of independent join, resolve, proof, profile, and display delays.
- Root cause: `play_media` resolved first, then used the first
  `PyTgCalls.play(stream)` call to join, treated assistant unmute as a
  best-effort background action, and awaited the proof/cache/display sequence
  inline.
- Fix: initial cold YouTube playback prejoins an empty call and requires unmute
  in parallel with raw-source resolution. It attaches immediately when both
  finish; startup proof, diagnostics, profile refresh, metadata, UI, and cache
  are owned background tasks. Failed unmute leaves the empty call and releases
  admission before any stream or download starts.
- Prevention: smoke coverage checks concurrent readiness/cancellation,
  acceptance-before-proof semantics, SSRF validation, hard unmute rollback,
  scoped `initial_start` wiring, and shutdown ownership of post-start tasks.

## Auto-Learn Candidate Mixed Datetime Warning (2026-08-03)

- Symptom: VPS log showed
  `Auto-learn candidate DB error ... can't compare offset-naive and offset-aware datetimes`
  while processing teach-by-reply candidates.
- Root cause: older Mongo/PyMongo values can be returned as offset-naive
  datetimes, while new runtime observations use aware UTC datetimes. Candidate
  pruning compared those mixed values directly when the learned-candidate map
  grew past its cap.
- Fix: auto-reply timestamp reads now coerce datetimes, numeric timestamps, and
  ISO strings to aware UTC before selection, pruning, stale cleanup, or re-save.
- Prevention: `test_auto_learn_lifecycle` seeds mixed naive/aware candidate
  timestamps and forces the pruning branch.

## Bot API `editMessageReplyMarkup` Reports Stale Message as ERROR (2026-08-03)

- Symptom: owner error report `e895529a44` showed
  `Bot API editMessageReplyMarkup failed: ... "Bad Request: message can't be edited"`.
- Root cause: Telegram returns this 400 when a stale/old/not-bot-editable
  message markup is updated. The Bot API wrapper did not classify that wording
  as a stale edit, so it logged a generic ERROR and the DeepSeek error monitor
  forwarded it even when `DEEPSEEK_API_KEY` was empty.
- Fix: `core/bot_api.py` and `helpers/_utilities.py` now treat
  `message can't/cannot be edited` as a stale edit. `core/error_monitor.py`
  also drops this exact benign Bot API edit text before owner reporting.
- Prevention: `test_log_error_hardening_guards` covers the exact production
  string and monitor filter. Real Bot API failures outside stale edit wording
  are still reported.

## `/play` Misreports Timeout or Provider Outage as `play_not_found` (2026-07-31)

- Symptom: a slow YouTube/SoundCloud resolution or temporary provider failure
  ended with the same message used for a genuine title miss.
- Root cause: `/play` had a fixed 12-second guard, and `resolve_source()`
  discarded fallback metadata and collapsed exhausted paths to `play_not_found`.
- Fix: `PLAY_RESOLVE_TIMEOUT_SEC` defaults to 18 seconds; timeout and provider
  transport/circuit failures now produce localized retryable messages, while
  only genuine no-match outcomes retain `play_not_found`.
- Prevention: keep `test_resolve_source_classifies_provider_failures_separately`
  and `test_play_and_startup_failure_boundaries` in the executable smoke suite.

## Startup Failure Re-enters an Unbounded Restart Loop (2026-07-31)

- Symptom: a startup `SystemExit` could repeat forever, and partial clients/tasks
  were not guaranteed to pass through shutdown cleanup.
- Fix: `_run_once()` is wrapped by `main()` with an unconditional `stop()`;
  explicit `SystemExit` exits for an external supervisor instead of retrying
  the same fatal configuration state inside the process.
- Prevention: keep the AST lifecycle guard and inspect the first fatal startup
  traceback before changing credentials or deployment configuration.

## Burmese Informal-Spelling Intent Miss (2026-07-31)

- Symptom: the private assistant treated observed text such as
  `ဘာတေဖစ်နေကျတာလဲဗျ` as an unknown Myanmar message instead of a bot
  status/error question. Typoed music forms such as `သိချင်း` could also
  miss the local music route when the AI credential was unavailable.
- Fix: degraded intent matching now includes bounded observed variants for
  diagnostic and music terms, while preserving the administrator check on
  live status data and stripping only those markers from music queries.
- Prevention: `test_ai_degraded_mode_guards` covers the exact observed status
  and music spellings, plus standard greeting precedence.

## Private Assistant Exception Containment and Informal Music Queries (2026-07-31)

- Symptom: an unexpected provider/tool exception could escape the private
  assistant handler, and informal phrases such as `သီချင်းနာမည် ... ရှာပေး`
  could leave request filler in the search query.
- Fix: AI and local degraded execution now have explicit cancellation-safe
  exception boundaries; operational failures fall through to the truthful
  language-matched response. The degraded parser removes common Burmese and
  English request scaffolding while preserving the actual song title.
- Prevention: `test_ai_degraded_mode_guards` covers normalized informal queries
  and a failing local tool. Keep external credentials unchanged; provider
  recovery still requires a valid `DEEPSEEK_API_KEY` and process restart.

## Private AI Assistant Falls Back to a Generic Reply (2026-07-31)

- Symptom: private messages received the same AI-service apology whenever the
  configured DeepSeek credential was rejected, so music requests and bot-error
  questions appeared to be misunderstood.
- Root cause: the runtime credential returned HTTP 401 and the failure branch
  had no intent-aware local path. The auth circuit now suppresses repeated
  invalid-key calls, but suppression alone cannot answer the user.
- Fix: `plugins/bot_assistant.py` now classifies only high-confidence degraded
  intents and delegates music search/download, cached-result download, owner,
  status, active-music, and top-song requests to existing verified tools. It
  gives a precise Myanmar clarification for missing music queries and an
  honest language-matched fallback for unknown text.
- Prevention: keep `test_ai_degraded_mode_guards` and `test_log_error_hardening_guards`;
  never replace verified tool results with invented live state.
- Operations: replace the rejected `DEEPSEEK_API_KEY` and restart the bot to
  restore open-ended conversation. The local degraded path remains available
  during provider outages.

## VC Is Inactive but `/play` Becomes Permanently Queued (2026-07-30)

- Symptom: a `/play`, `/vplay`, or force-play request made before a group video
  chat exists showed the localized no-call error once, then a later request
  could be marked queued forever with no active assistant stream.
- Root causes: initial playback admitted a queue head before proving that
  Telegram had a live group call; `stop()` used an already-left marker to skip
  all later local cleanup; and the VC-start update was incorrectly treated as
  a stop event while first playback was joining.
- Fix: `TgCall.has_active_group_call()` is checked before initial admission;
  `initial_playback_lock()` serializes each chat's first-play transaction; and
  playback is committed only after `db.get_call(chat_id)` confirms an active
  call. Failure or cancellation removes the exact immutable `request_id`, not
  another request with the same media ID. Playlist tails are added only after
  that commit, and stale resolver/progress tasks are cancelled with their
  status-card request scope.
- Cleanup rule: `stop()` always clears local queue/DB/prefetch/startup state.
  `_stopped_chats` suppresses only duplicate `leave_call` RPCs. Only
  `video_chat_ended` triggers VC cleanup; `video_chat_started` is not a stop
  signal.
- Operations: this bot does not create a VC. Start the group video chat, then
  submit a fresh play command; no inactive request is retained for retry.
- Prevention: `test_stream_media_vc_admission_transaction_guards`,
  `test_queue_remove_request_is_exact_for_duplicate_media_ids`, and the core
  lifecycle/watcher smoke tests cover the regression.

## One Song Starts Multiple yt-dlp Jobs or a Canceled Card Leaves Cache Work Stuck (2026-07-30)

- Symptom: a cold `/play` or `/vplay` starts a direct stream, foreground
  warmup, prefetch, or CDN task that independently extracts the same media;
  canceling one status card can also stop work still needed by another request.
- Root causes: ownership was tied to a caller or quality tier instead of the
  media identity, CDN prefetch could become a second extractor, and cancellation
  treated a UI watcher as though it owned the physical download.
- Fix: a single media-scoped yt-dlp task serves all waiters. Local/catalog hits
  are checked before provider work; only a fully validated artifact is persisted
  and published to CDN. The completed asset receives normalized query/title
  aliases and durable local metadata. A status-card cancel detaches its watcher,
  while the physical owner and other waiters remain alive.
- Failure rule: a request that has spent its one extraction attempt has a
  terminal result—no hidden retry ladder starts beneath the same card. A new
  user command is a fresh scope and may attempt the media again.
- Prevention: `test_resolve_source_prefers_cache_before_search_or_extraction`,
  `test_one_shot_download_publishes_stream_to_concurrent_waiter_once`,
  `test_one_shot_download_failure_does_not_retry_ytdlp`, and `test_store`.

## Cookie Export Looks Healthy but YouTube Still Requires Sign-in (2026-07-28)

- Symptom: cookie watcher reports usable cookies and authentication markers, but
  both `browser_authenticated transport=direct` and `configured_proxy` fail
  with `Sign in to confirm you're not a bot`.
- Root cause: the extraction path forced the stale `android,web` client list
  onto authenticated recovery. Current yt-dlp does not support account cookies
  on Android clients and maintains a different default client selection.
- Fix: normal extraction delegates client selection to yt-dlp. Authenticated
  recovery removes forced clients unless a PO token explicitly binds one.
- Related evidence: the reported selected ID `6X0xsWZ40FU` is independently
  unavailable, so correct classification must leave the existing alternate
  YouTube-upload fallback eligible before SoundCloud.
- Prevention: `test_youtube_auth_challenge_short_circuit` verifies forced
  clients are removed without mutating the caller and PO-bound clients remain.

## Inline Search Is Missing or Selection Does Nothing (2026-07-28)

- Symptom: `@BotUsername query` returns nothing, or selecting a result does not
  start a video download.
- Root causes: the prior inline subsystem was intentionally removed; inline mode
  may also remain disabled in `@BotFather`.
- Fix: `plugins/inline_search.py` now returns bounded YouTube articles whose
  selected content is `/vsong@BotUsername <canonical URL>`. The normal `/vsong`
  handler receives and processes that sent-via-bot command.
- Operations: enable `/setinline` once in `@BotFather`, restart the bot after
  deployment, and type a non-empty song query.
- Prevention: `test_inline_vsong_search_guards` validates the handler, bounded
  provider call, command entity, canonical URL, blacklist, and result cap.

## Telegram Inline Search Removed (2026-07-25, Superseded)

- Symptom: Telegram inline results could not render the operator's Premium custom emoji consistently.
- Resolution: the complete inline-search subsystem was removed at the operator's request; no local workaround remains active.
- Scope guard: ordinary bot-message inline keyboards and callbacks remain. Playback, downloads, queues, playlists, AI DJ, and normal YouTube Copy/Open buttons are preserved.
- Current status: the simple `/vsong` handoff was restored on 2026-07-28
  without restoring the incompatible Premium-emoji or token subsystem.


## Custom “Downloading” Card Has No Live Percentage (2026-07-24)

- Symptom: YouTube, TikTok, Facebook, or large Telegram media remains on the custom `play_downloading` card without showing whether bytes are advancing.
- Root cause: provider download callbacks were either not bound to the command's status message or replaced the custom template with a generic progress locale.
- Fix: current-track provider downloads now append a 12-cell progress bar, exact percentage, transferred/total size, measured speed, and ETA beneath the chat's custom `play_downloading` text. The same message is edited at most once every two seconds and retains its Cancel button; queued background prefetch remains silent.
- Race guard: once byte progress begins, the metadata-completion callback cannot overwrite the live card with the plain downloading template.
- Prevention: `test_parallel_external_source_guards` executes native Telegram and worker-thread yt-dlp callbacks and verifies custom text/entity preservation, progress rendering, and progress-start state.

## Bot API `getFile`: “file is too big” for Telegram Video (2026-07-24)

- Symptom: Telegram reply/link video cannot start and logs `Bad Request: file is too big`.
- Root cause: the official Bot API `getFile` download URL has a much smaller file ceiling than MTProto user clients; retrying the bot endpoint cannot make a multi-gigabyte file available.
- Fix: carry Telegram's original file size through playback metadata. Files above the safe Bot API threshold bypass `getFile` and use the already-parallel assistant MTProto download. Unknown-size files that receive this response are reclassified as an expected capability boundary and follow the same fallback without an error log.
- Capacity: `DOWNLOAD_LIMIT_GB` defaults to `3`; 3GB route coverage asserts that Bot API is never called. Playback still requires enough VPS disk and network time to obtain the file.
- Prevention: preserve the executable large-file routing assertions in `test_parallel_external_source_guards`.

## Telegram Link Crashes; TikTok/Facebook Artifacts Are Missing or Unplayable (2026-07-24)

- Symptom: Telegram reply/link playback crashes at `Telegram._duration_label`; TikTok can report a missing `.part` rename or `NoAudioSourceFound`; Facebook/TikTok/Telegram sources fall through to a generic request error.
- Root causes: Telegram metadata called an absent formatter and its local fallback lost the original message/client context; external providers did not share YouTube's assistant-ready metadata/download overlap; Facebook cancellation could leave a yt-dlp thread writing while a later request removed its partial output; file existence was accepted without proving required audio/video streams.
- Fix: add the Telegram duration formatter and original-message/story download context, resolve external metadata while assistant readiness runs, start source-specific local safety nets in parallel with direct streaming, single-flight Telegram/Facebook downloads by media ID, and accept local artifacts only after `ffprobe` confirms audio plus video for `/vplay`.
- Prevention: keep `test_parallel_external_source_guards`; warm-up failures must fall back to the normal resolver, and `/vplay` must never infer video capability from an extension or command flag alone.

## Every Button Shows the Same “Processing…” Banner (2026-07-24)

- Symptom: pause, resume, skip, stop, and settings buttons all display identical feedback instead of the selected action.
- Root cause: both controls and settings acknowledged callbacks with the generic `processing` key before dispatching the action; later answers could not replace the first callback answer.
- Fix: answer each callback once with its localized action/result, and report the resulting state for settings toggles.
- Related fix: play-mode settings now persist the newly toggled value; the previous order saved the old value and only rendered the opposite value.
- Prevention: callback smoke guards reject generic processing answers and require all playback status mappings.

## Callback Buttons Open Blocking “Processing…” Dialog (2026-07-24)

- Symptom: pressing a playback or settings button opens a centered Telegram dialog with an OK button.
- Root cause: callback handlers answered with `show_alert=True`, which explicitly requests modal presentation from Telegram clients.
- Fix: all callback feedback now uses `show_alert=False`, producing transient top toast/banner feedback without blocking the chat.
- Prevention: the smoke suite scans the complete active package and fails if any Python callback path reintroduces `show_alert=True`.

## Assistant Joins Voice Chat Muted (2026-07-24)

- Symptom: playback joins the group call but users cannot hear it because the selected assistant remains muted.
- Root cause: successful `PyTgCalls.play()` established transport but no Telegram group-call participant state repair followed it.
- Fix: after the join-ready event, schedule assistant self-unmute; if the mute is admin-enforced, let the main bot retry for that assistant when it has Manage Voice Chats permission.
- Prevention: keep the auto-unmute hook after successful `client.play()` and preserve the smoke guard that forbids timer-based delays or arbitrary participant edits.
- Runtime limit: Telegram will reject the fallback when the main bot lacks sufficient voice-chat admin rights.

> See `AnonX_3/ERRORS.md` for full master error documentation.
> Variant: AnonX_3 (AnonXMusic) — GIT-TRACKED
> Last updated: 2026-05-31

All master error patterns apply. Notable for this variant:
- First to receive `/play` query-resolution hardening (30-May-2026) — py_yt fallback chain fix
- `ntgcalls` `SignalError` → `SignalingError` fix applied earlier than other variants

See `../AnonX_3/ERRORS.md` for full root cause analysis and auto-fix patterns.

## Sudo Commands Silently Ignored After Startup (2026-07-23)
- Symptom: owner/sudo `/restart` or `/logs` commands sent directly after “Bot Started” or “Assistant Started” can remain visible in Telegram without a bot response.
- Root cause: the bot client received updates before plugins and persisted authorization state were loaded. Additionally, the outer `app.sudoers` filter used only its in-memory ID set, so a MongoDB fallback inside the handler could never run for a cache miss.
- Fix pattern: register handlers and warm authorization filters before starting the receiving client; allow the sudo filter to refresh persisted IDs; guard restart replay/concurrency; convert log-send exceptions into safe replies.
- Prevention: preserve the tested startup order `authorization → plugins → app.boot → assistants`, and keep `test_sudo_filter_and_startup_order` in the smoke suite.

## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX_3 and AnonX_3 .env).

## Thumbnail Card Bypass / Raw Default Photo (2026-07-09)
- turn_id: 2026-07-09-thumbnail-card-fallback
- Symptom: `/play` now-playing message can show a plain configured/default photo instead of the expected generated 1280x720 music card with blurred background, title, duration, and controls.
- Root cause: caller-side `can_generate_card` gate returned `False` for sparse media objects, causing direct `default_thumb` usage. Additional risk existed when media IDs contained path-unsafe characters or remote thumbnail download failed.
- Fix pattern: if `THUMB_GEN=True`, call `thumb.generate()` for now-playing/queue media and keep raw default images only as internal card backgrounds. Sanitize thumbnail cache keys and provide layered fallback sources before rendering.
- Prevention: never send `default_thumb` directly from playback UI when the intended surface is the music card; generator fallbacks should produce a card even with missing/failed source thumbnails.

## Thumbnail Card Falls Back To Default Image (2026-07-09)
- turn_id: 2026-07-09T13:27:27+06:30-thumbnail-card-fix
- Symptom: `/play` now-playing message can show the configured default thumbnail/photo instead of the generated card based on the song thumbnail.
- Root cause pattern: `Thumbnail.generate()` catches all exceptions and returns `default_thumb`, so render-path errors or bad remote image payloads appear to users as "thumbnail not working" with no clear log.
- Fix pattern: keep the async render path import-safe, validate remote thumbnail HTTP/image payloads before Pillow rendering, and log failed source/card generation before falling back.
- Prevention: future thumbnail changes must run `py_compile` and a live `/play` visual check with both a normal YouTube query and a direct URL. Confidence: 96%.

## Startup/Search/CDN Warning Cluster (2026-07-23)
- Symptom: logs repeat `VideosSearch ... unexpected keyword argument 'proxy'`, `Client has not been started yet`, optional FastAPI import warnings, or `Download finished but file not ready` even though CDN publication succeeded.
- Root causes: dependency call signatures differ across `py-yt-search` versions; import-time supervised tasks can run before Telegram clients boot; an optional API was default-on; and CDN promotion atomically moves the completed file while another waiter retains its old path.
- Fix pattern: feature-detect optional provider arguments, gate import-time network tasks on client readiness, keep optional services opt-in, and re-resolve the canonical ready path before declaring a completed download missing.
- Prevention: retain `test_log_regression_guards`, run the smoke suite after startup-order changes, and verify the post-restart VPS log contains neither the old compatibility warning nor a pre-start audience-sync warning.

## First Song Takes 20–40 Seconds (2026-07-23)
- Symptom: `/play` remains on Searching/Downloading for many seconds before the assistant starts playback.
- Root cause pattern: broad YouTube `bestaudio` may resolve WebM/Opus that the call backend rejects; warm and play tiers can duplicate extraction; a provider deadline mismatch can cancel a nearly-complete search and trigger a deeper repeat; direct failure branches can wait for the same local task more than once.
- Fix pattern: prefer the call-compatible M4A/AAC direct ladder, normalize audio inflight keys, use one search race, await the actual local task once, and move Telegram logging/status repair/thumbnail rendering off or alongside the critical path.
- Prevention: retain `test_fast_first_play_guards`; keep state transitions event-driven rather than sleep/window-driven; after deployment verify trace order `ack → source_ready → playback_dispatch → voice_started`. External provider/network latency remains variable.

## `/logs` Silently Ignored While `/log` Is Claimed Elsewhere (2026-07-23)
- Symptom: plural `/logs` gets no reply, while singular `/log` produces a category-settings response from another bot.
- Root cause: the unrelated singular-command response is a third-party collision. Independently, this bot's broad text-filter watcher and `/logs` command shared handler group 0, and plugin iteration used a `frozenset` that made registration order nondeterministic.
- Fix pattern: keep plugin import order deterministic, assign broad observers to later dedicated groups, and place recovery/admin commands in an earlier group with handler-owned authorization.
- Prevention: retain the `/logs` assertions in `test_log_regression_guards`; never convert the sorted module tuple back to a set; restart the bot after changing handler decorators.

## All Sudo Commands Silently Ignored (2026-07-23)
- Symptom: multiple otherwise unrelated owner/sudo commands remain visible without any bot reply even though startup logs confirm the owner and sudo IDs loaded.
- Root cause: protected handlers shared Pyrogram group 0 with broad message watchers. Pyrogram executes only the first matching handler in a group, so a broad watcher could consume the update before the command handler; returning early inside that watcher does not resume same-group dispatch.
- Fix pattern: retain authorization filters, but register all protected commands in dedicated early `group=-1`; keep broad observers in later positive groups.
- Prevention: retain the AST regression test across every plugin and restart the process after decorator changes. If authorization itself fails, check for `Sudo authorization refresh failed` separately rather than treating it as a dispatch collision.

## Keyword Filter UX Stores Unwanted Per-Keyword Replies (2026-07-23)
- Symptom: `/filter` exposes add/remove/list/clear/settext subcommands and requires a separate reply string for every keyword, making simple message deletion cumbersome.
- Root cause: filter storage coupled keyword membership to the response body (`{keyword: reply_text}`).
- Fix pattern: treat stored keys as the rule set, use one runtime `filter_warning` template, and keep legacy dictionary keys readable during migration.
- Prevention: retain `test_simple_delete_filter_guards`; do not reintroduce per-keyword response values. Send the shared warning unquoted after deleting the source message so Telegram never references an already-deleted message.

## Filter Violations Lack Escalation and Admin Controls (2026-07-23)
- Symptom: repeated filtered messages are deleted but never escalate, the warning cannot identify the matched keyword, and admins have no direct mute/unmute control.
- Root cause: the delete-only matcher had no per-user violation state or moderation callback path.
- Fix pattern: persist per-chat/user strikes, render safe dynamic placeholders, auto-mute on the third strike, and use chat-bound admin-authorized inline callbacks with styled Mute/Unmute states.
- Prevention: retain `test_filter_strike_moderation_guards`; never schedule auto-deletion for a muted message containing the only Unmute control, and never trust callback target/chat IDs without authorization and source-chat validation.

## YouTube Requests Signed-In / Robot Verification (2026-07-24)
- Symptom: yt-dlp or direct extraction reports that YouTube requires sign-in or bot verification, while a manually copied `cookies.txt` becomes stale.
- Root cause pattern: a static cookie file has no browser session lifecycle; re-exporting a revoked profile cannot recreate authentication.
- Fix pattern: use a dedicated legitimately signed-in VPS browser profile, warm it through a normal YouTube visit, then export and atomically validate cookies at startup, periodically, near expiry, and after bot-check failures.
- Prevention: never store browser passwords or cookie values in logs, never overwrite a valid cookie file with a failed export, and never represent the agent as a CAPTCHA/session bypass. Retain `test_local_cookie_agent_guards`.

## Configured Cookie Profile Falls Back To Chromium Default (2026-07-24)
- Symptom: logs report `could not find chromium cookies database in "/root/.config/chromium"` even though `COOKIE_BROWSER_PROFILE=/root/youtube-profile` is configured.
- Root cause: export honored the configured profile, but direct, search, and download options independently rebuilt `cookiesfrombrowser` with a hardcoded `None` profile.
- Fix pattern: construct every yt-dlp browser tuple through `_browser_cookie_spec()` and retain the configured profile in all paths.
- Prevention: regression tests reject `(self.cookie_browser, None, None)` and require the shared helper at each extraction/download call site.

## Cookie-Free Configuration Still Touches Browser Cookies (2026-07-24)
- Symptom: operators remove `cookies.txt`, but yt-dlp repeatedly searches a Chromium database and cookie refresh remains in startup/retry paths.
- Root cause: `AUTO_COOKIE_ENABLED` controlled refresh only; browser fallback and manual/startup cookie inputs were independent.
- Fix pattern: use the default-on `COOKIE_FREE_MODE` gate at every cookie boundary, including startup, browser detection, file lookup, URL import, uploads, refresh scheduling, and bot-check retry.
- 25-Jul-2026 refinement: do not weaken the normal cookie-free gate to solve an auth challenge. Use the separate `COOKIE_AUTH_RECOVERY_ENABLED` boundary (enabled by default but inert unless the remaining gates are explicitly configured), require `AUTO_COOKIE_ENABLED=True`, an explicit supported browser, and a dedicated non-empty profile, and pass `auth_recovery=True` only from classified auth/403 recovery paths. Normal lookups remain cookie-free and do not poison browser detection needed by the later authorized recovery.
- Requested-format failures inside the download strategy loop must continue to the next selector. Breaking immediately consumes an outer attempt and can turn a locally recoverable format mismatch into a later YouTube auth challenge.
- Final-attempt auth challenges must not schedule recovery through the ordinary retry loop: `attempt=3/3` has no next slot. After a validated real-time export, execute exactly one dedicated authenticated strategy immediately under the same resource semaphores, then succeed or fail terminally.
- Prevention: retain the cookie-free smoke guards and never infer that an optional PO-token provider is active without successful provider logs.

## Auth Recovery FORMAT Opens a Global Outage (2026-07-27)
- Symptom: a download first reports `class=format`, later reaches a real auth challenge, then the browser-authenticated retry ends as `class=format`; the process nevertheless opens the global YouTube auth circuit and unrelated songs immediately fail. SoundCloud fallback can also remain unavailable after several ordinary zero-result searches.
- Root causes: the authenticated branch retried only the selector that had already failed and unconditionally remembered the earlier auth error after any recovery failure. Separately, an empty SoundCloud search was counted as a provider-health failure, so normal query misses opened the process-wide source circuit.
- Fix pattern: authenticated recovery tries both the current and permissive selectors. Its final error is reclassified: only a final explicit `AUTH_CHALLENGE` opens the auth circuit, while `FORMAT` returns to the bounded format/client retry ladder. A successful empty SoundCloud response records provider health success and returns `no_candidates` without incrementing failures.
- Prevention: retain the executable auth-reclassification and empty-result source-health regressions in `test_youtube_auth_challenge_short_circuit` and `test_soundcloud_proxy_and_video_guards`.

## Chromium Recovery Configured in Code but Inactive at Runtime (2026-07-27)

- Symptom: YouTube reports `Sign in to confirm you're not a bot`, then
  SoundCloud fallback ends in the generic `Download failed` card.
- Root causes: challenge recovery required an explicit browser/profile but the
  shipped sample/container left the profile empty and did not install Chromium.
  Late SoundCloud fallback also rebuilt an over-specific query from YouTube
  metadata instead of using `media.original_query`; proxy download/direct-stream
  paths could return before their direct attempt. For the reported Burmese
  track, the direct SoundCloud candidate used a different uploader and a
  `Remix Version` suffix, so the normal 0.85 weighted matcher still rejected it.
- Fix pattern: install optional Chromium, mount one dedicated legitimately
  signed-in profile, and inject explicit challenge-only recovery settings.
  Bound SoundCloud proxy/direct metadata attempts, explicitly clear inherited
  proxy state for direct yt-dlp calls, continue after empty proxy results, and
  try the original non-URL query first. If normal scoring rejects that same
  query attempt, a narrow Unicode-mark-preserving rescue accepts only one
  distinct version-labelled title prefix with known durations within ten
  seconds and 5%; derived queries, URLs, ambiguity, and unknown durations fail
  closed.
- Prevention: retain cookie-wiring, proxy/direct option-order, download/stream,
  Burmese diacritic-collision, duration, ambiguity, original-attempt, and
  `/vplay` smoke guards. Never package cookie/profile data, log cookie values,
  auto-discover a personal profile, or claim that Google cannot revoke a
  session.

## YouTube Media Download Returns HTTP 403 / `class=client_po` (2026-07-24)
- Symptom: extraction reaches a Google media URL, then yt-dlp reports `unable to download video data: HTTP Error 403: Forbidden`; the retry log labels it `class=client_po`.
- Root causes: `client_po` is an error class, not the selected YouTube client. The provider integration used the obsolete `CLIENT+TOKEN` form instead of `CLIENT.CONTEXT+TOKEN`, cached video-bound GVS tokens globally across different videos, and retried equivalent yt-dlp options after a 403.
- Fix pattern: use `mweb.gvs+TOKEN`, cache/invalidate tokens per video ID, discard only that video's partial artifacts, refresh once, then rotate to cookie-free client sets excluding known problematic Android clients. Keep retries bounded and fall through to the existing alternate-source path.
- Prevention: retain `test_po_token_video_binding_and_403_rotation`; log only recovery mode and media ID, never token values. A video may still be unavailable, private, geo-blocked, deleted, or IP-blocked, so no YouTube path is guaranteed.

## Cancel Produces “Already Cancelled”, Leaves Searching, or Platform Playback Fails (2026-07-24)
- Symptom: pressing Cancel edits/cancels once but then shows `Download already cancelled`; the status card may remain, and Telegram/Facebook requests can fall into a generic `play_error` or `NoVideoSourceFound`.
- Root causes: `Telegram.cancel()` performed UI work but returned no success boolean; the shared callback only knew YouTube/Telegram tasks; warm search, handler, TikTok, and Facebook branches were not request-bound. Telegram remote media also entered the YouTube prefetch path, Facebook audio direct streams requested video auto-detection, and Telegram audio could be mislabeled as video by `/vplay`.
- Fix pattern: make the callback the single UI owner, acknowledge silently, delete the transient card first, cancel every task bound to its message ID, and make service cancel methods return booleans without rendering. Keep non-YouTube media out of YouTube prefetch and preserve actual source capabilities when constructing `MediaStream`.
- Prevention: retain `test_cancel_lifecycle_and_platform_recovery_guards`; expected missing-video/file conditions must render a specific terminal status and never a second generic error card.

## TikTok Playback Skips With `NoAudioSourceFound` (2026-07-24)
- Symptom: a TikTok request reaches voice-chat playback, then reports `Moving to the next track... Audio source not found in the file.`
- Root causes: the audio branch accepted the first matching artifact, including stale/partial or video-only files, and passed an unverified WebM directly to ntgcalls.
- Fix pattern: request formats with a real audio codec, normalize audio downloads to M4A/AAC, reject `.part`/`.ytdl` artifacts, and verify the required audio stream (plus video for `/vplay`) with `ffprobe` before publishing or reusing a file. Purge only invalid artifacts for that media ID and keep retries bounded.
- Prevention: retain `test_tiktok_audio_artifact_guards`; a file's extension or existence alone never proves it is playable.

## TikTok `.part -> .mp4` Rename Fails With ENOENT (2026-07-24)
- Symptom: yt-dlp reaches 100%, then reports `Unable to rename file: [Errno 2] No such file or directory`; concurrent fallback attempts emit repeated `Requested format is not available`.
- Root cause: cancellation removed the in-flight registry while `asyncio.to_thread()` could not stop its yt-dlp worker. A new request then purged the still-active `.part` file, and multiple audio/video/CDN paths could write the same media-ID prefix.
- Fix pattern: serialize all TikTok writes by media ID through a cancellation-shielded process-local singleflight, run cleanup only inside its owner, retain ownership until the worker thread exits, and cancel request waiters without cancelling shared singleflight/CDN owners.
- Prevention: the TikTok smoke guard now simulates owner cancellation plus a surviving waiter and proves only one yt-dlp operation runs. Do not move cleanup ahead of flight ownership or key the write lock by audio/video mode.

## Owner-Disabled YouTube Upload Repeats, Then SoundCloud Proxy Returns 502 (2026-07-24)
- Symptom: the same YouTube ID repeatedly reports `Playback on other websites has been disabled by the video owner`; subsequent SoundCloud fallback reports `Unable to connect to proxy` / `Tunnel connection failed: 502 Bad Gateway`.
- Root causes: independent follow-up download paths did not remember permanent media-ID failures, fallback crossed providers before looking for another upload, and omitting yt-dlp's proxy option on the direct attempt could still inherit `HTTP(S)_PROXY`.
- Fix pattern: retain a bounded six-hour negative cache for permanent/region failures, exclude those IDs from cached/deep search, try another YouTube upload once, then use SoundCloud. Set yt-dlp `proxy=""` for an actual direct attempt and make that attempt only after a proxy transport failure.
- Prevention: keep retries bounded and never attempt to bypass the owner's embedding restriction. Retain targeted negative-cache/proxy checks and `test_parallel_external_source_guards`.

## Custom Downloading Card Never Shows Live Progress (2026-07-24)
- Symptom: the configured `play_downloading` text and Cancel button appear, playback may start, but the card never gains a progress bar or percentage.
- Root causes: warm local download could begin before the base Downloading edit completed, allowing that older edit to overwrite a newer progress render; a late watcher could attach to the outer prefetch wrapper instead of the real yt-dlp task; and yt-dlp's final callback may omit byte fields, causing the final render to be skipped.
- Fix pattern: commit the base card before starting the local worker, attach watchers by media ID to the actual in-flight download task, retain last-known progress, infer totals from yt-dlp percentage hints, and use the completed file size for the terminal 100% render.
- Prevention: keep one-second event-driven throttling, preserve custom text/entities and Cancel markup, and retain the worker-watcher/order assertions in `test_parallel_external_source_guards`.

## Supplied Runtime Log: Remaining Error Clusters (2026-07-24)
- Symptom: the latest log contained `NoTextToEdit`, requested-format failure followed by permanent skips, a stale SoundCloud proxy 502, and duplicate voice-chat leave failures; smoke-test circuit warnings were mixed into the same file.
- Root causes: progress updates assumed every status was a text message; a broad permanent-error pattern captured format-only failures; successful empty direct searches retained an earlier proxy exception; stop cleanup lacked a per-chat idempotency boundary; tests imported the production rotating logger.
- Fix pattern: route edits by message media type and detach stale watchers; classify format mismatches as retryable; clear stale provider exceptions after any successful direct attempt; serialize stop cleanup and accept already-left outcomes; initialize tests with a null log handler.
- Adjacent hardening: direct URLs are codec-validated, SoundCloud video fallback is blocked, misleading `py_yt` proxy signatures recover quietly, known bot dialog limitations are skipped, and unavailable RAQM is no longer requested.
- Prevention: retain the four behavioral regression tests added to `tests/run_unit_smoke.py`. Do not rewrite or delete the forensic log; isolate future tests and compare its hash when diagnosing pollution.
- Deployment discriminator: the old traceback maps line 1938 to `edit_text()`, while the fixed file maps that line to the media-caption branch. If that old frame reappears, deploy the current archive and fully restart instead of changing the already-correct caller.
- Typed-exception regression: the smoke suite now raises the real `BotAPI.NoTextToEdit`, verifies both direct Utilities and Bot API routes preserve entities/styled markup, and asserts no exception traceback is logged.

## Now Playing Card Shows `DOWNLOADING... 100%`
- Symptom: the message has the generated Now Playing image and active playback timer/buttons, but its caption is still the custom Downloading header plus a full progress bar.
- Root cause: direct playback won while its parallel local-cache download continued. `update_now_playing()` repurposed the status message, then a retained progress watcher emitted its terminal `100%` edit. The text-to-caption fallback let the stale wrapper edit the new media caption, and the timer later restored playback markup.
- Fix pattern: enforce immutable per-message UI ownership. Route every progress edit through the shared guard, close and drain progress writes before `play_media`, and detach the YouTube watcher without cancelling the underlying download.
- Prevention: retain the deterministic in-flight handoff regression in `test_youtube_live_progress_pipeline`; no fixed delay, fake progress, or background-download cancellation is permitted.

## Direct YouTube Link Shows `YouTube Video` and `00:00`
- Symptom: `/play <youtube-url>` or `/vplay <youtube-url>` starts successfully, but the stream/queue card contains the placeholder title `YouTube Video` and duration `00:00`.
- Root cause: the direct-URL search fast path allowed only a 350 ms lightweight metadata lookup and returned a placeholder on timeout. The parallel yt-dlp direct-stream extraction then obtained the full info dictionary but discarded every field except the playable URL.
- Fix pattern: normalize and cache display metadata from that same direct extraction, await its existing singleflight on a lightweight-provider miss, and construct the Track before card rendering. Keep the 350 ms fast-provider budget and use no duplicate extraction.
- Prevention: retain `test_direct_youtube_metadata_propagation`, covering duration normalization, audio/video Track construction, and the async direct-search handoff.

## Manual Restart Raises `RecursionError` in `Task.cancel()`
- Symptom: `/restart` logs `Exception in callback Timeout._on_timeout()` followed by hundreds of recursive `Task.cancel()` frames.
- Root cause: the Pyrogram command handler awaited `stop()`. Shutdown then called `app.exit()`, whose dispatcher shutdown waited on the same handler execution chain; timeout cancellation recursively traversed that cycle.
- Fix pattern: keep a strong reference to a detached restart coordinator, return from the command handler, and let the coordinator perform bounded shutdown and process replacement.
- Prevention: retain `test_manual_restart_detaches_shutdown_from_handler`; never await main-client shutdown directly inside one of that client's command handlers.

## `/song` and `/vsong` Do Not Complete
- Symptom: valid song commands show usage, lose their query, or fail silently during resolve, download, conversion, thumbnail, cache reuse, or upload.
- Root causes: command parsing trusted only one framework-populated field; the handler used the default dispatch group; reply URLs could override explicit queries; partial MP3 files were reusable; optional thumbnail failures could abort video delivery; and the direct Pyrogram `Message.reply_text()` path received the Bot API dictionary returned by `cancel_dl()`, causing `AttributeError: 'dict' object has no attribute 'write'`.
- Fix pattern: parse both structured and raw command text, dispatch early, preserve explicit queries, pass progress/cancellation context into the shared downloader, convert through an atomic partial file, make thumbnails best-effort, log each failed pipeline stage, and use `cancel_dl_pyrogram()` whenever markup goes directly to a Pyrogram message method. Retain `cancel_dl()` only behind Bot API-aware Utilities methods.
- Prevention: retain `test_song_command_pipeline_guards` and keep the enabled command usage synchronized in `locales/en.json`.

## Resolved: Final Smoke Blocked by Obsolete Proxy-Signature Guard

- Symptom: runtime proxy compatibility passed, but the full suite stopped at
  46/47 because `test_log_regression_guards` required the removed
  `inspect.signature` source text.
- Root cause: the implementation had intentionally moved to a safer runtime
  `TypeError` fallback for py-yt-search builds that advertise `**kwargs` yet
  reject `proxy`; the older source-shape assertion was not updated.
- Fix pattern: assert the exact bounded fallback order in source and retain the
  executable two-constructor regression proving proxy-first and direct retry.
- Adjacent release fix: corrected each distributable `sample.env` variant and
  Mongo database name to prevent accidental sibling-database reuse.
- Prevention: final release requires 47/47 smoke in all four variants before
  archives are rebuilt.

## Resolved: SoundCloud Timeout Storm and DRM Search Abort (2026-07-28)

- Symptom: `scsearch` printed a raw DRM error for one result, returned no
  candidates, then repeated proxy and direct requests until the outer play
  deadline while timed-out yt-dlp workers continued logging.
- Root cause: search used full extraction for every result, yt-dlp retained
  three internal retries inside an equally sized outer timeout, transport
  failures collapsed into ordinary empty results, and fallback retried its
  derived query against the same unavailable provider.
- Fix: use flat discovery, capture yt-dlp diagnostics, disable internal
  metadata retries, distinguish transport outages from empty results, and
  probe a bounded ranked candidate set before accepting a SoundCloud track.
- Prevention: `test_soundcloud_proxy_and_video_guards` now covers flat search,
  explicit DRM filtering, DRM-first candidate recovery, terminal direct-stream
  DRM, and one-shot transport failure accounting.

## Resolved: Final Artifact Identity Drift (2026-07-28)

- Symptom: the verifier expected `AnonX_3-v3.2.0-final.zip`, while `dist/`
  contained a sibling-named archive; setup, `sample.env`, fallback text, and
  environment merge code also retained sibling identities.
- Root cause: release identity and version were duplicated across operational
  files, and the environment merge utility traversed the parent workspace.
- Fix: central release metadata, canonical active branding, current-root-only
  atomic environment merge, exact dependency lock, and one deterministic gate.
- Prevention: executable identity/packaging guards and manifest validation fail
  before a mismatched artifact can be published.

## Resolved: `/play` YouTube Auth Challenge Loop (2026-07-28)

- Symptom: `/play` ended with the generic `Download failed` card while the log
  showed a browser-cookie export, one failed authenticated retry, a 180-second
  auth circuit, repeated circuit skips, and an empty SoundCloud fallback.
- Root causes: the cookie watcher could overwrite yt-dlp's decrypted jar with
  raw SQLite rows whose modern Chromium values were encrypted/empty; cookie
  names alone were counted as authenticated; and authenticated recovery reused
  the configured proxy even though the local browser session could be bound to
  the VPS direct egress identity.
- Fix: watcher refreshes now call the canonical yt-dlp browser exporter; empty
  values are rejected; raw encrypted rows never replace the jar; recovery tries
  direct and configured-proxy transports before opening the circuit; repeated
  same-track circuit logs are deduplicated; and the affected request receives a
  specific browser-session message.
- Prevention: retain the empty-cookie health regression and the executable
  `proxy -> direct -> proxy` auth-challenge test in `tests/run_unit_smoke.py`.
