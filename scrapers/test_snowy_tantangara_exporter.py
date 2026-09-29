#!/usr/bin/env python3
"""Tests for snowy_tantangara_exporter.py.

Offline: every upstream response is served from scrapers/fixtures/, sampled
verbatim from snowyhydro.com.au's getData.php on 2026-09-29. No test touches the
network.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_snowy_tantangara_exporter.py

`-t scrapers` is required. `unittest discover` rejects a start directory that
is not importable, and scrapers/ intentionally has no __init__.py so the
exporter stays a single bind-mounted file.

One test per failure mode, not just the happy path. This endpoint fails in ways
the sibling WHT exporter never sees - HTTP 400 with a *plain-text* body, a
Content-Type of text/html on a JSON payload, and a year range the server clamps -
and each of those has bitten or would have bitten silently.
"""

from __future__ import annotations

import errno
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snowy_tantangara_exporter as exporter  # noqa: E402
from snowy_tantangara_exporter import ScrapeError  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

TANTANGARA = "Tantangara Reservoir"
JINDABYNE = "Lake Jindabyne"
EUCUMBENE = "Lake Eucumbene"

# Real values read out of snowy_levels_two_years.json, so the expectations here
# are the upstream's numbers and not numbers invented next to the parser.
LATEST_DATE = date(2026, 9, 29)
LATEST_LEVEL = 11.33
PRIOR_DATE = date(2026, 9, 22)
PRIOR_LEVEL = 14.06
DELTA_POINTS = -2.73
YTD_MIN = 9.53
YTD_MAX = 15.27
YTD_READINGS = 22


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def fixture_json(name: str):
    return json.loads(fixture(name))


def levels() -> dict:
    return fixture_json("snowy_levels_two_years.json")


def row(day: str, *lakes: tuple) -> dict:
    """Build one upstream-shaped row: (name, timestamp, value) triples."""
    return {
        "-date": day,
        "lake": [
            {"-name": name, "-dataTimestamp": stamp, "#text": value}
            for name, stamp, value in lakes
        ],
    }


def tantangara(day: str, value: str) -> tuple:
    return (TANTANGARA, "%sT07:00:00" % day, value)


# --------------------------------------------------------------------------- #
# is_transient
# --------------------------------------------------------------------------- #


def transient_reason() -> OSError:
    """The fault urllib surfaces as URLError.reason for a failed DNS lookup.

    EAI_AGAIN is the one that actually bites in a container: getaddrinfo raises
    gaierror and urllib re-wraps it in URLError. Note that http_get hands
    `exc.reason` to is_transient, never the URLError itself.
    """
    return socket.gaierror(socket.EAI_AGAIN, "Try again")


def permanent_reason() -> OSError:
    return OSError(errno.EACCES, "Permission denied")


def url_error(reason: OSError) -> urllib.error.URLError:
    return urllib.error.URLError(reason)


class IsTransientTest(unittest.TestCase):
    def test_timeout_and_connection_exceptions_are_transient(self):
        for reason in (TimeoutError(), ConnectionResetError(), ConnectionAbortedError()):
            with self.subTest(reason=type(reason).__name__):
                self.assertTrue(exporter.is_transient(reason))

    def test_eai_again_is_transient(self):
        self.assertTrue(exporter.is_transient(transient_reason()))

    def test_transient_errnos_are_transient(self):
        for code in (errno.EAGAIN, errno.ECONNRESET, errno.ECONNABORTED):
            with self.subTest(errno=code):
                self.assertTrue(exporter.is_transient(OSError(code, "boom")))

    def test_other_errnos_are_permanent(self):
        self.assertFalse(exporter.is_transient(permanent_reason()))

    def test_arbitrary_object_is_not_transient(self):
        self.assertFalse(exporter.is_transient(object()))
        self.assertFalse(exporter.is_transient(None))


# --------------------------------------------------------------------------- #
# http_get / http_get_json
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._stream = io.BytesIO(body)

    def read(self) -> bytes:
        return self._stream.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def raiser(exc):
    def fake_open(request, timeout=None):
        raise exc

    return fake_open


class HttpGetTest(unittest.TestCase):
    def setUp(self):
        self.original = exporter.urllib.request.urlopen
        self.addCleanup(lambda: setattr(exporter.urllib.request, "urlopen", self.original))
        self.calls = []
        self.outcomes = []
        self.attempts = 0

    def serve(self, *outcomes):
        """Queue response bodies / exceptions, in the order they will be seen."""
        self.outcomes = list(outcomes)
        self.calls = []
        self.attempts = 0

        def fake_open(request, timeout=None):
            self.attempts += 1
            if not self.outcomes:
                raise AssertionError("unexpected extra request (attempt %d)" % self.attempts)
            item = self.outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeResponse(item)

        exporter.urllib.request.urlopen = fake_open

    def test_success_returns_body(self):
        self.serve(b"{}")
        self.assertEqual(exporter.http_get("https://example.test/x", 5), b"{}")
        self.assertEqual(self.attempts, 1)

    def test_http_error_fails_immediately_without_retrying(self):
        # A 400 from this endpoint is a real answer about the year range, and a
        # retry would only reproduce it.
        error = urllib.error.HTTPError(
            "https://example.test/x", 400, "Bad Request", {}, io.BytesIO(b"nope")
        )
        self.serve(error, error, error)
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 5, retries=3)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertEqual(self.attempts, 1, "an HTTP error was retried")

    def test_permanent_urerror_fails_without_retrying(self):
        self.serve(url_error(permanent_reason()), url_error(permanent_reason()))
        with self.assertRaises(ScrapeError):
            exporter.http_get("https://example.test/x", 5, retries=3)
        self.assertEqual(self.attempts, 1, "a permanent fault was retried")

    def test_retries_zero_fails_on_first_transient_fault(self):
        self.serve(url_error(transient_reason()), b"unreachable")
        with self.assertRaises(ScrapeError):
            exporter.http_get("https://example.test/x", 5)
        self.assertEqual(self.attempts, 1)

    def test_transient_fault_recovers_within_the_retry_count(self):
        self.serve(url_error(transient_reason()), b"ok")
        with _no_sleep():
            self.assertEqual(exporter.http_get("https://example.test/x", 5, retries=1), b"ok")
        self.assertEqual(self.attempts, 2)

    def test_exhausting_the_retry_count_raises_after_retries_plus_one_attempts(self):
        self.serve(*([url_error(transient_reason())] * 4))
        with self.assertRaises(ScrapeError):
            with _no_sleep():
                exporter.http_get("https://example.test/x", 5, retries=2)
        self.assertEqual(self.attempts, 3, "expected retries + 1 attempts")

    def test_retry_budget_cuts_in_before_the_attempt_cap(self):
        # The clock jumps past the budget on the first check, so the loop must
        # stop there rather than burning all 11 attempts.
        clock = iter([0.0] + [9.0] * 20)
        self.serve(*([url_error(transient_reason())] * 12))
        with self.assertRaises(ScrapeError):
            with _no_sleep(), _monotonic(clock):
                exporter.http_get("https://example.test/x", 5, retries=10, retry_budget=6.0)
        self.assertEqual(self.attempts, 1)

    def test_zero_retry_budget_disables_the_budget_check(self):
        # A zero budget is the documented way to say "no budget": every attempt
        # runs even though the clock says the budget is long gone.
        clock = iter([0.0] + [99.0] * 20)
        self.serve(*([url_error(transient_reason())] * 5))
        with self.assertRaises(ScrapeError):
            with _no_sleep(), _monotonic(clock):
                exporter.http_get("https://example.test/x", 5, retries=3, retry_budget=0.0)
        self.assertEqual(self.attempts, 4, "expected retries + 1 attempts")

    def test_negative_retries_short_circuits_the_loop(self):
        # Matches the sibling exporter: the loop body never runs, and the
        # fallthrough is what raises, rather than a real attempt.
        self.serve(b"unreachable")
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 5, retries=-1)
        self.assertIn("gave up", str(ctx.exception))
        self.assertEqual(self.attempts, 0)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _no_sleep:
    def __enter__(self):
        self.original = exporter.time.sleep
        exporter.time.sleep = lambda seconds: None
        return self

    def __exit__(self, *exc):
        exporter.time.sleep = self.original
        return False


