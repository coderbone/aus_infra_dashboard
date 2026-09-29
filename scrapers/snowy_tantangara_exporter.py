#!/usr/bin/env python3
"""Prometheus exporter for Snowy Hydro scheme reservoir levels, focused on
Tantangara Reservoir - the upper storage of the Snowy Hydro 2.0 pumped scheme.

Source: https://www.snowyhydro.com.au/our-project/lake-levels/ , whose chart is
backed by a plain PHP include on the same origin:

    https://www.snowyhydro.com.au/wp-content/themes/snowyhydro/inc/getData.php
        ?yearA=<first year>&yearB=<last year>

It answers with the whole requested range as JSON, keyed
`year -> snowyhydro -> level[]`, one row per day. Every row carries a `lake[]`
array of `-name` / `-dataTimestamp` / `#text`, where `#text` is percent of gross
storage. Tantangara Reservoir is one of three lakes published.

**This is water data, not construction progress.** Snowy Hydro 2.0 publishes no
machine-readable project status at all - no progress dashboard, no TBM tracker,
nothing. Tantangara drawdown is the closest live proxy that exists for "is the
2.0 site active", because the 2.0 intake works at the upper storage. See
NOTES.md for the audit that established this, and do not describe these series
as project progress.

Exposed metrics
    snowy_tantangara_level_percent{reservoir}                 published level, % full
    snowy_tantangara_level_change_7d_percentage_points{reservoir}  7-day change
    snowy_tantangara_level_min_ytd_percent{reservoir}          calendar-year low
    snowy_tantangara_level_max_ytd_percent{reservoir}          calendar-year high
    snowy_tantangara_last_sample_timestamp_seconds{reservoir}  observation time
    snowy_tantangara_scrape_success                            1/0
    snowy_tantangara_scrape_duration_seconds                   last scrape wall time
    snowy_tantangara_last_scrape_timestamp_seconds             Unix time of last scrape

Units, because the two easy mistakes are "%" and "percentage points": the feed
publishes percent of gross storage, and the 7-day change is a difference of two
percents, i.e. percentage points. Never metres - the upstream is percent only
and no volume is published anywhere on this endpoint.

The whole requested range is fetched in one request and reduced locally, so the
7-day delta and the year-to-date extremes cost nothing extra. Two years are
requested by default because a single year cannot produce a 7-day delta for the
first week of January, and a metric that goes NaN exactly when the year rolls
over is a metric somebody pages about.

snowy_tantangara_scrape_success is 0 if *any* requested reservoir failed, so a
partial scrape is still reported as a failure. There is deliberately no
`config_stale` metric and no persistent cache: this exporter has no discovery
step and no configuration to cache, and the measurement gauges are never
served from a cache, so a feed that cannot be read simply loses its series for
that scrape rather than repeating a stale level as if it were current.

Usage
    snowy_tantangara_exporter.py                # serve /metrics on 127.0.0.1:9110
    snowy_tantangara_exporter.py --once         # print exposition to stdout and exit
    snowy_tantangara_exporter.py --listen-address 0.0.0.0 --port 9110
    snowy_tantangara_exporter.py --all-reservoirs

Standard library only.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_DATA_URL = (
    "https://www.snowyhydro.com.au/wp-content/themes/snowyhydro/inc/getData.php"
)

# The only reservoir in this feed that Snowy Hydro 2.0 actually touches. The
# other two (Lake Jindabyne, Lake Eucumbene) are pre-2.0 scheme infrastructure
# and are context, not project status, so they are opt-in via --all-reservoirs.
DEFAULT_RESERVOIR = "Tantangara Reservoir"

# The endpoint's year range is clamped server-side to [1954, <current year>], and
# the upper bound tracked the current year when checked on 2026-09-29. Requesting
# a pinned year therefore starts failing with HTTP 400 once the clock moves on,
# so the range is derived from the clock every scrape.
MIN_YEAR = 1954

# How many years to request, including the current one. 2 is the minimum that
# makes a 7-day delta work in early January.
DEFAULT_YEARS_BACK = 1

# The feed publishes naive local Sydney timestamps ("2026-09-29T07:00:00", no
# offset, no zone) at a fixed 07:00 each day. Australia/Sydney is UTC+10 in
# winter and UTC+11 in summer, and this exporter must not grow a zoneinfo
# dependency: python:3.12-alpine ships no tzdata, so ZoneInfo("Australia/Sydney")
# would raise ZoneInfoNotFoundError in the container. Pinning AEST costs at most
# one hour of error during daylight saving, which is irrelevant for a metric
# whose only consumer is `time() - last_sample_timestamp_seconds` against a
# multi-hour staleness threshold, and irrelevant for a once-daily series.
LOCAL_UTC_OFFSET_HOURS = 10

# An error body is a single short line ("Error: yearA and yearB must be between
# 1954 and 2026"), but the cap keeps a misrouted HTML error page from bloating
# every log line.
MAX_ERROR_BODY = 200

RETRY_BASE_DELAY = 0.5

log = logging.getLogger("snowy_tantangara_exporter")


class ScrapeError(Exception):
    """Raised when the upstream cannot be read or understood."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


