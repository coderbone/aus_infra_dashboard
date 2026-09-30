#!/usr/bin/env python3
"""Tests for oe_battery_exporter.py.

Offline: every upstream response is served from scrapers/fixtures/, sampled
verbatim from api.openelectricity.org.au on 2026-09-30. No test touches the
network. The `/me` fixture has the account's name, email and key id redacted -
it is a committed file and the real ones were not.

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
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oe_battery_exporter as exporter  # noqa: E402
from oe_battery_exporter import AuthError, ScrapeError  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Read out of oe_facilities_battery.json.
ERB_CAPACITY_MWH = 1997.0
WTAHB_CAPACITY_MWH = 1680.0
# Read out of oe_storage_battery_ERB.json: the newest non-null sample on the
# ERB01 series, and its timestamp.
ERB_NEWEST_MWH = 334.768342
ERB_NEWEST_TS = "2026-09-29T23:00:00+10:00"
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
    """Stands in for OpenElectricityClient, keyed by facility code."""

    def __init__(self, series=None, error=None, credits=494):
        self.series = series or {}
        self.error = error
        self.credits = credits
        self.requests_made = 0
        self.calls = []

    def storage_battery(self, facility_code, network, lookback_hours, now):
        self.requests_made += 1
        self.calls.append((facility_code, network))
        if self.error:
            raise self.error
        return self.series.get(facility_code, [])

    def credits_remaining(self):
        return self.credits


def erb_row():
    return next(r for r in exporter.unit_rows(facilities()) if r["facility"] == "ERB")


def series_from_fixture(unit="ERB01"):
    block = storage_erb()["data"][0]
    for result in block["results"]:
        if result["name"] == "storage_battery_%s" % unit:
            newest = exporter.latest_sample(result["data"])
            return [(unit, newest)]
    raise AssertionError("fixture has no series %s" % unit)


class PollCycleTest(unittest.TestCase):
    def setUp(self):
        self.now = exporter.parse_timestamp("2026-09-30T12:00:00+10:00")
        self.row = erb_row()

    def cycle(self, client, rows=None, max_age=exporter.DEFAULT_MAX_SAMPLE_AGE):
        return exporter.poll_cycle(
            client, rows or [self.row], "NEM", 12, max_age, self.now
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
            ("ERBG1", (self.now, 999.0)),
            ("ERBL1", (self.now, 888.0)),
        ]
        result = self.cycle(FakeClient(series={"ERB": series}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["series_without_capacity"], 2)

    def test_wrong_network_is_passed_through(self):
        client = FakeClient(series={"ERB": []})
        self.cycle(client, rows=[dict(self.row, network="WEM")])
        self.assertEqual(client.calls[0][1], "WEM")

    def test_http_404_becomes_an_unread_sample_not_an_exception(self):
        """quirk 4: absent facilities are an expected answer."""
        client = FakeClient(error=ScrapeError("GET ... -> HTTP 404: No data"))
        result = self.cycle(client)
        self.assertEqual(result["samples"][0]["scrape_success"], False)
        self.assertIsNone(result["samples"][0]["soc"])

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

        def storage(facility_code, network, lookback_hours, now):
            client.requests_made += 1
            client.calls.append((facility_code, network))
            if facility_code == "WTAHB":
                raise ScrapeError("HTTP 404: No data")
            return client.series.get(facility_code, [])

        client.storage_battery = storage
        result = exporter.poll_cycle(client, rows, "NEM", 12, exporter.DEFAULT_MAX_SAMPLE_AGE, self.now)
        by_facility = {s["facility"]: s for s in result["samples"]}
        self.assertTrue(by_facility["ERB"]["scrape_success"])
        self.assertFalse(by_facility["WTAHB"]["scrape_success"])
        self.assertIn("ERB", wanted)

    def test_soc_is_clamped_to_zero_one(self):
        for mwh, expected in ((-50.0, 0.0), (ERB_CAPACITY_MWH * 3, 1.0)):
            series = [("ERB01", (self.now, mwh))]
            result = self.cycle(FakeClient(series={"ERB": series}))
            self.assertEqual(result["samples"][0]["soc"], expected)

    def test_sample_older_than_max_age_is_dropped(self):
        old = (self.now - 48 * 3600, 500.0)
        result = self.cycle(
            FakeClient(series={"ERB": [("ERB01", old)]}), max_age=36 * 3600
        )
        self.assertEqual(result["series_too_stale"], 1)
        self.assertFalse(result["samples"][0]["scrape_success"])

    def test_empty_response_leaves_the_battery_unread(self):
        result = self.cycle(FakeClient(series={"ERB": []}))
        self.assertEqual(len(result["samples"]), 1)
        self.assertFalse(result["samples"][0]["scrape_success"])

    def test_all_null_series_count_as_unread(self):
        client = FakeClient(series={"ERB": []})
        result = self.cycle(client)
        self.assertEqual(result["series_without_capacity"], 0)


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
        "scrape_success": True,
        "rank": 1,
    }
    base.update(over)
    return base


def state(samples=None, **over):
    base = {
        "samples": samples if samples is not None else [sample()],
        "oe_batteries_enumerated": 74,
        "oe_batteries_in_scope": 10,
        "oe_batteries_monitored": 1,
        "oe_battery_series_without_capacity": 2,
        "oe_battery_series_too_stale": 0,
        "oe_battery_fleet_capacity_mwh": 32085.47,
        "oe_battery_monitored_capacity_mwh": ERB_CAPACITY_MWH,
        "oe_poll_cycle_duration_seconds": 4.32,
        "oe_last_poll_timestamp_seconds": 1790742930.0,
        "oe_last_fleet_refresh_timestamp_seconds": 1790742926.0,
        "oe_api_credits_remaining": 494,
        "oe_api_requests_total": 12,
    }
    base.update(over)
    return base


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
        for metric in (
            "oe_battery_soc_ratio",
            "oe_battery_energy_stored_mwh",
            "oe_battery_capacity_storage_mwh",
            "oe_battery_last_sample_timestamp_seconds",
            "oe_battery_sample_age_seconds",
            "oe_battery_capacity_rank",
            "oe_battery_scrape_success",
        ):
            self.assertEqual(self.text.count("# HELP %s " % metric), 1, metric)
            self.assertEqual(self.text.count("# TYPE %s " % metric), 1, metric)

    def test_ends_with_newline(self):
        self.assertTrue(self.text.endswith("\n"))

    def test_soc_carries_the_identifying_labels(self):
        line = self.lines_named("oe_battery_soc_ratio")[0]
        for label in ('facility="ERB"', 'unit="ERB01"', 'name="Eraring"',
                      'region="NSW1"', 'status="operating"'):
            self.assertIn(label, line)

    def test_missing_value_is_omitted_not_zero(self):
        """A battery that could not be read has an unknown SOC.

        Emitting 0 would read as "flat and empty" on the dashboard, which is a
        different and much more alarming claim than "no reading".
        """
        unread = sample(soc=None, stored_mwh=None, sampled_at=None, scrape_success=False)
        text = exporter.render(state([unread]), self.now)
        for metric in (
            "oe_battery_soc_ratio",
            "oe_battery_energy_stored_mwh",
            "oe_battery_last_sample_timestamp_seconds",
            "oe_battery_sample_age_seconds",
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
        for metric in (
            "oe_battery_soc_ratio",
            "oe_battery_energy_stored_mwh",
            "oe_battery_capacity_storage_mwh",
            "oe_battery_last_sample_timestamp_seconds",
            "oe_battery_sample_age_seconds",
            "oe_battery_capacity_rank",
            "oe_battery_scrape_success",
        ):
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
            client.storage_battery("X", "NEM", 12, 1790686800.0)
        self.assertIn("No data available", str(ctx.exception))
        client._request = original

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


class ScraperTest(unittest.TestCase):
    def test_poll_once_produces_exposition(self):
        client = ScraperClient(series={"ERB": series_from_fixture()})
        scraper = make_scraper(client)
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_soc_ratio{facility=\"ERB\"", body)
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
        """Ten at an hourly poll is 240 requests/day, inside the 366/day bucket."""
        self.assertEqual(exporter.DEFAULT_TOP, 10)
        self.assertEqual(exporter.DEFAULT_POLL_INTERVAL, 3600.0)


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

    def static(self, publishing, age_hours=0.5):
        """A fake where the named facilities publish a reading `age_hours` old."""
        return lambda now: {
            code: [(code + "1", (now - age_hours * self.HOUR, 100.0))]
            for code in publishing
        }

    @staticmethod
    def scope_units(body):
        return sorted(set(re.findall(r'capacity_rank\{[^}]*unit="([^"]+)"', body)))

    @staticmethod
    def ranks(body):
        return dict(re.findall(r'capacity_rank\{[^}]*unit="([^"]+)"[^}]*\} ([0-9]+)', body))

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
                code: [(code + "1", (stamp, 100.0))] for code in ("AAA", "BBB")
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


if __name__ == "__main__":
    unittest.main()
