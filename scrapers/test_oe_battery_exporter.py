#!/usr/bin/env python3
"""Tests for oe_battery_exporter.py.

Offline: every upstream response is served from scrapers/fixtures/, sampled
verbatim from api.openelectricity.org.au on 2026-09-30 and 2026-10-01. No test
touches the network. The `/me` fixture has the account's name, email and key id
redacted - it is a committed file and the real ones were not.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_oe_battery_exporter.py

`-t scrapers` is required, for the same reason as the sibling tests: scrapers/
has no __init__.py so the exporter stays a single bind-mounted file.

The expectations are the upstream's own numbers read out of the fixtures, not
values invented next to the parser - Eraring is 1997 MWh with a newest
published sample of 334.768342 MWh, and the tests assert against exactly those.

The tests concentrate on the four upstream quirks, because each one produces a
plausible wrong number rather than an obvious failure:

  1. G1/L1 series have no capacity, so summing them triples the fleet.
  2. The aggregate endpoint is a flat array with no unit attribution.
  3. The feed is legitimately sparse, so nulls must not read as 0%.
  4. Facilities 404, and WEM facilities 404 when queried as NEM.

Two more properties are pinned by tests rather than by inspection, because
neither shows up as a wrong-looking number on its own: `storage_battery` and
`power` arrive in one request (the budget is counted in requests, so a second
call per battery would double it), and they age independently - the real
fixture has energy seven hours older than power for the same battery.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oe_battery_exporter as exporter  # noqa: E402
from oe_battery_exporter import (  # noqa: E402
    AuthError,
    ScrapeError,
    infer_soc,
    trapezoid,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Read out of oe_facilities_battery.json.
ERB_CAPACITY_MWH = 1997.0
WTAHB_CAPACITY_MWH = 1680.0
# Read out of oe_storage_battery_ERB.json: the newest non-null sample on the
# ERB01 series, and its timestamp.
ERB_NEWEST_MWH = 334.768342
ERB_NEWEST_TS = "2026-09-29T23:00:00+10:00"
# Read out of oe_metrics_ERB.json, which is a real two-metric response captured
# at 11:53 on 2026-10-01. It is the interesting shape: the overnight energy
# series stops at 04:00 while the power series runs to 11:00, so the two are
# seven hours apart in one payload.
COMBINED_NEWEST_MWH = 124.1611
COMBINED_NEWEST_TS = "2026-10-01T04:00:00+10:00"
COMBINED_POWER_MW = -129.73099
COMBINED_POWER_TS = "2026-10-01T11:00:00+10:00"
# The two largest units in the whole fleet, both of which never dispatched.
RICHMOND_CAPACITY_MWH = 2200.0
TOMAGO_CAPACITY_MWH = 2000.0


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def fixture_json(name: str):
    return json.loads(fixture(name))


def facilities() -> list:
    return fixture_json("oe_facilities_battery.json")["data"]


def storage_erb() -> dict:
    return fixture_json("oe_storage_battery_ERB.json")


def metrics_erb() -> dict:
    return fixture_json("oe_metrics_ERB.json")


# --------------------------------------------------------------------------- #
# is_transient
# --------------------------------------------------------------------------- #


def transient_reason() -> OSError:
    return socket.gaierror(socket.EAI_AGAIN, "Try again")


def permanent_reason() -> OSError:
    return OSError(errno.EACCES, "Permission denied")


class IsTransientTest(unittest.TestCase):
    def test_timeout_and_connection_exceptions_are_transient(self):
        for reason in (TimeoutError(), ConnectionResetError(), ConnectionAbortedError()):
            with self.subTest(reason=type(reason).__name__):
                self.assertTrue(exporter.is_transient(reason))

    def test_dns_again_is_transient(self):
        self.assertTrue(exporter.is_transient(transient_reason()))

    def test_permission_denied_is_not_transient(self):
        self.assertFalse(exporter.is_transient(permanent_reason()))

    def test_plain_object_is_not_transient(self):
        self.assertFalse(exporter.is_transient(object()))


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


class TimestampTest(unittest.TestCase):
    def test_parses_the_offset_the_api_actually_sends(self):
        self.assertEqual(
            exporter.parse_timestamp(ERB_NEWEST_TS),
            exporter.parse_timestamp("2026-09-29T13:00:00+00:00"),
        )

    def test_naive_timestamps_are_read_as_fixed_aest(self):
        self.assertEqual(
            exporter.parse_timestamp("2026-09-29T23:00:00"),
            exporter.parse_timestamp(ERB_NEWEST_TS),
        )

    def test_z_suffix_is_accepted(self):
        self.assertEqual(
            exporter.parse_timestamp("2026-09-29T13:00:00Z"),
            exporter.parse_timestamp(ERB_NEWEST_TS),
        )

    def test_bare_date_is_accepted(self):
        self.assertIsNotNone(exporter.parse_timestamp("2026-09-29"))

    def test_unparseable_returns_none_rather_than_raising(self):
        for value in ("", None, "not a date", 17, "2026-13-45"):
            with self.subTest(value=value):
                self.assertIsNone(exporter.parse_timestamp(value))

    def test_iso_local_round_trips(self):
        epoch = exporter.parse_timestamp(ERB_NEWEST_TS)
        self.assertEqual(exporter.parse_timestamp(exporter.iso_local(epoch)), epoch)

    def test_iso_local_sends_no_offset(self):
        # The API takes naive local strings; sending "+10:00" would be a
        # different request shape than the one that works.
        self.assertNotIn("+", exporter.iso_local(1790686800.0))
        self.assertEqual(exporter.iso_local(1790686800.0), "2026-09-29T23:00:00")


# --------------------------------------------------------------------------- #
# latest_sample
# --------------------------------------------------------------------------- #


class LatestSampleTest(unittest.TestCase):
    def test_picks_the_newest_non_null(self):
        points = [
            ["2026-09-29T18:00:00+10:00", 1262.2],
            ["2026-09-29T23:00:00+10:00", 666.6],
            ["2026-09-30T02:00:00+10:00", 527.8],
        ]
        stamp, value = exporter.latest_sample(points)
        self.assertEqual(value, 527.8)
        self.assertEqual(exporter.parse_timestamp("2026-09-30T02:00:00+10:00"), stamp)

    def test_nulls_are_skipped_not_read_as_zero(self):
        # quirk 3. A null means "not published", and reading it as 0 would
        # report a charged battery as flat and empty.
        points = [
            ["2026-09-29T23:00:00+10:00", 666.6],
            ["2026-09-30T00:00:00+10:00", None],
            ["2026-09-30T01:00:00+10:00", None],
        ]
        self.assertEqual(exporter.latest_sample(points)[1], 666.6)

    def test_all_null_returns_none(self):
        self.assertIsNone(
            exporter.latest_sample(
                [["2026-09-29T23:00:00+10:00", None], ["2026-09-30T00:00:00+10:00", None]]
            )
        )

    def test_junk_rows_are_ignored(self):
        points = [
            ["2026-09-29T23:00:00+10:00", 5.0],
            ["bad-timestamp", 9.0],
            [None, 9.0],
            ["2026-09-29T22:00:00+10:00", "seven"],
            ["2026-09-29T21:00:00+10:00", True],
            ["2026-09-29T20:00:00+10:00", float("nan")],
            ["2026-09-29T19:00:00+10:00", float("inf")],
            ["2026-09-29T18:00:00+10:00"],
        ]
        self.assertEqual(exporter.latest_sample(points)[1], 5.0)

    def test_empty_and_missing(self):
        self.assertIsNone(exporter.latest_sample([]))
        self.assertIsNone(exporter.latest_sample(None))


# --------------------------------------------------------------------------- #
# unit_rows
# --------------------------------------------------------------------------- #


class UnitRowsTest(unittest.TestCase):
    def test_drops_undispatched_units_by_default(self):
        """The largest batteries in the fleet are mostly unbuilt.

        Without this filter the top ten is seven `committed` projects that
        return no data, and Eraring/Waratah/Orana fall out of scope entirely.
        """
        rows = exporter.unit_rows(facilities())
        codes = {r["facility"] for r in rows}
        self.assertNotIn("RICHMOND", codes)
        self.assertNotIn("TOMAGOBESS", codes)
        self.assertIn("ERB", codes)

    def test_include_undispatched_restores_them(self):
        rows = exporter.unit_rows(facilities(), require_data=False)
        codes = {r["facility"] for r in rows}
        self.assertIn("RICHMOND", codes)
        self.assertIn("TOMAGOBESS", codes)
        self.assertEqual(len(rows), 119)

    def test_counts(self):
        self.assertEqual(len(exporter.unit_rows(facilities())), 74)
        self.assertEqual(len(exporter.unit_rows(facilities(), False)), 119)

    def test_eraring_carries_its_own_capacity_not_its_facilitys(self):
        row = next(r for r in exporter.unit_rows(facilities()) if r["facility"] == "ERB")
        self.assertEqual(row["capacity_mwh"], ERB_CAPACITY_MWH)
        self.assertEqual(row["unit"], "ERB01")
        self.assertEqual(row["region"], "NSW1")

    def test_network_is_carried_per_row(self):
        """WEM facilities 404 when queried as NEM, so it cannot be assumed."""
        rows = {r["facility"]: r for r in exporter.unit_rows(facilities(), False)}
        self.assertEqual(rows["ERB"]["network"], "NEM")
        self.assertEqual(rows["COLLIE_ESR4"]["network"], "WEM")

    def test_units_without_capacity_are_dropped(self):
        payload = [
            {
                "code": "X",
                "name": "X",
                "network_id": "NEM",
                "units": [
                    {"code": "X1", "fueltech_id": "battery", "capacity_storage": None,
                     "data_last_seen": "2026-09-30"},
                    {"code": "X2", "fueltech_id": "battery", "capacity_storage": 0,
                     "data_last_seen": "2026-09-30"},
                    {"code": "X3", "fueltech_id": "solar_utility", "capacity_storage": 99,
                     "data_last_seen": "2026-09-30"},
                    {"code": "X4", "fueltech_id": "battery", "capacity_storage": "big",
                     "data_last_seen": "2026-09-30"},
                    {"code": "X5", "fueltech_id": "battery", "capacity_storage": 10,
                     "data_last_seen": "2026-09-30"},
                ],
            }
        ]
        self.assertEqual([r["unit"] for r in exporter.unit_rows(payload)], ["X5"])

    def test_malformed_input_is_tolerated(self):
        self.assertEqual(exporter.unit_rows(None), [])
        self.assertEqual(exporter.unit_rows(["nope", 3, None]), [])


# --------------------------------------------------------------------------- #
# select_top
# --------------------------------------------------------------------------- #


class SelectTopTest(unittest.TestCase):
    def rows(self):
        return [
            {"facility": "B", "unit": "B1", "capacity_mwh": 100.0},
            {"facility": "A", "unit": "A1", "capacity_mwh": 300.0},
            {"facility": "C", "unit": "C1", "capacity_mwh": 200.0},
        ]

    def test_ranked_by_capacity_descending(self):
        chosen = exporter.select_top(self.rows(), 2)
        self.assertEqual([r["facility"] for r in chosen], ["A", "C"])

    def test_ranks_are_one_based_and_dense(self):
        chosen = exporter.select_top(self.rows(), 3)
        self.assertEqual([r["rank"] for r in chosen], [1, 2, 3])

    def test_ties_break_deterministically(self):
        rows = [
            {"facility": "Z", "unit": "Z1", "capacity_mwh": 100.0},
            {"facility": "A", "unit": "A1", "capacity_mwh": 100.0},
        ]
        first = exporter.select_top(rows, 2)
        second = exporter.select_top(list(reversed(rows)), 2)
        self.assertEqual([r["facility"] for r in first], ["A", "Z"])
        self.assertEqual([r["facility"] for r in second], ["A", "Z"])

    def test_top_zero_means_everything(self):
        self.assertEqual(len(exporter.select_top(self.rows(), 0)), 3)

    def test_input_rows_are_not_mutated(self):
        rows = self.rows()
        exporter.select_top(rows, 1)
        self.assertNotIn("rank", rows[0])

    def test_real_fleet_top_ten(self):
        chosen = exporter.select_top(exporter.unit_rows(facilities()), 10)
        self.assertEqual(
            [r["facility"] for r in chosen],
            ["ERB", "WTAHB", "ORABESS", "ERB2", "COLLIE_BESS2",
             "COLLIE_ESR4", "COLLIE_ESR5", "STABESS", "SNB02", "LDBESS"],
        )
        self.assertEqual(chosen[0]["capacity_mwh"], ERB_CAPACITY_MWH)


# --------------------------------------------------------------------------- #
# poll_cycle
# --------------------------------------------------------------------------- #


class FakeClient:
    """Stands in for OpenElectricityClient, keyed by facility code.

    `series` is keyed by facility code and holds the same
    `[(unit, "storage"|"power", (epoch, value))]` triples the real client
    returns, so the tests exercise `poll_cycle` against the shape it will see.
    """

    def __init__(self, series=None, error=None, credits=494):
        self.series = series or {}
        self.error = error
        self.credits = credits
        self.requests_made = 0
        self.calls = []
        self.window_hours = []

    def battery_metrics(
        self,
        facility_code,
        network,
        lookback_hours,
        now,
        full_series=False,
        window_hours=None,
    ):
        self.requests_made += 1
        self.calls.append((facility_code, network, full_series))
        self.full_series_seen = full_series
        # Recorded so tests can assert the *effective* window rather than the
        # reach, which is what the request-cost work actually turns on.
        self.window_hours.append(window_hours)
        if self.error:
            raise self.error
        # The fake is already keyed on full triples, so `full_series` is a no-op
        # here beyond recording that the exporter asked for it.
        return self.series.get(facility_code, [])

    def credits_remaining(self):
        return self.credits


def erb_row():
    return next(r for r in exporter.unit_rows(facilities()) if r["facility"] == "ERB")


def series_from_fixture(unit="ERB01"):
    """The storage-only fixture, as the client would hand it over."""
    block = storage_erb()["data"][0]
    for result in block["results"]:
        if result["name"] == "storage_battery_%s" % unit:
            newest = exporter.latest_sample(result["data"])
            return [(unit, "storage", newest)]
    raise AssertionError("fixture has no series %s" % unit)


def combined_series_from_fixture(unit="ERB01"):
    """The real two-metric fixture, flattened to what the client returns.

    Every point of both series rather than just the newest, because inference
    integrates over the whole window and a fixture reduced to two samples would
    test the arithmetic but not the data. Triples are
    `(unit, metric, (epoch, value))`; the exporter normally only acts on the
    newest of each metric, and inference is the one path that needs the rest.
    """
    payload = metrics_erb()
    out = []
    for block in payload["data"]:
        metric = "storage" if block["metric"] == "storage_battery" else "power"
        for result in block["results"]:
            if result["columns"].get("unit_code") != unit:
                continue
            for stamp, value in result["data"]:
                if value is None:
                    continue
                out.append((unit, metric, (exporter.parse_timestamp(stamp), float(value))))
    if not out:
        raise AssertionError("fixture has no combined series for %s" % unit)
    return out


def both_metrics(unit="ERB01", stored_at=ERB_NEWEST_TS, stored_mwh=100.0,
                 power=-246.4, power_at=None):
    """One unit publishing both metrics, each with its own timestamp.

    `power_at` defaults to `stored_at`; pass it separately for the case that
    matters most, which is the two series being different ages.
    """
    return [
        (unit, "storage", (exporter.parse_timestamp(stored_at), stored_mwh)),
        (unit, "power", (exporter.parse_timestamp(power_at or stored_at), power)),
    ]


class PollCycleTest(unittest.TestCase):
    def setUp(self):
        self.now = exporter.parse_timestamp("2026-09-30T12:00:00+10:00")
        self.row = erb_row()

    def cycle(self, client, rows=None, max_age=exporter.DEFAULT_MAX_SAMPLE_AGE,
              full_series=False):
        return exporter.poll_cycle(
            client, rows or [self.row], "NEM", 12, max_age, self.now,
            full_series=full_series,
        )

    def test_soc_is_energy_over_capacity(self):
        result = self.cycle(FakeClient(series={"ERB": series_from_fixture()}))
        sample = result["samples"][0]
        self.assertAlmostEqual(sample["stored_mwh"], ERB_NEWEST_MWH, places=6)
        self.assertAlmostEqual(
            sample["soc"], ERB_NEWEST_MWH / ERB_CAPACITY_MWH, places=9
        )
        self.assertTrue(sample["scrape_success"])

    def test_g1_and_l1_are_not_exported_as_soc(self):
        """quirk 1: three series, one usable. Summing them triples the fleet."""
        result = self.cycle(FakeClient(series={"ERB": series_from_fixture()}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["samples"][0]["unit"], "ERB01")
        self.assertEqual(result["series_without_capacity"], 0)

    def test_extra_series_without_capacity_are_counted_not_dropped_silently(self):
        series = series_from_fixture() + [
            ("ERBG1", "storage", (self.now, 999.0)),
            ("ERBL1", "power", (self.now, 888.0)),
        ]
        result = self.cycle(FakeClient(series={"ERB": series}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["series_without_capacity"], 2)

    def test_series_without_capacity_counts_series_not_points(self):
        # With `--enable-inferred` the client hands over every point of every
        # series, so a per-point counter reports ~740 "series" for a fleet that
        # has 37. The count has to be per series for it to stay a useful
        # canary either way.
        uncapped = [
            ("ERBG1", "storage", (self.now - i * 3600.0, 999.0)) for i in range(24)
        ] + [("ERBL1", "power", (self.now - i * 3600.0, 888.0)) for i in range(24)]
        series = series_from_fixture() + uncapped
        result = self.cycle(
            FakeClient(series={"ERB": series}), full_series=True
        )
        self.assertEqual(result["series_without_capacity"], 2)

    def test_wrong_network_is_passed_through(self):
        client = FakeClient(series={"ERB": []})
        self.cycle(client, rows=[dict(self.row, network="WEM")])
        self.assertEqual(client.calls[0][1], "WEM")

    def test_http_404_becomes_an_unread_sample_not_an_exception(self):
        """quirk 4: absent facilities are an expected answer."""
        client = FakeClient(error=ScrapeError("GET ... -> HTTP 404: No data"))
        result = self.cycle(client)
        self.assertFalse(result["samples"][0]["scrape_success"])
        self.assertIsNone(result["samples"][0]["soc"])
        self.assertIsNone(result["samples"][0]["power_mw"])

    def test_auth_error_propagates(self):
        client = FakeClient(error=AuthError("HTTP 403"))
        with self.assertRaises(AuthError):
            self.cycle(client)

    def test_one_facility_failing_does_not_stop_the_others(self):
        rows = exporter.select_top(exporter.unit_rows(facilities()), 3)
        wanted = {r["facility"] for r in rows}
        client = FakeClient(
            series={"ERB": series_from_fixture()},
            error=None,
        )

        def metrics(
            facility_code,
            network,
            lookback_hours,
            now,
            full_series=False,
            window_hours=None,
        ):
            client.requests_made += 1
            client.calls.append((facility_code, network))
            if facility_code == "WTAHB":
                raise ScrapeError("HTTP 404: No data")
            return client.series.get(facility_code, [])

        client.battery_metrics = metrics
        result = exporter.poll_cycle(client, rows, "NEM", 12, exporter.DEFAULT_MAX_SAMPLE_AGE, self.now)
        by_facility = {s["facility"]: s for s in result["samples"]}
        self.assertTrue(by_facility["ERB"]["scrape_success"])
        self.assertFalse(by_facility["WTAHB"]["scrape_success"])
        self.assertIn("ERB", wanted)

    def test_soc_is_clamped_to_zero_one(self):
        for mwh, expected in ((-50.0, 0.0), (ERB_CAPACITY_MWH * 3, 1.0)):
            series = [("ERB01", "storage", (self.now, mwh))]
            result = self.cycle(FakeClient(series={"ERB": series}))
            self.assertEqual(result["samples"][0]["soc"], expected)

    def test_sample_older_than_max_age_is_dropped(self):
        old = (self.now - 48 * 3600, 500.0)
        result = self.cycle(
            FakeClient(series={"ERB": [("ERB01", "storage", old)]}), max_age=36 * 3600
        )
        self.assertEqual(result["series_too_stale"], 1)
        self.assertFalse(result["samples"][0]["scrape_success"])

    def test_empty_response_leaves_the_battery_unread(self):
        result = self.cycle(FakeClient(series={"ERB": []}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertFalse(result["samples"][0]["scrape_success"])


    def test_empty_response_leaves_the_battery_unread(self):
        result = self.cycle(FakeClient(series={"ERB": []}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertFalse(result["samples"][0]["scrape_success"])

    def test_all_null_series_count_as_unread(self):
        client = FakeClient(series={"ERB": []})
        result = self.cycle(client)
        self.assertEqual(result["series_without_capacity"], 0)

    # -- power ---------------------------------------------------------------- #

    def test_power_is_exported_with_a_timestamp_of_its_own(self):
        """The two series age independently, so they cannot share a stamp.

        storage_battery stops at 04:00 and power runs to 11:00, which is the
        normal state of this feed seven hours out of ten. Collapsing them into
        one timestamp would make a seven-hour-old SOC claim to be current.
        """
        stored_at = "2026-10-01T04:00:00+10:00"
        power_at = "2026-10-01T11:00:00+10:00"
        result = self.cycle(
            FakeClient(
                series={
                    "ERB": both_metrics(
                        stored_at=stored_at, stored_mwh=COMBINED_NEWEST_MWH,
                        power=COMBINED_POWER_MW, power_at=power_at,
                    )
                }
            )
        )
        sample = result["samples"][0]
        self.assertEqual(sample["sampled_at"], exporter.parse_timestamp(stored_at))
        self.assertAlmostEqual(sample["power_mw"], COMBINED_POWER_MW, places=6)
        self.assertEqual(sample["power_sampled_at"], exporter.parse_timestamp(power_at))

    def test_both_metrics_cost_one_request(self):
        """The budget is counted in requests, so power must not add one."""
        client = FakeClient(
            series={"ERB": both_metrics(stored_at=COMBINED_NEWEST_TS, power=123.4,
                                        power_at=COMBINED_POWER_TS)}
        )
        self.cycle(client)
        self.assertEqual(client.requests_made, 1)

    def test_power_survives_a_daytime_window_with_no_energy_reading(self):
        """The real daytime case: storage is all null, power is current.

        This is a successful poll of a healthy battery, not a failure, so the
        power value is exported even though there is no SOC to go with it.
        """
        at = COMBINED_POWER_TS
        result = self.cycle(
            FakeClient(series={"ERB": [("ERB01", "power", (exporter.parse_timestamp(at), -129.7))]})
        )
        sample = result["samples"][0]
        self.assertAlmostEqual(sample["power_mw"], -129.7, places=6)
        self.assertIsNone(sample["soc"])
        self.assertIsNone(sample["sampled_at"])
        # The flag is about SOC, so it stays 0 - but the power gauge is there.
        self.assertFalse(sample["scrape_success"])

    def test_energy_without_power_leaves_the_power_metric_empty(self):
        """Not every facility publishes both, and absence is not zero."""
        result = self.cycle(FakeClient(series={"ERB": series_from_fixture()}))
        sample = result["samples"][0]
        self.assertIsNone(sample["power_mw"])
        self.assertIsNone(sample["power_sampled_at"])
        self.assertTrue(sample["scrape_success"])

    def test_a_negative_power_value_is_passed_through(self):
        """Negative is charging, and clamping it away would invert the sign.

        A clamp that turned -246 MW into 0 would make a charging battery
        indistinguishable from an idle one.
        """
        result = self.cycle(
            FakeClient(series={"ERB": [("ERB01", "power", (self.now, -246.4))]})
        )
        self.assertAlmostEqual(result["samples"][0]["power_mw"], -246.4, places=6)

    def test_stale_power_is_dropped_without_costing_the_energy_reading(self):
        old = (self.now - 48 * 3600, 500.0)
        result = self.cycle(
            FakeClient(series={"ERB": series_from_fixture() + [("ERB01", "power", old)]}),
            max_age=36 * 3600,
        )
        sample = result["samples"][0]
        self.assertEqual(result["series_too_stale"], 1)
        self.assertIsNone(sample["power_mw"])
        self.assertAlmostEqual(sample["stored_mwh"], ERB_NEWEST_MWH, places=6)
        self.assertTrue(sample["scrape_success"])

    def test_a_second_unit_of_the_same_facility_is_not_exported_twice(self):
        """Two in-scope units, one request each, one sample each.

        The response for a facility carries every unit that facility owns, so
        without this check each of those units would be emitted twice per
        request - and two samples with identical labels in one exposition is
        something Prometheus rejects, not merely untidy.
        """
        rows = [dict(self.row, unit="ERB01"), dict(self.row, unit="ERBX1")]
        series = [
            ("ERB01", "storage", (self.now, 100.0)),
            ("ERB01", "power", (self.now, 10.0)),
            ("ERBX1", "storage", (self.now, 50.0)),
            ("ERBX1", "power", (self.now, -20.0)),
        ]
        result = exporter.poll_cycle(
            FakeClient(series={"ERB": series}), rows, "NEM", 12,
            exporter.DEFAULT_MAX_SAMPLE_AGE, self.now,
        )
        units = [s["unit"] for s in result["samples"]]
        self.assertEqual(sorted(units), ["ERB01", "ERBX1"])
        self.assertEqual(len(units), len(set(units)))
        self.assertAlmostEqual(
            next(s for s in result["samples"] if s["unit"] == "ERBX1")["power_mw"], -20.0
        )


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #


def sample(**over):
    base = {
        "facility": "ERB",
        "facility_name": "Eraring",
        "region": "NSW1",
        "unit": "ERB01",
        "status": "operating",
        "capacity_mwh": ERB_CAPACITY_MWH,
        "soc": 0.1676,
        "stored_mwh": ERB_NEWEST_MWH,
        "sampled_at": 1790686800.0,
        # A power reading seven hours newer than the energy one, which is the
        # ordinary state of this feed rather than an edge case.
        "power_mw": COMBINED_POWER_MW,
        "power_sampled_at": 1790710800.0,
        "scrape_success": True,
        "rank": 1,
    }
    base.update(over)
    return base


def state(samples=None, **over):
    base = {
        "samples": samples if samples is not None else [sample()],
        "oe_batteries_enumerated": 74,
        "oe_batteries_in_scope": 12,
        "oe_batteries_monitored": 1,
        "oe_battery_series_without_capacity": 4,
        "oe_battery_series_too_stale": 0,
        "oe_battery_fleet_capacity_mwh": 32085.47,
        "oe_battery_monitored_capacity_mwh": ERB_CAPACITY_MWH,
        "oe_poll_cycle_duration_seconds": 4.32,
        "oe_last_poll_timestamp_seconds": 1790742930.0,
        "oe_last_fleet_refresh_timestamp_seconds": 1790742926.0,
        "oe_api_credits_remaining": 494,
        "oe_api_requests_total": 13,
    }
    base.update(over)
    return base


PER_BATTERY_METRICS = (
    "oe_battery_soc_ratio",
    "oe_battery_energy_stored_mwh",
    "oe_battery_capacity_storage_mwh",
    "oe_battery_power_mw",
    "oe_battery_last_sample_timestamp_seconds",
    "oe_battery_sample_age_seconds",
    "oe_battery_power_sample_timestamp_seconds",
    "oe_battery_power_sample_age_seconds",
    "oe_battery_capacity_rank",
    "oe_battery_scrape_success",
)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.now = 1790742930.0
        self.text = exporter.render(state(), self.now)

    def lines_named(self, metric):
        return [l for l in self.text.splitlines() if l.startswith(metric + "{") or l == metric]

    def test_every_metric_has_help_and_type(self):
        for line in self.text.splitlines():
            if line.startswith("# TYPE "):
                self.assertTrue(
                    any(l.startswith("# HELP " + line.split()[2]) for l in self.text.splitlines()),
                    "no HELP for %s" % line.split()[2],
                )

    def test_help_and_type_appear_exactly_once(self):
        for metric in PER_BATTERY_METRICS:
            self.assertEqual(self.text.count("# HELP %s " % metric), 1, metric)
            self.assertEqual(self.text.count("# TYPE %s " % metric), 1, metric)

    def test_ends_with_newline(self):
        self.assertTrue(self.text.endswith("\n"))

    def test_soc_carries_the_identifying_labels(self):
        line = self.lines_named("oe_battery_soc_ratio")[0]
        for label in ('facility="ERB"', 'unit="ERB01"', 'name="Eraring"',
                      'region="NSW1"', 'status="operating"'):
            self.assertIn(label, line)

    def test_power_is_exported_in_megawatts(self):
        line = self.lines_named("oe_battery_power_mw")[0]
        self.assertAlmostEqual(
            float(line.rsplit(" ", 1)[1]), COMBINED_POWER_MW, places=6
        )
        self.assertIn('unit="ERB01"', line)

    def test_a_charging_battery_keeps_its_negative_value(self):
        """The sign is the reading: negative is charging, not an error."""
        line = self.lines_named("oe_battery_power_mw")[0]
        self.assertTrue(line.endswith(" -%s" % abs(COMBINED_POWER_MW)), line)

    def test_the_power_age_is_computed_from_the_power_timestamp(self):
        """Not from the energy timestamp, which is seven hours older here."""
        line = self.lines_named("oe_battery_power_sample_age_seconds")[0]
        self.assertAlmostEqual(
            float(line.rsplit(" ", 1)[1]), self.now - 1790710800.0, places=3
        )
        age_of_energy = self.lines_named("oe_battery_sample_age_seconds")[0]
        self.assertGreater(
            float(age_of_energy.rsplit(" ", 1)[1]), float(line.rsplit(" ", 1)[1])
        )

    def test_missing_value_is_omitted_not_zero(self):
        """A battery that could not be read has an unknown SOC.

        Emitting 0 would read as "flat and empty" on the dashboard, which is a
        different and much more alarming claim than "no reading".
        """
        unread = sample(
            soc=None, stored_mwh=None, sampled_at=None,
            power_mw=None, power_sampled_at=None, scrape_success=False,
        )
        text = exporter.render(state([unread]), self.now)
        for metric in (
            "oe_battery_soc_ratio",
            "oe_battery_energy_stored_mwh",
            "oe_battery_last_sample_timestamp_seconds",
            "oe_battery_sample_age_seconds",
            "oe_battery_power_mw",
            "oe_battery_power_sample_timestamp_seconds",
            "oe_battery_power_sample_age_seconds",
        ):
            self.assertEqual(
                [l for l in text.splitlines() if l.startswith(metric + "{")], [], metric
            )
        # Capacity and rank are facts about the fleet, not readings, so they stay
        # - which is what lets the dashboard keep listing the battery at all.
        self.assertEqual(
            len([l for l in text.splitlines()
                 if l.startswith("oe_battery_capacity_storage_mwh{")]), 1
        )
        self.assertIn(
            'oe_battery_scrape_success{facility="ERB",unit="ERB01",name="Eraring",'
            'region="NSW1",status="operating"} 0',
            text,
        )

    def test_power_is_exported_even_when_the_soc_reading_is_not(self):
        """The daytime case: energy silent, power current, SOC flag still 0.

        The two flags disagreeing is correct and informative here, which is why
        they are separate metrics rather than one "is it alive" boolean.
        """
        text = exporter.render(
            state([sample(soc=None, stored_mwh=None, sampled_at=None, scrape_success=False)]),
            self.now,
        )
        power = [l for l in text.splitlines() if l.startswith("oe_battery_power_mw{")]
        self.assertEqual(len(power), 1, power)
        self.assertAlmostEqual(float(power[0].rsplit(" ", 1)[1]), COMBINED_POWER_MW, places=6)
        self.assertEqual(
            [l for l in text.splitlines() if l.startswith("oe_battery_soc_ratio{")], []
        )
        self.assertIn(
            'oe_battery_scrape_success{facility="ERB",unit="ERB01",name="Eraring",'
            'region="NSW1",status="operating"} 0',
            text,
        )

    def test_sample_age_is_computed_against_now(self):
        line = self.lines_named("oe_battery_sample_age_seconds")[0]
        self.assertAlmostEqual(
            float(line.rsplit(" ", 1)[1]), self.now - 1790686800.0, places=3
        )

    def test_scrape_success_is_one_and_zero(self):
        text = exporter.render(state([sample(), sample(facility="X", unit="X1", scrape_success=False)]), self.now)
        values = sorted(l.rsplit(" ", 1)[1] for l in text.splitlines()
                        if l.startswith("oe_battery_scrape_success{"))
        self.assertEqual(values, ["0", "1"])

    def test_every_per_battery_metric_shares_one_label_set(self):
        """One Grafana variable has to select the same batteries everywhere.

        A reduced label set on any one of these breaks the dashboard: a
        `{name=~"$battery"}` selector matches nothing, and the table frame
        cannot join on `unit` because the field is not there.
        """
        expected = 'facility="ERB",unit="ERB01",name="Eraring",region="NSW1",status="operating"'
        for metric in PER_BATTERY_METRICS:
            line = [l for l in self.text.splitlines() if l.startswith(metric + "{")]
            self.assertEqual(len(line), 1, metric)
            self.assertEqual(line[0].split(" ", 1)[0], "%s{%s}" % (metric, expected), metric)

    def test_labels_are_escaped(self):
        tricky = sample(facility='A"B', facility_name="back\\slash")
        text = exporter.render(state([tricky]), self.now)
        line = [l for l in text.splitlines() if l.startswith("oe_battery_soc_ratio{")][0]
        self.assertIn('facility="A\\"B"', line)
        self.assertIn('name="back\\\\slash"', line)

    def test_fleet_context_gauges_are_present(self):
        for metric in (
            "oe_batteries_enumerated",
            "oe_batteries_in_scope",
            "oe_batteries_monitored",
            "oe_battery_fleet_capacity_mwh",
            "oe_battery_monitored_capacity_mwh",
            "oe_battery_series_without_capacity",
        ):
            self.assertIn(metric, self.text)

    def test_credit_and_request_counters(self):
        self.assertIn("oe_api_credits_remaining 494", self.text)
        self.assertIn("# TYPE oe_api_requests_total counter", self.text)

    def test_empty_state_still_renders(self):
        text = exporter.render(state(samples=[]), self.now)
        self.assertIn("# TYPE oe_battery_soc_ratio gauge", text)
        # The fleet gauges are facts about the fleet, not about the samples, so
        # they survive an empty poll - and a dashboard table keyed on capacity
        # keeps its rows.
        self.assertIn("oe_batteries_enumerated 74", text)
        self.assertIn("oe_battery_fleet_capacity_mwh 32085.47", text)
        self.assertEqual([l for l in text.splitlines() if l.startswith("oe_battery_soc_ratio{")], [])


class FmtTest(unittest.TestCase):
    def test_none_is_nan(self):
        self.assertEqual(exporter.fmt(None), "NaN")

    def test_integers_are_not_dressed_up(self):
        self.assertEqual(exporter.fmt(1997.0), "1997")
        self.assertEqual(exporter.fmt(1), "1")

    def test_specials(self):
        self.assertEqual(exporter.fmt(float("nan")), "NaN")
        self.assertEqual(exporter.fmt(float("inf")), "+Inf")
        self.assertEqual(exporter.fmt(float("-inf")), "-Inf")


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class ClientTest(unittest.TestCase):
    def test_403_raises_auth_error(self):
        """The API 403s urllib's default UA. This must not be a generic error."""
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)
        error = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(AuthError):
                client._request("http://x/does-not-matter")

    def _client(self):
        return exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)

    def test_sends_the_authorization_header(self):
        seen = {}

        def fake(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["ua"] = req.get_header("User-agent")
            raise AssertionError("stop")

        original = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            with self.assertRaises(AssertionError):
                self._client()._request("http://example.invalid/x")
        finally:
            urllib.request.urlopen = original
        self.assertEqual(seen["auth"], "Bearer k")
        # Not cosmetic: the API rejects "Python-urllib" outright.
        self.assertNotIn("Python-urllib", seen["ua"])
        self.assertIn(exporter.USER_AGENT, seen["ua"])

    def test_requests_are_counted(self):
        self.assertEqual(self._client().requests_made, 0)

    def test_success_false_becomes_scrape_error(self):
        client = self._client()
        original = client._request
        client._request = lambda url: {
            "success": False,
            "error": "No data available for facility=['X'] in the specified time range",
        }
        with self.assertRaises(ScrapeError) as ctx:
            client.battery_metrics("X", "NEM", 12, 1790686800.0)
        self.assertIn("No data available", str(ctx.exception))
        client._request = original

    # -- battery_metrics ------------------------------------------------------ #

    def answering(self, payload):
        """A client whose single request is answered with `payload`, URL kept."""
        client = self._client()
        seen = {"count": 0}

        def fake(url):
            seen["url"] = url
            seen["count"] += 1
            return payload

        client._request = fake
        return client, seen

    def test_both_metrics_are_asked_for_in_one_request(self):
        """The request budget is counted in requests, so this must stay one.

        Two `metrics` parameters is the API's own multi-metric shape; asking
        twice would cost a second request per battery per cycle and double a
        budget that is already the binding constraint.
        """
        client, seen = self.answering({"success": True, "data": []})
        client.battery_metrics("ERB", "NEM", 12, 1790686800.0)
        pairs = urllib.parse.parse_qsl(urllib.parse.urlparse(seen["url"]).query)
        self.assertEqual(
            [value for name, value in pairs if name == "metrics"], ["storage_battery", "power"]
        )
        self.assertEqual([v for k, v in pairs if k == "facility_code"], ["ERB"])
        # 5m by default: a query parameter on this same request, so the finer
        # series is free, and the hourly value is only its mean.
        self.assertEqual(
            [v for k, v in pairs if k == "interval"], [exporter.DEFAULT_API_INTERVAL]
        )
        self.assertEqual(exporter.DEFAULT_API_INTERVAL, "5m")

    def test_the_interval_is_a_query_parameter_not_another_request(self):
        # The whole reason to prefer 5m is that it costs nothing. Coarsening or
        # refining the series must stay one call - a second request here would
        # double the daily budget, which is the binding constraint.
        for interval in ("1h", "5m", "15m"):
            with self.subTest(interval=interval):
                client, seen = self.answering({"success": True, "data": []})
                client.interval = interval
                client.battery_metrics("ERB", "NEM", 12, 1790686800.0)
                pairs = urllib.parse.parse_qsl(
                    urllib.parse.urlparse(seen["url"]).query
                )
                self.assertEqual(
                    [v for k, v in pairs if k == "interval"], [interval]
                )
                self.assertEqual(
                    len([v for k, v in pairs if k == "metrics"]), 2
                )

    # ------------------------------------------------------------------ #
    # Inferred (power-integrated) SOC
    # ------------------------------------------------------------------ #

    def test_trapezoid_of_a_steady_sign_is_exact(self):
        # A constant -100 MW over one hour is -100 MWh: MW x h = MWh, so a
        # one-hour interval is the unit conversion itself, and the trapezoid is
        # exact for the straight line between two equal endpoints.
        self.assertAlmostEqual(
            trapezoid(-100.0, 0.0, -100.0, 3600.0), -100.0, places=9
        )

    def test_trapezoid_scales_with_elapsed_time(self):
        # The same power held for twice as long must be twice the energy - the
        # guard against a rule that silently ignored its own timestamps.
        one_hour = trapezoid(50.0, 0.0, 50.0, 3600.0)
        two_hours = trapezoid(50.0, 0.0, 50.0, 7200.0)
        self.assertAlmostEqual(one_hour, 50.0, places=9)
        self.assertAlmostEqual(two_hours, 100.0, places=9)
        self.assertAlmostEqual(two_hours, 2 * one_hour, places=9)

    def test_trapezoid_splits_a_step_between_its_endpoints(self):
        # A rectangle rule would put this whole hour's energy at the left edge.
        # 300 MW for 1h is 300 MWh; the trapezoid puts 150 MWh in the first
        # half-hour, which matters when integrating into a live window.
        self.assertAlmostEqual(
            trapezoid(300.0, 0.0, 0.0, 3600.0), 150.0, places=9
        )

    def test_trapezoid_of_zero_duration_is_zero(self):
        self.assertEqual(trapezoid(100.0, 500.0, -100.0, 500.0), 0.0)

    def test_charging_negative_power_raises_the_inferred_energy(self):
        # Upstream signs `power` from the grid's point of view, so charging is
        # negative while the battery's stored energy goes *up*. The integral is
        # therefore subtracted - the sign convention is the whole subtlety here.
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3600.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        self.assertIsNotNone(result)
        # 100 MWh - (-100 MW x 1h) = 200 MWh
        self.assertAlmostEqual(result["energy_mwh"], 200.0, places=9)

    def test_discharging_positive_power_lowers_the_inferred_energy(self):
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, 50.0), (3600.0, 50.0)],
            now=3600.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        # 100 MWh - (+50 MW x 1h) = 50 MWh
        self.assertAlmostEqual(result["energy_mwh"], 50.0, places=9)

    def test_inferred_soc_is_energy_over_capacity(self):
        result = infer_soc(
            anchor_mwh=250.0,
            anchor_ts=0.0,
            power_series=[(0.0, -100.0), (7200.0, -100.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        # 250 - (-100 x 2h) = 450 MWh of 500
        self.assertAlmostEqual(result["energy_mwh"], 450.0, places=9)
        self.assertAlmostEqual(result["soc"], 0.9, places=9)

    def test_inference_needs_at_least_two_power_samples(self):
        self.assertIsNone(
            infer_soc(100.0, 0.0, [(0.0, -50.0)], 3600.0, 500.0, 24.0, True)
        )

    def test_inference_needs_a_measured_anchor(self):
        # No anchor: there is nothing to integrate *from*. This is the rule
        # that stops inferred values from chaining into each other.
        self.assertIsNone(
            infer_soc(None, None, [(0.0, -50.0), (3600.0, -50.0)], 3600.0, 500.0, 24.0, True)
        )

    def test_inference_refuses_an_anchor_older_than_the_window(self):
        # 30h-old anchor, 24h window: integrating across that would be
        # arithmetic rather than measurement.
        self.assertIsNone(
            infer_soc(
                anchor_mwh=100.0,
                anchor_ts=0.0,
                power_series=[(0.0, -50.0), (30 * 3600.0, -50.0)],
                now=30 * 3600.0,
                capacity_mwh=500.0,
                max_hours=24.0,
                clamp=True,
            )
        )

    def test_inference_ignores_power_before_the_anchor(self):
        # Power samples all predate the measured reading. There is nothing to
        # integrate forward, and integrating backwards would be nonsense.
        self.assertIsNone(
            infer_soc(
                anchor_mwh=100.0,
                anchor_ts=7200.0,
                power_series=[(0.0, -50.0), (3600.0, -50.0)],
                now=10800.0,
                capacity_mwh=500.0,
                max_hours=24.0,
                clamp=True,
            )
        )

    def test_inference_clamps_at_empty(self):
        # Discharging hard from near-empty would integrate below zero, which no
        # battery can do. Clamped to 0 rather than exported as a negative SOC.
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, 300.0), (7200.0, 300.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        # 100 - (+300 x 2h) = -500 MWh, clamped up to 0.
        self.assertEqual(result["energy_mwh"], 0.0)
        self.assertEqual(result["soc"], 0.0)

    def test_inference_clamps_at_full_charge(self):
        # Charging past nameplate integrates above capacity, which no battery
        # can hold. Clamped to capacity rather than exported above 100%.
        result = infer_soc(
            anchor_mwh=400.0,
            anchor_ts=0.0,
            power_series=[(0.0, -300.0), (7200.0, -300.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        # 400 - (-300 x 2h) = 1000 MWh, clamped down to 500.
        self.assertEqual(result["energy_mwh"], 500.0)
        self.assertEqual(result["soc"], 1.0)
        # A clamped SOC is indistinguishable from a real 100% once exported, so
        # the clamp has to say that it happened.
        self.assertEqual(result["saturated"], 1.0)

    def test_saturation_is_not_reported_when_the_estimate_is_inside(self):
        result = infer_soc(
            anchor_mwh=200.0,
            anchor_ts=0.0,
            power_series=[(0.0, -50.0), (7200.0, -50.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        self.assertEqual(result["energy_mwh"], 300.0)
        self.assertEqual(result["saturated"], 0.0)

    def test_saturation_is_flagged_even_with_clamping_off(self):
        # With clamping off the runaway value is exported for diagnosis, but it
        # is still out of bounds and must still say so.
        result = infer_soc(
            anchor_mwh=400.0,
            anchor_ts=0.0,
            power_series=[(0.0, -300.0), (7200.0, -300.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=False,
        )
        self.assertEqual(result["energy_mwh"], 1000.0)
        self.assertEqual(result["saturated"], 1.0)

    def test_clamping_can_be_switched_off_for_diagnosis(self):
        # Unclamped output is what a chart should be inspected against when
        # inference looks wrong: it shows how far the raw integral ran past the
        # physical bound, which is a useful diagnostic and a bad metric.
        result = infer_soc(
            anchor_mwh=400.0,
            anchor_ts=0.0,
            power_series=[(0.0, -300.0), (7200.0, -300.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=False,
        )
        self.assertAlmostEqual(result["energy_mwh"], 1000.0, places=9)

    def test_inference_never_past_now(self):
        # A future-dated power sample must not drag the estimate forward.
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, 50.0), (999999.0, 50.0)],
            now=3600.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        # Only the (0.0, 50.0) sample survives; one point cannot form a segment.
        self.assertIsNone(result)

    def test_inferred_timestamp_is_the_newest_power_sample_used(self):
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, -10.0), (3600.0, -20.0), (7200.0, -30.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=24.0,
            clamp=True,
        )
        self.assertEqual(result["inferred_ts"], 7200.0)

    # -- inference wiring in the scraper itself ------------------------- #

    def _scraper_with_anchor(self, now, **kwargs):
        """A scraper pre-seeded with a measured anchor, no network needed."""
        client = FakeClient({})
        scraper = exporter.BatteryScraper(client, now_fn=lambda: now, **kwargs)
        scraper._last_measured_mwh["ERB01"] = 200.0
        scraper._last_measured_ts["ERB01"] = now - 3600.0
        return scraper

    def test_no_inferred_metrics_are_exported_by_default(self):
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now)
        self.assertFalse(scraper.enable_inferred)
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        self.assertEqual(scraper._compute_inferred(samples, now, now), [])
        body = exporter.render({"samples": samples, "inferred": []}, now)
        self.assertNotIn("oe_battery_soc_inferred_ratio{", body)

    def test_inferred_metrics_appear_when_enabled_and_measured_is_missing(self):
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True)
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        self.assertEqual(len(rows), 1)
        # 200 MWh anchor + 50 MW charged over an hour = 250 MWh.
        self.assertAlmostEqual(rows[0]["inferred_energy_mwh"], 250.0, places=6)
        body = exporter.render(
            {"samples": samples, "inferred": rows, "oe_batteries_inferred": 1}, now
        )
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_battery_energy_inferred_mwh{", body)
        self.assertIn("oe_battery_inferred_timestamp_seconds{", body)
        self.assertIn("oe_battery_inferred_age_seconds{", body)

    def test_a_fresh_measurement_is_still_integrated_rather_than_copied(self):
        # A measured reading inside --infer-fresh-hours does NOT become its own
        # estimate. The inferred family is computed from power whenever an
        # anchor and a power series exist, and freshness does not change that.
        #
        # Why not copy: publishing the reading verbatim makes the dashed line
        # lie exactly on the measured one and then bend away from it as it ages,
        # so the two series carry the same number for a different reason at the
        # same moment - and the handover has a visible kink in it. Integrating
        # instead means inferred_ts is the newest power sample, not the reading,
        # so `inferred_age` reports the age of the power that produced the
        # number rather than the age of a value that was never used.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True)
        samples = [
            dict(
                erb_row(),
                soc=0.5,
                stored_mwh=998.5,
                sampled_at=now - 60.0,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=True,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Explicitly not a copy: this is the field that says so, and it is the
        # assertion that would fail if the copy path were ever reconnected.
        self.assertFalse(row["from_measured"])
        # The anchor the scraper actually holds is 200.0 at now-3600 (see
        # _scraper_with_anchor), NOT the 998.5 this sample claims. Charging at
        # 50 MW for one hour at the default efficiency of 1.0 adds 50 MWh:
        # 200 + 50 = 250, and 250 / capacity(erb_row) is the reported ratio.
        # If the sample's own stored_mwh had been used instead the answer would
        # be 1048.5, so this also pins which anchor wins.
        self.assertEqual(row["inferred_energy_mwh"], 250.0)
        self.assertAlmostEqual(
            row["inferred_soc"], 250.0 / erb_row()["capacity_mwh"], places=12
        )
        # Stamped with the newest power sample integrated, not with the
        # measurement's own time - the difference is the whole point above.
        self.assertEqual(row["inferred_ts"], now)
        self.assertEqual(row["inferred_age"], 0.0)
        self.assertEqual(row["inferred_saturated"], 0.0)
        self.assertEqual(row["inferred_hold_hours"], 0.0)
        # The two series must NOT agree. They only would under the copy
        # semantics, and asserting the inequality is what stops that behaviour
        # returning unnoticed.
        body = exporter.render({"samples": samples, "inferred": rows}, now)

        def value_of(metric):
            for line in body.splitlines():
                if line.startswith(metric + "{"):
                    return float(line.rsplit(" ", 1)[1])
            return None

        measured = value_of("oe_battery_soc_ratio")
        inferred = value_of("oe_battery_soc_inferred_ratio")
        self.assertIsNotNone(measured)
        self.assertIsNotNone(inferred)
        self.assertNotEqual(measured, inferred)

    def test_inferred_rows_are_omitted_not_zeroed_when_there_is_no_anchor(self):
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        client = FakeClient({})
        scraper = exporter.BatteryScraper(
            client, enable_inferred=True, now_fn=lambda: now
        )
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        self.assertEqual(rows, [])
        body = exporter.render({"samples": samples, "inferred": rows}, now)
        # No anchor, no anchor-based line at all - and not a zero either.
        self.assertNotIn("oe_battery_soc_inferred_ratio{", body)

    def test_inference_stops_at_the_max_hours_window(self):
        # An anchor older than the window means we would be integrating across
        # more un-sampled hours than sampled ones.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True, max_infer_hours=0.5)
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        self.assertEqual(scraper._compute_inferred(samples, now, now), [])

    def test_inferred_metrics_carry_the_same_five_labels(self):
        # The label set has to match the measured metrics or the Grafana
        # `$battery` variable selects nothing on these frames and the table
        # cannot join on unit.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True)
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        body = exporter.render({"samples": samples, "inferred": rows}, now)
        measured = re.search(r'oe_battery_soc_ratio\{([^}]*)\}', body)
        self.assertIsNone(measured, "measured SOC absent from this fixture")
        power_line = re.search(r'oe_battery_power_mw\{([^}]*)\}', body)
        inferred_line = re.search(r'oe_battery_soc_inferred_ratio\{([^}]*)\}', body)
        self.assertIsNotNone(power_line)
        self.assertIsNotNone(inferred_line)
        self.assertEqual(power_line.group(1), inferred_line.group(1))

    def test_scrape_success_stays_tied_to_the_measured_reading(self):
        # Inference must not quietly promote the "has a reading" count: a unit
        # with only a synthetic SOC is still a unit we could not measure.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True)
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 3600.0, -50.0), (now, -50.0)],
                scrape_success=False,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        self.assertEqual(len(rows), 1)
        body = exporter.render({"samples": samples, "inferred": rows}, now)
        self.assertIn("oe_battery_scrape_success{", body)
        match = re.search(r'oe_battery_scrape_success\{([^}]*)\} (\d)', body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(2), "0")

    def test_inference_over_many_hours_does_not_run_away(self):
        # 12 hours of steady charging from half capacity, integrated hourly.
        # This is the shape the daytime gap actually has, and it is where a
        # drift bug would show up as a battery pinned at full.
        now = 12 * 3600.0
        series = [(i * 3600.0, -100.0) for i in range(13)]
        result = infer_soc(
            anchor_mwh=1000.0,
            anchor_ts=0.0,
            power_series=series,
            now=now,
            capacity_mwh=1997.0,
            max_hours=24.0,
            clamp=True,
        )
        # 1000 + 1200 = 2200 MWh of integrated energy, clamped to capacity.
        self.assertEqual(result["energy_mwh"], 1997.0)
        unclamped = infer_soc(
            anchor_mwh=1000.0,
            anchor_ts=0.0,
            power_series=series,
            now=now,
            capacity_mwh=1997.0,
            max_hours=24.0,
            clamp=False,
        )
        self.assertAlmostEqual(unclamped["energy_mwh"], 2200.0, places=6)

    def test_inference_of_a_full_day_of_holding_stays_put(self):
        # A battery idling at 0 MW all day should integrate to its anchor, not
        # drift. This is the null case a real feed will spend a lot of its life
        # in, so it is worth pinning.
        now = 24 * 3600.0
        series = [(i * 3600.0, 0.0) for i in range(25)]
        result = infer_soc(
            anchor_mwh=1234.5,
            anchor_ts=0.0,
            power_series=series,
            now=now,
            capacity_mwh=1997.0,
            max_hours=48.0,
            clamp=True,
        )
        self.assertAlmostEqual(result["energy_mwh"], 1234.5, places=6)
        self.assertAlmostEqual(result["soc"], 1234.5 / 1997.0, places=9)

    def test_a_full_day_of_inference_is_refused_by_default_window(self):
        # The default 24h window is a deliberate ceiling: it bounds how far a
        # single estimate can be from the measurement it is anchored to.
        now = 30 * 3600.0
        series = [(i * 3600.0, -10.0) for i in range(31)]
        self.assertIsNone(
            infer_soc(1000.0, 0.0, series, now, 1997.0, max_hours=24.0, clamp=True)
        )

    def test_uncapped_units_never_reach_inference(self):
        # G1/L1 have no capacity, so they are dropped before a sample is ever
        # built and there is no denominator to infer a ratio against.
        row = dict(erb_row(), capacity_mwh=0)
        self.assertFalse(row["capacity_mwh"])
        self.assertIsNone(
            infer_soc(100.0, 0.0, [(0.0, -50.0), (3600.0, -50.0)], 3600.0, 0, 24.0, True)
        )

    def test_an_inferred_row_carries_the_power_clock_not_the_energy_clock(self):
        # The anchor and the integrated point are deliberately different
        # timestamps; collapsing them would make a daytime estimate claim the
        # freshness of an overnight reading.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        scraper = self._scraper_with_anchor(now, enable_inferred=True)
        scraper._last_measured_ts["ERB01"] = now - 7200.0
        samples = [
            dict(
                erb_row(),
                soc=None,
                stored_mwh=None,
                sampled_at=None,
                power_mw=-50.0,
                power_sampled_at=now - 3600.0,
                power_series=[(now - 7200.0, -30.0), (now - 3600.0, -50.0)],
                scrape_success=False,
            )
        ]
        rows = scraper._compute_inferred(samples, now, now)
        self.assertEqual(len(rows), 1)
        # The newest *integrated* power point, which is an hour old even though
        # the poll just ran - so the age is honest rather than reset by the poll.
        self.assertEqual(rows[0]["inferred_ts"], now - 3600.0)
        self.assertAlmostEqual(rows[0]["inferred_age"], 3600.0, places=6)

    def test_inference_refuses_to_bridge_a_wide_gap(self):
        # Two samples 10h apart with nothing between. Integrating across the
        # hole would invent ten hours the feed never reported, so there is no
        # estimate at all rather than a plausible-looking one.
        self.assertIsNone(
            infer_soc(
                anchor_mwh=100.0,
                anchor_ts=0.0,
                power_series=[(0.0, -50.0), (36000.0, -50.0)],
                now=36000.0,
                capacity_mwh=500.0,
                max_hours=48.0,
                clamp=True,
                max_gap_hours=2.0,
            )
        )

    def test_inference_survives_one_missing_hourly_sample(self):
        # The ordinary case: hourly data with a single null interval is normal,
        # not an outage, so a 2h default gap limit integrates straight over it.
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, -10.0), (7200.0, -10.0)],
            now=7200.0,
            capacity_mwh=500.0,
            max_hours=48.0,
            clamp=True,
            max_gap_hours=2.0,
        )
        self.assertIsNotNone(result)
        # -10 MW is charging, so two hours of it puts 20 MWh *into* the battery.
        self.assertAlmostEqual(result["energy_mwh"], 120.0, places=9)

    def test_inference_stops_at_a_gap_rather_than_resuming_after_it(self):
        # Three hours of clean hourly data, then an outage, then more data.
        # Resuming past the outage would apply the later power to a battery
        # whose state across the outage is unknown, so the estimate is reported
        # as of the last continuous sample.
        result = infer_soc(
            anchor_mwh=100.0,
            anchor_ts=0.0,
            power_series=[(0.0, -10.0), (3600.0, -10.0), (7200.0, -10.0), (86400.0, -50.0)],
            now=86400.0,
            capacity_mwh=500.0,
            max_hours=48.0,
            clamp=True,
            max_gap_hours=2.0,
        )
        self.assertIsNotNone(result)
        # Two hours of charging integrated, not twenty-four: 100 + 2*10.
        self.assertAlmostEqual(result["energy_mwh"], 120.0, places=9)
        self.assertAlmostEqual(result["inferred_ts"], 7200.0)

    def test_the_real_response_integrates_to_a_plausible_energy(self):
        # The captured ERB response, integrated across its real overnight
        # window. Whatever the exact number, the estimate has to stay inside
        # the battery's physical envelope and near its anchor - an integration
        # bug shows up as an absurd value, not a subtly wrong one.
        series = combined_series_from_fixture("ERB01")
        points = [(t, v) for _, key, (t, v) in series if key == "power"]
        storage = sorted((t, v) for _, key, (t, v) in series if key == "storage")
        self.assertTrue(points)
        self.assertTrue(storage)
        anchor_ts, anchor_mwh = storage[0]
        result = infer_soc(
            anchor_mwh=anchor_mwh,
            anchor_ts=anchor_ts,
            power_series=points,
            now=max(t for t, _ in points),
            capacity_mwh=1997.0,
            max_hours=24.0,
            clamp=True,
        )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["energy_mwh"], 0.0)
        self.assertLessEqual(result["energy_mwh"], 1997.0)
        self.assertLess(abs(result["energy_mwh"] - anchor_mwh), 1997.0 * 0.35)

    def test_the_real_response_infers_a_ratio_within_bounds(self):
        series = combined_series_from_fixture("ERB01")
        points = [(t, v) for _, key, (t, v) in series if key == "power"]
        storage = sorted((t, v) for _, key, (t, v) in series if key == "storage")
        anchor_ts, anchor_mwh = storage[0]
        result = infer_soc(
            anchor_mwh=anchor_mwh,
            anchor_ts=anchor_ts,
            power_series=points,
            now=max(t for t, _ in points),
            capacity_mwh=1997.0,
            max_hours=24.0,
            clamp=True,
        )
        self.assertGreaterEqual(result["soc"], 0.0)
        self.assertLessEqual(result["soc"], 1.0)
        # The measured anchor's own SOC, for comparison.
        measured = anchor_mwh / 1997.0
        self.assertLess(abs(result["soc"] - measured), 0.35)

    def test_the_real_response_integrates_from_the_newest_measured_value(self):
        # Anchoring on the *oldest* storage point would integrate a whole
        # night of discharge and claim the battery is far emptier than it is.
        # This pins the behaviour to the newest measured reading.
        series = combined_series_from_fixture("ERB01")
        points = [(t, v) for _, key, (t, v) in series if key == "power"]
        storage = sorted((t, v) for _, key, (t, v) in series if key == "storage")
        anchor_ts, anchor_mwh = storage[-1]
        result = infer_soc(
            anchor_mwh=anchor_mwh,
            anchor_ts=anchor_ts,
            power_series=points,
            now=max(t for t, _ in points),
            capacity_mwh=1997.0,
            max_hours=24.0,
            clamp=True,
        )
        # Anchored on the newest reading and integrated forward over a morning
        # of charging (power negative, so stored energy rises), the estimate
        # lands above the anchor and inside the battery's envelope. Anchoring on
        # the *oldest* reading instead would integrate the night's discharge and
        # report it far emptier.
        self.assertGreater(result["energy_mwh"], anchor_mwh)
        self.assertLess(result["energy_mwh"], 1997.0)

    def test_the_real_response_integration_tracks_the_measured_deltas(self):
        # The real accuracy check, on the real data: integrate power across
        # each hour that also has a measured reading and compare. This is the
        # number that decides whether the feature is worth shipping at all.
        series = combined_series_from_fixture("ERB01")
        points = sorted((t, v) for _, key, (t, v) in series if key == "power")
        storage = sorted((t, v) for _, key, (t, v) in series if key == "storage")
        capacity = 1997.0
        errors = []
        for (a, start), (b, end) in zip(storage, storage[1:]):
            window = [(t, p) for t, p in points if a <= t <= b]
            if len(window) < 2 or (b - a) > 7 * 3600:
                continue
            implied = -sum(
                trapezoid(p0, t0, p1, t1)
                for (t0, p0), (t1, p1) in zip(window, window[1:])
            )
            errors.append(abs(implied - (end - start)) / capacity)
        self.assertGreaterEqual(len(errors), 4)
        # Every hour within 4% of capacity, and the mean within 1%. Direction
        # is right and the magnitude is usable - which is the whole claim.
        self.assertLess(max(errors), 0.04)
        self.assertLess(sum(errors) / len(errors), 0.01)

    def test_the_real_two_metric_response_splits_into_both_metrics(self):
        """Parsed against a captured response, not a hand-built one.

        The fixture is the interesting shape: energy stops at 04:00 and power
        runs to 11:00 for the same battery, so a parser that kept one timestamp
        per facility would lose seven hours of information silently.
        """
        client, _ = self.answering(metrics_erb())
        series = client.battery_metrics("ERB", "NEM", 13, 1790816400.0)
        by_key = {(unit, key): (stamp, value) for unit, key, (stamp, value) in series}
        self.assertEqual(
            sorted((unit, key) for unit, key in by_key),
            [
                ("ERB01", "power"), ("ERB01", "storage"),
                ("ERBG01", "power"), ("ERBG01", "storage"),
                ("ERBL01", "power"), ("ERBL01", "storage"),
            ],
        )
        stored_at, stored = by_key[("ERB01", "storage")]
        self.assertEqual(stored_at, exporter.parse_timestamp(COMBINED_NEWEST_TS))
        self.assertAlmostEqual(stored, COMBINED_NEWEST_MWH, places=6)
        power_at, power = by_key[("ERB01", "power")]
        self.assertEqual(power_at, exporter.parse_timestamp(COMBINED_POWER_TS))
        self.assertAlmostEqual(power, COMBINED_POWER_MW, places=6)
        # The point of asking for both: the power sample is seven hours newer
        # than the energy sample for the very same unit.
        self.assertEqual(power_at - stored_at, 7 * 3600)

    def test_full_series_returns_the_whole_window_in_order(self):
        """`full_series` is what makes inference possible at all.

        The default path collapses each series to its newest point, which is
        right for "what is the SOC now" and useless for integrating: with one
        point per series there is no trapezoid to draw. This is the request
        shape that inference depends on, against the real fixture.
        """
        client, seen = self.answering(metrics_erb())
        series = client.battery_metrics(
            "ERB", "NEM", 13, 1790816400.0, full_series=True
        )
        power = sorted(
            (stamp, value)
            for unit, key, (stamp, value) in series
            if unit == "ERB01" and key == "power"
        )
        self.assertGreater(len(power), 2)
        self.assertEqual(power, sorted(power), "points must come back time-ordered")
        # Energy stops at 04:00; power runs on to 11:00.
        storage = [
            stamp for unit, key, (stamp, _) in series if unit == "ERB01" and key == "storage"
        ]
        self.assertLess(max(storage), max(s for s, _ in power))
        # Same number of requests either way - this is not a budget question.
        self.assertEqual(seen["count"], 1)

    def test_full_series_skips_nulls_and_bad_rows(self):
        """A null interval is a gap, not a zero-power reading."""
        client, _ = self.answering(
            {
                "success": True,
                "data": [
                    {
                        "metric": "power",
                        "unit": "MW",
                        "results": [
                            {
                                "name": "power_X",
                                "columns": {"unit_code": "X"},
                                "data": [
                                    ["2026-10-01T00:00", 1.0],
                                    ["2026-10-01T01:00", None],
                                    ["2026-10-01T02:00", 3.0],
                                    ["2026-10-01T03:00", "junk"],
                                    ["2026-10-01T04:00"],
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        series = client.battery_metrics(
            "X", "NEM", 12, 1790686800.0, full_series=True
        )
        self.assertEqual([v for _, _, (_, v) in series], [1.0, 3.0])

    def test_the_unit_code_is_read_from_the_columns_block(self):
        """`columns: {unit_code: ...}` is the upstream's own attribution."""
        client, _ = self.answering(metrics_erb())
        series = client.battery_metrics("ERB", "NEM", 13, 1790816400.0)
        self.assertIn(("ERB01", "storage"), [(u, k) for u, k, _ in series])

    def test_the_series_name_is_used_when_columns_are_absent(self):
        client, _ = self.answering(
            {
                "success": True,
                "data": [
                    {
                        "metric": "power",
                        "unit": "MW",
                        "results": [
                            {
                                "name": "power_ERB01",
                                "data": [[COMBINED_POWER_TS, COMBINED_POWER_MW]],
                            }
                        ],
                    }
                ],
            }
        )
        series = client.battery_metrics("ERB", "NEM", 13, 1790816400.0)
        self.assertEqual([u for u, _, _ in series], ["ERB01"])

    def test_an_unexpected_unit_on_a_block_is_warned_about(self):
        """The exported metric name claims MW, so a change of unit must be named.

        It is a warning rather than a failure: refusing to export would hide the
        drift, and the value is still more use than nothing - but a silent unit
        change would plot as a plausible line a factor of 1000 out.
        """
        payload = metrics_erb()
        for block in payload["data"]:
            if block["metric"] == "power":
                block["unit"] = "kW"
        client, _ = self.answering(payload)
        with self.assertLogs(exporter.log, level="WARNING") as caught:
            series = client.battery_metrics("ERB", "NEM", 13, 1790816400.0)
        self.assertTrue(any("kW" in line for line in caught.output), caught.output)
        # Still exported, still in the documented unit.
        self.assertTrue(any(key == "power" for _, key, _ in series))

    def test_an_unrecognised_series_name_is_ignored(self):
        client, _ = self.answering(
            {
                "success": True,
                "data": [
                    {
                        "metric": "demand",
                        "unit": "MW",
                        "results": [
                            {"name": "demand_ERB01", "data": [[COMBINED_POWER_TS, 5.0]]}
                        ],
                    }
                ],
            }
        )
        with self.assertLogs(exporter.log, level="DEBUG"):
            self.assertEqual(client.battery_metrics("ERB", "NEM", 13, 1790816400.0), [])

    def test_a_series_of_nothing_but_nulls_is_dropped(self):
        """quirk 3 again, one metric down: no non-null point, no series."""
        client, _ = self.answering(
            {
                "success": True,
                "data": [
                    {
                        "metric": "power",
                        "unit": "MW",
                        "results": [
                            {
                                "name": "power_ERB01",
                                "columns": {"unit_code": "ERB01"},
                                "data": [[COMBINED_POWER_TS, None]],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(client.battery_metrics("ERB", "NEM", 13, 1790816400.0), [])

    def test_missing_data_key_is_an_empty_list(self):
        client = self._client()
        client._request = lambda url: {"success": True}
        self.assertEqual(client.battery_fleet(), [])

    def test_fleet_payload_must_be_a_list(self):
        client = self._client()
        client._request = lambda url: {"success": True, "data": {"nope": 1}}
        with self.assertRaises(ScrapeError):
            client.battery_fleet()

    def test_invalid_json_is_a_scrape_error(self):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)

        def raiser(req, timeout=None):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        with mock.patch.object(urllib.request, "urlopen", raiser):
            with self.assertRaises(ScrapeError):
                client.get("/facilities/")

    def test_credits_remaining_tolerates_a_missing_block(self):
        client = self._client()
        client._request = lambda url: {"success": True, "data": {}}
        self.assertIsNone(client.credits_remaining())

    def test_credits_remaining_is_a_float(self):
        client = self._client()
        client._request = lambda url: {"success": True, "data": {"credits": {"remaining": 494}}}
        self.assertEqual(client.credits_remaining(), 494.0)

    def test_credits_failure_is_not_fatal(self):
        client = self._client()
        client._request = lambda url: (_ for _ in ()).throw(ScrapeError("boom"))
        self.assertIsNone(client.credits_remaining())


# --------------------------------------------------------------------------- #
# Fleet cache
# --------------------------------------------------------------------------- #


class FleetCacheTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "fleet.json")
            exporter.save_cache(path, facilities())
            self.assertEqual(exporter.load_cache(path), facilities())

    def test_missing_file_returns_none(self):
        self.assertIsNone(exporter.load_cache("/nonexistent/fleet.json"))

    def test_unusable_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertIsNone(exporter.load_cache(path))

    def test_empty_facility_list_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            exporter.save_cache(path, [])
            self.assertIsNone(exporter.load_cache(path))


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #


class ScraperClient(FakeClient):
    def __init__(self, *args, fleet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fleet = fleet
        self.fleet_calls = 0

    def battery_fleet(self):
        self.fleet_calls += 1
        return facilities() if self.fleet is None else self.fleet


def make_scraper(client, **kwargs):
    options = dict(
        network="NEM",
        top=3,
        fleet_refresh_interval=86400.0,
    )
    options.update(kwargs)
    return exporter.BatteryScraper(client, **options)


class ChargeEfficiencyTest(unittest.TestCase):
    """The loss term: not all grid-facing charging reaches the cells."""

    def test_charging_stores_only_the_efficient_share(self):
        # -100 MW for 1h is 100 MWh into the grid connection. At 0.9, 90 MWh
        # reaches the cells.
        delta = exporter.stored_energy_delta(-100.0, 0.0, -100.0, 3600.0, 0.9)
        self.assertAlmostEqual(delta, 90.0, places=9)

    def test_discharge_costs_the_full_amount(self):
        # The discharge side is already measured at the terminals, so there is
        # nothing further to discount: the whole integral leaves the battery.
        delta = exporter.stored_energy_delta(100.0, 0.0, 100.0, 3600.0, 0.9)
        self.assertAlmostEqual(delta, -100.0, places=9)

    def test_no_efficiency_term_is_exactly_the_old_integral(self):
        # The default must stay inert, so the loss term is additive on top of
        # behaviour that was already tested.
        self.assertAlmostEqual(
            exporter.stored_energy_delta(-100.0, 0.0, -100.0, 3600.0, 1.0), 100.0, places=9
        )

    def test_a_segment_crossing_zero_is_split_at_the_crossing(self):
        # -100 MW ramping to +100 MW over 2h crosses zero at the midpoint.
        # Charging half an hour is 50 MWh grid-facing -> 45 MWh stored;
        # discharging half an hour costs 50 MWh. Net -5 MWh, not 0: averaging
        # across the switch point would apply one factor to both halves.
        delta = exporter.stored_energy_delta(-100.0, 0.0, 100.0, 7200.0, 0.9)
        self.assertAlmostEqual(delta, 45.0 - 50.0, places=9)

    def test_a_segment_crossing_zero_is_split_the_same_way_in_reverse(self):
        # The same transition the other way round: +100 MW for an hour then
        # -100 MW for an hour. Discharging 50 MWh then charging 50 MWh is
        # -50 + 45, and it must not come out as +5.
        #
        # This was the mirror of the case above and produced exactly the
        # negative of the right answer, because the split charged the first leg
        # to the efficiency whichever way the segment was heading. That
        # direction is the ordinary one - it is what happens every time dispatch
        # ends and the battery goes back to charging - so this was not an edge
        # case, it was every evening, and it cancelled against the correct
        # direction often enough to stay invisible in aggregate SOC.
        delta = exporter.stored_energy_delta(100.0, 0.0, -100.0, 7200.0, 0.9)
        self.assertAlmostEqual(delta, -50.0 + 45.0, places=9)

    def test_both_crossing_directions_agree_on_the_legs(self):
        # Crossing zero must cost the same energy whichever side it is
        # approached from: an hour at one sign and an hour at the other is the
        # same 50 MWh charged and the same 50 MWh discharged either way, so the
        # net is the same -5 MWh. Not the opposite sign - the order changes,
        # the arithmetic does not.
        forward = exporter.stored_energy_delta(-100.0, 0.0, 100.0, 7200.0, 0.9)
        reverse = exporter.stored_energy_delta(100.0, 0.0, -100.0, 7200.0, 0.9)
        self.assertAlmostEqual(forward, -5.0, places=9)
        self.assertAlmostEqual(reverse, forward, places=9)

    def test_efficiency_is_discounted_only_on_the_charging_leg(self):
        # At 1.0 the loss term is inert, so both directions must collapse to
        # the same zero. If a leg were being weighted by the wrong sign this
        # would separate here even though 0.9 happened to look plausible.
        for p0, p1 in ((-100.0, 100.0), (100.0, -100.0)):
            self.assertAlmostEqual(
                exporter.stored_energy_delta(p0, 0.0, p1, 7200.0, 1.0), 0.0, places=9
            )

    def test_charging_after_discharging_loses_energy_not_gains_it(self):
        # The sign test that actually matters in production: over a cycle that
        # returns to the same power, stored energy must not climb on its own.
        # A flipped crossing reported a gain of +5 MWh here, which over a night
        # of transitions is exactly the kind of drift that pins SOC at full.
        net = 0.0
        ts = 0.0
        for p0, p1 in ((100.0, -100.0), (-100.0, 100.0)):
            net += exporter.stored_energy_delta(p0, ts, p1, ts + 7200.0, 0.9)
            ts += 7200.0
        self.assertLess(net, 0.0)

    def test_round_trip_is_the_charging_coefficient(self):
        # 100 MWh in at the grid connection for an hour, then the same 90 MWh
        # out over the next hour: at 0.9 a battery gives back 90, not 100.
        in_ = exporter.stored_energy_delta(-100.0, 0.0, -100.0, 3600.0, 0.9)
        self.assertAlmostEqual(in_, 90.0, places=9)
        # Discharging `in_` MWh over one hour takes `in_` MW at the terminals.
        out = exporter.stored_energy_delta(in_, 3600.0, in_, 7200.0, 0.9)
        self.assertAlmostEqual(out, -90.0, places=9)
        self.assertAlmostEqual(in_ + out, 0.0, places=6)

    def test_efficiency_lowers_a_charging_estimate(self):
        args = dict(anchor_mwh=200.0, anchor_ts=0.0,
                    power_series=[(0.0, -100.0), (7200.0, -100.0)],
                    now=7200.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True)
        plain = infer_soc(**args, charge_efficiency=1.0)
        lossy = infer_soc(**args, charge_efficiency=0.9)
        # 200 MWh + 200 MWh charged, or the same charge at 90%.
        self.assertAlmostEqual(plain["energy_mwh"], 400.0, places=9)
        self.assertAlmostEqual(lossy["energy_mwh"], 380.0, places=9)

    def test_efficiency_leaves_a_discharge_only_estimate_alone(self):
        args = dict(anchor_mwh=500.0, anchor_ts=0.0,
                    power_series=[(0.0, 100.0), (7200.0, 100.0)],
                    now=7200.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True)
        plain = infer_soc(**args, charge_efficiency=1.0)
        lossy = infer_soc(**args, charge_efficiency=0.9)
        self.assertAlmostEqual(plain["energy_mwh"], lossy["energy_mwh"], places=9)

    def test_the_efficient_estimate_is_less_likely_to_saturate(self):
        # The reason the term exists: charging hard used to run past capacity
        # and clamp to a flat 100%. 105 MWh at the grid connection puts the
        # battery over its 1000 MWh nameplate at no loss, and just under it at
        # 0.9 - so the coefficient is the difference between "full" and "full".
        args = dict(anchor_mwh=900.0, anchor_ts=0.0,
                    power_series=[(0.0, -105.0), (3600.0, -105.0)],
                    now=3600.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True)
        self.assertEqual(infer_soc(**args, charge_efficiency=1.0)["saturated"], 1.0)
        lossy = infer_soc(**args, charge_efficiency=0.9)
        self.assertEqual(lossy["saturated"], 0.0)
        self.assertAlmostEqual(lossy["energy_mwh"], 994.5, places=9)

    def test_the_coefficient_is_carried_into_the_exposition_state(self):
        # Guards the plumbing only: a flag parsed but never handed to
        # infer_soc() would look like a working knob and change nothing.
        scraper = make_scraper(
            ScraperClient(series={"ERB": both_metrics()}),
            top=1,
            enable_inferred=True,
            infer_charge_efficiency=0.87,
        )
        self.assertEqual(scraper.infer_charge_efficiency, 0.87)


class ScraperTest(unittest.TestCase):
    def test_two_cycles_of_inference_across_the_daytime_gap(self):
        # The end-to-end shape of the feature: one poll with a measured reading
        # to establish the anchor, then a later poll where the energy feed has
        # gone quiet but power is still publishing. The second cycle is what
        # inference exists for.
        anchor_at = "2026-10-01T04:00:00+10:00"
        anchor_ts = exporter.parse_timestamp(anchor_at)
        day = "2026-10-01T12:00:00+10:00"
        now = exporter.parse_timestamp(day)
        capacity = 1997.0

        # Cycle 1: measured 124.16 MWh at 04:00 plus power through the morning.
        first = ScraperClient(
            series={
                "ERB": [
                    ("ERB01", "storage", (anchor_ts, 124.1611)),
                    ("ERB01", "power", (anchor_ts, -5.749592)),
                ]
            }
        )
        scraper = make_scraper(first, top=1, enable_inferred=True, now_fn=lambda: now)
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{", body)
        # Measured this cycle, so nothing synthetic is published beside it.
        self.assertNotIn("oe_battery_soc_inferred_ratio{", body)
        self.assertEqual(scraper._last_measured_ts["ERB01"], anchor_ts)
        self.assertAlmostEqual(scraper._last_measured_mwh["ERB01"], 124.1611)

        # Cycle 2: energy silent since 04:00, power charging hard at -100 MW
        # hour by hour. More than one point is required - the exporter
        # integrates between samples and refuses to guess across a gap.
        midday_ts = exporter.parse_timestamp("2026-10-01T11:00:00+10:00")
        second = ScraperClient(
            series={
                "ERB": [
                    ("ERB01", "power", (anchor_ts + i * 3600.0, -100.0))
                    for i in range(8)
                ]
            }
        )
        scraper.client = second
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        # The measured SOC is gone from the exposition...
        self.assertNotIn("oe_battery_soc_ratio{", body)
        self.assertIn("oe_battery_scrape_success{facility=\"ERB\"", body)
        self.assertIn("oe_battery_scrape_success{facility=\"ERB\",unit=\"ERB01\",name=\"Eraring\",region=\"NSW1\",status=\"operating\"} 0", body)
        # ...and a synthetic one has taken its place.
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_battery_energy_inferred_mwh{", body)
        self.assertIn("oe_batteries_inferred 1", body)

        match = re.search(
            r'oe_battery_energy_inferred_mwh\{[^}]*\} ([\d.]+)', body
        )
        self.assertIsNotNone(match)
        inferred = float(match.group(1))
        # Anchored at 124.16 MWh at 04:00, charging at ~100 MW through to 11:00
        # is roughly 700 MWh, so the estimate must sit well above the anchor
        # and inside the battery's 1997 MWh envelope.
        self.assertGreater(inferred, 124.1611)
        self.assertLess(inferred, capacity)
        ratio = float(
            re.search(r'oe_battery_soc_inferred_ratio\{[^}]*\} ([\d.]+)', body).group(1)
        )
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)
        self.assertAlmostEqual(ratio, inferred / capacity, places=4)

    def test_saturation_is_exported_so_a_pinned_soc_is_visible(self):
        # Waratah pinned at exactly 1.0 in the live data because the
        # integration ran past capacity. Without this flag it is
        # indistinguishable from a battery that is genuinely full.
        anchor_ts = exporter.parse_timestamp("2026-10-01T04:00:00+10:00")
        charging = [
            ("ERB01", "power", (anchor_ts + i * 3600.0, -900.0)) for i in range(4)
        ]
        scraper = make_scraper(
            ScraperClient(series={"ERB": charging}),
            top=1,
            enable_inferred=True,
            now_fn=lambda: anchor_ts + 4 * 3600.0,
        )
        scraper._last_measured_mwh["ERB01"] = 1900.0
        scraper._last_measured_ts["ERB01"] = anchor_ts
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_battery_inferred_saturated{facility=\"ERB\"", body)
        self.assertIn("oe_battery_inferred_saturated{facility=\"ERB\",unit=\"ERB01\",name=\"Eraring\",region=\"NSW1\",status=\"operating\"} 1", body)

    def test_measured_reading_hands_over_to_inference_when_it_goes_stale(self):
        # The core handover. `storage_battery` publishes overnight, so the
        # exporter sees the same 04:00 reading all day and keeps reporting
        # scrape_success=1. Inference must take over on freshness, not on
        # presence, or the daytime hours it exists for would never be covered.
        anchor_ts = exporter.parse_timestamp("2026-10-01T04:00:00+10:00")
        charging = [("ERB01", "power", (anchor_ts + i * 600.0, -60.0)) for i in range(80)]  # every 10 min
        series = {"ERB": [("ERB01", "storage", (anchor_ts, 400.0))] + charging}

        # 04:30 - the reading is 30 minutes old, so it is still today's answer.
        early = make_scraper(
            ScraperClient(series=series),
            top=1,
            enable_inferred=True,
            now_fn=lambda: anchor_ts + 1800.0,
        )
        body, ok = early.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{", body)
        # Inferred is computed from power integration regardless of freshness.
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_batteries_inferred 1", body)
        self.assertIn("oe_batteries_monitored 1", body)

        # 12:00 - same reading, eight hours old. Measured is still exported;
        # inference is computed from power regardless of freshness.
        midday = make_scraper(
            ScraperClient(series=series),
            top=1,
            enable_inferred=True,
            now_fn=lambda: anchor_ts + 8 * 3600.0,
        )
        body, ok = midday.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{", body)
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_batteries_inferred 1", body)
        self.assertIn("oe_batteries_monitored 1", body)

    def test_freshness_window_is_configurable(self):
        # A battery publishing twice a day cannot use the one-gap default: its
        # readings are ~12h apart, so the window has to be widened or the estimate
        # would flap between copying and integrating all day.
        anchor_ts = exporter.parse_timestamp("2026-10-01T04:00:00+10:00")
        charging = [("ERB01", "power", (anchor_ts + i * 3600.0, -60.0)) for i in range(9)]
        series = {"ERB": [("ERB01", "storage", (anchor_ts, 400.0))] + charging}
        scraper = make_scraper(
            ScraperClient(series=series),
            top=1,
            enable_inferred=True,
            infer_fresh_hours=12.0,
            now_fn=lambda: anchor_ts + 8 * 3600.0,
        )
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{", body)
        # Inferred is computed from power integration regardless of freshness.
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_batteries_inferred 1", body)

    def test_a_current_energy_feed_yields_a_copy_and_no_integration(self):
        # With new behavior, inferred is always computed from power if available.
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        client = ScraperClient(series={"ERB": [
            ("ERB01", "storage", (exporter.parse_timestamp("2026-10-01T11:00:00+10:00"), 100.0)),
            ("ERB01", "power", (exporter.parse_timestamp("2026-10-01T11:00:00+10:00"), -246.4)),
            ("ERB01", "power", (exporter.parse_timestamp("2026-10-01T12:00:00+10:00"), -246.4)),
        ]})
        scraper = make_scraper(client, top=1, enable_inferred=True, now_fn=lambda: now)
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{", body)
        self.assertIn("oe_battery_soc_inferred_ratio{", body)
        self.assertIn("oe_batteries_inferred 1", body)

    def test_inferred_soc_survives_a_poll_where_the_api_fails(self):
        # The anchor is in memory and is not a reading, so a transient API
        # failure must not wipe it: the next good poll should still be able to
        # infer. If this regresses, one 500 costs a day of inferred coverage.
        anchor_ts = exporter.parse_timestamp("2026-10-01T04:00:00+10:00")
        now = exporter.parse_timestamp("2026-10-01T12:00:00+10:00")
        charging = [("ERB01", "power", (anchor_ts + i * 3600.0, -50.0)) for i in range(8)]
        client = ScraperClient(
            series={"ERB": [("ERB01", "storage", (anchor_ts, 500.0))] + charging}
        )
        scraper = make_scraper(client, top=1, enable_inferred=True, now_fn=lambda: now)
        scraper.poll_once()
        self.assertIn("ERB01", scraper._last_measured_ts)

        scraper.client = ScraperClient(error=exporter.ScrapeError("HTTP 500"))
        body, ok = scraper.poll_once()
        # quirk 4 again: one facility failing is a warning, not a failed poll.
        self.assertTrue(ok)
        self.assertIn("ERB01", scraper._last_measured_ts)

        scraper.client = ScraperClient(series={"ERB": charging})
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_inferred_ratio{", body)

    def test_poll_once_produces_exposition(self):
        # The clock is pinned to just after the fixture's newest sample rather
        # than left on wall time: the fixture is a real response from a real
        # evening, and it silently aged past `--max-sample-age` until this test
        # started failing for a reason that had nothing to do with the code.
        now = exporter.parse_timestamp(ERB_NEWEST_TS) + 3600.0
        client = ScraperClient(series={"ERB": both_metrics(stored_at=ERB_NEWEST_TS)})
        scraper = make_scraper(client, now_fn=lambda: now)
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{facility=\"ERB\"", body)
        self.assertIn("oe_battery_power_mw{facility=\"ERB\"", body)
        self.assertIn("oe_batteries_in_scope 3", body)

    def test_metrics_are_served_before_the_first_poll(self):
        """Prometheus scrapes within seconds of start, long before the first poll.

        Every value is NaN rather than 0: nothing has been read yet, and a fleet
        count of 0 would claim the fleet is empty. /metrics still answers 200 and
        /healthz still says healthy, so a container that is mid-way through its
        first poll is not reported as down.
        """
        scraper = make_scraper(ScraperClient())
        body, ok = scraper.exposition()
        self.assertIn("# TYPE oe_battery_soc_ratio gauge", body)
        self.assertIn("oe_batteries_enumerated NaN", body)
        self.assertEqual(
            [l for l in body.splitlines() if l.startswith("oe_battery_soc_ratio{")], []
        )
        self.assertTrue(ok)
        healthy, message = scraper.health()
        self.assertTrue(healthy)
        self.assertIn("no poll", message)

    def test_auth_failure_marks_unhealthy_without_raising(self):
        client = ScraperClient(error=AuthError("HTTP 403"))
        scraper = make_scraper(client)
        body, ok = scraper.poll_once()
        self.assertFalse(ok)
        self.assertEqual(body, "")
        healthy, message = scraper.health()
        self.assertFalse(healthy)
        self.assertIn("403", message)

    def test_fleet_is_refetched_only_after_the_refresh_interval(self):
        client = ScraperClient(series={})
        clock = [1000.0]
        scraper = make_scraper(client, now_fn=lambda: clock[0])
        scraper.poll_once()
        scraper.poll_once()
        self.assertEqual(client.fleet_calls, 1)
        clock[0] += 90000.0
        scraper.poll_once()
        self.assertEqual(client.fleet_calls, 2)

    def test_fleet_cache_is_used_when_the_api_is_unreachable(self):
        client = ScraperClient(series={})
        scraper = make_scraper(client, fleet_file=os.path.join(
            tempfile.mkdtemp(), "fleet.json"))
        # Seed the cache, then make the fleet call fail.
        scraper.poll_once()
        self.assertTrue(os.path.exists(scorer_fleet := scraper.fleet_file))
        del scorer_fleet
        broken = ScraperClient(series={})
        broken.battery_fleet = lambda: (_ for _ in ()).throw(ScrapeError("no api"))
        second = make_scraper(broken, fleet_file=scraper.fleet_file)
        body, ok = second.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_batteries_enumerated 74", body)

    def test_stale_cache_does_not_pin_the_fleet(self):
        """The disk cache is a fallback, not a second source of truth.

        Reading the cache before the API would mean a cache file, once written,
        is trusted until it happens to be deleted - the daily refresh would
        never happen. A stale cache must be replaced as soon as the API answers.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            exporter.save_cache(path, facilities())
            payload = json.load(open(path, encoding="utf-8"))
            payload["cached_at"] = 0.0
            json.dump(payload, open(path, "w", encoding="utf-8"))

            doubled = facilities()
            for facility in doubled:
                for unit in facility.get("units") or []:
                    if unit.get("fueltech_id") == exporter.BATTERY_FUELTECH:
                        unit["capacity_storage"] = 100.0
            client = ScraperClient(series={}, fleet=doubled)
            scraper = make_scraper(client, fleet_file=path)
            body, ok = scraper.poll_once()
            self.assertTrue(ok)
            # 74 enumerable units at 100 MWh, i.e. the API's fleet, not the cache.
            self.assertIn("oe_battery_fleet_capacity_mwh 7400", body)
            self.assertEqual(client.fleet_calls, 1)
            # and the cache was brought up to date in the same cycle
            self.assertGreater(json.load(open(path, encoding="utf-8"))["cached_at"], 0.0)

    def test_fleet_cache_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            exporter.save_cache(path, facilities())
            self.assertFalse(os.path.exists(path + ".tmp"))
            with open(path, encoding="utf-8") as handle:
                self.assertIn("cached_at", json.load(handle))

    def test_background_poll_runs_and_stops(self):
        client = ScraperClient(series={})
        scraper = make_scraper(client, poll_interval=0.05)
        scraper.start()
        deadline = threading.Event()
        for _ in range(200):
            if client.requests_made:
                deadline.set()
                break
            threading.Event().wait(0.02)
        scraper.stop()
        self.assertTrue(deadline.is_set(), "background poll never ran")

    def test_a_raising_poll_does_not_kill_the_loop(self):
        client = ScraperClient(series={})
        scraper = make_scraper(client, poll_interval=0.05)
        calls = []
        original = scraper.poll_once

        def flaky():
            calls.append(1)
            raise RuntimeError("boom")

        scraper.poll_once = flaky
        scraper.start()
        threading.Event().wait(0.3)
        scraper.stop()
        self.assertGreaterEqual(len(calls), 2)
        scraper.poll_once = original


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.client = ScraperClient(series={"ERB": series_from_fixture()})
        self.scraper = make_scraper(self.client)
        self.scraper.poll_once()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), exporter.make_handler(self.scraper)
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(
            "http://127.0.0.1:%d%s" % (self.port, path), timeout=5
        ) as response:
            return response.status, response.read().decode()

    def test_metrics(self):
        status, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("oe_battery_soc_ratio", body)

    def test_index(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("battery state of charge", body)

    def test_healthz_ok(self):
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), "ok")

    def test_healthz_reports_failure(self):
        scraper = make_scraper(ScraperClient(error=AuthError("HTTP 403")))
        scraper.poll_once()
        server = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(scraper))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/healthz" % server.server_address[1]
            try:
                urllib.request.urlopen(url, timeout=5)
                self.fail("expected 503")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 503)
                self.assertIn("403", exc.read().decode())
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_route_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


class MainTest(unittest.TestCase):
    def test_missing_key_exits(self):
        with self.assertRaises(SystemExit):
            exporter.main(["--once", "--api-key-env", "OE_DEFINITELY_UNSET_VAR"])

    def test_key_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "key")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("  oe_secret\n")
            self.assertEqual(exporter.read_api_key(
                exporter.main.__globals__["argparse"].Namespace(
                    api_key_file=path, api_key_env="X"
                )
            ), "oe_secret")

    def test_key_from_environment(self):
        os.environ["OE_TEST_KEY_VAR"] = "oe_env_key"
        try:
            self.assertEqual(exporter.read_api_key(
                exporter.main.__globals__["argparse"].Namespace(
                    api_key_file=None, api_key_env="OE_TEST_KEY_VAR"
                )
            ), "oe_env_key")
        finally:
            del os.environ["OE_TEST_KEY_VAR"]

    def test_unreadable_key_file_exits(self):
        with self.assertRaises(SystemExit):
            exporter.main(["--once", "--api-key-file", "/nonexistent/key"])

    def test_default_top_is_ten(self):
        """Ten batteries, polled every 10 minutes.

        This deliberately pins 600 rather than an hourly interval. 10 units at
        600s is 1440 requests/day, which is ~4x the plan's 366/day bucket, so
        the *default* is only safe for a reduced fleet - `docker-compose.yml`
        passes `--poll-interval=3600` explicitly for the full 12-battery
        deployment, which is 288 requests/day and comfortably inside the bucket.

        Asserting the constant here is what makes the gap visible: if someone
        reads 600 as "the safe default" and drops the compose flag, the
        overage only shows up as HTTP 429s at runtime rather than in review.
        """
        self.assertEqual(exporter.DEFAULT_TOP, 10)
        self.assertEqual(exporter.DEFAULT_POLL_INTERVAL, 600.0)


# --------------------------------------------------------------------------- #
# Idle-based demotion
# --------------------------------------------------------------------------- #


def fleet_rows():
    """Four synthetic facilities, descending capacity, all seen upstream."""
    return [
        {"facility": "A", "unit": "A1", "facility_name": "Alpha", "region": "NSW1",
         "status": "operating", "capacity_mwh": 400.0, "network": "NEM"},
        {"facility": "B", "unit": "B1", "facility_name": "Bravo", "region": "VIC1",
         "status": "operating", "capacity_mwh": 300.0, "network": "NEM"},
        {"facility": "C", "unit": "C1", "facility_name": "Charlie", "region": "QLD1",
         "status": "operating", "capacity_mwh": 200.0, "network": "NEM"},
        {"facility": "D", "unit": "D1", "facility_name": "Delta", "region": "SA1",
         "status": "operating", "capacity_mwh": 100.0, "network": "NEM"},
    ]


class SelectScopeTest(unittest.TestCase):
    DAY = 86400.0
    NOW = exporter.parse_timestamp("2026-09-30T12:00:00+10:00")

    def test_no_idle_limit_keeps_a_strict_capacity_top_n(self):
        rows = fleet_rows()
        scope, demoted = exporter.select_scope(
            rows, 2, {"A1": self.NOW - 99 * self.DAY}, self.NOW, 0
        )
        self.assertEqual([r["unit"] for r in scope], ["A1", "B1"])
        self.assertEqual(demoted, [])

    def test_an_idle_unit_is_replaced_by_the_next_largest_candidate(self):
        """The point of the rotation: a dead battery costs a cycle, not a slot."""
        rows = fleet_rows()
        scope, demoted = exporter.select_scope(
            rows, 2, {"A1": self.NOW - 99 * self.DAY}, self.NOW, 36 * 3600
        )
        self.assertEqual([r["unit"] for r in scope], ["B1", "C1"])
        self.assertEqual([r["unit"] for r in demoted], ["A1"])

    def test_a_unit_never_polled_is_not_yet_idle(self):
        """No verdict means no demotion - a fresh candidate is not punished."""
        scope, demoted = exporter.select_scope(
            fleet_rows(), 2, {"A1": self.NOW - 99 * self.DAY}, self.NOW, 36 * 3600
        )
        self.assertNotIn("C1", [r["unit"] for r in demoted])
        self.assertEqual(len(scope), 2)

    def test_idle_boundary_is_exclusive(self):
        rows = fleet_rows()
        exactly = {"A1": self.NOW - 36 * 3600}
        scope, demoted = exporter.select_scope(rows, 1, exactly, self.NOW, 36 * 3600)
        self.assertEqual([r["unit"] for r in scope], ["A1"])
        self.assertEqual(demoted, [])
        scope, demoted = exporter.select_scope(
            rows, 1, {"A1": self.NOW - 36 * 3600 - 1}, self.NOW, 36 * 3600
        )
        self.assertEqual([r["unit"] for r in scope], ["B1"])
        self.assertEqual([r["unit"] for r in demoted], ["A1"])

    def test_demoted_rows_carry_how_long_they_have_been_idle(self):
        _, demoted = exporter.select_scope(
            fleet_rows(), 1, {"A1": self.NOW - 5 * self.DAY}, self.NOW, 36 * 3600
        )
        self.assertAlmostEqual(demoted[0]["idle_seconds"], 5 * self.DAY)

    def test_every_demoted_unit_is_still_reported_not_silently_hidden(self):
        _, demoted = exporter.select_scope(
            fleet_rows(), 1, {"A1": self.NOW - 5 * self.DAY, "B1": self.NOW - 6 * self.DAY},
            self.NOW, 36 * 3600,
        )
        self.assertEqual([r["unit"] for r in demoted], ["A1", "B1"])


class RecordReadingsTest(unittest.TestCase):
    """Liveness is measured from the reading's own timestamp, not the poll time."""

    NOW = 1_800_000_000.0

    def scraper(self):
        return exporter.BatteryScraper(FakeClient(), top=3, fleet_refresh_interval=86400.0)

    def test_a_success_stores_the_reading_timestamp(self):
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "scrape_success": True, "sampled_at": self.NOW - 3600}], self.NOW
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW - 3600)

    def test_an_older_reading_never_moves_the_stamp_backwards(self):
        scraper = self.scraper()
        scraper._last_reading["A1"] = self.NOW
        scraper._record_readings(
            [{"unit": "A1", "scrape_success": True, "sampled_at": self.NOW - 7200}], self.NOW
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW)

    def test_a_clock_skewed_into_the_future_is_clamped(self):
        """A stamp past the poll would pin the unit as fresh forever."""
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "scrape_success": True, "sampled_at": self.NOW + 86400}], self.NOW
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW)

    def test_a_failure_starts_the_clock_without_moving_an_existing_stamp(self):
        scraper = self.scraper()
        scraper._record_readings([{"unit": "A1", "scrape_success": False}], self.NOW)
        self.assertEqual(scraper._last_reading["A1"], self.NOW)
        scraper._last_reading["A1"] = self.NOW - 1000
        scraper._record_readings(
            [{"unit": "A1", "scrape_success": False}], self.NOW - 100
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW - 1000)

    def test_success_without_a_timestamp_falls_back_to_the_poll_time(self):
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "scrape_success": True, "sampled_at": None}], self.NOW
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW)

    def test_a_power_reading_on_its_own_keeps_the_unit_alive(self):
        """The daytime case: energy silent, power current.

        Judging liveness on storage alone would let a battery that is visibly
        dispatching on the power panel be demoted out of scope.
        """
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "sampled_at": None, "power_sampled_at": self.NOW - 1800}], self.NOW
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW - 1800)

    def test_the_newer_of_the_two_metrics_is_the_one_that_counts(self):
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "sampled_at": self.NOW - 7 * 3600,
              "power_sampled_at": self.NOW - 3600}],
            self.NOW,
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW - 3600)

    def test_an_older_power_sample_does_not_move_the_stamp_backwards(self):
        """Energy fresher than power is unusual but must not reset liveness."""
        scraper = self.scraper()
        scraper._last_reading["A1"] = self.NOW - 60
        scraper._record_readings(
            [{"unit": "A1", "sampled_at": self.NOW - 3600,
              "power_sampled_at": self.NOW - 7200}],
            self.NOW,
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW - 60)

    def test_a_future_power_stamp_is_clamped(self):
        scraper = self.scraper()
        scraper._record_readings(
            [{"unit": "A1", "sampled_at": None, "power_sampled_at": self.NOW + 86400}],
            self.NOW,
        )
        self.assertEqual(scraper._last_reading["A1"], self.NOW)


class RotationBase(unittest.TestCase):
    """End to end through poll_once, against a clock the test moves.

    Cycles are *hourly*, as deployed. That matters: demotion is 36h of no
    reading, so a test that jumps two days between polls would see every
    battery demoted - correctly, since a battery nobody asked cannot be known
    to be publishing. The fake also stamps each reading relative to the poll,
    because the exporter measures liveness from the reading's own timestamp.
    """

    AEST = exporter.timezone(exporter.timedelta(hours=10))
    HOUR = 3600.0
    DAY = 86400.0

    def setUp(self):
        self.clock = [exporter.parse_timestamp("2026-09-30T12:00:00+10:00")]
        self.scraper = None
        self.client = None

    @classmethod
    def overnight(cls, now, hour=4):
        """The last `hour`:00 at or before `now`, which is what the feed does.

        Values exist ~18:00-04:00, so through the afternoon the newest reading
        keeps ageing until the next evening's window opens.
        """
        stamp = exporter.datetime.fromtimestamp(now, cls.AEST).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        if stamp.timestamp() > now:
            stamp -= exporter.timedelta(days=1)
        return stamp.timestamp()

    def fleet_client(self, series_for):
        client = ScraperClient(series={})
        client.series_for = series_for
        client.fleet = [
            {"code": code, "name": name, "network_id": "NEM", "network_region": region,
             "units": [{"code": code + "1", "fueltech_id": "battery", "status_id": "operating",
                        "capacity_storage": capacity,
                        "data_last_seen": "2026-09-30T04:00:00+10:00"}]}
            for code, name, region, capacity in (
                ("AAA", "Alpha", "NSW1", 400.0), ("BBB", "Bravo", "VIC1", 300.0),
                ("CCC", "Charlie", "QLD1", 200.0), ("DDD", "Delta", "SA1", 100.0),
                ("EEE", "Echo", "TAS1", 50.0),
            )
        ]
        self.client = client
        return client

    def make(self, client, top=2, drop_hours=36.0, **kwargs):
        self.scraper = exporter.BatteryScraper(
            client, network="NEM", top=top, fleet_refresh_interval=86400.0,
            drop_idle_seconds=drop_hours * 3600, now_fn=lambda: self.clock[0], **kwargs
        )
        return self.scraper

    def cycle_at(self, when):
        self.clock[0] = when
        self.client.series = self.client.series_for(when)
        body, _ = self.scraper.poll_once()
        return body

    def run_hours(self, client, count, top=2, drop_hours=36.0, start=None, **kwargs):
        """Poll hourly, as deployed, and return (last_body, per_cycle_bodies)."""
        if self.scraper is None:
            self.make(client, top=top, drop_hours=drop_hours, **kwargs)
        first = self.clock[0] if start is None else start
        bodies = []
        for index in range(count):
            bodies.append(self.cycle_at(first + index * self.HOUR))
        return bodies[-1], bodies

    def static(self, publishing, age_hours=0.5, key="storage", value=100.0):
        """A fake where the named facilities publish one metric `age_hours` old."""
        return lambda now: {
            code: [(code + "1", key, (now - age_hours * self.HOUR, value))]
            for code in publishing
        }

    @staticmethod
    def scope_units(body):
        return sorted(set(re.findall(r'capacity_rank\{[^}]*unit="([^"]+)"', body)))

    @staticmethod
    def ranks(body):
        return dict(re.findall(r'capacity_rank\{[^}]*unit="([^"]+)"[^}]*\} ([0-9]+)', body))

class PowerHistoryStoreTest(unittest.TestCase):
    """The local power cache: what it promises, and what it must not promise."""

    def test_repolling_a_window_replaces_rather_than_duplicates(self):
        # The API hands back the whole lookback every cycle. Appending blindly
        # would quadruple the file within a day and put four readings on one
        # timestamp for the integrator to step through.
        history = exporter.PowerHistory()
        history.record({"E1": [(100.0, 5.0), (200.0, 6.0)]}, now=300.0)
        history.record({"E1": [(100.0, 5.0), (200.0, 6.0)]}, now=300.0)
        self.assertEqual(history.total_points(), 2)
        self.assertEqual(history.series("E1"), [(100.0, 5.0), (200.0, 6.0)])

    def test_a_revised_upstream_value_wins(self):
        history = exporter.PowerHistory()
        history.record({"E1": [(100.0, 5.0)]}, now=300.0)
        history.record({"E1": [(100.0, 7.5)]}, now=300.0)
        self.assertEqual(history.series("E1"), [(100.0, 7.5)])

    def test_merged_prefers_the_fresh_series_on_collision(self):
        history = exporter.PowerHistory()
        history.record({"E1": [(100.0, 5.0)]}, now=300.0)
        merged = history.merged("E1", [(100.0, 9.0), (200.0, 1.0)])
        self.assertEqual(merged, [(100.0, 9.0), (200.0, 1.0)])

    def test_retention_drops_old_points(self):
        day = 86400.0
        history = exporter.PowerHistory(retention_days=2.0)
        history.record(
            {"E1": [(1000.0, 1.0), (1000.0 + day, 2.0), (1000.0 + 3 * day, 3.0)]},
            now=1000.0 + 3 * day,
        )
        self.assertEqual(history.total_points(), 2)
        self.assertNotIn(1000.0, dict(history.series("E1")))

    def test_zero_retention_keeps_everything(self):
        # Disabling the bound must mean "unbounded", not "keep nothing".
        history = exporter.PowerHistory(retention_days=0)
        history.record({"E1": [(1.0, 1.0)]}, now=1e12)
        self.assertEqual(history.total_points(), 1)

    def test_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "power-history.json")
            first = exporter.PowerHistory(path)
            first.record({"E1": [(100.0, 5.0), (200.0, 6.0)], "E2": [(100.0, 1.0)]}, now=300.0)
            first.save()
            second = exporter.PowerHistory(path)
            self.assertEqual(second.load(), 3)
            self.assertEqual(second.series("E1"), [(100.0, 5.0), (200.0, 6.0)])
            self.assertEqual(second.series("E2"), [(100.0, 1.0)])

    def test_a_corrupt_file_starts_cold_instead_of_raising(self):
        # A cache is not worth an outage. This must warn and continue.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "power-history.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            history = exporter.PowerHistory(path)
            self.assertEqual(history.load(), 0)
            history.record({"E1": [(100.0, 5.0)]}, now=300.0)
            self.assertEqual(history.total_points(), 1)

    def test_no_path_means_no_writes_and_no_error(self):
        history = exporter.PowerHistory(None)
        history.record({"E1": [(100.0, 5.0)]}, now=300.0)
        history.save()
        self.assertEqual(history.load(), 0)

    def test_junk_rows_are_skipped_not_fatal(self):
        history = exporter.PowerHistory()
        history.record({"E1": [(None, 5.0), ("x", 1.0), (100.0, 2.0)]}, now=300.0)
        self.assertEqual(history.series("E1"), [(100.0, 2.0)])


class PowerHistoryIntegrationTest(RotationBase):
    """Inference must survive a scrape that brings no power of its own."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "power-history.json")
        # 04:00 is where the overnight feed lands, and the anchor has to sit
        # inside capacity: AAA registers 400 MWh, so a 500 MWh reading would be
        # past full before the test began.
        self.anchor = self.overnight(self.clock[0], hour=4)
        self.first = self.anchor + self.HOUR
        self.second = self.anchor + 3 * self.HOUR

    def series_with_history_then_gap(self):
        """One good cycle of power, then a silent one.

        The battery charges at -20 MW from a 100 MWh anchor. Once the API stops
        returning power, the only way to keep integrating is our own cache -
        which is the whole point of storing it.
        """
        seen = []

        def series_for(now):
            if seen:
                return {}
            seen.append(now)
            return {
                "AAA": [
                    ("AAA1", "storage", (self.anchor, 100.0)),
                    ("AAA1", "power", (self.anchor + 1800.0, -20.0)),
                    ("AAA1", "power", (self.anchor + 5400.0, -20.0)),
                ]
            }

        return series_for

    def build(self, history):
        client = self.fleet_client(self.series_with_history_then_gap())
        return self.make(
            client, top=1, enable_inferred=True, infer_fresh_hours=1.0,
            max_infer_hours=6.0, infer_max_gap_hours=2.0, power_history=history,
        )

    def test_cached_power_keeps_inference_alive_when_the_api_goes_quiet(self):
        history = exporter.PowerHistory(self.path)
        self.build(history)
        self.cycle_at(self.first)                 # records power into the cache
        self.assertGreater(history.total_points(), 0)
        # Two hours later the facility returns nothing at all.
        body = self.cycle_at(self.second)
        self.assertIn("oe_batteries_inferred 1", body)
        self.assertIn('oe_battery_soc_inferred_ratio{facility="AAA"', body)

    def test_without_the_cache_the_same_scrape_publishes_nothing(self):
        # The control, and the reason the first test means anything: same data,
        # no cache, so the silent scrape has no power to integrate and inference
        # has to stay silent. If this ever passed, the cache would be untested.
        self.build(None)
        self.cycle_at(self.first)
        body = self.cycle_at(self.second)
        self.assertIn("oe_batteries_inferred 0", body)
        self.assertNotIn('oe_battery_soc_inferred_ratio{facility="AAA"', body)

    def test_history_persists_across_a_restart(self):
        history = exporter.PowerHistory(self.path)
        self.build(history)
        self.cycle_at(self.first)
        self.assertTrue(os.path.exists(self.path))
        # A brand new store, exactly as a restarted process would build.
        revived = exporter.PowerHistory(self.path)
        self.assertGreater(revived.load(), 0)
        self.assertEqual(revived.series("AAA1"), history.series("AAA1"))

    def test_the_anchor_is_never_persisted(self):
        # The one thing this store must not hold is a claim about present
        # energy. A file cannot know whether it is still true, and a restart
        # that trusted one would serve a stale SOC as current - the failure mode
        # the rest of the exporter refuses everywhere.
        history = exporter.PowerHistory(self.path)
        self.build(history)
        self.cycle_at(self.first)
        history.save()
        with open(self.path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        self.assertEqual(sorted(blob["units"]), ["AAA1"])
        self.assertEqual(blob["version"], 1)
        for stamp, value in blob["units"]["AAA1"]:
            self.assertNotAlmostEqual(value, 100.0, places=6)   # not the anchor
            self.assertNotAlmostEqual(stamp, self.anchor, places=3)
        self.assertNotIn("anchor", json.dumps(blob).lower())
        self.assertNotIn("mwh", json.dumps(blob).lower())


class ReadingCacheTest(unittest.TestCase):
    """The store that makes a narrow request window safe.

    One reading per unit, newest-wins, bounded by age and clock. The guards are
    the whole point of the class, so they are pinned individually rather than
    only through the poll cycle that consumes it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "readings.json")

    def test_a_reading_round_trips_through_the_file(self):
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 1000.0, 250.0)
        cache.save()
        revived = exporter.ReadingCache(self.path)
        self.assertEqual(revived.load(), 1)
        self.assertEqual(revived.get("AAA1", 2000.0), (1000.0, 250.0))

    def test_an_older_reading_cannot_walk_the_anchor_backwards(self):
        # The narrow window can legitimately return an *older* reading than the
        # one already held, e.g. after an upstream rollback. Newest-wins is what
        # stops the anchor regressing every cycle.
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 1000.0, 250.0)
        cache.record("AAA1", 900.0, 999.0)
        self.assertEqual(cache.get("AAA1", 2000.0), (1000.0, 250.0))

    def test_a_newer_reading_does_replace_it(self):
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 1000.0, 250.0)
        cache.record("AAA1", 1100.0, 300.0)
        self.assertEqual(cache.get("AAA1", 2000.0), (1100.0, 300.0))

    def test_a_future_dated_reading_is_refused(self):
        # A skewed upstream clock would otherwise pin the unit as fresh forever.
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 5000.0, 250.0)
        self.assertIsNone(cache.get("AAA1", 2000.0))

    def test_a_reading_past_max_age_is_refused(self):
        # Same bound `poll_cycle` applies to a sample that arrives live, so the
        # two cannot disagree about how old a usable reading may be.
        cache = exporter.ReadingCache(self.path, max_age_seconds=3600.0)
        cache.record("AAA1", 1000.0, 250.0)
        self.assertIsNotNone(cache.get("AAA1", 4000.0))
        self.assertIsNone(cache.get("AAA1", 5000.0))

    def test_a_corrupt_file_starts_cold_rather_than_refusing_to_start(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        cache = exporter.ReadingCache(self.path)
        self.assertEqual(cache.load(), 0)
        self.assertIsNone(cache.get("AAA1", 2000.0))

    def test_a_missing_file_starts_cold_without_complaint(self):
        cache = exporter.ReadingCache(self.path)
        self.assertEqual(cache.load(), 0)

    def test_the_file_is_written_atomically(self):
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 1000.0, 250.0)
        cache.save()
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        self.assertEqual(blob["version"], 1)
        self.assertEqual(blob["readings"]["AAA1"], [1000.0, 250.0])
        self.assertEqual(blob["seeded"], ["AAA1"])

    def test_the_seeded_set_survives_a_restart(self):
        # It is the whole basis for deciding who *must* have a reading, so losing
        # it on restart would silently stop forcing the wide window on a
        # regression.
        cache = exporter.ReadingCache(self.path)
        cache.record("AAA1", 1000.0, 250.0)
        cache.save()
        revived = exporter.ReadingCache(self.path, max_age_seconds=1e9)
        revived.load()
        self.assertEqual(revived.missing(["AAA1", "BBB1"], 2000.0), [])

    def test_a_cache_written_before_seeded_existed_still_fails_safe(self):
        # Absent field means "unknown", which must be read as "seeded nobody" and
        # therefore as wide, never as "seeded everybody".
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "readings": {"AAA1": [1000.0, 250.0]}}, handle)
        cache = exporter.ReadingCache(self.path, max_age_seconds=1e9)
        self.assertEqual(cache.load(), 1)
        self.assertEqual(cache.missing(["AAA1"], 2000.0), ["AAA1"])

    def test_missing_only_complains_about_seeded_units(self):
        cache = exporter.ReadingCache(max_age_seconds=1e9)
        cache.record("AAA1", 1000.0, 250.0)
        # BBB1 is in scope but has never published, so it is not a missing
        # reading - there is nothing it could be hiding.
        self.assertEqual(cache.missing(["AAA1", "BBB1"], 2000.0), [])

    def test_an_unwritable_path_never_raises(self):
        # A read-only cache must not be able to stop polling.
        cache = exporter.ReadingCache(os.path.join(self.dir, "nope", "x", "r.json"))
        cache.record("AAA1", 1000.0, 250.0)
        cache.save()  # must not raise


