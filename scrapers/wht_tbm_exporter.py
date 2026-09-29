#!/usr/bin/env python3
"""Prometheus exporter for the Transport for NSW Western Harbour Tunnel (WHTP2)
TBM Tracker, published at https://caportal.com.au/rms/wht/tbm-tracker

That page embeds an ArcGIS Dashboard. This exporter resolves the chain
    portal page -> ArcGIS Dashboard item -> web map -> per-TBM feature layer
by reading the public ArcGIS item metadata (no scraping of rendered HTML), then
queries each "TBM <name> Location" layer for its most recent record.

Exposed metrics
    wht_tbm_info{tbm,name,machine}                  1
    wht_tbm_distance_excavated_m{tbm}               metres driven
    wht_tbm_target_distance_m{tbm}                  metres to drive (dashboard gauge max)
    wht_tbm_remaining_distance_m{tbm}               target - distance
    wht_tbm_progress_ratio{tbm}                     distance / target
    wht_tbm_ring_number{tbm}                        tunnel ring the TBM is in
    wht_tbm_last_report_timestamp_seconds{tbm}      survey report time
    wht_tbm_scrape_success                          1/0
    wht_tbm_config_stale                            1 when serving cached layer config
    wht_tbm_scrape_duration_seconds                 last scrape wall time

wht_tbm_scrape_success is 0 if *any* TBM failed, so a partial scrape is still
reported as a failure. The gauges are then emitted only for the TBMs that did
answer, and wht_tbm_info is carried over from the last good scrape rather than
being dropped - it is static metadata, and its absence would break joins on it.

Usage
    wht_tbm_exporter.py                      # serve /metrics on 127.0.0.1:9109
    wht_tbm_exporter.py --once               # print exposition to stdout and exit
    wht_tbm_exporter.py --listen-address 0.0.0.0 --port 9109

Standard library only.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PAGE_URL = "https://caportal.com.au/rms/wht/tbm-tracker"
ARCGIS_ITEM_DATA = "https://www.arcgis.com/sharing/rest/content/items/{item_id}/data?f=json"
DASHBOARD_URL_RE = re.compile(r"arcgis\.com/apps/dashboards/([0-9a-f]{32})", re.I)

# CloudFront rejects requests without a browser-like User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

PROGRESS_FIELD_HINTS = ("tunnel_progress_m", "progress_m", "progress")
RING_FIELD_HINTS = ("ring_number", "ring")

log = logging.getLogger("wht_tbm_exporter")


class ScrapeError(Exception):
    """Raised when a required upstream request fails."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


# Errnos worth a second attempt. The one actually seen here is EAI_AGAIN (-3,
# "Try again") out of getaddrinfo: about 10% of lookups fail inside the
# container (Docker's embedded resolver at 127.0.0.11 forwarding to
# systemd-resolved), and one failed lookup used to cost the whole scrape.
# HTTP errors are deliberately absent - the server answered, so an immediate
# retry is unlikely to differ.
TRANSIENT_ERRNOS = frozenset(
    {
        socket.EAI_AGAIN,  # -3, only in socket, not errno
        errno.EAGAIN,
        errno.ECONNRESET,
        errno.ECONNABORTED,
    }
)


RETRY_BASE_DELAY = 0.5


def is_transient(reason: object) -> bool:
    """True for faults a second attempt a moment later can plausibly clear."""
    if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(reason, "errno", None) in TRANSIENT_ERRNOS


