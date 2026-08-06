"""Check for GitInTheVan updates from GitHub releases, and chain multi-release upgrades.

Each release only guarantees it can migrate a database from the release immediately
before it, so a user several versions behind must be upgraded one release at a time.
`execute_update()` freezes an ordered plan of single-release hops into
`data/update-chain.json`; after each hop's server boots, `start_chain_resume()`
launches the next one.

The plan is frozen at click time and never re-resolved: an admin who approves an
upgrade to 0.18.0 must not be carried to 0.19.0 because it was published mid-chain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_RELEASES_API = "https://api.github.com/repos/Tydorius/GitInTheVan/releases"
CHANGELOG_URL = "https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/CHANGELOG.md"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"

CHAIN_SCHEMA_VERSION = 1
_CHAIN_PATH = _DATA_DIR / "update-chain.json"
_CHAIN_ARCHIVE_PATH = _DATA_DIR / "update-chain-last.json"
_CHAIN_LOG_PATH = _DATA_DIR / "update-chain.log"
_CHAIN_LOG_MAX_LINES = 500
_CHAIN_MAX_ATTEMPTS = 2
# Refreshed on every state write, so a slow multi-hop chain or one an admin fixes
# the next morning is not discarded. Only a genuinely abandoned chain expires.
_CHAIN_IDLE_EXPIRY_SECONDS = 7 * 24 * 3600

# The Admin page checks for updates on mount; the unauthenticated GitHub limit is
# 60 requests/hour per IP.
_RELEASES_CACHE_TTL = 900.0
_releases_cache: tuple[float, list[dict[str, Any]]] | None = None

_ACTIVE_CHAIN_STATUSES = frozenset({"active"})

# Matches "## [0.18.0] - 2026-07-15". Requires digits so "## [Unreleased]" is
# skipped; \d+ (not \d) so a double-digit major version still parses.
_CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[(\d+(?:\.\d+)*)\]", re.MULTILINE)

_version_cache: str | None = None


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def _version_from_changelog(text: str) -> str | None:
    """Return the newest version documented in a CHANGELOG.md body, if any."""
    match = _CHANGELOG_VERSION_RE.search(text)
    return match.group(1) if match else None


def get_current_version() -> str:
    """Get the installed version.

    CHANGELOG.md is the primary source: it ships in the release zip and is
    replaced the moment the update script extracts, so it reflects the code
    actually on disk. importlib.metadata reads the editable install's .dist-info,
    which is stale until `pip install -e` re-runs.

    The metadata fallback is required, not decorative -- the Dockerfile copies
    only pyproject.toml, README.md, app/ and static/, so CHANGELOG.md does not
    exist in the container image.
    """
    global _version_cache
    if _version_cache is not None:
        return _version_cache

    version = ""
    try:
        if _CHANGELOG_PATH.exists():
            version = _version_from_changelog(_CHANGELOG_PATH.read_text(encoding="utf-8")) or ""
    except Exception as e:
        logger.warning("Could not read version from CHANGELOG.md: %s", e)

    if not version:
        try:
            import importlib.metadata

            version = importlib.metadata.version("gitinthevan")
        except Exception:
            version = "0.0.0"

    _version_cache = version
    return version


def _clear_version_cache() -> None:
    """Test hook."""
    global _version_cache
    _version_cache = None


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '0.14.5' into a comparable tuple."""
    parts = v.lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


# ---------------------------------------------------------------------------
# Changelog rendering
# ---------------------------------------------------------------------------

def _extract_changelog_section(changelog: str, current: str, latest: str) -> str:
    """Extract changelog entries between current and latest version headers.

    Finds all version headers (## [X.Y.Z] ...), collects every section
    whose version is greater than current and less than or equal to latest.
    Returns the combined markdown text (max 4000 chars).
    """
    header_re = re.compile(r"^##\s*\[([\d.]+)\]", re.MULTILINE)
    matches = list(header_re.finditer(changelog))
    if not matches:
        return ""

    cur_ver = _parse_version(current)
    lat_ver = _parse_version(latest)

    sections: list[str] = []
    for i, m in enumerate(matches):
        ver = _parse_version(m.group(1))
        if ver <= cur_ver:
            continue
        if ver > lat_ver:
            break

        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        block = changelog[start:end].strip()
        if block:
            sections.append(block)

    result = "\n\n".join(sections)
    return result[:4000]


