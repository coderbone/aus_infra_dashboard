#!/usr/bin/env python3
"""Tests for `fleet` - the module that owns the shared fleet cache.

The fleet cache is a shared interface: the OpenElectricity exporter writes it,
the AEMO exporter and the web map read it. Before this module those three
programs each parsed it, so the rules about it lived in prose in
scrapers/README.md and had already drifted apart. They live here now:

  * a unit code names one unit - a duplicated code is one unit reported twice,
    resolved to the most advanced status;
  * every unit has a category derived from its fueltech, and the two "does this
    count" questions (is it fossil, does its capacity count) are answered here;
  * an AEMO dispatch unit id and an OpenElectricity unit code are different
    identifiers that are usually equal - the alias table covers when they are
    not.

The module's interface is `write_fleet` / `load`, so the tests round-trip
through a real file rather than reaching into a private parser.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet  # noqa: E402
import oe_battery_exporter as exporter  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FUELTECH_CATEGORIES = {
    "battery": "battery",
    "battery_charging": "battery",
    "battery_discharging": "battery",
    "solar": "solar",
    "solar_utility": "solar",
    "wind": "wind",
    "wind_farm": "wind",
    "hydro": "hydro",
    "pumps": "pumped hydro",
    "coal_black": "coal",
    "coal_brown": "coal",
    "gas_ccgt": "gas",
    "gas_ocgt": "gas",
    "gas_steam": "gas",
    "gas_recip": "gas",
    "gas_wcmg": "gas",
    "distillate": "distillate",
    "bioenergy_biogas": "bioenergy",
    "bioenergy_biomass": "bioenergy",
}


def fixture_facilities(name: str = "oe_facilities_battery.json") -> list:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)["data"]


def load_facilities(facilities: list) -> fleet.Fleet:
    """Round-trip a facility list through the module's real interface."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fleet.json")
        fleet.write_fleet(path, facilities)
        loaded = fleet.load(path)
    if loaded is None:
        raise AssertionError("fixture did not load")
    return loaded


def unit(**overrides) -> fleet.Unit:
    """A unit with sensible defaults; override only what the test is about."""
    fields = dict(
        code="TEST1",
        facility="TEST",
        facility_name="Test Facility",
        region="NSW1",
        network="NEM",
        status="operating",
        fueltech="battery",
        capacity_storage_mwh=100.0,
        capacity_registered_mw=50.0,
        capacity_maximum_mw=50.0,
        data_last_seen="2026-10-01T00:00:00+10:00",
    )
    fields.update(overrides)
    return fleet.Unit(**fields)


def facility(code="TEST", units=(), description="", region="NSW1", location=None):
    return {
        "code": code,
        "name": code,
        "network_region": region,
        "network_id": "NEM",
        "location": location or {"lat": -33.0, "lng": 151.0},
        "description": description,
        "units": list(units),
    }


def raw_unit(code, fueltech="battery", status="operating", **overrides):
    fields = dict(
        code=code,
        fueltech_id=fueltech,
        status_id=status,
        capacity_storage=100.0,
        capacity_registered=50.0,
        capacity_maximum=50.0,
        data_last_seen="2026-10-01T00:00:00+10:00",
    )
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------- #
# category
# --------------------------------------------------------------------------- #


class CategoryTest(unittest.TestCase):
    def test_every_known_fueltech_has_a_category(self):
        for fueltech, category in FUELTECH_CATEGORIES.items():
            with self.subTest(fueltech=fueltech):
                self.assertEqual(fleet.category_for(fueltech), category)

    def test_unrecognised_fueltech_is_other(self):
        self.assertEqual(fleet.category_for("nuclear"), "other")
        self.assertEqual(fleet.category_for(""), "other")

    def test_charge_and_discharge_siblings_are_still_batteries(self):
        # Categorising them as something else would be a lie the reader has to
        # unlearn; they are batteries, just not additional capacity (see
        # counts_toward_capacity).
        self.assertEqual(fleet.category_for("battery_charging"), "battery")
        self.assertEqual(fleet.category_for("battery_discharging"), "battery")


