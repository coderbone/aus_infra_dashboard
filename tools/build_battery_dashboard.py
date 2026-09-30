#!/usr/bin/env python3
"""Generate grafana/provisioning/dashboards/battery-state-of-charge.json.

Generated rather than hand-written so the repeated panel boilerplate stays
consistent, and so the file is reproducible: re-run after changing a query here
and the diff shows only what actually changed. Output uses indent=2,
sort_keys=True to match the other committed dashboards.

    python3 tools/build_battery_dashboard.py
"""

from __future__ import annotations

import json
import re
import os

DS = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(
    REPO, "grafana", "provisioning", "dashboards", "battery-state-of-charge.json"
)

# `last_over_time` for the instant panels, and a plain selector for the range
# panels. The exporter holds a reading between polls, so both are the same
# number - but the range panels must NOT bridge gaps, because a gap is the only
# visible sign that the exporter stopped answering. See the panel descriptions.
INSTANT = "last_over_time(%s[2h])"
SEL = "name=~\"$battery\""


def target(expr, refid="A", instant=True, legend="{{name}}", fmt=None):
    out = {
        "datasource": DS,
        "editorMode": "code",
        "exemplar": False,
        "expr": expr,
        "instant": instant,
        "legendFormat": legend,
        "range": not instant,
        "refId": refid,
    }
    if fmt:
        out["format"] = fmt
    return out


def ds_():
    return dict(DS)


def thresholds(steps, mode="absolute"):
    return {"mode": mode, "steps": steps}


def text_color_step():
    return [{"color": "text", "value": 0}]


def stat(title, grid, expr, description, unit=None, decimals=None, steps=None,
         text_mode="value_and_name", calc="lastNotNull", instant=True, legend="{{name}}",
         color_mode="none"):
    field = {"color": {"mode": "thresholds"}, "thresholds": thresholds(steps or text_color_step())}
    if unit:
        field["unit"] = unit
    if decimals is not None:
        field["decimals"] = decimals
    return {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": {"defaults": field},
        "gridPos": grid,
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "horizontal",
            "percentChangeColorMode": "standard",
            "reduceOptions": {"calcs": [calc], "fields": "", "values": False},
            "showPercentChange": False,
            "textMode": text_mode,
            "wideLayout": True,
        },
        "targets": [target(expr, instant=instant, legend=legend)],
        "title": title,
        "type": "stat",
    }


