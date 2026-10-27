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
  same table's `SS_*_UIGF` / `SS_*_CLEAREDMW` columns carry, per region per
  variable renewable fuel, the self-forecast and the dispatch-cleared MW, whose
  gap is that region's curtailment. The region-level stored MWh is the only
  AEMO figure that is both live and "measured" rather than dispatched.

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
    aemo_battery_region_curtailment_mw{region,fuel}   SS_*_UIGF minus
                                               SS_*_CLEAREDMW (MW, floored at 0;
                                               fuel = wind or solar)
    aemo_battery_region_fossil_generation_mw{region}  summed measured MW of the
                                               region's coal/gas/distillate units
    aemo_battery_fossil_units                  fossil units with a live reading
    aemo_interconnector_flow_mw{interconnector}  dispatched MW flow (MWFLOW,
                                               AEMO's own per-link sign convention)
    aemo_interconnector_metered_flow_mw{interconnector}  metered MW flow
    aemo_interconnector_export_limit_mw{interconnector}  EXPORTLIMIT, verbatim
    aemo_interconnector_import_limit_mw{interconnector}  IMPORTLIMIT, verbatim
    aemo_interconnector_nominal_forward_mw{interconnector}  static nominal MW,
                                               FROM->TO direction (positive flow)
    aemo_interconnector_nominal_reverse_mw{interconnector}  static nominal MW,
                                               TO->FROM direction (negative flow)
    aemo_interconnector_losses_mw{interconnector}  MWLOSSES
    aemo_interconnector_marginal_value{interconnector}  MARGINALVALUE ($/MWh)
    aemo_interconnector_marginal_loss_factor{interconnector}  MARGINALLOSS
    aemo_interconnector_violation_degree{interconnector}  VIOLATIONDEGREE
    aemo_interconnector_timestamp_seconds{interconnector}  interval SETTLEMENTDATE
    aemo_interconnector_age_seconds{interconnector}  its age at export time
    aemo_interconnector_report_info{file}        the DispatchIS file processed
    aemo_interconnector_count                   interconnectors with a reading
    aemo_battery_region_import_mw{region}       gross MW into the region, summed
                                               over its interconnectors
                                               (derived from MWFLOW)
    aemo_battery_region_export_mw{region}       gross MW out of the region,
                                               summed over its interconnectors
                                               (derived from MWFLOW)

The two derived region series both come from a DUID-level measurement paired
with a map, because AEMO publishes neither directly at region granularity:
curtailment is the DispatchIS REGIONSUM self-forecast minus the cleared MW per
fuel, and fossil generation sums each measured Dispatch_SCADA unit that the OE
fleet cache (`--fleet-cache-file`) tags as coal, gas or distillate. Absent
cache, no fossil series; a DispatchIS file predating the SS_* columns yields
no curtailment.

Interconnector flow and transfer limits are a third item riding the same
DispatchIS download: the DISPATCH,INTERCONNECTORRES table reports, per link
and interval, the settled flow (MWFLOW), the metered flow (METEREDMWFLOW) and
the dispatch engine's EXPORTLIMIT/IMPORTLIMIT, alongside losses, marginal
value and violation degree. AEMO settles flow under its own per-interconnector
sign convention and reports limits that are negative for some links
(T-V-MNSP1 reports both limits negative), so the columns are carried verbatim,
never re-derived or normalized. A parse failure of that one table leaves the
interconnector series absent while the region storage survives - the series is
gated on its own table, and `aemo_interconnector_count` makes the absence
visible.

The same table also yields the per-region gross import/export series: AEMO
publishes no gross regional figure (REGIONSUM carries only NETINTERCHANGE), so
each link's MWFLOW is attributed to both its ends via the link registry - a
positive flow is an export for its FROM region and an import for its TO region,
and vice versa. Interconnector losses are not modelled. A link the registry
does not know contributes to neither end; when no link reports a flow, neither
series is emitted.

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
    aemo_battery_inferred_clamped{duid}          1 = raw integral left the
                                                 [0, capacity] envelope
    aemo_battery_inferred_timestamp_seconds{duid}  the integrated interval
    aemo_battery_inferred_age_seconds{duid}      its age at export time
    aemo_battery_inferred_units                  inferred units being served
    aemo_battery_inferred_clamped_units          ... of them pinned at a boundary
    aemo_battery_inferred_fleet_capacity_units   ... of them with a known registered
                                                 capacity (feeds the top clamp)
    aemo_battery_infer_success                   1/0 (series is actually served)
    aemo_battery_inferred_anchor_age_seconds     age of the daily anchor interval;
                                                 large here = stale anchor
    aemo_battery_infer_charge_efficiency         the stored share of charging energy
    aemo_battery_inferred_report_info{file}      the daily report anchoring it

The clamped flag is the OE exporter's `--saturated` convention: it reports the
raw integral leaving the envelope, so a battery held at empty or full is
distinguishable from one genuinely there. `--no-infer-clamp` serves the raw
drift instead, for inspecting the estimate rather than reading it. The bottom
boundary (empty) needs no data; the top boundary needs the unit's registered
storage capacity, which is OpenElectricity data, so `--fleet-cache-file` points
the exporter at the OE fleet cache (`data[].units[].code` ->
`capacity_storage`). A unit with no known capacity - not in the OE fleet, or no
cache file - is floored at zero only, the pre-capacity behaviour.

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
import dataclasses
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from exposition import escape_label, fmt
# The one retrying client (docs/adr/0008), bound by name the way exposition's
# two helpers are. All four of its names are bound here, and test_http_client
# pins each as http_client's definition by identity, so a private copy of the
# retry loop cannot creep back in unnoticed.
from http_client import ScrapeError, describe_http_error, http_get, is_transient
import fleet
import inference
import links
import soc_policy

DEFAULT_DATA_URL = "https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/"
SCADA_DATA_URL = "https://nemweb.com.au/Reports/Current/Dispatch_SCADA/"
DISPATCHIS_DATA_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
ROOFTOP_PV_DATA_URL = "https://nemweb.com.au/Reports/CURRENT/ROOFTOP_PV/ACTUAL/"

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
# the exact convention the retired OE exporter used for its
# --infer-charge-efficiency. The value is the fitted one the SoC policy owns
# (soc_policy.CHARGE_EFFICIENCY, where the fit itself is recorded); taking it
# by identity is what keeps the dashboard's "fitted ~0.91" descriptions and the
# deployed model the same fact. See docs/adr/0005.
DEFAULT_INFER_CHARGE_EFFICIENCY = soc_policy.CHARGE_EFFICIENCY

# A unit that stops appearing in Dispatch_SCADA is unscheduled (idle), so a short
# absence is held frozen at its last value - but beyond this long a frozen number
# is a guess (a retag, a maintenance cycle, a feed regression), and the inferred
# line is dropped until the next daily report re-anchors it. The number is the
# SoC policy's to state (soc_policy.MAX_GAP_HOURS) - the dashboard descriptions
# quote it from there - and this name is the exporter's view of it.
MAX_INFER_GAP_HOURS = soc_policy.MAX_GAP_HOURS

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
# at zero by default. The top end needs the unit's registered storage capacity -
# OpenElectricity data - so it only clamps when --fleet-cache-file supplies it;
# without one this exporter can only floor. --no-infer-clamp serves the raw
# drift instead. Either way `aemo_battery_inferred_clamped` reports a raw
# integral that left the [0, capacity] envelope, so a battery pinned at empty
# or full is never mistaken for one genuinely there - the OE exporter's
# reported-rather-than-hidden convention.
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

# OpenElectricity is the fuel/region source for every per-region attribution in
# this exporter: AEMO publishes no per-region fuel breakdown (Dispatch_SCADA
# carries measured MW but only a DUID), so which units are fossil generation
# comes from the fleet module's `is_fossil` predicate rather than a local set.
# See fleet.FOSSIL_CATEGORIES.

# The DispatchIS REGIONSUM columns carrying, per region, the self-forecast
# (`UIGF`) and dispatch-cleared (`CLEAREDMW`) MW for each variable renewable
# fuel. The gap between them is that fuel's curtailment in the region: capacity
# available in the forecast that the dispatch engine did not clear. Verified
# present in the real 2026-10-08 file at SS_SOLAR_UIGF 109, SS_WIND_UIGF 110,
# SS_SOLAR_CLEAREDMW 111, SS_WIND_CLEAREDMW 112 (of a 130-column row).
CURTAILMENT_COLUMNS = (
    ("SS_WIND_UIGF", "SS_WIND_CLEAREDMW", "wind"),
    ("SS_SOLAR_UIGF", "SS_SOLAR_CLEAREDMW", "solar"),
)

# The DispatchIS INTERCONNECTORRES columns backing the interconnector series.
# Only the identifying columns are required; every numeric field is read when
# its column is present, so a file predating one of them still parses.
INTERCONNECTOR_REQUIRED = (
    "SETTLEMENTDATE",
    "INTERCONNECTORID",
)

INTERCONNECTOR_OPTIONAL = (
    "METEREDMWFLOW",
    "MWFLOW",
    "MWLOSSES",
    "MARGINALVALUE",
    "VIOLATIONDEGREE",
    "EXPORTLIMIT",
    "IMPORTLIMIT",
    "MARGINALLOSS",
)

log = logging.getLogger("aemo_battery_exporter")


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


# Rooftop PV actual measurement files, one per 5-minute interval:
# PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_<YYYYMMDDHHMM>_<seq>.zip. Unlike the
# Dispatch_SCADA/DispatchIS pair these carry only region totals - AEMO's
# satellite-based estimate of distributed (rooftop) PV output, per NEM region -
# so the newest file is all the map needs. Same naming rules as the SCADA files:
# lexicographic max is chronological and picks up corrections.
ROOFTOP_PV_FILE_RE = re.compile(
    r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_\d{14}_\d+\.zip"
)
ROOFTOP_PV_FILE_NAME_RE = re.compile(
    r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{14})_\d+\.zip"
)


