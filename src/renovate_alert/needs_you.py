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

Four optional checks, each off unless switched on, add entries to the same
message with the same rules: GitLab CI minutes and namespace storage near the
quota (GITLAB_USAGE), forge tokens close to expiry (TOKEN_EXPIRY), GitHub
workflows disabled for inactivity (DISABLED_WORKFLOWS), and every fourth
Monday the version holds (packageRules with allowedVersions) of a Renovate
config, so none is forgotten (VERSION_HOLDS).
"""

import base64
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


def switch(name):
    """An on/off variable, off by default."""
    value = os.environ.get(name, "off").lower()
    if value not in ("on", "off"):
        sys.exit(f"{name} is on or off.")
    return value == "on"


# GitLab CI minutes and namespace storage against the free tier's quotas.
GITLAB_USAGE = switch("GITLAB_USAGE")
GITLAB_MINUTES_QUOTA = int(os.environ.get("GITLAB_MINUTES_QUOTA") or 400)
GITLAB_MINUTES_WARN = int(os.environ.get("GITLAB_MINUTES_WARN") or 75)
GITLAB_MINUTES_PROJECTED_WARN = int(os.environ.get("GITLAB_MINUTES_PROJECTED_WARN") or 100)
# A projection from the first days of a month is mostly noise: one release
# build on the 1st projects to thirty.
GITLAB_MINUTES_PROJECT_AFTER_DAYS = int(os.environ.get("GITLAB_MINUTES_PROJECT_AFTER_DAYS") or 7)
GITLAB_STORAGE_LIMIT_GIB = float(os.environ.get("GITLAB_STORAGE_LIMIT_GIB") or 5)
GITLAB_STORAGE_WARN = int(os.environ.get("GITLAB_STORAGE_WARN") or 80)
# The GitHub and GitLab tokens' expiry dates.
TOKEN_EXPIRY = switch("TOKEN_EXPIRY")
TOKEN_EXPIRY_DAYS = int(os.environ.get("TOKEN_EXPIRY_DAYS") or 30)
# GitHub workflows disabled after 60 days without activity in the repository.
DISABLED_WORKFLOWS = switch("DISABLED_WORKFLOWS")
# The packageRules with allowedVersions in a Renovate config file, every fourth Monday.
VERSION_HOLDS = switch("VERSION_HOLDS")
HOLDS_FORGE = os.environ.get("HOLDS_FORGE", "forgejo").lower()
HOLDS_REPO = os.environ.get("HOLDS_REPO", "")
HOLDS_FILE = os.environ.get("HOLDS_FILE") or "renovate.json"
HOLDS_EVERY_DAYS = 28
FORGE_NAMES = {"github": "GitHub", "gitlab": "GitLab", "forgejo": "Forgejo"}
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
# The reader name of the version holds, which are listed every fourth Monday only.
HOLDS = "Version holds"
GIB = 1024**3

NOW = dt.datetime.now(dt.UTC)


def fetch(url, headers, timeout=TIMEOUT, data=None, with_headers=False):
    """GET url (POST data, if given) and parse its JSON, retrying once after a timeout or a 5xx.

    with_headers: return (JSON, response headers).
    """
    req = urllib.request.Request(url, headers=headers, data=data)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
                return (body, r.headers) if with_headers else body
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


def search(query, headers, kind="issues"):
    """Every issue or PR (or with kind="repositories", repository) the GitHub search finds."""
    query = urllib.parse.quote(query)
    found, page = [], 1
    while True:
        batch = fetch(f"{GITHUB_API}/search/{kind}?q={query}&per_page=100&page={page}", headers)
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


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gitlab_web():
    return GITLAB_URL.rstrip("/").removesuffix("/api/v4")


def gitlab_api():
    return gitlab_web() + "/api/v4"


def gitlab_headers():
    # Bearer takes a personal, group or project access token, and an OAuth token too.
    return {"Authorization": f"Bearer {GITLAB_TOKEN}"}


def forgejo_api():
    return FORGEJO_URL.rstrip("/").removesuffix("/api/v1") + "/api/v1"


def github():
    """Open PRs by the Renovate app in GITHUB_OWNER's repositories, private ones included, and stale dashboards."""
    headers = github_headers()
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
    api, headers = gitlab_api(), gitlab_headers()
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
    api, headers = forgejo_api(), {"Authorization": f"token {FORGEJO_TOKEN}"}
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


def entry(forge, group, title, url, reasons, status, note=""):
    """A list entry that isn't a PR: its status line is given, not worked out from its age."""
    fields = {"forge": forge, "repo": group, "title": title, "url": url, "created": ""}
    return fields | {"reasons": reasons, "status": status, "note": note}


