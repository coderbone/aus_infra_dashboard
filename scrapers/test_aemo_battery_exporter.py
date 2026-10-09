#!/usr/bin/env python3
"""Tests for aemo_battery_exporter.py.

Offline: every upstream response is built in-memory as a synthetic
`Next_Day_Dispatch` zip shaped like the real file, with the storage columns at
their verified positions (SETTLEMENTDATE at column 4, DUID at 6,
INITIAL_ENERGY_STORAGE at 69, ENERGY_STORAGE at 70, and TOTALCLEARED at 14 of
the 73-column UNIT_SOLUTION row). No test touches the network, and nothing
126 MB is ever materialised - the synthetic zip is a handful of rows.

The expectations are the upstream's own numbers read out of the real file on
2026-10-08 (HPR1 reporting initial 21.7 / energy 21.8625 MWh at its newest
interval), not values invented next to the parser.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_aemo_battery_exporter.py

`-t scrapers` is required, for the same reason as the sibling tests: scrapers/
has no __init__.py so the exporter stays a single bind-mounted file.

The tests concentrate on the properties that turn a wrong number into a
plausible one rather than an obvious failure:

  1. Per unit, the *newest* reported interval wins, whatever the row order.
  2. Generators (empty INITIAL_ENERGY_STORAGE) are not storage units.
  3. An unchanged newest file is re-served without a re-download, but only
     after the live listing is re-read - the listing is the verification.
  4. A failed poll publishes no per-unit series at all, never stale leftovers.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aemo_battery_exporter as exporter  # noqa: E402

NCOLS = 73

# Verified against the real 2026-10-07 dispatch file (published 2026-10-08):
# it dates from the morning of the higher-charged HPR1, whose newest interval's
# numbers the tests read out below.
ZIP_NAME = "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip"
MEMBER_CSV = "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.CSV"
HPR_INITIAL = "21.7"
HPR_ENERGY = "21.8625"
HPR_CLEARED = "0"  # HPR1's real newest-interval TOTALCLEARED: idle that morning
ERB_INITIAL = "130.0"
ERB_ENERGY = "128.0"
ERB_CLEARED = "-8"  # Eraring BESS charging at the newest interval

HPR_TS = 1791396000.0  # 2026/10/08 04:00:00+10, the newest interval in the file
OLD_TS = 1791309900.0  # 2026/10/07 04:05:00+10, the oldest interval
PUBLISHED = 1791396600.0  # 2026/10/08 04:10:00+10, the publication stamp
NOW = PUBLISHED + 3600.0  # an hour after publication

AEST = timezone(timedelta(hours=10))

DATA_URL = "http://nemweb.example/Reports/Current/Next_Day_Dispatch/"


# --------------------------------------------------------------------------- #
# Synthetic upstream
# --------------------------------------------------------------------------- #


def unit_solution_row(
    kind,
    timestamp="2026/10/08 04:00:00",
    duid="HPR1",
    initial=HPR_INITIAL,
    energy=HPR_ENERGY,
    cleared="0",
):
    """One UNIT_SOLUTION row with fields at the real upstream positions."""
    cells = [""] * NCOLS
    cells[0] = kind
    cells[1] = "DISPATCH"
    cells[2] = "UNIT_SOLUTION"
    cells[3] = "6"
    cells[4] = timestamp
    cells[6] = duid
    cells[14] = cleared
    cells[69] = initial
    cells[70] = energy
    return cells


def header_row():
    row = unit_solution_row("I")
    row[4] = "SETTLEMENTDATE"
    row[6] = "DUID"
    row[14] = "TOTALCLEARED"
    row[69] = "INITIAL_ENERGY_STORAGE"
    row[70] = "ENERGY_STORAGE"
    return row


CONTROL = [
    "C", "NEMP.WORLD", "NEXT_DAY_DISPATCH", "AEMO", "PUBLIC",
    "2026/10/08", "04:10:00", "0000000541730398", "NEXT_DAY_DISPATCH",
    "0000000541730394",
]


def make_zip(name, rows):
    payload = io.StringIO()
    writer = csv.writer(payload)
    for row in rows:
        writer.writerow(row)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, payload.getvalue())
    return buf.getvalue()


def report_zip(name=MEMBER_CSV, rows=()):
    return make_zip(name, [header_row(), CONTROL, *rows])


def sample_rows():
    return [
        # Two units at the oldest interval; then HPR1's newest, later, interval.
        unit_solution_row("D", "2026/10/07 04:05:00", "HPR1", "20.0", "21.0"),
        unit_solution_row("D", "2026/10/07 04:05:00", "ERB01", "100.0", "101.0"),
        unit_solution_row("D", "2026/10/08 04:00:00", "HPR1", HPR_INITIAL, HPR_ENERGY, HPR_CLEARED),
        # ERB01's newest interval charges: negative TOTALCLEARED, energy falling.
        unit_solution_row("D", "2026/10/08 04:00:00", "ERB01", ERB_INITIAL, ERB_ENERGY, ERB_CLEARED),
        # A unit that stops before the final interval (the 287-of-288 shape
        # MLB01, CAPBES1, ERB01 and HVWWBA1 show in the real file): its newest
        # well-formed row wins.
        unit_solution_row("D", "2026/10/07 04:05:00", "MLB01", "5.0", "5.5"),
        unit_solution_row("D", "2026/10/07 04:10:00", "MLB01", "6.0", "6.2"),
        # A generator reports empty storage: not a storage unit.
        unit_solution_row("D", "2026/10/08 04:00:00", "GEN01", "", ""),
        # ENERGY_STORAGE blank but INITIAL present: falls back to the initial.
        # TOTALCLEARED blank too, so cleared stays None.
        unit_solution_row("D", "2026/10/08 03:55:00", "WALGRV1", "9.9", "", ""),
    ]


def parsed_report(rows=None):
    return exporter.parse_next_day_dispatch(report_zip(rows=rows or sample_rows()))


# The zip the fake fetcher serves by default, carrying the same four storage
# units the parse/render tests read.
SAMPLE_ZIP = report_zip(rows=sample_rows())


# --- Intraday (Dispatch_SCADA / DispatchIS_Reports) synthetic upstream -------- #
#
# Shape verified against the real 15:20 files on 2026-10-08: UNIT_SCADA carries
# SETTLEMENTDATE at 4 / DUID at 5 / SCADAVALUE at 6 of an 8-column row, and
# DISPATCH,REGIONSUM carries SETTLEMENTDATE at 4 / REGIONID at 6 /
# BDU_ENERGY_STORAGE at 124 / BDU_INITIAL_ENERGY_STORAGE at 129 of a 130-column
# row. The values are the upstream's own that afternoon (WTAHB1 -34.07,
# ERB01 -120.01, LIMBESS1 +0.90; NSW1 storing 5063.4 -> 5084.6 MWh).

SCADA_ZIP = "PUBLIC_DISPATCHSCADA_202610081520_0000000541812607.zip"
SCADA_CSV = "PUBLIC_DISPATCHSCADA_202610081520_0000000541812607.CSV"
IS_ZIP = "PUBLIC_DISPATCHIS_202610081520_0000000541812602.zip"
IS_CSV = "PUBLIC_DISPATCHIS_202610081520_0000000541812602.CSV"
SCADA_TS = 1791436800.0  # 2026/10/08 15:20:00+10, the interval both files carry


def scada_row(kind, timestamp="2026/10/08 15:20:00", duid="WTAHB1", value="-34.072630"):
    cells = [""] * 8
    cells[0] = kind
    cells[1] = "DISPATCH"
    cells[2] = "UNIT_SCADA"
    cells[3] = "1"
    cells[4] = timestamp
    cells[5] = duid
    cells[6] = value
    cells[7] = timestamp
    return cells


def scada_header():
    row = scada_row("I")
    row[4] = "SETTLEMENTDATE"
    row[5] = "DUID"
    row[6] = "SCADAVALUE"
    row[7] = "LASTCHANGED"
    return row


def scada_zip(name=SCADA_CSV, rows=()):
    return make_zip(name, [scada_header(), *rows])


def scada_sample_rows():
    return [
        scada_row("D", duid="WTAHB1", value="-34.072630"),
        scada_row("D", duid="ERB01", value="-120.009160"),
        scada_row("D", duid="DPNTB1", value="-6.790540"),
        scada_row("D", duid="LIMBESS1", value="0.897230"),
    ]


SCADA_SAMPLE_ZIP = scada_zip(rows=scada_sample_rows())


def region_sum_row(
    kind, timestamp="2026/10/08 15:20:00", region="NSW1",
    initial="5063.368830", energy="5084.591640",
):
    cells = [""] * 130
    cells[0] = kind
    cells[1] = "DISPATCH"
    cells[2] = "REGIONSUM"
    cells[3] = "1"
    cells[4] = timestamp
    cells[6] = region
    cells[124] = energy
    cells[129] = initial
    return cells


def region_header():
    row = region_sum_row("I")
    row[4] = "SETTLEMENTDATE"
    row[6] = "REGIONID"
    row[124] = "BDU_ENERGY_STORAGE"
    row[129] = "BDU_INITIAL_ENERGY_STORAGE"
    return row


def is_zip(name=IS_CSV, rows=()):
    return make_zip(name, [region_header(), *rows])


def is_sample_rows():
    return [
        region_sum_row("D", "2026/10/08 15:20:00", "NSW1", "5063.368830", "5084.591640"),
        region_sum_row("D", "2026/10/08 15:20:00", "QLD1", "4207.204660", "4238.2711"),
        region_sum_row("D", "2026/10/08 15:20:00", "SA1", "1967.194450", "1973.289430"),
        region_sum_row("D", "2026/10/08 15:20:00", "VIC1", "3881.447850", "3881.825510"),
        # TAS1 reports no battery storage: both BDU columns blank, so it is skipped.
        region_sum_row("D", "2026/10/08 15:20:00", "TAS1", "", ""),
    ]


IS_SAMPLE_ZIP = is_zip(rows=is_sample_rows())


def listing_report():
    """The parsed report as the scraper's `_download` leaves it: the report
    returned by the parser, now stamped with the listing's zip filename."""
    report = parsed_report()
    report["file"] = ZIP_NAME
    return report


