#!/usr/bin/env python3
"""Generate grafana/provisioning/dashboards/interconnectors.json.

Generated for the same reason the battery board is: one panel per link is the
same boilerplate seven times over, and the file is reproducible - re-run after
changing a query here and the diff shows only what actually changed. Output
uses indent=2, sort_keys=True to match the other committed dashboards.

    python3 tools/build_interconnector_dashboard.py

The link set, the display names and the nominal ratings come from
scrapers/links.py (docs/adr/0003), imported rather than restated, and the five
metric families are cross-checked on every build against the web map's own
read contract (webmap/server.py LIVE_QUERIES) - a rename on either side of
that seam fails here instead of shipping a silent no-data panel.

Importing is safe: the write happens only under `__main__`, so the test suite
imports the module, rebuilds the dashboard in memory and compares it with the
committed file. `build_dashboard()` runs two checkers before returning: the
structural `check_invariants` (grid geometry - the same one the battery
builder enforces, imported rather than copied) and `check_semantic_invariants`
(the board's claims: one panel per registry link in registry order, every
query pinned to its own link, the five families with the reverse rating
negated, no rate() over held interval gauges, the megwatt unit on a centred
axis that
never bridges a gap, and prose that quotes the registry's ratings). See
tools/test_build_interconnector_dashboard.py.
"""

from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(
    REPO, "grafana", "provisioning", "dashboards", "interconnectors.json"
)

sys.path.insert(0, os.path.join(REPO, "scrapers"))  # server.py imports `fleet`
sys.path.insert(0, os.path.join(REPO, "webmap"))    # the read contract below
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402  (LIVE_QUERIES - the metric families, cross-checked)
import links  # noqa: E402  (link ids, names, regions, nominal ratings)
from build_battery_dashboard import (  # noqa: E402
    DS,
    by_name,
    check_invariants,
    override,
    target,
    timeseries,
)

# The five series every panel draws, and the payload fields they answer. The
# field half is checked against server.LIVE_QUERIES on every build, so the
# dashboard cannot drift from what the web map reads - one contract, two
# consumers, checked rather than trusted (the same pin webmap/test_server.py
# holds against model.js PAYLOAD_FIELDS).
FLOW = "aemo_interconnector_flow_mw"
EXPORT_LIMIT = "aemo_interconnector_export_limit_mw"
IMPORT_LIMIT = "aemo_interconnector_import_limit_mw"
NOMINAL_FORWARD = "aemo_interconnector_nominal_forward_mw"
NOMINAL_REVERSE = "aemo_interconnector_nominal_reverse_mw"
FAMILIES = (FLOW, EXPORT_LIMIT, IMPORT_LIMIT, NOMINAL_FORWARD, NOMINAL_REVERSE)
_CONTRACT = {
    "flow": FLOW,
    "export_limit": EXPORT_LIMIT,
    "import_limit": IMPORT_LIMIT,
    "nominal_forward": NOMINAL_FORWARD,
    "nominal_reverse": NOMINAL_REVERSE,
}

# The three colours, taken from the web map's line palette
# (webmap/static/app.js LINE_COLORS) so the board and the map read the same
# thing the same way: sky for the flow, amber for the dispatch limits (the
# map's mid band), slate for the rating envelope - which is drawn faintly
# there too, as the track of the tooltip's flow bar.
FLOW_COLOR = "#5ec1e6"
LIMIT_COLOR = "#f2c84b"
BAND_COLOR = "#94a3b8"

LEGEND = {
    FLOW: "flow",
    EXPORT_LIMIT: "export limit",
    IMPORT_LIMIT: "import limit",
    NOMINAL_FORWARD: "nominal forward",
    NOMINAL_REVERSE: "nominal reverse",
}

# Grafana's unit id for Megawatt (grafana-data valueFormats, Energy category:
# `id: 'megwatt'`). NOT "mw" - an unknown id is not rejected, it is printed
# verbatim as the axis suffix, so the board silently rendered "2000 mw" in
# lowercase until this was pinned here and in the invariant below.
UNIT = "megwatt"