def http_get(url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0) -> bytes:
    """Fetch a URL, retrying transient faults.

    `retries` caps the attempt count and `retry_budget` caps the total time
    spent retrying. The budget is the one that matters: a failing lookup here
    takes ~5s to give up, so an uncapped retry loop can outlast Prometheus's
    `scrape_timeout` (30s) and turn a recoverable gap into a target marked
    down. Bounding by time keeps the failure local to the request.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-AU,en;q=0.9",
        },
    )
    started = time.monotonic()
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ScrapeError("GET %s -> HTTP %s" % (url, exc.code)) from exc
        except urllib.error.URLError as exc:
            spent = time.monotonic() - started
            if not is_transient(exc.reason):
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            if attempt >= retries:
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            if retry_budget and spent >= retry_budget:
                log.warning(
                    "giving up on %s after %.1fs of retry budget: %s",
                    url,
                    spent,
                    exc.reason,
                )
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            # Exponential backoff with jitter. The failures arrive in
            # correlated bursts - both TBM layers can fail every attempt
            # inside the same window - so a flat short delay just walks
            # into the same outage, and retrying in lockstep piles two
            # machines onto the resolver at once. Jitter de-synchronises
            # them.
            delay = RETRY_BASE_DELAY * (2**attempt)
            delay *= 0.75 + 0.5 * random.random()
            log.warning(
                "transient failure (attempt %d of %d) on %s: %s; retrying in %.1fs",
                attempt + 1,
                retries + 1,
                url,
                exc.reason,
                delay,
            )
            time.sleep(delay)
            continue
    raise ScrapeError("GET %s -> gave up after %d attempts" % (url, retries + 1))


def http_get_json(url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0) -> dict:
    payload = http_get(url, timeout, retries, retry_budget)
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise ScrapeError("GET %s -> invalid JSON" % url) from exc
    # ArcGIS answers HTTP 200 with an error body for things like a stale or
    # mistyped service id, so http_get's status check passes and callers read
    # absent fields as None instead of finding out. Raise here, where the
    # URL is still in scope, rather than letting a half-built config reach
    # the query step.
    if isinstance(data, dict) and "error" in data:
        raise ScrapeError("GET %s -> ArcGIS error: %s" % (url, data["error"]))
    return data


# --------------------------------------------------------------------------- #
# Discovery: portal page -> dashboard -> web map -> feature layers
# --------------------------------------------------------------------------- #


def layer_slug(layer_title: str) -> str:
    """'TBM Barangaroo Location' -> 'barangaroo'."""
    slug = re.sub(r"(?i)^tbm\s+", "", layer_title or "")
    slug = re.sub(r"(?i)\s+(location|progress(\s+line)?)\s*$", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    return slug or "unknown"


def widget_value_field(widget: dict) -> str | None:
    series = (widget.get("chartConfig") or {}).get("series") or []
    for entry in series:
        if entry.get("x"):
            return entry["x"]
    return widget.get("valueField")


def static_dataset_value(widget: dict, *names: str) -> float | None:
    for dataset in widget.get("datasets") or []:
        if dataset.get("type") not in ("staticDataset", "staticValues"):
            continue
        if names and dataset.get("name") not in names:
            continue
        values = ((dataset.get("data") or {}).get("values")) or []
        for value in values:
            if isinstance(value, dict):
                value = value.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def discover_dashboard_item_id(
    page_url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> str:
    page = http_get(page_url, timeout, retries, retry_budget).decode("utf-8", "replace")
    match = DASHBOARD_URL_RE.search(page)
    if not match:
        raise ScrapeError("no ArcGIS dashboard id found in %s" % page_url)
    return match.group(1)


def collect_gauge_config(dashboard: dict) -> dict[str, dict]:
    """Map web map layerId -> {target_m, field} from the gauge/indicator widgets.

    The desktop view is authoritative; the mobile view carries a stale
    hard-coded reference value, so it is only consulted as a fallback.
    """
    collected: dict[str, dict] = {}
    views = (dashboard.get("mobileView"), dashboard.get("desktopView"))
    for view in views:
        if not view:
            continue
        for widget in view.get("widgets") or []:
            if widget.get("type") not in ("gaugeWidget", "indicatorWidget"):
                continue
            datasets = widget.get("datasets") or []
            main = next(
                (d for d in datasets if d.get("name") == "main" and d.get("dataSource")),
                None,
            )
            if not main:
                continue
            layer_id = (main.get("dataSource") or {}).get("layerId")
            if not layer_id:
                continue
            entry = collected.setdefault(layer_id, {})
            target = static_dataset_value(widget, "max", "reference")
            field = widget_value_field(widget)
            if target is not None:
                entry["target_m"] = target
            if field:
                entry["field"] = field
    return collected


def web_map_layers(
    item_id: str, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> dict[str, dict]:
    web_map = http_get_json(
        ARCGIS_ITEM_DATA.format(item_id=item_id), timeout, retries, retry_budget
    )
    layers = {}
    for layer in web_map.get("operationalLayers") or []:
        if layer.get("id") and layer.get("url"):
            layers[layer["id"]] = {
                "title": layer.get("title") or layer["id"],
                "url": layer["url"].rstrip("/"),
            }
    return layers


def describe_layer(
    layer_url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> dict:
    meta = http_get_json(layer_url + "?f=json", timeout, retries, retry_budget)
    names = [field["name"] for field in meta.get("fields") or []]
    lower = {name.lower(): name for name in names}

    progress = next(
        (lower[hint] for hint in PROGRESS_FIELD_HINTS if hint in lower), None
    )
    ring = next((lower[hint] for hint in RING_FIELD_HINTS if hint in lower), None)
    timestamp = next(
        (
            field["name"]
            for field in meta.get("fields") or []
            if field.get("type") == "esriFieldTypeDate"
        ),
        None,
    )
    name = next(
        (
            field["name"]
            for field in meta.get("fields") or []
            if field.get("type") == "esriFieldTypeString" and field["name"].lower() == "tbm"
        ),
        None,
    ) or next(
        (
            field["name"]
            for field in meta.get("fields") or []
            if field.get("type") == "esriFieldTypeString"
        ),
        None,
    )
    return {
        "name_field": name,
        "progress_field": progress,
        "ring_field": ring,
        "timestamp_field": timestamp,
    }


def discover(
    page_url: str, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> dict:
    dashboard_id = discover_dashboard_item_id(page_url, timeout, retries, retry_budget)
    dashboard = http_get_json(
        ARCGIS_ITEM_DATA.format(item_id=dashboard_id), timeout, retries, retry_budget
    )
    log.info("dashboard item %s", dashboard_id)

    gauges = collect_gauge_config(dashboard)
    web_map_ids = [
        widget.get("itemId")
        for view in (dashboard.get("desktopView"), dashboard.get("mobileView"))
        if view
        for widget in view.get("widgets") or []
        if widget.get("type") == "mapWidget" and widget.get("itemId")
    ]
    if not web_map_ids:
        raise ScrapeError("dashboard %s has no map widget" % dashboard_id)

    layers: dict[str, dict] = {}
    for web_map_id in web_map_ids:
        for layer_id, layer in web_map_layers(
            web_map_id, timeout, retries, retry_budget
        ).items():
            if layer_id in gauges:
                layers[layer_id] = layer
    if not layers:
        raise ScrapeError("no TBM layers referenced by dashboard widgets")

    machines = {}
    for layer_id, layer in layers.items():
        gauge = gauges[layer_id]
        info = describe_layer(layer["url"], timeout, retries, retry_budget)
        slug = layer_slug(layer["title"])
        machines[slug] = {
            "tbm": slug,
            "layer_title": layer["title"],
            "layer_id": layer_id,
            "layer_url": layer["url"],
            "target_m": gauge.get("target_m"),
            "name_field": info["name_field"],
            "progress_field": gauge.get("field") or info["progress_field"],
            "ring_field": info["ring_field"],
            "timestamp_field": info["timestamp_field"],
        }
        if not machines[slug]["progress_field"]:
            raise ScrapeError("no progress field found for layer %r" % layer["title"])
        if machines[slug]["target_m"] is None:
            log.warning("no target distance published for %r", layer["title"])
    return {
        "discovered_at": int(time.time()),
        "page_url": page_url,
        "dashboard_id": dashboard_id,
        "machines": machines,
    }


def load_cache(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("config cache %s unusable: %s", path, exc)
        return None
    if not config.get("machines"):
        log.warning("config cache %s has no machines", path)
        return None
    return config


def save_cache(path: str, config: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write config cache %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #


def latest_record(
    machine: dict, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> dict:
    out_fields = [
        field
        for field in (
            machine["name_field"],
            machine["progress_field"],
            machine["ring_field"],
            machine["timestamp_field"],
        )
        if field
    ]
    params = {
        "where": "1=1",
        "outFields": ",".join(out_fields),
        "orderByFields": "%s DESC" % machine["timestamp_field"],
        "resultRecordCount": "1",
        "returnGeometry": "false",
        "f": "json",
    }
    url = machine["layer_url"] + "/query?" + urllib.parse.urlencode(params)
    data = http_get_json(url, timeout, retries, retry_budget)
    features = data.get("features") or []
    if not features:
        raise ScrapeError("layer %r returned no features" % machine["layer_id"])
    return features[0]["attributes"]


def machine_number(name: str) -> str:
    match = re.search(r"\(([^)]+)\)", name or "")
    return match.group(1) if match else ""


def collect(
    config: dict, timeout: float, retries: int = 0, retry_budget: float = 0.0
) -> tuple[list[dict], list[str]]:
    """Gather the latest record per TBM.

    Each machine is isolated: one failing layer costs that TBM's sample and no
    others. Returns the samples that did succeed plus a list of per-machine
    errors, so the caller can still serve partial data while reporting the
    scrape as failed.
    """
    samples: list[dict] = []
    errors: list[str] = []
    for slug, machine in sorted(config["machines"].items()):
        try:
            attributes = latest_record(machine, timeout, retries, retry_budget)
            name = str(attributes.get(machine["name_field"]) or machine["layer_title"])
            distance = attributes.get(machine["progress_field"])

            if distance is None:
                raise ScrapeError("null progress value for %r" % name)
            distance = float(distance)
            target = machine.get("target_m")
            if target:
                target = float(target)

            samples.append(
                {
                    "tbm": slug,
                    "name": name,
                    "machine": machine_number(name),
                    "distance_m": distance,
                    "target_m": target,
                    "remaining_m": (target - distance) if target else None,
                    "ratio": (distance / target) if target and target > 0 else None,
                    "ring": attributes.get(machine["ring_field"]) if machine["ring_field"] else None,
                    "reported_at": (
                        attributes[machine["timestamp_field"]] / 1000.0
                        if machine["timestamp_field"] and attributes.get(machine["timestamp_field"])
                        else None
                    ),
                }
            )
        except (ScrapeError, ValueError, KeyError, OSError) as exc:
            log.error("%s: %s", slug, exc)
            errors.append("%s: %s" % (slug, exc))
    return samples, errors



# --------------------------------------------------------------------------- #
# Prometheus exposition
# --------------------------------------------------------------------------- #


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt(value) -> str:
    if value is None:
        return "NaN"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "NaN" if math.isnan(number) else ("+Inf" if number > 0 else "-Inf")
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 4))


def render(
    samples: list[dict],
    identities: list[dict],
    success: bool,
    config_stale: bool,
    duration: float,
    generated: float,
) -> str:
    lines = [
        "# HELP wht_tbm_info Static attributes of a WHTP2 tunnel boring machine.",
        "# TYPE wht_tbm_info gauge",
    ]
    # Emitted for every known machine, not just the ones that answered this
    # scrape: these are static facts that do not go stale, and dropping them
    # because of a transient upstream fault breaks any join on them.
    for sample in identities:
        lines.append(
            'wht_tbm_info{tbm="%s",name="%s",machine="%s"} 1'
            % (
                escape_label(sample["tbm"]),
                escape_label(sample["name"]),
                escape_label(sample["machine"]),
            )
        )

    gauges = [
        ("wht_tbm_distance_excavated_m", "Distance driven by the TBM, in metres.", "distance_m"),
        ("wht_tbm_target_distance_m", "Total distance the TBM is required to drive, in metres.", "target_m"),
        ("wht_tbm_remaining_distance_m", "Distance still to be driven, in metres.", "remaining_m"),
        ("wht_tbm_progress_ratio", "Distance driven divided by the target distance.", "ratio"),
        ("wht_tbm_ring_number", "Tunnel ring the TBM is currently excavating.", "ring"),
        ("wht_tbm_last_report_timestamp_seconds", "Unix time of the most recent survey report.", "reported_at"),
    ]
    for metric, help_text, key in gauges:
        lines.append("# HELP %s %s" % (metric, help_text))
        lines.append("# TYPE %s gauge" % metric)
        for sample in samples:
            lines.append(
                '%s{tbm="%s"} %s'
                % (metric, escape_label(sample["tbm"]), fmt(sample[key]))
            )

    lines += [
        "# HELP wht_tbm_scrape_success Whether the last scrape of the TBM tracker succeeded.",
        "# TYPE wht_tbm_scrape_success gauge",
        "wht_tbm_scrape_success %d" % (1 if success else 0),
        "# HELP wht_tbm_config_stale Whether the served TBM configuration came from the local cache.",
        "# TYPE wht_tbm_config_stale gauge",
        "wht_tbm_config_stale %d" % (1 if config_stale else 0),
        "# HELP wht_tbm_scrape_duration_seconds Wall time of the last scrape.",
        "# TYPE wht_tbm_scrape_duration_seconds gauge",
        "wht_tbm_scrape_duration_seconds %s" % fmt(duration),
        "# HELP wht_tbm_last_scrape_timestamp_seconds Unix time of the last scrape.",
        "# TYPE wht_tbm_last_scrape_timestamp_seconds gauge",
        "wht_tbm_last_scrape_timestamp_seconds %s" % fmt(generated),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Scraper state
# --------------------------------------------------------------------------- #


class TbmScraper:
    def __init__(
        self,
        page_url: str,
        cache_file: str,
        timeout: float,
        ttl: float,
        retries: int = 0,
        retry_budget: float = 0.0,
    ) -> None:
        self.page_url = page_url
        self.cache_file = cache_file
        self.timeout = timeout
        self.ttl = ttl
        self.retries = retries
        self.retry_budget = retry_budget
        self._config: dict | None = None
        self._config_stale = False
        self._exposition: str | None = None
        self._health = (True, "")
        self._lock = threading.Lock()
        self._fetched_at = 0.0
        # slug -> {tbm, name, machine}, last seen good. Survives a failed
        # scrape so wht_tbm_info keeps describing the fleet.
        self._identities: dict[str, dict] = {}

    def config(self) -> dict:
        if self._config is not None:
            return self._config
        try:
            self._config = discover(
                self.page_url, self.timeout, self.retries, self.retry_budget
            )
            self._config_stale = False
            save_cache(self.cache_file, self._config)
        except ScrapeError as exc:
            log.error("discovery failed: %s", exc)
            cached = load_cache(self.cache_file)
            if cached is None:
                raise
            log.warning("falling back to cached configuration in %s", self.cache_file)
            self._config = cached
            self._config_stale = True
        return self._config

    def refresh(self) -> tuple[str, bool]:
        with self._lock:
            started = time.monotonic()
            samples: list[dict] = []
            errors: list[str] = []
            config: dict | None = None
            try:
                config = self.config()
                samples, errors = collect(
                    config, self.timeout, self.retries, self.retry_budget
                )
            except (ScrapeError, ValueError, KeyError) as exc:
                # Only discovery-level failures land here; a per-machine
                # failure is already isolated inside collect(). config() can
                # still re-raise when discovery fails with no cache at all, so
                # the exposition degrades to "no machines" rather than 500ing.
                log.error("scrape failed: %s", exc)
                errors = [str(exc)]
            for sample in samples:
                self._identities[sample["tbm"]] = {
                    "tbm": sample["tbm"],
                    "name": sample["name"],
                    "machine": sample["machine"],
                }
            if config is not None:
                # Drop identities for machines the config no longer lists. Skipped
                # when there is no config at all, so there is nothing to prune.
                known = set(config["machines"])
                for slug in set(self._identities) - known:
                    del self._identities[slug]
            success = not errors
            error = "; ".join(errors)
            duration = time.monotonic() - started
            self._fetched_at = time.time()
            self._exposition = render(
                samples,
                list(self._identities.values()),
                success,
                self._config_stale,
                duration,
                self._fetched_at,
            )
            self._health = (success, error)
            return self._exposition, success

    def exposition(self) -> tuple[str, bool]:
        with self._lock:
            fresh = self._exposition is not None and (time.time() - self._fetched_at) < self.ttl
        if not fresh:
            return self.refresh()
        with self._lock:
            return self._exposition, self._health[0]

    def health(self) -> tuple[bool, str]:
        self.exposition()
        with self._lock:
            return self._health


INDEX_BODY = """<!doctype html>
<title>WHTP2 TBM tracker</title>
<h1>Western Harbour Tunnel - TBM tracker</h1>
<ul>
  <li><a href="/metrics">/metrics</a></li>
  <li><a href="/healthz">/healthz</a></li>