class _monotonic:
    def __init__(self, values) -> None:
        self.values = values

    def __enter__(self):
        self.original = exporter.time.monotonic
        clock = self.values

        def fake():
            try:
                return next(clock)
            except StopIteration:
                return 99.0

        exporter.time.monotonic = fake
        return self

    def __exit__(self, *exc):
        exporter.time.monotonic = self.original
        return False


class DescribeHttpErrorTest(unittest.TestCase):
    def error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://example.test/x", code, "Bad Request", {}, io.BytesIO(body)
        )

    def test_folds_the_plain_text_body_into_the_message(self):
        # The whole diagnostic value of this endpoint's 400s is in the body.
        exc = self.error(400, fixture("snowy_error_out_of_range.txt"))
        message = exporter.describe_http_error(exc)
        self.assertIn("HTTP 400", message)
        self.assertIn("must be between 1954 and 2026", message)

    def test_collapses_whitespace_so_a_multiline_body_stays_one_log_line(self):
        exc = self.error(400, b"Error: line one\n\n   line two   \n")
        self.assertEqual(
            exporter.describe_http_error(exc),
            "GET https://example.test/x -> HTTP 400: Error: line one line two",
        )

    def test_truncates_a_huge_error_body(self):
        exc = self.error(502, b"x" * 5000)
        message = exporter.describe_http_error(exc)
        self.assertLess(len(message), 300)

    def test_missing_or_unreadable_body_still_reports_the_status(self):
        self.assertEqual(
            exporter.describe_http_error(self.error(503, b"")),
            "GET https://example.test/x -> HTTP 503",
        )
        broken = self.error(500, b"")
        broken.read = lambda: (_ for _ in ()).throw(OSError("closed"))
        self.assertEqual(
            exporter.describe_http_error(broken), "GET https://example.test/x -> HTTP 500"
        )

    def test_decodes_a_non_utf8_body_without_raising(self):
        exc = self.error(400, b"\xff\xfe not utf-8")
        self.assertIn("not utf-8", exporter.describe_http_error(exc))


class HttpGetJsonTest(unittest.TestCase):
    def setUp(self):
        self.original = exporter.urllib.request.urlopen
        self.addCleanup(lambda: setattr(exporter.urllib.request, "urlopen", self.original))
        self.calls = []

    def serve(self, *outcomes):
        self.calls = []
        for outcome in outcomes:
            self.calls.append(outcome)

        def fake_open(request, timeout=None):
            item = self.calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeResponse(item)

        exporter.urllib.request.urlopen = fake_open

    def test_parses_json_object(self):
        self.serve(b'{"2026": {}}')
        self.assertEqual(exporter.http_get_json("https://example.test/x", 5), {"2026": {}})

    def test_invalid_json_raises(self):
        # A WordPress maintenance page or an HTML 200 both land here.
        self.serve(b"<!doctype html><title>Maintenance</title>")
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get_json("https://example.test/x", 5)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_non_object_json_raises(self):
        # The payload is keyed by year; a list would silently yield no readings.
        self.serve(b"[]")
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get_json("https://example.test/x", 5)
        self.assertIn("expected a JSON object", str(ctx.exception))

    def test_empty_body_raises(self):
        self.serve(b"")
        with self.assertRaises(ScrapeError):
            exporter.http_get_json("https://example.test/x", 5)


# --------------------------------------------------------------------------- #
# Year range and URL construction
# --------------------------------------------------------------------------- #


class CurrentYearTest(unittest.TestCase):
    def test_uses_the_sydney_year_not_the_utc_year(self):
        # 2026-12-31 14:00 UTC is already 2027-01-01 00:00 in Sydney. Asking for
        # the UTC year would request a year the endpoint has not opened yet.
        with _utc(2026, 12, 31, 14, 0):
            self.assertEqual(exporter.current_year(), 2027)
        with _utc(2026, 12, 31, 9, 0):
            self.assertEqual(exporter.current_year(), 2026)

    def test_summer_evening_utc_still_reads_as_the_same_sydney_day(self):
        with _utc(2026, 6, 15, 3, 0):
            self.assertEqual(exporter.current_year(), 2026)


