#!/usr/bin/env python3
"""Link facts: the one place a link's id, name, regions and ratings are stated.

A link's facts used to be triplicated: the AEMO exporter held a nominal-rating
table and a separate region-pair table, and `webmap/data/lines.geojson` carried
the display name (and, as coordinate order, the same FROM->TO direction the
region pairs assume). Adding or renaming a link meant editing all of them, and
they had already drifted in both directions - the exporter's ratings were not
what the map divided by, and a reversed coordinate order shipped a reversed
flow arrow. They live here now; see docs/adr/0003.

What the module owns (GLOSSARY: "Link registry"):

  * a link's id (the dict key, AEMO's interconnector id), its display name, the
    two regions it joins, and its nominal rating in each direction;
  * the direction convention: forward is from_region -> to_region, the sign
    AEMO gives MWFLOW, so the region ends and the two ratings are one fact
    rather than three that can disagree.

What it deliberately does not own: geometry. The map's line shapes are drawn by
hand in `webmap/data/lines.geojson` and keyed to these ids - the web map's
server attaches the name when it serves the file. Snowy and the schematic
corridors are not links and are not here.

A link AEMO reports but this module does not know still publishes its flow and
limits: it simply has no nominal series and contributes to no region totals.
The registry supplies facts; it is not a whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Link:
    """One link's facts. `nominal_forward_mw` is rated from_region ->
    to_region (AEMO's MWFLOW sign convention); `nominal_reverse_mw` is the
    other way. Both are system-normal nominal ratings from AEMO's
    "Interconnector Capabilities" report (NER clause 3.13.3(p)), deliberately
    *not* the dynamic EXPORTLIMIT/IMPORTLIMIT the dispatch engine publishes:
    those bend to the binding constraint, go negative on some links and can
    collapse onto the flow itself, so a utilisation taken against them is
    meaningless.
    """

    name: str
    from_region: str
    to_region: str
    nominal_forward_mw: float
    nominal_reverse_mw: float


# id -> link. The ids mix full region codes (NSW1-QLD1, VIC1-NSW1, V-SA,
# NSW1-SA1) with the historical MNSP abbreviations (N=NSW1, Q=QLD1, V=VIC1,
# S=SA1, T=TAS1), so the ends are spelled out in the entry rather than parsed
# from the id. Row comments name the project.
#
# VNI's rating is a range under different network conditions (400-1700 MW);
# the upper bound is the nominal. EnergyConnect is stage 1.
LINKS: dict[str, Link] = {
    "NSW1-QLD1": Link("QNI (Queensland\u2011New South Wales)", "NSW1", "QLD1", 900.0, 1400.0),
    "VIC1-NSW1": Link("New South Wales\u2011Victoria", "VIC1", "NSW1", 1700.0, 1450.0),
    "V-SA": Link("Heywood (Victoria\u2011South Australia)", "VIC1", "SA1", 600.0, 550.0),
    "V-S-MNSP1": Link("Murraylink (Victoria\u2011South Australia HVDC)", "VIC1", "SA1", 220.0, 200.0),
    "T-V-MNSP1": Link("Basslink (Tasmania\u2011Victoria HVDC)", "TAS1", "VIC1", 594.0, 478.0),
    "N-Q-MNSP1": Link("Directlink (Terranora HVDC)", "NSW1", "QLD1", 107.0, 210.0),
    "NSW1-SA1": Link("South Australia\u2011New South Wales (EnergyConnect)", "NSW1", "SA1", 150.0, 150.0),
}