class RequestWindowTest(unittest.TestCase):
    """`--poll-window-hours` is an optimisation, never an optimisation's excuse.

    Each test pins one of the conditions that must send a cycle back to the full
    `--lookback-hours`, because that reach is what makes a missed poll or a cold
    cache recoverable rather than permanent.
    """

    def setUp(self):
        self.now = 10 * 86400.0
        self.rows = [{"unit": "AAA1", "facility": "AAA"}, {"unit": "BBB1", "facility": "BBB"}]

    def scraper(self, cache=None, lookback=192.0, window=2.0, last_fetch=None):
        scraper = exporter.BatteryScraper(
            FakeClient({}),
            lookback_hours=lookback,
            poll_window_hours=window,
            reading_cache=cache,
            now_fn=lambda: self.now,
        )
        scraper._last_fetch_at = last_fetch
        return scraper

    def warm(self, units=("AAA1", "BBB1"), age_hours=24.0):
        cache = exporter.ReadingCache(max_age_seconds=1e9)
        for unit in units:
            cache.record(unit, self.now - age_hours * 3600.0, 500.0)
        return cache

    def test_a_cold_start_asks_for_the_full_reach(self):
        # Nothing cached and nothing fetched: the wide window is the only thing
        # that can find the last reading at all.
        self.assertEqual(self.scraper()._request_window(self.rows, self.now), 192.0)

    def test_a_cold_cache_asks_wide_even_after_a_fetch(self):
        # The cache file exists but has never recorded a unit - corrupt, empty, or
        # written by a version without `seeded`. There is no evidence any reading
        # is reachable, so narrowing would be a guess.
        cache = exporter.ReadingCache()
        cache._points["AAA1"] = (self.now - 86400.0, 500.0)
        scraper = self.scraper(cache=cache, last_fetch=self.now - 3600.0)
        self.assertEqual(scraper._request_window(self.rows, self.now), 192.0)

    def test_a_cold_start_with_a_populated_cache_still_asks_wide(self):
        # Survived restart, but this process has not fetched anything yet. A
        # narrow first request would have no way to know the cache was sane.
        self.assertEqual(
            self.scraper(cache=self.warm(), last_fetch=None)._request_window(self.rows, self.now),
            192.0,
        )

    def test_a_seeded_unit_that_went_stale_forces_the_wide_window(self):
        # The regression case that matters: this unit demonstrably used to
        # publish, so its absence is a fact about the feed and the wide window is
        # what finds out why.
        cache = self.warm(units=("AAA1", "BBB1"), age_hours=24.0)
        self.assertEqual(
            self.scraper(cache=cache, last_fetch=self.now - 3600.0)._request_window(
                self.rows, self.now
            ),
            2.0,
        )
        # BBB1's reading ages past the cache's own limit while AAA1's does not.
        cache.max_age_seconds = 12.0 * 3600.0
        self.assertEqual(
            self.scraper(cache=cache, last_fetch=self.now - 3600.0)._request_window(
                self.rows, self.now
            ),
            192.0,
        )

    def test_a_unit_that_never_published_does_not_block_narrowing(self):
        # Five of the deployed twelve batteries never publish storage_battery.
        # Requiring one of those would pin the window at 192h forever and make
        # this mechanism dead code in production.
        cache = self.warm(units=("AAA1",), age_hours=1.0)
        scraper = self.scraper(cache=cache, last_fetch=self.now - 3600.0)
        self.assertEqual(scraper._request_window(self.rows, self.now), 2.0)

    def test_a_never_published_unit_is_still_narrowed_because_it_needs_no_anchor(self):
        # Same state, expressed the other way round, so the narrowing is not
        # accidentally scoped to the facilities that happen to publish.
        cache = self.warm(units=("AAA1",), age_hours=1.0)
        scraper = self.scraper(cache=cache, last_fetch=self.now - 3600.0)
        self.assertEqual(scraper._request_window([{"unit": "BBB1"}], self.now), 2.0)

    def test_no_cache_configured_at_all_forces_the_wide_window(self):
        # Without a store there is nothing to fall back on, so the narrowing
        # cannot be offered at all. This is why the default keeps it wide.
        scraper = self.scraper(cache=None, last_fetch=self.now - 3600.0)
        self.assertEqual(scraper._request_window(self.rows, self.now), 192.0)

    def test_a_warm_cache_narrows_the_window(self):
        scraper = self.scraper(cache=self.warm(), last_fetch=self.now - 3600.0)
        self.assertEqual(scraper._request_window(self.rows, self.now), 2.0)

    def test_a_missed_poll_widens_the_window_to_cover_the_gap(self):
        # A hole in the power series is not a slightly wrong number: `infer_soc`
        # drops segments wider than `max_gap_hours` instead of integrating across
        # them, so the line would go flat at the last good point.
        scraper = self.scraper(cache=self.warm(), last_fetch=self.now - 6 * 3600.0)
        self.assertGreater(scraper._request_window(self.rows, self.now), 6.0)

    def test_the_window_never_exceeds_the_declared_reach(self):
        # The reach is a hard requirement and the client caps to it anyway, but
        # the policy should not ask for more than it declared.
        scraper = self.scraper(
            cache=self.warm(), lookback=48.0, last_fetch=self.now - 100 * 3600.0
        )
        self.assertEqual(scraper._request_window(self.rows, self.now), 48.0)

    def test_the_window_is_never_shorter_than_one_hour(self):
        # `battery_metrics` floors at 1h and a sub-hour window would ask for
        # less than a single interval.
        scraper = self.scraper(cache=self.warm(), window=0.1, last_fetch=self.now - 1.0)
        self.assertGreaterEqual(scraper._request_window(self.rows, self.now), 1.0)


