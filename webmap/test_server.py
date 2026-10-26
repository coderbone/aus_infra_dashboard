"""Tests for the web map's live payload builder.

The seam is `server.build_live()` with `_query_prometheus` stubbed: it is the
public boundary the front end consumes, so it is where the payload's contract
is observable without a running Prometheus. What it cannot see - whether
Prometheus serves the metrics at all, and how the front end turns a number into
a colour - is verified against the live stack instead.

Run with the second discovery root, from the repo root:

    python3 -m unittest discover -s webmap -t webmap
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scrapers"))  # server.py imports `fleet`
sys.path.insert(0, HERE)

import server  # noqa: E402


# Two links chosen because their fixtures are the ticket's own worked example:
# VIC1-NSW1 carries 690.8 MW against a 1700 MW forward rating (about 41%), and
# T-V-MNSP1 flows the other way with a negative export limit - the case the old
# denominator got wrong.
VICTORIA_NSW = "VIC1-NSW1"
BASSLINK = "T-V-MNSP1"
UNRATED = "NEWLINK"  # AEMO reports a link the nominal table does not know
MISSING = "MISSING"  # the exporter writes NaN for a reading it does not have

# query -> {interconnector: value}. Queries absent here return no series, which
# is what an exporter that has not published them would look like.
SERIES = {
    "aemo_interconnector_flow_mw": {
        VICTORIA_NSW: 690.8, BASSLINK: -33.1, UNRATED: 100.0, MISSING: "NaN",
    },
    # Deliberately pathological: VIC1-NSW1's export limit equals its flow, so
    # the old `flow / max(export, import)` denominator read exactly 100%.
    "aemo_interconnector_export_limit_mw": {VICTORIA_NSW: 690.8, BASSLINK: -185.0},
    "aemo_interconnector_import_limit_mw": {VICTORIA_NSW: -595.4, BASSLINK: -220.0},
    "aemo_interconnector_nominal_forward_mw": {VICTORIA_NSW: 1700.0, BASSLINK: 594.0, MISSING: 1000.0},
    "aemo_interconnector_nominal_reverse_mw": {VICTORIA_NSW: 1450.0, BASSLINK: 478.0, MISSING: 1000.0},
}


def fake_query(query: str, timeout: float = 10.0, at=None):
    return [
        {"metric": {"interconnector": ic}, "value": [1700000000.0, str(value)]}
        for ic, value in SERIES.get(query, {}).items()
    ]


class BuildLiveTest(unittest.TestCase):
    def build(self) -> dict:
        with mock.patch.object(server, "_query_prometheus", fake_query):
            return server.build_live()

    def test_nominal_ratings_reach_the_front_end(self):
        """The front end must get the ratings through the live feed, so that
        nothing in the browser has to hard-code a link's capacity."""
        live = self.build()
        vic = live["interconnectors"][VICTORIA_NSW]
        self.assertEqual(vic["flow"], 690.8)
        self.assertEqual(vic["nominal_forward"], 1700.0)
        self.assertEqual(vic["nominal_reverse"], 1450.0)

    def test_utilisation_is_flow_over_the_nominal_for_the_direction_of_travel(self):
        """Each direction has its own nominal, and the flow's sign says which.

        690.8 MW of a 1700 MW forward rating is the ticket's ~41%. The fixture
        sets VIC1-NSW1's export limit equal to its flow and its import limit
        negative, so the old `flow / max(|export|, |import|)` denominator read
        exactly 100% - this assertion fails if anything still divides by the
        dynamic limits.
        """
        live = self.build()
        vic = live["interconnectors"][VICTORIA_NSW]
        self.assertAlmostEqual(vic["utilisation"], 690.8 / 1700.0, places=6)
        self.assertNotAlmostEqual(vic["utilisation"], 1.0, places=3)

        # Flowing the other way: the reverse rating is the denominator.
        basslink = live["interconnectors"][BASSLINK]
        self.assertAlmostEqual(basslink["utilisation"], 33.1 / 478.0, places=6)

    def test_a_link_with_flow_but_no_nominal_has_no_utilisation(self):
        """AEMO can report a link the nominal table does not know.

        It has a flow but no denominator, so it must read as unknown rather
        than as infinitely loaded - and rather than falling back to the
        dynamic limits, which is how this ended up wrong in the first place.
        """
        live = self.build()
        unrated = live["interconnectors"][UNRATED]
        self.assertEqual(unrated["flow"], 100.0)
        self.assertNotIn("nominal_forward", unrated)
        self.assertIsNone(unrated["utilisation"])

    def test_a_missing_reading_has_no_utilisation(self):
        """The exporter writes NaN for a reading it does not have.

        `float("NaN")` is a float that satisfies neither `< 0` nor `<= 0`, so
        it would otherwise pass every guard and be returned as the answer -
        which `json.dumps` then emits as a bare NaN that the browser rejects.
        """
        live = self.build()
        self.assertTrue(math.isnan(live["interconnectors"][MISSING]["flow"]))
        self.assertIsNone(live["interconnectors"][MISSING]["utilisation"])


if __name__ == "__main__":
    unittest.main()