def gitlab_token_info():
    """The GitLab token's own details (name, scopes, expires_at); None if it isn't an access token."""
    try:
        return fetch(f"{gitlab_api()}/personal_access_tokens/self", gitlab_headers())
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            raise
        return None


def graphql(query, variables):
    """A GitLab GraphQL query's data; its errors raise."""
    url = gitlab_web() + "/api/graphql"
    data = json.dumps({"query": query, "variables": variables}).encode()
    answer = fetch(url, gitlab_headers() | {"Content-Type": "application/json"}, data=data)
    if answer.get("errors"):
        raise ValueError("; ".join(error.get("message", "?") for error in answer["errors"]))
    return answer["data"]


USAGE_QUERY = """query($path: ID!) {
  namespace(fullPath: $path) { id rootStorageStatistics { storageSize containerRegistrySize } }
}"""
MINUTES_QUERY = """query($id: NamespaceID, $month: Date) {
  ciMinutesUsage(namespaceId: $id, date: $month) { nodes { monthIso8601 minutes } }
}"""


def gitlab_usage():
    """GITLAB_NAMESPACE's CI minutes this month, used and projected, and its storage, against their limits."""
    info = gitlab_token_info()
    if info is not None and not {"api", "read_api"} & set(info.get("scopes", [])):
        print("GitLab usage skipped: GITLAB_TOKEN has no read_api scope (api works too).")
        return []
    try:
        namespace = graphql(USAGE_QUERY, {"path": GITLAB_NAMESPACE})["namespace"]
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        print(f"GitLab usage skipped: GraphQL answered HTTP {e.code}; GITLAB_TOKEN needs the read_api scope.")
        return []
    if not namespace:
        raise ValueError(f"namespace {GITLAB_NAMESPACE} not found")
    web = gitlab_web()
    user = "UserNamespace" in namespace["id"]
    quotas = f"{web}/-/profile/usage_quotas" if user else f"{web}/groups/{GITLAB_NAMESPACE}/-/usage_quotas"
    group, items = f"{GITLAB_NAMESPACE} usage quotas", []

    month = NOW.date().replace(day=1)
    nodes = graphql(MINUTES_QUERY, {"id": namespace["id"], "month": month.isoformat()})["ciMinutesUsage"]
    used = sum(node["minutes"] or 0 for node in (nodes or {}).get("nodes", []) if node["monthIso8601"] == str(month))
    following = (month + dt.timedelta(days=32)).replace(day=1)
    start = dt.datetime.combine(month, dt.time(), dt.UTC)
    elapsed = (NOW - start) / (dt.datetime.combine(following, dt.time(), dt.UTC) - start)
    projected = used / elapsed if (NOW - start).days >= GITLAB_MINUTES_PROJECT_AFTER_DAYS else None
    percent = 100 * used / GITLAB_MINUTES_QUOTA
    reasons = []
    if percent >= GITLAB_MINUTES_WARN:
        reasons.append(f"CI minutes over {GITLAB_MINUTES_WARN}%")
    if projected is not None and 100 * projected / GITLAB_MINUTES_QUOTA >= GITLAB_MINUTES_PROJECTED_WARN:
        reasons.append(f"CI minutes projected over {GITLAB_MINUTES_PROJECTED_WARN}%")
    title = f"CI minutes: {used} of {GITLAB_MINUTES_QUOTA} used ({percent:.0f}%)"
    if projected is not None:
        title += f", {projected:.0f} projected by month end ({100 * projected / GITLAB_MINUTES_QUOTA:.0f}%)"
    print(title)
    if reasons:
        items.append(entry("GitLab", group, title, f"{quotas}#pipelines-quota-tab", reasons, "; ".join(reasons)))

    storage = namespace["rootStorageStatistics"]
    if not storage:
        print("GitLab storage skipped: no storage statistics (the token's user must own the namespace).")
        return items
    size, registry = storage["storageSize"] / GIB, storage["containerRegistrySize"] / GIB
    percent = 100 * size / GITLAB_STORAGE_LIMIT_GIB
    title = f"Storage: {size:.1f} GiB of {GITLAB_STORAGE_LIMIT_GIB:g} GiB ({percent:.0f}%), container registry {registry:.1f} GiB"
    print(title)
    if percent >= GITLAB_STORAGE_WARN:
        reason = f"storage over {GITLAB_STORAGE_WARN}%"
        items.append(entry("GitLab", group, title, f"{quotas}#storage-quota-tab", [reason], reason))
    return items


def expiry_entry(forge, var, label, expires, url):
    """An entry for a token that expires on date expires within TOKEN_EXPIRY_DAYS, else None."""
    days = (expires - NOW.date()).days
    print(f"{var} expires {expires}, in {days} days")
    if days > TOKEN_EXPIRY_DAYS:
        return None
    when = f"expired {-days} day(s) ago" if days < 0 else f"expires in {days} day{'' if days == 1 else 's'}"
    reason = f"{var} expires within {TOKEN_EXPIRY_DAYS} days"
    return entry(forge, "access token", f"{label} expires {expires}", url, [reason], when)