def _mw(value):
    """A rating in the prose: 900, not 900.0 - the registry stores floats."""
    return "%g" % value


def _selector(link_id):
    return 'interconnector="%s"' % link_id


def _styles():
    """Per-series styling, one override per legend name.

    The band is `fillBelowTo`: the forward rating fills down to the negated
    reverse rating, so the envelope is the space between them and the flow
    visibly sits inside or outside it. The limit lines are dashed and the
    rating lines dotted so a greyscale print or a colour-blind read still
    separates "what may flow" from "what the dispatch engine will allow"
    from "what is flowing".
    """
    return [
        override(by_name(LEGEND[FLOW]), [
            ("color", {"mode": "fixed", "fixedColor": FLOW_COLOR}),
            ("custom.lineWidth", 2),
        ]),
        override(by_name(LEGEND[EXPORT_LIMIT]), [
            ("color", {"mode": "fixed", "fixedColor": LIMIT_COLOR}),
            ("custom.lineStyle", ["dash", "10 10"]),
        ]),
        override(by_name(LEGEND[IMPORT_LIMIT]), [
            ("color", {"mode": "fixed", "fixedColor": LIMIT_COLOR}),
            ("custom.lineStyle", ["dash", "10 10"]),
        ]),
        override(by_name(LEGEND[NOMINAL_FORWARD]), [
            ("color", {"mode": "fixed", "fixedColor": BAND_COLOR}),
            ("custom.lineStyle", ["dot", "1 10"]),
            ("custom.lineWidth", 1),
            ("custom.fillOpacity", 12),
            ("custom.fillBelowTo", LEGEND[NOMINAL_REVERSE]),
        ]),
        override(by_name(LEGEND[NOMINAL_REVERSE]), [
            ("color", {"mode": "fixed", "fixedColor": BAND_COLOR}),
            ("custom.lineStyle", ["dot", "1 10"]),
            ("custom.lineWidth", 1),
        ]),
    ]