def timeseries(title, grid, targets, description, unit=None, axis=None,
               decimals=None, span_nulls=True, step="stepAfter", legend_calcs=(),
               overrides=(), min_=None, max_=None, thresholds_steps=None,
               draw="line", stack=False):
    custom = {
        "axisBorderShow": False,
        "axisCenteredZero": False,
        "axisColorMode": "text",
        "axisLabel": axis or "",
        "axisPlacement": "auto",
        "barAlignment": 0,
        "barWidthFactor": 0.6,
        "drawStyle": draw,
        # Bars need fill, or `drawStyle: bars` draws a 1px sliver. Lines keep
        # fill off so an overlapping series does not grey out the one behind it.
        "fillOpacity": 80 if draw == "bars" else 0,
        "gradientMode": "none",
        "hideFrom": {"legend": False, "tooltip": False, "viz": False},
        "insertNulls": False,
        "lineInterpolation": step,
        "lineWidth": 1,
        "pointSize": 5,
        "scaleDistribution": {"type": "linear"},
        "showPoints": "auto",
        "showValues": False,
        # Off where a gap is information: the SOC is held between hourly polls
        # so a flat line is normal, and bridging across a missing scrape would
        # hide the one thing worth noticing.
        "spanNulls": bool(span_nulls),
        # `normal` rather than `percent`: the series are energy in MWh that
        # belong in the same total, so the stack should read as a sum, not as
        # each battery's share of it. Grafana forces the baseline to 0 when
        # stacking, which is also what a filled chart should do.
        "stacking": {"group": "A", "mode": "normal" if stack else "none"},
        "thresholdsStyle": {"mode": "off"},
    }
    field = {
        "color": {"mode": "palette-classic"},
        "custom": custom,
        "thresholds": thresholds(thresholds_steps or text_color_step()),
    }
    if unit:
        field["unit"] = unit
    if decimals is not None:
        field["decimals"] = decimals
    if min_ is not None:
        field["min"] = min_
    if max_ is not None:
        field["max"] = max_
    panel = {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": {"defaults": field},
        "gridPos": grid,
        "options": {
            "legend": {
                "calcs": list(legend_calcs),
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"},
        },
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }
    if overrides:
        panel["fieldConfig"]["overrides"] = list(overrides)
    return panel


def bargauge(title, grid, expr, description, unit="percent", min_=0, max_=100, steps=None):
    field = {
        "color": {"mode": "thresholds"},
        "decimals": 1,
        "max": max_,
        "min": min_,
        "thresholds": thresholds(steps or [{"color": "green", "value": 0}]),
        "unit": unit,
    }
    return {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": {"defaults": field},
        "gridPos": grid,
        "options": {
            "displayMode": "gradient",
            "maxVizHeight": 300,
            "minVizHeight": 16,
            "minVizWidth": 8,
            "namePlacement": "auto",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "text",
        },
        "targets": [target(expr)],
        "title": title,
        "type": "bargauge",
    }


SYMBOL_SLUGS = {"%": "pct", ",": "", " ": "_"}


def slug(header):
    """Field name for a column: 'SOC, %' -> 'SOC_pct', 'Capacity, MWh' -> 'Capacity_MWh'."""
    out = "".join(SYMBOL_SLUGS.get(c, c) for c in header)
    out = re.sub(r"[^A-Za-z0-9]+", "_", out).strip("_")
    return out or "column"


def table(title, grid, columns, description, sort_by="Rank"):
    """One Prometheus frame per column, joined on the unit code.

    `columns` is a list of `(expr, header, unit, decimals)` in display order.
    Three things here are not obvious, and all three were wrong on the first
    attempt - which is why the frames were inspected against the datasource API
    rather than reasoned about:

    - **The Prometheus datasource returns one frame *per series*, and the labels
      are not fields.** A `format: table` instant query for ten batteries comes
      back as ten frames of `Time, <metric name>`, with `facility`/`unit`/… on
      the frame rather than as columns. So `joinByField` on `unit` has no field
      to join on until `labelsToFields` puts the labels back. That is the first
      transformation, and the whole table depends on it.
    - **Every value column would be called `Value`.** After `max by (unit)`
      the metric name is gone, so the seven frames come back as `Value`,
      `Value #1` … `Value #6` after the join, numbered by frame order. Renaming
      those positionally works right up until a query returns no series and the
      numbering shifts. Instead each expression wraps itself in
      `label_replace(…, "__name__", …)`, which PromQL honours, so every column
      has a real name of its own and the renames are explicit.
    - **Joining full label sets produces `facility #1` … `facility #6`.** Only
      the first column keeps its labels; the rest are `max by (unit)`. `max`
      rather than `sum` because there is exactly one series per unit and a `sum`
      would double the value if that ever stopped being true.

    The join key is the unit code rather than the facility, because a facility
    could carry more than one battery unit in future. Capacity is the first
    column because it is exported for every unit in scope whether or not that
    unit produced a reading, so a battery that cannot be read still gets a row
    instead of silently vanishing from the table.
    """
    named = []
    for expr, header, unit, decimals in columns:
        field = slug(header)
        named.append(
            (
                'label_replace(%s, "__name__", "%s", "", "")' % (expr, field),
                field,
                header,
                unit,
                decimals,
            )
        )
    keep = ["unit", "name", "facility", "region", "status"]
    # Field order: the labels first (unit first, since it is the key), then the
    # value columns in the order the columns were declared.
    order = {name: pos for pos, name in enumerate(keep + [f for _, f, _, _, _ in named])}
    return {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                override(by_name(header), ["unit", ("decimals", decimals)])
                for _, _, header, unit, decimals in named
            ]
            + [override(by_name("Rank"), [("custom.width", 60)])],
        },
        "gridPos": grid,
        "options": {
            "cellHeight": "sm",
            "footer": {
                "countRows": False,
                "fields": "",
                "reducer": ["sum"],
                "show": False,
            },
            "showHeader": True,
            "sortBy": [{"desc": False, "displayName": sort_by}],
        },
        "targets": [
            target(expr, refid=chr(ord("A") + i), instant=True, legend="", fmt="table")
            for i, (expr, _, _, _, _) in enumerate(named)
        ],
        "title": title,
        "transformations": [
            {
                "id": "labelsToFields",
                "options": {"mode": "columns"},
            },
            {"id": "joinByField", "options": {"byField": "unit", "mode": "outer"}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "job": True,
                        "instance": True,
                        "__name__": True,
                    },
                    "includeByName": {},
                    "indexByName": order,
                    "renameByName": {field: header for _, field, header, _, _ in named},
                },
            },
        ],
        "type": "table",
    }


