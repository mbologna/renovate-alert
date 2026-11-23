"""One report of the night: what went bad as an alert, what went well silently.

With REPORT=on, renovate-alert stops sending its own messages one by one and
sends a single report instead (docs/report.md), made of:

- what the Gatus instances in GATUS_URLS saw since the last report: checks
  failing now, checks that failed and recovered, and, from heartbeats pushed
  with a duration, how long each job took;
- the Renovate problems and the needs-you list this run found, which would
  otherwise each be a message of their own.

A failure that started and ended inside one of the QUIET_WINDOWS (local
times, REPORT_TZ), when checks fail on purpose (a nightly reboot slot), is
expected: it goes into the good report, not the bad one.

The bad report goes to APPRISE_URL as a warning, and only when something went
bad. The good report always goes, to REPORT_GOOD_URL (default APPRISE_URL)
with Apprise's tag `info`, which selects a silent notification in the configs
this was written for. The time of the last delivered report is kept in
STATE_DIR/report-state.json; a first run looks back REPORT_FIRST_HOURS.
REPORT_SINCE (a local HH:MM) caps how far back any run looks, so a morning
report covers the night and not the day before, already alerted on live.
"""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

from apprise_notify import notify

_REPORT = os.environ.get("REPORT", "off").lower()
if _REPORT not in ("on", "off"):
    sys.exit("REPORT is on or off.")
ENABLED = _REPORT == "on"
# name=url pairs, comma-separated: each a Gatus base URL.
GATUS_URLS = [
    tuple(part.strip() for part in pair.split("=", 1))
    for pair in os.environ.get("GATUS_URLS", "").split(",")
    if "=" in pair
]
APPRISE_URL = os.environ.get("APPRISE_URL", "")
GOOD_URL = os.environ.get("REPORT_GOOD_URL") or APPRISE_URL
TZ = zoneinfo.ZoneInfo(os.environ.get("REPORT_TZ", "UTC"))
FIRST_HOURS = int(os.environ.get("REPORT_FIRST_HOURS", "24"))