def link_panel(index, link_id, link):
    """One link's panel: flow, both dispatch limits, both ratings.

    `index` decides the grid only - two panels per row of the 24-column grid -
    and the ids are stamped by `build_dashboard` in enumeration order, which
    is the registry's own order.
    """
    sel = _selector(link_id)
    grid = {"h": 8, "w": 12, "x": (index % 2) * 12, "y": (index // 2) * 8}
    targets = [
        target("%s{%s}" % (FLOW, sel), refid="A", instant=False,
               legend=LEGEND[FLOW]),
        target("%s{%s}" % (EXPORT_LIMIT, sel), refid="B", instant=False,
               legend=LEGEND[EXPORT_LIMIT]),
        target("%s{%s}" % (IMPORT_LIMIT, sel), refid="C", instant=False,
               legend=LEGEND[IMPORT_LIMIT]),
        target("%s{%s}" % (NOMINAL_FORWARD, sel), refid="D", instant=False,
               legend=LEGEND[NOMINAL_FORWARD]),
        # Negated so both ratings sit on the flow's own signed axis: the
        # envelope runs from -reverse to +forward, the way AEMO's MWFLOW sign
        # and the map's flow bar already draw it.
        target("- %s{%s}" % (NOMINAL_REVERSE, sel), refid="E", instant=False,
               legend=LEGEND[NOMINAL_REVERSE]),
    ]
    description = (
        "%s (%s -> %s): flow against the static nominal envelope of %s MW "
        "forward / %s MW reverse (scrapers/links.py) and AEMO's dynamic "
        "EXPORTLIMIT/IMPORTLIMIT. The axis follows AEMO's MWFLOW sign - "
        "forward is positive - so the reverse rating plots below zero and the "
        "grey band is the range the link may carry. The limits are signed and "
        "free to run past the rating; the dispatch engine solves them per "
        "interval. Values are held between 5-minute polls, so a gap means the "
        "exporter stopped answering, not zero flow."
        % (link.name, link.from_region, link.to_region,
           _mw(link.nominal_forward_mw), _mw(link.nominal_reverse_mw))
    )
    return timeseries(
        "%s · %s" % (link_id, link.name),
        grid,
        targets,
        description,
        unit=UNIT,
        span_nulls=False,
        center_zero=True,
        overrides=_styles(),
    )


panels = [
    link_panel(index, link_id, link)
    for index, (link_id, link) in enumerate(links.LINKS.items())
]


# --------------------------------------------------------------------------- #
# invariants                                                                   #
# --------------------------------------------------------------------------- #


def _target_exprs(dashboard):
    """Every Prometheus expression on the board: panel targets and
    template-variable queries. This board has no variables; the generator is
    here so a variable added later is checked too."""
    for panel in dashboard.get("panels", []):
        for one in panel.get("targets", []) or []:
            expr = one.get("expr")
            if expr:
                yield panel, expr
    for variable in dashboard.get("templating", {}).get("list", []):
        query = variable.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        if query:
            yield None, query


def _where(panel):
    """Name the offender for an error message."""
    return "panel %s" % panel["title"] if panel else "a template variable"


_SELECTOR_RE = re.compile(r'interconnector="([^"]*)"')
# Held interval gauges, not rates: dispatch publishes a level per 5-minute
# interval and the exporter repeats it between polls, so rate() would divide a
# held value by time and invent a slope.
_RATE_RE = re.compile(r"\b(rate|irate|increase|resets)\s*\(")


def check_semantic_invariants(dashboard):
    """Enforce the board's semantic rules on a built dashboard dict.

    The structural rules in `check_invariants` pin the grid; these pin the
    *claims* - which links exist, which query answers which panel, which way
    the rating is signed - so a regeneration cannot launder a semantic change
    through the golden-bytes test. Raises ValueError, like the structural
    checker. See tools/test_build_interconnector_dashboard.py.
    """
    # The metric families must still be the ones the web map reads.
    contract = server.LIVE_QUERIES["interconnectors"]
    for field, family in _CONTRACT.items():
        published = (contract.get(field) or {}).get("metric")
        if published != family:
            raise ValueError(
                "field %r is %r in the web map's contract (server.LIVE_QUERIES) "
                "but %r here; one contract, two consumers"
                % (field, published, family)
            )

    # One panel per registry link, in registry order: the panel set is the
    # link set, so a dropped link or an extra hand-added panel fails.
    ids = [p["title"].split(" · ", 1)[0] for p in dashboard.get("panels", [])]
    if ids != list(links.LINKS):
        raise ValueError(
            "panels are %s; the registry says %s" % (ids, list(links.LINKS))
        )

    for panel in dashboard["panels"]:
        link_id, _, name = panel["title"].partition(" · ")
        link = links.LINKS[link_id]
        if name != link.name:
            raise ValueError(
                "%s: title names %r, the registry says %r"
                % (_where(panel), name, link.name)
            )

        exprs = [t.get("expr") for t in panel.get("targets", []) or []]
        if len(exprs) != len(FAMILIES):
            raise ValueError(
                "%s has %d queries; every panel draws all %d series"
                % (_where(panel), len(exprs), len(FAMILIES))
            )
        seen = set()
        for expr in exprs:
            if _RATE_RE.search(expr):
                raise ValueError(
                    "%s uses a rate over a held interval gauge: %s"
                    % (_where(panel), expr)
                )
            pinned = _SELECTOR_RE.findall(expr)
            if pinned != [link_id]:
                raise ValueError(
                    "%s must pin interconnector=%r and nothing else, got %s"
                    % (_where(panel), link_id, pinned)
                )
            negated = expr.startswith("- ")
            core = expr[2:].strip() if negated else expr
            family = core.split("{", 1)[0].strip()
            if family not in FAMILIES:
                raise ValueError(
                    "%s queries %r; expected one of %s"
                    % (_where(panel), family, list(FAMILIES))
                )
            # The reverse rating is the one series that must be negated, so
            # the envelope sits on the flow's signed axis; negating anything
            # else silently moves it across zero.
            if family == NOMINAL_REVERSE and not negated:
                raise ValueError(
                    "%s: the reverse rating must be negated (- %s)"
                    % (_where(panel), NOMINAL_REVERSE)
                )
            if family != NOMINAL_REVERSE and negated:
                raise ValueError(
                    "%s: only the reverse rating is negated, not %s"
                    % (_where(panel), family)
                )
            seen.add(family)
        if seen != set(FAMILIES):
            raise ValueError(
                "%s draws %s; expected %s"
                % (_where(panel), sorted(seen), sorted(FAMILIES))
            )

        for one in panel.get("targets", []):
            if one.get("datasource") != DS:
                raise ValueError(
                    "%s query uses datasource %r, not the pinned %r"
                    % (_where(panel), one.get("datasource"), DS)
                )

        defaults = panel["fieldConfig"]["defaults"]
        if defaults.get("unit") != UNIT:
            raise ValueError(
                "%s unit is %r, not %r" % (_where(panel), defaults.get("unit"), UNIT)
            )
        custom = defaults.get("custom") or {}
        # A gap is the exporter stopped answering, not zero flow - the same
        # rule the battery board's range panels follow.
        if custom.get("spanNulls") is not False:
            raise ValueError("%s bridges gaps (spanNulls must be false)" % _where(panel))
        # Signed axis: flow and both limits cross zero.
        if custom.get("axisCenteredZero") is not True:
            raise ValueError(
                "%s must centre the axis on zero" % _where(panel)
            )

        # Prose that quotes the ratings must quote the registry's numbers:
        # a regenerated description that restates an old rating is the drift
        # this whole builder exists to prevent (docs/adr/0003).
        for rating in (link.nominal_forward_mw, link.nominal_reverse_mw):
            quoted = "%s MW" % _mw(rating)
            if quoted not in panel.get("description", ""):
                raise ValueError(
                    "%s description does not quote %s (registry says %s MW)"
                    % (_where(panel), quoted, _mw(rating))
                )


def build_dashboard():
    """Assemble the dashboard dict: assign panel ids, enforce the invariants.

    No disk I/O - the only write in this module is `main()`. The ids are
    stamped onto the panel dicts in place (gridPos comes from the registry
    enumeration; the id is the same enumeration order), so the returned dict
    aliases the module's panel list. The test suite imports and calls this to
    compare the builder with the committed JSON.
    """
    for index, panel in enumerate(panels, start=1):
        panel["id"] = index
    check_invariants(panels)
    dashboard = {
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "description": (
            "One panel per NEM link AEMO reports on the DISPATCH,INTERCONNECTORRES "
            "table: flow against the static nominal envelope - drawn as the grey "
            "band from -reverse to +forward, both ratings from scrapers/links.py "
            "(docs/adr/0003) - and the dispatch engine's live "
            "EXPORTLIMIT/IMPORTLIMIT. Signed axis throughout, AEMO's MWFLOW "
            "convention: forward is positive. Generated by "
            "tools/build_interconnector_dashboard.py; edit that, not this JSON."
        ),
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "liveNow": False,
        "panels": panels,
        "preload": False,
        "refresh": "5m",
        "schemaVersion": 42,
        "tags": ["interconnectors", "transmission", "nem", "aemo"],
        "templating": {"list": []},
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"]
        },
        "timezone": "browser",
        "title": "NEM interconnector flows",
        "uid": "interconnectors",
        "version": 1,
    }
    # The semantic claims run on the whole assembled dict - queries, prose and
    # display rules together - so `main()` cannot write a board that fails
    # them, and the test suite rebuilds through the same gate.
    check_semantic_invariants(dashboard)
    return dashboard


def main():
    """Write the dashboard to OUT. Importing this module never gets here.

    See tools/test_build_interconnector_dashboard.py for the suite that
    rebuilds the dashboard in memory and compares it with the committed file.
    """
    dashboard = build_dashboard()
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(dashboard, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print("wrote %s (%d panels)" % (OUT, len(dashboard["panels"])))


if __name__ == "__main__":
    main()
