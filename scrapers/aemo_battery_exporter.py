#!/usr/bin/env python3
"""Prometheus exporter for AEMO-reported battery energy storage.

Source: AEMO NEMWEB `Next_Day_Dispatch` archive, published daily. Verified by
downloading and parsing real files on 2026-10-08:

    https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/

is an HTML directory listing of `PUBLIC_NEXT_DAY_DISPATCH_<YYYYMMDD>_<seq>.zip`
files, one per dispatch day, kept for about a year. Each zip holds a single
~126 MB CSV of the same name; inside it the `UNIT_SOLUTION` table (73 columns)
carries, per 5-minute interval, `INITIAL_ENERGY_STORAGE` and `ENERGY_STORAGE`
in MWh and the dispatched output `TOTALCLEARED` in MW. For the units that
report storage - 67 of 572 scheduled units in the 2026-10-07 file - those are
the *measured* energy-storage figures AEMO publishes, and `TOTALCLEARED` is the
charge/discharge rate (negative = charging/absorbing, positive =
discharging/generating). Row layout: header lines start
`I,DISPATCH,UNIT_SOLUTION,6,...`, data lines start
`D,DISPATCH,UNIT_SOLUTION,6,...`, the storage columns sit at indices 69 and 70
and `TOTALCLEARED` at index 14 of the header.

This is the number NEMPulse-type dashboards use for state of charge: stored
energy divided by registered capacity. It is denser and whole-fleet where the
OpenElectricity `storage_battery` series (see oe_battery_exporter.py) is sparse
overnight-only for the handful of NEM/WEM facilities that publish it, and it is
measured - no integration behind it. AEMO asks for no key and no signup; the
archive is free and anonymous, back to the late 1990s.

Beyond the daily snapshot this exporter also polls two *intraday* AEMO archives,
each publishing a small file every 5-minute dispatch interval, which is where a
NEMPulse-style "live" view actually comes from. There is no public per-DUID
energy-storage figure that advances intraday - `UNIT_SOLUTION` storage only
exists in the daily file - so the live picture is built from the two things AEMO
does publish each interval:

* `Dispatch_SCADA` - `PUBLIC_DISPATCHSCADA_*.zip`, a ~4 KB single-interval
  snapshot with, per DUID, the unit's *measured* `SCADAVALUE` in MW (AEMO's
  screen convention: negative = charging/absorbing, positive = generating). That
  is the live charge/discharge rate per battery.
* `DispatchIS_Reports` - `PUBLIC_DISPATCHIS_*.zip`, a ~23 KB interim-solution
  snapshot whose `DISPATCH,REGIONSUM` table carries the *reported* total stored
  energy of all battery dispatch units, per region (`BDU_ENERGY_STORAGE` /
  `BDU_INITIAL_ENERGY_STORAGE`, MWh) plus each region's 5-minute price. The
  region-level stored MWh is the only AEMO figure that is both live and
  "measured" rather than dispatched.

Both files are identified by name in `/Reports/Current/`, both polled on their
own short cadence, and both filtered to the units the daily report has learned
are storage units (a battery set that survives restarts via the state file).
AEMO publishes no per-DUID live SOC; NEMPulse's own docs say their live SOC is
this daily reported anchor plus an integration of the 5-minute SCADA output at
~85% round-trip efficiency, which is exactly the kind of inference this exporter
leaves to the OE exporter and does not pretend to be measured.

The unavoidable caveat is freshness: this report is next-day. The file for
dispatch day D is published ~04:11 AEST on D+1 and covers intervals from
D 04:05 to D+1 04:00, so even right after publication the newest interval is a
few hours old, and by midnight it is ~20h old. The value only advances when the
next day's file lands. That is not a bug in the exporter: `sample_age_seconds`
and `report_generated_age_seconds` are the honest clock, and the consumer
decides how stale an anchor it will accept. Nothing here is presented as live.

Storage values are reported for some units on only part of the day (a battery
that begins dispatching after 04:05, or stops before the next 04:00), so the
exporter takes, per unit, the newest interval that actually reported a value -
the same "newest non-null sample" rule as the sibling OE exporter - rather than
assuming every unit reaches the final interval.

Timezone: AEMO MMS timestamps are AEST year-round (UTC+10, no daylight saving),
including the settlement times in this file, so they are parsed with a pinned
+10 offset, the same reason the Snowy exporter pins its offset and never imports
zoneinfo: python:3.12-alpine ships no tzdata.

The file is 8 MB compressed / 126 MB uncompressed, so the download cannot be
hosted by the scrape handler - a scrape that took 30s+ to download would blow
Prometheus's scrape_timeout and mark a healthy target down every morning. Like
the OE exporter, this one therefore polls on its own schedule in a background
thread and serves the **last completed cycle** to Prometheus, which can then
scrape as often as it likes for free. Each poll fetches only the ~90 KB listing,
and the ~126 MB zip is downloaded only when the listing shows a file we have not
processed. The listing is re-read on every poll, so "the newest file is still
PUBLIC_NEXT_DAY_DISPATCH_20261007_..." is verified fresh each hour - the
measurements are reused, never the assumption. `--state-file` persists the last
processed report so a restart does not force a fresh 126 MB download of an
unchanged file; a corrupt or missing state file costs a re-download, nothing
else (see `aemo_battery_report_reused`).

A second thread polls the two intraday archives (`Dispatch_SCADA`,
`DispatchIS_Reports`) on `--intraday-poll-interval` (default 300s). Those files
are tiny, one interval each, and advance every 5 minutes, so the two together
cost a few tens of KB per cycle - well inside what a free anonymous archive can
take from one host, and the reason the "one download a day" frame above does not
apply to them. The intraday cycle is a separate success domain from the daily
one (`aemo_battery_scada_success`, `aemo_battery_dispatchis_success`), and on a
failure it drops its own series rather than repeating them, exactly like the
daily side.

Exposed metrics. Per unit, carrying one label `duid` (the AEMO dispatch unit id,
which matches the OE exporter's unit codes - ERB01, WALGRV1, ...):

    aemo_battery_energy_stored_mwh{duid}       ENERGY_STORAGE, newest interval (MWh)
    aemo_battery_initial_energy_stored_mwh{duid}  INITIAL_ENERGY_STORAGE (MWh)
    aemo_battery_total_cleared_mw{duid}        TOTALCLEARED dispatch for the newest
                                               interval (MW; negative = charging)
    aemo_battery_last_sample_timestamp_seconds{duid}  interval SETTLEMENTDATE, Unix
    aemo_battery_sample_age_seconds{duid}      its age at export time

Unlabelled report and poll gauges:

    aemo_battery_report_info{file}              always 1; carries the processed zip
    aemo_battery_report_generated_timestamp_seconds  file's publication stamp
    aemo_battery_report_generated_age_seconds  its age at export time
    aemo_battery_report_newest_interval_timestamp_seconds  newest interval in the report
    aemo_battery_units_reporting               units with a storage sample
    aemo_battery_report_reused                 1 = this poll served the already-held
                                               report because the newest file was unchanged
    aemo_battery_scrape_success                1/0
    aemo_battery_poll_duration_seconds         wall time of the last cycle
    aemo_battery_last_poll_timestamp_seconds   when the last cycle finished

The intraday thread adds, per unit at the newest *measured* interval:

    aemo_battery_power_mw{duid}                Dispatch_SCADA SCADAVALUE (measured
                                               MW; negative = charging)
    aemo_battery_power_timestamp_seconds{duid} that interval's SETTLEMENTDATE, Unix
    aemo_battery_power_age_seconds{duid}       its age at export time

per region at the newest interval:

    aemo_battery_region_stored_mwh{region}            BDU_ENERGY_STORAGE (MWh)
    aemo_battery_region_initial_stored_mwh{region}    BDU_INITIAL_ENERGY_STORAGE
    aemo_battery_region_storage_timestamp_seconds{region}  interval SETTLEMENTDATE

and the intraday bookkeeping:

    aemo_battery_power_report_info{file}              the SCADA file processed
    aemo_battery_power_interval_timestamp_seconds     its interval
    aemo_battery_units_with_power                     storage units with a reading
    aemo_battery_region_storage_report_info{file}     the DispatchIS file processed
    aemo_battery_region_storage_interval_timestamp_seconds  its interval
    aemo_battery_scada_success                        1/0
    aemo_battery_dispatchis_success                   1/0
    aemo_battery_intraday_poll_duration_seconds       wall time of the last cycle
    aemo_battery_intraday_last_poll_timestamp_seconds when it finished

The intraday thread also publishes a per-unit *inferred* stored MWh (MWh), the
NEMPulse-style dead-reckoning the OE exporter already does: anchor at the daily
report's per-unit ENERGY_STORAGE and integrate the measured SCADA output each
5-minute interval. Charging stores `charge_efficiency x |MW| x dt`, discharging
deducts MW x dt in full, and a unit absent from SCADA longer than
MAX_INFER_GAP_HOURS is dropped until a fresh daily report re-anchors it. An
anchor is never stamped forward: an entry's timestamp is the time its value
actually belongs to, so when a live SCADA step is longer than one poll interval
the gap is rebuilt from the archived Dispatch_SCADA files (one per 5-minute
interval) before the live integration continues - a restart mid-morning
reconstructs the day's charging instead of anchoring at a 04:00 value that is
hours old and silently claiming it is current. When the archive cannot cover
the gap (too shallow, too old, or --no-infer-backfill), the series stays absent
until the next daily report rather than extrapolating the stale anchor. The
integral is floored at zero - stored energy cannot go negative - so an
estimate a stale or mis-sign-conventioned anchor drives below zero is pinned at
empty and flagged rather than silently showing a negative MWh.

    aemo_battery_inferred_stored_mwh{duid}       anchor + integrated SCADA (MWh)
    aemo_battery_inferred_clamped{duid}          1 = raw integral fell below 0
    aemo_battery_inferred_timestamp_seconds{duid}  the integrated interval
    aemo_battery_inferred_age_seconds{duid}      its age at export time
    aemo_battery_inferred_units                  inferred units being served
    aemo_battery_inferred_clamped_units          ... of them pinned at the zero floor
    aemo_battery_infer_success                   1/0 (series is actually served)
    aemo_battery_inferred_anchor_age_seconds     age of the daily anchor interval;
                                                 large here = stale anchor
    aemo_battery_infer_charge_efficiency         the stored share of charging energy
    aemo_battery_inferred_report_info{file}      the daily report anchoring it

The pinned flag is the OE exporter's `--infer-clamp` convention: it reports the
raw integral going negative whether or not clamping is applied, so a battery
held at empty is distinguishable from one genuinely empty. `--no-infer-clamp`
serves the raw drift instead, for inspecting the estimate rather than reading
it. The OE exporter clamps the top end too, against registered capacity; this
exporter has no capacity in scope (that is OpenElectricity data), so it can
only floor.

A unit is a "storage" unit if it reports a non-empty INITIAL_ENERGY_STORAGE in
any interval; everything else in UNIT_SOLUTION is a generator and is skipped.
`aemo_battery_scrape_success` is 0 when the listing or the zip could not be read
or produced no storage units, and the per-unit series are then omitted rather
than repeated - the same "never serve a measurement you did not just verify"
rule as the sibling exporters. When the newest file is unchanged the *verified*
report is re-rendered each scrape, with ages recomputed from the clock, so the
values refresh daily while freshness stays honest.

Usage
    aemo_battery_exporter.py                     # serve /metrics on 127.0.0.1:9112
    aemo_battery_exporter.py --once              # print exposition to stdout and exit
    aemo_battery_exporter.py --listen-address 0.0.0.0 --port 9112 --state-file=/cache/state.json
    aemo_battery_exporter.py --poll-interval=21600 --state-file=/cache/state.json
    aemo_battery_exporter.py --intraday-poll-interval=300
    aemo_battery_exporter.py --intraday-poll-interval=300 \
        --infer-charge-efficiency=0.9 --infer-state-file=/cache/infer-state.json
    aemo_battery_exporter.py --no-infer-clamp    # serve raw drift below zero

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import errno
import io
import json
import logging
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_DATA_URL = "https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/"
SCADA_DATA_URL = "https://nemweb.com.au/Reports/Current/Dispatch_SCADA/"
DISPATCHIS_DATA_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"

# NEMWEB is a free, anonymous, keyless archive, so unlike the OE exporter there
# is no rate budget to conserve - the poll cadence only needs to keep up with a
# file that is published once a day. Hourly checks a new file within the hour,
# and each unchanged check costs one ~90 KB listing.
DEFAULT_POLL_INTERVAL = 3600.0

# The intraday pair publishes a new ~5 KB file every 5-minute dispatch interval,
# so a 300s poll follows the intervals without hammering the archive: two tiny
# listings plus a handful of KB of zip per cycle, maybe a serial duplicate when a
# poll lands inside the same interval (that is what the reuse check absorbs).
DEFAULT_INTRADAY_POLL_INTERVAL = 300.0

# AEMO does not publish per-unit storage intraday, so the exporter dead-reckons
# it: start each unit at the daily report's per-unit ENERGY_STORAGE and integrate
# the 5-minute Dispatch_SCADA output. `charge_efficiency` is the share of
# charging energy that is actually stored (discharging is deducted in full),
# the exact convention of the OE exporter's --infer-charge-efficiency; the
# default 1.0 matches the coefficient OE is *deployed* with, so the two inferred
# feeds stay directly comparable.
DEFAULT_INFER_CHARGE_EFFICIENCY = 1.0

# A unit that stops appearing in Dispatch_SCADA is unscheduled (idle), so a short
# absence is held frozen at its last value - but beyond this long a frozen number
# is a guess (a retag, a maintenance cycle, a feed regression), and the inferred
# line is dropped until the next daily report re-anchors it.
MAX_INFER_GAP_HOURS = 6.0

# A step of up to an hour is treated as a brief poll hiccup and integrated at
# the one measured sample we have - the historical behaviour, and roughly what a
# missed poll or two looks like. Anything longer is a hole whose power shape is
# unknown: it must be rebuilt from the archived Dispatch_SCADA files one
# 5-minute interval at a time (see _backfill), or the unit is dropped.
MAX_INFER_LIVE_STEP_SECONDS = 3600.0

# The archive path (run the arithmetic back the other way: a day has 288
# 5-minute intervals, so MAX_INFER_GAP_HOURS on a fresh 04:00 anchor means a
# restart after 04:00 can reconstruct a span of up to a day at ~4 KB a file).
# Since _reanchor_if_needed resets to that day's report, the span from anchor
# to now is bounded by the daily cadence itself; 24 h of files plus the anchor
# is the most a reconstruction ever needs, and the oldest file check refuses a
# shallower archive rather than extrapolating a stale anchor.
MAX_INFER_BACKFILL_HOURS = 24.0

# Reconstructing the daily gap from the archive is the difference between a
# restart mid-day reading empty and reading the true state of charge, but it
# costs one listing plus up to 288 small zips once per anchor. On by default;
# --no-infer-backfill disables the archive fetches and the stale series simply
# stays absent until the next daily report.
DEFAULT_INFER_BACKFILL = True

# Stored energy cannot be negative, so the SCADA-integrated estimate is floored
# at zero by default, the same envelope the OE exporter clamps to (its
# --infer-clamp). This exporter has no capacity in scope to clamp the top end
# against, so it can only floor; --no-infer-clamp serves the raw drift instead.
# Either way `aemo_battery_inferred_clamped` reports a raw integral that went
# negative, so a battery pinned at empty is never mistaken for a genuinely empty
# one - the OE exporter's reported-rather-than-hidden convention.
DEFAULT_INFER_CLAMP = True

# The zip download is the only large transfer (8 MB), so the defaults are roomier
# than the sibling exporters': a listing that hangs is rare, and a retry budget
# cut to 6s (the OE/ArcGIS default) could abandon a slow-but-working download.
# 60s timeout vs 30s budget means a stuck request gives up inside one timeout;
# the scrape that matters is never blocked because scrapes are served from the
# last completed cycle, not a download.
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BUDGET = 30.0

# AEMO publishes NEM timestamps in Australian Eastern Standard Time, UTC+10,
# and deliberately does NOT apply daylight saving (the whole NEM runs on AEST).
# Pinning +10 keeps the exporter on stdlib only - python:3.12-alpine ships no
# tzdata, so ZoneInfo("Australia/Sydney") would raise in the container - and the
# pinned offset is exact here, not an approximation.
AEST_UTC_OFFSET_SECONDS = 10 * 3600

# An error body from these listing pages is not meaningful, but the cap keeps a
# misrouted HTML page from bloating a log line.
MAX_ERROR_BODY = 200

RETRY_BASE_DELAY = 0.5

log = logging.getLogger("aemo_battery_exporter")


class ScrapeError(Exception):
    """Raised when the upstream cannot be read or understood."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


