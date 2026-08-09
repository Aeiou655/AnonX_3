#!/usr/bin/env python3
"""Build a deterministic, secret-free AnonX_3 release archive."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from release_meta import (
    ARCHIVE_NAME,
    PROJECT,
    RELEASE_DATE,
    VERSION,
    ZIP_TIMESTAMP,
)

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ARCHIVE = DIST / ARCHIVE_NAME

ROOT_FILES = {
    ".env",
    ".compyl",
    ".dockerignore",
    ".gitignore",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "DECISIONS.md",
    "Dockerfile",
    "ERRORS.md",
    "MEMORY.md",
    "Procfile",
    "PROJECT_STATE.md",
    "README.md",
    "RELEASE_NOTES.md",
    "VARIANT.txt",
    "app.json",
    "broadcast.md",
    "config.py",
    "docker-compose.example.yml",
    "heroku.yml",
    "install_api_deps.sh",
    "requirements.in",
    "requirements.txt",
    "sample.env",
    "setup",
    "start",
}
ROOT_DIRS = {
    ".github",
    "AnonX_3",
    "docs",
    "nginx",
    "ops",
    "provider",
    "tests",
}
BLOCKED_PARTS = {
    ".git",
    ".kimi-codex",
    ".claude",
    "__pycache__",
    "dist",
    "downloads",
    "media",
    "firefox-profile",
    "node_modules",
    "venv",
    ".venv",
}
BLOCKED_NAMES = {
    "cookies.json",
    "cookies.txt",
    "docker-compose.yml",
    "log.txt",
    "prompt.txt",
    "SESSION_MEMORY.md",
}
BLOCKED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".session",
    ".instance.lock",
    ".journal",
}




def validate_release_env_bytes(data: bytes) -> None:
    """Require a safe deploy template and reject accidentally filled secrets."""
    text = data.decode("utf-8")
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    required = {
        "API_ID": "0",
        "API_HASH": "",
        "BOT_TOKEN": "",
        "LOGGER_ID": "0",
        "OWNER_ID": "0",
        "SESSION": "",
    }
    for key, expected in required.items():
        if values.get(key) != expected:
            raise SystemExit(
                f"Release .env must keep {key} at its safe placeholder value"
            )
    if values.get("MONGO_URL") != "mongodb://localhost:27017/AnonX_3":
        raise SystemExit("Release .env MONGO_URL must use the local placeholder")

def _runtime_cache_or_cookie_path(parts: tuple[str, ...]) -> bool:
    """Block runtime cache/cookie payloads while preserving package markers."""
    if not parts:
        return False
    # ``AnonX_3/core/cache`` is importable package code, not runtime cache.
    if parts[:3] == ("AnonX_3", "core", "cache"):
        return False
    # The package-level cookies directory must exist after ZIP extraction, but
    # only its inert marker is release-safe. Never ship runtime cookie material.
    if parts[:2] == ("AnonX_3", "cookies"):
        return parts != ("AnonX_3", "cookies", ".gitkeep")
    return "cache" in parts or "cookies" in parts


def allowed(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if path.is_symlink() or not path.is_file():
        return False
    if rel.name in BLOCKED_NAMES:
        return False
    if any(part in BLOCKED_PARTS for part in rel.parts):
        return False
    if _runtime_cache_or_cookie_path(tuple(rel.parts)):
        return False
    if any(rel.name.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return False
    if len(rel.parts) == 1:
        return rel.name in ROOT_FILES
    return rel.parts[0] in ROOT_DIRS


def release_files() -> list[Path]:
    return sorted(
        (path for path in ROOT.rglob("*") if allowed(path)),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def add_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def main() -> int:
    release_env = ROOT / ".env"
    if not release_env.is_file():
        raise SystemExit("Release .env template is missing")
    validate_release_env_bytes(release_env.read_bytes())
    files = release_files()
    if not files:
        raise SystemExit("No release files selected")

    entries = []
    payloads: list[tuple[str, bytes]] = []
    for path in files:
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        payloads.append((name, data))
        entries.append({"path": name, "size": len(data), "sha256": digest(data)})

    manifest = {
        "project": PROJECT,
        "version": VERSION,
        "release_date": RELEASE_DATE,
        "files": entries,
    }
    manifest_data = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"

    DIST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARCHIVE, "w") as archive:
        for name, data in payloads:
            add_bytes(archive, name, data)
        add_bytes(archive, "RELEASE_MANIFEST.json", manifest_data)

    archive_hash = digest(ARCHIVE.read_bytes())
    hash_path = ARCHIVE.with_suffix(ARCHIVE.suffix + ".sha256")
    hash_path.write_text(
        f"{archive_hash}  {ARCHIVE.name}\n",
        encoding="utf-8",
    )
    print(
        f"RELEASE OK path={ARCHIVE} files={len(entries) + 1} "
        f"sha256={archive_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