class _utc:
    def __init__(self, *parts) -> None:
        self.parts = parts
        self.moment = datetime(*parts, tzinfo=timezone.utc)

    def __enter__(self):
        self.original = exporter.datetime
        moment = self.moment

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        exporter.datetime = Frozen
        return self

    def __exit__(self, *exc):
        exporter.datetime = self.original
        return False


class BuildUrlTest(unittest.TestCase):
    def test_encodes_both_year_parameters(self):
        url = exporter.build_url("https://example.test/data.php", 2025, 2026)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["yearA"], ["2025"])
        self.assertEqual(params["yearB"], ["2026"])
        self.assertTrue(url.startswith("https://example.test/data.php?"))

    def test_equal_years_are_allowed(self):
        # yearA == yearB is how one year is requested; omitting them is a 400.
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(exporter.build_url("https://e.test/d", 2026, 2026)).query
        )
        self.assertEqual(params, {"yearA": ["2026"], "yearB": ["2026"]})


class LakeScraperFetchTest(unittest.TestCase):
    def scraper(self, **kwargs) -> "exporter.LakeScraper":
        options = dict(
            data_url="https://example.test/d",
            reservoirs=[TANTANGARA],
            timeout=5.0,
            ttl=60.0,
            year=2026,
        )
        options.update(kwargs)
        return exporter.LakeScraper(**options)

    def test_year_range_ends_at_the_current_year(self):
        self.assertEqual(self.scraper(years_back=1).year_range(), (2025, 2026))
        self.assertEqual(self.scraper(years_back=3).year_range(), (2023, 2026))

    def test_year_range_is_clamped_to_the_endpoint_floor(self):
        # The endpoint rejects anything below 1954 with an HTTP 400, so a large
        # --years-back must not turn into a request it will refuse.
        self.assertEqual(self.scraper(years_back=5000).year_range()[0], 1954)
        self.assertEqual(self.scraper(year=1955, years_back=5000).year_range(), (1954, 1955))

    def test_pinned_year_overrides_the_clock(self):
        self.assertEqual(self.scraper(year=2019).current_year(), 2019)
        self.assertEqual(self.scraper(year=None).current_year(), exporter.current_year())

    def test_years_back_is_at_least_one(self):
        self.assertEqual(self.scraper(years_back=0).years_back, 1)
        self.assertEqual(self.scraper(years_back=-5).years_back, 1)

    def test_fetch_builds_the_url_from_the_range(self):
        scraper = self.scraper(years_back=1)
        seen = []
        original = exporter.http_get_json
        exporter.http_get_json = lambda url, *a, **k: seen.append(url) or {}
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))
        scraper.fetch()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
        self.assertEqual(params, {"yearA": ["2025"], "yearB": ["2026"]})


# --------------------------------------------------------------------------- #
# Parsing: dates, timestamps, lake entries
# --------------------------------------------------------------------------- #


class ParseDateTest(unittest.TestCase):
    def test_reads_the_upstream_format(self):
        self.assertEqual(exporter.parse_date("2026-09-29"), date(2026, 9, 29))

    def test_tolerates_a_time_component(self):
        self.assertEqual(exporter.parse_date("2026-09-29T07:00:00"), date(2026, 9, 29))

    def test_missing_or_malformed_raises(self):
        for value in (None, "", 42, "not-a-date", "2026-13-01"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    exporter.parse_date(value)


class ParseTimestampTest(unittest.TestCase):
    def test_reads_the_naive_sydney_stamp_as_aest(self):
        # 2026-09-29T07:00:00 AEST == 2026-09-28T21:00:00Z.
        got = exporter.parse_timestamp("2026-09-29T07:00:00")
        self.assertEqual(
            datetime.fromtimestamp(got, timezone.utc),
            datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc),
        )

    def test_all_rows_stamp_seven_in_the_morning(self):
        # The whole series is a fixed 07:00 daily reading, so this pins the
        # assumption that the stamp is a local reading time, not a drift. Note
        # the conversion goes via UTC: the host's own zone is irrelevant now
        # that the exporter pins the offset itself.
        got = exporter.parse_timestamp("2026-09-29T07:00:00")
        local = datetime.fromtimestamp(got, timezone.utc) + timedelta(
            hours=exporter.LOCAL_UTC_OFFSET_HOURS
        )
        self.assertEqual((local.hour, local.minute), (7, 0))

    def test_result_does_not_depend_on_the_host_timezone(self):
        # The bug this pins: datetime.timestamp() on a naive value reads the
        # host's local zone, so the same fixture produced a different instant on
        # the AEST dev host than it would in the UTC container. Shifting the
        # process timezone must not move the exported number.
        original = os.environ.get("TZ")
        try:
            values = {}
            for name in ("UTC", "Australia/Sydney", "America/Los_Angeles"):
                os.environ["TZ"] = name
                time.tzset()
                values[name] = exporter.parse_timestamp("2026-09-29T07:00:00")
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()
        self.assertEqual(values["UTC"], values["Australia/Sydney"])
        self.assertEqual(values["UTC"], values["America/Los_Angeles"])
        self.assertEqual(
            datetime.fromtimestamp(values["UTC"], timezone.utc),
            datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc),
        )

    def test_keeps_an_explicit_offset_instead_of_shifting_it_again(self):
        # Forward compatibility: if upstream ever starts sending a zone, the
        # value is already correct and must not be offset twice.
        aware = exporter.parse_timestamp("2026-09-29T07:00:00+00:00")
        self.assertEqual(
            datetime.fromtimestamp(aware, timezone.utc),
            datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc),
        )

    def test_bad_or_missing_stamp_is_none_not_an_exception(self):
        # A row with a broken clock field still carries a usable level; losing
        # the reservoir over it would be the wrong trade.
        for value in (None, "", "yesterday", 7, {}):
            with self.subTest(value=value):
                self.assertIsNone(exporter.parse_timestamp(value))