# Same set as the sibling exporters: the faults a second attempt a moment later
# can plausibly clear. The one that actually bites inside a container is
# EAI_AGAIN (-3, "Try again") from getaddrinfo, which Docker's embedded resolver
# at 127.0.0.11 raises intermittently when forwarding to systemd-resolved.
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
    """`GET url -> HTTP 404: <body>` (body trimmed to one line)."""
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
    outlast half a poll interval and pile up behind the one download a day.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
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


# --------------------------------------------------------------------------- #
# Upstream shape
# --------------------------------------------------------------------------- #


# A filename is the sort key in one string: dispatch date then publisher
# sequence. Lexicographic order is chronological, and a same-day *correction*
# carries a larger sequence number, so taking the max of the names picks up
# corrections automatically.
REPORT_FILE_RE = re.compile(r"PUBLIC_NEXT_DAY_DISPATCH_\d{8}_\d+\.zip")


def list_report_files(html: str) -> list[str]:
    """Every Next_Day_Dispatch zip name in a listing page, sorted."""
    return sorted(set(REPORT_FILE_RE.findall(html)))


def newest_report_file(html: str) -> str:
    """The newest published zip, or ScrapeError when the listing has none.

    The listing always shows a year of files, so an empty list means the page
    changed shape; failing loudly beats endlessly re-downloading the same old
    file forever.
    """
    names = list_report_files(html)
    if not names:
        raise ScrapeError("listing contained no PUBLIC_NEXT_DAY_DISPATCH_*.zip links")
    return names[-1]


# The intraday files are one interval each and named for it:
# PUBLIC_DISPATCHSCADA_<YYYYMMDDHHMM>_<seq>.zip and
# PUBLIC_DISPATCHIS_<YYYYMMDDHHMM>_<seq>.zip. The same rules apply as the daily
# listing: lexicographic max is chronological and picks up corrections.
SCADA_FILE_RE = re.compile(r"PUBLIC_DISPATCHSCADA_\d{12}_\d+\.zip")
SCADA_FILE_NAME_RE = re.compile(r"PUBLIC_DISPATCHSCADA_(\d{12})_\d+\.zip")
DISPATCHIS_FILE_RE = re.compile(r"PUBLIC_DISPATCHIS_\d{12}_\d+\.zip")


def _newest(html: str, pattern: re.Pattern, label: str) -> str:
    names = sorted(set(pattern.findall(html)))
    if not names:
        raise ScrapeError("listing contained no %s links" % label)
    return names[-1]


def newest_scada_file(html: str) -> str:
    return _newest(html, SCADA_FILE_RE, "PUBLIC_DISPATCHSCADA_*.zip")


