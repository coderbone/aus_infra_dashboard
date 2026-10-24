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
# The two dashboard filters. `region` is multi-select and defaults to All, so
# `=~".*"` keeps every series that carries a region label and - because
# Prometheus treats a missing label as the empty string - every series that does
# not, which is what lets one matcher sit on both region-tagged and untagged
# metrics without dropping anything under All. It is folded into SEL so all the
# OE per-unit panels (whose metrics all carry `region`) filter in one place.
REGION = 'region=~"$region"'
SEL = 'name=~"$battery",%s' % REGION
OE_META_WINDOW = "2h"

# AEMO per-unit metrics carry only `duid`; the OE capacity gauge carries the
# display labels keyed by the same code under `unit`. `label_replace` copies
# the code into `duid` so the join keys match, and the right-hand side is
# always the SEL-filtered capacity gauge. Panels that should cover every
# battery use the `FLEET_*` form below instead. Verified against the live
# datasource 2026-10-20.
CAP_JOIN_DUID = (
    'label_replace(last_over_time(oe_battery_capacity_storage_mwh{%s}[%s]), "duid", "$1", "unit", "(.*)")'
    % (SEL, OE_META_WINDOW)
)
# Value-preserving form: capacity / capacity is 1 with the same labels, so
# multiplying imports them without touching the value. (There is no metric
# with value 1 carrying the display labels - `oe_battery_fleet_info` is 1 but
# has no `name` label.)
CAP_JOIN_ONE = "(%s / %s)" % (CAP_JOIN_DUID, CAP_JOIN_DUID)

# Whole-fleet counterpart of the pair above. Same five labels and the same
# `unit`/`duid` join, but sourced from `oe_battery_fleet_capacity_storage_mwh`
# so the join reaches every unit in the fleet metadata. Used
# by the panels that are meant to show every battery AEMO publishes.
FLEET_CAP_JOIN_DUID = (
    'label_replace(last_over_time(oe_battery_fleet_capacity_storage_mwh{%s}[%s]), '
    '"duid", "$1", "unit", "(.*)")' % (SEL, OE_META_WINDOW)
)
FLEET_INFO_STICKY = "last_over_time(oe_battery_fleet_info{%s}[%s])" % (REGION, OE_META_WINDOW)
FLEET_CAP_JOIN_ONE = "(%s / %s)" % (FLEET_CAP_JOIN_DUID, FLEET_CAP_JOIN_DUID)


def soc_pct(expr):
    """100 * expr / registered capacity, with the display labels attached.

    Division, so the join is the natural shape: AEMO publishes MWh, the
    capacity gauge publishes the MWh those bytes are a fraction of. The
    denominator is the whole-fleet capacity gauge, so state of charge is
    available for every unit AEMO publishes, not just the OE-monitored twelve.
    """
    return (
        "100 * %s / on(duid) "
        "group_left(name, region, facility, status, unit) %s"
        % (expr, FLEET_CAP_JOIN_DUID)
    )


def unit_scope_fleet(expr):
    """Like `unit_labels` but importing only `unit`, the table's join key.

    The table wraps every value column in `max by (unit)`, which drops the
    display labels anyway (the first column carries them for the whole row),
    so importing five labels here would only be thrown away. Fleet-scoped,
    like `soc_pct`.
    """
    return "%s * on(duid) group_left(unit) %s" % (expr, FLEET_CAP_JOIN_ONE)


def unit_labels(expr):
    """`expr` (duid-keyed) with the display labels attached, value untouched.

    For series that are not a fraction of capacity - power, stored MWh,
    ages, timestamps - where the join still has to happen but must not
    scale the number.
    """
    return "%s * on(duid) group_left(name, region, facility, status) %s" % (
        expr,
        CAP_JOIN_ONE,
    )


def unit_labels_fleet(expr):
    """Whole-fleet `unit_labels`: same labels, but joined through the fleet
    capacity gauge, so every unit AEMO publishes is included rather than just
    the twelve the OE exporter polls."""
    return "%s * on(duid) group_left(name, region, facility, status) (%s / %s)" % (
        expr,
        FLEET_CAP_JOIN_DUID,
        FLEET_CAP_JOIN_DUID,
    )


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


def project_soc_1h(selector):
    """Stored SOC one hour ahead at the current dispatch rate, clamped to
    0..100% of fleet capacity. Sign follows AEMO: discharging (power > 0)
    depletes stored energy, charging (power < 0) refills it, so the raw
    integral is `stored - power`. `selector` is the per-duid `topk` + sign
    tail that picks the batteries the panel is about; it must match refId A's
    so the same units are paired up in the panel."""
    return (
        "clamp_min(clamp_max(100 * (aemo_battery_inferred_stored_mwh - "
        "aemo_battery_power_mw) / on(duid) "
        "label_replace(max by (unit) (oe_battery_fleet_capacity_storage_mwh), "
        "\"duid\", \"$1\", \"unit\", \"(.*)\"), 100), 0) "
        "and on(duid) %s" % selector
    )


def fast_chargers_not_nearly_full():
    """The 12 fastest-absorbing batteries that still have headroom to take
    charge. A unit already at or above 99% of its whole-fleet capacity can
    only taper its intake, so it would otherwise sit in the top-12 forever;
    exclude it *before* `topk`, keeping the panel at a coherent twelve. The
    region join is inside the `topk` too, so the twelve are the fastest
    *within the selected regions* rather than a global twelve filtered down."""
    return (
        "topk(12, -aemo_battery_power_mw and on(duid) "
        "(aemo_battery_power_mw < -1) and on(duid) "
        "(aemo_battery_inferred_stored_mwh < on(duid) (0.99 * "
        "label_replace(max by (unit) (oe_battery_fleet_capacity_storage_mwh), "
        "\"duid\", \"$1\", \"unit\", \"(.*)\"))) "
        "and on(duid) %s)" % FLEET_INFO_STICKY
    )


