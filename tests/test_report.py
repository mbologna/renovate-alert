import datetime as dt
import io
import json
import urllib.error
import zoneinfo

import pytest

import renovate_alert as ra
from renovate_alert import needs_you as ny
from renovate_alert import report as rpt

# 2026-11-24 06:00 Rome (CET): the morning after a night chain.
NOW = dt.datetime(2026, 11, 24, 5, 0, tzinfo=dt.UTC)
ROME = zoneinfo.ZoneInfo("Europe/Rome")


def at(hm, day=24):
    """A UTC timestamp for HH:MM Rome time (CET) on 2026-11-<day>."""
    h, m = map(int, hm.split(":"))
    return dt.datetime(2026, 11, day, h, m, tzinfo=ROME).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def ok(hm, duration=None, day=24):
    r = {"success": True, "timestamp": at(hm, day)}
    if duration:
        r["duration"] = duration
    return r


def ko(hm, errors=None, conditions=None, day=24):
    return {
        "success": False,
        "timestamp": at(hm, day),
        "errors": errors or [],
        "conditionResults": conditions or [],
    }


def event(kind, hm, day=24):
    return {"type": kind, "timestamp": at(hm, day)}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(rpt, "NOW", NOW)
    monkeypatch.setattr(rpt, "TZ", ROME)
    monkeypatch.setattr(rpt, "QUIET_WINDOWS", rpt.windows("05:15-05:55"))
    monkeypatch.setattr(rpt, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(rpt, "STATE", str(tmp_path / "report-state.json"))
    monkeypatch.setattr(rpt, "APPRISE_URL", "http://apprise.example/notify/alerts")
    monkeypatch.setattr(rpt, "GOOD_URL", "http://apprise.example/notify/alerts")
    monkeypatch.setattr(rpt, "outbox", [])
    monkeypatch.setattr(rpt, "delivered", True)


@pytest.fixture
def sent(monkeypatch):
    """The bad report (notify) and the good one (post_silent)."""
    out = {"bad": [], "good": []}
    monkeypatch.setattr(rpt, "notify", lambda url, title, body, type_: out["bad"].append((title, body, type_)) or True)
    monkeypatch.setattr(rpt, "post_silent", lambda urls, title, body: out["good"].append((title, body)) or True)
    return out


def serve(monkeypatch, instances):
    """GATUS_URLS answering with these statuses, by name; an exception raises."""
    monkeypatch.setattr(rpt, "GATUS_URLS", [(name, f"http://{name}.example") for name in instances])

    def fake(url):
        answer = instances[url.removeprefix("http://").removesuffix(".example")]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rpt, "gatus", fake)


def test_windows_parse():
    assert rpt.windows("05:15-05:55, 13:00-13:10") == [(315, 355), (780, 790)]
    assert rpt.windows("") == []


def test_quiet_needs_one_window_on_one_day():
    start = dt.datetime(2026, 11, 24, 5, 21, tzinfo=ROME)
    assert rpt.quiet(start, start + dt.timedelta(minutes=8))
    assert not rpt.quiet(start, start + dt.timedelta(minutes=40))  # ends after 05:55
    assert not rpt.quiet(start - dt.timedelta(days=1), start)  # spans two days


def test_span():
    assert rpt.span(90) == "2 min"
    assert rpt.span(3 * 3600) == "3.0 h"


def test_send_collects_with_report_on(monkeypatch):
    monkeypatch.setattr(rpt, "ENABLED", True)
    assert rpt.send(lambda *a: pytest.fail("sent"), "u", "T", "B")
    assert rpt.outbox == [("T", "B")]


def test_send_delivers_with_report_off(monkeypatch):
    monkeypatch.setattr(rpt, "ENABLED", False)
    calls = []
    assert rpt.send(lambda *a: calls.append(a) or True, "u", "T", "B", "warning")
    assert calls == [("u", "T", "B", "warning")] and rpt.outbox == []