# Same set as wht_tbm_exporter: the faults a second attempt a moment later can
# plausibly clear. The one that actually bites inside a container is EAI_AGAIN
# (-3, "Try again") from getaddrinfo, which Docker's embedded resolver at
# 127.0.0.11 raises intermittently when forwarding to systemd-resolved.
# HTTP error responses are deliberately absent - the server answered, so an
# immediate retry is unlikely to differ.
TRANSIENT_ERRNOS = frozenset(
    {
        socket.EAI_AGAIN,  # -3, only in socket, not errno
        errno.EAGAIN,
        errno.ECONNRESET,
        errno.ECONNABORTED,
    }
)


def is_transient(reason: object) -> bool:
    """True for faults a second attempt a moment later can plausibly clear."""
    if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(reason, "errno", None) in TRANSIENT_ERRNOS


def describe_http_error(exc: urllib.error.HTTPError) -> str:
    """`GET url -> HTTP 400: Error: yearA and yearB must be between 1954 and 2026`

    Unlike the WHT exporter, this endpoint answers errors with a *plain-text*
    body that says exactly what was wrong, and those two messages are the whole
    reason a year-range bug would ever be diagnosable. HTTPError is a file-like
    object, so the body is read here and reported rather than discarded.
    """
    detail = ""
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001 - a broken error body must not mask the status
        body = b""
    finally:
        exc.close()
    if body:
        text = body.decode("utf-8", "replace").strip()
        if text:
            detail = ": " + " ".join(text.split())[:MAX_ERROR_BODY]
    return "GET %s -> HTTP %s%s" % (exc.url or "?", exc.code, detail)


