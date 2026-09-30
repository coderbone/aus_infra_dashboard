#!/usr/bin/env python3
"""Prometheus exporter for NEM/WEM battery state of charge, from OpenElectricity.

Source: the OpenElectricity API (formerly OpenNEM), at
https://api.openelectricity.org.au . Two endpoints, both bearer-authenticated:

    GET /facilities/?fueltech_id=battery
        Fleet metadata. Every battery facility, its units, and per unit the
        fields that matter here: `capacity_storage` (MWh) and `data_last_seen`.

    GET /data/facilities/NEM?metrics=storage_battery&facility_code=<CODE>
        Time series of stored energy in MWh, one series per *unit*, named
        `storage_battery_<UNIT_CODE>`.

There is no "state of charge" metric. The API publishes stored **energy in MWh**
and the fleet metadata publishes **capacity in MWh**, so SOC is derived here as

    soc = storage_battery / capacity_storage

and exported as a 0-1 ratio. Nothing upstream reports a percentage, and
`capacity_registered` is MW (power) and must not be used as the denominator -
that would be a units error that still produces a plausible-looking number.

Four upstream quirks this is built around, all measured on 2026-09-30 rather
than assumed:

1. **Three series per facility, one usable.** A battery facility returns
   `<CODE>1` plus `<CODE>G1` and `<CODE>L1`. Only `<CODE>1` exists in the fleet
   metadata; G1 and L1 have no `capacity_storage`, so no SOC can be computed for
   them, and their values track the battery's rather than being independent
   storage. Summing all three would triple-count the same MWh. Only series whose
   unit code is present in the metadata with a positive `capacity_storage` are
   exported as SOC; the rest are counted in
   `oe_battery_series_without_capacity` so the gap stays visible.

2. **The aggregate endpoint is unusable.** `?metrics=storage_battery` with no
   `facility_code` returns a single series named `storage_battery_total`
   holding ~14k points with **repeated timestamps and no unit attribution** -
   values from many units concatenated flat. It cannot be split back into units
   and summing it is meaningless. Hence one request per facility, and the
   per-facility `columns: {"unit_code": ...}` is the only thing that makes the
   numbers attributable.

3. **The feed is legitimately sparse.** As of 2026-09-30 the feed carries values
   only for roughly 18:00-04:00 local and nulls for the rest of the day, so a
   scrape at 14:00 correctly finds nothing new. That is not a fault and must not
   be reported as one. The exporter therefore exports the most recent *non-null*
   sample within `--lookback-hours` together with its real timestamp and age,
   and drops a series only once the newest sample is older than
   `--max-sample-age` - so a genuinely dead feed loses its series rather than
   repeating an ancient reading as if it were current.

4. **Facilities in the metadata can 404 on the data endpoint** (`COLLIE_BESS2`
   does), and a facility whose newest unit is `committed` has no data at all.
   Both are expected and counted, not raised as scrape failures.

Request budget, and why the poll loop is *not* the scrape handler
--------------------------------------------------------------------
The free Community plan allows 500 credits/day but also rate-limits by bucket:
**8 requests / 5 min, 32 / 1 h, 366 / 1 day** (verified against /v1/plans).
Each facility costs one request, so N batteries cost N requests per cycle.

That makes scrape-driven polling arithmetically impossible: the stack scrapes
every 5m (288 scrapes/day), and at even N=5 that is 1440 requests/day, blowing
the 366/day bucket by 4x and the credit allowance along with it.

So this exporter **polls on its own schedule** and serves the last completed
cycle to Prometheus. Prometheus can scrape as often as it likes - here 5m - at
zero credit cost, and the API is touched only `--poll-interval` times per cycle.
With the defaults (N=10, hourly) that is 240 requests/day, inside the 366/day
bucket, ~1 request per 6 min so the 8/5min bucket is never approached. The
rate buckets, not the credit allowance, are the binding constraint: individual
narrow calls have measured anywhere from 0 to 1 credit, and a five-request cycle
measured 491 from 494, so a full 12-request cycle is somewhere around 4-12
credits against a 500/day allowance. Both margins are wide, but the 366/day
request bucket is the one to watch, and `oe_api_requests_total` is the number
that predicts it.

`oe_api_credits_remaining` reads the free `/me` endpoint each cycle (verified
free: repeated reads on their own cost nothing) so the budget is visible on the
dashboard rather than being something you discover by being throttled.

The API also **rejects the default `Python-urllib` User-Agent with HTTP 403**,
which is why USER_AGENT below is explicit and must not be dropped. Verified
2026-09-30: `Python-urllib/3.12` -> 403, `curl/8.5.0` -> 200, and both
`python-requests/2.31` and a custom UA -> 200. It is a bot rule keyed on that
exact string, and it fails as a bare 403 with no body explaining why.

Exposed metrics

Every per-battery metric carries the same five labels - {facility, unit, name,
region, status} - so one Grafana variable selects the same batteries in every
panel and every table frame joins on the same key:

    oe_battery_soc_ratio                       SOC, 0-1
    oe_battery_energy_stored_mwh               MWh, as published
    oe_battery_capacity_storage_mwh            MWh, from metadata
    oe_battery_last_sample_timestamp_seconds   when that value was observed
    oe_battery_sample_age_seconds              its age at export time
    oe_battery_scrape_success                  1 = read, 0 = not
    oe_battery_capacity_rank                   1 = largest

Unlabelled fleet and poll gauges:

    oe_batteries_enumerated                    fleet size
    oe_batteries_in_scope                      after --top
    oe_batteries_monitored                     with a usable reading
    oe_battery_fleet_capacity_mwh              whole fleet
    oe_battery_monitored_capacity_mwh          monitored subset
    oe_battery_series_without_capacity         quirk 1
    oe_battery_series_too_stale                dropped
    oe_poll_cycle_duration_seconds             wall time
    oe_last_poll_timestamp_seconds             when
    oe_last_fleet_refresh_timestamp_seconds     metadata age
    oe_api_credits_remaining                   daily budget
    oe_api_requests_total                      counter, includes failures

`oe_battery_scrape_success` is 0 for a facility that could not be read, and the
SOC series is **omitted** for it rather than repeated, so a failure shows as a
gap in the gauge plus this flag, matching the sibling exporters.

Usage
    oe_battery_exporter.py --api-key-file=/run/secrets/openelectricity
    oe_battery_exporter.py --once
    oe_battery_exporter.py --api-key-env=OPENEA_API_KEY --top=15 --poll-interval=1800

The key is read from a file or an environment variable and never from a command
line argument, so it cannot end up in `docker inspect`, in the process table, or
in this process's own argv.

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

DEFAULT_API_BASE = "https://api.openelectricity.org.au"
DEFAULT_NETWORK = "NEM"

# Not cosmetic. The API 403s urllib's default UA outright; see the module
# docstring. The sibling exporters spoof a browser for HTML feeds, which is
# pointless here - this is a documented JSON API and it gates on the literal
# "Python-urllib" string, so a truthful UA is both allowed and correct.
USER_AGENT = "aus-infra-dashboard/1.0 (+battery-soc-exporter)"

# Fueltech filter for the fleet call. The API separates a battery into
# `battery`, `battery_charging` and `battery_discharging`; `battery` is the
# facility classification and is what carries capacity_storage.
BATTERY_FUELTECH = "battery"

# Statuses worth exporting. `committed` units have not operated yet and return
# no data, so including them would only pad the top-N with permanently empty
# series. `retired` likewise. `commissioning` IS included: a unit partway
# through commissioning is a real battery with a real SOC curve (ERB2 and
# COLLIE_BESS2 are both commissioning and both return data), and excluding it
# would hide the two largest batteries in the fleet.
INCLUDED_STATUSES = ("operating", "commissioning")

DEFAULT_TOP = 10

# Hours of history requested per facility call. This has to be at least as wide
# as the gap between publications, or a healthy battery looks dead. Values only
# exist ~18:00-04:00, so the newest reading is up to ~13h old during the
# afternoon; a 12h window was measured returning an *empty* series for Eraring
# and Waratah at 15:37, eleven and a half hours after their last value. Anything
# narrower than the publication period silently empties the whole dashboard in
# the late afternoon and refills it in the evening. Kept equal to
# DEFAULT_MAX_SAMPLE_AGE so the two agree: the API returns everything the
# staleness check is then allowed to judge, rather than being the thing that
# rejects it first. Credits are per request, not per point, so the wider window
# is free.
DEFAULT_LOOKBACK_HOURS = 36

# A sample older than this is dropped rather than exported. The feed's own
# daily cycle can leave a ~13h gap, so this has to be comfortably longer than
# that or every daytime scrape would empty the dashboard; it is set well above
# the publication period so it only fires on a feed that has actually stopped.
DEFAULT_MAX_SAMPLE_AGE = 36 * 3600.0

# A unit that has produced no usable reading for this long is dropped from
# scope, and the next-largest candidate takes its slot. Note this is a *duration
# since the last reading*, deliberately not a count of consecutive empty polls:
# the publication window makes a healthy battery look empty for a few cycles
# every afternoon, so a consecutive-failure counter would demote the entire
# scope daily and rotate every battery in and out. At 36h a unit has to miss a
# whole night before it counts as idle, and a battery that has genuinely
# stopped publishing trips it within a day or so.
DEFAULT_DROP_IDLE_SECONDS = 36 * 3600.0

DEFAULT_POLL_INTERVAL = 3600.0
# GET /v1/plans, plan COMMUNITY, is explicit: "burst_rate_limit": "2/s". A poll
# cycle of 12-14 sequential requests otherwise goes out in two or three seconds
# (~5 req/s) and a 429 is not retried - it fails the whole cycle, because a rate
# limited request is treated as a hard answer. Half a second of spacing holds
# the exporter under the documented burst limit for the price of a few seconds
# out of a 3600s interval.
DEFAULT_REQUEST_INTERVAL = 0.6
DEFAULT_FLEET_REFRESH_INTERVAL = 24 * 3600.0

MAX_ERROR_BODY = 200
RETRY_BASE_DELAY = 0.5

# Statuses from /facilities that are not a usable battery, kept for reporting.
SKIPPED_STATUSES = ("committed", "retired")

log = logging.getLogger("oe_battery_exporter")


class ScrapeError(Exception):
    """Raised when the upstream cannot be read or understood."""


class AuthError(ScrapeError):
    """Raised on HTTP 401/403, which for this API usually means the UA rule."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


