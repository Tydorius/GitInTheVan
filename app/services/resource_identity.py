"""Decentralized identity for installable resources.

Two independent keys, used in that order by the pack installer:

**Origin** -- ``(normalized repo URL, resource path)``. Stable, human-meaningful,
and requires no central registry: a self-hosted forge, a GitHub fork and a local
directory each produce their own namespace. This is what makes "I already have
this cantrip" answerable.

**Content hash** -- sha256 over the *meaningful* fields of a resource, so
formatting churn (key order, an added description) does not change identity.
Catches copies with no origin at all: hand-made resources, and ones that arrived
through Maps -> Import JSON from a file rather than a repo.

Neither is authoritative over the content itself. A hash mismatch against a
known origin is a signal to warn, never to overwrite -- see
``app/services/map_transfer.py``.
"""
from __future__ import annotations

import hashlib
import json
import re

# Folders a repo publishes installable resources from. A resource path may not
# address anything else -- see safe_repo_join() in app/services/git_sync.py.
RESOURCE_FOLDERS = (
    "cantrips",
    "lorebooks",
    "skills",
    "rules",
    "scenario_rules",
    "maps",
)

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")


def normalize_repo_url(url: str) -> str:
    """Reduce the ways of writing one repo location to a single string.

    ``git@github.com:x/y.git``, ``https://github.com/x/y/`` and
    ``https://github.com/x/y.git`` all normalize to ``github.com/x/y``. The
    scheme is dropped deliberately: cloning one repo over ssh and over https is
    the same repo, and treating them separately would defeat deduplication for
    anyone who switched transports. A local repo (a filesystem path rather than
    a URL) normalizes to a resolved absolute path, case-folded, since Windows
    paths are case-insensitive.

    Returns "" for empty input, which callers read as "same repo as the file
    that referenced it".
    """
    url = (url or "").strip()
    if not url:
        return ""

    if "://" in url:
        _scheme, rest = url.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
        else:
            host, path = rest, ""
        # Credentials embedded in the URL are not part of its identity.
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        return f"{host.lower()}/{_strip_git_suffix(path)}".rstrip("/")

    # A Windows drive letter looks like scp syntax; a path does not.
    looks_like_path = (
        len(url) > 1 and url[1] == ":"
    ) or url.startswith(("/", "\\", ".", "~"))

    if not looks_like_path:
        match = _SCP_LIKE.match(url)
        if match:
            host = match.group("host").lower()
            path = _strip_git_suffix(match.group("path"))
            return f"{host}/{path}".rstrip("/")

    return _normalize_local_path(url)


def _normalize_local_path(path: str) -> str:
    from pathlib import Path

    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return path.replace("\\", "/").rstrip("/").casefold()
    return str(resolved).replace("\\", "/").rstrip("/").casefold()


def _strip_git_suffix(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def parse_resource_path(path: str) -> tuple[str, str]:
    """Split a resource path into ``(file_path, resource_key)``.

    Not every resource is published as its own file. One embedded in a map is
    addressed through the map that carries it::

        cantrips/Dice_Controller.json                    -> ("cantrips/...", "")
        maps/pipeline.json:Dice Controller               -> ("maps/...", "Dice Controller")

    Repo-relative paths never contain ``:``, so the first one separates the two
    halves. The key is the resource *name* as it appears in the file: that is
    what an author edits, and it survives a re-export where an index would not.
    """
    path = (path or "").strip()
    if not path:
        return "", ""
    file_path, sep, key = path.partition(":")
    if not sep:
        return path, ""
    return file_path.strip(), key.strip()


def join_resource_path(file_path: str, resource_key: str = "") -> str:
    """Inverse of parse_resource_path()."""
    file_path = (file_path or "").strip()
    resource_key = (resource_key or "").strip()
    if not resource_key:
        return file_path
    return f"{file_path}:{resource_key}"


def canonical_payload(resource_type: str, content: dict) -> dict:
    """The fields that decide whether two resources are the same thing.

    Descriptions, versions and authorship are deliberately excluded: re-tagging
    a cantrip is not a new cantrip, and treating it as one would defeat the
    deduplication this exists for.
    """
    content = content or {}

    if resource_type == "cantrip":
        return {
            "name": (content.get("name") or "").strip(),
            "code": (content.get("code") or "").strip(),
            "llm_instructions": (content.get("llm_instructions") or "").strip(),
        }

    if resource_type == "lorebook":
        entries = content.get("entries") or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        canon_entries = [
            {
                "name": (e.get("name") or "").strip(),
                "keys": sorted(_as_list(e.get("keys"))),
                "secondary_keys": sorted(_as_list(e.get("secondary_keys"))),
                "content": (e.get("content") or "").strip(),
                "position": e.get("position") or "",
                "insertion_order": e.get("insertion_order") or 0,
                "is_constant": bool(e.get("is_constant")),
                "is_selective": bool(e.get("is_selective")),
            }
            for e in entries
            if isinstance(e, dict)
        ]
        # Entry order is presentation, not identity; insertion_order carries the
        # ordering that actually matters and is inside each entry.
        canon_entries.sort(key=lambda e: (e["name"], e["content"][:64]))
        return {
            "name": (content.get("name") or "").strip(),
            "entries": canon_entries,
        }

    if resource_type in ("skill", "sample"):
        declared = content.get("type")
        return {
            "name": (content.get("name") or "").strip(),
            "content": (content.get("content") or "").strip(),
            "type": declared if declared in ("skill", "sample") else resource_type,
        }

    if resource_type == "rule":
        return {
            "name": (content.get("name") or "").strip(),
            "prompt": (content.get("prompt") or "").strip(),
        }

    if resource_type == "scenario_rule":
        return {
            "name": (content.get("name") or "").strip(),
            "prompt": (content.get("prompt") or "").strip(),
            "fire_position": content.get("fire_position") or "",
            "token_threshold": content.get("token_threshold") or 0,
        }

    # Unknown types hash their whole payload rather than silently colliding.
    return {"name": (content.get("name") or "").strip(), "_raw": content}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return [value]
        value = parsed if isinstance(parsed, list) else [value]
    if not isinstance(value, list):
        return [str(value)]
    return [str(v) for v in value]


def content_hash(resource_type: str, content: dict) -> str:
    """``sha256:<hex>`` over the canonical payload for this resource type."""
    payload = canonical_payload(resource_type, content)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
