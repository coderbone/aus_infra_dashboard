#!/usr/bin/env python3
"""Prometheus exporter for NEM/WEM battery state of charge, from OpenElectricity.

Source: the OpenElectricity API (formerly OpenNEM), at
https://api.openelectricity.org.au . Two endpoints, both bearer-authenticated:

    GET /facilities/?fueltech_id=battery
        Fleet metadata. Every battery facility, its units, and per unit the
        fields that matter here: `capacity_storage` (MWh) and `data_last_seen`.

    GET /data/facilities/NEM?metrics=storage_battery&metrics=power&facility_code=<CODE>
        Time series for one facility, one series per *unit* per metric, named
        `<METRIC>_<UNIT_CODE>`:

            storage_battery_<UNIT>   energy stored, MWh
            power_<UNIT>             charge/discharge power, MW

        Both metrics are asked for in one request by repeating `metrics`. That is
        deliberate and is the whole reason this is not two calls: a facility
        costs one request out of a 366/day bucket, so a second call per battery
        would double the budget to obtain a number that is free to fetch
        alongside.

There is no "state of charge" metric. The API publishes stored **energy in MWh**
and the fleet metadata publishes **capacity in MWh**, so SOC is derived here as

    soc = storage_battery / capacity_storage

and exported as a 0-1 ratio. Nothing upstream reports a percentage, and
`capacity_registered` is MW (power) and must not be used as the denominator -
that would be a units error that still produces a plausible-looking number.

`power` publishes no unit per series, but each block of the response declares
one - the `power` block came back as `MW` on 2026-10-01 - and the exporter
checks that against the unit in the exported metric's name rather than assuming
it. The values corroborate it: on that date the seven usable units reported
-415 to +325, against `capacity_registered` of 150-500 MW for the same units.
The sign convention is read off the data as well - negative while charging,
positive while discharging - and is consistent with a bidirectional
dispatch_type. Nothing upstream states the sign, so it is carried in the metric's
HELP rather than assumed silently.

Four upstream quirks this is built around, all measured on 2026-09-30 rather
than assumed:

1. **Six series per facility, two usable.** A battery facility returns
   `<CODE>1` plus `<CODE>G1` and `<CODE>L1`, for each of the two metrics. Only
   `<CODE>1` exists in the fleet metadata; G1 and L1 have no
   `capacity_storage`, so no SOC can be computed for them, and their values
   track the battery's rather than being independent storage. Summing all three
   would triple-count the same MWh. Only series whose unit code is present in
   the metadata with a positive `capacity_storage` are exported; the rest are
   counted in `oe_battery_series_without_capacity` so the gap stays visible.

2. **The aggregate endpoint is unusable.** `?metrics=storage_battery` with no
   `facility_code` returns a single series named `storage_battery_total`
   holding ~14k points with **repeated timestamps and no unit attribution** -
   values from many units concatenated flat. It cannot be split back into units
   and summing it is meaningless. Hence one request per facility, and the
   per-facility `columns: {"unit_code": ...}` is the only thing that makes the
   numbers attributable.

3. **The energy feed is legitimately sparse; the power feed is not.** As of
   2026-10-01 `storage_battery` carries values only for roughly 18:00-04:00
   local and nulls for the rest of the day, while `power` updates all day for
   the same units. Both are correct and neither is a fault, so they must not be
   reported as one. The exporter therefore exports the most recent *non-null*
   sample of each within `--lookback-hours` together with its own real
   timestamp and age, and drops a series only once that metric's newest sample
   is older than `--max-sample-age` - so a genuinely dead feed loses its series
   rather than repeating an ancient reading as if it were current.

   The two ages are kept apart deliberately. `oe_battery_sample_age_seconds`
   answers "how old is this SOC?", and `oe_battery_power_sample_age_seconds`
   answers "how old is this power?"; collapsing them would make a battery with
   yesterday's energy and today's power look fresh in both. Liveness is the one
   place they are combined, and it takes the newer of the two: a battery that is
   plainly dispatching is not idle, however quiet its energy series is.

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
    oe_battery_power_mw                        MW, + discharging / - charging
    oe_battery_last_sample_timestamp_seconds   when that value was observed
    oe_battery_sample_age_seconds              its age at export time
    oe_battery_power_sample_timestamp_seconds  when the power value was observed
    oe_battery_power_sample_age_seconds        its age at export time
    oe_battery_scrape_success                  1 = SOC read, 0 = not
    oe_battery_capacity_rank                   1 = largest
    oe_battery_idle_seconds                    age of newest reading, either feed

Synthetic, only with --enable-inferred and only for units with no measured
reading this cycle. Kept in their own family so they can never be mistaken
for, or silently substituted into, the measured series above:

    oe_battery_soc_inferred_ratio              SOC, 0-1, from integrating power
    oe_battery_energy_inferred_mwh             MWh, the value it came from
    oe_battery_inferred_timestamp_seconds      newest power point integrated
    oe_battery_inferred_age_seconds            its age at export time
    oe_battery_inferred_saturated              1 = the integral left [0, cap]

Evidence about --infer-charge-efficiency, only with --efficiency-calibration.
In their own family again, because these are neither a SOC nor a measurement of
one - they are how far the loss term behind every inferred value above is
trusted. Two per-battery, and one fleet-wide verdict:

    oe_battery_inferred_calibration_pairs      scored reading pairs, per unit
    ..._calibration_error_ratio                median miss as a fraction of cap
    ..._calibration_recommended_efficiency     fleet-wide, ABSENT if unsupported
    ..._calibration_plausible_pairs            fleet-wide, count scored

The per-unit series carry the same labels as every other per-battery metric. The
recommendation is deliberately unlabelled and deliberately absent by default:
it exists to be read by a person, is never applied by the exporter, and `0` would
be a live instruction to set the coefficient to zero rather than an absence of
opinion.

Unlabelled fleet and poll gauges:

    oe_batteries_enumerated                    fleet size
    oe_batteries_in_scope                      after --top
    oe_batteries_monitored                     with a usable SOC sample
    oe_battery_fleet_capacity_mwh              whole fleet
    oe_battery_monitored_capacity_mwh          monitored subset
    oe_battery_series_without_capacity         quirk 1
    oe_battery_series_too_stale                dropped
    oe_batteries_inferred                      units carrying a synthetic SOC
    oe_poll_cycle_duration_seconds             wall time
    oe_last_poll_timestamp_seconds             when
    oe_last_fleet_refresh_timestamp_seconds     metadata age
    oe_api_credits_remaining                   daily budget
    oe_api_requests_total                      counter, includes failures

`oe_battery_scrape_success` is 0 for a facility whose *energy* reading could not
be read, and the SOC series is **omitted** for it rather than repeated, so a
failure shows as a gap in the gauge plus this flag, matching the sibling
exporters. The flag is about state of charge only: `oe_battery_power_mw` is
published independently of it, so a unit can report power while this reads 0.
That combination is real and expected - `storage_battery` is an overnight series
and `power` is not - so the two ages are published separately rather than
collapsed into one.

Inferred SOC is the daytime gap-filler, and it is deliberately cautious. Each
estimate is anchored to a real measured reading and integrated over at most
`--max-infer-hours`, so error cannot compound across days; inference is skipped
entirely when there is no anchor, when the anchor is too old, or when no power
sample postdates it. Measured always wins: a unit whose reading is younger than
`--infer-fresh-hours` exports no inferred value at all, so there is never a
moment when the two families both claim the same cycle.
`oe_battery_scrape_success` stays tied to the measured reading, so inference
never inflates the "has a reading" count.

Accuracy, measured against the live feed on 2026-10-01 by integrating from one
measured reading to the next: over a 1-hour window the median error is 0.25% of
capacity (n=140), and across the ~14-hour daytime gap it grows to a mean of 7.4%
of capacity (n=7). The drift was one-directional - the integration consistently
over-predicted stored energy, which is what ignoring conversion losses looks
like. `--infer-charge-efficiency` is the correction for that: a single share of
grid-facing charging that reaches the cells, applied to charging only, because
the discharge side is already metered at the terminals. The default stays 1.0,
which assumes no loss and therefore nothing about the hardware; the deployment
sets 0.9, which roughly halves the long-window error and leaves the 1h case
untouched. Treat this family as a trend line, not a measurement, and keep
`--max-infer-hours` short if you want it to mean anything.

Because of that, `oe_battery_inferred_saturated` exists: when the raw integral
leaves [0, capacity] the value is clamped, and a clamped 1.0 is otherwise
indistinguishable from a battery that is genuinely full.

Two things that look like they would sharpen this, and are not worth doing.
Polling faster does not help: at the shipped `interval=5m` the upstream series
is already fully resolved, so two fetches a minute apart return byte-identical
data while costing four times the requests - and at 14 requests per cycle the
deployment is already at 336 of the plan's 366 per day. Ask for a *coarser*
interval and you lose accuracy for nothing. Conversely the obvious win -
requesting the 5-minute series instead of the hourly one - is free and roughly
halves the error at every horizon, because the hourly value is only the
arithmetic mean of the twelve 5m samples under it. `--power-history-file`
exists for the separate reason that a local copy of the power series keeps
inference integrating across a restart or a failed scrape.

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

# How far back a poll actually *asks* for, once every in-scope unit's last energy
# reading is held locally. Distinct from `DEFAULT_LOOKBACK_HOURS` above, which is
# the *reach* - the worst case a cycle may need - rather than the everyday cost.
#
# The two were the same number until 2026-10-06, which is what made each cycle
# cost 494 KB and 13.6k points per facility: a wide window has to be re-fetched
# whole every hour to pick up the handful of samples that are new, because the
# request is all-or-nothing. Measured against ERB, both metrics, `5m`:
#
#     192h -> 494 KB / 13654 points      2h -> 4.8 KB / 96 points
#      48h -> 120 KB /  3288 points    1.5h -> 3.7 KB / 72 points
#      24h ->  68 KB /  1680 points     1h -> 3.0 KB / 48 points
#
# 2h is the floor that is still safely wider than a poll interval: at
# `--poll-interval=3600` a poll that runs slightly late must not re-ask for less
# than a full interval, and 2x leaves room for the clock jitter in `_loop` plus a
# dropped sample at the seam. Going narrower buys little - 1h is only another
# 1.5 KB - and costs margin.
#
# Note this changes bytes per request, not the number of requests, which is what
# the plan's 366/day bucket counts. It does nothing for the rate limit.
DEFAULT_POLL_WINDOW_HOURS = 2.0

# The `interval` query parameter on the one battery request. `5m` rather than
# `1h` because it is free - same endpoint, same request count, same credits -
# and it roughly halves inferred-SOC error at every horizon, since the hourly
# value is only the arithmetic mean of the five-minute ones underneath it.
DEFAULT_API_INTERVAL = "5m"

# How far the last observed power rate may be carried forward past the newest
# sample. The poll runs hourly, so the normal hold is a few minutes and this
# bound is never reached in steady state; it exists so that a poll that stops
# arriving degrades into a frozen line with a growing age rather than a value
# extrapolated indefinitely from a rate nobody is confirming.
DEFAULT_INFER_MAX_HOLD_HOURS = 6.0

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

DEFAULT_POLL_INTERVAL = 600.0
# NOTE: this default is *not* safe for a full fleet. 10 units at 600s is 1440
# requests/day against the plan's 366/day bucket, so the default assumes a
# reduced scope. `docker-compose.yml` passes `--poll-interval=3600` explicitly,
# which is 288 requests/day for the deployed 12 batteries. Changing this value
# to match the deployment is fine and would be an improvement; what must not
# happen is reading 600 as the budgeted figure and dropping the compose flag.
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
        interval: str = DEFAULT_API_INTERVAL,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_budget = retry_budget
        self.request_interval = max(0.0, request_interval)
        self.interval = interval
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

    def get(self, path: str, params: dict | list | None = None) -> dict:
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

    # Upstream metric name -> the short key used everywhere below. Both are asked
    # for in the one call `battery_metrics` makes, and each series arrives named
    # `<METRIC>_<UNIT_CODE>`.
    METRICS = (("storage_battery", "storage"), ("power", "power"))
    # Unit each metric's *block* declares. The series themselves carry no unit,
    # but the block does - `power` came back as "MW" on 2026-10-01 - so this is
    # checked rather than assumed. A units error here would still plot as a
    # plausible-looking line, which is the whole failure mode to avoid.
    METRIC_UNITS = {"storage": "MWh", "power": "MW"}
    _SHORT = {name: short for name, short in METRICS}

    def battery_metrics(
        self,
        facility_code: str,
        network: str,
        lookback_hours: float,
        now: float,
        full_series: bool = False,
        window_hours: float | None = None,
    ) -> list[tuple[str, str, tuple[float, float]]]:
        """Newest non-null sample of each metric, for one facility.

        Returns `[(unit_code, "storage"|"power", (epoch, value)), ...]` - one
        entry per series the facility published, which is what lets the caller
        count the series it has no capacity for without re-parsing names.

        With `full_series`, every non-null point of each series is returned
        instead of only the newest. That is what inferred SOC integrates over,
        and it costs nothing extra: the response already carries the whole
        window either way, so the exporter simply stops discarding most of it.
        The number of *requests* is what the budget counts, and this changes
        none.

        Both metrics go out in a single request, as `metrics=storage_battery`
        plus `metrics=power`. The API treats a repeated parameter as a
        multi-meter query and answers with one block per metric; the exporter
        does not spend a second request per battery to get the power value,
        because that would double a budget measured in requests per day.

        The window is `date_start` .. `date_end` with `interval=<self.interval>`,
        `5m` by default. This is a query parameter, not a different endpoint, so
        it costs exactly the same single request as `1h` - and it is worth
        roughly a factor of two on inferred SOC, because the finer series
        captures the intra-hour shape that a single hourly average flattens out.

        **`window_hours` is the lever that actually controls cost.** `interval`
        is free; the *width* of the range is not, and the response grows linearly
        with it. Measured on 2026-10-06 against ERB, both metrics, `5m`:

            192h -> 494 KB, 13654 points     2h -> 4.8 KB, 96 points
             48h -> 120 KB,  3288 points   1.5h -> 3.7 KB, 72 points
             24h ->  68 KB,  1680 points    1h -> 3.0 KB, 48 points

        So `lookback_hours` is the *reach* - how far back the caller may need to
        see - and `window_hours` is how much of that reach this request actually
        asks for. They differ because the only thing forcing a wide window is the
        need to re-read a `storage_battery` reading that is hours old, and
        `ReadingCache` holds that locally, so after start-up the window only has
        to cover the gap since the last poll. See `ReadingCache` for the full
        argument, including why caching a *reading* is safe here when the
        liveness file deliberately refuses to.

        Passing `window_hours` larger than `lookback_hours` is capped rather than
        honoured: the reach is a hard requirement, not a preference, and a caller
        that asks for more of it than it declared is a bug.
        """
        end = now
        reach = max(1.0, lookback_hours)
        if window_hours is not None:
            reach = max(1.0, min(reach, float(window_hours)))
        start = end - reach * 3600.0
        params = [("metrics", name) for name, _ in self.METRICS]
        params += [
            ("facility_code", facility_code),
            ("interval", self.interval),
            ("date_start", iso_local(start)),
            ("date_end", iso_local(end)),
        ]
        payload = self.get("/data/facilities/%s" % urllib.parse.quote(network), params)
        out: list[tuple[str, str, tuple[float, float]]] = []
        for block in payload.get("data") or []:
            metric = block.get("metric")
            expected = self.METRIC_UNITS.get(self._SHORT.get(metric))
            declared = block.get("unit")
            if expected and declared and declared != expected:
                # Keep exporting - a wrong unit label is upstream's problem, and
                # refusing to export would hide the drift instead of naming it -
                # but say so, because the export name claims a unit.
                log.warning(
                    "%s block for %s declares unit %r, expected %r; exporting as %s",
                    metric, facility_code, declared, expected, expected,
                )
            for series in block.get("results") or []:
                name = series.get("name") or ""
                key = None
                for upstream, short in self.METRICS:
                    if name.startswith(upstream + "_"):
                        key, suffix = short, name[len(upstream) + 1 :]
                        break
                if key is None:
                    # Only the two requested metrics can come back, so an
                    # unrecognised name is an upstream change rather than a
                    # series to report. Skipping it does not drop data: there is
                    # nothing in it that is not in a series we did ask for.
                    log.debug("ignoring unrecognised series %r from %s", name, facility_code)
                    continue
                # The response labels the unit in `columns`; the series name is
                # the fallback for a response that omits it.
                columns = series.get("columns") if isinstance(series.get("columns"), dict) else {}
                unit_code = columns.get("unit_code") or suffix
                points = series.get("data") or []
                if full_series:
                    for stamp, value in usable_points(points):
                        out.append((unit_code, key, (stamp, value)))
                    continue
                latest = latest_sample(points)
                if latest is None:
                    continue
                out.append((unit_code, key, latest))
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


def usable_points(points: list) -> list[tuple[float, float]]:
    """Every well-formed `(epoch, value)` in an upstream `[[ts, value], ...]`.

    Nulls and non-numeric values are skipped rather than treated as zero, which
    is the whole point of quirk 3: a null means "not published for this
    interval", and reading it as 0 would report a charged battery as flat and
    empty twice a day.
    """
    out: list[tuple[float, float]] = []
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
        out.append((epoch, float(value)))
    out.sort()
    return out


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
                    # Named for what it is, not `power_mw`: `power_mw` is the
                    # battery's *instantaneous* dispatch power from the data
                    # endpoint, and the two would be silently interchangeable
                    # when a sample row is built from this one.
                    "registered_mw": unit.get("capacity_registered"),
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
    full_series: bool = False,
    window_hours: float | None = None,
    reading_cache: "ReadingCache | None" = None,
) -> dict:
    """One request per in-scope facility, reduced to one row per battery.

    Each row carries both metrics, which arrive together in the same response:
    `soc`/`stored_mwh`/`sampled_at` from `storage_battery`, and
    `power_mw`/`power_sampled_at` from `power`. They are kept apart all the way
    to the exposition because they age independently - one is an overnight
    series and the other is not - and a single `sampled_at` cannot honestly
    describe both.

    `window_hours` narrows the request; `lookback_hours` remains the hard reach.
    `reading_cache` supplies the last energy reading when the narrowed window
    does not reach it, which is what allows the narrowing to be safe - see
    `ReadingCache`. A row filled from the cache is flagged `reading_from_cache`
    so the substitution is visible in the exposition rather than silent.
    """
    by_unit = {row["unit"]: row for row in rows}
    samples: list[dict] = []
    too_stale = 0
    # Series returned upstream whose unit has no capacity, as (facility, unit,
    # metric). A set across the whole cycle: `full_series` yields every point
    # of a series, and counting points would inflate this by the window length.
    uncapped: set[tuple[str, str, str]] = set()
    cached_readings = 0

    for row in rows:
        try:
            series = client.battery_metrics(
                row["facility"],
                row.get("network") or network,
                lookback_hours,
                now,
                full_series=full_series,
                window_hours=window_hours,
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
                    power_mw=None,
                    power_sampled_at=None,
                    power_series=None,
                    scrape_success=False,
                    # Nothing was fetched, so nothing was cached into the row.
                    # The anchor cache is *not* consulted on this path: a
                    # facility that failed to answer is a fact about the feed,
                    # and papering over it with a cached reading would report a
                    # battery as monitored during an outage it actually failed.
                    reading_from_cache=False,
                )
            )
            continue

        values: dict[str, tuple[float, float]] = {}
        power_series_list: list[tuple[float, float]] = []
        for unit_code, key, (stamp, value) in series:
            known = by_unit.get(unit_code)
            if known is None or not known.get("capacity_mwh"):
                # quirk 1: G1/L1 have no capacity, so no SOC exists for them -
                # and nothing for power either, since the two travel together.
                # Counted per *series*, not per point: `full_series` hands over
                # every sample of a series, and a counter that multiplied by the
                # window length would report ~740 uncapped "series" for a fleet
                # with 37 of them.
                uncapped.add((row["facility"], unit_code, key))
                continue
            if unit_code != row["unit"]:
                # Another in-scope unit of the same facility. It has its own
                # request, and emitting it from this one too would put two
                # samples with identical labels in a single exposition, which
                # Prometheus rejects outright.
                continue
            if now - stamp > max_sample_age:
                too_stale += 1
                continue
            values[key] = (stamp, value)
            if key == "power":
                power_series_list.append((float(stamp), float(value)))

        stored = values.get("storage")
        power = values.get("power")
        from_cache = False
        if stored is None and reading_cache is not None:
            # The narrowed window did not reach the last energy reading. With a
            # wide window this row would have carried that reading, so serving it
            # from the cache keeps every exported value identical to the
            # wide-window behaviour - including its true age, because the cached
            # timestamp travels with it. What changes is only that we did not
            # re-download it.
            found = reading_cache.get(row["unit"], now)
            if found is not None:
                stored = found
                from_cache = True
                cached_readings += 1
        samples.append(
            dict(
                row,
                soc=(
                    None
                    if stored is None
                    else max(0.0, min(1.0, stored[1] / row["capacity_mwh"]))
                ),
                stored_mwh=None if stored is None else stored[1],
                sampled_at=None if stored is None else stored[0],
                power_mw=None if power is None else power[1],
                power_sampled_at=None if power is None else power[0],
                scrape_success=stored is not None,
                reading_from_cache=from_cache,
                power_series=sorted(power_series_list),
            )
        )
        if sleep_between:
            time.sleep(sleep_between)

    return {
        "samples": samples,
        "series_without_capacity": len(uncapped),
        "series_too_stale": too_stale,
        "readings_from_cache": cached_readings,
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


def trapezoid(p0: float, t0: float, p1: float, t1: float) -> float:
    """`∫P dt` between two sampled powers, MWh.

    Power is published hourly, so a rectangle rule would put a step's whole
    energy at its left edge and carry the error a full hour forward. The
    trapezoid splits the interval between the two endpoints instead, which is
    exact for a linear ramp and about half the error of a step otherwise.

    Signed as the upstream reports it: negative while charging, positive while
    discharging. Callers integrating into *stored energy* subtract the result,
    since the battery's content moves opposite to its power.
    """
    dt = t1 - t0
    if dt <= 0:
        return 0.0
    # MW x h = MWh, so the elapsed time has to come in as hours. Leaving it in
    # seconds is silently a factor of 3600 out and produces a number that still
    # clamps to something plausible, which is the worst kind of bug: 100 MWh of
    # charging would read as 360,000 MWh and pin every battery at empty.
    hours = dt / 3600.0
    return hours * (p0 + p1) / 2.0


def stored_energy_delta(
    p0: float, t0: float, p1: float, t1: float, charge_efficiency: float = 1.0
) -> float:
    """Change in a battery's stored energy over one segment, MWh.

    Positive means the battery gained energy. `charge_efficiency` is the share
    of grid-facing charging that actually lands in the cells; the remainder is
    lost to conversion losses and is never recoverable, so the battery stores
    `charge_efficiency x |charging energy|` rather than all of it.

    Only charging is discounted. Discharge is charged at the full amount,
    because the meter on the other side of the battery already reports what left
    the terminals - there is nothing further to discount it by. That makes the
    coefficient a *round-trip* figure: at 0.9, 100 MWh into the grid connection
    puts 90 MWh in the cells and roughly 90 MWh comes back out.

    Without this term the integration systematically **over**-predicts stored
    energy, which is what the live backtest showed: the error was
    one-directional across every unit and every window.

    A segment that crosses zero is split at the crossing, because charging and
    discharging have to be weighted differently and averaging across the switch
    point would apply the wrong factor to part of the interval.
    """
    if t1 <= t0:
        return 0.0
    if p0 >= 0 and p1 >= 0:
        # Pure discharge: the full integral leaves the battery.
        return -trapezoid(p0, t0, p1, t1)
    if p0 <= 0 and p1 <= 0:
        # Pure charge: only the efficient share is stored.
        return -trapezoid(p0, t0, p1, t1) * charge_efficiency
    # Straddles zero. Power is linearly interpolated between the samples, so
    # the crossing time is exact for the same reason the trapezoid is.
    fraction = -p0 / (p1 - p0)
    cross_t = t0 + (t1 - t0) * fraction
    # The first leg keeps `p0`'s sign and the second keeps `p1`'s, so which leg
    # is discounted depends on which way the segment was heading. Discounting the
    # first leg unconditionally is right for charge -> discharge and exactly
    # backwards for discharge -> charge, where it both mis-weighted the two legs
    # and flipped the sign of the result: an hour discharging at 120 MW then an
    # hour charging at 30 MW reported +3 MWh gained instead of -48 MWh lost.
    # Batteries cross zero in that direction every time dispatch ends, so this
    # was reachable on ordinary overnight behaviour, not an edge case.
    first = -trapezoid(p0, t0, 0.0, cross_t)
    second = -trapezoid(0.0, cross_t, p1, t1)
    if p0 > 0:
        # Discharging first, charging second: only the charging leg is lossy.
        return first + second * charge_efficiency
    return first * charge_efficiency + second


def infer_soc(
    anchor_mwh: float,
    anchor_ts: float,
    power_series: list[tuple[float, float]],
    now: float,
    capacity_mwh: float,
    max_hours: float,
    clamp: bool,
    max_gap_hours: float = 2.0,
    charge_efficiency: float = 1.0,
    hold_rate: float | None = None,
    hold_max_hours: float = 6.0,
) -> dict | None:
    """Dead-reckon stored energy from the last measured SOC and power since.

    Returns a dict with the inferred MWh, the inferred ratio, and the epoch of
    the newest power sample actually integrated - or `None` when inference does
    not apply.

    The preconditions are deliberately strict, because a synthetic number that
    looks like a measurement is worse than no number at all:

    - A **measured** anchor is required. There is no integration from an
      inferred anchor: chaining inferences compounds each one's error, so the
      chain is cut at every measured reading and the drift cannot accumulate
      past a single overnight gap.
    - The anchor must be no older than `max_hours`. Past that the integral
      covers more un-sampled hours than sampled ones and the answer is
      arithmetic rather than measurement.
    - Past the newest sample, `hold_rate` carries the last observed power
      forward to `now`, scaled by the real elapsed time and capped at
      `hold_max_hours`. Pass `hold_rate=None` (the default) to stop at the
      newest sample instead - which is the honest floor: the value is then
      exactly the integral, and simply old.
    - Power samples must exist at or after the anchor. Nothing to integrate
      means nothing to say.
    - Nothing past `now` is integrated, so a future-dated sample cannot pull
      the estimate forward.
    - Gaps wider than `max_gap_hours` are **not** bridged: the segment is
      dropped rather than integrated across, because integrating across a hole
      invents hours the feed never reported and would quietly drain or fill the
      battery over exactly the periods where it is thinnest. The default is two
      intervals, which tolerates one missing hourly point without accepting a
      real outage.

    Integration therefore stops at the first wide gap: it would be wrong to
    resume after one, since the battery's state on the far side is unknown.

    `charge_efficiency` discounts the charging share of the integral, because
    grid-facing energy and stored energy differ by the conversion losses. It
    defaults to 1.0, i.e. no loss term, so this function makes no claim about
    hardware it was not told about; the deployment sets it explicitly.

    The hold is what makes the output a usable *time series* rather than a
    series of once-an-hour steps. `power` arrives on a fixed grid and `now`
    falls between those grid points, so without it every scrape in an interval
    reports the same number - technically exact, visually a staircase, and
    indistinguishable on a dashboard from a battery that stopped moving. The
    held value is an estimate in the way a speedometer needle is, not a
    measurement, so `hold_hours` is reported alongside it and the same capacity
    clamp applies: a battery held up to full stays at full and reports
    `saturated` rather than continuing past it.

    Auxiliary load and any non-battery losses upstream of the meter are still
    ignored, and the coefficient is assumed identical for every unit. Both are
    approximations, and they are the reason the anchor window is kept short.
    """
    if not power_series:
        return None
    if anchor_ts is None or anchor_mwh is None:
        return None
    if not capacity_mwh or capacity_mwh <= 0:
        return None
    if now - anchor_ts > max_hours * 3600.0:
        # Too much unverified time between anchor and now to integrate across.
        return None

    points = sorted(
        (float(t), float(p)) for t, p in power_series if float(t) >= anchor_ts and float(t) <= now
    )
    if not points:
        return None
    # Need at least the anchor and one later sample to form a segment.
    if len(points) < 2:
        return None

# `power` is signed from the *grid's* point of view: negative while the
    # battery charges, positive while it discharges. Stored energy moves the
    # other way, so each segment's contribution is **subtracted**. Adding it
    # would run the battery backwards - the real fixture shows why, and the
    # failure is not subtle: Eraring discharges at +152 MW from 22:00 and its
    # stored energy falls 322 -> 227 MWh over the same hour, so `+∫P` would
    # report it filling while it empties, and then clamp the whole fleet to
    # zero.
    energy = float(anchor_mwh)
    used_ts = points[0][0]
    gap_limit = max_gap_hours * 3600.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t1 - t0 > gap_limit:
            # Stop rather than continue. Resuming on the far side would apply
            # the integral to a battery whose state we no longer know, so the
            # estimate is reported as of the last continuous sample instead.
            break
        energy += stored_energy_delta(p0, t0, p1, t1, charge_efficiency)
        used_ts = t1
    if used_ts <= points[0][0]:
        # No segment survived: the samples are not continuous with each other
        # within the gap limit, so there is nothing to integrate.
        return None
    hold_hours = 0.0
    # Zero-order hold past the newest sample. The integration above is exact up
    # to `used_ts`; between that sample and `now` there is nothing measured, so
    # the last observed rate is carried forward and scaled by how long it has
    # actually been: an hour-old hourly rate contributes 1 hour of energy, a
    # five-minute-old one contributes 1/12th. That is what makes the line move
    # between polls instead of sitting on an hour-long plateau.
    #
    # Bounded three ways, because a carried-forward rate is an assumption and an
    # unbounded one silently becomes invention:
    #   - `max_hold_hours`, so a stale rate cannot be extrapolated forever;
    #   - the anchor check above, so the whole estimate still dies at the anchor;
    #   - the capacity clamp below, so a battery being pushed past full stops
    #     at full and says so via `saturated` rather than running away.
    lag = float(now) - used_ts
    if hold_rate and lag > 0.0:
        cap_hold = float(hold_max_hours) * 3600.0
        step = lag if lag <= cap_hold else cap_hold
        energy += stored_energy_delta(
            hold_rate, used_ts, hold_rate, used_ts + step, charge_efficiency
        )
        hold_hours = step / 3600.0
    # Snapshot after the hold, not before: a hold that pushes the estimate past
    # full is exactly the case `saturated` exists to report, and capturing `raw`
    # earlier would let a held battery sit pinned at capacity claiming to be a
    # clean measurement.
    raw = energy
    if clamp:
        energy = max(0.0, min(float(capacity_mwh), energy))
    return {
        "energy_mwh": energy,
        "soc": max(0.0, min(1.0, energy / float(capacity_mwh))),
        "inferred_ts": used_ts,
        "hold_hours": hold_hours,
        # Whether the raw integral was outside the envelope, whether or not
        # clamping is on. Reported rather than hidden, because a clamped value
        # is indistinguishable from a real 0% or 100% once it is in the
        # exposition - and saturation across a fleet is exactly what a units or
        # sign bug looks like.
        "saturated": 0.0 if 0.0 <= raw <= float(capacity_mwh) else 1.0,
    }


# Window-length buckets, hours, used when scoring calibration pairs. Pooling them
# would destroy the test the deployed coefficient was actually accepted on, which
# was "improve the long window and leave the short window alone" - a term that
# wins one and wrecks the other is invisible in a single mean.
CALIBRATION_BUCKETS = ((0.0, 2.0), (2.0, 6.0), (6.0, 12.0), (12.0, float("inf")))

# Below this many independent pairs, nothing is claimed. The notes record a
# symmetric loss term surviving a day on the strength of a long-window
# improvement over very few samples; the rejection of that term afterwards is the
# reason this floor exists.
DEFAULT_CALIBRATION_MIN_PAIRS = 5

# An implied efficiency outside this range is not a measurement. Above 1.0 the
# battery would have stored more than the grid delivered, which no passive
# storage can do; below 0.0 it would have stored nothing while charging hard.
# Either way the reading or the power series is wrong, and averaging it in would
# corrupt the very number it is supposed to inform.
IMPLIED_EFFICIENCY_RANGE = (0.0, 1.2)


def charge_discharge_split(p0: float, t0: float, p1: float, t1: float) -> tuple[float, float]:
    """Grid-facing energy charged and discharged over one segment, both positive.

    Obtained from `stored_energy_delta` at two efficiencies rather than by
    duplicating its zero-crossing logic. At efficiency 0 nothing is stored, so
    the result is exactly `-discharged`; at efficiency 1 everything is, so it is
    `charged - discharged`. Two calls therefore recover both legs exactly, and
    the split-at-crossing behaviour stays defined in one place - so this cannot
    drift away from what inference actually integrates.
    """
    if t1 <= t0:
        return 0.0, 0.0
    at_zero = stored_energy_delta(p0, t0, p1, t1, charge_efficiency=0.0)
    at_one = stored_energy_delta(p0, t0, p1, t1, charge_efficiency=1.0)
    discharged = max(0.0, -at_zero)
    charged = max(0.0, at_one + discharged)
    return charged, discharged


def calibrate_pair(
    power_series: list[tuple[float, float]],
    t0: float,
    e0: float,
    t1: float,
    e1: float,
    capacity_mwh: float | None = None,
    max_gap_hours: float = 2.0,
    min_charge_mwh: float = 1.0,
    tolerance_hours: float = 0.5,
) -> dict | None:
    """Score one pair of consecutive measured energy readings.

    Both ends were published by the feed, so `e1 - e0` is real measured change -
    this is the only honest ground truth available for the loss term, and the
    reason it is worth accumulating rather than assuming 0.9.

    Integrating the power series over the interval splits it into what the grid
    delivered and what the grid took, so the pair *implies* a coefficient:

        measured = efficiency x charged - discharged
        implied  = (measured + discharged) / charged

    Returns `None` when the pair cannot say anything, which is most of them:

    - fewer than two power samples, or a gap wider than `max_gap_hours`, so the
      interval is not covered. Integration stops at the gap rather than bridging
      it, exactly as inference does, and a pair whose far end was never reached
      is dropped rather than scored on part of its span.
    - the integration finishing more than `tolerance_hours` short of `t1`. The
      reading is published from its own timestamp, which can precede the newest
      power sample, so some slack is needed; too much and the tail of the
      interval is unaccounted for.
    - `charged` below `min_charge_mwh`, or below 1% of capacity. The implied
      coefficient divides by it, so a pair that barely charged produces an
      enormous number from a rounding error. A discharge-only pair carries no
      information about charging losses at all and is dropped for the same
      reason: `charged` is zero, so it is not a small denominator but none.

    An implied value outside `IMPLIED_EFFICIENCY_RANGE` is returned with
    `plausible=False` rather than discarded, so it can be counted - a run of
    implausible pairs is itself the signal that something upstream is broken.
    """
    if t1 is None or t0 is None or e0 is None or e1 is None:
        return None
    t0 = float(t0)
    t1 = float(t1)
    if t1 <= t0:
        return None

    points = sorted((float(t), float(p)) for t, p in (power_series or [])
                    if t0 <= float(t) <= t1)
    if len(points) < 2:
        return None

    gap_limit = float(max_gap_hours) * 3600.0
    charged = 0.0
    discharged = 0.0
    used_ts = points[0][0]
    for (s0, p0), (s1, p1) in zip(points, points[1:]):
        if s1 - s0 > gap_limit:
            break
        leg_charged, leg_discharged = charge_discharge_split(p0, s0, p1, s1)
        charged += leg_charged
        discharged += leg_discharged
        used_ts = s1
    if used_ts <= points[0][0]:
        return None
    if t1 - used_ts > float(tolerance_hours) * 3600.0:
        return None

    floor = float(min_charge_mwh)
    if capacity_mwh:
        floor = max(floor, 0.01 * float(capacity_mwh))
    if charged < floor:
        return None

    measured = float(e1) - float(e0)
    implied = (measured + discharged) / charged
    low, high = IMPLIED_EFFICIENCY_RANGE
    return {
        "t0": t0,
        "t1": t1,
        "window_hours": (t1 - t0) / 3600.0,
        "charged_mwh": charged,
        "discharged_mwh": discharged,
        "measured_delta_mwh": measured,
        "implied_efficiency": implied,
        "capacity_mwh": float(capacity_mwh) if capacity_mwh else None,
        "plausible": low <= implied <= high,
    }


def _calibration_bucket(hours: float) -> str:
    for low, high in CALIBRATION_BUCKETS:
        if low <= hours < high:
            if high == float("inf"):
                return "%gh+" % low
            # Spelled out rather than built from a format string: "%g-%sh" reads
            # as a left-align flag on the string conversion, which silently
            # renders "0-2.0h" and leaves the label unparseable on the way back.
            return "%g-%gh" % (low, high)
    return "?"


# Rank rather than midpoint for ordering buckets, so a label is only ever a
# display string and cannot change which window is treated as the longest.
_BUCKET_RANK = {
    _calibration_bucket(low): rank for rank, (low, high) in enumerate(CALIBRATION_BUCKETS)
}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def summarise_calibration(
    observations: list[dict],
    deployed_efficiency: float,
    min_pairs: int = DEFAULT_CALIBRATION_MIN_PAIRS,
    sweep_low: float = 0.70,
    sweep_high: float = 1.00,
    sweep_step: float = 0.02,
) -> dict:
    """Turn scored pairs into the numbers worth exporting.

    Three things, deliberately separated:

    * What the batteries actually did - the charged-weighted implied
      coefficient, `sum(measured + discharged) / sum(charged)`. Weighted rather
      than a mean of per-pair ratios, because a pair that barely charged would
      otherwise carry the same vote as one that charged all night.
    * How the deployed coefficient is doing - median absolute error as a
      fraction of capacity, per window bucket so a short-window regression is
      visible rather than averaged away.
    * Whether to change it - and the answer is `None` unless the pairs clear
      `min_pairs` *and* the candidate improves the longest window with data
      without making the short windows worse.

    Nothing here applies a value. The exporter publishes what it measured and a
    human decides, because the history records a coefficient that was changed on
    the strength of exactly this kind of sweep and then had to be reverted.
    """
    usable = [o for o in observations if o.get("plausible")]
    result = {
        "pairs": len(usable),
        "pairs_implausible": sum(1 for o in observations if not o.get("plausible")),
        "implied_efficiency": None,
        "error_ratio": None,
        "recommended_efficiency": None,
        "reason": "",
        "buckets": {},
        "per_unit": {},
    }
    if not usable:
        result["reason"] = "no plausible pairs yet"
        return result

    total_charged = sum(o["charged_mwh"] for o in usable)
    if total_charged > 0:
        result["implied_efficiency"] = sum(
            o["measured_delta_mwh"] + o["discharged_mwh"] for o in usable
        ) / total_charged

    def error_at(efficiency: float) -> dict:
        by_bucket: dict[str, list[float]] = {}
        per_unit: dict[str, list[float]] = {}
        for observation in usable:
            capacity = observation.get("capacity_mwh")
            if not capacity:
                continue
            predicted = efficiency * observation["charged_mwh"] - observation["discharged_mwh"]
            share = abs(predicted - observation["measured_delta_mwh"]) / float(capacity)
            by_bucket.setdefault(_calibration_bucket(observation["window_hours"]), []).append(share)
            per_unit.setdefault(observation.get("unit") or "", []).append(share)
        return {
            "buckets": {
                key: {"n": len(vals), "median": _median(vals)}
                for key, vals in sorted(by_bucket.items())
            },
            "per_unit": {
                unit: {"n": len(vals), "median": _median(vals)}
                for unit, vals in sorted(per_unit.items())
                if unit
            },
        }

    baseline = error_at(deployed_efficiency)
    result["buckets"] = baseline["buckets"]
    result["per_unit"] = baseline["per_unit"]
    all_errors = [v["median"] for v in baseline["buckets"].values() if v["median"] is not None]
    result["error_ratio"] = _median(all_errors) if all_errors else None

    if len(usable) < min_pairs:
        result["reason"] = "only %d plausible pairs, need %d" % (len(usable), min_pairs)
        return result

    longest = None
    for key, cell in baseline["buckets"].items():
        if cell["n"] >= min_pairs and (
            longest is None or _BUCKET_RANK.get(key, -1) > _BUCKET_RANK.get(longest, -1)
        ):
            longest = key
    if longest is None:
        result["reason"] = "no single window bucket has %d pairs" % min_pairs
        return result

    short_key = _calibration_bucket(1.0)
    grid = sorted({
        round(sweep_low + i * sweep_step, 4)
        for i in range(int(round((sweep_high - sweep_low) / sweep_step)) + 1)
    } | {round(deployed_efficiency, 4)}, reverse=True)

    scored = []
    for candidate in grid:
        cells = error_at(candidate)["buckets"]
        if longest in cells:
            scored.append((candidate, cells[longest]["median"]))
    if not scored:
        result["reason"] = "no candidate had data in the longest window"
        return result

    best_median = min(error for _, error in scored)
    # A sweep on a symmetric grid ties constantly - every value between the true
    # coefficient and the deployed one is equally wrong - so a bare min() would
    # recommend whichever arbitrary member of a tie the list happened to hit
    # first. Among values that fit equally well, the one nearest the deployed
    # value wins: no measurable gain is not a reason to move the flag.
    best_value = min(
        (value for value, error in scored if error <= best_median + 1e-12),
        key=lambda value: abs(value - deployed_efficiency),
    )
    deployed_median = dict(scored).get(round(deployed_efficiency, 4))

    short_cells = error_at(best_value)["buckets"].get(short_key)
    short_deployed = baseline["buckets"].get(short_key)
    if short_cells and short_deployed and short_cells["median"] > short_deployed["median"]:
        result["reason"] = (
            "%.2f wins the %s window but is worse over %s than the deployed %.2f, "
            "which is the signature of a term fitted to noise"
            % (best_value, longest, short_key, deployed_efficiency)
        )
        return result
    if deployed_median is not None and best_median >= deployed_median:
        result["reason"] = "no candidate beats the deployed %.2f" % deployed_efficiency
        return result

    result["recommended_efficiency"] = best_value
    result["reason"] = "best over %s from %d pairs" % (longest, len(usable))
    return result




def _render_calibration(lines: list[str], calibration: dict) -> None:
    """Emit the calibration block. Called only when calibration is enabled."""
    per_unit = calibration.get("per_unit") or {}
    labels = calibration.get("labels") or {}
    if per_unit:
        lines.append(
            "# HELP oe_battery_inferred_calibration_pairs Consecutive measured "
            "readings scored for this battery, and how far the deployed charge "
            "efficiency was off on each as a fraction of capacity (median). A "
            "unit with no pairs published is absent here entirely rather than "
            "zero: nothing was measured, which is not the same as nothing wrong."
        )
        lines.append("# TYPE oe_battery_inferred_calibration_pairs gauge")
        for unit in sorted(per_unit):
            cell = per_unit[unit]
            lines.append(
                "oe_battery_inferred_calibration_pairs{%s} %s"
                % (label_set(labels.get(unit, {})), fmt(cell.get("n")))
            )
        lines.append(
            "# HELP oe_battery_inferred_calibration_error_ratio Median absolute "
            "error of the deployed charge efficiency over this battery's scored "
            "pairs, as a fraction of capacity. Zero is a coefficient that "
            "reproduces every measured reading; it is the metric to watch after "
            "any change to --infer-charge-efficiency."
        )
        lines.append("# TYPE oe_battery_inferred_calibration_error_ratio gauge")
        for unit in sorted(per_unit):
            cell = per_unit[unit]
            lines.append(
                "oe_battery_inferred_calibration_error_ratio{%s} %s"
                % (label_set(labels.get(unit, {})), fmt(cell.get("median")))
            )

    # Fleet-wide verdict, and only ever a verdict. Nothing here writes to
    # configuration; the exporter publishes what the readings imply and a human
    # changes the flag. The notes record a loss term being adopted on the
    # strength of a sweep like this one and reverted the next day.
    lines.append(
        "# HELP oe_battery_inferred_calibration_recommended_efficiency Charge "
        "efficiency the accumulated readings favour, or absent when the "
        "evidence does not support a claim. Absent is the normal state: a "
        "recommendation needs enough independent pairs, a sweep that improves "
        "the longest window without degrading the short one, and no implied "
        "efficiency outside the physically plausible range. Reported, never "
        "applied."
    )
    lines.append("# TYPE oe_battery_inferred_calibration_recommended_efficiency gauge")
    recommended = calibration.get("recommended_efficiency")
    if recommended is not None:
        lines.append(
            "oe_battery_inferred_calibration_recommended_efficiency %s" % fmt(recommended)
        )
    lines.append(
        "# HELP oe_battery_inferred_calibration_plausible_pairs Scored pairs "
        "whose implied efficiency is physically possible. Kept beside the total "
        "so an implausible run is visible: it means the readings or the power "
        "series disagree, and averaging them into a coefficient would hide it."
    )
    lines.append("# TYPE oe_battery_inferred_calibration_plausible_pairs gauge")
    lines.append(
        "oe_battery_inferred_calibration_plausible_pairs %s"
        % fmt(calibration.get("pairs"))
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
        "oe_battery_power_mw",
        "Charge/discharge power of a NEM/WEM battery in MW, as published by the "
        "power metric: positive is discharging to the network, negative is "
        "charging. The series carry no unit, but the response block declares MW "
        "and the exporter warns if it ever stops. The sign is not documented "
        "upstream either - it is read off the data, where a charging battery "
        "goes negative and a discharging one positive, and it is stated here "
        "rather than assumed silently.",
        "power_mw",
    )
    gauge(
        "oe_battery_last_sample_timestamp_seconds",
        "Unix time of the observation behind the current state-of-charge reading.",
        "sampled_at",
    )
    gauge(
        "oe_battery_sample_age_seconds",
        "Age of the newest published sample for this battery's state of charge. "
        "Non-zero by design: the storage_battery feed only publishes overnight, "
        "so a daytime reading is hours old. This is the energy reading's age and "
        "nothing else's - see oe_battery_power_sample_age_seconds.",
        "sampled_at",
        transform=lambda s, n: n - s["sampled_at"],
    )
    gauge(
        "oe_battery_power_sample_timestamp_seconds",
        "Unix time of the observation behind the current power value. Published "
        "separately from oe_battery_last_sample_timestamp_seconds because the two "
        "series age independently: storage_battery publishes overnight, power "
        "updates through the day, so one timestamp cannot honestly cover both.",
        "power_sampled_at",
    )
    gauge(
        "oe_battery_power_sample_age_seconds",
        "Age of the newest published power sample for this battery. Kept apart "
        "from oe_battery_sample_age_seconds for the same reason - a battery with "
        "yesterday's energy and today's power would otherwise look equally fresh "
        "in both.",
        "power_sampled_at",
        transform=lambda s, n: n - s["power_sampled_at"],
    )
    gauge(
        "oe_battery_capacity_rank",
        "Rank of this battery by storage capacity among the monitored set, 1 = largest.",
        "rank",
    )

    lines.append(
        "# HELP oe_battery_scrape_success Whether the last poll read a usable "
        "state-of-charge sample for this battery. Carries the same labels as the "
        "reading metrics, not a reduced set, so that a single Grafana variable "
        "selects the same batteries in every panel and every table frame joins on "
        "the same key. It is about SOC only: oe_battery_power_mw is published "
        "independently of this flag, so a unit can report power here and still "
        "read 0."
    )
    lines.append("# TYPE oe_battery_scrape_success gauge")
    for sample in samples:
        lines.append(
            "oe_battery_scrape_success{%s} %d"
            % (label_set(sample), 1 if sample.get("scrape_success") else 0)
        )

    # Anchor age: how far back the inference anchor is, in hours.
    #
    # Published for every unit with a known anchor, independently of whether
    # inference produced anything. That independence is the whole point - a
    # missed upstream publication and a battery with no data both look like "no
    # inferred line", and the absence was previously the only symptom. The
    # anchor keeps advancing monotonically across polls, so this stays readable
    # even when a poll returns nothing at all and the sample rows go with it.
    #
    # Deliberately in hours rather than seconds: it is read against
    # --max-infer-hours, and the comparison that decides whether an estimate is
    # published at all is hours-against-hours.
    anchor_ts_by_unit = state.get("last_measured_ts") or {}
    lines.append(
        "# HELP oe_battery_anchor_age_hours Age in hours of the measured "
        "state-of-charge reading that inferred SOC integrates away from. This is "
        "the quantity --max-infer-hours is compared against: while it is smaller "
        "than that setting an estimate is published, and once it exceeds it "
        "inference stops entirely and oe_battery_soc_inferred_ratio goes absent. "
        "Published whether or not an estimate exists, so a missing inferred line "
        "can be attributed to this rather than guessed at."
    )
    lines.append("# TYPE oe_battery_anchor_age_hours gauge")
    lines.append(
        "# HELP oe_battery_anchor_timestamp_seconds Unix time of the measured "
        "state-of-charge reading that inferred SOC is anchored to. Unlike "
        "oe_battery_last_sample_timestamp_seconds this never moves backwards and "
        "is retained across a failed poll, so a stalled upstream feed shows up as "
        "a frozen timestamp rather than a gap in the series."
    )
    lines.append("# TYPE oe_battery_anchor_timestamp_seconds gauge")
    for sample in samples:
        unit = sample.get("unit")
        stamp = anchor_ts_by_unit.get(unit) if unit else None
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            continue
        labels = label_set(sample)
        lines.append(
            "oe_battery_anchor_timestamp_seconds{%s} %s" % (labels, fmt(stamp))
        )
        lines.append(
            "oe_battery_anchor_age_hours{%s} %s"
            % (labels, fmt(max(0.0, (now - float(stamp)) / 3600.0)))
        )

    # Inferred SOC: a synthetic estimate, published in its own metric family so
    # it can never be mistaken for - or silently substituted into - the measured
    # series above. `oe_battery_soc_ratio` stays exactly what upstream said.
    inferred_rows = state.get("inferred") or []
    lines.append(
        "# HELP oe_battery_soc_inferred_ratio Synthetic state of charge, 0-1, "
        "dead-reckoned by integrating this battery's power since its last "
        "MEASURED state-of-charge reading. Not an upstream figure and not a "
        "substitute for oe_battery_soc_ratio. Computed from power as long as "
        "power samples and an anchor are available within range (--max-infer-hours), "
        "even if the measured reading is fresh. Each estimate is anchored to a real "
        "reading and integrated over at most --max-infer-hours, so error cannot "
        "compound across multiple days."
    )
    lines.append("# TYPE oe_battery_soc_inferred_ratio gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_soc_inferred_ratio{%s} %s"
            % (label_set(row), fmt(row.get("inferred_soc")))
        )
    lines.append(
        "# HELP oe_battery_energy_inferred_mwh Synthetic stored energy in MWh, "
        "the quantity oe_battery_soc_inferred_ratio is derived from, clamped to "
        "the battery's registered capacity."
    )
    lines.append("# TYPE oe_battery_energy_inferred_mwh gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_energy_inferred_mwh{%s} %s"
            % (label_set(row), fmt(row.get("inferred_energy_mwh")))
        )
    lines.append(
        "# HELP oe_battery_inferred_timestamp_seconds Unix time of the newest "
        "power sample integrated into the inferred value. It is the power "
        "clock, not the energy clock - oe_battery_last_sample_timestamp_seconds "
        "is the anchor the estimate started from, and the two differ by "
        "construction."
    )
    lines.append("# TYPE oe_battery_inferred_timestamp_seconds gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_inferred_timestamp_seconds{%s} %s"
            % (label_set(row), fmt(row.get("inferred_ts")))
        )
    lines.append(
        "# HELP oe_battery_inferred_age_seconds Age of the newest power sample "
        "behind the inferred value, at export time."
    )
    lines.append("# TYPE oe_battery_inferred_age_seconds gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_inferred_age_seconds{%s} %s"
            % (label_set(row), fmt(row.get("inferred_age")))
        )
    lines.append(
        "# HELP oe_battery_inferred_saturated 1 when the unclamped integration "
        "fell outside [0, capacity] and the value was clamped, so this SOC is "
        "pinned at a bound rather than sitting there. Worth alerting on: it "
        "means the integration and the anchor disagree by at least a full "
        "battery, which is what a sign error, a capacity change, or a long "
        "run of drift looks like. 0 when the estimate is inside the envelope."
    )
    lines.append("# TYPE oe_battery_inferred_saturated gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_inferred_saturated{%s} %s"
            % (label_set(row), fmt(row.get("inferred_saturated")))
        )
    lines.append(
        "# HELP oe_battery_inferred_hold_hours How long the newest power rate has "
        "been carried forward past its own timestamp to reach export time. "
        "Non-zero means part of this SOC is extrapolated rather than integrated, "
        "so it is published rather than hidden: with an hourly poll this cycles "
        "0 -> ~1h between polls, and a value stuck high means the power feed "
        "has stopped arriving."
    )
    lines.append("# TYPE oe_battery_inferred_hold_hours gauge")
    for row in inferred_rows:
        lines.append(
            "oe_battery_inferred_hold_hours{%s} %s"
            % (label_set(row), fmt(row.get("inferred_hold_hours")))
        )

    # Calibration evidence for the deployed charge efficiency. Deliberately
    # separate from the inferred block above: those rows say what the SOC *is*,
    # these say how much the loss term behind it is trusted.
    #
    # The whole block is gated on the flag, so an exporter running without
    # calibration emits nothing at all here - not even a zero pair count. A
    # NaN or a 0 from a disabled feature reads as a measurement, and would
    # quietly answer the question the panel is asking.
    calibration = state.get("calibration") or {}
    if calibration:
        _render_calibration(lines, calibration)

    idle_rows = {}
    for sample in samples:
        idle_rows[sample.get("unit")] = sample
    for row in state.get("demoted") or []:
        idle_rows.setdefault(row.get("unit"), row)
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
            "reading seen for this battery - state of charge or power, whichever "
            "is more recent. A unit is dropped from scope once this passes the "
            "idle limit, and counting either metric is deliberate: storage_battery "
            "is an overnight series, so judging liveness on it alone would demote "
            "batteries that are visibly dispatching."
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
        ("oe_batteries_monitored", "Battery units that returned a usable state-of-charge sample in the last poll. This is a count of energy readings, not of polls: a unit can publish oe_battery_power_mw and still not be counted here, because storage_battery is an overnight series."),
        ("oe_battery_series_without_capacity", "Storage or power series returned upstream whose unit has no capacity in the fleet metadata, so no SOC can be derived. Non-zero is expected: each battery facility also returns a G1 and an L1 series for each of the two metrics."),
        ("oe_battery_series_too_stale", "Samples dropped for being older than the maximum sample age."),
        ("oe_batteries_inferred", "Battery units for which a synthetic (power-integrated) SOC is being exported this cycle, because no measured SOC was available. This is a subset of the monitored set; a unit with a fresh measured SOC exports no inferred value."),
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
        ("oe_last_fetch_attempt_timestamp_seconds",
         "Unix time a battery request was last issued. Sizes the next request's "
         "window, so a failed cycle widens it rather than leaving a hole in the "
         "power series."),
        ("oe_batteries_reading_from_cache",
         "Units in the last cycle whose energy reading came from the local "
         "reading cache because the narrowed request window did not reach it. "
         "Expected to equal the monitored count in steady state: storage_battery "
         "only publishes overnight, so most daytime polls cannot see a reading "
         "this recent. The values are unchanged by this - the age is carried "
         "with them - but the substitution is reported rather than silent."),
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
        enable_inferred: bool = False,
        max_infer_hours: float = 24.0,
        infer_clamp: bool = True,
        infer_fresh_hours: float = 2.0,
        infer_max_gap_hours: float = 2.0,
        infer_max_hold_hours: float = DEFAULT_INFER_MAX_HOLD_HOURS,
        infer_charge_efficiency: float = 1.0,
        power_history: "PowerHistory | None" = None,
        calibration: "EfficiencyCalibrator | None" = None,
        reading_cache: "ReadingCache | None" = None,
        poll_window_hours: float = DEFAULT_POLL_WINDOW_HOURS,
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
        self.enable_inferred = enable_inferred
        self.max_infer_hours = max_infer_hours
        self.infer_clamp = infer_clamp
        self.infer_fresh_hours = infer_fresh_hours
        self.infer_max_gap_hours = infer_max_gap_hours
        self.infer_max_hold_hours = infer_max_hold_hours
        self.infer_charge_efficiency = infer_charge_efficiency
        self.power_history = power_history
        self.calibration = calibration
        self.reading_cache = reading_cache
        self.poll_window_hours = poll_window_hours
        self.now_fn = now_fn
        self._state: dict = {}
        self._fleet: list[dict] = []
        self._fleet_at = 0.0
        # unit code -> epoch of the newest usable reading we have ever been
        # shown for it. Only the poll thread touches this; the serving thread
        # reads the derived counts out of `self._state`, which is published
        # under the lock like everything else.
        self._last_reading: dict[str, float] = {}
        # Anchors for inferred SOC: last measured stored energy (from storage_battery)
        self._last_measured_mwh: dict[str, float] = {}
        self._last_measured_ts: dict[str, float] = {}
        # When this process last *attempted* a fetch, not last succeeded. The
        # request window is sized from this, so a failed cycle widens the next
        # one enough to repair the power series rather than leaving a hole that
        # `infer_soc` would refuse to integrate across.
        self._last_fetch_at: float | None = None
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
        if reading_cache is not None and reading_cache.path:
            # Restoring the last energy reading is what lets the first request
            # after a restart be narrow instead of wide. It feeds the *inferred*
            # family only - see `ReadingCache` for why that is safe where the
            # liveness file above refuses to hold a reading - and a real reading
            # supersedes it as soon as one arrives, because `_record_readings`
            # only ever moves an anchor forward.
            restored = reading_cache.load()
            now = self.now_fn()
            usable, skipped = {}, 0
            for unit, (stamp, value) in reading_cache.items():
                if stamp <= now and reading_cache.get(unit, now) is not None:
                    usable[unit] = (stamp, value)
                else:
                    skipped += 1
            for unit, (stamp, value) in usable.items():
                self._last_measured_ts[unit] = stamp
                self._last_measured_mwh[unit] = value
            if usable:
                oldest = min(s for s, _ in usable.values())
                log.info(
                    "restored energy readings for %d units from %s "
                    "(oldest anchor %.1fh old)%s",
                    len(usable),
                    reading_cache.path,
                    (now - oldest) / 3600.0,
                    " (%d unusable ignored)" % skipped if skipped else "",
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
            window = self._request_window(scope, started_at)
            result = poll_cycle(
                self.client,
                scope,
                self.network,
                self.lookback_hours,
                self.max_sample_age,
                started_at,
                full_series=self.enable_inferred,
                window_hours=window,
                reading_cache=self.reading_cache,
            )
            self._record_readings(result["samples"], started_at)
            self._absorb_power_history(result["samples"], started_at)
            self._absorb_reading_cache(result["samples"], started_at)
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
                "readings_from_cache": 0,
            }

        finished = self.now_fn()
        # Build inferred (power-integrated) SOC for units missing a measured SOC
        inferred_rows: list[dict] = []
        if self.enable_inferred and not errors:
            inferred_rows = self._compute_inferred(result["samples"], started_at, finished)

        state = {
            "samples": result["samples"],
            "inferred": inferred_rows,
            "oe_batteries_enumerated": len(self._fleet),
            "oe_batteries_in_scope": len(scope) if not errors else 0,
            "oe_batteries_demoted": len(demoted) if not errors else 0,
            "demoted": demoted,
            "last_reading": dict(self._last_reading),
            "last_measured_mwh": dict(self._last_measured_mwh),
            "last_measured_ts": dict(self._last_measured_ts),
            "calibration": (
                self.calibration.summary(self.infer_charge_efficiency)
                if self.calibration is not None
                else {}
            ),
            "oe_batteries_monitored": sum(
                1 for s in result["samples"] if s.get("scrape_success")
            ),
            "oe_battery_series_without_capacity": result["series_without_capacity"],
            "oe_battery_series_too_stale": result["series_too_stale"],
            # Integrated rows only, deliberately. A unit whose reading is still
            # current also carries an inferred row now, so counting them all
            # would make this the monitored count wearing a different name; what
            # is worth knowing is how much of the fleet is actually being
            # dead-reckoned rather than read.
            "oe_batteries_inferred": sum(
                1 for row in inferred_rows if not row.get("from_measured")
            ),
            "oe_battery_fleet_capacity_mwh": sum(r["capacity_mwh"] for r in self._fleet),
            "oe_battery_monitored_capacity_mwh": sum(
                s.get("capacity_mwh") or 0.0
                for s in result["samples"]
                if s.get("scrape_success")
            ),
            "oe_poll_cycle_duration_seconds": time.monotonic() - started,
            "oe_last_poll_timestamp_seconds": finished,
            "oe_last_fleet_refresh_timestamp_seconds": self._fleet_at,
            "oe_last_fetch_attempt_timestamp_seconds": self._last_fetch_at or 0.0,
            "oe_batteries_reading_from_cache": result.get("readings_from_cache", 0),
            "oe_api_credits_remaining": self.client.credits_remaining(),
            "oe_api_requests_total": self.client.requests_made,
            "enable_inferred": self.enable_inferred,
            "max_infer_hours": self.max_infer_hours,
            "infer_clamp": self.infer_clamp,
            "infer_charge_efficiency": self.infer_charge_efficiency,
            "infer_max_hold_hours": self.infer_max_hold_hours,
            "fleet_rows": rows,
            "now": started_at,
            "samples_map": {s["unit"]: s for s in result["samples"] if s.get("unit")},
        }
        ok = not errors
        with self._lock:
            self._state = state
            self._health = (ok, "; ".join(errors))
        return render(state, self.now_fn()), ok

    def _compute_inferred(
        self, samples: list[dict], started_at: float, now: float
    ) -> list[dict]:
        """Inferred-SOC rows for units with no *fresh* measured SOC this poll.

        Only runs when `enable_inferred` is on. Every returned row is a real
        sample row (so it inherits the same five labels) with the inferred
        fields attached; units with a fresh measured reading this poll are
        skipped, so the two families never both claim the same cycle.

        "Fresh" is `--infer-fresh-hours`, not "present". That distinction is the
        entire feature: `storage_battery` publishes overnight, so a reading from
        The inferred value is computed from power as long as there is an
        anchor and power series to integrate, regardless of how stale the
        measured reading is. The `now` passed in is the wall clock at render
        time, not the newest power sample time.
        """
        rows: list[dict] = []
        if not self.enable_inferred:
            # Guarded here as well as at the call site: this method builds the
            # synthetic rows, and a future caller reaching it directly must not
            # be able to bypass the opt-in.
            return rows
        for sample in samples:
            unit = sample.get("unit")
            if not unit:
                continue
            # Always compute inferred from power integration when we have an anchor.
            # Even if measured is fresh, we want to show the integrated state
            # back to the last power value regardless of power staleness (drift increases).
            anchor_mwh = self._last_measured_mwh.get(unit)
            anchor_ts = self._last_measured_ts.get(unit)
            series = self._power_for_inference(sample, anchor_ts or 0.0)
            inferred = infer_soc(
                anchor_mwh=anchor_mwh,
                anchor_ts=anchor_ts,
                power_series=series,
                now=now,
                capacity_mwh=sample.get("capacity_mwh") or 0.0,
                max_hours=self.max_infer_hours,
                clamp=self.infer_clamp,
                max_gap_hours=self.infer_max_gap_hours,
                charge_efficiency=self.infer_charge_efficiency,
                hold_rate=self._hold_rate(series, now),
                hold_max_hours=self.infer_max_hold_hours,
            )
            if inferred is None:
                # No anchor, no power, or too far from the anchor. Omitted
                # rather than zeroed, same rule as every other reading here.
                continue
            rows.append(
                dict(
                    sample,
                    inferred_soc=inferred["soc"],
                    inferred_energy_mwh=inferred["energy_mwh"],
                    inferred_ts=inferred["inferred_ts"],
                    inferred_age=max(0.0, now - inferred["inferred_ts"]),
                    inferred_saturated=inferred["saturated"],
                    inferred_hold_hours=inferred.get("hold_hours", 0.0),
                    from_measured=False,
                )
            )
        return rows

    @staticmethod
    def _copy_of_measured(sample: dict, now: float) -> dict | None:
        """Inferred row for a battery whose measured reading is still current.

        .. deprecated::
            **Dead code.** `_measured_is_current` has no callers and nothing on
            the poll path calls this, so `--infer-fresh-hours` gates nothing at
            runtime: `_compute_inferred` integrates whenever an anchor and power
            exist, freshness aside. Decided 2026-10-05 - see
            `_measured_is_current` for why integrating unconditionally won, and
            `NOTES.md` under "Still open: the copy/integrate contradiction" (now
            resolved) for the test conflict that was settled along with it.

            Kept, along with the `from_measured` branch in
            `_refresh_inferred_for_render`, so the alternative is reversible
            without re-deriving it. Read it as a record of the rejected
            design, not as a dormant feature: switching it back on is not a
            one-line change, it is the branch plus these tests plus the metric
            HELP text, which currently documents the always-integrate behaviour.

        The simplest inference there is: the feed says X, so the estimate is X.
        Publishing the reading as its own estimate rather than withholding one
        keeps the inferred family continuous, which is the whole point of it -
        a consumer can plot a single series all day instead of stitching
        measured to inferred across a handover, and the daytime gap this
        family exists to cover is bounded by when upstream *stops* publishing
        rather than by when the last reading stops being true. Because it is an
        exact copy it adds no error of its own.

        The cost is that the family no longer distinguishes its two kinds of
        row by presence, which it used to do: an absent inferred value used to
        mean "measured is current, use that instead". `oe_battery_scrape_success`
        and `oe_battery_sample_age_seconds` still carry that distinction, and
        `from_measured` records which kind this row is for the exporter's own
        bookkeeping.

        No integration happens here, so `inferred_saturated` and
        `inferred_hold_hours` are 0 by construction rather than by
        measurement, and `inferred_ts` is the reading's own timestamp - which
        makes `oe_battery_inferred_age_seconds` the age of the measurement
        itself, the honest thing to report for a copy.

        Returns `None` if the reading's energy or SOC is not actually there to
        copy, which is omitted rather than zeroed everywhere else in this
        exporter. The `_measured_is_current` check determines freshness, though
        the production code no longer relies on this path for inferred output.
        """
        soc = sample.get("soc")
        stored = sample.get("stored_mwh")
        sampled_at = sample.get("sampled_at")
        if soc is None or stored is None or sampled_at is None:
            return None
        sampled_at = float(sampled_at)
        return dict(
            sample,
            inferred_soc=float(soc),
            inferred_energy_mwh=float(stored),
            inferred_ts=sampled_at,
            inferred_age=max(0.0, now - sampled_at),
            inferred_saturated=0.0,
            inferred_hold_hours=0.0,
            from_measured=True,
        )

    def _refresh_inferred_for_render(self, state: dict, now: float) -> dict:
        """Re-derive inferred rows at scrape time so they track the clock.

        State is published once per poll and rendered on every scrape, so
        anything derived from the wall clock at poll time goes stale in between.
        That was visible as `oe_battery_inferred_age_seconds` disagreeing with
        `oe_battery_power_sample_age_seconds` for the *same* timestamp - two
        clocks in one exposition, up to a poll interval apart.

        Because of the zero-order hold this is now a *recomputation*, not a
        restamp: the held value grows with elapsed time, so a scrape ten minutes
        after the poll legitimately publishes a different SOC than the poll did.
        The integral is over the same power samples, so only the hold moves.

        Cheap on purpose. Right after a poll the newest sample is nearly
        current, `_hold_rate` returns `None`, and the row is passed through with
        only its age restamped. The full re-integration only runs in the window
        between polls, which is what it costs to turn an hourly staircase into a
        line that moves.

        Returns a new dict rather than mutating, so a render in flight keeps a
        consistent view of the state it started with.
        """
        samples = state.get("samples_map") or {}
        last_measured_mwh = state.get("last_measured_mwh") or {}
        last_measured_ts = state.get("last_measured_ts") or {}
        max_hold = float(state.get("infer_max_hold_hours") or 0.0)
        rows = list(state.get("inferred") or [])
        # A unit that was current when the poll ran carries a copy of that
        # reading, and that copy stops being the best estimate once the reading
        # ages past `--infer-fresh-hours`. Re-derive those here rather than
        # waiting out a whole poll interval, which is the handover drift this
        # refresh exists to remove. The returned rows *replace* their copies
        # rather than joining them, because two rows for one unit is not
        # something this family can express.
        upgraded = self._rows_for_newly_stale(
            samples, last_measured_mwh, last_measured_ts, state, now
        )
        if upgraded:
            replaced = {row.get("unit") for row in upgraded}
            rows = [row for row in rows if row.get("unit") not in replaced] + upgraded
        if not rows:
            return state
        refreshed = []
        for row in rows:
            updated = dict(row)
            stamp = row.get("inferred_ts")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                updated["inferred_age"] = max(0.0, now - float(stamp))
            if row.get("from_measured"):
                # A copy of the current reading. Re-integrating it would move
                # it off the measurement it is reporting, and `inferred_ts`
                # would then name a power sample that was never used, so the
                # age restamp above is all that is left to do.
                refreshed.append(updated)
                continue
            unit = row.get("unit")
            sample = samples.get(unit) if unit else None
            anchor_ts = last_measured_ts.get(unit) if unit else None
            anchor_mwh = last_measured_mwh.get(unit) if unit else None
            capacity = (sample or {}).get("capacity_mwh") or 0.0
            series = (
                self._power_for_inference(sample, anchor_ts or 0.0) if sample else []
            )
            # Only worth redoing when a hold is actually in play; otherwise the
            # age above is the only thing that could have changed.
            if anchor_ts is not None and anchor_mwh is not None:
                again = infer_soc(
                    anchor_mwh=anchor_mwh,
                    anchor_ts=anchor_ts,
                    power_series=series,
                    now=now,
                    capacity_mwh=capacity,
                    max_hours=float(state.get("max_infer_hours") or 24.0),
                    clamp=self.infer_clamp,
                    max_gap_hours=self.infer_max_gap_hours,
                    charge_efficiency=float(
                        state.get("infer_charge_efficiency") or 1.0
                    ),
                    hold_rate=self._hold_rate(series, now),
                    hold_max_hours=max_hold,
                )
                if again is not None:
                    updated["inferred_soc"] = again["soc"]
                    updated["inferred_energy_mwh"] = again["energy_mwh"]
                    updated["inferred_saturated"] = again["saturated"]
                    updated["inferred_hold_hours"] = again.get("hold_hours", 0.0)
                    # `inferred_ts` has to move with them. The integral above runs
                    # to `again["inferred_ts"]`, not to the stamp the poll wrote:
                    # samples that were future-dated at poll time are not
                    # integrated then, but become integrable as the clock advances
                    # to them. Leaving the old stamp would make `inferred_age`
                    # describe a different timestamp than the one the energy was
                    # actually integrated to - two disagreeing clocks in one row,
                    # which is the exact symptom this method exists to remove.
                    stamp = again["inferred_ts"]
                    updated["inferred_ts"] = stamp
                    updated["inferred_age"] = max(0.0, now - float(stamp))
            refreshed.append(updated)
        out = dict(state)
        out["inferred"] = refreshed
        return out

    def _rows_for_newly_stale(
        self,
        samples: dict,
        last_measured_mwh: dict,
        last_measured_ts: dict,
        state: dict,
        now: float,
    ) -> list[dict]:
        """Inferred rows for units whose measured reading has expired since the poll.

        `_compute_inferred` decides measured-versus-inferred once, when the poll
        runs, and a unit inside `--infer-fresh-hours` at that moment gets a *copy*
        of the reading rather than an integrated row. Nothing then re-evaluates
        that decision until the next poll, so a reading that was 1h old at poll
        time keeps publishing that copy for as long as the poll cycle takes to
        come round - even at 5h old, four hours past the point where the same
        reading would have been judged stale on arrival.

        The handover therefore drifted by up to one poll interval, which showed up
        as the inferred line going flat at a measured value long after it stopped
        being current. Re-running the freshness test against the render clock
        closes it, so a unit crosses over at `--infer-fresh-hours` exactly as it
        would have if the scrape had happened then.

        Only units whose existing row is *not* already integrated are considered:
        a copy is exactly what needs replacing, whereas an integrated row is
        already re-derived every scrape by `_refresh_inferred_for_render`. The
        caller drops the copies these replace, so a unit never has two rows. The
        arithmetic stays entirely in `_compute_inferred` and `infer_soc` rather
        than being reimplemented here.
        """
        if not self.enable_inferred:
            return []
        present = {
            row.get("unit")
            for row in (state.get("inferred") or [])
            if isinstance(row, dict) and not row.get("from_measured")
        }
        candidates = []
        for unit, sample in samples.items():
            if unit in present:
                continue
            # Always attempt to generate inferred rows from power data
            candidates.append(sample)
        if not candidates:
            return []
        return self._compute_inferred(candidates, now, now)

    @staticmethod
    def _hold_rate(series: list[tuple[float, float]], now: float) -> float | None:
        """The power value to carry forward past the newest sample, if any.

        The most recent sample whose timestamp is at or before `now`. Anything
        newer is future-dated - `infer_soc` ignores those - and carrying one
        forward would extrapolate backwards, so it is not eligible.

        Returns `None` when the newest sample is already `now` or later, which
        is the case every scrape immediately following a poll: nothing is held
        because nothing has elapsed. That is the common path, and it is why
        this is cheap.
        """
        eligible = [float(v) for stamp, v in series if float(stamp) <= now]
        if not eligible:
            return None
        # `series` is sorted oldest-first by `_power_for_inference`, so the
        # last eligible entry is the newest one at or before `now`.
        return eligible[-1]

    def _measured_is_current(self, sample: dict, now: float) -> bool:
        """True when this poll read a measured SOC that is still current.

        .. deprecated::
            **No caller.** This is the gate for the copy semantics that
            `--infer-fresh-hours` was written to drive: a reading inside the
            window would be published as its own estimate (`from_measured=True`)
            rather than integrated. As of 2026-10-05 that path is not wired up -
            `_compute_inferred` integrates whenever an anchor and power exist,
            regardless of freshness, and `_rows_for_newly_stale` cannot revive it
            because it also just calls `_compute_inferred`. So
            `--infer-fresh-hours` gates nothing at runtime.

            The decision to integrate unconditionally was deliberate (see
            `NOTES.md`, "Still open: the copy/integrate contradiction", now
            resolved). The reasons it won, for anyone tempted to reconnect this:

            - Publishing the reading verbatim makes the dashed inferred line
              coincide exactly with the measured one and then bend away from it,
              so both series carry the same number for different reasons at the
              same moment, and the handover puts a visible kink in the chart.
            - The copy has to be replaced later anyway, since it stops being the
              best estimate as the reading ages. That replacement is the
              complexity this avoids: a second code path, and a window where the
              published value is up to one poll interval out of date.
            - `inferred_ts` on a copy names the measurement, not a power sample,
              so `inferred_age` would report the age of a value that was never
              integrated - the age metric would describe work that did not happen.

            Retained rather than deleted because `_copy_of_measured` and the
            `from_measured` branch in `_refresh_inferred_for_render` are still
            reachable through it, and removing that is a separate cleanup with its
            own test changes. Treat this whole path as dead code, not as a
            dormant feature to switch back on.

        Args:
            sample: The polled sample row.
            now: Wall-clock epoch seconds to judge freshness against.

        Returns:
            True if the sample carries a successfully-scraped reading no older
            than `--infer-fresh-hours`.

        Presence is not freshness. `storage_battery` publishes overnight, so its
        newest reading can be many hours old while still being returned by the
        API and counted in `oe_battery_scrape_success`; that metric is about
        "did we get a reading", not "is it now". Inference needs the second
        question answered separately, so a reading counts as current only while
        it is within `--infer-fresh-hours` of the clock.

        The window is one publication gap by default, so a battery publishing
        each night crosses the boundary mid-morning and hands over to inference
        for the rest of the day without any tuning.
        """
        if not sample.get("scrape_success"):
            return False
        sampled_at = sample.get("sampled_at")
        if not isinstance(sampled_at, (int, float)) or isinstance(sampled_at, bool):
            return False
        age = now - float(sampled_at)
        return age <= self.infer_fresh_hours * 3600.0

    def _request_window(self, scope: list[dict], now: float) -> float:
        """How far back this cycle's requests should actually reach.

        This is the cost lever, and it is deliberately conservative: the *only*
        reason a poll ever needed a window wider than the gap since the last poll
        was that the newest `storage_battery` reading can be hours old, and the
        API can only return readings that fall inside the window. `ReadingCache`
        holds that reading locally, so once every in-scope unit has one there is
        nothing left to fetch from the past.

        Narrowing is all-or-nothing per cycle, on the worst unit rather than the
        average, but "worst" means the worst unit *that could still be hiding a
        reading*. A unit that has never published `storage_battery` at all - and
        five of the deployed twelve never do - cannot be missing a reading, so
        requiring one of those would pin the window at 192h forever and make this
        entire mechanism dead code in production. So the requirement is on units
        the cache has *seeded*, i.e. observed publishing at least once:

        - A seeded unit with no usable cached reading means something regressed -
          it used to publish, now it cannot be reached - and the wide window is
          what finds out why.
        - A never-seeded unit is not evidence of anything. It gets the narrow
          window too, which costs it nothing: it has no anchor to reach, and the
          narrow window still covers its power series. If it ever starts
          publishing, the 2h window against a 1h poll interval is guaranteed to
          overlap the new reading, and it becomes seeded from that moment on.

        Averaging here would be a way to lose an anchor on a unit nobody was
        looking at.

        Four conditions force the wide window, and they are the whole argument
        for keeping it at all:

        - **No cache configured.** Without a local anchor store the only way to
          reach the last energy reading is to ask for it. This is why the flag
          defaults off.
        - **Nothing has been fetched yet** (`_last_fetch_at` unset), so the first
          cycle after a restart is wide even though the cache survived. This
          process has not yet proven it can fetch; the surviving cache is a claim
          from a previous process, and trusting it to size the very first request
          is how a bad restore becomes a silent outage.
        - **No unit has been seeded yet**, which is a cold cache reached by any
          other route - a corrupt or empty file. Distinguishing this from the
          case above is what stops a cold restore from narrowing.
        - **A poll was missed.** The window has to cover the gap or the power
          series gets a hole in it, and `infer_soc` drops segments wider than
          `max_gap_hours` rather than integrating across them - so a hole is not
          a slightly-wrong number, it is the line going flat at the last good
          point. This is why the window tracks the time since the last *attempt*
          rather than the last success: a cycle that failed must not shrink the
          window that would have repaired it.

        Returns hours, always `>= 1.0` and never above `--lookback-hours` - the
        reach is a hard requirement and the client caps the request to it.
        """
        wide = max(1.0, float(self.lookback_hours))
        narrow = max(1.0, float(self.poll_window_hours))
        if self.reading_cache is None:
            # No local anchor store, so the only way to reach the last energy
            # reading is to ask for it.
            return wide
        if self._last_fetch_at is None:
            return wide
        missing = self.reading_cache.missing(
            [row["unit"] for row in scope], now
        )
        if missing:
            log.info(
                "wide window this cycle: no cached reading for %s",
                ", ".join(sorted(missing)[:4]) + ("..." if len(missing) > 4 else ""),
            )
            return wide
        # Cover the time since the last attempt, plus a margin, so jitter in the
        # poll clock and one dropped sample at the seam cannot open a hole.
        since = (now - self._last_fetch_at) / 3600.0
        return min(wide, max(narrow, since + 0.5))

    def _absorb_reading_cache(self, samples: list[dict], now: float) -> None:
        """Fold this cycle's live energy readings into the cache and persist.

        Written after the poll, for the same reason as power history: a failed
        cycle must leave the previous cache untouched rather than half-overwrite
        it with whatever arrived before the failure.

        Only live readings are recorded - rows marked `reading_from_cache` are
        skipped. Recording them would be harmless (newest-wins makes it a no-op)
        but it would mean writing back what was just read out, and a future-dated
        or hand-edited entry in the file would then be re-blessed on every cycle
        instead of being overwritten by the real reading.
        """
        if self.reading_cache is None:
            return
        recorded = 0
        for sample in samples:
            unit = sample.get("unit")
            if not unit or sample.get("reading_from_cache"):
                continue
            if not sample.get("scrape_success"):
                continue
            stored, stamp = sample.get("stored_mwh"), sample.get("sampled_at")
            if stored is None or stamp is None:
                continue
            try:
                ts, value = float(stamp), float(stored)
            except (TypeError, ValueError):
                continue
            if ts > now:
                # A future-dated reading is not a reading. Mirrors the clamp in
                # `_record_readings`; without it a skewed upstream clock would
                # pin the unit as fresh forever.
                continue
            self.reading_cache.record(unit, ts, value)
            recorded += 1
        self._last_fetch_at = now
        if recorded:
            self.reading_cache.save()

    def _absorb_power_history(self, samples: list[dict], now: float) -> None:
        """Fold this cycle's power samples into the local history and persist it.

        Written after the poll rather than inside it, so a failed cycle leaves
        the previous history untouched instead of half-overwritten by a partial
        result.
        """
        if self.power_history is None:
            return
        by_unit: dict[str, list[tuple[float, float]]] = {}
        for sample in samples:
            unit = sample.get("unit")
            series = sample.get("power_series") or []
            if unit and series:
                by_unit[unit] = list(series)
        if not by_unit:
            return
        self.power_history.record(by_unit, now)
        self.power_history.save()

    def _power_for_inference(self, sample: dict, anchor_ts: float) -> list[tuple[float, float]]:
        """The power series to integrate: what we just polled, plus our history.

        With no history configured this is exactly what it always was, so the
        default path is unchanged.
        """
        series = sample.get("power_series") or []
        if self.power_history is None:
            return sorted(series)
        unit = sample.get("unit")
        if not unit:
            return sorted(series)
        merged = self.power_history.merged(unit, series)
        # Only the window the estimator can actually use: everything from the
        # anchor up to the inferred timestamp. Without this the merged list
        # carries the whole retention window into every call, which is wasted
        # work on the render path and grows with retention rather than with the
        # inference horizon.
        return [(s, v) for s, v in merged if anchor_ts <= s]

    def _record_readings(self, samples: list[dict], now: float) -> None:
        """Note the newest usable reading seen for each unit polled.

        Keyed on the *sample's own* timestamp rather than the poll time, so a
        unit that has not published since last night is correctly measured as
        idle from last night. That distinction is the whole point: with `now` as
        the clock, a battery polled hourly with a 36h publication gap would
        never look idle, and one polled against a 12h gap would look idle every
        afternoon.

        Either metric counts, and the newer of the two wins. This is the one
        place the ages are combined, and it has to be: the two series have
        different publication habits - `storage_battery` carries values only
        overnight, `power` updates all day - so judging liveness on storage
        alone would demote a battery that is plainly dispatching and visible on
        the power panel. It does *not* mean the sample is interchangeable: the
        exported timestamps stay per-metric.
        """
        for sample in samples:
            unit = sample.get("unit")
            if not unit:
                continue
            # Anchor updates from measured SOC
            if sample.get("scrape_success") and sample.get("stored_mwh") is not None and sample.get("sampled_at"):
                try:
                    ts = float(sample["sampled_at"])
                except (TypeError, ValueError):
                    ts = None
                if ts and ts <= now:
                    val = float(sample["stored_mwh"])
                    prev_ts = self._last_measured_ts.get(unit)
                    prev_val = self._last_measured_mwh.get(unit)
                    if prev_ts is not None and prev_val is not None and self.calibration is not None:
                        # A new reading just superseded an earlier one, so the
                        # interval between them is a completed, fully measured
                        # test of the loss term: both ends published by the feed,
                        # and the power series in between is the same one
                        # inference integrates. Recorded before the anchor moves
                        # on, while the previous reading is still the one in hand.
                        self.calibration.observe(
                            unit,
                            prev_ts,
                            prev_val,
                            ts,
                            val,
                            self._power_for_inference(sample, prev_ts),
                            capacity_mwh=sample.get("capacity_mwh"),
                            sample=sample,
                        )
                    if prev_ts is None or ts > prev_ts:
                        self._last_measured_mwh[unit] = val
                        self._last_measured_ts[unit] = ts
            observed = None
            for key in ("sampled_at", "power_sampled_at"):
                stamp = sample.get(key)
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                    stamp = float(stamp)
                    observed = stamp if observed is None else max(observed, stamp)
            if observed is None:
                # Nothing usable this poll. Start the clock for a unit seen for
                # the first time so that it is judged from now rather than
                # being immortal, but never move an existing stamp - a failed
                # poll must not reset progress towards dropping it.
                self._last_reading.setdefault(unit, now)
                continue
            # A clock skewed into the future would pin the unit as fresh
            # forever, so never trust a stamp past the poll.
            stamp = min(observed, now)
            previous = self._last_reading.get(unit)
            if previous is None or stamp > previous:
                self._last_reading[unit] = stamp

        # One write per poll rather than one per pair, so a busy poll does not
        # rewrite the file a dozen times. Safe to do unconditionally: the store
        # is rewritten whole and `os.replace` is atomic.
        if self.calibration is not None:
            self.calibration.save()

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
        # Track anchors for any samples from probe
        for s in result["samples"]:
            unit = s.get("unit")
            if not unit:
                continue
            if s.get("scrape_success") and s.get("stored_mwh") is not None and s.get("sampled_at"):
                try:
                    ts = float(s["sampled_at"])
                except (TypeError, ValueError):
                    ts = None
                if ts and ts <= now:
                    val = float(s["stored_mwh"])
                    prev_ts = self._last_measured_ts.get(unit)
                    if prev_ts is None or ts > prev_ts:
                        self._last_measured_mwh[unit] = val
                        self._last_measured_ts[unit] = ts
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
        now = self.now_fn()
        with self._lock:
            state = self._state
            health = self._health
            # Inside the lock, because this reads state the poll thread
            # replaces wholesale. Only the lock-protected publish touches it,
            # and taking the snapshot here keeps the pair consistent.
            if state:
                state = self._refresh_inferred_for_render(state, now)
        if not state:
            return render({}, now), health[0]
        return render(state, now), health[0]

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


def resolve_power_history_path(explicit: str | None, fleet_file: str | None) -> str | None:
    """Where power history is persisted: explicit, or beside the fleet cache.

    Same derivation as liveness, for the same reason: a deployment that already
    mounts a cache volume should not need a second flag to remember, and one
    with neither should get no persistence rather than a surprise file in the
    working directory.
    """
    if explicit:
        return explicit
    if fleet_file:
        return os.path.join(os.path.dirname(fleet_file) or ".", "power-history.json")
    return None


def resolve_calibration_path(explicit: str | None, fleet_file: str | None) -> str | None:
    """Where calibration observations are persisted: beside the fleet cache.

    Same derivation as power history, for the same reason: a deployment that
    already mounts a cache volume should not need another flag to remember.
    Kept as a separate file because it has a different lifecycle - power points
    are a rolling 14-day window, observations are evidence that stays valid and
    has to survive the window it was measured over being deleted.
    """
    if explicit:
        return explicit
    if fleet_file:
        return os.path.join(os.path.dirname(fleet_file) or ".", "efficiency-calibration.json")
    return None


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


def resolve_reading_cache_path(explicit: str | None, fleet_file: str | None) -> str | None:
    """Where the newest energy reading per unit is persisted: beside the fleet cache.

    Same derivation as liveness and power history, for the same reason: a
    deployment that already mounts a cache volume should not need another flag to
    remember, and one with neither should get no persistence rather than a
    surprise file in the working directory.
    """
    if explicit:
        return explicit
    if fleet_file:
        return os.path.join(os.path.dirname(fleet_file) or ".", "readings.json")
    return None


class ReadingCache:
    """The newest `storage_battery` reading per unit, in memory and on disk.

    This exists because of a request-cost problem, and it is worth being precise
    about why, because the obvious fix has a catch.

    `battery_metrics` can only see the newest reading that falls *inside the
    window it asked for*. The window therefore has to be wide enough to reach
    back to the last overnight reading - measured at 90h on 2026-10-06 - which
    meant 192h at `interval=5m`, and that is not a small request: 494 KB and
    ~13.6k points for one facility, ~5.9 MB and ~164k points for the deployed 12,
    re-fetched every hour to pick up a handful of new samples. `interval` is a
    query parameter and costs the same as `1h`, but the *response* is what grows.

    So the window can be narrow - and holding the last reading locally is what
    makes that safe. Once the newest reading is cached, a poll only has to cover
    the gap since the last one: at `--poll-interval=3600` a 2h window is 4.8 KB
    and 96 points, a 100x reduction that is re-fetched every hour.

    Why the API rate limit does not fix this: the binding bucket is 366
    *requests*/day, and this change makes no difference to it. N facilities is N
    requests per cycle either way. What drops is bytes and server-side work per
    request, which is the scrape time that actually went up.

    **This stores readings, which the liveness file deliberately does not.** That
    is a real change to a safety argument, so it is worth stating the shape of
    it rather than glossing:

    - **A cached reading can reach a measured metric.** When a narrow window
      comes back with power but no `storage_battery` point inside it, `poll_cycle`
      substitutes the cached reading and the row is exported exactly as a live one
      would be. That is deliberate - it is the point of the buffer, and it is what
      keeps `oe_battery_soc_ratio` continuous through the hours the overnight feed
      does not publish - but it is *not* the same guarantee the liveness file
      gives, and the divergence is bounded and reported rather than assumed:
      - The value is **not** presented as fresh. It keeps its own timestamp, so
        `oe_battery_sample_age_seconds` and `oe_battery_anchor_age_hours` report
        its true age, and the row sets `reading_from_cache=True` with
        `oe_battery_reading_from_cache` counting the units in that state.
      - It is **never** used for a facility that failed to answer. A failed
        request stays `scrape_success=False`; substituting a cached reading there
        would report a battery as monitored during an upstream outage, which is
        the one thing the exporter must never do.
      - A live reading always supersedes it (`record` only moves forward), so the
        cache can lag but never contradict what the API just said.
      - It is bounded by `max_sample_age`, the same staleness limit `poll_cycle`
        applies to a live sample, so a restored reading cannot outlive a limit
        that live data would have been held to anyway.
      - Future-dated stamps are dropped, as in liveness: a skewed clock would
        otherwise pin a battery as fresh forever.
    - The counter to watch after this ships is
      `oe_batteries_reading_from_cache`. If it sits at the monitored count
      indefinitely, the buffer has stopped being an optimisation and become the
      only source of truth for the measured family.

    Bounded by one entry per unit plus a seeded-unit set - a flat map of a couple
    of hundred bytes.
    """

    def __init__(self, path: str | None = None, max_age_seconds: float = 0.0):
        self.path = path
        self.max_age_seconds = float(max_age_seconds)
        # unit -> (reading timestamp, stored MWh)
        self._points: dict[str, tuple[float, float]] = {}
        # Units ever *observed* to publish `storage_battery`. Distinct from
        # `_points`: a unit can be known to publish and still be missing from
        # `_points` right now, which is the case that has to force a wide
        # window. A unit that has never published is not that case - see
        # `_request_window`.
        self._seeded: set[str] = set()
        # The poll thread writes this while the render path reads the derived
        # rows, so the same discipline as PowerHistory applies: a bare dict
        # mutated on one thread and iterated on another is not safe.
        self._lock = threading.Lock()

    def load(self) -> int:
        """Read persisted readings. Returns how many units were restored.

        A corrupt file is a warning and an empty cache, never an exception:
        refusing to start because a cache is unreadable would turn a cosmetic
        problem into an outage.
        """
        if not self.path:
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("reading cache %s unusable, starting cold: %s", self.path, exc)
            return 0
        data = payload.get("readings") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            if data is not None:
                log.warning("reading cache %s has no readings map", self.path)
            return 0
        restored: dict[str, tuple[float, float]] = {}
        for unit, pair in data.items():
            try:
                restored[str(unit)] = (float(pair[0]), float(pair[1]))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        seeded = payload.get("seeded")
        # Absent `seeded` means a cache written before the field existed. Treat
        # it as empty rather than guessing from `readings`: `_request_window`
        # goes wide on an empty seed set, so the only cost is one wide cycle.
        known = {str(u) for u in seeded} if isinstance(seeded, list) else set()
        with self._lock:
            self._points.update(restored)
            self._seeded |= known
        return len(restored)

    def save(self) -> None:
        """Persist atomically. Best effort: an unwritable cache must not stop polling."""
        if not self.path:
            return
        with self._lock:
            units = {
                unit: [stamp, value] for unit, (stamp, value) in sorted(self._points.items())
            }
            seeded = sorted(self._seeded)
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "saved_at": time.time(),
                        "readings": units,
                        "seeded": seeded,
                    },
                    handle,
                    indent=1,
                    sort_keys=True,
                )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not write reading cache %s: %s", self.path, exc)

    def record(self, unit: str, stamp: float, value: float) -> None:
        """Note a reading, keeping whichever is newer.

        Newest-wins rather than unconditional, so that a narrow window which
        returns an older reading than the one already cached cannot walk the
        anchor backwards.
        """
        with self._lock:
            self._seeded.add(unit)
            previous = self._points.get(unit)
            if previous is None or float(stamp) > previous[0]:
                self._points[unit] = (float(stamp), float(value))

    def get(self, unit: str, now: float) -> tuple[float, float] | None:
        """Cached `(stamp, mwh)` for `unit`, or None if unusable right now.

        Rejects future-dated stamps and anything past `max_age_seconds`, so a
        restored or long-stale reading cannot outlive the staleness limit
        `poll_cycle` applies to samples that arrive live.
        """
        with self._lock:
            found = self._points.get(unit)
        if found is None:
            return None
        stamp, value = found
        if stamp > now:
            return None
        if self.max_age_seconds > 0.0 and (now - stamp) > self.max_age_seconds:
            return None
        return stamp, value

    def has(self, unit: str, now: float) -> bool:
        return self.get(unit, now) is not None

    def missing(self, units: list[str], now: float) -> list[str]:
        """In-scope units that are known to publish but cannot be reached now.

        Deliberately narrower than "every unit without a reading". The deployed
        fleet has 12 batteries in scope and only 7 that ever publish
        `storage_battery`; treating the other 5 as missing would force the wide
        window on every cycle forever and make the narrowing unreachable. A unit
        is only "missing" if this cache has seen it publish before - i.e. it is
        seeded - and no usable reading is held for it now, which is a regression
        from an observed state rather than an absence of evidence.

        An empty seed set returns everything, deliberately. That is the cold-cache
        case and it must go wide; see `_request_window`.
        """
        with self._lock:
            seeded = set(self._seeded)
        if not seeded:
            return sorted(set(units))
        return sorted(unit for unit in set(units) if unit in seeded and not self.has(unit, now))

    def items(self) -> list[tuple[str, tuple[float, float]]]:
        """Every `(unit, (stamp, mwh))` pair, unfiltered.

        Unfiltered on purpose: `get` applies the age and clock guards, and the
        caller restoring at start-up needs to count what it is throwing away as
        well as keep what it is keeping.
        """
        with self._lock:
            return sorted(self._points.items())

    def count(self) -> int:
        with self._lock:
            return len(self._points)


DEFAULT_POWER_HISTORY_DAYS = 14.0

# Observations are far smaller than power points - a handful per unit per day -
# so they are kept far longer. The reason to keep them is that the evidence only
# becomes conclusive slowly: the coefficient is worth revisiting when there are
# enough independent pairs to survive a bad night, not when there are enough to
# fit a curve.
DEFAULT_CALIBRATION_DAYS = 90.0


class PowerHistory:
    """A local, self-hosted time series of per-unit charge/discharge power.

    Inferred SOC integrates the power series between two measured energy
    readings, and until now that series came only from the API response of the
    current cycle. That couples the estimator to two things it should not
    depend on: how deep a lookback the exporter happened to ask for, and whether
    the API was reachable. A scrape that failed mid-window silently shortened
    the integration, and the result looked like a battery that had stopped.

    Keeping our own copy removes both couplings. The exporter merges what it
    cached with what the API just returned, so a cycle that comes back empty
    costs freshness rather than history.

    Deliberately *not* stored here: the inference anchor. `_last_measured_*`
    still re-derives from the API on start-up, because a persisted anchor would
    let a restart serve a stale SOC as if it were current - the one failure mode
    this exporter refuses everywhere else. Power is a bounded, self-correcting
    physical signal; an energy anchor is a claim about the present, and a file
    on disk cannot tell whether it is still true.

    The store is bounded twice over: by age (`retention_days`) and by the fact
    that it only ever holds one value per (unit, timestamp). At the shipped
    cadence that is ~24 points per unit per day, so two weeks of a 12-unit fleet
    is a few thousand pairs - small enough to keep the whole thing readable with
    `cat`, which is the same trade `load_liveness` makes.
    """

    def __init__(self, path: str | None = None, retention_days: float = DEFAULT_POWER_HISTORY_DAYS):
        self.path = path
        self.retention_days = float(retention_days)
        # unit -> {timestamp: value}. Keyed on the timestamp so a re-poll of a
        # window we already hold replaces the old reading instead of appending a
        # duplicate, which is what makes merging with a fresh response safe.
        self._points: dict[str, dict[float, float]] = {}
        # The poll thread writes this while the HTTP handler reads it, because
        # the render path re-stamps inferred rows off the cached series between
        # polls. A bare dict mutated on one thread and iterated on another can
        # raise "dictionary changed size during iteration", so the reads and
        # writes are guarded. Deliberately separate from the scraper's own lock:
        # that one is held only to publish state, and taking it here would mean
        # holding it across a file write.
        self._lock = threading.Lock()

    def record(self, series_by_unit: dict[str, list[tuple[float, float]]], now: float) -> int:
        """Merge freshly polled power samples in. Returns how many were new."""
        added = 0
        with self._lock:
            for unit, series in (series_by_unit or {}).items():
                bucket = self._points.setdefault(unit, {})
                for stamp, value in series or []:
                    try:
                        stamp = float(stamp)
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if stamp not in bucket:
                        added += 1
                    bucket[stamp] = value
            self.prune(now)
        return added

    def series(self, unit: str, since: float | None = None) -> list[tuple[float, float]]:
        """Cached points for `unit`, oldest first, optionally only >= `since`."""
        with self._lock:
            bucket = dict(self._points.get(unit) or {})
        return sorted(
            (stamp, value)
            for stamp, value in bucket.items()
            if since is None or stamp >= since
        )

    def merged(
        self, unit: str, series: list[tuple[float, float]] | None
    ) -> list[tuple[float, float]]:
        """Cached history for `unit` with `series` layered on top.

        The API's reading wins on a timestamp collision: it is the fresher
        source, and an upstream revision of an old point should not be masked by
        our older copy of it.
        """
        merged = dict(self.series(unit))
        for stamp, value in series or []:
            try:
                merged[float(stamp)] = float(value)
            except (TypeError, ValueError):
                continue
        return sorted(merged.items())

    def prune(self, now: float) -> None:
        """Drop anything older than the retention window.

        Callers hold `self._lock` already (`record` does), so this does not
        re-acquire it.
        """
        if self.retention_days <= 0:
            return
        cutoff = float(now) - self.retention_days * 86400.0
        for unit, bucket in list(self._points.items()):
            kept = {s: v for s, v in bucket.items() if s >= cutoff}
            if kept:
                self._points[unit] = kept
            else:
                del self._points[unit]

    def total_points(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._points.values())

    def save(self) -> None:
        """Persist atomically. Best effort: a read-only cache must not stop polling."""
        if not self.path:
            return
        # Snapshot under the lock, then write outside it: this can touch the
        # filesystem, and holding the history lock across that would stall the
        # render path for the length of the write.
        with self._lock:
            units = {
                unit: [[s, v] for s, v in sorted(bucket.items())]
                for unit, bucket in sorted(self._points.items())
            }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "saved_at": time.time(),
                        "retention_days": self.retention_days,
                        "units": units,
                    },
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not write power history file %s: %s", self.path, exc)

    def load(self) -> int:
        """Read a persisted history. Returns the point count, 0 if there is none.

        A corrupt file is a warning and an empty history, never an exception:
        the alternative is refusing to start because a cache is unreadable,
        which would turn a cosmetic problem into an outage.
        """
        if not self.path:
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("power history file %s unusable, starting cold: %s", self.path, exc)
            return 0
        units = payload.get("units") if isinstance(payload, dict) else None
        if not isinstance(units, dict):
            return 0
        for unit, pairs in units.items():
            bucket: dict[float, float] = {}
            for pair in pairs or []:
                try:
                    bucket[float(pair[0])] = float(pair[1])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
            if bucket:
                self._points[str(unit)] = bucket
        return self.total_points()

    def total_points(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._points.values())


class EfficiencyCalibrator:
    """Accumulates evidence about `--infer-charge-efficiency` from real readings.

    The coefficient is currently a judgement call: 0.9 was chosen from a backtest
    and never re-examined against the fleet as it actually behaves. The evidence
    needed to examine it already exists and is unambiguous - every time the feed
    publishes a second energy reading for a unit, the change between the two is
    measured, and the power series in between says how much the grid delivered
    and how much it took. That is enough to solve for the coefficient instead of
    assuming it.

    Pairs are recorded where they are formed, in `_record_readings`, which is the
    one place that sees a new reading supersede an old one. Nothing is inferred
    from Prometheus for this: Prometheus only ever exposes the newest power value
    per scrape, so the intermediate points the exporter integrates are not in it,
    and a calibration run against that proxy measures the proxy. Here the input
    is the same series inference integrates, which is the whole point.

    Observations are *evidence*, not configuration. This class never changes the
    deployed coefficient; it publishes what it measured and lets a human decide.
    The notes record a loss term being adopted on the strength of a sweep like
    this one and then reverted a day later, so the bar for acting on the number
    is deliberately high and the reasoning is exported alongside it.

    Bounded by age, and by the fact that it holds one observation per
    (unit, reading timestamp). At the published cadence that is a handful of pairs
    per unit per day, so a month of a 12-unit fleet is a few hundred rows.
    """

    def __init__(
        self,
        path: str | None = None,
        retention_days: float = DEFAULT_CALIBRATION_DAYS,
        max_gap_hours: float = 2.0,
        min_charge_mwh: float = 1.0,
        tolerance_hours: float = 0.5,
        min_pairs: int = DEFAULT_CALIBRATION_MIN_PAIRS,
    ):
        self.path = path
        self.retention_days = float(retention_days)
        self.max_gap_hours = float(max_gap_hours)
        self.min_charge_mwh = float(min_charge_mwh)
        self.tolerance_hours = float(tolerance_hours)
        self.min_pairs = int(min_pairs)
        # unit -> {reading timestamp: observation}. Keyed on the closing reading
        # so re-seeing the same reading replaces it rather than double-counting,
        # which is what makes repeated polls safe.
        self._observations: dict[str, dict[float, dict]] = {}
        # Label fields per unit, so exported series carry the same labels as every
        # other per-battery metric and Grafana's $battery variable keeps working.
        self._labels: dict[str, dict] = {}
        self._lock = threading.Lock()

    def observe(
        self,
        unit: str,
        t0: float,
        e0: float,
        t1: float,
        e1: float,
        power_series: list[tuple[float, float]],
        capacity_mwh: float | None = None,
        sample: dict | None = None,
    ) -> dict | None:
        """Record the pair formed by a reading superseding an earlier one.

        Returns the observation, or `None` when the pair cannot say anything -
        which is the common case, since a unit that only discharged over the
        interval carries no information about charging losses.
        """
        if not unit:
            return None
        observation = calibrate_pair(
            power_series,
            t0,
            e0,
            t1,
            e1,
            capacity_mwh=capacity_mwh,
            max_gap_hours=self.max_gap_hours,
            min_charge_mwh=self.min_charge_mwh,
            tolerance_hours=self.tolerance_hours,
        )
        with self._lock:
            if sample:
                self._labels[unit] = {
                    key: sample.get(key, "")
                    for key in ("facility", "unit", "facility_name", "region", "status")
                }
            if observation is None:
                return None
            bucket = self._observations.setdefault(unit, {})
            bucket[float(observation["t1"])] = dict(observation, unit=unit)
            self.prune(float(observation["t1"]))
        return observation

    def observations(self) -> list[dict]:
        with self._lock:
            return [
                dict(observation)
                for unit in sorted(self._observations)
                for _, observation in sorted(self._observations[unit].items())
            ]

    def unit_labels(self) -> dict[str, dict]:
        with self._lock:
            return {unit: dict(fields) for unit, fields in self._labels.items()}

    def pairs(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._observations.values())

    def prune(self, now: float) -> None:
        """Drop observations older than the retention window. Caller holds the lock."""
        if self.retention_days <= 0:
            return
        cutoff = float(now) - self.retention_days * 86400.0
        for unit in list(self._observations):
            bucket = self._observations[unit]
            for stamp in [s for s in bucket if s < cutoff]:
                del bucket[stamp]
            if not bucket:
                del self._observations[unit]

    def summary(self, deployed_efficiency: float) -> dict:
        result = summarise_calibration(self.observations(), deployed_efficiency,
                                       min_pairs=self.min_pairs)
        result["labels"] = self.unit_labels()
        return result

    def save(self) -> None:
        """Persist atomically. Best effort, exactly like the power history."""
        if not self.path:
            return
        with self._lock:
            units = {
                unit: [[obs["t1"], obs] for _, obs in sorted(bucket.items())]
                for unit, bucket in sorted(self._observations.items())
            }
            labels = {unit: dict(fields) for unit, fields in sorted(self._labels.items())}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "saved_at": time.time(),
                        "retention_days": self.retention_days,
                        "units": units,
                        "labels": labels,
                    },
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not write efficiency calibration file %s: %s", self.path, exc)

    def load(self) -> int:
        """Read persisted observations. A corrupt file is a warning, never fatal."""
        if not self.path:
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("efficiency calibration file %s unusable, starting cold: %s",
                        self.path, exc)
            return 0
        if not isinstance(payload, dict):
            return 0
        for unit, rows in (payload.get("units") or {}).items():
            bucket: dict[float, dict] = {}
            for row in rows or []:
                try:
                    observation = row[1]
                    bucket[float(observation["t1"])] = dict(observation, unit=str(unit))
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
            if bucket:
                self._observations[str(unit)] = bucket
        for unit, fields in (payload.get("labels") or {}).items():
            if isinstance(fields, dict):
                self._labels[str(unit)] = dict(fields)
        return self.pairs()


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


def _build_power_history(args) -> PowerHistory | None:
    """Construct the power-history store and warm it from disk, if enabled."""
    path = resolve_power_history_path(
        getattr(args, "power_history_file", None), args.fleet_cache_file
    )
    if not path:
        return None
    history = PowerHistory(path, retention_days=args.power_history_days)
    restored = history.load()
    if restored:
        log.info("restored %d cached power points from %s", restored, path)
    return history


def _build_reading_cache(args) -> ReadingCache | None:
    """Construct the energy-reading store, if persistence is configured.

    Bounded by `max_sample_age` rather than by a window of its own, so a restored
    reading cannot outlive the staleness limit `poll_cycle` already applies to
    samples that arrive live - the two must not disagree about how old a usable
    reading is allowed to be.
    """
    path = resolve_reading_cache_path(
        getattr(args, "reading_cache_file", None), args.fleet_cache_file
    )
    if not path:
        return None
    return ReadingCache(path, max_age_seconds=args.max_sample_age)


def _build_calibration(args) -> EfficiencyCalibrator | None:
    """Construct the calibration store and warm it from disk, if enabled."""
    if not getattr(args, "efficiency_calibration", False):
        return None
    path = resolve_calibration_path(
        getattr(args, "efficiency_calibration_file", None), args.fleet_cache_file
    )
    calibration = EfficiencyCalibrator(
        path,
        retention_days=getattr(args, "efficiency_calibration_days", DEFAULT_CALIBRATION_DAYS),
        max_gap_hours=getattr(args, "calibration_max_gap_hours", 2.0),
        min_charge_mwh=getattr(args, "calibration_min_charge_mwh", 1.0),
        min_pairs=getattr(args, "calibration_min_pairs", DEFAULT_CALIBRATION_MIN_PAIRS),
    )
    restored = calibration.load()
    if restored:
        log.info("restored %d efficiency calibration pairs from %s", restored, path)
    return calibration


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
        "--poll-window-hours",
        type=float,
        default=DEFAULT_POLL_WINDOW_HOURS,
        help="how far back each poll actually requests, once every unit's last "
        "energy reading is cached locally (default %s). This is the everyday "
        "cost; --lookback-hours is the worst-case reach. Widened back to "
        "--lookback-hours whenever a unit has no cached reading yet or a poll "
        "was missed, so the narrow window can only ever be an optimisation. "
        "Must comfortably exceed --poll-interval or the request re-asks for "
        "less than it consumed and the power series grows holes, which "
        "inference refuses to integrate across."
        % DEFAULT_POLL_WINDOW_HOURS,
    )
    parser.add_argument(
        "--api-interval",
        default=DEFAULT_API_INTERVAL,
        help="resolution of the one battery request, as the API's interval "
        "parameter: 5m, 1h, and so on (default %s). Free - it is a query "
        "parameter, not another request - and finer is better, because the "
        "hourly value is only the mean of the five-minute ones under it"
        % DEFAULT_API_INTERVAL,
    )
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
    parser.add_argument(
        "--enable-inferred",
        action="store_true",
        help="export synthetic SOC inferred by integrating power since the last "
        "measured storage_battery reading. Measured values are never overwritten; "
        "inferred values are emitted only while the measured SOC is stale, within "
        "max-infer-hours, and clamped.",
    )
    parser.add_argument(
        "--max-infer-hours",
        type=float,
        default=24.0,
        help="how far back to trust the anchor when integrating power for inferred "
        "SOC. Also the hard bound on inference: past it no estimate is published "
        "at all, so the setting trades a visibly-degrading estimate against no "
        "estimate. Compare against oe_battery_anchor_age_hours, which is the "
        "quantity being bounded. Sized for the normal overnight publication gap; "
        "raise it to ride out a missed upstream publication, at the cost of "
        "integrating a longer stretch of power from a single anchor.",
    )
    parser.add_argument(
        "--infer-clamp",
        action="store_true",
        default=True,
        help="clamp inferred energy to [0, capacity_mwh] (default true)",
    )
    parser.add_argument(
        "--no-infer-clamp",
        action="store_false",
        dest="infer_clamp",
        help="disable clamping of inferred energy",
    )
    parser.add_argument(
        "--infer-fresh-hours",
        type=float,
        default=2.0,
        help="a measured SOC older than this is treated as stale and replaced by "
        "inference, even though the API still returns it. storage_battery publishes "
        "overnight, so this is what hands over to inference for the daytime hours "
        "(default 2.0, about one publication gap)",
    )
    parser.add_argument(
        "--infer-charge-efficiency",
        type=float,
        default=1.0,
        help="share of grid-facing charging energy that reaches the cells when "
        "integrating power for inferred SOC; the rest is conversion loss and is "
        "never recoverable, so the battery stores this fraction of it. Applies to "
        "charging only - the discharge side is already measured at the "
        "terminals - so the value reads as a round-trip figure (0.9 = 100 MWh in, "
        "~90 MWh back out). Default 1.0, i.e. no loss term at all, so the "
        "exporter assumes nothing about the hardware; set it deliberately.",
    )
    parser.add_argument(
        "--infer-max-gap-hours",
        type=float,
        default=2.0,
        help="maximum spacing between consecutive power samples that inference "
        "will integrate across. A wider gap ends the integration instead of "
        "bridging it, so a real outage cannot be integrated over (default 2.0)",
    )
    parser.add_argument(
        "--infer-max-hold-hours",
        type=float,
        default=DEFAULT_INFER_MAX_HOLD_HOURS,
        help="how far the last observed power rate is carried forward past the "
        "newest sample, so the inferred SOC keeps moving between polls instead "
        "of sitting on a plateau until the next hourly sample lands. Scaled by "
        "the real elapsed time, so a five-minute-old rate contributes 1/12th of "
        "its hourly energy. 0 disables it and reports the exact integral "
        "instead, which is older but never extrapolated (default %.1f)"
        % DEFAULT_INFER_MAX_HOLD_HOURS,
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
    parser.add_argument(
        "--power-history-file",
        help="JSON file to cache per-unit power samples in, so inferred SOC can "
        "integrate across restarts and failed scrapes (default: power-history.json "
        "beside --fleet-cache-file; omit both for no persistence).",
    )
    parser.add_argument(
        "--reading-cache-file",
        help="JSON file to cache each unit's newest energy reading in, so polls "
        "can request a narrow window instead of re-fetching a multi-day range "
        "for one old sample (default: readings.json beside --fleet-cache-file; "
        "omit both for no cache and a wide window every cycle). Unlike the "
        "liveness file this does hold readings, but it feeds inferred SOC only "
        "and is bounded by --max-sample-age - see ReadingCache.",
    )
    parser.add_argument(
        "--power-history-days",
        type=float,
        default=DEFAULT_POWER_HISTORY_DAYS,
        help="how long to keep cached power samples (default %g days). "
        "Bounded so the file cannot grow without limit." % DEFAULT_POWER_HISTORY_DAYS,
    )
    parser.add_argument(
        "--efficiency-calibration",
        action="store_true",
        help="score the deployed --infer-charge-efficiency against consecutive "
        "measured readings and export what the evidence supports. Records a pair "
        "whenever a reading supersedes an earlier one, integrating the real power "
        "series between them. Reports only: no value is ever applied. Without "
        "this flag the exporter does no calibration work at all.",
    )
    parser.add_argument(
        "--efficiency-calibration-file",
        help="JSON file to persist calibration observations in, so evidence "
        "outlives a restart (default: efficiency-calibration.json beside "
        "--fleet-cache-file; omit both for no persistence).",
    )
    parser.add_argument(
        "--efficiency-calibration-days",
        type=float,
        default=DEFAULT_CALIBRATION_DAYS,
        help="how long to keep calibration observations (default %g days). Much "
        "longer than power history, because the pairs are the evidence and the "
        "window they were measured over is deliberately pruned."
        % DEFAULT_CALIBRATION_DAYS,
    )
    parser.add_argument(
        "--calibration-min-pairs",
        type=int,
        default=DEFAULT_CALIBRATION_MIN_PAIRS,
        help="independent pairs required before any efficiency recommendation is "
        "published (default %d). Below this the answer is noise, and a "
        "recommendation should not exist to be acted on."
        % DEFAULT_CALIBRATION_MIN_PAIRS,
    )
    parser.add_argument(
        "--calibration-max-gap-hours",
        type=float,
        default=2.0,
        help="widest power-sample gap still treated as covering an interval when "
        "scoring a pair (default 2h). Wider gaps truncate the interval and the "
        "pair is dropped rather than scored on part of its span.",
    )
    parser.add_argument(
        "--calibration-min-charge-mwh",
        type=float,
        default=1.0,
        help="minimum grid energy charged over a pair for it to be scored "
        "(default 1 MWh, or 1%% of capacity, whichever is larger). Implied "
        "efficiency divides by this, so a pair that barely charged would report "
        "an enormous number from a rounding error.",
    )
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
        interval=args.api_interval,
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
        enable_inferred=args.enable_inferred,
        max_infer_hours=args.max_infer_hours,
        infer_clamp=args.infer_clamp,
        infer_fresh_hours=args.infer_fresh_hours,
        infer_max_gap_hours=args.infer_max_gap_hours,
        infer_max_hold_hours=args.infer_max_hold_hours,
        infer_charge_efficiency=args.infer_charge_efficiency,
        power_history=_build_power_history(args),
        calibration=_build_calibration(args),
        reading_cache=_build_reading_cache(args),
        poll_window_hours=args.poll_window_hours,
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