class FossilTest(unittest.TestCase):
    def test_fossil_categories(self):
        for fueltech in ("coal_black", "coal_brown", "gas_ccgt", "gas_ocgt",
                         "gas_steam", "gas_recip", "gas_wcmg", "distillate"):
            with self.subTest(fueltech=fueltech):
                self.assertTrue(unit(fueltech=fueltech).is_fossil)

    def test_non_fossil_categories(self):
        for fueltech in ("battery", "solar_utility", "wind", "hydro", "pumps",
                         "bioenergy_biogas"):
            with self.subTest(fueltech=fueltech):
                self.assertFalse(unit(fueltech=fueltech).is_fossil)

    def test_fossil_includes_distillate_but_the_categories_stay_distinct(self):
        # Q3: one taxonomy, two named predicates. is_fossil rolls up to the
        # region fossil total; the web map draws distillate in its own bucket.
        self.assertIn("distillate", fleet.FOSSIL_CATEGORIES)
        self.assertNotEqual(fleet.category_for("distillate"), "gas")


class CountsTowardCapacityTest(unittest.TestCase):
    def test_operating_and_committed_units_count(self):
        for status in ("operating", "commissioning", "committed"):
            with self.subTest(status=status):
                self.assertTrue(unit(status=status).counts_toward_capacity)

    def test_retired_unit_does_not_count(self):
        # Closed Hazelwood must not read as 1 GW of idle coal.
        self.assertFalse(unit(fueltech="coal_black", status="retired").counts_toward_capacity)

    def test_charge_and_discharge_series_do_not_count(self):
        # Counting them triples the fleet: 203,404 MWh against 68,041 MWh.
        for fueltech in ("battery_charging", "battery_discharging"):
            with self.subTest(fueltech=fueltech):
                self.assertFalse(unit(fueltech=fueltech).counts_toward_capacity)

    def test_predicate_is_independent_of_whether_a_capacity_number_exists(self):
        self.assertTrue(unit(capacity_registered_mw=None).counts_toward_capacity)


# --------------------------------------------------------------------------- #
# pumped storage (a facility, not a unit)
# --------------------------------------------------------------------------- #


class IsPumpedTest(unittest.TestCase):
    def fl(self, units, description=""):
        return fleet.Facility(
            code="HYDRO", name="Hydro", region="NSW1", network="NEM",
            location={"lat": 1.0, "lng": 2.0}, description=description,
            units=tuple(units),
        )

    def test_fueltech_pumps_marks_the_facility(self):
        u = unit(code="P1", fueltech="pumps", capacity_storage_mwh=None)
        self.assertTrue(self.fl([u]).is_pumped)

    def test_hydro_unit_with_stored_energy_marks_the_facility(self):
        u = unit(code="H1", fueltech="hydro", capacity_storage_mwh=10.0)
        self.assertTrue(self.fl([u]).is_pumped)

    def test_plain_hydro_is_not_pumped(self):
        u = unit(code="H1", fueltech="hydro", capacity_storage_mwh=None)
        self.assertFalse(self.fl([u]).is_pumped)

    def test_description_is_an_independent_signal(self):
        u = unit(code="H1", fueltech="hydro", capacity_storage_mwh=None)
        self.assertTrue(self.fl([u], description="a pumped storage scheme").is_pumped)

    def test_a_retired_pump_sibling_still_marks_the_scheme(self):
        live = unit(code="H1", fueltech="hydro", capacity_storage_mwh=None)
        retired_pump = unit(code="P1", fueltech="pumps", status="retired",
                            capacity_storage_mwh=None)
        self.assertTrue(self.fl([live, retired_pump]).is_pumped)

    def test_batteries_do_not_mark_a_facility_pumped(self):
        # Every battery carries capacity_storage; only hydro/pumps count.
        u = unit(code="B1", fueltech="battery", capacity_storage_mwh=100.0)
        self.assertFalse(self.fl([u]).is_pumped)


# --------------------------------------------------------------------------- #
# duplicate unit codes
# --------------------------------------------------------------------------- #