# Same transient set as the sibling exporters: EAI_AGAIN from Docker's embedded
# resolver is the one that actually bites in a container.
TRANSIENT_ERRNOS = frozenset(
    {
        socket.EAI_AGAIN,
        errno.EAGAIN,
        errno.ECONNRESET,
        errno.ECONNABORTED,
    }
)


def is_transient(reason: object) -> bool:
    if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(reason, "errno", None) in TRANSIENT_ERRNOS


def describe_http_error(exc: urllib.error.HTTPError) -> str:
    """`GET url -> HTTP 403: Forbidden`, with the body folded in when present.

    This API's 403 carries no body at all, which is exactly why the UA trap
    cost time to find: there is nothing in the response to say the request was
    rejected for who it claimed to be. The message is still worth building
    because the 401 case (a genuinely bad key) does carry a JSON body.
    """
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001 - a broken error body must not mask the status
        body = b""
    finally:
        exc.close()
    detail = ""
    if body:
        text = body.decode("utf-8", "replace").strip()
        if text:
            detail = ": " + " ".join(text.split())[:MAX_ERROR_BODY]
    return "GET %s -> HTTP %s%s" % (exc.url or "?", exc.code, detail)


class OpenElectricityClient:
    """Minimal authenticated JSON client for the OpenElectricity API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_API_BASE,
        timeout: float = 20.0,
        retries: int = 3,
        retry_budget: float = 6.0,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_budget = retry_budget
        self.request_interval = max(0.0, request_interval)
        self.requests_made = 0
        self._last_request_at = 0.0

    def _pace(self) -> None:
        """Hold the documented burst limit, not the request budget.

        The 5m bucket is a separate constraint and is *not* satisfied by this:
        13 requests in one cycle is over the 8/5m allowance whatever the
        spacing. It has been that way since the exporter shipped, with no 429 in
        18h of running, so the bucket is evidently enforced leniently - but
        `--request-interval 38` would satisfy it (300/8 = 37.5s between
        requests) for 8 minutes of a 60-minute cycle, if it ever needs to.
        """
        if not self.request_interval:
            return
        wait = self.request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, url: str) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        started = time.monotonic()
        last = "gave up"
        for attempt in range(self.retries + 1):
            self._pace()
            # Counted when issued, not when answered: the budget is spent by the
            # request, so a 404 or a 500 still has to show up here or the daily
            # tally quietly under-reports what the exporter actually costs.
            self.requests_made += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = describe_http_error(exc)
                if exc.code in (401, 403):
                    raise AuthError(message) from exc
                # 404 and 4xx are answers, not faults: an absent facility is
                # expected (quirk 4) and retrying cannot change it.
                raise ScrapeError(message) from exc
            except urllib.error.URLError as exc:
                if not is_transient(exc.reason):
                    raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
                last = str(exc.reason)
                if attempt >= self.retries:
                    break
                spent = time.monotonic() - started
                if self.retry_budget and spent >= self.retry_budget:
                    log.warning("giving up on %s after %.1fs of retry budget", url, spent)
                    break
                delay = RETRY_BASE_DELAY * (2**attempt) * (0.75 + 0.5 * random.random())
                log.warning(
                    "transient failure (attempt %d of %d) on %s: %s; retrying in %.1fs",
                    attempt + 1,
                    self.retries + 1,
                    url,
                    exc.reason,
                    delay,
                )
                time.sleep(delay)
            except ValueError as exc:
                raise ScrapeError("GET %s -> invalid JSON: %s" % (url, exc)) from exc
        raise ScrapeError("GET %s -> %s" % (url, last))

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        payload = self._request(url)
        # The API signals application-level failure with success:false rather
        # than a non-2xx status, so it has to be checked explicitly.
        if not payload.get("success", True):
            raise ScrapeError(
                "GET %s -> API error: %s" % (url, payload.get("error") or "unspecified")
            )
        return payload

    # -- endpoints ---------------------------------------------------------- #

    def battery_fleet(self) -> list[dict]:
        payload = self.get("/facilities/", {"fueltech_id": BATTERY_FUELTECH})
        facilities = payload.get("data") or []
        if not isinstance(facilities, list):
            raise ScrapeError("/facilities/ -> data is %s, not a list" % type(facilities).__name__)
        return facilities

    def storage_battery(
        self, facility_code: str, network: str, lookback_hours: float, now: float
    ) -> list[dict]:
        """Return `[(unit_code, [(epoch, mwh), ...])]` for one facility.

        The window is `date_start` .. `date_end` with `interval=1h`, and only
        the newest non-null value per series is kept - the dashboard wants
        state of charge now, not a backfill, and one point per series is the
        cheapest shape this call can have.
        """
        end = now
        start = now - max(1.0, lookback_hours) * 3600.0
        payload = self.get(
            "/data/facilities/%s" % urllib.parse.quote(network),
            {
                "metrics": "storage_battery",
                "facility_code": facility_code,
                "interval": "1h",
                "date_start": iso_local(start),
                "date_end": iso_local(end),
            },
        )
        out: list[tuple[str, tuple[float, float]]] = []
        for block in payload.get("data") or []:
            for series in block.get("results") or []:
                name = series.get("name") or ""
                unit_code = name.split("storage_battery_", 1)[-1] if "storage_battery_" in name else name
                latest = latest_sample(series.get("data") or [])
                if latest is None:
                    continue
                out.append((unit_code, latest))
        return out

    def credits_remaining(self) -> float | None:
        """Remaining daily credits, or None if /me could not be read.

        /me is free (verified: reads on their own moved the balance by zero),
        so this is a cheap way to keep the budget visible. A failure here is never
        fatal - it costs a gauge, not the scrape.
        """
        try:
            payload = self.get("/me")
        except ScrapeError as exc:
            log.warning("could not read credit balance: %s", exc)
            return None
        credits = ((payload.get("data") or {}).get("credits") or {})
        value = credits.get("remaining")
        return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #
# Time handling
# --------------------------------------------------------------------------- #


def iso_local(epoch: float) -> str:
    """`2026-09-29T18:00:00` in the network's local time, which is what the API
    takes.

    The API's timestamps carry a +10:00 offset and it is a fixed AEST offset -
    no DST handling in the response either - so the exporter sends a naive local
    string and parses what comes back as a fixed offset. That is symmetric, so
    the round trip is exact. This is the same trade-off the Snowy exporter
    documents: `zoneinfo` needs tzdata, which `python:3.12-alpine` does not ship,
    and one hour of error during daylight saving is irrelevant against a
    multi-hour staleness threshold.
    """
    tz = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))
    return datetime.fromtimestamp(epoch, tz).replace(tzinfo=None).isoformat()


def parse_timestamp(value: str) -> float | None:
    """Parse an API timestamp into epoch seconds, or None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Tolerate a bare date, which the metadata's date fields sometimes are.
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))
        )
    return parsed.timestamp()


