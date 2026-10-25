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

The exporter is **metadata-only** since 2026-10-20: it makes one
`GET /facilities/` request per refresh and publishes fleet registration data
plus its own API-budget metrics. Live state of charge, stored energy and power
come from the sibling AEMO exporter (aemo_battery_*). The per-facility time
series - and every metric derived from them (SOC, energy, inferred SOC,
efficiency calibration, liveness, request-window optimisation) - were retired
with that change, and RETIRED_METRICS below pins them as gone.

The expectations are the upstream's own numbers read out of the fixtures, not
values invented next to the parser - Eraring is 1997 MWh, and the fleet totals
32085.47 MWh over 74 units with data (119 battery units in all, committed and
never-built ones included).

Two upstream properties are pinned by tests rather than by inspection, because
each one produces a plausible wrong number rather than an obvious failure:
`capacity_storage` is a *unit* field, so a facility's capacity must not be read
as its unit's (and the largest units in the fleet are mostly unbuilt, so the
dispatchability gate matters); and the whole-fleet info metric must cover the
units the numeric series skip, because describing them is its entire purpose.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet  # noqa: E402
import oe_battery_exporter as exporter  # noqa: E402
from oe_battery_exporter import (  # noqa: E402
    AuthError,
    ScrapeError,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Read out of oe_facilities_battery.json.
ERB_CAPACITY_MWH = 1997.0
WTAHB_CAPACITY_MWH = 1680.0
ERB_FLEET_CAPACITY_MWH = 32085.47
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


def fl() -> fleet.Fleet:
    """The fixture as the module parses it.

    `unit_rows` takes a parsed fleet: the de-duplication, the facility context
    and the capacity normalisation all happen in `fleet`, so a caller hands it a
    `Fleet` rather than the raw facility list.
    """
    return fleet.from_facilities(facilities())


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
# unit_rows
# --------------------------------------------------------------------------- #


class UnitRowsTest(unittest.TestCase):
    def test_drops_undispatched_units_by_default(self):
        """The largest batteries in the fleet are mostly unbuilt.

        Without this filter the top ten is seven `committed` projects that
        return no data, and Eraring/Waratah/Orana fall out of scope entirely.
        """
        rows = exporter.unit_rows(fl())
        codes = {r["facility"] for r in rows}
        self.assertNotIn("RICHMOND", codes)
        self.assertNotIn("TOMAGOBESS", codes)
        self.assertIn("ERB", codes)

    def test_include_undispatched_restores_them(self):
        rows = exporter.unit_rows(fl(), require_data=False)
        codes = {r["facility"] for r in rows}
        self.assertIn("RICHMOND", codes)
        self.assertIn("TOMAGOBESS", codes)
        self.assertEqual(len(rows), 119)

    def test_counts(self):
        self.assertEqual(len(exporter.unit_rows(fl())), 74)
        self.assertEqual(len(exporter.unit_rows(fl(), False)), 119)

    def test_eraring_carries_its_own_capacity_not_its_facilitys(self):
        row = next(r for r in exporter.unit_rows(fl()) if r["facility"] == "ERB")
        self.assertEqual(row["capacity_mwh"], ERB_CAPACITY_MWH)
        self.assertEqual(row["unit"], "ERB01")
        self.assertEqual(row["region"], "NSW1")

    def test_network_is_carried_per_row(self):
        """WEM facilities 404 when queried as NEM, so it cannot be assumed."""
        rows = {r["facility"]: r for r in exporter.unit_rows(fl(), False)}
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
        self.assertEqual(
            [r["unit"] for r in exporter.unit_rows(fleet.from_facilities(payload))],
            ["X5"],
        )

    def test_malformed_input_is_tolerated(self):
        self.assertEqual(exporter.unit_rows(None), [])
        self.assertEqual(
            exporter.unit_rows(fleet.from_facilities(["nope", 3, None])), []
        )


# --------------------------------------------------------------------------- #
# Prometheus exposition
# --------------------------------------------------------------------------- #


def render_rows(**over):
    """A caller-shaped state: rows (numeric series) and fleet_meta (info)."""
    base = {
        "fleet_rows": exporter.unit_rows(fl()),
        "fleet_meta": exporter.unit_rows(fl(), require_data=False),
        "oe_battery_fleet_capacity_mwh": ERB_FLEET_CAPACITY_MWH,
        "oe_last_poll_timestamp_seconds": 1790742930.0,
        "oe_last_fleet_refresh_timestamp_seconds": 1790742926.0,
        "oe_api_credits_remaining": 494,
        "oe_api_requests_total": 13,
    }
    base.update(over)
    return base


PER_UNIT_METRICS = (
    "oe_battery_capacity_storage_mwh",
    "oe_battery_fleet_capacity_storage_mwh",
)

# Families retired on 2026-10-20 in favour of the AEMO exporter. They must not
# come back: a second, slower source for the same number is what this change
# removed, and a stale duplicate on a dashboard is worse than a gap.
RETIRED_METRICS = (
    "oe_battery_soc_ratio",
    "oe_battery_energy_stored_mwh",
    "oe_battery_power_mw",
    "oe_battery_last_sample_timestamp_seconds",
    "oe_battery_sample_age_seconds",
    "oe_battery_power_sample_timestamp_seconds",
    "oe_battery_power_sample_age_seconds",
    "oe_battery_anchor_age_hours",
    "oe_battery_anchor_timestamp_seconds",
    "oe_battery_soc_inferred_ratio",
    "oe_battery_energy_inferred_mwh",
    "oe_battery_inferred_timestamp_seconds",
    "oe_battery_inferred_age_seconds",
    "oe_battery_inferred_saturated",
    "oe_battery_inferred_hold_hours",
    "oe_battery_inferred_calibration_pairs",
    "oe_battery_inferred_calibration_error_ratio",
    "oe_battery_inferred_calibration_recommended_efficiency",
    "oe_battery_inferred_calibration_plausible_pairs",
    "oe_batteries_inferred",
    "oe_poll_cycle_duration_seconds",
)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.now = 1790742930.0
        self.text = exporter.render(render_rows(), self.now)

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
        for metric in PER_UNIT_METRICS:
            self.assertEqual(self.text.count("# HELP %s " % metric), 1, metric)
            self.assertEqual(self.text.count("# TYPE %s " % metric), 1, metric)

    def test_ends_with_newline(self):
        self.assertTrue(self.text.endswith("\n"))

    def test_fleet_info_is_absent_without_metadata(self):
        """The info metric is opt-in per render: a state without `fleet_meta`
        (e.g. the exporter right after start, before the first fleet read)
        must not emit the metric at all rather than a half-populated one."""
        text = exporter.render(render_rows(fleet_meta=[]), self.now)
        self.assertNotIn("oe_battery_fleet_info", text)

    def test_fleet_info_carries_registration_labels_as_strings(self):
        text = exporter.render(
            render_rows(fleet_meta=[
                {
                    "facility": "ERB",
                    "facility_name": "Eraring",
                    "region": "NSW1",
                    "network": "NEM",
                    "unit": "ERB01",
                    "status": "operating",
                    "capacity_mwh": 400.0,
                    "registered_mw": 96.0,
                    "data_last_seen": "2026-09-29T13:00:00+00:00",
                },
                {
                    "facility": "GRP",
                    "facility_name": "Goyder South",
                    "region": "SA1",
                    "network": "NEM",
                    "unit": "GRPYDS1",
                    "status": "committed",
                    "capacity_mwh": 20.0,
                    "registered_mw": None,
                    "data_last_seen": None,
                },
            ]),
            self.now,
        )
        self.assertEqual(text.count("# HELP oe_battery_fleet_info "), 1)
        self.assertEqual(text.count("# TYPE oe_battery_fleet_info gauge"), 1)
        lines = [l for l in text.splitlines() if l.startswith("oe_battery_fleet_info{")]
        self.assertEqual(len(lines), 2)

        operating = lines[0]
        self.assertTrue(operating.endswith(" 1"), operating)
        for label in (
            'unit="ERB01"', 'facility="ERB"', 'facility_name="Eraring"',
            'region="NSW1"', 'network="NEM"', 'status="operating"',
        ):
            self.assertIn(label, operating)
        self.assertIn('capacity_storage_mwh="400"', operating)
        self.assertIn('capacity_registered_mw="96"', operating)
        self.assertIn('data_last_seen="2026-09-29T13:00:00+00:00"', operating)

        # Missing figures render as empty label values, never as "NaN" text.
        committed = lines[1]
        self.assertIn('unit="GRPYDS1"', committed)
        self.assertIn('status="committed"', committed)
        self.assertIn('capacity_registered_mw=""', committed)
        self.assertIn('data_last_seen=""}', committed)

    def test_retired_reading_families_are_never_exported(self):
        """SOC, power, stored energy and their clocks are AEMO's now.

        Rendering a state that still carries those fields must not resurrect
        them: the exposition is the boundary where they stop.
        """
        for metric in RETIRED_METRICS:
            self.assertNotIn("%s{" % metric, self.text, metric)
            self.assertNotIn(" %s " % metric, self.text, metric)

    def test_capacity_carries_the_identifying_labels(self):
        line = next(l for l in self.lines_named("oe_battery_capacity_storage_mwh")
                    if 'facility="ERB"' in l)
        for label in ('facility="ERB"', 'unit="ERB01"', 'name="Eraring"',
                      'region="NSW1"', 'status="operating"'):
            self.assertIn(label, line)

    def test_capacity_is_exported_in_megawatt_hours(self):
        line = next(l for l in self.lines_named("oe_battery_capacity_storage_mwh")
                    if 'facility="ERB"' in l)
        self.assertAlmostEqual(
            float(line.rsplit(" ", 1)[1]), ERB_CAPACITY_MWH, places=6
        )
        self.assertIn('unit="ERB01"', line)

    def test_every_per_unit_metric_shares_one_label_set(self):
        """One Grafana variable has to select the same batteries everywhere.

        A reduced label set on any one of these breaks the dashboard: a
        `{name=~"$battery"}` selector matches nothing, and the table frame
        cannot join on `unit` because the field is not there.
        """
        expected = 'facility="ERB",unit="ERB01",name="Eraring",region="NSW1",status="operating"'
        for metric in PER_UNIT_METRICS:
            line = [l for l in self.text.splitlines()
                    if l.startswith(metric + "{") and 'facility="ERB"' in l]
            self.assertEqual(len(line), 1, metric)
            self.assertEqual(line[0].split(" ", 1)[0], "%s{%s}" % (metric, expected), metric)

    def test_labels_are_escaped(self):
        row = dict(exporter.unit_rows(fl())[0])
        row["facility"] = 'A"B'
        row["facility_name"] = "back\\slash"
        text = exporter.render(render_rows(fleet_rows=[row]), self.now)
        line = text.splitlines()[
            next(i for i, l in enumerate(text.splitlines())
                 if l.startswith("oe_battery_capacity_storage_mwh{"))
        ]
        self.assertIn('facility="A\\"B"', line)
        self.assertIn('name="back\\\\slash"', line)

    def test_fleet_capacity_gauge_present(self):
        self.assertIn("oe_battery_fleet_capacity_mwh 32085.47", self.text)

    def test_poll_and_refresh_clocks_present(self):
        self.assertIn("oe_last_poll_timestamp_seconds 1790742930", self.text)
        self.assertIn("oe_last_fleet_refresh_timestamp_seconds 1790742926", self.text)

    def test_credit_and_request_counters(self):
        self.assertIn("oe_api_credits_remaining 494", self.text)
        self.assertIn("# TYPE oe_api_requests_total counter", self.text)

    def test_empty_state_still_renders(self):
        text = exporter.render(
            render_rows(fleet_rows=[], fleet_meta=[], oe_battery_fleet_capacity_mwh=None),
            self.now,
        )
        self.assertIn("# TYPE oe_battery_fleet_capacity_mwh gauge", text)
        # The fleet gauge is a fact about the fleet, not about the rows, so it
        # survives an empty poll - and a dashboard table keyed on capacity keeps
        # its rows once the next poll lands.
        self.assertIn("oe_battery_fleet_capacity_mwh NaN", text)
        self.assertEqual(
            [l for l in text.splitlines()
             if l.startswith("oe_battery_capacity_storage_mwh{")], []
        )


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
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(error)
        try:
            with self.assertRaises(AuthError):
                client._request("http://x/does-not-matter")
        finally:
            urllib.request.urlopen = original

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

    # -- facilities / credits ------------------------------------------------ #

    def answering(self, payload):
        client = self._client()
        seen = {"count": 0}

        def fake(url):
            seen["url"] = url
            seen["count"] += 1
            return payload

        client._request = fake
        return client, seen

    def test_facilities_returns_the_data_list(self):
        client, seen = self.answering({"success": True, "data": [{"code": "X"}]})
        self.assertEqual(client.facilities(), [{"code": "X"}])
        self.assertTrue(seen["url"].endswith("/facilities/"))

    def test_facilities_is_not_fueltech_filtered(self):
        """The AEMO exporter reads this cache and needs every unit's fueltech.

        So the exporter must fetch the unfiltered fleet, not
        `?fueltech_id=battery`, or the cached file loses the units AEMO needs
        to attribute its SCADA MW.
        """
        client, seen = self.answering({"success": True, "data": []})
        client.facilities()
        self.assertNotIn("fueltech", seen["url"])

    def test_success_false_becomes_scrape_error(self):
        client = self._client()
        client._request = lambda url: {
            "success": False,
            "error": "No data available for facility=['X']",
        }
        with self.assertRaises(ScrapeError) as ctx:
            client.get("/facilities/")
        self.assertIn("No data available", str(ctx.exception))

    def test_data_not_a_list_is_a_scrape_error(self):
        client, _ = self.answering({"success": True, "data": {"nope": 1}})
        with self.assertRaises(ScrapeError):
            client.facilities()

    def test_credits_remaining_is_read_from_me(self):
        client, seen = self.answering(
            {"success": True, "data": {"credits": {"remaining": 494}}}
        )
        self.assertEqual(client.credits_remaining(), 494.0)
        self.assertTrue(seen["url"].endswith("/me"))

    def test_credits_failure_is_not_fatal(self):
        """A failed /me costs a gauge, not the scrape."""
        client = self._client()
        client._request = lambda url: (_ for _ in ()).throw(ScrapeError("boom"))
        self.assertIsNone(client.credits_remaining())

    def test_credits_missing_is_none(self):
        client, _ = self.answering({"success": True, "data": {}})
        self.assertIsNone(client.credits_remaining())


# The fleet cache file - its on-disk shape, the atomic write, and the reads
# that tolerate a missing or unusable file - is now `fleet`'s contract rather
# than this exporter's, so those assertions live in test_fleet.py alongside the
# module they describe.


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #


class ScraperClient:
    """Stands in for OpenElectricityClient: only `facilities` and credits."""

    def __init__(self, fleet=None, error=None, credits=494):
        self.fleet = fleet
        self.error = error
        self.credits = credits
        self.fleet_calls = 0
        self.requests_made = 0

    def facilities(self):
        self.fleet_calls += 1
        self.requests_made += 1
        if self.error:
            raise self.error
        return facilities() if self.fleet is None else self.fleet

    def credits_remaining(self):
        self.requests_made += 1
        return self.credits


def make_scraper(client, **kwargs):
    options = dict(
        fleet_refresh_interval=86400.0,
        require_data=True,
    )
    options.update(kwargs)
    return exporter.BatteryScraper(client, **options)


class ScraperTest(unittest.TestCase):
    def test_poll_once_produces_exposition(self):
        scraper = make_scraper(ScraperClient())
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        self.assertIn('oe_battery_capacity_storage_mwh{facility="ERB"', body)
        self.assertIn("oe_battery_fleet_capacity_mwh 32085.47", body)
        self.assertIn("oe_api_credits_remaining 494", body)
        for metric in RETIRED_METRICS:
            self.assertNotIn("%s{" % metric, body, metric)

    def test_metrics_are_served_before_the_first_poll(self):
        """Prometheus scrapes within seconds of start, long before the first poll.

        Every value is NaN rather than 0: nothing has been read yet, and a fleet
        count of 0 would claim the fleet is empty. /metrics still answers 200 and
        /healthz still says healthy, so a container that is mid-way through its
        first poll is not reported as down.
        """
        scraper = make_scraper(ScraperClient())
        body, ok = scraper.exposition()
        self.assertIn("# TYPE oe_battery_fleet_capacity_mwh gauge", body)
        self.assertIn("oe_battery_fleet_capacity_mwh NaN", body)
        self.assertEqual(
            [l for l in body.splitlines()
             if l.startswith("oe_battery_capacity_storage_mwh{")], []
        )
        self.assertTrue(ok)
        healthy, message = scraper.health()
        self.assertTrue(healthy)
        self.assertIn("no poll", message)

    def test_auth_failure_marks_unhealthy_without_raising(self):
        scraper = make_scraper(ScraperClient(error=AuthError("HTTP 403")))
        body, ok = scraper.poll_once()
        self.assertFalse(ok)
        self.assertEqual(body, "")
        healthy, message = scraper.health()
        self.assertFalse(healthy)
        self.assertIn("403", message)

    def test_fleet_is_refetched_only_after_the_refresh_interval(self):
        client = ScraperClient()
        clock = [1000.0]
        scraper = make_scraper(client, now_fn=lambda: clock[0])
        scraper.poll_once()
        scraper.poll_once()
        self.assertEqual(client.fleet_calls, 1)
        clock[0] += 90000.0
        scraper.poll_once()
        self.assertEqual(client.fleet_calls, 2)

    def test_fleet_cache_is_used_when_the_api_is_unreachable(self):
        path = os.path.join(tempfile.mkdtemp(), "fleet.json")
        scraper = make_scraper(ScraperClient(), fleet_file=path)
        scraper.poll_once()
        self.assertTrue(os.path.exists(path))

        broken = ScraperClient(error=ScrapeError("no api"))
        second = make_scraper(broken, fleet_file=path)
        body, ok = second.poll_once()
        self.assertTrue(ok)
        self.assertIn("oe_battery_fleet_capacity_mwh 32085.47", body)

    def test_fleet_info_covers_units_the_poll_never_reads(self):
        """`oe_battery_fleet_info` is the whole-fleet row set. The fixture has
        119 battery units but only 74 have ever published data; the info metric
        must carry all 119, committed and unbuilt included, because those are
        exactly the units the numeric series skip."""
        scraper = make_scraper(ScraperClient())
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        infos = [l for l in body.splitlines() if l.startswith("oe_battery_fleet_info{")]
        self.assertEqual(len(infos), 119)
        for line in infos:
            self.assertTrue(line.endswith(" 1"), line)

        baranduda = next(l for l in infos if 'unit="BARANDUDAB1"' in l)
        # A committed, never-dispatched unit: region-classified metadata, but
        # no data_last_seen.
        self.assertIn('region="VIC1"', baranduda)
        self.assertIn('network="NEM"', baranduda)
        self.assertIn('status="committed"', baranduda)
        self.assertTrue(baranduda.endswith('data_last_seen=""} 1'), baranduda)

        # And the western units are there under their own network id.
        self.assertTrue(any('network="WEM"' in l for l in infos), "no WEM unit in info metric")

        # The whole-fleet numeric capacity gauge, one row per unit with the
        # same per-unit labels.
        caps = [l for l in body.splitlines()
                if l.startswith("oe_battery_fleet_capacity_storage_mwh{")]
        self.assertEqual(len(caps), 119)
        baranduda_cap = next(l for l in caps if 'unit="BARANDUDAB1"' in l)
        self.assertTrue(baranduda_cap.endswith(" 1886"), baranduda_cap)
        for label in ('facility="BARANDUDA"', 'name="Baranduda"', 'region="VIC1"', 'status="committed"'):
            self.assertIn(label, baranduda_cap)

    def test_stale_cache_does_not_pin_the_fleet(self):
        """The disk cache is a fallback, not a second source of truth.

        Reading the cache before the API would mean a cache file, once written,
        is trusted until it happens to be deleted - the daily refresh would
        never happen. A stale cache must be replaced as soon as the API answers.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            fleet.write_fleet(path, facilities())
            payload = json.load(open(path, encoding="utf-8"))
            payload["cached_at"] = 0.0
            json.dump(payload, open(path, "w", encoding="utf-8"))

            doubled = facilities()
            for facility in doubled:
                for unit in facility.get("units") or []:
                    if unit.get("fueltech_id") == exporter.BATTERY_FUELTECH:
                        unit["capacity_storage"] = 100.0
            client = ScraperClient(fleet=doubled)
            scraper = make_scraper(client, fleet_file=path)
            body, ok = scraper.poll_once()
            self.assertTrue(ok)
            # 74 enumerable units at 100 MWh, i.e. the API's fleet, not the cache.
            self.assertIn("oe_battery_fleet_capacity_mwh 7400", body)
            self.assertEqual(client.fleet_calls, 1)
            # and the cache was brought up to date in the same cycle
            self.assertGreater(json.load(open(path, encoding="utf-8"))["cached_at"], 0.0)

    def test_include_undispatched_widens_the_numeric_series(self):
        scraper = make_scraper(ScraperClient(), require_data=False)
        body, ok = scraper.poll_once()
        self.assertTrue(ok)
        rows = [l for l in body.splitlines()
                if l.startswith("oe_battery_capacity_storage_mwh{")]
        self.assertEqual(len(rows), 119)

    def test_background_poll_runs_and_stops(self):
        client = ScraperClient()
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
        scraper = make_scraper(ScraperClient(), poll_interval=0.05)
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
        self.scraper = make_scraper(ScraperClient())
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
        self.assertIn("oe_battery_capacity_storage_mwh", body)
        self.assertNotIn("oe_battery_soc_ratio", body)

    def test_index(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("registration metadata", body)

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

    def test_metadata_only_defaults(self):
        """Metadata-only polling costs one request per cycle, so the default
        cadence is daily; the Prometheus scrape interval is independent."""
        self.assertEqual(exporter.DEFAULT_POLL_INTERVAL, 24 * 3600.0)
        self.assertEqual(exporter.DEFAULT_FLEET_REFRESH_INTERVAL, 24 * 3600.0)


if __name__ == "__main__":
    unittest.main()