class RequestWindowIntegrationTest(unittest.TestCase):
    """The window reaching the wire, and the cache filling what it cannot see."""

    def captured_params(self, client, facility="AAA", lookback_hours=192.0, **kwargs):
        """Call battery_metrics and return the params it would have sent."""
        seen = {}

        def grab(path, params):
            seen["params"] = list(params)
            return {"data": []}

        client.get = grab
        client.battery_metrics(facility, "NEM", lookback_hours, 100000.0, **kwargs)
        return dict(seen["params"])

    def client(self):
        return exporter.OpenElectricityClient("k", request_interval=0.0)

    def test_window_hours_really_narrows_the_requested_range(self):
        client = self.client()
        wide = self.captured_params(client)
        narrow = self.captured_params(client, window_hours=2.0)
        def span(p):
            return exporter.parse_timestamp(p["date_end"]) - exporter.parse_timestamp(p["date_start"])
        self.assertAlmostEqual(span(wide) / 3600.0, 192.0, places=3)
        self.assertAlmostEqual(span(narrow) / 3600.0, 2.0, places=3)
        # Same request, same interval, same metrics - only the range moved.
        self.assertEqual(wide["interval"], narrow["interval"])
        self.assertEqual(wide["metrics"], narrow["metrics"])

    def test_a_window_wider_than_the_reach_is_capped_not_honoured(self):
        client = self.client()
        capped = self.captured_params(client, lookback_hours=12.0, window_hours=192.0)
        span = (
            exporter.parse_timestamp(capped["date_end"])
            - exporter.parse_timestamp(capped["date_start"])
        )
        self.assertAlmostEqual(span / 3600.0, 12.0, places=3)

    def test_a_narrow_window_still_reports_the_substitution(self):
        # The window did not reach the reading, so the cache supplies it - and
        # the row says so rather than presenting it as live.
        now = 100000.0
        cache = exporter.ReadingCache()
        cache.record("AAA1", now - 6 * 3600.0, 400.0)
        result = exporter.poll_cycle(
            FakeClient({"AAA": [("AAA1", "power", (now - 300.0, -50.0))]}),
            [{"unit": "AAA1", "facility": "AAA", "capacity_mwh": 1000.0}],
            "NEM",
            192.0,
            exporter.DEFAULT_MAX_SAMPLE_AGE,
            now,
            window_hours=2.0,
            reading_cache=cache,
        )
        row = result["samples"][0]
        self.assertTrue(row["reading_from_cache"])
        self.assertEqual(result["readings_from_cache"], 1)
        self.assertAlmostEqual(row["stored_mwh"], 400.0)
        self.assertAlmostEqual(row["soc"], 0.4)
        # The age travels with it: a cache-served reading must not look current.
        self.assertAlmostEqual(row["sampled_at"], now - 6 * 3600.0)
        self.assertTrue(row["scrape_success"])

    def test_a_live_reading_is_not_marked_as_coming_from_the_cache(self):
        now = 100000.0
        cache = exporter.ReadingCache()
        cache.record("AAA1", now - 6 * 3600.0, 400.0)
        result = exporter.poll_cycle(
            FakeClient(
                {
                    "AAA": [
                        ("AAA1", "storage", (now - 600.0, 700.0)),
                        ("AAA1", "power", (now - 300.0, -50.0)),
                    ]
                }
            ),
            [{"unit": "AAA1", "facility": "AAA", "capacity_mwh": 1000.0}],
            "NEM",
            192.0,
            exporter.DEFAULT_MAX_SAMPLE_AGE,
            now,
            window_hours=2.0,
            reading_cache=cache,
        )
        row = result["samples"][0]
        self.assertFalse(row["reading_from_cache"])
        self.assertAlmostEqual(row["stored_mwh"], 700.0)
        self.assertEqual(result["readings_from_cache"], 0)

    def test_a_failed_facility_is_never_papered_over_with_a_cached_reading(self):
        # A facility that failed to answer is a fact about the feed. Substituting
        # a cached reading would report the battery as monitored during an outage.
        now = 100000.0
        cache = exporter.ReadingCache()
        cache.record("AAA1", now - 6 * 3600.0, 400.0)
        result = exporter.poll_cycle(
            FakeClient({}, error=ScrapeError("HTTP 404")),
            [{"unit": "AAA1", "facility": "AAA", "capacity_mwh": 1000.0}],
            "NEM",
            192.0,
            exporter.DEFAULT_MAX_SAMPLE_AGE,
            now,
            window_hours=2.0,
            reading_cache=cache,
        )
        row = result["samples"][0]
        self.assertFalse(row["scrape_success"])
        self.assertFalse(row["reading_from_cache"])
        self.assertIsNone(row["stored_mwh"])
        self.assertEqual(result["readings_from_cache"], 0)

    def test_a_restarted_exporter_can_reach_an_anchor_it_cached_yesterday(self):
        # The behaviour the whole change exists for. Without the cache the
        # exporter must ask 192h back to find a 90h-old reading, every cycle.
        now = 100000.0
        cache = exporter.ReadingCache()
        cache.record("AAA1", now - 90 * 3600.0, 400.0)
        scraper = exporter.BatteryScraper(
            FakeClient({}),
            lookback_hours=192.0,
            poll_window_hours=2.0,
            reading_cache=cache,
            enable_inferred=True,
            # Mirrors the deployment: a 90h anchor is only usable because
            # --max-infer-hours was raised to match --lookback-hours. At the 24h
            # default `infer_soc` refuses it outright, which is worth stating here
            # so the two horizons are never raised independently.
            max_infer_hours=192.0,
            now_fn=lambda: now,
        )
        scraper._last_measured_mwh["AAA1"] = 400.0
        scraper._last_measured_ts["AAA1"] = now - 90 * 3600.0
        # The first cycle after a restart is wide even though the cache
        # survived - this process has not proven it can fetch yet.
        self.assertEqual(scraper._request_window([{"unit": "AAA1"}], now), 192.0)
        # From the second cycle on it is narrow, and the anchor is still usable
        # for integration, so nothing was lost by never re-downloading it.
        scraper._last_fetch_at = now - 3600.0
        self.assertEqual(scraper._request_window([{"unit": "AAA1"}], now), 2.0)
        row = scraper._compute_inferred(
            [
                {
                    "unit": "AAA1",
                    "capacity_mwh": 1000.0,
                    "power_series": [(now - 600.0, 100.0), (now - 300.0, 100.0)],
                }
            ],
            now,
            now,
        )
        self.assertEqual(len(row), 1)
        # 400 anchored, minus two minutes of discharge at 100 MW (8.33 MWh), and
        # minus the zero-order hold over the five minutes between the newest power
        # sample and `now` (another 8.33). Both legs are stated so that a change
        # to either the integration or the hold has to be deliberate here.
        self.assertAlmostEqual(row[0]["inferred_energy_mwh"], 400.0 - 8.3333 - 8.3333, places=3)
        self.assertAlmostEqual(row[0]["inferred_hold_hours"], 300.0 / 3600.0, places=6)