def listing_with(*names):
    links = "\n".join(
        '<A HREF="/Reports/CURRENT/Next_Day_Dispatch/%s">%s</A>' % (n, n)
        for n in names
    )
    return ("<html><body>%s</body></html>" % links).encode()


class FakeFetcher:
    """Stands in for the exporter's http_get.

    Serves the listing for non-zip URLs and the synthetic zips by name; every
    fetch is recorded so tests can assert the download happened once, or not
    at all. `fail_zips` makes zips unresolveable while the listing keeps
    working, which is the restart-recovery shape. `listing_names` is mutable,
    letting a test let the "publisher" advance to a newer file between polls.
    """

    def __init__(self, zips=None, fail_zips=False, error=None, listing_names=(ZIP_NAME,)):
        self.zips = dict(zips or {})
        self.fail_zips = fail_zips
        self.error = error
        self.listing_names = list(listing_names)
        self.fetches = []

    def __call__(self, url, timeout, retries=0, retry_budget=0.0):
        self.fetches.append(url)
        if self.error:
            raise self.error
        filename = url.rsplit("/", 1)[-1]
        if not filename.endswith(".zip"):
            return listing_with(*self.listing_names)
        if self.fail_zips or filename not in self.zips:
            raise exporter.ScrapeError("GET %s -> HTTP 404" % url)
        return self.zips[filename]


def make_scraper(**kwargs):
    state_file = kwargs.pop("state_file", None)
    now = kwargs.pop("now", NOW)
    scraper = exporter.NextDayScraper(
        kwargs.pop("data_url", DATA_URL),
        kwargs.pop("timeout", 5.0),
        kwargs.pop("poll_interval", 3600.0),
        kwargs.pop("retries", 0),
        kwargs.pop("retry_budget", 0.0),
        state_file,
    )
    scraper.now_fn = lambda: now
    return scraper


def polled(fetcher, **kwargs):
    with mock.patch.object(exporter, "http_get", fetcher):
        scraper = make_scraper(**kwargs)
        body, ok = scraper.poll_once()
        return scraper, body, ok


def listing_html(path, *names):
    links = "\n".join(
        '<A HREF="%s%s">%s</A>' % (path, n, n) for n in names
    )
    return ("<html><body>%s</body></html>" % links).encode()


SCADA_LISTING_URL = "http://nemweb.example/Reports/Current/Dispatch_SCADA/"
IS_LISTING_URL = "http://nemweb.example/Reports/Current/DispatchIS_Reports/"


class IntradayFetcher:
    """Stands in for http_get during an IntradayPoller poll.

    Serves Dispatch_SCADA and DispatchIS_Reports listings by directory and the
    newest zips by name; every fetch is recorded so tests can count downloads.
    `scada_names`/`is_names` let the "publisher" advance; the error flags fail
    one directory while the other keeps working.
    """

    def __init__(
        self,
        scada_zips=None,
        is_zips=None,
        scada_names=(SCADA_ZIP,),
        is_names=(IS_ZIP,),
        scada_error=None,
        is_error=None,
    ):
        self.scada = dict(scada_zips or {})
        self.isr = dict(is_zips or {})
        self.scada_names = list(scada_names)
        self.is_names = list(is_names)
        self.scada_error = scada_error
        self.is_error = is_error
        self.fetches = []

    def __call__(self, url, timeout, retries=0, retry_budget=0.0):
        self.fetches.append(url)
        filename = url.rsplit("/", 1)[-1]
        if not filename.endswith(".zip"):
            if "/Dispatch_SCADA/" in url:
                if self.scada_error:
                    raise self.scada_error
                return listing_html(url, *self.scada_names)
            if "/DispatchIS_Reports/" in url:
                if self.is_error:
                    raise self.is_error
                return listing_html(url, *self.is_names)
            raise exporter.ScrapeError("GET %s -> HTTP 404" % url)
        if filename in self.scada:
            return self.scada[filename]
        if filename in self.isr:
            return self.isr[filename]
        raise exporter.ScrapeError("GET %s -> HTTP 404" % url)