class LakeEntriesTest(unittest.TestCase):
    def test_reads_a_list(self):
        entries = exporter.lake_entries({"lake": [{"-name": TANTANGARA}]})
        self.assertEqual([entry["-name"] for entry in entries], [TANTANGARA])

    def test_wraps_a_bare_object(self):
        # An XML-shaped upstream collapses a single child into one object, which
        # is why the theme's own JS checks for both. Iterating a dict directly
        # would yield its keys.
        entries = exporter.lake_entries({"lake": {"-name": TANTANGARA}})
        self.assertEqual(entries, [{"-name": TANTANGARA}])

    def test_drops_non_dict_entries(self):
        self.assertEqual(exporter.lake_entries({"lake": ["x", {"-name": "y"}, 3]}), [{"-name": "y"}])

    def test_missing_or_wrong_type_is_empty(self):
        for value in (None, "lake", 7, [1, 2]):
            with self.subTest(value=value):
                self.assertEqual(exporter.lake_entries({"lake": value}), [])

    def test_reads_snow_too(self):
        entries = exporter.lake_entries({"snow": {"-name": "Spencers Creek"}}, "snow")
        self.assertEqual(entries[0]["-name"], "Spencers Creek")


class FindLakeTest(unittest.TestCase):
    def test_exact_match(self):
        entries = [{"-name": JINDABYNE}, {"-name": TANTANGARA}]
        self.assertEqual(exporter.find_lake(entries, TANTANGARA)["-name"], TANTANGARA)

    def test_falls_back_to_case_insensitive(self):
        # So a cosmetic upstream rename costs a NaN rather than a whole series.
        entries = [{"-name": "tantangara reservoir"}]
        self.assertIsNotNone(exporter.find_lake(entries, TANTANGARA))

    def test_absent_reservoir_is_none(self):
        self.assertIsNone(exporter.find_lake([{"-name": JINDABYNE}], TANTANGARA))
        self.assertIsNone(exporter.find_lake([], TANTANGARA))

    def test_exact_match_wins_over_a_case_variant(self):
        entries = [{"-name": "tantangara reservoir"}, {"-name": TANTANGARA}]
        self.assertEqual(exporter.find_lake(entries, TANTANGARA)["-name"], TANTANGARA)


class ListLakeNamesTest(unittest.TestCase):
    def test_lists_the_three_published_lakes_in_order(self):
        self.assertEqual(
            exporter.list_lake_names(levels()), [EUCUMBENE, JINDABYNE, TANTANGARA]
        )

    def test_deduplicates_across_years(self):
        self.assertEqual(len(exporter.list_lake_names(levels())), 3)

    def test_tolerates_an_empty_or_broken_payload(self):
        self.assertEqual(exporter.list_lake_names({}), [])
        self.assertEqual(exporter.list_lake_names({"2026": {}}), [])
        self.assertEqual(exporter.list_lake_names({"2026": {"snowyhydro": {"level": []}}}), [])


# --------------------------------------------------------------------------- #
# daily_series
# --------------------------------------------------------------------------- #