def http_get(url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0) -> bytes:
    """Fetch a URL, retrying transient faults.

    `retries` caps the attempt count and `retry_budget` caps the total time
    spent retrying. The budget is the one that matters: a failing DNS lookup
    inside a container takes ~5s to give up, so an uncapped retry loop can
    outlast Prometheus's `scrape_timeout` and turn a recoverable gap into a
    target marked down. Bounding by time keeps the failure local to the request.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-AU,en;q=0.9",
        },
    )
    started = time.monotonic()
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Never retried, and the plain-text body is folded into the message
            # because for this endpoint it is the entire explanation.
            raise ScrapeError(describe_http_error(exc)) from exc
        except urllib.error.URLError as exc:
            if not is_transient(exc.reason):
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            if attempt >= retries:
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            spent = time.monotonic() - started
            if retry_budget and spent >= retry_budget:
                log.warning(
                    "giving up on %s after %.1fs of retry budget: %s",
                    url,
                    spent,
                    exc.reason,
                )
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            # Exponential backoff with jitter. The failures arrive in correlated
            # bursts, so a flat short delay just walks into the same outage and
            # lockstep retries de-synchronise nothing.
            delay = RETRY_BASE_DELAY * (2**attempt)
            delay *= 0.75 + 0.5 * random.random()
            log.warning(
                "transient failure (attempt %d of %d) on %s: %s; retrying in %.1fs",
                attempt + 1,
                retries + 1,
                url,
                exc.reason,
                delay,
            )
            time.sleep(delay)
            continue
    raise ScrapeError("GET %s -> gave up after %d attempts" % (url, retries + 1))


def http_get_json(url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0) -> dict:
    payload = http_get(url, timeout, retries, retry_budget)
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise ScrapeError("GET %s -> invalid JSON" % url) from exc
    if not isinstance(data, dict):
        raise ScrapeError("GET %s -> expected a JSON object, got %s" % (url, type(data).__name__))
    return data


# --------------------------------------------------------------------------- #
# Upstream shape
# --------------------------------------------------------------------------- #


def current_year() -> int:
    """The current year in the endpoint's own timezone, not UTC.

    Shifting by the pinned AEST offset before reading the year is not pedantry:
    between 10:00 UTC on 31 December and 10:00 UTC on 1 January the UTC year is
    already the new one while Sydney has not turned over yet, and requesting a
    year the server has not opened returns HTTP 400.
    """
    return (datetime.now(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)).year


def build_url(data_url: str, year_a: int, year_b: int) -> str:
    """Query string for a year range.

    `yearA` and `yearB` may be equal (that is how you ask for one year) but
    neither may be omitted: the endpoint 400s rather than defaulting.
    """
    return "%s?%s" % (data_url, urllib.parse.urlencode({"yearA": year_a, "yearB": year_b}))


def parse_date(text: object) -> datetime.date:
    if not isinstance(text, str) or not text:
        raise ValueError("missing -date")
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def parse_timestamp(text: object) -> float | None:
    """Naive local Sydney "2026-09-29T07:00:00" -> Unix seconds.

    Returns None for a missing or unparseable stamp rather than raising: a row
    with a bad timestamp still carries a perfectly good level, and losing the
    whole reservoir over a malformed clock field would be a bad trade. The
    caller gets NaN on the staleness metric and a real level, which is the
    honest outcome.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        # Pin the zone explicitly. datetime.timestamp() on a *naive* value
        # silently interprets it in the host's local timezone, so without this
        # the same fixture yields a different instant on the AEST development
        # host than in the UTC container, and a host that changes its zone
        # changes the exported numbers.
        moment = moment.replace(tzinfo=timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS)))
    return moment.timestamp()


def lake_entries(row: dict, key: str = "lake") -> list[dict]:
    """Normalise a row's `lake`/`snow` value to a list.

    The theme's own JS checks for both a list and a bare object, because the
    same include is fed by an XML-shaped upstream that collapses a single child
    into one object. A single-day lake would arrive as a dict here, and
    iterating it directly would yield keys, not entries.
    """
    value = row.get(key)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def find_lake(entries: list[dict], reservoir: str) -> dict | None:
    """Exact name match first, then case-insensitive.

    The names are stable upstream strings ("Tantangara Reservoir"), so the
    fallback only exists so a future "Tantangara reservoir" cannot silently take
    a reservoir out of the metrics.
    """
    for entry in entries:
        if entry.get("-name") == reservoir:
            return entry
    wanted = reservoir.strip().lower()
    for entry in entries:
        name = entry.get("-name")
        if isinstance(name, str) and name.strip().lower() == wanted:
            return entry
    return None


def list_lake_names(payload: dict) -> list[str]:
    """Every `-name` the feed publishes, in first-seen order.

    Backs `--all-reservoirs`. The names are not hard-coded anywhere else in this
    file on purpose: they are upstream's to change, and a hard-coded list would
    quietly export nothing after an upstream rename instead of failing loudly.
    """
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return names
    for year_block in payload.values():
        if not isinstance(year_block, dict):
            continue
        rows = ((year_block.get("snowyhydro") or {}).get("level")) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for entry in lake_entries(row):
                name = entry.get("-name")
                if isinstance(name, str) and name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