def fossil_runtime():
    """Seconds the region's stored battery energy would carry the region's
    *current* fossil generation if the fossil fleet stopped at once:
    `stored MWh / fossil MW` (x3600 for seconds). Fanciful - lossless
    discharge, unbounded rate, fossil output taken as the load left behind.

    `clamp_min` keeps a denominator that momentarily reads zero from dividing
    by it, and `clamp_max` pins the display at a week (in seconds) so a
    near-zero denominator cannot blow the axis. Regions with no fossil units
    publish no fossil series, so they simply have no line here."""
    return (
        "clamp_max(3600 * aemo_battery_region_stored_mwh{%s} / on(region) "
        "clamp_min(aemo_battery_region_fossil_generation_mw{%s}, 1), 604800)"
        % (REGION, REGION)
    )


def ds_():
    return dict(DS)


def thresholds(steps, mode="absolute"):
    return {"mode": mode, "steps": steps}


def text_color_step():
    return [{"color": "text", "value": 0}]

def gauge(title, grid, targets, description, unit=None, decimals=None, threshold_steps=None, orientation="auto", max_per_row=6, min_=None, max_=None, field_min_max=False, overrides=()):
    options = {
        "legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True},
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "showThresholdLabels": False,
        "showThresholdMarkers": True,
        "orientation": orientation,
        "sizing": "fit",
        "minVizHeight": 75,
        "minVizWidth": 75,
        "textMode": "auto",
        "sparkline": True,
        "barWidthFactor": 0.54,
        "barShape": "flat",
        "segmentSpacing": 0.3,
        "segmentCount": 1,
        "shape": "gauge",
        "endpointMarker": "point",
        "effects": {"gradient": False, "barGlow": False, "centerGlow": False},
        "maxPerRow": max_per_row,
    }
    defaults = {
        "color": {"mode": "palette-classic"},
        "thresholds": thresholds(threshold_steps or [{"color": "red", "value": 0}, {"color": "#EAB839", "value": 60}, {"color": "green", "value": 80}]),
    }
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    # Standard options. `min`/`max` fix the scale ends; leave `max_` unset to
    # keep it auto. `field_min_max` makes Grafana derive the range per field
    # (per series) instead of across the whole panel - the only way a gauge for
    # one region can top out at that region's own max when regions differ.
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if field_min_max:
        defaults["fieldMinMax"] = True
    config = {"defaults": defaults}
    if overrides:
        config["overrides"] = list(overrides)
    return {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": config,
        "gridPos": grid,
        "options": options,
        "pluginVersion": "13.2.2",
        "targets": targets,
        "title": title,
        "type": "gauge",
    }


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
               draw="line", stack=False, center_zero=False):
    custom = {
        "axisBorderShow": False,
        # Only for series that cross zero, where an uncentred axis makes the
        # magnitude of the negative side read as an artefact of the scale rather
        # than as half the story.
        "axisCenteredZero": center_zero,
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

# The label fields arrive from `labelsToFields` under their raw PromQL names, so
# without this they render as `unit`, `name`, `facility`, `region`, `status` -
# which read as column identifiers rather than as headings. Renaming them here
# is also what makes the table's `sortBy` and the fieldConfig overrides address
# something a person can see.
LABEL_HEADERS = {
    "unit": "Unit code",
    "name": "Battery",
    "facility": "Facility",
    "region": "Region",
    "status": "Status",
}


def slug(header):
    """Field name for a column: 'SOC, %' -> 'SOC_pct', 'Capacity, MWh' -> 'Capacity_MWh'."""
    out = "".join(SYMBOL_SLUGS.get(c, c) for c in header)
    out = re.sub(r"[^A-Za-z0-9]+", "_", out).strip("_")
    return out or "column"


def table(title, grid, columns, description, sort_by="Rank", sort_desc=False, badges=()):
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
    - **Every value column is called `Value` and the metric name is gone.**
      The Prometheus datasource in `format: table` returns one frame per query
      whose numeric field is always named `Value`; the metric name only exists
      as the `__name__` *label*. `label_replace(…, "__name__", …)` rewrites
      that label (so `labelsToFields` shows a friendly `__name__` column, which
      `organize` then drops) but never touches the actual field name, which is
      what the old comment here claimed. When the seven frames are joined,
      `joinByField` disambiguates the seven identical `Value` fields by appending
      the query's `refId`: `Value #A` … `Value #G`. The letter is the refId, so
      unlike the positional numbering the original version feared, it is stable
      for a fixed column order even it a query returns no series - the surviving
      columns keep their refId letters.
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
    for i, (expr, header, unit, decimals) in enumerate(columns):
        field = slug(header)
        refid = chr(ord("A") + i)
        named.append(
            (
                'label_replace(%s, "__name__", "%s", "", "")' % (expr, field),
                # The Prometheus table format names the numeric field `Value`,
                # and when multiple queries are joined they collide and come
                # back as `Value #A` … `Value #G` — the letter is the query's
                # refId, so the suffix is stable for a fixed column order.
                "Value #%s" % refid,
                field,
                refid,
                header,
                unit,
                decimals,
            )
        )
    keep = ["unit", "name", "facility", "region", "status"]
    # Field order: the labels first (unit first, since it is the key), then the
    # value columns in the order the columns were declared.
    order = {name: pos for pos, name in enumerate(keep + [f for _, f, _, _, _, _, _ in named])}
    return {
        "datasource": ds_(),
        "description": description,
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                override(by_name(header), ["unit", ("decimals", decimals)])
                for _, _, _, _, header, unit, decimals in named
            ]
            + [
                # Widths are set per column because the table is the only place
                # the fleet's identity is spelled out, and a truncated "WTAHB1"
                # or "COLLIE_ESR4" makes two rows look alike. The numeric
                # columns are left to their content except the two that are
                # always a small integer.
                override(by_name("Unit code"), [("custom.width", 110)]),
                override(by_name("Battery"), [("custom.width", 130)]),
                override(by_name("Facility"), [("custom.width", 110)]),
                override(by_name("Rank"), [("custom.width", 60)]),
                override(by_name("Read OK"), [("custom.width", 80)]),
            ]
            # Opt-in per panel: a badge is only worth a filled cell when the
            # column has a handful of known values that mean something on their
            # own. Applied by display name, so it lands after `renameByName`.
            + [badge(column, options) for column, options in badges],
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
            "sortBy": [{"desc": sort_desc, "displayName": sort_by}],
        },
        "targets": [
            target(expr, refid=refid, instant=True, legend="", fmt="table")
            for (expr, _, _, refid, _, _, _) in named
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
                    "renameByName": dict(
                        LABEL_HEADERS,
                        **{field: header for _, field, _, _, _, _, header in named},
                    ),
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


# Named colours Grafana's value mappings accept, kept here so a typo in a
# colour name cannot silently render as "no colour at all" - an unmapped value
# falls back to plain text and the badge quietly disappears.
_BADGE_COLORS = {"green", "red", "yellow", "orange", "blue", "purple", "text"}


def badge(column, options, mode="color-background-solid"):
    """Render a column's few known values as scannable, filled cells.

    `options` maps the raw field value to `(text, color)`. The mapping supplies
    both the label and the fill, so a column can stop showing `1`/`0` or
    `operating`/`commissioning` without a second query.

    **Why filled cells and not icons**, which was the original request: Grafana
    13.2.2 has no icon cell type. `TableCellDisplayMode` is actions, auto,
    basic, color-background, color-background-solid, color-text, custom,
    data-links, gauge, geo, gradient-gauge, image, json-view, lcd-gauge,
    markdown, pill and sparkline - icons are not among them, and the table's
    cell renderer calls `formattedValueToString(displayValue)`, which prints the
    mapped text and ignores `ValueMappingResult.icon` even where the data model
    accepts one. The `pill` mode would be the closest visual, but it only
    exists in the newer TableNG table, and this Grafana runs the classic one
    (no tableNG feature toggle is set), so a pill setting would be ignored.

    A filled cell is therefore the strongest at-a-glance signal actually
    available, and it is honest about the data: it encodes *state*, which is
    what these columns are. `color-background-solid` is used rather than
    `color-background` because the latter is a gradient in Grafana and reads as
    decoration rather than as a status.
    """
    for value, (text, color) in options.items():
        if color not in _BADGE_COLORS:
            raise ValueError("unknown badge colour %r for column %r" % (color, column))
    mapping = [
        {
            "type": "value",
            "options": {
                value: {"index": index, "text": text, "color": color}
                for index, (value, (text, color)) in enumerate(options.items())
            },
        }
    ]
    return override(
        by_name(column),
        [
            ("mappings", mapping),
            ("custom.cellOptions", {"type": mode}),
            ("decimals", 0),
        ],
    )


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #

ABOUT = """\
### NEM battery state of charge

**State of charge, power and stored energy come from AEMO; OpenElectricity \
supplies the fleet's identity.** AEMO's daily `Next_Day_Dispatch` report gives \
each unit's registered `ENERGY_STORAGE` anchor once a day (~04:11 AEST), and \
the 5-minute `Dispatch_SCADA` feed gives measured charge/discharge MW. The \
exporter anchors on the daily report and integrates the 5-minute MW between \
anchors (charging discounted by a fitted ~0.91 conversion efficiency), which is \
what makes a *live* per-unit SOC possible at all - AEMO publishes no live \
per-unit SOC of its own. OE never published SOC either (its ratio was derived \
the same way from its own stored-energy feed); what OE has that AEMO does not \
is the clean registered-capacity and facility metadata, so the OE exporter now \
exports just that - capacity, names, status and network - plus its API-budget \
metrics, and every number that moves is AEMO's.

**The board covers the whole AEMO fleet.** Every panel pairs the AEMO numbers \
with OE's capacity metadata across all dispatch units - every unit with an \
anchor and a SCADA stream, most of a hundred-odd units - because the SOC, power \
and stored-energy numbers are AEMO's and AEMO already publishes them all; the \
bars, the detail table and the live SCADA panels carry a DUID or facility legend \
and honour the Region control, not `$battery`. The join underpins it all: AEMO \
metrics are keyed by DUID, the OE capacity gauge by `unit`, and the codes match \
(`ERB01` = `ERB01`), so dividing one by the other yields % of registered \
capacity for any unit both sides know.

**The live SOC is an inference and this board says so.** The live SOC panels \
are the integrated estimate; AEMO's own daily report is not plotted. The \
estimate re-anchors each morning when the next daily report lands, and the \
drift between anchors *is* the integration error. A bar pinned at 0% or 100% \
is the estimate hitting its capacity bound (or the zero floor), not a battery \
that is empty or full. \
`aemo_battery_infer_success == 0` or a missing series means the exporter has \
no fresh anchor or the SCADA gap exceeded its 6h bridge limit, not that the \
battery went dark.

**Sample age is the honest companion.** Both age lines - SOC's integration \
and power's measurement - track the same 5-minute SCADA feed and should sit \
near zero; either climbing past ~15 minutes means the intraday feed stalled, \
and past 30 the panels above are showing the last good state, not the current \
one. The daily report that re-anchors the live inference arrives each morning \
around 04:11 AEST; nothing on the board plots the anchor age itself.

**WEM is out of scope for AEMO.** NEMWEB publishes the NEM only, so the \
occasional WEM battery (Collie, Synergy) on the board will simply have no \
AEMO series - no bar, no line, absent rather than zero. OE still tracks them \
in its capacity and info metrics.

**OpenElectricity is metadata-only and nearly free.** Its exporter makes one \
`GET /facilities/` request per refresh (daily by default) plus a free `/me` \
credit read - two requests a day against the free plan's 366/day bucket. It \
publishes the fleet's registration data (capacity, names, status) and API \
budget metrics; every number that moves is AEMO's.\
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
        "Batteries with a live SOC",
        {"h": 4, "w": 6, "x": 0, "y": 9},
        "aemo_battery_inferred_units",
        "How many batteries AEMO is currently serving a live inferred SOC for "
        "- the units with both a daily anchor and a fresh-enough SCADA stream. "
        "This is **AEMO feed health**: a drop means the daily report is stale "
        "or the intraday feed has a gap wider than the exporter's 6h bridge "
        "limit, not that batteries have gone dark. Every AEMO unit is included.",
        unit="short",
        steps=[
            {"color": "red", "value": 0},
            {"color": "orange", "value": 1},
            {"color": "green", "value": 50},
        ],
        text_mode="value",
    )
)

panels.append(
    stat(
        "Fleet capacity, GWh",
        {"h": 4, "w": 6, "x": 6, "y": 9},
        "oe_battery_fleet_capacity_mwh / 1000",
        "Registered storage capacity of every battery unit in the OpenElectricity "
        "fleet. OpenElectricity is the fleet's identity source - names, "
        "facilities and registered capacity - so this is the whole registered "
        "NEM fleet, not the AEMO-reported subset. The metric is MWh, so the "
        "query divides by 1000: in GWh the number reads at a glance, where in "
        "MWh it was a six-digit value with a 'k' buried in it.",
        unit="short",
        decimals=1,
        text_mode="value",
    )
)

panels.append(
    stat(
        "Age of oldest reading",
        {"h": 4, "w": 6, "x": 12, "y": 9},
        "max(aemo_battery_inferred_age_seconds and on(duid) %s)"
        % FLEET_INFO_STICKY,
        "The oldest sample behind any SOC number on this board, across the "
        "selected regions. Both the SOC integration and the power measurement "
        "ride the same 5-minute `Dispatch_SCADA` feed, so this should sit near "
        "zero; orange past 15 minutes (three intervals) and red past 30 means "
        "the intraday feed stalled and every panel above is showing the last "
        "good state rather than the current one. Scoped through the OE fleet "
        "map so a dispatch unit OE has never heard of cannot set the dial.",
        unit="s",
        decimals=0,
        steps=[
            {"color": "green", "value": 0},
            {"color": "orange", "value": 900},
            {"color": "red", "value": 1800},
        ],
        text_mode="value",
    )
)

panels.append(
    stat(
        "Since last AEMO poll",
        {"h": 4, "w": 6, "x": 18, "y": 9},
        "time() - aemo_battery_intraday_last_poll_timestamp_seconds",
        "How long ago the AEMO exporter finished its intraday poll - the 5-minute "
        "SCADA cycle that feeds every SOC number on this board. Green under 20m, "
        "red past 45m: the cycle runs every 5 minutes, so this is the panel that "
        "says the loop has stopped, even while every battery keeps showing its "
        "last reading.",
        unit="s",
        decimals=0,
        steps=[
            {"color": "green", "value": 0},
            {"color": "orange", "value": 1200},
            {"color": "red", "value": 2700},
        ],
        text_mode="value",
    )
)

panels.append(
    bargauge(
        "State of charge, largest batteries first",
        {"h": 12, "w": 9, "x": 0, "y": 13},
        # AEMO's live inferred SOC, joined to the whole-fleet capacity gauge
        # for both the % denominator and the display labels. `last_over_time`
        # on the AEMO side only: it holds each bar through a brief scrape gap,
        # same trade the OE-fed version made - a permanent hole still shows.
        "100 * last_over_time(aemo_battery_inferred_stored_mwh[2h]) / on(duid) "
        "group_left(name, region, facility, status, unit) %s" % FLEET_CAP_JOIN_DUID,
        "State of charge for every battery AEMO publishes a series for, as a "
        "percentage. Each bar is AEMO's live inferred SOC: the daily "
        "`Next_Day_Dispatch` `ENERGY_STORAGE` anchor integrated forward with "
        "5-minute `Dispatch_SCADA` MW (charging discounted by the fitted ~0.91 "
        "conversion efficiency). AEMO publishes no live per-unit SOC of its "
        "own, so this is an estimate and it drifts between anchors - a bar "
        "pinned flat at 0% or 100% is the estimate hitting its capacity bound "
        "or zero floor, not a battery that is empty or full. It re-anchors "
        "every morning when the next daily report lands; "
        "`State of charge: live inferred (SCADA)` below plots the same "
        "estimate.\n\n"
        "The ramp is interpolated across the bar, so it runs dark red at empty, "
        "through orange and yellow across the middle, and is green from about "
        "three-quarters full. A nearly-full battery is unambiguously green and an "
        "empty one unambiguously red, with the in-between states readable at a "
        "glance instead of having to check the number.\n\n"
        "Batteries with no AEMO series have **no bar at all** rather than a "
        "zero-length one - a WEM unit (NEMWEB covers the NEM only) or a unit "
        "whose SCADA gap passed the exporter's 6h bridge limit simply omits "
        "the series, because an empty bar here would read as a flat battery, "
        "which is a completely different claim. Capacity comes from the "
        "whole-fleet OE metadata, so every AEMO NEM "
        "unit is eligible for a bar.",
        steps=[
            {"color": "dark-red", "value": 0},
            {"color": "orange", "value": 25},
            {"color": "yellow", "value": 50},
            {"color": "green", "value": 75},
            # Held flat rather than ramping further, so full reads as solidly
            # full instead of running into a different hue at the very top.
            {"color": "green", "value": 100},
        ],
    )
)

panels.append(
    table(
        "Battery detail",
        {"h": 12, "w": 15, "x": 9, "y": 13},
        [
            (
                # Whole-fleet capacity, but only for units AEMO actually
                # publishes an inferred SOC for. The fleet metadata also
                # carries committed/never-built units (and WEM units), and
                # those would otherwise be rows of blanks.
                "oe_battery_fleet_capacity_storage_mwh{%s} and on(unit) "
                'label_replace(aemo_battery_inferred_stored_mwh, "unit", "$1", '
                '"duid", "(.*)")' % SEL,
                "Capacity, MWh",
                "short",
                0,
            ),
            (
                "max by (unit) (%s)" % soc_pct("aemo_battery_inferred_stored_mwh"),
                "SOC, %",
                "percent",
                1,
            ),
            (
                "max by (unit) (%s)"
                % unit_scope_fleet("aemo_battery_inferred_stored_mwh"),
                "Stored, MWh",
                "short",
                1,
            ),
            (
                # `dateTimeAsIso` is milliseconds, and
                # `aemo_battery_inferred_timestamp_seconds` is seconds - without
                # the x1000 this column rendered as a 1970 date, which is worse
                # than showing nothing because it looks like a real timestamp.
                "1000 * max by (unit) (%s)"
                % unit_scope_fleet("aemo_battery_inferred_timestamp_seconds"),
                "Sampled at",
                "dateTimeAsIso",
                0,
            ),
            (
                "max by (unit) (%s)"
                % unit_scope_fleet("aemo_battery_inferred_age_seconds"),
                "Sample age",
                "s",
                0,
            ),
        ],
        "One row per battery AEMO publishes an inferred SOC for, joined on the "
        "unit code and sorted by registered capacity, largest first. Capacity "
        "and the identity/status labels come from the whole-fleet OE metadata; "
        "SOC, stored, sampled-at and age are AEMO's live inferred values, "
        "joined through the same capacity gauge the first column prints. Units "
        "with no AEMO series are omitted rather than shown as blank rows - a "
        "WEM battery (NEMWEB is NEM-only), a never-built committed unit, or a "
        "unit whose SCADA gap passed the exporter's bridge limit. `Sample age` "
        "should sit near zero (the feed is 5-minute); if it climbs, the SOC "
        "column is a snapshot of the last good state.",
        sort_by="Capacity, MWh",
        sort_desc=True,
        badges=[
            # commissioning is not a fault, it is a unit that exists and is
            # registered but is not yet producing, so it is amber rather than
            # red. Both states come from the fleet metadata, not from a reading.
            ("Status", {"operating": ("Operating", "green"), "commissioning": ("Commissioning", "yellow")}),
        ],
    )
)

panels.append(
    timeseries(
        "Energy stored, GWh",
        {"h": 9, "w": 12, "x": 0, "y": 25},
        [
            target(
                "(aemo_battery_inferred_stored_mwh / 1000) * on(duid) group_left(name, region, facility, status) label_replace(last_over_time(oe_battery_fleet_capacity_storage_mwh{name=~\"$battery\",region=~\"$region\"}[%s]), \"duid\", \"$1\", \"unit\", \"(.*)\")"
                % OE_META_WINDOW,
                instant=False,
                legend="{{facility}}",
            )
        ],
        "**Not an upstream reading.** Stored energy divided by 1000 so the axis "
        "reads in GWh, but the energy is AEMO's live inference: the daily "
        "`Next_Day_Dispatch` anchor integrated forward with 5-minute "
        "`Dispatch_SCADA` MW (charging discounted by the fitted ~0.91 "
        "conversion efficiency). The value is clamped "
        "to the battery's registered capacity, so a segment sitting flat at "
        "its capacity is the estimate hitting that bound, not a battery that "
        "is genuinely full.\n\n"
        "Stacked, because the question this panel answers is how much the "
        "fleet is holding in total, and the segments show which battery is "
        "responsible. Every AEMO unit is included - the whole fleet. "
        "Two decimals rather than one: the segments "
        "span hundredths of a GWh, and at one decimal many would read 0.0.\n\n"
        "A gap here means the inference was unavailable - no fresh anchor, or "
        "a SCADA gap wider than the exporter's 6h bridge limit - so the bars "
        "simply stop, which is a feed problem rather than a fleet holding no "
        "charge.",
        axis="GWh",
        decimals=2,
        span_nulls=False,
        draw="bars",
        stack=True,
    )
)

panels.append(
    timeseries(
        "API credits remaining",
        {"h": 8, "w": 8, "x": 0, "y": 34},
        [target("oe_api_credits_remaining", instant=False, legend="credits left")],
        "Daily OpenElectricity credit balance, read from the free /me endpoint each "
        "cycle. The free plan allows 500 a day; this stack is metadata-only and "
        "uses a couple.",
        axis="credits",
        decimals=0,
        span_nulls=False,
        step="linear",
    )
)


# --- AEMO Next_Day_Dispatch panels ---------------------------------------- #
#
# AEMO publishes stored energy in MWh and no capacity, while the OE exporter's
# `unit` label *is* the AEMO DUID attached to the registered capacity. So the
# AEMO state of charge is derived where the two overlap: divide AEMO's reported
# `ENERGY_STORAGE` by the OE capacity of the same DUID. The `on(duid)` join
# works because both sides are keyed by the same identifier; only units both
# sides know survive. The whole-fleet capacity gauge is the denominator, so
# that is every AEMO NEM unit with OE metadata (68 today).

panels.append(
    timeseries(
        "Charge/discharge rate: live SCADA (5-min)",
        {"h": 11, "w": 24, "x": 0, "y": 42},
        [
            target(
                "aemo_battery_power_mw and on(duid) %s" % FLEET_INFO_STICKY,
                instant=False,
                legend="{{duid}}",
            )
        ],
        "Each unit's **measured** output from AEMO's `Dispatch_SCADA` archive, "
        "polled every 5-minute interval and served with the interval's age. "
        "**Negative (below zero) is charging, positive (above) is discharging** - "
        "above the line is exporting, below is absorbing - and the axis is "
        "centred on zero so the two sides compare directly. Verified off real "
        "data at 15:20 on 2026-10-08: Eraring BESS −120 MW (charging), WTAHB1 "
        "−34, LIMBESS1 +0.9.\n\n"
        "This is the \"live\" view: AEMO publishes no public per-DUID live SOC, "
        "so the charge/discharge is this measured MW, and the \"SoC\" a NEMPulse "
        "shows is the daily reported anchor plus an integration of these 5-minute "
        "numbers (an inference, like the live SOC panels this board plots). "
        "This panel is the whole fleet by DUID. A "
        "flat line here is a unit actually idle; a *gap* plus a growing "
        "`aemo_battery_power_age_seconds`, or `aemo_battery_scada_success == 0`, "
        "is a missing feed.\n\n"
        "The **Region** filter scopes this per-unit view through the OE region "
        "map (`oe_battery_fleet_info`), the only per-duid region source - so a "
        "dispatch unit with no OE facility (TB3B1 today) drops out even under "
        "All. That is the same scoping the by-region panel below and the "
        "`aemo_battery_power_mw_region` recording rule already use.",
        unit="mwatth",
        axis="MW",
        decimals=1,
        span_nulls=False,
        center_zero=True,
    )
)

# Region-grouped charge/discharge rate (sum of the per-unit SCADA panel above)
panels.append(
    timeseries(
        "Charge/discharge rate by region: live SCADA (5-min)",
        {"h": 11, "w": 24, "x": 0, "y": 53},
        [
            target(
                "sum by (region) (aemo_battery_power_mw_region{%s})" % REGION,
                instant=False,
                legend="{{region}}",
            )
        ],
        "The same measured `Dispatch_SCADA` output as the per-unit panel "
        "above, summed by region. `aemo_battery_power_mw_region` is a "
        "Prometheus recording rule (`prometheus_rules.yml`) that attaches a "
        "`region` label to each unit's power by joining to the OE fleet "
        "info gauge (`oe_battery_fleet_info`), so any panel can group by "
        "region without restating the join. **Negative is charging, positive "
        "is discharging.** As a "
        "cross-check this mirrors (sign-inverted) the derivative of the "
        "published per-region `aemo_battery_region_stored_mwh` - see the "
        "panel's notes above; the two disagree by whatever AEMO's reported "
        "regional storage and per-unit SCADA do.",
        unit="mwatth",
        axis="MW",
        decimals=1,
        span_nulls=False,
        center_zero=True,
    )
)

# Inferred per-unit SoC (SCADA-integrated)
panels.append(
    timeseries(
        "State of charge: live inferred (SCADA)",
        {"h": 10, "w": 24, "x": 0, "y": 64},
        [
            target(
                "100 * aemo_battery_inferred_stored_mwh / on(duid) "
                "label_replace(max by (unit) (oe_battery_fleet_capacity_storage_mwh), "
                "\"duid\", \"$1\", \"unit\", \"(.*)\") "
                "and on(duid) %s" % FLEET_INFO_STICKY,
                instant=False,
                legend="{{duid}}",
            )
        ],
        "Per-unit state of charge dead-reckoned from the daily "
        "`Next_Day_Dispatch` ENERGY_STORAGE anchor and integrated 5-minute "
        "`Dispatch_SCADA` MW, using the same charging-leg convention as the OE "
        "exporter's inference (`charge_efficiency × |MW|×dt` when charging, "
        "`MW×dt` when discharging). Only units present in the daily anchor and "
        "within `MAX_INFER_GAP_HOURS` of their last SCADA reading are shown "
        "(`aemo_battery_infer_success == 1` when the feed is live). "
        "Join to capacity is by DUID against the **whole-fleet** capacity gauge, "
        "so this is every unit AEMO knows. This is a complement to the "
        "region-reported aggregate, not a replacement.",
        unit="percent",
        axis="SoC (%)",
        decimals=1,
        span_nulls=False,
        center_zero=False,
    )
)

# Cross-check: NEM-total inferred vs reported region BDU
panels.append(
    timeseries(
        "Inferred vs reported storage (NEM total)",
        {"h": 9, "w": 24, "x": 0, "y": 74},
        [
            target("sum(aemo_battery_inferred_stored_mwh)", refid="A", instant=False, legend="inferred total (MWh)"),
            target("sum(aemo_battery_region_stored_mwh)", refid="B", instant=False, legend="reported region total (MWh)"),
        ],
        "Cross-check of the live inference: sum of per-unit inferred stored MWh "
        "(anchored + SCADA-integrated) versus the sum of AEMO's region-level "
        "`BDU_ENERGY_STORAGE` from `DispatchIS_Reports`. The comparison is "
        "**NEM-total only** because AEMO publishes no `REGIONID` on UNIT_SOLUTION "
        "or any per-DUID→region mapping in these intraday feeds, so per-region "
        "attribution of inferred per-unit storage is not possible without an "
        "upstream mapping (e.g. MLF/connection point data). When `aemo_battery_infer_success` "
        "drops to 0, the inferred line will span gaps or disappear; when "
        "`aemo_battery_dispatchis_success` drops to 0, the reported line will."
        " Divergence is expected at boundaries (anchor publication) and during "
        "large gaps or feed regressions.",
        axis="MWh",
        unit="mwatth",
        decimals=1,
        span_nulls=False,
    )
)

panels.append(
    timeseries(
        "Stored energy by region: live (BDU)",
        {"h": 9, "w": 24, "x": 0, "y": 83},
        [
            target(
                "aemo_battery_region_stored_mwh{%s}" % REGION,
                instant=False,
                legend="{{region}}",
            )
        ],
        "The region-level aggregate stored MWh from AEMO's `DispatchIS_Reports` "
        "archive, `DISPATCH,REGIONSUM` `BDU_ENERGY_STORAGE`, polled every "
        "5-minute interval. Stacked so the top of the stack reads as the NEM's "
        "live total battery storage and the segments attribute it by region. "
        "**Reported, not inferred**: this is AEMO's own aggregate, and the only "
        "storage figure that is both live and measured across the whole fleet. "
        "Verified 15:20 on 2026-10-08: NSW1 5063.4 → 5084.6 MWh, QLD1 4238.3, "
        "SA1 1973.3, VIC1 3881.8; TAS1 has no BDU storage and publishes blanks, "
        "so it is absent by design.\n\n"
        "It is an aggregate on purpose: AEMO reports no per-unit storage "
        "intraday, so this panel cannot be unwound into per-DUID MWh - the "
        "per-unit shares only exist in the daily snapshot. A missing segment is "
        "`aemo_battery_dispatchis_success == 0` or "
        "`aemo_battery_region_storage_interval_timestamp_seconds` drifting away "
        "from `now`.",
        axis="MWh",
        unit="mwatth",
        decimals=1,
        span_nulls=False,
        draw="bars",
        stack=True,
    )
)


panels.append(
    gauge(
        "Stored energy by region: live (BDU) gauges",
        {"h": 9, "w": 24, "x": 0, "y": 92},
        [
            target(
                "aemo_battery_region_stored_mwh{%s}" % REGION,
                instant=False,
                legend="{{region}}",
            )
        ],
        "Gauges showing current stored energy per region (reactive units). "
        "Scaled from a fixed 0 floor to each region's own maximum (`fieldMinMax`), "
        "so a smaller region is not dwarfed by the largest one's range.",
        unit="mwatth",
        decimals=1,
        min_=0,
        field_min_max=True,
    )
)


panels.append(
    timeseries(
        "Fossil-load runtime by region: stored MWh / fossil MW (fanciful)",
        {"h": 9, "w": 24, "x": 0, "y": 101},
        [
            target(
                fossil_runtime(),
                instant=False,
                legend="{{region}}",
            )
        ],
        "How long the region's stored battery energy would carry the region's "
        "**current** fossil generation if the whole fossil fleet stopped at "
        "once: `stored MWh / fossil MW` (x3600 for seconds, which Grafana's "
        "time unit renders as h/d). Fanciful on every axis - it assumes "
        "lossless discharge at an unbounded rate, that fossil output is exactly "
        "the load left behind, and that nothing else picks up the slack - so "
        "read it as a mood, not a plan. It nevertheless *moves* the way one "
        "wants: it rises as batteries fill and as fossil backs off (midday "
        "solar) and falls as fossil ramps. `clamp_min` keeps a momentary zero "
        "denominator from dividing by it and the value is pinned at a week so "
        "one cannot blow the axis. Regions with no fossil units at all publish "
        "no fossil series and so have no line here.\n\n"
        "The numerator is AEMO's reported region `BDU_ENERGY_STORAGE` "
        "(`aemo_battery_region_stored_mwh`); the denominator is measured "
        "`Dispatch_SCADA` MW attributed to coal/gas/distillate by the OE fleet "
        "cache (`aemo_battery_region_fossil_generation_mw`).",
        axis="fossil-load runtime",
        unit="s",
        decimals=1,
        span_nulls=False,
    )
)


panels.append(
    gauge(
        "Most active batteries (charging): stored energy vs capacity",
        {"h": 9, "w": 24, "x": 0, "y": 110},
        [
            target(
                "100 * aemo_battery_inferred_stored_mwh / on(duid) "
                "label_replace(max by (unit) (oe_battery_fleet_capacity_storage_mwh), "
                "\"duid\", \"$1\", \"unit\", \"(.*)\") "
                "and on(duid) %s" % fast_chargers_not_nearly_full(),
                instant=True,
                legend="{{duid}}",
            ),
            target(
                project_soc_1h(fast_chargers_not_nearly_full()),
                refid="B",
                instant=True,
                legend="{{duid}} (1h)",
            ),
        ],
        "The 12 batteries charging fastest right now that still have headroom, "
        "showing % of capacity stored. Units already at or above 99% of their "
        "whole-fleet capacity are excluded *before* `topk`, since a nearly "
        "full battery can only taper its intake, not keep charging. Sign "
        "follows AEMO: `aemo_battery_power_mw < 0` is charging/absorbing, so "
        "the selection is `topk(12, -power)`. "
        "`oe_battery_fleet_capacity_storage_mwh` is the whole-fleet capacity "
        "gauge, so any dispatch unit in the SCADA panel qualifies. "
        "The dim, `(1h)` ring below each is where "
        "the unit's stored energy would sit one hour ahead at the current "
        "dispatch rate, clamped to 0..100% of capacity (`stored - power`), "
        "marked by the Point endpoint on a fixed 0-100 % scale. The **Region** "
        "filter picks the top twelve *within* the selected regions - the join "
        "sits inside the `topk`, so it is not a global twelve narrowed after.",

        unit="percent",
        decimals=1,
        min_=0,
        max_=100,
        max_per_row=12,
        overrides=[override(by_frame("B"), [("color", {"fixedColor": "#8B949E", "mode": "fixed"})])],
    )
)


panels.append(
    gauge(
        "Most active batteries (discharging): stored energy vs capacity",
        {"h": 9, "w": 24, "x": 0, "y": 119},
        [
            target(
                "100 * aemo_battery_inferred_stored_mwh / on(duid) "
                "label_replace(max by (unit) (oe_battery_fleet_capacity_storage_mwh), "
                "\"duid\", \"$1\", \"unit\", \"(.*)\") "
                "and on(duid) topk(12, aemo_battery_power_mw and on(duid) "
                "%s) "
                "and on(duid) (aemo_battery_power_mw > 0)" % FLEET_INFO_STICKY,
                instant=True,
                legend="{{duid}}",
            ),
            target(
                project_soc_1h(
                    "topk(12, aemo_battery_power_mw and on(duid) "
                    "%s) "
                    "and on(duid) (aemo_battery_power_mw > 0)" % FLEET_INFO_STICKY
                ),
                refid="B",
                instant=True,
                legend="{{duid}} (1h)",
            ),
        ],
        "The 12 batteries discharging fastest right now, showing % of capacity "
        "stored. Sign follows AEMO: `aemo_battery_power_mw > 0` is "
        "discharging, so the selection is `topk(12, power)`. "
        "`oe_battery_fleet_capacity_storage_mwh` is the whole-fleet capacity "
        "gauge, so any dispatch unit in the SCADA panel qualifies. "
        "The dim, `(1h)` ring bottom row is where "
        "each unit's stored energy would sit one hour ahead at the current "
        "dispatch rate, clamped to 0..100% of its capacity (`stored - power`), "
        "marked by the Point endpoint on a fixed 0-100 % scale. The **Region** "
        "filter picks the top twelve *within* the selected regions - the join "
        "sits inside the `topk`, so it is not a global twelve narrowed after.",
        unit="percent",
        decimals=1,
        min_=0,
        max_=100,
        max_per_row=12,
        overrides=[override(by_frame("B"), [("color", {"fixedColor": "#8B949E", "mode": "fixed"})])],
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
        "State of charge, power and stored energy for the NEM battery fleet, "
        "driven by AEMO: the daily Next_Day_Dispatch "
        "ENERGY_STORAGE anchor integrated with 5-minute Dispatch_SCADA MW for "
        "the live per-unit inference, plus AEMO's own measured MW and "
        "region-level reported storage. OpenElectricity supplies the fleet's "
        "registered capacity, names and status (metadata only) plus its own "
        "API-budget metrics - its "
        "per-unit SOC/power families were retired when AEMO coverage proved a "
        "superset. The board is the whole AEMO fleet as NEMWEB reports "
        "it (every unit AEMO publishes, NEM only, no WEM)."
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
    "tags": ["battery", "storage", "openelectricity", "nem", "aemo"],
    "templating": {
        "list": [
            {
                "allowCustomValue": True,
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                "datasource": ds_(),
                "definition": 'label_values(oe_battery_fleet_capacity_storage_mwh{'
                              'status=~"operating|commissioning"}, name)',
                "hide": 0,
                "includeAll": True,
                "label": "Battery",
                "multi": True,
                "name": "battery",
                "options": [],
                "query": {
                    "query": 'label_values(oe_battery_fleet_capacity_storage_mwh{'
                             'status=~"operating|commissioning"}, name)',
                    "refId": "PrometheusVariableQueryEditor-VariableQuery",
                },
                # 2 = on time range change. The fleet list changes only when a
                # unit is commissioned, but the value has to refresh once a poll
                # has landed, and this is the cheapest hook for that. Kept to
                # operating/commissioning rather than every capacity row so
                # committed (never-built) units do not clutter the picker; the
                # panels themselves still join to AEMO, so a selected battery
                # with no dispatch series simply has no data.
                "refresh": 2,
                "regex": "",
                "regexApplyTo": "value",
                "skipUrlSync": False,
                "sort": 3,
                "type": "query",
                "allValue": ".*",
            },
            # The Region filter. A multi-select over the union of every source
            # that carries a `region` label, because no single metric spans them
            # all: `oe_battery_fleet_info` has the WEM batteries but no TAS1 (no
            # OE battery there), while the AEMO region metrics have TAS1 but no
            # WEM. The union selector lists all six. `allValue: ".*"` makes the
            # All default a match-everything regex, so `region=~"$region"` is a
            # no-op under All and scopes the panel once a region is picked.
            {
                "allowCustomValue": True,
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                "datasource": ds_(),
                "definition": 'label_values({__name__=~"oe_battery_fleet_info|'
                              'aemo_battery_region_fossil_generation_mw"}, region)',
                "hide": 0,
                "includeAll": True,
                "label": "Region",
                "multi": True,
                "name": "region",
                "options": [],
                "query": {
                    "query": 'label_values({__name__=~"oe_battery_fleet_info|'
                             'aemo_battery_region_fossil_generation_mw"}, region)',
                    "refId": "PrometheusVariableQueryEditor-VariableQuery",
                },
                # 2 = on time range change, matching the battery variable.
                "refresh": 2,
                "regex": "",
                "regexApplyTo": "value",
                "skipUrlSync": False,
                # Alphabetical so the codes read NSW1, QLD1, SA1, TAS1, VIC1, WEM.
                "sort": 1,
                "type": "query",
                "allValue": ".*",
            },
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
