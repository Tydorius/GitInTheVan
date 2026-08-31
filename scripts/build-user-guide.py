#!/usr/bin/env python3
"""Regenerate docs/user-guide.html from docs/user-guide.md.

The HTML is what users actually read: `app/main.py` mounts `docs/` at `/help`,
and the `?` button on every page opens `user-guide.html#<section>`. Before this
script the HTML was produced by hand, so editing the Markdown left users looking
at the old page with no warning that the two had diverged.

Run it after any edit to the guide:

    python scripts/build-user-guide.py

`--check` exits non-zero when the HTML is out of date, which is what the test in
`tests/test_docs_build.py` uses. It never writes in that mode.

Requires the `markdown` package. It is not a runtime dependency of the app -- the
guide is built here and committed, not rendered on the server -- so it is not in
`requirements/`. Install it with `pip install markdown` if the import fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SRC = DOCS / "user-guide.md"
OUT = DOCS / "user-guide.html"

# Kept byte-for-byte from the hand-written original so regenerating does not
# reformat the page. Dark by default, matching the app it is served from.
STYLE = (
    "body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; "
    "max-width: 900px; margin: 0 auto; padding: 20px 40px; line-height: 1.6; color: #e0e0e6; "
    "background: #1a1d27; } "
    "h1 { color: #8b9ad0; border-bottom: 2px solid #2a2e3e; padding-bottom: 10px; } "
    "h2 { color: #8b9ad0; margin-top: 40px; border-bottom: 1px solid #2a2e3e; padding-bottom: 6px; } "
    "h3 { color: #a0aec0; } "
    "a { color: #6b8aff; } "
    "code { background: #15171f; padding: 2px 6px; border-radius: 4px; font-size: 13px; color: #c8c9d3; } "
    "pre { background: #15171f; padding: 12px; border-radius: 8px; overflow-x: auto; } "
    "pre code { background: none; padding: 0; } "
    "table { border-collapse: collapse; width: 100%; margin: 12px 0; } "
    "th, td { border: 1px solid #2a2e3e; padding: 8px 12px; text-align: left; } "
    "th { background: #15171f; } "
    "blockquote { border-left: 3px solid #8b9ad0; margin: 12px 0; padding: 8px 16px; "
    "background: #15171f; border-radius: 0 8px 8px 0; } "
    "img { max-width: 100%; border-radius: 8px; margin: 12px 0; }"
)

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitInTheVan User Guide</title>
<style>
{style}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def render() -> str:
    try:
        import markdown
    except ImportError:
        sys.exit(
            "This script needs the 'markdown' package (dev-only, not an app "
            "dependency).\n    pip install markdown"
        )

    text = SRC.read_text(encoding="utf-8")
    # `toc` supplies the heading ids the in-page links rely on; `attr_list` lets
    # the guide's explicit <a id="..."> alias anchors survive, which is what the
    # app's help buttons actually target.
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "attr_list", "md_in_html"],
        output_format="html",
    )
    return TEMPLATE.format(style=STYLE, body=body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the HTML is stale; write nothing",
    )
    args = parser.parse_args()

    if not SRC.exists():
        sys.exit(f"missing source: {SRC}")

    html = render()

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != html:
            print(
                "docs/user-guide.html is out of date.\n"
                "Run: python scripts/build-user-guide.py",
                file=sys.stderr,
            )
            return 1
        print("docs/user-guide.html is up to date")
        return 0

    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