def github_token_expiry():
    """GITHUB_TOKEN's expiry, from the header GitHub adds to an authenticated call; none without an expiry."""
    _, headers = fetch(f"{GITHUB_API}/rate_limit", github_headers(), with_headers=True)
    value = headers.get("github-authentication-token-expiration")
    if not value:
        print("GITHUB_TOKEN has no expiry date.")
        return []
    expires = dt.date.fromisoformat(value.strip()[:10])
    kind = "personal-access-tokens" if GITHUB_TOKEN.startswith("github_pat_") else "tokens"
    found = expiry_entry("GitHub", "GITHUB_TOKEN", "GITHUB_TOKEN", expires, f"https://github.com/settings/{kind}")
    return [found] if found else []


def gitlab_token_expiry():
    """GITLAB_TOKEN's expiry, from the token's own details."""
    info = gitlab_token_info()
    if info is None:
        print("GITLAB_TOKEN expiry skipped: it is not an access token GitLab can describe.")
        return []
    if not info.get("expires_at"):
        print("GITLAB_TOKEN has no expiry date.")
        return []
    web = gitlab_web()
    label = f"GITLAB_TOKEN ({info.get('name', '?')})"
    url = f"{web}/-/user_settings/personal_access_tokens"
    found = expiry_entry("GitLab", "GITLAB_TOKEN", label, dt.date.fromisoformat(info["expires_at"]), url)
    return [found] if found else []


def disabled_workflows():
    """Workflows that GitHub disabled for inactivity in GITHUB_OWNER's non-archived repositories."""
    headers = github_headers()
    items = []
    for repo in search(f"user:{GITHUB_OWNER} archived:false", headers, "repositories"):
        name = repo["full_name"]
        workflows = fetch(f"{GITHUB_API}/repos/{name}/actions/workflows?per_page=100", headers)["workflows"]
        for workflow in workflows:
            if workflow["state"] == "disabled_inactivity":
                status = "disabled after 60 days without activity: no scheduled runs until enabled again"
                items.append(entry("GitHub", name, workflow["name"], workflow["html_url"], ["disabled"], status))
    return items


def config_file():
    """HOLDS_FILE in HOLDS_REPO, read through HOLDS_FORGE's contents API, as (text, web URL)."""
    path = urllib.parse.quote(HOLDS_FILE)
    if HOLDS_FORGE == "github":
        found = fetch(f"{GITHUB_API}/repos/{HOLDS_REPO}/contents/{path}", github_headers())
        url = found["html_url"]
    elif HOLDS_FORGE == "gitlab":
        project, file = urllib.parse.quote(HOLDS_REPO, safe=""), urllib.parse.quote(HOLDS_FILE, safe="")
        found = fetch(f"{gitlab_api()}/projects/{project}/repository/files/{file}?ref=HEAD", gitlab_headers())
        url = f"{gitlab_web()}/{HOLDS_REPO}/-/blob/HEAD/{HOLDS_FILE}"
    else:
        headers = {"Authorization": f"token {FORGEJO_TOKEN}"}
        found = fetch(f"{forgejo_api()}/repos/{HOLDS_REPO}/contents/{path}", headers, FORGEJO_TIMEOUT)
        url = found["html_url"]
    return base64.b64decode(found["content"]).decode(), url


# The match fields that name what a rule applies to, the first one set naming it in the list.
MATCHERS = (
    "matchPackageNames",
    "matchDepNames",
    "matchPackagePatterns",
    "matchPackagePrefixes",
    "matchSourceUrls",
    "matchSourceUrlPrefixes",
)


def version_holds():
    """Every packageRules entry with allowedVersions in HOLDS_FILE (JSON, not JSON5), with its description."""
    text, url = config_file()
    items = []
    for n, rule in enumerate(json.loads(text).get("packageRules", [])):
        if "allowedVersions" not in rule:
            continue
        names = next((rule[m] for m in MATCHERS if rule.get(m)), [f"packageRules[{n}]"])
        names = [names] if isinstance(names, str) else names
        description = rule.get("description") or ""
        description = " ".join(description) if isinstance(description, list) else description
        title = f"{', '.join(names)}: allowedVersions {rule['allowedVersions']}"
        status = "version hold: still needed?"
        forge = FORGE_NAMES[HOLDS_FORGE]
        items.append(entry(forge, f"{HOLDS_REPO} {HOLDS_FILE}", title, url, ["hold"], status, description))
    return items


def forges():
    """The configured forges as (name, reader, what it reads); exits on a half-configured one."""
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
            found.append((name, reader, "PRs"))
    if not found:
        sys.exit("NEEDS_YOU needs GITHUB_TOKEN, GITLAB_TOKEN or FORGEJO_TOKEN.")
    return found