def override(matcher, props):
    """A fieldConfig override for `matcher`, e.g. `by_name("SOC, %")`.

    A bare string in `props` means "hide"; a `(id, value)` pair sets that
    property to that value.
    """
    out = {"matcher": matcher, "properties": []}
    for prop in props:
        if isinstance(prop, tuple):
            out["properties"].append({"id": prop[0], "value": prop[1]})
        else:
            out["properties"].append({"id": prop, "value": {"fixed": 0}})
    return out


def by_name(name):
    """Match a column by the name it has *after* the transformations have run."""
    return {"id": "byName", "options": name}


def by_frame(refid):
    """Match by query refId, for styling one series of a chart."""
    return {"id": "byFrameRefID", "options": refid}


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #

ABOUT = """\
### NEM battery state of charge

**OpenElectricity publishes no state-of-charge metric.** Every percentage here is \
derived in the exporter as `energy stored / registered capacity` \
(`oe_battery_soc_ratio` is 0-1; the panels multiply by 100). Capacity comes from \
the facility metadata, energy from the `storage_battery` metric, so a capacity \
correction upstream moves the reading. See `scrapers/oe_battery_exporter.py` for \
the derivation and `scrapers/NOTES.md` for the upstream quirks.

**Scope is the NEM and WEM battery fleet.** The `NEM` facilities endpoint also \
returns WEM units, so the top 10 by capacity is currently 9 NEM and 1 WEM \
(Collie 2). Each facility is queried on its own `network_id`, because asking for \
a WEM unit on the NEM dataset returns 404 rather than empty data.

**Idle batteries are rotated out, not monitored in vain.** A slot held by a \
battery that has published nothing for 36h goes to the next-largest candidate, at \
no extra API cost, so the scope stays full. The three Collie WEM units return \
only nulls, so `skipped as idle` settles at 3 and the board fills up with the \
next-largest publishing batteries. `Batteries with a reading` should therefore \
match `Batteries in scope`, not fall short of it.

**The feed is sparse on purpose.** As of 2026-09-30 the upstream only carries \
values for roughly 18:00-04:00 Sydney time, so a daytime reading is hours old by \
design. The `Sample age` panel is the one to read before the SOC panels: a reading \
older than a day means the feed went quiet, not that the battery emptied. The SOC \
series is **omitted** rather than zeroed when a battery cannot be read, so a \
missing bar means "no reading", never "flat and empty".

**This is the largest N batteries that have ever reported**, not the largest N \
batteries in Australia. Of the ten highest-capacity units in the fleet metadata, \
seven are `committed` and have never dispatched - Richmond Valley (2200 MWh), \
Tomago (2000), Baranduda (1886) - and they publish nothing at all. Ranking over \
units with data is the only ranking that produces a chart.

Polled hourly and scoped to 10 batteries: 11 API requests per cycle (12 on the \
once-daily cycle that also refreshes the fleet metadata), 265 a day, inside the \
free plan's 366 requests/day bucket. The 5m Prometheus scrape does not cost the \
API anything - the exporter answers it from the last completed cycle.\
"""

panels = []

panels.append(
    {
        "gridPos": {"h": 9, "w": 24, "x": 0, "y": 0},
        "id": 1,
        "options": {
            "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
            "content": ABOUT,
            "mode": "markdown",
        },
        "targets": [],
        "title": "About these metrics",
        "type": "text",
    }
)

panels.append(
    stat(
        "Batteries with a reading",
        {"h": 4, "w": 4, "x": 0, "y": 9},
        "oe_batteries_monitored",
        "How many of the in-scope batteries returned a usable sample in the last "
        "poll. This normally equals `Batteries in scope`: a battery that has "
        "published nothing for 36h is rotated out of scope and its slot goes to "
        "the next-largest candidate, so a shortfall here means a battery is in "
        "scope but unread right now, not that it is dead.",
        unit="short",
        steps=[
            {"color": "red", "value": 0},
            {"color": "orange", "value": 1},
            {"color": "green", "value": 2},
        ],
        text_mode="value",
    )
)

panels.append(
    stat(
        "Batteries in scope",
        {"h": 4, "w": 4, "x": 4, "y": 9},
        "oe_batteries_in_scope",
        "Batteries being polled: the top N by capacity, set by the exporter's "
        "`--top` flag (10 in this stack).",
        unit="short",
        text_mode="value",
    )
)