class DuplicateTest(unittest.TestCase):
    def load_with(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return fleet.load(path)

    def payload(self, *units):
        return {"cached_at": 1.0, "data": [facility("MERREDIN", units)]}

    def test_most_advanced_status_wins(self):
        fl = self.load_with(self.payload(
            raw_unit("MERREDIN_ESR1", status="commissioning"),
            raw_unit("MERREDIN_ESR1", status="operating"),
        ))
        self.assertIsNotNone(fl)
        self.assertEqual(len(fl.units), 1)
        self.assertEqual(fl.units[0].status, "operating")

    def test_dedup_wins_over_the_order_upstream_returned(self):
        fl = self.load_with(self.payload(
            raw_unit("MERREDIN_ESR1", status="operating"),
            raw_unit("MERREDIN_ESR1", status="commissioning"),
        ))
        self.assertEqual(fl.units[0].status, "operating")

    def test_duplicate_logs_both_statuses(self):
        with self.assertLogs("fleet", level="WARNING") as captured:
            self.load_with(self.payload(
                raw_unit("MERREDIN_ESR1", status="commissioning"),
                raw_unit("MERREDIN_ESR1", status="operating"),
            ))
        joined = "\n".join(captured.output)
        self.assertIn("MERREDIN_ESR1", joined)
        self.assertIn("commissioning", joined)
        self.assertIn("operating", joined)

    def test_the_dropped_row_leaves_its_facility_too(self):
        fl = self.load_with(self.payload(
            raw_unit("MERREDIN_ESR1", status="commissioning"),
            raw_unit("MERREDIN_ESR1", status="operating"),
        ))
        self.assertEqual(len(fl.facilities), 1)
        self.assertEqual(len(fl.facilities[0].units), 1)
        self.assertIs(fl.facilities[0].units[0], fl.units[0])

    def test_unrecognised_status_loses_to_a_recognised_one(self):
        fl = self.load_with(self.payload(
            raw_unit("U1", status="vapourising"),
            raw_unit("U1", status="operating"),
        ))
        self.assertEqual(fl.units[0].status, "operating")

    def test_first_wins_when_both_statuses_tie(self):
        fl = self.load_with(self.payload(
            raw_unit("U1", status="operating", capacity_storage=1.0),
            raw_unit("U1", status="operating", capacity_storage=2.0),
        ))
        self.assertEqual(fl.units[0].capacity_storage_mwh, 1.0)

    def test_no_duplicates_gives_every_unit_its_own_entry(self):
        fl = load_facilities(fixture_facilities())
        codes = [u.code for u in fl.units]
        self.assertEqual(len(codes), len(set(codes)))


# --------------------------------------------------------------------------- #
# dispatch unit id <-> unit code
# --------------------------------------------------------------------------- #


class AliasTest(unittest.TestCase):
    def fl(self):
        return load_facilities([
            facility("TB2SF", [raw_unit("TB2B1"), raw_unit("TB2BG1",
                                                           fueltech="battery_charging")]),
            facility("TBBESS3", [raw_unit("TBBESS3B1")]),
        ])

    def test_identity_duid_resolves_to_its_own_unit(self):
        self.assertEqual(self.fl().unit_for_duid("TB2B1").code, "TB2B1")

    def test_aliased_duid_resolves_to_the_fleet_unit(self):
        self.assertEqual(self.fl().unit_for_duid("TB3B1").code, "TBBESS3B1")

    def test_unknown_duid_resolves_to_none(self):
        self.assertIsNone(self.fl().unit_for_duid("NOSUCH1"))

    def test_empty_duid_resolves_to_none(self):
        self.assertIsNone(self.fl().unit_for_duid(""))

    def test_alias_table_lists_only_mismatches(self):
        # Identity entries would be noise: the exact match is tried first, so a
        # key equal to its value would never be reached.
        for duid, code in fleet.UNIT_ALIASES.items():
            with self.subTest(duid=duid):
                self.assertNotEqual(duid, code)

    def test_alias_table_is_the_known_divergence(self):
        self.assertEqual(fleet.UNIT_ALIASES, {"TB3B1": "TBBESS3B1"})

    def test_a_duid_matching_nothing_returns_none_rather_than_raising(self):
        # The AEMO exporter calls this for every dispatch unit in SCADA, most
        # of which are not storage units.
        fl = self.fl()
        for duid in ("RT_VIC1", "DG_NSW1", "UWF1", "KIDSPHG1"):
            with self.subTest(duid=duid):
                self.assertIsNone(fl.unit_for_duid(duid))


# --------------------------------------------------------------------------- #
# the module's interface: write_fleet / load
# --------------------------------------------------------------------------- #


class RoundTripTest(unittest.TestCase):
    def test_written_file_has_the_shared_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            fleet.write_fleet(path, fixture_facilities())
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(sorted(payload), ["cached_at", "data"])
        self.assertIsInstance(payload["data"], list)
        self.assertLess(abs(payload["cached_at"] - time.time()), 30)

    def test_written_data_is_the_raw_payload_unreshaped(self):
        # ADR 0002: the file stays the raw OpenElectricity payload. Narrowing it
        # to battery rows, or de-duplicating it here, would change the interface
        # every other reader of the volume sees.
        facilities = fixture_facilities()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            fleet.write_fleet(path, facilities)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["data"], facilities)

    def test_a_missing_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "deeper", "fleet.json")
            fleet.write_fleet(path, fixture_facilities())
            self.assertTrue(os.path.exists(path))
            self.assertIsNone(fleet.load(os.path.join(tmp, "nested", "deeper", "nope.json")))

    def test_cached_at_survives_the_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            fleet.write_fleet(path, fixture_facilities())
            fl = fleet.load(path)
        self.assertIsNotNone(fl)
        self.assertLess(abs(fl.cached_at - time.time()), 30)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            fleet.write_fleet(path, fixture_facilities())
            self.assertEqual(os.listdir(tmp), ["fleet.json"])

    def test_missing_file_is_none(self):
        self.assertIsNone(fleet.load(os.path.join("no", "such", "file.json")))

    def test_unparseable_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fleet.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertIsNone(fleet.load(path))

    def test_payload_without_a_facility_list_is_none(self):
        for payload in ({}, {"cached_at": 1.0}, {"cached_at": 1.0, "data": None},
                        {"cached_at": 1.0, "data": []},
                        {"cached_at": 1.0, "data": "nope"}):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "fleet.json")
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                    self.assertIsNone(fleet.load(path))

    def test_missing_facility_fields_are_tolerated(self):
        # The map and the AEMO exporter both degrade rather than raise when a
        # facility arrives without the fields they want.
        fl = load_facilities([{"code": "BARE", "units": [raw_unit("BARE1")]}])
        self.assertIsNotNone(fl)
        fac = fl.facilities[0]
        self.assertEqual(fac.name, "")
        self.assertEqual(fac.region, "")
        self.assertEqual(fac.network, "NEM")
        self.assertEqual(fac.units[0].facility, "BARE")
        self.assertEqual(fac.units[0].facility_name, "")


