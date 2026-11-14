import base64
import contextlib
import datetime as dt
import io
import json
import urllib.error

import pytest

import renovate_alert as ra
from renovate_alert import needs_you as ny

GH = "https://api.github.com"
GL = "https://gitlab.example/api/v4"
FJ = "https://forgejo.example/api/v1"
SEARCH = f"{GH}/search/issues?q=is%3Apr%20is%3Aopen%20author%3Aapp/renovate%20user%3Aowner&per_page=100&page=1"
DASHBOARD_SEARCH = (
    f"{GH}/search/issues?q=is%3Aissue%20is%3Aopen%20in%3Atitle%20%22Renovate%20Dependency%20Dashboard%22"
    "%20user%3Aowner&per_page=100&page=1"
)
DASHBOARD_TITLE = "🤖 Renovate Dependency Dashboard"
AWAITING_BODY = """This issue lists Renovate updates and detected dependencies.

## Awaiting Schedule

The following updates are awaiting their schedule. To get an update now, click on a checkbox below.

 - [ ] <!-- unschedule-branch=renovate/ruff-0.x -->chore(deps): update ruff to v0.9.1

## Detected Dependencies
"""
QUIET_BODY = """This issue lists Renovate updates and detected dependencies.

## Detected Dependencies
"""
# A Tuesday: no Monday reminder unless a test moves NOW.
TUESDAY = dt.datetime(2026, 11, 10, 13, 0, tzinfo=dt.UTC)
MANUAL_BODY = """This PR contains the following updates:

👤 **Why this waits for you:** a major update; read the release notes before merging.

---
"""


def ago(days):
    return (TUESDAY - dt.timedelta(days=days)).isoformat()


class Responses:
    """Answers to the same URL, one per call, in order."""

    def __init__(self, *answers):
        self.answers = list(answers)


class Reply(io.BytesIO):
    """A JSON answer with response headers."""

    def __init__(self, answer, headers=None):
        super().__init__(json.dumps(answer).encode())
        self.headers = headers or {}


