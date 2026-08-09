# Dynamic Cookie Watcher — Real-Time YouTube Authentication Sync

The Cookie Watcher automatically extracts cookies from a running Firefox browser instance and syncs them to the bot in real-time, fixing "Sign in to confirm you're not a bot" errors.

## What It Does

- **Monitors** a Firefox user-data-dir for cookie changes
- **Extracts** YouTube authentication cookies (SID, HSID, SSID, APISID, SAPISID, __Secure-*PSID)
- **Converts** from Firefox's SQLite format to Netscape cookies.txt format
- **Syncs** automatically on a schedule and **immediately** when bot detection occurs
- **Integrates** with existing cookie agent system for seamless operation

## How It Works

1. Cookie Watcher monitors `Cookies` database in Firefox profile
2. When cookies change or bot detection occurs, extracts cookies to Netscape format
3. Writes to `AnonX_3/cookies/cookies.txt` with atomic replacement
4. Bot automatically picks up the new cookies for next YouTube request
5. No restart needed — updates happen in real-time

## Setup

### Step 1: Create Dedicated Firefox Profile on VPS

```bash
# Create profile directory
mkdir -p /root/firefox-profile

# Launch Firefox with dedicated profile (one-time setup)
firefox --profile /root/firefox-profile --no-remote
```

**IMPORTANT:** Open YouTube in this Firefox instance and sign in with a dedicated Google account. Complete any 2FA or verification. Then close Firefox.

### Step 2: Configure AnonX_3

Edit your `.env` file:

```env
# Enable cookie watcher
COOKIE_WATCHER_ENABLED=True

# Point to the Firefox profile you created
COOKIE_WATCHER_USER_DATA_DIR=/root/firefox-profile

# Check interval (default: 60 seconds)
COOKIE_WATCHER_INTERVAL_SEC=60

# Only sync YouTube-related cookies (recommended)
COOKIE_WATCHER_YOUTUBE_ONLY=True

# Keep challenge-only mode for best performance
COOKIE_FREE_MODE=True
COOKIE_AUTH_RECOVERY_ENABLED=True
AUTO_COOKIE_ENABLED=True
```

### Step 3: Start the Bot

```bash
python -m AnonX_3
```

You should see in the logs:

```
Cookie watcher started: monitoring /root/youtube-profile every 60s
```

## Modes of Operation

### Mode 1: Challenge-Only Recovery (Recommended)

```env
COOKIE_FREE_MODE=True
COOKIE_AUTH_RECOVERY_ENABLED=True
COOKIE_WATCHER_ENABLED=True
```

- Normal requests stay cookie-free (fast, low detection risk)
- Only uses cookies when YouTube explicitly challenges
- Watcher extracts cookies on-demand when bot detection occurs
- Best balance of performance and reliability

### Mode 2: Full Cookie Mode

```env
COOKIE_FREE_MODE=False
COOKIE_WATCHER_ENABLED=True
```

- All requests use cookies
- Watcher syncs on schedule + on bot detection
- Higher detection risk but handles account-restricted content
- Use only if you need authenticated access for all requests

## How It Handles Bot Detection

When YouTube returns "Sign in to confirm you're not a bot":

1. Bot detects the auth challenge
2. Triggers `request_cookie_refresh(reason="bot-check")`
3. Cookie Watcher **immediately** syncs latest cookies from Chromium
4. Bot retries the request with fresh cookies
5. Request succeeds with authenticated session

The sync happens in **under 2 seconds** from detection to retry.

## Monitoring

### Check Cookie Status

```bash
# In bot, send to logger channel
/cookies
```

Shows:
- Valid cookies count
- Authenticated cookies (SID, HSID, etc.)
- Expiration dates
- Days remaining

### Logs to Watch

```
Cookie watcher extracted cookies total=127 authenticated=7
Cookie watcher sync completed cookies=127 generation=5
Cookie watcher triggered immediate sync for bot-check
```

## Troubleshooting

### "Cookie watcher could not find Firefox cookies database"

**Cause:** Wrong user-data-dir path or profile doesn't exist

**Fix:** Verify the path exists and contains a `cookies.sqlite` file:

```bash
ls -la /root/firefox-profile/cookies.sqlite
```

### "No cookies found in Firefox database"

**Cause:** No YouTube sign-in in that profile

**Fix:** Launch Firefox with that profile, visit YouTube, sign in, then close:

```bash
firefox --profile /root/firefox-profile --no-remote
# Visit youtube.com, sign in, close Firefox
```

### "Cookie agent found YouTube cookies but no signed-in authentication cookies"

**Cause:** Signed into YouTube but session cookies not present

**Fix:** Sign out completely, then sign back in. Make sure to complete any 2FA challenges.

### Bot detection still occurring

**Causes:**
1. Cookies expired
2. Google revoked the session
3. Too many requests from the account

