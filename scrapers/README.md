# WHTP2 TBM tracker Prometheus exporter

`wht_tbm_exporter.py` publishes the tunnel boring machine (TBM) progress shown on
the Transport for NSW Western Harbour Tunnel TBM tracker
(<https://caportal.com.au/rms/wht/tbm-tracker>) as Prometheus metrics.

## How it gets the numbers

The portal page is a wrapper around an ArcGIS Dashboard. Nothing is scraped from
rendered HTML — the exporter walks the public ArcGIS item graph:

1. `GET https://caportal.com.au/rms/wht/tbm-tracker` → dashboard id `973e0836…`
   (this page sits behind CloudFront and returns 403 without a browser `User-Agent`).
2. Dashboard item data (`/sharing/rest/content/items/<id>/data`) → the gauge and
   indicator widgets. Each has a `main` dataset pointing at a web map layer plus a
   hard-coded `max`/`reference` dataset: **that number is the TBM's target distance.**
3. Web map item data → the `FeatureServer/0` URL behind each gauge's `layerId`
   (the `TBM <name> Location` point layers, `Tunnel_Progress_m` field).
4. One `query` per layer, ordered by `Timestamp DESC`, limit 1.

The desktop view is treated as authoritative for the target distance; the mobile
view still carries an older hard-coded reference (1500 m) and is only a fallback.
The resolved layer configuration is cached to `~/.cache/wht-tbm/config.json`
(`/cache/config.json` on the `exporter-cache` volume under compose), so a
CloudFront outage degrades to `wht_tbm_config_stale 1` rather than a dead target.

## Metrics

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `wht_tbm_info` | gauge | `tbm`, `name`, `machine` | Always 1; carries the TBM's display name and machine number |
| `wht_tbm_distance_excavated_m` | gauge | `tbm` | Metres driven so far |
| `wht_tbm_target_distance_m` | gauge | `tbm` | Total metres the TBM must drive |
| `wht_tbm_remaining_distance_m` | gauge | `tbm` | `target - distance` |
| `wht_tbm_progress_ratio` | gauge | `tbm` | `distance / target`, 0–1 |
| `wht_tbm_ring_number` | gauge | `tbm` | Tunnel ring currently being excavated |
| `wht_tbm_last_report_timestamp_seconds` | gauge | `tbm` | Unix time of the newest survey report |
| `wht_tbm_scrape_success` | gauge | — | 1 on success, 0 on failure |
| `wht_tbm_config_stale` | gauge | — | 1 when serving the cached layer config |
| `wht_tbm_scrape_duration_seconds` | gauge | — | Wall time of the last scrape |
| `wht_tbm_last_scrape_timestamp_seconds` | gauge | — | Unix time of the last scrape |

Unknown values are exported as `NaN`.

## Running

```bash
# one-shot exposition on stdout (exit 1 if the upstream scrape failed)
./wht_tbm_exporter.py --once

# long-running exporter
./wht_tbm_exporter.py --listen-address 127.0.0.1 --port 9109
curl -s localhost:9109/metrics
curl -s localhost:9109/healthz
```

Options: `--page-url`, `--cache-file`, `--timeout`, `--cache-ttl` (seconds a scrape
is reused before re-querying upstream, default 60), `--once`, `-v`.

Standard library only — no third-party packages required.

## How it is deployed here

In the normal setup this script is not run by hand: `../docker-compose.yml`
bind-mounts it read-only into `python:3.12-alpine` and Prometheus scrapes the
container over the compose network. See `../README.md` for that stack and
`../NOTES.md` for its history. The invocation below is only for running the
exporter outside compose.

## Standalone alternatives

### systemd

```ini
# /etc/systemd/system/wht-tbm-exporter.service
[Unit]
Description=WHTP2 TBM tracker Prometheus exporter
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/bone/src/monitoring/scrapers/wht_tbm_exporter.py \
    --listen-address 127.0.0.1 --port 9109
Restart=always
User=nobody

[Install]
WantedBy=multi-user.target
```

### Prometheus

```yaml
scrape_configs:
  - job_name: wht_tbm
    scrape_interval: 5m
    scrape_timeout: 30s
    static_configs:
      - targets: ['127.0.0.1:9109']
```

The upstream only publishes a survey line every few hours, so a 5 minute scrape
interval is plenty; alert on `wht_tbm_scrape_success == 0` and
`time() - wht_tbm_last_report_timestamp_seconds`. Note that a long
`scrape_interval` also delays the *first* scrape by up to a full interval, since
Prometheus jitters the initial one — keep it at 5m or less.