LOCAL_UTC_OFFSET_HOURS = 10


def latest_sample(points: list) -> tuple[float, float] | None:
    """Newest `(epoch, value)` in an upstream `[[ts, value], ...]` block.

    Nulls are skipped rather than treated as zero, which is the whole point of
    quirk 3: a null means "not published for this interval", and reading it as 0
    would report a charged battery as flat and empty twice a day.
    """
    best: tuple[float, float] | None = None
    for row in points or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        stamp, value = row[0], row[1]
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if math.isnan(float(value)) or math.isinf(float(value)):
            continue
        epoch = parse_timestamp(stamp)
        if epoch is None:
            continue
        if best is None or epoch > best[0]:
            best = (epoch, float(value))
    return best


# --------------------------------------------------------------------------- #
# Fleet selection
# --------------------------------------------------------------------------- #


def unit_rows(facilities: list[dict], require_data: bool = True) -> list[dict]:
    """Flatten `/facilities/` into one row per battery unit.

    `capacity_storage` is a *unit* field, not a facility field, so the SOC
    denominator has to be looked up per unit code. A facility whose units carry
    no capacity cannot yield a SOC and is dropped here rather than emitting a
    series with no denominator.

    `require_data` drops units with no `data_last_seen`, and it defaults on for
    a reason that is not tidiness. **The largest batteries in the fleet are
    mostly unbuilt.** As of 2026-09-30, of the ten highest-capacity units,
    seven are `committed` and have never dispatched: Richmond Valley (2200 MWh,
    the largest in the country), Tomago (2000), Baranduda (1886), Wooreen
    (1400), Elaine (1244), Western Downs 3 (1220), Supernode 3 (1217). Every one
    of them 404s or returns an empty series on the data endpoint, because there
    is nothing to report.

    Ranking without this filter spends 7 of 10 daily request slots on
    permanently-empty series and pushes the batteries that actually have SOC
    curves - Eraring, Waratah, Orana - out of scope entirely. So the top-N is
    taken over units that have been observed at least once. `commissioning` is
    *kept* (Eraring 2 and Collie 2 are both commissioning and both return real
    data); it is `committed` that is excluded, and only via this data check
    rather than by trusting `status_id`.
    """
    rows: list[dict] = []
    for facility in facilities or []:
        if not isinstance(facility, dict):
            continue
        for unit in facility.get("units") or []:
            if not isinstance(unit, dict):
                continue
            if unit.get("fueltech_id") != BATTERY_FUELTECH:
                continue
            capacity = unit.get("capacity_storage")
            if not isinstance(capacity, (int, float)) or isinstance(capacity, bool):
                continue
            capacity = float(capacity)
            if capacity <= 0:
                continue
            if require_data and not unit.get("data_last_seen"):
                continue
            rows.append(
                {
                    "facility": facility.get("code") or "",
                    "facility_name": facility.get("name") or "",
                    "region": facility.get("network_region") or "",
                    # The endpoint is per-network, and the fleet spans two. Ten
                    # of the 119 battery facilities are WEM (the Collie and
                    # Synergy BESS in Western Australia) and querying one of
                    # those under network_code=NEM returns HTTP 404 "No data
                    # available" - which is indistinguishable from a genuinely
                    # empty battery. 404 was masking a routing bug, so the
                    # network is carried per row from the facility metadata
                    # rather than assumed.
                    "network": facility.get("network_id") or DEFAULT_NETWORK,
                    "unit": unit.get("code") or "",
                    "status": unit.get("status_id") or "",
                    "capacity_mwh": capacity,
                    "power_mw": unit.get("capacity_registered"),
                    "data_last_seen": unit.get("data_last_seen"),
                }
            )
    return rows


