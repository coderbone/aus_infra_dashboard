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
import subprocess
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

# The payload's top-level keys as the front end's applySnapshot is written
# against them (webmap/static/app.js): the groups it copies into state plus
# the envelope around them. Its own list rather than a read of the server's
# declaration, because the point is to check the two against each other - a
# group dropped from one and not the other has to fail here. The historical
# `at` is not in it because it is optional; the declaration checks that one.
FRONT_END_KEYS = (
    "fetched_at", "prometheus", "errors", "interconnectors", "regions",
    "batteries", "units", "reservoirs", "soc_ramp",
    "fleet", "renewables", "hydro", "coal", "gas", "distillate",
)

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


def write_cache(path, payload) -> str:
    """Write `payload` as a fleet cache file and return the path."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def write_battery_cache(path, capacity_mwh=1997.0) -> str:
    """Write a one-facility fleet cache holding ERB01, and return its path.

    1997 MWh is the unit's real registered storage - the figure the SoC
    denominator is pinned against - and `path` is the seam the fleet metadata
    reader takes, so a test points at its own file instead of editing the
    environment the process was started with.
    """
    return write_cache(
        path,
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
                            "capacity_storage": capacity_mwh,
                        }
                    ],
                }
            ],
        },
    )


# --- the declared live payload contract, and the walk that pins it ----------


def complete_series() -> dict:
    """A canned series for every metric the query table fields.

    One finite reading under one key per group, so a built payload carries
    every field the contract declares and a rename cannot hide behind a
    reading that never arrived. Built from the query table for coverage, but
    asserted against the contract below rather than against the table - the
    two are independent, which is what makes a rename fail here.
    """
    key_of = {
        "interconnector": VICTORIA_NSW,
        "region": "NSW1",
        "unit": "ERB01",
        "reservoir": "TANTANGARA",
    }
    series = {}
    for fields in server.LIVE_QUERIES.values():
        for spec in fields.values():
            key = key_of[spec["key"]]
            if spec.get("extra"):
                # A multi-valued field (curtailment per fuel) is nested.
                series[spec["metric"]] = {key: {"wind": 1.0, "solar": 2.0}}
            else:
                series[spec["metric"]] = {key: 1.0}
    return series


# One facility per drawn fleet group, carrying the fueltech spellings that
# group's markers are drawn from: a battery, a wind + solar site (the front
# end's `wind_farm` arrives as `wind`, normalised here), a pumped scheme with
# its sibling pumps unit, and one plant per fossil category. Every one has a
# coordinate, because the point sources are dropped without one.
SHAPE_FLEET = {
    "cached_at": 1.0,
    "data": [
        {
            "code": "ERB",
            "name": "Eraring BESS",
            "network_region": "NSW1",
            "location": {"lat": -32.88, "lng": 151.55},
            "units": [
                {
                    "code": "ERB01",
                    "status_id": "operating",
                    "fueltech_id": "battery",
                    "capacity_storage": 1997.0,
                    "capacity_registered": 100.0,
                }
            ],
        },
        {
            "code": "SWF",
            "name": "Snowy Wind Farm",
            "network_region": "NSW1",
            "location": {"lat": -36.0, "lng": 148.0},
            "units": [
                {
                    "code": "SWFW1",
                    "status_id": "operating",
                    "fueltech_id": "wind_farm",
                    "capacity_registered": 100.0,
                },
                {
                    "code": "SWFS1",
                    "status_id": "operating",
                    "fueltech_id": "solar_utility",
                    "capacity_registered": 50.0,
                },
                {
                    "code": "SWFS2",
                    "status_id": "operating",
                    "fueltech_id": "solar",
                    "capacity_registered": 25.0,
                },
            ],
        },
        {
            "code": "TUM3",
            "name": "Tumut 3",
            "network_region": "NSW1",
            "description": "Pumped Storage Scheme",
            "location": {"lat": -35.4, "lng": 148.2},
            "units": [
                {
                    "code": "TUM31",
                    "status_id": "operating",
                    "fueltech_id": "hydro",
                    "capacity_registered": 600.0,
                    "capacity_storage": 2000.0,
                },
                {
                    "code": "TUM3P",
                    "status_id": "operating",
                    "fueltech_id": "pumps",
                    "capacity_registered": 600.0,
                },
            ],
        },
        {
            "code": "CLB",
            "name": "Coal B",
            "network_region": "NSW1",
            "location": {"lat": -32.0, "lng": 151.0},
            "units": [
                {
                    "code": "CLB01",
                    "status_id": "operating",
                    "fueltech_id": "coal_black",
                    "capacity_registered": 500.0,
                }
            ],
        },
        {
            "code": "GCC",
            "name": "Gas CCGT",
            "network_region": "NSW1",
            "location": {"lat": -33.0, "lng": 150.0},
            "units": [
                {
                    "code": "GCC01",
                    "status_id": "operating",
                    "fueltech_id": "gas_ccgt",
                    "capacity_registered": 300.0,
                }
            ],
        },
        {
            "code": "DST",
            "name": "Distillate A",
            "network_region": "QLD1",
            "location": {"lat": -27.0, "lng": 153.0},
            "units": [
                {
                    "code": "DST01",
                    "status_id": "operating",
                    "fueltech_id": "distillate",
                    "capacity_registered": 50.0,
                }
            ],
        },
    ],
}


def walk_shape(test, payload, shape, path):
    """Check `payload` against the declared `shape`, key by key, both ways.

    Reads the shape language documented at `server.LIVE_PAYLOAD` - the
    declaration owns that description, this is only its interpreter. Every
    failure names the path it failed at, so a rename says which key moved
    rather than only that something did.
    """
    if isinstance(shape, dict):
        test.assertIsInstance(payload, dict, path)
        wildcard = shape.get("*")
        optional = set()
        for key, sub in shape.items():
            if key == "*":
                continue
            if key.startswith("?"):
                # Declared optional: absent is fine, present must still match.
                optional.add(key[1:])
                key = key[1:]
                if key not in payload:
                    continue
            else:
                test.assertIn(key, payload, f"{path}.{key} is missing")
            walk_shape(test, payload[key], sub, f"{path}.{key}")
        for key, value in payload.items():
            if key in shape or key in optional:
                continue
            test.assertIsNotNone(
                wildcard, f"{path}.{key} is not in the declared shape"
            )
            walk_shape(test, value, wildcard, f"{path}[{key}]")
        if wildcard is not None:
            test.assertTrue(payload, f"{path} is empty")
    elif isinstance(shape, list):
        test.assertIsInstance(payload, (list, tuple), path)
        if len(shape) != 1:
            raise AssertionError("a sequence shape is exactly one element shape")
        for index, item in enumerate(payload):
            walk_shape(test, item, shape[0], f"{path}[{index}]")
    elif isinstance(shape, frozenset):
        test.assertIn(payload, shape, f"{path} is not a declared spelling")
    # `...`: presence is the assertion, already made by the caller's key check.


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

    def test_an_empty_source_still_answers_with_every_group_the_front_end_reads(self):
        """Shape pin: whatever the source, the payload answers with every
        group the front end reads - an assembly that dropped one would blank
        a whole layer of the map. Two oracles, checked against each other:
        the front end's own list above, and the server's declaration."""
        live = server.build_live(source=memory({}))
        for key in FRONT_END_KEYS:
            self.assertIn(key, live, key)
        declared = {
            key for key in server.LIVE_PAYLOAD if not key.startswith("?")
        }
        self.assertEqual(declared, set(FRONT_END_KEYS))
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


class PayloadContractTest(unittest.TestCase):
    """The /data/live contract, declared once and walked key by key.

    The front end reads these names in JavaScript and cannot be tested here:
    a field renamed in the assembler blanks the layer that read it while every
    other test stays green, because nothing else knows the names. So the
    server declares them - every group, every field inside a group, and the
    fueltech spellings its markers are drawn from - and the built payload is
    walked against that declaration in both directions: a declared key that
    never arrives fails, and a key the payload carries that is not declared
    fails.
    """

    @staticmethod
    def build(at: float | None = None) -> dict:
        """A payload with every declared field populated.

        Every metric the query table fields carries a finite reading, and the
        fleet cache draws one facility per fleet group, so no declared field
        is absent for want of a reading and a rename has nowhere to hide.
        `at` asks for the historical shape, which is the only one carrying
        that key.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cache(os.path.join(tmp, "fleet.json"), SHAPE_FLEET)
            return server.build_live(
                at=at,
                source=memory(complete_series()),
                fleet_path=path,
            )

    def assert_contract(self, live: dict) -> None:
        walk_shape(self, live, server.LIVE_PAYLOAD, "$")
        # The walk descends into a group's members, so a group that came back
        # empty would check nothing at all and let a rename through. The map
        # groups are covered by the walk's own non-empty rule for a dynamic
        # map; the fleet groups are lists, which a sequence shape cannot
        # distinguish from an empty one.
        for group in ("fleet", "renewables", "hydro", "coal", "gas", "distillate"):
            self.assertTrue(live[group], group)
        # The envelope's values, which a shape cannot state: a healthy build
        # says so and reports no group errors.
        self.assertEqual(live["prometheus"], "ok")
        self.assertEqual(live["errors"], [])

    def test_the_built_payload_matches_the_declared_contract_key_by_key(self):
        """Both shapes the contract describes: the default live payload, and
        the historical snapshot that adds `at` - declared optional, so that
        one key has to be right either way round."""
        default = self.build()
        self.assert_contract(default)
        self.assertNotIn("at", default)
        historical = self.build(at=1700000000.0)
        self.assert_contract(historical)
        self.assertEqual(historical["at"], 1700000000.0)