def newest_rooftop_pv_file(html: str) -> str:
    return _newest(
        html, ROOFTOP_PV_FILE_RE, "PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_*.zip"
    )


def rooftop_pv_file_timestamp(name: str) -> float | None:
    """`PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_<YYYYMMDDHHMM>_<seq>.zip` -> Unix.

    Same convention as scada_file_timestamp: AEMO names the file for the
    settlement interval (UTC+10), so the timestamp is recoverable without
    fetching it; a malformed stamp returns None.
    """
    match = ROOFTOP_PV_FILE_NAME_RE.match(name)
    if not match:
        return None
    try:
        naive = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return naive.replace(
        tzinfo=timezone(timedelta(seconds=AEST_UTC_OFFSET_SECONDS))
    ).timestamp()


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


def csv_row_reader(stream):
    """Rows from a compressed member stream.

    Kept as a thin wrapper so the test suite can feed plain in-memory bytes
    through the same path without reaching for a temp file.
    """
    return csv.reader(io.TextIOWrapper(stream, encoding="utf-8", errors="replace"))


# One reading as a feed's projection deposits it: which result map it belongs
# to, the interval stamp, the key within that map (DUID / region / link), and
# the entry to keep when it is the newest for that key.
PutReading = Callable[[str, float, str, dict], None]


def read_aemo_table(
    data: bytes,
    *,
    table: tuple[str, str],
    columns: Callable[[list[str]], dict[str, int] | None],
    results: tuple[str, ...],
    project: Callable[[list[str], dict[str, int], PutReading], None],
    missing: str,
    on_control: Callable[[list[str]], None] | None = None,
) -> dict:
    """Reduce one AEMO dispatch archive's `table` through `project`.

    The skeleton every feed shares, implemented once: open the zip, find its
    CSV member, stream the tagged rows, index the header's columns by name,
    keep the newest stamp - per key and for the file - and fail when the file
    yielded nothing at all.

    An `I` row naming `table` builds the name -> position index through
    `columns` (None = the table changed shape, so no data row parses); a `C`
    row goes to `on_control`; a `D` row of `table` goes to
    `project(row, indices, put)`, where `put(bucket, stamp, key, entry)` keeps
    the newest entry per key. `results` names the maps the return always
    carries - declared empty, filled as readings land. `missing` is the
    `%s`/`%d` ScrapeError format (filename, handled rows) raised when every
    map is still empty: a feed that read nothing publishes nothing.

    Returns {"file": str, "newest_interval": float|None, <results>: {...}}.

    The CSV is streamed one row at a time - the daily file is ~126 MB
    uncompressed, so it must never be materialised in memory.
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

    buckets: dict[str, dict] = {name: {} for name in results}
    newest: float | None = None
    indices: dict[str, int] | None = None
    handled_rows = 0

    def put(bucket: str, stamp: float, key: str, entry: dict) -> None:
        nonlocal newest
        readings = buckets.setdefault(bucket, {})
        prev = readings.get(key)
        if prev is None or stamp >= prev["ts"]:
            readings[key] = entry
        if newest is None or stamp > newest:
            newest = stamp

    with archive.open(members[0]) as member:
        for row in csv_row_reader(member):
            if not row:
                continue
            tag = row[0]
            if tag == "I":
                if len(row) >= 3 and (row[1], row[2]) == table:
                    indices = columns(row)
                continue
            if tag == "C":
                if on_control is not None:
                    on_control(row)
                continue
            if tag != "D" or indices is None:
                continue
            if len(row) < 3 or (row[1], row[2]) != table:
                continue
            if len(row) <= max(indices.values()):
                continue
            handled_rows += 1
            project(row, indices, put)

    if not any(buckets.values()):
        raise ScrapeError(missing % (filename, handled_rows))
    return {"file": filename, "newest_interval": newest, **buckets}


def _indices_by_name(
    header_row: list[str],
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> dict[str, int] | None:
    """name -> column position for `required` (+ `optional` when present).

    Indexed by *name*, never position, so a future column reorder upstream
    cannot silently read the wrong number. A missing `required` name means
    the file changed shape and yields None; a missing `optional` name is
    simply absent from the result, and the feed reads that series as None.
    """
    positions = {}
    for name in required:
        try:
            positions[name] = header_row.index(name)
        except ValueError:
            return None
    for name in optional:
        try:
            positions[name] = header_row.index(name)
        except ValueError:
            pass
    return positions


def _unit_solution_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a UNIT_SOLUTION header, by name.

    Returns None when the table header is missing the storage or dispatch
    columns, in which case the file has changed shape. The provenance for the
    column set is in the parse tests' docstring (verified positions in the
    real 2026-10-08 file); the lookup itself is name-based per the reader's
    contract.
    """
    return _indices_by_name(
        header_row,
        (
            "DUID",
            "SETTLEMENTDATE",
            "TOTALCLEARED",
            "INITIAL_ENERGY_STORAGE",
            "ENERGY_STORAGE",
        ),
    )


def _project_unit_solution(
    row: list[str], indices: dict[str, int], put: PutReading
) -> None:
    """One UNIT_SOLUTION row -> the storage reading it carries, or nothing.

    A non-empty INITIAL_ENERGY_STORAGE is what makes a unit a storage unit:
    generators never report one and are skipped here, before anything is
    deposited.
    """
    initial = row[indices["INITIAL_ENERGY_STORAGE"]]
    if not initial:
        return
    stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
    if stamp is None:
        return
    duid = row[indices["DUID"]]
    if not duid:
        return
    try:
        initial_value = float(initial)
        energy = row[indices["ENERGY_STORAGE"]]
        cleared = row[indices["TOTALCLEARED"]]
        energy_value = float(energy) if energy else initial_value
        cleared_value = float(cleared) if cleared else None
    except ValueError:
        log.warning("duid %s: non-numeric storage at %s", duid, stamp)
        return
    put(
        "units",
        stamp,
        duid,
        {
            "ts": stamp,
            "initial": initial_value,
            "energy": energy_value,
            "cleared": cleared_value,
        },
    )


def parse_next_day_dispatch(data: bytes) -> dict:
    """Reduce one Next_Day_Dispatch CSV into per-unit newest measurements.

    Returns {"generated": float|None, "newest_interval": float|None,
    "units": {duid: {"ts": float, "initial": float, "energy": float,
    "cleared": float|None}}}. The reader keeps only the newest interval per
    storage unit that reported a value; `generated` is the publication stamp
    from the archive's control line.
    """
    generated: float | None = None

    def read_control(row: list[str]) -> None:
        # The first control line is the publication stamp:
        # C,NEMP.WORLD,NEXT_DAY_DISPATCH,AEMO,PUBLIC,2026/10/08,04:10:00,...
        nonlocal generated
        if len(row) >= 7 and row[1] == "NEMP.WORLD":
            generated = parse_aest(row[5] + " " + row[6])

    report = read_aemo_table(
        data,
        table=("DISPATCH", "UNIT_SOLUTION"),
        columns=_unit_solution_indices,
        results=("units",),
        project=_project_unit_solution,
        missing="no storage units found in %s (handled %d rows)",
        on_control=read_control,
    )
    report["generated"] = generated
    return report


def _unit_scada_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a UNIT_SCADA header, by name.

    Verified against the real file on 2026-10-08: SETTLEMENTDATE, DUID,
    SCADAVALUE at indices 4, 5, 6 - positions recorded for provenance only;
    the lookup follows the names so a column reorder upstream cannot silently
    read the wrong number. Returns None when one is missing (shape change).
    """
    return _indices_by_name(
        header_row, ("SETTLEMENTDATE", "DUID", "SCADAVALUE")
    )


def _project_unit_scada(
    row: list[str], indices: dict[str, int], put: PutReading
) -> None:
    """One UNIT_SCADA row -> the measured output it carries, or nothing.

    Every row is deposited; the caller filters to the storage units it cares
    about. A re-sent row simply loses to the newer stamp inside the reader.
    """
    stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
    duid = row[indices["DUID"]]
    if stamp is None or not duid:
        return
    try:
        value = float(row[indices["SCADAVALUE"]])
    except ValueError:
        log.warning("duid %s: non-numeric SCADA at %s", duid, stamp)
        return
    put("units", stamp, duid, {"ts": stamp, "scada": value})


def parse_dispatch_scada(data: bytes) -> dict:
    """Reduce one Dispatch_SCADA CSV into per-unit measured output.

    Returns {"file": str, "newest_interval": float, "units": {duid: {"ts": float,
    "scada": float}}}. The file is a single 5-minute interval snapshot, so each
    DUID appears once; where a unit does appear twice (a re-sent row), the later
    stamp wins. Every row is kept here; the caller filters to the storage units
    it cares about.
    """
    return read_aemo_table(
        data,
        table=("DISPATCH", "UNIT_SCADA"),
        columns=_unit_scada_indices,
        results=("units",),
        project=_project_unit_scada,
        missing="no UNIT_SCADA rows in %s (handled %d)",
    )


def _rooftop_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a ROOFTOP/ACTUAL header, by name.

    INTERVAL_DATETIME is this table's stamp column, the role SETTLEMENTDATE
    plays in the dispatch tables. Returns None when one of the three is
    missing and the file has changed shape.
    """
    return _indices_by_name(
        header_row, ("INTERVAL_DATETIME", "REGIONID", "POWER")
    )