class Forges:
    """Stands in for urlopen: each URL answers with JSON, or with an HTTP error code.

    An answer that is a function is called with the request, for URLs such as
    GitLab's GraphQL endpoint that answer by what is posted.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        if req.full_url not in self.routes:
            pytest.fail(f"unexpected request: {req.full_url}")
        answer = self.routes[req.full_url]
        if isinstance(answer, Responses):
            answer = answer.answers.pop(0)
        if callable(answer):
            answer = answer(req)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {}, None)
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(answer if isinstance(answer, Reply) else Reply(answer))


@pytest.fixture
def forges(tmp_path, monkeypatch):
    for name, value in {
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_OWNER": "owner",
        "GITLAB_TOKEN": "gl-token",
        "GITLAB_URL": "https://gitlab.example",
        "GITLAB_NAMESPACE": "owner",
        "FORGEJO_TOKEN": "fj-token",
        "FORGEJO_URL": "https://forgejo.example",
        "FORGEJO_REPOS": ["owner/site"],
        "AUTOMERGE_LABEL": "automerge",
        "STATE": str(tmp_path / "needs-you-state.json"),
        "NOW": TUESDAY,
        "RETRY_DELAY": 0,
    }.items():
        monkeypatch.setattr(ny, name, value)
    fake = Forges()
    monkeypatch.setattr(ny.urllib.request, "urlopen", fake)
    sent = []
    monkeypatch.setattr(ny, "notify", lambda url, title, body, type_: sent.append((title, body)) or True)
    return fake, sent


def github_pr(number, repo="owner/app", labels=("dependencies", "automerge"), days=3, draft=False, body=""):
    return {
        "number": number,
        "title": f"chore(deps): update thing {number}",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "repository_url": f"{GH}/repos/{repo}",
        "labels": [{"name": label} for label in labels],
        "created_at": ago(days).replace("+00:00", "Z"),
        "draft": draft,
        "body": body,
    }


def dashboard(repo="owner/app", title=DASHBOARD_TITLE, body=AWAITING_BODY, days=9):
    return {
        "number": 1,
        "title": title,
        "html_url": f"https://github.com/{repo}/issues/1",
        "repository_url": f"{GH}/repos/{repo}",
        "created_at": "2025-01-06T03:00:00Z",
        "updated_at": ago(days).replace("+00:00", "Z"),
        "body": body,
    }


def route_github(fake, prs, checks=None, statuses=None, dashboards=()):
    """The searches find prs and dashboards; checks and statuses map PR number to the head commit's check runs and
    statuses."""
    fake.routes[SEARCH] = {"total_count": len(prs), "incomplete_results": False, "items": prs}
    fake.routes[DASHBOARD_SEARCH] = {"total_count": len(dashboards), "incomplete_results": False, "items": dashboards}
    for pr in prs:
        if pr["draft"]:
            continue  # never asked about
        repo, n = pr["repository_url"].split("/repos/")[1], pr["number"]
        fake.routes[f"{GH}/repos/{repo}/pulls/{n}"] = {"head": {"sha": f"sha{n}"}}
        runs = (checks or {}).get(n, [{"name": "test", "status": "completed", "conclusion": "success"}])
        fake.routes[f"{GH}/repos/{repo}/commits/sha{n}/check-runs?per_page=100"] = {
            "total_count": len(runs),
            "check_runs": runs,
        }
        fake.routes[f"{GH}/repos/{repo}/commits/sha{n}/status?per_page=100"] = {
            "state": "success",
            "statuses": (statuses or {}).get(n, []),
        }


def route_gitlab(fake, mrs=(), pipelines=None, jobs=None):
    """A user namespace: the group listing 404s and the MRs come project by project."""
    fake.routes[f"{GL}/groups/owner/merge_requests?state=opened&per_page=100&page=1"] = 404
    fake.routes[f"{GL}/users/owner/projects?archived=false&simple=true&per_page=100&page=1"] = [{"id": 11}]
    fake.routes[f"{GL}/projects/11/merge_requests?state=opened&per_page=100&page=1"] = list(mrs)
    for mr in mrs:
        fake.routes[f"{GL}/projects/11/merge_requests/{mr['iid']}"] = mr | {
            "head_pipeline": (pipelines or {}).get(mr["iid"])
        }
    for pipeline, found in (jobs or {}).items():
        fake.routes[f"{GL}/projects/11/pipelines/{pipeline}/jobs?scope%5B%5D=failed&per_page=100&page=1"] = found


def gitlab_mr(iid, branch="renovate/python-3.x", labels=("dependencies", "manual-review"), days=1, draft=False):
    return {
        "iid": iid,
        "project_id": 11,
        "title": f"chore(deps): update python {iid}",
        "web_url": f"https://gitlab.example/owner/app/-/merge_requests/{iid}",
        "references": {"full": f"owner/app!{iid}"},
        "source_branch": branch,
        "labels": list(labels),
        "created_at": ago(days),
        "draft": draft,
        "description": "",
    }


def route_forgejo(fake, prs=(), combined=None):
    fake.routes[f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=1"] = list(prs)
    for pr in prs:
        sha = pr["head"]["sha"]
        fake.routes[f"{FJ}/repos/owner/site/commits/{sha}/status?limit=50"] = (combined or {}).get(
            pr["number"], {"state": "success", "statuses": []}
        )


def forgejo_pr(number, ref="renovate/traefik-3.x", labels=("dependencies", "automerge"), days=2, body=""):
    return {
        "number": number,
        "title": f"chore(deps): update traefik {number}",
        "html_url": f"https://forgejo.example/owner/site/pulls/{number}",
        "head": {"ref": ref, "sha": f"fsha{number}"},
        "labels": [{"name": label} for label in labels],
        "created_at": ago(days).replace("+00:00", "+01:00"),
        "draft": False,
        "body": body,
    }


def only(monkeypatch, *keep):
    """Leave only the named forges configured."""
    for forge, var in (("GitHub", "GITHUB_TOKEN"), ("GitLab", "GITLAB_TOKEN"), ("Forgejo", "FORGEJO_TOKEN")):
        if forge not in keep:
            monkeypatch.setattr(ny, var, "")


def test_github_manual_review_and_failed_checks(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    prs = [
        github_pr(1, labels=("dependencies", "manual-review", "major"), days=9, body=MANUAL_BODY),
        github_pr(2, repo="owner/site"),  # automerge, failing check run and status
        github_pr(3),  # automerge, green: not listed
        github_pr(4, labels=("dependencies",)),  # no automerge label: manual review
        github_pr(5, labels=("dependencies", "manual-review"), draft=True),  # draft: ignored
        github_pr(6),  # automerge, still running and cancelled: not listed
    ]
    route_github(
        fake,
        prs,
        checks={
            2: [
                {"name": "lint", "status": "completed", "conclusion": "failure"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ],
            6: [
                {"name": "test", "status": "in_progress", "conclusion": None},
                {"name": "old", "status": "completed", "conclusion": "cancelled"},
            ],
        },
        statuses={2: [{"context": "ci/build", "state": "error"}], 6: [{"context": "renovate", "state": "pending"}]},
    )
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 3 PR(s) need you, 3 new"
    # Grouped by repository, oldest first, with the PR note for a held update.
    assert body == (
        "GitHub owner/app\n"
        "- chore(deps): update thing 1\n"
        "  manual review, open 9 days (new)\n"
        "  Why this waits for you: a major update; read the release notes before merging.\n"
        "  https://github.com/owner/app/pull/1\n"
        "- chore(deps): update thing 4\n"
        "  manual review, open 3 days (new)\n"
        "  https://github.com/owner/app/pull/4\n"
        "\n"
        "GitHub owner/site\n"
        "- chore(deps): update thing 2\n"
        "  CI failed: ci/build, lint, open 3 days (new)\n"
        "  https://github.com/owner/site/pull/2"
    )
    req = fake.requests[0][0]
    assert req.get_header("Authorization") == "Bearer gh-token"


@pytest.mark.parametrize("code", [403, 404])
def test_github_falls_back_to_workflow_runs_when_check_runs_are_unreadable(forges, monkeypatch, code):
    # A fine-grained token: no permission covers check runs, Actions read covers workflow runs.
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(2), github_pr(3)], statuses={2: [{"context": "ci/build", "state": "failure"}]})
    for n, runs in (
        (2, [{"name": "CI", "conclusion": "failure"}, {"name": "Docs", "conclusion": "success"}]),
        (3, [{"name": "CI", "conclusion": None}, {"name": "Old", "conclusion": "cancelled"}]),
    ):
        fake.routes[f"{GH}/repos/owner/app/commits/sha{n}/check-runs?per_page=100"] = code
        fake.routes[f"{GH}/repos/owner/app/actions/runs?head_sha=sha{n}&per_page=100"] = {
            "total_count": len(runs),
            "workflow_runs": runs,
        }
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 1 PR(s) need you, 1 new"
    assert "  CI failed: CI, ci/build, open 3 days (new)\n  https://github.com/owner/app/pull/2" in body


@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure", "action_required"])
def test_github_workflow_run_conclusions_that_fail(forges, monkeypatch, conclusion):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(2)])
    fake.routes[f"{GH}/repos/owner/app/commits/sha2/check-runs?per_page=100"] = 403
    fake.routes[f"{GH}/repos/owner/app/actions/runs?head_sha=sha2&per_page=100"] = {
        "total_count": 1,
        "workflow_runs": [{"name": "Release", "conclusion": conclusion}],
    }
    ny.run()
    assert "CI failed: Release" in sent[0][1]


def test_github_other_check_run_errors_fail_the_forge(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(2)])
    fake.routes[f"{GH}/repos/owner/app/commits/sha2/check-runs?per_page=100"] = 401
    assert ny.run() == ["GitHub could not be read (HTTP Error 401: error); its PRs are as of the last check."]
    assert sent == []


def test_github_order_and_layout(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(1, labels=("manual-review",), days=1), github_pr(7, labels=(), days=5)])
    ny.run()
    assert sent[0][1] == (
        "GitHub owner/app\n"
        "- chore(deps): update thing 7\n"
        "  manual review, open 5 days (new)\n"
        "  https://github.com/owner/app/pull/7\n"
        "- chore(deps): update thing 1\n"
        "  manual review, open 1 day (new)\n"
        "  https://github.com/owner/app/pull/1"
    )


def test_github_stale_dashboard(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    dashboards = [
        dashboard("owner/app"),  # awaiting schedule, 9 days without a change: flagged
        dashboard("owner/quiet", body=QUIET_BODY, days=60),  # nothing awaiting: a quiet repo, not flagged
        dashboard("owner/fresh", days=2),  # awaiting, but visited 2 days ago: not flagged
        dashboard("owner/other", title="Renovate Dependency Dashboard (old)"),  # the search also finds this
        dashboard("owner/null", body=None, days=30),
    ]
    route_github(fake, [github_pr(1, labels=("manual-review",))], dashboards=dashboards)
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 2 item(s) need you, 2 new"
    assert body == (
        "GitHub owner/app\n"
        f"- {DASHBOARD_TITLE}\n"
        "  Renovate hasn't visited in 9 days (updates awaiting schedule) (new)\n"
        "  https://github.com/owner/app/issues/1\n"
        "- chore(deps): update thing 1\n"
        "  manual review, open 3 days (new)\n"
        "  https://github.com/owner/app/pull/1"
    )

    # A day later it is still unvisited: the growing count is no news.
    monkeypatch.setattr(ny, "NOW", TUESDAY + dt.timedelta(days=1))
    ny.run()
    assert len(sent) == 1
    assert json.load(open(ny.STATE))["sent"] == [
        "https://github.com/owner/app/issues/1 not visited by Renovate",
        "https://github.com/owner/app/pull/1 manual review",
    ]

    # Renovate visits and opens the PRs: the dashboard drops off; the next miss is news again.
    monkeypatch.setattr(ny, "NOW", TUESDAY)
    route_github(fake, [github_pr(1, labels=("manual-review",))], dashboards=[dashboard(days=0)])
    ny.run()
    route_github(fake, [github_pr(1, labels=("manual-review",))], dashboards=[dashboard(days=8)])
    ny.run()
    assert len(sent) == 2
    assert "Renovate hasn't visited in 8 days (updates awaiting schedule) (new)" in sent[1][1]


@pytest.mark.parametrize(
    ("days", "stale_days", "flagged"),
    [(8, 8, True), (7, 8, False), (12, 14, False), (400, 0, False)],
)
def test_github_dashboard_stale_days(forges, monkeypatch, days, stale_days, flagged):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    monkeypatch.setattr(ny, "DASHBOARD_STALE_DAYS", stale_days)
    route_github(fake, [], dashboards=[dashboard(days=days)])
    ny.run()
    assert bool(sent) == flagged
    if not stale_days:  # 0 turns the check off: no search at all
        assert all("is%3Aissue" not in req.full_url for req, _ in fake.requests)


def test_github_dashboard_title_is_configurable(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    monkeypatch.setattr(ny, "DASHBOARD_TITLE", "Renovate Dependency Dashboard")
    route_github(
        fake, [], dashboards=[dashboard("owner/a"), dashboard("owner/b", title="Renovate Dependency Dashboard")]
    )
    ny.run()
    assert "GitHub owner/b\n" in sent[0][1]
    assert "owner/a" not in sent[0][1]


def test_gitlab_user_namespace_failed_pipeline(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitLab")
    mrs = [
        gitlab_mr(1, labels=("dependencies", "automerge")),  # failed pipeline
        gitlab_mr(2, labels=("dependencies", "automerge")),  # running pipeline: not listed
        gitlab_mr(3),  # manual review, no pipeline
        gitlab_mr(4, branch="feat/thing"),  # not Renovate's
        gitlab_mr(5, draft=True),
        gitlab_mr(6, labels=("dependencies", "automerge")),  # failed, only allowed failures left
    ]
    route_gitlab(
        fake,
        mrs,
        pipelines={
            1: {"id": 101, "status": "failed"},
            2: {"id": 102, "status": "running"},
            6: {"id": 106, "status": "failed"},
        },
        jobs={
            101: [{"name": "check", "allow_failure": False}, {"name": "flaky", "allow_failure": True}],
            106: [{"name": "flaky", "allow_failure": True}],
        },
    )
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 3 PR(s) need you, 3 new"
    assert body.startswith("GitLab owner/app\n")
    assert "update python 1\n  CI failed: check, open 1 day (new)" in body
    assert "update python 3\n  manual review, open 1 day" in body
    assert "update python 6\n  CI failed: pipeline 106" in body
    for skipped in ("python 2", "python 4", "python 5"):
        assert skipped not in body
    assert fake.requests[0][0].get_header("Authorization") == "Bearer gl-token"


def test_gitlab_group_namespace(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitLab")
    mr = gitlab_mr(1) | {"references": {"full": "owner/sub/app!1"}}
    fake.routes[f"{GL}/groups/owner/merge_requests?state=opened&per_page=100&page=1"] = [mr]
    fake.routes[f"{GL}/projects/11/merge_requests/1"] = mr | {"head_pipeline": None}
    ny.run()
    assert sent[0][1].startswith("GitLab owner/sub/app\n- chore(deps): update python 1\n  manual review")


def test_forgejo_failed_status_and_pagination(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "Forgejo")
    page1 = [forgejo_pr(n) for n in range(100, 150)]
    page2 = [
        forgejo_pr(1),  # failed commit status
        forgejo_pr(2, labels=("dependencies", "manual-review"), body="**Why this waits for you**: k3s minor."),
        forgejo_pr(3, ref="feature/x", labels=()),  # not Renovate's
    ]
    fake.routes[f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=1"] = page1
    fake.routes[f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=2"] = page2
    for pr in page1 + page2:
        fake.routes[f"{FJ}/repos/owner/site/commits/{pr['head']['sha']}/status?limit=50"] = {
            "state": "pending",
            "statuses": None,
        }
    fake.routes[f"{FJ}/repos/owner/site/commits/fsha1/status?limit=50"] = {
        "state": "failure",
        "statuses": [
            {"context": "ci / lint (pull_request)", "status": "failure"},
            {"context": "ci / test (pull_request)", "status": "success"},
        ],
    }
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 2 PR(s) need you, 2 new"
    assert "update traefik 1\n  CI failed: ci / lint (pull_request), open 2 days (new)" in body
    assert "update traefik 2\n  manual review, open 2 days (new)\n  Why this waits for you: k3s minor." in body
    assert {timeout for _, timeout in fake.requests} == {120}
    assert fake.requests[0][0].get_header("Authorization") == "token fj-token"


def test_github_search_pages(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    prs = [github_pr(n) for n in range(1, 102)]
    route_github(fake, prs, checks={101: [{"name": "lint", "conclusion": "timed_out"}]})
    fake.routes[SEARCH] = {"total_count": 101, "items": prs[:100]}
    fake.routes[SEARCH.removesuffix("1") + "2"] = {"total_count": 101, "items": prs[100:]}
    ny.run()
    assert sent[0][0] == "Renovate: 1 PR(s) need you, 1 new"
    assert "update thing 101\n  CI failed: lint" in sent[0][1]


def test_gitlab_timeout_is_retried_and_other_errors_raise(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitLab")
    route_gitlab(fake, [gitlab_mr(1)])
    url = f"{GL}/projects/11/merge_requests?state=opened&per_page=100&page=1"
    fake.routes[url] = Responses(urllib.error.URLError(TimeoutError("timed out")), [gitlab_mr(1)])
    assert ny.run() == []
    assert "update python 1" in sent[0][1]
    # A 403 on the group listing is not a user namespace: the forge counts as down.
    fake.routes[f"{GL}/groups/owner/merge_requests?state=opened&per_page=100&page=1"] = 403
    assert ny.run() == ["GitLab could not be read (HTTP Error 403: error); its PRs are as of the last check."]


def test_forgejo_504_is_retried_once(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "Forgejo")
    pr = forgejo_pr(1, labels=("manual-review",))
    route_forgejo(fake, [pr])
    url = f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=1"
    fake.routes[url] = Responses(504, [pr])
    assert ny.run() == []
    assert "update traefik 1" in sent[0][1]


def test_a_forge_down_keeps_its_last_list_and_fails_the_run(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub", "Forgejo")
    route_github(fake, [github_pr(1, labels=("manual-review",))])
    route_forgejo(fake, [forgejo_pr(2, labels=("manual-review",))])
    ny.run()
    assert len(sent) == 1

    # Codeberg times out twice: its PR stays on the list, not new, and the run fails.
    fake.routes[f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=1"] = Responses(504, 504)
    failures = ny.run()
    assert len(sent) == 1
    assert failures == ["Forgejo could not be read (HTTP Error 504: error); its PRs are as of the last check."]

    # Something new on GitHub meanwhile: the message still lists the Forgejo PR, and says why.
    route_github(fake, [github_pr(1, labels=("manual-review",)), github_pr(3, labels=("manual-review",))])
    fake.routes[f"{FJ}/repos/owner/site/pulls?state=open&limit=50&page=1"] = Responses(504, 504)
    ny.run()
    title, body = sent[1]
    assert title == "Renovate: 3 PR(s) need you, 1 new"
    assert "update traefik 2\n  manual review, open 2 days\n" in body
    assert body.endswith("Forgejo could not be read (HTTP Error 504: error); its PRs are as of the last check.")


def test_sent_once_then_only_when_something_is_new(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(1, labels=("manual-review",))])
    ny.run()
    ny.run()
    assert len(sent) == 1  # same list: not repeated
    state = json.load(open(ny.STATE))
    assert state["sent"] == ["https://github.com/owner/app/pull/1 manual review"]

    # The same PR's CI goes red: a new reason is news.
    route_github(
        fake, [github_pr(1, labels=("manual-review",))], checks={1: [{"name": "lint", "conclusion": "failure"}]}
    )
    ny.run()
    assert len(sent) == 2
    assert sent[1][0] == "Renovate: 1 PR(s) need you, 1 new"
    assert "manual review; CI failed: lint, open 3 days (new)" in sent[1][1]

    # CI is fixed: a shorter list is no news, and nothing is sent.
    route_github(fake, [github_pr(1, labels=("manual-review",))])
    ny.run()
    assert len(sent) == 2

    # It fails again: news again.
    route_github(
        fake, [github_pr(1, labels=("manual-review",))], checks={1: [{"name": "lint", "conclusion": "failure"}]}
    )
    ny.run()
    assert len(sent) == 3


def test_nothing_to_send_when_no_pr_needs_you(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(1)])
    monkeypatch.setattr(ny, "NOW", TUESDAY - dt.timedelta(days=1))  # Monday
    assert ny.run() == []
    assert sent == []
    assert json.load(open(ny.STATE))["sent"] == []


def test_monday_reminder(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(1, labels=("manual-review",))])
    monkeypatch.setattr(ny, "NOW", TUESDAY - dt.timedelta(days=8))  # the Monday before
    ny.run()
    assert sent[0][0] == "Renovate: 1 PR(s) need you, 1 new"
    for day in range(7, 1, -1):  # Tuesday to Sunday: nothing new
        monkeypatch.setattr(ny, "NOW", TUESDAY - dt.timedelta(days=day))
        ny.run()
    assert len(sent) == 1
    monday = TUESDAY - dt.timedelta(days=1)
    monkeypatch.setattr(ny, "NOW", monday)
    ny.run()
    ny.run()  # a second run the same Monday (a Job retry) doesn't repeat it
    assert len(sent) == 2
    title, body = sent[1]
    assert title == "Renovate: 1 PR(s) need you (Monday reminder)"
    assert "(new)" not in body
    assert json.load(open(ny.STATE))["last_sent"] == monday.date().isoformat()


def test_undelivered_message_is_retried(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    route_github(fake, [github_pr(1, labels=("manual-review",))])
    monkeypatch.setattr(ny, "notify", lambda *a: False)
    assert ny.run() == ["Apprise did not accept the needs-you message; it is retried next run."]
    assert json.load(open(ny.STATE))["sent"] == []
    monkeypatch.setattr(ny, "notify", lambda url, title, body, type_: sent.append(body) or True)
    ny.run()
    assert "update thing 1" in sent[0]


def test_half_configured_forge_exits(forges, monkeypatch):
    monkeypatch.setattr(ny, "FORGEJO_REPOS", [])
    with pytest.raises(SystemExit, match="Forgejo's token is set but FORGEJO_REPOS is not"):
        ny.run()
    only(monkeypatch)
    with pytest.raises(SystemExit, match="NEEDS_YOU needs"):
        ny.run()


@pytest.mark.parametrize(
    ("body", "note"),
    [
        ("Why this waits for you: a major update.", "Why this waits for you: a major update."),
        ("> 👤 **Why this waits for you:** k3s minor.", "Why this waits for you: k3s minor."),
        ("- why this waits for you - GitHub Actions.", "why this waits for you - GitHub Actions."),
        ("Nothing about it.\nThis is why this waits for you.", ""),
        (None, ""),
    ],
)
def test_why_note(body, note):
    assert ny.why_note(body) == note


@pytest.fixture
def logs(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(ra, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(ra, "STATE", str(log_dir / "alert-state.json"))
    monkeypatch.setattr(ra, "API", "")
    calls = []
    monkeypatch.setattr(ra, "check_renovate", lambda: calls.append("renovate") or [])
    monkeypatch.setattr(ny, "run", lambda: calls.append("needs-you") or [])
    return calls


@pytest.mark.parametrize(
    ("mode", "checks"),
    [("off", ["renovate"]), ("on", ["renovate", "needs-you"]), ("only", ["needs-you"])],
)
def test_needs_you_mode(logs, monkeypatch, mode, checks):
    monkeypatch.setattr(ra, "NEEDS_YOU", mode)
    ra.main()
    assert logs == checks


def test_failures_from_both_checks_fail_the_run(logs, monkeypatch):
    monkeypatch.setattr(ra, "NEEDS_YOU", "on")
    monkeypatch.setattr(ra, "check_renovate", lambda: ["digest undelivered"])
    monkeypatch.setattr(ny, "run", lambda: ["GitLab could not be read"])
    with pytest.raises(SystemExit, match="digest undelivered\nGitLab could not be read"):
        ra.main()


# The optional checks. They add entries to the same message, with the same rules.

GRAPHQL = "https://gitlab.example/api/graphql"
TOKEN_SELF = f"{GL}/personal_access_tokens/self"
USER_NS = "gid://gitlab/Namespaces::UserNamespace/42"
GIB = 1024**3


def route_usage(fake, minutes=0, storage=1 * GIB, registry=0, namespace_id=USER_NS, scopes=("api",)):
    """GitLab answers the token's details and both GraphQL queries; minutes is this month's use."""
    route_gitlab(fake)
    fake.routes[TOKEN_SELF] = {"name": "alerts", "scopes": list(scopes), "expires_at": None}
    month = ny.NOW.date().replace(day=1).isoformat()

    def graphql(req):
        body = json.loads(req.data)
        if "ciMinutesUsage" in body["query"]:
            assert body["variables"] == {"id": namespace_id, "month": month}
            nodes = [{"monthIso8601": month, "minutes": minutes}]
            return {"data": {"ciMinutesUsage": {"nodes": nodes}}}
        assert body["variables"] == {"path": "owner"}
        stats = {"storageSize": float(storage), "containerRegistrySize": float(registry)}
        return {"data": {"namespace": {"id": namespace_id, "rootStorageStatistics": stats}}}

    fake.routes[GRAPHQL] = graphql


@pytest.fixture
def usage(forges, monkeypatch):
    only(monkeypatch, "GitLab")
    monkeypatch.setattr(ny, "GITLAB_USAGE", True)
    return forges


def test_gitlab_usage_minutes_and_storage(usage):
    fake, sent = usage
    # 9 days and 13 hours into a 30-day month: 150 minutes project to 471.
    route_usage(fake, minutes=150 + 150, storage=4.5 * GIB, registry=4 * GIB)
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 2 item(s) need you, 2 new"
    assert body == (
        "GitLab owner usage quotas\n"
        "- CI minutes: 300 of 400 used (75%), 943 projected by month end (236%)\n"
        "  CI minutes over 75%; CI minutes projected over 100% (new)\n"
        "  https://gitlab.example/-/profile/usage_quotas#pipelines-quota-tab\n"
        "- Storage: 4.5 GiB of 5 GiB (90%), container registry 4.0 GiB\n"
        "  storage over 80% (new)\n"
        "  https://gitlab.example/-/profile/usage_quotas#storage-quota-tab"
    )
    graphql = next(req for req, _ in fake.requests if req.full_url == GRAPHQL)
    assert graphql.get_header("Authorization") == "Bearer gl-token"

    # The numbers grow the next day, but the reasons are the same: no news.
    route_usage(fake, minutes=320, storage=4.6 * GIB)
    ny.run()
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("minutes", "reasons"),
    [
        (100, []),  # 25%, 314 projected (79%)
        (150, ["CI minutes projected over 100%"]),  # 38%, 471 projected
        (300, ["CI minutes over 75%", "CI minutes projected over 100%"]),
    ],
)
def test_gitlab_minutes_thresholds(usage, minutes, reasons):
    fake, sent = usage
    route_usage(fake, minutes=minutes)
    ny.run()
    assert json.load(open(ny.STATE))["sent"] == [
        f"https://gitlab.example/-/profile/usage_quotas#pipelines-quota-tab {reason}" for reason in reasons
    ]


def test_gitlab_minutes_thresholds_are_configurable(usage, monkeypatch):
    fake, sent = usage
    for name, value in {
        "GITLAB_MINUTES_QUOTA": 1000,
        "GITLAB_MINUTES_WARN": 50,
        "GITLAB_MINUTES_PROJECTED_WARN": 200,
        "GITLAB_STORAGE_LIMIT_GIB": 10,
        "GITLAB_STORAGE_WARN": 40,
    }.items():
        monkeypatch.setattr(ny, name, value)
    route_usage(fake, minutes=600, storage=4.5 * GIB)
    ny.run()
    assert (
        "CI minutes: 600 of 1000 used (60%), 1886 projected by month end (189%)\n  CI minutes over 50% (new)"
        in (sent[0][1])
    )
    assert "Storage: 4.5 GiB of 10 GiB (45%), container registry 0.0 GiB\n  storage over 40%" in sent[0][1]


def test_gitlab_minutes_not_projected_early_in_the_month(usage, monkeypatch, capsys):
    fake, sent = usage
    monkeypatch.setattr(ny, "NOW", dt.datetime(2026, 11, 3, 13, 0, tzinfo=dt.UTC))
    route_usage(fake, minutes=60)  # would project to 677
    ny.run()
    assert sent == []
    assert "CI minutes: 60 of 400 used (15%)\n" in capsys.readouterr().out


def test_gitlab_group_namespace_usage_link(usage):
    fake, sent = usage
    route_usage(fake, storage=5 * GIB, namespace_id="gid://gitlab/Group/7")
    ny.run()
    assert "https://gitlab.example/groups/owner/-/usage_quotas#storage-quota-tab" in sent[0][1]


def test_gitlab_usage_needs_read_api(usage, capsys):
    fake, sent = usage
    route_usage(fake, minutes=400, scopes=("read_user", "read_repository"))
    assert ny.run() == []
    assert sent == []
    assert all(req.full_url != GRAPHQL for req, _ in fake.requests)
    assert "GITLAB_TOKEN has no read_api scope" in capsys.readouterr().out


def test_gitlab_usage_graphql_refused(usage, capsys):
    fake, sent = usage
    route_usage(fake)
    fake.routes[TOKEN_SELF] = 404  # not a personal access token: GraphQL decides
    fake.routes[GRAPHQL] = 403
    assert ny.run() == []
    assert "GITLAB_TOKEN needs the read_api scope" in capsys.readouterr().out


def test_gitlab_usage_errors_keep_the_last_read(usage):
    fake, sent = usage
    route_usage(fake, storage=5 * GIB)
    ny.run()
    fake.routes[GRAPHQL] = lambda req: {"errors": [{"message": "Internal server error"}]}
    assert ny.run() == ["GitLab usage could not be read (Internal server error); its entries are as of the last check."]
    assert len(sent) == 1
    assert json.load(open(ny.STATE))["sent"] == [
        "https://gitlab.example/-/profile/usage_quotas#storage-quota-tab storage over 80%"
    ]


@pytest.fixture
def expiry(forges, monkeypatch):
    only(monkeypatch, "GitHub", "GitLab")
    monkeypatch.setattr(ny, "TOKEN_EXPIRY", True)
    fake, sent = forges
    route_github(fake, [], dashboards=[])
    route_gitlab(fake)
    return forges


@pytest.mark.parametrize(
    ("github", "gitlab", "listed"),
    [
        ("2026-11-24 10:00:00 UTC", "2026-12-10", ["GITHUB_TOKEN expires 2026-11-24", "GITLAB_TOKEN (alerts)"]),
        ("2026-12-11 08:27:43 +0100", "2026-12-11", []),  # 31 days
        (None, None, []),  # no expiry
    ],
)
def test_token_expiry(expiry, github, gitlab, listed, capsys):
    fake, sent = expiry
    headers = {"github-authentication-token-expiration": github} if github else {}
    fake.routes[f"{GH}/rate_limit"] = Reply({"resources": {}}, headers)
    fake.routes[TOKEN_SELF] = {"name": "alerts", "scopes": ["read_api"], "expires_at": gitlab}
    assert ny.run() == []
    if not listed:
        assert sent == []
        return
    title, body = sent[0]
    assert title == "Renovate: 2 item(s) need you, 2 new"
    assert body == (
        "GitHub access token\n"
        "- GITHUB_TOKEN expires 2026-11-24\n"
        "  expires in 14 days (new)\n"
        "  https://github.com/settings/tokens\n"
        "\n"
        "GitLab access token\n"
        "- GITLAB_TOKEN (alerts) expires 2026-12-10\n"
        "  expires in 30 days (new)\n"
        "  https://gitlab.example/-/user_settings/personal_access_tokens"
    )
    assert "gh-token" not in capsys.readouterr().out


def test_token_expiry_days_is_configurable(expiry, monkeypatch):
    fake, sent = expiry
    only(monkeypatch, "GitLab")
    monkeypatch.setattr(ny, "TOKEN_EXPIRY_DAYS", 60)
    fake.routes[TOKEN_SELF] = {"name": "alerts", "scopes": ["api"], "expires_at": "2027-01-01"}
    ny.run()
    assert json.load(open(ny.STATE))["sent"] == [
        "https://gitlab.example/-/user_settings/personal_access_tokens GITLAB_TOKEN expires within 60 days"
    ]


def test_token_expiry_skips_a_token_gitlab_cannot_describe(expiry, monkeypatch, capsys):
    fake, sent = expiry
    only(monkeypatch, "GitLab")
    fake.routes[TOKEN_SELF] = 401
    assert ny.run() == []
    assert "GITLAB_TOKEN expiry skipped" in capsys.readouterr().out


REPO_SEARCH = f"{GH}/search/repositories?q=user%3Aowner%20archived%3Afalse&per_page=100&page=1"


def test_disabled_workflows(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "GitHub")
    monkeypatch.setattr(ny, "DISABLED_WORKFLOWS", True)
    route_github(fake, [], dashboards=[])
    fake.routes[REPO_SEARCH] = {"total_count": 2, "items": [{"full_name": "owner/app"}, {"full_name": "owner/site"}]}
    fake.routes[f"{GH}/repos/owner/app/actions/workflows?per_page=100"] = {
        "total_count": 2,
        "workflows": [
            {"name": "CI", "state": "active", "html_url": "https://github.com/owner/app/actions/workflows/ci.yml"},
            {
                "name": "Nightly",
                "state": "disabled_inactivity",
                "html_url": "https://github.com/owner/app/actions/workflows/nightly.yml",
            },
            {
                "name": "Old",
                "state": "disabled_manually",
                "html_url": "https://github.com/owner/app/actions/workflows/old.yml",
            },
        ],
    }
    fake.routes[f"{GH}/repos/owner/site/actions/workflows?per_page=100"] = {"total_count": 0, "workflows": []}
    assert ny.run() == []
    title, body = sent[0]
    assert title == "Renovate: 1 item(s) need you, 1 new"
    assert body == (
        "GitHub owner/app\n"
        "- Nightly\n"
        "  disabled after 60 days without activity: no scheduled runs until enabled again (new)\n"
        "  https://github.com/owner/app/actions/workflows/nightly.yml"
    )


RENOVATE_JSON = {
    "extends": ["config:recommended"],
    "packageRules": [
        {"matchPackageNames": ["example/server"], "automerge": True},
        {
            "description": "example/server: held at 2.4.x until its clients support 2.5.",
            "matchDepNames": ["registry.example/example/server"],
            "allowedVersions": "/^2\\.4\\./",
        },
        {"description": ["Kept on", "the LTS line."], "matchPackageNames": "example/db", "allowedVersions": "<17"},
        {"matchManagers": ["dockerfile"], "allowedVersions": "!/-rc/"},
    ],
}
CONTENTS = f"{FJ}/repos/owner/infra/contents/renovate.json"
HOLDS_BODY = """Forgejo owner/infra renovate.json
- registry.example/example/server: allowedVersions /^2\\.4\\./
  version hold: still needed?
  example/server: held at 2.4.x until its clients support 2.5.
  https://forgejo.example/owner/infra/src/branch/main/renovate.json
