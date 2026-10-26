"""Tests for the web map's live payload builder.

The seam is `server.build_live()` fed by a snapshot source. Two adapters sit
behind it: `server.MemorySource`, an in-memory one carrying canned series in
Prometheus wire shape, which every assembly test below uses; and
`server.PrometheusSource`, the production one, pinned through the
`_query_prometheus` HTTP boundary - one batched instant query per group, no
capacity metric, and a failed batch that degrades without sinking the rest.

What these tests cannot see - whether Prometheus serves the metrics at all,
and how the front end turns a number into a colour - is verified against the
live stack instead.

Run with the second discovery root, from the repo root:

    python3 -m unittest discover -s webmap -t webmap
"""

from __future__ import annotations

import json
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

# metric -> {key: value}, the in-memory adapter's canned series. Queries absent
# here return no series, which is what an exporter that has not published them
# would look like. The nominal series are built from the registry - the
# exporter publishes exactly those numbers - so the ratings exist in one place;
# the other values are synthetic Prometheus output, not link facts.
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


def memory(series=None, fail=()) -> server.MemorySource:
    return server.MemorySource(series if series is not None else SERIES, fail=fail)


class BuildLiveTest(unittest.TestCase):
    def build(self, source=None) -> dict:
        return server.build_live(source=source or memory())

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

    def test_a_non_finite_historical_time_does_not_poison_the_payload(self):
        """`?t=` is part of /data/live too.

        The historical snapshot timestamp is echoed into the payload, so a
        non-finite `t` would reach `json.dumps` the same way a NaN reading
        did - as a bare NaN or Infinity the browser rejects.
        """
        for at in (float("nan"), float("inf"), float("-inf")):
            live = server.build_live(at=at, source=memory())
            self.assertNotIn("at", live)
            json.dumps(live, allow_nan=False)

    def test_a_missing_reading_is_absent_and_has_no_utilisation(self):
        """The exporter writes NaN for a reading it does not have.

        The reading must arrive as absent, not as a number: `float("NaN")` is
        a float, so it would pass every guard and `json.dumps` would emit a
        bare NaN that the browser rejects, taking the whole payload - and the
        live feed - down with it.
        """
        live = self.build()
        missing = live["interconnectors"][MISSING]
        self.assertNotIn("flow", missing)
        # The rest of the link's readings still arrive.
        self.assertEqual(missing["nominal_forward"], 1000.0)
        self.assertIsNone(missing["utilisation"])