class RotationTest(RotationBase):
    """The rotation itself: who is in scope, and at what cost."""

    def test_a_battery_that_never_publishes_is_dropped_and_replaced(self):
        """The point of the rotation: a silent battery costs a cycle, not a slot."""
        client = self.fleet_client(self.static(["AAA"]))
        _, bodies = self.run_hours(client, 40)
        self.assertEqual(self.scope_units(bodies[0]), ["AAA1", "BBB1"])
        final = bodies[-1]
        self.assertIn("oe_batteries_demoted 1", final)
        self.assertEqual(self.scope_units(final), ["AAA1", "CCC1"])
        self.assertNotIn('oe_battery_soc_ratio{facility="BBB"', final)
        # The replacement is ranked densely alongside the survivor.
        self.assertEqual(self.ranks(final), {"AAA1": "1", "CCC1": "2"})

    def test_a_healthy_battery_survives_an_empty_afternoon(self):
        """The regression this design exists to prevent.

        The feed publishes ~18:00-04:00, so a poll in the late afternoon finds
        nothing newer than 04:00. Counting consecutive empty polls would demote
        every healthy battery each afternoon and rotate the whole scope daily.
        Measuring the age of the last reading instead keeps them.
        """
        def overnight_publishers(now):
            stamp = self.overnight(now)
            return {
                code: [(code + "1", "storage", (stamp, 100.0))] for code in ("AAA", "BBB")
            }

        client = self.fleet_client(overnight_publishers)
        _, bodies = self.run_hours(client, 72)  # three days, hourly
        for index, body in enumerate(bodies):
            self.assertIn("oe_batteries_demoted 0", body, "demoted at hour %d" % index)
            self.assertIn('oe_battery_scrape_success{facility="AAA"', body)
            self.assertIn('oe_battery_soc_ratio{facility="AAA"', body)
        # The reading it found really is hours old by the end of an afternoon.
        self.assertIn("oe_battery_sample_age_seconds", bodies[-1])

    def test_demotion_follows_reading_age_not_the_count_of_empty_polls(self):
        """Empty polls are fine; only an old *reading* demotes."""
        start = exporter.parse_timestamp("2026-09-30T04:30:00+10:00")
        published = start - 1800
        # Publishes on the first night only, then the facility goes quiet.
        client = self.fleet_client(
            lambda now: {"AAA": [("AAA1", (published, 100.0))]} if now < start else {}
        )
        self.make(client)
        self.assertIn('oe_battery_scrape_success{facility="AAA"', self.cycle_at(start))
        for hour in range(1, 36):
            body = self.cycle_at(start + hour * self.HOUR)
            self.assertIn("oe_batteries_demoted 0", body, "demoted at empty poll %d" % hour)
            self.assertEqual(self.scope_units(body), ["AAA1", "BBB1"])
        # Past 36h the reading itself is too old, and it goes - on the 37th
        # empty poll, which is the point.
        body = self.cycle_at(start + 37 * self.HOUR)
        self.assertIn("oe_batteries_demoted 2", body)
        self.assertEqual(self.scope_units(body), ["CCC1", "DDD1"])

    def test_a_battery_that_still_publishes_power_keeps_its_slot(self):
        """Liveness follows whichever metric is fresher.

        `storage_battery` can go days without publishing anything while `power`
        keeps updating all day. A battery in that state is dispatching, not dead,
        so rotating its slot away would drop a working battery off the board to
        make room for another one that is equally alive.
        """

        def power_only_publishers(now):
            # AAA has no energy reading for days, but power as fresh as ever.
            return {
                "AAA": [("AAA1", "power", (now - 1800.0, -246.4))],
                "BBB": [("BBB1", "storage", (self.overnight(now), 100.0))],
            }

        client = self.fleet_client(power_only_publishers)
        _, bodies = self.run_hours(client, 40)
        final = bodies[-1]
        self.assertIn("oe_batteries_demoted 0", final)
        self.assertEqual(self.scope_units(final), ["AAA1", "BBB1"])
        # It is in scope and publishing power, but it has no SOC to report - the
        # two are separate facts and the exporter says so rather than guessing.
        self.assertNotIn('oe_battery_soc_ratio{facility="AAA"', final)
        self.assertIn('oe_battery_power_mw{facility="AAA",unit="AAA1"', final)
        self.assertIn('oe_battery_scrape_success{facility="AAA",unit="AAA1"', final)

    def test_idle_seconds_are_exported_for_in_scope_and_demoted_units(self):
        client = self.fleet_client(self.static([]))
        final, _ = self.run_hours(client, 40)
        idle = {
            unit: float(value)
            for unit, value in re.findall(
                r'oe_battery_idle_seconds\{[^}]*unit="([^"]+)"[^}]*\} ([0-9.e+-]+)', final
            )
        }
        # The two dropped units are still exported, which is the only way to
        # answer "why did that battery stop appearing" from the metrics alone.
        # EEE never won a slot, so it was never asked and has no liveness to
        # report. Everything that has been polled does, demoted or not.
        self.assertEqual(sorted(idle), ["AAA1", "BBB1", "CCC1", "DDD1"])
        self.assertGreater(idle["AAA1"], 36 * 3600)
        # The replacement started its own clock when promoted, so it is hours
        # idle rather than zero - and comfortably inside the limit.
        self.assertLess(idle["CCC1"], 36 * 3600)

    def test_watchlist_probe_recovers_a_demoted_unit(self):
        client = self.fleet_client(self.static([]))
        final, _ = self.run_hours(client, 40, watchlist_per_cycle=1)
        self.assertIn("oe_batteries_demoted 2", final)
        self.assertEqual(self.scope_units(final), ["CCC1", "DDD1"])
        # The probe round-robins, so with two demoted units it takes two cycles
        # to reach AAA. Probing is a separate step from selecting the scope, so
        # the cycle that probes AAA still reports the scope it actually polled
        # and AAA returns to scope on the one after.
        self.client.series_for = self.static(["AAA", "BBB", "CCC"])
        probed = recovered = ""
        for _ in range(4):
            probed = self.cycle_at(self.clock[0] + self.HOUR)
            if 'oe_battery_soc_ratio{facility="AAA"' in probed:
                recovered = probed
                break
        self.assertNotEqual(recovered, "", "AAA never came back:\n" + probed)
        self.assertIn("oe_batteries_demoted 0", recovered)
        self.assertEqual(self.scope_units(recovered), ["AAA1", "BBB1"])

    def test_watchlist_is_off_by_default(self):
        client = self.fleet_client(self.static([]))
        _, bodies = self.run_hours(client, 3)
        # One request per in-scope facility, every cycle, and nothing at all
        # extra for the units that were demoted.
        self.assertEqual(client.requests_made, 2 * len(bodies))

    def test_demotion_costs_no_extra_requests(self):
        """The whole point: same spend, more batteries actually monitored."""
        client = self.fleet_client(self.static(["AAA", "BBB", "CCC"]))
        self.make(client, top=4)
        first = self.cycle_at(self.clock[0])
        self.assertIn("oe_batteries_monitored 3", first)
        self.assertEqual(client.requests_made, 4)
        # DDD never publishes, so after 36h it is dropped and EEE takes its slot.
        # The scope stays full, the three that publish keep reporting, and the
        # cycle still costs exactly one request per slot.
        final, bodies = self.run_hours(client, 40, start=self.clock[0] + self.HOUR)
        self.assertIn("oe_batteries_demoted 1", final)
        self.assertIn("oe_batteries_monitored 3", final)
        self.assertIn("oe_batteries_in_scope 4", final)
        self.assertEqual(self.scope_units(final), ["AAA1", "BBB1", "CCC1", "EEE1"])
        self.assertEqual(client.requests_made, 4 * (len(bodies) + 1))


