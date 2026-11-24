"""Alert when a self-hosted Renovate stops keeping up, instead of failing silently.

Every failure this catches produced no error anywhere: an update with no
release timestamp sat in Pending Status Checks for two months, the queue
filled branchConcurrentLimit and new updates were skipped with a debug-level
log line, and runs that never started left nothing to read.

Reads the Dependency Dashboard and the open PRs through the Forgejo/Gitea API,
and every Renovate run log (written with Renovate's LOG_FILE) since the last
check, from LOG_DIR or, with LOG_SOURCE=gs://bucket[/prefix], from a Google
Cloud Storage bucket (gcs.py); a failed run counts only if Renovate did not recover from it
(to_report). The logs also name each repository whose run ended in an
error, the updates still pending OVERDUE_DAYS after their release, and the
pins missing from their dependency's releases (STUCK_PINS).
Without FORGE_API it checks the run logs only, for a Renovate
on another platform (one instance per log prefix, with its own state file). Keeps first-seen dates, and how far the logs have been read, in
STATE_DIR/alert-state.json and notifies once per problem, as a single Apprise
digest. Never exits non-zero for a problem it
reports; a crash here is a bug in this program, not in Renovate. An
undelivered digest does exit non-zero, so a failed Job shows it.

NEEDS_YOU=on adds the Renovate PRs that need a person, on GitHub, GitLab and
Forgejo, as a message of their own (needs_you.py); NEEDS_YOU=only runs that
check alone. Optional checks add GitLab CI minutes and storage, token expiry,
disabled GitHub workflows and a Renovate config's version holds to it.

REPORT=on folds both messages, with what Gatus saw overnight, into one report:
what went bad as an alert, what went well silently (report.py).
"""

import datetime as dt
import glob
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from renovate_alert import gcs, needs_you, report
from renovate_alert.report import notify

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
# 24 merges a week; automerge PRs wait at most for the next merge window, a few
# days at most even over a weekend.
PENDING_DAYS = int(os.environ.get("PENDING_DAYS", "35"))
RATE_LIMITED_DAYS = int(os.environ.get("RATE_LIMITED_DAYS", "10"))
OPEN_AUTOMERGE_DAYS = int(os.environ.get("OPEN_AUTOMERGE_DAYS", "8"))
# Runs every day: a day without one, with some slack, means Renovate stopped.
RUN_STALE_HOURS = int(os.environ.get("RUN_STALE_HOURS", "26"))
# Days since its release before a pending update counts as overdue: no age
# gate (minimumReleaseAge) lasts this long, so something else holds it.
OVERDUE_DAYS = int(os.environ.get("OVERDUE_DAYS", "95"))
# Pins missing from their dependency's releases (add_stuck): on or off.
STUCK_PINS = os.environ.get("STUCK_PINS", "on").lower()
if STUCK_PINS not in ("on", "off"):
    sys.exit("STUCK_PINS is on or off.")

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
# Under minimumReleaseAgeBehaviour=timestamp-required (the default), a release
# with no release date is pending for good, and Renovate says so only in this
# debug line, followed by the updates it holds. The dashboard's Pending Status
# Checks lists a branch only when every update in it is pending, so a grouped
# update held this way shows nowhere else.
DATELESS = "do not have a releaseTimestamp and we're running with minimumReleaseAgeBehaviour=timestamp-required"
REPOSITORY = re.compile(r'\(repository=([^,)]+)|"repository":\s*"([^"]+)"')
DEP_NAME = re.compile(r'"depName":\s*"([^"]+)"')
# Pretty-format log lines carry a record's fields indented under its message,
# as "key": value lines joined by commas, so braces around them make JSON.
DETAIL = "       "
# Each repository's run ends with this record; its result says how. Each of
# its fields is printed on its own and can be a bare undefined, which isn't
# JSON, so the result is matched instead of parsed.
FINISHED = "Repository finished"
RESULT = re.compile(r'"result":\s*"([^"]+)"')
# Results that are no failure: success, a repository Renovate skips by design
# (archived, no config, a fork...), an external host error (ABORTED has a check
# of its own) and the temporary errors the next run clears. Anything else,
# such as unknown-error, forbidden or a config error, means Renovate skipped the
# repository: nothing in it is updated until someone fixes the cause.
REPOSITORY_OK = frozenset(
    {
        "done",
        "automerged",
        "onboarding",
        "archived",
        "blocked",
        "cannot-fork",
        "disabled",
        "disabled-by-config",
        "disabled-closed-onboarding",
        "disabled-no-config",
        "empty",
        "fork",
        "fork-missing",
        "fork-mode-forked",
        "mirror",
        "no-package-files",
        "pending-deletion",
        "renamed",
        "uninitiated",
        "external-host-error",
        "no-vulnerability-alerts",
        "rate-limit-exceeded",
        "repository-changed",
        "temporary-error",
    }
)
# The records whose err names why a repository failed (workers/repository/error.ts).
CAUSES = ("Repository has unknown error", "Repository has invalid config", "Git error - aborting")
ERR_MESSAGE = re.compile(r'"message":\s*("(?:[^"\\]|\\.)*")')
# The dependencies of each package file, with the updates found for them.
PACKAGE_FILES = "packageFiles with updates"
# The records read_record reads.
RECORDS = (DATELESS, FINISHED, PACKAGE_FILES, *CAUSES)