def scada_file_timestamp(name: str) -> float | None:
    """`PUBLIC_DISPATCHSCADA_<YYYYMMDDHHMM>_<seq>.zip` -> its interval, Unix.

    AEMO names the file for the settlement interval (UTC+10), so the timestamp
    is recoverable without fetching it; a malformed stamp returns None.
    """
    match = SCADA_FILE_NAME_RE.match(name)
    if not match:
        return None
    stamp = match.group(1)
    try:
        naive = datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone(timedelta(seconds=AEST_UTC_OFFSET_SECONDS))).timestamp()


def scada_files_in_range(html: str, start_ts: float, end_ts: float) -> list[str]:
    """Archived Dispatch_SCADA file names whose interval overlaps [start, end].

    Returns ascending; used by the inference backfill to reconstruct the power
    shape across a gap between a stale anchor and the live measurement. Only the
    overlap is returned, never the whole page (corrections and the previous days
    are filtered out by interval).
    """
    names = []
    for name in sorted(set(SCADA_FILE_RE.findall(html))):
        ts = scada_file_timestamp(name)
        if ts is None:
            continue
        if start_ts <= ts <= end_ts:
            names.append(name)
    return names


def newest_dispatchis_file(html: str) -> str:
    return _newest(html, DISPATCHIS_FILE_RE, "PUBLIC_DISPATCHIS_*.zip")


