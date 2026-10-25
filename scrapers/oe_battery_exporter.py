#!/usr/bin/env python3
"""Prometheus exporter for NEM/WEM battery registration metadata, from OpenElectricity.

Source: the OpenElectricity API (formerly OpenNEM), at
https://api.openelectricity.org.au . One endpoint, bearer-authenticated:

    GET /facilities/
        Fleet metadata for the whole NEM, one request. Every facility, its
        region, its units, and per unit the fields that matter here:
        `fueltech_id`, `capacity_storage` (MWh), `capacity_registered` (MW) and
        `data_last_seen`. Fetched unfiltered (not `?fueltech_id=battery`)
        because the same response is cached to disk and read by the AEMO
        exporter, which needs the fueltech/region of *every* unit; `unit_rows`
        narrows it to batteries for this exporter's own series.

This exporter is **metadata-only**. It publishes what AEMO does not: the fleet
registration data (capacity, region, status) and the health of the one metadata
poll. Live state of charge, stored energy and power come from the sibling AEMO
exporter (`aemo_battery_*`, five-minute NEMWEB data), which covers the same
units. Everything this exporter used to fetch per facility - the `storage_battery`
and `power` time series, and every metric derived from them (SOC, energy,
inferred/estimated SOC, efficiency calibration, liveness, request-window
optimisation) - was retired on 2026-10-20. The single `GET /facilities/` request
per refresh is all that remains.

There is no "state of charge" metric upstream. The API publishes stored
**energy in MWh** and the fleet metadata publishes **capacity in MWh**, and SOC
is derived by the AEMO exporter from AEMO's live energy against this exporter's
registered capacity (joined `on(duid)`). `capacity_registered` here is MW
(power) and must not be used as a SOC denominator - that would be a units error
that still produces a plausible-looking number.

Two upstream quirks this is built around, both measured rather than assumed:

1. **Six series per facility, two usable.** A battery *data* facility returns
   `<CODE>1` plus `<CODE>G1` and `<CODE>L1`. Only `<CODE>1` exists in the
   fleet metadata; G1 and L1 have no `capacity_storage`. This exporter does not
   read those series any more, but the metadata still separates units by their
   capacity, and a battery unit with no positive `capacity_storage` yields no
   row here - there would be no registered capacity to publish for it.

2. **Facilities come and go.** The metadata carries committed and retired units
   that have never dispatched. `oe_battery_fleet_info` publishes the *whole*
   fleet - including those units - because describing them is the point: the
   AEMO exporter's polling set will never tell you they exist.

Exposed metrics

    oe_battery_capacity_storage_mwh{facility,unit,name,region,status}
                                              MWh, from metadata
    oe_battery_fleet_capacity_storage_mwh{facility,unit,name,region,status}
                                              MWh, whole fleet (same labels)
    oe_battery_fleet_info{duid,facility,facility_name,region,network,status,
        capacity_storage_mwh,capacity_registered_mw,data_last_seen}
                                              value 1; registration metadata for
                                              every battery unit
    oe_battery_fleet_capacity_mwh             whole fleet total, MWh
    oe_last_fleet_refresh_timestamp_seconds   when the metadata was last read
    oe_last_poll_timestamp_seconds            when the last poll completed
    oe_api_credits_remaining                  daily budget, from /me
    oe_api_requests_total                     counter, includes failures

`oe_battery_fleet_info` is an info metric: value 1, with the unit's fields in
the labels, so a Prometheus recording rule or a Grafana variable can filter by
facility, network or status without a lookup table. The battery's own code is
published under `duid` here (rather than the `unit` the other series use)
because that is the identifier AEMO dispatch series join to.

Request budget
--------------
The free Community plan allows 500 credits/day but also rate-limits by bucket:
**8 requests / 5 min, 32 / 1 h, 366 / 1 day** (verified against /v1/plans).
Metadata-only polling costs **one request per cycle** - the whole `/facilities/`
response, refreshed at most once per `--fleet-refresh-interval` (24h by
default). That is one request a day, well inside every bucket, which is why
`--poll-interval` can be relaxed and Prometheus can scrape this exporter as
often as it likes at zero additional credit cost.

`oe_api_credits_remaining` reads the free `/me` endpoint each cycle (verified
free: repeated reads on their own cost nothing) so the budget stays visible.

The API also **rejects the default `Python-urllib` User-Agent with HTTP 403**,
which is why USER_AGENT below is explicit and must not be dropped. Verified
2026-09-30: `Python-urllib/3.12` -> 403, `curl/8.5.0` -> 200. It is a bot rule
keyed on that exact string, and it fails as a bare 403 with no body explaining
why.

Usage
    oe_battery_exporter.py --api-key-file=/run/secrets/openelectricity
    oe_battery_exporter.py --once
    oe_battery_exporter.py --api-key-env=OPENEA_API_KEY --poll-interval=86400

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fleet

DEFAULT_API_BASE = "https://api.openelectricity.org.au"
DEFAULT_NETWORK = "NEM"

# Not cosmetic. The API 403s urllib's default UA outright; see the module
# docstring. The sibling exporters spoof a browser for HTML feeds, which is
# pointless here - this is a documented JSON API and it gates on the literal
# "Python-urllib" string, so a truthful UA is both allowed and correct.
USER_AGENT = "aus-infra-dashboard/1.0 (+battery-soc-exporter)"

# Fueltech this exporter keeps when flattening the (unfiltered) fleet response
# into battery rows. The API separates a battery into `battery`,
# `battery_charging` and `battery_discharging`; `battery` is the facility
# classification and is what carries capacity_storage. The siblings are the
# facility's storage re-reported through its charge and discharge paths, so they
# are not separate rows - see scrapers/README.md.
BATTERY_FUELTECH = "battery"

DEFAULT_POLL_INTERVAL = 24 * 3600.0

# Metadata is essentially static - capacity changes a few times a year - so
# re-reading `/facilities/` more often than daily buys nothing. Prometheus now
# scrapes every 5m, so the exporter must re-serve the cached metadata on every
# scrape while only hitting the OE API approximately once per hour.
DEFAULT_FLEET_REFRESH_INTERVAL = 24 * 3600.0
HOURLY_REFRESH_INTERVAL = 3600.0

# The /me credit read is free, but there is no reason to make it more than once
# per poll.
DEFAULT_REQUEST_INTERVAL = 0.6

MAX_ERROR_BODY = 200
RETRY_BASE_DELAY = 0.5

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

        Metadata-only polling is two requests per cycle, so the burst limit is
        never approached; this is kept for the same reason as the sibling
        exporters, so a future change cannot accidentally burst.
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
                # expected and retrying cannot change it.
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

    def facilities(self) -> list[dict]:
        """The whole NEM facility list, one request.

        Deliberately unfiltered: the exporter only needs the battery units, but
        the same response is cached to disk and read by the sibling AEMO
        exporter, which needs every unit's fueltech and region to attribute its
        measured SCADA MW. `unit_rows` narrows this to batteries for the
        battery-specific series. WEM facilities ride along harmlessly - their
        unit codes never match an AEMO dispatch DUID.
        """
        payload = self.get("/facilities/")
        facilities = payload.get("data") or []
        if not isinstance(facilities, list):
            raise ScrapeError("/facilities/ -> data is %s, not a list" % type(facilities).__name__)
        return facilities

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
# Fleet metadata
# --------------------------------------------------------------------------- #


def unit_rows(fl: "fleet.Fleet | None", require_data: bool = True) -> list[dict]:
    """One row per battery unit in a parsed fleet.

    What comes from `fleet`: which unit codes are units (a duplicated code is
    one unit, not two), which facility each belongs to, and its region, network
    and status. What stays here is which units this exporter publishes and how
    it labels them - presentation, not fleet knowledge.

    `capacity_storage` is a *unit* field, not a facility field, so the capacity
    has to be looked up per unit. A unit with no positive capacity yields no row
    rather than a series with no value: there would be no registered capacity to
    publish for it.

    `require_data` drops units with no `data_last_seen`, and it defaults on for
    a reason that is not tidiness. **The largest batteries in the fleet are
    mostly unbuilt.** As of 2026-09-30, of the ten highest-capacity units, seven
    are `committed` and have never dispatched: Richmond Valley (2200 MWh, the
    largest in the country), Tomago (2000), Baranduda (1886), Wooreen (1400),
    Elaine (1244), Western Downs 3 (1220), Supernode 3 (1217). Every one of them
    has no data to report. The numeric capacity series skip them; the
    whole-fleet info metric does not (callers pass `require_data=False` for
    that), because describing exactly those units is its purpose.

    No status filter: `committed` and `retired` units are published in both
    shapes, gated only by `require_data` - see scrapers/README.md.
    """
    if fl is None:
        return []
    rows: list[dict] = []
    for unit in fl.units:
        if unit.fueltech != BATTERY_FUELTECH:
            continue
        if unit.capacity_storage_mwh is None:
            continue
        if require_data and not unit.data_last_seen:
            continue
        rows.append(
            {
                "facility": unit.facility,
                "facility_name": unit.facility_name,
                "region": unit.region,
                # The endpoint is per-network, and the fleet spans two. Ten
                # of the battery facilities are WEM and querying one of those
                # under network_code=NEM returns HTTP 404 - which was masking
                # a routing bug, so the network is carried per row from the
                # facility metadata rather than assumed.
                "network": unit.network,
                "unit": unit.code,
                "status": unit.status,
                "capacity_mwh": unit.capacity_storage_mwh,
                # Named for what it is, not `power_mw`: `power_mw` is the
                # battery's *instantaneous* dispatch power, and the two would
                # be silently interchangeable in a name.
                "registered_mw": unit.capacity_registered_mw,
                "data_last_seen": unit.data_last_seen,
            }
        )
    return rows


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


def info_set(row: dict) -> str:
    """Label set for `oe_battery_fleet_info`.

    The battery's own code is published under `duid` here (rather than the
    `unit` the other series use) because that is the identifier AEMO dispatch
    series join to: `aemo_battery_power_mw` and friends carry `duid`, so a
    `group_left(region) oe_battery_fleet_info` needs the same name on both
    sides. The capacity figures ride as label strings - that is what an info
    metric is for - and a missing figure (registered MW on an old metadata
    row) renders as an empty value rather than `NaN`.
    """
    capacity_mwh = row.get("capacity_mwh")
    registered_mw = row.get("registered_mw")
    return (
        'duid="%s",facility="%s",facility_name="%s",region="%s",'
        'network="%s",status="%s",capacity_storage_mwh="%s",'
        'capacity_registered_mw="%s",data_last_seen="%s"'
        % (
            escape_label(row.get("unit", "")),
            escape_label(row.get("facility", "")),
            escape_label(row.get("facility_name", "")),
            escape_label(row.get("region", "")),
            escape_label(row.get("network", "")),
            escape_label(row.get("status", "")),
            fmt(capacity_mwh)
            if isinstance(capacity_mwh, (int, float)) and not isinstance(capacity_mwh, bool)
            else "",
            fmt(registered_mw)
            if isinstance(registered_mw, (int, float)) and not isinstance(registered_mw, bool)
            else "",
            escape_label(str(row.get("data_last_seen") or "")),
        )
    )


def render(state: dict, now: float) -> str:
    lines: list[str] = []

    # Registration/capacity facts only. The live SOC, stored energy, power and
    # their timestamps come from the AEMO exporter (aemo_battery_*), which
    # covers the same units at five-minute resolution; they were retired here on
    # 2026-10-20 rather than kept as a second, slower source for the same
    # quantity, and with them the whole per-facility fetch.
    fleet_rows = state.get("fleet_rows") or []
    if fleet_rows:
        lines.append(
            "# HELP oe_battery_capacity_storage_mwh Registered storage capacity "
            "of a NEM/WEM battery unit, MWh, from the fleet metadata."
        )
        lines.append("# TYPE oe_battery_capacity_storage_mwh gauge")
        for row in fleet_rows:
            lines.append(
                "oe_battery_capacity_storage_mwh{%s} %s"
                % (label_set(row), fmt(row["capacity_mwh"]))
            )

    # Static registration metadata for every battery unit in the fleet, not just
    # those with data. As an info metric it is joined to per-unit series
    # (aemo_battery_power_mw, the AEMO SOC gauges) with group_left(), which is
    # what lets a Prometheus recording rule or a Grafana variable filter by
    # facility, network or status without a lookup table. Published for the whole
    # metadata - committed and never-built units included - because those are
    # exactly the units the live feed never mentions; take the value 1 and read
    # the labels. Nothing is emitted until the fleet metadata has been loaded at
    # least once, so absence of the metric (not just of rows) is the "metadata
    # not yet read" signal.
    fleet_meta = state.get("fleet_meta") or []
    if fleet_meta:
        lines.append(
            "# HELP oe_battery_fleet_info Registration metadata for a NEM/WEM "
            "battery unit. Value is always 1; the unit lives in the labels: duid, "
            "facility, facility_name, region, network, status, storage capacity in "
            "MWh and registered capacity in MW (both as label strings, suitable "
            "for display and filtering), and the date the unit was last seen on "
            "the data endpoint, empty for units that have never published. Covers "
            "the whole fleet including committed, never-built units, so a duid "
            "appears here whenever the metadata knows it, dispatched or not."
        )
        lines.append("# TYPE oe_battery_fleet_info gauge")
        for row in fleet_meta:
            lines.append("oe_battery_fleet_info{%s} 1" % info_set(row))

        # Whole-fleet numeric complement to the info gauge above. The info metric
        # carries capacity as a *label string*, which is exactly wrong for an
        # operand: PromQL cannot divide by a label, and a dashboard that wants %
        # of capacity for a unit needs a number. Same five labels as every
        # per-unit series, same value shape (MWh), one row per battery unit in
        # the metadata.
        lines.append(
            "# HELP oe_battery_fleet_capacity_storage_mwh Registered storage "
            "capacity of a NEM/WEM battery unit in MWh, for the whole fleet "
            "metadata. Same five labels as every per-unit series, so a % of "
            "capacity for any unit can be built as energy / on(unit) this series."
        )
        lines.append("# TYPE oe_battery_fleet_capacity_storage_mwh gauge")
        for row in fleet_meta:
            lines.append(
                "oe_battery_fleet_capacity_storage_mwh{%s} %s"
                % (label_set(row), fmt(row["capacity_mwh"]))
            )

    lines.append(
        "# HELP oe_battery_fleet_capacity_mwh Total registered storage capacity "
        "of the whole enumerated fleet, MWh."
    )
    lines.append("# TYPE oe_battery_fleet_capacity_mwh gauge")
    lines.append(
        "oe_battery_fleet_capacity_mwh %s" % fmt(state.get("oe_battery_fleet_capacity_mwh"))
    )

    lines.append(
        "# HELP oe_last_poll_timestamp_seconds Unix time of the last completed poll cycle."
    )
    lines.append("# TYPE oe_last_poll_timestamp_seconds gauge")
    lines.append(
        "oe_last_poll_timestamp_seconds %s" % fmt(state.get("oe_last_poll_timestamp_seconds"))
    )

    lines.append(
        "# HELP oe_last_fleet_refresh_timestamp_seconds Unix time the fleet "
        "metadata was last re-read."
    )
    lines.append("# TYPE oe_last_fleet_refresh_timestamp_seconds gauge")
    lines.append(
        "oe_last_fleet_refresh_timestamp_seconds %s"
        % fmt(state.get("oe_last_fleet_refresh_timestamp_seconds"))
    )

    lines.append(
        "# HELP oe_api_credits_remaining OpenElectricity API credits remaining in the current day, from /me."
    )
    lines.append("# TYPE oe_api_credits_remaining gauge")
    lines.append("oe_api_credits_remaining %s" % fmt(state.get("oe_api_credits_remaining")))

    lines.append("# HELP oe_api_requests_total Authenticated API requests issued since start, including any that failed.")
    lines.append("# TYPE oe_api_requests_total counter")
    lines.append("oe_api_requests_total %s" % fmt(state.get("oe_api_requests_total")))

    return "\n".join(lines) + "\n"


# The fleet cache file itself - its on-disk shape, the atomic write, and the
# read that tolerates a missing or corrupt file - lives in `fleet`, next to the
# parse that this exporter and its two readers share. See docs/adr/0002.


# --------------------------------------------------------------------------- #
# Exporter
# --------------------------------------------------------------------------- #


class BatteryScraper:
    def __init__(
        self,
        client: OpenElectricityClient,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        fleet_refresh_interval: float = DEFAULT_FLEET_REFRESH_INTERVAL,
        fleet_file: str | None = None,
        require_data: bool = True,
        now_fn=time.time,
    ) -> None:
        self.client = client
        self.poll_interval = poll_interval
        self.fleet_refresh_interval = fleet_refresh_interval
        self.fleet_file = fleet_file
        self.require_data = require_data
        self.now_fn = now_fn
        self._state: dict = {}
        # The units with data, filtered by the dispatchability gate - the rows
        # that back the numeric capacity series.
        self._fleet: list[dict] = []
        # The whole fleet, unfiltered by the data gate: every battery unit in the
        # metadata, committed and never-built included. It backs
        # oe_battery_fleet_info, whose whole point is to describe units the
        # numeric series will never tell you about.
        self._fleet_meta: list[dict] = []
        self._fleet_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = (True, "no poll completed yet")
        # Prometheus now scrapes every 5m; only hit the OE API ~once per hour,
        # re-serving cached metadata between real refreshes.
        self._last_api_refresh = 0.0
        self._api_refresh_interval = HOURLY_REFRESH_INTERVAL
        # Keep the last successful API responses to re-serve between refreshes.
        self._last_credits = None

    # -- fleet -------------------------------------------------------------- #

    def load_fleet(self, force: bool = False) -> list[dict]:
        """Re-read the fleet metadata, at most once per refresh interval.

        The metadata is essentially static - capacity does not change - so
        polling it every cycle would waste a request per cycle on data that
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
        if fresh and not force:
            return self._fleet
        try:
            facilities = self.client.facilities()
        except (ScrapeError, AuthError) as exc:
            cached = self.cached_fleet()
            if cached is None:
                raise
            rows, meta, cached_at = cached
            log.warning("using cached fleet metadata (%s): %s", self.fleet_file, exc)
            with self._lock:
                self._fleet, self._fleet_at = rows, cached_at
                self._fleet_meta = meta
            return rows
        fl = fleet.from_facilities(facilities)
        rows = unit_rows(fl, self.require_data)
        if not rows:
            raise ScrapeError("/facilities/ -> no battery units with a storage capacity")
        if self.fleet_file:
            fleet.write_fleet(self.fleet_file, facilities)
        with self._lock:
            self._fleet, self._fleet_at = rows, now
            # require_data=False here is not an oversight: the info metric is
            # metadata for the whole fleet, and the units `require_data` drops
            # (committed, never dispatched) are precisely the ones the numeric
            # rows cannot describe. Both row shapes come from the same parsed
            # fleet, so they can never disagree.
            self._fleet_meta = unit_rows(fl, require_data=False)
        return rows

    def cached_fleet(self) -> tuple[list[dict], list[dict], float] | None:
        """Rows, full-fleet metadata rows, and cache age from the on-disk
        fleet, or None if unusable. The metadata rows are the `require_data=False`
        shape, mirroring what a live fetch stores.

        Read through `fleet` rather than by parsing the JSON here, so the file
        is interpreted one way whether it has just been written by this exporter
        or read back days later after the API failed.
        """
        if not self.fleet_file:
            return None
        fl = fleet.load(self.fleet_file)
        if fl is None:
            return None
        rows = unit_rows(fl, self.require_data)
        if not rows:
            return None
        meta = unit_rows(fl, require_data=False)
        return (rows, meta, fl.cached_at)

    # -- poll --------------------------------------------------------------- #

    def poll_once(self) -> tuple[str, bool]:
        # Deliberately NOT holding self._lock for the network work. The lock is
        # only taken to publish state, because /metrics has to stay answerable
        # while the request is in flight.
        started = time.monotonic()
        errors: list[str] = []
        rows: list[dict] = []
        now = self.now_fn()
        with self._lock:
            fleet_ready = bool(self._fleet) and (now - self._fleet_at) < self.fleet_refresh_interval
            need_api_refresh = (now - self._last_api_refresh) >= self._api_refresh_interval

        try:
            if need_api_refresh or not fleet_ready:
                rows = self.load_fleet(force=True)
                credits = self.client.credits_remaining()
                with self._lock:
                    self._last_api_refresh = now
                    self._last_credits = credits
            else:
                # No API call - re-serve cached metadata every 5m scrape.
                with self._lock:
                    rows = self._fleet
                    credits = self._last_credits
        except AuthError as exc:
            log.error("authentication rejected: %s", exc)
            with self._lock:
                self._health = (False, str(exc))
            return "", False
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("poll failed: %s", exc)
            errors = [str(exc)]
            credits = None

        finished = self.now_fn()
        state = {
            "oe_battery_fleet_capacity_mwh": sum(r["capacity_mwh"] for r in self._fleet),
            "oe_api_credits_remaining": credits,
            "oe_api_requests_total": self.client.requests_made,
            "oe_last_poll_timestamp_seconds": finished,
            "oe_last_fleet_refresh_timestamp_seconds": self._fleet_at,
            "fleet_rows": rows,
            "fleet_meta": self._fleet_meta or [],
            "now": finished,
        }
        ok = not errors
        with self._lock:
            self._state = state
            self._health = (ok, "; ".join(errors))
        return render(state, self.now_fn()), ok

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.exception("poll cycle raised; continuing")
            # Jitter so two clones of this stack do not land on the same minute.
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
        if not state:
            return render({}, now), health[0]
        return render(state, now), health[0]

    def health(self) -> tuple[bool, str]:
        with self._lock:
            return self._health


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


