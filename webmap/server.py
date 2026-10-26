#!/usr/bin/env python3
"""Nem Map server.

Serves the static web map, proxies live metric queries to Prometheus, and
aggregates them with the scraper fleet cache into one /data/live JSON blob the
frontend polls.

The live readings arrive through a snapshot seam: `build_live()` asks a source
for per-group series and never talks to Prometheus itself. Two adapters sit
behind the seam - `PrometheusSource`, one batched instant query per group for
production, and `MemorySource`, canned wire-shape series for tests - so the
assembly is covered without a running Prometheus.

Routes:
  /                       html app
  /data/live              merged {interconnectors, regions, batteries, fleet,
                           soc_ramp}
  /data/fleet.json        raw scraper fleet cache (if the volume is mounted)
  /data/*.geojson         static region/line geometry (the lines' link names
                          come from the link registry, not the file)
  /api/v1/query           raw Prometheus instant-query proxy (debugging)
  /healthz                tcp-friendly probe

Stdlib only; runs inside python:3.12-alpine.
"""

import argparse
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mounted from ../scrapers/fleet.py so all three readers of the fleet cache -
# this server and the two exporters - interpret it one way. See docs/adr/0002.
import fleet
# Mounted from ../scrapers/links.py for the same reason: this server attaches
# link names to the geometry the exporter's nominal series are keyed by.
# See docs/adr/0003.
import links
# Mounted from ../scrapers/soc_policy.py for the same reason again: the SoC
# colour ramp is stated once and this server hands it to the front end, which
# cannot import a Python module. See docs/adr/0005.
import soc_policy

STATIC_DIR = os.environ.get("WEBMAP_STATIC", os.path.join(os.path.dirname(__file__), "static"))
DATA_DIR = os.environ.get("WEBMAP_DATA", os.path.join(os.path.dirname(__file__), "data"))
PROMETHEUS_URL = os.environ.get("PROM_URL", "http://prometheus:9090")