def make_intraday(**kwargs):
    poll_interval = kwargs.pop("poll_interval", 300.0)
    now = kwargs.pop("now", NOW)
    poller = exporter.IntradayPoller(
        kwargs.pop("scada_url", SCADA_LISTING_URL),
        kwargs.pop("dispatchis_url", IS_LISTING_URL),
        kwargs.pop("timeout", 5.0),
        poll_interval,
        kwargs.pop("retries", 0),
        kwargs.pop("retry_budget", 0.0),
        battery_duids=kwargs.pop("battery_duids", None),
        storage_duids_fn=kwargs.pop("storage_duids_fn", None),
    )
    poller.now_fn = lambda: now
    return poller


def polled_intraday(fetcher, **kwargs):
    with mock.patch.object(exporter, "http_get", fetcher):
        poller = make_intraday(**kwargs)
        body, ok = poller.poll_once()
        return poller, body, ok


def make_daily_report(units_dict=None, file_name="PUBLIC_NEXT_DAY_DISPATCH_TEST.zip"):
    """Build a minimal daily report snapshot (like NextDayScraper.daily_report())."""
    if units_dict is None:
        units_dict = {}
    return {"file": file_name, "units": units_dict}


def lines_named(text, metric):
    return [l for l in text.splitlines() if l.startswith(metric + "{")]


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


class ParseAestTest(unittest.TestCase):
    def test_parses_a_fixed_utc_ten_stamp(self):
        self.assertEqual(exporter.parse_aest("2026/10/08 04:00:00"), HPR_TS)

    def test_oldest_interval_of_the_verified_file(self):
        self.assertEqual(exporter.parse_aest("2026/10/07 04:05:00"), OLD_TS)

    def test_publication_stamp_parses(self):
        self.assertEqual(exporter.parse_aest("2026/10/08 04:10:00"), PUBLISHED)

    def test_unparseable_returns_none_rather_than_raising(self):
        for value in ("", None, "not a date", 17, [], "2026-13-45 04:00:00",
                      "2026/10/08 99:00:00", "20261008 040000", "2026/10/08"):
            with self.subTest(value=value):
                self.assertIsNone(exporter.parse_aest(value))


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class ListingTest(unittest.TestCase):
    # The exporter's own contract is str: poll_once() decodes the fetched body
    # before it calls these. The tests decode the same way.
    def html(self, *names):
        return listing_with(*names).decode()

    def test_links_are_extracted_and_sorted(self):
        names = [
            "PUBLIC_NEXT_DAY_DISPATCH_20261006_0000000541730397.zip",
            "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip",
            "PUBLIC_NEXT_DAY_DISPATCH_20261007_0000000541730398.zip",
            "PUBLIC_NEXT_DAY_DISPATCH_20261007_0000000541730398.zip",
        ]
        found = exporter.list_report_files(self.html(*names))
        self.assertEqual(
            found,
            [
                "PUBLIC_NEXT_DAY_DISPATCH_20261006_0000000541730397.zip",
                "PUBLIC_NEXT_DAY_DISPATCH_20261007_0000000541730398.zip",
                "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip",
            ],
        )

    def test_distractor_links_are_ignored(self):
        html = (
            "<html><body>"
            '<A HREF="/Reports/CURRENT/Raw_IL_Settlement/RAW_X.zip">RAW_X.zip</A>'
            '<A HREF="/Reports/CURRENT/Next_Day_Dispatch/PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip">'
            "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip</A>"
            '<A HREF="https://other.example/PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip">x</A>'
            "</body></html>"
        )
        self.assertEqual(exporter.list_report_files(html), [ZIP_NAME])

    def test_newest_is_the_lexicographic_max(self):
        self.assertEqual(
            exporter.newest_report_file(self.html(
                "PUBLIC_NEXT_DAY_DISPATCH_20261007_0000000541730398.zip",
                "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip",
            )),
            ZIP_NAME,
        )

    def test_a_same_day_correction_is_the_newest(self):
        """A correction carries a larger publisher sequence number."""
        names = [
            "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730392.zip",
            "PUBLIC_NEXT_DAY_DISPATCH_20261008_0000000541730399.zip",
        ]
        self.assertEqual(exporter.newest_report_file(self.html(*names)), ZIP_NAME)

    def test_an_empty_listing_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.newest_report_file("<html><body></body></html>")


# --------------------------------------------------------------------------- #
# parse_next_day_dispatch
# --------------------------------------------------------------------------- #


class ParseTest(unittest.TestCase):
    def report(self):
        return parsed_report()

    def test_newest_interval_wins_per_unit_whatever_the_order(self):
        units = self.report()["units"]
        self.assertEqual(set(units), {"HPR1", "ERB01", "MLB01", "WALGRV1"})
        self.assertEqual(units["HPR1"]["ts"], HPR_TS)
        self.assertEqual(units["HPR1"]["initial"], 21.7)
        self.assertAlmostEqual(units["HPR1"]["energy"], 21.8625, places=4)
        self.assertEqual(units["HPR1"]["cleared"], 0)

    def test_cleared_is_negative_for_a_charging_unit(self):
        units = self.report()["units"]
        self.assertEqual(units["ERB01"]["ts"], HPR_TS)
        self.assertEqual(units["ERB01"]["cleared"], -8)
        self.assertAlmostEqual(units["ERB01"]["energy"], 128.0, places=4)

    def test_a_unit_stopping_early_keeps_its_last_row(self):
        """The 287-of-288 shape: an early stop must not zero the unit out."""
        self.assertEqual(self.report()["units"]["MLB01"]["energy"], 6.2)
        self.assertEqual(self.report()["units"]["MLB01"]["ts"], OLD_TS + 300.0)

    def test_cleared_none_when_the_field_is_blank(self):
        """WALGRV1 stops before the final interval and reports no clearing."""
        self.assertIsNone(self.report()["units"]["WALGRV1"]["cleared"])

    def test_generators_are_not_storage_units(self):
        self.assertNotIn("GEN01", self.report()["units"])

    def test_energy_falls_back_to_initial_when_blank(self):
        self.assertEqual(self.report()["units"]["WALGRV1"]["energy"], 9.9)

    def test_newest_interval_is_the_largest_stamp_seen(self):
        self.assertEqual(self.report()["newest_interval"], HPR_TS)

    def test_generated_comes_from_the_control_line(self):
        self.assertEqual(self.report()["generated"], PUBLISHED)

    def test_a_unit_reporting_only_an_early_interval_stays(self):
        """WALGRV1 only reports the penultimate interval; it keeps that row."""
        self.assertEqual(self.report()["units"]["WALGRV1"]["ts"], HPR_TS - 300.0)

    def test_non_numeric_storage_in_one_row_is_skipped_not_fatal(self):
        rows = sample_rows()
        rows.append(unit_solution_row("D", "2026/10/08 04:00:00", "BROKEN", "x", "y"))
        with self.assertLogs(exporter.log, level="WARNING"):
            report = parsed_report(rows)
        self.assertNotIn("BROKEN", report["units"])
        self.assertEqual(report["units"]["HPR1"]["ts"], HPR_TS)

    def test_bad_zip_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.parse_next_day_dispatch(b"not a zip at all")

    def test_zip_without_a_csv_member_is_a_scrape_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "hi")
        with self.assertRaises(exporter.ScrapeError):
            exporter.parse_next_day_dispatch(buf.getvalue())

    def test_no_storage_units_is_a_scrape_error(self):
        rows = [unit_solution_row("D", "2026/10/08 04:00:00", "GEN01", "", "")]
        with self.assertRaises(exporter.ScrapeError):
            parsed_report(rows)


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


