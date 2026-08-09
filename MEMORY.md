# Project Memory

## Current verified state — 2026-08-09

- Active project/package: `AnonX_3`; entry point: `python -m AnonX_3`.
- Initial cold YouTube `/play` and `/vplay` now prepare VC join plus mandatory
  assistant unmute concurrently with raw-source resolution, then submit an
  observed raw PyTgCalls `Stream` in an owned task. This bypasses the installed
  `MediaStream.check_stream()` remote ffprobe/capability scan on this cold path.
  Exact play/FFmpeg/input/decode/attach/first-packet events are logged; only the
  non-network URL/SSRF boundary remains before submission. Cache/download and
  profile/UI work begin after outgoing packet evidence. This is intentionally
  not applied to queued, seek/replay, skip, established-call, forced, or
  non-YouTube paths, and the existing local fallback remains authoritative.
- Automated change validation passes compile, parallel/unmute/proof regressions,
  the executable FFmpeg relay test, 28/28 stream scaling, 26/26 resource
  control, recursion, structure, locale, and secret checks. Current full smoke
  is 86/87 with the unchanged pre-existing one-shot concurrent-download
  failure; the production `direct_startup_event` timeline and live Telegram
  audibility remain deployment checks.
- Current final release: `3.4.10`.
- v3.4.10 isolates Telegram presentation failures from playback rollback. A
  deleted queued card preserves queue state and clears stale message IDs.
- The first process-wide `YoutubeDL` constructor is now single-flight to protect
  yt-dlp's global plugin registry; all later constructors and extraction remain
  concurrent. A full process restart is mandatory after deployment.
- v3.4.9 removes Telegram acknowledgement and VC-presence lookup from the
  serial startup path. Authorized initial YouTube requests use an exclusive
  admission lease plus command-level EXTERNAL preconnect, then transfer the
  exact transport to the resolved Track with `reconnect=0`. `/vplay` proves an
  external-audio outgoing-clock tick before its raw A/V swap.
- Micro player defaults now follow yt-dlp authentication policy
  (`tv_downgraded`, `web_safari`, `android_vr`) and pass client default ytcfg.
  The live acceptance contract is p95 <=1500 ms for resolver, packet tail, and
  command-to-packet over 20 fresh `/play` and 20 `/vplay` samples; it is not yet
  proven by the pre-change production log.
- The direct YouTube player-response fast lane now recovers only safe signed
  envelopes (never encrypted `s` challenges), caps fetch + GVS proof at 1450
  ms, and races both authoritative full lanes through 200/206 validation.
- Local regression/release gates prove control flow and packaging. The <=1.5s
  `search` -> `play_task_scheduled` p95 remains a production acceptance gate
  requiring fresh uncached logs; it is not inferred from synthetic timings.
- Current identity migration is complete: package, project root, imports, paths,
  environment keys, docs, and configs use `AnonX_3`.
- The deep directory/package rename and identity-wide reference migration were
  completed with generated runtime data preserved; the active entry point is
  `python -m AnonX_3`.
- Rename validation passed: compileall, package/entry-point imports, 69/69
  smoke tests, recursion checks, structure verification, and residual scan.
- Telegram inline-search handlers and configuration are intentionally removed;
  ordinary inline keyboards and the play/download commands remain.
- Performance hot paths use early acknowledgement, Mongo negative caching,
  bounded YouTube search caches, singleflight, HTTP session reuse, and
  non-blocking FFmpeg/FFprobe execution.
- `/broadcast -pin` is opt-in for silent group/supergroup pinning; private users
  are never pinned, and pin failures are reported without stopping delivery.
- Deep identity migration now targets `AnonX_3`; protected runtime artifacts
  remain preserved and the active entry point is `python -m AnonX_3`.

## Fixed in 3.3.1

- YouTube search now retains valid slow provider results and uses one bounded
  yt-dlp metadata fallback before reporting `play_not_found`.
- Auto-learn stores pending candidates and requires two matching observations by
  default before activation; explicit `/reply` remains immediate and manual
  rules cannot be overwritten by automatic learning.