def test_all_good_sends_only_the_silent_report(monkeypatch, sent):
    statuses = [
        {"name": "Apprise (hotel)", "group": "alerting", "results": [ok("05:30")], "events": []},
        {
            "name": "backup-yankee",
            "group": "heartbeats",
            "results": [ok("03:58", duration=37 * 60 * 10**9)],
            "events": [],
        },
    ]
    serve(monkeypatch, {"hotel": statuses})
    assert rpt.run() == []
    assert sent["bad"] == []
    title, body = sent["good"][0]
    assert title == "Night report: all good"
    assert "Checks healthy now: 2/2 on hotel." in body
    assert "Took: backup-yankee 37 min." in body
    assert json.loads(open(rpt.STATE).read())["last_report"] == NOW.isoformat()


def test_failures_sort_into_bad_and_expected(monkeypatch, sent):
    statuses = [
        # Rebooted at 05:22, back at 05:27: expected.
        {
            "name": "Jellyfin",
            "group": "public",
            "results": [ok("05:30")],
            "events": [event("UNHEALTHY", "05:22"), event("HEALTHY", "05:27")],
        },
        # Down 01:00-01:05: a blip, counted, not listed.
        {
            "name": "Navidrome",
            "group": "public",
            "results": [ok("05:30")],
            "events": [event("UNHEALTHY", "01:00"), event("HEALTHY", "01:05")],
        },
        # Down at 02:10 for 25 min: bad.
        {
            "name": "Radarr",
            "group": "yankee",
            "results": [ok("05:30")],
            "events": [event("UNHEALTHY", "02:10"), event("HEALTHY", "02:35")],
        },
        # Down now.
        {
            "name": "Kavita",
            "group": "yankee",
            "results": [ok("04:00"), ko("05:50", conditions=[{"condition": "[STATUS] == 200", "success": False}])],
            "events": [event("UNHEALTHY", "05:45")],
        },
        # A missed heartbeat: Gatus's error, no events kept.
        {
            "name": "pins-pull-oscar",
            "group": "heartbeats",
            "results": [ko("05:59", errors=["no update received in 26h"])],
            "events": [],
        },
        # Recovered before the last report: old news.
        {
            "name": "Old",
            "group": "x",
            "results": [ok("05:30")],
            "events": [event("UNHEALTHY", "01:00", day=22), event("HEALTHY", "01:05", day=22)],
        },
        {"name": "Empty", "group": "x", "results": [], "events": []},
    ]
    serve(monkeypatch, {"yankee": statuses})
    with open(rpt.STATE, "w") as f:
        json.dump({"last_report": at("05:50", day=23)}, f)
    rpt.run()
    title, body, kind = sent["bad"][0]
    assert kind == "warning" and title == "Night report: 3 thing(s) went bad"
    assert "yankee/Radarr (yankee): failed 02:10-02:35 (25 min)" in body
    assert "yankee/Kavita (yankee): failing since 05:45: [STATUS] == 200" in body
    assert "heartbeats/pins-pull-oscar (yankee): failing since at least the whole history: no update received" in body
    assert "Old" not in body
    good_title, good_body = sent["good"][0]
    assert good_title.startswith("Night report: the rest went well (since Mon 05:50")
    assert "public/Jellyfin (yankee): failed 05:22-05:27 (5 min)" in good_body
    assert "Checks healthy now: 4/6 on yankee." in good_body
    assert "Brief blips (recovered within 10 min): 1." in good_body
    assert "Navidrome" not in body


def test_failing_since_falls_back_on_the_last_success(monkeypatch, sent):
    statuses = [{"name": "A", "group": "g", "results": [ok("04:00"), ko("05:00")], "events": []}]
    serve(monkeypatch, {"hotel": statuses})
    rpt.run()
    assert "failing since after 04:00: failed" in sent["bad"][0][1]


def test_an_unreadable_gatus_is_bad_news(monkeypatch, sent):
    serve(monkeypatch, {"hotel": urllib.error.URLError("refused")})
    rpt.run()
    assert "hotel's Gatus could not be read" in sent["bad"][0][1]
    assert "no Gatus read" in sent["good"][0][1]