class StateFileTest(unittest.TestCase):
    def test_round_trip(self):
        report = parsed_report()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "state.json")
            exporter.save_state(path, report)
            restored = exporter.load_state(path)
        self.assertEqual(restored["file"], report["file"])
        self.assertEqual(restored["newest_interval"], report["newest_interval"])
        self.assertEqual(restored["generated"], report["generated"])
        self.assertEqual(
            restored["units"]["HPR1"],
            {"ts": HPR_TS, "initial": 21.7, "energy": 21.8625, "cleared": 0.0},
        )

    def test_missing_file_returns_none(self):
        self.assertIsNone(exporter.load_state("/nonexistent/state.json"))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertIsNone(exporter.load_state(path))

    def test_state_without_units_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"file": "x.zip", "units": {}}, handle)
            self.assertIsNone(exporter.load_state(path))

    def test_bad_unit_entries_are_dropped(self):
        payload = {
            "file": "x.zip",
            "units": {
                "OK": {"ts": 100.0, "initial": 5.0, "energy": 6.0},
                "NOTYPE": "junk",
                "NEGATIVE": {"ts": 100.0, "initial": -5.0, "energy": 6.0},
                "BADTS": {"ts": 0.0, "initial": 5.0, "energy": 6.0},
                "NONUM": {"ts": 100.0, "initial": "x", "energy": 6.0},
            },
        }
        self.assertEqual(set(exporter._state_from_json(payload)["units"]), {"OK"})


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.report = listing_report()
        self.text = exporter.render(self.report, True, 1.5, self.now)

    def test_every_metric_has_help_and_type(self):
        typed = {l.split()[2] for l in self.text.splitlines() if l.startswith("# TYPE ")}
        helped = {l.split()[2] for l in self.text.splitlines() if l.startswith("# HELP ")}
        for metric in typed:
            self.assertIn(metric, helped, "no HELP for %s" % metric)

    def test_per_unit_help_and_type_appear_exactly_once(self):
        for name, help_text, _key in exporter.UNIT_GAUGE_SPECS:
            self.assertEqual(self.text.count("# HELP %s " % name), 1, name)
            self.assertEqual(self.text.count("# TYPE %s " % name), 1, name)
            self.assertEqual(self.text.count('%s{duid="' % name), 4, name)

    def test_per_unit_readings_carry_the_duid_label(self):
        line = next(
            l for l in lines_named(self.text, "aemo_battery_energy_stored_mwh")
            if 'duid="HPR1"' in l
        )
        self.assertAlmostEqual(float(line.rsplit(" ", 1)[1]), 21.8625, places=4)

    def test_total_cleared_carries_the_sign(self):
        erb = next(
            l for l in lines_named(self.text, "aemo_battery_total_cleared_mw")
            if 'duid="ERB01"' in l
        )
        self.assertEqual(float(erb.rsplit(" ", 1)[1]), -8)
        hpr = next(
            l for l in lines_named(self.text, "aemo_battery_total_cleared_mw")
            if 'duid="HPR1"' in l
        )
        self.assertEqual(float(hpr.rsplit(" ", 1)[1]), 0)

    def test_total_cleared_renders_nan_when_absent(self):
        report = listing_report()
        report["units"]["WALGRV1"]["cleared"] = None
        text = exporter.render(report, True, 0.0, self.now)
        line = next(
            l for l in lines_named(text, "aemo_battery_total_cleared_mw")
            if 'duid="WALGRV1"' in l
        )
        self.assertEqual(line.rsplit(" ", 1)[1], "NaN")

    def test_sample_age_is_computed_at_rendering_from_now(self):
        line = next(
            l for l in lines_named(self.text, "aemo_battery_sample_age_seconds")
            if 'duid="HPR1"' in l
        )
        self.assertAlmostEqual(float(line.rsplit(" ", 1)[1]), self.now - HPR_TS, places=3)

    def test_report_info_carries_the_file(self):
        self.assertIn('aemo_battery_report_info{file="%s"} 1' % ZIP_NAME, self.text)

    def test_report_generated_age_is_live(self):
        line = [l for l in self.text.splitlines()
                if l.startswith("aemo_battery_report_generated_age_seconds ")]
        self.assertAlmostEqual(float(line[0].rsplit(" ", 1)[1]), self.now - PUBLISHED, places=3)

    def test_units_reporting_counts_only_storage_units(self):
        self.assertIn("aemo_battery_units_reporting 4", self.text)

    def test_reused_is_zero_after_a_download(self):
        self.assertIn("aemo_battery_report_reused 0", self.text)

    def test_reused_is_one_for_a_reused_report(self):
        text = exporter.render(dict(self.report, reused=True), True, 0.0, self.now)
        self.assertIn("aemo_battery_report_reused 1", text)
        self.assertIn("aemo_battery_units_reporting 4", text)

    def test_failure_renders_no_per_unit_series(self):
        text = exporter.render({}, False, 2.0, self.now)
        self.assertIn("aemo_battery_scrape_success 0", text)
        self.assertEqual(lines_named(text, "aemo_battery_energy_stored_mwh"), [])
        self.assertIn("aemo_battery_units_reporting 0", text)

    def test_failure_renders_nothing_from_a_held_report(self):
        """The render-level guard: never print readings from a failed poll."""
        text = exporter.render(self.report, False, 2.0, self.now)
        self.assertEqual(lines_named(text, "aemo_battery_energy_stored_mwh"), [])
        self.assertIn("aemo_battery_scrape_success 0", text)

    def test_duids_are_escaped(self):
        report = listing_report()
        report["units"]["A\"B"] = {"ts": HPR_TS, "initial": 1.0, "energy": 2.0}
        text = exporter.render(report, True, 0.0, self.now)
        self.assertIn('aemo_battery_energy_stored_mwh{duid="A\\"B"} 2', text)


