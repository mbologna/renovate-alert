"""Renovate PRs that need a person: held for manual review, or with red CI.

Everything else Renovate opens merges by itself, so these are the only PRs
worth a message. Reads the open Renovate PRs on GitHub, GitLab and Forgejo
(each forge only if its token is set) and sends one Apprise message listing
the ones that need you, grouped by forge and repository. A PR needs you if it
carries MANUAL_REVIEW_LABEL or lacks AUTOMERGE_LABEL, or if a check on its
head commit failed; pending checks and draft PRs don't count. On GitHub it
also lists a repository whose Dependency Dashboard still has updates awaiting
their schedule after DASHBOARD_STALE_DAYS without a change: the Renovate app
has not visited it for a whole schedule window.

The message goes out when something new appears, and again every Monday as a
reminder while the list is not empty. What was last sent is kept in
STATE_DIR (default LOG_DIR)/needs-you-state.json, keyed by PR URL and reason, so a PR that merely
stays open is not repeated, and one that goes away and comes back is.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from apprise_notify import notify

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_API = "https://api.github.com"
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
GITLAB_URL = os.environ.get("GITLAB_URL") or "https://gitlab.com"
GITLAB_NAMESPACE = os.environ.get("GITLAB_NAMESPACE", "")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")
FORGEJO_URL = os.environ.get("FORGEJO_URL", "")
FORGEJO_REPOS = [repo.strip() for repo in os.environ.get("FORGEJO_REPOS", "").split(",") if repo.strip()]
APPRISE_URL = os.environ.get("APPRISE_URL", "")
AUTOMERGE_LABEL = os.environ.get("AUTOMERGE_LABEL", "renovate-automerge")
MANUAL_REVIEW_LABEL = os.environ.get("MANUAL_REVIEW_LABEL", "manual-review")
DASHBOARD_TITLE = os.environ.get("DASHBOARD_TITLE") or "🤖 Renovate Dependency Dashboard"
DASHBOARD_STALE_DAYS = int(os.environ.get("DASHBOARD_STALE_DAYS") or 8)
STATE = os.path.join(
    os.environ.get("STATE_DIR") or os.environ.get("LOG_DIR", "/logs"),
    os.environ.get("NEEDS_YOU_STATE_FILE", "needs-you-state.json"),
)

# Codeberg's PR list takes up to 30 s and answers 504 at times: Forgejo calls
# get a long timeout, and every call one retry after a timeout or a 5xx.
TIMEOUT = 30
FORGEJO_TIMEOUT = 120
RETRY_DELAY = 5

# GitHub check run conclusions that mean red CI. cancelled is left out: a
# newer run of the same workflow cancels the older one.
FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
# The same for Actions workflow runs, read when the check runs can't be.
# action_required: a run waiting for approval never reports a check.
FAILED_RUN_CONCLUSIONS = FAILED_CONCLUSIONS | {"action_required"}
# The preset's PR note for a held update; the line is quoted as it stands,
# without its emoji and bold markup.
WHY = re.compile(r"^[\W_]*(why this waits for you\b.*)", re.IGNORECASE)
AWAITING = re.compile(r"^## Awaiting Schedule\s*$", re.MULTILINE)
# The key reason of a stale dashboard; the message says how many days.
NOT_VISITED = "not visited by Renovate"

NOW = dt.datetime.now(dt.UTC)


def fetch(url, headers, timeout=TIMEOUT):
    """GET url and parse its JSON, retrying once after a timeout or a 5xx."""
    req = urllib.request.Request(url, headers=headers)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(RETRY_DELAY)


def pages(url, headers, size_param="per_page", size=100, timeout=TIMEOUT):
    """Every item of a paginated list: pages are read until one comes back short."""
    out, page = [], 1
    sep = "&" if "?" in url else "?"
    while True:
        batch = fetch(f"{url}{sep}{size_param}={size}&page={page}", headers, timeout)
        out += batch
        if len(batch) < size:
            return out
        page += 1


def why_note(body):
    for line in (body or "").splitlines():
        if m := WHY.match(line.replace("**", "")):
            return m[1].strip()
    return ""


def item(forge, repo, title, url, labels, created, body, failed):
    """The PR as a list entry if it needs you, else None."""
    reasons = []
    if MANUAL_REVIEW_LABEL in labels or AUTOMERGE_LABEL not in labels:
        reasons.append("manual review")
    if failed:
        reasons.append(f"CI failed: {', '.join(sorted(set(failed)))}")
    if not reasons:
        return None
    note = why_note(body) if "manual review" in reasons else ""
    entry = {"forge": forge, "repo": repo, "title": title, "url": url, "created": created}
    return entry | {"reasons": reasons, "note": note}


def search(query, headers):
    """Every issue or PR the GitHub search finds."""
    query = urllib.parse.quote(query)
    found, page = [], 1
    while True:
        batch = fetch(f"{GITHUB_API}/search/issues?q={query}&per_page=100&page={page}", headers)
        found += batch["items"]
        if len(batch["items"]) < 100 or len(found) >= batch["total_count"]:
            return found
        page += 1


def days_since(timestamp):
    return (NOW - dt.datetime.fromisoformat(timestamp)).days


def stale_dashboards(headers):
    """Dashboards with updates awaiting their schedule and no change in DASHBOARD_STALE_DAYS.

    The Renovate app visits a quiet repository irregularly. On its visit in a
    schedule window it opens the PRs awaiting that window, which changes the
    dashboard; a dashboard that still lists them after a whole window means no
    visit. One with nothing awaiting may rightly stay unchanged for months.
    """
    if DASHBOARD_STALE_DAYS <= 0:
        return []
    found = search(f'is:issue is:open in:title "Renovate Dependency Dashboard" user:{GITHUB_OWNER}', headers)
    items = []
    for issue in found:
        if issue["title"] != DASHBOARD_TITLE or not AWAITING.search(issue["body"] or ""):
            continue
        if days_since(issue["updated_at"]) < DASHBOARD_STALE_DAYS:
            continue
        repo = issue["repository_url"].split("/repos/", 1)[1]
        entry = {"forge": "GitHub", "repo": repo, "title": issue["title"], "url": issue["html_url"]}
        dates = {"created": issue["created_at"], "updated": issue["updated_at"]}
        items.append(entry | dates | {"reasons": [NOT_VISITED], "note": ""})
    return items


def github_failed_checks(api, sha, headers):
    """The names of the failed check runs on sha.

    A fine-grained token can't read check runs (403, or 404 on a private
    repository), so then the failed Actions workflow runs are named instead,
    which its Actions read permission covers.
    """
    try:
        runs = fetch(f"{api}/commits/{sha}/check-runs?per_page=100", headers)["check_runs"]
        return [run["name"] for run in runs if run["conclusion"] in FAILED_CONCLUSIONS]
    except urllib.error.HTTPError as e:
        if e.code not in (403, 404):
            raise
    runs = fetch(f"{api}/actions/runs?head_sha={sha}&per_page=100", headers)["workflow_runs"]
    return [run["name"] for run in runs if run["conclusion"] in FAILED_RUN_CONCLUSIONS]


def github():
    """Open PRs by the Renovate app in GITHUB_OWNER's repositories, private ones included, and stale dashboards."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prs = search(f"is:pr is:open author:app/renovate user:{GITHUB_OWNER}", headers)
    items = []
    for pr in prs:
        if pr.get("draft"):
            continue
        repo = pr["repository_url"].split("/repos/", 1)[1]
        api = f"{GITHUB_API}/repos/{repo}"
        sha = fetch(f"{api}/pulls/{pr['number']}", headers)["head"]["sha"]
        failed = github_failed_checks(api, sha, headers)
        statuses = fetch(f"{api}/commits/{sha}/status?per_page=100", headers)["statuses"]
        failed += [status["context"] for status in statuses if status["state"] in ("failure", "error")]
        labels = {label["name"] for label in pr["labels"]}
        items.append(item("GitHub", repo, pr["title"], pr["html_url"], labels, pr["created_at"], pr["body"], failed))
    return items + stale_dashboards(headers)