- example/db: allowedVersions <17
  version hold: still needed?
  Kept on the LTS line.
  https://forgejo.example/owner/infra/src/branch/main/renovate.json
- packageRules[3]: allowedVersions !/-rc/
  version hold: still needed?
  https://forgejo.example/owner/infra/src/branch/main/renovate.json"""


@pytest.fixture
def holds(forges, monkeypatch):
    fake, sent = forges
    only(monkeypatch, "Forgejo")
    monkeypatch.setattr(ny, "VERSION_HOLDS", True)
    monkeypatch.setattr(ny, "HOLDS_REPO", "owner/infra")
    route_forgejo(fake)
    fake.routes[CONTENTS] = {
        "content": base64.b64encode(json.dumps(RENOVATE_JSON).encode()).decode(),
        "encoding": "base64",
        "html_url": "https://forgejo.example/owner/infra/src/branch/main/renovate.json",
    }
    return forges


def at(monkeypatch, day):
    monkeypatch.setattr(ny, "NOW", dt.datetime.combine(day, dt.time(13), dt.UTC))


def test_version_holds_every_fourth_monday(holds, monkeypatch):
    fake, sent = holds
    # The first run after they're switched on lists them, whatever the day.
    assert ny.run() == []
    assert sent == [("Renovate: 3 item(s) need you (version hold reminder)", HOLDS_BODY)]
    assert json.load(open(ny.STATE))["sent"] == []  # never news on their own
    monday = TUESDAY.date() + dt.timedelta(days=6)
    for day in range(1, 28):  # the next days, and the next three Mondays: nothing
        at(monkeypatch, TUESDAY.date() + dt.timedelta(days=day))
        ny.run()
    assert len(sent) == 1
    at(monkeypatch, monday + dt.timedelta(days=21))  # 27 days since: not yet
    ny.run()
    assert len(sent) == 1
    at(monkeypatch, monday + dt.timedelta(days=28))  # 34 days since
    ny.run()
    ny.run()  # a second run the same day doesn't repeat it
    assert len(sent) == 2
    assert sent[1][0] == "Renovate: 3 item(s) need you (version hold reminder)"
    assert all(req.get_header("Authorization") == "token fj-token" for req, _ in fake.requests)