MODEL_JS = os.path.join(HERE, "static", "model.js")


def resolve_shape(shape, path: str):
    """Resolve a dotted model-read path against a LIVE_PAYLOAD shape.

    The path language is the one model.js declares in PAYLOAD_FIELDS: `*`
    steps into a wildcard member (a region/unit map) or a facility list's
    element shape. Returns the sub-shape at that path, or None when the
    contract does not declare it - which is the failure this exists to catch.
    """
    current = shape
    for seg in path.split("."):
        if isinstance(current, dict):
            if seg == "*":
                if "*" not in current:
                    return None
                current = current["*"]
            elif seg in current:
                current = current[seg]
            elif "?" + seg in current:
                current = current["?" + seg]
            else:
                return None
        elif isinstance(current, list):
            if seg != "*" or not current:
                return None
            current = current[0]
        else:
            return None
    return current


def model_payload_fields() -> list[str]:
    """The model layer's declared payload reads, read from model.js itself.

    Node executes the module and returns PAYLOAD_FIELDS, so the list lives in
    exactly one place - beside the code that does the reading - and this test
    cannot drift from it by being a second hand-maintained copy.
    """
    out = subprocess.check_output(
        ["node", "-e",
         "console.log(JSON.stringify(require(process.argv[1]).PAYLOAD_FIELDS))",
         MODEL_JS],
        text=True,
    )
    return json.loads(out)