def clock(spec):
    """'03:00' -> (3, 0); '' -> None."""
    if not spec.strip():
        return None
    h, m = (int(x) for x in spec.strip().split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(spec)
    return h, m


try:
    SINCE = clock(os.environ.get("REPORT_SINCE", ""))
except ValueError:
    sys.exit("REPORT_SINCE is a local time, HH:MM.")
STATE_DIR = os.environ.get("STATE_DIR") or os.environ.get("LOG_DIR", "/logs")
STATE = os.path.join(STATE_DIR, os.environ.get("REPORT_STATE_FILE", "report-state.json"))
# Results per Gatus check. Gatus keeps 100 by default (storage
# maximum-number-of-results), about 8 hours of 5-minute checks; the events it
# keeps (50) reach further back and are what failures are read from.
PAGE_SIZE = int(os.environ.get("REPORT_PAGE_SIZE", "100"))


def windows(spec):
    """'05:15-05:55,13:00-13:10' -> [(315, 355), (780, 790)] minutes of the day."""
    out = []
    for part in spec.split(","):
        if part.strip():
            start, end = (int(h) * 60 + int(m) for h, m in (t.strip().split(":") for t in part.split("-")))
            out.append((start, end))
    return out


QUIET_WINDOWS = windows(os.environ.get("QUIET_WINDOWS", ""))
# A failure that recovered sooner than this is a blip: one failed check every
# so often is normal, and Gatus's own alert waits for several in a row. Blips
# are counted in the good report instead of listed in the bad one.
MIN_DOWN_MINUTES = int(os.environ.get("REPORT_MIN_DOWN_MINUTES", "10"))
NOW = dt.datetime.now(dt.UTC)

# What the other checks would have sent, collected instead while REPORT is on.
outbox: list[tuple[str, str]] = []
# Whether run() delivered the bad report (or had nothing bad to send).
delivered = True


def snapshot(paths):
    """The current content of each state file, None for a missing one."""
    out = {}
    for path in paths:
        try:
            with open(path) as f:
                out[path] = f.read()
        except FileNotFoundError:
            out[path] = None
    return out


def restore(saved):
    """Put state files back as snapshot() found them."""
    for path, content in saved.items():
        if content is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w") as f:
                f.write(content)


def send(deliver, url, title, body, type_="info"):
    """deliver() (the caller's notify), or, with REPORT=on, keep the message
    for the report."""
    if ENABLED:
        outbox.append((title, body))
        return True
    return deliver(url, title, body, type_)


def parse_time(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def local(when):
    return when.astimezone(TZ)


def quiet(start, end):
    """Did a failure from start to end fall inside one quiet window, the same day?"""
    a, b = local(start), local(end)
    if a.date() != b.date():
        return False
    minute = lambda t: t.hour * 60 + t.minute  # noqa: E731
    return any(lo <= minute(a) and minute(b) <= hi for lo, hi in QUIET_WINDOWS)


def hm(when):
    return local(when).strftime("%H:%M")


def span(seconds):
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310: GATUS_URLS, set by the operator
        return json.load(response)


def gatus(url):
    """Every check's status from one Gatus, with its results and events.

    The list of statuses carries no events, so each check's own status, which
    does, is read too."""
    base = url.rstrip("/")
    statuses = get_json(f"{base}/api/v1/endpoints/statuses?page=1&pageSize={PAGE_SIZE}")
    for status in statuses:
        key = urllib.parse.quote(status["key"], safe="")
        status.update(get_json(f"{base}/api/v1/endpoints/{key}/statuses?page=1&pageSize={PAGE_SIZE}"))
    return statuses


def why(result):
    """A failed result's errors, or the conditions it failed."""
    errors = [e for e in result.get("errors") or [] if e]
    if errors:
        return "; ".join(errors)[:200]
    failed = [c["condition"] for c in result.get("conditionResults") or [] if not c.get("success")]
    return ", ".join(failed)[:200] or "failed"


def look(name, statuses, since, bad, good):
    """Sort one Gatus's checks into the bad and good lists; return (checks, healthy)."""
    total = healthy = 0
    for status in statuses:
        label = f"{status.get('group') or name}/{status['name']}"
        results = status.get("results") or []
        if not results:
            continue
        total += 1
        last = results[-1]
        if last.get("success"):
            healthy += 1
        else:
            bad.append(f"{label} ({name}): failing since {failing_since(status, results)}: {why(last)}")
        # Failures that came and went since the last report.
        down = None
        for event in status.get("events") or []:
            when = parse_time(event["timestamp"])
            if event["type"] == "UNHEALTHY":
                down = when
            elif event["type"] == "HEALTHY" and down is not None:
                if when > since:
                    line = f"{label} ({name}): failed {hm(down)}-{hm(when)} ({span((when - down).total_seconds())})"
                    if quiet(down, when):
                        good["expected"].append(line)
                    elif when - down < dt.timedelta(minutes=MIN_DOWN_MINUTES):
                        good["blips"] += 1
                    else:
                        bad.append(line)
                down = None
        if status.get("group") == "heartbeats":
            for result in results:
                if result.get("success") and result.get("duration") and parse_time(result["timestamp"]) > since:
                    good["durations"][status["name"]] = span(result["duration"] / 1e9)
    return total, healthy


def failing_since(status, results):
    """When a check that is failing now went down, as best the history says."""
    events = [e for e in status.get("events") or [] if e["type"] == "UNHEALTHY"]
    if events:
        return hm(parse_time(events[-1]["timestamp"]))
    first = next((r for r in reversed(results) if r.get("success")), None)
    return f"after {hm(parse_time(first['timestamp']))}" if first else "at least the whole history"


def post_silent(urls, title, body):
    """POST one message with Apprise's tag `info` (a silent notification) to the
    first URL that takes it; notify() has no tag."""
    data = json.dumps({"title": title, "body": body, "type": "success", "tag": "info"}).encode()
    for url in urls.split():
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310: REPORT_GOOD_URL, set by the operator
                if response.status == 200:
                    return True
        except (OSError, urllib.error.HTTPError) as e:
            print(f"the good report to {url.split('/notify/')[0]} failed: {e}", file=sys.stderr)
    return False


def period_start(state):
    """The last report's time (or REPORT_FIRST_HOURS ago), but no earlier than
    the last time the local clock read REPORT_SINCE."""
    since = parse_time(state["last_report"]) if "last_report" in state else NOW - dt.timedelta(hours=FIRST_HOURS)
    if SINCE:
        now = local(NOW)
        floor = now.replace(hour=SINCE[0], minute=SINCE[1], second=0, microsecond=0)
        if floor > now:
            floor -= dt.timedelta(days=1)
        since = max(since, floor)
    return since


def run():
    """Build and send the report; return what failed, for the exit status."""
    global delivered
    try:
        with open(STATE) as f:
            state = json.load(f)
    except FileNotFoundError, json.JSONDecodeError:
        state = {}
    since = period_start(state)
    bad, good = [], {"expected": [], "durations": {}, "blips": 0}
    seen = []
    for name, url in GATUS_URLS:
        try:
            total, healthy = look(name, gatus(url), since, bad, good)
            seen.append(f"{healthy}/{total} on {name}")
        except (OSError, ValueError, KeyError) as e:
            bad.append(f"{name}'s Gatus could not be read ({e}), so its checks are missing from this report.")
    for title, body in outbox:
        bad.append(f"{title}\n{body}")

    period = f"since {local(since):%a %H:%M}"
    delivered = True
    if bad:
        title = f"Night report: {len(bad)} thing(s) went bad"
        delivered = notify(APPRISE_URL, title, "\n\n".join(f"- {line}" for line in bad), "warning")
    lines = [f"Checks healthy now: {', '.join(seen) or 'no Gatus read'}."]
    if good["durations"]:
        lines.append("Took: " + ", ".join(f"{k} {v}" for k, v in sorted(good["durations"].items())) + ".")
    if good["blips"]:
        lines.append(f"Brief blips (recovered within {MIN_DOWN_MINUTES} min): {good['blips']}.")
    if good["expected"]:
        lines.append("Expected (inside the quiet windows):\n" + "\n".join(f"- {line}" for line in good["expected"]))
    good_title = "Night report: all good" if not bad else f"Night report: the rest went well ({period})"
    good_sent = post_silent(GOOD_URL, good_title, "\n".join(lines)) if GOOD_URL else False
    print(good_title, *lines, *bad, sep="\n")

    if delivered:
        state["last_report"] = NOW.isoformat()
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE + ".tmp", "w") as f:
            json.dump(state, f, indent=1)
        os.replace(STATE + ".tmp", STATE)
    failures = [] if delivered else ["Apprise did not accept the bad report; the next run reports the same period."]
    if not good_sent:
        failures.append("Apprise did not accept the good report.")
    return failures