def test_version_holds_join_a_message_only_when_due(holds, monkeypatch):
    fake, sent = holds
    ny.run()
    route_forgejo(fake, [forgejo_pr(2, labels=("manual-review",))])
    ny.run()  # a new PR the next run: the holds aren't due, so not listed
    assert sent[1][0] == "Renovate: 1 PR(s) need you, 1 new"
    assert "allowedVersions" not in sent[1][1]
    # Four weeks on, Monday: the reminder lists both.
    at(monkeypatch, TUESDAY.date() + dt.timedelta(days=34))
    ny.run()
    title, body = sent[2]
    assert title == "Renovate: 4 item(s) need you (Monday reminder)"
    assert body.startswith(
        HOLDS_BODY + "\n\nForgejo owner/site\n- chore(deps): update traefik 2\n  manual review, open 36 days\n"
    )


def test_version_holds_undelivered_are_retried(holds, monkeypatch):
    fake, sent = holds
    ny.run()
    monday = TUESDAY.date() + dt.timedelta(days=34)
    at(monkeypatch, monday)
    monkeypatch.setattr(ny, "notify", lambda *a: False)
    assert ny.run() == ["Apprise did not accept the needs-you message; it is retried next run."]
    assert json.load(open(ny.STATE))["holds_last_sent"] == TUESDAY.date().isoformat()
    # The next run, a Tuesday, sends them.
    at(monkeypatch, monday + dt.timedelta(days=1))
    monkeypatch.setattr(ny, "notify", lambda url, title, body, type_: sent.append((title, body)) or True)
    ny.run()
    assert sent[1][0] == "Renovate: 3 item(s) need you (version hold reminder)"
    at(monkeypatch, monday + dt.timedelta(days=2))
    ny.run()
    assert len(sent) == 2