NOW = dt.datetime.now(dt.UTC)
ITEM = re.compile(r"^ - \[ \] <!-- [\w-]+=(?P<branch>[^ ]+) -->\[?(?P<title>.*?)(\]\(pulls/\d+\))?$")


# Codeberg answered 504s and timed out reads on 2026-11-16; one timeout
# crashed the daily run. A read retries a gateway error or a timeout twice,
# and waits up to 120 s: listing infra's 17 open pull requests took 42 s that
# day (image-cve-alert waits as long for the same list).
READ_TIMEOUT_SECONDS = 120
READ_ATTEMPTS = 3
READ_RETRY_SECONDS = 10


def get(path, sleep=time.sleep):
    req = urllib.request.Request(f"{API}/repos/{REPO}/{path}", headers={"Authorization": f"token {TOKEN}"})
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECONDS) as r:  # nosec B310: FORGE_API, set by the operator
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (502, 503, 504) or attempt == READ_ATTEMPTS:
                raise
        except urllib.error.URLError, TimeoutError:
            if attempt == READ_ATTEMPTS:
                raise
        sleep(READ_RETRY_SECONDS * attempt)
    raise AssertionError("unreachable")


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


class Scan:
    """What one run log says: see scan_log."""

    def __init__(self):
        self.aborted = self.exit_error = False
        self.skipped, self.dateless, self.results, self.overdue, self.stuck = {}, {}, {}, {}, {}


def scan_log(path):
    """Read one run log into a Scan.

    aborted and exit_error say whether the run failed. skipped maps each limit
    option to the branches it kept from being created, as "(repository=...,
    branch=...)" ids. Per repository: dateless holds the dependencies held
    pending for want of a release date, results how its run ended, as
    (result, cause), overdue the updates pending for longer than
    OVERDUE_DAYS, as (depName, current, new, days), and stuck the
    dependencies add_stuck finds. Reads LOG_FILE_FORMAT=pretty
    and JSON logs. Streamed: a full debug log runs to tens of megabytes, so
    only the record being read is held.
    """
    scan = Scan()
    limit = None
    # The record being read: its first line, and its detail lines if it is one
    # read_record reads (others can run to megabytes).
    record = None
    causes = {}  # repository -> why its run failed, until it finishes
    with open_log(path) as f:
        for n, line in enumerate(f):
            if record is not None and line.startswith(DETAIL):
                if record[1] is not None:
                    record[1].append(line)
            else:
                if record is not None and record[1] is not None:
                    read_record(scan, causes, *record)
                record = (line, [] if any(marker in line for marker in RECORDS) else None)
            scan.aborted = scan.aborted or any(marker in line for marker in ABORTED)
            scan.exit_error = scan.exit_error or any(marker in line for marker in EXIT_ERROR)
            if m := LIMIT_REACHED.search(line):
                limit = LIMIT_OPTION[m[1]]
            elif BRANCH_LIMIT in line:
                branch = BRANCH_ID.search(line)
                scan.skipped.setdefault(limit or "an unidentified limit", set()).add(
                    branch[0] if branch else f"{path}:{n}"
                )
                limit = None
        if record is not None and record[1] is not None:
            read_record(scan, causes, *record)
    return scan


