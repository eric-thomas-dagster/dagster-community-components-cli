#!/usr/bin/env python3
"""Generate blog/feed.xml (RSS 2.0) + blog/README.md Posts table from
the markdown files in blog/.

Reads YAML frontmatter (title / date / author / description) at the top
of each *.md file, produces an RSS 2.0 feed AND the Posts table in
blog/README.md, sorted newest-first.

Usage:
    python tools/generate_rss.py            # regen feed.xml + README Posts table
    python tools/generate_rss.py --check    # exit 1 if either is stale
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
BLOG_DIR = ROOT / "blog"
FEED_PATH = BLOG_DIR / "feed.xml"
README_PATH = BLOG_DIR / "README.md"

# Where the CLI repo (and this feed) lives publicly.
SITE_URL = "https://github.com/eric-thomas-dagster/dagster-community-components-cli"
RAW_URL = "https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main"
FEED_URL = f"{RAW_URL}/blog/feed.xml"

CHANNEL_TITLE = "Dagster Community Components — Blog"
CHANNEL_DESCRIPTION = (
    "Long-form posts from Eric Thomas on the Dagster community components "
    "registry — design essays, component tours, honest retrospectives."
)
CHANNEL_LANGUAGE = "en-us"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract YAML frontmatter (between `---` fences at the very top of the file).

    Returns (frontmatter_dict, body_text). Frontmatter is required — files
    without it are skipped by the caller.
    """
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        fm[key] = value
    return fm, body


def parse_date(raw: str) -> datetime:
    """Parse an ISO date (YYYY-MM-DD) into an aware UTC datetime at midnight."""
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def read_post(path: Path) -> dict | None:
    """Read a blog post file, return a dict with title/date/author/description/link — or None if unparseable."""
    text = path.read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(text)
    if not fm.get("title") or not fm.get("date"):
        return None
    try:
        pub_date = parse_date(fm["date"])
    except ValueError:
        return None
    return {
        "title": fm["title"],
        "author": fm.get("author", "Eric Thomas"),
        "description": fm.get("description", ""),
        "date": pub_date,
        "link": f"{SITE_URL}/blob/main/blog/{path.name}",
        "guid": f"{RAW_URL}/blog/{path.name}",
        "filename": path.name,
    }


def build_readme_posts_section(posts: list[dict]) -> str:
    """Emit the ## Posts table body (rows only) for blog/README.md."""
    lines = ["| Date | Title |", "|---|---|"]
    for p in posts:
        slug = p["filename"]
        date = p["date"].date().isoformat()
        title = p["title"]
        desc = p["description"]
        lines.append(f"| {date} | [{title}]({slug}) — {desc} |")
    return "\n".join(lines)


def splice_readme_posts(current: str, new_table: str) -> str:
    """Replace the `## Posts` … `## Writing a new post` block's TABLE only,
    preserving the section headers. Returns the updated README text."""
    pattern = re.compile(
        r"(## Posts\n\n)(.*?)(\n\n## Writing a new post)",
        re.DOTALL,
    )
    if not pattern.search(current):
        raise RuntimeError(
            "blog/README.md missing `## Posts\\n\\n<table>\\n\\n## Writing a new post` "
            "block — table cannot be spliced. Fix by hand once, then re-run."
        )
    return pattern.sub(rf"\g<1>{new_table}\g<3>", current)


def build_feed(posts: list[dict]) -> str:
    """Emit RSS 2.0 XML for a sorted list of posts (newest first)."""
    now = datetime.now(timezone.utc)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{escape(CHANNEL_TITLE)}</title>",
        f"    <link>{escape(SITE_URL)}/tree/main/blog</link>",
        f"    <description>{escape(CHANNEL_DESCRIPTION)}</description>",
        f"    <language>{CHANNEL_LANGUAGE}</language>",
        f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        f'    <atom:link href="{escape(FEED_URL)}" rel="self" type="application/rss+xml" />',
    ]
    for p in posts:
        parts += [
            "    <item>",
            f"      <title>{escape(p['title'])}</title>",
            f"      <link>{escape(p['link'])}</link>",
            f"      <guid isPermaLink=\"false\">{escape(p['guid'])}</guid>",
            f"      <pubDate>{format_datetime(p['date'])}</pubDate>",
            f"      <dc:creator xmlns:dc=\"http://purl.org/dc/elements/1.1/\">{escape(p['author'])}</dc:creator>",
            f"      <description>{escape(p['description'])}</description>",
            "    </item>",
        ]
    parts += ["  </channel>", "</rss>", ""]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the on-disk feed.xml differs from what would be generated.",
    )
    args = parser.parse_args()

    md_files = sorted(BLOG_DIR.glob("*.md"))
    posts = [p for p in (read_post(f) for f in md_files) if p is not None]
    posts.sort(key=lambda p: p["date"], reverse=True)

    if not posts:
        print("no posts with valid frontmatter found in blog/", file=sys.stderr)
        return 1

    xml = build_feed(posts)
    new_table = build_readme_posts_section(posts)
    current_readme = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""
    new_readme = splice_readme_posts(current_readme, new_table) if current_readme else current_readme

    if args.check:
        current_feed = FEED_PATH.read_text(encoding="utf-8") if FEED_PATH.exists() else ""
        # Ignore lastBuildDate churn — replace both sides' lastBuildDate for the diff.
        strip = re.compile(r"<lastBuildDate>[^<]+</lastBuildDate>")
        feed_stale = strip.sub("", current_feed) != strip.sub("", xml)
        readme_stale = current_readme != new_readme
        if not feed_stale and not readme_stale:
            return 0
        rel = Path(__file__).relative_to(ROOT)
        if feed_stale:
            print(f"feed.xml is stale — run: python {rel}", file=sys.stderr)
        if readme_stale:
            print(f"blog/README.md Posts table is stale — run: python {rel}", file=sys.stderr)
        return 1

    FEED_PATH.write_text(xml, encoding="utf-8")
    if new_readme and new_readme != current_readme:
        README_PATH.write_text(new_readme, encoding="utf-8")
        readme_status = "updated"
    else:
        readme_status = "unchanged"
    print(f"wrote {FEED_PATH} — {len(posts)} post(s); blog/README.md Posts table {readme_status}")
    for p in posts:
        print(f"  {p['date'].date()}  {p['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