def daily_series(payload: dict, reservoir: str) -> list[dict]:
    """Reduce the whole payload to one sorted series of daily readings.

    Walks every year block the endpoint returned rather than assuming a single
    one, so a year range that comes back empty or short degrades to less history
    rather than to an exception.
    """
    series: list[dict] = []
    skipped = 0
    # http_get_json already guarantees a dict, so this only fires if that
    # contract is ever broken. Guarding it here turns a confusing
    # AttributeError deep in a scrape into a ScrapeError that names the cause.
    if not isinstance(payload, dict):
        raise ScrapeError("expected a JSON object, got %s" % type(payload).__name__)
    for year_block in payload.values():
        if not isinstance(year_block, dict):
            continue
        rows = ((year_block.get("snowyhydro") or {}).get("level")) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                day = parse_date(row.get("-date"))
            except ValueError:
                skipped += 1
                continue
            entry = find_lake(lake_entries(row), reservoir)
            if entry is None:
                continue
            try:
                level = float(entry["#text"])
            except (KeyError, TypeError, ValueError):
                # One bad reading must not cost the other 271 days.
                skipped += 1
                continue
            series.append(
                {
                    "date": day,
                    "level_percent": level,
                    "observed_at": parse_timestamp(entry.get("-dataTimestamp")),
                }
            )
    if skipped:
        log.warning("%s: skipped %d unreadable row(s)", reservoir, skipped)
    series.sort(key=lambda point: point["date"])
    if not series:
        raise ScrapeError("no readings found for %r" % reservoir)
    return series


def value_on_or_before(series: list[dict], day) -> dict | None:
    """Newest reading dated on or before `day`.

    Deliberately date-based rather than an index offset: "7 days ago" means seven
    days, and if a day is ever missing from the feed an index-based delta would
    silently slide to a 6- or 8-day window and report a wrong change with no
    visible sign of it.
    """
    best = None
    for point in series:
        if point["date"] <= day:
            best = point
        else:
            break
    return best


def summarise(series: list[dict], change_days: int = 7) -> dict:
    """Latest reading, 7-day change, and calendar-year extremes.

    Sorts defensively rather than trusting the caller: it picks the latest
    reading as `series[-1]`, and `value_on_or_before` breaks out of its scan on
    the first row in the future, so both are only correct on ordered input.
    """
    series = sorted(series, key=lambda point: point["date"])
    latest = series[-1]
    target = latest["date"] - timedelta(days=change_days)
    prior = value_on_or_before(series, target)
    ytd = [point["level_percent"] for point in series if point["date"].year == latest["date"].year]
    return {
        "reservoir": "",  # filled in by collect(), which owns the label
        "level_percent": latest["level_percent"],
        "observed_at": latest["observed_at"],
        "observed_date": latest["date"],
        "change_days": change_days,
        "change_points": (latest["level_percent"] - prior["level_percent"]) if prior else None,
        "change_from": prior["date"] if prior else None,
        "min_ytd_percent": min(ytd) if ytd else None,
        "max_ytd_percent": max(ytd) if ytd else None,
        "ytd_readings": len(ytd),
    }


def discovered_reservoirs(
    data_url: str,
    timeout: float,
    retries: int = 0,
    retry_budget: float = 0.0,
    year: int | None = None,
) -> list[str]:
    """Ask the feed which reservoirs it publishes (one year is enough to list them)."""
    end = year if year is not None else current_year()
    payload = http_get_json(
        build_url(data_url, end, end), timeout, retries, retry_budget
    )
    return list_lake_names(payload)


def collect(
    payload: dict,
    reservoirs: list[str],
    change_days: int = 7,
) -> tuple[list[dict], list[str]]:
    """Gather one summary per requested reservoir.

    Each reservoir is isolated: one missing or malformed lake costs that series
    and nothing else. Returns the samples that did succeed plus a list of
    per-reservoir errors, so the caller can still serve partial data while
    reporting the scrape as failed.
    """
    samples: list[dict] = []
    errors: list[str] = []
    for reservoir in reservoirs:
        try:
            sample = summarise(daily_series(payload, reservoir), change_days)
            sample["reservoir"] = reservoir
            samples.append(sample)
        except (ScrapeError, ValueError, KeyError, TypeError) as exc:
            log.error("%s: %s", reservoir, exc)
            errors.append("%s: %s" % (reservoir, exc))
    return samples, errors


# --------------------------------------------------------------------------- #
# Prometheus exposition
# --------------------------------------------------------------------------- #


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt(value) -> str:
    if value is None:
        return "NaN"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "NaN" if math.isnan(number) else ("+Inf" if number > 0 else "-Inf")
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 4))


