#!/usr/bin/env python3
"""Normalize H2 headings across examples/*.md walkthroughs.

Two passes per file:

  1. Alias rename — replace non-canonical H2 titles with their canonical form
     (e.g. `## Run it` → `## Run`, `## Related` → `## See also`).
     Never rename if the canonical form already exists in the same file
     (avoids duplicates); flag as a warning instead.

  2. Best-effort gap fill — if a walkthrough is missing one of the three
     minimum sections (Components used, Run, See also) AND the corresponding
     content can be inferred from a colocated setup_<demo>_demo.sh, insert
     a stub section with derived content.

     - Components used: parse `dagster-component add <id>` from the setup
       script and emit a bulleted list.
     - Run: emit the standard `curl … | bash setup_<demo>_demo.sh` block.
     - See also: emit an empty stub with a TODO marker (safer than guessing).

Usage:
  python3 tools/normalize_walkthroughs.py --dry-run
  python3 tools/normalize_walkthroughs.py --dry-run --only kafka.md,temporal_workflow.md
  python3 tools/normalize_walkthroughs.py --sample 5
  python3 tools/normalize_walkthroughs.py
"""
import argparse
import re
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Alias → canonical. Keys and values are lowercased for comparison but the
# canonical form on the right is what gets written to the file (Title Case).
ALIAS_MAP = {
    # Run
    "run it": "Run",
    "setup": "Run",
    "run the demo": "Run",
    "one-command demo": "Run",
    # See also
    "related": "See also",
    "related walkthroughs": "See also",
    # Components used
    "components covered": "Components used",
    "components exercised": "Components used",
    "components": "Components used",
    # Prerequisites
    "prereqs": "Prerequisites",
    # Required env vars
    "required env var": "Required env vars",
    # Teardown
    "cleanup": "Teardown",
    # Architecture
    "asset graph": "Architecture",
    "the asset graph": "Architecture",
    # Trade-offs & gotchas
    "trade-offs": "Trade-offs & gotchas",
    # What the script does
    "what the setup script does": "What the script does",
}

# Regex that ALSO matches counted variants like "Components covered (2)" —
# strip the count and rename to canonical.
COUNTED_ALIAS = re.compile(r"^(components covered)\s*\(\s*\d+\s*\)$", re.I)

CANONICAL_MIN = ["Components used", "Run", "See also"]

REPO_RAW = "https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples"


def canonical_form(title: str) -> str | None:
    """Return the canonical rename target for `title`, or None if not aliased."""
    t = title.strip().lower()
    if t in ALIAS_MAP:
        return ALIAS_MAP[t]
    m = COUNTED_ALIAS.match(t)
    if m:
        return "Components used"
    return None


def parse_setup_script_components(script: Path) -> list[str]:
    """Extract component ids from `dagster-component add <id>` calls."""
    if not script.exists():
        return []
    text = script.read_text()
    ids = set()
    # Explicit CLI adds: `dagster-component add <id>` or `$CLI add <id>`
    for m in re.finditer(r"\b(?:dagster-component|CLI)\s+add\s+([a-z0-9_@]+)", text):
        cid = m.group(1).split("@")[0]  # strip version pins
        if cid.startswith("-"):
            continue
        ids.add(cid)
    # Loop form: `for c in a b c; do ... dagster-component add "$c"` — walk
    # backwards from any `add "$c"` to find the `for c in` list.
    if re.search(r"add\s+\"?\$c\"?", text) or re.search(r"add\s+\"?\$COMP\"?", text):
        for loop in re.finditer(
            r"for\s+(?:c|COMP)\s+in\s+([^;]+?);\s*do", text, re.S
        ):
            items = loop.group(1).split()
            for item in items:
                item = item.strip().strip("\\").strip('"')
                if re.fullmatch(r"[a-z][a-z0-9_]+", item):
                    ids.add(item)
    return sorted(ids)


def build_components_used_section(ids: list[str]) -> str:
    lines = ["## Components used", ""]
    for cid in ids:
        lines.append(f"- `{cid}`")
    lines.append("")
    return "\n".join(lines)


def build_run_section(script_name: str) -> str:
    return (
        "## Run\n"
        "\n"
        "```bash\n"
        f"curl -fsSL {REPO_RAW}/{script_name} \\\n"
        f"  -o {script_name}\n"
        f"bash {script_name}\n"
        "```\n"
    )


def build_see_also_stub() -> str:
    return "## See also\n\n<!-- TODO: link related walkthroughs -->\n"


def existing_h2_titles(text: str) -> set[str]:
    """Set of lowercased H2 titles currently in the file."""
    return {m.group(1).strip().lower()
            for m in re.finditer(r"^## (.+)$", text, flags=re.MULTILINE)}


