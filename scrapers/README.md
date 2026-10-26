# Prometheus exporters

Four independent, stdlib-only exporters live here. All are single files that
`docker-compose.yml` bind-mounts into `python:3.12-alpine`; none needs an image
build.

| Exporter | Upstream | Port | Metrics prefix |
| --- | --- | --- | --- |
| [`wht_tbm_exporter.py`](#whtp2-tbm-tracker-exporter) | Transport for NSW Western Harbour Tunnel TBM tracker (ArcGIS) | 9109 | `wht_tbm_` |
| [`snowy_tantangara_exporter.py`](#snowy-hydro-reservoir-levels-exporter) | Snowy Hydro scheme reservoir levels (`getData.php`) | 9110 | `snowy_tantangara_` |
| [`oe_battery_exporter.py`](#nembattery-state-of-charge-exporter) | OpenElectricity NEM battery storage (`api.openelectricity.org.au`) | 9111 | `oe_battery_`, `oe_batteries_`, `oe_api_`, `oe_poll_`, `oe_last_` |
| [`aemo_battery_exporter.py`](#aemo-reported-battery-energy-storage-exporter) | AEMO NEMWEB `Next_Day_Dispatch` (`nemweb.com.au`) | 9112 | `aemo_battery_` |

They share the retry-with-time-budget approach, the `--once` mode, the
`/metrics` + `/healthz` + index handler, and the "never serve a measurement you
did not just verify" rule (the two battery exporters add a background poll loop,
for upstreams far too big or too metered to fetch per scrape) — but they are
separate processes with separate upstreams and separate failure isolation.
`NOTES.md` has the upstream details for all four.

## WHTP2 TBM tracker exporter

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

136 tests in this file (738 for all four exporters), about 15 seconds, **no network
access** — every upstream response is
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

## Snowy Hydro reservoir levels exporter

`snowy_tantangara_exporter.py` publishes Snowy Hydro scheme reservoir levels,
focused on **Tantangara Reservoir** — the upper storage of the Snowy Hydro 2.0
pumped scheme.

> **This is water data, not construction progress.** Snowy Hydro 2.0 publishes no
> machine-readable project status of any kind: no progress dashboard, no TBM
> tracker, no progress layer. `NOTES.md` records the audit that established
> this. Tantangara drawdown is the closest live proxy that exists for "is the 2.0
> site active", because the 2.0 intake works are at the upper storage. Do not
> describe these series as project progress, and do not add a
> `snowy_tantangara_progress_*` metric on the strength of the name.

### How it gets the numbers

The chart on <https://www.snowyhydro.com.au/our-project/lake-levels/> is backed
by a plain PHP include on the same origin:

```
https://www.snowyhydro.com.au/wp-content/themes/snowyhydro/inc/getData.php
  ?yearA=<first year>&yearB=<last year>
```

One request returns the whole range as JSON, keyed
`year → snowyhydro → level[]`, one row per day. Each row carries a `lake[]`
array of `-name` / `-dataTimestamp` / `#text`, where **`#text` is percent of
gross storage**. No auth, no `Referer`, no rendered-HTML scraping.

The whole range is fetched in **one** request and reduced locally, so the 7-day
change and the year-to-date extremes cost no extra round trip. Two years are
requested by default because a single year cannot produce a 7-day delta for the
first week of January, and a metric that goes `NaN` exactly when the year rolls
over is a metric somebody pages about.

Three of the feed's properties drive real design decisions, all detailed in
`NOTES.md`:

- **Errors are HTTP 400 with a plain-text body**, not JSON. `http_get` folds that
  body into the exception message, so the two diagnostic strings
  (`must be valid integers`, `must be between 1954 and 2026`) survive into the
  log. This is the one place the exporter deliberately diverges from
  `wht_tbm_exporter.py`, which reports only the status code.
- **The year range is clamped server-side to `[1954, <current year>]`.** The year
  is derived from the clock on every scrape, never pinned, and read in Sydney
  time rather than UTC — otherwise 31 December afternoon UTC requests a year the
  endpoint has not opened yet.
- **`-dataTimestamp` is naive Sydney local time with no offset.** The exporter
  pins UTC+10 rather than using `zoneinfo`, because `python:3.12-alpine` ships no
  tzdata and `ZoneInfo("Australia/Sydney")` would raise there. The cost is at
  most one hour of error during daylight saving, which is irrelevant for a
  once-daily series whose timestamp exists only to feed
  `time() - last_sample_timestamp_seconds`.

### Metrics

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `snowy_tantangara_level_percent` | gauge | `reservoir` | Latest published level, **percent of gross storage** |
| `snowy_tantangara_level_change_7d_percentage_points` | gauge | `reservoir` | Change over the last 7 days, in **percentage points** |
| `snowy_tantangara_level_min_ytd_percent` | gauge | `reservoir` | Lowest level in the current calendar year |
| `snowy_tantangara_level_max_ytd_percent` | gauge | `reservoir` | Highest level in the current calendar year |
| `snowy_tantangara_last_sample_timestamp_seconds` | gauge | `reservoir` | Unix time of the observation behind the current reading |
| `snowy_tantangara_scrape_success` | gauge | — | 1 only if *every* requested reservoir was read |
| `snowy_tantangara_scrape_duration_seconds` | gauge | — | Wall time of the last scrape |
| `snowy_tantangara_last_scrape_timestamp_seconds` | gauge | — | Unix time of the last scrape |

Unknown values are exported as `NaN` — never `0`, because on a reservoir a zero
reads as "the lake is empty", which is a claim about the water rather than about
the scrape.

**The units are the trap.** The feed publishes *percent*, and the 7-day change is
a difference of two percents, i.e. *percentage points* — which is why the metric
name says `percentage_points` rather than `percent`. The metric never reports
metres: the upstream is percent only, no volume is published anywhere on this
endpoint, and the "−2.73 m/week" figure in the project's own copy is the same
2.73 read as percentage points.

**Only Tantangara is exported by default.** The feed also carries Lake Jindabyne
and Lake Eucumbene, which are pre-2.0 scheme infrastructure — scheme context, not
project status. Exposing them by default would dress them up as 2.0 data. Use
`--all-reservoirs` or a repeatable `--reservoir` to include them. **The compose
service does pass `--all-reservoirs`**, because the web map's "Snowy reservoirs"
card shows all three; that is the deployment's choice, not the exporter's
default, and the Grafana dashboard is unaffected because its `reservoir`
variable has `current` pinned to Tantangara (see NOTES.md).

**There is deliberately no `config_stale` metric and no persistent cache.** This
exporter has no discovery step and no configuration to cache, so a
`config_stale` gauge would have nothing to ever be `1`. The level gauges are
never served from a stored copy either: a feed that cannot be read loses the
series for that scrape, which is what makes the gap honest, and
`snowy_tantangara_scrape_success` is `0` throughout. That is also why the compose
service mounts no cache volume.

### Running

```bash
# one-shot exposition on stdout (exit 1 if the upstream scrape failed)
./snowy_tantangara_exporter.py --once

# long-running exporter
./snowy_tantangara_exporter.py --listen-address 127.0.0.1 --port 9110
curl -s localhost:9110/metrics
curl -s localhost:9110/healthz

# the two non-2.0 lakes as well
./snowy_tantangara_exporter.py --once --all-reservoirs
```

Options: `--data-url`, `--reservoir` (repeatable), `--all-reservoirs`,
`--change-days` (default 7, the window in Snowy Hydro's own weekly phrasing;
changing it renames the metric to match, so a `--change-days 14` run does not
silently keep writing under the `7d` name), `--years-back` (default 1, i.e.
current year plus the one before), `--listen-address`, `--port`,
`--timeout`, `--cache-ttl` (default 900s), `--retries` (default 3),
`--retry-budget` (default 6s), `--once`, `-v`.

`--cache-ttl` defaults high compared with the WHT exporter's 60s because this
feed publishes once a day. The TTL only guards against a burst of scrapes each
refetching ~190 KB; it is not a freshness guarantee.

Alert on `snowy_tantangara_scrape_success == 0` and on
`time() - snowy_tantangara_last_sample_timestamp_seconds`.

### Tests

```bash
python3 -m unittest discover -s scrapers -t scrapers    # all four, 726 tests
./scrapers/test_snowy_tantangara_exporter.py            # 121 tests, this one only
```

**121 tests, no network access** — every upstream response is
replayed from `scrapers/fixtures/`, sampled verbatim from `getData.php` on
2026-09-29. Verified offline by re-running with `socket.getaddrinfo` and every
non-loopback `socket.connect` blocked; the suite still passes.

Each of these has a dedicated regression test, because each is a way for this
endpoint to fail quietly:

- **HTTP 400 carrying a plain-text body** — the message must keep
  `must be between 1954 and 2026`, since that string is the entire diagnosis of a
  year-range bug.
- **Naive local timestamps not being shifted by the host timezone.** This was a
  real bug, caught by the suite: `datetime.timestamp()` on a naive value reads
  the *host's* local zone, so the same fixture produced a different instant on
  the AEST dev host than in the UTC container. The test now shifts the process
  timezone with `time.tzset()` and asserts the number does not move.
- **Year-to-date extremes not leaking across New Year.** A 2025 low must not
  become the 2026 minimum. The main fixture cannot catch this — its 2025 values
  sit inside the 2026 range — so the test builds a series where a leak would
  show.
- **The 7-day delta using date arithmetic, not a row offset.** With a missing
  day, an index-based delta would silently report a 6- or 8-day change as a
  7-day one. The fallback takes the newest reading *at or before* the target
  date, and `change_from` records which one was used.
- **A `NaN` change, not `0`, when there is no prior reading** — a zero would
  read as "the level has not moved", which is a claim about the reservoir.
- **A malformed payload not escaping as `AttributeError`**, and reservoir
  discovery failing without taking the exporter down.
- **`--change-days 14` renaming the metric**, so a reconfigured window cannot
  keep writing under the `7d` name.

## NEM battery registration metadata exporter

`oe_battery_exporter.py` publishes **registration metadata** for the NEM/WEM
battery fleet from **OpenElectricity** (formerly OpenNEM), the successor to
opennem.com.au. It is metadata-only by design: it makes a single `GET
/facilities/` request per refresh and exports the fleet's registered capacity,
unit identity and status, plus its own API-budget health. Nothing it publishes
moves minute to minute.

> **The API publishes no state of charge.** There is no SOC metric, no
> percentage, and no full-charge flag anywhere in the OpenElectricity data model.
> Every percentage on the dashboard comes from the **AEMO** exporter
> (`aemo_battery_*`), which combines a daily reported `ENERGY_STORAGE` anchor with
> live 5-minute power. This exporter also used to fetch `storage_battery` and
> `power` per facility and derive its own `oe_battery_soc_ratio` the same way; the
> per-facility fetch and **every metric derived from it — SOC, stored energy,
> power, the inferred/SOC estimate, efficiency calibration, liveness and the
> request-window machinery — were retired on 2026-10-20**. See
> [Metrics](#metrics) for what remains.

### How it gets the numbers

One endpoint for the data, one for the budget:

1. `GET /facilities/` — the fleet metadata for the **whole** NEM (batteries and
   everything else), each unit's `fueltech_id`, `network_region`,
   `capacity_storage`, `capacity_registered`, `status_id` and `data_last_seen`.
   Read once per `--fleet-refresh-interval` (24h) and cached to disk as a
   fallback. Despite being unfiltered this is one request (~1.3 MB, ~631
   facilities / 1142 units, no pagination) — the battery rows feed this
   exporter, and the file is *also* read by the AEMO exporter to attribute its
   measured SCADA MW, which needs the fueltech and region of every unit. So the
   request is deliberately **not** filtered by `?fueltech_id=battery`: doing so
   would strip the non-battery rows AEMO needs.
2. `GET /me` — the free daily credit balance, read once per cycle to keep the
   budget visible. A failure here costs a gauge, not the scrape.

That is **two requests a day** against the free Community plan's 366/day bucket,
which is why the poll interval can be relaxed and why Prometheus can scrape the
exporter as often as it likes at zero additional credit cost.

### Why the poll loop is not the scrape handler

The free Community plan allows 500 credits/day but rate-limits by bucket:
**8 requests / 5 min, 32 / 1 h, 366 / 1 day**. Metadata-only polling costs one
request per cycle, so scrape-driven polling would still be arithmetically fine —
but there is no reason to re-read a year-static fleet every scrape. So the
exporter **polls on its own schedule** and serves the last completed cycle to
Prometheus; the scrape interval is independent and free.

Consequence worth knowing: a stopped poll loop leaves the capacity gauges showing
the last good metadata forever. That is what `oe_last_poll_timestamp_seconds`
and `oe_last_fleet_refresh_timestamp_seconds` are for.

### The upstream quirks that shaped it

1. **The largest batteries in the country are unbuilt.** Of the ten
   highest-capacity units, seven are `committed` and have never dispatched —
   Richmond Valley (2200 MWh, the largest in Australia), Tomago (2000),
   Baranduda (1886), Wooreen, Elaine, Western Downs 3, Supernode 3. They have no
   `data_last_seen`. The numeric capacity series skip them (`require_data`,
   default on); `oe_battery_fleet_info` and
   `oe_battery_fleet_capacity_storage_mwh` cover *all* of them, because
   describing the units the live feed never mentions is the whole point of the
   info metric. `--include-undispatched` widens the numeric series to match.
   `commissioning` is *kept* — Eraring 2 and Collie 2 are both commissioning and
   both publish real data.
2. **`capacity_storage` is a *unit* field, not a facility field.** Reading it
   off the facility yields no denominator at all. A unit with no positive
   `capacity_storage` yields no numeric row; a battery data facility returns
   `<CODE>1`, `<CODE>G1` and `<CODE>L1`, and only `<CODE>1` carries the
   capacity.
3. **Facilities come and go, and span two networks.** Ten of the battery
   facilities are WEM and `COLLIE_BESS2` 404s when queried as NEM. The network is
   carried per row from `network_id` rather than assumed; in this metadata-only
   exporter that is only a label distinction, but the AEMO exporter reads the
   same file and relies on it.

Plus one that is not about the data: **the API rejects the default
`Python-urllib` User-Agent with a bare HTTP 403** and no body. `USER_AGENT` is
explicit and must not be dropped. Verified 2026-09-30: `Python-urllib/3.12` →
403, `curl/8.5.0` → 200, `python-requests/2.31` → 200.

### Metrics

Per-unit, all carrying the same five labels —
`{facility, unit, name, region, status}` — so one Grafana variable selects the
same units everywhere and the table can join on `unit`:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_battery_capacity_storage_mwh` | gauge | Registered storage capacity from metadata, for units with data (or all, with `--include-undispatched`) |
| `oe_battery_fleet_capacity_storage_mwh` | gauge | Registered storage capacity in MWh for **every** unit in the fleet metadata — the numeric operand the info gauge's capacity label cannot be, so any unit's % of capacity can be computed (`energy / on(unit) this series`) |

A labelled **info** gauge accompanies the whole fleet, value 1 with the data in
the labels, and covers *every* battery unit in the metadata — committed and
never-built units included:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_battery_fleet_info{unit, facility, facility_name, region, network, status, capacity_storage_mwh, capacity_registered_mw, data_last_seen}` | gauge | Registration metadata for one unit. `unit` is the fleet unit code and the join key: AEMO per-unit dispatch series carry it too, so `aemo_battery_power_mw * on(unit) group_left(region) oe_battery_fleet_info` enriches them with region/network/status with no lookup table. `duid` was dropped once every consumer had moved across to `unit` — on this metric it held the fleet's spelling while on the AEMO side it held AEMO's, so the same name meant two things across the join. Capacity figures ride as label strings for display and filtering; `data_last_seen` is empty for units that have never published. Absent entirely until the fleet metadata is first read. |

Unlabelled fleet and poll gauges:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_battery_fleet_capacity_mwh` | gauge | Capacity of the whole enumerated fleet (units with data) |
| `oe_last_poll_timestamp_seconds` | gauge | When the last cycle finished |
| `oe_last_fleet_refresh_timestamp_seconds` | gauge | When the metadata was last read |
| `oe_api_credits_remaining` | gauge | Daily balance, from the free `/me` |
| `oe_api_requests_total` | counter | Requests **issued**, failures included |

**Retired on 2026-10-20.** The live SOC, stored energy, power, their timestamps
and ages, the anchor gauges, the whole inferred/SOC estimate and its calibration
metrics, the per-unit `oe_battery_scrape_success` /
`oe_battery_capacity_rank` / `oe_battery_idle_seconds`, the scope counters
(`oe_batteries_enumerated`, `_in_scope`, `_demoted`, `_monitored`,
`oe_batteries_inferred`), `oe_battery_monitored_capacity_mwh`,
`oe_battery_series_without_capacity`, `oe_battery_series_too_stale`,
`oe_battery_reading_from_cache` and `oe_poll_cycle_duration_seconds` are all
gone. AEMO now publishes the live numbers
(`aemo_battery_power_mw`, `aemo_battery_energy_stored_mwh`,
`aemo_battery_inferred_stored_mwh` and their age metrics) for the same units at
5-minute resolution, and a second slower source for the same quantity is worse
than a gap. Nothing emits the retired names and no dashboard or webmap query
references them.

### Running

```bash
# one-shot exposition on stdout (exit 1 if the poll failed)
OPENEA_API_KEY=… ./oe_battery_exporter.py --once

# long-running exporter, refreshing metadata daily
OPENEA_API_KEY=… ./oe_battery_exporter.py \
    --listen-address 127.0.0.1 --port 9111 --poll-interval=86400 \
    --fleet-cache-file=/cache/fleet.json
curl -s localhost:9111/metrics
curl -s localhost:9111/healthz
```

Options: `--api-key-env` (default `OPENEA_API_KEY`) and `--api-key-file` (mutually
exclusive), `--poll-interval` (default 86400s),
`--fleet-refresh-interval` (default 86400s), `--fleet-cache-file`,
`--include-undispatched`,
`--listen-address`, `--port` (9111), `--timeout`, `--retries` (3),
`--retry-budget` (6s), `--once`, `-v`.

The key is read from the environment or a file, **never from argv**, so it cannot
land in the process table, in shell history, or in a `docker inspect` command
line. Do not add a `--api-key` flag to "make it easier".

`--fleet-cache-file` writes `fleet.json` atomically after every clean poll. In
this exporter it is a *fallback*: it is only read when the API cannot be reached,
and its `cached_at` stamp is carried over as the refresh time so a stale cache is
replaced as soon as the API answers. **Its on-disk shape
(`{"cached_at", "data": [...]}`, the raw unfiltered facilities JSON) is a shared
interface**: the AEMO exporter reads the same file directly and needs every
unit's fueltech and region, so it must not be narrowed to battery rows or
re-shaped.

### Tests

```bash
python3 -m unittest discover -s scrapers -t scrapers    # all exporters
./scrapers/test_oe_battery_exporter.py                  # this one only
```

**No network access** — the fleet metadata, a real Eraring storage response, an
empty response and a PII-redacted `/me` are replayed from `scrapers/fixtures/`,
captured verbatim from the API on 2026-09-30. (The retired per-facility fixtures
remain on disk but are no longer read.)

The regressions worth naming, because each is a quiet failure:

- **The 403 User-Agent rejection** raising `AuthError` rather than a generic
  scrape error, and HTTP 401/403 never being retried.
- **`capacity_storage` being a *unit* field, not a facility field** — reading it
  off the facility yields no denominator and silently drops every capacity row.
- **A committed/unreported facility counting as a fleet *observation*** — the
  largest units in the country are unbuilt, so the numeric series must gate on
  `data_last_seen` while the info metric must *not*.
- **A stale cache pinning the fleet forever** — the cache is a fallback, and the
  daily metadata refresh has to still happen once a file exists.
- **The `/facilities/` request being fueltech-filtered**, which would silently
  strip the non-battery rows the AEMO exporter reads from the same cache.
- **WEM metadata losing its `network` label**, which the shared cache relies on.
- **A 429/5xx transient being retried inside the time budget, and a 404 not
  being retried at all** — an absent facility is an answer, not a fault.
- **A retired family creeping back into the exposition** — pinned by a
  `RETIRED_METRICS` list, because a stale duplicate is worse than a gap.
## AEMO reported battery energy storage exporter

Two feeds from AEMO's NEMWEB archive, both free and anonymous, exposing
`aemo_battery_` metrics:

- **Daily** — `Next_Day_Dispatch`, the file the NEM batteries report against,
  per-DUID measured `ENERGY_STORAGE`/`INITIAL_ENERGY_STORAGE` and the dispatched
  power `TOTALCLEARED` (negative = charging). It states the *next day's*
  dispatch, so the values are the quoted battery energy levels for the current
  day, published the previous evening; use the freshness metrics, not the scrape
  timestamp, to judge age.
- **Intraday** — `Dispatch_SCADA` (measured output MW per unit, every 5-minute
  interval) and `DispatchIS_Reports` (region-level `BDU_ENERGY_STORAGE`, the
  `SS_*` curtailment columns, and the `DISPATCH,INTERCONNECTORRES` flow/limit
  table, every 5-minute interval). These are the feeds that move a
  charge/discharge chart through the day. There is **no** public per-DUID live
  SOC: the daily report is the only per-unit storage, and AEMO's only live
  *measured* storage number is the region-level BDU figure. The same two files
  also yield region curtailment (wind and solar as separate series), region
  fossil generation (joined against the OE fleet cache), and interconnector
  transfer flows and limits.

### How it gets the numbers — the daily file

1. `https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/` lists the current
   day's files. Each file is `PUBLIC_NEXT_DAY_DISPATCH_YYYYMMDD_000000NNN` in a
   same-named `.zip`; the newest is the lexicographic maximum of the listing
   (verified against live data — at browse time the archive had ~790 entries and
   the max was indeed the newest, including same-day corrected re-publishes with
   a larger sequence number).
2. The zip contains one `<name>.CSV` — a full day is ~126MB, so it is read
   streaming (header preview to choose the `UNIT_SOLUTION` table, then
   `csv.reader`), never loaded whole. Rows that don't parse are counted and
   warned, and a subsequent cycle with the same file skips the download because
   the state file says it already has this one.
3. The table's columns that matter: `SETTLEMENTDATE` (idx 4), `DUID` (idx 6),
   `TOTALCLEARED` (idx 14, cleared dispatch MW; negative = charging),
   `INITIAL_ENERGY_STORAGE` (idx 69), `ENERGY_STORAGE` (idx 70), both MWh. There
   are 288 intervals/day and ~67 battery storage units (a few report 287 of 288).
   Each unit is reported at its newest available interval. All timestamps are
   AEST, pinned to UTC+10 (AEMO publishes no DST and nothing corrects for it).

### How it gets the numbers — the intraday live feed

A second thread polls two tiny archives on `--intraday-poll-interval` (default
300s), which is what tracks the 5-minute dispatch intervals:

1. `https://nemweb.com.au/Reports/Current/Dispatch_SCADA/` lists
   `PUBLIC_DISPATCHSCADA_<YYYYMMDDHHMM>_<seq>.zip`, a ~4KB single-interval
   `SCADAVALUE` in MW (negative = charging/absorbing). Verified live
   2026-10-08 15:20: ERB01 −120, WTAHB1 −34, LIMBESS1 +0.9. Every row is
   published as `aemo_unit_power_mw{unit,duid,fueltech,region}` — generators, loads
   and storage alike, labelled from the OE fleet cache (empty labels for a unit
   the cache does not know). `unit` is the fleet unit code, `duid` is AEMO's
   name for the same unit; they differ for a handful of units (Tailem Bend 3
   is `TBBESS3B1`/`TB3B1`). The legacy `aemo_battery_power_mw` series stays
   filtered to the units the daily report has learned are storage units (the
   battery set is seeded from the state file at startup so a restart does not
   wait for the daily poll); a DUID the daily file has never clocked as storage
   is absent from the battery series but still present in the unit series. The
   unit series is the migration target for consumers of the battery one.
2. `https://nemweb.com.au/Reports/Current/DispatchIS_Reports/` lists
   `PUBLIC_DISPATCHIS_<YYYYMMDDHHMM>_<seq>.zip`, a ~23KB interim-solution
   snapshot whose `DISPATCH,REGIONSUM` table carries `BDU_INITIAL_ENERGY_STORAGE`
   (idx 129) and `BDU_ENERGY_STORAGE` (idx 124) — the reported aggregate stored
   MWh of all battery dispatch units per region (NSW1 5063.4 → 5084.6 MWh at
   15:20). Regions with no storage publish blanks (TAS1) and are skipped *for
   storage*, but the same table is the source of the curtailment series below.
3. The file's other table, `DISPATCH,INTERCONNECTORRES`, carries per link and
   interval the settled flow `MWFLOW`/`METEREDMWFLOW`, the dispatch engine's
   `EXPORTLIMIT`/`IMPORTLIMIT`, losses, marginal value and violation degree
   (verified 2026-10-11: NSW1-QLD1 flow −926.7 MW, VIC1-NSW1 binding on its
   690.8 MW export limit, T-V-MNSP1 reporting *both* limits negative). AEMO
   settles flow under its own per-link sign convention, so every column is
   carried verbatim and never normalized — a negative limit is AEMO's own
   number, not a sign error.

Both files are one interval each and advance every 5 minutes, so a cycle costs
two tiny listings plus ~30KB of zips; the reuse check absorbs a poll that lands
inside the same interval (the newest file is unchanged, so it is served without
a re-download). The two sides fail independently — `aemo_battery_scada_success`
and `aemo_battery_dispatchis_success` — and a failed side drops its own series
rather than repeating the last one, the same rule as the daily file.

### Curtailment (from `DISPATCH,REGIONSUM`)

The same region rows carry AEMO's own self-forecast and cleared MW for
semi-scheduled plant in four columns: `SS_WIND_UIGF` (idx 110),
`SS_SOLAR_UIGF` (idx 109), `SS_WIND_CLEAREDMW` (idx 112) and
`SS_SOLAR_CLEAREDMW` (idx 111). Curtailment is the gap the region did not get to
send — `UIGF − CLEAREDMW` per fuel, **floored at 0** because a unit beating its
own forecast can push the raw gap negative. The region map is built even when
the storage columns are blank, so TAS1 curtails even though it has no storage
row; when the four columns are absent entirely the curtailment map is simply
empty. Slack: real captures on 2026-10-11 14:20 read VIC1 solar 1641.0 → 227.9
curtailed (1413.2 MW) and SA1 wind 2119.3 → 527.0 (1592.3 MW).

### Fossil generation

AEMO publishes no per-region fossil feed (`Next_Day_Actual_Gen` is per-DUID
MWh, `Regional_Summary_Report` is weekly settlements, `DUDETAILSUMMARY` carries
no fuel type), so the exporter gets fuel from the OpenElectricity fleet cache
(the same `--fleet-cache-file` the inference clamp reads): the OE exporter now
caches the *whole* NEM facility list, and the aemo side reads each unit's
`fueltech_id` + `network_region`. Units whose fueltech is
`coal_black`/`coal_brown`/`distillate`/`gas_ccgt`/`gas_ocgt`/`gas_steam`/
`gas_recip`/`gas_wcmg` are summed per region against the live
`Dispatch_SCADA` MW. Bioenergy and hydro are excluded. With no cache readable,
`aemo_battery_region_fossil_generation_mw` is omitted entirely (never a wrong
zero). The sums are computed on the *unfiltered* SCADA units, before the
battery filter, and are not recomputed on a reused poll — otherwise fossil
would vanish on every other cycle. The cache is re-read while either map is
still empty (not just the capacity map), so a cache written before this feature
- battery capacity but no fossil units - is superseded on the OE exporter's
next in-place refresh instead of pinning the fossil map empty.

### Interconnector transfer flows and limits

The `aemo_interconnector_*` series come from the `DISPATCH,INTERCONNECTORRES`
table in the *same* `DispatchIS_Reports` zip already downloaded for region
storage, parsed in-memory from the same bytes — transmission telemetry costs no
second request. It covers the seven interconnectors plus the MNSP links. AEMO
applies its own per-interconnector sign convention to `MWFLOW` and reports
`EXPORTLIMIT`/`IMPORTLIMIT` that are negative for some links (T-V-MNSP1 reports
both), so the columns are carried verbatim, never re-derived or mirrored; a link
"binding on export with a negative import limit" (VIC1-NSW1, MWFLOW ==
EXPORTLIMIT) stays visible as-is. The table fails independently of region
storage: a file without it keeps the region series and publishes the
interconnector set empty, made explicit by `aemo_interconnector_count 0` and its
`report_info`.

### Freshness and the poll loop

The daily value changes once a day, and the file is large, so the exporter
background-polls (hourly by default, `--poll-interval`) and only downloads a zip
when the listing shows a new file; otherwise it re-serves the last parsed report
(`aemo_battery_report_reused=1` when the answer came from the state-file cache).
Like the sibling exporters, a failed poll publishes an empty report, so a series
is dropped rather than frozen on stale data — a dead feed loses its series. The
intraday feed is the inverse: small files, so it polls every 5 minutes and its
series are genuinely live (age = the 5-minute interval behind the reading).

### The inferred stored-MWh series

No public per-DUID live SOC exists, so the intraday poller builds one: each
storage unit is anchored at the daily report's `ENERGY_STORAGE` and the measured
`Dispatch_SCADA` MW is integrated across every 5-minute interval
(`charge_efficiency×|MW|×dt` while charging, `MW×dt` while discharging). The
arithmetic and its policy live in the pure `inference` module
(`scrapers/inference.py`, see `docs/adr/0004`); the poller supplies the anchor,
the measured steps and the impure parts (archive backfill, state file, the
live-step guard). The SoC policy's values — the bridge-gap limit and the fitted
charging share — are stated once in `scrapers/soc_policy.py` (see
`docs/adr/0005`), which the exporter's defaults take and the dashboard's panel
descriptions quote; `--infer-charge-efficiency` still overrides the share for a
deployment that fits its own. The raw
integral is clamped into `[0, capacity]`: the zero floor always applies, and the
100% ceiling applies wherever `--fleet-cache-file` supplies the unit's
registered storage capacity (the OE exporter's cache). `--no-infer-clamp` serves
the raw drift instead. `aemo_battery_infer_success` reports whether the series is
actually being served, and `aemo_battery_inferred_anchor_age_seconds` the age of
the daily anchor — let them drop/rise and the estimate is stale.

**A stale anchor is never stamped forward.** A restart mid-day re-anchors at the
04:00 daily value. Bridging that gap from the live feed in *one* step would
claim a five-hour-old measurement was current, so the poller instead rebuilds
the gap one 5-minute interval at a time from the archived files on
`https://nemweb.com.au/Reports/Current/Dispatch_SCADA/` (that listing keeps
current-day files). When the archive does not reach back far enough, or backfill
is disabled (`--no-infer-backfill`), the series is served *absent* — never a
wrong number — until a fresh daily report re-anchors it.

### Metrics

| Metric | Meaning |
| --- | --- |
| `aemo_battery_energy_stored_mwh{duid}` | battery energy storage from `ENERGY_STORAGE`, MWh (daily) |
| `aemo_battery_initial_energy_stored_mwh{duid}` | from `INITIAL_ENERGY_STORAGE`, MWh (daily) |
| `aemo_battery_total_cleared_mw{duid}` | cleared dispatch for the newest interval, MW (negative = charging/absorbing; daily) |
| `aemo_battery_last_sample_timestamp_seconds{duid}` | SETTLEMENTDATE AEST→UTC of the reported interval |
| `aemo_battery_sample_age_seconds{duid}` | now − last_sample (age of the reading, *not* the scrape) |
| `aemo_battery_report_info{file}` | filename, values 1; `report_generated_timestamp_seconds`, `report_generated_age_seconds`, `report_newest_interval_timestamp_seconds` give the file's own timestamps |
| `aemo_battery_units_reporting` | units in the report |
| `aemo_battery_report_reused` | 1 if served from the state-file cache this poll |
| `aemo_battery_scrape_success` | 1 on success, 0 on failure (daily only) |
| `aemo_battery_poll_duration_seconds`, `aemo_battery_last_poll_timestamp_seconds` | poll timing |
| `aemo_battery_power_mw{unit,duid}` | live measured output from `UNIT_SCADA` `SCADAVALUE`, MW (negative = charging). Legacy storage-filtered series; superseded by `aemo_unit_power_mw` |
| `aemo_battery_power_timestamp_seconds{unit,duid}` | the 5-minute interval behind the power reading |
| `aemo_battery_power_age_seconds{unit,duid}` | its age at export time |
| `aemo_battery_power_report_info{file}` | the processed `PUBLIC_DISPATCHSCADA` file |
| `aemo_battery_power_interval_timestamp_seconds` | its interval |
| `aemo_battery_units_with_power` | storage units with a reading in the SCADA snapshot |
| `aemo_unit_power_mw{unit,duid,fueltech,region}` | live measured output from `UNIT_SCADA` `SCADAVALUE`, MW, for **every** dispatch unit in the file (generators, loads, storage; negative = charging/absorbing). Superset of `aemo_battery_power_mw`; `fueltech`/`region` come from the OE fleet cache (empty when a unit is absent from it) |
| `aemo_unit_power_timestamp_seconds{unit,duid}` | the 5-minute interval behind the unit reading |
| `aemo_unit_power_age_seconds{unit,duid}` | its age at export time |
| `aemo_unit_power_report_info{file}` | the processed `PUBLIC_DISPATCHSCADA` file |
| `aemo_unit_power_interval_timestamp_seconds` | its interval |
| `aemo_unit_power_units` | dispatch units with a reading in the SCADA snapshot |
| `aemo_battery_region_stored_mwh{region}` | live region aggregate from `BDU_ENERGY_STORAGE`, MWh |
| `aemo_battery_region_initial_stored_mwh{region}` | from `BDU_INITIAL_ENERGY_STORAGE`, MWh |
| `aemo_battery_region_storage_timestamp_seconds{region}` | the 5-minute interval behind the region reading |
| `aemo_battery_region_storage_report_info{file}` | the processed `PUBLIC_DISPATCHIS` file |
| `aemo_battery_region_storage_interval_timestamp_seconds` | its interval |
| `aemo_battery_region_curtailment_mw{region,fuel}` | renewables curtailed in the region, MW (`fuel="wind"`/`"solar"`; self-forecast `SS_*_UIGF` − cleared `SS_*_CLEAREDMW`, floored at 0) |
| `aemo_battery_region_curtailment_timestamp_seconds{region}` | the 5-minute interval behind the curtailment reading |
| `aemo_battery_region_fossil_generation_mw{region}` | live fossil (coal/gas/distillate) generation per region, MW — summed from `Dispatch_SCADA` using the OE fleet cache's DUID→region+fueltech map |
| `aemo_battery_region_fossil_timestamp_seconds{region}` | the 5-minute interval behind the fossil reading |
| `aemo_battery_fossil_units` | fossil units that contributed to the region sums |
| `aemo_interconnector_flow_mw{interconnector}` | settled MW flow from `MWFLOW` (AEMO's per-link sign convention) |
| `aemo_interconnector_metered_flow_mw{interconnector}` | `METEREDMWFLOW`, MW |
| `aemo_interconnector_export_limit_mw{interconnector}` | `EXPORTLIMIT`, MW — verbatim, can be negative (T-V-MNSP1) |
| `aemo_interconnector_import_limit_mw{interconnector}` | `IMPORTLIMIT`, MW — verbatim, can be negative |
| `aemo_interconnector_losses_mw{interconnector}` | `MWLOSSES`, MW |
| `aemo_interconnector_marginal_value{interconnector}` | `MARGINALVALUE`, $/MWh |
| `aemo_interconnector_marginal_loss_factor{interconnector}` | `MARGINALLOSS` |
| `aemo_interconnector_violation_degree{interconnector}` | `VIOLATIONDEGREE` |
| `aemo_interconnector_timestamp_seconds{interconnector}` | the 5-minute interval behind the readings |
| `aemo_interconnector_age_seconds{interconnector}` | its age at export time |
| `aemo_interconnector_report_info{file}` | the processed `PUBLIC_DISPATCHIS` file |
| `aemo_interconnector_count` | links with a reading; 0 when the INTERCONNECTORRES table was missing/unparsable |
| `aemo_battery_scada_success`, `aemo_battery_dispatchis_success` | 1 on success, 0 on failure, per intraday feed |
| `aemo_battery_intraday_poll_duration_seconds`, `aemo_battery_intraday_last_poll_timestamp_seconds` | intraday poll timing |
| `aemo_battery_inferred_stored_mwh{unit,duid}` | live per-unit estimate: daily `ENERGY_STORAGE` anchor + integrated SCADA MW, MWh |
| `aemo_battery_inferred_timestamp_seconds{unit,duid}` | the integrated SCADA interval behind the estimate |
| `aemo_battery_inferred_age_seconds{unit,duid}` | its age at export time |
| `aemo_battery_inferred_clamped{unit,duid}` | 1 when the raw integral left `[0, capacity]` and is pinned at a boundary |
| `aemo_battery_inferred_units` | inferred units being served |
| `aemo_battery_inferred_clamped_units` | ... of them pinned at a boundary |
| `aemo_battery_inferred_fleet_capacity_units` | ... of them with a known registered capacity (`--fleet-cache-file`), feeding the 100% clamp |
| `aemo_battery_infer_success` | 1/0 — the series is actually being served (0 while absent) |
| `aemo_battery_inferred_anchor_age_seconds` | age of the daily anchor interval the estimates integrate away from |
| `aemo_battery_infer_charge_efficiency` | the stored share of charging energy |
| `aemo_battery_inferred_report_info{file}` | the daily report anchoring the estimates |

### Running

```bash
python3 scrapers/aemo_battery_exporter.py --listen-address 0.0.0.0 --port 9112 \
    --state-file /cache/state.json
python3 scrapers/aemo_battery_exporter.py --once --state-file /cache/state.json
python3 scrapers/aemo_battery_exporter.py --intraday-poll-interval=300
python3 scrapers/aemo_battery_exporter.py --no-infer-backfill        # never fetch the SCADA archive
python3 scrapers/aemo_battery_exporter.py --infer-backfill-max-hours=48
python3 scrapers/aemo_battery_exporter.py --fleet-cache-file=/oefleet/fleet.json  # clamp charging at 100%
```

### Tests

143 tests in this file, **no network access** — upstream responses are synthetic
zips built in-test against the real column indices, and the parsers have been run
against real captures (details in `NOTES.md`). Alert on `aemo_battery_scrape_success
== 0` or `time() - aemo_battery_report_newest_interval_timestamp_seconds` being
older than the ~24h next-day gap; remember the value is the *next* day's, so a
forty-hour-old scalar is normal. The intraday feeds have their own health in
`aemo_battery_scada_success`/`aemo_battery_dispatchis_success`, and a gap on the
live power panel that keeps `aemo_battery_power_age_seconds` growing past a few
minutes points at the SCADA feed before anything else.

## How it is deployed here

In the normal setup these scripts are not run by hand: `../docker-compose.yml`
bind-mounts each read-only into `python:3.12-alpine` and Prometheus scrapes the
containers over the compose network. See `../README.md` for that stack and
`../NOTES.md` for its history. The invocations above are only for running an
exporter outside compose.

### The NEM web map (`webmap`, container `nem-map`, port 9119)

The seventh service in the compose file is not an exporter. `webmap` is a
stdlib-only `ThreadingHTTPServer` (same `python:3.12-alpine` image, mounted the
same way) serving a **standalone Leaflet page with no tile dependency** — it
draws region polygons, the interconnector lines, the battery circles, the
solar/wind, hydro, coal and gas markers and a Snowy reservoirs card onto a
plain dark background, so it renders with no internet at all. `./webmap/data`
is hand-authored schematic `*.geojson` (six regions, the seven
interconnectors/MNSP links plus the Snowy link, and intra-regional corridors
AEMO does not publish numbers for): the line files carry only `kind` and `id`
for links — display names are attached by the server from the link registry
(`scrapers/links.py`, see `docs/adr/0003`), and the coordinate order encodes
each link's FROM→TO direction. The state-of-charge colour ramp the battery
markers fill with travels the same way: stated once in `scrapers/soc_policy.py`
(see `docs/adr/0005`), adapted by the server into `/data/live`, never restated
in the browser — the dashboard's bar gauge reads the same module. The payload's
per-unit capacity (the SoC denominator) comes from the fleet module alone;
there is deliberately no Prometheus capacity series beside it. `./webmap/static/app.js` polls
`/data/live` every 30s, which the server builds from one Prometheus instant
query per field — 27 of them, for the interconnector flow/limits/nominal and region
stored/fossil/curtailment series above, plus the top dozen `oe_battery_*`
units — and it ships so the map is not left depending on the point-in-time
state of any one poll.

Two properties matter for the sign convention this README insists on: the map
takes a link's flow sign from `aemo_interconnector_flow_mw` unchanged, and it
reads the fleet **straight off the `battery-fleet-cache` volume the OE
exporter writes** — read-only, never a copy, so the map cannot drift from what
the scrapers last fetched.

Utilisation is derived in the server from the nominal ratings
(`aemo_interconnector_nominal_{forward,reverse}_mw`, the system-normal
capability), never from the dynamic dispatch limits: those bend to the binding
constraint, can go negative, and can collapse onto the flow itself, so a load
factor taken against them is meaningless. The limits still reach the front end,
labelled as limits, for display only. The unmonitored ~110 batteries render grey at their cached capacity,
which is why the whole-fleet cache (facility coordinates included) is the map's
data source rather than a fresh API call. Healthcheck hits `127.0.0.1:9119`
like the other services.

Battery markers carry two status features. A **`committed` (planned) checkbox**
hides the units that have never dispatched. And **commissioning** units — built
but not yet fully energised — are drawn as a dim disc with a coloured arc over
the fraction of registered storage actually energised: peak stored MWh over
`capacity_storage_mwh`. That ratio is the physical proxy for how much of the
battery exists, since AEMO publishes no module count; it is a lower bound,
because a unit that has never charged to its ceiling reads low. The arc runs
**red-yellow-green** by how charged the energised plant is *now* relative to
that peak. Both numbers ride on `/data/live`; the peak comes from the
`aemo_battery_peak_stored_mwh` recording rule
(`max_over_time(aemo_battery_inferred_stored_mwh[90d])` in
`../prometheus_rules.yml`) — recorded rather than queried per poll, so the
90-day window is scanned once per interval instead of on every 30s request.
WEM units have no `aemo_battery_inferred_stored_mwh` series and simply get no
arc.

Hydro generation is a **hexagon sized by registered capacity**, one marker per
facility rather than per unit: Tumut 3's eight units share a single coordinate,
so per-unit polygons would stack invisibly. Fill is output — generation plus
pumping, as a fraction of capacity — on the same red-yellow-green ramp the
solar/wind markers use, and grey when the facility has no live
`aemo_unit_power_mw` reading (24 of the 63 drawn facilities, mostly the small
irrigation and mini-hydro schemes AEMO does not meter). A marker **pulses while
it is producing or pumping at least `max(1 MW, 5% of capacity)`**, so a station
trickling a few MW out of a 1,500 MW scheme does not throb all day — hence the
1 MW floor, since 5% of a 1.4 MW mini-hydro is noise. Generation (`hydro` DUIDs)
and pumping (`pumps` DUIDs, which report pumping as a **positive** MW) are
summed separately for the tooltip; the pulse fires on their sum.

**OpenNEM has no `pumped_hydro` fueltech** — a pumped station reports `hydro`,
plus a sibling `pumps` unit only where the load DUID is in the fleet — so
`build_fleet()` in `webmap/server.py` classifies a facility as pumped storage
when **any** of three independent signals agrees: a `pumps` unit, a non-zero
`capacity_storage` on a unit, or the facility description matching
`pumped|pump[- ]?storage`. All three agree on the same seven schemes (Tumut 3,
Wivenhoe, Shoalhaven, Guthega/Snowy 2, Kidston, Snowy 2.0, Walpole); the other
56 draw as classic hydro. Pumped markers add a **second, concentric hexagon
outline** so the two are distinguishable at a glance. Retired units are dropped,
so a half-demolished station is not counted at its historic capacity — Tumut 3's
six retired 250 MW units would otherwise add 1,500 MW to a 2,100 MW facility.

**Coal and gas** are drawn the same way hydro is — one marker per facility, sized
by registered capacity and filled by output as a fraction of capacity — because a
thermal plant's units also share a coordinate (Bayswater's four boilers) and
would stack. Shapes extend the existing language: **coal is a square, gas a
pentagon** (solar's diamond, wind's triangle and hydro's hexagon are taken), and
the square is rotated 45° so it reads as a square rather than a second diamond.
`build_fleet()` splits them into two buckets from the fueltech — coal =
`coal_black` + `coal_brown`, gas = `gas_ccgt`/`gas_ocgt`/`gas_steam`/`gas_recip`/
`gas_wcmg` — leaving `distillate` and the bioenergy fuels out, since folding them
in would misreport the capacity. Retired units are dropped, so the closed
Hazelwood, Callide A and Energy Brix blocks do not linger at their old ratings.
Output comes from `aemo_unit_power_mw`, so the WEM coal and gas plants
(Bluewaters, Collie, Muja, the Kwinana/Pinjar peakers) and the small
non-scheduled mine-gas and cogeneration units render grey "no live data" — they
are outside the NEM dispatch set the exporter reads. Unlike hydro these do not
pulse: thermal plant is baseload, so a marker would throb almost constantly and
the motion would carry no signal.

A **Snowy reservoirs** card (top right) shows the three published lakes'
percent-of-gross-storage and 7-day direction, straight from the
`snowy_tantangara_*` series on `/data/live`. They are catchment storages that map
to no single station, so they get their own card rather than a fabricated
per-facility tooltip, and the card simply stays hidden if the exporter is down.
That needs the reservoir exporter running with `--all-reservoirs`, which the
compose file passes. The server wraps these three queries in
`last_over_time(...[2h])` — an hourly job against Prometheus's default 5m
lookback delta means a bare selector is only answerable for five minutes in
every hour (see NOTES.md). The card prints the reading's own observation date
rather than an age, because a value published once a day has no meaningful
"freshness".

The WHT exporter's deployment is:

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
