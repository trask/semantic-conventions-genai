#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Re-sync the locally-owned semconv namespaces from upstream.

This repo is the canonical home for GenAI/MCP/OpenAI semantic conventions but
still lives downstream of `open-telemetry/semantic-conventions` until upstream
finishes deleting the migrated namespaces. While both repos coexist, we
periodically re-import the upstream copy of each migrated namespace into
`model/<namespace>/` to pick up community fixes that landed there, then
re-apply local changes via PR review.

This script does only the import step:

1. Clone (or fetch) `open-telemetry/semantic-conventions` at the requested
   ref (default `main`).
2. Replace each locally-owned namespace under `model/` with the matching
   subtree from upstream.

The freshly imported files may still be on Weaver's legacy `definition/1`
layout; run `internal/tools/convert_model_to_v2.py` separately to convert them.

Usage
-----
    # Pull upstream main:
    uv run internal/tools/overwrite_model_from_upstream.py

    # Pin to a tag/SHA:
    uv run internal/tools/overwrite_model_from_upstream.py --ref v1.41.0

Upstream is fresh-cloned into a temp directory on every run; this script is
rarely invoked, so caching adds complexity for no real win.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM_REPO = "https://github.com/open-telemetry/semantic-conventions.git"

# Namespaces under model/ that this repo owns and re-imports from upstream
# while the migration is in flight. Keep in sync with
# `SC_UPSTREAM_MIGRATED_DIRS` in the Makefile.
NAMESPACES = ("gen-ai", "mcp", "openai")

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO_ROOT / "model"

# Weaver build cache populated by the Makefile. Its `sc-upstream-filtered/`
# subtree is stamped only by `SEMCONV_VERSION`, so a previous partial build at
# the same pin gets reused even when upstream added shared attributes that the
# freshly re-imported model now references (observed: `aws.bedrock.*`). Wipe
# it on every re-import so the next `make check-policies` rebuilds from a
# clean upstream clone. This script is rarely invoked; the rebuild cost is
# irrelevant.
BUILD_DIR = REPO_ROOT / ".build"


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=True)


def _clone_upstream(clone_dir: Path, ref: str) -> None:
    """Full clone of upstream so any ref (branch, tag, SHA) resolves, then
    check it out. We fresh-clone every run; this script is rarely invoked."""
    _run(["git", "clone", "--quiet", UPSTREAM_REPO, str(clone_dir)])
    _run(
        ["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", ref],
        cwd=clone_dir,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=clone_dir, text=True
    ).strip()
    print(f"upstream checkout: {ref} -> {sha}", file=sys.stderr)


def _import_namespace(clone_dir: Path, namespace: str) -> None:
    src = clone_dir / "model" / namespace
    dst = MODEL_DIR / namespace
    if not src.is_dir():
        print(
            f"resync: upstream has no model/{namespace}/ at this ref -- "
            f"namespace removal upstream? skipping",
            file=sys.stderr,
        )
        return

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"imported model/{namespace}/", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--ref",
        default="main",
        help="upstream ref to import (branch, tag, or SHA; default: main).",
    )
    args = p.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="sc-upstream-") as tmp:
        clone_dir = Path(tmp) / "semantic-conventions"
        _clone_upstream(clone_dir, args.ref)
        for ns in NAMESPACES:
            _import_namespace(clone_dir, ns)

    if BUILD_DIR.exists():
        # Git packs inside `.build/sc-upstream-*/.git/` are read-only on
        # Windows, which makes `shutil.rmtree` raise PermissionError. Force
        # writable on each failed unlink and retry.
        def _force_writable(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(BUILD_DIR, onerror=_force_writable)
        print(f"cleared {BUILD_DIR.relative_to(REPO_ROOT)}/", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
