#!/usr/bin/env python3
"""The fleet: OpenElectricity's registered NEM facility list, shared by three
programs.

The OpenElectricity exporter writes this file, the AEMO exporter reads each
unit's capacity and fueltech from it, and the web map reads coordinates and
status from it. Before this module each of the three parsed it separately, so
the rules about it lived in prose in scrapers/README.md and had already drifted
apart. They live here now, behind two calls: `write_fleet` and `load`.

What the module owns:

  * the on-disk shape - `{"cached_at", "data"}` written atomically, so a reader
    never sees a half-written file;
  * parsing, as `from_facilities` for a payload already in hand and `load` for
    the file on disk;
  * a unit code names one unit. A duplicated code is one unit reported twice,
    resolved to the most advanced status (see STATUS_PRECEDENCE);
  * the fleet taxonomy - every unit has a `category` derived from its fueltech,
    and the questions that follow from it: is it fossil generation, does its
    capacity count;
  * pumped-storage classification, which is a property of a facility rather
    than of a unit, because a scheme is identified by any of three independent
    signals (a sibling pump unit, stored energy on a hydro unit, or free text
    in the facility description);
  * the bridge between an AEMO dispatch unit id and an OpenElectricity unit
    code, which are different identifiers that are usually - but not always -
    equal. Neither upstream payload carries a bridge between them, so the link
    can only be asserted: see UNIT_ALIASES and docs/adr/0001.

What it deliberately does *not* own: what a caller does with a unit. Whether a
thermal plant without a coordinate is drawable, or which capacity number to
show, are the caller's decisions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("fleet")

# How far a unit has come, most advanced first. When the same unit code appears
# twice in the payload - which is an upstream artefact, not two units - the row
# further along this list is the unit's status. An unrecognised status ranks
# last, so a new upstream value never displaces a recognised one.
STATUS_PRECEDENCE: tuple[str, ...] = ("operating", "commissioning", "committed", "retired")

# OpenElectricity publishes a battery facility as a primary unit plus its charge
# and discharge paths, each carrying the facility's registered storage capacity.
# Counting all three triples the fleet: 203,404 MWh against 68,041 MWh over the
# units in this repository's live cache. They are batteries - see category_for -
# but they are not additional capacity, which is what counts_toward_capacity
# answers.
CHARGE_DISCHARGE_FUELTECHS: frozenset[str] = frozenset(
    {"battery_charging", "battery_discharging"}
)

# The categories that roll up into a region's fossil generation. Wider than the
# web map's coal and gas buckets, which exclude distillate because the map draws
# it on its own - both are deliberate; see docs/agents/domain.md and GLOSSARY.md.
FOSSIL_CATEGORIES: frozenset[str] = frozenset({"coal", "gas", "distillate"})

# OpenNEM publishes no "pumped hydro" fueltech: pumped stations report `hydro`
# (with a sibling `pumps` unit where the load DUID is in the fleet) and their
# facility description is free text. Hence three independent signals on
# Facility.is_pumped rather than one reliable one.
PUMPED_DESC_RE = re.compile(r"pumped|pump[- ]?storage", re.IGNORECASE)

# AEMO dispatch unit id -> OpenElectricity unit code, for the units where the
# two disagree. The only known case is the Tailem Bend 3 unit: AEMO abbreviates
# the facility code (`TB3`), OpenElectricity does not (`TBBESS3`), so `TB3B1`
# and `TBBESS3B1` name
# the same unit and a string-equality join silently drops it - it lost its
# region, its fueltech and its registered capacity, and therefore the 100% top
# clamp on its inferred stored energy.
#
# Both upstream payloads identify a unit by one identifier only (AEMO's
# UNIT_SCADA carries SETTLEMENTDATE, DUID, SCADAVALUE, LASTCHANGED - no facility
# name), so this link cannot be derived. It is asserted as data. One entry per
# mismatched dispatch unit, added when a new mismatch is noticed; the AEMO
# exporter publishes `aemo_battery_fleet_metadata_missing` for the storage
# dispatch units it could not resolve, so the next one shows up as a series
# rather than as a unit quietly missing its region, capacity and clamp.
# See docs/adr/0001.
UNIT_ALIASES: dict[str, str] = {"TB3B1": "TBBESS3B1"}

_CATEGORIES: dict[str, str] = {
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

_OTHER_CATEGORY = "other"

DEFAULT_NETWORK = "NEM"


def category_for(fueltech: str) -> str:
    """The fleet category a fueltech belongs to.

    The taxonomy is the only definition of "what kind of unit is this" in the
    stack: the AEMO exporter builds region fossil totals from it, and the web
    map's drawing buckets are a projection of it. An unrecognised fueltech is
    `other`, which no caller buckets - an unknown upstream value should drop out
    of the drawing rather than land in the wrong category.
    """
    return _CATEGORIES.get(fueltech or "", _OTHER_CATEGORY)


@dataclass(frozen=True)
class Unit:
    """One registered unit.

    `facility` and `facility_name` are repeated here rather than left on the
    parent Facility because callers that work unit-by-unit (the exporter's rows,
    the AEMO exporter's capacity map) would otherwise have to hold the facility
    to render them.
    """

    code: str
    facility: str
    facility_name: str
    region: str
    network: str
    status: str
    fueltech: str
    capacity_storage_mwh: float | None
    capacity_registered_mw: float | None
    capacity_maximum_mw: float | None
    data_last_seen: str | None

    @property
    def category(self) -> str:
        return category_for(self.fueltech)

    @property
    def is_fossil(self) -> bool:
        """Rolls up into a region's fossil generation: coal, gas or distillate."""
        return self.category in FOSSIL_CATEGORIES

    @property
    def counts_toward_capacity(self) -> bool:
        """Whether this unit's registered capacity is real, additional capacity.

        False for two reasons, both about what the number *means* rather than
        about rendering:

          * a retired unit's capacity is nominal - closed Hazelwood must not
            read as 1 GW of idle coal;
          * a battery's charge and discharge paths re-report the facility's
            storage rather than adding to it.

        Deliberately independent of whether a capacity number exists: a unit
        whose capacity is unknown still counts, it just contributes nothing.
        """
        if self.status == "retired":
            return False
        return self.fueltech not in CHARGE_DISCHARGE_FUELTECHS


@dataclass(frozen=True)
class Facility:
    """One registered facility and the units it owns."""

    code: str
    name: str
    region: str
    network: str
    location: dict | None
    description: str
    units: tuple[Unit, ...]

    @property
    def is_pumped(self) -> bool:
        """Whether to draw this as pumped storage rather than classic hydro.

        None of the three signals is reliable alone - the fleet has no "pumped
        hydro" fueltech, stored energy appears on the primary hydro unit, and
        the distinction otherwise lives in free text - so all three are
        consulted, in the order they are cheapest to check. Sibling units are
        considered whether or not they still count toward capacity, because a
        retired pump DUID still identifies the scheme.
        """
        for unit in self.units:
            if unit.category == "pumped hydro":
                return True
            if unit.category == "hydro" and (unit.capacity_storage_mwh or 0.0) > 0:
                return True
        return bool(PUMPED_DESC_RE.search(self.description or ""))


class Fleet:
    """A loaded fleet: the facilities, the units, and the identity bridge.

    Built by `load`. The flat `units` and the per-facility `units` hold the same
    objects, so a caller can work either way without the two disagreeing.
    """

    __slots__ = ("cached_at", "facilities", "units", "_by_code")

    def __init__(self, cached_at: float, facilities: tuple[Facility, ...],
                 units: tuple[Unit, ...]) -> None:
        self.cached_at = cached_at
        self.facilities = facilities
        self.units = units
        self._by_code = {unit.code: unit for unit in units}

    def unit_for_duid(self, duid: str) -> Unit | None:
        """The fleet unit an AEMO dispatch unit id names, or None.

        Tries the exact match first - it holds for all but a handful of units -
        then the alias table. None means the fleet does not know this dispatch
        unit at all, which for most AEMO dispatch units is simply correct: they
        are rooftop and distributed-generation aggregates rather than facilities.
        """
        if not duid:
            return None
        known = self._by_code.get(duid)
        if known is not None:
            return known
        target = UNIT_ALIASES.get(duid)
        if target is None:
            return None
        return self._by_code.get(target)


def write_fleet(path: str, facilities: list) -> None:
    """Write the fleet cache atomically: temp file first, then rename.

    Atomic because three programs read it while a fourth writes it, and a reader
    must never see half of a facility list. Failures are logged, not raised:
    failing to cache must not fail the poll that fetched the data.
    """
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"cached_at": time.time(), "data": facilities},
                handle, indent=1, sort_keys=True,
            )
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write fleet cache %s: %s", path, exc)


