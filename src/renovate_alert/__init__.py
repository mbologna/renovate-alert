"""Alert when a self-hosted Renovate stops keeping up, instead of failing silently.

Every failure this catches produced no error anywhere: an update with no
release timestamp sat in Pending Status Checks for two months, the queue
filled branchConcurrentLimit and new updates were skipped with a debug-level
log line, and runs that never started left nothing to read.

Reads the Dependency Dashboard and the open PRs through the Forgejo/Gitea API,
and every Renovate run log (written with Renovate's LOG_FILE) since the last
check, from LOG_DIR or, with LOG_SOURCE=gs://bucket[/prefix], from a Google
Cloud Storage bucket (gcs.py); a failed run counts only if Renovate did not recover from it
(to_report). Without FORGE_API it checks the run logs only, for a Renovate
on another platform (one instance per log prefix, with its own state file). Keeps first-seen dates, and how far the logs have been read, in
STATE_DIR/alert-state.json and notifies once per problem, as a single Apprise
digest. Never exits non-zero for a problem it
reports; a crash here is a bug in this program, not in Renovate. An
undelivered digest does exit non-zero, so a failed Job shows it.

NEEDS_YOU=on adds the Renovate PRs that need a person, on GitHub, GitLab and
Forgejo, as a message of their own (needs_you.py); NEEDS_YOU=only runs that
check alone.
"""

import datetime as dt
import glob
import gzip
import io
import json
import os
import re
import sys
import urllib.request

from apprise_notify import notify

from renovate_alert import gcs, needs_you

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
# gs://bucket[/prefix]: read the run logs from Google Cloud Storage instead of
# LOG_DIR, with the service account key in GOOGLE_APPLICATION_CREDENTIALS.
LOG_SOURCE = os.environ.get("LOG_SOURCE", "")
try:
    GCS_BUCKET, GCS_PREFIX = gcs.parse_url(LOG_SOURCE) if LOG_SOURCE else ("", "")
except ValueError as e:
    sys.exit(f"LOG_SOURCE: {e}")
# The state files; LOG_DIR unless set (with LOG_SOURCE, LOG_DIR holds nothing else).
STATE_DIR = os.environ.get("STATE_DIR") or LOG_DIR
# Run logs are LOG_PREFIX*.log[.gz]. Two Renovates can share LOG_DIR with
# different prefixes; each check then needs its own STATE_FILE.
LOG_PREFIX = os.environ.get("LOG_PREFIX", "renovate-")
STATE = os.path.join(STATE_DIR, os.environ.get("STATE_FILE", "alert-state.json"))
# A run log in the bucket: plain or gzipped.
LOG_NAME = re.compile(r"\.log(\.gz)?$")
# off: the Renovate checks only; on: those and the PRs that need you; only: the PRs alone.
NEEDS_YOU = os.environ.get("NEEDS_YOU", "off").lower()
if NEEDS_YOU not in ("off", "on", "only"):
    sys.exit("NEEDS_YOU is off, on or only.")

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
# Renovate logs BRANCH_LIMIT whichever limit stopped the branch; the debug
# line just before it names the limit (workers/global/limits.ts).
LIMIT_REACHED = re.compile(r"\b(Hourly PRs|Hourly commits|Commits|(?:Vulnerability)?Branches) limit reached")
LIMIT_OPTION = {
    "Hourly PRs": "prHourlyLimit",
    "Hourly commits": "commitHourlyLimit",
    "Commits": "prCommitsPerRunLimit",
    "Branches": "branchConcurrentLimit",
    "VulnerabilityBranches": "branchConcurrentLimit",
}
LIMIT_HINT = {
    "branchConcurrentLimit": "Updates are arriving faster than the merge window merges them, or branches are stuck.",
    "prHourlyLimit": "An hourly cap parks updates even while the queue has room; Renovate's default is 2.",
}
BRANCH_ID = re.compile(r"\(repository=[^,)]+, branch=[^)]+\)")

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


def log_source():
    """Where the run logs are read from, for messages."""
    return LOG_SOURCE or LOG_DIR


def run_logs():
    """Every run log, current (plain) and rotated (.gz), as (path or object name, mtime), oldest first."""
    if GCS_BUCKET:
        logs = [log for log in gcs.list_objects(GCS_BUCKET, GCS_PREFIX + LOG_PREFIX) if LOG_NAME.search(log[0])]
    else:
        logs = [
            (path, os.path.getmtime(path))
            for path in glob.glob(os.path.join(LOG_DIR, f"{glob.escape(LOG_PREFIX)}*.log*"))
        ]
    return sorted(logs, key=lambda log: log[1])


def open_log(path):
    """One run log as text, from LOG_DIR or the bucket, decompressing a .gz."""
    raw = gcs.open_object(GCS_BUCKET, path) if GCS_BUCKET else open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(raw, "rt", encoding="utf-8", errors="replace")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def run_name(path):
    return os.path.basename(path).split(".")[0]


