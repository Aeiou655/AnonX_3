# Manual Telegram VC smoke checklist

After deploy/restart:

1. [ ] `/play` known track — starts (direct or local)
2. [ ] Same track again — cache HIT (fast, little/no yt-dlp)
3. [ ] Two users same song — one download only
4. [ ] Force network fail on direct (if possible) — local READY plays
5. [ ] YouTube dead / private — SoundCloud fallback or clear error
6. [ ] Second track while playing — queue card fields correct (title/duration/user)
7. [ ] Stop — VC leaves; no stuck status
8. [ ] High load (many plays) — quality drops / no crash
9. [ ] Disk nearly full — GC reclaims without deleting active file
10. [ ] Optional: `HEALTH_PORT=9100` → `curl 127.0.0.1:9100/health`

## Unrealistic (do not fail the checklist for these)

- Perfect seamless mid-track URL→file switch
- Zero YouTube 403 forever without PO/cookies