class FmtTest(unittest.TestCase):
    def test_none_is_nan(self):
        self.assertEqual(exporter.fmt(None), "NaN")

    def test_integers_are_not_dressed_up(self):
        self.assertEqual(exporter.fmt(21.0), "21")
        self.assertEqual(exporter.fmt(1), "1")

    def test_specials(self):
        self.assertEqual(exporter.fmt(float("nan")), "NaN")
        self.assertEqual(exporter.fmt(float("inf")), "+Inf")
        self.assertEqual(exporter.fmt(float("-inf")), "-Inf")

    def test_escape_label(self):
        self.assertEqual(exporter.escape_label('A"B\\C\nD'), 'A\\"B\\\\C\\nD')


# --------------------------------------------------------------------------- #
# Scraper poll
# --------------------------------------------------------------------------- #


class ScraperTest(unittest.TestCase):
    def zip_fetcher(self):
        return FakeFetcher(zips={ZIP_NAME: SAMPLE_ZIP})

    def zip_fetches(self, fetcher):
        return sum(1 for url in fetcher.fetches if url.endswith(".zip"))

    def test_first_poll_downloads_and_publishes(self):
        fetcher = self.zip_fetcher()
        scraper, body, ok = polled(fetcher)
        self.assertTrue(ok)
        self.assertEqual(self.zip_fetches(fetcher), 1)
        self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"', body)
        self.assertIn("aemo_battery_report_reused 0", body)
        self.assertEqual(scraper.health(), (True, ""))
        self.assertEqual(scraper._state["file"], ZIP_NAME)

    def test_unchanged_listing_is_served_without_a_redownload(self):
        """The second cycle re-reads the listing but downloads the zip again
        never - the listing IS the verification, and that is what is asserted."""
        fetcher = self.zip_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            scraper = make_scraper()
            body, ok = scraper.poll_once()
            self.assertTrue(ok)
            fetcher.fetches.clear()
            body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertEqual(self.zip_fetches(fetcher), 0)
        self.assertGreaterEqual(len(fetcher.fetches), 1)  # the listing was re-read
        self.assertIn("aemo_battery_report_reused 1", body)
        self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"', body)

    def test_a_new_file_is_downloaded_when_the_listing_advances(self):
        newer = "PUBLIC_NEXT_DAY_DISPATCH_20261009_0000000541730400.zip"
        newer_csv = "PUBLIC_NEXT_DAY_DISPATCH_20261009_0000000541730400.CSV"
        fetcher = FakeFetcher(
            zips={
                ZIP_NAME: SAMPLE_ZIP,
                newer: report_zip(
                    name=newer_csv,
                    rows=[unit_solution_row("D", "2026/10/09 04:00:00", "HPR1", "30.0", "31.0")],
                ),
            },
            listing_names=[ZIP_NAME],
        )
        with mock.patch.object(exporter, "http_get", fetcher):
            first = make_scraper()
            first.poll_once()
            fetcher.listing_names[:] = [newer]
            body, ok = first.poll_once()
        self.assertTrue(ok)
        self.assertEqual(self.zip_fetches(fetcher), 2)
        self.assertIn('aemo_battery_report_info{file="%s"} 1' % newer, body)
        self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"} 31', body)

    def test_a_whole_cycle_costs_the_listing_plus_one_zip(self):
        fetcher = self.zip_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            make_scraper().poll_once()
        self.assertEqual(fetcher.fetches, [DATA_URL, DATA_URL + ZIP_NAME])

    def test_failure_publishes_no_series_and_marks_unhealthy(self):
        fetcher = FakeFetcher(error=exporter.ScrapeError("GET ... -> HTTP 500"))
        scraper, body, ok = polled(fetcher)
        self.assertFalse(ok)
        self.assertIn("aemo_battery_scrape_success 0", body)
        self.assertNotIn('duid="HPR1"', body)
        self.assertFalse(scraper.health()[0])

    def test_a_failure_after_a_success_drops_the_stale_series(self):
        fetcher = self.zip_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            scraper = make_scraper()
            scraper.poll_once()
            fetcher.error = exporter.ScrapeError("GET ... -> HTTP 500")
            body, ok = scraper.poll_once()
            exposed, healthy = scraper.exposition()
        self.assertFalse(ok)
        self.assertFalse(healthy)
        self.assertNotIn("aemo_battery_energy_stored_mwh{", body)
        self.assertNotIn("aemo_battery_energy_stored_mwh{", exposed)

    def test_state_file_survives_a_restart_without_a_redownload(self):
        """The recovery shape: a fresh process, state file on disk, live listing
        still naming the same file - served, never re-downloaded."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(exporter, "http_get", self.zip_fetcher()):
                make_scraper(state_file=path).poll_once()
            self.assertTrue(os.path.exists(path))

            fetcher = FakeFetcher(fail_zips=True)  # the zip must not be needed again
            with mock.patch.object(exporter, "http_get", fetcher):
                restarted = make_scraper(state_file=path)
                body, ok = restarted.poll_once()
            self.assertTrue(ok)
            self.assertIn("aemo_battery_report_reused 1", body)
            self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"', body)
            self.assertEqual(self.zip_fetches(fetcher), 0)

    def test_a_state_file_for_a_different_file_is_not_reused(self):
        """A stale state file must not be served against a newer listing.

        The newer file is unresolveable here, so the poll fails - which is the
        point: it attempted a real download rather than quietly re-serving a
        report the publisher has already replaced.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(exporter, "http_get", self.zip_fetcher()):
                make_scraper(state_file=path).poll_once()

            fetcher = FakeFetcher(
                zips={},  # the newly published file is not fetchable
                listing_names=["PUBLIC_NEXT_DAY_DISPATCH_20261009_0000000541730400.zip"],
            )
            with mock.patch.object(exporter, "http_get", fetcher):
                restarted = make_scraper(state_file=path)
                body, ok = restarted.poll_once()
            self.assertFalse(ok)
            # It tried to fetch the new file; it never served the stale report.
            self.assertEqual(self.zip_fetches(fetcher), 1)
            self.assertNotIn('duid="HPR1"', body)
            self.assertIn("aemo_battery_report_reused 0", body)

    def test_exposition_serves_the_last_cycle(self):
        fetcher = self.zip_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            scraper = make_scraper()
            scraper.poll_once()
            body, ok = scraper.exposition()
        self.assertTrue(ok)
        self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"', body)


# --------------------------------------------------------------------------- #
# Intraday listings
# --------------------------------------------------------------------------- #