class ModelPayloadContractTest(unittest.TestCase):
    """The model layer's payload reads, pinned to the server's contract.

    The map's model (webmap/static/model.js) reads the /data/live payload
    through names it declares in PAYLOAD_FIELDS. PayloadContractTest walks a
    built payload against the server's declaration; this test walks the
    model's declaration against the same server contract, so a field renamed
    on either side of the browser seam fails here instead of blanking a map
    layer in production. It needs Node (the model is JavaScript); the rest of
    the webmap suite does not.
    """

    def test_every_field_the_model_reads_is_declared_by_the_contract(self):
        for path in model_payload_fields():
            self.assertIsNotNone(
                resolve_shape(server.LIVE_PAYLOAD, path),
                f"the model reads {path}, which server.LIVE_PAYLOAD does not declare",
            )

    def test_the_model_declaration_is_not_empty(self):
        """A broken require would hand back nothing, and an empty pin walks
        over an empty list without noticing."""
        self.assertTrue(model_payload_fields())


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
            cache = write_battery_cache(os.path.join(tmp, "fleet.json"))
            live = server.build_live(source=memory(), fleet_path=cache)

        batteries = [
            unit
            for fac in live["fleet"]
            for unit in fac["units"]
            if unit["code"] == "ERB01"
        ]
        self.assertEqual(len(batteries), 1)
        self.assertEqual(batteries[0]["capacity_mwh"], 1997.0)


class FleetMetadataPathTest(unittest.TestCase):
    """The fleet metadata reader takes its file path; the environment is only
    the default.

    The deployment says where the volume is mounted by environment, and that
    is the whole job of that variable. A caller that already knows which file
    it wants passes the path, so pointing a test at another cache costs no
    save/restore of the process's environment and no reset of the module's
    memo - the two files are memoised apart, keyed by the path they came from.
    """

    @staticmethod
    def codes(drawn) -> list:
        return [unit["code"] for fac in drawn["fleet"] for unit in fac["units"]]

    def test_the_environment_is_the_default_when_no_path_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = write_battery_cache(os.path.join(tmp, "fleet.json"))
            with mock.patch.dict(os.environ, {"FLEET_CACHE": cache}):
                self.assertEqual(self.codes(server.build_fleet()), ["ERB01"])
            # A mount that is not there degrades to no fleet, as it always has.
            with mock.patch.dict(
                os.environ, {"FLEET_CACHE": os.path.join(tmp, "unmounted.json")}
            ):
                self.assertEqual(server.build_fleet(), [])

    def test_an_explicit_path_is_read_as_given_and_wins_over_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = write_battery_cache(os.path.join(tmp, "fleet.json"))
            unmounted = os.path.join(tmp, "unmounted.json")
            with mock.patch.dict(os.environ, {"FLEET_CACHE": unmounted}):
                # The environment names a file that is not there; the explicit
                # path answers anyway, and the two do not share a memo entry.
                self.assertEqual(self.codes(server.build_fleet(cache)), ["ERB01"])
                self.assertEqual(server.build_fleet(), [])


if __name__ == "__main__":
    unittest.main()