def select_top(rows: list[dict], top: int) -> list[dict]:
    """Rank by `capacity_mwh` descending and keep the first `top`.

    Ties are broken on facility then unit code so the choice is deterministic
    and does not wobble between polls just because the upstream reordered a
    list - a set of monitored batteries that changes every cycle would leave
    holes in the TSDB for no reason.
    """
    ranked = sorted(
        rows,
        key=lambda r: (-r["capacity_mwh"], r["facility"], r["unit"]),
    )
    chosen = ranked[: max(0, top)] if top else ranked
    return [dict(row, rank=i) for i, row in enumerate(chosen, start=1)]


def idle_units(
    rows: list[dict],
    last_reading: dict[str, float],
    now: float,
    drop_idle_seconds: float,
) -> list[dict]:
    """Rows whose last usable reading is older than `drop_idle_seconds`.

    Free to compute - it is the exporter's own record of what it has already
    been told, no request involved. What it cannot do is predict the future: the
    fleet metadata's `data_last_seen` is the only free liveness signal and it
    says "today" even for the Collie units whose `storage_battery` series come
    back as 1151 points of nothing. So a unit is only ever found idle *after*
    asking it, and the cost of finding out is the one request it already costs
    as a candidate.
    """
    if drop_idle_seconds <= 0:
        return []
    idle = []
    for row in rows:
        seen = last_reading.get(row["unit"])
        # A unit never polled yet has no verdict, so it is not idle - otherwise
        # a fresh candidate would be demoted before it was ever asked.
        if seen is None:
            continue
        if (now - seen) > drop_idle_seconds:
            idle.append(row)
    return idle


def select_scope(
    rows: list[dict],
    top: int,
    last_reading: dict[str, float],
    now: float,
    drop_idle_seconds: float,
) -> tuple[list[dict], list[dict]]:
    """`select_top`, minus the units that have stopped publishing.

    Returns `(scope, demoted)`. Dropping a unit does not shrink the scope, it
    promotes the next-largest candidate into the freed slot, so a battery that
    dies costs a slot for one cycle rather than a slot permanently - the fleet
    has 64 live units below the current cutoff and the top 10 of them are all
    the ones worth watching.
    """
    if drop_idle_seconds <= 0:
        return select_top(rows, top), []
    idle = {row["unit"] for row in idle_units(rows, last_reading, now, drop_idle_seconds)}
    scope = select_top([r for r in rows if r["unit"] not in idle], top)
    demoted = [dict(r, idle_seconds=now - last_reading[r["unit"]]) for r in rows if r["unit"] in idle]
    demoted.sort(key=lambda r: (-r["capacity_mwh"], r["facility"], r["unit"]))
    return scope, demoted


# --------------------------------------------------------------------------- #
# Poll cycle
# --------------------------------------------------------------------------- #


def poll_cycle(
    client: OpenElectricityClient,
    rows: list[dict],
    network: str,
    lookback_hours: float,
    max_sample_age: float,
    now: float,
    sleep_between: float = 0.0,
) -> dict:
    """One request per in-scope facility, reduced to one SOC per battery."""
    by_unit = {row["unit"]: row for row in rows}
    samples: list[dict] = []
    without_capacity = 0
    too_stale = 0

    for row in rows:
        try:
            series = client.storage_battery(
                row["facility"], row.get("network") or network, lookback_hours, now
            )
        except AuthError:
            # A rejected key is not a per-battery failure; every request will
            # fail the same way, so stop rather than burn N requests on it.
            raise
        except ScrapeError as exc:
            # quirk 4: an absent facility is an expected answer, not a fault.
            log.warning("facility %s: %s", row["facility"], exc)
            samples.append(
                dict(
                    row,
                    soc=None,
                    stored_mwh=None,
                    sampled_at=None,
                    scrape_success=False,
                )
            )
            continue

        matched = False
        for unit_code, (stamp, mwh) in series:
            known = by_unit.get(unit_code)
            if known is None or not known.get("capacity_mwh"):
                # quirk 1: G1/L1 have no capacity, so no SOC exists for them.
                without_capacity += 1
                continue
            if now - stamp > max_sample_age:
                too_stale += 1
                continue
            matched = True
            samples.append(
                dict(
                    known,
                    soc=max(0.0, min(1.0, mwh / known["capacity_mwh"])),
                    stored_mwh=mwh,
                    sampled_at=stamp,
                    scrape_success=True,
                )
            )
        if not matched and not any(
            s["unit"] == row["unit"] and not s.get("scrape_success") for s in samples
        ):
            # The call succeeded but this battery's unit is absent from the
            # response. Record it as unread so the gauge drops rather than
            # silently disappearing with no flag.
            samples.append(
                dict(
                    row,
                    soc=None,
                    stored_mwh=None,
                    sampled_at=None,
                    scrape_success=False,
                )
            )
        if sleep_between:
            time.sleep(sleep_between)

    return {
        "samples": samples,
        "series_without_capacity": without_capacity,
        "series_too_stale": too_stale,
    }


