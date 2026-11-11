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


class Forges:
    """Stands in for urlopen: each URL answers with JSON, or with an HTTP error code."""

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
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {}, None)
        if isinstance(answer, Exception):
            raise answer
        return contextlib.nullcontext(io.BytesIO(json.dumps(answer).encode()))


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


def route_github(fake, prs, checks=None, statuses=None):
    """The search finds prs; checks and statuses map PR number to the head commit's check runs and statuses."""
    fake.routes[SEARCH] = {"total_count": len(prs), "incomplete_results": False, "items": prs}
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
