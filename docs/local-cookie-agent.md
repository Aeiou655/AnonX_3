# Local VPS Cookie Agent

Cookie-free mode is the default:

```env
COOKIE_FREE_MODE=True
```

In this mode AnonX_3 does not read or create cookie files, inspect browser
profiles, download `COOKIES_URL`, accept cookie uploads, start cookie refresh
tasks, or attach cookies to yt-dlp. Public media uses the normal client/proxy
path and an optional PO-token provider. Account-restricted content may be
unavailable.

There are two controlled modes:

- Keep `COOKIE_FREE_MODE=True` for challenge-only recovery. The recovery flag
  defaults on, but remains inert until an explicit supported
  `COOKIE_BROWSER` and non-empty `COOKIE_BROWSER_PROFILE` are configured.
  Normal public requests remain cookie-free. After YouTube explicitly requests
  authentication, AnonX_3 exports the configured profile in real time and runs
  one dedicated authenticated retry, even if all ordinary retries were used.
- Set `COOKIE_FREE_MODE=False` for full cookie-agent mode: startup, periodic,
  near-expiry, and bot-check refreshes.

Challenge-only recovery requires `AUTO_COOKIE_ENABLED=True`, a
supported explicit `COOKIE_BROWSER` value, and a non-empty
`COOKIE_BROWSER_PROFILE`. `auto` and an empty/default profile fail closed in
this mode so the bot cannot inspect an unrelated personal browser profile.

AnonX_3 can maintain `cookies.txt` locally without an API key or a remote
downloader service. It exports cookies from a dedicated browser profile at
startup, on a configurable schedule, near expiry, and after YouTube bot-check
failures.

## Important boundary

The agent cannot manufacture a signed-in Google session, bypass CAPTCHA, or
solve account verification. The VPS browser profile must first receive a
legitimate YouTube sign-in. If Google revokes that session, sign in again once
through a normal interactive browser session.

No Google password is stored in the bot configuration. Cookie values are never
written to application logs, and the generated cookie file is restricted to
the process owner where the operating system supports file modes.

## One-time VPS setup

1. Install a supported browser such as Firefox, Firefox ESR, Chromium, Chrome, Edge, or Brave.
2. Create a dedicated browser profile inside the VPS project or another
   process-owned directory.
3. Open that profile interactively through VNC/X11 and sign in to YouTube
   normally. Complete any verification shown by Google.
4. Close the interactive browser before starting the bot so its profile
   database is not locked.
5. Configure the bot:

```env
COOKIE_FREE_MODE=True
COOKIE_AUTH_RECOVERY_ENABLED=True
AUTO_COOKIE_ENABLED=True
COOKIE_BROWSER=firefox
COOKIE_BROWSER_PROFILE=/root/firefox-profile
COOKIE_BROWSER_WARMUP=True
COOKIE_BROWSER_TIMEOUT_SEC=30
COOKIE_REFRESH_SEC=21600
COOKIE_EXPIRY_WINDOW_SEC=604800
COOKIE_FAILURE_COOLDOWN_SEC=300
```

Use the actual browser ID and profile path on the VPS. `COOKIE_BROWSER=auto`
works only in full cookie-agent mode; challenge-only mode requires an explicit
supported browser ID and profile path.

## Docker setup

The production image installs Firefox by default (`WITH_FIREFOX=1`). Compose
binds a dedicated host directory to `/app/firefox-profile` and supplies the
explicit challenge-recovery settings. Set the host path before building:

```env
COOKIE_BROWSER_PROFILE_HOST=/root/firefox-profile
```

Create and sign into that dedicated profile from the same Linux host/security
context used by the container, then close the interactive Firefox process.
Do not copy a personal Windows/macOS profile into a Linux container: browser
cookie encryption and keyrings are not portable across those contexts.

The profile mount is read-write because Firefox may rotate its own session
state during the bounded warmup. Cookie text files and `firefox-profile/` are
excluded from Docker build context, Git, and release archives. To omit
Firefox from an image that will never use auth recovery, build with
`WITH_FIREFOX=0`.

## Runtime behavior

- Challenge-only mode: normal requests do not touch cookies. A direct-stream
  auth failure schedules refresh; the racing local-download recovery waits on
  the same lock, reuses that export, and retries once. An export without a
  recognized signed-in YouTube authentication cookie is rejected for recovery.
- Full mode startup: warms the configured profile by visiting YouTube, then
  exports its cookie database without extracting a video.
- Full mode periodic refresh: repeats using `COOKIE_REFRESH_SEC`; there is no
  request-flow sleep or fixed playback delay.
- Near expiry: replaces the current file only after the new export validates.
- Bot-check response: schedules a refresh for direct-stream failures and retries
  once for download failures.
- Failure safety: keeps the last valid `cookies.txt` if export fails and uses an
  atomic `.next` replacement on success.

The profile visit can keep a still-valid session active, but it cannot extend a
session that Google has revoked. yt-dlp PO-token/provider fallback remains
separate from this local cookie mechanism. Authenticated yt-dlp use can also
cause YouTube to restrict the dedicated account; keep request volume bounded
and never use a primary personal account.

## Expected log markers

Logs show browser identity, refresh reason, counts, and failure class only.
They must never show cookie names paired with values or browser database
contents.

After deployment, restart the bot and confirm a startup message similar to
`Cookie agent refreshed the local browser session` in full mode. In
challenge-only mode, that marker should appear only after a test `/play`
request produces the YouTube sign-in/robot challenge, followed by one
`next=authenticated_retry` marker.