class IntradayListingTest(unittest.TestCase):
    def scada_html(self, *names):
        return listing_html("/Reports/CURRENT/Dispatch_SCADA/", *names).decode()

    def is_html(self, *names):
        return listing_html("/Reports/CURRENT/DispatchIS_Reports/", *names).decode()

    def test_scada_links_are_found_and_lexicographic_max_is_newest(self):
        names = [
            "PUBLIC_DISPATCHSCADA_202610081515_0000000541811991.zip",
            "PUBLIC_DISPATCHSCADA_202610081520_0000000541812607.zip",
            "PUBLIC_DISPATCHSCADA_202610081515_0000000541812322.zip",
        ]
        self.assertEqual(
            exporter.newest_scada_file(self.scada_html(*names)),
            "PUBLIC_DISPATCHSCADA_202610081520_0000000541812607.zip",
        )

    def test_dispatchis_links_are_found_and_newest(self):
        names = [
            "PUBLIC_DISPATCHIS_202610081515_0000000541811979.zip",
            "PUBLIC_DISPATCHIS_202610081520_0000000541812602.zip",
        ]
        self.assertEqual(
            exporter.newest_dispatchis_file(self.is_html(*names)),
            IS_ZIP,
        )

    def test_an_empty_scada_listing_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.newest_scada_file("<html><body></body></html>")

    def test_an_empty_dispatchis_listing_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.newest_dispatchis_file("<html><body></body></html>")


# --------------------------------------------------------------------------- #
# parse_dispatch_scada
# --------------------------------------------------------------------------- #


class ParseScadaTest(unittest.TestCase):
    def parsed(self, rows=None):
        return exporter.parse_dispatch_scada(scada_zip(rows=rows or scada_sample_rows()))

    def test_measured_output_carries_the_sign(self):
        units = self.parsed()["units"]
        self.assertEqual(units["WTAHB1"]["scada"], -34.07263)
        self.assertEqual(units["ERB01"]["scada"], -120.00916)
        self.assertAlmostEqual(units["LIMBESS1"]["scada"], 0.897230, places=4)

    def test_the_interval_stamp_is_carried(self):
        units = self.parsed()["units"]
        self.assertEqual(units["ERB01"]["ts"], SCADA_TS)
        self.assertEqual(self.parsed()["newest_interval"], SCADA_TS)

    def test_a_later_row_for_the_same_unit_wins(self):
        rows = scada_sample_rows()
        rows.append(scada_row("D", "2026/10/08 15:25:00", "ERB01", "5.0"))
        parsed = self.parsed(rows)
        self.assertEqual(parsed["units"]["ERB01"]["scada"], 5.0)
        self.assertEqual(parsed["units"]["ERB01"]["ts"], SCADA_TS + 300.0)
        self.assertEqual(parsed["newest_interval"], SCADA_TS + 300.0)

    def test_non_numeric_scada_is_skipped_not_fatal(self):
        rows = scada_sample_rows()
        rows.append(scada_row("D", duid="BROKEN", value="x"))
        with self.assertLogs(exporter.log, level="WARNING"):
            parsed = self.parsed(rows)
        self.assertNotIn("BROKEN", parsed["units"])
        self.assertEqual(parsed["units"]["WTAHB1"]["scada"], -34.07263)

    def test_bad_zip_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.parse_dispatch_scada(b"not a zip")

    def test_zip_without_a_csv_member_is_a_scrape_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "hi")
        with self.assertRaises(exporter.ScrapeError):
            exporter.parse_dispatch_scada(buf.getvalue())

    def test_no_scada_rows_is_a_scrape_error(self):
        with self.assertRaises(exporter.ScrapeError):
            exporter.parse_dispatch_scada(scada_zip(rows=[]))


# --------------------------------------------------------------------------- #
# parse_dispatchis_region_storage
# --------------------------------------------------------------------------- #


class ParseDispatchIsTest(unittest.TestCase):
    def parsed(self, rows=None):
        return exporter.parse_dispatchis_region_storage(is_zip(rows=rows or is_sample_rows()))

    def test_region_storage_readings(self):
        regions = self.parsed()["regions"]
        self.assertEqual(set(regions), {"NSW1", "QLD1", "SA1", "VIC1"})
        self.assertAlmostEqual(regions["NSW1"]["initial"], 5063.36883, places=4)
        self.assertAlmostEqual(regions["NSW1"]["energy"], 5084.59164, places=4)
        self.assertEqual(regions["NSW1"]["ts"], SCADA_TS)

    def test_a_region_without_storage_is_skipped(self):
        """TAS1 reports blank BDU columns in the real file and must be skipped."""
        self.assertNotIn("TAS1", self.parsed()["regions"])

    def test_energy_falls_back_to_initial_when_blank(self):
        rows = [region_sum_row("D", "2026/10/08 15:20:00", "NSW1", "5000.0", "")]
        self.assertEqual(self.parsed(rows)["regions"]["NSW1"]["energy"], 5000.0)

    def test_latest_stamp_wins_per_region(self):
        rows = [
            region_sum_row("D", "2026/10/08 15:20:00", "NSW1", "5000.0", "5100.0"),
            region_sum_row("D", "2026/10/08 15:25:00", "NSW1", "5200.0", "5300.0"),
        ]
        regions = self.parsed(rows)["regions"]
        self.assertEqual(regions["NSW1"]["energy"], 5300.0)
        self.assertEqual(self.parsed(rows)["newest_interval"], SCADA_TS + 300.0)

    def test_no_regions_is_a_scrape_error(self):
        rows = [region_sum_row("D", "2026/10/08 15:20:00", "TAS1", "", "")]
        with self.assertRaises(exporter.ScrapeError):
            self.parsed(rows)


# --------------------------------------------------------------------------- #
# render_intraday
# --------------------------------------------------------------------------- #


class RenderIntradayTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW + 40000.0  # well past the intraday interval age
        self.scada = exporter.parse_dispatch_scada(SCADA_SAMPLE_ZIP)
        self.scada["file"] = SCADA_ZIP
        self.isr = exporter.parse_dispatchis_region_storage(IS_SAMPLE_ZIP)
        self.isr["file"] = IS_ZIP

    def text(self):
        return exporter.render_intraday(self.scada, True, self.isr, True, 1.5, self.now)

    def test_every_metric_has_help_and_type(self):
        text = self.text()
        typed = {l.split()[2] for l in text.splitlines() if l.startswith("# TYPE ")}
        helped = {l.split()[2] for l in text.splitlines() if l.startswith("# HELP ")}
        self.assertLessEqual(typed, helped)

    def test_power_series_render_with_the_sign(self):
        text = self.text()
        erb = next(
            l for l in lines_named(text, "aemo_battery_power_mw")
            if 'duid="ERB01"' in l
        )
        self.assertAlmostEqual(float(erb.rsplit(" ", 1)[1]), -120.00916, places=4)
        lim = next(
            l for l in lines_named(text, "aemo_battery_power_mw")
            if 'duid="LIMBESS1"' in l
        )
        self.assertAlmostEqual(float(lim.rsplit(" ", 1)[1]), 0.897230, places=4)

    def test_power_age_is_computed_from_now(self):
        text = self.text()
        line = next(
            l for l in lines_named(text, "aemo_battery_power_age_seconds")
            if 'duid="ERB01"' in l
        )
        self.assertAlmostEqual(float(line.rsplit(" ", 1)[1]), self.now - SCADA_TS, places=3)

    def test_region_series_carry_the_region_label(self):
        text = self.text()
        nsw = next(
            l for l in text.splitlines()
            if l.startswith('aemo_battery_region_stored_mwh{region="NSW1"} ')
        )
        self.assertAlmostEqual(float(nsw.rsplit(" ", 1)[1]), 5084.59164, places=4)
        self.assertNotIn('region="TAS1"', text)

    def test_success_gauges_report_the_split(self):
        text = self.text()
        self.assertIn("aemo_battery_scada_success 1", text)
        self.assertIn("aemo_battery_dispatchis_success 1", text)
        self.assertIn("aemo_battery_units_with_power 4", text)

    def test_a_failed_scada_read_omits_power_but_keeps_regions(self):
        text = exporter.render_intraday(self.scada, False, self.isr, True, 1.5, self.now)
        self.assertIn("aemo_battery_scada_success 0", text)
        self.assertEqual(lines_named(text, "aemo_battery_power_mw"), [])
        self.assertIn('aemo_battery_region_stored_mwh{region="NSW1"}', text)

    def test_a_failed_dispatchis_read_omits_regions_but_keeps_power(self):
        text = exporter.render_intraday(self.scada, True, self.isr, False, 1.5, self.now)
        self.assertIn("aemo_battery_dispatchis_success 0", text)
        self.assertNotIn("aemo_battery_region_stored_mwh", text)
        self.assertIn('aemo_battery_power_mw{duid="ERB01"}', text)

    def test_reports_info_carries_the_processed_files(self):
        text = self.text()
        self.assertIn('aemo_battery_power_report_info{file="%s"} 1' % SCADA_ZIP, text)
        self.assertIn('aemo_battery_region_storage_report_info{file="%s"} 1' % IS_ZIP, text)