class DailySeriesTest(unittest.TestCase):
    def test_reads_the_real_tantangara_series(self):
        series = exporter.daily_series(levels(), TANTANGARA)
        self.assertEqual(len(series), 36)  # 14 in 2025 + 22 in 2026
        self.assertEqual(series[-1]["date"], LATEST_DATE)
        self.assertAlmostEqual(series[-1]["level_percent"], LATEST_LEVEL)
        self.assertAlmostEqual(series[0]["level_percent"], 10.04)

    def test_is_sorted_by_date_across_the_year_boundary(self):
        series = exporter.daily_series(levels(), TANTANGARA)
        dates = [point["date"] for point in series]
        self.assertEqual(dates, sorted(dates))
        # The endpoint returns 2025 first, so nothing here is a lucky accident.
        self.assertEqual(series[13]["date"], date(2025, 12, 31))
        self.assertEqual(series[14]["date"], date(2026, 1, 1))

    def test_carries_the_observation_timestamp(self):
        series = exporter.daily_series(levels(), TANTANGARA)
        self.assertIsNotNone(series[-1]["observed_at"])

    def test_reads_the_other_lakes(self):
        for name in (JINDABYNE, EUCUMBENE):
            with self.subTest(reservoir=name):
                series = exporter.daily_series(levels(), name)
                self.assertEqual(len(series), 36)

    def test_ignores_the_snow_block(self):
        # Snow depth is sparse (26 of 272 rows in 2026) and carries a -quality
        # field; it is not a daily series and must not be mixed in.
        with self.assertRaises(ScrapeError):
            exporter.daily_series(levels(), "Spencers Creek")

    def test_absent_reservoir_raises(self):
        with self.assertRaises(ScrapeError) as ctx:
            exporter.daily_series(levels(), "Lake Nee Nee")
        self.assertIn("no readings found", str(ctx.exception))

    def test_one_unreadable_value_does_not_lose_the_other_days(self):
        payload = {
            "2026": {
                "snowyhydro": {
                    "level": [
                        row("2026-09-28", tantangara("2026-09-28", "12.0")),
                        row("2026-09-29", (TANTANGARA, "2026-09-29T07:00:00", "n/a")),
                        row("2026-09-30", tantangara("2026-09-30", "11.0")),
                    ]
                }
            }
        }
        series = exporter.daily_series(payload, TANTANGARA)
        self.assertEqual([point["level_percent"] for point in series], [12.0, 11.0])

    def test_a_row_without_the_reservoir_is_skipped_not_fatal(self):
        payload = {
            "2026": {
                "snowyhydro": {
                    "level": [
                        row("2026-09-28", (JINDABYNE, "2026-09-28T07:00:00", "80.0")),
                        row("2026-09-29", tantangara("2026-09-29", "11.33")),
                    ]
                }
            }
        }
        series = exporter.daily_series(payload, TANTANGARA)
        self.assertEqual(len(series), 1)

    def test_a_row_with_a_bad_date_is_skipped(self):
        payload = {
            "2026": {
                "snowyhydro": {
                    "level": [
                        {"-date": "not-a-date", "lake": [{"-name": TANTANGARA, "#text": "9"}]},
                        row("2026-09-29", tantangara("2026-09-29", "11.33")),
                    ]
                }
            }
        }
        self.assertEqual(len(exporter.daily_series(payload, TANTANGARA)), 1)

    def test_tolerates_missing_and_broken_year_blocks(self):
        # A short or oddly shaped range must degrade to less history, not raise.
        for payload in (
            {},
            {"2026": None},
            {"2026": {}},
            {"2026": {"snowyhydro": {}}},
            {"2026": {"snowyhydro": {"level": {}}}},
            {"2026": {"snowyhydro": {"level": [None, 7]}}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ScrapeError):
                    exporter.daily_series(payload, TANTANGARA)

    def test_numeric_value_as_a_number_is_accepted(self):
        payload = {
            "2026": {
                "snowyhydro": {
                    "level": [
                        {"-date": "2026-09-29", "lake": [{"-name": TANTANGARA, "#text": 11.33}]}
                    ]
                }
            }
        }
        self.assertAlmostEqual(exporter.daily_series(payload, TANTANGARA)[0]["level_percent"], 11.33)


# --------------------------------------------------------------------------- #
# value_on_or_before / summarise
# --------------------------------------------------------------------------- #


def series_from(pairs):
    return [
        {
            "date": date.fromisoformat(day),
            "level_percent": float(value),
            "observed_at": exporter.parse_timestamp("%sT07:00:00" % day),
        }
        for day, value in pairs
    ]


class ValueOnOrBeforeTest(unittest.TestCase):
    def setUp(self):
        self.series = series_from(
            [("2026-09-20", 14.71), ("2026-09-22", 14.06), ("2026-09-29", 11.33)]
        )

    def test_exact_match(self):
        point = exporter.value_on_or_before(self.series, date(2026, 9, 22))
        self.assertEqual(point["date"], date(2026, 9, 22))

    def test_falls_back_to_the_newest_earlier_reading(self):
        # A gap must slide the window, not the answer: reporting a 6-day change
        # as if it were 7 is exactly the silent wrong number to avoid.
        point = exporter.value_on_or_before(self.series, date(2026, 9, 24))
        self.assertEqual(point["date"], date(2026, 9, 22))

    def test_before_the_first_reading_is_none(self):
        self.assertIsNone(exporter.value_on_or_before(self.series, date(2026, 9, 1)))

    def test_after_the_last_reading_is_the_last_reading(self):
        point = exporter.value_on_or_before(self.series, date(2027, 1, 1))
        self.assertEqual(point["date"], date(2026, 9, 29))


class SummariseTest(unittest.TestCase):
    def test_reads_the_real_fixture(self):
        summary = exporter.summarise(exporter.daily_series(levels(), TANTANGARA))
        self.assertAlmostEqual(summary["level_percent"], LATEST_LEVEL)
        self.assertEqual(summary["observed_date"], LATEST_DATE)
        self.assertAlmostEqual(summary["change_points"], DELTA_POINTS, places=4)
        self.assertEqual(summary["change_from"], PRIOR_DATE)
        self.assertAlmostEqual(summary["min_ytd_percent"], YTD_MIN)
        self.assertAlmostEqual(summary["max_ytd_percent"], YTD_MAX)
        self.assertEqual(summary["ytd_readings"], YTD_READINGS)

    def test_year_to_date_ignores_the_previous_year(self):
        # 2025's 9.74-10.09 sits inside 2026's range, so this fixture alone
        # cannot catch a YTD that leaked across New Year. Values chosen so a leak
        # would show: a 2025 low of 1.0 must not become the 2026 minimum.
        series = series_from(
            [("2025-12-30", 1.0), ("2025-12-31", 2.0), ("2026-01-01", 50.0), ("2026-01-08", 40.0)]
        )
        summary = exporter.summarise(series)
        self.assertAlmostEqual(summary["min_ytd_percent"], 40.0)
        self.assertAlmostEqual(summary["max_ytd_percent"], 50.0)
        self.assertEqual(summary["ytd_readings"], 2)

    def test_change_uses_date_arithmetic_not_a_row_offset(self):
        # 2026-01-08 minus 7 days is 2026-01-01; a row-offset delta of 1 would
        # be indistinguishable here, which is the point - the date-based lookup
        # has to agree with the naive answer on clean data.
        series = series_from([("2026-01-01", 50.0), ("2026-01-08", 40.0)])
        summary = exporter.summarise(series, change_days=7)
        self.assertAlmostEqual(summary["change_points"], -10.0)
        self.assertEqual(summary["change_from"], date(2026, 1, 1))

    def test_change_window_is_configurable(self):
        # A contiguous series, so --change-days 1 lands on the previous day
        # rather than falling back to the newest earlier reading.
        series = series_from(
            [("2026-01-07", 45.0), ("2026-01-08", 40.0)]
        )
        self.assertAlmostEqual(exporter.summarise(series, change_days=1)["change_points"], -5.0)
        summary = exporter.summarise(series, change_days=1)
        self.assertEqual(summary["change_from"], date(2026, 1, 7))
        self.assertEqual(summary["change_days"], 1)

    def test_change_window_falls_back_across_a_gap(self):
        # A missing day slides the window rather than the answer: the change is
        # still reported, and change_from says which reading it came from.
        series = series_from([("2026-01-01", 50.0), ("2026-01-08", 40.0)])
        summary = exporter.summarise(series, change_days=1)
        self.assertEqual(summary["change_from"], date(2026, 1, 1))
        self.assertAlmostEqual(summary["change_points"], -10.0)

    def test_change_is_none_when_there_is_no_history(self):
        # First week of January with a single year fetched, if --years-back 0
        # were ever allowed. Must be NaN, not zero: a zero would read as "the
        # level has not moved", which is a claim about the reservoir.
        summary = exporter.summarise(series_from([("2026-01-01", 50.0)]))
        self.assertIsNone(summary["change_points"])
        self.assertIsNone(summary["change_from"])

    def test_latest_reading_wins_regardless_of_payload_order(self):
        series = series_from([("2026-09-29", 11.33), ("2026-09-20", 14.71)])
        summary = exporter.summarise(series)
        self.assertEqual(summary["observed_date"], date(2026, 9, 29))

    def test_reservoir_label_is_left_to_collect(self):
        self.assertEqual(exporter.summarise(series_from([("2026-01-01", 5.0)]))["reservoir"], "")


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #


class CollectTest(unittest.TestCase):
    def test_all_three_lakes_succeed(self):
        samples, errors = exporter.collect(levels(), [TANTANGARA, JINDABYNE, EUCUMBENE])
        self.assertEqual(errors, [])
        self.assertEqual(
            [sample["reservoir"] for sample in samples], [TANTANGARA, JINDABYNE, EUCUMBENE]
        )

    def test_labels_each_sample_with_its_reservoir(self):
        samples, _ = exporter.collect(levels(), [JINDABYNE])
        self.assertEqual(samples[0]["reservoir"], JINDABYNE)

    def test_one_missing_reservoir_does_not_take_down_the_others(self):
        # The property the design rests on: a bad name costs that series only.
        samples, errors = exporter.collect(levels(), [TANTANGARA, "Lake Nee Nee"])
        self.assertEqual([sample["reservoir"] for sample in samples], [TANTANGARA])
        self.assertEqual(len(errors), 1)
        self.assertIn("Lake Nee Nee", errors[0])

    def test_default_set_is_tantangara_only(self):
        # Jindabyne and Eucumbene are pre-2.0 scheme infrastructure; exporting
        # them by default would dress scheme context up as project status.
        self.assertEqual(exporter.DEFAULT_RESERVOIR, TANTANGARA)

    def test_change_window_is_passed_through(self):
        samples, _ = exporter.collect(levels(), [TANTANGARA], change_days=1)
        self.assertEqual(samples[0]["change_days"], 1)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def sample(**overrides) -> dict:
    base = {
        "reservoir": TANTANGARA,
        "level_percent": 11.33,
        "observed_at": 1790665200.0,
        "observed_date": LATEST_DATE,
        "change_days": 7,
        "change_points": -2.73,
        "change_from": PRIOR_DATE,
        "min_ytd_percent": 9.53,
        "max_ytd_percent": 15.27,
        "ytd_readings": 22,
    }
    base.update(overrides)
    return base


class RenderTest(unittest.TestCase):
    def render(self, samples, success=True, duration=0.42, generated=1.79e9, change_days=7):
        return exporter.render(samples, success, duration, generated, change_days)

    def test_emits_every_documented_metric(self):
        body = self.render([sample()])
        for metric in (
            "snowy_tantangara_level_percent",
            "snowy_tantangara_level_change_7d_percentage_points",
            "snowy_tantangara_level_min_ytd_percent",
            "snowy_tantangara_level_max_ytd_percent",
            "snowy_tantangara_last_sample_timestamp_seconds",
            "snowy_tantangara_scrape_success",
            "snowy_tantangara_scrape_duration_seconds",
            "snowy_tantangara_last_scrape_timestamp_seconds",
        ):
            with self.subTest(metric=metric):
                self.assertIn("# TYPE %s gauge" % metric, body)

    def test_values_render(self):
        body = self.render([sample()])
        self.assertIn(
            'snowy_tantangara_level_percent{reservoir="Tantangara Reservoir"} 11.33', body
        )
        self.assertIn(
            'snowy_tantangara_level_change_7d_percentage_points'
            '{reservoir="Tantangara Reservoir"} -2.73',
            body,
        )
        self.assertIn(
            'snowy_tantangara_level_min_ytd_percent{reservoir="Tantangara Reservoir"} 9.53', body
        )
        self.assertIn(
            'snowy_tantangara_level_max_ytd_percent{reservoir="Tantangara Reservoir"} 15.27', body
        )
        self.assertIn("snowy_tantangara_scrape_success 1", body)
        self.assertIn("snowy_tantangara_scrape_duration_seconds 0.42", body)

    def test_missing_values_render_as_nan(self):
        # A level must never be reported as 0: on a reservoir that reads as
        # "empty", which is a claim about the water rather than about the scrape.
        body = self.render([sample(level_percent=None, change_points=None, observed_at=None)])
        self.assertIn(
            'snowy_tantangara_level_percent{reservoir="Tantangara Reservoir"} NaN', body
        )
        self.assertIn("snowy_tantangara_last_sample_timestamp_seconds{reservoir=\"Tantangara Reservoir\"} NaN", body)

    def test_failure_drops_every_level_series(self):
        body = self.render([], success=False)
        self.assertIn("snowy_tantangara_scrape_success 0", body)
        self.assertNotIn("snowy_tantangara_level_percent{", body)

    def test_no_config_stale_metric(self):
        # Deliberate: there is no discovery step and no cached configuration, so
        # a config_stale gauge would have nothing to ever be 1.
        self.assertNotIn("config_stale", self.render([sample()]))

    def test_change_days_renames_the_metric(self):
        # A --change-days 14 run must not keep writing under the 7d name.
        body = self.render([sample()], change_days=14)
        self.assertIn("# TYPE snowy_tantangara_level_change_14d_percentage_points gauge", body)
        self.assertNotIn("level_change_7d", body)

    def test_one_series_per_reservoir(self):
        body = self.render([sample(), sample(reservoir=JINDABYNE, level_percent=83.77)])
        self.assertIn('snowy_tantangara_level_percent{reservoir="Tantangara Reservoir"} 11.33', body)
        self.assertIn('snowy_tantangara_level_percent{reservoir="Lake Jindabyne"} 83.77', body)

    def test_escapes_label_values(self):
        body = self.render([sample(reservoir='A "quoted" \\ lake')])
        self.assertIn('reservoir="A \\"quoted\\" \\\\ lake"', body)


class FmtTest(unittest.TestCase):
    def test_none_is_nan(self):
        self.assertEqual(exporter.fmt(None), "NaN")

    def test_integral_values_render_without_a_decimal(self):
        self.assertEqual(exporter.fmt(1567.0), "1567")

    def test_fractions_are_rounded_to_four_places(self):
        self.assertEqual(exporter.fmt(11.333333), "11.3333")

    def test_nan_and_infinities(self):
        self.assertEqual(exporter.fmt(float("nan")), "NaN")
        self.assertEqual(exporter.fmt(float("inf")), "+Inf")
        self.assertEqual(exporter.fmt(float("-inf")), "-Inf")

    def test_negative_values(self):
        self.assertEqual(exporter.fmt(-2.73), "-2.73")


class EscapeLabelTest(unittest.TestCase):
    def test_escapes_backslash_quote_and_newline(self):
        self.assertEqual(
            exporter.escape_label('a\\b"c\nd'), 'a\\\\b\\"c\\nd'
        )


# --------------------------------------------------------------------------- #
# LakeScraper
# --------------------------------------------------------------------------- #


class LakeScraperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.calls = []

    def scraper(self, body=b"{}", **kwargs) -> "exporter.LakeScraper":
        options = dict(
            data_url="https://example.test/d",
            reservoirs=[TANTANGARA],
            timeout=5.0,
            ttl=0.0,
            year=2026,
        )
        options.update(kwargs)
        scraper = exporter.LakeScraper(**options)
        self.patch_http_get_json(body)
        return scraper

    def patch_http_get_json(self, body):
        original = exporter.http_get_json
        calls = self.calls

        def fake(url, timeout, retries=0, retry_budget=0.0):
            calls.append(url)
            if isinstance(body, Exception):
                raise body
            return json.loads(body)

        exporter.http_get_json = fake
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))
        return calls

    def test_refresh_emits_the_series_and_succeeds(self):
        scraper = self.scraper(fixture("snowy_levels_two_years.json"))
        body, success = scraper.refresh()
        self.assertTrue(success)
        self.assertIn(
            'snowy_tantangara_level_percent{reservoir="Tantangara Reservoir"} 11.33', body
        )
        self.assertIn("snowy_tantangara_scrape_success 1", body)

    def test_requests_the_pinned_year_range(self):
        scraper = self.scraper(fixture("snowy_levels_two_years.json"), year=2026, years_back=1)
        scraper.refresh()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.calls[0]).query)
        self.assertEqual(params["yearA"], ["2025"])
        self.assertEqual(params["yearB"], ["2026"])

    def test_a_failing_fetch_yields_success_zero_and_no_series(self):
        # The level gauges are never cached, so a read failure loses the series
        # rather than repeating a stale level as if it were current.
        scraper = self.scraper(ScrapeError("GET x -> HTTP 400: Error: yearA ..."))
        body, success = scraper.refresh()
        self.assertFalse(success)
        self.assertIn("snowy_tantangara_scrape_success 0", body)
        self.assertNotIn("snowy_tantangara_level_percent{", body)

    def test_a_broken_payload_shape_does_not_take_down_the_exporter(self):
        scraper = self.scraper(b"[1, 2, 3]")
        body, success = scraper.refresh()
        self.assertFalse(success)
        self.assertIn("snowy_tantangara_scrape_success 0", body)

    def test_a_missing_reservoir_fails_the_scrape_but_keeps_the_other(self):
        scraper = self.scraper(
            fixture("snowy_levels_two_years.json"), reservoirs=[TANTANGARA, "Lake Nee Nee"]
        )
        body, success = scraper.refresh()
        self.assertFalse(success)
        self.assertIn('reservoir="Tantangara Reservoir"} 11.33', body)

    def test_ttl_reuses_the_scrape(self):
        scraper = self.scraper(fixture("snowy_levels_two_years.json"), ttl=3600.0)
        scraper.exposition()
        scraper.exposition()
        scraper.exposition()
        self.assertEqual(len(self.calls), 1)

    def test_expired_ttl_refetches(self):
        scraper = self.scraper(fixture("snowy_levels_two_years.json"), ttl=0.0)
        scraper.exposition()
        scraper.exposition()
        self.assertEqual(len(self.calls), 2)

    def test_health_reflects_the_last_scrape(self):
        scraper = self.scraper(fixture("snowy_levels_two_years.json"), ttl=3600.0)
        self.assertEqual(scraper.health(), (True, ""))

    def test_health_reports_the_reason(self):
        scraper = self.scraper(ScrapeError("boom"), ttl=3600.0)
        healthy, message = scraper.health()
        self.assertFalse(healthy)
        self.assertIn("boom", message)

    def test_a_failure_does_not_lose_a_good_scrape(self):
        # The exposition is replaced, not blanked: the next scrape must be able
        # to report health without re-fetching a payload that is already known
        # to be unreadable.
        scraper = self.scraper(fixture("snowy_levels_two_years.json"), ttl=3600.0)
        scraper.refresh()
        body, success = scraper.exposition()
        self.assertTrue(success)
        self.assertIn("snowy_tantangara_scrape_success 1", body)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


