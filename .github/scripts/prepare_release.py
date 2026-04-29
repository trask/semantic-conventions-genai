#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Prepare a release of this registry.

Performs the deterministic, file-level edits a release requires:

  1. Bump ``schema_url`` in ``model/manifest.yaml`` to the new version.
  2. Roll ``CHANGELOG.md``: rename the ``## Unreleased`` heading to
     ``## v<version> (<date>)``, drop empty (no-bullet) subsections from
     that block, and insert a fresh empty ``## Unreleased`` block above it.

This script does NOT regenerate docs, commit, tag, or push. The
``prepare-release`` workflow drives it and runs ``make generate-docs``
afterwards so the registry pages pick up the new ``schema_url``.

Usage:
    uv run --no-project .github/scripts/prepare_release.py --version 1.43.0
    uv run --no-project .github/scripts/prepare_release.py --version 1.43.0 --date 2026-05-01

Version must be ``MAJOR.MINOR.PATCH`` with no leading ``v``. Date defaults
to today (UTC).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "model" / "manifest.yaml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

SCHEMA_URL_RE = re.compile(
    r"^(schema_url:\s*https://opentelemetry\.io/schemas/gen-ai/)"
    r"(?P<version>\d+\.\d+\.\d+)\s*$",
    re.MULTILINE,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Empty changelog subsection: a `### …` heading followed only by blank lines
# until the next `### `, `## `, or end-of-file. Matched against the body of
# the section being rolled (between `## Unreleased` and the next `## `).
EMPTY_SUBSECTION_RE = re.compile(
    r"^### [^\n]*\n(?:[ \t]*\n)*(?=^### |^## |\Z)",
    re.MULTILINE,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--version",
        required=True,
        help="New version, e.g. 1.43.0 (no leading 'v').",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Release date in YYYY-MM-DD (default: today UTC).",
    )
    return p.parse_args(argv)


def bump_manifest(new_version: str) -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    m = SCHEMA_URL_RE.search(text)
    if not m:
        raise SystemExit(
            f"Could not find a 'schema_url: https://opentelemetry.io/schemas/gen-ai/<version>'"
            f" line in {MANIFEST.relative_to(REPO_ROOT)}"
        )
    old_version = m.group("version")
    if old_version == new_version:
        raise SystemExit(
            f"manifest.yaml already at version {new_version}; nothing to bump."
        )
    if _semver_tuple(new_version) <= _semver_tuple(old_version):
        raise SystemExit(
            f"Refusing to bump backwards: manifest is at {old_version},"
            f" requested {new_version}."
        )
    new_text = SCHEMA_URL_RE.sub(rf"\g<1>{new_version}", text, count=1)
    MANIFEST.write_text(new_text, encoding="utf-8")
    return old_version


def _semver_tuple(v: str) -> tuple[int, int, int]:
    parts = v.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def roll_changelog(new_version: str, date_str: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Locate the ## Unreleased heading and the boundary of its section.
    unreleased_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "## Unreleased":
            unreleased_idx = i
            break
    if unreleased_idx is None:
        raise SystemExit(
            f"Could not find a '## Unreleased' heading in"
            f" {CHANGELOG.relative_to(REPO_ROOT)}"
        )

    # Section runs until the next `## ` heading (or EOF).
    next_section_idx = len(lines)
    for j in range(unreleased_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            next_section_idx = j
            break

    section_body = "".join(lines[unreleased_idx + 1 : next_section_idx])
    pruned_body = EMPTY_SUBSECTION_RE.sub("", section_body)
    # Collapse runs of >2 consecutive blank lines that pruning may leave behind.
    pruned_body = re.sub(r"\n{3,}", "\n\n", pruned_body)
    # Ensure exactly one trailing blank line before the next `## ` heading
    # (or EOF), and a leading blank line after the new dated heading.
    pruned_body = pruned_body.strip("\n")
    if pruned_body:
        pruned_body = "\n" + pruned_body + "\n\n"
    else:
        pruned_body = "\n"

    dated_heading = f"## v{new_version} ({date_str})\n"
    fresh_unreleased = (
        "## Unreleased\n"
        "\n"
        "### 🛑 Breaking changes 🛑\n"
        "\n"
        "### 🚩 Deprecations 🚩\n"
        "\n"
        "### 🚀 New components 🚀\n"
        "\n"
        "### 💡 Enhancements 💡\n"
        "\n"
        "### 🧰 Bug fixes 🧰\n"
        "\n"
        "### 📚 Clarifications 📚\n"
        "\n"
    )

    new_text = (
        "".join(lines[:unreleased_idx])
        + fresh_unreleased
        + dated_heading
        + pruned_body
        + "".join(lines[next_section_idx:])
    )
    CHANGELOG.write_text(new_text, encoding="utf-8")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not SEMVER_RE.match(args.version):
        raise SystemExit(
            f"--version must be MAJOR.MINOR.PATCH (no leading 'v'); got {args.version!r}"
        )
    date_str = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise SystemExit(
            f"--date must be YYYY-MM-DD; got {date_str!r}"
        )

    old_version = bump_manifest(args.version)
    roll_changelog(args.version, date_str)
    print(
        f"Prepared release v{args.version} (was v{old_version}, dated {date_str}).\n"
        f"Next: run `make generate-docs` and review the diff."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
