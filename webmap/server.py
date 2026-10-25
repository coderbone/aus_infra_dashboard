#!/usr/bin/env python3
"""Nem Map server.

Serves the static web map, proxies live metric queries to Prometheus, and
aggregates them with the scraper fleet cache into one /data/live JSON blob the
frontend polls.

Routes:
  /                       html app
  /data/live              merged {interconnectors, regions, batteries, fleet}
  /data/fleet.json        raw scraper fleet cache (if the volume is mounted)
  /data/*.geojson         static region/line geometry
  /api/v1/query           raw Prometheus instant-query proxy (debugging)
  /healthz                tcp-friendly probe

Stdlib only; runs inside python:3.12-alpine.
"""

import argparse
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mounted from ../scrapers/fleet.py so all three readers of the fleet cache -
# this server and the two exporters - interpret it one way. See docs/adr/0002.
import fleet

STATIC_DIR = os.environ.get("WEBMAP_STATIC", os.path.join(os.path.dirname(__file__), "static"))
DATA_DIR = os.environ.get("WEBMAP_DATA", os.path.join(os.path.dirname(__file__), "data"))
PROMETHEUS_URL = os.environ.get("PROM_URL", "http://prometheus:9090")

# PromQL -> merge spec. Each field carries the label whose value identifies the
# series (key) and, for multi-valued series (e.g. curtailment per fuel), an
# extra label the value is filed under.
LIVE_QUERIES = {
    "interconnectors": {
        "flow": {"expr": "aemo_interconnector_flow_mw", "key": "interconnector"},
        "metered": {"expr": "aemo_interconnector_metered_flow_mw", "key": "interconnector"},
        "export_limit": {"expr": "aemo_interconnector_export_limit_mw", "key": "interconnector"},
        "import_limit": {"expr": "aemo_interconnector_import_limit_mw", "key": "interconnector"},
        "timestamp": {"expr": "aemo_interconnector_timestamp_seconds", "key": "interconnector"},
        "age": {"expr": "aemo_interconnector_age_seconds", "key": "interconnector"},
    },
    "regions": {
        "stored_mwh": {"expr": "aemo_battery_region_stored_mwh", "key": "region"},
        "fossil_mw": {"expr": "aemo_battery_region_fossil_generation_mw", "key": "region"},
        "rooftop_mw": {"expr": "aemo_battery_region_rooftop_solar_mw", "key": "region"},
        "import_mw": {"expr": "aemo_battery_region_import_mw", "key": "region"},
        "export_mw": {"expr": "aemo_battery_region_export_mw", "key": "region"},
        "curtail": {"expr": "aemo_battery_region_curtailment_mw", "key": "region", "extra": "fuel"},
    },
    "batteries": {
        # OE keeps exporting only fleet metadata; the per-unit SOC/power/stored
        # families it used to export were retired 2026-10-20 once AEMO coverage
        # (below) was verified a superset, so capacity is the only OE series here.
        "capacity_mwh": {"expr": "oe_battery_capacity_storage_mwh", "key": "unit"},
        # whole-fleet live coverage from the AEMO exporter's per-duid estimates
        "inf_stored_mwh": {"expr": "aemo_battery_inferred_stored_mwh", "key": "duid"},
        "inf_age_s": {"expr": "aemo_battery_inferred_age_seconds", "key": "duid"},
        "inf_timestamp": {"expr": "aemo_battery_inferred_timestamp_seconds", "key": "duid"},
        # peak stored energy over the commissioning window (recording rule), used
        # to shade a commissioning facility's progress arc
        "peak_stored_mwh": {"expr": "aemo_battery_peak_stored_mwh", "key": "duid"},
        # fresh 5-min SCADA power from AEMO
        "aemo_power_mw": {"expr": "aemo_battery_power_mw", "key": "duid"},
        "aemo_power_age_s": {"expr": "aemo_battery_power_age_seconds", "key": "duid"},
        "aemo_power_ts": {"expr": "aemo_battery_power_timestamp_seconds", "key": "duid"},
    },
    # Every dispatch unit (generators, loads, storage) with its live measured MW
    # and fueltech/region labels, keyed by DUID. This is what lets the map shade
    # each wind/solar marker by its own generation instead of a region estimate.
    "units": {
        "power_mw": {"expr": "aemo_unit_power_mw", "key": "duid"},
        "age_s": {"expr": "aemo_unit_power_age_seconds", "key": "duid"},
        "timestamp": {"expr": "aemo_unit_power_timestamp_seconds", "key": "duid"},
    },
    # Snowy Hydro reservoir levels, reported per reservoir label by the
    # reservoir-exporter (compose runs it with --all-reservoirs). Wrapped in
    # last_over_time because this job is scraped hourly while Prometheus's
    # default lookback delta is 5m: a bare selector is only answerable for the
    # five minutes after each scrape and would blank the card for most of the
    # hour. A 2h window survives a skipped scrape and still clears the card
    # within 2h if the exporter dies - generous is safe here because the
    # upstream publishes these levels once a day. An absent exporter yields an
    # empty result, not an error, so it never degrades the whole page.
    "reservoirs": {
        "level": {"expr": "last_over_time(snowy_tantangara_level_percent[2h])", "key": "reservoir"},
        "change_7d": {"expr": "last_over_time(snowy_tantangara_level_change_7d_percentage_points[2h])", "key": "reservoir"},
        "last_sample": {"expr": "last_over_time(snowy_tantangara_last_sample_timestamp_seconds[2h])", "key": "reservoir"},
    },
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
    obj = accum.setdefault(key, {})
    if spec.get("extra"):
        obj.setdefault("curtail", {})[labels[spec["extra"]]] = float(value)
    else:
        obj[field] = float(value)


def build_live(at: float | None = None):
    out = {"fetched_at": time.time(), "prometheus": "ok", "errors": [],
           "interconnectors": {}, "regions": {}, "batteries": {}, "units": {},
           "reservoirs": {}}
    if at is not None:
        out["at"] = at
    for group, fields in LIVE_QUERIES.items():
        for field, spec in fields.items():
            try:
                for series in _query_prometheus(spec["expr"], at=at):
                    _merge(out[group], field, spec, series.get("metric", {}), series.get("value", [None, None])[1])
            except Exception as exc:  # noqa: BLE001 - one bad metric must not sink the page
                out["prometheus"] = "degraded"
                out["errors"].append(f"{spec['expr']}: {exc}")
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
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

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
            body, code = _read_file(os.path.join(DATA_DIR, path[len("/data/"):]), DATA_DIR)
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