def rename_aliases(text: str, warnings: list[str], filename: str,
                    rename_log: list[tuple[str, str]] | None = None) -> tuple[str, int]:
    """Rewrite alias H2s to canonical form; skip when canonical already present."""
    lines = text.splitlines(keepends=True)
    present = existing_h2_titles(text)
    renamed = 0
    for i, line in enumerate(lines):
        m = re.match(r"^## (.+?)\s*$", line)
        if not m:
            continue
        title = m.group(1)
        canonical = canonical_form(title)
        if not canonical:
            continue
        if canonical.lower() in present and canonical.lower() != title.strip().lower():
            warnings.append(
                f"{filename}: has both `## {title}` and `## {canonical}`; "
                f"leaving alias alone (manual merge)."
            )
            continue
        lines[i] = f"## {canonical}\n"
        present.discard(title.strip().lower())
        present.add(canonical.lower())
        renamed += 1
        if rename_log is not None:
            rename_log.append((title, canonical))
    return "".join(lines), renamed


def insert_before_first_h2_or_end(text: str, block: str,
                                   skip_h2s: set[str] | None = None) -> str:
    if skip_h2s is None:
        skip_h2s = set()
    """Insert `block` before the first H2 that's NOT in skip_h2s. If no such
    H2 exists, append at end of file."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"^## (.+?)\s*$", line)
        if m and m.group(1).strip().lower() not in {s.lower() for s in skip_h2s}:
            return "".join(lines[:i]) + block + "\n" + "".join(lines[i:])
    # No suitable H2 — append.
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block


def fill_gaps(text: str, walkthrough_stem: str, notes: list[str]) -> str:
    """Add missing minimum sections when we can infer content."""
    script_name = f"setup_{walkthrough_stem}_demo.sh"
    script = EXAMPLES / script_name

    present = existing_h2_titles(text)

    # Components used — insert before first H2 that ISN'T Architecture/Pipeline/etc.
    if "components used" not in present:
        ids = parse_setup_script_components(script)
        if ids:
            block = build_components_used_section(ids)
            # Skip past Architecture / Pipeline / The story before inserting so
            # the components list sits AFTER any architectural framing.
            skip = {"architecture", "pipeline", "the story",
                    "what this demo shows", "what it demonstrates",
                    "why this exists", "why this matters"}
            text = insert_before_first_h2_or_end(text, block, skip)
            notes.append(f"  +Components used ({len(ids)} components from {script_name})")
            present.add("components used")

    # Run — insert at end just before See also if present, else append.
    if "run" not in present and script.exists():
        block = build_run_section(script_name)
        # Insert before "See also" if present; otherwise append.
        if "see also" in present:
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if re.match(r"^## See also\s*$", line, re.I):
                    text = "".join(lines[:i]) + block + "\n" + "".join(lines[i:])
                    break
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n" + block
        notes.append(f"  +Run (from {script_name})")
        present.add("run")

    # See also — append TODO stub at end.
    if "see also" not in present:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + build_see_also_stub()
        notes.append("  +See also (TODO stub)")

    return text


def process_file(path: Path, warnings: list[str]) -> tuple[str, list[tuple[str, str]], list[str]]:
    original = path.read_text()
    notes: list[str] = []
    rename_log: list[tuple[str, str]] = []
    text, _ = rename_aliases(original, warnings, path.name, rename_log)
    text = fill_gaps(text, path.stem, notes)
    return text, rename_log, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="Comma-separated .md filenames to limit to.")
    ap.add_argument("--sample", type=int, default=0,
                    help="Only process N files (first N by name) — for review.")
    args = ap.parse_args()

    if args.only:
        want = {p.strip() for p in args.only.split(",") if p.strip()}
        files = sorted(EXAMPLES / n for n in want if (EXAMPLES / n).exists())
    else:
        files = sorted(p for p in EXAMPLES.glob("*.md") if p.name != "README.md")
    if args.sample:
        files = files[: args.sample]

    total_renamed = 0
    total_filled_files = 0
    changed_files = 0
    warnings: list[str] = []

    for path in files:
        original = path.read_text()
        text, rename_log, notes = process_file(path, warnings)
        if text == original:
            continue
        changed_files += 1
        total_renamed += len(rename_log)
        if notes:
            total_filled_files += 1
        print(f"{path.name}: rename={len(rename_log)}, fills={len(notes)}")
        for old, new in rename_log:
            print(f"  ## {old}  →  ## {new}")
        for n in notes:
            print(n)
        if not args.dry_run:
            path.write_text(text)

    print()
    print(f"files touched:     {changed_files}")
    print(f"total renames:     {total_renamed}")
    print(f"files with fills:  {total_filled_files}")
    if warnings:
        print(f"\nwarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")
    if args.dry_run:
        print("\n(dry-run — no writes)")


if __name__ == "__main__":
    main()