class LivenessPersistenceTest(RotationBase):
    """A restart must not hand every unreadable battery a fresh reprieve."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "liveness.json")

    def test_a_restart_does_not_reset_the_idle_clock(self):
        """The bug this exists to fix.

        Without persistence the first sight of an unreadable unit sets its clock
        to *now*, so a container that restarts more often than the 36h idle
        window never demotes anything - the three Collie units would hold their
        slots forever, and restarting the exporter would silently undo the
        rotation.
        """
        client = self.fleet_client(self.static(["AAA"]))
        self.make(client, top=2, liveness_file=self.path)
        _, bodies = self.run_hours(client, 40)
        self.assertIn("oe_batteries_demoted 1", bodies[-1])
        self.assertTrue(os.path.exists(self.path))

        # A fresh scraper, same file, clock advanced by one hour - as a restart
        # would be. BBB is still unreadable, so it must stay demoted.
        self.scraper = None
        self.clock[0] += self.HOUR
        self.make(client, top=2, liveness_file=self.path)
        body = self.cycle_at(self.clock[0])
        self.assertIn("oe_batteries_demoted 1", body)
        self.assertIn("oe_batteries_demoted 1", body)
        self.assertEqual(self.scope_units(body), ["AAA1", "CCC1"])

    def test_only_liveness_is_persisted_never_a_reading(self):
        """The file must not be able to serve a stale SOC after a restart.

        This is the line the exporter will not cross anywhere else, so it is
        asserted against the file's actual contents rather than the docstring.
        """
        client = self.fleet_client(self.static(["AAA"]))
        self.make(client, top=2, liveness_file=self.path)
        self.cycle_at(self.clock[0])
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(sorted(payload), ["cached_at", "last_reading"])
        for unit, stamp in payload["last_reading"].items():
            self.assertIsInstance(unit, str)
            self.assertIsInstance(stamp, float)
        raw = open(self.path, "r", encoding="utf-8").read()
        for leak in ("soc", "energy", "mwh", "stored", "value"):
            self.assertNotIn(leak, raw.lower(), "%r leaked into the liveness file" % leak)

    def test_a_corrupt_file_is_survivable_and_starts_cold(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        client = self.fleet_client(self.static(["AAA"]))
        self.make(client, top=2, liveness_file=self.path)
        body = self.cycle_at(self.clock[0])
        self.assertIn('oe_battery_soc_ratio{facility="AAA"', body)
        # Cold start, so nothing is demoted yet - the same as a first ever run.
        self.assertIn("oe_batteries_demoted 0", body)

    def test_future_dated_stamps_are_not_trusted(self):
        """A clock skewed into the future would pin a dead battery in scope."""
        future = self.clock[0] + 10 * self.DAY
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"cached_at": future, "last_reading": {"AAA1": future}}, handle)
        client = self.fleet_client(self.static([]))
        self.make(client, top=2, liveness_file=self.path)
        self.assertEqual(self.scraper._last_reading, {})

    def test_writing_is_atomic_and_never_leaves_a_partial_file(self):
        client = self.fleet_client(self.static(["AAA"]))
        self.make(client, top=2, liveness_file=self.path)
        self.cycle_at(self.clock[0])
        # os.replace is what makes this true; assert the leftover is not there.
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_no_file_is_written_when_persistence_is_off(self):
        client = self.fleet_client(self.static(["AAA"]))
        self.make(client, top=2)
        self.cycle_at(self.clock[0])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(exporter.resolve_liveness_path(None, None), None)

    def test_path_defaults_beside_the_fleet_cache(self):
        self.assertEqual(
            exporter.resolve_liveness_path(None, "/cache/fleet.json"),
            os.path.join("/cache", "liveness.json"),
        )
        self.assertEqual(
            exporter.resolve_liveness_path("/tmp/x.json", "/cache/fleet.json"),
            "/tmp/x.json",
        )


class ZeroOrderHoldTest(unittest.TestCase):
    """Carrying the last observed rate forward to `now`, scaled by elapsed time.

    The power series is sampled on a fixed grid and scrapes land between those
    points, so without a hold every scrape in an interval republishes the same
    number. With one, an hour-old hourly rate contributes an hour of energy and
    a five-minute-old one contributes 1/12th.
    """
    def _scraper(self, now=0.0):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)
        return make_scraper(client, enable_inferred=True, now_fn=lambda: now)


    def test_the_your_one_twelfth_example(self):
        # -100 MW held for 5 minutes: 100/12 MWh grid-side, times 0.9 efficiency.
        r = exporter.infer_soc(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3900.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=0.9,
            hold_rate=-100.0, hold_max_hours=6.0,
        )
        # One full hour integrated (90 MWh stored) plus 5 minutes held (7.5).
        self.assertAlmostEqual(r["energy_mwh"], 500.0 + 90.0 + 7.5, places=6)
        self.assertAlmostEqual(r["hold_hours"], 5.0 / 60.0, places=6)

    def test_the_hold_is_proportional_to_elapsed_time(self):
        # Same rate, three different ages: energy must scale linearly. This is
        # the property that makes it a rate rather than a per-poll step.
        base = dict(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=0.9,
            hold_rate=-100.0, hold_max_hours=6.0,
        )
        energies = [
            exporter.infer_soc(now=3900.0 + 600 * k, **base)["energy_mwh"]
            for k in range(3)
        ]
        # 10 min of -100 MW is 16.667 MWh grid-side, 15.0 at 0.9 efficiency.
        self.assertAlmostEqual(energies[1] - energies[0], 15.0, places=6)
        self.assertAlmostEqual(energies[2] - energies[1], 15.0, places=6)

    def test_no_hold_by_default_keeps_the_exact_integral(self):
        # Default is off, so the pre-existing behaviour is preserved for any
        # caller that does not ask for a hold.
        r = exporter.infer_soc(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3900.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=0.9,
        )
        self.assertAlmostEqual(r["energy_mwh"], 590.0, places=6)
        self.assertEqual(r["hold_hours"], 0.0)

    def test_the_hold_is_capped(self):
        # A rate nobody is confirming cannot be carried indefinitely. 24h of
        # staleness against a 6h cap contributes exactly 6h of energy.
        r = exporter.infer_soc(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3600.0 + 24 * 3600.0, capacity_mwh=100000.0, max_hours=48.0,
            clamp=True, max_gap_hours=2.0, charge_efficiency=1.0,
            hold_rate=-100.0, hold_max_hours=6.0,
        )
        self.assertAlmostEqual(r["hold_hours"], 6.0, places=6)
        self.assertAlmostEqual(r["energy_mwh"], 500.0 + 100.0 + 600.0, places=6)

    def test_a_zero_cap_disables_the_hold_entirely(self):
        r = exporter.infer_soc(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3900.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=0.9,
            hold_rate=-100.0, hold_max_hours=0.0,
        )
        self.assertAlmostEqual(r["energy_mwh"], 590.0, places=6)
        self.assertEqual(r["hold_hours"], 0.0)

    def test_a_held_battery_stops_at_full_and_says_so(self):
        # Charging at 100 MW from 990/1000 would pass full within the hold. It
        # must clamp at capacity and report saturation rather than exceed it -
        # otherwise the line walks off the top of the panel.
        r = exporter.infer_soc(
            anchor_mwh=990.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=4200.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=1.0,
            hold_rate=-100.0, hold_max_hours=6.0,
        )
        self.assertEqual(r["energy_mwh"], 1000.0)
        self.assertEqual(r["saturated"], 1.0)

    def test_the_hold_only_ever_runs_forwards(self):
        # now=3600 is exactly the newest sample, so there is no lag to fill and
        # the hold is zero. `now` earlier than that is rejected outright, since
        # `infer_soc` refuses to integrate a partial segment rather than report
        # a half-integrated anchor as if it were the whole window.
        r = exporter.infer_soc(
            anchor_mwh=500.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            now=3600.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=1.0,
            hold_rate=-100.0, hold_max_hours=6.0,
        )
        self.assertEqual(r["hold_hours"], 0.0)
        self.assertAlmostEqual(r["energy_mwh"], 600.0, places=6)
        self.assertIsNone(
            exporter.infer_soc(
                anchor_mwh=500.0, anchor_ts=0.0,
                power_series=[(0.0, -100.0), (3600.0, -100.0)],
                now=1000.0, capacity_mwh=1000.0, max_hours=24.0, clamp=True,
                max_gap_hours=2.0, charge_efficiency=1.0,
                hold_rate=-100.0, hold_max_hours=6.0,
            )
        )

    def test_an_integrated_row_keeps_moving_as_the_reading_ages(self):
        # The unit is inside --infer-fresh-hours at poll time (reading 1h old,
        # window 2h) but still gets an *integrated* row: freshness does not buy
        # the copy. So there is no handover to get wrong, and the row is already
        # re-derived on every scrape by the refresh rather than sitting on a
        # stale value until the next poll.
        scraper = self._scraper()
        scraper.infer_fresh_hours = 2.0
        unit = "E1"
        series = [(t * 300.0, -100.0) for t in range(0, 25)]
        scraper._last_measured_mwh = {unit: 500.0}
        scraper._last_measured_ts = {unit: 0.0}
        sample = {
            "unit": unit, "facility": "ERB", "scrape_success": 1,
            "sampled_at": 0.0, "capacity_mwh": 1000.0, "power_series": series,
            "soc": 0.5, "stored_mwh": 500.0,
        }
        poll_t = 3600.0
        rows = scraper._compute_inferred([sample], poll_t, poll_t)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["from_measured"])
        # Charging at 100 MW with the default efficiency of 1.0 for the 1h of
        # power available at poll time: 500 + 100 = 600 of 1000.
        self.assertAlmostEqual(rows[0]["inferred_energy_mwh"], 600.0, places=6)
        self.assertAlmostEqual(rows[0]["inferred_soc"], 0.6, places=6)
        scraper._state = {
            "samples": [sample], "samples_map": {unit: sample}, "inferred": rows,
            "last_measured_mwh": {unit: 500.0}, "last_measured_ts": {unit: 0.0},
            "max_infer_hours": 15.0, "infer_max_hold_hours": 6.0,
            "infer_charge_efficiency": 0.9,
        }
        # Still inside the freshness window, and still integrated rather than
        # copied - the flag must not change the shape of the row here.
        early = scraper._refresh_inferred_for_render(scraper._state, poll_t + 3600.0)
        self.assertEqual(len(early["inferred"]), 1)
        self.assertFalse(early["inferred"][0]["from_measured"])
        # Two hours of charging at 100 MW with the state's 0.9 efficiency:
        # 500 + 100 * 2 * 0.9 = 680.
        self.assertAlmostEqual(
            early["inferred"][0]["inferred_energy_mwh"], 680.0, places=6
        )
        self.assertAlmostEqual(early["inferred"][0]["inferred_soc"], 0.68, places=6)
        # Past the window: same behaviour, still integrated, still one row. The
        # value advances because the samples past the poll clock have become
        # integrable, and `inferred_ts` moves with them so the reported age
        # describes the timestamp the energy was actually integrated to.
        late = scraper._refresh_inferred_for_render(scraper._state, poll_t + 5400.0)
        self.assertEqual(len(late["inferred"]), 1)
        row = late["inferred"][0]
        self.assertEqual(row["unit"], unit)
        self.assertFalse(row["from_measured"])
        self.assertEqual(row["inferred_ts"], poll_t + 3600.0)
        self.assertEqual(row["inferred_age"], 1800.0)
        self.assertAlmostEqual(row["inferred_energy_mwh"], 725.0, places=6)
        self.assertAlmostEqual(row["inferred_soc"], 0.725, places=6)
        # No power sample between t=7200 and now=9000, so the last 30 minutes
        # are all hold: 100 MW * 0.5h * 0.9 = 45 MWh on top of the 680.
        self.assertAlmostEqual(row["inferred_hold_hours"], 0.5, places=6)

    def test_the_newly_stale_path_never_duplicates_an_existing_row(self):
        # E2 is already stale at poll time so the poll produced a row for it.
        # E1 is fresh then and only earns one later. Adding E1 must not disturb
        # E2, and neither unit may end up with two rows - Grafana would read that
        # as two batteries.
        scraper = self._scraper()
        scraper.infer_fresh_hours = 2.0

        def make(unit):
            return {
                "unit": unit, "facility": "ERB", "scrape_success": 1,
                "sampled_at": 0.0, "capacity_mwh": 1000.0,
                "power_series": [(t * 300.0, -100.0) for t in range(0, 25)],
            }

        e1, e2 = make("E1"), make("E2")
        scraper._last_measured_mwh = {"E1": 500.0, "E2": 500.0}
        scraper._last_measured_ts = {"E1": 0.0, "E2": 0.0}
        poll_t = 10 * 3600.0                  # E1 10h old -> inferred, E2 same
        rows = scraper._compute_inferred([e1, e2], poll_t, poll_t)
        self.assertEqual(sorted(r["unit"] for r in rows), ["E1", "E2"])
        scraper._state = {
            "samples": [e1, e2], "samples_map": {"E1": e1, "E2": e2},
            "inferred": rows,
            "last_measured_mwh": {"E1": 500.0, "E2": 500.0},
            "last_measured_ts": {"E1": 0.0, "E2": 0.0},
            "max_infer_hours": 15.0, "infer_max_hold_hours": 6.0,
            "infer_charge_efficiency": 0.9,
        }
        out = scraper._refresh_inferred_for_render(scraper._state, poll_t + 3600.0)
        units = [r["unit"] for r in out["inferred"]]
        self.assertEqual(sorted(units), ["E1", "E2"])
        self.assertEqual(len(units), len(set(units)))

    def test_refreshing_one_unit_neither_duplicates_nor_drops_the_other(self):
        # E1 is fresh (1h old) and E2 is stale (9h) at poll time. Under
        # always-integrate both are integrated rows from the start, so there is no
        # copy to replace - but the invariant the old copy path had to protect
        # still has to hold: one row per unit. Two rows for one unit is what
        # Grafana reads as two batteries, and dropping the other unit's estimate
        # silently loses a battery.
        #
        # The two units are given different power so each row's own number is
        # checkable: a refresh that leaked one unit's series into the other, or
        # rebuilt the list from a single unit, would show up as a wrong value
        # rather than only as a wrong count.
        scraper = self._scraper()
        scraper.infer_fresh_hours = 2.0

        def make(unit, sampled_at, mw):
            return {
                "unit": unit, "facility": "ERB", "scrape_success": 1,
                "sampled_at": sampled_at, "capacity_mwh": 1000.0,
                "soc": 0.5, "stored_mwh": 500.0,
                "power_series": [(t * 300.0, mw) for t in range(0, 25)],
            }

        poll_t = 3600.0
        e1 = make("E1", 0.0, -100.0)             # 1h old at poll time: fresh
        e2 = make("E2", -8 * 3600.0, -50.0)      # 9h old at poll time: stale
        scraper._last_measured_mwh = {"E1": 500.0, "E2": 500.0}
        scraper._last_measured_ts = {"E1": 0.0, "E2": -8 * 3600.0}
        rows = scraper._compute_inferred([e1, e2], poll_t, poll_t)
        at_poll = {r["unit"]: r for r in rows}
        self.assertEqual(sorted(at_poll), ["E1", "E2"])
        # Freshness changes neither row's shape nor its arithmetic.
        for unit in ("E1", "E2"):
            self.assertFalse(at_poll[unit]["from_measured"])
        # 1h of charge at the default 1.0 efficiency: E1 +100, E2 +50.
        self.assertAlmostEqual(at_poll["E1"]["inferred_energy_mwh"], 600.0, places=6)
        self.assertAlmostEqual(at_poll["E2"]["inferred_energy_mwh"], 550.0, places=6)
        scraper._state = {
            "samples": [e1, e2], "samples_map": {"E1": e1, "E2": e2},
            "inferred": rows,
            "last_measured_mwh": {"E1": 500.0, "E2": 500.0},
            "last_measured_ts": {"E1": 0.0, "E2": -8 * 3600.0},
            "max_infer_hours": 15.0, "infer_max_hold_hours": 6.0,
            "infer_charge_efficiency": 0.9,
        }
        out = scraper._refresh_inferred_for_render(
            scraper._state, poll_t + 3 * 3600.0
        )
        units = [r["unit"] for r in out["inferred"]]
        self.assertEqual(sorted(units), ["E1", "E2"])
        self.assertEqual(len(units), len(set(units)))
        after = {r["unit"]: r for r in out["inferred"]}
        for unit in ("E1", "E2"):
            self.assertFalse(after[unit]["from_measured"])
        # Each unit advanced on its own power: E1 2h at 100 MW and a 2h hold at
        # 0.9 = 500 + 180 + 180; E2 the same at 50 MW = 500 + 90 + 90.
        self.assertAlmostEqual(after["E1"]["inferred_energy_mwh"], 860.0, places=6)
        self.assertAlmostEqual(after["E2"]["inferred_energy_mwh"], 680.0, places=6)

    def test_the_newly_stale_path_respects_the_inference_opt_out(self):
        scraper = self._scraper()
        scraper.enable_inferred = False
        unit = "E1"
        sample = {
            "unit": unit, "facility": "ERB", "scrape_success": 1,
            "sampled_at": 0.0, "capacity_mwh": 1000.0,
            "power_series": [(t * 300.0, -100.0) for t in range(0, 25)],
        }
        state = {
            "samples": [sample], "samples_map": {unit: sample}, "inferred": [],
            "last_measured_mwh": {unit: 500.0}, "last_measured_ts": {unit: 0.0},
            "max_infer_hours": 15.0, "infer_max_hold_hours": 6.0,
            "infer_charge_efficiency": 0.9,
        }
        out = scraper._refresh_inferred_for_render(state, 20000.0)
        self.assertEqual(out.get("inferred") or [], [])

    def test_saturation_is_judged_after_the_hold_not_before(self):
        # The held stretch is part of the estimate, so it has to be included in
        # what `saturated` looks at. Capturing the pre-hold value let a battery
        # sit pinned at exactly capacity reporting sat=0 - indistinguishable
        # from a genuine full battery, which is the one thing the flag exists to
        # prevent.
        # 950 MWh anchor, -70 MW: integrated 378 MWh reaches the 1090 ceiling
        # during the hold, not before it.
        r = exporter.infer_soc(
            anchor_mwh=950.0, anchor_ts=0.0,
            power_series=[(t * 300.0, -70.0) for t in range(0, 24)],   # 0..2h
            now=7200.0 + 3600.0, capacity_mwh=1090.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, charge_efficiency=1.0,
            hold_rate=-70.0, hold_max_hours=6.0,
        )
        # 2h of samples puts the raw integral at 980 MWh; the hold then carries
        # the rate forward from 6900s to 10800s, adding ~75.8 more and taking it
        # past the 1090 ceiling. It must clamp at capacity and report the raw
        # value as outside the envelope, which is what distinguishes a held
        # estimate sitting at 100% from a genuinely full battery.
        self.assertEqual(r["energy_mwh"], 1090.0)
        self.assertEqual(r["saturated"], 1.0)
        self.assertAlmostEqual(r["hold_hours"], 3900.0 / 3600.0, places=6)

    def test_charging_is_still_discounted_in_the_held_stretch(self):
        # The hold is part of the estimate, so the efficiency term applies to
        # it exactly as it does to an integrated segment.
        kw = dict(
            anchor_mwh=0.0, anchor_ts=0.0,
            power_series=[(0.0, -100.0), (3600.0, -100.0)],
            capacity_mwh=100000.0, max_hours=24.0, clamp=True,
            max_gap_hours=2.0, hold_rate=-100.0, hold_max_hours=6.0,
        )
        efficient = exporter.infer_soc(now=5400.0, charge_efficiency=1.0, **kw)
        lossy = exporter.infer_soc(now=5400.0, charge_efficiency=0.9, **kw)
        # now=5400 is one hour integrated plus 30 minutes held.
        self.assertAlmostEqual(efficient["energy_mwh"], 100.0 + 50.0, places=6)
        self.assertAlmostEqual(lossy["energy_mwh"], (100.0 + 50.0) * 0.9, places=6)

    def test_the_render_refresh_advances_the_value_between_polls(self):
        # End to end through the scraper: two scrapes with no poll in between,
        # same power samples, and the published SOC must move - because the
        # hold grows with the wall clock rather than being frozen at poll time.
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)
        anchor_ts = 0.0
        series = [(0.0, -100.0), (3600.0, -100.0)]
        scraper = make_scraper(
            client, enable_inferred=True, now_fn=lambda: 3600.0
        )
        scraper.infer_max_hold_hours = 6.0
        scraper.infer_charge_efficiency = 0.9
        scraper.infer_max_gap_hours = 2.0
        scraper.infer_clamp = True
        scraper._last_measured_mwh = {"E1": 500.0}
        scraper._last_measured_ts = {"E1": anchor_ts}
        # The reading has to be older than --infer-fresh-hours or measured wins
        # and inference correctly publishes nothing at all.
        scraper.infer_fresh_hours = 0.5
        sample = {
            "unit": "E1", "facility": "ERB", "scrape_success": 1,
            "sampled_at": 0.0, "capacity_mwh": 100000.0,
            "power_series": series,
        }
        rows = scraper._compute_inferred([sample], 3600.0, 3600.0)
        self.assertEqual(len(rows), 1)
        scraper._state = {
            "samples": [sample],
            "samples_map": {"E1": sample},
            "inferred": rows,
            "last_measured_mwh": {"E1": 500.0},
            "last_measured_ts": {"E1": anchor_ts},
            "max_infer_hours": 24.0,
            "infer_max_hold_hours": 6.0,
            "infer_charge_efficiency": 0.9,
        }
        scraper.now_fn = lambda: 3600.0
        first, _ = scraper.exposition()
        scraper.now_fn = lambda: 4200.0   # ten minutes later, no new poll
        second, _ = scraper.exposition()

        def soc_of(body):
            for line in body.splitlines():
                if line.startswith("oe_battery_soc_inferred_ratio{"):
                    return float(line.rsplit(" ", 1)[1])
            return None

        self.assertIsNotNone(soc_of(first))
        # 10 min of -100 MW at 0.9 is 15 MWh against 100000 MWh of capacity.
        self.assertAlmostEqual(soc_of(second) - soc_of(first), 15.0 / 100000.0, places=9)
        # And the hold is published, so the extrapolation is visible.
        self.assertIn("oe_battery_inferred_hold_hours{", second)


class AnchorAgeTest(unittest.TestCase):
    """`oe_battery_anchor_age_hours` is the evidence that an inferred line is
    missing because the upstream feed went quiet, rather than because the
    battery has no data. It has to be published in both of those cases, which
    means independently of whether inference produced anything at all.
    """

    NOW = 1_700_000_000.0

    def _render(self, samples, last_measured_ts, inferred=None, max_infer_hours=24.0):
        return exporter.render(
            {
                "samples": samples,
                "inferred": inferred or [],
                "last_measured_ts": last_measured_ts,
                "max_infer_hours": max_infer_hours,
            },
            self.NOW,
        )

    def _value(self, text, metric, unit="E1"):
        for line in text.splitlines():
            if line.startswith(metric + "{") and ('unit="%s"' % unit) in line:
                return float(line.rsplit(" ", 1)[1])
        return None

    def _sample(self, unit="E1", **kw):
        base = {
            "unit": unit, "facility": "ERB", "facility_name": "Eraring",
            "region": "NSW1", "status": "operating", "capacity_mwh": 1997.0,
            "scrape_success": 1, "sampled_at": self.NOW - 31.8 * 3600,
            "stored_mwh": 330.3, "soc": 330.3 / 1997.0,
        }
        base.update(kw)
        return base

    def test_anchor_age_is_published_in_hours(self):
        stamp = self.NOW - 31.8 * 3600
        text = self._render([self._sample()], {"E1": stamp})
        self.assertAlmostEqual(
            self._value(text, "oe_battery_anchor_age_hours"), 31.8, places=6
        )

    def test_anchor_timestamp_is_published(self):
        stamp = self.NOW - 31.8 * 3600
        text = self._render([self._sample()], {"E1": stamp})
        self.assertEqual(self._value(text, "oe_battery_anchor_timestamp_seconds"), stamp)

    def test_it_is_published_even_with_no_estimate_at_all(self):
        # The whole reason this metric exists. A 31.8h-old anchor is past
        # --max-infer-hours, so inference refuses and the inferred line is
        # absent - which used to be the only symptom, indistinguishable from a
        # battery with no data at all.
        stamp = self.NOW - 31.8 * 3600
        text = self._render([self._sample()], {"E1": stamp}, max_infer_hours=15.0)
        self.assertEqual(self._value(text, "oe_battery_soc_inferred_ratio"), None)
        self.assertIsNotNone(self._value(text, "oe_battery_anchor_age_hours"))

    def test_it_is_published_while_an_estimate_does_exist(self):
        stamp = self.NOW - 6.0 * 3600
        row = dict(
            self._sample(), unit="E1", inferred_soc=0.42, inferred_ts=stamp,
            inferred_age=6.0 * 3600, inferred_saturated=0, inferred_hold_hours=0.5,
        )
        text = self._render([self._sample()], {"E1": stamp}, inferred=[row])
        self.assertIsNotNone(self._value(text, "oe_battery_soc_inferred_ratio"))
        self.assertAlmostEqual(
            self._value(text, "oe_battery_anchor_age_hours"), 6.0, places=6
        )

    def test_units_with_no_known_anchor_are_omitted_not_zeroed(self):
        # A zero here would read as "published right now", which is the opposite
        # of the truth and would defeat the metric's purpose.
        text = self._render([self._sample()], {})
        self.assertEqual(self._value(text, "oe_battery_anchor_age_hours"), None)
        self.assertEqual(self._value(text, "oe_battery_anchor_timestamp_seconds"), None)

    def test_a_future_anchor_clamps_to_zero_rather_than_going_negative(self):
        stamp = self.NOW + 600.0
        text = self._render([self._sample()], {"E1": stamp})
        self.assertEqual(self._value(text, "oe_battery_anchor_age_hours"), 0.0)

    def test_each_unit_gets_its_own_anchor(self):
        a, b = self.NOW - 3.0 * 3600, self.NOW - 20.0 * 3600
        text = self._render(
            [self._sample(unit="E1"), self._sample(unit="E2")],
            {"E1": a, "E2": b},
        )
        self.assertAlmostEqual(
            self._value(text, "oe_battery_anchor_age_hours", "E1"), 3.0, places=6
        )
        self.assertAlmostEqual(
            self._value(text, "oe_battery_anchor_age_hours", "E2"), 20.0, places=6
        )

    def test_it_survives_a_failed_poll_that_returns_no_samples(self):
        # A failed cycle publishes an empty sample list. The measured ages go
        # with it, so nothing distinguishes that from a quiet feed - this is the
        # second blind spot the anchor pair is meant to close, so the rendering
        # must at least not crash or invent a value.
        text = self._render([], {"E1": self.NOW - 3600.0})
        self.assertEqual(self._value(text, "oe_battery_anchor_age_hours"), None)


class InferredAgeClockTest(unittest.TestCase):
    """State is published per poll and rendered per scrape, so the wall clock
    has to be read at render time. Otherwise two ages in one exposition
    disagree, and the measured/inferred handover fires late.
    """

    def _scraper(self, now):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)
        scraper = make_scraper(
            client, enable_inferred=True, now_fn=lambda: now
        )
        return scraper

    def _state_with_inferred(self, scraper, inferred_ts, inferred_age):
        # The shape poll_once publishes: an inferred row plus a state wrapper.
        state = {
            "samples": [],
            "inferred": [
                {
                    "unit": "E1",
                    "facility": "ERB",
                    "inferred_ts": inferred_ts,
                    "inferred_age": inferred_age,
                    "inferred_soc": 0.5,
                }
            ],
            "oe_batteries_inferred": 1,
        }
        return state

    def test_the_bug_this_fixes_two_ages_in_one_exposition_disagree(self):
        # Reproduces the live symptom: same inferred timestamp, two clocks.
        poll_now = 1000.0
        scraper = self._scraper(poll_now)
        state = self._state_with_inferred(scraper, inferred_ts=900.0, inferred_age=100.0)
        scraper._state = state

        # Scrape 30 minutes after the poll, with no new power samples.
        scraper.now_fn = lambda: 1300.0
        body, _ = scraper.exposition()
        self.assertIn("oe_battery_inferred_age_seconds", body)
        # The age must now be measured from the scrape, not replayed from the
        # poll: 1300 - 900 = 400s, not the 100s frozen into the state.
        self.assertIn("} 400", body)
        self.assertNotIn("} 100", body)

    def test_inferred_age_matches_the_power_sample_age(self):
        # The two metrics describe the same power timestamp, so at scrape time
        # they must agree. Before the fix they were computed against different
        # `now`s and drifted apart by up to a whole poll interval.
        poll_now = 1000.0
        scraper = self._scraper(poll_now)
        power_ts = 900.0
        state = {
            "samples": [
                {
                    "unit": "E1",
                    "facility": "ERB",
                    "scrape_success": 1,
                    "power_sampled_at": power_ts,
                    "power_age": 100.0,
                }
            ],
            "inferred": [
                {
                    "unit": "E1",
                    "facility": "ERB",
                    "inferred_ts": power_ts,
                    "inferred_age": 100.0,
                }
            ],
        }
        scraper._state = state
        scraper.now_fn = lambda: 1300.0
        body, _ = scraper.exposition()
        ages = {}
        for line in body.splitlines():
            for key in (
                "oe_battery_inferred_age_seconds",
                "oe_battery_power_sample_age_seconds",
            ):
                if line.startswith(key + "{"):
                    ages[key] = float(line.rsplit(" ", 1)[1])
        self.assertIn("oe_battery_inferred_age_seconds", ages)
        self.assertIn("oe_battery_power_sample_age_seconds", ages)
        self.assertEqual(ages["oe_battery_inferred_age_seconds"], 400.0)

    def test_without_a_hold_the_energy_does_not_move_between_polls(self):
        # Hold disabled: the value is the exact integral up to the newest
        # sample, so a later scrape changes nothing but the age. This is the
        # older behaviour, still reachable with --infer-max-hold-hours=0.
        poll_now = 1000.0
        scraper = self._scraper(poll_now)
        scraper.infer_max_hold_hours = 0.0
        state = self._state_with_inferred(scraper, inferred_ts=900.0, inferred_age=100.0)
        scraper._state = state
        scraper.now_fn = lambda: 1000.0
        before, _ = scraper.exposition()
        scraper.now_fn = lambda: 4000.0
        after, _ = scraper.exposition()

        def value_of(body):
            for line in body.splitlines():
                if line.startswith("oe_battery_energy_inferred_mwh{"):
                    return line.rsplit(" ", 1)[1]
            return None

        self.assertEqual(value_of(before), value_of(after))
        self.assertIn("} 3100", after)  # the age did move

    def test_render_does_not_mutate_the_published_state(self):
        # A render in flight must keep a consistent view: the refresh returns a
        # new dict rather than editing the one the poll thread published.
        scraper = self._scraper(1000.0)
        state = self._state_with_inferred(scraper, inferred_ts=900.0, inferred_age=100.0)
        scraper._state = state
        scraper.now_fn = lambda: 1300.0
        scraper.exposition()
        self.assertEqual(state["inferred"][0]["inferred_age"], 100.0)
        self.assertIs(scraper._state, state)

    def test_a_state_with_no_inferred_rows_is_returned_untouched(self):
        scraper = self._scraper(1000.0)
        state = {"samples": [], "inferred": []}
        scraper._state = state
        scraper.now_fn = lambda: 1300.0
        scraper.exposition()
        # Untouched: an empty inferred list means nothing to re-stamp, so the
        # refresh short-circuits rather than rebuilding the state dict.
        self.assertEqual(scraper._state["inferred"], [])
        self.assertEqual(len(scraper._state["inferred"]), 0)

    def test_the_handover_to_inference_is_not_delayed_by_a_poll(self):
        # A reading inside --infer-fresh-hours at poll time must stop counting
        # as current once the wall clock passes it, without waiting for the next
        # poll to re-evaluate. Checked on the predicate the render path uses.
        scraper = self._scraper(1000.0)
        scraper.infer_fresh_hours = 1.0
        sample = {"scrape_success": 1, "sampled_at": 1000.0}
        # 500s in: comfortably inside the 1h window.
        self.assertTrue(scraper._measured_is_current(sample, 1500.0))
        # The window is inclusive of its boundary, so 3600s still counts.
        self.assertTrue(scraper._measured_is_current(sample, 4600.0))
        # One second past it, and the reading stops being current.
        self.assertFalse(scraper._measured_is_current(sample, 4601.0))


class PacingTest(unittest.TestCase):
    """The plan documents burst_rate_limit 2/s, so the exporter paces itself.

    A 429 is not retried anywhere in `_request` - it is raised as a ScrapeError
    on the first attempt, because a rate-limited request is treated as a hard
    answer. So going over the burst limit does not cost extra requests, it costs
    the entire cycle and blanks the dashboard. That asymmetry is why the pacing
    exists at all.
    """

    def test_requests_are_spaced_by_the_configured_interval(self):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.05)
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            started = time.monotonic()
            for _ in range(4):
                client.get("/me")
            elapsed = time.monotonic() - started
        self.assertEqual(client.requests_made, 4)
        # 3 gaps between 4 requests, not 4.
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.0)

    def test_pacing_is_off_when_the_interval_is_zero(self):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=0.0)
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            started = time.monotonic()
            for _ in range(4):
                client.get("/me")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_negative_interval_is_clamped_not_honoured(self):
        client = exporter.OpenElectricityClient("k", retries=0, request_interval=-5.0)
        self.assertEqual(client.request_interval, 0.0)

    def test_the_default_interval_keeps_us_under_the_documented_burst_limit(self):
        self.assertLessEqual(
            1.0 / exporter.DEFAULT_REQUEST_INTERVAL,
            2.0,
            "default pacing must not exceed the plan's 2/s burst limit",
        )


class LookbackTest(unittest.TestCase):
    def test_lookback_window_covers_the_publication_gap(self):
        """A window narrower than the gap empties the dashboard every afternoon.

        Measured: at 15:37 the newest Eraring value was 11.6h old, so the old
        12h default was minutes from returning an empty series for every healthy
        battery in the fleet.
        """
        self.assertGreaterEqual(
            exporter.DEFAULT_LOOKBACK_HOURS * 3600.0, exporter.DEFAULT_MAX_SAMPLE_AGE
        )

    def test_idle_limit_matches_the_sample_age_limit(self):
        """One place decides how old is too old; two would eventually disagree."""
        self.assertEqual(exporter.DEFAULT_DROP_IDLE_SECONDS, exporter.DEFAULT_MAX_SAMPLE_AGE)

    def test_idle_limit_tolerates_one_missed_night(self):
        self.assertGreater(exporter.DEFAULT_DROP_IDLE_SECONDS, 24 * 3600)


class CalibrationSplitTest(unittest.TestCase):
    """Recovering the two legs of an interval from `stored_energy_delta` itself."""

    def test_pure_charging_is_all_one_leg(self):
        charged, discharged = exporter.charge_discharge_split(-50.0, 0.0, -50.0, 3600.0)
        self.assertAlmostEqual(charged, 50.0, places=9)
        self.assertAlmostEqual(discharged, 0.0, places=9)

    def test_pure_discharging_is_all_the_other_leg(self):
        charged, discharged = exporter.charge_discharge_split(50.0, 0.0, 50.0, 3600.0)
        self.assertAlmostEqual(charged, 0.0, places=9)
        self.assertAlmostEqual(discharged, 50.0, places=9)

    def test_a_zero_crossing_is_split_at_the_crossing_not_averaged(self):
        # 100 MW for an hour then 100 MW the other way: 50 MWh each side.
        charged, discharged = exporter.charge_discharge_split(-100.0, 0.0, 100.0, 7200.0)
        self.assertAlmostEqual(charged, 50.0, places=9)
        self.assertAlmostEqual(discharged, 50.0, places=9)

    def test_the_split_inverts_stored_energy_delta_at_every_efficiency(self):
        """The whole design rests on this, so it is checked by identity.

        `efficiency * charged - discharged` must reproduce what inference
        actually integrates, for every coefficient and every segment shape. If
        this drifts, calibration would be scoring a different model from the
        one in production - silently, and in a direction that looks plausible.
        """
        segments = [
            (100.0, 0.0, -100.0, 7200.0),
            (-100.0, 0.0, 100.0, 7200.0),
            (120.0, 0.0, -30.0, 3600.0),
            (-30.0, 0.0, 120.0, 3600.0),
            (50.0, 0.0, 50.0, 3600.0),
            (-50.0, 0.0, -50.0, 3600.0),
        ]
        for p0, t0, p1, t1 in segments:
            charged, discharged = exporter.charge_discharge_split(p0, t0, p1, t1)
            for efficiency in (0.0, 0.5, 0.7, 0.9, 1.0):
                self.assertAlmostEqual(
                    efficiency * charged - discharged,
                    exporter.stored_energy_delta(p0, t0, p1, t1, efficiency),
                    places=9,
                )

    def test_a_zero_length_interval_is_empty(self):
        self.assertEqual(exporter.charge_discharge_split(50.0, 100.0, 50.0, 100.0), (0.0, 0.0))


class CalibratePairTest(unittest.TestCase):
    """Turning two published readings plus a power series into an implied loss."""

    HOUR = 3600.0

    def series(self, *powers, start=0.0, step=None):
        step = self.HOUR if step is None else step
        return [(start + i * step, float(p)) for i, p in enumerate(powers)]

    def charge_cycle(self, hours_discharge=4, hours_charge=4, mw=100.0, start=0.0):
        """Discharge then charge, hourly, with the transition straddling a sample."""
        powers = [mw] * hours_discharge + [-mw] * hours_charge
        return self.series(*powers, start=start)

    def reading_for(self, power_series, true_efficiency, start_energy=500.0):
        """The stored energy a battery of that efficiency would report at the end."""
        charged = discharged = 0.0
        for (s0, p0), (s1, p1) in zip(power_series, power_series[1:]):
            c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
            charged += c
            discharged += d
        end = start_energy + true_efficiency * charged - discharged
        return charged, discharged, start_energy, end

    def test_it_recovers_a_known_efficiency_exactly(self):
        """The property that makes the whole feature worth having.

        A battery behaving at a known coefficient, integrated and re-solved,
        must give that coefficient back - not approximately, exactly, because
        the arithmetic is linear in the unknown.
        """
        power = self.charge_cycle()
        t1 = power[-1][0]
        for true_efficiency in (0.70, 0.80, 0.85, 0.90, 0.95):
            _, _, e0, e1 = self.reading_for(power, true_efficiency)
            observation = exporter.calibrate_pair(
                power, 0.0, e0, t1, e1, capacity_mwh=1000.0
            )
            self.assertIsNotNone(observation)
            self.assertAlmostEqual(
                observation["implied_efficiency"], true_efficiency, places=9
            )
            self.assertTrue(observation["plausible"])

    def test_the_deployed_efficiency_is_reproducible_from_the_observation(self):
        """Feeding the implied efficiency back through inference is a no-op."""
        power = self.charge_cycle()
        t1 = power[-1][0]
        _, _, e0, e1 = self.reading_for(power, 0.87)
        observation = exporter.calibrate_pair(power, 0.0, e0, t1, e1, capacity_mwh=1000.0)
        predicted = (
            observation["implied_efficiency"] * observation["charged_mwh"]
            - observation["discharged_mwh"]
        )
        self.assertAlmostEqual(predicted, observation["measured_delta_mwh"], places=9)

    def test_a_discharge_only_pair_is_refused(self):
        """No charging means no information about charging losses.

        Not a small denominator but a zero one, and the implied efficiency
        would be an infinite multiple of noise. This is the common case: a
        battery sitting out a calm afternoon publishes readings all day and
        says nothing at all.
        """
        power = self.series(*([100.0] * 6))
        self.assertIsNone(
            exporter.calibrate_pair(power, 0.0, 500.0, power[-1][0], 100.0,
                                    capacity_mwh=1000.0)
        )

    def test_a_pair_with_too_little_charging_is_refused(self):
        # 5 MWh against a 1000 MWh battery: half a percent of capacity, so the
        # implied coefficient divides by a number that cannot support it.
        power = self.series(*([-1.0] * 6))
        charged = exporter.charge_discharge_split(-1.0, 0.0, -1.0, self.HOUR)[0] * 5
        self.assertLess(charged, 0.01 * 1000.0)
        self.assertIsNone(
            exporter.calibrate_pair(power, 0.0, 500.0, power[-1][0], 500.0 + charged * 0.9,
                                    capacity_mwh=1000.0)
        )

    def test_the_capacity_floor_overrides_the_absolute_one(self):
        # A big battery charges trivially small amounts all the time; 1 MWh of
        # charging against 10 GWh of capacity is not a measurement.
        power = self.series(*([-30.0] * 3))
        charged = exporter.charge_discharge_split(-30.0, 0.0, -30.0, self.HOUR)[0] * 2
        self.assertGreater(charged, 1.0)
        self.assertLess(charged, 0.01 * 10000.0)
        self.assertIsNone(
            exporter.calibrate_pair(power, 0.0, 5000.0, power[-1][0], 5000.0 + charged * 0.9,
                                    capacity_mwh=10000.0)
        )

    def test_a_hole_wider_than_the_gap_limit_truncates_the_interval(self):
        # Power arrives for 3h then stops for a day. The pair is scored on
        # nothing, because the far end was never covered.
        power = self.series(*([100.0] * 4), step=self.HOUR) + [
            (25 * self.HOUR, 100.0), (26 * self.HOUR, 100.0)
        ]
        self.assertIsNone(
            exporter.calibrate_pair(power, 0.0, 500.0, power[-1][0], 400.0,
                                    capacity_mwh=1000.0)
        )

    def test_a_power_series_that_stops_short_of_the_reading_is_refused(self):
        # The integration finished 3h before the reading it is being scored
        # against, so the tail is unaccounted for.
        power = self.charge_cycle()
        self.assertIsNone(
            exporter.calibrate_pair(power, 0.0, 500.0, power[-1][0] + 3 * self.HOUR,
                                    100.0, capacity_mwh=1000.0)
        )

    def test_a_little_slack_at_the_far_end_is_allowed(self):
        """Readings are stamped from their own timestamp, which can lag power.

        Up to half an hour of that is normal and must not throw away pairs.
        """
        power = self.charge_cycle()
        _, _, e0, e1 = self.reading_for(power, 0.9)
        t1 = power[-1][0] + 600.0
        observation = exporter.calibrate_pair(power, 0.0, e0, t1, e1, capacity_mwh=1000.0)
        self.assertIsNotNone(observation)
        self.assertAlmostEqual(observation["implied_efficiency"], 0.9, places=9)

    def test_an_impossible_implied_efficiency_is_counted_not_discarded(self):
        """Above 1.0 the battery stored more than the grid delivered.

        Returned with `plausible=False` rather than dropped: a run of these is
        itself the signal that the readings or the power series are wrong, and
        quietly filtering them out is how a broken feed looks like a healthy
        calibration.
        """
        power = self.charge_cycle()
        _, charged, _, _ = self.reading_for(power, 0.9)
        # Report a gain far larger than any efficiency could explain.
        observation = exporter.calibrate_pair(
            power, 0.0, 500.0, power[-1][0], 500.0 + charged, capacity_mwh=1000.0
        )
        self.assertIsNotNone(observation)
        self.assertFalse(observation["plausible"])
        self.assertGreater(observation["implied_efficiency"], 1.0)

    def test_samples_outside_the_interval_are_ignored(self):
        """Power either side of the pair must not contribute to it."""
        inner = self.charge_cycle()
        power = [(inner[0][0] - 5 * self.HOUR, -500.0)] + inner + [
            (inner[-1][0] + 5 * self.HOUR, 500.0)
        ]
        _, _, e0, e1 = self.reading_for(inner, 0.9)
        observation = exporter.calibrate_pair(
            power, 0.0, e0, inner[-1][0], e1, capacity_mwh=1000.0
        )
        self.assertAlmostEqual(observation["implied_efficiency"], 0.9, places=9)

    def test_a_backwards_or_empty_interval_is_refused(self):
        power = self.charge_cycle()
        self.assertIsNone(exporter.calibrate_pair(power, 100.0, 500.0, 0.0, 400.0))
        self.assertIsNone(exporter.calibrate_pair([], 0.0, 500.0, 100.0, 400.0))
        self.assertIsNone(exporter.calibrate_pair([(0.0, -50.0)], 0.0, 500.0, 100.0, 400.0))

    def test_missing_inputs_are_refused_rather_than_crashing(self):
        power = self.charge_cycle()
        self.assertIsNone(exporter.calibrate_pair(power, None, 500.0, power[-1][0], 400.0))
        self.assertIsNone(exporter.calibrate_pair(power, 0.0, None, power[-1][0], 400.0))
        self.assertIsNone(exporter.calibrate_pair(power, 0.0, 500.0, None, 400.0))
        self.assertIsNone(exporter.calibrate_pair(power, 0.0, 500.0, power[-1][0], None))


def bucket_error(summary, label):
    return (summary.get("buckets") or {}).get(label, {}).get("median")


class SummariseCalibrationTest(unittest.TestCase):
    """Turning scored pairs into a number worth - or not worth - acting on."""

    HOUR = 3600.0

    def observation(self, unit, window_hours, charged, discharged, measured,
                    capacity=1000.0, plausible=True):
        return {
            "unit": unit,
            "t0": 0.0,
            "t1": window_hours * self.HOUR,
            "window_hours": float(window_hours),
            "charged_mwh": charged,
            "discharged_mwh": discharged,
            "measured_delta_mwh": measured,
            "implied_efficiency": (measured + discharged) / charged if charged else None,
            "capacity_mwh": capacity,
            "plausible": plausible,
        }

    def pairs_across(self, units, window_hours, true_efficiency):
        """Scored pairs behaving at `true_efficiency` over a given window."""
        charged = 100.0 * window_hours
        discharged = 60.0 * window_hours
        measured = true_efficiency * charged - discharged
        return [
            self.observation(unit, window_hours, charged, discharged, measured)
            for unit in units
        ]

    def test_no_pairs_yields_no_claim_rather_than_a_guess(self):
        summary = exporter.summarise_calibration([], 0.9)
        self.assertEqual(summary["pairs"], 0)
        self.assertIsNone(summary["implied_efficiency"])
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("no plausible pairs", summary["reason"])

    def test_implausible_pairs_are_excluded_from_the_weighted_estimate(self):
        observations = self.pairs_across(["A1"], 8, 0.9)
        observations.append(
            self.observation("A1", 8, 100.0, 0.0, 9000.0, plausible=False)
        )
        summary = exporter.summarise_calibration(observations, 0.9)
        self.assertEqual(summary["pairs"], 1)
        self.assertEqual(summary["pairs_implausible"], 1)
        self.assertAlmostEqual(summary["implied_efficiency"], 0.9, places=9)

    def test_the_implied_efficiency_is_charged_weighted_not_a_mean_of_ratios(self):
        """One long charge must not outvote one short one on equal terms.

        Averaging per-pair ratios gives a small pair the same vote as a night
        of charging, and the short pairs are precisely the noisy ones - they
        divide by the least energy.
        """
        big = self.observation("A1", 12, 1200.0, 0.0, 0.9 * 1200.0)
        small = self.observation("A1", 2, 100.0, 0.0, 0.70 * 100.0)
        summary = exporter.summarise_calibration([big, small], 0.9)
        weighted = (0.9 * 1200.0 + 0.70 * 100.0) / 1300.0
        unweighted = (0.9 + 0.70) / 2
        self.assertAlmostEqual(summary["implied_efficiency"], weighted, places=9)
        self.assertNotAlmostEqual(summary["implied_efficiency"], unweighted, places=3)

    def test_too_few_pairs_means_no_recommendation(self):
        """The floor exists because of a term that was adopted on two pairs."""
        observations = self.pairs_across(["A1", "A2"], 12, 0.95)
        summary = exporter.summarise_calibration(observations, 0.9, min_pairs=5)
        self.assertEqual(summary["pairs"], 2)
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("need 5", summary["reason"])

    def test_errors_are_stratified_by_window_length(self):
        """A short-window regression has to stay visible in the output.

        One mean over all windows is how a change that fixes long nights and
        wrecks short ones looks like an improvement.
        """
        observations = (
            self.pairs_across(["A1", "A2", "A3"], 12, 0.9)
            + self.pairs_across(["A1", "A2", "A3"], 1, 0.9)
        )
        summary = exporter.summarise_calibration(observations, 0.9)
        # Both windows are exactly reproduced by the deployed coefficient.
        self.assertAlmostEqual(bucket_error(summary, "12h+"), 0.0, places=9)
        self.assertAlmostEqual(bucket_error(summary, "0-2h"), 0.0, places=9)
        # And they are counted separately, not pooled.
        self.assertEqual(summary["buckets"]["12h+"]["n"], 3)
        self.assertEqual(summary["buckets"]["0-2h"]["n"], 3)

    def test_a_better_efficiency_is_recommended_with_ample_evidence(self):
        observations = self.pairs_across(["A1", "A2", "A3", "A4", "A5", "A6"], 12, 0.96)
        summary = exporter.summarise_calibration(observations, 0.9, min_pairs=5)
        self.assertAlmostEqual(summary["recommended_efficiency"], 0.96, places=9)
        self.assertIn("best over 12h+", summary["reason"])

    def test_a_tied_sweep_recommends_nothing_when_the_deployed_value_is_already_best(self):
        """The sweep ties constantly, so the deployed value is often optimal.

        0.94 and 0.96 are equally wrong for a fleet behaving at 0.95, and the
        grid has no 0.95 to land on - so the flag already sits on a tied-best
        value. The right answer is no recommendation at all, not a suggestion
        to change to whichever member of the tie iteration met first. Nothing is
        gained by moving, so nothing is proposed.
        """
        observations = self.pairs_across(["A1", "A2", "A3", "A4", "A5", "A6"], 12, 0.95)
        summary = exporter.summarise_calibration(observations, 0.94, min_pairs=5)
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("no candidate beats", summary["reason"])

    def test_a_tied_sweep_prefers_the_value_nearest_the_deployed_one(self):
        """Same tie, but the deployed value is worse than both members.

        0.9 sits two grid steps from the truth while 0.94 and 0.96 sit one, so
        moving is justified - and the recommendation is the *nearer* of the two
        equally good values, so the suggestion is the smallest change the
        evidence supports rather than an arbitrary one.
        """
        observations = self.pairs_across(["A1", "A2", "A3", "A4", "A5", "A6"], 12, 0.95)
        summary = exporter.summarise_calibration(observations, 0.9, min_pairs=5)
        self.assertAlmostEqual(summary["recommended_efficiency"], 0.94, places=9)

    def test_a_candidate_that_helps_long_windows_and_hurts_short_ones_is_refused(self):
        """The specific failure mode the deployed coefficient was rejected for.

        The evidence has to be built so a coefficient fits the long windows and
        misses the short ones, which is what the sweep finds and what the
        history says was acted on. Refusing it is the entire point of the
        short-window check.
        """
        observations = []
        for unit in ("A1", "A2", "A3", "A4", "A5", "A6"):
            # Long windows: 0.9 is wrong by 5% of charge; short ones: exact.
            observations.append(self.observation(unit, 12, 1200.0, 0.0, 0.85 * 1200.0))
            observations.append(self.observation(unit, 1, 100.0, 0.0, 0.9 * 100.0))
        summary = exporter.summarise_calibration(observations, 0.9, min_pairs=5)
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("worse over 0-2h", summary["reason"])  # label, not a tie

    def test_nothing_beats_the_deployed_value_so_nothing_is_recommended(self):
        observations = self.pairs_across(["A1", "A2", "A3", "A4", "A5", "A6"], 12, 0.90)
        summary = exporter.summarise_calibration(observations, 0.90, min_pairs=5)
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("no candidate beats", summary["reason"])

    def test_no_single_bucket_with_enough_pairs_means_no_claim(self):
        """Pairs spread thin across windows are not a basis for a decision.

        Eight pairs, two in each of four windows, clears the overall count and
        clears nothing else. The floor is applied per bucket because five pairs
        of twelve hours and five pairs of two hours are not the same evidence -
        the short ones divide by the least energy and are the noisy ones.
        """
        observations = []
        for i, window in enumerate((1, 4, 8, 20)):
            for j in range(2):
                charged = 100.0 * window
                observations.append(
                    self.observation("A%d" % (i * 2 + j), window, charged, 0.0, 0.9 * charged)
                )
        summary = exporter.summarise_calibration(observations, 0.9, min_pairs=5)
        self.assertIsNone(summary["recommended_efficiency"])
        self.assertIn("no single window bucket", summary["reason"])

    def test_the_deployed_value_is_always_in_the_sweep(self):
        """Otherwise "improves on current" is measured against a grid that
        never contained the value actually running."""
        observations = self.pairs_across(["A1", "A2", "A3", "A4", "A5", "A6"], 12, 0.93)
        summary = exporter.summarise_calibration(observations, 0.93, min_pairs=5)
        self.assertIsNone(summary["recommended_efficiency"])

    def test_per_unit_errors_are_reported_separately(self):
        """One battery with an implausible history must not hide behind the fleet."""
        observations = self.pairs_across(["GOOD"], 12, 0.9)
        observations.append(self.observation("ODD", 12, 1200.0, 0.0, 0.6 * 1200.0))
        summary = exporter.summarise_calibration(observations, 0.9)
        per_unit = summary["per_unit"]
        self.assertIn("GOOD", per_unit)
        self.assertIn("ODD", per_unit)
        self.assertLess(per_unit["GOOD"]["median"], per_unit["ODD"]["median"])


class EfficiencyCalibratorStoreTest(unittest.TestCase):
    """The store: keyed so polls cannot double-count, bounded, survives restarts."""

    HOUR = 3600.0

    def power(self, hours=8, mw=100.0, start=0.0):
        return [(start + i * self.HOUR, mw if i < hours // 2 else -mw) for i in range(hours)]

    def good_pair(self, unit="A1", true_efficiency=0.9, start=0.0, hours=8):
        power = self.power(hours=hours, start=start)
        charged = discharged = 0.0
        for (s0, p0), (s1, p1) in zip(power, power[1:]):
            c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
            charged += c
            discharged += d
        e0 = 500.0
        e1 = e0 + true_efficiency * charged - discharged
        return exporter.calibrate_pair(
            power, start, e0, power[-1][0], e1, capacity_mwh=1000.0
        )

    def calibrated(self, **kwargs):
        options = dict(path=None, retention_days=90.0, min_pairs=5)
        options.update(kwargs)
        return exporter.EfficiencyCalibrator(**options)

    def test_a_pair_is_recorded_with_its_unit(self):
        store = self.calibrated()
        power = self.power()
        observation = store.observe("A1", 0.0, 500.0, power[-1][0], 400.0,
                                    power, capacity_mwh=1000.0)
        self.assertIsNotNone(observation)
        self.assertEqual(store.pairs(), 1)
        self.assertEqual(store.observations()[0]["unit"], "A1")

    def test_a_refused_pair_records_nothing(self):
        """Most pairs say nothing; that must not look like evidence piling up."""
        store = self.calibrated()
        power = self.power()
        self.assertIsNone(store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, []))
        self.assertEqual(store.pairs(), 0)

    def test_a_refused_pair_still_learns_the_labels(self):
        """Otherwise a unit that never charges never gets its series exported."""
        store = self.calibrated()
        power = self.power()
        store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, [],
                      sample={"unit": "A1", "facility": "F1", "facility_name": "One"})
        self.assertIn("A1", store.unit_labels())
        self.assertEqual(store.unit_labels()["A1"]["facility_name"], "One")

    def test_reseeing_the_same_reading_replaces_rather_than_adds(self):
        """Polls repeat; the same published reading must not count twice."""
        store = self.calibrated()
        power = self.power()
        for _ in range(3):
            store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
        self.assertEqual(store.pairs(), 1)

    def test_pairs_accumulate_across_readings(self):
        store = self.calibrated()
        start = 0.0
        for i in range(4):
            power = self.power(start=start + i * 12 * self.HOUR)
            charged = discharged = 0.0
            for (s0, p0), (s1, p1) in zip(power, power[1:]):
                c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
                charged += c
                discharged += d
            store.observe("A1", power[0][0], 500.0 + i, power[-1][0],
                          500.0 + i + 0.9 * charged - discharged, power, capacity_mwh=1000.0)
        self.assertEqual(store.pairs(), 4)

    def test_retention_prunes_old_observations(self):
        store = self.calibrated(retention_days=1.0)
        old = self.power(start=0.0)
        store.observe("A1", 0.0, 500.0, old[-1][0], 400.0, old, capacity_mwh=1000.0)
        self.assertEqual(store.pairs(), 1)
        store.prune(10 * 86400.0)
        self.assertEqual(store.pairs(), 0)
        self.assertEqual(store.observations(), [])

    def test_zero_retention_keeps_everything(self):
        """Disabling the bound means unbounded, as it does for power history."""
        store = self.calibrated(retention_days=0)
        power = self.power()
        store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
        store.prune(1e12)
        self.assertEqual(store.pairs(), 1)

    def test_it_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "efficiency-calibration.json")
            first = self.calibrated(path=path)
            power = self.power()
            first.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power,
                          capacity_mwh=1000.0,
                          sample={"unit": "A1", "facility": "F1", "name": "n"})
            first.observe("A2", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
            first.save()

            second = self.calibrated(path=path)
            self.assertEqual(second.load(), 2)
            self.assertEqual(second.observations()[0]["unit"], "A1")
            self.assertEqual(second.observations()[0]["charged_mwh"],
                             first.observations()[0]["charged_mwh"])
            self.assertEqual(second.unit_labels()["A1"]["facility"], "F1")

    def test_a_corrupt_file_starts_cold_instead_of_raising(self):
        """Evidence not worth an outage, same as the power history."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "efficiency-calibration.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            store = self.calibrated(path=path)
            self.assertEqual(store.load(), 0)
            power = self.power()
            store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
            self.assertEqual(store.pairs(), 1)

    def test_rows_it_cannot_read_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "efficiency-calibration.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "units": {"A1": [["bad", None], [1, {"t1": 2}]]}},
                          handle)
            store = self.calibrated(path=path)
            self.assertEqual(store.load(), 1)
            self.assertEqual(store.observations()[0]["unit"], "A1")

    def test_no_path_means_no_writes_and_no_error(self):
        store = self.calibrated(path=None)
        power = self.power()
        store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
        store.save()
        self.assertEqual(store.pairs(), 1)

    def test_an_unwritable_path_is_a_warning_not_an_outage(self):
        store = self.calibrated(path="/proc/nonexistent-dir/calibration.json")
        power = self.power()
        store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0)
        store.save()
        self.assertEqual(store.pairs(), 1)

    def test_it_never_changes_the_deployed_coefficient(self):
        """The store holds evidence. Acting on it is a human's decision.

        Worth pinning directly: a feature that quietly rewrote the flag it is
        measuring would make every recommendation self-fulfilling.
        """
        store = self.calibrated()
        observations = []
        start = 0.0
        for i in range(6):
            power = self.power(start=start + i * 24 * self.HOUR)
            charged = discharged = 0.0
            for (s0, p0), (s1, p1) in zip(power, power[1:]):
                c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
                charged += c
                discharged += d
            store.observe("A1", power[0][0], 500.0, power[-1][0],
                          500.0 + 0.96 * charged - discharged, power, capacity_mwh=1000.0)
        summary = store.summary(deployed_efficiency=0.9)
        self.assertAlmostEqual(summary["recommended_efficiency"], 0.96, places=9)
        self.assertNotIn("deployed", summary)
        self.assertNotIn("applied", summary)

    def test_summary_carries_labels_for_export(self):
        store = self.calibrated()
        power = self.power()
        store.observe("A1", 0.0, 500.0, power[-1][0], 400.0, power, capacity_mwh=1000.0,
                      sample={"unit": "A1", "facility": "F1", "facility_name": "One",
                              "region": "NSW", "status": "operating"})
        summary = store.summary(deployed_efficiency=0.9)
        self.assertEqual(summary["labels"]["A1"]["region"], "NSW")