**Fixes:**
1. Re-sign in to YouTube in the Chromium profile
2. Use a different Google account
3. Lower request volume
4. Add proxy support (see main docs)

## Docker Setup

### Dockerfile

The production image now installs Firefox:

```dockerfile
ARG WITH_CHROMIUM=0
ARG WITH_FIREFOX=1
```

### Docker Compose

Mount the browser profile directory:

```yaml
services:
  AnonX_3:
    volumes:
      - /root/firefox-profile:/app/firefox-profile:rw
    environment:
      - COOKIE_WATCHER_ENABLED=True
      - COOKIE_WATCHER_USER_DATA_DIR=/app/firefox-profile
```

**Before starting container:**

1. Create the profile on your host
2. Launch Firefox from the host with that profile
3. Sign into YouTube
4. Close Firefox
5. Start the container

The container will monitor that profile for cookie updates.

## Security Notes

- **Never use your personal Google account** — create a dedicated account for the bot
- **Cookie values are never logged** — only counts and metadata
- **File permissions** are set to 0600 (owner read/write only)
- **Atomic writes** prevent partial cookie files
- **No CAPTCHA solving** — you must sign in manually once

## Performance Impact

- **Memory:** +5-10 MB for watcher background task
- **CPU:** Negligible (only checks database mtime)
- **Disk I/O:** One read every 60s (configurable)
- **Network:** Zero (reads local SQLite database)

The watcher is extremely lightweight and runs in the background without affecting bot performance.

## Advanced Configuration

### Adjust Check Interval

```env
# Check every 30 seconds (more responsive to changes)
COOKIE_WATCHER_INTERVAL_SEC=30

# Check every 5 minutes (lower overhead)
COOKIE_WATCHER_INTERVAL_SEC=300
```

### Include All Cookies (Not Just YouTube)

```env
# Extract all cookies from browser
COOKIE_WATCHER_YOUTUBE_ONLY=False
```

This includes Google, YouTube, Google Video, and any other domains. May help with certain edge cases but increases cookie file size.

### Force Immediate Sync

From Python code:

```python
from AnonX_3 import yt

# Trigger immediate sync
await yt._cookie_watcher.force_sync()
```

## Comparison with Other Methods

| Method | Setup | Refresh | Bot Detection | Account Risk |
|--------|-------|---------|---------------|-------------|
| **Cookie Watcher** | One-time sign-in | Real-time | Immediate | Low |
| Manual upload | Upload each time | Manual | None | Low |
| cookies.txt URL | Setup URL | Periodic | Delayed | Medium |
| Browser export | Each restart | Manual | None | Low |
| No cookies | None | N/A | High | None |

Cookie Watcher combines the reliability of browser cookies with the convenience of automatic sync.

## FAQ

**Q: Can I use the same Google account for multiple bots?**  
A: Not recommended. Google may detect simultaneous usage and revoke the session. Use one account per bot.

**Q: How long do cookies stay valid?**  
A: Typically 30-90 days for YouTube. The watcher syncs them continuously so you always have fresh cookies.

**Q: Does this bypass CAPTCHA?**  
A: No. You must complete the initial sign-in and any verification manually. The watcher only syncs cookies from an already-authenticated session.

**Q: Can I run Chromium headless for signing in?**  
A: No. Google detects headless browsers and blocks sign-in. You must sign in through a GUI browser (via VNC, X11, or directly on the VPS).

**Q: Will this work with Chromium?**  
A: The bot has been migrated to use Firefox. For Chromium support, you would need to modify the Dockerfile and configuration.

**Q: What happens if Firefox is running while the bot extracts cookies?**  
A: Safe. The watcher creates a temporary copy of the cookies database, so it doesn't interfere with the running browser.

**Q: Does this work on Windows?**  
A: Yes. Point `COOKIE_WATCHER_USER_DATA_DIR` to your Windows Firefox profile path (e.g., `C:/Users/YourName/AppData/Roaming/Mozilla/Firefox/Profiles/xxxxx.default-release`).

## Related Documentation

- [Local Cookie Agent](local-cookie-agent.md) — Browser profile export system
- [Cookies Plugin](../AnonX_3/plugins/cookies.py) — Manual cookie upload commands
- [YouTube Core](../AnonX_3/core/youtube.py) — Cookie integration with yt-dlp

## Support

If you encounter issues:

1. Check the logs for error messages
2. Verify your Chromium profile has a valid YouTube sign-in
3. Test cookie extraction manually (see next section)
4. Open an issue with logs and configuration

## Manual Testing

Test cookie extraction:

```bash
python3 -m AnonX_3.core.cookie_watcher_test /root/firefox-profile
```

This will show you:
- Cookies database location
- Number of cookies extracted
- Authentication cookie count
- Netscape format output sample
