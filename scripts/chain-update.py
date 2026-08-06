#!/usr/bin/env python3
"""Upgrade a GitInTheVan install one release at a time, offline of the app.

Why this exists
---------------
Each GitInTheVan release only guarantees it can migrate a database from the
release immediately before it. Versions 0.19.0 and later chain multi-release
upgrades automatically, but a user on 0.15.x-0.18.0 is running *frozen* code:
their `app/services/updater.py` and their staged update script are snapshots
taken before this feature existed, so nothing shipped later can make their
install chain by itself. This script is the escape hatch for them, and the
repair tool when an in-app chain stalls.

Deliberately stdlib-only (no httpx, no venv required) so it can be fetched as a
single file and run under any Python 3.12:

    curl -O https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/scripts/chain-update.py
    python chain-update.py --root /path/to/GitInTheVan

Usage
-----
    python chain-update.py [--root DIR] [--to-version X.Y.Z] [--yes] [--dry-run]

Run it with the server stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

RELEASES_API = "https://api.github.com/repos/Tydorius/GitInTheVan/releases"
CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[(\d+(?:\.\d+)*)\]", re.MULTILINE)
USER_AGENT = "gitinthevan-chain-update"

_log_handle = None


def log(message: str) -> None:
    print(message, flush=True)
    if _log_handle is not None:
        _log_handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")
        _log_handle.flush()


def die(message: str) -> None:
    log(f"ERROR: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------

def parse_version(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in value.lstrip("v").split("."))
    except ValueError:
        return (0,)


def installed_version(root: Path) -> str:
    """Read the version from CHANGELOG.md, which ships in every release zip."""
    changelog = root / "CHANGELOG.md"
    if not changelog.exists():
        die(f"No CHANGELOG.md in {root}. Is --root pointing at the GitInTheVan folder?")
    match = CHANGELOG_VERSION_RE.search(changelog.read_text(encoding="utf-8"))
    if not match:
        die("Could not find a version header in CHANGELOG.md")
    return match.group(1)


def fetch_json(url: str):
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response), response.headers.get("Link", "")


def next_page(link_header: str) -> str:
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) >= 2 and 'rel="next"' in "".join(segments[1:]):
            return segments[0].strip().strip("<>")
    return ""


def list_releases(max_pages: int = 5) -> list[dict]:
    releases: list[dict] = []
    url = f"{RELEASES_API}?per_page=100"
    for _ in range(max_pages):
        try:
            page, link = fetch_json(url)
        except urllib.error.HTTPError as e:
            die(f"GitHub API returned {e.code}. If this is 403, you have hit the rate limit; wait an hour.")
        except urllib.error.URLError as e:
            die(f"Could not reach GitHub: {e.reason}")
        releases.extend(r for r in page if not r.get("draft") and not r.get("prerelease"))
        url = next_page(link)
        if not url:
            break
    return releases


def release_zip_url(release: dict) -> str:
    for asset in release.get("assets", []):
        if asset.get("name", "").endswith(".zip"):
            return asset.get("browser_download_url", "")
    return release.get("zipball_url", "")


def build_chain(current: str, releases: list[dict], target: str | None) -> list[dict]:
    current_ver = parse_version(current)
    target_ver = parse_version(target) if target else None

    candidates: dict[tuple[int, ...], dict] = {}
    for release in releases:
        raw = (release.get("tag_name") or release.get("name") or "").lstrip("v")
        version = parse_version(raw)
        if not raw or version == (0,) or version <= current_ver:
            continue
        if target_ver and version > target_ver:
            continue
        url = release_zip_url(release)
        if not url:
            continue
        candidates.setdefault(version, {"version": raw, "zip_url": url})
    return [candidates[key] for key in sorted(candidates)]


# ---------------------------------------------------------------------------
# Hop execution
# ---------------------------------------------------------------------------

def venv_python(root: Path) -> Path:
    candidate = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    if not candidate.exists():
        die(f"No virtualenv at {candidate}. Run the deploy script for your platform first.")
    return candidate


def backup_database(root: Path) -> str:
    """Per hop, not once overall: each hop's migrations are what can corrupt."""
    db = root / "data" / "gitinthevan.db"
    if not db.exists():
        log("  No SQLite database found; skipping backup (PostgreSQL/MySQL: back up yourself).")
        return ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = root / "data" / f"gitinthevan_backup_{stamp}.db"
    shutil.copy2(db, dest)
    log(f"  Backed up database to {dest.name}")
    return dest.name


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        dest.write_bytes(response.read())