class FacilityAndUnitTest(unittest.TestCase):
    def setUp(self):
        self.fl = load_facilities(fixture_facilities())

    def test_facilities_keep_their_own_units(self):
        for fac in self.fl.facilities:
            with self.subTest(facility=fac.code):
                self.assertTrue(fac.units)

    def test_flat_units_are_the_same_objects_as_the_facility_ones(self):
        nested = {id(u) for fac in self.fl.facilities for u in fac.units}
        self.assertEqual(nested, {id(u) for u in self.fl.units})

    def test_region_comes_from_the_facility_not_the_unit(self):
        # AEMO's dispatch data carries no per-unit region, so this is the only
        # place the fleet's region comes from - and every unit of a facility
        # shares it.
        fl = load_facilities([
            facility("A", [raw_unit("A1")], region="QLD1"),
            facility("B", [raw_unit("B1")], region="TAS1"),
        ])
        regions = {u.code: u.region for u in fl.units}
        self.assertEqual(regions, {"A1": "QLD1", "B1": "TAS1"})

    def test_network_is_carried_from_each_facility(self):
        # The fleet spans two networks, and a WEM facility queried under
        # network_code=NEM 404s - so the network rides per facility rather than
        # being assumed.
        self.assertEqual({u.network for u in self.fl.units}, {"NEM", "WEM"})

    def test_location_is_a_dict_or_absent(self):
        # Facilities without a coordinate are normalised to None, which is what
        # lets the map drop a thermal plant rather than pin it to (0, 0).
        located = 0
        for fac in self.fl.facilities:
            with self.subTest(facility=fac.code):
                if fac.location is None:
                    continue
                self.assertIsInstance(fac.location, dict)
                self.assertIn("lat", fac.location)
                self.assertIn("lng", fac.location)
                located += 1
        self.assertGreater(located, 0)

    def test_description_is_carried_through(self):
        facs = [f for f in self.fl.facilities if f.description]
        self.assertTrue(facs)


