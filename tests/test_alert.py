import datetime as dt
import gzip
import json
import os

import pytest

import renovate_alert as ra

DASHBOARD = """This issue lists Renovate updates.

## Repository Problems

 - WARN: Package lookup failures
 - WARN: minimumReleaseAge timestamp-optional, proceeding
 - WARN: Could not determine resolved version after updating package file; skipping pending-version check

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


# What Renovate 44 writes with LOG_FILE_FORMAT=pretty when a host error
# aborts the repository: the run still finishes and exits 0.
ABORTED_LOG = """ INFO: Repository started (repository=owner/infra)
 WARN: Host error (repository=owner/infra)
       "hostType": "gitlab-tags",
       "packageName": "gitlab-org/cli",
       "err": {"message": "connect ETIMEDOUT 172.65.251.78:443", "code": "ETIMEDOUT"}
 INFO: External host error causing abort - skipping (repository=owner/infra)
 INFO: Repository finished (repository=owner/infra)
       "cloned": true,
       "durationMs": 412345,
       "result": "external-host-error",
       "status": "unknown",
       "enabled": true,
       "onboarded": true
"""


def write_log(log_dir, text, age_hours=1, name="renovate-20261018-013000.log"):
    path = log_dir / name
    if name.endswith(".gz"):
        with gzip.open(path, "wt") as f:
            f.write(text)
    else:
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
    write_log(
        log_dir,
        "x\nBranches limit reached\nReached branch limit - skipping branch creation\nRenovate is exiting with a non-zero code\n",
    )
    ra.main()
    assert len(sent) == 1
    title, body = sent[0]
    assert title == "Renovate (owner/infra): 3 new problem(s)"
    assert "hit branchConcurrentLimit and skipped 1" in body
    assert "exited with an error" in body
    assert "Package lookup failures" in body
    assert "timestamp-optional" not in body and "Could not determine resolved version" not in body
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


# What Renovate 44 writes with LOG_FILE_FORMAT=pretty before each skipped
# branch: the line naming the limit, its fields, then the shared skip line.
def limit_log(*skips):
    out = []
    for message, field, branch in skips:
        ids = f"(repository=owner/dotfiles, branch={branch})"
        out += [
            f"DEBUG: {message} {ids}",
            f'       "{field}": 2,',
            f"DEBUG: Reached branch limit - skipping branch creation {ids}",
        ]
    return "\n".join(out) + "\n"


def test_branch_limit_skips_are_told_apart_and_deduplicated(env):
    log_dir, sent, _ = env
    branches = ("Branches limit reached", "currentCount", "renovate/fastfetch")
    hourly = ("Hourly PRs limit reached", "hourlyPrCount", "renovate/chezmoi")
    vulnerability = ("VulnerabilityBranches limit reached", "currentCount", "renovate/uv-vulnerability")
    write_log(log_dir, limit_log(branches, vulnerability), age_hours=2, name="renovate-20261023-053013.log.gz")
    write_log(log_dir, limit_log(branches, hourly), age_hours=1, name="renovate-20261023-062945.log")
    ra.main()
    body = sent[0][1]
    assert (
        "Run renovate-20261023-053013, renovate-20261023-062945 hit branchConcurrentLimit "
        "and skipped 2 new branch(es). Updates are arriving" in body
    )
    assert "Run renovate-20261023-062945 hit prHourlyLimit and skipped 1 new branch(es). An hourly cap" in body
    assert {"branch-limit", "branch-limit:prHourlyLimit"} <= set(json.load(open(ra.STATE))["alerted"])


def test_branch_limit_without_a_limit_line(env):
    log_dir, sent, _ = env
    write_log(log_dir, "Reached branch limit - skipping branch creation\n" * 2)
    ra.main()
    assert "hit an unidentified limit and skipped 2 new branch(es)." in sent[0][1]


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
    state = json.load(open(ra.STATE))
    assert state["alerted"] == []
    # The log stays unread, so the next run reads it again and reports it.
    assert state["logs_read_until"] < ra.NOW.timestamp() - 3600
    sent = []
    monkeypatch.setattr(ra, "notify", lambda url, title, body, type_: sent.append(body) or True)
    ra.main()
    assert "exited with an error" in sent[0]


def test_aborted_run_is_reported(env):
    log_dir, sent, _ = env
    write_log(log_dir, ABORTED_LOG, name="renovate-20261021-133000.log")
    ra.main()
    body = sent[0][1]
    assert "Renovate run renovate-20261021-133000 aborted on an external host error" in body
    assert "exited with an error" not in body  # Renovate exited 0


@pytest.mark.parametrize(
    "line",
    [
        " INFO: External host error causing abort - skipping (repository=owner/infra)",
        '       "result": "external-host-error",',
    ],
)
def test_either_abort_line_is_enough(env, line):
    log_dir, sent, _ = env
    write_log(log_dir, f" INFO: Repository started\n{line}\n INFO: Repository finished\n")
    ra.main()
    assert "aborted on an external host error" in sent[0][1]


def test_error_specific_exit_code(env):
    log_dir, sent, _ = env
    write_log(log_dir, " INFO: Renovate is exiting with an error-specific code due to a repository error\n")
    ra.main()
    assert "Renovate run renovate-20261018-013000 exited with an error." in sent[0][1]


def test_reads_every_log_since_the_last_check_including_rotated(env):
    log_dir, sent, _ = env
    # An earlier run, already gzipped by the next one, aborted; the newest is clean.
    write_log(log_dir, ABORTED_LOG, age_hours=12, name="renovate-20261021-013000.log.gz")
    write_log(
        log_dir,
        "Branches limit reached\nReached branch limit - skipping branch creation\n",
        age_hours=2,
        name="renovate-20261021-133000.log.gz",
    )
    newest = write_log(log_dir, " INFO: Repository finished\n", name="renovate-20261022-013000.log")
    ra.main()
    body = sent[0][1]
    assert "Renovate run renovate-20261021-013000 aborted" in body
    assert "Run renovate-20261021-133000 hit branchConcurrentLimit and skipped 1" in body
    assert json.load(open(ra.STATE))["logs_read_until"] == os.path.getmtime(newest)


def test_each_log_is_read_once(env):
    log_dir, sent, _ = env
    write_log(log_dir, ABORTED_LOG, age_hours=12, name="renovate-20261021-013000.log")
    ra.main()
    assert len(sent) == 1
    # Rotation keeps the mtime: the gzipped copy of a read log is not read again.
    plain = log_dir / "renovate-20261021-013000.log"
    write_log(log_dir, ABORTED_LOG, age_hours=12, name="renovate-20261021-013000.log.gz")
    plain.unlink()
    write_log(log_dir, "clean", name="renovate-20261022-013000.log")
    ra.main()
    assert len(sent) == 1
    assert "aborted" not in json.load(open(ra.STATE))["alerted"]
    # A later aborted run is a recurrence and alerts again.
    write_log(log_dir, ABORTED_LOG, age_hours=0, name="renovate-20261022-133000.log")
    ra.main()
    assert len(sent) == 2 and "renovate-20261022-133000 aborted" in sent[1][1]


def test_first_check_looks_back_run_stale_hours(env):
    log_dir, sent, _ = env
    write_log(log_dir, ABORTED_LOG, age_hours=30, name="renovate-20261020-013000.log.gz")
    write_log(log_dir, "clean", age_hours=20, name="renovate-20261020-133000.log")
    ra.main()
    assert "aborted" not in sent[0][1]  # the dashboard problem is still sent


def test_logs_only_mode_with_its_own_prefix(env, monkeypatch):
    # Without FORGE_API (a Renovate on GitLab): only its own run logs, no
    # dashboard or PR calls, and the title names it by NAME.
    log_dir, sent, _ = env
    monkeypatch.setattr(ra, "API", "")
    monkeypatch.setattr(ra, "NAME", "GitLab")
    monkeypatch.setattr(ra, "LOG_PREFIX", "gitlab-renovate-")
    monkeypatch.setattr(ra, "get", lambda path: pytest.fail(f"forge API called: {path}"))
    write_log(log_dir, ABORTED_LOG, name="gitlab-renovate-20261023-053000.log")
    write_log(log_dir, " INFO: Renovate is exiting with a non-zero code\n", name="renovate-20261023-011500.log")
    ra.main()
    title, body = sent[0]
    assert title == "Renovate (GitLab): 1 new problem(s)"
    assert "gitlab-renovate-20261023-053000 aborted" in body
    assert "exited with an error" not in body  # the other prefix's log is not read
    assert "Dashboard:" not in body


def test_logs_only_mode_reports_a_missing_run(env, monkeypatch):
    log_dir, sent, _ = env
    monkeypatch.setattr(ra, "API", "")
    monkeypatch.setattr(ra, "LOG_PREFIX", "gitlab-renovate-")
    write_log(log_dir, " INFO: done\n", age_hours=30, name="gitlab-renovate-20261021-053000.log")
    ra.main()
    assert "No Renovate run for 30h (newest log gitlab-renovate-20261021-053000.log)" in sent[0][1]