def checks():
    """The checks switched on, as (name, reader, what it reads); exits on one that lacks what it needs."""
    found = []
    if GITLAB_USAGE:
        if not GITLAB_TOKEN:
            sys.exit("GITLAB_USAGE needs GITLAB_TOKEN and GITLAB_NAMESPACE.")
        found.append(("GitLab usage", gitlab_usage, "entries"))
    if TOKEN_EXPIRY:
        if not (GITHUB_TOKEN or GITLAB_TOKEN):
            sys.exit("TOKEN_EXPIRY needs GITHUB_TOKEN or GITLAB_TOKEN.")
        if GITHUB_TOKEN:
            found.append(("GitHub token", github_token_expiry, "entries"))
        if GITLAB_TOKEN:
            found.append(("GitLab token", gitlab_token_expiry, "entries"))
    if DISABLED_WORKFLOWS:
        if not GITHUB_TOKEN:
            sys.exit("DISABLED_WORKFLOWS needs GITHUB_TOKEN and GITHUB_OWNER.")
        found.append(("GitHub workflows", disabled_workflows, "entries"))
    if VERSION_HOLDS:
        if HOLDS_FORGE not in FORGE_NAMES:
            sys.exit("HOLDS_FORGE is github, gitlab or forgejo.")
        token = {"github": GITHUB_TOKEN, "gitlab": GITLAB_TOKEN, "forgejo": FORGEJO_TOKEN}[HOLDS_FORGE]
        if not (HOLDS_REPO and token):
            sys.exit(f"VERSION_HOLDS needs HOLDS_REPO and {HOLDS_FORGE.upper()}_TOKEN.")
        found.append((HOLDS, version_holds, "holds"))
    return found


def keys(entry):
    return [f"{entry['url']} {reason}" for reason in entry["reasons"]]


def status(entry):
    """The entry's reasons and age, as listed."""
    if "status" in entry:  # not a PR: an extra check's finding
        return entry["status"]
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


def holds_due(state):
    """Whether the version holds go out today: every fourth Monday, on the first run after they're on, and
    on the run after one that failed to deliver them."""
    last = state.get("holds_last_sent")
    if last is None or state.get("holds_pending"):
        return True
    return NOW.weekday() == 0 and (NOW.date() - dt.date.fromisoformat(last)).days >= HOLDS_EVERY_DAYS


def run():
    """Check the forges and send the list if needed; return what failed, for the exit status."""
    readers = forges() + checks()
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    previous = state.get("items", {})
    read, errors, items = {}, [], []
    for name, reader, what in readers:
        try:
            read[name] = [entry for entry in reader() if entry]
        except (OSError, ValueError) as e:
            # Keep the last good read, so a forge that is down for a day
            # neither drops its PRs from the list nor brings them back as new.
            errors.append(f"{name} could not be read ({e}); its {what} are as of the last check.")
            read[name] = previous.get(name, [])
        if name != HOLDS:
            items += read[name]
    # The holds are no news on their own: they never count as new, and are
    # listed only on the days they are due.
    holds = read.get(HOLDS, [])
    holds_today = bool(holds) and holds_due(state)

    current = {key for entry in items for key in keys(entry)}
    sent_before = set(state.get("sent", []))
    new = current - sent_before
    today = NOW.date().isoformat()
    reminder = NOW.weekday() == 0 and state.get("last_sent") != today
    sent, delivered = sent_before & current, True
    listed = items + (holds if holds_today else [])
    what = "PR(s)" if all("updated" not in e and "status" not in e for e in listed) else "item(s)"
    if (items and (new or reminder)) or holds_today:
        fresh = sum(1 for entry in items if new & set(keys(entry)))
        if fresh:
            why = f", {fresh} new"
        elif reminder and items:
            why = " (Monday reminder)"
        else:
            why = " (version hold reminder)"
        title = f"Renovate: {len(listed)} {what} need you{why}"
        body = render(listed, new) + "".join(f"\n\n{error}" for error in errors)
        delivered = notify(APPRISE_URL, title, body, "info")
        if delivered:
            sent, state["last_sent"] = current, today
        if holds_today:
            state["holds_pending"] = not delivered
            if delivered:
                state["holds_last_sent"] = today
    holds_found = (f"Version hold: {e['title']}" for e in holds)
    urls = (e["url"] for e in items)
    print(f"{len(items)} {what} need you, {len(new)} new reason(s)", *urls, *holds_found, *errors, sep="\n")

    state.update(items=read, sent=sorted(sent))
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(STATE + ".tmp", STATE)
    failures = errors[:]
    if not delivered:
        failures.append("Apprise did not accept the needs-you message; it is retried next run.")
    return failures
