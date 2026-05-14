#!/usr/bin/env python3
"""Union-merge two notification-state.json ledgers.

Used by the PR dashboard workflow after a rejected `git push --force-with-lease`
of the state branch. The first run already sent Slack notifications and wrote
the local ledger; the reset to the remote tip would otherwise discard those
just-sent entries and the next retry would re-send the same pings.

For each PR slot, the entry with the most recent `last_notified_at` wins (or
`waiting_since` as a tiebreaker), so the next retry's 24h cadence gate sees
our just-sent pings as already-notified.

Usage: merge-notification-state.py <local.json> <remote.json>

Writes the merged result back to <remote.json>.
"""

import json
import pathlib
import sys


def entry_ts(entry: dict) -> str:
    return entry.get("last_notified_at") or entry.get("waiting_since") or ""


def main() -> int:
    local_path, remote_path = sys.argv[1], sys.argv[2]
    local = json.loads(pathlib.Path(local_path).read_text(encoding="utf-8"))
    remote_p = pathlib.Path(remote_path)
    if remote_p.exists():
        remote = json.loads(remote_p.read_text(encoding="utf-8"))
    else:
        remote = {"version": 1, "prs": {}}

    merged_prs = dict(remote.get("prs") or {})
    for pr_key, local_entry in (local.get("prs") or {}).items():
        remote_entry = merged_prs.get(pr_key)
        if remote_entry is None or entry_ts(local_entry) > entry_ts(remote_entry):
            merged_prs[pr_key] = local_entry

    remote_p.write_text(
        json.dumps({"version": 1, "prs": merged_prs}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