# --------------------------------------------------------------------------- #
# Intraday poller
# --------------------------------------------------------------------------- #


class IntradayPollerTest(unittest.TestCase):
    def battery_fetcher(self):
        return IntradayFetcher(
            scada_zips={SCADA_ZIP: SCADA_SAMPLE_ZIP},
            is_zips={IS_ZIP: IS_SAMPLE_ZIP},
        )

    def zip_fetches(self, fetcher):
        return sum(1 for url in fetcher.fetches if url.endswith(".zip"))

    def test_first_poll_downloads_and_filters_scada_to_batteries(self):
        duids = {"WTAHB1", "ERB01", "LIMBESS1"}
        fetcher = self.battery_fetcher()
        poller, body, ok = polled_intraday(fetcher, battery_duids=duids)
        self.assertTrue(ok)
        self.assertEqual(self.zip_fetches(fetcher), 2)  # one SCADA + one DispatchIS zip
        self.assertEqual(len([l for l in body.splitlines() if l.startswith('aemo_battery_power_mw{')]), 3)
        self.assertIn('aemo_battery_power_mw{duid="ERB01"} -120.0092', body)
        self.assertNotIn('duid="DPNTB1"', body)
        self.assertIn('aemo_battery_region_stored_mwh{region="NSW1"}', body)
        self.assertIn("aemo_battery_scada_success 1", body)

    def test_batteries_are_learned_from_the_daily_scraper(self):
        fetcher = self.battery_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            poller = make_intraday(
                battery_duids=set(),
                storage_duids_fn=lambda: {"WTAHB1", "ERB01"},
            )
            body, ok = poller.poll_once()
        self.assertTrue(ok)
        self.assertEqual(len([l for l in body.splitlines() if l.startswith('aemo_battery_power_mw{')]), 2)

    def test_no_batteries_known_yet_exports_no_power_series(self):
        poller, body, ok = polled_intraday(self.battery_fetcher(), battery_duids=set())
        self.assertTrue(ok)
        self.assertNotIn("aemo_battery_power_mw{", body)
        self.assertIn("aemo_battery_scada_success 1", body)
        self.assertIn('aemo_battery_region_stored_mwh{region="NSW1"}', body)

    def test_an_unchanged_file_is_served_without_a_redownload(self):
        fetcher = self.battery_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            poller = make_intraday(battery_duids={"WTAHB1"})
            poller.poll_once()
            fetcher.fetches.clear()
            body, ok = poller.poll_once()
        self.assertTrue(ok)
        self.assertEqual(self.zip_fetches(fetcher), 0)
        self.assertGreaterEqual(len(fetcher.fetches), 2)  # both listings re-read
        self.assertIn('aemo_battery_power_mw{duid="WTAHB1"}', body)

    def test_a_publisher_advance_downloads_the_new_file(self):
        newer = "PUBLIC_DISPATCHSCADA_202610081525_0000000541813358.zip"
        newer_csv = "PUBLIC_DISPATCHSCADA_202610081525_0000000541813358.CSV"
        fetcher = IntradayFetcher(
            scada_zips={
                SCADA_ZIP: SCADA_SAMPLE_ZIP,
                newer: scada_zip(
                    name=newer_csv,
                    rows=[scada_row("D", "2026/10/08 15:25:00", "ERB01", "5.0")],
                ),
            },
            is_zips={IS_ZIP: IS_SAMPLE_ZIP},
        )
        with mock.patch.object(exporter, "http_get", fetcher):
            poller = make_intraday(battery_duids={"ERB01"})
            poller.poll_once()
            fetcher.scada_names[:] = [newer]
            body, ok = poller.poll_once()
        self.assertTrue(ok)
        scada_downloads = sum(
            1 for url in fetcher.fetches
            if "/Dispatch_SCADA/" in url and url.endswith(".zip")
        )
        self.assertEqual(scada_downloads, 2)
        self.assertIn('aemo_battery_power_mw{duid="ERB01"} 5', body)

    def test_a_failed_scada_read_keeps_regions_but_drops_power(self):
        fetcher = self.battery_fetcher()
        fetcher.scada_error = exporter.ScrapeError("GET ... -> HTTP 500")
        poller, body, ok = polled_intraday(fetcher, battery_duids={"WTAHB1"})
        self.assertFalse(ok)
        self.assertIn("aemo_battery_scada_success 0", body)
        self.assertNotIn("aemo_battery_power_mw{", body)
        self.assertIn("aemo_battery_dispatchis_success 1", body)
        self.assertIn('aemo_battery_region_stored_mwh{region="NSW1"}', body)

    def test_a_failed_dispatchis_read_keeps_power_but_drops_regions(self):
        fetcher = self.battery_fetcher()
        fetcher.is_error = exporter.ScrapeError("GET ... -> HTTP 500")
        poller, body, ok = polled_intraday(fetcher, battery_duids={"ERB01"})
        self.assertFalse(ok)
        self.assertIn("aemo_battery_dispatchis_success 0", body)
        self.assertNotIn("aemo_battery_region_stored_mwh", body)
        self.assertIn('aemo_battery_power_mw{duid="ERB01"}', body)

    def test_exposition_serves_the_last_cycle(self):
        fetcher = self.battery_fetcher()
        with mock.patch.object(exporter, "http_get", fetcher):
            poller = make_intraday(battery_duids={"WTAHB1"})
            poller.poll_once()
            body, ok = poller.exposition()
        self.assertTrue(ok)
        self.assertIn('aemo_battery_power_mw{duid="WTAHB1"}', body)
        self.assertIn("aemo_battery_scada_success 1", body)


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


class HandlerTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(exporter, "http_get", FakeFetcher(zips={ZIP_NAME: SAMPLE_ZIP})):
            self.scraper = make_scraper()
            self.scraper.poll_once()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), exporter.make_handler(self.scraper)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5.0)

    def test_metrics(self):
        with self.get("/metrics") as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("text/plain; version=0.0.4", response.headers["Content-Type"])
            self.assertIn('aemo_battery_energy_stored_mwh{duid="HPR1"', body)

    def test_root_serves_an_index(self):
        with self.get("/") as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("/metrics", body)

    def test_healthz_ok_when_polled(self):
        with self.get("/healthz") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok\n")

    def test_healthz_503_before_any_poll(self):
        scraper = make_scraper()
        server = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(scraper))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/healthz" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(url, timeout=5.0)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_metrics_includes_the_intraday_series(self):
        intraday = exporter.IntradayPoller(
            SCADA_LISTING_URL, IS_LISTING_URL, 5.0, 300.0,
            battery_duids={"WTAHB1", "ERB01"},
        )
        fetcher = IntradayFetcher(
            scada_zips={SCADA_ZIP: SCADA_SAMPLE_ZIP},
            is_zips={IS_ZIP: IS_SAMPLE_ZIP},
        )
        with mock.patch.object(exporter, "http_get", fetcher):
            intraday.poll_once()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), exporter.make_handler(self.scraper, intraday)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with urllib.request.urlopen(base + "/metrics", timeout=5.0) as response:
                body = response.read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)
        self.assertIn("aemo_battery_scada_success 1", body)
        self.assertIn('aemo_battery_power_mw{duid="ERB01"}', body)
        self.assertIn("aemo_battery_report_info{", body)  # the daily side is still served


# --------------------------------------------------------------------------- #
# Inference (dead-reckoned stored MWh)
# --------------------------------------------------------------------------- #


class InferenceTest(unittest.TestCase):
    def setUp(self):
        self.daily = make_daily_report(
            {
                "ERB01": {"ts": 1791396000.0, "initial": 130.0, "energy": 130.0},  # 04:00:00+10
                "LIMBESS1": {"ts": 1791396000.0, "initial": 50.0, "energy": 50.0},
            }
        )

    def scada_at(self, duid, value_mw, ts):
        return {"file": SCADA_ZIP, "units": {duid: {"ts": ts, "scada": float(value_mw)}}}

    def test_discharge_deducts_full_and_charging_applies_efficiency(self):
        poller = make_intraday(battery_duids={"ERB01", "LIMBESS1"})
        poller._daily_report_fn = lambda: self.daily
        poller.charge_efficiency = 0.9
        # ERB01 discharges 10 MW for 0.5h -> -5.0 MWh
        poller._advance_inference(self.scada_at("ERB01", 10.0, 1791396000.0 + 1800.0)["units"], NOW)
        # LIMBESS1 charges 10 MW for 0.5h -> +10*0.5*0.9 = +4.5 MWh (power negative in SCADA; convention: power<0 charging)
        poller._advance_inference(self.scada_at("LIMBESS1", -10.0, 1791396000.0 + 1800.0)["units"], NOW)
        v = poller._infer_view(NOW)
        self.assertAlmostEqual(v["units"]["ERB01"]["mwh"], 125.0, places=4)
        self.assertAlmostEqual(v["units"]["LIMBESS1"]["mwh"], 54.5, places=4)
        self.assertEqual(v["file"], self.daily["file"])

    def test_reanchor_on_daily_file_change(self):
        poller = make_intraday(battery_duids={"ERB01"})
        poller._daily_report_fn = lambda: self.daily
        poller._advance_inference(self.scada_at("ERB01", 10.0, 1791396000.0 + 1800.0)["units"], NOW)
        v1 = poller._infer_view(NOW)
        self.assertAlmostEqual(v1["units"]["ERB01"]["mwh"], 125.0, places=4)
        daily2 = make_daily_report(
            {"ERB01": {"ts": 1791396000.0 + 300.0, "initial": 100.0, "energy": 100.0}},
            file_name="NEXT2.zip",
        )
        poller._daily_report_fn = lambda: daily2
        poller._advance_inference(self.scada_at("ERB01", 0.0, 1791396000.0 + 300.0)["units"], NOW)
        v2 = poller._infer_view(NOW)
        self.assertEqual(v2["file"], "NEXT2.zip")
        self.assertAlmostEqual(v2["units"]["ERB01"]["mwh"], 100.0, places=4)

    def test_first_sighting_anchors_from_daily_when_known(self):
        poller = make_intraday(battery_duids={"ERB01"})
        poller._daily_report_fn = lambda: self.daily
        poller._advance_inference(self.scada_at("ERB01", 5.0, 1791396000.0 + 600.0)["units"], NOW)
        v = poller._infer_view(NOW)
        # First sighting anchors at the daily ENERGY_STORAGE (and may have
        # integrated the current interval; the test accepts the anchored value).
        self.assertAlmostEqual(v["units"]["ERB01"]["mwh"], 129.1667, places=4)

    def test_large_gap_drops_unit_until_reanchor(self):
        poller = make_intraday(battery_duids={"ERB01"})
        poller._daily_report_fn = lambda: self.daily
        poller._advance_inference(self.scada_at("ERB01", 10.0, 1791396000.0 + 1800.0)["units"], NOW)
        self.assertIn("ERB01", poller._infer_view(NOW)["units"])
        gap_ts = 1791396000.0 + 1800.0 + (6.1 * 3600.0)
        poller._advance_inference(self.scada_at("ERB01", 0.0, gap_ts)["units"], NOW)
        self.assertNotIn("ERB01", poller._infer_view(NOW)["units"])

    def test_render_intraday_includes_inferred_metrics(self):
        scada = exporter.parse_dispatch_scada(SCADA_SAMPLE_ZIP)
        scada["file"] = SCADA_ZIP
        isr = exporter.parse_dispatchis_region_storage(IS_SAMPLE_ZIP)
        isr["file"] = IS_ZIP
        infer = {
            "file": "DAILY.zip",
            "units": {
                "ERB01": {"ts": SCADA_TS, "mwh": 128.5},
                "LIMBESS1": {"ts": SCADA_TS, "mwh": 51.2},
            },
            "efficiency": 0.9,
        }
        now = NOW + 40000.0
        text = exporter.render_intraday(scada, True, isr, True, 1.5, now, infer, True)
        self.assertIn("aemo_battery_inferred_stored_mwh{duid=\"ERB01\"} 128.5", text)
        self.assertIn("aemo_battery_inferred_units 2", text)
        self.assertIn("aemo_battery_infer_success 1", text)
        self.assertIn("aemo_battery_infer_charge_efficiency 0.9", text)
        self.assertIn('aemo_battery_inferred_report_info{file="DAILY.zip"} 1', text)


if __name__ == "__main__":
    unittest.main()