# --------------------------------------------------------------------------- #
# Prometheus exposition
# --------------------------------------------------------------------------- #


def escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt(value) -> str:
    if value is None:
        return "NaN"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "NaN" if math.isnan(number) else ("+Inf" if number > 0 else "-Inf")
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 6))


def label_set(sample: dict) -> str:
    return (
        'facility="%s",unit="%s",name="%s",region="%s",status="%s"'
        % (
            escape_label(sample.get("facility", "")),
            escape_label(sample.get("unit", "")),
            escape_label(sample.get("facility_name", "")),
            escape_label(sample.get("region", "")),
            escape_label(sample.get("status", "")),
        )
    )


def render(state: dict, now: float) -> str:
    lines: list[str] = []
    samples = state.get("samples") or []

    def gauge(metric: str, help_text: str, key, transform=None) -> None:
        lines.append("# HELP %s %s" % (metric, help_text))
        lines.append("# TYPE %s gauge" % metric)
        for sample in samples:
            value = sample.get(key)
            if value is None:
                # Omitted, not zero: a battery that could not be read has an
                # unknown SOC, and 0% would read as "flat and empty".
                continue
            if transform is not None:
                value = transform(sample, now)
            lines.append("%s{%s} %s" % (metric, label_set(sample), fmt(value)))

    gauge(
        "oe_battery_soc_ratio",
        "State of charge of a NEM/WEM battery, 0-1, derived as stored energy divided "
        "by registered storage capacity. The API publishes no SOC metric; this "
        "is energy/capacity.",
        "soc",
    )
    gauge(
        "oe_battery_energy_stored_mwh",
        "Energy stored in a NEM/WEM battery, MWh, as published by the storage_battery metric.",
        "stored_mwh",
    )
    gauge(
        "oe_battery_capacity_storage_mwh",
        "Registered storage capacity of a NEM/WEM battery unit, MWh, from the fleet metadata.",
        "capacity_mwh",
    )
    gauge(
        "oe_battery_last_sample_timestamp_seconds",
        "Unix time of the observation behind the current reading.",
        "sampled_at",
    )
    gauge(
        "oe_battery_sample_age_seconds",
        "Age of the newest published sample for this battery. Non-zero by design: "
        "the feed only publishes overnight, so a daytime reading is hours old.",
        "sampled_at",
        transform=lambda s, n: n - s["sampled_at"],
    )
    gauge(
        "oe_battery_capacity_rank",
        "Rank of this battery by storage capacity among the monitored set, 1 = largest.",
        "rank",
    )

    lines.append(
        "# HELP oe_battery_scrape_success Whether the last poll read this battery. "
        "Carries the same labels as the reading metrics, not a reduced set, so "
        "that a single Grafana variable selects the same batteries in every "
        "panel and every table frame joins on the same key."
    )
    lines.append("# TYPE oe_battery_scrape_success gauge")
    for sample in samples:
        lines.append(
            "oe_battery_scrape_success{%s} %d"
            % (label_set(sample), 1 if sample.get("scrape_success") else 0)
        )

    # Emitted for in-scope and demoted units alike, because the question this
    # answers is "why did that battery stop appearing on the dashboard" - and
    # the answer is only visible for the units that are *not* in scope.
    idle_rows = {}
    for sample in samples:
        idle_rows[sample.get("unit")] = sample
    for row in state.get("demoted") or []:
        idle_rows.setdefault(row.get("unit"), row)
    last_reading = state.get("last_reading") or {}
    if idle_rows:
        lines.append(
            "# HELP oe_battery_idle_seconds Seconds since the newest usable "
            "reading seen for this battery. A unit is dropped from scope once "
            "this passes the idle limit."
        )
        lines.append("# TYPE oe_battery_idle_seconds gauge")
        for unit, row in sorted(idle_rows.items()):
            seen = last_reading.get(unit)
            lines.append(
                "oe_battery_idle_seconds{%s} %s"
                % (label_set(row), fmt(None if seen is None else max(0.0, now - seen)))
            )

    scalars = (
        ("oe_batteries_enumerated", "Battery units in the fleet metadata with a positive storage capacity, after the dispatchability filter (see --include-undispatched)."),
        ("oe_batteries_in_scope", "Battery units selected for polling after applying the top-N limit and dropping units that have stopped publishing."),
        ("oe_batteries_demoted", "Candidate units dropped from scope because no usable reading has arrived within the idle limit. Their slots go to the next-largest candidate, so this counts batteries skipped, not slots lost."),
        ("oe_batteries_monitored", "Battery units that returned a usable sample in the last poll."),
        ("oe_battery_series_without_capacity", "Storage series returned upstream that have no capacity in the fleet metadata, so no SOC can be derived. Non-zero is expected: each battery facility also returns G1 and L1 series."),
        ("oe_battery_series_too_stale", "Samples dropped for being older than the maximum sample age."),
    )
    for metric, help_text in scalars:
        lines.append("# HELP %s %s" % (metric, help_text))
        lines.append("# TYPE %s gauge" % metric)
        lines.append("%s %s" % (metric, fmt(state.get(metric))))

    for metric, help_text in (
        ("oe_battery_fleet_capacity_mwh", "Total registered storage capacity of the whole enumerated fleet, MWh."),
        ("oe_battery_monitored_capacity_mwh", "Total registered storage capacity of the monitored subset, MWh."),
    ):
        lines.append("# HELP %s %s" % (metric, help_text))
        lines.append("# TYPE %s gauge" % metric)
        lines.append("%s %s" % (metric, fmt(state.get(metric))))

    for metric, help_text in (
        ("oe_poll_cycle_duration_seconds", "Wall time of the last poll cycle."),
        ("oe_last_poll_timestamp_seconds", "Unix time of the last completed poll cycle."),
        ("oe_last_fleet_refresh_timestamp_seconds", "Unix time the fleet metadata was last re-read."),
    ):
        lines.append("# HELP %s %s" % (metric, help_text))
        lines.append("# TYPE %s gauge" % metric)
        lines.append("%s %s" % (metric, fmt(state.get(metric))))

    lines.append(
        "# HELP oe_api_credits_remaining OpenElectricity API credits remaining in the current day, from /me."
    )
    lines.append("# TYPE oe_api_credits_remaining gauge")
    lines.append("oe_api_credits_remaining %s" % fmt(state.get("oe_api_credits_remaining")))

    lines.append("# HELP oe_api_requests_total Authenticated API requests issued since start, including any that failed.")
    lines.append("# TYPE oe_api_requests_total counter")
    lines.append("oe_api_requests_total %s" % fmt(state.get("oe_api_requests_total")))

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Exporter
# --------------------------------------------------------------------------- #