def _project_rooftop(
    row: list[str], indices: dict[str, int], put: PutReading
) -> None:
    """One ROOFTOP/ACTUAL row -> the region's MW at that interval, or nothing."""
    stamp = parse_aest(row[indices["INTERVAL_DATETIME"]])
    region = row[indices["REGIONID"]]
    if stamp is None or not region:
        return
    try:
        value = float(row[indices["POWER"]])
    except ValueError:
        log.warning("region %s: non-numeric rooftop PV at %s", region, stamp)
        return
    put("regions", stamp, region, {"ts": stamp, "mw": value})


def parse_rooftop_pv(data: bytes) -> dict:
    """Reduce one ROOFTOP_PV_ACTUAL_MEASUREMENT CSV into per-region MW.

    Returns {"file": str, "newest_interval": float, "regions": {region: {"ts":
    float, "mw": float}}}. The file is a single 5-minute interval snapshot of
    AEMO's satellite-based distributed-PV estimate, one POWER row per NEM region
    (NSW1, QLD1, SA1, TAS1, VIC1), so each region appears once; a re-sent row
    keeps its later stamp. Columns are indexed by the header names, so a column
    reorder upstream cannot silently read the wrong number.
    """
    return read_aemo_table(
        data,
        table=("ROOFTOP", "ACTUAL"),
        columns=_rooftop_indices,
        results=("regions",),
        project=_project_rooftop,
        missing="no ROOFTOP PV rows in %s (handled %d)",
    )