</ul>
"""


def make_handler(scraper: TbmScraper):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            route = self.path.split("?", 1)[0].rstrip("/") or "/"
            if route in ("/", "/index.html"):
                self._respond(200, "text/html; charset=utf-8", INDEX_BODY.encode())
            elif route == "/metrics":
                body, _ = scraper.exposition()
                self._respond(200, "text/plain; version=0.0.4; charset=utf-8", body.encode())
            elif route == "/healthz":
                healthy, message = scraper.health()
                body = "ok\n" if healthy else "unavailable: %s\n" % message
                self._respond(200 if healthy else 503, "text/plain; charset=utf-8", body.encode())
            else:
                self._respond(404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, fmtstr: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmtstr % args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page-url", default=DEFAULT_PAGE_URL)
    parser.add_argument(
        "--cache-file",
        default=os.path.join(
            os.path.expanduser("~"), ".cache", "wht-tbm", "config.json"
        ),
        help="where the discovered layer configuration is cached",
    )
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9109)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="retries for transient upstream failures (DNS EAI_AGAIN, resets, timeouts)",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=6.0,
        help="seconds a single request may spend retrying before giving up; keeps a scrape under Prometheus scrape_timeout",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=60.0,
        help="seconds to reuse a scrape before re-querying upstream",
    )
    parser.add_argument("--once", action="store_true", help="print exposition and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    scraper = TbmScraper(
        args.page_url,
        args.cache_file,
        args.timeout,
        args.cache_ttl,
        args.retries,
        args.retry_budget,
    )

    if args.once:
        body, success = scraper.refresh()
        sys.stdout.write(body)
        return 0 if success else 1

    server = ThreadingHTTPServer((args.listen_address, args.port), make_handler(scraper))
    log.info(
        "serving http://%s:%d/metrics for %s", args.listen_address, args.port, args.page_url
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
