"""Alert when a self-hosted Renovate stops keeping up, instead of failing silently.

Every failure this catches produced no error anywhere: an update with no
release timestamp sat in Pending Status Checks for two months, the queue
filled branchConcurrentLimit and new updates were skipped with a debug-level
log line, and runs that never started left nothing to read.

Reads the Dependency Dashboard and the open PRs through the Forgejo/Gitea API,
and every Renovate run log in LOG_DIR (written with Renovate's LOG_FILE) since
the last check. Without FORGE_API it checks the run logs only, for a Renovate
on another platform (one instance per log prefix, with its own state file). Keeps first-seen dates, and how far the logs have been read, in
LOG_DIR/alert-state.json and notifies once per problem, as a single Apprise
digest. Never exits non-zero for a problem it
reports; a crash here is a bug in this program, not in Renovate. An
undelivered digest does exit non-zero, so a failed Job shows it.
"""

import datetime as dt
import glob
import gzip
import json
import os
import re
import sys
import urllib.request

from apprise_notify import notify

# FORGE_API is the Forgejo/Gitea API base, e.g. https://codeberg.org/api/v1
# (CODEBERG_API, its first name, still works). Unset: run logs only, no
# dashboard or PR checks (e.g. a Renovate run on GitLab).
API = os.environ.get("FORGE_API") or os.environ.get("CODEBERG_API", "")
REPO = os.environ.get("REPOSITORY", "")
DASHBOARD = os.environ.get("DASHBOARD_ISSUE", "")
TOKEN = os.environ.get("RENOVATE_TOKEN", "")
if API and not (REPO and DASHBOARD and TOKEN):
    sys.exit("FORGE_API needs REPOSITORY, DASHBOARD_ISSUE and RENOVATE_TOKEN.")
APPRISE_URL = os.environ["APPRISE_URL"]
# Names the Renovate in the digest title: the repository, or NAME in logs-only mode.
NAME = os.environ.get("NAME") or REPO or "Renovate"
# Web URL of the repository, for the dashboard link: the API URL without /api/v1.
WEB_URL = os.environ.get("WEB_URL") or API.removesuffix("/").removesuffix("/api/v1")
AUTOMERGE_LABEL = os.environ.get("AUTOMERGE_LABEL", "renovate-automerge")
LOG_DIR = os.environ.get("LOG_DIR", "/logs")
# Run logs are LOG_PREFIX*.log[.gz]. Two Renovates can share LOG_DIR with
# different prefixes; each check then needs its own STATE_FILE.
LOG_PREFIX = os.environ.get("LOG_PREFIX", "renovate-")
STATE = os.path.join(LOG_DIR, os.environ.get("STATE_FILE", "alert-state.json"))

# Days before something counts as stuck. Pending legitimately lasts as long as
# the longest minimumReleaseAge (30 days); rate-limited items drain at about
# 24 merges a week; automerge PRs wait at most for the next Tue/Wed/Thu window.
PENDING_DAYS = int(os.environ.get("PENDING_DAYS", "35"))
RATE_LIMITED_DAYS = int(os.environ.get("RATE_LIMITED_DAYS", "10"))
OPEN_AUTOMERGE_DAYS = int(os.environ.get("OPEN_AUTOMERGE_DAYS", "8"))
# Runs every day, the longest gap between scheduled runs is 11h45m.
RUN_STALE_HOURS = int(os.environ.get("RUN_STALE_HOURS", "26"))

# Dashboard Repository Problems that are expected, so never reported.
HARMLESS_PROBLEMS = (
    # Rules that opt out of the age gate on purpose (minimumReleaseAgeBehaviour
    # timestamp-optional) log this for every such update.
    "timestamp-optional, proceeding",
    # After updating a file, Renovate re-reads it to check that no version
    # still inside minimumReleaseAge got pulled in; when it can't find a
    # version to compare it skips that extra check and logs this. Terraform
    # provider and lock file branches trigger it on every run, and the update
    # itself goes ahead normally.
    "Could not determine resolved version after updating package file",
)

# Run log lines that mean the run failed. An ExternalHostError (a registry or
# forge timing out, e.g. gitlab.com ETIMEDOUT on 2026-10-21) aborts the whole
# repository, yet Renovate exits 0 unless exitCodeForErrors is set, so the Job
# looks fine. "Repository finished" is logged for aborted runs too: its absence
# is no sign of an abort.
ABORTED = ('"result": "external-host-error"', "External host error causing abort")
EXIT_ERROR = ("Renovate is exiting with a non-zero code", "Renovate is exiting with an error-specific code")
BRANCH_LIMIT = "Reached branch limit - skipping branch creation"

NOW = dt.datetime.now(dt.UTC)
ITEM = re.compile(r"^ - \[ \] <!-- [\w-]+=(?P<branch>[^ ]+) -->\[?(?P<title>.*?)(\]\(pulls/\d+\))?$")