class SnapshotSeamTest(unittest.TestCase):
    """The assembly must not care where its readings come from, and the
    production adapter must not go back to one Prometheus round trip per
    field."""

    @staticmethod
    def _record_queries(raise_for=None):
        """Patch the HTTP boundary with a recorder of every batch expr issued.

        Returns the list `build_live` will have appended to. `raise_for` is a
        substring that makes the matching batch raise, as a dead Prometheus
        call would.
        """
        queried = []

        def recording(query, timeout=10.0, at=None):
            queried.append(query)
            if raise_for is not None and raise_for in query:
                raise RuntimeError("connection refused")
            return []

        return mock.patch.object(server, "_query_prometheus", recording), queried

    def test_the_payload_carries_every_group_the_front_end_reads(self):
        """Shape pin: whatever the source, the payload answers with every
        group `applySnapshot` copies into state - an assembly that drops one
        would blank a whole layer of the map."""
        live = server.build_live(source=memory({}))
        for key in (
            "fetched_at", "prometheus", "errors", "interconnectors", "regions",
            "batteries", "units", "reservoirs", "soc_ramp",
            "fleet", "renewables", "hydro", "coal", "gas", "distillate",
        ):
            self.assertIn(key, live, key)
        self.assertEqual(live["prometheus"], "ok")
        self.assertEqual(live["errors"], [])

    def test_one_prometheus_round_trip_per_group(self):
        """The payload used to cost one instant query per field - 27 in
        total, repeated per historical frame during 24h playback. The batch
        is one regex __name__ selector per group, so a full payload is
        `len(LIVE_QUERIES)` round trips."""
        patch, queried = self._record_queries()
        with patch:
            server.build_live()
        self.assertEqual(len(queried), len(server.LIVE_QUERIES))

    def test_the_reservoir_batch_keeps_its_lookback_window(self):
        """The reservoir job scrapes hourly, outside Prometheus's 5m default
        lookback, so its batch must stay wrapped in last_over_time - a bare
        selector would blank the card for most of every hour."""
        patch, queried = self._record_queries()
        with patch:
            server.build_live()
        reservoir_batches = [
            q for q in queried if "snowy_tantangara" in q
        ]
        self.assertEqual(len(reservoir_batches), 1)
        self.assertTrue(reservoir_batches[0].startswith("last_over_time("), reservoir_batches[0])
        self.assertIn("[2h]", reservoir_batches[0])

    def test_no_batch_queries_for_capacity(self):
        """The SoC denominator is the fleet module's, and the only one.

        The payload used to carry a second cache-derived capacity - the
        `oe_battery_capacity_storage_mwh` series the OE exporter publishes
        from the same fleet.json - beside the fleet module's `capacity_mwh`.
        The front end never divided by it; it existed only as a second reading
        of the cache that a future change could pick up by mistake. Pinned at
        the query level here; SoCPolicyTest pins the payload level.
        """
        patch, queried = self._record_queries()
        with patch:
            server.build_live()
        self.assertFalse(
            [q for q in queried if "capacity" in q],
            "capacity must not be queried from Prometheus; the fleet module "
            "supplies it",
        )

    def test_a_non_finite_reading_is_absent_and_the_payload_stays_strict_json(self):
        """A NaN or infinity from Prometheus must not reach the payload.

        This is the seam the front end consumes: if a bare NaN reaches the
        JSON, `JSON.parse` rejects the whole response and the map stops
        applying live data. The reading must be absent instead, leaving the
        link's other fields and every other group intact. The per-fuel
        curtailment map is merged down a different path, so it is covered too.
        """
        wire = [
            {"metric": {"__name__": "aemo_interconnector_flow_mw",
                        "interconnector": VICTORIA_NSW},
             "value": [1700000000.0, "NaN"]},
            {"metric": {"__name__": "aemo_interconnector_flow_mw",
                        "interconnector": BASSLINK},
             "value": [1700000000.0, "+Inf"]},
            {"metric": {"__name__": "aemo_interconnector_flow_mw",
                        "interconnector": UNRATED},
             "value": [1700000000.0, "-Inf"]},
            {"metric": {"__name__": "aemo_interconnector_nominal_forward_mw",
                        "interconnector": VICTORIA_NSW},
             "value": [1700000000.0, "1700"]},
            {"metric": {"__name__": "aemo_battery_region_curtailment_mw",
                        "region": "NSW1", "fuel": "wind"},
             "value": [1700000000.0, "NaN"]},
            {"metric": {"__name__": "aemo_battery_region_curtailment_mw",
                        "region": "NSW1", "fuel": "solar"},
             "value": [1700000000.0, "12.5"]},
        ]

        def query(text, timeout=10.0, at=None):
            return wire if "aemo_" in text else []

        with mock.patch.object(server, "_query_prometheus", query):
            live = server.build_live()

        for link in (VICTORIA_NSW, BASSLINK, UNRATED):
            # A link whose only reading was non-finite has nothing left to
            # report, so the entry itself may be absent.
            self.assertNotIn("flow", live["interconnectors"].get(link, {}), link)
        vic = live["interconnectors"][VICTORIA_NSW]
        self.assertEqual(vic["nominal_forward"], 1700.0)
        self.assertIsNone(vic["utilisation"])
        curtail = live["regions"]["NSW1"]["curtail"]
        self.assertNotIn("wind", curtail)
        self.assertEqual(curtail["solar"], 12.5)
        json.dumps(live, allow_nan=False)

    def test_a_failed_group_degrades_the_payload_without_dropping_the_rest(self):
        """One bad metric family must not sink the page: the failed group
        reads as degraded, every other group still reaches the front end."""
        series = {
            **SERIES,
            "aemo_unit_power_mw": {"ERB01": 50.0},
        }
        live = server.build_live(source=memory(series, fail={"regions"}))
        self.assertEqual(live["prometheus"], "degraded")
        self.assertTrue(any("regions" in err for err in live["errors"]), live["errors"])
        # The rest survives.
        self.assertEqual(live["interconnectors"][VICTORIA_NSW]["flow"], 690.8)
        self.assertEqual(live["units"]["ERB01"]["power_mw"], 50.0)
        self.assertEqual(live["regions"], {})

    def test_a_failed_prometheus_batch_is_caught_by_the_adapter(self):
        """The production adapter turns a raised batch into a group error
        instead of an exception escaping build_live - the HTTP route's 500
        path is for whole-build failures only."""
        patch, _ = self._record_queries(raise_for="aemo_battery_region")
        with patch:
            live = server.build_live()
        self.assertEqual(live["prometheus"], "degraded")
        self.assertTrue(any("regions" in err for err in live["errors"]), live["errors"])


class SoCPolicyTest(unittest.TestCase):
    """The SoC policy travels to the consumers that cannot import it.

    The front end is JavaScript: it cannot read `scrapers/soc_policy.py`, so
    the ramp has to arrive with the live payload - the same plumbing the
    nominal ratings take (registry -> Prometheus -> payload), minus the
    Prometheus hop. Nothing in the browser may hard-code a stop.
    """

    def build(self) -> dict:
        return server.build_live(source=memory())

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
        """The SoC denominator is the fleet module's, and the only one - and
        nothing named capacity may ride along in any battery's metrics."""
        live = self.build()
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
                live = server.build_live(source=memory())
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
