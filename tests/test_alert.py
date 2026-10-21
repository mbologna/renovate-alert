import datetime as dt
import json
import os

import pytest

import renovate_alert as ra

DASHBOARD = """This issue lists Renovate updates.

## Repository Problems

 - WARN: Package lookup failures
 - WARN: minimumReleaseAge timestamp-optional, proceeding

## Rate-Limited

 - [ ] <!-- unlimit-branch=renovate/alpine-3.x -->chore(deps): update alpine to v3.23

## Pending Status Checks

 - [ ] <!-- approvePr-branch=renovate/foo-1.x -->[chore(deps): update foo to v1.2](pulls/12)
 - [ ] <!-- approvePr-branch=other/bar -->not a renovate branch
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(ra, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(ra, "STATE", str(log_dir / "alert-state.json"))
    sent = []
    monkeypatch.setattr(ra, "notify", lambda url, title, body, type_: sent.append((title, body)) or True)
    prs = []
    monkeypatch.setattr(ra, "get", lambda path: {"body": DASHBOARD} if path.startswith("issues/") else prs)
    return log_dir, sent, prs


def write_log(log_dir, text, age_hours=1):
    path = log_dir / "renovate-20261018-013000.log"
    path.write_text(text)
    t = ra.NOW.timestamp() - age_hours * 3600
    os.utime(path, (t, t))
    return path


def test_sections_and_items():
    secs = ra.sections(DASHBOARD)
    assert set(secs) == {"Repository Problems", "Rate-Limited", "Pending Status Checks"}
    assert ra.items(secs["Pending Status Checks"]) == {"renovate/foo-1.x": "update foo to v1.2"}
    assert ra.items(secs["Rate-Limited"]) == {"renovate/alpine-3.x": "update alpine to v3.23"}


def test_reports_log_and_dashboard_problems_once(env):
    log_dir, sent, _ = env
    write_log(log_dir, "x\nReached branch limit - skipping branch creation\nRenovate is exiting with a non-zero code\n")
    ra.main()
    assert len(sent) == 1
    title, body = sent[0]
    assert title == "Renovate (owner/infra): 3 new problem(s)"
    assert "hit branchConcurrentLimit and skipped 1" in body
    assert "exited with an error" in body
    assert "Package lookup failures" in body and "timestamp-optional" not in body
    assert body.endswith("Dashboard: https://forge.example/owner/infra/issues/7")
    ra.main()
    assert len(sent) == 1  # nothing new the second time


def test_repository_problems_are_keyed_per_line(env, monkeypatch):
    log_dir, sent, _ = env
    write_log(log_dir, "ok")
    dashboard = DASHBOARD
    monkeypatch.setattr(ra, "get", lambda path: {"body": dashboard} if path.startswith("issues/") else [])
    ra.main()
    assert "Dashboard Repository Problem: WARN: Package lookup failures" in sent[0][1]
    assert "repo-problem:WARN: Package lookup failures" in json.load(open(ra.STATE))["alerted"]

    # A second line appears: only it is sent, the first is not repeated.
    dashboard = DASHBOARD.replace(" - WARN: Package", " - WARN: Base branch does not exist\n - WARN: Package")
    ra.main()
    assert len(sent) == 2
    assert "Base branch does not exist" in sent[1][1] and "Package lookup failures" not in sent[1][1]

    # The new line clears: the remaining one is still not repeated.
    dashboard = DASHBOARD
    ra.main()
    assert len(sent) == 2


def test_stuck_items_count_from_first_seen(env):
    log_dir, sent, _ = env
    write_log(log_dir, "ok")
    old = (ra.NOW - dt.timedelta(days=40)).isoformat()
    with open(ra.STATE, "w") as f:
        json.dump({"first_seen": {"Pending Status Checks:renovate/foo-1.x": old}, "alerted": []}, f)
    ra.main()
    body = sent[0][1]
    assert "update foo to v1.2: pending (likely no release timestamp) for 40 days." in body
    assert "alpine" not in body  # rate-limited, but only first seen today


def test_stale_and_missing_logs(env):
    log_dir, sent, _ = env
    ra.main()
    assert "No Renovate run log" in sent[0][1]
    write_log(log_dir, "ok", age_hours=30)
    ra.main()
    assert "No Renovate run for 30h" in sent[1][1]


def test_old_automerge_pr(env):
    log_dir, sent, prs = env
    write_log(log_dir, "ok")
    created = (ra.NOW - dt.timedelta(days=9)).isoformat()
    prs.append({"number": 5, "title": "update x", "created_at": created, "labels": [{"name": "renovate-automerge"}]})
    prs.append({"number": 6, "title": "manual", "created_at": created, "labels": []})
    ra.main()
    body = sent[0][1]
    assert "PR #5 update x: automerge PR open for 9 days." in body
    assert "#6" not in body


def test_undelivered_digest_is_retried(env, monkeypatch):
    log_dir, _, _ = env
    write_log(log_dir, "Renovate is exiting with a non-zero code")
    monkeypatch.setattr(ra, "notify", lambda *a: False)
    with pytest.raises(SystemExit):
        ra.main()
    assert json.load(open(ra.STATE))["alerted"] == []
