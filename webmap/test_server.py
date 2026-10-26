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

import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scrapers"))  # server.py imports `fleet`
sys.path.insert(0, HERE)

import links  # noqa: E402
import server  # noqa: E402
import soc_policy  # noqa: E402


# Two links chosen because their fixtures are the ticket's own worked example:
# VIC1-NSW1 carries 690.8 MW against a 1700 MW forward rating (about 41%), and
# T-V-MNSP1 flows the other way with a negative export limit - the case the old
# denominator got wrong.
VICTORIA_NSW = "VIC1-NSW1"
BASSLINK = "T-V-MNSP1"
UNRATED = "NEWLINK"  # AEMO reports a link the link registry does not know
MISSING = "MISSING"  # the exporter writes NaN for a reading it does not have

# The links whose nominal ratings come from the registry in these fixtures -
# the exporter publishes exactly those numbers.
RATED_IDS = (VICTORIA_NSW, BASSLINK)

# query -> {interconnector: value}. Queries absent here return no series, which
# is what an exporter that has not published them would look like. The nominal
# series are built from the registry - the exporter publishes exactly those
# numbers - so the ratings exist in one place; the other values are synthetic
# Prometheus output, not link facts.
SERIES = {
    "aemo_interconnector_flow_mw": {
        VICTORIA_NSW: 690.8, BASSLINK: -33.1, UNRATED: 100.0, MISSING: "NaN",
    },
    # Deliberately pathological: VIC1-NSW1's export limit equals its flow, so
    # the old `flow / max(export, import)` denominator read exactly 100%.
    "aemo_interconnector_export_limit_mw": {VICTORIA_NSW: 690.8, BASSLINK: -185.0},
    "aemo_interconnector_import_limit_mw": {VICTORIA_NSW: -595.4, BASSLINK: -220.0},
    "aemo_interconnector_nominal_forward_mw": {
        **{ic: links.LINKS[ic].nominal_forward_mw for ic in RATED_IDS},
        # Synthetic: a denominator for a link with no registry entry, so the
        # only reason its utilisation can be None is its NaN flow.
        MISSING: 1000.0,
    },
    "aemo_interconnector_nominal_reverse_mw": {
        **{ic: links.LINKS[ic].nominal_reverse_mw for ic in RATED_IDS},
        MISSING: 1000.0,
    },
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
        nothing in the browser has to hard-code a link's nominal rating. The
        values travel registry -> Prometheus -> payload; this pins the
        plumbing, and the exporter's tests pin the numbers themselves."""
        live = self.build()
        vic = live["interconnectors"][VICTORIA_NSW]
        self.assertEqual(vic["flow"], 690.8)
        self.assertEqual(vic["nominal_forward"], links.LINKS[VICTORIA_NSW].nominal_forward_mw)
        self.assertEqual(vic["nominal_reverse"], links.LINKS[VICTORIA_NSW].nominal_reverse_mw)

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
        """AEMO can report a link the link registry does not know.

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


class SoCPolicyTest(unittest.TestCase):
    """The SoC policy travels to the consumers that cannot import it.

    The front end is JavaScript: it cannot read `scrapers/soc_policy.py`, so
    the ramp has to arrive with the live payload - the same plumbing the
    nominal ratings take (registry -> Prometheus -> payload), minus the
    Prometheus hop. Nothing in the browser may hard-code a stop.
    """

    def build(self) -> dict:
        with mock.patch.object(server, "_query_prometheus", fake_query):
            return server.build_live()

    def test_the_soc_ramp_reaches_the_front_end_from_the_policy_module(self):
        live = self.build()
        ramp = live.get("soc_ramp")
        self.assertIsNotNone(ramp, "the live payload must carry the SoC ramp")
        # The server adapts the module's percent stops to the front end's
        # 0..1 scale; the colours must be the module's own values, and the
        # scale must be what `ramp(soc, stops)` interpolates.
        expected = [
            {"v": value / 100.0, "c": list(rgb)}
            for value, rgb in soc_policy.SOC_RAMP
        ]
        self.assertEqual(ramp, expected)
        self.assertEqual(ramp[0]["v"], 0.0)
        self.assertEqual(ramp[-1]["v"], 1.0)

    def test_no_prometheus_capacity_series_reaches_the_payload(self):
        """The SoC denominator is the fleet module's, and the only one.

        The payload used to carry a second cache-derived capacity - the
        `oe_battery_capacity_storage_mwh` series the OE exporter publishes
        from the same fleet.json - beside the fleet module's `capacity_mwh`.
        The front end never divided by it; it existed only as a second reading
        of the cache that a future change could pick up by mistake.
        """
        queried = []

        def recording_query(query, timeout=10.0, at=None):
            queried.append(query)
            return fake_query(query, timeout, at)

        with mock.patch.object(server, "_query_prometheus", recording_query):
            live = server.build_live()
        self.assertFalse(
            [q for q in queried if "capacity" in q],
            "capacity must not be queried from Prometheus; the fleet module "
            "supplies it",
        )
        for unit_metrics in live.get("batteries", {}).values():
            self.assertNotIn("capacity_mwh", unit_metrics)

    def test_the_denominator_in_the_payload_is_the_fleet_modules(self):
        """A fleet cache's registered storage capacity is what the map divides by.

        Pinned end to end: the cache says 1997 MWh (ERB01's real registered
        figure), the payload's fleet entry carries it, and no other capacity
        for that unit exists in the payload to divide by instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "fleet.json")
            with open(cache, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "cached_at": 1.0,
                        "data": [
                            {
                                "code": "ERB",
                                "name": "Eraring BESS",
                                "network_region": "NSW1",
                                "units": [
                                    {
                                        "code": "ERB01",
                                        "status_id": "operating",
                                        "fueltech_id": "battery",
                                        "capacity_storage": 1997.0,
                                    }
                                ],
                            }
                        ],
                    },
                    handle,
                )
            old_cache = os.environ.get("FLEET_CACHE")
            old_memo = dict(server._fleet_cache)
            os.environ["FLEET_CACHE"] = cache
            server._fleet_cache.update(mtime=None, data=None)
            try:
                with mock.patch.object(server, "_query_prometheus", fake_query):
                    live = server.build_live()
            finally:
                server._fleet_cache.update(old_memo)
                if old_cache is None:
                    os.environ.pop("FLEET_CACHE", None)
                else:
                    os.environ["FLEET_CACHE"] = old_cache

        batteries = [
            unit
            for fac in live["fleet"]
            for unit in fac["units"]
            if unit["code"] == "ERB01"
        ]
        self.assertEqual(len(batteries), 1)
        self.assertEqual(batteries[0]["capacity_mwh"], 1997.0)


if __name__ == "__main__":
    unittest.main()