INDEX_BODY = """<!doctype html>
<title>NEM battery registration metadata</title>
<h1>NEM battery registration metadata</h1>
<p>From OpenElectricity: registered storage capacity and unit registration
metadata for the NEM/WEM battery fleet. Live state of charge comes from the
AEMO exporter.</p>
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
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds between poll cycles (default %d). Metadata-only polling "
        "costs one request per cycle, so this can be relaxed freely; the "
        "Prometheus scrape interval is independent and costs nothing."
        % int(DEFAULT_POLL_INTERVAL),
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
        "still has something to serve (and so the AEMO exporter can read it)",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL,
        help="minimum seconds between API requests (default %.1f, which keeps the "
        "exporter inside the plan's documented 2/s burst limit)"
        % DEFAULT_REQUEST_INTERVAL,
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-budget", type=float, default=6.0)
    parser.add_argument(
        "--include-undispatched",
        action="store_true",
        help="also include battery units that have never been observed "
        "dispatching in the numeric capacity series. Off by default because the "
        "largest units in the fleet are mostly committed projects with no data; "
        "oe_battery_fleet_info always covers them regardless.",
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
        poll_interval=args.poll_interval,
        fleet_refresh_interval=args.fleet_refresh_interval,
        fleet_file=args.fleet_cache_file,
        require_data=not args.include_undispatched,
    )

    if args.once:
        body, success = scraper.poll_once()
        sys.stdout.write(body)
        return 0 if success else 1

    if not args.no_background_poll:
        scraper.start()

    server = ThreadingHTTPServer((args.listen_address, args.port), make_handler(scraper))
    log.info(
        "serving http://%s:%d/metrics, refreshing fleet metadata every %ds",
        args.listen_address,
        args.port,
        int(args.fleet_refresh_interval),
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