def test_the_checks_messages_join_the_bad_report(monkeypatch, sent):
    serve(monkeypatch, {})
    monkeypatch.setattr(rpt, "outbox", [("Renovate (infra): 1 new problem(s)", "- a problem")])
    rpt.run()
    assert "Renovate (infra): 1 new problem(s)\n- a problem" in sent["bad"][0][1]


def test_an_undelivered_report_keeps_the_period(monkeypatch):
    serve(monkeypatch, {})
    monkeypatch.setattr(rpt, "outbox", [("T", "B")])
    monkeypatch.setattr(rpt, "notify", lambda *a: False)
    monkeypatch.setattr(rpt, "post_silent", lambda *a: False)
    failures = rpt.run()
    assert len(failures) == 2 and not rpt.delivered
    with pytest.raises(FileNotFoundError):
        open(rpt.STATE)


def test_snapshot_and_restore(tmp_path):
    kept, made = tmp_path / "a.json", tmp_path / "b.json"
    kept.write_text("old")
    saved = rpt.snapshot([str(kept), str(made)])
    kept.write_text("new")
    made.write_text("new")
    rpt.restore(saved)
    assert kept.read_text() == "old" and not made.exists()


def test_main_restores_the_checks_state_when_the_report_fails(monkeypatch, tmp_path):
    state = tmp_path / "alert-state.json"
    state.write_text('{"alerted": []}')
    monkeypatch.setattr(ra, "STATE", str(state))
    monkeypatch.setattr(ny, "STATE", str(tmp_path / "needs-you-state.json"))
    monkeypatch.setattr(ra, "NEEDS_YOU", "off")
    monkeypatch.setattr(rpt, "ENABLED", True)

    def check():
        state.write_text('{"alerted": ["x"]}')
        return []

    def failed_report():
        rpt.delivered = False
        return ["undelivered"]

    monkeypatch.setattr(ra, "check_renovate", check)
    monkeypatch.setattr(rpt, "run", failed_report)
    with pytest.raises(SystemExit):
        ra.main()
    assert state.read_text() == '{"alerted": []}'


class Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_post_silent_sends_the_info_tag_and_fails_over(monkeypatch):
    posted = []

    def urlopen(request, timeout):
        posted.append((request.full_url, json.loads(request.data)))
        if "first" in request.full_url:
            raise urllib.error.URLError("down")
        return Reply(b"{}")

    monkeypatch.setattr(rpt.urllib.request, "urlopen", urlopen)
    assert rpt.post_silent("http://first.example/notify/a http://second.example/notify/a", "T", "B")
    assert [u for u, _ in posted] == ["http://first.example/notify/a", "http://second.example/notify/a"]
    assert posted[1][1] == {"title": "T", "body": "B", "type": "success", "tag": "info"}


def test_post_silent_reports_failure(monkeypatch):
    monkeypatch.setattr(rpt.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert not rpt.post_silent("http://a.example/notify/a", "T", "B")


def test_gatus_reads_each_checks_events(monkeypatch):
    seen = []
    answers = {
        "statuses": b'[{"name": "A (x)", "key": "g_a-(x)", "results": [{"success": true}]}]',
        "g_a-%28x%29": b'{"name": "A (x)", "key": "g_a-(x)", "results": [], "events": [{"type": "START"}]}',
    }

    def urlopen(request, timeout):
        seen.append(request.full_url)
        return Reply(answers[request.full_url.split("/api/v1/endpoints/")[1].split("/")[0].split("?")[0]])

    monkeypatch.setattr(rpt.urllib.request, "urlopen", urlopen)
    statuses = rpt.gatus("http://gatus.example/")
    assert statuses == [{"name": "A (x)", "key": "g_a-(x)", "results": [], "events": [{"type": "START"}]}]
    assert seen == [
        f"http://gatus.example/api/v1/endpoints/statuses?page=1&pageSize={rpt.PAGE_SIZE}",
        f"http://gatus.example/api/v1/endpoints/g_a-%28x%29/statuses?page=1&pageSize={rpt.PAGE_SIZE}",
    ]
