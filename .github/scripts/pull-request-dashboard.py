#!/usr/bin/env python3
"""Generate a deterministic PR review dashboard with thread-level LLM triage.

The script keeps repository facts deterministic and asks the LLM only one
narrow question per unresolved discussion thread: who has the next action for
that thread?

By default, updates the dashboard issue. In dry-run mode, writes
pull-request-dashboard.md for local inspection.

Usage:
    python .github/scripts/pull-request-dashboard.py
    python .github/scripts/pull-request-dashboard.py --dry-run
                                                   [--jobs N]
                                                   [--model NAME]

Architecture overview
---------------------

State that survives across runs lives in two files under --state-dir:

  dashboard-state.json     cached per-PR routing results
  notification-state.json  per-PR Slack history

The workflow checks out an orphan branch (otelbot/pull-request-dashboard-state) into
--state-dir before invoking this script, and after the script returns
commits + pushes both files with `git push --force-with-lease`. That gives
us a real atomic CAS via git refs: concurrent runs that race on state
deterministically lose the push and the workflow re-runs the script
against the freshly fetched state.

The dashboard issue body is rendered fresh each run; no state markers are
embedded in it.

A run flows like this:

  list_open_prs
       v
  compute_pr_results
       single-PR + cache hit:  reuse cached results, recompute only the trigger PR
       otherwise:              rebuild all PRs in parallel
       v
  load_notification_state_file
       v
  next_notification_state            (also performs Slack I/O)
       v
  reconcile_with_latest_dashboard
       reload dashboard-state in case a concurrent run updated it
       v
  render_dashboard_body                (write pull-request-dashboard.md)
       v
  save_dashboard_state_cache           + save_notification_state_file

The workflow commits and pushes state files first. Only after that state
branch push succeeds does it publish pull-request-dashboard.md to the
dashboard issue.

Full (no --pr-number) runs always rebuild every PR and write unconditionally.
Single-PR runs are optimistic-concurrency updates of just one PR slot in the
cached state.

Field schemas
-------------

Two record shapes flow through the pipeline as ``dict[str, Any]``. They are
built up across stages, so not every field is present at every point.

``result`` (one per PR) — produced by ``build_pr_result``:

  pr_number             int            PR number.
  pr_title              str            PR title.
  pr_url                str            PR URL.
  failed                bool           True on any failure; False on success.
  route                  str            Routing bucket: one of ROUTE_ORDER
                                       ("maintainer", "approver", "author",
                                       "external", "transient-failure",
                                       "unknown") or "draft".
  facts                 dict           See below. Empty on failure.
  threads               list[dict]     Unresolved discussion threads. Internal.
  classifications       list[dict]     Per-thread LLM decisions. Internal.
  error                 str            Error detail, set only on failure paths.

Only ``pr_number``, ``pr_url``, ``failed``, ``route``, and ``facts``
survive into the cached dashboard state (see ``stored_result``).

``facts`` (one per PR) — built in two stages:

  Stage 1 — compute_facts (deterministic from GitHub data):
    author                          str           Effective author (human, after
                                                  bot-delegation resolution).
    assignees                       list[str]     PR assignees.
    is_otelbot_author               bool          PR opened by app/otelbot.
    is_draft                        bool
    approved                        bool          reviewDecision == APPROVED.
    ci_failing_count                int           Absent when checks could not
                                                  be fetched.
    ci_pending_count                int           Absent when checks could not
                                                  be fetched.
    conflicts                       str           "yes" | "no" | "unknown".
    created_at                      str (iso)
    last_activity_at                str (iso)
    last_author_activity_at         str (iso)
    last_approver_activity_at       str (iso)
    last_external_activity_at       str (iso)

  Stage 2 — add_wait_age_facts (depends on routing + threads):
    waiting_since                   str (iso)     Oldest pending thread, or
                                                  route-appropriate fallback,
                                                  or PR creation time.
    waiting_age_basis               str           Which heuristic chose
                                                  waiting_since.

Stage-2 fields are absent on failure paths (failed is True). Human-readable
``age`` strings (e.g. ``3h``) are derived at render time from these
timestamps rather than persisted, so the cached JSON stays stable across
runs when no underlying PR data has changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- dashboard issue identity ----------------------------------------------
DRY_RUN_OUTPUT = "pull-request-dashboard.md"

# --- CLI defaults ----------------------------------------------------------
# Default --jobs: parallel PRs processed at once (each PR's threads are
# classified sequentially within that worker).
DEFAULT_JOBS = 4
DEFAULT_MODEL = "gpt-5.4-mini"

# --- gh subprocess retry ---------------------------------------------------
GH_RETRY_ATTEMPTS = 4
GH_RETRY_DELAY_SECONDS = 1.5

# --- LLM prompt + classification -------------------------------------------
# subprocess timeout (seconds) for a single `copilot` invocation classifying
# one discussion thread.
LLM_THREAD_TIMEOUT_SECONDS = 180

# Per-PR thread classification cache. Keyed by sha256 of the thread JSON
# only (not the full prompt), so events that change PR-level facts but not
# thread content (e.g. pull_request_review.submitted) re-use prior
# classifications. The workflow restores/saves this directory via
# actions/cache scoped to TRIGGER_PR_NUMBER.
CLASSIFICATION_CACHE_DIR = Path(".cache/pr-classifications")
# Directory holding state that must survive across runs:
#   dashboard-state.json     cached per-PR routing results
#   notification-state.json  per-PR Slack history
# The workflow checks out an orphan branch (otelbot/pull-request-dashboard-state) into
# this directory and commits + pushes the updated files with
# --force-with-lease after the dashboard run, giving us a real atomic CAS
# via git refs. Overridden at runtime with --state-dir.
DEFAULT_STATE_DIR = Path("state")
_state_dir: Path = DEFAULT_STATE_DIR


def set_state_dir(path: Path) -> None:
    global _state_dir
    _state_dir = path


def dashboard_state_path() -> Path:
    return _state_dir / "dashboard-state.json"


def notification_state_path() -> Path:
    return _state_dir / "notification-state.json"
# Per-thread, keep at most this many of the most recent comments when
# building the LLM prompt (older comments are dropped, not truncated).
THREAD_RECENT_COMMENTS_LIMIT = 20
# Secondary per-comment cap applied only when the formatted prompt would
# still exceed MAX_PROMPT_CHARS after the comment-window trim.
THREAD_COMMENT_BODY_MAX_CHARS = 500
# Default truncation length (chars) for free-text bodies passed to the LLM
# (commit messages, comment bodies, PR description).
DEFAULT_TRUNCATE_CHARS = 1200
# Soft cap on the total length (chars) of the rendered LLM prompt; if
# exceeded, thread comments are re-trimmed with THREAD_COMMENT_BODY_MAX_CHARS.
MAX_PROMPT_CHARS = 18_000

# --- Slack notifications ---------------------------------------------------
# Quiet period before re-pinging an approver about the same PR via Slack
# (also gated on weekday in the call site).
APPROVER_FOLLOW_UP_SECONDS = 24 * 60 * 60
SLACK_WEBHOOK_RETRY_ATTEMPTS = 3
SLACK_WEBHOOK_RETRY_DELAY_SECONDS = 1.0

APPROVER_TEAM_SLUGS = [
    "semconv-genai-approvers",
]

THREAD_PROMPT_TEMPLATE = """You are triaging one discussion thread from pull request #{number} in {repo}.

Classify ONLY this one thread. You are not deciding the final dashboard section.
The final routing is computed later from deterministic facts and all thread
classifications.

Question: who has the next action for this discussion thread?

Use these labels:
  - author: the PR author needs to respond, implement, rebase, or otherwise act
  - reviewer: a reviewer/approver/maintainer needs to review, answer, approve, or merge
  - external: the thread is blocked on something outside this repository
  - none: no follow-up is needed for this thread
  - unclear: the thread does not contain enough information to decide