def parse_aest(text: object) -> float | None:
    """AEMO "2026/10/07 04:05:00" -> Unix seconds (pinned UTC+10).

    Returns None for a missing or unparseable stamp rather than raising: one
    bad clock field in a 164k-row file must cost that row, not the report.
    """
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        naive = datetime.strptime(text[:19], "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone(timedelta(seconds=AEST_UTC_OFFSET_SECONDS))).timestamp()


def _unit_solution_indices(header_row: list[str]) -> tuple[int, int, int] | None:
    """(initial, energy, cleared) column indices for a UNIT_SOLUTION header.

    Indexed by *name*, not position, so a future column reorder upstream does
    not silently read the wrong number. Returns None when the table header is
    missing the storage or dispatch columns, in which case the file has changed
    shape.
    """
    wanted = (
        "DUID",
        "SETTLEMENTDATE",
        "TOTALCLEARED",
        "INITIAL_ENERGY_STORAGE",
        "ENERGY_STORAGE",
    )
    positions = {}
    for name in wanted:
        try:
            positions[name] = header_row.index(name)
        except ValueError:
            return None
    return (
        positions["INITIAL_ENERGY_STORAGE"],
        positions["ENERGY_STORAGE"],
        positions["TOTALCLEARED"],
    )


def parse_next_day_dispatch(data: bytes) -> dict:
    """Reduce one Next_Day_Dispatch CSV into per-unit newest measurements.

    Returns {"generated": float|None, "newest_interval": float|None,
    "units": {duid: {"ts": float, "initial": float, "energy": float,
    "cleared": float|None}}}.

    The CSV is streamed from the zip one row at a time - the uncompressed file
    is ~126 MB, so it must never be materialised in memory. The parser keeps
    only the newest interval per storage unit that reported a value.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScrapeError("zip corrupted or not a zip: %s" % exc) from exc
    members = [
        name
        for name in archive.namelist()
        if name.rpartition(".")[-1].lower() == "csv"
    ]
    if not members:
        raise ScrapeError("zip contained no .csv member (%s)" % archive.namelist())
    filename = members[0].rsplit(".", 1)[0]

    units: dict[str, dict] = {}
    generated: float | None = None
    newest_interval: float | None = None
    storage_indices: tuple[int, int, int] | None = None
    handled_rows = 0

    with archive.open(members[0]) as member:
        reader = csv_row_reader(member)
        for row in reader:
            if not row:
                continue
            tag = row[0]
            if tag == "I":
                if len(row) >= 3 and row[2] == "UNIT_SOLUTION":
                    storage_indices = _unit_solution_indices(row)
                continue
            if tag == "C":
                # The first control line is the publication stamp:
                # C,NEMP.WORLD,NEXT_DAY_DISPATCH,AEMO,PUBLIC,2026/10/08,04:10:00,...
                if len(row) >= 7 and row[1] == "NEMP.WORLD":
                    generated = parse_aest(row[5] + " " + row[6])
                continue
            if tag != "D" or storage_indices is None:
                continue
            if len(row) < 3 or row[2] != "UNIT_SOLUTION":
                continue
            if storage_indices is None:
                continue
            ini_idx, eng_idx, clr_idx = storage_indices
            if len(row) <= max(eng_idx, clr_idx):
                continue
            handled_rows += 1
            initial = row[ini_idx]
            if not initial:
                # Units that never report storage (generators) have this empty;
                # the presence of a value is what makes a unit a storage unit.
                continue
            stamp = parse_aest(row[4]) if len(row) > 4 else None
            if stamp is None:
                continue
            duid = row[6] if len(row) > 6 else ""
            if not duid:
                continue
            entry = units.get(duid)
            if entry is None or stamp >= entry["ts"]:
                energy = row[eng_idx]
                cleared = row[clr_idx]
                try:
                    initial_value = float(initial)
                    energy_value = float(energy) if energy else initial_value
                    cleared_value = float(cleared) if cleared else None
                except ValueError:
                    log.warning("duid %s: non-numeric storage at %s", duid, stamp)
                    continue
                units[duid] = {
                    "ts": stamp,
                    "initial": initial_value,
                    "energy": energy_value,
                    "cleared": cleared_value,
                }
            if newest_interval is None or stamp > newest_interval:
                newest_interval = stamp

    if not units:
        raise ScrapeError("no storage units found in %s (handled %d rows)" % (filename, handled_rows))
    return {
        "file": filename,
        "generated": generated,
        "newest_interval": newest_interval,
        "units": units,
    }


def csv_row_reader(stream):
    """Rows from a compressed member stream.

    Kept as a thin wrapper so the test suite can feed plain in-memory bytes
    through the same path without reaching for a temp file.
    """
    return csv.reader(io.TextIOWrapper(stream, encoding="utf-8", errors="replace"))


def _unit_scada_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a UNIT_SCADA header, by name.

    Verified against the real file on 2026-10-08: SETTLEMENTDATE, DUID,
    SCADAVALUE at indices 4, 5, 6. Indexed by name so a column reorder upstream
    cannot silently read the wrong number.
    """
    positions = {}
    for name in ("SETTLEMENTDATE", "DUID", "SCADAVALUE"):
        try:
            positions[name] = header_row.index(name)
        except ValueError:
            return None
    return positions


def parse_dispatch_scada(data: bytes) -> dict:
    """Reduce one Dispatch_SCADA CSV into per-unit measured output.

    Returns {"file": str, "newest_interval": float, "units": {duid: {"ts": float,
    "scada": float}}}. The file is a single 5-minute interval snapshot, so each
    DUID appears once; where a unit does appear twice (a re-sent row), the later
    stamp wins. Every row is kept here; the caller filters to the storage units
    it cares about.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScrapeError("zip corrupted or not a zip: %s" % exc) from exc
    members = [
        name
        for name in archive.namelist()
        if name.rpartition(".")[-1].lower() == "csv"
    ]
    if not members:
        raise ScrapeError("zip contained no .csv member (%s)" % archive.namelist())
    filename = members[0].rsplit(".", 1)[0]

    units: dict[str, dict] = {}
    newest: float | None = None
    indices: dict[str, int] | None = None
    handled_rows = 0
    with archive.open(members[0]) as member:
        for row in csv_row_reader(member):
            if not row:
                continue
            if row[0] == "I":
                if len(row) >= 3 and row[2] == "UNIT_SCADA":
                    indices = _unit_scada_indices(row)
                continue
            if row[0] != "D" or indices is None:
                continue
            if len(row) < 3 or row[2] != "UNIT_SCADA":
                continue
            stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
            duid = row[indices["DUID"]]
            if stamp is None or not duid:
                continue
            handled_rows += 1
            try:
                value = float(row[indices["SCADAVALUE"]])
            except ValueError:
                log.warning("duid %s: non-numeric SCADA at %s", duid, stamp)
                continue
            entry = units.get(duid)
            if entry is None or stamp >= entry["ts"]:
                units[duid] = {"ts": stamp, "scada": value}
            if newest is None or stamp > newest:
                newest = stamp

    if not units:
        raise ScrapeError(
            "no UNIT_SCADA rows in %s (handled %d)" % (filename, handled_rows)
        )
    return {"file": filename, "newest_interval": newest, "units": units}


def _region_sum_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a DISPATCH,REGIONSUM header, by name.

    Verified against the real file on 2026-10-08: SETTLEMENTDATE at 4, REGIONID
    at 6, BDU_ENERGY_STORAGE at 124, BDU_INITIAL_ENERGY_STORAGE at 129. The BDU
    columns report the aggregate stored MWh of all battery dispatch units in a
    region and are empty for regions with none (TAS1). The table never appears
    for regions with no storage, and a region whose BDU columns are both empty
    is skipped.
    """
    positions = {}
    for name in ("SETTLEMENTDATE", "REGIONID", "BDU_INITIAL_ENERGY_STORAGE",
                 "BDU_ENERGY_STORAGE"):
        try:
            positions[name] = header_row.index(name)
        except ValueError:
            return None
    return positions


def parse_dispatchis_region_storage(data: bytes) -> dict:
    """Reduce one DispatchIS CSV into per-region reported stored MWh.

    Returns {"file": str, "newest_interval": float, "regions": {region: {"ts":
    float, "initial": float, "energy": float}}}. Only regions that report a
    non-empty BDU_ENERGY_STORAGE or BDU_INITIAL_ENERGY_STORAGE are kept; the
    stored figure inside each row falls back to the initial when blank.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScrapeError("zip corrupted or not a zip: %s" % exc) from exc
    members = [
        name
        for name in archive.namelist()
        if name.rpartition(".")[-1].lower() == "csv"
    ]
    if not members:
        raise ScrapeError("zip contained no .csv member (%s)" % archive.namelist())
    filename = members[0].rsplit(".", 1)[0]

    regions: dict[str, dict] = {}
    newest: float | None = None
    indices: dict[str, int] | None = None
    handled_rows = 0
    with archive.open(members[0]) as member:
        for row in csv_row_reader(member):
            if not row:
                continue
            if row[0] == "I":
                if len(row) >= 3 and row[1] == "DISPATCH" and row[2] == "REGIONSUM":
                    indices = _region_sum_indices(row)
                continue
            if row[0] != "D" or indices is None:
                continue
            if len(row) < 3 or row[1] != "DISPATCH" or row[2] != "REGIONSUM":
                continue
            if len(row) <= indices["BDU_ENERGY_STORAGE"]:
                continue
            stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
            region = row[indices["REGIONID"]]
            if stamp is None or not region:
                continue
            handled_rows += 1
            initial_text = row[indices["BDU_INITIAL_ENERGY_STORAGE"]]
            energy_text = row[indices["BDU_ENERGY_STORAGE"]]
            if not initial_text and not energy_text:
                continue  # TAS1: a region with no BDU storage reports blanks
            try:
                initial = float(initial_text) if initial_text else None
                energy = float(energy_text) if energy_text else initial
            except ValueError:
                log.warning("region %s: non-numeric BDU storage at %s", region, stamp)
                continue
            entry = regions.get(region)
            if entry is None or stamp >= entry["ts"]:
                regions[region] = {"ts": stamp, "initial": initial, "energy": energy}
            if newest is None or stamp > newest:
                newest = stamp

    if not regions:
        raise ScrapeError(
            "no REGIONSUM BDU storage in %s (handled %d)" % (filename, handled_rows)
        )
    return {"file": filename, "newest_interval": newest, "regions": regions}


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


# Per-unit gauges, in the order they are documented. `key` is the per-unit
# field in a parsed report.
UNIT_GAUGE_SPECS = (
    ("aemo_battery_initial_energy_stored_mwh",
     "AEMO-reported energy storage at the START of the newest interval, MWh.",
     "initial"),
    ("aemo_battery_energy_stored_mwh",
     "AEMO-reported energy storage at the END of the newest interval, MWh.",
     "energy"),
    ("aemo_battery_total_cleared_mw",
     "AEMO TOTALCLEARED dispatch for the newest interval, MW "
     "(negative = charging/absorbing).",
     "cleared"),
    ("aemo_battery_last_sample_timestamp_seconds",
     "Unix time of the newest reported interval behind the storage readings.",
     "ts"),
    ("aemo_battery_sample_age_seconds",
     "Age of that interval at export time.",
     "_age"),
)


def render(
    report: dict,
    success: bool,
    duration: float,
    now: float,
) -> str:
    """Exposition text for a parsed report (or empty, on failure).

    Ages are computed from `now` here, not stored, so a report that is reused
    across polls keeps reporting honest, growing ages instead of frozen ones.
    """
    lines: list[str] = []
    units = report.get("units") or {}
    if units and success:
        for template, help_text, key in UNIT_GAUGE_SPECS:
            lines.append("# HELP %s %s" % (template, help_text))
            lines.append("# TYPE %s gauge" % template)
            for duid in sorted(units):
                unit = units[duid]
                if key == "_age":
                    stamp = unit.get("ts")
                    value = (now - stamp) if stamp is not None else None
                else:
                    value = unit.get(key)
                lines.append(
                    '%s{duid="%s"} %s'
                    % (template, escape_label(duid), fmt(value))
                )
        lines += [
            "# HELP aemo_battery_units_reporting Storage units in the report.",
            "# TYPE aemo_battery_units_reporting gauge",
            "aemo_battery_units_reporting %d" % len(units),
        ]
    else:
        lines += [
            "# HELP aemo_battery_units_reporting Storage units in the report.",
            "# TYPE aemo_battery_units_reporting gauge",
            "aemo_battery_units_reporting 0",
        ]

    if success and report.get("file"):
        generated = report.get("generated")
        newest_interval = report.get("newest_interval")
        lines += [
            "# HELP aemo_battery_report_info Processed Next_Day_Dispatch file.",
            "# TYPE aemo_battery_report_info gauge",
            'aemo_battery_report_info{file="%s"} 1'
            % escape_label(report["file"]),
            "# HELP aemo_battery_report_generated_timestamp_seconds "
            "Publication time of the processed file.",
            "# TYPE aemo_battery_report_generated_timestamp_seconds gauge",
            "aemo_battery_report_generated_timestamp_seconds %s" % fmt(generated),
            "# HELP aemo_battery_report_generated_age_seconds "
            "Age of the processed file at export time.",
            "# TYPE aemo_battery_report_generated_age_seconds gauge",
            "aemo_battery_report_generated_age_seconds %s"
            % (fmt(now - generated) if generated is not None else "NaN"),
            "# HELP aemo_battery_report_newest_interval_timestamp_seconds "
            "Newest 5-minute interval in the processed file.",
            "# TYPE aemo_battery_report_newest_interval_timestamp_seconds gauge",
            "aemo_battery_report_newest_interval_timestamp_seconds %s"
            % fmt(newest_interval),
        ]

    lines += [
        "# HELP aemo_battery_report_reused "
        "1 when this poll served an already-held report because the newest "
        "file was unchanged.",
        "# TYPE aemo_battery_report_reused gauge",
        "aemo_battery_report_reused %d" % (1 if report.get("reused") else 0),
        "# HELP aemo_battery_scrape_success "
        "Whether the last poll of the dispatch archive succeeded.",
        "# TYPE aemo_battery_scrape_success gauge",
        "aemo_battery_scrape_success %d" % (1 if success else 0),
        "# HELP aemo_battery_poll_duration_seconds Wall time of the last poll cycle.",
        "# TYPE aemo_battery_poll_duration_seconds gauge",
        "aemo_battery_poll_duration_seconds %s" % fmt(duration),
        "# HELP aemo_battery_last_poll_timestamp_seconds Unix time of the last poll cycle.",
        "# TYPE aemo_battery_last_poll_timestamp_seconds gauge",
        "aemo_battery_last_poll_timestamp_seconds %s" % fmt(now),
    ]
    return "\n".join(lines) + "\n"


def render_intraday(
    scada: dict,
    scada_ok: bool,
    region_storage: dict,
    region_ok: bool,
    duration: float,
    now: float,
    infer: dict | None = None,
    infer_ok: bool = False,
) -> str:
    """Exposition for the intraday cycle's last readings.

    Each side is independent: a failed SCADA read omits the per-unit power
    series while a healthy DispatchIS read still publishes the region storage
    (and vice versa); each has its own success gauge, and neither repeats a
    measurement it did not verify this cycle. The per-unit *inferred* stored MWh
    is a fourth, coupled side: it is served only when a daily anchor exists AND
    the SCADA feed is live this cycle, since it is built from SCADA.
    """
    lines: list[str] = []
    units = scada.get("units") or {}
    if units and scada_ok:
        lines += [
            "# HELP aemo_battery_power_mw Measured output at the newest interval, "
            "MW (negative = charging/absorbing).",
            "# TYPE aemo_battery_power_mw gauge",
        ]
        for duid in sorted(units):
            u = units[duid]
            lines += [
                'aemo_battery_power_mw{duid="%s"} %s' % (escape_label(duid), fmt(u["scada"])),
                'aemo_battery_power_timestamp_seconds{duid="%s"} %s'
                % (escape_label(duid), fmt(u["ts"])),
                'aemo_battery_power_age_seconds{duid="%s"} %s'
                % (escape_label(duid), fmt(now - u["ts"])),
            ]
        lines += [
            "# HELP aemo_battery_power_report_info Processed Dispatch_SCADA file.",
            "# TYPE aemo_battery_power_report_info gauge",
            'aemo_battery_power_report_info{file="%s"} 1'
            % escape_label(scada.get("file", "")),
            "# HELP aemo_battery_power_interval_timestamp_seconds "
            "Newest 5-minute interval in the SCADA file.",
            "# TYPE aemo_battery_power_interval_timestamp_seconds gauge",
            "aemo_battery_power_interval_timestamp_seconds %s"
            % fmt(scada.get("newest_interval")),
            "# HELP aemo_battery_units_with_power Storage units in the SCADA file.",
            "# TYPE aemo_battery_units_with_power gauge",
            "aemo_battery_units_with_power %d" % len(units),
        ]

    regions = region_storage.get("regions") or {}
    if regions and region_ok:
        lines += [
            "# HELP aemo_battery_region_initial_stored_mwh BDU_INITIAL_ENERGY_"
            "STORAGE at the newest interval, MWh.",
            "# TYPE aemo_battery_region_initial_stored_mwh gauge",
            "# HELP aemo_battery_region_stored_mwh BDU_ENERGY_STORAGE at the "
            "newest interval, MWh.",
            "# TYPE aemo_battery_region_stored_mwh gauge",
            "# HELP aemo_battery_region_storage_timestamp_seconds Newest interval "
            "behind the region storage readings.",
            "# TYPE aemo_battery_region_storage_timestamp_seconds gauge",
        ]
        for region in sorted(regions):
            r = regions[region]
            lines += [
                'aemo_battery_region_initial_stored_mwh{region="%s"} %s'
                % (escape_label(region), fmt(r["initial"])),
                'aemo_battery_region_stored_mwh{region="%s"} %s'
                % (escape_label(region), fmt(r["energy"])),
                'aemo_battery_region_storage_timestamp_seconds{region="%s"} %s'
                % (escape_label(region), fmt(r["ts"])),
            ]
        lines += [
            "# HELP aemo_battery_region_storage_report_info Processed DispatchIS "
            "file.",
            "# TYPE aemo_battery_region_storage_report_info gauge",
            'aemo_battery_region_storage_report_info{file="%s"} 1'
            % escape_label(region_storage.get("file", "")),
            "# HELP aemo_battery_region_storage_interval_timestamp_seconds "
            "Newest 5-minute interval in the DispatchIS file.",
            "# TYPE aemo_battery_region_storage_interval_timestamp_seconds gauge",
            "aemo_battery_region_storage_interval_timestamp_seconds %s"
            % fmt(region_storage.get("newest_interval")),
        ]

    infer_units = (infer or {}).get("units") or {}
    if infer_units and infer_ok:
        lines += [
            "# HELP aemo_battery_inferred_stored_mwh Anchor ENERGY_STORAGE plus "
            "SCADA-integrated change, MWh (charge_efficiency x charging energy is "
            "stored; discharging is deducted in full).",
            "# TYPE aemo_battery_inferred_stored_mwh gauge",
            "# HELP aemo_battery_inferred_clamped Whether the raw SCADA-"
            "integrated stored MWh went below zero and was floored at 0. "
            "Reported whether or not clamping is applied, so a battery pinned "
            "at empty is distinguishable from one genuinely empty.",
            "# TYPE aemo_battery_inferred_clamped gauge",
            "# HELP aemo_battery_inferred_timestamp_seconds Integrated interval "
            "behind the inferred stored MWh.",
            "# TYPE aemo_battery_inferred_timestamp_seconds gauge",
            "# HELP aemo_battery_inferred_age_seconds Age of that interval at "
            "export time.",
            "# TYPE aemo_battery_inferred_age_seconds gauge",
        ]
        for duid in sorted(infer_units):
            u = infer_units[duid]
            lines += [
                'aemo_battery_inferred_stored_mwh{duid="%s"} %s'
                % (escape_label(duid), fmt(u["mwh"])),
                'aemo_battery_inferred_clamped{duid="%s"} %s'
                % (escape_label(duid), fmt(u.get("clamped", 0.0))),
                'aemo_battery_inferred_timestamp_seconds{duid="%s"} %s'
                % (escape_label(duid), fmt(u["ts"])),
                'aemo_battery_inferred_age_seconds{duid="%s"} %s'
                % (escape_label(duid), fmt(now - u["ts"])),
            ]
        lines += [
            "# HELP aemo_battery_inferred_units Inferred units being served.",
            "# TYPE aemo_battery_inferred_units gauge",
            "aemo_battery_inferred_units %d" % len(infer_units),
            "# HELP aemo_battery_inferred_clamped_units Inferred units held at "
            "the zero floor because the raw integral went negative.",
            "# TYPE aemo_battery_inferred_clamped_units gauge",
            "aemo_battery_inferred_clamped_units %d"
            % sum(1 for u in infer_units.values() if float(u.get("clamped", 0.0)) > 0.0),
            "# HELP aemo_battery_inferred_report_info Daily report anchoring the "
            "inferred series.",
            "# TYPE aemo_battery_inferred_report_info gauge",
            'aemo_battery_inferred_report_info{file="%s"} 1'
            % escape_label(infer.get("file", "")),
        ]
    lines += [
        "# HELP aemo_battery_infer_success Whether the inferred stored-MWh series "
        "is being served (fresh anchor, live SCADA, and any anchor gap "
        "reconstructed from the archive).",
        "# TYPE aemo_battery_infer_success gauge",
        "aemo_battery_infer_success %d" % (1 if infer_ok else 0),
        "# HELP aemo_battery_inferred_anchor_age_seconds Age of the daily-report "
        "interval behind the inferred series, at export time. The per-unit "
        "values advance with SCADA; this is the anchor itself, so a fleet-wide "
        "stale anchor reads large here even while individual ages look fine.",
        "# TYPE aemo_battery_inferred_anchor_age_seconds gauge",
        "aemo_battery_inferred_anchor_age_seconds %s"
        % fmt(now - (infer or {}).get("anchor_ts")
              if isinstance((infer or {}).get("anchor_ts"), (int, float)) else None),
        "# HELP aemo_battery_infer_charge_efficiency Share of charging energy "
        "counted as stored when integrating SCADA.",
        "# TYPE aemo_battery_infer_charge_efficiency gauge",
        "aemo_battery_infer_charge_efficiency %s"
        % fmt((infer or {}).get("efficiency", DEFAULT_INFER_CHARGE_EFFICIENCY)),
        "# HELP aemo_battery_scada_success "
        "Whether the last poll of the Dispatch_SCADA archive succeeded.",
        "# TYPE aemo_battery_scada_success gauge",
        "aemo_battery_scada_success %d" % (1 if scada_ok else 0),
        "# HELP aemo_battery_dispatchis_success "
        "Whether the last poll of the DispatchIS_Reports archive succeeded.",
        "# TYPE aemo_battery_dispatchis_success gauge",
        "aemo_battery_dispatchis_success %d" % (1 if region_ok else 0),
        "# HELP aemo_battery_intraday_poll_duration_seconds "
        "Wall time of the last intraday poll cycle.",
        "# TYPE aemo_battery_intraday_poll_duration_seconds gauge",
        "aemo_battery_intraday_poll_duration_seconds %s" % fmt(duration),
        "# HELP aemo_battery_intraday_last_poll_timestamp_seconds "
        "Unix time of the last intraday poll cycle.",
        "# TYPE aemo_battery_intraday_last_poll_timestamp_seconds gauge",
        "aemo_battery_intraday_last_poll_timestamp_seconds %s" % fmt(now),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Poller state
# --------------------------------------------------------------------------- #


def _state_to_json(report: dict) -> dict:
    return {
        "file": report["file"],
        "generated": report.get("generated"),
        "newest_interval": report.get("newest_interval"),
        "units": {
            duid: {
                "ts": u["ts"],
                "initial": u["initial"],
                "energy": u["energy"],
                "cleared": u.get("cleared"),
            }
            for duid, u in report["units"].items()
        },
    }


def _state_from_json(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    file_name = payload.get("file")
    units = payload.get("units")
    if not isinstance(file_name, str) or not isinstance(units, dict):
        return None
    clean: dict[str, dict] = {}
    for duid, entry in units.items():
        if not isinstance(duid, str) or not isinstance(entry, dict):
            continue
        try:
            ts = float(entry["ts"])
            initial = float(entry["initial"])
            energy = float(entry.get("energy", entry["initial"]))
            cleared = entry.get("cleared")
            cleared_value = float(cleared) if cleared is not None else None
        except (TypeError, ValueError, KeyError):
            continue
        if ts <= 0 or initial < 0:
            continue
        clean[duid] = {
            "ts": ts,
            "initial": initial,
            "energy": energy,
            "cleared": cleared_value,
        }
    if not clean:
        return None
    return {
        "file": file_name,
        "generated": payload.get("generated"),
        "newest_interval": payload.get("newest_interval"),
        "units": clean,
    }


def load_state(path: str) -> dict | None:
    """Last processed report from disk, or None when missing/corrupt.

    Missing and corrupt are the same outcome (a cold start and a re-download).
    Measurements are only ever *served* after the live listing is re-read and
    says the newest file is unchanged; the file is a restart cache, not a
    second source of truth.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _state_from_json(json.load(handle))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable state file %s: %s", path, exc)
        return None


def save_state(path: str, report: dict) -> None:
    """Atomic whole-file write of the processed report."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_state_to_json(report), handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write state file %s: %s", path, exc)


class NextDayScraper:
    def __init__(
        self,
        data_url: str,
        timeout: float,
        poll_interval: float,
        retries: int = 0,
        retry_budget: float = 0.0,
        state_file: str | None = None,
    ) -> None:
        self.data_url = data_url.rstrip("/") + "/"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.retries = retries
        self.retry_budget = retry_budget
        self.state_file = state_file
        # Injectable for tests.
        self.now_fn = time.time
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._health = (False, "not polled yet")
        self._last_poll: float | None = None
        self._last_duration = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- poll --------------------------------------------------------------- #

    def poll_once(self) -> tuple[str, bool]:
        """One full cycle: list, (re)download if changed, publish state.

        The network work runs OUTSIDE the lock (the same decision as the OE
        exporter): /metrics must stay answerable while an 8 MB download is in
        flight, and a scrape that needs concurrent access to the state must not
        queue behind one download a day.
        """
        started = time.monotonic()
        success = False
        report: dict = {}
        errors: list[str] = []
        try:
            listing = http_get(self.data_url, self.timeout, self.retries, self.retry_budget)
            newest = newest_report_file(listing.decode("utf-8", "replace"))
            report = self._resolve_report(newest)
            success = True
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("poll failed: %s", exc)
            errors = [str(exc)]

        finished = self.now_fn()
        duration = time.monotonic() - started
        if success and report.get("file") and self.state_file and not report.get("reused"):
            save_state(self.state_file, report)
        # On failure report is {}: the per-unit series are omitted, never
        # repeated from the previous cycle. A transient glitch therefore shows
        # as a gap plus scrape_success 0, and the next successful poll either
        # re-downloads a new file or re-loads the last good one from the state
        # file (still verified against this cycle's fresh listing first).
        with self._lock:
            self._state = report
            self._last_poll = finished
            self._last_duration = duration
            self._health = (success, "" if success else "; ".join(errors))
        return render(report, success, duration, finished), success

    def _resolve_report(self, newest: str) -> dict:
        """The report to serve for `newest`, downloading only when needed.

        Served without a download when the newest published file is one we
        already hold - in memory from an earlier poll, or on disk from a state
        file written before a restart. The listing compared against is always
        fetched fresh in this cycle, so a file we have not actually re-read is
        verified to *be* the newest the publisher offers, and the ages on the
        reused measurements are recomputed from the clock at render time.
        """
        held = self._state
        if held is not None and held.get("file") == newest:
            report = dict(held)
            report["reused"] = True
            return report
        if self.state_file:
            persisted = load_state(self.state_file)
            if persisted is not None and persisted.get("file") == newest:
                report = dict(persisted)
                report["reused"] = True
                return report
        return self._download(newest)

    def _download(self, filename: str) -> dict:
        url = urllib.parse.urljoin(self.data_url, filename)
        log.info("downloading %s", url)
        payload = http_get(url, self.timeout, self.retries, self.retry_budget)
        report = parse_next_day_dispatch(payload)
        report["file"] = filename
        report["reused"] = False
        return report

    # -- serving ------------------------------------------------------------ #

    def exposition(self) -> tuple[str, bool]:
        now = self.now_fn()
        with self._lock:
            state = self._state
            health = self._health
            duration = self._last_duration
        if not state:
            return render({}, False, 0.0, now), health[0]
        return render(state, health[0], duration, now), health[0]

    def health(self) -> tuple[bool, str]:
        with self._lock:
            return self._health

    def storage_duids(self) -> frozenset[str]:
        """The DUIDs the daily report currently classifies as storage units.

        Before the first successful poll the set is empty; the intraday poller
        learns it from here (and is seeded from the state file at startup so a
        restart does not have to wait for the first daily poll).
        """
        with self._lock:
            state = self._state
        if not state:
            return frozenset()
        units = state.get("units")
        return frozenset(units) if units else frozenset()

    def daily_report(self) -> dict:
        """A shallow snapshot of the processed daily report (file + units).

        The intraday poller uses it as the inference anchor: when the `file`
        changes, every unit is re-anchored at its per-unit ENERGY_STORAGE.
        """
        with self._lock:
            state = self._state
        if not isinstance(state, dict):
            return {}
        report = {"file": state.get("file", ""), "units": state.get("units") or {}}
        return report

    # -- poll loop ---------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.exception("poll cycle raised; continuing")
            interval = self.poll_interval * (0.95 + 0.1 * random.random())
            self._stop.wait(interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="aemondd-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class IntradayPoller:
    """Polls the two intraday archives on their 5-minute cadence.

    Each cycle reads the `Dispatch_SCADA` listing (measured MW per unit) and the
    `DispatchIS_Reports` listing (region-level BDU stored MWh), downloads only
    the newest zip of each, and filters the unit rows to the storage units the
    daily report has learned. The two sides fail independently, and like the
    daily scraper the poll runs outside the lock so /metrics never queues behind
    a fetch. A cycle carries no state file - these files are small, refresh
    every five minutes, and nothing here is worth a restart cache beyond the
    battery set, which main() seeds from the daily state file.
    """

    def __init__(
        self,
        scada_url: str,
        dispatchis_url: str,
        timeout: float,
        poll_interval: float,
        retries: int = 0,
        retry_budget: float = 0.0,
        battery_duids: set | None = None,
        storage_duids_fn=None,
        daily_report_fn=None,
        charge_efficiency: float = DEFAULT_INFER_CHARGE_EFFICIENCY,
        infer_clamp: bool = DEFAULT_INFER_CLAMP,
        infer_backfill: bool = DEFAULT_INFER_BACKFILL,
        infer_backfill_max_hours: float = MAX_INFER_BACKFILL_HOURS,
        infer_state_file: str | None = None,
    ) -> None:
        self.scada_url = scada_url.rstrip("/") + "/"
        self.dispatchis_url = dispatchis_url.rstrip("/") + "/"
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.retries = retries
        self.retry_budget = retry_budget
        # The set of known storage DUIDs; seeded at startup so a restart does
        # not have to wait out the daily report, then refreshed from the daily
        # scraper each cycle.
        self._battery_duids = set(battery_duids or ())
        self._storage_duids_fn = storage_duids_fn
        # Inference: anchor per-unit ENERGY_STORAGE from the daily report and
        # integrate measured SCADA output between anchors.
        self._daily_report_fn = daily_report_fn
        self.charge_efficiency = charge_efficiency
        self.infer_clamp = infer_clamp
        self.infer_backfill = infer_backfill
        self.infer_backfill_max_hours = infer_backfill_max_hours
        self.infer_state_file = infer_state_file
        self._anchor_file: str | None = None
        # The daily report's newest interval, kept apart from the per-unit ts
        # (which advances as SCADA is integrated) so the anchor's own age stays
        # reportable: that is the number that exposes a stale anchor.
        self._anchor_ts: float | None = None
        # Whether the current anchor's gap has already been handed to the
        # archive. None = not tried, a ts = reconstructed up to there, -1.0 =
        # tried and unusable (do not re-download the archive every cycle).
        self._backfilled_end: float | None = None
        self._inferred: dict[str, dict] = {}
        if infer_state_file:
            self._load_infer_state()
        self.now_fn = time.time
        self._lock = threading.Lock()
        self._scada_state: dict | None = None
        self._is_state: dict | None = None
        self._scada_health = (False, "not polled yet")
        self._is_health = (False, "not polled yet")
        self._last_poll: float | None = None
        self._last_duration = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _learned_batteries(self) -> frozenset[str]:
        if self._storage_duids_fn is not None:
            known = self._storage_duids_fn()
            if known:
                self._battery_duids = set(known)
        return frozenset(self._battery_duids)

    # -- poll --------------------------------------------------------------- #

    def poll_once(self) -> tuple[str, bool]:
        started = time.monotonic()
        scada_ok = region_ok = False
        scada_state: dict = {}
        is_state: dict = {}
        try:
            listing = http_get(
                self.scada_url, self.timeout, self.retries, self.retry_budget
            )
            newest = newest_scada_file(listing.decode("utf-8", "replace"))
            scada_state = self._resolve_scada(newest)
            scada_ok = True
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("Dispatch_SCADA poll failed: %s", exc)
        try:
            listing = http_get(
                self.dispatchis_url, self.timeout, self.retries, self.retry_budget
            )
            newest = newest_dispatchis_file(listing.decode("utf-8", "replace"))
            is_state = self._resolve_is(newest)
            region_ok = True
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("DispatchIS poll failed: %s", exc)

        if scada_ok:
            batteries = self._learned_batteries()
            units = scada_state.get("units") or {}
            if batteries:
                scada_state["units"] = {
                    duid: u for duid, u in units.items() if duid in batteries
                }
            else:
                # No batteries known yet (first cycle after a cold start): keep
                # the parse but publish an empty per-unit set rather than every
                # scheduled unit in the NEM.
                scada_state["units"] = {}

        finished = self.now_fn()
        duration = time.monotonic() - started
        if scada_ok:
            self._advance_inference(scada_state.get("units") or {}, finished)
            if self.infer_state_file:
                self._save_infer_state()
        infer = self._infer_view(finished)
        with self._lock:
            self._scada_state = scada_state
            self._is_state = is_state
            self._last_poll = finished
            self._last_duration = duration
            self._scada_health = (scada_ok, "" if scada_ok else "Dispatch_SCADA poll failed")
            self._is_health = (region_ok, "" if region_ok else "DispatchIS poll failed")
        # The inferred side is only "up" when it is actually serving units: an
        # anchor that exists but whose gap could not be reconstructed serves
        # nothing, and reporting success there is how a fleet-wide stale anchor
        # stayed invisible. Success now tracks the served series, not the file.
        infer_ok = (
            scada_ok
            and self._anchor_file is not None
            and bool(infer.get("units"))
        )
        return (
            render_intraday(
                scada_state, scada_ok, is_state, region_ok, duration, finished,
                infer, infer_ok,
            ),
            scada_ok and region_ok,
        )

    def _resolve_scada(self, newest: str) -> dict:
        held = self._scada_state
        if held is not None and held.get("file") == newest:
            report = dict(held)
            report["reused"] = True
            return report
        url = urllib.parse.urljoin(self.scada_url, newest)
        payload = http_get(url, self.timeout, self.retries, self.retry_budget)
        report = parse_dispatch_scada(payload)
        report["file"] = newest
        report["reused"] = False
        return report

    def _resolve_is(self, newest: str) -> dict:
        held = self._is_state
        if held is not None and held.get("file") == newest:
            report = dict(held)
            report["reused"] = True
            return report
        url = urllib.parse.urljoin(self.dispatchis_url, newest)
        payload = http_get(url, self.timeout, self.retries, self.retry_budget)
        report = parse_dispatchis_region_storage(payload)
        report["file"] = newest
        report["reused"] = False
        return report

    # -- inference ---------------------------------------------------------- #
    #
    # AEMO publishes no per-unit stored MWh live, so each unit starts from the
    # daily report's per-unit ENERGY_STORAGE and its measured Dispatch_SCADA
    # output is integrated between intervals. A new daily file re-anchors
    # everything. The invariant that keeps the estimate honest: an entry's `ts`
    # is always the timestamp its value actually belongs to. An anchor keeps the
    # daily interval's own timestamp - a value from 04:00 is never re-stamped as
    # if it were measured at 13:00. When a live SCADA step is longer than
    # MAX_INFER_LIVE_STEP_SECONDS, the hole is rebuilt from the archived
    # Dispatch_SCADA files
    # (the backfill); when the archive cannot cover it, the unit is dropped and
    # stays absent until a fresh daily report re-anchors it, rather than
    # extrapolating a stale anchor. A gap longer than MAX_INFER_GAP_HOURS that
    # survives all that is dropped the same way.

    def _current_daily(self) -> dict:
        if self._daily_report_fn is None:
            return {}
        try:
            report = self._daily_report_fn()
        except Exception:  # noqa: BLE001 - inference must not kill a poll cycle
            log.exception("daily report snapshot failed; skipping re-anchor")
            return {}
        return report if isinstance(report, dict) else {}

    def _reanchor_if_needed(self, daily: dict) -> bool:
        file_name = daily.get("file")
        units = daily.get("units")
        if not isinstance(file_name, str) or not isinstance(units, dict):
            return False
        if file_name and file_name == self._anchor_file:
            return False
        anchored: dict[str, dict] = {}
        for duid, u in units.items():
            if not isinstance(duid, str) or not isinstance(u, dict):
                continue
            energy = u.get("energy")
            ts = u.get("ts")
            if not isinstance(energy, (int, float)) or not isinstance(ts, (int, float)):
                continue
            if ts <= 0:
                continue
            mwh, clamped = self._clamp_inferred(float(energy))
            anchored[duid] = {"ts": ts, "mwh": mwh, "clamped": clamped}
        if not anchored:
            return False
        self._inferred = anchored
        self._anchor_file = file_name
        self._anchor_ts = max((u["ts"] for u in anchored.values()), default=None)
        self._backfilled_end = None
        log.info("inference re-anchored to %s (%d units)", file_name, len(anchored))
        return True

    def _prune_stale(self, now: float) -> None:
        limit = now - MAX_INFER_GAP_HOURS * 3600.0
        self._inferred = {d: u for d, u in self._inferred.items() if u["ts"] > limit}

    def _clamp_inferred(self, raw: float) -> tuple[float, float]:
        """Floor the inferred stored MWh at zero; returns (mwh, clamped_flag).

        Stored energy cannot go negative, so a raw integral below zero is pinned
        at empty. The flag reports the raw integral going negative whether or
        not clamping is actually applied, so a battery held at empty is
        distinguishable from one genuinely empty - the OE exporter's
        `saturated` convention, here without a capacity ceiling because this
        exporter has no capacity in scope.
        """
        clamped = 1.0 if raw < 0.0 else 0.0
        mwh = max(0.0, raw) if self.infer_clamp else raw
        return mwh, clamped

    def _advance_entry(self, entry: dict, ts: float, power: float) -> dict:
        """Integrate one measured 5-minute power step into the inferred entry.

        Shared by the live advance and the archive backfill. A step that does
        not move the clock returns the entry unchanged, so re-sent or out-of-
        order archive rows never double-count.
        """
        delta_hours = (ts - entry["ts"]) / 3600.0
        if delta_hours <= 0:
            return entry
        if power > 0:
            delta = -power * delta_hours
        else:
            delta = -power * delta_hours * self.charge_efficiency
        mwh, clamped = self._clamp_inferred(entry["mwh"] + delta)
        return {"ts": ts, "mwh": mwh, "clamped": clamped}

    def _backfill_if_stale(self, end_ts: float | None, now: float) -> None:
        """Reconstruct any anchor->live gap from the SCADA archive, once.

        `end_ts` is the newest measured interval this cycle. A step within
        MAX_INFER_LIVE_STEP_SECONDS is normal poll-to-poll integration and
        needs no archive; anything longer needs the gap rebuilt one 5-minute
        interval at a time. The reconstruction runs at most once per anchor -
        re-downloading the archive every 5 minutes for the same stale anchor
        would be both spammy and pointless, and a failed reconstruction leaves
        the series absent (honest) until the next daily report.
        """
        if end_ts is None or not self._inferred:
            return
        if self._backfilled_end is not None:
            return
        trunk = min((u["ts"] for u in self._inferred.values()))
        if end_ts - trunk <= MAX_INFER_LIVE_STEP_SECONDS:
            self._backfilled_end = end_ts
            return
        if not self.infer_backfill:
            self._backfilled_end = -1.0
            log.info(
                "inference: anchor %s is %.1fh behind SCADA and backfill is "
                "disabled; the inferred series stays absent until a fresh "
                "daily report",
                self._anchor_file, (end_ts - trunk) / 3600.0,
            )
            return
        advanced, complete = self._backfill(end_ts)
        self._backfilled_end = end_ts if complete else -1.0

    def _backfill(self, end_ts: float) -> tuple[int, bool]:
        """Integrate the archived Dispatch_SCADA files across the anchor gap.

        Returns (units_advanced, complete). `complete` False means the archive
        could not be read or does not reach back to the anchor; the caller then
        leaves units at their anchor timestamps and the age gate drops them -
        a stale anchor is never stamped forward, and a partially covered hole
        is never extrapolated from the anchor.
        """
        anchor_ts = min((u["ts"] for u in self._inferred.values()), default=None)
        if anchor_ts is None:
            return 0, False
        span = end_ts - anchor_ts
        if span <= 0:
            return 0, True
        if span > self.infer_backfill_max_hours * 3600.0:
            log.info(
                "inference: anchor %s too old to reconstruct (%.1fh); staying absent",
                self._anchor_file, span / 3600.0,
            )
            return 0, False
        try:
            listing = http_get(
                self.scada_url, self.timeout, self.retries, self.retry_budget
            )
            names = scada_files_in_range(listing.decode("utf-8", "replace"), anchor_ts, end_ts)
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.warning("inference backfill: could not read %s: %s", self.scada_url, exc)
            return 0, False
        if not names:
            log.warning("inference backfill: no Dispatch_SCADA files cover %.1fh-%s",
                        anchor_ts, end_ts)
            return 0, False
        oldest_ts = scada_file_timestamp(names[0])
        if oldest_ts is not None and oldest_ts > anchor_ts + 2 * 300.0:
            log.warning(
                "inference backfill: archive only reaches %.1fh ago, cannot bridge "
                "the %.1fh gap; series stays absent",
                (end_ts - oldest_ts) / 3600.0, span / 3600.0,
            )
            return 0, False
        advanced = 0
        failures = 0
        for name in names:
            try:
                payload = http_get(
                    urllib.parse.urljoin(self.scada_url, name),
                    self.timeout, self.retries, self.retry_budget,
                )
                report = parse_dispatch_scada(payload)
            except (ScrapeError, OSError, ValueError) as exc:
                failures += 1
                log.debug("inference backfill: %s failed: %s", name, exc)
                if failures >= 3:
                    break
                continue
            for duid, u in report["units"].items():
                entry = self._inferred.get(duid)
                if entry is None:
                    continue
                stepped = self._advance_entry(entry, u["ts"], u["scada"])
                if stepped is not entry:
                    self._inferred[duid] = stepped
                    advanced += 1
        log.info(
            "inference: backfilled the %.1fh anchor gap from %d archive files "
            "(%d unit steps)",
            span / 3600.0, len(names), advanced,
        )
        return advanced, True

    def _advance_inference(self, scada_units: dict, now: float) -> None:
        """Integrate one 5-minute SCADA snapshot into the inferred state."""
        daily = self._current_daily()
        self._reanchor_if_needed(daily)
        daily_units = daily.get("units") or {}
        # First sightings are anchored at the daily interval's *own* timestamp,
        # never stamped forward: a value from 04:00 must not claim to be current
        # at 13:00. The archive backfill below is what actually carries a stale
        # anchor up to the live interval; a unit it cannot carry stays at its
        # anchor ts and is dropped by the age gate.
        end_ts: float | None = None
        for duid, u in scada_units.items():
            if not isinstance(duid, str) or not isinstance(u, dict):
                continue
            try:
                ts = float(u["ts"])
            except (TypeError, ValueError, KeyError):
                continue
            if end_ts is None or ts > end_ts:
                end_ts = ts
            if duid in self._inferred:
                continue
            entry = daily_units.get(duid)
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("energy"), (int, float))
                and isinstance(entry.get("ts"), (int, float))
                and float(entry["ts"]) > 0
            ):
                mwh, clamped = self._clamp_inferred(float(entry["energy"]))
                self._inferred[duid] = {
                    "ts": float(entry["ts"]), "mwh": mwh, "clamped": clamped,
                }
        self._backfill_if_stale(end_ts, now)
        for duid, u in scada_units.items():
            if not isinstance(duid, str) or not isinstance(u, dict):
                continue
            try:
                ts = float(u["ts"])
                power = float(u["scada"])
            except (TypeError, ValueError, KeyError):
                continue
            current = self._inferred.get(duid)
            if current is None:
                continue
            delta_hours = (ts - current["ts"]) / 3600.0
            if delta_hours <= 0:
                continue
            delta_seconds = delta_hours * 3600.0
            if delta_hours > MAX_INFER_GAP_HOURS:
                self._inferred.pop(duid, None)
                continue
            if (
                delta_seconds > MAX_INFER_LIVE_STEP_SECONDS
                and (
                    self._backfilled_end == -1.0
                    or (self._anchor_ts is not None and current["ts"] <= self._anchor_ts)
                )
            ):
                # The anchor gap could not be reconstructed for this unit (archive
                # too shallow, backfill disabled, or the archive never carried the
                # unit): integrating it in one step would stamp a stale anchor
                # forward, so the unit is dropped instead.
                self._inferred.pop(duid, None)
                continue
            self._inferred[duid] = self._advance_entry(current, ts, power)
        self._prune_stale(now)

    def _infer_view(self, now: float) -> dict:
        """Snapshot for rendering: live units only, plus the anchor's age."""
        limit = now - MAX_INFER_GAP_HOURS * 3600.0
        with self._lock:
            anchor = self._anchor_file
            inferred = {
                d: {"ts": u["ts"], "mwh": u["mwh"], "clamped": u.get("clamped", 0.0)}
                for d, u in self._inferred.items()
                if u["ts"] > limit
            }
        return {
            "file": anchor or "",
            "anchor_ts": self._anchor_ts,
            "units": inferred,
            "efficiency": self.charge_efficiency,
        }

    def _load_infer_state(self) -> None:
        """Restore the integrated state across a restart (a restart cache only)."""
        try:
            with open(self.infer_state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable infer state %s: %s", self.infer_state_file, exc)
            return
        anchor = payload.get("anchor_file")
        units = payload.get("units")
        if not isinstance(anchor, str) or not isinstance(units, dict):
            return
        clean: dict[str, dict] = {}
        for duid, entry in units.items():
            if not isinstance(duid, str) or not isinstance(entry, dict):
                continue
            try:
                ts = float(entry["ts"])
                mwh = float(entry["mwh"])
            except (TypeError, ValueError, KeyError):
                continue
            if ts > 0:
                mwh, clamped = self._clamp_inferred(mwh)
                clean[duid] = {"ts": ts, "mwh": mwh, "clamped": clamped}
        if not clean:
            return
        self._anchor_file = anchor
        anchor_ts = payload.get("anchor_ts")
        self._anchor_ts = float(anchor_ts) if isinstance(anchor_ts, (int, float)) else None
        self._inferred = clean

    def _save_infer_state(self) -> None:
        """Atomic whole-file write of the integrated state (restarts only)."""
        try:
            makedirs = os.makedirs
            makedirs(os.path.dirname(self.infer_state_file) or ".", exist_ok=True)
            tmp = self.infer_state_file + ".tmp"
            payload = {
                "anchor_file": self._anchor_file,
                "anchor_ts": self._anchor_ts,
                "saved_at": self.now_fn(),
                "units": {
                    d: {"ts": u["ts"], "mwh": u["mwh"], "clamped": u.get("clamped", 0.0)}
                    for d, u in self._inferred.items()
                },
            }
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, sort_keys=True)
            os.replace(tmp, self.infer_state_file)
        except OSError as exc:
            log.warning("could not write infer state %s: %s", self.infer_state_file, exc)

    # -- serving ------------------------------------------------------------ #

    def exposition(self) -> tuple[str, bool]:
        now = self.now_fn()
        with self._lock:
            scada_state = self._scada_state
            is_state = self._is_state
            scada_health = self._scada_health
            is_health = self._is_health
            duration = self._last_duration
        body = render_intraday(
            scada_state or {}, scada_health[0],
            is_state or {}, is_health[0],
            duration, now,
            self._infer_view(now), scada_health[0] and self._anchor_file is not None,
        )
        return body, scada_health[0] and is_health[0]

    def health(self) -> tuple[bool, str]:
        with self._lock:
            sa, sb = self._scada_health
            ia, ib = self._is_health
        return sa and ia, "" if (sa and ia) else "%s; %s" % (
            "" if sa else sb, "" if ia else ib,
        )

    # -- poll loop ---------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.exception("intraday poll cycle raised; continuing")
            interval = self.poll_interval * (0.95 + 0.1 * random.random())
            self._stop.wait(interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="aemointraday", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


INDEX_BODY = """<!doctype html>
<title>AEMO-reported battery energy storage</title>
<h1>AEMO-reported battery energy storage</h1>
<p>Two feeds from AEMO NEMWEB, both free and anonymous. The daily
<b>Next_Day_Dispatch</b> UNIT_SOLUTION ENERGY_STORAGE gives measured stored MWh
per unit but is published once a day (~04:11 AEST) and is the day's dispatch
target, not a live view. The intraday pair - <b>Dispatch_SCADA</b> (measured
output MW per unit) and <b>DispatchIS_Reports</b> (region-level BDU stored MWh) -
updates every 5-minute interval and is what moves the chart through the day.</p>
<ul>
  <li><a href="/metrics">/metrics</a></li>
  <li><a href="/healthz">/healthz</a></li>
</ul>
"""


def make_handler(scraper: NextDayScraper, intraday: IntradayPoller | None = None):
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
                # /healthz keeps the daily poll's health (its TCP probe is what the
                # container healthcheck uses); the intraday pair reports its own
                # health per feed via aemo_battery_scada_success and
                # aemo_battery_dispatchis_success so a partial outage stays visible
                # as a gap on the panel rather than hiding behind a binary health.
                body, _ = scraper.exposition()
                if intraday is not None:
                    i_body, _ = intraday.exposition()
                    body += i_body
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
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds between poll cycles (default %d). The archive publishes "
        "once a day, so this only sets how quickly a new file is noticed."
        % int(DEFAULT_POLL_INTERVAL),
    )
    parser.add_argument(
        "--scada-data-url",
        default=SCADA_DATA_URL,
        help="Dispatch_SCADA archive directory (default %s)." % SCADA_DATA_URL,
    )
    parser.add_argument(
        "--dispatchis-data-url",
        default=DISPATCHIS_DATA_URL,
        help="DispatchIS_Reports archive directory (default %s)."
        % DISPATCHIS_DATA_URL,
    )
    parser.add_argument(
        "--intraday-poll-interval",
        type=float,
        default=DEFAULT_INTRADAY_POLL_INTERVAL,
        help="seconds between intraday poll cycles (default %d). The Dispatch_"
        "SCADA and DispatchIS_Reports archives publish a new small file every "
        "5-minute interval." % int(DEFAULT_INTRADAY_POLL_INTERVAL),
    )
    parser.add_argument(
        "--state-file",
        help="JSON file persisting the last processed report, so a restart does "
        "not re-download the ~126 MB of an unchanged file. Measurements are "
        "re-verified against the live listing before they are ever served.",
    )
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9112)
    parser.add_argument(
        "--infer-charge-efficiency",
        type=float,
        default=DEFAULT_INFER_CHARGE_EFFICIENCY,
        help="share of charging energy counted as stored when integrating SCADA "
        "into the inferred stored MWh (default %g, matching the coefficient the "
        "OE exporter is deployed with; NEMPulse applies ~0.85 round-trip, here "
        "on the charging leg)." % DEFAULT_INFER_CHARGE_EFFICIENCY,
    )
    parser.add_argument(
        "--infer-clamp",
        action="store_true",
        default=DEFAULT_INFER_CLAMP,
        help="floor the inferred stored MWh at zero (default true)",
    )
    parser.add_argument(
        "--no-infer-clamp",
        action="store_false",
        dest="infer_clamp",
        help="serve the raw SCADA-integrated drift below zero instead of "
        "floored; the clamped flag keeps reporting it either way",
    )
    parser.add_argument(
        "--infer-backfill",
        action="store_true",
        default=DEFAULT_INFER_BACKFILL,
        help="rebuild an anchor-to-live SCADA gap from the archived "
        "Dispatch_SCADA files instead of serving nothing (default true)",
    )
    parser.add_argument(
        "--no-infer-backfill",
        action="store_false",
        dest="infer_backfill",
        help="never fetch archive files for reconstruction; a stale anchor "
        "simply leaves the inferred series absent until the next daily report",
    )
    parser.add_argument(
        "--infer-backfill-max-hours",
        type=float,
        default=MAX_INFER_BACKFILL_HOURS,
        help="the oldest anchor gap that may be reconstructed from the archive "
        "(default %g; a daily anchor is bounded by the daily cadence anyway)"
        % MAX_INFER_BACKFILL_HOURS,
    )
    parser.add_argument(
        "--infer-state-file",
        help="JSON file persisting the integrated per-unit stored MWh, so a "
        "restart does not re-anchor the day's drift to the daily report.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds to allow each upstream request (default %d; the zip "
        "download is 8 MB, hence roomier than the sibling exporters' 20)"
        % int(DEFAULT_TIMEOUT),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="retries for transient upstream failures (DNS EAI_AGAIN, resets, timeouts)",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=DEFAULT_RETRY_BUDGET,
        help="seconds a single request may spend retrying before giving up "
        "(default %d)" % int(DEFAULT_RETRY_BUDGET),
    )
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

    scraper = NextDayScraper(
        args.data_url,
        args.timeout,
        args.poll_interval,
        args.retries,
        args.retry_budget,
        args.state_file,
    )

    # Seed the intraday poller with the battery set from the daily state file so
    # a restart filters SCADA rows correctly before the first daily poll finishes.
    seed_duids: set = set()
    if args.state_file:
        persisted = load_state(args.state_file)
        if persisted and persisted.get("units"):
            seed_duids = set(persisted["units"])
    intraday = IntradayPoller(
        args.scada_data_url,
        args.dispatchis_data_url,
        args.timeout,
        args.intraday_poll_interval,
        args.retries,
        args.retry_budget,
        battery_duids=seed_duids,
        storage_duids_fn=scraper.storage_duids,
        daily_report_fn=scraper.daily_report,
        charge_efficiency=args.infer_charge_efficiency,
        infer_clamp=args.infer_clamp,
        infer_backfill=args.infer_backfill,
        infer_backfill_max_hours=args.infer_backfill_max_hours,
        infer_state_file=args.infer_state_file,
    )

    if args.once:
        daily_body, daily_ok = scraper.poll_once()
        i_body, i_ok = intraday.poll_once()
        sys.stdout.write(daily_body + i_body)
        return 0 if (daily_ok and i_ok) else 1

    if not args.no_background_poll:
        scraper.start()
        intraday.start()

    server = ThreadingHTTPServer(
        (args.listen_address, args.port), make_handler(scraper, intraday)
    )
    log.info(
        "serving http://%s:%d/metrics, polling %s every %ds and intraday (%s, "
        "%s) every %ds",
        args.listen_address,
        args.port,
        args.data_url,
        int(args.poll_interval),
        args.scada_data_url,
        args.dispatchis_data_url,
        int(args.intraday_poll_interval),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        scraper.stop()
        intraday.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())