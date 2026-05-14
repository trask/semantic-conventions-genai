#!/usr/bin/env python3
"""Send due Slack notifications from accepted PR dashboard state."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from github_cli import detect_repo, list_open_prs
from notifications import next_notification_state
from state import (
    load_dashboard_state_cache,
    load_notification_state_file,
    load_state_file,
    notification_state_path,
    results_from_dashboard_state,
    save_notification_state_file,
    set_state_dir,
    union_merge_notification_state,
)
import state_branch
from utils import utc_now


def notify_slack_from_state(
    repo: str,
    prior_notification_state: Path | None,
) -> None:
    prs = list_open_prs(repo)
    open_pr_numbers = {p["number"] for p in prs}
    dashboard_state = load_dashboard_state_cache()
    results = results_from_dashboard_state(dashboard_state, open_pr_numbers)

    state_file_notification_state = load_notification_state_file()
    previous_state = state_file_notification_state
    if prior_notification_state and prior_notification_state.exists():
        prior = load_state_file(prior_notification_state)
        previous_state = union_merge_notification_state(previous_state, prior)

    notification_state = next_notification_state(
        repo,
        results,
        previous_state,
        utc_now(),
    )
    notification_state_changed = (notification_state.get("prs") or {}) != (
        state_file_notification_state.get("prs") or {}
    )
    if not notification_state_changed and state_file_notification_state.get("_loaded_from_dashboard"):
        print("notification state unchanged", file=sys.stderr)
        return

    save_notification_state_file(notification_state)


def prior_notification_state_path() -> Path:
    return Path(os.environ.get("RUNNER_TEMP", ".")) / "prior-notification-state.json"


def notify_slack(prior_notification_state: Path) -> int:
    repo = detect_repo()
    notify_slack_from_state(repo, prior_notification_state)
    return 0


def notify_slack_with_state(args: argparse.Namespace, state_dir: Path) -> int:
    prior_notification_state = prior_notification_state_path()
    return state_branch.push_state_changes(
        state_dir,
        "Update dashboard notification state",
        lambda: notify_slack(prior_notification_state),
        state_branch=args.state_branch,
        add_paths=["notification-state.json"],
        retry_snapshots=[(notification_state_path(), prior_notification_state)],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-branch",
        required=True,
        help="git branch used for workflow state",
    )
    args = parser.parse_args()
    with state_branch.temporary_state_dir() as state_dir:
        set_state_dir(state_dir)
        return notify_slack_with_state(args, state_dir)


if __name__ == "__main__":
    sys.exit(main())