Guidance:
  - Default heuristic: whoever commented last has passed the ball to the other
    side. If the latest comment is from a reviewer/approver, the author owes a
    response (classify as author). If the latest comment is from the author,
    the reviewer owes a response (classify as reviewer).
  - This applies even to optional suggestions, "for ideas" links, references,
    or links to a reviewer's own pull request / patch with proposed changes.
    The author still needs to acknowledge, accept, or push back.
  - Exceptions that map to none:
    - Purely social comments ("thanks", "LGTM", "nice work") with no follow-up
      requested or implied.
    - The reviewer's last comment is a clear acknowledgement of the author's
      previous reply ("sounds good", "ok thanks") that closes the thread.
  - Exception that keeps the ball with the author: if the author's latest
    comment is a self-deferral ("still working on it", "WIP", "I'll get to
    this", "will fix") rather than a question or completed reply, classify as
    author — they have not yet handed the ball back.

Respond with a single JSON object and nothing else:
{{"thread_action": "author" | "reviewer" | "external" | "none" | "unclear", "reason": "short explanation grounded in this thread"}}

---BEGIN PR FACTS---
{facts}
---END PR FACTS---

---BEGIN THREAD---
{thread}
---END THREAD---
"""


# ---------------------------------------------------------------- gh helpers


class TransientGhError(RuntimeError):
    pass


_RETRYABLE_GH_ERROR_FRAGMENTS = (
    "http 5",
    "gateway timeout",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
)


def is_retryable_gh_error(stderr: str) -> bool:
    text = stderr.lower()
    return any(fragment in text for fragment in _RETRYABLE_GH_ERROR_FRAGMENTS)


def sleep_for_retry(attempt: int) -> None:
    time.sleep(GH_RETRY_DELAY_SECONDS * (attempt + 1))


def run_gh(
    cmd: list[str],
    token: str | None = None,
    input_text: str | None = None,
    allowed_exit_codes: frozenset[int] | set[int] = frozenset({0}),
) -> str:
    env = {**os.environ, "GH_TOKEN": token} if token else None
    last_stderr = ""
    for attempt in range(GH_RETRY_ATTEMPTS):
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if proc.returncode in allowed_exit_codes:
            return proc.stdout
        last_stderr = proc.stderr.strip()
        if attempt == GH_RETRY_ATTEMPTS - 1 or not is_retryable_gh_error(last_stderr):
            break
        sleep_for_retry(attempt)
    message = f"{' '.join(cmd)} failed: {last_stderr}"
    if is_retryable_gh_error(last_stderr):
        raise TransientGhError(message)
    raise RuntimeError(message)


def run_gh_json(cmd: list[str], token: str | None = None, input_text: str | None = None) -> Any:
    return json.loads(run_gh(cmd, token=token, input_text=input_text) or "null")


def gh_api(path: str, paginate: bool = False, token: str | None = None) -> Any:
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    cmd.append(path)
    data = run_gh_json(cmd, token=token)
    if paginate and isinstance(data, list):
        flat: list[Any] = []
        for page in data:
            if isinstance(page, list):
                flat.extend(page)
            else:
                flat.append(page)
        return flat
    return data


def gh_graphql(query: str, fields: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in fields.items():
        if value is None:
            continue
        cmd.extend(["-F", f"{name}={value}"])
    return run_gh_json(cmd, token=token)


def gh_pr_view(repo: str, number: int) -> dict[str, Any]:
    fields = ",".join([
        "number", "title", "url", "author", "state", "isDraft",
        "mergeable", "mergeStateStatus", "createdAt", "updatedAt",
        "reviewDecision", "assignees",
        "additions", "deletions", "changedFiles", "baseRefName",
        "headRefOid", "body",
    ])
    cmd = ["gh", "pr", "view", str(number), "--repo", repo, "--json", fields]
    # `run_gh_json` retries transient subprocess failures; this loop retries
    # the orthogonal case where GitHub returns mergeable=UNKNOWN while it
    # finishes computing mergeability.
    last: dict[str, Any] = {}
    for attempt in range(GH_RETRY_ATTEMPTS):
        last = run_gh_json(cmd) or {}
        if last.get("mergeable") not in (None, "", "UNKNOWN"):
            return last
        if attempt < GH_RETRY_ATTEMPTS - 1:
            sleep_for_retry(attempt)
    return last


def gh_pr_checks(repo: str, number: int) -> list[dict[str, Any]] | None:
    # `gh pr checks` exit codes that still produce valid JSON output:
    #   0  all checks passed
    #   1  at least one check failed   (JSON still emitted)
    #   2  at least one check pending  (JSON still emitted)
    #   8  no checks configured for the PR (empty stdout)
    # Other non-zero exits go through `run_gh`'s retry logic; persistent
    # failures surface here as None so the dashboard shows unknown CI
    # instead of green.
    try:
        stdout = run_gh(
            [
                "gh", "pr", "checks", str(number), "--repo", repo, "--json",
                "name,state,bucket,workflow,description,link",
            ],
            allowed_exit_codes={0, 1, 2, 8},
        )
    except (RuntimeError, TransientGhError):
        return None
    if not stdout.strip():
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def list_open_prs(repo: str) -> list[dict[str, Any]]:
    return run_gh_json([
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "500",
        "--json", "number,title,author,isDraft,updatedAt,url",
    ])


def detect_repo() -> str:
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip()


def load_reviewer_set(org: str) -> set[str]:
    token = os.environ.get("OTELBOT_TOKEN") or None
    reviewers: set[str] = set()
    for slug in APPROVER_TEAM_SLUGS:
        members = gh_api(
            f"/orgs/{org}/teams/{slug}/members?per_page=100",
            paginate=True,
            token=token,
        )
        reviewers.update(m["login"] for m in members)
    if not reviewers:
        raise RuntimeError(
            f"no reviewers found in teams {APPROVER_TEAM_SLUGS}; "
            f"the token must have org:read permission"
        )
    return {r.lower() for r in reviewers}


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
    repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
            reviewThreads(first: 100, after: $after) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                nodes {
                    id
                    isResolved
                    isOutdated
                    path
                    line
                    comments(first: 100) {
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                        nodes {
                            id
                            body
                            createdAt
                            author {
                                login
                            }
                        }
                    }
                }
            }
        }
    }
}
"""

REVIEW_THREAD_COMMENTS_QUERY = """
query($thread_id: ID!, $after: String) {
    node(id: $thread_id) {
        ... on PullRequestReviewThread {
            comments(first: 100, after: $after) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                nodes {
                    id
                    body
                    createdAt
                    author {
                        login
                    }
                }
            }
        }
    }
}
"""


def fetch_remaining_review_thread_comments(thread_id: str, after: str | None) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    while after:
        data = gh_graphql(
            REVIEW_THREAD_COMMENTS_QUERY,
            {"thread_id": thread_id, "after": after},
        )
        connection = (((data.get("data") or {}).get("node") or {}).get("comments") or {})
        comments.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor") or ""
    return comments


def fetch_review_threads(owner: str, repo_name: str, number: int) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = gh_graphql(
            REVIEW_THREADS_QUERY,
            {"owner": owner, "name": repo_name, "number": number, "after": after},
        )
        page = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
        for thread in page.get("nodes") or []:
            comments = thread.get("comments") or {}
            page_info = comments.get("pageInfo") or {}
            if page_info.get("hasNextPage"):
                nodes = list(comments.get("nodes") or [])
                nodes.extend(fetch_remaining_review_thread_comments(
                    thread.get("id") or "",
                    page_info.get("endCursor") or "",
                ))
                comments["nodes"] = nodes
                comments["pageInfo"] = {"hasNextPage": False, "endCursor": ""}
                thread["comments"] = comments
            threads.append(thread)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return threads
        after = page_info.get("endCursor") or ""


# ---------------------------------------------------------------- model helpers


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_since(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))


def activity_age(ts: datetime | None) -> str:
    seconds = seconds_since(ts)
    if seconds is None:
        return "?"
    minutes = seconds // 60
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def truncate(s: str, n: int = DEFAULT_TRUNCATE_CHARS) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + " ...[truncated]"


def actor_login(obj: dict[str, Any] | None) -> str:
    return ((obj or {}).get("login") or "").strip()