def get(path):
    req = urllib.request.Request(f"{API}/repos/{REPO}/{path}", headers={"Authorization": f"token {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def sections(body):
    out, name = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            out[name] = []
        elif name:
            out[name].append(line)
    return out


def items(lines):
    found = {}
    for line in lines:
        m = ITEM.match(line)
        if m and m["branch"].startswith("renovate/"):
            found[m["branch"]] = re.sub(r"^chore\(deps\): ", "", m["title"])
    return found


def run_logs():
    """Every run log, current (plain) and rotated (.gz), oldest first."""
    return sorted(glob.glob(os.path.join(LOG_DIR, f"{glob.escape(LOG_PREFIX)}*.log*")), key=os.path.getmtime)


def run_name(path):
    return os.path.basename(path).split(".")[0]


def scan_log(path):
    """Return (aborted, exit_error, branch-limit skips) for one run log.

    Streamed line by line: a full debug log runs to tens of megabytes.
    """
    aborted = exit_error = False
    skipped = 0
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", errors="replace") as f:
        for line in f:
            aborted = aborted or any(marker in line for marker in ABORTED)
            exit_error = exit_error or any(marker in line for marker in EXIT_ERROR)
            skipped += BRANCH_LIMIT in line
    return aborted, exit_error, skipped


def days(since):
    return (NOW - dt.datetime.fromisoformat(since)).days


def main():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    seen = state.get("first_seen", {})
    alerted = set(state.get("alerted", []))
    problems = {}  # key -> message; key decides "already alerted"

    # 1. Did Renovate run, and did every run since the last check finish cleanly?
    logs = run_logs()
    if not logs:
        problems["no-log"] = f"No Renovate run log in {LOG_DIR}."
    else:
        age_h = (NOW.timestamp() - os.path.getmtime(logs[-1])) / 3600
        if age_h > RUN_STALE_HOURS:
            problems["stale"] = (
                f"No Renovate run for {age_h:.0f}h (newest log {os.path.basename(logs[-1])}). "
                "Check whatever schedules Renovate (CronJob, timer) and its last run."
            )

    # Read each log modified since the newest one the last check read, so a
    # failed run is caught even when later runs came after it. gzip keeps the
    # mtime when the next run compresses a log, so a rotated log is not read
    # twice. With no record yet, look back RUN_STALE_HOURS.
    since = state.get("logs_read_until", NOW.timestamp() - RUN_STALE_HOURS * 3600)
    new_logs = [path for path in logs if os.path.getmtime(path) > since]
    aborted, exit_error, limited, skipped = [], [], [], 0
    for path in new_logs:
        run_aborted, run_exit_error, run_skipped = scan_log(path)
        name = run_name(path)
        if run_aborted:
            aborted.append(name)
        if run_exit_error:
            exit_error.append(name)
        if run_skipped:
            limited.append(name)
            skipped += run_skipped
    if aborted:
        problems["aborted"] = (
            f"Renovate run {', '.join(aborted)} aborted on an external host error (a registry or the forge "
            "timing out or failing) and skipped the whole repository, although the Job succeeded."
        )
    if exit_error:
        problems["exit-error"] = f"Renovate run {', '.join(exit_error)} exited with an error."
    if skipped:
        problems["branch-limit"] = (
            f"Run {', '.join(limited)} hit branchConcurrentLimit and skipped {skipped} new branch(es). "
            "Updates are arriving faster than the merge window merges them, or branches are stuck."
        )

    current = {}
    if API:
        check_forge(problems, seen, current)

    new = {k: v for k, v in problems.items() if k not in alerted}
    sent = True
    if new:
        lines = [f"- {v}" for v in new.values()]
        link = f"\n\nDashboard: {WEB_URL}/{REPO}/issues/{DASHBOARD}" if API else ""
        sent = notify(APPRISE_URL, f"Renovate ({NAME}): {len(new)} new problem(s)", "\n".join(lines) + link, "warning")
    print(f"{len(problems)} problem(s), {len(new)} new", *problems.values(), sep="\n")
    # Undelivered problems stay unalerted, so the next run tries again.
    delivered = set(problems) if sent else set(problems) & alerted

    # Keys are stable while a problem lasts, so each alerts once; forgetting the
    # resolved ones means a recurrence alerts again. An undelivered digest also
    # keeps the logs unread, so the next run reports what was in them.
    read_until = max((os.path.getmtime(path) for path in new_logs), default=since) if sent else since
    with open(STATE + ".tmp", "w") as f:
        json.dump({"first_seen": current, "alerted": sorted(delivered), "logs_read_until": read_until}, f, indent=1)
    os.replace(STATE + ".tmp", STATE)
    if not sent:
        sys.exit("Apprise did not accept the digest; it is retried next run.")


def check_forge(problems, seen, current):
    """Dashboard problems, stuck updates and automerge PRs that never merge."""
    # 2. The dashboard: problems, and anything stuck in one state too long.
    body = get(f"issues/{DASHBOARD}")["body"]
    secs = sections(body)
    repo_problems = [
        line[3:].strip()
        for line in secs.get("Repository Problems", [])
        if line.startswith(" - ") and not any(harmless in line for harmless in HARMLESS_PROBLEMS)
    ]
    # One key per line: a line appearing or clearing must not re-send the others.
    for line in repo_problems:
        problems[f"repo-problem:{line}"] = f"Dashboard Repository Problem: {line}"

    for section, limit, what in (
        ("Rate-Limited", RATE_LIMITED_DAYS, "rate-limited (queue not draining)"),
        ("Pending Status Checks", PENDING_DAYS, "pending (likely no release timestamp)"),
    ):
        for branch, title in items(secs.get(section, [])).items():
            key = f"{section}:{branch}"
            current[key] = seen.get(key, NOW.isoformat())
            age = days(current[key])
            if age >= limit:
                problems[key] = f"{title}: {what} for {age} days."

    # 3. Automerge PRs that never merge.
    for pr in get("pulls?state=open&limit=50"):
        labels = {label["name"] for label in pr["labels"]}
        if AUTOMERGE_LABEL in labels:
            age = days(pr["created_at"])
            if age >= OPEN_AUTOMERGE_DAYS:
                problems[f"pr:{pr['number']}"] = f"PR #{pr['number']} {pr['title']}: automerge PR open for {age} days."
