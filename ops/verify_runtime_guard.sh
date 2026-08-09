#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-/root/မဂ်လာပါ မြန်မာ 🇲🇲}"
BOT_API_FILE="$ROOT_DIR/AnonX/core/bot_api.py"
UTILS_FILE="$ROOT_DIR/AnonX/helpers/_utilities.py"
CALLBACKS_FILE="$ROOT_DIR/AnonX/plugins/callbacks.py"

echo "[verify] root=$ROOT_DIR"

missing=0

check_pattern() {
  local file="$1"
  local pattern="$2"
  local label="$3"
  if grep -n "$pattern" "$file" >/tmp/verify_guard_match.txt 2>/dev/null; then
    echo "[ok] $label"
    cat /tmp/verify_guard_match.txt
  else
    echo "[fail] $label"
    missing=1
  fi
  rm -f /tmp/verify_guard_match.txt
}

if [[ ! -f "$BOT_API_FILE" ]]; then
  echo "[fail] missing file: $BOT_API_FILE"
  exit 2
fi
if [[ ! -f "$UTILS_FILE" ]]; then
  echo "[fail] missing file: $UTILS_FILE"
  exit 2
fi
if [[ ! -f "$CALLBACKS_FILE" ]]; then
  echo "[fail] missing file: $CALLBACKS_FILE"
  exit 2
fi

echo "[step] validate MESSAGE_IDS_EMPTY guard"
check_pattern "$BOT_API_FILE" "MESSAGE_IDS_EMPTY" "bot_api MESSAGE_IDS_EMPTY fallback"
check_pattern "$BOT_API_FILE" "MESSAGE_ID_INVALID" "bot_api MESSAGE_ID_INVALID fallback"
check_pattern "$BOT_API_FILE" "Skipping get_messages" "bot_api skip get_messages warning"

echo "[step] validate ENTITY_TEXT_INVALID fallback chain"
check_pattern "$UTILS_FILE" "retry_entity_text_invalid" "utilities retry function exists"
check_pattern "$UTILS_FILE" "without custom emoji entities" "custom emoji fallback log exists"
check_pattern "$UTILS_FILE" "without entities" "empty entities fallback exists"

echo "[step] validate stale callback edits are tolerant"
check_pattern "$CALLBACKS_FILE" "ignore_stale=True" "callbacks ignore stale reply markup"

echo "[step] source fingerprint"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$BOT_API_FILE" "$UTILS_FILE" "$CALLBACKS_FILE"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$BOT_API_FILE" "$UTILS_FILE" "$CALLBACKS_FILE"
else
  echo "[warn] sha256 tool not available"
fi

echo "[step] recent runtime errors (last 400 lines)"
if [[ -f "$ROOT_DIR/log.txt" ]]; then
  tail -n 400 "$ROOT_DIR/log.txt" | grep -nE "MESSAGE_IDS_EMPTY|MESSAGE_ID_INVALID|ENTITY_TEXT_INVALID|Unexpected exception raised in MessageHandler" || true
else
  echo "[warn] log.txt not found at $ROOT_DIR/log.txt"
fi

if [[ "$missing" -ne 0 ]]; then
  echo "[result] FAIL: runtime code does not include required guards."
  exit 1
fi

echo "[result] PASS: required runtime guards are present."