# Metric -> merge spec. Each field carries the metric family it reads (metric),
# the label whose value identifies the series (key) and, for multi-valued series
# (e.g. curtailment per fuel), an extra label the value is filed under. The
# Prometheus adapter batches a whole group into one regex __name__ selector, so
# a field names its metric rather than its own ready-made query.
LIVE_QUERIES = {
    "interconnectors": {
        "flow": {"metric": "aemo_interconnector_flow_mw", "key": "interconnector"},
        "metered": {"metric": "aemo_interconnector_metered_flow_mw", "key": "interconnector"},
        # Dynamic dispatch limits, carried for display only - never a
        # denominator. `_utilisation` below says why they cannot be one.
        "export_limit": {"metric": "aemo_interconnector_export_limit_mw", "key": "interconnector"},
        "import_limit": {"metric": "aemo_interconnector_import_limit_mw", "key": "interconnector"},
        "nominal_forward": {"metric": "aemo_interconnector_nominal_forward_mw", "key": "interconnector"},
        "nominal_reverse": {"metric": "aemo_interconnector_nominal_reverse_mw", "key": "interconnector"},
        "timestamp": {"metric": "aemo_interconnector_timestamp_seconds", "key": "interconnector"},
        "age": {"metric": "aemo_interconnector_age_seconds", "key": "interconnector"},
    },
    "regions": {
        "stored_mwh": {"metric": "aemo_battery_region_stored_mwh", "key": "region"},
        "fossil_mw": {"metric": "aemo_battery_region_fossil_generation_mw", "key": "region"},
        "rooftop_mw": {"metric": "aemo_battery_region_rooftop_solar_mw", "key": "region"},
        "import_mw": {"metric": "aemo_battery_region_import_mw", "key": "region"},
        "export_mw": {"metric": "aemo_battery_region_export_mw", "key": "region"},
        "curtail": {"metric": "aemo_battery_region_curtailment_mw", "key": "region", "extra": "fuel"},
    },
    "batteries": {
        # OE keeps exporting only fleet metadata; the per-unit SOC/power/stored
        # families it used to export were retired 2026-10-20 once AEMO coverage
        # (below) was verified a superset. No capacity series here either: the
        # map's SoC denominator is the fleet module's `capacity_mwh` (see
        # build_fleet), and a Prometheus capacity would be a second reading of
        # the same fleet.json. See docs/adr/0005.
        # whole-fleet live coverage from the AEMO exporter's per-unit estimates
        "inf_stored_mwh": {"metric": "aemo_battery_inferred_stored_mwh", "key": "unit"},
        "inf_age_s": {"metric": "aemo_battery_inferred_age_seconds", "key": "unit"},
        "inf_timestamp": {"metric": "aemo_battery_inferred_timestamp_seconds", "key": "unit"},
        # peak stored energy over the commissioning window (recording rule), used
        # to shade a commissioning facility's progress arc
        "peak_stored_mwh": {"metric": "aemo_battery_peak_stored_mwh", "key": "unit"},
        # fresh 5-min SCADA power from AEMO
        "aemo_power_mw": {"metric": "aemo_battery_power_mw", "key": "unit"},
        "aemo_power_age_s": {"metric": "aemo_battery_power_age_seconds", "key": "unit"},
        "aemo_power_ts": {"metric": "aemo_battery_power_timestamp_seconds", "key": "unit"},
    },
    # Every dispatch unit (generators, loads, storage) with its live measured MW
    # and fueltech/region labels. This is what lets the map shade
    # each wind/solar marker by its own generation instead of a region estimate.
    #
    # Every field in both per-unit groups is keyed by `unit`, the fleet unit
    # code, rather than by `duid` - the AEMO spelling. The front end looks
    # these groups up by the unit code it has from the fleet, and AEMO and
    # OpenElectricity spell a handful of units differently (AEMO's `TB3B1` is
    # the fleet's `TBBESS3B1`), so a `duid` key left that unit with no power
    # and no state of charge. Keying everything the same way also collapses
    # two label namespaces into one. Units with no fleet row carry an empty
    # `unit` and so never match a lookup. See docs/adr/0001.
    "units": {
        "power_mw": {"metric": "aemo_unit_power_mw", "key": "unit"},
        "age_s": {"metric": "aemo_unit_power_age_seconds", "key": "unit"},
        "timestamp": {"metric": "aemo_unit_power_timestamp_seconds", "key": "unit"},
    },
    # Snowy Hydro reservoir levels, reported per reservoir label by the
    # reservoir-exporter (compose runs it with --all-reservoirs). The whole
    # group is wrapped in last_over_time because this job is scraped hourly
    # while Prometheus's default lookback delta is 5m: a bare selector is only
    # answerable for the five minutes after each scrape and would blank the
    # card for most of the hour. A 2h window survives a skipped scrape and
    # still clears the card within 2h if the exporter dies - generous is safe
    # here because the upstream publishes these levels once a day. An absent
    # exporter yields an empty result, not an error, so it never degrades the
    # whole page.
    "reservoirs": {
        "level": {"metric": "snowy_tantangara_level_percent", "key": "reservoir", "window": "2h"},
        "change_7d": {"metric": "snowy_tantangara_level_change_7d_percentage_points", "key": "reservoir", "window": "2h"},
        "last_sample": {"metric": "snowy_tantangara_last_sample_timestamp_seconds", "key": "reservoir", "window": "2h"},
    },
}

# metric name -> (group, field, spec). Metric names are unique across the
# table, which is what lets a batched result set - every series the group's
# regex selector returned, tagged with __name__ - be filed back onto fields.
_FIELD_BY_METRIC = {
    spec["metric"]: (group, field, spec)
    for group, fields in LIVE_QUERIES.items()
    for field, spec in fields.items()
}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".geojson": "application/geo+json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _query_prometheus(query: str, timeout: float = 10.0, at: float | None = None):
    """Run an instant query, optionally at a historical timestamp.

    `at` is passed straight to Prometheus's `time` parameter (Unix seconds).
    An absent/None `at` keeps the old behaviour: evaluate at "now"."""
    url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(query)
    if at is not None:
        url += "&time=" + urllib.parse.quote(str(at))
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if body.get("status") != "success":
        raise RuntimeError(body.get("error") or body.get("status"))
    return body.get("data", {}).get("result", [])