async def _fetch_changelog(client: httpx.AsyncClient) -> str:
    """Fetch CHANGELOG.md from the repo."""
    resp = await client.get(CHANGELOG_URL)
    if resp.status_code == 200:
        return resp.text
    return ""


# ---------------------------------------------------------------------------
# GitHub releases
# ---------------------------------------------------------------------------

def _release_zip_url(release: dict[str, Any]) -> str:
    """Prefer an attached .zip asset, fall back to the source zipball."""
    for asset in release.get("assets", []):
        if asset.get("name", "").endswith(".zip"):
            url = asset.get("browser_download_url", "")
            if url:
                return url
    return release.get("zipball_url", "")


def _next_page_url(link_header: str) -> str:
    """Extract the rel="next" URL from a GitHub Link header."""
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        if 'rel="next"' in "".join(segments[1:]):
            return segments[0].strip().strip("<>")
    return ""


async def _list_releases(
    client: httpx.AsyncClient, *, max_pages: int = 5
) -> list[dict[str, Any]]:
    """Fetch published releases, following pagination.

    Drafts and prereleases are excluded: a chain must only ever step through
    releases the maintainer has actually published.
    """
    releases: list[dict[str, Any]] = []
    url = f"{GITHUB_RELEASES_API}?per_page=100"

    for _ in range(max_pages):
        resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            raise httpx.HTTPStatusError(
                f"GitHub API returned {resp.status_code}", request=resp.request, response=resp
            )

        page = resp.json()
        if not isinstance(page, list):
            break
        releases.extend(r for r in page if not r.get("draft") and not r.get("prerelease"))

        url = _next_page_url(resp.headers.get("link", ""))
        if not url:
            break

    return releases


async def get_releases(*, force: bool = False) -> list[dict[str, Any]]:
    """Return published releases, cached briefly to protect the API rate limit."""
    global _releases_cache

    if not force and _releases_cache is not None:
        cached_at, cached = _releases_cache
        if time.monotonic() - cached_at < _RELEASES_CACHE_TTL:
            return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        releases = await _list_releases(client)

    _releases_cache = (time.monotonic(), releases)
    return releases


def _clear_releases_cache() -> None:
    """Test hook."""
    global _releases_cache
    _releases_cache = None


def _release_version(release: dict[str, Any]) -> str:
    return (release.get("tag_name") or release.get("name") or "").lstrip("v")