# Ordered as they are documented. `change_days` is a parameter, so the metric
# name has to be built to match it - a `--change-days 14` run must not silently
# keep writing the 7d name.
GAUGE_SPECS = (
    (
        "snowy_tantangara_level_percent",
        "Snowy scheme reservoir level, percent of gross storage.",
        "level_percent",
    ),
    (
        "snowy_tantangara_level_change_{change_days}d_percentage_points",
        "Change in reservoir level over the last {change_days} days, in percentage points.",
        "change_points",
    ),
    (
        "snowy_tantangara_level_min_ytd_percent",
        "Lowest published reservoir level in the current calendar year, percent of gross storage.",
        "min_ytd_percent",
    ),
    (
        "snowy_tantangara_level_max_ytd_percent",
        "Highest published reservoir level in the current calendar year, percent of gross storage.",
        "max_ytd_percent",
    ),
    (
        "snowy_tantangara_last_sample_timestamp_seconds",
        "Unix time of the observation behind the current level reading.",
        "observed_at",
    ),
)


def render(
    samples: list[dict],
    success: bool,
    duration: float,
    generated: float,
    change_days: int = 7,
) -> str:
    lines: list[str] = []
    for template, help_template, key in GAUGE_SPECS:
        metric = template.format(change_days=change_days)
        lines.append("# HELP %s %s" % (metric, help_template.format(change_days=change_days)))
        lines.append("# TYPE %s gauge" % metric)
        for sample in samples:
            lines.append(
                '%s{reservoir="%s"} %s'
                % (metric, escape_label(sample["reservoir"]), fmt(sample.get(key)))
            )
    lines += [
        "# HELP snowy_tantangara_scrape_success Whether the last scrape of the reservoir levels succeeded.",
        "# TYPE snowy_tantangara_scrape_success gauge",
        "snowy_tantangara_scrape_success %d" % (1 if success else 0),
        "# HELP snowy_tantangara_scrape_duration_seconds Wall time of the last scrape.",
        "# TYPE snowy_tantangara_scrape_duration_seconds gauge",
        "snowy_tantangara_scrape_duration_seconds %s" % fmt(duration),
        "# HELP snowy_tantangara_last_scrape_timestamp_seconds Unix time of the last scrape.",
        "# TYPE snowy_tantangara_last_scrape_timestamp_seconds gauge",
        "snowy_tantangara_last_scrape_timestamp_seconds %s" % fmt(generated),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Scraper state
# --------------------------------------------------------------------------- #


class LakeScraper:
    def __init__(
        self,
        data_url: str,
        reservoirs: list[str],
        timeout: float,
        ttl: float,
        years_back: int = DEFAULT_YEARS_BACK,
        change_days: int = 7,
        retries: int = 0,
        retry_budget: float = 0.0,
        year: int | None = None,
    ) -> None:
        self.data_url = data_url
        self.reservoirs = reservoirs
        self.timeout = timeout
        self.ttl = ttl
        self.years_back = max(1, years_back)
        self.change_days = change_days
        self.retries = retries
        self.retry_budget = retry_budget
        # Injectable so tests can pin the year without freezing the clock.
        self.year = year
        self._exposition: str | None = None
        self._health = (True, "")
        self._lock = threading.Lock()
        self._fetched_at = 0.0

    def current_year(self) -> int:
        return self.year if self.year is not None else current_year()

    def year_range(self) -> tuple[int, int]:
        """(first year, last year) to request, clamped to the endpoint's floor.

        The clamp matters: the endpoint rejects anything below 1954 with an HTTP
        400, so a large `--years-back` must not turn into a request it will
        refuse.
        """
        end = self.current_year()
        return max(MIN_YEAR, end - self.years_back), end

    def fetch(self) -> dict:
        start, end = self.year_range()
        url = build_url(self.data_url, start, end)
        log.info("fetching %s", url)
        return http_get_json(url, self.timeout, self.retries, self.retry_budget)

    def refresh(self) -> tuple[str, bool]:
        with self._lock:
            started = time.monotonic()
            samples: list[dict] = []
            errors: list[str] = []
            try:
                payload = self.fetch()
                samples, errors = collect(payload, self.reservoirs, self.change_days)
            except (ScrapeError, ValueError, KeyError, TypeError) as exc:
                log.error("scrape failed: %s", exc)
                errors = [str(exc)]
            success = not errors
            duration = time.monotonic() - started
            self._fetched_at = time.time()
            self._exposition = render(
                samples, success, duration, self._fetched_at, self.change_days
            )
            self._health = (success, "; ".join(errors))
            return self._exposition, success

    def exposition(self) -> tuple[str, bool]:
        with self._lock:
            fresh = self._exposition is not None and (time.time() - self._fetched_at) < self.ttl
        if not fresh:
            return self.refresh()
        with self._lock:
            return self._exposition, self._health[0]

    def health(self) -> tuple[bool, str]:
        self.exposition()
        with self._lock:
            return self._health


INDEX_BODY = """<!doctype html>
<title>Snowy Hydro reservoir levels</title>
<h1>Snowy Hydro scheme reservoir levels</h1>
<p>Water data for the Snowy scheme. Not construction progress.</p>
<ul>
  <li><a href="/metrics">/metrics</a></li>
  <li><a href="/healthz">/healthz</a></li>
</ul>
"""


def make_handler(scraper: LakeScraper):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            route = self.path.split("?", 1)[0].rstrip("/") or "/"
            if route in ("/", "/index.html"):
                self._respond(200, "text/html; charset=utf-8", INDEX_BODY.encode())
            elif route == "/metrics":
                body, _ = scraper.exposition()
                self._respond(200, "text/plain; version=0.0.4; charset=utf-8", body.encode())
            elif route == "/healthz":
                healthy, message = scraper.health()
                body = "ok\n" if healthy else "unavailable: %s\n" % message
                self._respond(200 if healthy else 503, "text/plain; charset=utf-8", body.encode())
            else:
                self._respond(404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, fmtstr: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmtstr % args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-url", default=DEFAULT_DATA_URL)
    parser.add_argument(
        "--reservoir",
        action="append",
        dest="reservoirs",
        metavar="NAME",
        help="reservoir to export, as named by the upstream feed; repeatable. "
        "Defaults to %r" % DEFAULT_RESERVOIR,
    )
    parser.add_argument(
        "--all-reservoirs",
        action="store_true",
        help="export every lake in the feed, not just %r" % DEFAULT_RESERVOIR,
    )
    parser.add_argument(
        "--change-days",
        type=int,
        default=7,
        help="window for the level-change metric (default 7, matching the weekly "
        "figure quoted in Snowy Hydro's own copy)",
    )
    parser.add_argument(
        "--years-back",
        type=int,
        default=DEFAULT_YEARS_BACK,
        help="years of history to request, including the current one (default 1, "
        "so the previous year is fetched too and the change window works in January)",
    )
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9110)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="retries for transient upstream failures (DNS EAI_AGAIN, resets, timeouts)",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=6.0,
        help="seconds a single request may spend retrying before giving up; keeps a "
        "scrape under Prometheus scrape_timeout",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=900.0,
        help="seconds to reuse a scrape before re-fetching upstream. The feed is "
        "published once a day around 07:00 Sydney time, so this only needs to be "
        "long enough to stop a burst of scrapes each refetching ~190 KB.",
    )
    parser.add_argument("--once", action="store_true", help="print exposition and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.all_reservoirs:
        try:
            reservoirs = discovered_reservoirs(
                args.data_url, args.timeout, args.retries, args.retry_budget
            )
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            # Discovery is an optimisation, not a dependency. Losing it costs the
            # two non-2.0 lakes; crashing the exporter would cost everything.
            log.error("reservoir discovery failed (%s); using %r", exc, DEFAULT_RESERVOIR)
            reservoirs = []
        if not reservoirs:
            log.error("no reservoirs found upstream; falling back to %r", DEFAULT_RESERVOIR)
            reservoirs = [DEFAULT_RESERVOIR]
    else:
        reservoirs = args.reservoirs or [DEFAULT_RESERVOIR]

    scraper = LakeScraper(
        args.data_url,
        reservoirs,
        args.timeout,
        args.cache_ttl,
        args.years_back,
        args.change_days,
        args.retries,
        args.retry_budget,
    )

    if args.once:
        body, success = scraper.refresh()
        sys.stdout.write(body)
        return 0 if success else 1

    server = ThreadingHTTPServer((args.listen_address, args.port), make_handler(scraper))
    log.info(
        "serving http://%s:%d/metrics for %s",
        args.listen_address,
        args.port,
        ", ".join(reservoirs),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