class CalibrationRenderTest(unittest.TestCase):
    """The calibration block in the exposition, including its absences.

    The shape of the output matters as much as the numbers. A metric that reads
    zero must be distinguishable from a metric that was never measured, because
    one means "the deployed coefficient reproduces every reading" and the other
    means "nothing was measured yet" - opposite states that both render as a
    number unless the exporter is careful.
    """

    def summary(self, **over):
        base = {
            "pairs": 6,
            "pairs_implausible": 0,
            "implied_efficiency": 0.93,
            "error_ratio": 0.012,
            "recommended_efficiency": None,
            "reason": "no candidate beats the deployed 0.90",
            "buckets": {"12h+": {"n": 6, "median": 0.012}},
            "per_unit": {
                "ERB01": {"n": 4, "median": 0.008},
                "ERB02": {"n": 2, "median": 0.031},
            },
            "labels": {
                "ERB01": {"facility": "ERAR", "unit": "ERB01", "facility_name": "Eraring",
                          "region": "NSW", "status": "operating"},
                "ERB02": {"facility": "ERAR", "unit": "ERB02", "facility_name": "Eraring",
                          "region": "NSW", "status": "operating"},
            },
        }
        base.update(over)
        return base

    def render(self, summary):
        return exporter.render(state(calibration=summary), self.NOW)

    NOW = 1790742930.0

    def lines_named(self, text, metric):
        return [l for l in text.splitlines() if l.startswith(metric)]

    def test_every_calibration_metric_has_help_and_type(self):
        text = self.render(self.summary())
        for metric in (
            "oe_battery_inferred_calibration_pairs",
            "oe_battery_inferred_calibration_error_ratio",
            "oe_battery_inferred_calibration_recommended_efficiency",
            "oe_battery_inferred_calibration_plausible_pairs",
        ):
            self.assertEqual(text.count("# HELP %s " % metric), 1, metric)
            self.assertEqual(text.count("# TYPE %s " % metric), 1, metric)

    def test_help_and_type_appear_exactly_once(self):
        text = self.render(self.summary())
        for line in self.lines_named(text, "# TYPE oe_battery_inferred_calibration"):
            metric = line.split()[2]
            self.assertEqual(text.count("# HELP %s " % metric), 1, metric)

    def test_per_unit_series_carry_the_standard_labels(self):
        # Missing these would drop the panel out of every Grafana variable that
        # selects a battery, which is how a metric ends up silently unused.
        text = self.render(self.summary())
        lines = self.lines_named(text, "oe_battery_inferred_calibration_pairs{")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all('unit="ERB0' in l for l in lines))
        self.assertTrue(all('facility="ERAR"' in l for l in lines))
        self.assertTrue(all('name="Eraring"' in l for l in lines))
        self.assertTrue(all('region="NSW"' in l for l in lines))
        self.assertTrue(all('status="operating"' in l for l in lines))

    def test_no_recommendation_means_the_metric_is_absent_not_zero(self):
        """Zero would read as "use zero efficiency", which is a live instruction.

        Absent reads as "nothing to report", which is what it means. The same
        reasoning is why `pairs` is absent for a unit with no evidence rather
        than published as 0.
        """
        text = self.render(self.summary())
        self.assertIn("# TYPE oe_battery_inferred_calibration_recommended_efficiency gauge",
                      text)
        self.assertEqual(
            [l for l in text.splitlines()
             if l.startswith("oe_battery_inferred_calibration_recommended_efficiency")
             and not l.startswith("#")],
            [],
        )

    def test_a_recommendation_is_published_as_a_bare_series(self):
        # Fleet-wide by design: a recommendation is not a per-battery claim, and
        # giving it per-battery labels would mean several values that have to
        # agree with each other.
        text = self.render(self.summary(recommended_efficiency=0.96))
        self.assertIn(
            "oe_battery_inferred_calibration_recommended_efficiency 0.96", text
        )

    def test_a_unit_with_no_pairs_is_absent_rather_than_zero(self):
        text = self.render(self.summary(per_unit={"ERB01": {"n": 4, "median": 0.008}}))
        lines = self.lines_named(text, "oe_battery_inferred_calibration_pairs{")
        self.assertEqual(len(lines), 1)
        self.assertIn('unit="ERB01"', lines[0])

    def test_nothing_is_published_when_calibration_is_off(self):
        # The flag is opt-in, and an unflagged exporter must be byte-identical
        # to how it behaved before this existed.
        text = exporter.render(state(), self.NOW)
        self.assertNotIn("oe_battery_inferred_calibration", text)

    def test_no_evidence_yet_still_publishes_the_pair_count(self):
        """Zero pairs is a real state worth graphing, unlike no recommendation."""
        text = self.render(self.summary(pairs=0, per_unit={}, implied_efficiency=None,
                                        error_ratio=None))
        self.assertIn("oe_battery_inferred_calibration_plausible_pairs 0", text)
        self.assertEqual(self.lines_named(text, "oe_battery_inferred_calibration_pairs{"), [])

    def test_implausible_pairs_are_visible_alongside_the_usable_ones(self):
        text = self.render(self.summary(pairs=5, pairs_implausible=2))
        self.assertIn("oe_battery_inferred_calibration_plausible_pairs 5", text)

    def test_the_panel_survives_a_unit_with_no_labels_recorded(self):
        # Labels only get captured alongside a scored pair, so a unit whose
        # pairs all predate this build would export without them.
        text = self.render(self.summary(labels={}))
        lines = self.lines_named(text, "oe_battery_inferred_calibration_pairs{")
        self.assertEqual(len(lines), 2)
        self.assertIn('unit=""', lines[0])


