#!/usr/bin/env python3
"""Verify the final release archive, manifest, and exclusion policy."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

from build_release import (
    ARCHIVE,
    BLOCKED_NAMES,
    BLOCKED_PARTS,
    BLOCKED_SUFFIXES,
    _runtime_cache_or_cookie_path,
    validate_release_env_bytes,
)
from release_meta import PROJECT, RELEASE_DATE, VERSION


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"unsafe archive path: {name}")
    if path.name in BLOCKED_NAMES:
        raise ValueError(f"blocked file included: {name}")
    if any(part in BLOCKED_PARTS for part in path.parts):
        raise ValueError(f"blocked directory included: {name}")
    if _runtime_cache_or_cookie_path(tuple(path.parts)):
        raise ValueError(f"blocked runtime cache/cookie path included: {name}")
    if any(path.name.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        raise ValueError(f"blocked suffix included: {name}")


def main() -> int:
    if not ARCHIVE.is_file():
        raise SystemExit(f"Release archive missing: {ARCHIVE}")

    hash_path = ARCHIVE.with_suffix(ARCHIVE.suffix + ".sha256")
    expected_archive_hash = hash_path.read_text(encoding="utf-8").split()[0]
    actual_archive_hash = digest(ARCHIVE.read_bytes())
    if actual_archive_hash != expected_archive_hash:
        raise SystemExit("Release archive SHA-256 does not match sidecar")

    with zipfile.ZipFile(ARCHIVE, "r") as archive:
        if archive.testzip() is not None:
            raise SystemExit("Release archive contains a corrupt member")

        names = archive.namelist()
        if len(names) != len(set(names)):
            raise SystemExit("Release archive contains duplicate paths")
        for name in names:
            validate_name(name)
        if ".env" not in names:
            raise SystemExit("Release archive is missing required .env template")
        validate_release_env_bytes(archive.read(".env"))

        try:
            manifest = json.loads(archive.read("RELEASE_MANIFEST.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid release manifest: {exc}") from exc

        if (
            manifest.get("project") != PROJECT
            or manifest.get("version") != VERSION
            or manifest.get("release_date") != RELEASE_DATE
        ):
            raise SystemExit("Release manifest identity/version mismatch")

        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise SystemExit("Release manifest files entry is invalid")
        expected_names = set(names) - {"RELEASE_MANIFEST.json"}
        manifest_names = {entry.get("path") for entry in entries}
        if manifest_names != expected_names or len(entries) != len(manifest_names):
            raise SystemExit("Release manifest paths do not match archive members")

        for entry in entries:
            name = entry["path"]
            data = archive.read(name)
            if entry.get("size") != len(data) or entry.get("sha256") != digest(data):
                raise SystemExit(f"Release manifest checksum mismatch: {name}")

    print(
        f"RELEASE VERIFY OK files={len(names)} "
        f"sha256={actual_archive_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