def gitlab():
    """Open MRs from renovate/ branches in GITLAB_NAMESPACE, a group (subgroups included) or a user."""
    api = GITLAB_URL.rstrip("/").removesuffix("/api/v4") + "/api/v4"
    # Bearer takes a personal, group or project access token, and an OAuth token too.
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}
    namespace = urllib.parse.quote(GITLAB_NAMESPACE, safe="")
    try:
        mrs = pages(f"{api}/groups/{namespace}/merge_requests?state=opened", headers)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # A user namespace has no MR listing of its own: go through its projects.
        projects = pages(f"{api}/users/{namespace}/projects?archived=false&simple=true", headers)
        mrs = [mr for p in projects for mr in pages(f"{api}/projects/{p['id']}/merge_requests?state=opened", headers)]
    items = []
    for mr in mrs:
        if not mr["source_branch"].startswith("renovate/") or mr.get("draft"):
            continue
        # The MR list leaves out the pipeline; the single MR has it.
        pipeline = fetch(f"{api}/projects/{mr['project_id']}/merge_requests/{mr['iid']}", headers).get("head_pipeline")
        failed = []
        if pipeline and pipeline["status"] == "failed":
            project = pipeline.get("project_id", mr["project_id"])
            jobs = pages(f"{api}/projects/{project}/pipelines/{pipeline['id']}/jobs?scope%5B%5D=failed", headers)
            failed = [job["name"] for job in jobs if not job.get("allow_failure")] or [f"pipeline {pipeline['id']}"]
        repo, labels = mr["references"]["full"].rsplit("!", 1)[0], set(mr["labels"])
        items.append(
            item("GitLab", repo, mr["title"], mr["web_url"], labels, mr["created_at"], mr["description"], failed)
        )
    return items


