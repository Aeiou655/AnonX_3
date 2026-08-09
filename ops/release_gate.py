#!/usr/bin/env python3
"""Run the complete AnonX_3 release gate and build a deterministic archive."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from release_meta import ARCHIVE_NAME, PROJECT, VERSION


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist" / ARCHIVE_NAME
PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^;]+")
NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")


def normalized_name(line: str) -> str:
    match = NAME_RE.match(line)
    if not match:
        raise ValueError(f"invalid requirement: {line}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def validate_identity_and_lock() -> None:
    variant = {}
    for line in (ROOT / "VARIANT.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            variant[key.strip()] = value.strip()
    if variant.get("VARIANT") != PROJECT or ROOT.name != PROJECT:
        raise SystemExit("Canonical project identity mismatch")

    sample = (ROOT / "sample.env").read_text(encoding="utf-8")
    if f"# {PROJECT} MINIMAL .env" not in sample:
        raise SystemExit("sample.env project identity mismatch")
    if f"MONGO_URL=mongodb://localhost:27017/{PROJECT}" not in sample:
        raise SystemExit("sample.env MongoDB identity mismatch")

    source = requirement_lines(ROOT / "requirements.in")
    locked = requirement_lines(ROOT / "requirements.txt")
    unpinned = [line for line in locked if not PIN_RE.match(line)]
    if unpinned:
        raise SystemExit(f"Unpinned locked requirements: {', '.join(unpinned)}")
    source_names = {normalized_name(line) for line in source}
    locked_names = {normalized_name(line) for line in locked}
    missing = sorted(source_names - locked_names)
    if missing:
        raise SystemExit(f"Direct requirements absent from lock: {', '.join(missing)}")

    print(
        f"RELEASE IDENTITY OK project={PROJECT} version={VERSION} "
        f"locked={len(locked)}"
    )


def run(label: str, *command: str) -> None:
    print(f"\n== {label} ==")
    env = os.environ.copy()
    env["AnonX_TESTING"] = "1"
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    os.chdir(ROOT)
    validate_identity_and_lock()

    run("dependency consistency", sys.executable, "-m", "pip", "check")
    run(
        "compile",
        sys.executable,
        "-m",
        "compileall",
        "-q",
        PROJECT,
        "tests",
        "ops",
    )
    run("unit smoke", sys.executable, "-B", "tests/run_unit_smoke.py")
    run(
        "sub-1.5 resolver regression",
        sys.executable,
        "-B",
        "tests/test_v348_sub15_resolver.py",
    )
    run(
        "sub-1.5 end-to-end architecture regression",
        sys.executable,
        "-B",
        "tests/test_v349_sub15_e2e.py",
    )
    run("recursion regression", sys.executable, "-B", "tests/test_recursion_fix.py")
    run("structure", sys.executable, "-B", "ops/verify_structure.py")
    run("secret scan", sys.executable, "-B", "ops/secret_scan.py")
    run(
        "Downloader API import",
        sys.executable,
        "-B",
        "-c",
        (
            "from AnonX_3.downloader_api.main import app; "
            "assert app.title == 'Self-Hosted Downloader API'; "
            "print('DOWNLOADER API IMPORT OK')"
        ),
    )

    run("release build 1", sys.executable, "-B", "ops/build_release.py")
    first_hash = sha256(ARCHIVE)
    run("release build 2", sys.executable, "-B", "ops/build_release.py")
    second_hash = sha256(ARCHIVE)
    if first_hash != second_hash:
        raise SystemExit("Release archive is not deterministic")
    run("release verify", sys.executable, "-B", "ops/verify_release.py")

    print(
        f"\nRELEASE GATE OK archive={ARCHIVE.name} "
        f"sha256={second_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
