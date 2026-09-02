"""Documentation the assistant can read on demand.

Two sources, both shipped with the app. `docs/user-guide.md` is the single
source of truth for how the product works and is split here on the same `<a
id>` anchors the in-app help links use, so a rewritten section reaches the
assistant with no separate copy to keep honest. The concept files under
`app/services/assistant/docs/` cover the things the guide states but does not
explain in the depth a model needs -- the activation hierarchy, parameter
layers, pipeline order.

Loading happens once at import. A missing guide is a degraded install, not a
crash: the dictionary is empty and a warning is logged.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Same derivation as `app/main.py`'s `_docs_dir`, so the guide is found from
# wherever the app is launched.
_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "user-guide.md"
_CONCEPT_DIR = Path(__file__).resolve().parent / "docs"

_ANCHOR = re.compile(r'<a id="([A-Za-z0-9_-]+)"></a>')

# Cap on a single section handed back to the model. The longest guide sections
# are a few thousand words; this stops one of them eating a whole turn.
MAX_SECTION_CHARS = 12 * 1024
MAX_EXCERPT_CHARS = 1200

# Hash route -> guide anchor. The routes are the ones `NAV_PAGES` allows.
PAGE_ANCHORS: dict[str, str] = {
    "/": "dashboard",
    "/endpoints": "endpoints",
    "/cantrips": "cantrips",
    "/lorebooks": "lorebooks",
    "/skills": "skills",
    "/tags": "tags-and-groups",
    "/verification": "verification",
    "/memories": "memories",
    "/maps": "maps",
    "/packs": "content-packs",
    "/settings": "settings",
    "/debug/compare": "debug",
}

# Concept documents worth mentioning alongside a page's guide section.
PAGE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "/": ("pipeline-order",),
    "/endpoints": ("parameter-layers", "pipeline-order"),
    "/cantrips": ("cantrip-context-api", "activation-hierarchy", "pipeline-order"),
    "/lorebooks": ("activation-hierarchy", "pipeline-order"),
    "/skills": ("pipeline-order",),
    "/tags": ("tags", "activation-hierarchy"),
    "/verification": ("pipeline-order", "parameter-layers"),
    "/memories": ("pipeline-order",),
    "/maps": ("activation-hierarchy", "parameter-layers", "pipeline-order"),
    "/packs": (),
    "/settings": ("parameter-layers",),
    "/debug/compare": ("debug-and-sandbox",),
}


def _load_guide() -> dict[str, str]:
    """Split the user guide into `{anchor: markdown}`."""
    try:
        text = _GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Assistant docs: user guide not found at %s", _GUIDE_PATH)
        return {}

    matches = list(_ANCHOR.finditer(text))
    if not matches:
        logger.warning("Assistant docs: user guide has no <a id> anchors")
        return {}

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[start:end].strip()
    return sections


def _load_concepts() -> dict[str, str]:
    """Load the concept markdown files, keyed by filename stem."""
    if not _CONCEPT_DIR.is_dir():
        logger.warning("Assistant docs: concept directory missing at %s", _CONCEPT_DIR)
        return {}
    out: dict[str, str] = {}
    for path in sorted(_CONCEPT_DIR.glob("*.md")):
        try:
            out[path.stem] = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Assistant docs: could not read %s", path)
    return out


GUIDE: dict[str, str] = _load_guide()
CONCEPTS: dict[str, str] = _load_concepts()


def guide_section(anchor: str) -> str:
    """One guide section by anchor, capped."""
    text = GUIDE.get(anchor, "")
    if len(text) > MAX_SECTION_CHARS:
        return text[:MAX_SECTION_CHARS] + "\n\n[section truncated; use search_docs for the rest]"
    return text


def describe_page(page: str) -> str:
    """The guide section for a management UI page, plus related concepts."""
    key = (page or "").strip() or "/"
    anchor = PAGE_ANCHORS.get(key)
    if anchor is None:
        known = ", ".join(sorted(PAGE_ANCHORS))
        return f"No documentation for page '{page}'. Known pages: {known}."

    body = guide_section(anchor)
    if not body:
        body = f"The user guide has no section for '{anchor}' in this install."

    related = [name for name in PAGE_CONCEPTS.get(key, ()) if name in CONCEPTS]
    if related:
        body += "\n\nRelated concept documents (search_docs will return their text): " + ", ".join(related)
    return body


def _paragraphs(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _score(paragraph: str, terms: list[str]) -> int:
    lowered = paragraph.lower()
    return sum(lowered.count(term) for term in terms)


def search_docs(query: str, limit: int = 3) -> list[dict[str, str]]:
    """Term-scored search over the guide and the concept documents.

    Deliberately simple: split the query into terms, count occurrences per
    paragraph, return the best few with bounded excerpts. There is no index and
    no dependency, and the corpus is a few hundred kilobytes.
    """
    terms = [t for t in re.split(r"[^a-z0-9_]+", (query or "").lower()) if len(t) > 2]
    if not terms:
        return []

    try:
        limit = max(1, min(int(limit or 3), 10))
    except (TypeError, ValueError):
        limit = 3

    scored: list[tuple[int, str, str, str]] = []
    for anchor, text in GUIDE.items():
        for paragraph in _paragraphs(text):
            score = _score(paragraph, terms)
            if score:
                scored.append((score, "user-guide", anchor, paragraph))
    for stem, text in CONCEPTS.items():
        for paragraph in _paragraphs(text):
            score = _score(paragraph, terms)
            if score:
                scored.append((score, "concept", stem, paragraph))

    scored.sort(key=lambda row: (-row[0], row[1], row[2]))

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for score, source, anchor, paragraph in scored:
        if (source, anchor) in seen:
            continue
        seen.add((source, anchor))
        results.append(
            {
                "source": source,
                "anchor": anchor,
                "excerpt": paragraph[:MAX_EXCERPT_CHARS],
            }
        )
        if len(results) >= limit:
            break
    return results


def describe_tool(name: str) -> dict[str, Any]:
    """Full documentation and argument schema for one registered tool."""
    from app.services.assistant.registry import GROUPS, TOOLS
    from app.services.assistant.schema import tool_schema

    tool = TOOLS.get((name or "").strip())
    if tool is None:
        return {
            "error": {
                "status": 404,
                "detail": f"No tool named '{name}'. The tools you can call are listed in the system prompt.",
            }
        }
    group = GROUPS[tool.group]
    return {
        "name": tool.name,
        "summary": tool.summary,
        "doc": tool.doc,
        "risk": str(tool.risk),
        "kind": tool.kind,
        "group": {"key": group.key, "label": group.label, "page": group.route},
        "schema": tool_schema(tool)["function"]["parameters"],
    }


def _handle_describe_tool(ctx: Any, args: dict[str, Any]) -> Any:
    return describe_tool(str(args.get("name") or ""))


def _handle_describe_page(ctx: Any, args: dict[str, Any]) -> Any:
    return {"page": args.get("page"), "documentation": describe_page(str(args.get("page") or ""))}


def _handle_search_docs(ctx: Any, args: dict[str, Any]) -> Any:
    return {"results": search_docs(str(args.get("query") or ""), args.get("limit", 3))}


HANDLERS = {
    "describe_tool": _handle_describe_tool,
    "describe_page": _handle_describe_page,
    "search_docs": _handle_search_docs,
}