def role_for(login: str, author: str, reviewers: set[str]) -> str:
    if not login:
        return "outsider"
    low = login.lower()
    if low == author.lower():
        return "author"
    if low in reviewers:
        return "approver"
    if low.startswith("app/") or low.endswith("[bot]"):
        return "bot"
    return "outsider"


# Bot logins appear in two shapes depending on the API:
#   - `gh pr view`'s `author` field uses the `app/<slug>` form (e.g.
#     `app/copilot-swe-agent`), which is what `_DELEGATING_BOT_AUTHORS`
#     matches against in `effective_author`.
#   - The Pulls/commits endpoint's `committer.login` field returns the bare
#     slug (e.g. `copilot`), which is what `_BOT_COMMITTER_LOGINS` matches
#     against in `detect_human_delegator`.
# Both sets contain `copilot` because the slug shows up in both contexts.
_BOT_COMMITTER_LOGINS = {"copilot"}
_DELEGATING_BOT_AUTHORS = {"app/copilot-swe-agent", "copilot"}


def is_bot_login(login: str) -> bool:
    if not login:
        return True
    low = login.lower()
    if low in _BOT_COMMITTER_LOGINS:
        return True
    return low.startswith("app/") or low.endswith("[bot]")


def detect_human_delegator(commits: list[dict[str, Any]]) -> str:
    if not commits:
        return ""
    login = actor_login(commits[0].get("committer") or {})
    return "" if is_bot_login(login) else login


def fetch_pr_raw(
    repo: str,
    owner: str,
    repo_name: str,
    pr_summary: dict[str, Any],
) -> dict[str, Any]:
    number = pr_summary["number"]
    with ThreadPoolExecutor() as pool:
        f_pr = pool.submit(gh_pr_view, repo, number)
        f_issue = pool.submit(
            gh_api,
            f"/repos/{owner}/{repo_name}/issues/{number}/comments?per_page=100",
            True,
        )
        f_revcom = pool.submit(
            gh_api,
            f"/repos/{owner}/{repo_name}/pulls/{number}/comments?per_page=100",
            True,
        )
        f_reviews = pool.submit(
            gh_api,
            f"/repos/{owner}/{repo_name}/pulls/{number}/reviews?per_page=100",
            True,
        )
        f_commits = pool.submit(
            gh_api,
            f"/repos/{owner}/{repo_name}/pulls/{number}/commits?per_page=100",
            True,
        )
        f_checks = pool.submit(gh_pr_checks, repo, number)
        f_threads = pool.submit(fetch_review_threads, owner, repo_name, number)
        return {
            "summary": pr_summary,
            "pr": f_pr.result(),
            "issue_comments": f_issue.result() or [],
            "review_comments": f_revcom.result() or [],
            "reviews": f_reviews.result() or [],
            "commits": f_commits.result() or [],
            "checks": f_checks.result(),
            "review_threads": f_threads.result() or [],
        }


def effective_author(raw: dict[str, Any]) -> str:
    pr = raw["pr"]
    summary = raw["summary"]
    author = actor_login(pr.get("author") or {}) or actor_login(summary.get("author") or {})
    if author.lower() in _DELEGATING_BOT_AUTHORS:
        delegator = detect_human_delegator(raw["commits"])
        if delegator:
            return delegator
    return author


def is_merge_commit(commit: dict[str, Any]) -> bool:
    return len(commit.get("parents") or []) >= 2


