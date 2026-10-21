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
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    return out


_fleet_cache = {"mtime": None, "data": None}

# OpenNEM has no "pumped hydro" fueltech: pumped stations report `hydro`
# (+ a sibling `pumps` unit where the load DUID is in the fleet) and their
# facility description is free text. Classify a facility as pumped storage
# from any of three independent signals; otherwise draw it as classic hydro.
_PUMPED_DESC_RE = re.compile(r"pumped|pump[- ]?storage", re.I)

# Thermal generation, split into the two categories the map draws. Coal lumps
# black and brown together (the popup still breaks the grade down); gas is every
# gas-fired fueltech. `distillate` and the bioenergy fuels are deliberately not
# here: they are neither, and folding them in would misreport the capacity.
_COAL_FUELS = ("coal_black", "coal_brown")
_GAS_FUELS = ("gas_ccgt", "gas_ocgt", "gas_steam", "gas_recip", "gas_wcmg")


def _thermal_unit(unit, ft):
    """A drawable coal/gas unit, or None when it should be skipped. Retired
    units still carry a registered capacity but produce nothing, so they are
    excluded - the same rule the hydro bucket uses (e.g. the closed Hazelwood
    and Callide A blocks must not read as 1 GW of idle coal)."""
    if (unit.get("status_id") or "") == "retired":
        return None
    cap = unit.get("capacity_registered") or unit.get("capacity_maximum")
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
        return {
            "code": unit.get("code") or "",
            "capacity_mw": float(cap),
            "fueltech_id": ft,
            "status": unit.get("status_id") or "",
        }
    return None


def build_fleet():
    path = os.environ.get("FLEET_CACHE", "/cache/fleet.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    cached = _fleet_cache
    if cached["mtime"] == mtime:
        return cached["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh).get("data") or []
    except Exception:  # noqa: BLE001
        return []
    fleet = []
    renewables = []
    hydro = []
    coal = []
    gas = []
    for facility in raw:
        if not isinstance(facility, dict):
            continue
        units_bat = []
        units_ren = []
        units_hyd = []
        units_coal = []
        units_gas = []
        pumped_hint = False
        for unit in facility.get("units") or []:
            if not isinstance(unit, dict):
                continue
            ft = unit.get("fueltech_id") or ""
            if ft == "battery":
                capacity = unit.get("capacity_storage")
                if isinstance(capacity, (int, float)) and not isinstance(capacity, bool) and capacity > 0:
                    units_bat.append({
                        "code": unit.get("code") or "",
                        "capacity_mwh": float(capacity),
                        "registered_mw": unit.get("capacity_registered"),
                        "status": unit.get("status_id") or "",
                        "data_last_seen": unit.get("data_last_seen"),
                        "fueltech_id": "battery",
                    })
            elif ft in ("solar", "solar_utility", "wind", "wind_farm"):
                cap = unit.get("capacity_registered") or unit.get("capacity_maximum")
                if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
                    units_ren.append({
                        "code": unit.get("code") or "",
                        "capacity_mw": float(cap),
                        "fueltech_id": ft if ft in ("solar", "solar_utility", "wind") else ("solar" if "solar" in str(ft) else "wind"),
                        "status": unit.get("status_id") or "",
                    })
            elif ft in ("hydro", "pumps"):
                # pumped-hydro detection runs over every unit (a retired pump
                # DUID still identifies the scheme); retired units themselves
                # are dropped from the drawn capacity below.
                storage = unit.get("capacity_storage")
                if isinstance(storage, (int, float)) and not isinstance(storage, bool) and storage > 0:
                    pumped_hint = True
                if ft == "pumps":
                    pumped_hint = True
                if (unit.get("status_id") or "") != "retired":
                    cap = unit.get("capacity_registered") or unit.get("capacity_maximum")
                    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
                        units_hyd.append({
                            "code": unit.get("code") or "",
                            "capacity_mw": float(cap),
                            "fueltech_id": ft,
                            "status": unit.get("status_id") or "",
                        })
            elif ft in _COAL_FUELS:
                u = _thermal_unit(unit, ft)
                if u:
                    units_coal.append(u)
            elif ft in _GAS_FUELS:
                u = _thermal_unit(unit, ft)
                if u:
                    units_gas.append(u)
        if units_bat:
            fleet.append({
                "facility": facility.get("code") or "",
                "name": facility.get("facility_name") or facility.get("name") or "",
                "region": facility.get("network_region") or "",
                "location": facility.get("location"),
                "units": units_bat,
                "kind": "battery",
            })
        if units_ren:
            renewables.append({
                "facility": facility.get("code") or "",
                "name": facility.get("facility_name") or facility.get("name") or "",
                "region": facility.get("network_region") or "",
                "location": facility.get("location"),
                "units": units_ren,
                "kind": "renewable",
            })
        if units_hyd:
            loc = facility.get("location")
            if isinstance(loc, dict) and loc.get("lat") and loc.get("lng"):
                hydro.append({
                    "facility": facility.get("code") or "",
                    "name": facility.get("facility_name") or facility.get("name") or "",
                    "region": facility.get("network_region") or "",
                    "location": loc,
                    "units": units_hyd,
                    "kind": "hydro",
                    "pumped": bool(pumped_hint) or bool(_PUMPED_DESC_RE.search(str(facility.get("description") or ""))),
                })
        # Coal and gas are point sources like hydro, so they need a coordinate to
        # be drawn; a plant missing one is dropped rather than pinned to (0, 0).
        for bucket, units, kind in ((coal, units_coal, "coal"), (gas, units_gas, "gas")):
            loc = facility.get("location")
            if units and isinstance(loc, dict) and loc.get("lat") and loc.get("lng"):
                bucket.append({
                    "facility": facility.get("code") or "",
                    "name": facility.get("facility_name") or facility.get("name") or "",
                    "region": facility.get("network_region") or "",
                    "location": loc,
                    "units": units,
                    "kind": kind,
                })
    _fleet_cache["mtime"] = mtime
    _fleet_cache["data"] = {"fleet": fleet, "renewables": renewables, "hydro": hydro,
                            "coal": coal, "gas": gas}
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