def _region_sum_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a DISPATCH,REGIONSUM header, by name.

    Verified against the real file on 2026-10-08: SETTLEMENTDATE at 4, REGIONID
    at 6, BDU_ENERGY_STORAGE at 124, BDU_INITIAL_ENERGY_STORAGE at 129. The BDU
    columns report the aggregate stored MWh of all battery dispatch units in a
    region and are empty for regions with none (TAS1). The table never appears
    for regions with no storage, and a region whose BDU columns are both empty
    is skipped.

    The SS_* curtailment columns are recorded when present but are not required:
    a REGIONSUM table without them still yields region storage, and any header a
    synthetic test builds may omit them. Returns None when a required column is
    missing (shape change).
    """
    return _indices_by_name(
        header_row,
        (
            "SETTLEMENTDATE",
            "REGIONID",
            "BDU_INITIAL_ENERGY_STORAGE",
            "BDU_ENERGY_STORAGE",
        ),
        (
            "SS_WIND_UIGF",
            "SS_WIND_CLEAREDMW",
            "SS_SOLAR_UIGF",
            "SS_SOLAR_CLEAREDMW",
        ),
    )


def _interconnector_indices(header_row: list[str]) -> dict[str, int] | None:
    """Column indices for a DISPATCH,INTERCONNECTORRES header, by name.

    Verified against the real 2026-10-11 file: SETTLEMENTDATE at 4,
    INTERCONNECTORID at 6, METEREDMWFLOW at 9, MWFLOW at 10, MWLOSSES at 11,
    MARGINALVALUE at 12, VIOLATIONDEGREE at 13, EXPORTLIMIT at 15, IMPORTLIMIT
    at 16, MARGINALLOSS at 17. The identification columns are required; any
    optional column that is absent simply yields None series. Returns None
    when a required column is missing (shape change).
    """
    return _indices_by_name(
        header_row, INTERCONNECTOR_REQUIRED, INTERCONNECTOR_OPTIONAL
    )


def _project_region_sum(
    row: list[str], indices: dict[str, int], put: PutReading
) -> None:
    """One DISPATCH,REGIONSUM row -> its storage and curtailment readings.

    The two maps are independent, so a region with renewables but no batteries
    still contributes curtailment and a region whose BDU columns are both blank
    is simply absent from the storage map.
    """
    stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
    region = row[indices["REGIONID"]]
    if stamp is None or not region:
        return
    initial_text = row[indices["BDU_INITIAL_ENERGY_STORAGE"]]
    energy_text = row[indices["BDU_ENERGY_STORAGE"]]
    if initial_text or energy_text:
        try:
            initial = float(initial_text) if initial_text else None
            energy = float(energy_text) if energy_text else initial
        except ValueError:
            log.warning("region %s: non-numeric BDU storage at %s", region, stamp)
        else:
            put(
                "regions",
                stamp,
                region,
                {"ts": stamp, "initial": initial, "energy": energy},
            )
    # Which (UIGF, CLEAREDMW, fuel) triples this header can supply; none when
    # the file predates the SS_* columns.
    curtail_fields = [
        (indices[uigf], indices[cleared], fuel)
        for uigf, cleared, fuel in CURTAILMENT_COLUMNS
        if uigf in indices and cleared in indices
    ]
    if not curtail_fields:
        return
    readings: dict[str, float] | None = {}
    for uigf_idx, cleared_idx, fuel in curtail_fields:
        uigf_text = row[uigf_idx]
        cleared_text = row[cleared_idx]
        if not uigf_text and not cleared_text:
            continue
        try:
            uigf = float(uigf_text) if uigf_text else 0.0
            cleared = float(cleared_text) if cleared_text else 0.0
        except ValueError:
            log.warning(
                "region %s: non-numeric %s curtailment at %s",
                region, fuel, stamp,
            )
            readings = None
            break
        # Floored at zero: the gap is legitimately negative when a unit
        # generates above its own self-forecast.
        readings[fuel] = max(0.0, uigf - cleared)
    if readings:
        put("curtailment", stamp, region, {"ts": stamp, **readings})


def parse_dispatchis_region_storage(data: bytes) -> dict:
    """Reduce one DispatchIS CSV into per-region storage and curtailment.

    Returns {"file": str, "newest_interval": float, "regions": {region: {"ts":
    float, "initial": float, "energy": float}}, "curtailment": {region: {"ts":
    float, "wind"?: float, "solar"?: float}}}. Storage keeps only regions that
    report a non-empty BDU_ENERGY_STORAGE or BDU_INITIAL_ENERGY_STORAGE (the
    stored figure falls back to the initial when blank); curtailment keeps any
    region whose SS_* columns are present, per fuel, as the self-forecast minus
    the cleared MW floored at zero. The two maps are independent, so a region
    with renewables but no batteries still contributes curtailment.
    """
    return read_aemo_table(
        data,
        table=("DISPATCH", "REGIONSUM"),
        columns=_region_sum_indices,
        results=("regions", "curtailment"),
        project=_project_region_sum,
        missing="no REGIONSUM rows in %s (handled %d)",
    )


def _cell_float(row: list[str], idx: int | None, what: str, id_: str) -> float | None:
    """One optional numeric cell: None when blank or absent, else the float."""
    if idx is None or idx >= len(row) or not row[idx]:
        return None
    try:
        return float(row[idx])
    except ValueError:
        log.warning("interconnector %s: non-numeric %s", id_, what)
        return None


def _project_interconnector(
    row: list[str], indices: dict[str, int], put: PutReading
) -> None:
    """One DISPATCH,INTERCONNECTORRES row -> its flow and limits, verbatim.

    Every optional column is carried as AEMO publishes it, never re-derived:
    limits can legitimately be negative (T-V-MNSP1 reports both negative), and
    a blank cell reads as None rather than 0.
    """
    stamp = parse_aest(row[indices["SETTLEMENTDATE"]])
    ic = row[indices["INTERCONNECTORID"]]
    if stamp is None or not ic:
        return
    entry = {
        "ts": stamp,
        "mwflow": _cell_float(row, indices.get("MWFLOW"), "MWFLOW", ic),
        "metered_mwflow": _cell_float(
            row, indices.get("METEREDMWFLOW"), "METEREDMWFLOW", ic
        ),
        "export_limit": _cell_float(
            row, indices.get("EXPORTLIMIT"), "EXPORTLIMIT", ic
        ),
        "import_limit": _cell_float(
            row, indices.get("IMPORTLIMIT"), "IMPORTLIMIT", ic
        ),
        "losses": _cell_float(row, indices.get("MWLOSSES"), "MWLOSSES", ic),
        "marginal_value": _cell_float(
            row, indices.get("MARGINALVALUE"), "MARGINALVALUE", ic
        ),
        "marginal_loss": _cell_float(
            row, indices.get("MARGINALLOSS"), "MARGINALLOSS", ic
        ),
        "violation_degree": _cell_float(
            row, indices.get("VIOLATIONDEGREE"), "VIOLATIONDEGREE", ic
        ),
    }
    put("interconnectors", stamp, ic, entry)


def parse_dispatchis_interconnectors(data: bytes) -> dict:
    """Reduce one DispatchIS CSV into per-interconnector flow and limits.

    Returns {"file": str, "newest_interval": float, "interconnectors": {id:
    {"ts": float, "mwflow": float|None, "metered_mwflow": float|None,
    "export_limit": float|None, "import_limit": float|None, "losses":
    float|None, "marginal_value": float|None, "marginal_loss": float|None,
    "violation_degree": float|None}}}. The DISPATCH,INTERCONNECTORRES table
    carries, per interval and interconnector, the dispatch engine's settled
    flow (MWFLOW) and its limit solution (EXPORTLIMIT/IMPORTLIMIT) alongside
    the metered flow, losses and marginal values. AEMO settles flow under its
    own per-interconnector sign convention and reports limits that can be
    negative for some links (T-V-MNSP1 reports both limits negative), so every
    column is carried verbatim, never re-derived. Where a row is re-sent the
    newest stamp wins.
    """
    return read_aemo_table(
        data,
        table=("DISPATCH", "INTERCONNECTORRES"),
        columns=_interconnector_indices,
        results=("interconnectors",),
        project=_project_interconnector,
        missing="no INTERCONNECTORRES rows in %s (handled %d)",
    )


def region_interchange(interconnectors: dict) -> dict:
    """Gross imports and exports per region, MW, derived from the per-link flows.

    AEMO publishes no gross import/export at region granularity: DispatchIS
    REGIONSUM carries only NETINTERCHANGE, a single net figure. The gross flows
    are attributed across each link's two ends instead. With AEMO's sign
    convention (positive MWFLOW leaves the link's from_region and enters its
    to_region) a positive flow is an export for FROM and an import for TO, and
    a negative flow is the reverse. Interconnector losses are not modelled, so
    a region's import is the flow value AEMO signs on the link - the same
    number the map draws on the line. Returns
    {region: {"import": float, "export": float}}, with a region present only
    when at least one of its links reported a flow. A link the link registry
    does not know, or whose MWFLOW is missing, contributes to neither end.
    """
    out: dict[str, dict] = {}
    for ic, entry in interconnectors.items():
        link = links.LINKS.get(ic)
        if link is None:
            continue
        flow = entry.get("mwflow")
        if not isinstance(flow, (int, float)) or isinstance(flow, bool):
            continue
        if flow == 0:
            continue
        frm, to = link.from_region, link.to_region
        a = out.setdefault(frm, {"import": 0.0, "export": 0.0})
        b = out.setdefault(to, {"import": 0.0, "export": 0.0})
        if flow > 0:
            a["export"] += flow
            b["import"] += flow
        else:
            a["import"] += -flow
            b["export"] += -flow
    return out


# --------------------------------------------------------------------------- #
# Prometheus exposition
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Intraday feed policy
# --------------------------------------------------------------------------- #
#
# Each block of intraday series is emitted by one function that owns its data
# gate (an empty map publishes nothing) and nothing else - never a feed flag.
# Which feed a block rides on lives in INTRADAY_BLOCKS below, so the failure
# policy is one table rather than flags threaded through the render.


def _emit_unit_power(src: dict) -> list[str]:
    """`aemo_unit_power_mw` and its per-unit timestamps/ages, for every
    dispatch unit in the SCADA file.

    The series carries every dispatch unit - generators, loads and storage
    alike - labelled with its fueltech and region from the OE fleet cache
    (`unit_meta`). It is the superset of the legacy `aemo_battery_power_mw`
    series, which stays filtered to storage units so existing dashboards keep
    their exact semantics while consumers migrate.
    """
    scada = src["scada"]
    now = src["now"]
    meta = src["meta"]
    all_units = scada.get("all_units") or {}
    if not all_units:
        return []
    lines: list[str] = [
        "# HELP aemo_unit_power_mw Measured output at the newest interval, "
        "MW, for every dispatch unit in Dispatch_SCADA "
        "(negative = charging/absorbing). Supersedes aemo_battery_power_mw.",
        "# TYPE aemo_unit_power_mw gauge",
    ]
    for duid in sorted(all_units):
        u = all_units[duid]
        m = meta.get(duid) or {}
        lines += [
            'aemo_unit_power_mw{unit="%s",duid="%s",fueltech="%s",region="%s"} %s'
            % (
                escape_label(m.get("unit") or ""),
                escape_label(duid),
                escape_label(m.get("fueltech") or ""),
                escape_label(m.get("region") or ""),
                fmt(u["scada"]),
            ),
            'aemo_unit_power_timestamp_seconds{unit="%s",duid="%s"} %s'
            % (escape_label(m.get("unit") or ""), escape_label(duid), fmt(u["ts"])),
            'aemo_unit_power_age_seconds{unit="%s",duid="%s"} %s'
            % (escape_label(m.get("unit") or ""), escape_label(duid), fmt(now - u["ts"])),
        ]
    lines += [
        "# HELP aemo_unit_power_report_info Processed Dispatch_SCADA file.",
        "# TYPE aemo_unit_power_report_info gauge",
        'aemo_unit_power_report_info{file="%s"} 1'
        % escape_label(scada.get("file", "")),
        "# HELP aemo_unit_power_interval_timestamp_seconds "
        "Newest 5-minute interval in the SCADA file.",
        "# TYPE aemo_unit_power_interval_timestamp_seconds gauge",
        "aemo_unit_power_interval_timestamp_seconds %s"
        % fmt(scada.get("newest_interval")),
        "# HELP aemo_unit_power_units Number of dispatch units in the "
        "SCADA file.",
        "# TYPE aemo_unit_power_units gauge",
        "aemo_unit_power_units %d" % len(all_units),
    ]
    return lines


def _emit_battery_power(src: dict) -> list[str]:
    """The legacy `aemo_battery_power_mw` series, storage units only."""
    scada = src["scada"]
    now = src["now"]
    meta = src["meta"]
    units = scada.get("units") or {}
    if not units:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_power_mw Measured output at the newest interval, "
        "MW (negative = charging/absorbing). Deprecated in favour of "
        "aemo_unit_power_mw, which carries the same measurement for every "
        "dispatch unit with fueltech/region labels.",
        "# TYPE aemo_battery_power_mw gauge",
    ]
    for duid in sorted(units):
        u = units[duid]
        m = meta.get(duid) or {}
        lines += [
            'aemo_battery_power_mw{unit="%s",duid="%s"} %s'
            % (escape_label(m.get("unit") or ""), escape_label(duid), fmt(u["scada"])),
            'aemo_battery_power_timestamp_seconds{unit="%s",duid="%s"} %s'
            % (escape_label(m.get("unit") or ""), escape_label(duid), fmt(u["ts"])),
            'aemo_battery_power_age_seconds{unit="%s",duid="%s"} %s'
            % (escape_label(m.get("unit") or ""), escape_label(duid), fmt(now - u["ts"])),
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
    return lines


def _emit_fleet_metadata_missing(src: dict) -> list[str]:
    """`aemo_battery_fleet_metadata_missing`: storage units with no fleet row.

    Which dispatch units in the storage set have no OpenElectricity fleet row.
    Without one they get no fueltech, no region and no registered capacity, so
    they drop out of every region grouping and out of the 100% top clamp on
    their inferred stored energy - and that used to happen silently.

    Scoped to the storage set rather than every dispatch unit in SCADA: the
    aggregate DUIDs AEMO publishes for rooftop PV and distributed generation
    are not facilities and will never be in OpenElectricity's fleet, so
    counting them would make the baseline permanent instead of zero. Gated on
    a non-empty map for the same reason: an empty `unit_meta` means the fleet
    cache has not been read yet, not that every unit is missing.
    """
    units = src["scada"].get("units") or {}
    fleet_by_duid = src["meta"]
    if not units or not fleet_by_duid:
        return []
    missing = [duid for duid in sorted(units) if duid not in fleet_by_duid]
    if not missing:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_fleet_metadata_missing Dispatch units in "
        "the storage set with no OpenElectricity fleet row, and "
        "therefore no fueltech, region or registered capacity. Expected "
        "to be empty; a DUID listed here is one to add to "
        "fleet.UNIT_ALIASES or to raise upstream.",
        "# TYPE aemo_battery_fleet_metadata_missing gauge",
    ]
    for duid in missing:
        lines.append(
            'aemo_battery_fleet_metadata_missing{duid="%s"} 1'
            % escape_label(duid)
        )
    return lines


def _emit_region_storage(src: dict) -> list[str]:
    """Per-region stored/initial MWh and their interval timestamps."""
    region_storage = src["region_storage"]
    regions = region_storage.get("regions") or {}
    if not regions:
        return []
    lines: list[str] = [
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
    return lines


def _emit_curtailment(src: dict) -> list[str]:
    """Per-region renewable curtailment (self-forecast minus cleared MW)."""
    curtailment = src["region_storage"].get("curtailment") or {}
    if not curtailment:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_region_curtailment_mw Renewable capacity the "
        "dispatch engine did not clear, at the newest interval, MW: the "
        "self-forecast (SS_*_UIGF) minus the cleared MW (SS_*_CLEAREDMW), "
        "per region and fuel. Floored at zero; the raw gap is negative when "
        "a unit generates above its own forecast.",
        "# TYPE aemo_battery_region_curtailment_mw gauge",
        "# HELP aemo_battery_region_curtailment_timestamp_seconds Newest "
        "interval behind the region curtailment readings.",
        "# TYPE aemo_battery_region_curtailment_timestamp_seconds gauge",
    ]
    for region in sorted(curtailment):
        r = curtailment[region]
        for fuel in ("wind", "solar"):
            if fuel in r:
                lines.append(
                    'aemo_battery_region_curtailment_mw{region="%s",fuel="%s"} %s'
                    % (escape_label(region), fuel, fmt(r[fuel]))
                )
        lines.append(
            'aemo_battery_region_curtailment_timestamp_seconds{region="%s"} %s'
            % (escape_label(region), fmt(r["ts"]))
        )
    return lines


def _emit_region_fossil(src: dict) -> list[str]:
    """Per-region measured fossil generation, summed from the SCADA rows."""
    fossil = src["scada"].get("region_fossil") or {}
    if not fossil:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_region_fossil_generation_mw Measured output of "
        "coal, gas and distillate units, summed per region, MW. The MW are "
        "AEMO Dispatch_SCADA; the fuel and region attribution is "
        "OpenElectricity fleet metadata (--fleet-cache-file), the only "
        "source of a per-region fuel breakdown.",
        "# TYPE aemo_battery_region_fossil_generation_mw gauge",
        "# HELP aemo_battery_region_fossil_timestamp_seconds Newest SCADA "
        "interval behind the region fossil generation.",
        "# TYPE aemo_battery_region_fossil_timestamp_seconds gauge",
    ]
    for region in sorted(fossil):
        f = fossil[region]
        lines += [
            'aemo_battery_region_fossil_generation_mw{region="%s"} %s'
            % (escape_label(region), fmt(f["mw"])),
            'aemo_battery_region_fossil_timestamp_seconds{region="%s"} %s'
            % (escape_label(region), fmt(f["ts"])),
        ]
    lines += [
        "# HELP aemo_battery_fossil_units Fossil units with a live reading "
        "this cycle.",
        "# TYPE aemo_battery_fossil_units gauge",
        "aemo_battery_fossil_units %d"
        % sum(f["units"] for f in fossil.values()),
    ]
    return lines


def _emit_interconnectors(src: dict) -> list[str]:
    """Interconnector flow/limits, plus report_info and the count gauge.

    The one block that is not withheld on missing data: a feed-verified cycle
    with an empty interconnector set still publishes report_info and
    `aemo_interconnector_count 0` - that absence is the signal a DISPATCH,
    INTERCONNECTORRES table went missing - while the per-link series and their
    HELP headers need readings to exist.
    """
    region_storage = src["region_storage"]
    now = src["now"]
    interconnectors = region_storage.get("interconnectors") or {}
    lines: list[str] = []
    if interconnectors:
        lines += [
            "# HELP aemo_interconnector_flow_mw Algebraic MW flow on the "
            "interconnector at the newest interval (AEMO's own per-link sign "
            "convention).",
            "# TYPE aemo_interconnector_flow_mw gauge",
            "# HELP aemo_interconnector_metered_flow_mw Metered MW flow at the "
            "newest interval (METEREDMWFLOW).",
            "# TYPE aemo_interconnector_metered_flow_mw gauge",
            "# HELP aemo_interconnector_export_limit_mw Export limit in the "
            "dispatch solution, MW. Carried verbatim: AEMO reports negative "
            "limits for some links (e.g. T-V-MNSP1).",
            "# TYPE aemo_interconnector_export_limit_mw gauge",
            "# HELP aemo_interconnector_import_limit_mw Import limit in the "
            "dispatch solution, MW. Carried verbatim; a negative value is "
            "AEMO's own, not a sign error here.",
            "# TYPE aemo_interconnector_import_limit_mw gauge",
            "# HELP aemo_interconnector_nominal_forward_mw Nominal transfer "
            "capability, MW, in the FROM->TO direction (positive flow). Static "
            "system-normal rating from AEMO's Interconnector Capabilities "
            "report, unlike the dynamic EXPORTLIMIT/IMPORTLIMIT.",
            "# TYPE aemo_interconnector_nominal_forward_mw gauge",
            "# HELP aemo_interconnector_nominal_reverse_mw Nominal transfer "
            "capability, MW, in the TO->FROM direction (negative flow).",
            "# TYPE aemo_interconnector_nominal_reverse_mw gauge",
            "# HELP aemo_interconnector_losses_mw Interconnector losses at the "
            "newest interval, MW.",
            "# TYPE aemo_interconnector_losses_mw gauge",
            "# HELP aemo_interconnector_marginal_value Marginal value of the "
            "interconnector constraint, $/MWh.",
            "# TYPE aemo_interconnector_marginal_value gauge",
            "# HELP aemo_interconnector_marginal_loss_factor Marginal loss "
            "factor at the newest interval.",
            "# TYPE aemo_interconnector_marginal_loss_factor gauge",
            "# HELP aemo_interconnector_violation_degree Constraint violation "
            "degree at the newest interval.",
            "# TYPE aemo_interconnector_violation_degree gauge",
            "# HELP aemo_interconnector_timestamp_seconds Newest interval "
            "behind the interconnector readings.",
            "# TYPE aemo_interconnector_timestamp_seconds gauge",
            "# HELP aemo_interconnector_age_seconds Age of that interval at "
            "export time.",
            "# TYPE aemo_interconnector_age_seconds gauge",
        ]
        for ic in sorted(interconnectors):
            i = interconnectors[ic]
            lines += [
                'aemo_interconnector_flow_mw{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("mwflow"))),
                'aemo_interconnector_metered_flow_mw{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("metered_mwflow"))),
                'aemo_interconnector_export_limit_mw{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("export_limit"))),
                'aemo_interconnector_import_limit_mw{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("import_limit"))),
                'aemo_interconnector_losses_mw{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("losses"))),
                'aemo_interconnector_marginal_value{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("marginal_value"))),
                'aemo_interconnector_marginal_loss_factor{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("marginal_loss"))),
                'aemo_interconnector_violation_degree{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("violation_degree"))),
                'aemo_interconnector_timestamp_seconds{interconnector="%s"} %s'
                % (escape_label(ic), fmt(i.get("ts"))),
                'aemo_interconnector_age_seconds{interconnector="%s"} %s'
                % (
                    escape_label(ic),
                    fmt(
                        now - i["ts"]
                        if i.get("ts") is not None
                        else None
                    ),
                ),
            ]
            link = links.LINKS.get(ic)
            if link is not None:
                lines += [
                    'aemo_interconnector_nominal_forward_mw{interconnector="%s"} %s'
                    % (escape_label(ic), fmt(link.nominal_forward_mw)),
                    'aemo_interconnector_nominal_reverse_mw{interconnector="%s"} %s'
                    % (escape_label(ic), fmt(link.nominal_reverse_mw)),
                ]
    lines += [
        "# HELP aemo_interconnector_report_info Processed DispatchIS file.",
        "# TYPE aemo_interconnector_report_info gauge",
        'aemo_interconnector_report_info{file="%s"} 1'
        % escape_label(region_storage.get("file", "")),
        "# HELP aemo_interconnector_count Interconnectors with a reading "
        "this cycle; 0 when the DISPATCH,INTERCONNECTORRES table was "
        "missing or unparsable.",
        "# TYPE aemo_interconnector_count gauge",
        "aemo_interconnector_count %d" % len(interconnectors),
    ]
    return lines


def _emit_region_interchange(src: dict) -> list[str]:
    """Gross per-region import/export, derived from the per-link MWFLOW."""
    interchange = src["interchange"]
    if not interchange:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_region_import_mw Gross MW flow into the region "
        "at the newest interval, summed over its interconnectors. AEMO "
        "publishes no gross regional import/export (REGIONSUM carries only "
        "NETINTERCHANGE), so this is derived from the per-link MWFLOW: a "
        "positive flow on a link leaves its FROM region and enters its TO "
        "region. Interconnector losses are not modelled, so a region's "
        "import is the flow value AEMO signs on the link.",
        "# TYPE aemo_battery_region_import_mw gauge",
        "# HELP aemo_battery_region_export_mw Gross MW flow out of the region "
        "at the newest interval, summed over its interconnectors. See "
        "aemo_battery_region_import_mw for how it is derived.",
        "# TYPE aemo_battery_region_export_mw gauge",
    ]
    for region in sorted(interchange):
        r = interchange[region]
        lines += [
            'aemo_battery_region_import_mw{region="%s"} %s'
            % (escape_label(region), fmt(r["import"])),
            'aemo_battery_region_export_mw{region="%s"} %s'
            % (escape_label(region), fmt(r["export"])),
        ]
    return lines


def _emit_rooftop(src: dict) -> list[str]:
    """AEMO's satellite-estimated rooftop PV output, per region."""
    rooftop = src["rooftop"]
    rp = rooftop.get("regions") or {}
    if not rp:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_region_rooftop_solar_mw AEMO satellite-based "
        "estimate of distributed (rooftop) PV generation, MW, per region, "
        "at the newest 5-minute interval.",
        "# TYPE aemo_battery_region_rooftop_solar_mw gauge",
    ]
    for region in sorted(rp):
        r = rp[region]
        if isinstance(r.get("mw"), (int, float)) and not isinstance(
            r.get("mw"), bool
        ):
            lines.append(
                'aemo_battery_region_rooftop_solar_mw{region="%s"} %s'
                % (escape_label(region), fmt(r["mw"]))
            )
    lines += [
        "# HELP aemo_battery_region_rooftop_solar_timestamp_seconds "
        "Newest interval behind the rooftop PV readings.",
        "# TYPE aemo_battery_region_rooftop_solar_timestamp_seconds gauge",
    ]
    for region in sorted(rp):
        r = rp[region]
        lines.append(
            'aemo_battery_region_rooftop_solar_timestamp_seconds{region="%s"} %s'
            % (escape_label(region), fmt(r.get("ts")))
        )
    lines += [
        "# HELP aemo_battery_region_rooftop_solar_report_info Processed "
        "ROOFTOP_PV file.",
        "# TYPE aemo_battery_region_rooftop_solar_report_info gauge",
        'aemo_battery_region_rooftop_solar_report_info{file="%s"} 1'
        % escape_label(rooftop.get("file", "")),
    ]
    return lines


