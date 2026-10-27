#!/usr/bin/env python3
"""Tests for `http_client` - the one retrying HTTP client.

The seam under test is the *single definition* property: each exporter's
`ScrapeError`, `http_get`, `is_transient` and `describe_http_error` are this
module's names by identity - a copy would satisfy `==` and drift, which is
exactly the bug (three byte-identical copies of the retry loop plus an
OpenElectricity variant, one of them already dropping the error body). The
retry behaviour itself is exercised through each exporter's suite, at the
mock seams those suites already own; what is pinned here is that every one of
those seams lands on this one definition, and the request defaults a caller
gets by passing no headers.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_http_client.py
"""

from __future__ import annotations

import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aemo_battery_exporter as aemo  # noqa: E402
import http_client  # noqa: E402
import oe_battery_exporter as oe  # noqa: E402
import snowy_tantangara_exporter as snowy  # noqa: E402
import wht_tbm_exporter as wht  # noqa: E402

# The four names the shared client owns. The exporter suites reach all four
# through their own module namespace, so all four must be bound there - by
# identity, or a private copy can creep back in unnoticed.
SHARED_NAMES = ("ScrapeError", "http_get", "is_transient", "describe_http_error")


class _Stop(Exception):
    """Raised by the fake urlopen once the request has been captured."""


class SingleDefinitionTest(unittest.TestCase):
    def test_every_exporter_fetches_through_the_shared_client(self):
        """Identity, not equality: a re-copied helper would satisfy `==`."""
        for exporter in (aemo, snowy, wht, oe):
            for name in SHARED_NAMES:
                with self.subTest(exporter=exporter.__name__, name=name):
                    self.assertIs(getattr(exporter, name), getattr(http_client, name))

    def test_the_oe_auth_error_subclasses_the_shared_scrape_error(self):
        """OE's 401/403 classification hangs off the one scrape exception, so
        `except ScrapeError` above it still catches an auth rejection."""
        self.assertTrue(issubclass(oe.AuthError, http_client.ScrapeError))


class RequestHeadersTest(unittest.TestCase):
    def sent_headers(self, **kwargs) -> dict:
        """The headers one `http_get` call puts on the wire, lowercased."""
        captured: dict = {}

        def fake_open(request, timeout=None):
            captured.update({k.lower(): v for k, v in request.headers.items()})
            raise _Stop()

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_open
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        with self.assertRaises(_Stop):
            http_client.http_get("https://example.test/x", 1.0, **kwargs)
        return captured

    def test_the_default_request_is_the_one_three_exporters_rely_on(self):
        """WHT, Snowy and AEMO pass no headers, so the defaults are theirs.

        CloudFront 403s a non-browser User-Agent, and every parser here reads
        the bytes it is given - hence the identity encoding.
        """
        headers = self.sent_headers()
        self.assertIn("Chrome", headers["user-agent"])
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["accept-language"], "en-AU,en;q=0.9")

    def test_a_caller_override_wins_and_keeps_the_rest(self):
        """OE sends a truthful UA, a JSON Accept and a bearer token; the
        shared defaults must not shadow them or lose the identity encoding."""
        headers = self.sent_headers(
            headers={
                "Authorization": "Bearer k",
                "User-Agent": "aus-infra-dashboard/1.0 (+test)",
                "Accept": "application/json",
            }
        )
        self.assertEqual(headers["authorization"], "Bearer k")
        self.assertEqual(headers["user-agent"], "aus-infra-dashboard/1.0 (+test)")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["accept-encoding"], "identity")


if __name__ == "__main__":
    unittest.main()