class CalibrationPlumbingTest(unittest.TestCase):
    """The flag, the constructor, and the point where pairs actually form."""

    def test_calibration_is_off_unless_asked_for(self):
        self.assertIsNone(exporter._build_calibration(argparse.Namespace(
            efficiency_calibration=False, fleet_cache_file=None)))
        self.assertIsNone(exporter._build_calibration(argparse.Namespace(
            efficiency_calibration=False, fleet_cache_file="/cache/fleet.json")))

    def test_it_defaults_alongside_the_fleet_cache(self):
        """A deployment that already mounts a cache volume should not need a
        second flag to remember where its evidence lives."""
        self.assertEqual(
            exporter.resolve_calibration_path(None, "/cache/fleet.json"),
            "/cache/efficiency-calibration.json",
        )
        self.assertEqual(
            exporter.resolve_calibration_path("/elsewhere/c.json", "/cache/fleet.json"),
            "/elsewhere/c.json",
        )
        self.assertIsNone(exporter.resolve_calibration_path(None, None))

    def test_it_warms_from_disk_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "calibration.json")
            seed = exporter.EfficiencyCalibrator(path)
            power = [(i * 3600.0, 100.0 if i < 4 else -100.0) for i in range(8)]
            charged = discharged = 0.0
            for (s0, p0), (s1, p1) in zip(power, power[1:]):
                c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
                charged += c
                discharged += d
            seed.observe("A1", 0.0, 500.0, power[-1][0],
                         500.0 + 0.9 * charged - discharged, power, capacity_mwh=1000.0)
            seed.save()

            built = exporter._build_calibration(argparse.Namespace(
                efficiency_calibration=True,
                efficiency_calibration_file=path,
                efficiency_calibration_days=90.0,
                calibration_max_gap_hours=2.0,
                calibration_min_charge_mwh=1.0,
                calibration_min_pairs=5,
                fleet_cache_file=None,
            ))
            self.assertIsNotNone(built)
            self.assertEqual(built.pairs(), 1)

    def test_a_scraper_without_a_calibrator_does_none_of_it(self):
        scraper = make_scraper(ScraperClient(series={"ERB": both_metrics()}))
        self.assertIsNone(scraper.calibration)

    def test_the_calibrator_is_handed_to_the_scraper(self):
        # Guards the plumbing: a flag parsed but never threaded through would
        # look like a working knob and silently record nothing.
        store = exporter.EfficiencyCalibrator(None)
        scraper = make_scraper(
            ScraperClient(series={"ERB": both_metrics()}),
            enable_inferred=True, infer_charge_efficiency=0.9, calibration=store,
        )
        self.assertIs(scraper.calibration, store)

    def test_a_second_reading_forms_a_pair(self):
        """The end-to-end path: poll, poll again with a newer reading.

        Goes through `poll_once` rather than `_record_readings` because the point
        is that the pairing happens as a side effect of ordinary polling - no
        separate calibration job, no second API request.
        """
        store = exporter.EfficiencyCalibrator(None, min_pairs=1)
        anchor = 0.0
        scraper = make_scraper(
            ScraperClient(series={"ERB": both_metrics()}),
            top=1, enable_inferred=True, infer_charge_efficiency=0.9,
            calibration=store,
        )
        scraper._record_readings(
            [{"unit": "ERB", "scrape_success": True, "sampled_at": anchor,
              "stored_mwh": 500.0, "capacity_mwh": 1000.0,
              "facility": "ERAR", "facility_name": "Eraring", "region": "NSW",
              "status": "operating"}],
            anchor + 3600.0,
        )
        self.assertEqual(store.pairs(), 0, "the first reading has nothing to pair with")
        power = [(anchor + i * 3600.0, 100.0) for i in range(4)]
        later = anchor + 4 * 3600.0
        scraper._power_history = exporter.PowerHistory(None)
        for stamp, value in power:
            scraper._power_history.record({"ERB": [(stamp, value)]}, now=stamp)
        scraper._record_readings(
            [{"unit": "ERB", "scrape_success": True, "sampled_at": later,
              "stored_mwh": 380.0, "capacity_mwh": 1000.0,
              "facility": "ERAR", "facility_name": "Eraring", "region": "NSW",
              "status": "operating"}],
            later + 3600.0,
        )
        # Discharge only: no information about charging losses, so no pair.
        self.assertEqual(store.pairs(), 0)

    def test_an_out_of_order_reading_does_not_form_a_backwards_pair(self):
        """A reading older than the anchor must not become an interval.

        Integration would run backwards and produce a negative-duration pair that
        divides into a plausible-looking number.
        """
        store = exporter.EfficiencyCalibrator(None)
        scraper = make_scraper(
            ScraperClient(series={"ERB": both_metrics()}),
            top=1, enable_inferred=True, calibration=store,
        )
        now = 100000.0
        scraper._record_readings(
            [{"unit": "ERB", "scrape_success": True, "sampled_at": now,
              "stored_mwh": 500.0, "capacity_mwh": 1000.0}],
            now + 1,
        )
        scraper._record_readings(
            [{"unit": "ERB", "scrape_success": True, "sampled_at": now - 7200,
              "stored_mwh": 480.0, "capacity_mwh": 1000.0}],
            now + 2,
        )
        self.assertEqual(store.pairs(), 0)

    def test_the_deployed_coefficient_survives_a_recommendation_against_it(self):
        """The exporter must not act on its own finding.

        Worth pinning at the scraper level rather than only on the summary: the
        summary could be right and the wiring could still reassign the flag.
        """
        store = exporter.EfficiencyCalibrator(None, min_pairs=1)
        anchor = 0.0
        power = [(anchor + i * 3600.0, 100.0 if i < 4 else -100.0) for i in range(8)]
        charged = discharged = 0.0
        for (s0, p0), (s1, p1) in zip(power, power[1:]):
            c, d = exporter.charge_discharge_split(p0, s0, p1, s1)
            charged += c
            discharged += d
        for _ in range(6):
            store.observe("ERB", power[0][0], 500.0, power[-1][0],
                          500.0 + 0.98 * charged - discharged, power,
                          capacity_mwh=1000.0)
        scraper = make_scraper(
            ScraperClient(series={"ERB": both_metrics()}),
            top=1, enable_inferred=True, infer_charge_efficiency=0.9, calibration=store,
        )
        summary = scraper.calibration.summary(scraper.infer_charge_efficiency)
        self.assertIsNotNone(summary["recommended_efficiency"])
        self.assertEqual(scraper.infer_charge_efficiency, 0.9)


if __name__ == "__main__":
    unittest.main()