def forgejo():
    """Open PRs from renovate/ branches in each of FORGEJO_REPOS."""
    api = FORGEJO_URL.rstrip("/").removesuffix("/api/v1") + "/api/v1"
    headers = {"Authorization": f"token {FORGEJO_TOKEN}"}
    items = []
    for repo in FORGEJO_REPOS:
        for pr in pages(f"{api}/repos/{repo}/pulls?state=open", headers, "limit", 50, FORGEJO_TIMEOUT):
            if not pr["head"]["ref"].startswith("renovate/") or pr.get("draft"):
                continue
            status_url = f"{api}/repos/{repo}/commits/{pr['head']['sha']}/status?limit=50"
            combined = fetch(status_url, headers, FORGEJO_TIMEOUT)
            failed = []
            if combined["state"] in ("failure", "error"):
                statuses = combined.get("statuses") or []
                failed = [s["context"] for s in statuses if s["status"] in ("failure", "error")] or ["combined status"]
            labels = {label["name"] for label in pr["labels"]}
            items.append(
                item("Forgejo", repo, pr["title"], pr["html_url"], labels, pr["created_at"], pr["body"], failed)
            )
    return items


def forges():
    """The configured forges as (name, reader); exits on a half-configured one."""
    found = []
    for name, reader, token, needed in (
        ("GitHub", github, GITHUB_TOKEN, {"GITHUB_OWNER": GITHUB_OWNER}),
        ("GitLab", gitlab, GITLAB_TOKEN, {"GITLAB_NAMESPACE": GITLAB_NAMESPACE}),
        ("Forgejo", forgejo, FORGEJO_TOKEN, {"FORGEJO_URL": FORGEJO_URL, "FORGEJO_REPOS": FORGEJO_REPOS}),
    ):
        if token:
            missing = [var for var, value in needed.items() if not value]
            if missing:
                sys.exit(f"{name}'s token is set but {' and '.join(missing)} is not.")
            found.append((name, reader))
    if not found:
        sys.exit("NEEDS_YOU needs GITHUB_TOKEN, GITLAB_TOKEN or FORGEJO_TOKEN.")
    return found


def keys(entry):
    return [f"{entry['url']} {reason}" for reason in entry["reasons"]]


def status(entry):
    """The entry's reasons and age, as listed."""
    if "updated" in entry:  # a stale dashboard
        days = days_since(entry["updated"])
        return f"Renovate hasn't visited in {days} days (updates awaiting schedule)"
    days = days_since(entry["created"])
    return f"{'; '.join(entry['reasons'])}, open {days} day{'' if days == 1 else 's'}"


def render(items, new):
    lines, group = [], None
    for entry in sorted(items, key=lambda e: (e["forge"], e["repo"], e["created"])):
        if (entry["forge"], entry["repo"]) != group:
            group = entry["forge"], entry["repo"]
            lines += [""] if lines else []
            lines.append(f"{entry['forge']} {entry['repo']}")
        flag = " (new)" if new & set(keys(entry)) else ""
        lines.append(f"- {entry['title']}")
        lines.append(f"  {status(entry)}{flag}")
        if entry["note"]:
            lines.append(f"  {entry['note']}")
        lines.append(f"  {entry['url']}")
    return "\n".join(lines)


def run():
    """Check the forges and send the list if needed; return what failed, for the exit status."""
    readers = forges()
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    previous = state.get("items", {})
    read, errors, items = {}, [], []
    for name, reader in readers:
        try:
            read[name] = [entry for entry in reader() if entry]
        except (OSError, ValueError) as e:
            # Keep the last good read, so a forge that is down for a day
            # neither drops its PRs from the list nor brings them back as new.
            errors.append(f"{name} could not be read ({e}); its PRs are as of the last check.")
            read[name] = previous.get(name, [])
        items += read[name]

    current = {key for entry in items for key in keys(entry)}
    sent_before = set(state.get("sent", []))
    new = current - sent_before
    today = NOW.date().isoformat()
    reminder = NOW.weekday() == 0 and state.get("last_sent") != today
    sent, delivered = sent_before & current, True
    what = "item(s)" if any("updated" in entry for entry in items) else "PR(s)"
    if items and (new or reminder):
        fresh = sum(1 for entry in items if new & set(keys(entry)))
        title = f"Renovate: {len(items)} {what} need you" + (f", {fresh} new" if fresh else " (Monday reminder)")
        body = render(items, new) + "".join(f"\n\n{error}" for error in errors)
        delivered = notify(APPRISE_URL, title, body, "info")
        if delivered:
            sent, state["last_sent"] = current, today
    print(f"{len(items)} {what} need you, {len(new)} new reason(s)", *(e["url"] for e in items), *errors, sep="\n")

    state.update(items=read, sent=sorted(sent))
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(STATE + ".tmp", STATE)
    failures = errors[:]
    if not delivered:
        failures.append("Apprise did not accept the needs-you message; it is retried next run.")
    return failures
