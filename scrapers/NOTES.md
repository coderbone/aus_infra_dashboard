# NOTES — WHTP2 TBM exporter

Working notes for whoever picks the scraper up next. Everything here is about
`wht_tbm_exporter.py` and the upstream data it reads; the Docker/Prometheus/
Grafana stack it runs in is documented one level up in `../README.md` and
`../NOTES.md`.

## Upstream data — durable facts

Source: <https://caportal.com.au/rms/wht/tbm-tracker> (Transport for NSW Western
Harbour Tunnel, WHTP2). Nothing is scraped from rendered HTML; the exporter
walks the public ArcGIS item graph instead.

1. Portal page → ArcGIS dashboard id `973e08367a544c879edd7f345f9d6a15`.
   **The page sits behind CloudFront and returns 403 to any request without a
   browser-like `User-Agent`.** That is the only reason the exporter sends one.
2. Dashboard item data → gauge widgets. Each has a `main` dataset pointing at a
   web map layer and a hard-coded `max`/`reference` static dataset. **That
   hard-coded number is the TBM's target distance** — it is not published
   anywhere else. Desktop view is authoritative; the mobile view still has a
   stale 1500 m and is only a fallback.
3. Web map item `644ba22abece4a08bd5795ec50160d50` → the two
   `TBM <name> Location` point layers (`Tunnel_Progress_m`, `Ring_Number`,
   `Timestamp`, `TBM`).
4. One `query` per layer, `orderByFields=Timestamp DESC`, limit 1.

| TBM | Layer feature service | Target (at 2026-09-26) |
| --- | --- | --- |
| Barangaroo (M110) | `utility.arcgis.com/usrsvcs/servers/b613c8229c174e429200155d5e829e11/rest/services/P_AU_WHTP2_TBM_Barangaroo_Location_PublicView/FeatureServer/0` | 1567 m |
| Patyegarang (M111) | `utility.arcgis.com/usrsvcs/servers/21ebd5cac3e3405cbce55a7703c65af0/rest/services/P_AU_WHTP2_TBM_Patyegarang_Location_PublicView/FeatureServer/0` | 1560 m |

Targets only change when someone edits the dashboard, so re-discovery picks up
new values automatically. Delete the cache file in the exporter container
(`/cache/config.json` on the `exporter-cache` volume) to force re-discovery; it
is only a fallback for when discovery fails.

## Exporter gotchas

- The default `--listen-address 127.0.0.1` only works when something else
  shares the exporter's network namespace. On a bridge network it must be
  `0.0.0.0`, otherwise "works on curl localhost, dead from the container".
- The first `/metrics` request after a start is cold: it does full discovery and
  takes ~5-9 s.
- Never run a second, unmanaged copy of the exporter alongside the stack — it
  scrapes the same upstream. The pre-compose `~/start_*.sh` scripts that did
  exactly that have been deleted; don't recreate them.
- Metrics are documented in `README.md` in this directory. Alert on
  `wht_tbm_scrape_success == 0` and on
  `time() - wht_tbm_last_report_timestamp_seconds`.