panels.append(
    stat(
        "Fleet capacity, GWh",
        {"h": 4, "w": 4, "x": 8, "y": 9},
        "oe_battery_fleet_capacity_mwh / 1000",
        "Registered storage capacity of every battery unit in the OpenElectricity "
        "fleet that has ever reported - 74 units. The whole fleet, not just the "
        "monitored ten. The metric is MWh, so the query divides by 1000: in GWh "
        "the number reads at a glance, where in MWh it was a six-digit value with "
        "a 'k' buried in it.",
        unit="short",
        decimals=1,
        text_mode="value",
    )
)

panels.append(
    stat(
        "Monitored capacity, GWh",
        {"h": 4, "w": 4, "x": 12, "y": 9},
        "oe_battery_monitored_capacity_mwh / 1000",
        "Registered capacity of the batteries that produced a reading. The gap "
        "against the in-scope ten is capacity that is in scope but unread. "
        "Divided by 1000 for GWh, same as the fleet panel.",
        unit="short",
        decimals=1,
        text_mode="value",
    )
)

panels.append(
    stat(
        "Age of oldest reading",
        {"h": 4, "w": 4, "x": 16, "y": 9},
        "max(oe_battery_sample_age_seconds)",
        "The oldest reading currently on the board. Non-zero by design - the feed "
        "only publishes overnight - but a jump into the tens of hours means the "
        "upstream went quiet, and every SOC panel is about to start lying by "
        "omission.",
        unit="s",
        decimals=0,
        steps=[
            {"color": "green", "value": 0},
            {"color": "orange", "value": 129600},
            {"color": "red", "value": 172800},
        ],
        text_mode="value",
    )
)

panels.append(
    stat(
        "Since last poll",
        {"h": 4, "w": 4, "x": 20, "y": 9},
        "time() - oe_last_poll_timestamp_seconds",
        "How long ago the exporter finished a poll. Green under 90m, red past 3h: "
        "the poll is hourly, so this is the panel that says the loop has stopped, "
        "even while every battery keeps showing its last reading.",
        unit="s",
        decimals=0,
        steps=[
            {"color": "green", "value": 0},
            {"color": "orange", "value": 5400},
            {"color": "red", "value": 10800},
        ],
        text_mode="value",
    )
)

panels.append(
    bargauge(
        "State of charge, largest batteries first",
        {"h": 12, "w": 9, "x": 0, "y": 13},
        "100 * " + INSTANT % ("oe_battery_soc_ratio{%s}" % SEL),
        "Derived state of charge for each monitored battery: stored energy divided "
        "by registered capacity. Batteries that could not be read have **no bar at "
        "all** rather than a zero-length one - the exporter omits the series "
        "instead, because an empty bar here would read as a flat battery, which is "
        "a completely different claim. The battery list comes from the capacity "
        "gauge, so an unread battery still shows up as a blank row.",
        steps=[
            {"color": "dark-red", "value": 0},
            {"color": "orange", "value": 10},
            {"color": "yellow", "value": 25},
            {"color": "green", "value": 60},
            {"color": "blue", "value": 95},
        ],
    )
)

panels.append(
    table(
        "Battery detail",
        {"h": 12, "w": 15, "x": 9, "y": 13},
        [
            (
                "oe_battery_capacity_storage_mwh{%s}" % SEL,
                "Capacity, MWh",
                "short",
                0,
            ),
            ("max by (unit) (100 * oe_battery_soc_ratio{%s})" % SEL, "SOC, %", "percent", 1),
            (
                "max by (unit) (oe_battery_energy_stored_mwh{%s})" % SEL,
                "Stored, MWh",
                "short",
                1,
            ),
            (
                "max by (unit) (oe_battery_last_sample_timestamp_seconds{%s})" % SEL,
                "Sampled at",
                "dateTimeAsIso",
                0,
            ),
            (
                "max by (unit) (oe_battery_sample_age_seconds{%s})" % SEL,
                "Sample age",
                "s",
                0,
            ),
            ("max by (unit) (oe_battery_capacity_rank{%s})" % SEL, "Rank", "short", 0),
            (
                "max by (unit) (oe_battery_scrape_success{%s})" % SEL,
                "Read",
                "short",
                0,
            ),
        ],
        "One row per monitored battery, joined on the unit code, sorted by capacity "
        "rank. A blank SOC means the exporter could not read that battery in the "
        "last poll - the row stays, because capacity is a property of the fleet "
        "rather than of the reading. `Sample age` is the honest version of the "
        "reading next to it: compare it against the overnight publication window "
        "before reading anything into a flat SOC. A battery that has gone quiet "
        "for 36h leaves the table entirely - watch `skipped as idle` in the scope "
        "panel below, and `oe_battery_idle_seconds` for the per-unit reason.",
    )
)