def from_facilities(facilities: Any, cached_at: float = 0.0) -> Fleet:
    """Parse a facility list already in hand.

    The writer's path: the OpenElectricity exporter has the payload from the
    API and should not have to write it to a file to read it back. Anything
    that is not a list of facilities parses as an empty fleet, because every
    caller degrades rather than raising - a malformed response from the API is
    a bad poll, not a crash.

    The cache file's `cached_at` is not meaningful here (0.0), since the
    timestamp describes a file that has not been written yet.
    """
    if not isinstance(facilities, list):
        return Fleet(cached_at=cached_at, facilities=(), units=())
    return _parse(facilities, cached_at)


def load(path: str) -> Fleet | None:
    """Read and parse the fleet cache, or None when it is unusable.

    None rather than an exception, because every caller degrades: the AEMO
    exporter falls back to flooring its inference at zero, and the web map
    serves an empty fleet. A missing file, unparseable JSON and a payload
    without a facility list are all the same case to them.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("fleet cache %s unreadable: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        log.warning("fleet cache %s is not an object", path)
        return None
    cached_at = float(payload.get("cached_at") or 0.0)
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        log.warning("fleet cache %s has no facilities", path)
        return None
    return _parse(data, cached_at)


def _parse(data: list, cached_at: float) -> Fleet:
    facilities: list[Facility] = []
    survivors: dict[str, Unit] = {}
    order: list[str] = []

    for raw_facility in data:
        if not isinstance(raw_facility, dict):
            continue
        facility_code = str(raw_facility.get("code") or "")
        facility_name = str(raw_facility.get("name") or "")
        region = str(raw_facility.get("network_region") or "")
        network = str(raw_facility.get("network_id") or DEFAULT_NETWORK)
        description = str(raw_facility.get("description") or "")
        location = raw_facility.get("location")
        if not isinstance(location, dict):
            location = None

        own: list[Unit] = []
        for raw in raw_facility.get("units") or ():
            if not isinstance(raw, dict):
                continue
            code = raw.get("code")
            if not isinstance(code, str) or not code:
                # A unit with no code cannot be keyed by anything downstream,
                # so it is not a unit this stack can talk about.
                continue
            built = Unit(
                code=code,
                facility=facility_code,
                facility_name=facility_name,
                region=region,
                network=network,
                status=str(raw.get("status_id") or ""),
                fueltech=str(raw.get("fueltech_id") or ""),
                capacity_storage_mwh=_positive(raw.get("capacity_storage")),
                capacity_registered_mw=_positive(raw.get("capacity_registered")),
                capacity_maximum_mw=_positive(raw.get("capacity_maximum")),
                data_last_seen=raw.get("data_last_seen") or None,
            )
            own.append(built)
            existing = survivors.get(code)
            if existing is None:
                survivors[code] = built
                order.append(code)
            else:
                survivors[code] = _survivor(existing, built)
                log.warning(
                    "fleet lists unit %s twice with status %s and %s; keeping %s",
                    code, existing.status, built.status, survivors[code].status,
                )

        # A code claimed twice ends up owned by exactly one row - which may
        # belong to a different facility - so each facility keeps only the rows
        # that won.
        kept_units = tuple(u for u in own if survivors.get(u.code) is u)
        facilities.append(
            Facility(
                code=facility_code,
                name=facility_name,
                region=region,
                network=network,
                location=location,
                description=description,
                units=kept_units,
            )
        )

    units = tuple(survivors[code] for code in order)
    return Fleet(cached_at=cached_at, facilities=tuple(facilities), units=units)


def _survivor(first: Unit, second: Unit) -> Unit:
    """Which of two rows naming the same unit code is the unit.

    Ties keep the row met first, so the result depends on nothing beyond what
    the payload actually said.
    """
    if _precedence(second.status) < _precedence(first.status):
        return second
    return first


def _precedence(status: str) -> int:
    try:
        return STATUS_PRECEDENCE.index(status)
    except ValueError:
        return len(STATUS_PRECEDENCE)


def _positive(value: Any) -> float | None:
    """A capacity as a positive float, or None when there is not one.

    Not 0.0 for an absent capacity: a caller totalling a category would treat
    zero as a real answer when the upstream simply did not publish a figure.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return float(value)