def test_version_holds_none_sends_nothing(holds):
    fake, sent = holds
    config = {"packageRules": [{"matchPackageNames": ["x"], "automerge": True}]}
    fake.routes[CONTENTS]["content"] = base64.b64encode(json.dumps(config).encode()).decode()
    assert ny.run() == []
    assert sent == []


@pytest.mark.parametrize(
    ("forge", "url", "header"),
    [
        ("github", f"{GH}/repos/owner/infra/contents/renovate.json", "Bearer gh-token"),
        ("gitlab", f"{GL}/projects/owner%2Finfra/repository/files/renovate.json?ref=HEAD", "Bearer gl-token"),
    ],
)
def test_version_holds_on_other_forges(holds, monkeypatch, forge, url, header):
    fake, sent = holds
    monkeypatch.setattr(ny, "HOLDS_FORGE", forge)
    monkeypatch.setattr(ny, f"{forge.upper()}_TOKEN", header.split()[1])
    if forge == "github":
        route_github(fake, [], dashboards=[])
    else:
        route_gitlab(fake)
    content = base64.b64encode(json.dumps(RENOVATE_JSON).encode()).decode()
    fake.routes[url] = {"content": content, "html_url": "https://github.com/owner/infra/blob/main/renovate.json"}
    ny.run()
    assert sent[0][1].startswith(f"{ny.FORGE_NAMES[forge]} owner/infra renovate.json\n- registry.example")
    assert next(req for req, _ in fake.requests if req.full_url == url).get_header("Authorization") == header