panels.append(
    timeseries(
        "State of charge over time",
        {"h": 11, "w": 24, "x": 0, "y": 25},
        [target("100 * oe_battery_soc_ratio{%s}" % SEL, instant=False, legend="{{name}} ({{facility}})")],
        "State of charge as a percentage. `stepAfter` because this is a step "
        "function: the exporter holds each reading until the next hourly poll, so "
        "a sloped line would invent levels that were never published. The daytime "
        "flat stretches are the sparse feed, not a fault - and Prometheus only "
        "holds what this job has scraped since it started, so there is no history "
        "further back than the exporter's first successful poll. Gaps are not "
        "bridged: if the exporter stops answering, the line stops.",
        unit="percent",
        axis="% full",
        decimals=1,
        span_nulls=False,
        min_=0,
        max_=100,
    )
)

panels.append(
    timeseries(
        "Energy stored, GWh",
        {"h": 9, "w": 12, "x": 0, "y": 36},
        [target("oe_battery_energy_stored_mwh{%s} / 1000" % SEL, instant=False,
                legend="{{name}} ({{facility}})")],
        "Stored energy, exactly as the upstream publishes it but divided by 1000 "
        "so the axis reads in GWh. Stacked, because the question this panel "
        "answers is how much the monitored fleet is holding in total, and the "
        "segments show which battery is responsible. Two decimals rather than "
        "one: the segments span 0.04 to 0.77 GWh, and at one decimal four of the "
        "seven would read 0.0. The SOC panel is stored energy divided by "
        "capacity; a battery whose energy moves while its SOC does not has had "
        "its capacity changed upstream.",
        axis="GWh",
        decimals=2,
        span_nulls=False,
        draw="bars",
        stack=True,
    )
)

panels.append(
    timeseries(
        "Sample age",
        {"h": 9, "w": 12, "x": 12, "y": 36},
        [target("oe_battery_sample_age_seconds{%s}" % SEL, instant=False, legend="{{name}}")],
        "How old the reading behind each SOC value is. This is the honest "
        "companion to the SOC panels: the sawtooth is the overnight publication "
        "window resetting each day, and a line that climbs without resetting is a "
        "feed that has stopped, however healthy the SOC curve looks.",
        axis="age",
        decimals=0,
        span_nulls=False,
    )
)

panels.append(
    timeseries(
        "API credits remaining",
        {"h": 8, "w": 8, "x": 0, "y": 45},
        [target("oe_api_credits_remaining", instant=False, legend="credits left")],
        "Daily OpenElectricity credit balance, read from the free /me endpoint each "
        "cycle. The free plan allows 500 a day; this stack's hourly ten-battery "
        "cycle uses a handful.",
        axis="credits",
        decimals=0,
        span_nulls=False,
        step="linear",
    )
)

panels.append(
    timeseries(
        "API requests against the daily bucket",
        {"h": 8, "w": 8, "x": 8, "y": 45},
        [
            target(
                "sum(increase(oe_api_requests_total{job=\"openelectricity_battery\"}[1d]))",
                refid="A",
                instant=False,
                legend="requests in the last 24h",
            ),
            target("vector(366)", refid="B", instant=False, legend="daily bucket ceiling, 366"),
        ],
        "Requests issued in the trailing 24h against the API's own 366/day bucket "
        "- the real limit, and it binds long before the 500 daily credits run out. "
        "The two are compared as *daily* quantities on purpose: the poll happens "
        "once an hour as a single burst of 12 requests, so an hourly rate would "
        "draw a spike in one 5m bucket and zero everywhere else, and a ceiling "
        "line derived the same way would sit at 0 for 23 hours out of 24. "
        "Steady state here is ~265, just under the ceiling. Failed requests "
        "count too - the exporter increments the counter when a request is "
        "issued, not when it succeeds - and a restart resets it, which "
        "`increase` accounts for.",
        axis="requests",
        decimals=0,
        span_nulls=False,
        step="linear",
        overrides=[
            override(
                by_frame("B"),
                ["custom.fillOpacity", ("custom.lineWidth", 1), ("color", {"fixedColor": "text", "mode": "fixed"})],
            )
        ],
    )
)