def _raiser(exc):
    def boom(*args, **kwargs):
        raise exc

    return boom


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.scraper = exporter.LakeScraper(
            data_url="https://example.test/d",
            reservoirs=[TANTANGARA],
            timeout=5.0,
            ttl=3600.0,
            year=2026,
        )
        original = exporter.http_get_json
        exporter.http_get_json = lambda url, timeout, retries=0, retry_budget=0.0: json.loads(
            fixture("snowy_levels_two_years.json")
        )
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(self.scraper))
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_metrics_is_text_plain(self):
        status, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("snowy_tantangara_scrape_success 1", body)

    def test_healthz_is_ok(self):
        self.assertEqual(self.get("/healthz"), (200, "ok\n"))

    def test_index_links_to_the_endpoints(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("/metrics", body)
        # The index says what the data is, so nobody mistakes it for progress.
        self.assertIn("Not construction progress", body)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_trailing_slash_and_query_string_are_ignored(self):
        self.assertEqual(self.get("/metrics/")[0], 200)
        self.assertEqual(self.get("/metrics?x=1")[0], 200)

    def test_healthz_reports_503_when_the_upstream_failed(self):
        self.setUp()
        original = exporter.http_get_json
        exporter.http_get_json = _raiser(ScrapeError("upstream exploded"))
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))
        self.scraper._exposition = None
        status, body = self.get("/healthz")
        self.assertEqual(status, 503)
        self.assertIn("upstream exploded", body)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


class MainTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.original = sys.stdout
        sys.stdout = self.out
        self.addCleanup(lambda: setattr(sys, "stdout", self.original))
        self.http = exporter.http_get_json
        self.addCleanup(lambda: setattr(exporter, "http_get_json", self.http))
        self.urls = []

    def serve(self, body=f"", calls=None):
        def fake(url, timeout, retries=0, retry_budget=0.0):
            if calls is not None:
                calls.append(url)
            if isinstance(body, Exception):
                raise body
            return json.loads(body)

        exporter.http_get_json = fake

    def test_once_prints_the_exposition_and_exits_zero(self):
        self.serve(fixture("snowy_levels_two_years.json"))
        code = exporter.main(["--once"])
        self.assertEqual(code, 0)
        self.assertIn("snowy_tantangara_level_percent", self.out.getvalue())

    def test_once_exits_one_when_the_scrape_fails(self):
        self.serve(ScrapeError("HTTP 400"))
        self.assertEqual(exporter.main(["--once"]), 1)

    def test_once_still_prints_on_failure(self):
        # A failing scrape must remain scannable: the point of --once is to see
        # why, and an empty stdout hides scrape_success 0.
        self.serve(ScrapeError("HTTP 400"))
        exporter.main(["--once"])
        self.assertIn("snowy_tantangara_scrape_success 0", self.out.getvalue())

    def test_defaults_to_tantangara_only(self):
        calls = []
        self.serve(fixture("snowy_levels_two_years.json"), calls)
        exporter.main(["--once"])
        body = self.out.getvalue()
        self.assertIn('reservoir="Tantangara Reservoir"', body)
        self.assertNotIn("Lake Jindabyne", body)
        self.assertEqual(len(calls), 1, "one request, not one per reservoir")

    def test_reservoir_flag_is_repeatable(self):
        self.serve(fixture("snowy_levels_two_years.json"))
        exporter.main(["--once", "--reservoir", TANTANGARA, "--reservoir", JINDABYNE])
        body = self.out.getvalue()
        self.assertIn('reservoir="Tantangara Reservoir"', body)
        self.assertIn('reservoir="Lake Jindabyne"', body)

    def test_all_reservoirs_discovers_from_the_feed(self):
        calls = []
        self.serve(fixture("snowy_levels_two_years.json"), calls)
        exporter.main(["--once", "--all-reservoirs"])
        body = self.out.getvalue()
        for name in (TANTANGARA, JINDABYNE, EUCUMBENE):
            self.assertIn('reservoir="%s"' % name, body)
        # One extra request, for a single year, purely to learn the names.
        self.assertEqual(len(calls), 2)

    def test_all_reservoirs_falls_back_when_discovery_fails(self):
        # Discovery is one extra request. If it fails the exporter must still
        # serve the reservoir it knows about rather than serving nothing.
        calls = []

        def fake(url, timeout, retries=0, retry_budget=0.0):
            calls.append(url)
            if len(calls) == 1:
                raise ScrapeError("discovery failed")
            return json.loads(fixture("snowy_levels_two_years.json"))

        exporter.http_get_json = fake
        code = exporter.main(["--once", "--all-reservoirs"])
        self.assertEqual(code, 0)
        self.assertIn('reservoir="Tantangara Reservoir"', self.out.getvalue())
        self.assertNotIn("Lake Jindabyne", self.out.getvalue())

    def test_change_days_reaches_the_metric_name(self):
        self.serve(fixture("snowy_levels_two_years.json"))
        exporter.main(["--once", "--change-days", "14"])
        self.assertIn(
            "snowy_tantangara_level_change_14d_percentage_points", self.out.getvalue()
        )

    def test_binds_loopback_on_the_documented_default_port(self):
        # 0.0.0.0 is set explicitly by compose; the standalone default is
        # loopback, and the port must not collide with the WHT exporter's 9109.
        bound = {}
        original = exporter.ThreadingHTTPServer

        def make_server(address, handler):
            bound["address"] = address
            server = original(("127.0.0.1", 0), handler)
            self.addCleanup(server.server_close)
            server.serve_forever = _raiser(KeyboardInterrupt)
            return server

        exporter.ThreadingHTTPServer = make_server
        self.addCleanup(lambda: setattr(exporter, "ThreadingHTTPServer", original))
        self.serve(fixture("snowy_levels_two_years.json"))
        exporter.main([])
        self.assertEqual(bound["address"], ("127.0.0.1", 9110))
        self.assertNotEqual(9110, 9109)