- Authenticated YouTube recovery now drops a stale provider-injected client
  binding unless a concrete PO token is present, while direct provider-bound
  options remain preserved.
- Release identity, deterministic archive metadata, and the Python 3.13 lock
  are aligned for `AnonX_3-v3.3.1-final`. The full release gate passed with
  69/69 smoke tests and a 249-member archive; the generated `.sha256` sidecar
  is the authoritative digest.

## Fixed in 3.3.0

- **TikTok & Facebook resolve/download failures**: Both modules now pass
  `nocheckcertificate`, `cookiefile` (shared `AnonX_3/cookies/*.txt`),
  browser User-Agent headers, and `socket_timeout` to every yt-dlp call
  (`resolve`, `resolve_direct_stream`, `download`). Retries raised from
  2→3. Error paths log the real exception instead of swallowing it.
  Missing `FACEBOOK_DIRECT_*` env vars added to `.env`.
- **Process-level crash auto-restart**: `__main__.py` now wraps `main()` in
  an exponential-backoff restart loop (2s→60s).  Survives clean shutdown
  (SystemExit code 0, KeyboardInterrupt).  Global `sys.excepthook` catches
  unhandled thread exceptions and writes to stderr + file log.
- **Bot connection health monitor**: New `_periodic_bot_health()` task pings
  Pyrogram client every 30s; auto-reconnects bot + userbot clients on
  disconnect.
- **MongoDB resilience**: `AsyncMongoClient` now configured with
  `retryWrites`, `retryReads`, connection pooling (2-20), and socket
  timeouts.  Added `_retry_operation()` helper for transient network/
  timeout/replica-set errors (3 retries, exponential backoff).
- **Plugin crash isolation**: Plugin imports in `main()` are now individually
  try/except wrapped — one broken plugin won't prevent the bot from booting.
- **asyncio loop guard**: Custom `_loop_exception_handler` no longer calls
  `default_exception_handler` (which can `os._exit()`).  Unhandled task
  exceptions log but never kill the process.
- **CancelledError noise filter**: `TransientRuntimeNoiseFilter` suppresses
  `CancelledError` log spam during normal shutdown.
- **Cookie watcher `FileNotFoundError`**: `_extract_cookies_to_netscape` now
  creates `cookies/` directory before `shutil.copy2`.  Three-layer defense:
  `ensure_dirs()` at startup → `start()` mkdir → `_extract` mkdir + stale
  cleanup.  All `__pycache__/` cleared to prevent stale-bytecode masking.
- **`RecursionError` on task cancel**: Python 3.12 `Task.cancel()` recursively
  walks `_children`.  Deep trees from supervisor restarts overflowed the stack.
  Fix: `stop()` uses iterative `_collect_all_tasks()` + clears `_children`
  before cancel; `supervisor._runner` clears children on `CancelledError`.

- Release validation baseline: compile OK (all .py), secret scan OK, 0 stale
  `AnonX_3` references.
- Build with `python ops/build_release.py`; verify with
  `python ops/verify_release.py`. The release archive is allowlisted and excludes
  live environment files, credentials, sessions, cookies, logs, and media/cache
  artifacts.

Store durable project facts, conventions, and decisions here.
# Dynamic Master Memory

> Purpose: Maintain durable, accurate context across sessions while keeping each project's memory isolated and loading only relevant information.

## 1. Startup Protocol — Always Run

At the beginning of every new session or conversation:

1. Read this `MEMORY.md` completely.
2. Detect the active project from the current workspace, repository name, folder structure, configuration files, and the user's request.
3. Load only the memory files relevant to the active project.
4. Read the latest entries in `memory/session-log.md` and `memory/current-state.md`.
5. Restore unresolved tasks, confirmed decisions, conventions, constraints, known bugs, and the last working state.
6. Never mix facts from unrelated projects.
7. If the active project is uncertain, inspect the workspace first and state the inferred project before making changes.

## 2. Memory Scope

### Global durable memory
Store information that remains useful across projects:

