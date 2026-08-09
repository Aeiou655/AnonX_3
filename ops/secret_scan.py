#!/usr/bin/env python3
"""Scan tracked tree for accidental secret material.

Usage (from AnonX_3 root):
  python ops/secret_scan.py
Exit 1 if findings (except ignored paths).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Paths never scanned
SKIP_PARTS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "downloads",
    "media",
    "cache",
    "firefox-profile",
    ".env",
    "cookies",
}

# Filename basenames to skip
SKIP_NAMES = {
    ".env",
    "cookies.txt",
    "cookies.json",
    "log.txt",
}

# Built without embedding live-looking secrets in this file's source lines.
_MONGO = "mongo" + "db" + r"(\+srv)?://[^\s'\"]+:[^\s'\"]+@"
PATTERNS = [
    (re.compile(r"\bBOT_TOKEN\s*=\s*['\"]?\d{6,}:[A-Za-z0-9_-]{20,}"), "BOT_TOKEN"),
    (re.compile(r"\bAPI_HASH\s*=\s*['\"]?[a-f0-9]{32}['\"]?"), "API_HASH"),
    (re.compile(r"\bSESSION\d?\s*=\s*['\"]?[A-Za-z0-9_-]{30,}"), "SESSION"),
    (re.compile(_MONGO), "MONGO_URI_WITH_PASSWORD"),
    (re.compile(r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----"), "PRIVATE_KEY"),
]


def should_skip(path: Path) -> bool:
    if path.name in SKIP_NAMES or path.name.endswith(".session"):
        return True
    if path.name == "secret_scan.py":
        return True
    parts = set(path.parts)
    if parts & SKIP_PARTS:
        return True
    if path.suffix in {".pyc", ".png", ".jpg", ".webp", ".mp4", ".webm", ".ttf"}:
        return True
    return False


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or should_skip(path):
            continue
        # Only scan likely text
        if path.suffix not in {
            "",
            ".py",
            ".md",
            ".txt",
            ".json",
            ".yml",
            ".yaml",
            ".env",
            ".sample",
            ".sh",
            ".conf",
        } and path.name not in {"Procfile", "Dockerfile", "start", "setup"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat, label in PATTERNS:
            if pat.search(text):
                rel = path.relative_to(ROOT)
                rel_s = str(rel).lower()
                # Allow placeholders / intentional test fixtures
                if "sample" in rel_s or "example" in rel_s:
                    if "YOUR_" in text or "xxx" in text.lower() or "placeholder" in text.lower():
                        continue
                if "tests" in rel_s or "test_" in path.name:
                    # Fixture strings like mongodb://user:pass@host for redaction tests
                    if "supersecret" in text or "user:pass@" in text or "redact" in text.lower():
                        continue
                findings.append(f"{rel}: possible {label}")

    if findings:
        print("SECRET SCAN FAILED:")
        for f in findings:
            print(" ", f)
        return 1
    print("SECRET SCAN OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