async def check_for_update() -> dict[str, Any]:
    """Check GitHub for the latest release.

    Returns dict with:
        current_version: str
        latest_version: str
        update_available: bool
        release_url: str
        release_notes: str
        zip_url: str
        step_count: int   -- releases that must be installed to reach latest
    """
    current = get_current_version()

    try:
        releases = await get_releases()
        if not releases:
            return {
                "current_version": current,
                "latest_version": current,
                "update_available": False,
                "error": "No published releases found",
            }

        newest = max(releases, key=lambda r: _parse_version(_release_version(r)))
        latest = _release_version(newest)
        if not latest:
            return {
                "current_version": current,
                "latest_version": current,
                "update_available": False,
                "error": "Could not parse release version",
            }

        steps = build_chain(current, releases)
        update_available = bool(steps)
        release_notes = newest.get("body", "")[:2000]

        if update_available:
            async with httpx.AsyncClient(timeout=15.0) as client:
                changelog = await _fetch_changelog(client)
            if changelog:
                extracted = _extract_changelog_section(changelog, current, latest)
                if extracted:
                    release_notes = extracted

        return {
            "current_version": current,
            "latest_version": latest,
            "update_available": update_available,
            "release_url": newest.get("html_url", ""),
            "release_notes": release_notes,
            "zip_url": _release_zip_url(newest),
            "step_count": len(steps),
        }
    except Exception as e:
        logger.warning("Update check failed: %s", e)
        return {
            "current_version": current,
            "latest_version": current,
            "update_available": False,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Chain planning and state
# ---------------------------------------------------------------------------

def build_chain(current: str, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the ordered list of hops needed to get from `current` to the newest release.

    Every release strictly newer than `current`, ascending, deduplicated by parsed
    version. Derived from releases rather than CHANGELOG headers because not every
    changelog entry is published as a release (0.17.1 has an entry but no release),
    and only published releases are installable.
    """
    current_ver = _parse_version(current)

    candidates: dict[tuple[int, ...], dict[str, Any]] = {}
    for release in releases:
        # _list_releases already excludes these; repeated here so the function is
        # correct for any caller, not just the one that pre-filters.
        if release.get("draft") or release.get("prerelease"):
            continue
        raw = _release_version(release)
        if not raw:
            continue
        version = _parse_version(raw)
        if version == (0,) or version <= current_ver:
            continue
        zip_url = _release_zip_url(release)
        if not zip_url:
            logger.warning("Release %s has no downloadable zip, skipping", raw)
            continue
        candidates.setdefault(version, {
            "version": raw,
            "tag": release.get("tag_name", ""),
            "zip_url": zip_url,
            "release_url": release.get("html_url", ""),
            "status": "pending",
            "attempts": 0,
            "error": "",
        })

    return [candidates[key] for key in sorted(candidates)]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _chain_log(message: str, *args: Any) -> None:
    """Append one line to data/update-chain.log and the server log.

    Separate from data/updater.log, which the update scripts truncate: this file
    is the only record that survives every hop of a chain.
    """
    logger.info("Update chain: " + message, *args)
    try:
        rendered = message % args if args else message
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if _CHAIN_LOG_PATH.exists():
            lines = _CHAIN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(f"{_now()} {rendered}")
        _CHAIN_LOG_PATH.write_text(
            "\n".join(lines[-_CHAIN_LOG_MAX_LINES:]) + "\n", encoding="utf-8", newline="\n"
        )
    except Exception as e:
        logger.warning("Could not write update-chain.log: %s", e)


def read_chain() -> dict[str, Any] | None:
    """Load the frozen chain, or None if there isn't a usable one.

    Never raises: a corrupt chain file must not stop the server from booting.
    """
    try:
        if not _CHAIN_PATH.exists():
            return None
        chain = json.loads(_CHAIN_PATH.read_text(encoding="utf-8"))
        if not isinstance(chain, dict):
            return None
    except Exception as e:
        logger.warning("Could not read update-chain.json: %s", e)
        return None

    schema = chain.get("schema_version")
    if schema != CHAIN_SCHEMA_VERSION:
        # Written by a newer version whose format we cannot interpret. Refuse to
        # act on it rather than guessing at the meaning of its fields.
        logger.warning("update-chain.json has unrecognized schema_version %r", schema)
        return {"schema_version": schema, "status": "unrecognized", "steps": []}

    chain.setdefault("status", "active")
    chain.setdefault("steps", [])
    chain.setdefault("from_version", "")
    chain.setdefault("target_version", "")
    chain.setdefault("error", "")
    for step in chain["steps"]:
        step.setdefault("status", "pending")
        step.setdefault("attempts", 0)
        step.setdefault("error", "")
    return chain


def write_chain(chain: dict[str, Any]) -> None:
    """Persist the chain, stamping activity time.

    newline="\\n" because write_text defaults to os.linesep translation on
    Windows, and this file gets pasted into bug reports.
    """
    chain["last_activity_at"] = _now()
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CHAIN_PATH.write_text(
            json.dumps(chain, indent=2), encoding="utf-8", newline="\n"
        )
    except Exception as e:
        logger.error("Could not write update-chain.json: %s", e)


def clear_chain(*, status: str, archive: bool = True) -> None:
    """Finish a chain: archive it for the UI, then remove the live file."""
    chain = read_chain()
    if chain is not None and archive:
        chain["status"] = status
        chain["last_activity_at"] = _now()
        try:
            _CHAIN_ARCHIVE_PATH.write_text(
                json.dumps(chain, indent=2), encoding="utf-8", newline="\n"
            )
        except Exception as e:
            logger.warning("Could not archive update chain: %s", e)
    try:
        _CHAIN_PATH.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Could not remove update-chain.json: %s", e)


def _chain_expired(chain: dict[str, Any]) -> bool:
    stamp = chain.get("last_activity_at") or chain.get("created_at") or ""
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last).total_seconds() > _CHAIN_IDLE_EXPIRY_SECONDS


def chain_is_active(chain: dict[str, Any] | None) -> bool:
    return bool(chain and chain.get("status") in _ACTIVE_CHAIN_STATUSES)


def _next_pending_index(chain: dict[str, Any]) -> int | None:
    """First step still awaiting an outcome. Used by automatic progression."""
    for i, step in enumerate(chain.get("steps", [])):
        if step.get("status") in ("pending", "launched"):
            return i
    return None


def _first_unfinished_index(chain: dict[str, Any]) -> int | None:
    """First step that has not been installed, including failed ones.

    Retrying is only useful for a step that already failed, so an admin-driven
    resume must look wider than automatic progression does.
    """
    for i, step in enumerate(chain.get("steps", [])):
        if step.get("status") != "installed":
            return i
    return None


# ---------------------------------------------------------------------------
# Hop execution
# ---------------------------------------------------------------------------

async def _download_zip(zip_url: str) -> str:
    """Download a hop's zip to data/gitinthevan.zip. Returns "" or an error message."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _DATA_DIR / "gitinthevan.zip"
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(zip_url)
            if resp.status_code != 200:
                return f"Download failed: HTTP {resp.status_code}"
            zip_path.write_bytes(resp.content)
    except Exception as e:
        return f"Download failed: {e}"
    return ""


def _stage_script(*, is_windows: bool) -> tuple[Path | None, str]:
    """Copy the platform update script into data/ so extraction can't overwrite it."""
    if is_windows:
        script_src = _SCRIPTS_DIR / "update-windows.bat"
        script_dst = _DATA_DIR / "auto-update.bat"
    else:
        script_src = _SCRIPTS_DIR / (
            "update-macos.sh" if sys.platform == "darwin" else "update-linux.sh"
        )
        script_dst = _DATA_DIR / "auto-update.sh"

    if not script_src.exists():
        return None, f"Update script not found: {script_src.name}"

    try:
        shutil.copy2(script_src, script_dst)
        if not is_windows:
            script_dst.chmod(0o755)
    except Exception as e:
        return None, f"Could not stage update script: {e}"
    return script_dst, ""


def _launch_script(script_dst: Path, *, is_windows: bool, port: int) -> str:
    """Spawn the staged update script detached. Returns "" or an error message.

    `--auto` and the port are positional extras that older scripts ignore
    harmlessly -- hop 1 of any upgrade is always launched by the previously
    installed version's script.
    """
    args = ["--auto", str(port)]
    try:
        if is_windows:
            logger.info("Launching update script: %s from %s", script_dst, _REPO_ROOT)
            subprocess.Popen(
                ["cmd", "/c", str(script_dst), *args],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=str(_REPO_ROOT),
            )
        else:
            subprocess.Popen(
                ["bash", str(script_dst), *args],
                start_new_session=True,
                cwd=str(_REPO_ROOT),
            )
    except Exception as e:
        logger.error("Failed to launch update script: %s", e)
        return f"Failed to launch update script: {e}"
    return ""


async def _run_step(chain: dict[str, Any], index: int) -> dict[str, Any]:
    """Download and launch one hop of the chain."""
    steps = chain["steps"]
    step = steps[index]
    is_windows = sys.platform == "win32"

    # Persist the attempt BEFORE anything can kill this process. The dominant
    # failure mode is "the new version never boots", and nothing is alive
    # afterwards to record it -- a hop that vanishes must still burn an attempt.
    step["attempts"] = int(step.get("attempts", 0)) + 1
    step["status"] = "launched"
    step["launched_at"] = _now()
    step["error"] = ""
    write_chain(chain)
    _chain_log(
        "hop %d/%d launching %s (attempt %d)",
        index + 1, len(steps), step["version"], step["attempts"],
    )

    error = await _download_zip(step["zip_url"])
    if not error:
        script_dst, error = _stage_script(is_windows=is_windows)
        if script_dst is not None:
            error = _launch_script(script_dst, is_windows=is_windows, port=settings.port)

    if error:
        step["status"] = "failed"
        step["error"] = error
        chain["status"] = "failed"
        chain["error"] = error
        write_chain(chain)
        _chain_log("hop %d/%d failed: %s", index + 1, len(steps), error)
        return {"success": False, "error": error}

    return {
        "success": True,
        "message": (
            f"Installing {step['version']} (step {index + 1} of {len(steps)}). "
            "The server will restart shortly."
        ),
    }


async def execute_update() -> dict[str, Any]:
    """Freeze an upgrade plan and launch its first hop.

    The plan is built from releases fetched right now and then never re-resolved,
    so a release published mid-chain cannot change where the user ends up.
    """
    existing = read_chain()
    if chain_is_active(existing):
        return {
            "success": False,
            "error": "An update is already in progress. Wait for it to finish, or abort it first.",
        }

    current = get_current_version()
    try:
        releases = await get_releases(force=True)
    except Exception as e:
        logger.warning("Could not list releases: %s", e)
        return {"success": False, "error": f"Could not list releases: {e}"}

    steps = build_chain(current, releases)
    if not steps:
        return {"success": False, "error": "No update available"}

    chain = {
        "schema_version": CHAIN_SCHEMA_VERSION,
        "status": "active",
        "created_at": _now(),
        "last_activity_at": _now(),
        "from_version": current,
        "target_version": steps[-1]["version"],
        "error": "",
        "steps": steps,
    }
    write_chain(chain)
    _chain_log(
        "frozen %s -> %s in %d step(s): %s",
        current, steps[-1]["version"], len(steps),
        ", ".join(s["version"] for s in steps),
    )

    return await _run_step(chain, 0)


# ---------------------------------------------------------------------------
# Resume after restart
# ---------------------------------------------------------------------------

def _health_url() -> str:
    scheme = "https" if settings.ssl_enabled else "http"
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host
    return f"{scheme}://{host}:{settings.port}/health"


async def _wait_for_server_ready(*, timeout: float = 120.0) -> bool:
    """Poll our own /health until this server is really serving.

    A listening port proves nothing -- the update script's maintenance page binds
    the same port. That page serves HTML for every path, so a JSON body with
    status "ok" is what proves the real server answered. Two consecutive
    successes guard against a flapping worker.

    /health is exempt from both rate limiters (see app/main.py), so polling it
    cannot trip them.
    """
    url = _health_url()
    deadline = time.monotonic() + timeout
    consecutive = 0

    await asyncio.sleep(2.0)
    async with httpx.AsyncClient(verify=False, timeout=3.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.json().get("status") == "ok":
                    consecutive += 1
                    if consecutive >= 2:
                        return True
                else:
                    consecutive = 0
            except Exception:
                consecutive = 0
            await asyncio.sleep(2.0)

    return False


def reconcile_chain_on_startup() -> dict[str, Any] | None:
    """Record the outcome of the hop that just booted; return the chain if more remain.

    Synchronous and side-effect-light so it can run inline during startup.
    """
    chain = read_chain()
    if chain is None:
        return None
    if chain.get("status") == "unrecognized":
        return None
    if not chain_is_active(chain):
        return None

    steps = chain.get("steps", [])
    index = _next_pending_index(chain)
    if index is None:
        clear_chain(status="completed")
        _chain_log("completed: reached %s", chain.get("target_version", "?"))
        return None

    step = steps[index]
    installed = get_current_version()

    if step.get("status") == "launched":
        # >=, not ==: a post-tag documentation commit or an asset built from a
        # different SHA can legitimately make the shipped CHANGELOG header differ
        # from the release tag. Requiring equality would halt valid chains.
        if _parse_version(installed) >= _parse_version(step["version"]):
            step["status"] = "installed"
            step["installed_at"] = _now()
            step["installed_version"] = installed
            _chain_log(
                "hop %d/%d installed (%s reported %s)",
                index + 1, len(steps), step["version"], installed,
            )
            if index + 1 >= len(steps):
                chain["status"] = "completed"
                write_chain(chain)
                clear_chain(status="completed")
                _chain_log("completed: now on %s", installed)
                return None
            write_chain(chain)
            return chain

        if int(step.get("attempts", 0)) >= _CHAIN_MAX_ATTEMPTS:
            chain["status"] = "failed"
            chain["error"] = (
                f"Step {index + 1} did not install after {step['attempts']} attempts "
                f"(expected {step['version']}, running {installed})."
            )
            step["status"] = "failed"
            step["error"] = chain["error"]
            write_chain(chain)
            _chain_log("failed: %s", chain["error"])
            return None

        chain["status"] = "version_mismatch"
        chain["error"] = (
            f"Expected {step['version']} after the update but found {installed}."
        )
        write_chain(chain)
        _chain_log("halted: %s", chain["error"])
        return None

    if _chain_expired(chain):
        chain["status"] = "expired"
        chain["error"] = "Chain was idle for more than 7 days and will not resume automatically."
        write_chain(chain)
        _chain_log("expired without completing")
        return None

    return chain


async def _resume_after_ready(chain: dict[str, Any]) -> None:
    """Wait for this server to be serving, then launch the next hop."""
    index = _next_pending_index(chain)
    if index is None:
        return

    if not await _wait_for_server_ready():
        chain["status"] = "stalled"
        chain["error"] = "Server did not become ready in time; next update was not started."
        write_chain(chain)
        _chain_log("stalled: %s", chain["error"])
        return

    # Give hop N's Windows .bat time to finish its trailing del/exit before hop
    # N+1 starts killing things on the same port.
    await asyncio.sleep(3.0)
    await _run_step(chain, index)


def start_chain_resume() -> asyncio.Task | None:
    """Continue a frozen chain if one is waiting. Called from the app lifespan."""
    if not settings.auto_update_chain_enabled:
        return None
    try:
        chain = reconcile_chain_on_startup()
    except Exception:
        logger.exception("Update chain reconciliation failed")
        return None
    if chain is None:
        return None
    return asyncio.create_task(_resume_after_ready(chain))


async def resume_chain_now() -> dict[str, Any]:
    """Admin-triggered retry of a failed, stalled, mismatched or expired chain.

    Clears the error and the attempt counter for the outstanding hop, then runs
    it immediately -- the frozen plan is reused as-is, never rebuilt, so a retry
    still lands on the version the admin originally approved.
    """
    chain = read_chain()
    if chain is None:
        return {"success": False, "error": "No update chain to resume"}
    if chain.get("status") == "unrecognized":
        return {"success": False, "error": "Update chain was written by a newer version"}

    index = _first_unfinished_index(chain)
    if index is None:
        clear_chain(status="completed")
        return {"success": False, "error": "Update chain has no remaining steps"}

    step = chain["steps"][index]
    step["status"] = "pending"
    step["attempts"] = 0
    step["error"] = ""
    chain["status"] = "active"
    chain["error"] = ""
    write_chain(chain)
    _chain_log("resumed by admin at hop %d/%d", index + 1, len(chain["steps"]))

    return await _run_step(chain, index)


def abort_chain() -> dict[str, Any]:
    """Discard a frozen chain so the admin can start over."""
    chain = read_chain()
    if chain is None:
        return {"success": False, "error": "No update chain to abort"}
    clear_chain(status="aborted")
    _chain_log("aborted by admin")
    return {"success": True, "message": "Update chain aborted."}


def chain_status() -> dict[str, Any]:
    """Summarise chain state for the admin UI, falling back to the last archive."""
    chain = read_chain()
    active = chain_is_active(chain)

    if chain is None:
        try:
            if _CHAIN_ARCHIVE_PATH.exists():
                chain = json.loads(_CHAIN_ARCHIVE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read archived update chain: %s", e)
            chain = None

    if chain is None:
        return {"active": False, "status": "", "steps": [], "log_tail": []}

    steps = chain.get("steps", [])
    installed = sum(1 for s in steps if s.get("status") == "installed")

    return {
        "active": active,
        "status": chain.get("status", ""),
        "from_version": chain.get("from_version", ""),
        "target_version": chain.get("target_version", ""),
        "current_step": min(installed + 1, len(steps)) if active else installed,
        "total_steps": len(steps),
        "error": chain.get("error", ""),
        "steps": [
            {
                "version": s.get("version", ""),
                "tag": s.get("tag", ""),
                "status": s.get("status", "pending"),
                "attempts": int(s.get("attempts", 0)),
                "error": s.get("error", ""),
                "release_url": s.get("release_url", ""),
            }
            for s in steps
        ],
        "log_tail": _chain_log_tail(),
    }


def _chain_log_tail(limit: int = 40) -> list[str]:
    try:
        if not _CHAIN_LOG_PATH.exists():
            return []
        return _CHAIN_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
