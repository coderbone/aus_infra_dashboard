"""Tests tying the map's drawn lines to the link registry.

`lines.geojson` is hand-drawn, but everything a viewer reads off a line except
its shape is a registry fact: the id keys it to the live feed, the name is what
the tooltip prints, and the coordinate order encodes the same FROM->TO
direction the registry's regions state. These tests hold the file, the served
payload and `scrapers/links.py` to each other at those three seams - the one
place a hand-syncs two copies, which has already shipped a reversed arrow.

Run with the second discovery root, from the repo root:

    python3 -m unittest discover -s webmap -t webmap
"""

from __future__ import annotations

import http.client
import http.server
import json
import math
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scrapers"))  # server.py imports `fleet` and `links`
sys.path.insert(0, HERE)

import links  # noqa: E402
import server  # noqa: E402

# The kinds app.js draws from the live payload as a link (see initGeometry /
# applyLines: anything that is not these is snow- or schematic-cased).
LINK_KINDS = ("interconnector", "mnsp")
# The kinds app.js renders without ever consulting /data/live, so a registry
# link drawn under one of these would have facts nobody could see.
STATIC_KINDS = ("schematic", "snowy")


def _raw_lines() -> dict:
    with open(os.path.join(server.DATA_DIR, "lines.geojson"), encoding="utf-8") as fh:
        return json.load(fh)


def _features(doc: dict) -> dict:
    return {f["properties"]["id"]: f for f in doc["features"]}


def _region_centroids() -> dict:
    """Rough centre of each region polygon, in degrees - enough to ask which
    end of a line is nearer."""
    with open(os.path.join(server.DATA_DIR, "regions.geojson"), encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for f in doc["features"]:
        pts = []

        def walk(node):
            if isinstance(node[0], (int, float)):
                pts.append(node)
            else:
                for child in node:
                    walk(child)

        walk(f["geometry"]["coordinates"])
        out[f["properties"]["code"]] = (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
        )
    return out


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class GeometryRegistryAgreementTest(unittest.TestCase):
    def test_every_registry_link_is_drawn_as_a_live_line(self):
        """Both halves of "the map still draws every link": the geometry
        exists, and it is drawn under a kind the map renders from the live
        payload - a registry link filed as `schematic` would exist and never
        show."""
        drawn = _features(_raw_lines())
        for id_ in links.LINKS:
            self.assertIn(id_, drawn, "no geometry drawn")
            self.assertNotIn(
                drawn[id_]["properties"].get("kind"), STATIC_KINDS,
                "%s is drawn as a kind the map never feeds live data to" % id_,
            )

    def test_every_line_drawn_as_a_link_is_in_the_registry(self):
        """The other direction: a line the map colours and arrows must have
        facts, or it renders with no name and no rating."""
        for id_, feat in _features(_raw_lines()).items():
            if feat["properties"].get("kind") in LINK_KINDS:
                self.assertIn(id_, links.LINKS)

    def test_the_file_carries_no_link_names(self):
        """The name is a registry fact, attached when the server serves the
        file - if it is also in the file, the two copies drift again."""
        for id_, feat in _features(_raw_lines()).items():
            if feat["properties"].get("kind") in LINK_KINDS:
                self.assertNotIn("name", feat["properties"], id_)


class ServedRouteTest(unittest.TestCase):
    """The names must arrive over HTTP: the front end fetches
    /data/lines.geojson, so a test that only calls load_lines() would stay
    green if the route stopped attaching them."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    def _served_lines(self) -> dict:
        status, ctype, body = self._get("/data/lines.geojson")
        self.assertEqual(status, 200)
        self.assertIn("geo+json", ctype)
        return json.loads(body)

    def test_served_lines_carry_the_registry_names(self):
        by_id = _features(self._served_lines())
        for id_, link in links.LINKS.items():
            self.assertEqual(by_id[id_]["properties"]["name"], link.name, id_)

    def test_the_displayed_name_is_the_known_one(self):
        # A literal pin independent of the registry: the exact string the map
        # showed before names moved, so a registry typo cannot pass unnoticed.
        by_id = _features(self._served_lines())
        self.assertEqual(
            by_id["NSW1-QLD1"]["properties"]["name"],
            "QNI (Queensland\u2011New South Wales)",
        )

    def test_features_the_registry_does_not_know_pass_through(self):
        """Snowy and the schematic corridors are not links; they keep whatever
        the file gave them."""
        by_id = _features(self._served_lines())
        self.assertEqual(
            by_id["SNOWY"]["properties"]["name"],
            "Snowy 2.0 (Tantangara\u2011Talbingo)",
        )
        self.assertEqual(
            by_id["nsw-coast"]["properties"]["name"],
            "NSW coastal trunk",
        )

    def test_an_unknown_data_path_still_gets_the_generic_answer(self):
        """The registry branch must not change any other /data/* answer."""
        status, _, _ = self._get("/data/no-such-file.geojson")
        self.assertEqual(status, 404)


class GeometryDirectionTest(unittest.TestCase):
    def test_each_line_starts_at_its_from_region_and_ends_at_its_to_region(self):
        """Coordinate order is the map's copy of the registry's direction: the
        flow arrow's bearing reads it, and getting it wrong shipped once."""
        centroids = _region_centroids()
        drawn = _features(_raw_lines())
        for id_, link in links.LINKS.items():
            coords = drawn[id_]["geometry"]["coordinates"]
            start, end = coords[0], coords[-1]
            frm = centroids[link.from_region]
            to = centroids[link.to_region]
            self.assertLess(
                _dist(start, frm), _dist(end, frm),
                "%s: does not start at %s" % (id_, link.from_region),
            )
            self.assertLess(
                _dist(end, to), _dist(start, to),
                "%s: does not end at %s" % (id_, link.to_region),
            )


if __name__ == "__main__":
    unittest.main()
