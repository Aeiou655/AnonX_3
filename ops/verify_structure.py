#!/usr/bin/env python3
"""Verify a deployable numbered project structure is clean and complete.

Usage (from package deploy root, e.g. AnonX_3/):
  python ops/verify_structure.py
  python ops/verify_structure.py --root .
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_ROOT_FILES = (
    "config.py",
    "requirements.in",
    "requirements.txt",
    "Dockerfile",
    "Procfile",
    "heroku.yml",
    "app.json",
    "sample.env",
    "start",
    "setup",
    ".gitignore",
)

REQUIRED_PKG_ENTRIES = (
    "__init__.py",
    "__main__.py",
    "core",
    "plugins",
    "helpers",
    "locales",
    "cookies",
)

REQUIRED_CORE = (
    "bot.py",
    "calls.py",
    "lifecycle.py",
    "mongo.py",
    "youtube.py",
    "stream_profile.py",
    "prefetch.py",
    "performance.py",
    "resource_budget.py",
    "supervisor.py",
    "error_monitor.py",
    "playback.py",
    "playback_orchestrator.py",
    "resource_manager.py",
    "metrics.py",
    "health.py",
    "security.py",
)

# Package dirs required by CDN/playback (never exclude bare name "cache" in copy tools)
REQUIRED_CORE_DIRS = (
    "cache",
    "cdn",
    "downloader",
    "resolver",
    "provider",
)

FORBIDDEN_NAMES = {
    "__MACOSX",
    ".DS_Store",
}


def package_name(root: Path) -> str:
    # Prefer directory name when it matches a nested package folder.
    name = root.name
    if (root / name).is_dir() and (root / name / "__init__.py").is_file():
        return name
    # Fallback: first child dir with __init__.py that is not ops/cache/etc.
    skip = {"ops", "cache", "downloads", ".git", ".github", "__pycache__"}
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in skip and (child / "__init__.py").is_file():
            return child.name
    return name


def find_empty_py(pkg: Path) -> list[Path]:
    empty: list[Path] = []
    for path in pkg.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            if path.stat().st_size == 0:
                empty.append(path)
        except OSError:
            continue
    return empty


def verify(root: Path) -> list[str]:
    errors: list[str] = []
    root = root.resolve()
    if not root.is_dir():
        return [f"root is not a directory: {root}"]

    pkg_name = package_name(root)
    pkg = root / pkg_name
    if not pkg.is_dir():
        errors.append(f"missing package directory: {pkg_name}/")
        return errors

    for name in REQUIRED_ROOT_FILES:
        if not (root / name).exists():
            errors.append(f"missing root file: {name}")

    for name in REQUIRED_PKG_ENTRIES:
        if not (pkg / name).exists():
            errors.append(f"missing package entry: {pkg_name}/{name}")

    for name in REQUIRED_CORE:
        if not (pkg / "core" / name).is_file():
            errors.append(f"missing core module: {pkg_name}/core/{name}")

    for name in REQUIRED_CORE_DIRS:
        d = pkg / "core" / name
        if not d.is_dir() or not (d / "__init__.py").is_file():
            errors.append(f"missing core package dir: {pkg_name}/core/{name}/")

    for bad in FORBIDDEN_NAMES:
        if (root / bad).exists() or (pkg / bad).exists():
            errors.append(f"forbidden path present: {bad}")

    # No zip archives inside deploy root
    for z in root.glob("*.zip"):
        errors.append(f"zip archive in deploy root: {z.name}")

    empty = find_empty_py(pkg)
    for path in empty:
        errors.append(f"empty python file: {path.relative_to(root)}")

    # Renamed copies must import their own package, not another numbered sibling.
    import_re = re.compile(r"^\s*(?:from|import)\s+(AnonX_\d+)(?:\.|\s|$)")
    for path in pkg.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = import_re.match(line)
            if match and match.group(1) != pkg_name:
                errors.append(
                    f"stale {match.group(1)} import in {path.relative_to(root)}: "
                    f"{line.strip()[:80]}"
                )
                break

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify AnonX_3 project structure")
    parser.add_argument(
        "--root",
        default=".",
        help="Deploy root containing config.py and package dir (default: .)",
    )
    args = parser.parse_args()
    root = Path(args.root)
    errors = verify(root)
    pkg = package_name(root.resolve()) if root.exists() else "?"
    if errors:
        print(f"STRUCTURE FAIL [{pkg}] ({len(errors)} issue(s))")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"STRUCTURE OK [{pkg}] root={root.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