def normalize_events(raw: dict[str, Any], author: str, reviewers: set[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for c in raw["commits"]:
        commit_obj = c.get("commit") or {}
        commit_author = commit_obj.get("author") or {}
        login = actor_login(c.get("author") or {}) or commit_author.get("name") or ""
        sha = c.get("sha") or ""
        events.append({
            "kind": "commit",
            "timestamp": commit_author.get("date") or "",
            "actor": login,
            "actor_role": role_for(login, author, reviewers),
            "body": commit_obj.get("message") or "",
            "state": None,
            "path": None,
            "sha": sha[:7],
            "is_merge_from_base_by_non_author": is_merge_commit(c) and login.lower() != author.lower(),
        })
    for c in raw["issue_comments"]:
        login = actor_login(c.get("user") or {})
        events.append({
            "kind": "issue-comment",
            "timestamp": c.get("created_at") or "",
            "actor": login,
            "actor_role": role_for(login, author, reviewers),
            "body": c.get("body") or "",
            "state": None,
            "path": None,
            "sha": None,
            "is_merge_from_base_by_non_author": False,
        })
    for c in raw["review_comments"]:
        login = actor_login(c.get("user") or {})
        events.append({
            "kind": "review-comment",
            "timestamp": c.get("created_at") or "",
            "actor": login,
            "actor_role": role_for(login, author, reviewers),
            "body": c.get("body") or "",
            "state": None,
            "path": c.get("path"),
            "sha": None,
            "is_merge_from_base_by_non_author": False,
        })
    for r in raw["reviews"]:
        login = actor_login(r.get("user") or {})
        state = r.get("state") or ""
        events.append({
            "kind": "review-state",
            "timestamp": r.get("submitted_at") or "",
            "actor": login,
            "actor_role": role_for(login, author, reviewers),
            "body": r.get("body") or "",
            "state": state,
            "path": None,
            "sha": None,
            "is_merge_from_base_by_non_author": False,
        })
    events = [e for e in events if e["timestamp"]]
    events.sort(key=lambda e: e["timestamp"])
    return events


def is_substantive_activity(event: dict[str, Any]) -> bool:
    if event.get("is_merge_from_base_by_non_author"):
        return False
    # Bot events never count as substantive: merge-bot pings, CI status
    # comments, and the like must not refresh the waiting clock. Bot PR
    # authors are remapped to their human delegator in `effective_author`,
    # so a real human's activity still shows up here under that login.
    if event.get("actor_role") == "bot":
        return False
    if event["kind"] == "review-state" and event.get("state") != "COMMENTED":
        return True
    return bool((event.get("body") or "").strip())


def compute_conflicts(pr: dict[str, Any]) -> str:
    merge_state = pr.get("mergeStateStatus")
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return "yes"
    if mergeable in (None, "", "UNKNOWN"):
        return "unknown"
    return "no"


def latest_substantive_activity(events: list[dict[str, Any]], actor_roles: set[str]) -> datetime | None:
    timestamps = [
        parse_ts(e["timestamp"])
        for e in events
        if e.get("actor_role") in actor_roles and is_substantive_activity(e)
    ]
    timestamps = [ts for ts in timestamps if ts is not None]
    return max(timestamps) if timestamps else None


def format_ts(ts: datetime | None) -> str:
    return ts.isoformat() if ts else ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def empty_state() -> dict[str, Any]:
    return {"version": 1, "prs": {}, "_loaded_from_dashboard": False}


def _load_state_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    # A corrupt state file must not break the dashboard run. Log and start
    # fresh; the next save replaces the bad file.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"warning: ignoring unreadable state file {path}: {e!r}",
            file=sys.stderr,
        )
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    if not isinstance(data.get("prs"), dict):
        data["prs"] = {}
    data["version"] = 1
    data["_loaded_from_dashboard"] = True
    return data


def _save_state_file(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {k: v for k, v in state.items() if not k.startswith("_")}
    stored.setdefault("version", 1)
    stored.setdefault("prs", {})
    path.write_text(
        json.dumps(stored, sort_keys=True, indent=2), encoding="utf-8"
    )


def load_dashboard_state_cache() -> dict[str, Any]:
    return _load_state_file(dashboard_state_path())


def save_dashboard_state_cache(state: dict[str, Any]) -> None:
    _save_state_file(dashboard_state_path(), state)


def load_notification_state_file() -> dict[str, Any]:
    return _load_state_file(notification_state_path())


def save_notification_state_file(state: dict[str, Any]) -> None:
    _save_state_file(notification_state_path(), state)


def union_merge_notification_state(
    base: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    """Union-merge `overlay`'s per-PR entries into `base`.

    For each PR, the entry with the newer `last_notified_at` wins.
    Used by the workflow's CAS retry loop: an earlier attempt's
    just-sent notification state is carried into the next attempt so
    the cadence gate sees those pings as already-notified after a
    reset to the remote tip.
    """
    base_prs = dict(base.get("prs") or {})
    for pr_key, overlay_entry in (overlay.get("prs") or {}).items():
        base_entry = base_prs.get(pr_key)
        if base_entry is None:
            base_prs[pr_key] = overlay_entry
            continue
        overlay_ts = (overlay_entry or {}).get("last_notified_at") or ""
        base_ts = base_entry.get("last_notified_at") or ""
        if overlay_ts > base_ts:
            base_prs[pr_key] = overlay_entry
    merged = dict(base)
    merged["prs"] = base_prs
    return merged


def stored_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr_number": result.get("pr_number"),
        "pr_url": result.get("pr_url") or "",
        "failed": bool(result.get("failed")),
        "route": result.get("route") or "unknown",
        "facts": result.get("facts") or {},
    }


def results_from_dashboard_state(state: dict[str, Any], open_pr_numbers: set[int]) -> dict[int, dict[str, Any]]:
    results: dict[int, dict[str, Any]] = {}
    for key, value in (state.get("prs") or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            number = int(key)
        except ValueError:
            continue
        if number in open_pr_numbers:
            results[number] = value
    return results


def dashboard_state_from_results(results: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "prs": {str(number): stored_result(result) for number, result in sorted(results.items())},
        "_loaded_from_dashboard": True,
    }


def update_dashboard_state_for_pr(
    state: dict[str, Any],
    number: int,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    prs = dict(state.get("prs") or {})
    key = str(number)
    if result is None:
        prs.pop(key, None)
    else:
        prs[key] = stored_result(result)
    return {
        "version": 1,
        "prs": prs,
        "_loaded_from_dashboard": bool(state.get("_loaded_from_dashboard")),
    }


def compute_facts(raw: dict[str, Any], author: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    pr = raw["pr"]
    checks = raw["checks"]
    failing = [c for c in checks or [] if (c.get("state") or "").upper() in ("FAILURE", "ERROR")]
    pending = [c for c in checks or [] if (c.get("state") or "").upper() in ("PENDING", "QUEUED", "IN_PROGRESS")]
    last_activity_ts = parse_ts(pr["updatedAt"])
    created_ts = parse_ts(pr["createdAt"])
    author_activity_ts = latest_substantive_activity(events, {"author"})
    approver_activity_ts = latest_substantive_activity(events, {"approver"})
    external_activity_ts = latest_substantive_activity(events, {"outsider"})
    api_author = actor_login(pr.get("author") or {})
    assignees = [actor_login(a) for a in (pr.get("assignees") or [])]
    assignees = [a for a in assignees if a]
    facts = {
        "author": author,
        "assignees": assignees,
        "is_otelbot_author": api_author.lower() == "app/otelbot",
        "is_draft": bool(pr.get("isDraft")),
        "approved": pr.get("reviewDecision") == "APPROVED",
        "conflicts": compute_conflicts(pr),
        "created_at": format_ts(created_ts),
        "last_activity_at": format_ts(last_activity_ts),
        "last_author_activity_at": format_ts(author_activity_ts),
        "last_approver_activity_at": format_ts(approver_activity_ts),
        "last_external_activity_at": format_ts(external_activity_ts),
    }
    if checks is not None:
        facts["ci_failing_count"] = len(failing)
        facts["ci_pending_count"] = len(pending)
    return facts


def thread_comment(timestamp: str, actor: str, author: str, reviewers: set[str], body: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "actor": actor,
        "actor_role": role_for(actor, author, reviewers),
        "body": truncate(body),
    }


def add_thread_facts(
    thread: dict[str, Any],
    comments: list[dict[str, Any]],
    facts: dict[str, Any],
) -> dict[str, Any]:
    thread["thread_facts"] = {
        "latest_comment_role": comments[-1].get("actor_role"),
        "current_conflicts": facts.get("conflicts"),
    }
    return thread


def group_review_threads(
    raw: dict[str, Any],
    author: str,
    reviewers: set[str],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for thread in raw["review_threads"]:
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        comments = []
        for c in ((thread.get("comments") or {}).get("nodes") or []):
            actor = actor_login(c.get("author") or {})
            comments.append(thread_comment(c.get("createdAt") or "", actor, author, reviewers, c.get("body") or ""))
        comments = [c for c in comments if c["timestamp"]]
        comments.sort(key=lambda c: c["timestamp"])
        if not comments:
            continue
        threads.append(add_thread_facts({
            "thread_id": thread.get("id") or f"review-thread-{len(threads) + 1}",
            "thread_kind": "review-comment-thread",
            "path": thread.get("path"),
            "line": thread.get("line"),
            "resolved": False,
            "comments": comments,
        }, comments, facts))
    threads.sort(key=lambda t: t["comments"][-1]["timestamp"])
    return threads


def latest_approver_review_event(events: list[dict[str, Any]]) -> str | None:
    timestamps = [
        e["timestamp"]
        for e in events
        if e.get("actor_role") == "approver"
        and e["kind"] in ("review-comment", "review-state")
        and is_substantive_activity(e)
    ]
    return max(timestamps) if timestamps else None


def group_pr_conversation(
    raw: dict[str, Any],
    events: list[dict[str, Any]],
    review_threads: list[dict[str, Any]],
    author: str,
    reviewers: set[str],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    comments = []
    for c in raw["issue_comments"]:
        actor = actor_login(c.get("user") or {})
        comment = thread_comment(c.get("created_at") or "", actor, author, reviewers, c.get("body") or "")
        if comment["timestamp"] and comment["actor_role"] != "bot" and comment["body"]:
            comments.append(comment)
    comments.sort(key=lambda c: c["timestamp"])
    if not comments:
        return []

    latest_review_ts = latest_approver_review_event(events)
    if latest_review_ts:
        selected = [c for c in comments if c["timestamp"] > latest_review_ts]
        if not selected and review_threads:
            return []
    elif review_threads:
        selected = []
    else:
        selected = comments

    if facts.get("conflicts") == "no":
        selected = [c for c in selected if not is_conflict_resolution_comment(c.get("body") or "")]
    selected = selected[-THREAD_RECENT_COMMENTS_LIMIT:]
    if not selected:
        return []
    return [add_thread_facts({
        "thread_id": "pr-conversation",
        "thread_kind": "pr-conversation",
        "path": None,
        "line": None,
        "resolved": False,
        "comments": selected,
    }, selected, facts)]


def group_discussion_threads(
    raw: dict[str, Any],
    events: list[dict[str, Any]],
    author: str,
    reviewers: set[str],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    review_threads = group_review_threads(raw, author, reviewers, facts)
    return review_threads + group_pr_conversation(raw, events, review_threads, author, reviewers, facts)


# ---------------------------------------------------------------- LLM call


def parse_copilot_jsonl(s: str) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    usage: dict[str, Any] = {}
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "assistant.message":
            content = (evt.get("data") or {}).get("content")
            if isinstance(content, str):
                parts.append(content)
        elif evt.get("type") == "result":
            usage_obj = evt.get("usage") or {}
            if isinstance(usage_obj.get("premiumRequests"), int):
                usage["premium_requests"] = usage_obj["premiumRequests"]
    return "\n".join(parts), usage


def extract_json_object(s: str) -> dict[str, Any] | None:
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    i = 0
    while i < len(s):
        j = s.find("{", i)
        if j == -1:
            break
        try:
            obj, end = decoder.raw_decode(s, j)
        except json.JSONDecodeError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = end
    return objects[-1] if objects else None


def normalize_thread_action(action: str) -> str:
    action = (action or "").lower().strip()
    if action in ("author", "reviewer", "external", "none", "unclear"):
        return action
    if action == "approver":
        return "reviewer"
    return "unclear"


def parse_thread_decision(response_text: str) -> dict[str, str]:
    obj = extract_json_object(response_text) if response_text else None
    if not obj:
        return {"thread_action": "unclear", "reason": "LLM did not return valid JSON"}
    action = normalize_thread_action(str(obj.get("thread_action") or obj.get("route") or ""))
    reason = truncate(str(obj.get("reason") or ""), 300)
    if not reason:
        reason = "No reason provided"
    return {"thread_action": action, "reason": reason}


def is_conflict_resolution_comment(body: str) -> bool:
    # Heuristic used by `group_pr_conversation` to drop stale "please resolve
    # the conflicts" pings from the LLM prompt once conflicts are actually
    # gone (gated on `facts["conflicts"] == "no"` at the call site). The
    # match is intentionally broad — a few false positives are fine because
    # the gate already ensures the underlying issue is resolved.
    text = (body or "").lower()
    return "conflict" in text and any(word in text for word in ("resolve", "resolved", "merge"))


def thread_prompt(repo: str, number: int, pr: dict[str, Any], facts: dict[str, Any], thread: dict[str, Any]) -> str:
    pr_facts = {
        "number": number,
        "title": pr.get("title") or "",
        "description": truncate(pr.get("body") or "", 800),
        **facts,
    }
    facts_text = json.dumps(pr_facts, indent=2, sort_keys=True)
    thread_text = json.dumps(thread, indent=2, sort_keys=True)
    prompt = THREAD_PROMPT_TEMPLATE.format(repo=repo, number=number, facts=facts_text, thread=thread_text)
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    trimmed = dict(thread)
    comments = [dict(c) for c in thread.get("comments") or []]
    for c in comments:
        c["body"] = truncate(c.get("body") or "", THREAD_COMMENT_BODY_MAX_CHARS)
    trimmed["comments"] = comments[-THREAD_RECENT_COMMENTS_LIMIT:]
    thread_text = json.dumps(trimmed, indent=2, sort_keys=True)
    return THREAD_PROMPT_TEMPLATE.format(repo=repo, number=number, facts=facts_text, thread=thread_text)


def run_llm_for_thread(
    repo: str,
    number: int,
    pr: dict[str, Any],
    facts: dict[str, Any],
    thread: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    prompt = thread_prompt(repo, number, pr, facts, thread)
    proc = subprocess.run(
        ["copilot", "-p", prompt, "--output-format", "json", "--model", model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=LLM_THREAD_TIMEOUT_SECONDS,
    )
    response_text, usage = parse_copilot_jsonl(proc.stdout)
    decision = parse_thread_decision(response_text)
    return {
        "thread_id": thread["thread_id"],
        "thread_kind": thread["thread_kind"],
        "failed": proc.returncode != 0,
        "decision": decision,
        "usage": usage,
        "error": proc.stderr[-2000:] if proc.stderr else "",
        "response_text": response_text,
    }


def thread_cache_key(thread: dict[str, Any]) -> str:
    payload = json.dumps(thread, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_classification_cache(pr_number: int) -> dict[str, dict[str, Any]]:
    path = CLASSIFICATION_CACHE_DIR / f"{pr_number}.json"
    if not path.exists():
        return {}
    # A corrupt cache file must not break the dashboard run. Log and start
    # fresh; the next save replaces the bad file.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  warning: ignoring unreadable classification cache {path}: {e!r}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_classification_cache(pr_number: int, cache: dict[str, dict[str, Any]]) -> None:
    CLASSIFICATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CLASSIFICATION_CACHE_DIR / f"{pr_number}.json"
    path.write_text(json.dumps(cache, sort_keys=True, indent=2), encoding="utf-8")


def prune_classification_cache(open_pr_numbers: set[int]) -> None:
    # Per-event runs only touch one PR, so they can't safely prune. The
    # full-rebuild path knows the complete open-PR set and is the only
    # place where files for closed/merged PRs can be deleted.
    if not CLASSIFICATION_CACHE_DIR.exists():
        return
    for path in CLASSIFICATION_CACHE_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue
        if int(path.stem) not in open_pr_numbers:
            path.unlink()


def classify_threads(
    repo: str,
    number: int,
    pr: dict[str, Any],
    facts: dict[str, Any],
    threads: list[dict[str, Any]],
    model: str,
) -> list[dict[str, Any]]:
    cache_in = load_classification_cache(number)
    # Only keys for threads we actually saw this run end up in cache_out,
    # so entries for removed/renamed threads are pruned automatically.
    cache_out: dict[str, dict[str, Any]] = {}
    classifications: list[dict[str, Any]] = []
    for thread in threads:
        key = thread_cache_key(thread)
        cached = cache_in.get(key)
        if cached:
            record = dict(cached)
            # thread_id/thread_kind belong to the current thread instance,
            # not the cached classification.
            record["thread_id"] = thread["thread_id"]
            record["thread_kind"] = thread["thread_kind"]
            classifications.append(record)
            cache_out[key] = cached
            continue
        try:
            record = run_llm_for_thread(repo, number, pr, facts, thread, model)
        except subprocess.TimeoutExpired:
            record = {
                "thread_id": thread["thread_id"],
                "thread_kind": thread["thread_kind"],
                "failed": True,
                "decision": {"thread_action": "unclear", "reason": "LLM timeout"},
                "error": "timeout",
            }
        except Exception as e:
            # Boundary: one bad thread must not break the PR. Log the
            # traceback so genuine bugs are visible in workflow logs.
            print(
                f"  warning: thread {thread['thread_id']} on PR #{number} failed to classify:",
                file=sys.stderr,
            )
            traceback.print_exc()
            record = {
                "thread_id": thread["thread_id"],
                "thread_kind": thread["thread_kind"],
                "failed": True,
                "decision": {"thread_action": "unclear", "reason": f"LLM failed: {e!r}"},
                "error": repr(e),
            }
        classifications.append(record)
        # Only cache clean results so transient LLM failures don't get sticky.
        if not record.get("failed"):
            cache_out[key] = record
    save_classification_cache(number, cache_out)
    return classifications


# ---------------------------------------------------------------- routing and rendering


ROUTE_LABELS = {
    "maintainer": "Waiting on maintainers",
    "approver": "Waiting on approvers",
    "author": "Waiting on authors",
    "external": "Waiting on external",
    "transient-failure": "Transient GitHub failure retrieving PR data",
    "unknown": "Unknown",
}
ROUTE_ORDER = ["maintainer", "approver", "author", "external", "transient-failure", "unknown"]
ROUTE_THREAD_ACTIONS = {
    "author": "author",
    "approver": "reviewer",
    "maintainer": "reviewer",
    "external": "external",
}


def action_counts(classifications: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"author": 0, "reviewer": 0, "external": 0, "none": 0, "unclear": 0}
    for c in classifications:
        action = normalize_thread_action((c.get("decision") or {}).get("thread_action") or "")
        counts[action] += 1
    return counts


def route_pr(facts: dict[str, Any], classifications: list[dict[str, Any]]) -> str:
    counts = action_counts(classifications)
    # Precedence:
    #   1. otelbot PRs are always either "external" (if any thread points
    #      outside the repo) or "approver" — they have no human author to
    #      route to.
    #   2. Any single thread waiting on the author -> "author".
    #   3. Otherwise any thread waiting on something external -> "external".
    #   4. Otherwise the PR's approval status decides: approved -> ready for
    #      a maintainer to merge; not approved -> still waiting on approvers
    #      (whether or not a thread is currently pending on a reviewer).
    if facts.get("is_otelbot_author"):
        return "external" if counts["external"] else "approver"
    if counts["author"]:
        return "author"
    if counts["external"]:
        return "external"
    if facts.get("approved"):
        return "maintainer"
    return "approver"


def threads_by_id(threads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {t["thread_id"]: t for t in threads}


def thread_latest_comment_ts(thread: dict[str, Any] | None) -> datetime | None:
    comments = (thread or {}).get("comments") or []
    if not comments:
        return None
    return parse_ts(comments[-1].get("timestamp") or "")


def oldest_thread_wait_ts(
    threads: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    action: str,
) -> datetime | None:
    by_id = threads_by_id(threads)
    timestamps = [
        thread_latest_comment_ts(by_id.get(c.get("thread_id") or ""))
        for c in classifications
        if normalize_thread_action((c.get("decision") or {}).get("thread_action") or "") == action
    ]
    timestamps = [ts for ts in timestamps if ts is not None]
    return min(timestamps) if timestamps else None


def fallback_wait_ts(route: str, facts: dict[str, Any]) -> tuple[datetime | None, str]:
    if route in ("approver", "maintainer"):
        return parse_ts(facts.get("last_author_activity_at") or ""), "last_author_activity"
    if route == "author":
        return parse_ts(facts.get("last_approver_activity_at") or ""), "last_approver_activity"
    if route == "external":
        return parse_ts(facts.get("last_external_activity_at") or ""), "last_external_activity"
    return parse_ts(facts.get("last_activity_at") or ""), "last_activity"


def add_wait_age_facts(
    facts: dict[str, Any],
    route: str,
    threads: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
) -> None:
    action = ROUTE_THREAD_ACTIONS.get(route)
    wait_ts = oldest_thread_wait_ts(threads, classifications, action) if action else None
    basis = "oldest_pending_thread" if wait_ts else ""
    if wait_ts is None:
        wait_ts, basis = fallback_wait_ts(route, facts)
    if wait_ts is None:
        wait_ts = parse_ts(facts.get("created_at") or "")
        basis = "created"
    facts["waiting_since"] = format_ts(wait_ts)
    facts["waiting_age_basis"] = basis


def load_slack_user_map() -> dict[str, str]:
    raw = os.environ.get("SLACK_USER_MAP_JSON") or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SLACK_USER_MAP_JSON must be valid JSON: {e.msg} at char {e.pos}") from e
    if not isinstance(data, dict):
        raise RuntimeError("SLACK_USER_MAP_JSON must be a JSON object mapping GitHub logins to Slack user IDs")
    return {str(k).lower(): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}


def slack_webhook_retry_delay(attempt: int, e: urllib.error.HTTPError | None = None) -> float:
    if e is not None:
        retry_after = e.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
    return min(SLACK_WEBHOOK_RETRY_DELAY_SECONDS * (2**attempt), 30.0)


def should_retry_slack_http_error(e: urllib.error.HTTPError) -> bool:
    return e.code == 429 or 500 <= e.code < 600


def post_slack_webhook(message: str, webhook_url: str) -> None:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": message, "unfurl_links": False}).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    for attempt in range(SLACK_WEBHOOK_RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if attempt + 1 < SLACK_WEBHOOK_RETRY_ATTEMPTS and should_retry_slack_http_error(e):
                time.sleep(slack_webhook_retry_delay(attempt, e))
                continue
            raise RuntimeError(f"Slack webhook request failed with HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            if attempt + 1 < SLACK_WEBHOOK_RETRY_ATTEMPTS:
                time.sleep(slack_webhook_retry_delay(attempt))
                continue
            raise RuntimeError(f"Slack webhook request failed: {e}") from e
        if body.strip().lower() != "ok":
            raise RuntimeError(f"Slack webhook request failed: {body}")
        return


def slack_message(repo: str, result: dict[str, Any], assignee_mention: str, kind: str) -> str:
    facts = result.get("facts") or {}
    number = result.get("pr_number")
    url = result.get("pr_url") or f"https://github.com/{repo}/pull/{number}"
    if kind == "follow-up":
        waiting_age = activity_age(parse_ts(facts.get("waiting_since") or ""))
        lead = f"is waiting on approvers for {waiting_age}"
    else:
        lead = "moved to waiting on approvers"
    return f"{assignee_mention} <{url}|PR #{number}> {lead}"


def pending_notification_kind(
    previous_state_exists: bool,
    previous_pr_state: dict[str, Any],
    current_waiting_since: datetime | None,
    now: datetime,
) -> str | None:
    if not previous_state_exists:
        # Bootstrap or missing/corrupt state: observe once without Slack so
        # first deployment doesn't blast old waits. If a PR is still waiting
        # on the next stateful run, it may receive an initial ping then.
        return None
    if current_waiting_since is None:
        return None
    last_notified = parse_ts(previous_pr_state.get("last_notified_at") or "")
    # `last_notified_at` is only set after a Slack send actually
    # succeeds. If it is missing, either this PR has never been pinged
    # or the previous attempt failed; in both cases fire "initial" and
    # let the next cron tick retry on persistent failure. The webhook
    # client already retries transient errors with exponential backoff
    # within a single tick.
    if last_notified is None:
        return "initial"
    if current_waiting_since > last_notified:
        return "initial"
    if now.weekday() < 5 and (now - last_notified).total_seconds() >= APPROVER_FOLLOW_UP_SECONDS:
        return "follow-up"
    return None


def send_slack_notification(
    repo: str,
    result: dict[str, Any],
    assignee: str,
    kind: str,
    webhook_url: str,
    slack_user_id: str | None,
) -> str | None:
    number = result.get("pr_number")
    if not webhook_url:
        return "SLACK_WEBHOOK_URL is not set"
    try:
        post_slack_webhook(slack_message(repo, result, f"<@{slack_user_id}>", kind), webhook_url)
    except Exception as e:
        return f"PR #{number}: failed to notify @{assignee}: {e}"
    print(f"  mentioned @{assignee} on Slack for PR #{number} ({kind})", file=sys.stderr)
    return None


def migrated_pr_notification_state(state: dict[str, Any]) -> dict[str, Any]:
    # Older runs stored `assignee_notifications: {<login>: {last_notified_at}}`
    # under each PR. Lift the most recent timestamp to PR level so the
    # follow-up cadence survives the first run after deploy instead of
    # blasting every waiting PR with a fresh initial ping.
    if state.get("last_notified_at") or not state.get("assignee_notifications"):
        return state
    timestamps = [
        a.get("last_notified_at")
        for a in state["assignee_notifications"].values()
        if isinstance(a, dict) and a.get("last_notified_at")
    ]
    if not timestamps:
        return state
    return {
        "last_notified_at": max(timestamps),
        "last_notification_kind": "initial",
    }


def next_notification_state(
    repo: str,
    results: dict[int, dict[str, Any]],
    previous_state: dict[str, Any],
    dry_run: bool,
    now: datetime,
    notification_numbers: set[int] | None = None,
) -> dict[str, Any]:
    previous_prs = previous_state.get("prs") or {}
    previous_state_exists = bool(previous_state.get("_loaded_from_dashboard"))
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL") or ""
    slack_user_map = {} if dry_run else load_slack_user_map()

    new_prs: dict[str, Any] = {}
    for number, result in sorted(results.items()):
        pr_key = str(number)
        previous_pr_state = migrated_pr_notification_state(previous_prs.get(pr_key) or {})

        # Scoped run: preserve unrelated PRs' prior state and move on.
        if notification_numbers is not None and number not in notification_numbers:
            if previous_pr_state:
                new_prs[pr_key] = previous_pr_state
            continue

        route = result.get("route") or "unknown"
        # On failure/unknown, preserve prior state without recomputing.
        if result.get("failed") or route in ("transient-failure", "unknown"):
            if previous_pr_state:
                new_prs[pr_key] = previous_pr_state
            continue

        # Only PRs waiting on approvers trigger notifications.
        if route != "approver":
            continue

        facts = result.get("facts") or {}

        # Slack mapping is optional per assignee: an assignee with no
        # mapping is out of scope for Slack pings, not a missed
        # notification. Filter them out before deciding whether this PR
        # has any notification work at all.
        mapped_assignees = [
            (a, slack_user_map[a.lower()])
            for a in (facts.get("assignees") or [])
            if a.lower() in slack_user_map
        ]
        if not mapped_assignees:
            continue

        current_waiting_since = parse_ts(facts.get("waiting_since") or "")
        kind = pending_notification_kind(
            previous_state_exists, previous_pr_state, current_waiting_since, now,
        )

        new_pr_state: dict[str, Any] = {
            "last_notified_at": previous_pr_state.get("last_notified_at") or "",
            "last_notification_kind": previous_pr_state.get("last_notification_kind") or "",
        }

        if kind and not dry_run:
            sent_any = False
            for assignee, slack_user_id in mapped_assignees:
                error = send_slack_notification(
                    repo, result, assignee, kind, webhook_url, slack_user_id,
                )
                if error:
                    print(f"  warning: {error}", file=sys.stderr)
                else:
                    sent_any = True
            # Bump the cadence as soon as at least one assignee was pinged.
            # If every assignee failed, leave `last_notified_at` alone so the
            # next cron tick retries.
            if sent_any:
                new_pr_state["last_notified_at"] = format_ts(now)
                new_pr_state["last_notification_kind"] = kind

        if new_pr_state["last_notified_at"]:
            new_prs[pr_key] = new_pr_state
    return {"version": 1, "prs": new_prs}


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def github_run_url(repo: str) -> str:
    server_url = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    repository = os.environ.get("GITHUB_REPOSITORY") or repo
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    if run_id:
        return f"{server_url}/{repository}/actions/runs/{run_id}"
    return f"https://github.com/{repo}/actions/workflows/pr-review-dashboard.yml"


def render_draft_pr_section(prs: list[dict[str, Any]]) -> list[str]:
    drafts = [p for p in prs if p.get("isDraft")]
    if not drafts:
        return []
    drafts.sort(key=lambda p: p.get("updatedAt") or "")
    lines = ["## Draft pull requests", ""]
    lines.append("| PR | Author | Updated |")
    lines.append("|---|---|:---:|")
    for pr in drafts:
        number = pr["number"]
        title = _md_escape(pr.get("title", ""))
        url = pr.get("url", "")
        author = actor_login(pr.get("author") or {})
        updated = activity_age(parse_ts(pr.get("updatedAt") or ""))
        lines.append(f"| [{title} (#{number})]({url}) | {author} | {updated} |")
    lines.append("")
    return lines


def ci_cell(facts: dict[str, Any]) -> str:
    if "ci_failing_count" not in facts and "ci_pending_count" not in facts:
        return "?"
    if facts.get("ci_failing_count", 0) > 0:
        return "❌"
    if facts.get("ci_pending_count", 0) > 0:
        return "⏳"
    return "✅"


def conflicts_cell(facts: dict[str, Any]) -> str:
    conflicts = facts.get("conflicts")
    if conflicts == "yes":
        return "❌"
    if conflicts == "no":
        return "✅"
    return "?"


def _age_ts(facts: dict[str, Any]) -> datetime | None:
    return parse_ts(facts.get("waiting_since") or facts.get("last_activity_at") or "")


def age_seconds(facts: dict[str, Any]) -> int | None:
    return seconds_since(_age_ts(facts))


def age_cell(facts: dict[str, Any]) -> str:
    return activity_age(_age_ts(facts))


def _neutralize_code_fence(s: str) -> str:
    # Insert a zero-width joiner between backticks so an LLM reason or error
    # string containing a literal ``` can't prematurely close the diagnostics
    # code fence. HTML escaping is not needed: data lines live inside a
    # Markdown code block where GitHub does not render HTML.
    return (s or "").replace("```", "`\u200d`\u200d`")


def render_diagnostics_section(results: dict[int, dict[str, Any]]) -> list[str]:
    data_lines: list[str] = []
    for number in sorted(results, reverse=True):
        result = results[number]
        classifications = result.get("classifications") or []
        error = result.get("error")
        if not classifications and not error:
            continue
        data_lines.append(f"PR #{number}")
        for c in classifications:
            decision = c.get("decision") or {}
            reason = (decision.get("reason") or "").replace("\n", " ")
            data_lines.append(f"llm: {c.get('thread_id')} -> {decision.get('thread_action')} ({reason})")
        if error:
            data_lines.append(f"error: {error}")
        data_lines.append("")
    return [
        "<details>",
        "<summary>Diagnostics</summary>",
        "",
        "```text",
        *(_neutralize_code_fence(line) for line in data_lines),
        "```",
        "",
        "</details>",
        "",
    ]


def render_pr_tables(
    prs: list[dict[str, Any]],
    results: dict[int, dict[str, Any]],
    repo: str,
) -> str:
    source_url = f"https://github.com/{repo}/blob/main/.github/scripts/pull-request-dashboard.py"
    refresh_url = f"https://github.com/{repo}/actions/workflows/pr-review-dashboard.yml"
    out: list[str] = [
        "> [!NOTE]",
        "> Open non-draft PRs grouped by who is expected to act next. Draft PRs are "
        "listed separately. The grouping is "
        f"partly performed by an LLM ([source]({source_url})) and could contain mistakes.",
        "",
    ]

    by_route: dict[str, list[dict[str, Any]]] = {}
    for pr in prs:
        if pr.get("isDraft"):
            continue
        res = results.get(pr["number"]) or {"route": "unknown"}
        by_route.setdefault(res.get("route") or "unknown", []).append(pr)

    def row_sort_key(pr: dict[str, Any]) -> tuple[int, int]:
        res = results.get(pr["number"]) or {}
        facts = res.get("facts") or {}
        activity = age_seconds(facts)
        return (activity if activity is not None else -1, pr["number"])

    for route in ROUTE_ORDER:
        rows = by_route.get(route) or []
        if not rows:
            continue
        rows.sort(key=row_sort_key, reverse=True)
        out.append(f"## {ROUTE_LABELS.get(route, route)}")
        out.append("")
        out.append("| PR | Author | Assignees | CI | Conflicts | Age |")
        out.append("|---|---|---|:---:|:---:|:---:|")
        for pr in rows:
            number = pr["number"]
            title = _md_escape(pr.get("title", ""))
            url = pr.get("url", "")
            res = results.get(number) or {}
            facts = res.get("facts") or {}
            author = facts.get("author") or actor_login(pr.get("author") or {})
            assignees = facts.get("assignees") or [
                actor_login(a) for a in (pr.get("assignees") or [])
            ]
            assignees_cell = _md_escape(", ".join(a for a in assignees if a))
            activity_cell = age_cell(facts)
            pr_cell = f"[{title} (#{number})]({url})"
            if facts.get("approved"):
                pr_cell += " ✅"
            out.append(
                f"| {pr_cell} | {author} | {assignees_cell} | {ci_cell(facts)} | "
                f"{conflicts_cell(facts)} | {activity_cell} |"
            )
        out.append("")

    out.extend(render_draft_pr_section(prs))
    out.extend(render_diagnostics_section(results))
    out.append(f"_Approvers may [force a refresh]({refresh_url})._")
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- main


def build_pr_result(
    repo: str,
    owner: str,
    repo_name: str,
    pr_summary: dict[str, Any],
    reviewers: set[str],
    model: str,
) -> dict[str, Any] | None:
    number = pr_summary["number"]
    try:
        raw = fetch_pr_raw(repo, owner, repo_name, pr_summary)
        if raw["pr"].get("state") != "OPEN" or raw["pr"].get("isDraft"):
            return None
        author = effective_author(raw)
        events = normalize_events(raw, author, reviewers)
        facts = compute_facts(raw, author, events)
        threads = group_discussion_threads(raw, events, author, reviewers, facts)
        classifications = classify_threads(repo, number, raw["pr"], facts, threads, model)
        route = route_pr(facts, classifications)
        add_wait_age_facts(facts, route, threads, classifications)
        return {
            "pr_number": number,
            "pr_title": raw["pr"].get("title") or "",
            "pr_url": raw["pr"].get("url") or "",
            "failed": False,
            "facts": facts,
            "threads": threads,
            "classifications": classifications,
            "route": route,
        }
    except TransientGhError as e:
        return {
            "pr_number": number,
            "failed": True,
            "facts": {},
            "threads": [],
            "classifications": [],
            "route": "transient-failure",
            "error": repr(e),
        }
    except Exception as e:
        # Boundary: one bad PR must not break the dashboard run. Log the
        # traceback so genuine bugs are visible in workflow logs instead
        # of being silently routed to "Unknown" forever.
        print(f"  warning: PR #{number} failed to build result:", file=sys.stderr)
        traceback.print_exc()
        return {
            "pr_number": number,
            "failed": True,
            "facts": {},
            "threads": [],
            "classifications": [],
            "route": "unknown",
            "error": repr(e),
        }


@dataclass
class DashboardCalculation:
    results: dict[int, dict[str, Any]]
    dashboard_state: dict[str, Any]
    trigger_pr_result: dict[str, Any] | None = None
    current_pr_result: dict[str, Any] | None = None
    used_cached_dashboard_state: bool = False


def compute_pr_results(
    repo: str,
    owner: str,
    repo_name: str,
    non_drafts: list[dict[str, Any]],
    open_pr_numbers: set[int],
    reviewers: set[str],
    pr_number: int | None,
    jobs: int,
    model: str,
) -> DashboardCalculation:
    dashboard_state = empty_state()
    if pr_number:
        dashboard_state = load_dashboard_state_cache()

    if pr_number and dashboard_state.get("_loaded_from_dashboard"):
        print(f"refreshing dashboard state for PR #{pr_number}", file=sys.stderr)
        results = results_from_dashboard_state(dashboard_state, open_pr_numbers)
        trigger_pr_result = build_pr_result(repo, owner, repo_name, {"number": pr_number}, reviewers, model)
        if trigger_pr_result is None:
            results.pop(pr_number, None)
        else:
            results[pr_number] = trigger_pr_result
        current_pr_result = stored_result(trigger_pr_result) if trigger_pr_result is not None else None
        return DashboardCalculation(
            results=results,
            dashboard_state=dashboard_state,
            trigger_pr_result=trigger_pr_result,
            current_pr_result=current_pr_result,
            used_cached_dashboard_state=True,
        )

    if pr_number:
        print("dashboard result state not found; rebuilding all PRs", file=sys.stderr)
    print(f"processing {len(non_drafts)} PR(s) in {repo} (model={model}, jobs={jobs})", file=sys.stderr)
    results = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(build_pr_result, repo, owner, repo_name, pr, reviewers, model): pr
            for pr in non_drafts
        }
        for i, fut in enumerate(as_completed(futures), 1):
            pr = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                # Boundary: `build_pr_result` already catches its own
                # exceptions, so this is a safety net for cancellations or
                # bugs that escape the inner handler. One bad future must
                # not break the whole dashboard run.
                res = {"pr_number": pr["number"], "failed": True, "route": "unknown", "error": repr(e)}
            if res is None:
                # PR was closed or converted to draft between list_open_prs
                # and the worker run; skip it.
                continue
            results[pr["number"]] = res
            counts = action_counts(res.get("classifications") or [])
            print(
                f"  [{i}/{len(non_drafts)}] #{pr['number']} -> {res.get('route', 'unknown')} "
                f"({', '.join(f'{k}={v}' for k, v in counts.items())})",
                file=sys.stderr,
            )

    dashboard_state = dashboard_state_from_results(results)
    trigger_pr_result = results.get(pr_number) if pr_number else None
    current_pr_result = stored_result(trigger_pr_result) if trigger_pr_result is not None else None
    return DashboardCalculation(
        results=results,
        dashboard_state=dashboard_state,
        trigger_pr_result=trigger_pr_result,
        current_pr_result=current_pr_result,
    )


def reconcile_with_latest_dashboard(
    calculation: DashboardCalculation,
    pr_number: int | None,
    open_pr_numbers: set[int],
) -> tuple[DashboardCalculation, bool]:
    if not pr_number or not calculation.used_cached_dashboard_state:
        return calculation, False

    if calculation.trigger_pr_result is None:
        # The trigger PR is a draft, closed, or was dropped between
        # list_open_prs and the worker run. We cannot tell from the cache
        # alone whether the "Draft pull requests" section needs an update
        # (newly opened drafts have no cache entry, so the equality check
        # below would spuriously return True), so always re-render in that
        # case.
        return calculation, False

    # Reload the cache so we pick up any concurrent writer's update of
    # other PR slots before we merge in our own.
    latest_dashboard_state = load_dashboard_state_cache()
    previous_pr_result = (latest_dashboard_state.get("prs") or {}).get(str(pr_number))
    dashboard_state = calculation.dashboard_state
    results = calculation.results

    if previous_pr_result == calculation.current_pr_result:
        if latest_dashboard_state.get("_loaded_from_dashboard"):
            dashboard_state = latest_dashboard_state
            results = results_from_dashboard_state(dashboard_state, open_pr_numbers)
        return replace(calculation, results=results, dashboard_state=dashboard_state), True

    if latest_dashboard_state.get("_loaded_from_dashboard"):
        dashboard_state = latest_dashboard_state
    dashboard_state = update_dashboard_state_for_pr(dashboard_state, pr_number, calculation.trigger_pr_result)
    results = results_from_dashboard_state(dashboard_state, open_pr_numbers)
    return replace(calculation, results=results, dashboard_state=dashboard_state), False


def render_dashboard_body(
    prs: list[dict[str, Any]],
    results: dict[int, dict[str, Any]],
    repo: str,
) -> str:
    return render_pr_tables(prs, results, repo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=f"Write rendered dashboard markdown to {DRY_RUN_OUTPUT} instead of updating the dashboard issue or Slack",
    )
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"parallel workers (default: {DEFAULT_JOBS})")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"copilot model (default: {DEFAULT_MODEL})")
    ap.add_argument("--pr-number", type=int, help="only refresh dashboard state for this PR")
    ap.add_argument(
        "--prior-notification-state",
        type=Path,
        help=(
            "path to a prior attempt's notification-state.json snapshot; "
            "union-merged into the loaded state so the cadence gate sees "
            "just-sent pings as already-notified after a CAS retry"
        ),
    )
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"directory holding state files (default: {DEFAULT_STATE_DIR})",
    )
    args = ap.parse_args()
    set_state_dir(args.state_dir)

    repo = detect_repo()
    owner, repo_name = repo.split("/", 1)

    prs = list_open_prs(repo)
    open_pr_numbers = {p["number"] for p in prs}
    if args.pr_number is None:
        prune_classification_cache(open_pr_numbers)
    drafts = [p for p in prs if p.get("isDraft")]
    non_drafts = [p for p in prs if not p.get("isDraft")]

    reviewers = load_reviewer_set(owner)

    calculation = compute_pr_results(
        repo,
        owner,
        repo_name,
        non_drafts,
        open_pr_numbers,
        reviewers,
        args.pr_number,
        args.jobs,
        args.model,
    )

    # Read the previous notification state from the state file on the
    # otelbot/pull-request-dashboard-state orphan branch.
    state_file_notification_state = load_notification_state_file()
    previous_state = state_file_notification_state
    if args.prior_notification_state and args.prior_notification_state.exists():
        # A prior attempt of this same workflow run already sent pings
        # and snapshotted its notification state here before the reset
        # to the remote tip. Union-merge it so we don't re-send.
        prior = _load_state_file(args.prior_notification_state)
        previous_state = union_merge_notification_state(previous_state, prior)
    notification_numbers = {args.pr_number} if args.pr_number else None
    notification_state = next_notification_state(
        repo,
        calculation.results,
        previous_state,
        args.dry_run,
        utc_now(),
        notification_numbers,
    )
    notification_state_changed = (notification_state.get("prs") or {}) != (
        state_file_notification_state.get("prs") or {}
    )

    calculation, dashboard_state_unchanged = reconcile_with_latest_dashboard(
        calculation,
        args.pr_number,
        open_pr_numbers,
    )

    md = render_dashboard_body(
        prs,
        calculation.results,
        repo,
    )
    output_path = Path(DRY_RUN_OUTPUT)
    output_path.write_text(md, encoding="utf-8")
    print(f"wrote dashboard markdown to {output_path.resolve()}", file=sys.stderr)

    if dashboard_state_unchanged and not notification_state_changed:
        if args.pr_number:
            print(f"PR #{args.pr_number} dashboard state unchanged", file=sys.stderr)
        else:
            print("dashboard state unchanged", file=sys.stderr)
        return 0

    if args.dry_run:
        return 0
    # Persist the new state to the on-disk state files. The workflow
    # commits + pushes these to the otelbot/pull-request-dashboard-state branch with
    # --force-with-lease after this script returns. The workflow publishes
    # the rendered dashboard issue only after that push succeeds.
    save_dashboard_state_cache(calculation.dashboard_state)
    save_notification_state_file(notification_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