def _merge(accum, field, spec, labels, value):
    key = labels.get(spec["key"], "")
    if key == "":
        return
    # A reading enters the payload here, so this is where `_number` rejects a
    # non-finite sample. Leave the field absent: the front end already renders
    # an absent field as "n/a", and the rest of the payload survives.
    reading = _number(value)
    if reading is None:
        return
    obj = accum.setdefault(key, {})
    if spec.get("extra"):
        obj.setdefault("curtail", {})[labels[spec["extra"]]] = reading
    else:
        obj[field] = reading


def _number(value):
    """`value` as a finite float, or None when it is not one.

    Readings arrive as strings, and the exporter writes `NaN` for a missing
    one - `float("NaN")` is a float that satisfies neither `< 0` nor `<= 0`,
    so it would sail past a plain isinstance check, reach `json.dumps`, and
    come out as a bare `NaN` that the browser's `response.json()` rejects,
    taking the whole payload with it. Parsing the wire string here keeps that
    rule in one place, for readings and derived values alike.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _utilisation(link):
    """How loaded a link is, as a fraction of its nominal rating.

    Each direction has its own nominal: AEMO publishes them separately and a
    link can be asymmetric (VIC1-NSW1 is 1700 MW one way, 1450 the other), so
    the flow's sign picks the denominator - positive is the forward direction,
    which is how `nominal_forward` is defined.

    Deliberately not the dynamic dispatch limits, although those are also in the
    payload for display. They bend to the binding constraint, go negative on
    some links and can collapse onto the flow itself, so dividing by them reads
    a heavily loaded link as idle and an idle one as full.

    None when there is no rating to divide by: a link with flow but no known
    nominal is unknown, not infinitely loaded.
    """
    flow = _number(link.get("flow"))
    if flow is None:
        return None
    nominal = _number(link.get("nominal_reverse") if flow < 0 else link.get("nominal_forward"))
    if nominal is None or nominal <= 0:
        return None
    return abs(flow) / nominal


def _batch_expr(fields: dict) -> str:
    """One instant query for every metric a group needs.

    A regex __name__ selector returns each metric family in one result set,
    tagged with __name__, which is what build_live keys the merge by.
    Prometheus anchors regex matchers fully, so the alternation cannot
    over-match a metric outside the group. A group is either wholly windowed
    (the reservoirs' last_over_time) or not; a mixed group is a table bug.
    """
    names = "|".join(sorted(spec["metric"] for spec in fields.values()))
    selector = '{__name__=~"%s"}' % names
    windows = {spec.get("window") for spec in fields.values() if spec.get("window")}
    if len(windows) > 1:
        raise ValueError("one lookback window per group")
    if windows:
        return "last_over_time(%s[%s])" % (selector, next(iter(windows)))
    return selector


class PrometheusSource:
    """Production snapshot adapter: one batched instant query per group.

    This used to be one query per field - 27 round trips per payload, and per
    historical frame during the front end's 24h playback. A failed batch is
    caught here and returned as a group error, so one dead metric family
    degrades its group without sinking the others; build_live turns the
    errors into the payload's degraded status.
    """

    def fetch(self, at: float | None = None):
        groups, errors = {}, []
        for group, fields in LIVE_QUERIES.items():
            try:
                groups[group] = _query_prometheus(_batch_expr(fields), at=at)
            except Exception as exc:  # noqa: BLE001 - one bad batch must not sink the page
                groups[group] = []
                errors.append(f"{group}: {exc}")
        return groups, errors


class MemorySource:
    """In-memory snapshot adapter: canned series in Prometheus wire shape.

    The test double for the Prometheus path - same `fetch` contract, no
    network - so the assembly is exercised exactly as production runs it.
    `series` maps a metric name to {key: value}; a value that is a dict is
    expanded per key of the field's `extra` label (curtailment per fuel).
    `fail` names groups whose batch reports an error, mirroring a Prometheus
    batch that failed and was caught by the adapter.
    """

    def __init__(self, series: dict | None = None, fail=()):
        self.series = series or {}
        self.fail = set(fail)

    def fetch(self, at: float | None = None):
        groups, errors = {}, []
        for group, fields in LIVE_QUERIES.items():
            groups[group] = []
            if group in self.fail:
                errors.append(f"{group}: canned failure")
                continue
            for field, spec in fields.items():
                metric = spec["metric"]
                for key, value in self.series.get(metric, {}).items():
                    if spec.get("extra") and isinstance(value, dict):
                        for extra_key, nested in value.items():
                            groups[group].append({
                                "metric": {"__name__": metric, spec["key"]: key,
                                           spec["extra"]: extra_key},
                                "value": [1700000000.0, str(nested)],
                            })
                    else:
                        groups[group].append({
                            "metric": {"__name__": metric, spec["key"]: key},
                            "value": [1700000000.0, str(value)],
                        })
        return groups, errors


def build_live(at: float | None = None, source=None):
    """The /data/live payload: readings from the snapshot source, shaped for
    the front end and merged with the fleet cache.

    The source defaults to the production Prometheus adapter; tests pass a
    MemorySource. Errors the source reports (a failed group batch) mark the
    payload degraded without dropping the groups that did answer.
    """
    if source is None:
        source = PrometheusSource()
    out = {"fetched_at": time.time(), "prometheus": "ok", "errors": [],
           "interconnectors": {}, "regions": {}, "batteries": {}, "units": {},
           "reservoirs": {}}
    at = _number(at)
    if at is not None:
        out["at"] = at
    # The SoC ramp, adapted from the policy module's percent stops to the
    # front end's 0..1 scale. Static policy, not a reading - but it travels
    # with the payload because the browser cannot import the module, and
    # because the front end styles from what it is given rather than
    # restating a rule it has no way to test (same reasoning as utilisation
    # being derived here).
    out["soc_ramp"] = [
        {"v": value / 100.0, "c": list(rgb)}
        for value, rgb in soc_policy.SOC_RAMP
    ]
    groups, errors = source.fetch(at)
    out["errors"] = list(errors)
    if errors:
        out["prometheus"] = "degraded"
    for group, series_list in groups.items():
        for series in series_list:
            labels = series.get("metric", {})
            located = _FIELD_BY_METRIC.get(labels.get("__name__"))
            if located is None or located[0] != group:
                continue  # a series the table does not field (or the wrong batch)
            _, field, spec = located
            _merge(out[group], field, spec, labels, series.get("value", [None, None])[1])
    # Derived here rather than in the browser: the front end styles from a
    # number instead of recomputing a rule it has no way to test.
    for link in out["interconnectors"].values():
        link["utilisation"] = _utilisation(link)
    fl = build_fleet()
    out["fleet"] = fl.get("fleet", []) if isinstance(fl, dict) else fl
    out["renewables"] = fl.get("renewables", []) if isinstance(fl, dict) else []
    out["hydro"] = fl.get("hydro", []) if isinstance(fl, dict) else []
    out["coal"] = fl.get("coal", []) if isinstance(fl, dict) else []
    out["gas"] = fl.get("gas", []) if isinstance(fl, dict) else []
    out["distillate"] = fl.get("distillate", []) if isinstance(fl, dict) else []
    return out


_fleet_cache = {"mtime": None, "data": None}

# What a facility and a unit *are* - its category, whether its capacity counts,
# whether a scheme is pumped storage - is the fleet module's answer, not this
# server's. Two decisions about drawing stay here: a point source with no
# coordinate is dropped rather than pinned to (0, 0), and the solar/wind
# markers carry the fueltech spelling the front end expects.


def _facility_entry(fac: fleet.Facility, units: list, kind: str) -> dict:
    return {
        "facility": fac.code,
        "name": fac.name,
        "region": fac.region,
        "location": fac.location,
        "units": units,
        "kind": kind,
    }


def build_fleet():
    path = os.environ.get("FLEET_CACHE", "/cache/fleet.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    cached = _fleet_cache
    if cached["mtime"] == mtime:
        return cached["data"]
    fl = fleet.load(path)
    if fl is None:
        return []
    out = {"fleet": [], "renewables": [], "hydro": [],
           "coal": [], "gas": [], "distillate": []}
    for fac in fl.facilities:
        drawn = {"battery": [], "renewable": [], "hydro": [],
                 "coal": [], "gas": [], "distillate": []}
        for unit in fac.units:
            if not unit.counts_toward_capacity:
                # Retired units keep a registered capacity but produce nothing
                # (closed Hazelwood must not read as 1 GW of idle coal), and a
                # battery's charge/discharge siblings re-report the facility's
                # storage rather than adding to it. `fleet` owns both rules.
                continue
            category = unit.category
            if category == "battery":
                if unit.capacity_storage_mwh is not None:
                    drawn["battery"].append({
                        "code": unit.code,
                        "capacity_mwh": unit.capacity_storage_mwh,
                        "registered_mw": unit.capacity_registered_mw,
                        "status": unit.status,
                        "data_last_seen": unit.data_last_seen,
                        "fueltech_id": "battery",
                    })
                continue
            capacity = unit.capacity_registered_mw or unit.capacity_maximum_mw
            if capacity is None:
                continue
            if category in ("solar", "wind"):
                drawn["renewable"].append({
                    "code": unit.code,
                    "capacity_mw": capacity,
                    # The front end keys its markers off these spellings, so
                    # `wind_farm` is drawn as `wind` while `solar_utility`
                    # keeps the value it already matches on.
                    "fueltech_id": (unit.fueltech if unit.fueltech in
                                    ("solar", "solar_utility", "wind") else category),
                    "status": unit.status,
                })
            elif category in ("hydro", "pumped hydro"):
                drawn["hydro"].append({
                    "code": unit.code,
                    "capacity_mw": capacity,
                    "fueltech_id": unit.fueltech,
                    "status": unit.status,
                })
            elif category in ("coal", "gas", "distillate"):
                drawn[category].append({
                    "code": unit.code,
                    "capacity_mw": capacity,
                    "fueltech_id": unit.fueltech,
                    "status": unit.status,
                })
        if drawn["battery"]:
            out["fleet"].append(_facility_entry(fac, drawn["battery"], "battery"))
        if drawn["renewable"]:
            out["renewables"].append(
                _facility_entry(fac, drawn["renewable"], "renewable")
            )
        # Hydro and the point sources are drawn at a coordinate; a plant
        # missing one is dropped rather than pinned to (0, 0). Batteries and
        # renewables are markers that do not need one to be placed.
        located = bool(
            fac.location and fac.location.get("lat") and fac.location.get("lng")
        )
        if drawn["hydro"] and located:
            entry = _facility_entry(fac, drawn["hydro"], "hydro")
            entry["pumped"] = fac.is_pumped
            out["hydro"].append(entry)
        for category in ("coal", "gas", "distillate"):
            if drawn[category] and located:
                out[category].append(_facility_entry(fac, drawn[category], category))
    _fleet_cache["mtime"] = mtime
    _fleet_cache["data"] = out
    return _fleet_cache["data"]


def _read_file(path, root):
    real = os.path.realpath(path)
    root = os.path.realpath(root)
    if not real.startswith(root + os.sep) and real != root:
        return None, 403
    if not os.path.isfile(real):
        return None, 404
    try:
        with open(real, "rb") as fh:
            return fh.read(), 200
    except OSError:
        return None, 404


def load_lines():
    """lines.geojson with each link's display name attached from the registry.

    The file holds only kind/id/geometry for links: the name is a link-registry
    fact (scrapers/links.py), attached here by id so the file and the registry
    cannot drift (docs/adr/0003). Features the registry does not know - Snowy
    and the schematic corridors - pass through with their file properties
    untouched.

    Returns (doc, ok): ok is True only when the file was read *and* parsed, so
    the caller can fall back to the generic file path - a missing, forbidden or
    corrupt lines.geojson then gets exactly the answer it got before names
    moved.
    """
    body, code = _read_file(os.path.join(DATA_DIR, "lines.geojson"), DATA_DIR)
    if code != 200:
        return None, False
    try:
        doc = json.loads(body)
    except ValueError:
        return None, False
    for feat in doc.get("features", []):
        link = links.LINKS.get(feat.get("properties", {}).get("id"))
        if link is not None:
            feat["properties"]["name"] = link.name
    return doc, True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the compose log quiet
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        # allow_nan=False: bare NaN/Infinity is not JSON, and the browser's
        # JSON.parse rejects the whole response. A 500 error body is better
        # than a 200 the map cannot read.
        self._send(code, json.dumps(obj, allow_nan=False),
                   "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            body, code = _read_file(os.path.join(STATIC_DIR, "index.html"), STATIC_DIR)
            if code == 200:
                self._send(200, body, STATIC_TYPES[".html"])
            else:
                self._send(404, "not found", "text/plain")
            return

        if path == "/data/live":
            # Optional historical snapshot: /data/live?t=<unix seconds> answers
            # with the fleet state as of that instant, using Prometheus's native
            # `time=` parameter. Missing/invalid `t` (or t in the future) keeps
            # the old "now" behaviour.
            at = None
            raw_t = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
            if raw_t:
                try:
                    t = float(raw_t)
                    if not math.isfinite(t):
                        raise ValueError("t must be finite")
                    if t > time.time() + 1:  # a little slack for clock skew
                        raise ValueError("t is in the future")
                    at = t
                except ValueError:
                    self._send_json({"error": "invalid t", "fetched_at": time.time()}, 400)
                    return
            try:
                self._send_json(build_live(at))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc), "fetched_at": time.time()}, 500)
            return

        if path == "/data/fleet.json":
            try:
                cache = os.environ.get("FLEET_CACHE", "/cache/fleet.json")
                body, code = _read_file(cache, os.path.dirname(cache))
                if code != 200:
                    self._send(404, "fleet cache not mounted", "text/plain")
                else:
                    self._send(200, body, STATIC_TYPES[".json"])
            except OSError:
                self._send(404, "fleet cache not mounted", "text/plain")
            return

        if path.startswith("/data/"):
            rel = path[len("/data/"):]
            body = None
            if rel == "lines.geojson":
                # The one geometry file whose link names are not in the file.
                doc, ok = load_lines()
                if ok:
                    body, code = json.dumps(doc), 200
            if body is None:
                # The generic path, also the fallback for a lines.geojson that
                # could not be read or parsed: same bytes, same 403/404, same
                # content type as before names moved to the registry.
                body, code = _read_file(os.path.join(DATA_DIR, rel), DATA_DIR)
            ctype = STATIC_TYPES.get(os.path.splitext(path)[1], "application/octet-stream")
            self._send(code, body or (b"not found" if code == 404 else b"forbidden"), ctype)
            return

        if path.startswith("/api/v1/query"):
            qs = urllib.parse.parse_qs(parsed.query)
            query = qs.get("query", [""])[0]
            at = None
            raw_t = qs.get("t", [""])[0]
            if raw_t:
                try:
                    at = float(raw_t)
                except ValueError:
                    self._send_json({"error": "invalid t"}, 400)
                    return
            try:
                self._send_json({"result": _query_prometheus(query, at=at)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, 502)
            return

        if path == "/healthz":
            self._send(200, "ok", "text/plain")
            return

        if path.startswith("/static/"):
            body, code = _read_file(os.path.join(STATIC_DIR, path[len("/static/"):]), STATIC_DIR)
            ctype = STATIC_TYPES.get(os.path.splitext(path)[1], "application/octet-stream")
            self._send(code, body or b"", ctype)
            return

        self._send(404, "not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-address", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9119)
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.listen_address, args.port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()