def record_data(line, details):
    """A record's fields: a JSON line's, or a pretty record's detail lines; {} if they don't parse."""
    try:
        data = json.loads(line) if line.startswith("{") else json.loads("{" + "".join(details) + "}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_record(scan, causes, line, details):
    """Add one log record to scan. A JSON log has no detail lines: each line is a record."""
    found = REPOSITORY.search(line)
    repo = (found[1] or found[2]) if found else NAME
    if DATELESS in line:
        held = scan.dateless.setdefault(repo, set())
        held.update(DEP_NAME.findall(line))
        for detail in details:
            held.update(DEP_NAME.findall(detail))
    elif any(cause in line for cause in CAUSES):
        causes[repo] = error_message(line, details)
    elif FINISHED in line:
        found = next((m for m in map(RESULT.search, [line, *details]) if m), None)
        if found:
            scan.results[repo] = (found[1], causes.pop(repo, ""))
    elif PACKAGE_FILES in line:
        config = record_data(line, details).get("config")
        if isinstance(config, dict):
            add_overdue(scan.overdue, repo, config)
            add_stuck(scan.stuck, repo, config)


def error_message(line, details):
    """The first line of a record's err.message, or ""."""
    if line.startswith("{"):
        err = record_data(line, details).get("err")
        message = err.get("message") if isinstance(err, dict) else None
    else:
        found = next((m for m in map(ERR_MESSAGE.search, details) if m), None)
        message = json.loads(found[1]) if found else None
    return message.split("\n")[0][:200] if isinstance(message, str) else ""


def add_overdue(overdue, repo, config):
    """Add the updates of packageFiles (manager -> files) pending for over OVERDUE_DAYS."""
    for files in config.values():
        for file in files if isinstance(files, list) else []:
            for dep in file.get("deps") or []:
                for update in dep.get("updates") or []:
                    stamp = update.get("releaseTimestamp")
                    if not (update.get("pendingChecks") and stamp):
                        continue
                    try:
                        age = (NOW - dt.datetime.fromisoformat(stamp)).days
                    except TypeError, ValueError:
                        continue
                    if age > OVERDUE_DAYS:
                        new = update.get("newVersion") or update.get("newValue") or update.get("newDigest")
                        overdue.setdefault(repo, set()).add((dep.get("depName"), dep.get("currentValue"), new, age))


def add_stuck(stuck, repo, config):
    """Add repo's pins missing from their releases, as (depName, packageName, current, newest).

    A dependency with no update, a mostRecentTimestamp and no
    currentVersionTimestamp has releases, but the pin isn't among them: its
    datasource dropped it, as infra's lazylibrarian datasource does with the
    builds before a linuxserver -ls counter reset, and no update will come.
    A pin older than the newest release proves nothing: Renovate computes
    mostRecentTimestamp from the raw releases, before extractVersion, the
    unstable filter and allowedVersions drop any (a Python alpha, a held
    major), and a real sort-order break leaves it out. Each repository gets an
    entry, so a run that finds nothing clears an earlier one.
    """
    found = stuck.setdefault(repo, set())
    if STUCK_PINS == "off":
        return
    for files in config.values():
        for file in files if isinstance(files, list) else []:
            for dep in file.get("deps") or []:
                if dep.get("skipReason") or dep.get("updates") or dep.get("currentVersionTimestamp"):
                    continue
                try:
                    newest = dt.datetime.fromisoformat(dep["mostRecentTimestamp"])
                except KeyError, TypeError, ValueError:
                    continue
                found.add((dep.get("depName"), dep.get("packageName"), dep.get("currentValue"), newest.date()))


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
    # With REPORT=on the checks below hand their messages to the report
    # instead of sending them, and record them as sent; if the report then
    # isn't delivered, their state goes back, so the next run reports them.
    saved = report.snapshot([STATE, needs_you.STATE]) if report.ENABLED else {}
    if NEEDS_YOU != "only":
        failures += check_renovate()
    if NEEDS_YOU != "off":
        failures += needs_you.run()
    if report.ENABLED:
        failures += report.run()
        if not report.delivered:
            report.restore(saved)
    if failures:
        sys.exit("\n".join(failures))


def check_renovate():
    """Run logs, and with FORGE_API the dashboard and PRs; return what failed, for the exit status."""
    try:
        with open(STATE) as f:
            state = json.load(f)
    except FileNotFoundError, json.JSONDecodeError:
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
    aborted, exit_error, limited, skipped, dateless, runs = [], [], {}, {}, {}, []
    results, overdue, stuck = {}, {}, {}  # results and stuck: each repository's newest
    for path, _ in new_logs:
        scan = scan_log(path)
        for repo, deps in scan.dateless.items():
            dateless.setdefault(repo, set()).update(deps)
        for repo, updates in scan.overdue.items():
            overdue.setdefault(repo, set()).update(updates)
        results.update(scan.results)
        stuck.update(scan.stuck)
        name = run_name(path)
        runs.append((name, scan.aborted or scan.exit_error))
        if scan.aborted:
            aborted.append(name)
        if scan.exit_error:
            exit_error.append(name)
        for option, branches in scan.skipped.items():
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

    # One key per dependency: a new one alerts without repeating the others.
    for repo, deps in sorted(dateless.items()):
        for dep in sorted(deps) or ["an unnamed dependency"]:
            problems[f"dateless:{repo}:{dep}"] = (
                f"{dep} ({repo}): a release has no release date, so minimumReleaseAge holds it pending for good. "
                "Look it up in a datasource that dates releases, or give it minimumReleaseAgeBehaviour "
                "timestamp-optional."
            )

    # A repository whose newest run failed; one that recovered since is fine.
    for repo, (result, cause) in sorted(results.items()):
        if result not in REPOSITORY_OK:
            problems[f"repo-failed:{repo}:{result}"] = (
                f"{repo}: Renovate's run ended in {result}"
                + (f" ({cause})" if cause else "")
                + ", so it updates nothing there until the cause is fixed."
            )

    for repo, updates in sorted(overdue.items()):
        for dep, current_value, new_value, age in sorted(updates, key=str):
            problems[f"overdue:{repo}:{dep}:{new_value}"] = (
                f"{dep} {current_value} -> {new_value} ({repo}): released {age} days ago and still pending. "
                f"No age gate should last {OVERDUE_DAYS} days: check the repository's rules."
            )

    for repo, deps in sorted(stuck.items()):
        for dep, _, current_value, newest in sorted(deps, key=str):
            problems[f"stuck:{repo}:{dep}:{current_value}"] = (
                f"{dep} {current_value} ({repo}): Renovate offers no update, and the pin is not among its "
                f"releases (newest {newest}). Its datasource dropped it, as after a linuxserver -ls counter "
                "reset: re-pin by hand to the newest release."
            )

    current = {}
    if API:
        check_forge(problems, seen, current)

    new = {k: v for k, v in problems.items() if k not in alerted}
    sent = True
    if new:
        lines = [f"- {v}" for v in new.values()]
        link = f"\n\nDashboard: {WEB_URL}/{REPO}/issues/{DASHBOARD}" if API else ""
        title = f"Renovate ({NAME}): {len(new)} new problem(s)"
        sent = report.send(notify, APPRISE_URL, title, "\n".join(lines) + link, "warning")
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
