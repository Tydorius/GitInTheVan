"""The user guide is a shipped artifact, not just a file in the repo.

`app/main.py` mounts `docs/` at `/help`, and the `?` button on every page opens
`user-guide.html#<section>`. Two things could rot silently, and both had:

1. Every one of those twelve links pointed at an anchor that did not exist. The
   links use bare names (`#settings`); Markdown generated numbered ids
   (`#14-settings`). Each help button dumped the reader at the top of a 74KB
   page. Explicit alias anchors in the Markdown fix it; this pins it.
2. The HTML was maintained by hand. Editing the Markdown left users reading the
   old page, with nothing to notice the divergence.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE_HTML = ROOT / "docs" / "user-guide.html"
GUIDE_MD = ROOT / "docs" / "user-guide.md"
FRONTEND = ROOT / "frontend" / "src"
BUILDER = ROOT / "scripts" / "build-user-guide.py"

HELP_LINK = re.compile(r'href="/help/user-guide\.html#([a-z0-9-]+)"')


def _help_anchors() -> set[str]:
    """Every anchor the app's help buttons link to."""
    anchors: set[str] = set()
    for path in FRONTEND.rglob("*.svelte"):
        anchors |= set(HELP_LINK.findall(path.read_text(encoding="utf-8")))
    return anchors


def test_the_app_actually_links_to_the_guide():
    """A guard on the guard: if the help links are ever renamed or removed,
    the test below would pass vacuously."""
    assert len(_help_anchors()) >= 10


def test_every_help_button_lands_on_its_section():
    html = GUIDE_HTML.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))

    missing = sorted(a for a in _help_anchors() if a not in ids)
    assert not missing, (
        f"Help buttons link to anchors that do not exist in user-guide.html: {missing}. "
        "Add an <a id=\"name\"></a> above the matching heading in user-guide.md and "
        "rerun scripts/build-user-guide.py."
    )


def test_internal_guide_links_resolve():
    """The guide's own cross-links, which had a dead `#11-settings` in the body."""
    html = GUIDE_HTML.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    targets = set(re.findall(r'href="#([a-z0-9-]+)"', html))

    broken = sorted(t for t in targets if t not in ids)
    assert not broken, f"Dead in-page links in the user guide: {broken}"


def test_referenced_screenshots_exist():
    html = GUIDE_HTML.read_text(encoding="utf-8")
    missing = [
        src for src in re.findall(r'src="(media/[^"]+)"', html)
        if not (GUIDE_HTML.parent / src).exists()
    ]
    assert not missing, f"Guide references images that are not in docs/media: {missing}"


def test_the_published_html_matches_the_markdown():
    """Fails when someone edits the Markdown and forgets to rebuild.

    Skipped when the dev-only `markdown` package is absent, since the builder
    cannot run at all then -- but the checks above still hold.
    """
    pytest.importorskip("markdown", reason="dev-only dependency for the docs builder")

    result = subprocess.run(
        [sys.executable, str(BUILDER), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"{GUIDE_MD.name} is newer than {GUIDE_HTML.name}.\n"
        "Run: python scripts/build-user-guide.py\n"
        f"{result.stdout}{result.stderr}"
    )