def _emit_inferred(src: dict) -> list[str]:
    """The per-unit inferred stored MWh, anchored at the daily report."""
    infer = src["infer"]
    now = src["now"]
    meta = src["meta"]
    infer_units = infer.get("units") or {}
    if not infer_units:
        return []
    lines: list[str] = [
        "# HELP aemo_battery_inferred_stored_mwh Anchor ENERGY_STORAGE plus "
        "SCADA-integrated change, MWh (charge_efficiency x charging energy is "
        "stored; discharging is deducted in full).",
        "# TYPE aemo_battery_inferred_stored_mwh gauge",
        "# HELP aemo_battery_inferred_clamped Whether the raw SCADA-"
        "integrated stored MWh left the [0, capacity] envelope (below zero "
        "always; above the unit's registered capacity when it is known) and "
        "was clamped. Reported whether or not clamping is applied, so a "
        "battery pinned at empty or full is distinguishable from one "
        "genuinely there.",
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
        m = meta.get(duid) or {}
        unit = escape_label(m.get("unit") or "")
        key = 'unit="%s",duid="%s"' % (unit, escape_label(duid))
        lines += [
            'aemo_battery_inferred_stored_mwh{%s} %s' % (key, fmt(u["mwh"])),
            'aemo_battery_inferred_clamped{%s} %s'
            % (key, fmt(u.get("clamped", 0.0))),
            'aemo_battery_inferred_timestamp_seconds{%s} %s' % (key, fmt(u["ts"])),
            'aemo_battery_inferred_age_seconds{%s} %s' % (key, fmt(now - u["ts"])),
        ]
    lines += [
        "# HELP aemo_battery_inferred_units Inferred units being served.",
        "# TYPE aemo_battery_inferred_units gauge",
        "aemo_battery_inferred_units %d" % len(infer_units),
        "# HELP aemo_battery_inferred_clamped_units Inferred units held at a "
        "boundary because the raw integral left the [0, capacity] envelope.",
        "# TYPE aemo_battery_inferred_clamped_units gauge",
        "aemo_battery_inferred_clamped_units %d"
        % sum(1 for u in infer_units.values() if float(u.get("clamped", 0.0)) > 0.0),
        "# HELP aemo_battery_inferred_fleet_capacity_units Inferred units "
        "with a known registered storage capacity (from --fleet-cache-file), "
        "which is what the 100% top clamp clamps against. 0 = no capacity in "
        "scope, so only the zero floor applies.",
        "# TYPE aemo_battery_inferred_fleet_capacity_units gauge",
        "aemo_battery_inferred_fleet_capacity_units %d"
        % int(infer.get("capacity_units") or 0),
        "# HELP aemo_battery_inferred_report_info Daily report anchoring the "
        "inferred series.",
        "# TYPE aemo_battery_inferred_report_info gauge",
        'aemo_battery_inferred_report_info{file="%s"} 1'
        % escape_label(infer.get("file", "")),
    ]
    return lines


# The intraday failure policy, as data: every block of series and the feed it
# rides on. render_intraday walks this table in order and emits a block only
# when its feed verified this cycle; a `None` feed means the block has no feed
# gate at all and is withheld only by its own data being absent. A failed feed
# therefore drops exactly its own blocks and leaves the others publishing - and
# a new feed adds lines here (plus its emitters and a line in the render's
# `feeds` map), never another branch in the render itself. The block name is
# the row's human-readable key, and the feed-policy test pins names, feeds and
# emission order together so the table cannot drift unreviewed.
INTRADAY_BLOCKS: tuple[tuple[str, str | None, Callable[[dict], list[str]]], ...] = (
    ("unit_power", "scada", _emit_unit_power),
    ("battery_power", "scada", _emit_battery_power),
    ("fleet_metadata_missing", None, _emit_fleet_metadata_missing),
    ("region_storage", "dispatchis", _emit_region_storage),
    ("curtailment", "dispatchis", _emit_curtailment),
    ("region_fossil", "scada", _emit_region_fossil),
    ("interconnectors", "dispatchis", _emit_interconnectors),
    ("region_interchange", "dispatchis", _emit_region_interchange),
    ("rooftop", "rooftop", _emit_rooftop),
    ("inferred", "infer", _emit_inferred),
)


@dataclasses.dataclass(frozen=True)
class CycleState:
    """One poll cycle's renderable output: every feed's state and health flag.

    The render takes this single value rather than a spread of arguments, so
    the render itself grows only by fields here. Both serving paths build a
    CycleState through build_cycle_state and hand it to render_intraday, so
    they cannot drift apart on what the render sees.
    """

    now: float
    duration: float
    scada: dict
    scada_ok: bool
    region_storage: dict
    region_ok: bool
    rooftop: dict
    rooftop_ok: bool
    infer: dict
    infer_ok: bool
    unit_meta: dict


def build_cycle_state(
        scada: dict,
        scada_ok: bool,
        region_storage: dict,
        region_ok: bool,
        duration: float,
        now: float,
        *,
        infer: dict | None = None,
        unit_meta: dict | None = None,
        rooftop: dict | None = None,
        rooftop_ok: bool = False,
    ) -> CycleState:
    """Assemble one cycle's render inputs; the single home of the infer predicate.

    The inferred side is only "up" when it is actually serving units: an
    anchor that exists but whose units have aged out (or whose gap could not
    be reconstructed) serves nothing, and reporting success there is how a
    fleet-wide stale anchor stayed invisible. Success tracks the served
    series, not the file - and this is the only place that predicate is
    written, so the poll path and the scrape path cannot diverge again.

    The optional feeds are keyword-only: a caller that has no rooftop or
    inferred side names only what it has, and a new feed arrives as one
    keyword here and one field on CycleState rather than another positional
    every caller must thread.
    """
    infer = infer or {}
    infer_ok = (
        scada_ok
        and bool(infer.get("file"))
        and bool(infer.get("units"))
    )
    return CycleState(
        now=now,
        duration=duration,
        scada=scada,
        scada_ok=scada_ok,
        region_storage=region_storage,
        region_ok=region_ok,
        rooftop=rooftop or {},
        rooftop_ok=rooftop_ok,
        infer=infer,
        infer_ok=infer_ok,
        unit_meta=unit_meta or {},
    )


def render_intraday(state: CycleState) -> str:
    """Exposition for the intraday cycle's last readings.

    Each side is independent: a failed SCADA read omits the per-unit power and
    per-region fossil series while a healthy DispatchIS read still publishes the
    region storage and curtailment (and vice versa); each has its own success
    gauge, and neither repeats a measurement it did not verify this cycle. The
    per-unit *inferred* stored MWh is a coupled side: it is served only when a
    daily anchor exists AND the SCADA feed is live this cycle, since it is built
    from SCADA - and `state.infer_ok` already carries that combined verdict,
    computed once in build_cycle_state.

    That policy is declared in INTRADAY_BLOCKS rather than coded here: the walk
    below emits each block only when its feed verified this cycle, so the
    branches in this function are loop iterations, not feed conditions.

    The `aemo_unit_power_mw` series carries every dispatch unit in the SCADA
    file - generators, loads and storage alike - labelled with its fueltech and
    region from the OE fleet cache (`state.unit_meta`). It is the superset of the
    legacy `aemo_battery_power_mw` series, which stays filtered to storage units
    so existing dashboards keep their exact semantics while consumers migrate.
    """
    feeds = {
        "scada": state.scada_ok,
        "dispatchis": state.region_ok,
        "rooftop": state.rooftop_ok,
        "infer": state.infer_ok,
    }
    # `meta` is keyed by DUID (not AEMO's spelling of it): the fleet unit code
    # rides on every per-unit series so consumers can join on the fleet's own
    # identifier - see docs/adr/0001 and fleet.UNIT_ALIASES.
    src: dict = {
        "meta": state.unit_meta,
        "now": state.now,
        "scada": state.scada,
        "region_storage": state.region_storage,
        "rooftop": state.rooftop,
        "infer": state.infer,
        "interchange": region_interchange(
            state.region_storage.get("interconnectors") or {}
        ),
    }
    lines: list[str] = []
    for _block, feed, emit in INTRADAY_BLOCKS:
        if feed is None or feeds[feed]:
            lines += emit(src)
    lines += [
        "# HELP aemo_battery_infer_success Whether the inferred stored-MWh series "
        "is being served (fresh anchor, live SCADA, and any anchor gap "
        "reconstructed from the archive).",
        "# TYPE aemo_battery_infer_success gauge",
        "aemo_battery_infer_success %d" % (1 if state.infer_ok else 0),
        "# HELP aemo_battery_inferred_anchor_age_seconds Age of the daily-report "
        "interval behind the inferred series, at export time. The per-unit "
        "values advance with SCADA; this is the anchor itself, so a fleet-wide "
        "stale anchor reads large here even while individual ages look fine.",
        "# TYPE aemo_battery_inferred_anchor_age_seconds gauge",
        "aemo_battery_inferred_anchor_age_seconds %s"
        % fmt(state.now - state.infer.get("anchor_ts")
              if isinstance(state.infer.get("anchor_ts"), (int, float)) else None),
        "# HELP aemo_battery_infer_charge_efficiency Share of charging energy "
        "counted as stored when integrating SCADA.",
        "# TYPE aemo_battery_infer_charge_efficiency gauge",
        "aemo_battery_infer_charge_efficiency %s"
        % fmt(state.infer.get("efficiency", DEFAULT_INFER_CHARGE_EFFICIENCY)),
        "# HELP aemo_battery_scada_success "
        "Whether the last poll of the Dispatch_SCADA archive succeeded.",
        "# TYPE aemo_battery_scada_success gauge",
        "aemo_battery_scada_success %d" % (1 if state.scada_ok else 0),
        "# HELP aemo_battery_dispatchis_success "
        "Whether the last poll of the DispatchIS_Reports archive succeeded.",
        "# TYPE aemo_battery_dispatchis_success gauge",
        "aemo_battery_dispatchis_success %d" % (1 if state.region_ok else 0),
        "# HELP aemo_battery_intraday_poll_duration_seconds "
        "Wall time of the last intraday poll cycle.",
        "# TYPE aemo_battery_intraday_poll_duration_seconds gauge",
        "aemo_battery_intraday_poll_duration_seconds %s" % fmt(state.duration),
        "# HELP aemo_battery_intraday_last_poll_timestamp_seconds "
        "Unix time of the last intraday poll cycle.",
        "# TYPE aemo_battery_intraday_last_poll_timestamp_seconds gauge",
        "aemo_battery_intraday_last_poll_timestamp_seconds %s" % fmt(state.now),
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
        fleet_cache_file: str | None = None,
        rooftop_url: str | None = None,
    ) -> None:
        self.scada_url = scada_url.rstrip("/") + "/"
        self.dispatchis_url = dispatchis_url.rstrip("/") + "/"
        self.rooftop_url = (rooftop_url or ROOFTOP_PV_DATA_URL).rstrip("/") + "/"
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
        self.now_fn = time.time
        # The OE fleet cache serves two derived maps. Registered storage
        # capacity per DUID lets charging clamp at the battery's capacity as
        # well as at zero; absent (no --fleet-cache-file, or one not yet
        # written) means the top clamp is simply not available and units stay
        # floored at zero only. DUID -> region for fossil units lets each
        # measured coal/gas/distillate MW be attributed to a region. Loaded
        # before the state restore so a restored value above capacity is pinned.
        self.fleet_cache_file = fleet_cache_file
        self._fleet_capacity: dict[str, float] = {}
        self._fossil_region: dict[str, str] = {}
        self._unit_meta: dict[str, dict] = {}
        self._fleet_metadata_last_attempt = 0.0
        self._maybe_load_fleet_metadata(force=True)
        self._inferred: dict[str, dict] = {}
        if infer_state_file:
            self._load_infer_state()
        self._lock = threading.Lock()
        self._scada_state: dict | None = None
        self._is_state: dict | None = None
        self._rooftop_state: dict | None = None
        self._scada_health = (False, "not polled yet")
        self._is_health = (False, "not polled yet")
        self._rooftop_health = (False, "not polled yet")
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
        # Pick up the OE fleet cache if it was not there at startup (a cold
        # start usually beats the OE exporter to the shared volume), so this
        # cycle's integration already clamps at the units' capacities and the
        # fossil region map is available.
        self._maybe_load_fleet_metadata()
        scada_ok = region_ok = False
        rooftop_ok = False
        scada_state: dict = {}
        is_state: dict = {}
        rooftop_state: dict = {}
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
        try:
            listing = http_get(
                self.rooftop_url, self.timeout, self.retries, self.retry_budget
            )
            newest = newest_rooftop_pv_file(listing.decode("utf-8", "replace"))
            rooftop_state = self._resolve_rooftop(newest)
            rooftop_ok = True
        except (ScrapeError, ValueError, KeyError, TypeError, OSError) as exc:
            log.error("ROOFTOP_PV poll failed: %s", exc)

        if scada_ok:
            units = scada_state.get("units") or {}
            # Fossil generation is summed from the *unfiltered* SCADA rows,
            # before the battery filter below narrows `units`; it needs only the
            # DUID -> region map, not the battery set. A reused state already
            # carries its region_fossil from the cycle that parsed the file, and
            # its `units` are already filtered, so it must not be recomputed.
            if not scada_state.get("reused"):
                scada_state["region_fossil"] = self._fossil_generation(units)
                # The unit-level series wants every dispatch unit, so the full
                # parsed set is kept before the battery filter narrows `units`.
                # A reused state already carries it from the parsing cycle.
                scada_state["all_units"] = dict(units)
            batteries = self._learned_batteries()
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
            self._rooftop_state = rooftop_state
            self._last_poll = finished
            self._last_duration = duration
            self._scada_health = (scada_ok, "" if scada_ok else "Dispatch_SCADA poll failed")
            self._is_health = (region_ok, "" if region_ok else "DispatchIS poll failed")
            self._rooftop_health = (
                rooftop_ok, "" if rooftop_ok else "ROOFTOP_PV poll failed"
            )
        # The inferred side is only "up" when it is actually serving units: an
        # anchor that exists but whose gap could not be reconstructed serves
        # nothing, and reporting success there is how a fleet-wide stale anchor
        # stayed invisible. That verdict lives in build_cycle_state, shared
        # with the exposition path.
        state = build_cycle_state(
            scada_state, scada_ok, is_state, region_ok, duration, finished,
            infer=infer, unit_meta=self._unit_meta,
            rooftop=rooftop_state, rooftop_ok=rooftop_ok,
        )
        return (
            render_intraday(state),
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
        # Interconnector flow/limits live in the same DispatchIS payload (the
        # DISPATCH,INTERCONNECTORRES table), so they are parsed in-memory from
        # the very download that already yields region storage. A failure of
        # that one table must not drop the region series: log it and publish an
        # empty interconnector set, which `aemo_interconnector_count` makes
        # visible.
        report["interconnectors"] = {}
        try:
            report["interconnectors"] = parse_dispatchis_interconnectors(
                payload
            )["interconnectors"]
        except ScrapeError as exc:
            log.warning(
                "DispatchIS interconnector table failed; interconnector series "
                "absent: %s",
                exc,
            )
        report["file"] = newest
        report["reused"] = False
        return report

    def _resolve_rooftop(self, newest: str) -> dict:
        held = self._rooftop_state
        if held is not None and held.get("file") == newest:
            report = dict(held)
            report["reused"] = True
            return report
        url = urllib.parse.urljoin(self.rooftop_url, newest)
        payload = http_get(url, self.timeout, self.retries, self.retry_budget)
        report = parse_rooftop_pv(payload)
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
            mwh, clamped = self._clamp_inferred(
                float(energy), self._fleet_capacity.get(duid)
            )
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
        limit = now - self._infer_policy().max_gap_hours * 3600.0
        self._inferred = {d: u for d, u in self._inferred.items() if u["ts"] > limit}

    def _infer_policy(self) -> inference.Policy:
        """The estimator's policy, stated from this poller's live settings.

        Built per call because the tests - and the CLI - mutate
        `charge_efficiency` and `infer_clamp` after construction.
        """
        return inference.Policy(
            charge_efficiency=self.charge_efficiency,
            max_gap_hours=MAX_INFER_GAP_HOURS,
            clamp=self.infer_clamp,
        )

    def _clamp_inferred(
        self, raw: float, capacity: float | None = None
    ) -> tuple[float, float]:
        """Clamp the inferred stored MWh into [0, capacity]; returns (mwh, flag).

        Thin delegation to `inference.clamp_stored`, which owns the rule: the
        zero floor applies regardless; the top clamp only where a registered
        storage capacity is known; the flag reports the raw integral leaving
        the envelope whether or not clamping is applied.
        """
        return inference.clamp_stored(raw, capacity, self._infer_policy())

    def _advance_entry(
        self, entry: dict, ts: float, power: float, capacity: float | None = None
    ) -> dict:
        """Integrate one measured 5-minute power step into the inferred entry.

        Thin delegation to `inference.advance`, shared by the live advance and
        the archive backfill. A step that does not move the clock returns the
        entry unchanged, so re-sent or out-of-order archive rows never
        double-count.
        """
        return inference.advance(entry, ts, power, capacity, self._infer_policy())

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
                stepped = self._advance_entry(
                    entry, u["ts"], u["scada"], self._fleet_capacity.get(duid)
                )
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
                mwh, clamped = self._clamp_inferred(
                    float(entry["energy"]), self._fleet_capacity.get(duid)
                )
                self._inferred[duid] = {
                    "ts": float(entry["ts"]), "mwh": mwh, "clamped": clamped,
                }
        self._backfill_if_stale(end_ts, now)
        policy = self._infer_policy()
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
            if delta_hours > policy.max_gap_hours:
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
            self._inferred[duid] = self._advance_entry(
                current, ts, power, self._fleet_capacity.get(duid)
            )
        self._prune_stale(now)

    def _maybe_load_fleet_metadata(self, force: bool = False) -> None:
        """Derive the two DUID maps this exporter needs from the OE fleet cache.

        The OE exporter writes this cache for the whole NEM fleet, so it carries
        a registered storage capacity for units this exporter never anchors
        (exactly the set the top clamp needs) and a fueltech/region for every
        unit, from which the fossil set is built. Missing or unreadable is not
        fatal: the maps stay empty (or keep their last good contents), units are
        floored at zero only, and no fossil series is published, so a malformed
        cache degrades instead of dropping the inferred series.

        The stop condition is *both* maps, not either. A cache written by the
        previous exporter holds only battery capacity, so an early return on
        capacity alone would leave the fossil map empty forever - the file is
        replaced in place by the OE exporter's next fleet refresh, and this
        poller has to notice. Retrying until the fossil map is populated (at
        most once per 300s, below) picks that up within a cycle or two.
        """
        if not self.fleet_cache_file:
            return
        if not force and self._fleet_capacity and self._fossil_region:
            return
        now = self.now_fn()
        # A cold start routinely beats the OE exporter to the shared volume;
        # retry at most once per poll cycle rather than on every integration
        # step, and never spam the log in between.
        if not force and now - self._fleet_metadata_last_attempt < 300.0:
            return
        self._fleet_metadata_last_attempt = now
        fl = fleet.load(self.fleet_cache_file)
        if fl is None:
            log.warning(
                "fleet cache %s unusable; inference is floored at zero "
                "only and fossil generation is omitted",
                self.fleet_cache_file,
            )
            return
        # Three views of the same parsed fleet, each keyed by fleet unit code.
        # `is_fossil` rather than a local fueltech set: the taxonomy is the
        # fleet's, and AEMO publishes no per-region fuel breakdown - Dispatch_SCADA
        # carries measured MW but only a DUID - so every attribution runs through
        # this map.
        caps: dict[str, float] = {
            unit.code: unit.capacity_storage_mwh
            for unit in fl.units
            if unit.capacity_storage_mwh is not None
        }
        fossil_region: dict[str, str] = {
            unit.code: unit.region
            for unit in fl.units
            if unit.is_fossil and unit.region
        }
        unit_meta: dict[str, dict] = {
            unit.code: {
                "fueltech": unit.fueltech,
                "region": unit.region,
                "unit": unit.code,
            }
            for unit in fl.units
        }
        # Index the same facts under AEMO's dispatch unit id where the two
        # identifiers disagree, so the lookups below - which are all keyed by
        # DUID - resolve for every unit rather than all but one. See
        # fleet.UNIT_ALIASES and docs/adr/0001.
        for duid, code in fleet.UNIT_ALIASES.items():
            if code in caps:
                caps[duid] = caps[code]
            if code in fossil_region:
                fossil_region[duid] = fossil_region[code]
            if code in unit_meta:
                unit_meta[duid] = unit_meta[code]
        if caps:
            self._fleet_capacity = caps
            log.info(
                "fleet capacity loaded: %d units from %s",
                len(caps), self.fleet_cache_file,
            )
        if fossil_region:
            self._fossil_region = fossil_region
            log.info(
                "fossil region map loaded: %d units from %s",
                len(fossil_region), self.fleet_cache_file,
            )
        if unit_meta:
            self._unit_meta = unit_meta
            log.info(
                "unit metadata loaded: %d units from %s",
                len(unit_meta), self.fleet_cache_file,
            )

    def _fossil_generation(self, units: dict) -> dict:
        """Per-region measured fossil MW from the unfiltered SCADA rows.

        AEMO publishes no per-region fuel breakdown, so each measured DUID's MW
        is attributed to its region with the map read from the OE fleet cache
        and summed. Returns {region: {"ts": newest interval that region saw,
        "mw": total MW, "units": contributing units}}; regions with no mapped
        unit are absent. Positive MW is generation.
        """
        regions: dict[str, dict] = {}
        for duid, u in units.items():
            region = self._fossil_region.get(duid)
            if region is None:
                continue
            entry = regions.get(region)
            if entry is None:
                regions[region] = {"ts": u["ts"], "mw": u["scada"], "units": 1}
            else:
                entry["mw"] += u["scada"]
                entry["units"] += 1
                if u["ts"] > entry["ts"]:
                    entry["ts"] = u["ts"]
        return regions

    def _infer_view(self, now: float) -> dict:
        """Snapshot for rendering: live units only, plus the anchor's age."""
        limit = now - self._infer_policy().max_gap_hours * 3600.0
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
            # Of the served units, how many have a registered capacity in scope -
            # the ones the 100% top clamp can actually hold back.
            "capacity_units": sum(1 for d in inferred if d in self._fleet_capacity),
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
                mwh, clamped = self._clamp_inferred(mwh, self._fleet_capacity.get(duid))
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
            rooftop_state = self._rooftop_state
            scada_health = self._scada_health
            is_health = self._is_health
            rooftop_health = self._rooftop_health
            duration = self._last_duration
        body = render_intraday(
            build_cycle_state(
                scada_state or {}, scada_health[0],
                is_state or {}, is_health[0],
                duration, now,
                infer=self._infer_view(now), unit_meta=self._unit_meta,
                rooftop=rooftop_state or {}, rooftop_ok=rooftop_health[0],
            )
        )
        return body, scada_health[0] and is_health[0]

    def health(self) -> tuple[bool, str]:
        with self._lock:
            sa, sb = self._scada_health
            ia, ib = self._is_health
            ra, rb = self._rooftop_health
        ok = sa and ia and ra
        parts = []
        if not sa:
            parts.append(sb)
        if not ia:
            parts.append(ib)
        if not ra:
            parts.append(rb)
        return ok, "; ".join(parts) if parts else ""

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
output MW per unit) and <b>DispatchIS_Reports</b> (region-level BDU stored MWh
and SS_* wind/solar curtailment) - updates every 5-minute interval and is what
moves the chart through the day. Per-region fossil generation is summed from
Dispatch_SCADA using the OpenElectricity fleet cache for fuel and region.</p>
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
        "--rooftop-pv-url",
        default=ROOFTOP_PV_DATA_URL,
        help="ROOFTOP_PV ACTUAL archive directory (default %s)."
        % ROOFTOP_PV_DATA_URL,
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
        "into the inferred stored MWh (default %g, the value fitted against "
        "AEMO's reported regional storage and stated once in soc_policy.py - "
        "see NOTES.md for the fit. NEMPulse applies ~0.85 round-trip, here on "
        "the charging leg)." % DEFAULT_INFER_CHARGE_EFFICIENCY,
    )
    parser.add_argument(
        "--infer-clamp",
        action="store_true",
        default=DEFAULT_INFER_CLAMP,
        help="clamp the inferred stored MWh into [0, capacity] (default true). "
        "The zero floor always applies; the capacity ceiling only applies where "
        "--fleet-cache-file supplies a registered capacity for the unit.",
    )
    parser.add_argument(
        "--no-infer-clamp",
        action="store_false",
        dest="infer_clamp",
        help="serve the raw SCADA-integrated drift outside [0, capacity] "
        "instead of clamped; the clamped flag keeps reporting it either way",
    )
    parser.add_argument(
        "--fleet-cache-file",
        help="JSON file the OE exporter writes (its --fleet-cache-file), read "
        "for the per-DUID registered storage capacity that the inference top "
        "clamps at 100%%, and for the per-DUID fuel and region that attribute "
        "measured Dispatch_SCADA MW to the per-region fossil series. Read-only; "
        "a missing or unreadable file degrades to the zero floor only and "
        "omits fossil generation.",
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
        fleet_cache_file=args.fleet_cache_file,
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