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
| `wht_tbm_scrape_success` | gauge | — | 1 only if *every* TBM was read; 0 on any per-TBM failure |
| `wht_tbm_config_stale` | gauge | — | 1 when serving the cached layer config; independent of scrape success |
| `wht_tbm_scrape_duration_seconds` | gauge | — | Wall time of the last scrape |
| `wht_tbm_last_scrape_timestamp_seconds` | gauge | — | Unix time of the last scrape |

Unknown values are exported as `NaN`.

`wht_tbm_info` is exported from a cache of the last successful read, so a TBM's
name and machine number stay visible through a failed poll. The measurement
gauges are *not* cached: a TBM that cannot be read simply loses its series for
that scrape, which is what makes the gap honest. `wht_tbm_scrape_success` is
still 0 in that case, so partial data is never mistaken for complete data.

## Upstream flakiness

The ArcGIS endpoints intermittently fail DNS resolution with
`socket.gaierror(-3)` (`EAI_AGAIN`, "Try again"). Historically this hit
**19 of 289 scrapes (6.6%)** over 24h, and because the old collector had no
per-TBM isolation a single lookup failure blanked *both* TBMs and dropped
`wht_tbm_info` with them.

Two mitigations, both on by default:

- **Retry with a time budget.** Transient failures (DNS `EAI_AGAIN`, resets,
  timeouts) are retried with exponential backoff plus jitter. The jitter matters
  as much as the retry: failures arrive in correlated bursts, so a flat delay
  just walks back into the same outage and lockstep retries pile both TBM layers
  onto the resolver at once. HTTP error responses are *not* retried — those are
  real answers, not faults.
- **Per-TBM isolation.** Each TBM is read independently, so one failure no
  longer takes out the other.

Both mitigations ship on by default, and the container also sets
`dns: [1.1.1.1, 9.9.9.9]` — the retry is the backstop, the resolver is the fix.
`--retry-budget` (default `6s`) caps the total time one request may spend
retrying, and is the knob that keeps this safe. A failing lookup on the default
Docker resolver takes ~5s to give up, so an uncapped loop measured **23.2s for a
single request** — two TBMs would run to ~47s and blow through Prometheus's 30s
`scrape_timeout`, converting a recoverable gap into a target marked down. With
the budget a scrape stays around 1-10s, and a live 21s scrape was observed
landing safely inside the timeout. Raise the budget only if `scrape_timeout` is
raised to match.

**Why the resolver is overridden.** The default resolver (Docker's embedded
`127.0.0.11` forwarding to systemd-resolved) intermittently gives up with
`EAI_AGAIN` after a full **5s**. Measured during a live failure window: default
`2/130` lookups failed at `~5005ms` each, `1.1.1.1` `0/141`, `9.9.9.9` `0/147`.

Worth knowing if this is ever re-litigated: comparing resolvers while DNS was
*healthy* makes the default look better than the public ones (2ms median vs
8-9ms) and argues for leaving it alone. That is measuring the wrong thing — a
good median hides the 5s cliff, and the cliff is what consumes the retry
budget. Measure failure latency during an outage, not success latency during
health. Post-change: 176 in-container lookups, 0 failures, 9ms median.


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
is reused before re-querying upstream, default 60), `--retries` (default 3),
`--retry-budget` (default 6s), `--once`, `-v`.

Standard library only — no third-party packages required.

## Tests

```bash
python3 -m unittest discover -s scrapers -t scrapers        # from the repo root
python3 -m unittest discover -s scrapers -t scrapers -v
./scrapers/test_wht_tbm_exporter.py                         # equivalent
```

136 tests, about 4 seconds, **no network access** — every upstream response is
replayed from `scrapers/fixtures/`, captured verbatim from ArcGIS on
2026-09-29. Verified offline by re-running with `socket.getaddrinfo` and every
non-loopback `socket.connect` blocked; the suite still passes. Nothing in the
suite reads `~/.cache/wht-tbm/config.json` or talks to caportal.com.au.

`-t scrapers` is required: `unittest discover` only accepts an importable start
directory, and `scrapers/` deliberately has no `__init__.py` so the exporter
stays a single bind-mounted file.

There is one test per failure mode, not just the happy path, because the
failure paths are the ones that page someone at 3am. In particular each of
these has a dedicated regression test:

- **ArcGIS returning HTTP 200 with an `{"error":...}` body** — see the
  gotcha in `NOTES.md`. Without the guard in `http_get_json` this silently
  produced a config with four `None` field names that was then cached.
- **A single TBM layer failing while the other succeeds** — the property the
  whole scrape design rests on: one bad layer must cost that machine's sample
  and nothing else.
- **Discovery failing while a valid config cache exists** — falls back and
  sets `wht_tbm_config_stale 1`; and failing with *no* cache — degrades to
  `wht_tbm_scrape_success 0` rather than a 500.
- **`wht_tbm_info` surviving a failed scrape** — static fleet metadata is
  carried over, because dropping it breaks joins on it.
- **The retry budget cutting in before the retry count**, and HTTP errors *not*
  being retried at all.
- **The desktop gauge target overriding the stale mobile `1500 m` reference** —
  `collect_gauge_config` deliberately walks `mobileView` *before* `desktopView`
  so the authoritative desktop value overwrites the mobile one; reversing the
  tuple silently poisons every target with 1500.

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

`scrape_timeout: 30s` is what the retry budget is sized against. If you raise
the timeout, `--retry-budget` can go up with it; if you lower it, lower the
budget too. `wht_tbm_scrape_duration_seconds` is the direct signal — sustained
values near the timeout mean the budget is too generous for your timeout.