class BatteryScraper:
    def __init__(
        self,
        client: OpenElectricityClient,
        network: str = DEFAULT_NETWORK,
        top: int = DEFAULT_TOP,
        lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
        max_sample_age: float = DEFAULT_MAX_SAMPLE_AGE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        fleet_refresh_interval: float = DEFAULT_FLEET_REFRESH_INTERVAL,
        fleet_file: str | None = None,
        liveness_file: str | None = None,
        require_data: bool = True,
        drop_idle_seconds: float = DEFAULT_DROP_IDLE_SECONDS,
        watchlist_per_cycle: int = 0,
        now_fn=time.time,
    ) -> None:
        self.client = client
        self.network = network
        self.top = top
        self.lookback_hours = lookback_hours
        self.max_sample_age = max_sample_age
        self.poll_interval = poll_interval
        self.fleet_refresh_interval = fleet_refresh_interval
        self.fleet_file = fleet_file
        self.liveness_file = liveness_file
        self.require_data = require_data
        self.drop_idle_seconds = drop_idle_seconds
        self.watchlist_per_cycle = watchlist_per_cycle
        self.now_fn = now_fn
        self._state: dict = {}
        self._fleet: list[dict] = []
        self._fleet_at = 0.0
        # unit code -> epoch of the newest usable reading we have ever been
        # shown for it. Only the poll thread touches this; the serving thread
        # reads the derived counts out of `self._state`, which is published
        # under the lock like everything else.
        self._last_reading: dict[str, float] = {}
        if liveness_file:
            # Restoring liveness is what stops a restart from handing every
            # unreadable battery a fresh 36h reprieve. Stamps in the future are
            # dropped rather than trusted: a skewed clock would otherwise pin a
            # dead battery in scope indefinitely.
            restored = load_liveness(liveness_file)
            now = self.now_fn()
            self._last_reading = {
                unit: stamp for unit, stamp in restored.items() if stamp <= now
            }
            skipped = len(restored) - len(self._last_reading)
            if restored:
                log.info(
                    "restored liveness for %d units from %s%s",
                    len(self._last_reading),
                    liveness_file,
                    " (%d future-dated stamps ignored)" % skipped if skipped else "",
                )
        self._probe_offset = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = (True, "no poll completed yet")

    # -- fleet -------------------------------------------------------------- #

    def load_fleet(self) -> list[dict]:
        """Re-read the fleet metadata, at most once per refresh interval.

        The metadata is essentially static - capacity does not change - so
        polling it every cycle would waste a request per hour on data that
        changes a few times a year.

        The on-disk copy is a *fallback*, not a second source of truth: it is
        only read when the API cannot be reached, and its `cached_at` stamp is
        carried over as the refresh time. A cache that is older than the refresh
        interval therefore does not pin the fleet forever - the next cycle
        retries the API, exactly as it would with no cache at all.
        """
        now = self.now_fn()
        with self._lock:
            fresh = self._fleet and (now - self._fleet_at) < self.fleet_refresh_interval
        if fresh:
            return self._fleet
        try:
            facilities = self.client.battery_fleet()
        except (ScrapeError, AuthError) as exc:
            cached = self.cached_fleet()
            if cached is None:
                raise
            rows, cached_at = cached
            log.warning("using cached fleet metadata (%s): %s", self.fleet_file, exc)
            with self._lock:
                self._fleet, self._fleet_at = rows, cached_at
            return rows
        rows = unit_rows(facilities, self.require_data)
        if not rows:
            raise ScrapeError("/facilities/ -> no battery units with a storage capacity")
        if self.fleet_file:
            save_cache(self.fleet_file, facilities)
        with self._lock:
            self._fleet, self._fleet_at = rows, now
        return rows

    def cached_fleet(self) -> tuple[list[dict], float] | None:
        """Rows and cache age from the on-disk fleet, or None if unusable."""
        if not self.fleet_file:
            return None
        cached = load_cache_entry(self.fleet_file)
        if cached is None:
            return None
        data, cached_at = cached
        rows = unit_rows(data, self.require_data)
        return (rows, cached_at) if rows else None

    # -- poll --------------------------------------------------------------- #

    def poll_once(self) -> tuple[str, bool]:
        # Deliberately NOT holding self._lock for the network work. The lock is
        # only taken to publish state, because /metrics has to stay answerable
        # while a cycle of N requests is in flight - holding a plain Lock across
        # the poll would block every scrape for the whole cycle, and calling
        # load_fleet() (which takes the lock itself) from inside it would
        # deadlock outright, since threading.Lock is not reentrant.
        started = time.monotonic()
        errors: list[str] = []
        scope: list[dict] = []
        demoted: list[dict] = []
        try:
            rows = self.load_fleet()
            started_at = self.now_fn()
            scope, demoted = select_scope(
                rows, self.top, self._last_reading, started_at, self.drop_idle_seconds
            )
            result = poll_cycle(
                self.client,
                scope,
                self.network,
                self.lookback_hours,
                self.max_sample_age,
                started_at,
            )
            self._record_readings(result["samples"], started_at)
            demoted = self._probe_demoted(rows, demoted, started_at)
            # Saved last, and only on a clean poll: the watchlist probe above
            # also records liveness, so saving before it would lose the recovery
            # it just found, and saving after an error would persist a clock
            # that never actually advanced.
            if self.liveness_file and not errors:
                save_liveness(self.liveness_file, self._last_reading)
        except AuthError as exc:
            log.error("authentication rejected: %s", exc)
            with self._lock:
                self._health = (False, str(exc))
            return "", False
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("poll failed: %s", exc)
            errors = [str(exc)]
            result = {
                "samples": [],
                "series_without_capacity": 0,
                "series_too_stale": 0,
            }

        finished = self.now_fn()
        state = {
            "samples": result["samples"],
            "oe_batteries_enumerated": len(self._fleet),
            "oe_batteries_in_scope": len(scope) if not errors else 0,
            "oe_batteries_demoted": len(demoted) if not errors else 0,
            "demoted": demoted,
            "last_reading": dict(self._last_reading),
            "oe_batteries_monitored": sum(
                1 for s in result["samples"] if s.get("scrape_success")
            ),
            "oe_battery_series_without_capacity": result["series_without_capacity"],
            "oe_battery_series_too_stale": result["series_too_stale"],
            "oe_battery_fleet_capacity_mwh": sum(r["capacity_mwh"] for r in self._fleet),
            "oe_battery_monitored_capacity_mwh": sum(
                s.get("capacity_mwh") or 0.0
                for s in result["samples"]
                if s.get("scrape_success")
            ),
            "oe_poll_cycle_duration_seconds": time.monotonic() - started,
            "oe_last_poll_timestamp_seconds": finished,
            "oe_last_fleet_refresh_timestamp_seconds": self._fleet_at,
            "oe_api_credits_remaining": self.client.credits_remaining(),
            "oe_api_requests_total": self.client.requests_made,
        }
        ok = not errors
        with self._lock:
            self._state = state
            self._health = (ok, "; ".join(errors))
        return render(state, self.now_fn()), ok

    def _record_readings(self, samples: list[dict], now: float) -> None:
        """Note the newest usable reading seen for each unit polled.

        Keyed on the *sample's own* timestamp rather than the poll time, so a
        unit that has not published since last night is correctly measured as
        idle from last night. That distinction is the whole point: with `now` as
        the clock, a battery polled hourly with a 36h publication gap would
        never look idle, and one polled against a 12h gap would look idle every
        afternoon.
        """
        for sample in samples:
            unit = sample.get("unit")
            if not unit:
                continue
            if sample.get("scrape_success"):
                stamp = sample.get("sampled_at") or now
                # A clock skewed into the future would pin the unit as fresh
                # forever, so never trust a stamp past the poll.
                if stamp > now:
                    stamp = now
                previous = self._last_reading.get(unit)
                if previous is None or stamp > previous:
                    self._last_reading[unit] = stamp
            else:
                # First sight of a unit that returned nothing. Start its clock so
                # it is judged from now rather than being immortal, but do not
                # move an existing stamp - a failed poll must not reset progress
                # towards dropping it.
                self._last_reading.setdefault(unit, now)

    def _probe_demoted(self, rows: list[dict], demoted: list[dict], now: float) -> list[dict]:
        """Optionally re-ask a demoted unit whether it has started publishing.

        Off by default (`watchlist_per_cycle=0`) because it is the only part of
        the rotation that costs anything: a demoted unit is otherwise never
        asked again, so it can only return by outranking a unit that fails, or
        by a restart clearing the in-memory state. Turned on it round-robins one
        unit per cycle, so a battery that comes back is back within
        `len(demoted)` cycles - at the cost of that many extra requests a cycle.
        """
        if not self.watchlist_per_cycle or not demoted:
            return demoted
        # Round-robin from the largest demoted unit so the most capacity at
        # stake is retried first.
        order = {row["unit"]: index for index, row in enumerate(demoted)}
        probe = sorted(
            demoted, key=lambda r: (order[r["unit"]] + self._probe_offset) % max(1, len(demoted))
        )[: self.watchlist_per_cycle]
        try:
            result = poll_cycle(
                self.client, probe, self.network, self.lookback_hours, self.max_sample_age, now
            )
        except AuthError:
            raise
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.warning("watchlist probe failed: %s", exc)
            return demoted
        self._record_readings(result["samples"], now)
        self._probe_offset = (self._probe_offset + self.watchlist_per_cycle) % max(1, len(demoted))
        if any(s.get("scrape_success") for s in result["samples"]):
            log.info("watchlist: a demoted unit is publishing again")
        return demoted

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.exception("poll cycle raised; continuing")
            # Jitter so two clones of this stack do not land on the same minute
            # and jointly exceed the 8-per-5min bucket.
            interval = self.poll_interval * (0.95 + 0.1 * random.random())
            self._stop.wait(interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="oe-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # -- serving ------------------------------------------------------------ #

    def exposition(self) -> tuple[str, bool]:
        with self._lock:
            state = self._state
            health = self._health
        if not state:
            return render({}, self.now_fn()), health[0]
        return render(state, self.now_fn()), health[0]

    def health(self) -> tuple[bool, str]:
        with self._lock:
            return self._health


# --------------------------------------------------------------------------- #
# Fleet cache
# --------------------------------------------------------------------------- #


def load_cache_entry(path: str) -> tuple[list[dict], float] | None:
    """Read the cached fleet as `(facilities, cached_at)`, or None if unusable.

    `cached_at` is what lets the caller tell a fresh cache from a stale one; it
    is recorded by `save_cache` and is 0.0 for a cache file written by hand.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("fleet cache %s unusable: %s", path, exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list) or not data:
        log.warning("fleet cache %s has no facilities", path)
        return None
    cached_at = float(payload.get("cached_at") or 0.0) if isinstance(payload, dict) else 0.0
    return data, cached_at


def resolve_liveness_path(explicit: str | None, fleet_file: str | None) -> str | None:
    """Where liveness is persisted: explicit, or beside the fleet cache.

    Deriving it from `--fleet-cache-file` means a deployment that already has a
    cache volume gets crash-safe liveness without a second flag to remember, and
    one with neither gets no persistence rather than a surprise file in the CWD.
    """
    if explicit:
        return explicit
    if fleet_file:
        return os.path.join(os.path.dirname(fleet_file) or ".", "liveness.json")
    return None


def load_liveness(path: str) -> dict[str, float]:
    """Read persisted per-unit liveness, or {} if there is nothing usable.

    Deliberately a plain JSON file and not sqlite. The state is one float per
    battery unit - a few kB for the whole fleet - written by the one poll
    thread and read once at start-up. A database would add a schema, migrations
    and locking around a flat dict with no queries over it, and would make the
    file unreadable with `cat` at 3am, which is the only time anyone will want
    to read it.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("liveness file %s unusable, starting cold: %s", path, exc)
        return {}
    data = payload.get("last_reading") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        if data is not None:
            log.warning("liveness file %s has no last_reading map", path)
        return {}
    out: dict[str, float] = {}
    for unit, stamp in data.items():
        if not isinstance(unit, str):
            continue
        try:
            out[unit] = float(stamp)
        except (TypeError, ValueError):
            log.warning("liveness file %s: ignoring non-numeric stamp for %s", path, unit)
    return out


def save_liveness(path: str, last_reading: dict[str, float]) -> None:
    """Persist liveness so a restart does not reset the idle clock.

    This stores *liveness only* - when each unit was last seen publishing.
    No reading, no SOC, no energy value is ever written here, so a restart can
    never serve a stale SOC as if it were current; that failure mode is the one
    the exporter refuses everywhere else and it is not negotiable here either.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"cached_at": time.time(), "last_reading": last_reading},
                handle,
                indent=1,
                sort_keys=True,
            )
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write liveness file %s: %s", path, exc)


def load_cache(path: str) -> list[dict] | None:
    entry = load_cache_entry(path)
    return entry[0] if entry is not None else None


def save_cache(path: str, facilities: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"cached_at": time.time(), "data": facilities}, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write fleet cache %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


INDEX_BODY = """<!doctype html>
<title>NEM battery state of charge</title>
<h1>NEM battery state of charge</h1>
<p>Derived from OpenElectricity: stored energy / registered storage capacity.</p>
<p>The ranked fleet is the NEM and WEM battery fleet; units in scope can be in
either network, so per-unit queries are sent to each facility's own
<code>network_id</code>.</p>
<ul>
  <li><a href="/metrics">/metrics</a></li>
  <li><a href="/healthz">/healthz</a></li>
</ul>
"""


def make_handler(scraper: BatteryScraper):
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


def read_api_key(args) -> str:
    """Resolve the key from a file or the environment, never from argv."""
    if args.api_key_file:
        with open(args.api_key_file, "r", encoding="utf-8") as handle:
            key = handle.read().strip()
    else:
        key = os.environ.get(args.api_key_env, "").strip()
    if not key:
        raise SystemExit(
            "no API key: pass --api-key-file, or set %s in the environment"
            % args.api_key_env
        )
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--network", default=DEFAULT_NETWORK)
    parser.add_argument(
        "--api-key-file",
        help="file containing the OpenElectricity bearer token, e.g. a Docker secret",
    )
    parser.add_argument(
        "--api-key-env",
        default="OPENEA_API_KEY",
        help="environment variable holding the bearer token (default OPENEA_API_KEY)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help="monitor only the N largest batteries by storage capacity (default %d). "
        "Each one costs one API request per poll, and the free plan rate-limits "
        "to 366 requests/day, so 10 at an hourly poll is 240/day." % DEFAULT_TOP,
    )
    parser.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument(
        "--max-sample-age",
        type=float,
        default=DEFAULT_MAX_SAMPLE_AGE,
        help="seconds before a sample is dropped rather than exported (default %d, "
        "which is comfortably longer than the feed's overnight publication cycle)"
        % int(DEFAULT_MAX_SAMPLE_AGE),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds between poll cycles (default %d). This, not the Prometheus "
        "scrape interval, is what sets the API request rate." % int(DEFAULT_POLL_INTERVAL),
    )
    parser.add_argument(
        "--fleet-refresh-interval",
        type=float,
        default=DEFAULT_FLEET_REFRESH_INTERVAL,
        help="seconds between re-reads of the fleet metadata (default %d; capacity "
        "changes a few times a year)" % int(DEFAULT_FLEET_REFRESH_INTERVAL),
    )
    parser.add_argument(
        "--fleet-cache-file",
        help="where the fleet metadata is cached, so a restart without API access "
        "still has something to poll",
    )
    parser.add_argument(
        "--liveness-file",
        help="where per-unit liveness (when each battery was last seen publishing) "
        "is persisted, so a restart does not reset the idle clock and hand every "
        "unreadable battery a fresh 36h reprieve. Liveness only - readings and SOC "
        "are never cached. Defaults to liveness.json beside --fleet-cache-file",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL,
        help="minimum seconds between API requests (default %.1f, which keeps the "
        "exporter inside the plan's documented 2/s burst limit). Does not "
        "satisfy the 8-per-5minutes bucket, which 13 requests in one cycle "
        "exceeds regardless of spacing; 38 would, at 8 minutes per hour cycle"
        % DEFAULT_REQUEST_INTERVAL,
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-budget", type=float, default=6.0)
    parser.add_argument(
        "--include-undispatched",
        action="store_true",
        help="also consider battery units that have never been observed dispatching. "
        "Off by default because the largest units in the fleet are mostly "
        "committed projects that return no data at all, and including them would "
        "spend the daily request budget on empty series.",
    )
    parser.add_argument(
        "--drop-idle-hours",
        type=float,
        default=DEFAULT_DROP_IDLE_SECONDS / 3600.0,
        help="drop a battery from scope when no usable reading has arrived for "
        "this long, promoting the next-largest candidate into its slot. 0 "
        "disables the rotation and keeps a strict capacity top-N. The default "
        "matches the sample-age limit, so a unit has to miss a whole night's "
        "publication before it is judged idle - counting consecutive empty polls "
        "instead would demote healthy batteries every afternoon, because the feed "
        "only publishes overnight.",
    )
    parser.add_argument(
        "--watchlist-per-cycle",
        type=int,
        default=0,
        help="re-ask this many demoted batteries per cycle whether they have "
        "started publishing, round-robin. Off by default because it is the only "
        "part of the rotation that costs an extra request: a demoted unit is "
        "otherwise never polled again, so it can only return by outranking a "
        "battery that fails, or by a restart. Each unit here costs one more "
        "request per cycle.",
    )
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9111)
    parser.add_argument(
        "--no-background-poll",
        action="store_true",
        help="serve /metrics but never poll; used by tests that drive poll_once()",
    )
    parser.add_argument("--once", action="store_true", help="run one poll, print exposition, exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        api_key = read_api_key(args)
    except OSError as exc:
        raise SystemExit("could not read API key file: %s" % exc)

    client = OpenElectricityClient(
        api_key,
        base_url=args.api_base,
        timeout=args.timeout,
        retries=args.retries,
        retry_budget=args.retry_budget,
        request_interval=args.request_interval,
    )
    scraper = BatteryScraper(
        client,
        network=args.network,
        top=args.top,
        lookback_hours=args.lookback_hours,
        max_sample_age=args.max_sample_age,
        poll_interval=args.poll_interval,
        fleet_refresh_interval=args.fleet_refresh_interval,
        fleet_file=args.fleet_cache_file,
        liveness_file=resolve_liveness_path(args.liveness_file, args.fleet_cache_file),
        require_data=not args.include_undispatched,
        drop_idle_seconds=args.drop_idle_hours * 3600.0,
        watchlist_per_cycle=args.watchlist_per_cycle,
    )

    if args.once:
        body, success = scraper.poll_once()
        sys.stdout.write(body)
        return 0 if success else 1

    if not args.no_background_poll:
        scraper.start()

    server = ThreadingHTTPServer((args.listen_address, args.port), make_handler(scraper))
    log.info(
        "serving http://%s:%d/metrics, polling %d largest batteries every %ds",
        args.listen_address,
        args.port,
        args.top,
        int(args.poll_interval),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        scraper.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