def test_version_holds_unreadable_keep_the_last_read(holds, monkeypatch):
    fake, sent = holds
    ny.run()
    fake.routes[CONTENTS] = Responses(404)
    at(monkeypatch, TUESDAY.date() + dt.timedelta(days=34))
    failures = ny.run()
    assert failures == ["Version holds could not be read (HTTP Error 404: error); its holds are as of the last check."]
    assert sent[1][0] == "Renovate: 3 item(s) need you (version hold reminder)"


@pytest.mark.parametrize(
    ("switch", "unset", "message"),
    [
        ("GITLAB_USAGE", ["GITLAB_TOKEN"], "GITLAB_USAGE needs GITLAB_TOKEN"),
        ("TOKEN_EXPIRY", ["GITHUB_TOKEN", "GITLAB_TOKEN"], "TOKEN_EXPIRY needs GITHUB_TOKEN or GITLAB_TOKEN"),
        ("DISABLED_WORKFLOWS", ["GITHUB_TOKEN"], "DISABLED_WORKFLOWS needs GITHUB_TOKEN"),
        ("VERSION_HOLDS", [], "VERSION_HOLDS needs HOLDS_REPO and FORGEJO_TOKEN"),
    ],
)
def test_checks_missing_what_they_need_exit(forges, monkeypatch, switch, unset, message):
    monkeypatch.setattr(ny, switch, True)
    for var in unset:
        monkeypatch.setattr(ny, var, "")
    with pytest.raises(SystemExit, match=message):
        ny.run()


def test_holds_forge_must_be_known(forges, monkeypatch):
    monkeypatch.setattr(ny, "VERSION_HOLDS", True)
    monkeypatch.setattr(ny, "HOLDS_FORGE", "bitbucket")
    with pytest.raises(SystemExit, match="HOLDS_FORGE is github, gitlab or forgejo"):
        ny.run()


@pytest.mark.parametrize(("value", "on"), [(None, False), ("off", False), ("on", True), ("ON", True)])
def test_switch(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("SOME_CHECK", raising=False)
    else:
        monkeypatch.setenv("SOME_CHECK", value)
    assert ny.switch("SOME_CHECK") is on


def test_switch_rejects_other_values(monkeypatch):
    monkeypatch.setenv("SOME_CHECK", "yes")
    with pytest.raises(SystemExit, match="SOME_CHECK is on or off"):
        ny.switch("SOME_CHECK")