- User communication and output preferences
- Stable coding conventions
- Preferred tools and workflow
- Security and privacy rules
- General quality expectations

Read: `memory/preferences.md`

### Project durable memory
Store information specific to the active project:

- Project purpose and architecture
- Technology stack and folder structure
- Important files and entry points
- Confirmed requirements
- API contracts and data models
- Environment and deployment conventions
- Known bugs and verified fixes
- Decisions and their reasons
- Pending tasks and next actions
- Last known working state

Read: `memory/projects.md`, `memory/decisions.md`,
`memory/current-state.md`, and `memory/pending-tasks.md`

### Session history
Store a compact chronological summary of each meaningful session:

- Date and project
- User objective
- Files inspected or changed
- Decisions made
- Commands or tests run
- Results
- Remaining issues
- Exact next step

Read recent relevant entries from: `memory/session-log.md`

## 3. Dynamic Retrieval Rules

- Search memory by project name, feature name, file path, error text, command, and decision.
- Load the smallest relevant set of memory entries first.
- Expand to older session entries only when needed.
- Prefer confirmed recent facts over older conflicting facts.
- Mark uncertain or unverified information explicitly.
- Never invent missing history.
- When two memories conflict, inspect the current project files and ask only when the conflict cannot be resolved safely.

## 4. Automatic Memory Update Rules

During and after every meaningful task:

1. Extract only durable facts, confirmed requirements, decisions, fixes, conventions, and unresolved work.
2. Update an existing entry instead of creating duplicates.
3. Replace obsolete facts and preserve a short reason when a decision changes.
4. Add a dated session summary to `memory/session-log.md`.
5. Update `memory/current-state.md` with the latest verified state.
6. Update `memory/pending-tasks.md` with clear next actions.
7. Keep this index concise; put details in linked topic files.
8. Do not store casual chat, temporary guesses, or redundant explanations.

## 5. Security Rules

Never store:

- API keys, access tokens, passwords, cookies, OTP codes, private keys, or recovery codes
- Full credentials copied from `.env` files
- Sensitive personal data unless the user explicitly asks to preserve it

Store only safe references such as:

- `API key is provided through environment variable`
- `Secret name: ANTHROPIC_API_KEY`
- `Credential location: server environment`

If a secret appears in a conversation, redact it in memory.

## 6. Work Continuity Protocol

Before editing code:

1. Restore the latest project state from memory.
2. Inspect the actual current files; memory is guidance, not proof.
3. Check previous failed attempts before repeating a fix.
4. Preserve confirmed working behavior unless the user requests a change.
5. Record important test results and rollback information.

At the end of the task, update:

- What changed
- What was verified
- What remains incomplete
- The exact next action
- Any command needed to continue

## 7. Project Isolation

Each project must have a unique section in `memory/projects.md`.

Use this key format:

`project_id: <normalized-workspace-or-repository-name>`

Never apply one project's:

- Architecture
- Environment variables
- Commands
- File paths
- Business rules
- Bugs or fixes

to another project unless the user explicitly requests reuse.

## 8. Old Conversation Import

This memory system cannot reconstruct conversations it has never received.

When an old conversation, transcript, exported chat, or summary is provided:

1. Identify the related project.
2. Extract durable facts and decisions.
3. Merge them without duplication.
4. Add a dated migration note to `memory/session-log.md`.
5. Mark uncertain information as `unverified`.
6. Never claim an old conversation was imported unless its content was actually available.

## 9. Response Behavior

When relevant memory exists:

- Use it silently to continue the work.
- Do not ask the user to repeat already confirmed information.
- Mention a remembered constraint only when it affects the current decision.
- Clearly distinguish remembered facts from newly inferred facts.
- If memory and current files disagree, trust verified current files and update memory.

## 10. Linked Memory Files

- [User Preferences](memory/preferences.md)
- [Project Registry](memory/projects.md)
- [Decision Log](memory/decisions.md)
- [Current State](memory/current-state.md)
- [Pending Tasks](memory/pending-tasks.md)
- [Session Log](memory/session-log.md)