# --------------------------------------------------------------------------- #
# Q11(c): the OE exporter's published labels must be the module's units
# --------------------------------------------------------------------------- #


class PublishedLabelsMatchModuleTest(unittest.TestCase):
    """Pin the exporter's output to the module so the two cannot drift.

    The exporter is free to keep its own row shape and its own rendering, but
    *which units it publishes and what it says about them* must be exactly the
    module's battery units. If someone re-derives a filter beside the module,
    this fails.
    """

    INFO_LABEL_RE = re.compile(r'^oe_battery_fleet_info\{(.*)\} 1$')

    @classmethod
    def setUpClass(cls):
        facilities = fixture_facilities()
        cls.module = load_facilities(facilities)
        state = {
            "fleet_meta": exporter.unit_rows(
                fleet.from_facilities(facilities), require_data=False
            )
        }
        cls.rendered = exporter.render(state, 1_700_000_000.0)

    def published(self) -> dict:
        labels = {}
        for line in self.rendered.splitlines():
            match = self.INFO_LABEL_RE.match(line)
            if match:
                parsed = dict(
                    re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', match.group(1))
                )
                labels[parsed["unit"]] = parsed
        return labels

    def module_battery_units(self) -> dict:
        """The units this exporter is entitled to publish.

        `fueltech == "battery"` rather than `category == "battery"`: the charge
        and discharge siblings share the category but re-report the facility's
        storage, so publishing them would triple the fleet (203,404 MWh against
        68,041 MWh). No status gate either - scrapers/README.md is explicit that
        the info metric covers committed and retired units, and only
        `require_data` narrows the numeric series; this test renders with
        `require_data=False`.
        """
        return {
            u.code: u
            for u in self.module.units
            if u.fueltech == "battery" and u.capacity_storage_mwh is not None
        }

    def test_it_publishes_exactly_the_module_battery_units(self):
        self.assertEqual(set(self.published()), set(self.module_battery_units()))

    def test_every_label_is_the_module_field(self):
        expected = self.module_battery_units()
        for code, labels in self.published().items():
            with self.subTest(unit=code):
                u = expected[code]
                self.assertEqual(labels["facility"], u.facility)
                self.assertEqual(labels["facility_name"], u.facility_name)
                self.assertEqual(labels["region"], u.region)
                self.assertEqual(labels["network"], u.network)
                self.assertEqual(labels["status"], u.status)

    def test_capacities_are_rendered_from_the_module_values(self):
        expected = self.module_battery_units()
        for code, labels in self.published().items():
            with self.subTest(unit=code):
                u = expected[code]
                self.assertEqual(
                    labels["capacity_storage_mwh"],
                    exporter.fmt(u.capacity_storage_mwh),
                )
                self.assertEqual(
                    labels["capacity_registered_mw"],
                    exporter.fmt(u.capacity_registered_mw) if u.capacity_registered_mw is not None else "",
                )

    def test_the_module_fixture_covers_the_whole_fleet(self):
        # Published with require_data=False: committed, never-built units
        # included, because describing them is the point of the info metric.
        self.assertEqual(len(self.published()), 119)


if __name__ == "__main__":
    unittest.main()