class MainServeTest(unittest.TestCase):
    def setUp(self):
        self.original = exporter.http_get_json
        exporter.http_get_json = lambda url, timeout, retries=0, retry_budget=0.0: json.loads(
            fixture("snowy_levels_two_years.json")
        )
        self.addCleanup(lambda: setattr(exporter, "http_get_json", self.original))
        self.servers = []
        self.result = {}
        self.done = threading.Event()

    def launch(self):
        original_init = exporter.ThreadingHTTPServer

        def make_server(*args, **kwargs):
            server = original_init(*args, **kwargs)
            self.servers.append(server)
            return server

        exporter.ThreadingHTTPServer = make_server
        self.addCleanup(lambda: setattr(exporter, "ThreadingHTTPServer", original_init))

        def run():
            try:
                self.result["code"] = exporter.main(
                    ["--port", "0", "--listen-address", "127.0.0.1"]
                )
            finally:
                self.done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (thread.join(timeout=2), [s.shutdown() for s in self.servers]))

        deadline = time.time() + 5
        while not self.servers and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.servers, "main() never started a server")
        return self.servers[-1].server_address[1], thread

    def test_serves_metrics_and_exits_cleanly_on_shutdown(self):
        port, thread = self.launch()
        url = "http://127.0.0.1:%d/metrics" % port

        body = None
        deadline = time.time() + 5
        while body is None and time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    body = response.read().decode()
            except OSError:
                time.sleep(0.02)
        self.assertIsNotNone(body, "server never answered /metrics")
        self.assertIn("snowy_tantangara_scrape_success 1", body)

        # Shutting the server down must let main() return 0.
        self.servers[-1].shutdown()
        self.assertTrue(self.done.wait(timeout=5), "main() did not return after shutdown")
        thread.join(timeout=2)
        self.assertEqual(self.result["code"], 0)

    def test_keyboard_interrupt_shuts_down_cleanly(self):
        # Ctrl-C / docker stop path: the finally block must still close the
        # socket and main() must return 0 rather than propagating.
        original_init = exporter.ThreadingHTTPServer

        def make_server(*args, **kwargs):
            server = original_init(*args, **kwargs)
            self.servers.append(server)
            server.serve_forever = _raiser(KeyboardInterrupt)
            return server

        exporter.ThreadingHTTPServer = make_server
        self.addCleanup(lambda: setattr(exporter, "ThreadingHTTPServer", original_init))

        def run():
            self.result["code"] = exporter.main(
                ["--port", "0", "--listen-address", "127.0.0.1"]
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "main() hung on KeyboardInterrupt")
        self.assertEqual(self.result["code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