def extract_over(zip_path: Path, root: Path) -> None:
    """Extract the release zip over the install, handling the zipball wrapper folder."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp_path)

        entries = list(tmp_path.iterdir())
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp_path

        for item in source.rglob("*"):
            if item.is_dir():
                continue
            relative = item.relative_to(source)
            # data/ holds the database, backups and chain state; the release zip
            # never contains it, but refuse to write there under any circumstance.
            if relative.parts and relative.parts[0] == "data":
                continue
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def run(cmd: list[str], cwd: Path, *, what: str) -> None:
    log(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-15:]
        die(f"{what} failed:\n" + "\n".join(tail))


def apply_migrations(root: Path) -> None:
    """Run migrations and the schema repair without starting a server.

    NOTE: this mirrors what app/main.py's lifespan does before it starts serving.
    It is currently `init_db()` plus `run_schema_repair()`. If lifespan gains
    another database-touching step, add it here too -- otherwise this script will
    silently skip it.
    """
    code = (
        "import asyncio\n"
        "from app.database import engine, init_db\n"
        "async def main():\n"
        "    await init_db()\n"
        "    try:\n"
        "        from app.services.schema_repair import run_schema_repair\n"
        "    except ImportError:\n"
        "        return\n"
        "    await run_schema_repair(engine)\n"
        "asyncio.run(main())\n"
    )
    run([str(venv_python(root)), "-c", code], root, what="Database migration")


def rebuild_frontend(root: Path) -> None:
    npm = shutil.which("npm")
    portable = root / ".node" / ("npm.cmd" if os.name == "nt" else "bin/npm")
    if portable.exists():
        npm = str(portable)
    if not npm:
        log("  Node.js not found; keeping the existing frontend build.")
        return

    frontend = root / "frontend"
    # `npm ci` installs strictly from package-lock.json (exact pins only, never ranges).
    # Older releases predate the exact pins, so fall back rather than abort.
    result = subprocess.run([npm, "ci"], cwd=str(frontend), capture_output=True, text=True)
    if result.returncode != 0:
        log("  npm ci failed (lockfile may predate exact pinning); trying npm install")
        run([npm, "install"], frontend, what="npm install")
    run([npm, "run", "build"], frontend, what="Frontend build")


def install_hop(root: Path, step: dict, *, skip_frontend: bool) -> None:
    log(f"[{step['version']}] starting")
    backup_database(root)

    zip_path = root / "data" / "gitinthevan.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"  Downloading {step['zip_url']}")
    download(step["zip_url"], zip_path)

    log("  Extracting over the install")
    extract_over(zip_path, root)
    zip_path.unlink(missing_ok=True)

    log("  Installing Python dependencies")
    run([str(venv_python(root)), "-m", "pip", "install", "-e", ".[dev]", "-q"],
        root, what="pip install")

    log("  Applying database migrations")
    apply_migrations(root)

    if not skip_frontend:
        log("  Rebuilding frontend")
        rebuild_frontend(root)

    landed = installed_version(root)
    if parse_version(landed) < parse_version(step["version"]):
        die(f"Expected {step['version']} after install but CHANGELOG.md reports {landed}")
    log(f"[{step['version']}] done (now on {landed})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    global _log_handle

    parser = argparse.ArgumentParser(
        description="Upgrade GitInTheVan one release at a time.",
        epilog="Run with the server stopped.",
    )
    parser.add_argument("--root", default=None,
                        help="GitInTheVan install directory (default: this script's parent)")
    parser.add_argument("--from-version", default=None,
                        help="Override the detected installed version")
    parser.add_argument("--to-version", default=None,
                        help="Stop at this version instead of the newest release")
    parser.add_argument("--yes", action="store_true", help="Do not prompt for confirmation")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="Do not rebuild the frontend (faster; keeps the shipped build)")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not (root / "pyproject.toml").exists():
        die(f"{root} does not look like a GitInTheVan install (no pyproject.toml). Use --root.")

    (root / "data" / "update-logs").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_handle = (root / "data" / "update-logs" / f"chain-update-{stamp}.log").open(
        "w", encoding="utf-8", newline="\n"
    )

    current = args.from_version or installed_version(root)
    log(f"Installed version: {current}")
    log(f"Install directory: {root}")

    chain = build_chain(current, list_releases(), args.to_version)
    if not chain:
        log("Already up to date. Nothing to do.")
        return 0

    log("")
    log(f"Plan: {len(chain)} release(s) will be installed in order:")
    for i, step in enumerate(chain, 1):
        log(f"  {i}. {step['version']}")
    log("")
    log("Each step backs up the database, extracts the release, reinstalls dependencies,")
    log("and runs that release's migrations before moving on.")
    log("")

    if args.dry_run:
        log("Dry run; nothing was changed.")
        return 0

    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            log("Aborted.")
            return 1

    for i, step in enumerate(chain, 1):
        log("")
        log(f"=== Step {i} of {len(chain)}: {step['version']} ===")
        install_hop(root, step, skip_frontend=args.skip_frontend)

    log("")
    log(f"Upgrade complete: {current} -> {installed_version(root)}")
    log("Start the server with the deploy script for your platform.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        if _log_handle is not None:
            _log_handle.close()