panels.append(
    timeseries(
        "Poll cycle duration",
        {"h": 8, "w": 8, "x": 16, "y": 45},
        [target("oe_poll_cycle_duration_seconds", instant=False, legend="cycle")],
        "Wall time of the last poll: eleven sequential API calls plus the credit "
        "read. A duration near or above the 3600s poll interval means cycles are "
        "starting to overlap the next one.",
        axis="s",
        decimals=1,
        span_nulls=False,
        step="linear",
    )
)

panels.append(
    timeseries(
        "Batteries enumerated, in scope, monitored",
        {"h": 8, "w": 24, "x": 0, "y": 53},
        [
            target("oe_batteries_enumerated", refid="A", instant=False, legend="with data upstream"),
            target("oe_batteries_in_scope", refid="B", instant=False, legend="in scope (--top)"),
            target("oe_batteries_monitored", refid="C", instant=False, legend="reading this poll"),
            target("oe_batteries_demoted", refid="F", instant=False, legend="skipped as idle"),
            target("oe_battery_series_without_capacity", refid="D", instant=False, legend="series without capacity"),
            target("oe_battery_series_too_stale", refid="E", instant=False, legend="dropped as stale"),
        ],
        "Scope accounting, all as step functions because they only change on a "
        "poll. `enumerated` is the fleet that has ever reported (74 of 119 "
        "capacity-bearing units), `in scope` is the top 10, `monitored` is what "
        "actually read. `skipped as idle` counts candidates dropped for going "
        "quiet for 36h - their slots go to the next-largest battery, so the scope "
        "stays full at no extra API cost, and 3 is the expected steady state "
        "here (the Collie WEM units). `series without capacity` is expected to be "
        "about 9: each battery facility also returns a G1 and an L1 series, which "
        "are counted and then dropped because the metadata has no capacity for "
        "them. `dropped as stale` is the one that should stay at 0.",
        axis="units",
        decimals=0,
        span_nulls=False,
    )
)

# Panel ids and y offsets have to be unique and monotonic; the gridPos values
# above are authored by hand, so assert the invariants rather than trusting them.
for index, panel in enumerate(panels, start=1):
    panel["id"] = index
ids = [p["id"] for p in panels]
assert len(set(ids)) == len(ids), "duplicate panel id: %s" % ids
positions = [(p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["w"]) for p in panels]
for i in range(1, len(positions)):
    assert positions[i][1] > positions[i - 1][1] or positions[i][0] > positions[i - 1][0], (
        "panels %d and %d overlap" % (ids[i - 1], ids[i])
    )
    assert positions[i][0] + positions[i][2] <= 24, "panel %d runs off the grid" % ids[i]

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
        "State of charge for the largest N battery storage units in the NEM and WEM "
        "fleet that publish data, from OpenElectricity. SOC is derived, not "
        "published: stored energy divided by registered capacity. The NEM facilities "
        "list also returns WEM units, so one of the units in scope may be WEM and is "
        "queried against its own network. Hourly poll, free-plan request budget."
    ),
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "id": None,
    "links": [],
    "liveNow": False,
    "preload": False,
    "refresh": "5m",
    "schemaVersion": 42,
    "tags": ["battery", "storage", "openelectricity", "nem"],
    "templating": {
        "list": [
            {
                "allowCustomValue": True,
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                "datasource": ds_(),
                "definition": "label_values(oe_battery_capacity_storage_mwh, name)",
                "hide": 0,
                "includeAll": True,
                "label": "Battery",
                "multi": True,
                "name": "battery",
                "options": [],
                "query": {
                    "query": "label_values(oe_battery_capacity_storage_mwh, name)",
                    "refId": "PrometheusVariableQueryEditor-VariableQuery",
                },
                # 2 = on time range change. The battery list is stable, but the
                # value has to refresh once a poll has landed, and this is the
                # cheapest hook for that.
                "refresh": 2,
                "regex": "",
                "regexApplyTo": "value",
                "skipUrlSync": False,
                "sort": 3,
                "type": "query",
                "allValue": ".*",
            }
        ]
    },
    "time": {"from": "now-7d", "to": "now"},
    "timepicker": {
        "refresh_intervals": ["30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"]
    },
    "timezone": "browser",
    "title": "NEM battery state of charge",
    "uid": "nembattery-soc",
    "version": 1,
    "panels": panels,
}

with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(dashboard, handle, indent=2, sort_keys=True)
    handle.write("\n")
print("wrote %s (%d panels)" % (OUT, len(panels)))
