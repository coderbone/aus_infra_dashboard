#!/usr/bin/env python3
"""Tests for wht_tbm_exporter.py.

Offline: every upstream response is served from scrapers/fixtures/, captured
verbatim from ArcGIS on 2026-09-29. No test touches the network.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_wht_tbm_exporter.py

`-t scrapers` is required. `unittest discover` rejects a start directory that
is not importable, and scrapers/ intentionally has no __init__.py so the
exporter stays a single bind-mounted file.

One test per failure mode, not just the happy path - the failure paths are the
ones that page someone at 3am, and several of them (an HTTP 200 carrying an
ArcGIS error body; a per-machine fault that must not take the whole scrape
down; a discovery fault that must fall back to cache) are easy to regress
silently.
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
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wht_tbm_exporter as exporter  # noqa: E402
from wht_tbm_exporter import ScrapeError  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

DASHBOARD_ID = "973e08367a544c879edd7f345f9d6a15"
WEBMAP_ID = "644ba22abece4a08bd5795ec50160d50"
BARANGAROO_ID = "1a00d5f1e47-layer-9"
PATYEGARANG_ID = "1a00d56bb59-layer-8"
BARANGAROO_URL = (
    "https://utility.arcgis.com/usrsvcs/servers/"
    "b613c8229c174e429200155d5e829e31/rest/services/"
    "P_AU_WHTP2_TBM_Barangaroo_Location_PublicView/FeatureServer/0"
)
PATYEGARANG_URL = (
    "https://utility.arcgis.com/usrsvcs/servers/"
    "21ebd5cac3e3405cbce55a7703c65af0/rest/services/"
    "P_AU_WHTP2_TBM_Patyegarang_Location_PublicView/FeatureServer/0"
)


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def fixture_json(name: str):
    return json.loads(fixture(name))


class Recorder:
    """Stand-in for http_get_json that records URLs and replays parsed bodies.

    Keys are matched by substring, so a test can pin one call and leave the
    rest of the discovery chain to the fixtures.
    """

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, timeout, retries=0, retry_budget=0.0):
        self.calls.append(url)
        for fragment, body in self.routes.items():
            if fragment in url:
                return json.loads(body)
        raise AssertionError("unexpected URL: %s" % url)

    @property
    def urls(self) -> list[str]:
        return list(self.calls)


# --------------------------------------------------------------------------- #
# is_transient
# --------------------------------------------------------------------------- #


def transient_reason() -> OSError:
    """The fault urllib surfaces as URLError.reason for a failed DNS lookup.

    This is the EAI_AGAIN that actually bites in the container: getaddrinfo
    raises gaierror and urllib re-wraps it in URLError. Note that http_get
    hands `exc.reason` to is_transient, never the URLError itself.
    """
    return socket.gaierror(socket.EAI_AGAIN, "Try again")


def permanent_reason() -> OSError:
    return OSError(errno.EACCES, "Permission denied")


def url_error(reason: OSError) -> urllib.error.URLError:
    return urllib.error.URLError(reason)


class IsTransientTest(unittest.TestCase):
    def test_timeout_and_connection_exceptions_are_transient(self):
        for exc in (TimeoutError(), ConnectionResetError(), ConnectionAbortedError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(exporter.is_transient(exc))

    def test_eai_again_is_transient(self):
        # The one actually seen in the container, about 10% of lookups.
        self.assertTrue(exporter.is_transient(transient_reason()))

    def test_transient_errnos_are_transient(self):
        for code in (errno.EAGAIN, errno.ECONNRESET, errno.ECONNABORTED):
            with self.subTest(errno=code):
                self.assertTrue(exporter.is_transient(OSError(code, os.strerror(code))))

    def test_other_errnos_are_permanent(self):
        for code in (errno.EACCES, errno.ENOENT, errno.EHOSTUNREACH):
            with self.subTest(errno=code):
                self.assertFalse(exporter.is_transient(OSError(code, os.strerror(code))))

    def test_arbitrary_object_is_not_transient(self):
        self.assertFalse(exporter.is_transient("Try again"))
        self.assertFalse(exporter.is_transient(None))
        self.assertFalse(exporter.is_transient(object()))


# --------------------------------------------------------------------------- #
# http_get
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpGetTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []

    def patch_urlopen(self, outcomes):
        """outcomes: list of bytes | Exception, one per attempt."""
        self.calls = []
        queue = list(outcomes)

        def fake_urlopen(request, timeout=None):
            self.calls.append(request.full_url)
            self.assertTrue(
                exporter.USER_AGENT in request.headers.get("User-agent", ""),
                "requests must carry a browser User-Agent or CloudFront 403s",
            )
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome)

        self.original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", self.original))

    def no_sleep(self):
        self.original_sleep = time.sleep
        time.sleep = lambda _seconds: None
        self.addCleanup(lambda: setattr(time, "sleep", self.original_sleep))

    def test_success_returns_body(self):
        self.patch_urlopen([b'{"ok":true}'])
        self.assertEqual(exporter.http_get("https://example.test/x", 1.0), b'{"ok":true}')
        self.assertEqual(len(self.calls), 1)

    def test_http_error_fails_immediately_without_retrying(self):
        # The server answered, so an immediate retry is unlikely to differ.
        self.patch_urlopen([urllib.error.HTTPError("u", 404, "Not Found", {}, None)])
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 1.0, retries=3)
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertEqual(len(self.calls), 1, "HTTP errors must not be retried")

    def test_permanent_urerror_fails_without_retrying(self):
        self.patch_urlopen([url_error(permanent_reason())])
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 1.0, retries=3)
        self.assertIn("Permission denied", str(ctx.exception))
        self.assertEqual(len(self.calls), 1)

    def test_retries_zero_fails_on_first_transient_fault(self):
        self.patch_urlopen([url_error(transient_reason())])
        with self.assertRaises(ScrapeError):
            exporter.http_get("https://example.test/x", 1.0, retries=0)
        self.assertEqual(len(self.calls), 1)

    def test_transient_fault_recovers_within_the_retry_count(self):
        self.no_sleep()
        self.patch_urlopen(
            [
                url_error(transient_reason()),
                url_error(OSError(errno.ECONNRESET, "Connection reset by peer")),
                b'{"ok":true}',
            ]
        )
        self.assertEqual(
            exporter.http_get("https://example.test/x", 1.0, retries=3), b'{"ok":true}'
        )
        self.assertEqual(len(self.calls), 3)

    def test_exhausting_the_retry_count_raises_and_tries_retries_plus_one_times(self):
        self.no_sleep()
        self.patch_urlopen([url_error(transient_reason())] * 4)
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 1.0, retries=2)
        self.assertIn("Try again", str(ctx.exception))
        self.assertEqual(len(self.calls), 3, "retries=2 means 3 attempts total")

    def test_retry_budget_stops_retrying_before_the_attempt_cap(self):
        # A failing lookup costs ~5s here, so an uncapped loop can outlast
        # Prometheus's 30s scrape_timeout. The budget is the real bound.
        self.no_sleep()
        self.patch_urlopen([url_error(transient_reason())] * 5)
        ticks = iter([100.0, 106.0, 200.0, 300.0, 400.0])
        original = time.monotonic
        time.monotonic = lambda: next(ticks)
        self.addCleanup(lambda: setattr(time, "monotonic", original))

        with self.assertRaises(ScrapeError):
            exporter.http_get("https://example.test/x", 1.0, retries=4, retry_budget=6.0)
        self.assertEqual(len(self.calls), 1, "budget must cut in before the retry cap")

    def test_zero_retry_budget_disables_the_budget_check(self):
        # `if retry_budget and ...` - a 0 budget means "no cap", not "no retries".
        self.no_sleep()
        self.patch_urlopen([url_error(transient_reason()), b"ok"])
        self.assertEqual(
            exporter.http_get("https://example.test/x", 1.0, retries=1, retry_budget=0.0),
            b"ok",
        )

    def test_negative_retries_short_circuits_the_loop(self):
        # The only way to reach the trailing `raise`, and it is a wart: the
        # caller never made a request, yet the error claims a give-up count.
        # `--retries -1` on the command line lands here.
        self.patch_urlopen([url_error(transient_reason())])
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get("https://example.test/x", 1.0, retries=-1)
        self.assertIn("gave up after 0 attempts", str(ctx.exception))
        self.assertEqual(self.calls, [], "range(0) means no request is attempted")


# --------------------------------------------------------------------------- #
# http_get_json
# --------------------------------------------------------------------------- #


class HttpGetJsonTest(unittest.TestCase):
    def patch_http_get(self, payload: bytes):
        original = exporter.http_get
        exporter.http_get = lambda *a, **k: payload
        self.addCleanup(lambda: setattr(exporter, "http_get", original))

    def test_parses_json_object(self):
        self.patch_http_get(b'{"fields":[]}')
        self.assertEqual(exporter.http_get_json("https://x", 1.0), {"fields": []})

    def test_invalid_json_raises(self):
        self.patch_http_get(b"<html>not json</html>")
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get_json("https://x", 1.0)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_arcgis_error_body_raises_even_though_status_is_200(self):
        # The whole reason this guard exists: ArcGIS answers 200 with
        # {"error":...} for a stale or mistyped service id, so a status check
        # passes and callers would silently read absent fields as None.
        body = fixture("arcgis_error_body.json")
        self.assertEqual(json.loads(body)["error"]["code"], 400)
        self.patch_http_get(body)
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get_json("https://x/rest/services/gone/FeatureServer/0?f=json", 1.0)
        message = str(ctx.exception)
        self.assertIn("ArcGIS error", message)
        self.assertIn("Item does not exist", message)

    def test_error_body_names_the_url(self):
        self.patch_http_get(b'{"error":{"code":400,"message":"nope"}}')
        with self.assertRaises(ScrapeError) as ctx:
            exporter.http_get_json("https://example.test/specific", 1.0)
        self.assertIn("https://example.test/specific", str(ctx.exception))

    def test_non_dict_json_is_returned_untouched(self):
        # Guarded by isinstance so a list body cannot crash the check itself.
        self.patch_http_get(b"[1, 2, 3]")
        self.assertEqual(exporter.http_get_json("https://x", 1.0), [1, 2, 3])

    def test_dict_without_error_key_is_returned(self):
        self.patch_http_get(b'{"features":[]}')
        self.assertEqual(exporter.http_get_json("https://x", 1.0), {"features": []})


# --------------------------------------------------------------------------- #
# Pure parsing helpers
# --------------------------------------------------------------------------- #


class LayerSlugTest(unittest.TestCase):
    def test_strips_tbm_prefix_and_location_suffix(self):
        self.assertEqual(exporter.layer_slug("TBM Barangaroo Location"), "barangaroo")

    def test_strips_progress_suffix(self):
        self.assertEqual(
            exporter.layer_slug("TBM Patyegarang Progress"), "patyegarang"
        )
        self.assertEqual(
            exporter.layer_slug("TBM Patyegarang Progress Line"), "patyegarang"
        )

    def test_camelcase_progressline_is_not_stripped(self):
        # "ProgressLine" has no separating space, so the suffix pattern does
        # not match. Not a live case - the real web map uses "Progress".
        self.assertEqual(
            exporter.layer_slug("TBM Patyegarang ProgressLine"), "patyegarang_progressline"
        )

    def test_titles_from_the_real_web_map(self):
        self.assertEqual(exporter.layer_slug("Tunnel Boundary"), "tunnel_boundary")
        self.assertEqual(exporter.layer_slug("TBM Barangaroo Progress"), "barangaroo")

    def test_case_insensitive(self):
        self.assertEqual(exporter.layer_slug("tbm barangaroo LOCATION"), "barangaroo")

    def test_underscores_punctuation(self):
        self.assertEqual(exporter.layer_slug("TBM Western-Barr Location"), "western_barr")

    def test_falls_back_to_unknown(self):
        for value in ("", None, "   ", "!!!"):
            with self.subTest(value=value):
                self.assertEqual(exporter.layer_slug(value), "unknown")


class WidgetValueFieldTest(unittest.TestCase):
    def test_prefers_chart_config_series(self):
        widget = {"chartConfig": {"series": [{"x": "Tunnel_Progress_m"}]}, "valueField": "Other"}
        self.assertEqual(exporter.widget_value_field(widget), "Tunnel_Progress_m")

    def test_falls_back_to_value_field(self):
        self.assertEqual(exporter.widget_value_field({"valueField": "Progress"}), "Progress")

    def test_skips_series_entries_without_x(self):
        widget = {"chartConfig": {"series": [{"y": "nope"}, {"x": "Real"}]}}
        self.assertEqual(exporter.widget_value_field(widget), "Real")

    def test_returns_none_when_absent(self):
        self.assertIsNone(exporter.widget_value_field({}))
        self.assertIsNone(exporter.widget_value_field({"chartConfig": {}}))


class StaticDatasetValueTest(unittest.TestCase):
    @staticmethod
    def widget(values, name="max", kind="staticDataset"):
        return {"datasets": [{"name": name, "type": kind, "data": {"values": values}}]}

    def test_reads_plain_number(self):
        self.assertEqual(exporter.static_dataset_value(self.widget([1567]), "max"), 1567.0)

    def test_reads_labelled_value_dict(self):
        self.assertEqual(
            exporter.static_dataset_value(
                self.widget([{"id": "0", "label": "1567", "type": "labelledValue", "value": 1567}]),
                "max",
            ),
            1567.0,
        )

    def test_accepts_staticvalues_type(self):
        self.assertEqual(
            exporter.static_dataset_value(
                self.widget([1500], name="reference", kind="staticValues"), "reference"
            ),
            1500.0,
        )

    def test_ignores_datasets_not_named(self):
        self.assertIsNone(
            exporter.static_dataset_value(self.widget([1567], name="min"), "max", "reference")
        )

    def test_ignores_non_static_dataset_types(self):
        self.assertIsNone(exporter.static_dataset_value(self.widget([1567], kind="featureSet"), "max"))

    def test_ignores_booleans(self):
        # bool is an int subclass; True must not become 1.0.
        self.assertIsNone(exporter.static_dataset_value(self.widget([True]), "max"))

    def test_ignores_non_numeric_values(self):
        self.assertIsNone(exporter.static_dataset_value(self.widget(["abc", None]), "max"))

    def test_empty_names_matches_any_static_dataset(self):
        self.assertEqual(exporter.static_dataset_value(self.widget([42], name="whatever")), 42.0)

    def test_returns_none_when_no_datasets(self):
        self.assertIsNone(exporter.static_dataset_value({}, "max"))
        self.assertIsNone(exporter.static_dataset_value({"datasets": []}, "max"))


class MachineNumberTest(unittest.TestCase):
    def test_extracts_from_parentheses(self):
        self.assertEqual(exporter.machine_number("Barangaroo (M110)"), "M110")

    def test_returns_empty_when_absent(self):
        self.assertEqual(exporter.machine_number("Barangaroo"), "")
        self.assertEqual(exporter.machine_number(""), "")
        self.assertEqual(exporter.machine_number(None), "")


class EscapeLabelTest(unittest.TestCase):
    def test_escapes_backslash_quote_and_newline(self):
        self.assertEqual(exporter.escape_label('a\\b"c\nd'), 'a\\\\b\\"c\\nd')


class FmtTest(unittest.TestCase):
    def test_none_is_nan(self):
        self.assertEqual(exporter.fmt(None), "NaN")

    def test_integral_values_render_without_decimal(self):
        self.assertEqual(exporter.fmt(3.0), "3")
        self.assertEqual(exporter.fmt(3), "3")

    def test_fractions_are_rounded(self):
        self.assertEqual(exporter.fmt(0.15955555), "0.1596")

    def test_nan_and_infinities(self):
        self.assertEqual(exporter.fmt(float("nan")), "NaN")
        self.assertEqual(exporter.fmt(float("inf")), "+Inf")
        self.assertEqual(exporter.fmt(float("-inf")), "-Inf")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


class CollectGaugeConfigTest(unittest.TestCase):
    def test_reads_the_real_dashboard(self):
        config = exporter.collect_gauge_config(fixture_json("dashboard_item.json"))
        self.assertEqual(set(config), {BARANGAROO_ID, PATYEGARANG_ID})
        self.assertEqual(config[BARANGAROO_ID]["target_m"], 1567.0)
        self.assertEqual(config[PATYEGARANG_ID]["target_m"], 1560.0)
        self.assertEqual(config[BARANGAROO_ID]["field"], "Tunnel_Progress_m")

    def test_desktop_target_overrides_stale_mobile_reference(self):
        # mobileView is visited first and still carries the stale 1500 m
        # reference; the desktop gauge must win. NOTES.md calls this out.
        config = exporter.collect_gauge_config(fixture_json("dashboard_item.json"))
        self.assertNotEqual(config[BARANGAROO_ID]["target_m"], 1500.0)
        self.assertNotEqual(config[PATYEGARANG_ID]["target_m"], 1500.0)

    def test_ignores_non_gauge_widgets(self):
        dashboard = {"desktopView": {"widgets": [{"type": "richTextWidget", "itemId": "x"}]}}
        self.assertEqual(exporter.collect_gauge_config(dashboard), {})

    def test_ignores_gauge_without_main_dataset(self):
        widget = {"type": "gaugeWidget", "datasets": [{"name": "max", "dataSource": None}]}
        self.assertEqual(exporter.collect_gauge_config({"desktopView": {"widgets": [widget]}}), {})

    def test_ignores_main_dataset_without_layer_id(self):
        widget = {
            "type": "gaugeWidget",
            "datasets": [{"name": "main", "dataSource": {"itemId": "x"}}],
        }
        self.assertEqual(exporter.collect_gauge_config({"desktopView": {"widgets": [widget]}}), {})

    def test_tolerates_missing_views(self):
        self.assertEqual(exporter.collect_gauge_config({}), {})
        self.assertEqual(exporter.collect_gauge_config({"desktopView": None, "mobileView": None}), {})

    def test_indicator_widget_is_accepted(self):
        widget = {
            "type": "indicatorWidget",
            "valueField": "Tunnel_Progress_m",
            "datasets": [
                {"name": "main", "dataSource": {"layerId": "L1"}},
                {"name": "reference", "type": "staticDataset", "data": {"values": [1500]}},
            ],
        }
        config = exporter.collect_gauge_config({"mobileView": {"widgets": [widget]}})
        self.assertEqual(config["L1"], {"target_m": 1500.0, "field": "Tunnel_Progress_m"})

    def test_layer_with_no_target_is_still_collected(self):
        widget = {
            "type": "gaugeWidget",
            "datasets": [{"name": "main", "dataSource": {"layerId": "L1"}}],
        }
        config = exporter.collect_gauge_config({"desktopView": {"widgets": [widget]}})
        self.assertEqual(config, {"L1": {}})


class WebMapLayersTest(unittest.TestCase):
    def test_reads_the_real_web_map(self):
        self.patch(fixture("webmap_item.json"))
        layers = exporter.web_map_layers(WEBMAP_ID, 1.0)
        self.assertEqual(len(layers), 5, "the real map carries non-TBM layers too")
        self.assertEqual(layers[BARANGAROO_ID]["title"], "TBM Barangaroo Location")
        self.assertEqual(layers[PATYEGARANG_ID]["title"], "TBM Patyegarang Location")
        self.assertTrue(layers[BARANGAROO_ID]["url"].startswith("https://"))

    def test_strips_trailing_slash_from_url(self):
        body = json.dumps(
            {"operationalLayers": [{"id": "L1", "title": "T", "url": "https://x/FeatureServer/0/"}]}
        ).encode()
        self.patch(body)
        self.assertEqual(exporter.web_map_layers("i", 1.0)["L1"]["url"], "https://x/FeatureServer/0")

    def test_title_falls_back_to_id(self):
        body = json.dumps({"operationalLayers": [{"id": "L1", "url": "https://x"}]}).encode()
        self.patch(body)
        self.assertEqual(exporter.web_map_layers("i", 1.0)["L1"]["title"], "L1")

    def test_skips_layers_without_id_or_url(self):
        body = json.dumps(
            {
                "operationalLayers": [
                    {"id": "L1", "url": "https://x"},
                    {"id": "L2"},
                    {"url": "https://y"},
                ]
            }
        ).encode()
        self.patch(body)
        self.assertEqual(list(exporter.web_map_layers("i", 1.0)), ["L1"])

    def test_tolerates_missing_operational_layers(self):
        self.patch(b"{}")
        self.assertEqual(exporter.web_map_layers("i", 1.0), {})

    def patch(self, body: bytes):
        original = exporter.http_get_json
        exporter.http_get_json = lambda *a, **k: json.loads(body)
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))


class DescribeLayerTest(unittest.TestCase):
    def patch(self, body: bytes):
        original = exporter.http_get_json
        exporter.http_get_json = lambda *a, **k: json.loads(body)
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))

    def test_reads_the_real_layer_metadata(self):
        self.patch(fixture("layer_barangaroo_meta.json"))
        info = exporter.describe_layer(BARANGAROO_URL, 1.0)
        self.assertEqual(
            info,
            {
                "name_field": "TBM",
                "progress_field": "Tunnel_Progress_m",
                "ring_field": "Ring_Number",
                "timestamp_field": "Timestamp",
            },
        )

    def test_progress_hint_priority(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "progress", "type": "esriFieldTypeDouble"},
                        {"name": "progress_m", "type": "esriFieldTypeDouble"},
                        {"name": "tunnel_progress_m", "type": "esriFieldTypeDouble"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(
            exporter.describe_layer("u", 1.0)["progress_field"], "tunnel_progress_m"
        )

    def test_preserves_original_field_case(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "Tunnel_Progress_M", "type": "esriFieldTypeDouble"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(exporter.describe_layer("u", 1.0)["progress_field"], "Tunnel_Progress_M")

    def test_ring_hint_prefers_ring_number(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "ring", "type": "esriFieldTypeInteger"},
                        {"name": "ring_number", "type": "esriFieldTypeInteger"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(exporter.describe_layer("u", 1.0)["ring_field"], "ring_number")

    def test_timestamp_is_first_date_field(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "Created", "type": "esriFieldTypeDate"},
                        {"name": "Edited", "type": "esriFieldTypeDate"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(exporter.describe_layer("u", 1.0)["timestamp_field"], "Created")

    def test_name_prefers_field_labelled_tbm(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "Asset", "type": "esriFieldTypeString"},
                        {"name": "tbm", "type": "esriFieldTypeString"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(exporter.describe_layer("u", 1.0)["name_field"], "tbm")

    def test_name_falls_back_to_first_string_field(self):
        self.patch(
            json.dumps(
                {
                    "fields": [
                        {"name": "Chainage", "type": "esriFieldTypeDouble"},
                        {"name": "Label", "type": "esriFieldTypeString"},
                    ]
                }
            ).encode()
        )
        self.assertEqual(exporter.describe_layer("u", 1.0)["name_field"], "Label")

    def test_no_fields_yields_all_none(self):
        self.patch(b'{"fields":[]}')
        self.assertEqual(
            exporter.describe_layer("u", 1.0),
            {
                "name_field": None,
                "progress_field": None,
                "ring_field": None,
                "timestamp_field": None,
            },
        )

    def test_arcgis_error_body_raises_instead_of_returning_nones(self):
        # Regression: before the http_get_json guard this returned four Nones
        # with no error, and that got cached and only blew up later. Patched
        # below http_get_json so the guard itself is what is under test.
        original = exporter.http_get
        exporter.http_get = lambda *a, **k: fixture("arcgis_error_body.json")
        self.addCleanup(lambda: setattr(exporter, "http_get", original))
        with self.assertRaises(ScrapeError):
            exporter.describe_layer("https://x/FeatureServer/0", 1.0)


class DiscoverDashboardItemIdTest(unittest.TestCase):
    def patch(self, body: bytes):
        original = exporter.http_get
        exporter.http_get = lambda *a, **k: body
        self.addCleanup(lambda: setattr(exporter, "http_get", original))

    def test_extracts_id_from_the_real_portal_page(self):
        self.patch(fixture("portal_page.html"))
        self.assertEqual(
            exporter.discover_dashboard_item_id("https://caportal.test/tbm-tracker", 1.0),
            DASHBOARD_ID,
        )

    def test_accepts_uppercase_hex(self):
        self.patch(("arcgis.com/apps/dashboards/" + DASHBOARD_ID.upper()).encode())
        self.assertEqual(
            exporter.discover_dashboard_item_id("https://x", 1.0), DASHBOARD_ID.upper()
        )

    def test_raises_when_no_dashboard_id_in_page(self):
        self.patch(b"<html>CloudFront says no</html>")
        with self.assertRaises(ScrapeError) as ctx:
            exporter.discover_dashboard_item_id("https://caportal.test/tbm-tracker", 1.0)
        self.assertIn("no ArcGIS dashboard id", str(ctx.exception))

    def test_raises_on_a_404_portal_page(self):
        # The live portal 403s without a browser User-Agent; http_get turns
        # that into ScrapeError before we ever get here.
        original = exporter.http_get

        def raise_403(*a, **k):
            raise ScrapeError("GET https://x -> HTTP 403")

        exporter.http_get = raise_403
        self.addCleanup(lambda: setattr(exporter, "http_get", original))
        with self.assertRaises(ScrapeError) as ctx:
            exporter.discover_dashboard_item_id("https://caportal.test/tbm-tracker", 1.0)
        self.assertIn("403", str(ctx.exception))


class DiscoverTest(unittest.TestCase):
    """End-to-end discovery against fixtures, plus every failure branch."""

    def patch(self, http_get=None, http_get_json=None, discover_id=None):
        for name, replacement in (
            ("http_get", http_get),
            ("http_get_json", http_get_json),
            ("discover_dashboard_item_id", discover_id),
        ):
            if replacement is None:
                continue
            original = getattr(exporter, name)
            setattr(exporter, name, replacement)
            self.addCleanup(lambda n=name, o=original: setattr(exporter, n, o))

    @staticmethod
    def only_map_widgets(dashboard: dict) -> dict:
        """Strip every gauge/indicator, so the synthetic ones below are the only
        source of targets and field names."""
        for view in ("desktopView", "mobileView"):
            widgets = (dashboard.get(view) or {}).get("widgets") or []
            (dashboard[view])["widgets"] = [w for w in widgets if w.get("type") == "mapWidget"]
        return dashboard

    @staticmethod
    def add_bare_gauge(dashboard: dict, layer_id: str) -> dict:
        """A gauge with a main dataset but no target and no value field."""
        dashboard["desktopView"]["widgets"].append(
            {
                "type": "gaugeWidget",
                "datasets": [{"name": "main", "dataSource": {"layerId": layer_id}}],
            }
        )
        return dashboard

    def good_stack(self):
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=Recorder(
                {
                    DASHBOARD_ID: fixture("dashboard_item.json"),
                    WEBMAP_ID: fixture("webmap_item.json"),
                    "/FeatureServer/0?f=json": fixture("layer_barangaroo_meta.json"),
                }
            ),
        )

    def test_discovers_both_tbms_from_fixtures(self):
        self.good_stack()
        config = exporter.discover("https://caportal.test/tbm-tracker", 1.0)
        self.assertEqual(config["dashboard_id"], DASHBOARD_ID)
        self.assertEqual(set(config["machines"]), {"barangaroo", "patyegarang"})

        barangaroo = config["machines"]["barangaroo"]
        self.assertEqual(barangaroo["target_m"], 1567.0)
        self.assertEqual(barangaroo["progress_field"], "Tunnel_Progress_m")
        self.assertEqual(barangaroo["name_field"], "TBM")
        self.assertEqual(barangaroo["ring_field"], "Ring_Number")
        self.assertEqual(barangaroo["timestamp_field"], "Timestamp")
        self.assertEqual(barangaroo["layer_id"], BARANGAROO_ID)

        patyegarang = config["machines"]["patyegarang"]
        self.assertEqual(patyegarang["target_m"], 1560.0)

    def test_only_layers_referenced_by_a_gauge_are_included(self):
        # The real web map also carries "Tunnel Boundary" and two
        # "...Progress" layers that no gauge points at.
        self.good_stack()
        config = exporter.discover("https://caportal.test/tbm-tracker", 1.0)
        self.assertEqual(len(config["machines"]), 2)

    def test_raises_when_dashboard_has_no_map_widget(self):
        dashboard = {"desktopView": {"widgets": [{"type": "gaugeWidget"}]}}
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=Recorder({DASHBOARD_ID: json.dumps(dashboard).encode()}),
        )
        with self.assertRaises(ScrapeError) as ctx:
            exporter.discover("https://x", 1.0)
        self.assertIn("no map widget", str(ctx.exception))

    def test_raises_when_no_gauge_layer_matches_the_web_map(self):
        dashboard = fixture_json("dashboard_item.json")
        web_map = {"operationalLayers": [{"id": "unrelated", "url": "https://x"}]}
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=Recorder(
                {DASHBOARD_ID: json.dumps(dashboard).encode(),
                 WEBMAP_ID: json.dumps(web_map).encode()}
            ),
        )
        with self.assertRaises(ScrapeError) as ctx:
            exporter.discover("https://x", 1.0)
        self.assertIn("no TBM layers referenced", str(ctx.exception))

    def test_raises_when_a_layer_publishes_no_progress_field(self):
        # No gauge series and no column matching a progress hint, so there is
        # nothing to read metres from and the whole discovery must refuse.
        dashboard = self.add_bare_gauge(
            self.only_map_widgets(fixture_json("dashboard_item.json")), BARANGAROO_ID
        )
        blank = json.dumps({"fields": [{"name": "TBM", "type": "esriFieldTypeString"}]}).encode()
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=Recorder({
                DASHBOARD_ID: json.dumps(dashboard).encode(),
                WEBMAP_ID: fixture("webmap_item.json"),
                "/FeatureServer/0?f=json": blank,
            }),
        )
        with self.assertRaises(ScrapeError) as ctx:
            exporter.discover("https://x", 1.0)
        self.assertIn("no progress field", str(ctx.exception))

    def test_missing_target_is_a_warning_not_an_error(self):
        # Untargeted TBMs are still worth exporting; the gauges that need a
        # target render as NaN rather than the scrape failing.
        dashboard = fixture_json("dashboard_item.json")
        dashboard = self.add_bare_gauge(
            self.only_map_widgets(dashboard), BARANGAROO_ID
        )
        self.add_bare_gauge(dashboard, PATYEGARANG_ID)
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=Recorder({
                DASHBOARD_ID: json.dumps(dashboard).encode(),
                WEBMAP_ID: fixture("webmap_item.json"),
                "/FeatureServer/0?f=json": fixture("layer_barangaroo_meta.json"),
            }),
        )
        config = exporter.discover("https://x", 1.0)
        self.assertIsNone(config["machines"]["barangaroo"]["target_m"])
        self.assertIsNone(config["machines"]["patyegarang"]["target_m"])
        # progress still resolves, from the layer's own columns.
        self.assertEqual(
            config["machines"]["barangaroo"]["progress_field"], "Tunnel_Progress_m"
        )

    def test_propagates_portal_failure(self):
        self.patch(discover_id=_raiser(ScrapeError("no ArcGIS dashboard id found")))
        with self.assertRaises(ScrapeError):
            exporter.discover("https://x", 1.0)

    def test_propagates_arcgis_error_body(self):
        self.patch(
            discover_id=lambda *a, **k: DASHBOARD_ID,
            http_get_json=lambda *a, **k: (_ for _ in ()).throw(
                ScrapeError("GET x -> ArcGIS error")
            ),
        )
        with self.assertRaises(ScrapeError):
            exporter.discover("https://x", 1.0)


def _raiser(exc: Exception):
    def raise_it(*a, **k):
        raise exc

    return raise_it


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "nested", "config.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def test_roundtrip(self):
        config = {"dashboard_id": DASHBOARD_ID, "machines": {"barangaroo": {"target_m": 1}}}
        exporter.save_cache(self.path, config)
        self.assertEqual(exporter.load_cache(self.path), config)

    def test_save_creates_missing_directories(self):
        deeper = os.path.join(self.dir.name, "does", "not", "exist", "config.json")
        self.assertFalse(os.path.isdir(os.path.dirname(deeper)))
        exporter.save_cache(deeper, {"machines": {"a": {}}})
        self.assertTrue(os.path.isfile(deeper))

    def test_save_leaves_no_temp_file_behind(self):
        exporter.save_cache(self.path, {"machines": {"a": {}}})
        self.assertEqual(
            [f for f in os.listdir(os.path.dirname(self.path)) if f.endswith(".tmp")], []
        )

    def test_missing_file_returns_none(self):
        self.assertIsNone(exporter.load_cache(os.path.join(self.dir.name, "nope.json")))

    def test_corrupt_json_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(exporter.load_cache(self.path))

    def test_cache_without_machines_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"dashboard_id": DASHBOARD_ID}, handle)
        self.assertIsNone(exporter.load_cache(self.path))

    def test_unwritable_path_is_a_warning_not_an_exception(self):
        # A read-only or nonexistent-volume cache must not kill the scrape.
        exporter.save_cache(os.path.join(self.dir.name, "a", "b", "c", "config.json"), {})
        blocked = os.path.join(self.dir.name, "blocked")
        os.makedirs(blocked)
        exporter.save_cache(blocked, {"machines": {"a": {}}})  # target is a directory


# --------------------------------------------------------------------------- #
# latest_record
# --------------------------------------------------------------------------- #


def machine(**overrides) -> dict:
    base = {
        "tbm": "barangaroo",
        "layer_title": "TBM Barangaroo Location",
        "layer_id": BARANGAROO_ID,
        "layer_url": BARANGAROO_URL,
        "target_m": 1567.0,
        "name_field": "TBM",
        "progress_field": "Tunnel_Progress_m",
        "ring_field": "Ring_Number",
        "timestamp_field": "Timestamp",
    }
    base.update(overrides)
    return base


class LatestRecordTest(unittest.TestCase):
    def patch(self, body: bytes):
        recorder = Recorder({"/query?": body})
        original = exporter.http_get_json
        exporter.http_get_json = recorder
        self.addCleanup(lambda: setattr(exporter, "http_get_json", original))
        return recorder

    def test_reads_the_real_query_response(self):
        self.patch(fixture("query_barangaroo.json"))
        record = exporter.latest_record(machine(), 1.0)
        self.assertEqual(record["TBM"], "Barangaroo (M110)")
        self.assertEqual(record["Ring_Number"], 93)

    def test_builds_the_expected_query_url(self):
        recorder = self.patch(fixture("query_barangaroo.json"))
        exporter.latest_record(machine(), 1.0)
        url = recorder.urls[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith(BARANGAROO_URL + "/query?"))
        self.assertEqual(params["where"], ["1=1"])
        self.assertEqual(params["orderByFields"], ["Timestamp DESC"])
        self.assertEqual(params["resultRecordCount"], ["1"])
        self.assertEqual(params["returnGeometry"], ["false"])
        self.assertEqual(
            params["outFields"], ["TBM,Tunnel_Progress_m,Ring_Number,Timestamp"]
        )

    def test_omits_absent_optional_fields_from_outfields(self):
        recorder = self.patch(fixture("query_barangaroo.json"))
        exporter.latest_record(machine(ring_field=None, name_field=None), 1.0)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(recorder.urls[0]).query)
        self.assertEqual(params["outFields"], ["Tunnel_Progress_m,Timestamp"])

    def test_raises_when_no_features(self):
        self.patch(b'{"features":[]}')
        with self.assertRaises(ScrapeError) as ctx:
            exporter.latest_record(machine(), 1.0)
        self.assertIn("returned no features", str(ctx.exception))

    def test_raises_when_features_key_absent(self):
        self.patch(b"{}")
        with self.assertRaises(ScrapeError):
            exporter.latest_record(machine(), 1.0)

    def test_raises_on_arcgis_error_body(self):
        # Patched below http_get_json so the guard is what is under test.
        original = exporter.http_get
        exporter.http_get = lambda *a, **k: fixture("arcgis_error_body.json")
        self.addCleanup(lambda: setattr(exporter, "http_get", original))
        with self.assertRaises(ScrapeError) as ctx:
            exporter.latest_record(machine(), 1.0)
        self.assertIn("ArcGIS error", str(ctx.exception))

    def test_returns_the_first_feature(self):
        self.patch(
            json.dumps(
                {"features": [{"attributes": {"TBM": "newest"}}, {"attributes": {"TBM": "older"}}]}
            ).encode()
        )
        self.assertEqual(exporter.latest_record(machine(), 1.0)["TBM"], "newest")


# --------------------------------------------------------------------------- #
# collect - per-machine isolation
# --------------------------------------------------------------------------- #


def attributes(**overrides) -> dict:
    base = {"TBM": "Barangaroo (M110)", "Tunnel_Progress_m": 250.88568, "Ring_Number": 93, "Timestamp": 1790652017000}
    base.update(overrides)
    return base


class CollectTest(unittest.TestCase):
    def two_machine_config(self, target=True) -> dict:
        first = machine()
        second = machine(
            tbm="patyegarang",
            layer_title="TBM Patyegarang Location",
            layer_id=PATYEGARANG_ID,
            layer_url=PATYEGARANG_URL,
        )
        if target:
            second["target_m"] = 1560.0
        else:
            second["target_m"] = None
        return {"machines": {"barangaroo": first, "patyegarang": second}}

    def patch_latest(self, side_effect):
        original = exporter.latest_record
        exporter.latest_record = side_effect
        self.addCleanup(lambda: setattr(exporter, "latest_record", original))

    def test_both_machines_succeed(self):
        def fake(m, *a, **k):
            return attributes(TBM="Barangaroo (M110)") if m["tbm"] == "barangaroo" else attributes(
                TBM="Patyegarang (M111)", Tunnel_Progress_m=463.4, Ring_Number=182
            )

        self.patch_latest(fake)
        samples, errors = exporter.collect(self.two_machine_config(), 1.0)
        self.assertEqual(errors, [])
        self.assertEqual([s["tbm"] for s in samples], ["barangaroo", "patyegarang"])

    def test_computes_derived_values(self):
        self.patch_latest(lambda m, *a, **k: attributes())
        samples, _ = exporter.collect({"machines": {"barangaroo": machine()}}, 1.0)
        sample = samples[0]
        self.assertEqual(sample["name"], "Barangaroo (M110)")
        self.assertEqual(sample["machine"], "M110")
        self.assertAlmostEqual(sample["distance_m"], 250.88568)
        self.assertEqual(sample["target_m"], 1567.0)
        self.assertAlmostEqual(sample["remaining_m"], 1567.0 - 250.88568)
        self.assertAlmostEqual(sample["ratio"], 250.88568 / 1567.0)
        self.assertEqual(sample["ring"], 93)
        self.assertAlmostEqual(sample["reported_at"], 1790652017000 / 1000.0)

    def test_one_failing_machine_does_not_take_down_the_other(self):
        # This is the property the whole design turns on: a single bad layer
        # costs that TBM's sample and nothing else.
        def fake(m, *a, **k):
            if m["tbm"] == "barangaroo":
                raise ScrapeError("upstream exploded")
            return attributes(TBM="Patyegarang (M111)", Tunnel_Progress_m=463.4)

        self.patch_latest(fake)
        samples, errors = exporter.collect(self.two_machine_config(), 1.0)
        self.assertEqual([s["tbm"] for s in samples], ["patyegarang"])
        self.assertEqual(len(errors), 1)
        self.assertIn("barangaroo", errors[0])

    def test_null_progress_is_an_error_not_a_zero(self):
        self.patch_latest(lambda m, *a, **k: attributes(Tunnel_Progress_m=None))
        samples, errors = exporter.collect({"machines": {"barangaroo": machine()}}, 1.0)
        self.assertEqual(samples, [])
        self.assertIn("null progress value", errors[0])

    def test_non_numeric_progress_is_caught(self):
        self.patch_latest(lambda m, *a, **k: attributes(Tunnel_Progress_m="not a number"))
        samples, errors = exporter.collect({"machines": {"barangaroo": machine()}}, 1.0)
        self.assertEqual(samples, [])
        self.assertEqual(len(errors), 1)

    def test_missing_key_is_caught(self):
        self.patch_latest(lambda m, *a, **k: {"TBM": "x"})
        samples, errors = exporter.collect({"machines": {"barangaroo": machine()}}, 1.0)
        self.assertEqual(samples, [])
        self.assertEqual(len(errors), 1)

    def test_missing_timestamp_field_is_survivable(self):
        self.patch_latest(lambda m, *a, **k: attributes())
        config = {"machines": {"barangaroo": machine(timestamp_field=None)}}
        samples, errors = exporter.collect(config, 1.0)
        self.assertEqual(errors, [])
        self.assertIsNone(samples[0]["reported_at"])

    def test_untargeted_machine_leaves_ratio_and_remaining_empty(self):
        self.patch_latest(lambda m, *a, **k: attributes())
        config = {"machines": {"barangaroo": machine(target_m=None)}}
        samples, errors = exporter.collect(config, 1.0)
        self.assertEqual(errors, [])
        self.assertIsNone(samples[0]["ratio"])
        self.assertIsNone(samples[0]["remaining_m"])

    def test_zero_target_does_not_divide_by_zero(self):
        self.patch_latest(lambda m, *a, **k: attributes())
        config = {"machines": {"barangaroo": machine(target_m=0)}}
        samples, errors = exporter.collect(config, 1.0)
        self.assertEqual(errors, [])
        self.assertIsNone(samples[0]["ratio"])

    def test_name_falls_back_to_layer_title(self):
        self.patch_latest(lambda m, *a, **k: attributes(TBM=None))
        samples, _ = exporter.collect({"machines": {"barangaroo": machine()}}, 1.0)
        self.assertEqual(samples[0]["name"], "TBM Barangaroo Location")


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def sample(**overrides) -> dict:
    base = {
        "tbm": "barangaroo",
        "name": "Barangaroo (M110)",
        "machine": "M110",
        "distance_m": 250.88568,
        "target_m": 1567.0,
        "remaining_m": 1316.11432,
        "ratio": 0.16016927,
        "ring": 93,
        "reported_at": 1790652017.0,
    }
    base.update(overrides)
    return base


def identity(**overrides) -> dict:
    base = {"tbm": "barangaroo", "name": "Barangaroo (M110)", "machine": "M110"}
    base.update(overrides)
    return base


class RenderTest(unittest.TestCase):
    def render(self, samples, identities, success=True, stale=False, duration=1.25, generated=1.0e9):
        return exporter.render(samples, identities, success, stale, duration, generated)

    def test_emits_every_documented_metric(self):
        body = self.render([sample()], [identity()])
        for metric in (
            "wht_tbm_info",
            "wht_tbm_distance_excavated_m",
            "wht_tbm_target_distance_m",
            "wht_tbm_remaining_distance_m",
            "wht_tbm_progress_ratio",
            "wht_tbm_ring_number",
            "wht_tbm_last_report_timestamp_seconds",
            "wht_tbm_scrape_success",
            "wht_tbm_config_stale",
            "wht_tbm_scrape_duration_seconds",
            "wht_tbm_last_scrape_timestamp_seconds",
        ):
            with self.subTest(metric=metric):
                self.assertIn("# TYPE %s gauge" % metric, body)

    def test_values_render(self):
        body = self.render([sample()], [identity()])
        self.assertIn('wht_tbm_info{tbm="barangaroo",name="Barangaroo (M110)",machine="M110"} 1', body)
        self.assertIn('wht_tbm_distance_excavated_m{tbm="barangaroo"} 250.8857', body)
        self.assertIn('wht_tbm_target_distance_m{tbm="barangaroo"} 1567', body)
        self.assertIn('wht_tbm_ring_number{tbm="barangaroo"} 93', body)

    def test_info_survives_a_failed_scrape(self):
        # Static metadata is carried over rather than dropped: a transient
        # upstream fault must not break joins on wht_tbm_info.
        body = self.render([], [identity()], success=False)
        self.assertIn('wht_tbm_info{tbm="barangaroo",name="Barangaroo (M110)",machine="M110"} 1', body)
        self.assertIn("wht_tbm_scrape_success 0", body)
        self.assertNotIn("wht_tbm_distance_excavated_m{tbm=", body)

    def test_partial_scrape_emits_only_answering_machines(self):
        body = self.render(
            [sample(tbm="patyegarang", name="Patyegarang (M111)", machine="M111")],
            [identity(), identity(tbm="patyegarang", name="Patyegarang (M111)", machine="M111")],
            success=False,
        )
        self.assertIn('wht_tbm_distance_excavated_m{tbm="patyegarang"}', body)
        self.assertNotIn('wht_tbm_distance_excavated_m{tbm="barangaroo"}', body)
        self.assertIn('wht_tbm_info{tbm="barangaroo"', body)

    def test_missing_values_render_as_nan(self):
        body = self.render([sample(ratio=None, remaining_m=None)], [identity()])
        self.assertIn('wht_tbm_progress_ratio{tbm="barangaroo"} NaN', body)
        self.assertIn('wht_tbm_remaining_distance_m{tbm="barangaroo"} NaN', body)

    def test_flags(self):
        self.assertIn("wht_tbm_scrape_success 1", self.render([], [], success=True))
        self.assertIn("wht_tbm_scrape_success 0", self.render([], [], success=False))
        self.assertIn("wht_tbm_config_stale 0", self.render([], [], stale=False))
        self.assertIn("wht_tbm_config_stale 1", self.render([], [], stale=True))
        self.assertIn("wht_tbm_scrape_duration_seconds 1.25", self.render([], []))
        self.assertIn("wht_tbm_last_scrape_timestamp_seconds 1000000000", self.render([], []))

    def test_labels_are_escaped(self):
        # A quote or newline in a layer title must not break the exposition.
        body = self.render(
            [sample(name='Barangaroo "B"\nnext', machine="M110")],
            [identity(name='Barangaroo "B"\nnext')],
        )
        self.assertIn(r'name="Barangaroo \"B\"\nnext"', body)

    def test_output_ends_with_newline(self):
        self.assertTrue(self.render([], []).endswith("\n"))


# --------------------------------------------------------------------------- #
# TbmScraper
# --------------------------------------------------------------------------- #


def scraper_with(cache_file: str, ttl: float = 60.0) -> exporter.TbmScraper:
    return exporter.TbmScraper(
        "https://caportal.test/tbm-tracker", cache_file, 1.0, ttl, retries=0, retry_budget=0.0
    )


class TbmScraperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = os.path.join(self.dir.name, "config.json")
        self.scraper = scraper_with(self.cache)

    def patch(self, name, replacement):
        original = getattr(exporter, name)
        setattr(exporter, name, replacement)
        self.addCleanup(lambda: setattr(exporter, name, original))

    def working_discovery(self, machines=None):
        return lambda *a, **k: {
            "discovered_at": 1,
            "page_url": "https://caportal.test/tbm-tracker",
            "dashboard_id": DASHBOARD_ID,
            "machines": machines
            if machines is not None
            else {"barangaroo": machine(), "patyegarang": machine(tbm="patyegarang")},
        }

    def healthy_collect(self, config, *a, **k):
        return [sample(tbm=slug) for slug in config["machines"]], []

    def test_cold_scrape_discovers_collects_and_caches(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", self.healthy_collect)
        body, success = self.scraper.refresh()
        self.assertTrue(success)
        self.assertIn("wht_tbm_distance_excavated_m{tbm=\"barangaroo\"}", body)
        self.assertTrue(os.path.isfile(self.cache), "config should be written to cache")
        self.assertIn("wht_tbm_config_stale 0", body)

    def test_discovery_failure_falls_back_to_cache(self):
        exporter.save_cache(
            self.cache,
            {"dashboard_id": DASHBOARD_ID, "machines": {"barangaroo": machine()}},
        )
        self.patch("discover", _raiser(ScrapeError("portal is down")))
        self.patch("collect", lambda config, *a, **k: ([sample()], []))
        body, success = self.scraper.refresh()
        self.assertTrue(success, "cached data is still real data")
        self.assertIn("wht_tbm_config_stale 1", body)
        self.assertIn('wht_tbm_distance_excavated_m{tbm="barangaroo"}', body)

    def test_discovery_failure_with_no_cache_degrades_without_raising(self):
        # refresh() must not 500: it reports the failure and keeps serving.
        self.patch("discover", _raiser(ScrapeError("portal is down")))
        body, success = self.scraper.refresh()
        self.assertFalse(success)
        self.assertIn("wht_tbm_scrape_success 0", body)
        self.assertIn("portal is down", self.scraper.health()[1] or "portal is down")
        self.assertNotIn("wht_tbm_info{", body)

    def test_per_machine_failure_keeps_info_and_other_samples(self):
        def half_broken(config, *a, **k):
            return [sample(tbm="patyegarang", name="Patyegarang (M111)", machine="M111")], [
                "barangaroo: upstream exploded"
            ]

        self.patch("discover", self.working_discovery())
        self.patch("collect", half_broken)
        body, success = self.scraper.refresh()
        self.assertFalse(success, "a partial scrape is still a failure")
        self.assertIn('wht_tbm_distance_excavated_m{tbm="patyegarang"}', body)
        self.assertIn('wht_tbm_info{tbm="patyegarang"', body)
        self.assertNotIn('wht_tbm_info{tbm="barangaroo"', body)

    def test_info_carries_over_a_later_failed_scrape(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", self.healthy_collect)
        self.scraper.refresh()
        self.assertIn('wht_tbm_info{tbm="barangaroo"', self.scraper._exposition)

        # Now the whole scrape fails; the fleet description must survive.
        self.patch("collect", lambda config, *a, **k: ([], ["upstream exploded"]))
        body, success = self.scraper.refresh()
        self.assertFalse(success)
        self.assertIn('wht_tbm_info{tbm="barangaroo"', body)

    def test_identities_pruned_when_config_drops_a_machine(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", self.healthy_collect)
        self.scraper.refresh()
        self.assertIn("patyegarang", self.scraper._identities)

        # Barangaroo alone on the next discovery: the retired machine's
        # wht_tbm_info must go away rather than linger forever. Config is
        # memoised in memory, so clear it to model a restart picking up a
        # changed dashboard.
        self.scraper._config = None
        self.patch("discover", self.working_discovery(machines={"barangaroo": machine()}))
        self.patch("collect", lambda config, *a, **k: ([sample()], []))
        body, _ = self.scraper.refresh()
        self.assertNotIn("patyegarang", self.scraper._identities)
        self.assertNotIn('wht_tbm_info{tbm="patyegarang"', body)

    def test_config_is_discovered_once_and_cached_in_memory(self):
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return self.working_discovery()()

        self.patch("discover", counting)
        self.patch("collect", self.healthy_collect)
        self.scraper.refresh()
        self.scraper.refresh()
        self.assertEqual(len(calls), 1, "config discovery should not repeat")

    def test_exposition_is_cached_within_the_ttl(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", self.healthy_collect)
        self.scraper.exposition()
        first = self.scraper._exposition
        self.scraper.exposition()
        self.assertEqual(self.scraper._exposition, first)

    def test_exposition_refreshes_once_the_ttl_expires(self):
        scraper = scraper_with(self.cache, ttl=0.01)
        calls = []

        def counting_collect(config, *a, **k):
            calls.append(1)
            return self.healthy_collect(config, *a, **k)

        self.patch("discover", self.working_discovery())
        self.patch("collect", counting_collect)
        scraper.exposition()
        time.sleep(0.05)
        scraper.exposition()
        self.assertEqual(len(calls), 2)

    def test_health_reflects_the_last_scrape(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", lambda config, *a, **k: ([], ["boom"]))
        healthy, message = self.scraper.health()
        self.assertFalse(healthy)
        self.assertIn("boom", message)

    def test_health_ok_on_a_good_scrape(self):
        self.patch("discover", self.working_discovery())
        self.patch("collect", self.healthy_collect)
        self.assertEqual(self.scraper.health()[0], True)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = os.path.join(self.dir.name, "config.json")
        self.scraper = scraper_with(self.cache)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), exporter.make_handler(self.scraper))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def patch(self, name, replacement):
        original = getattr(exporter, name)
        setattr(exporter, name, replacement)
        self.addCleanup(lambda: setattr(exporter, name, original))

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()

    def failing_stack(self):
        self.patch("discover", _raiser(ScrapeError("portal is down")))

    def test_index(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("/metrics", body)

    def test_index_html_alias(self):
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_metrics_serves_exposition(self):
        self.patch(
            "discover",
            lambda *a, **k: {
                "discovered_at": 1,
                "page_url": "u",
                "dashboard_id": DASHBOARD_ID,
                "machines": {"barangaroo": machine()},
            },
        )
        self.patch("collect", lambda config, *a, **k: ([sample()], []))
        status, headers, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("version=0.0.4", headers["Content-Type"])
        self.assertIn("wht_tbm_scrape_success 1", body)

    def test_metrics_stays_200_when_the_scrape_fails(self):
        # A failing scrape must still be exposable so Prometheus records
        # scrape_success 0 rather than the target going away entirely.
        self.failing_stack()
        status, _, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("wht_tbm_scrape_success 0", body)

    def test_healthz_ok(self):
        self.patch(
            "discover",
            lambda *a, **k: {"discovered_at": 1, "page_url": "u", "dashboard_id": "d", "machines": {"a": machine()}},
        )
        self.patch("collect", lambda config, *a, **k: ([sample()], []))
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok\n")

    def test_healthz_reports_503_when_unavailable(self):
        self.failing_stack()
        try:
            with urllib.request.urlopen(self.base + "/healthz", timeout=10) as response:
                status, body = response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode()
        self.assertEqual(status, 503)
        self.assertTrue(body.startswith("unavailable:"))

    def test_unknown_path_404s(self):
        try:
            with urllib.request.urlopen(self.base + "/nope", timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = os.path.join(self.dir.name, "config.json")

    def patch(self, name, replacement):
        original = getattr(exporter, name)
        setattr(exporter, name, replacement)
        self.addCleanup(lambda: setattr(exporter, name, original))

    def run_once(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = exporter.main(
                ["--once", "--cache-file", self.cache, "--page-url", "https://caportal.test/x"]
            )
        return code, stdout.getvalue()

    @staticmethod
    def only_map_widgets(dashboard: dict) -> dict:
        """Strip every gauge/indicator, so the synthetic ones below are the only
        source of targets and field names."""
        for view in ("desktopView", "mobileView"):
            widgets = (dashboard.get(view) or {}).get("widgets") or []
            (dashboard[view])["widgets"] = [w for w in widgets if w.get("type") == "mapWidget"]
        return dashboard

    @staticmethod
    def add_bare_gauge(dashboard: dict, layer_id: str) -> dict:
        """A gauge with a main dataset but no target and no value field."""
        dashboard["desktopView"]["widgets"].append(
            {
                "type": "gaugeWidget",
                "datasets": [{"name": "main", "dataSource": {"layerId": layer_id}}],
            }
        )
        return dashboard

    def good_stack(self):
        self.patch(
            "discover",
            lambda *a, **k: {
                "discovered_at": 1,
                "page_url": "u",
                "dashboard_id": DASHBOARD_ID,
                "machines": {"barangaroo": machine()},
            },
        )
        self.patch("collect", lambda config, *a, **k: ([sample()], []))

    def test_once_exits_zero_on_success(self):
        self.good_stack()
        code, body = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn("wht_tbm_scrape_success 1", body)

    def test_once_exits_nonzero_on_failure(self):
        # So `docker run` and cron wrappers can detect a failed one-shot scrape.
        self.patch("discover", _raiser(ScrapeError("portal is down")))
        code, body = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("wht_tbm_scrape_success 0", body)

    def test_default_cache_path_is_under_the_home_directory(self):
        # Guards the surprise where --cache-file is omitted and the container's
        # HOME is not writable, silently turning the cache into a no-op.
        import argparse

        parser_defaults = {}
        original = argparse.ArgumentParser.parse_args

        def capture(self, *a, **k):
            for action in self._actions:
                if action.dest == "cache_file":
                    parser_defaults["cache_file"] = action.default
            return original(self, *a, **k)

        argparse.ArgumentParser.parse_args = capture
        self.addCleanup(lambda: setattr(argparse.ArgumentParser, "parse_args", original))
        self.good_stack()
        self.run_once()
        self.assertTrue(parser_defaults["cache_file"].endswith(os.path.join(".cache", "wht-tbm", "config.json")))


class MainServeTest(unittest.TestCase):
    """The mode the container actually runs: a long-lived HTTP server.

    Covered here rather than in HandlerTest because this drives the real
    main() argument plumbing, ThreadingHTTPServer construction and the
    serve_forever/shutdown lifecycle. `--port 0` binds an ephemeral port,
    which is then read back off the server object, so there is no
    bind-a-free-port-then-race-for-it flake.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = os.path.join(self.dir.name, "config.json")
        self.servers: list = []
        captured = self.servers

        real = exporter.ThreadingHTTPServer

        class Capturing(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured.append(self)

        exporter.ThreadingHTTPServer = Capturing
        self.addCleanup(lambda: setattr(exporter, "ThreadingHTTPServer", real))

    def patch(self, name, replacement):
        original = getattr(exporter, name)
        setattr(exporter, name, replacement)
        self.addCleanup(lambda: setattr(exporter, name, original))

    def good_stack(self):
        self.patch(
            "discover",
            lambda *a, **k: {
                "discovered_at": 1,
                "page_url": "u",
                "dashboard_id": DASHBOARD_ID,
                "machines": {"barangaroo": machine()},
            },
        )
        self.patch("collect", lambda config, *a, **k: ([sample()], []))

    def launch(self):
        """Start main() in a thread; return (port, done, result-dict)."""
        result: dict = {}
        done = threading.Event()

        def run():
            try:
                result["code"] = exporter.main(
                    [
                        "--port", "0",
                        "--listen-address", "127.0.0.1",
                        "--cache-file", self.cache,
                        "--page-url", "https://caportal.test/x",
                    ]
                )
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (thread.join(timeout=2), [s.shutdown() for s in self.servers]))

        deadline = time.time() + 5
        while not self.servers and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.servers, "main() never started a server")
        return self.servers[-1].server_address[1], thread, result, done

    def test_serves_metrics_and_exits_cleanly_on_shutdown(self):
        self.good_stack()
        port, thread, result, done = self.launch()
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
        self.assertIn("wht_tbm_scrape_success 1", body)

        # Shutting the server down must let main() return 0.
        self.servers[-1].shutdown()
        self.assertTrue(done.wait(timeout=5), "main() did not return after shutdown")
        thread.join(timeout=2)
        self.assertEqual(result["code"], 0)

    def test_keyboard_interrupt_shuts_down_cleanly(self):
        # Ctrl-C / docker stop path: the finally block must still close the
        # socket and main() must return 0 rather than propagating.
        self.good_stack()
        result: dict = {}
        thread = None
        original_init = exporter.ThreadingHTTPServer

        def make_server(*args, **kwargs):
            server = original_init(*args, **kwargs)
            self.servers.append(server)
            server.serve_forever = _raiser(KeyboardInterrupt)
            return server

        self.patch("ThreadingHTTPServer", make_server)

        def run():
            result["code"] = exporter.main(
                ["--port", "0", "--listen-address", "127.0.0.1", "--cache-file", self.cache]
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "main() hung on KeyboardInterrupt")
        self.assertEqual(result["code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