def scan_log(path):
    """Return (aborted, exit_error, branch-limit skips) for one run log.

    The skips map each limit option to the branches it kept from being
    created, as "(repository=..., branch=...)" ids. Streamed line by line: a
    full debug log runs to tens of megabytes.
    """
    aborted = exit_error = False
    skipped = {}
    limit = None
    with open_log(path) as f:
        for n, line in enumerate(f):
            aborted = aborted or any(marker in line for marker in ABORTED)
            exit_error = exit_error or any(marker in line for marker in EXIT_ERROR)
            if m := LIMIT_REACHED.search(line):
                limit = LIMIT_OPTION[m[1]]
            elif BRANCH_LIMIT in line:
                branch = BRANCH_ID.search(line)
                skipped.setdefault(limit or "an unidentified limit", set()).add(branch[0] if branch else f"{path}:{n}")
                limit = None
    return aborted, exit_error, skipped


def to_report(runs, failed_before=False):
    """The names of the failed runs worth a report, of runs as (name, failed) oldest first.

    A failed run is reported when no later run succeeded (the newest run failed
    too), or when it is one of two or more failed runs in a row: a single
    failure that the next run recovered from is a blip, such as a forge timing
    out once. failed_before says whether the run before the first one failed.
    """
    report, streak, length = set(), [], int(failed_before)
    for name, failed in runs:
        if failed:
            streak.append(name)
            length += 1
            continue
        if length >= 2:
            report.update(streak)
        streak, length = [], 0
    return report | set(streak)  # the newest run failed too: nothing recovered from these


def days(since):
    return (NOW - dt.datetime.fromisoformat(since)).days


def main():
    failures = []
    if NEEDS_YOU != "only":
        failures += check_renovate()
    if NEEDS_YOU != "off":
        failures += needs_you.run()
    if failures:
        sys.exit("\n".join(failures))


def check_renovate():
    """Run logs, and with FORGE_API the dashboard and PRs; return what failed, for the exit status."""
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
        problems["no-log"] = f"No Renovate run log in {log_source()}."
    else:
        age_h = (NOW.timestamp() - logs[-1][1]) / 3600
        if age_h > RUN_STALE_HOURS:
            problems["stale"] = (
                f"No Renovate run for {age_h:.0f}h (newest log {os.path.basename(logs[-1][0])}). "
                "Check whatever schedules Renovate (CronJob, timer) and its last run."
            )

    # Read each log modified since the newest one the last check read, so a
    # failed run is caught even when later runs came after it. gzip keeps the
    # mtime when the next run compresses a log, so a rotated log is not read
    # twice. With no record yet, look back RUN_STALE_HOURS.
    since = state.get("logs_read_until", NOW.timestamp() - RUN_STALE_HOURS * 3600)
    new_logs = [(path, mtime) for path, mtime in logs if mtime > since]
    # A branch skipped again by each run is one problem, not one per run.
    aborted, exit_error, limited, skipped, runs = [], [], {}, {}, []
    for path, _ in new_logs:
        run_aborted, run_exit_error, run_skipped = scan_log(path)
        name = run_name(path)
        runs.append((name, run_aborted or run_exit_error))
        if run_aborted:
            aborted.append(name)
        if run_exit_error:
            exit_error.append(name)
        for option, branches in run_skipped.items():
            limited.setdefault(option, []).append(name)
            skipped.setdefault(option, set()).update(branches)
    # Only failures the runs did not recover from (to_report).
    failed_before = state.get("last_run_failed", False)
    reported = to_report(runs, failed_before)
    aborted = [name for name in aborted if name in reported]
    exit_error = [name for name in exit_error if name in reported]
    if aborted:
        problems["aborted"] = (
            f"Renovate run {', '.join(aborted)} aborted on an external host error (a registry or the forge "
            "timing out or failing) and skipped the whole repository, although the Job succeeded."
        )
    if exit_error:
        problems["exit-error"] = f"Renovate run {', '.join(exit_error)} exited with an error."
    for option, branches in sorted(skipped.items()):
        # "branch-limit" alone is branchConcurrentLimit's key from before limits were told apart.
        key = "branch-limit" if option == "branchConcurrentLimit" else f"branch-limit:{option}"
        problems[key] = (
            f"Run {', '.join(limited[option])} hit {option} and skipped {len(branches)} new branch(es). "
            + LIMIT_HINT.get(option, "")
        ).rstrip()

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
    read_until = max((mtime for _, mtime in new_logs), default=since) if sent else since
    last_failed = runs[-1][1] if runs and sent else failed_before
    with open(STATE + ".tmp", "w") as f:
        json.dump(
            {
                "first_seen": current,
                "alerted": sorted(delivered),
                "logs_read_until": read_until,
                "last_run_failed": last_failed,
            },
            f,
            indent=1,
        )
    os.replace(STATE + ".tmp", STATE)
    return [] if sent else ["Apprise did not accept the digest; it is retried next run."]


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
