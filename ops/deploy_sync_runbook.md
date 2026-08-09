# Deploy Sync Runbook (`MESSAGE_IDS_EMPTY` / stale edit)

This runbook executes the "deploy mismatch first" strategy without changing bot behavior.

## 1) Baseline capture (before redeploy)

Run on the production host/container (or via Heroku run shell):

```bash
cd /root/မဂ်လာပါ မြန်မာ 🇲🇲
date -Is
tail -n 400 log.txt | grep -nE "MESSAGE_IDS_EMPTY|MESSAGE_ID_INVALID|Unexpected exception raised in MessageHandler|ENTITY_TEXT_INVALID" || true
```

Save timestamp + matching lines as baseline evidence.

## 2) Runtime source verification

In the running container/dyno:

```bash
cd /root/မဂ်လာပါ မြန်မာ 🇲🇲
bash ops/verify_runtime_guard.sh /root/မဂ်လာပါ မြန်မာ 🇲🇲
```

Expected: `[result] PASS`.

If `FAIL`, runtime is not using the expected source (deploy mismatch confirmed).

## 3) Heroku container redeploy (cache-bypassed)

From CI/CD machine with `heroku` CLI authenticated:

```bash
heroku container:login
heroku container:push worker -a <HEROKU_APP_NAME>
heroku container:release worker -a <HEROKU_APP_NAME>
heroku ps:restart worker -a <HEROKU_APP_NAME>
heroku ps -a <HEROKU_APP_NAME>
```

Ensure only intended worker dyno(s) are running after release.

## 4) Startup fingerprint capture (after redeploy)

```bash
heroku logs --tail -a <HEROKU_APP_NAME>
```

Capture startup lines:
- Cache directories updated
- Loaded languages
- Loaded module count
- Loaded sudo user count

Then run:

```bash
heroku run bash -a <HEROKU_APP_NAME>
# inside dyno shell:
cd /root/မဂ်လာပါ မြန်မာ 🇲🇲
bash ops/verify_runtime_guard.sh /root/မဂ်လာပါ မြန်မာ 🇲🇲
```

## 5) Post-redeploy functional checks

Perform Telegram checks:
1. Trigger `/play`, then make target edit stale (delete/expire message path) to force edit fallback.
2. Trigger `/settings` callback and queue control callbacks on stale controls.
3. Confirm bot keeps working and no unhandled traceback.

## 6) Pass criteria

Deployment is accepted when all are true:
- No new `Unexpected exception ... MessageIdsEmpty` tracebacks.
- No fatal `Bot API editMessageReplyMarkup ... MESSAGE_ID_INVALID`.
- `ENTITY_TEXT_INVALID` appears only as retry warning and flow continues.
- 30-60 minutes smoke usage (`/play`, `/pause`, `/resume`, `/skip`, `/settings`) remains crash-free.





## Doc Update (2026-06-01)
- 01-Jun-2026: Startgroup force-ID override removed. Current behavior uses STARTGROUP_WEIGHTS only (set to 45,30,25 in active AnonX and AnonX .env).
