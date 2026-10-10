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

136 tests in this file (689 for all four exporters), about 15 seconds, **no network
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
`--all-reservoirs` or a repeatable `--reservoir` to include them.

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
python3 -m unittest discover -s scrapers -t scrapers    # all four, 684 tests
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

## NEM battery state of charge exporter

`oe_battery_exporter.py` publishes state of charge for the largest N NEM battery
storage units from **OpenElectricity** (formerly OpenNEM), the successor to
opennem.com.au.

> **The API publishes no state of charge.** There is no SOC metric, no
> percentage, and no full-charge flag anywhere in the OpenElectricity data model.
> What it does publish is stored energy (`storage_battery`, MWh) and charge/
> discharge power (`power`, MW), together with registered capacity
> (`capacity_storage`, MWh) in the facility metadata. There is no SOC metric or
> full-charge flag. Every percentage in the dashboard is therefore **derived**
> as `energy / capacity` in the exporter, and inherits both of their error
> sources: a capacity correction upstream moves the reading. Do not describe
> `oe_battery_soc_ratio` as an upstream figure.

### How it gets the numbers

Two endpoints, one request per battery per cycle:

1. `GET /facilities/?fueltech_id=battery` — the fleet metadata: every battery
   unit, its `capacity_storage`, its `status_id`, and its `data_last_seen`. Read
   once per `--fleet-refresh-interval` (24h) and cached to disk as a fallback.
2. `GET /data/facilities/{NEM|WEM}?metrics=storage_battery&metrics=power&facility_code=…`
   — the data, **per facility**, with a `date_start`/`date_end` window and
   `interval=1h`. The response holds one block per metric and one series per
   *unit* code (`storage_battery_ERB01`, `power_ERB01`), which is the only way
   to attribute a number to a battery.

Both metrics are asked for in that **one** request, by repeating the `metrics`
parameter. That is deliberate: the budget is counted in requests per day, and a
second call per battery would double it to fetch a number that is free to
collect alongside. Adding charge/discharge power cost the daily budget nothing,
which the `API requests against the daily bucket` panel confirms.

The per-facility form is not optional. The same endpoint without
`facility_code` returns one flat array of timestamps with no unit attribution at
all, and summing it is meaningless.

### The two feeds, and why they are not one metric

| Upstream metric | Exported as | Unit | Publishes |
| --- | --- | --- | --- |
| `storage_battery` | `oe_battery_soc_ratio`, `oe_battery_energy_stored_mwh` | MWh | overnight only, ~18:00–04:00 |
| `power` | `oe_battery_power_mw` | MW | through the day |

They behave differently and are kept apart end to end. On 2026-10-01 at 11:53
the newest Eraring energy reading was 7 hours older than its newest power
reading, and that is the normal state of this feed rather than an anomaly. So:

- Each gets **its own timestamp and age** (`oe_battery_*_sample_age_seconds`).
  One shared timestamp would make a 7-hour-old SOC claim to be current, which is
  the failure this exporter exists to avoid.
- `oe_battery_scrape_success` stays tied to the **energy** reading, so
  `Batteries with a reading` means what it says. A battery can publish power and
  still read 0 here; that combination is real and is not a fault.
- **Liveness takes whichever is newer.** A battery that is visibly dispatching on
  the power panel is not idle, so a power-only battery keeps its slot. Judging
  liveness on energy alone would rotate working batteries off the board every
  morning.

On the sign: nothing upstream documents it. It is read off the data — negative
while charging, positive while discharging, consistent with a bidirectional
dispatch_type — and stated in the metric's `# HELP` and the panel description
rather than left to be inferred from the chart. The unit is not inferred
either: each response block declares it (`MW`), and the exporter warns if that
ever stops being true.

### The five upstream quirks that shaped it

These are the whole reason the file is the shape it is; `../scrapers/NOTES.md`
has the full audit.

1. **Six series per facility, two with capacity.** Each battery facility returns
   `<CODE>1`, `<CODE>G1` and `<CODE>L1` for *each* of the two metrics it
   publishes. Only `<CODE>1` has a `capacity_storage` in the metadata, so only
   it can yield a SOC. Summing the three triples the apparent fleet size. The
   other four are counted in `oe_battery_series_without_capacity` (~37 for the
   deployed top 12) and dropped.
2. **The largest batteries in the country are unbuilt.** Of the ten
   highest-capacity units, seven are `committed` and have never dispatched —
   Richmond Valley (2200 MWh, the largest in Australia), Tomago (2000),
   Baranduda (1886), Wooreen, Elaine, Western Downs 3, Supernode 3. They publish
   nothing. The top-N is therefore taken over units with a `data_last_seen`,
   which cuts the fleet from 119 capacity-bearing units to 74 and makes the
   ranking produce a chart. `--require-data` (default on) is this filter;
   `--include-undispatched` turns it off if you would rather see the empty rows.
   `commissioning` is *kept* — Eraring 2 and Collie 2 are both commissioning and
   both publish real data.
3. **`data_last_seen` is not proof of a series.** It says the *facility* has
   dispatched, not that the battery's `storage_battery` series has a value. The
   three Collie WEM units in the original top 10 are all `data_last_seen` current
   and return 1151 points that are all null — on both metrics. Liveness has to be
   learned by polling, so the exporter tracks it — see the rotation below.
4. **The energy feed is sparse on purpose; the power feed is not.** As of
   2026-10-01 `storage_battery` values exist only for roughly 18:00–04:00 Sydney
   time and are null for the rest of the day, while `power` updates all day. So a
   daytime scrape legitimately finds no new energy even though the battery is
   dispatching. The exporter exports the newest *non-null* sample of each within
   the lookback window together with its own real timestamp and age, and drops a
   series only once that sample is older than `--max-sample-age` (36h,
   comfortably past the feed's own daily gap). A daytime SOC reading is hours old
   *by design*, and `oe_battery_sample_age_seconds` is how you tell that from a
   dead feed — read it next to `oe_battery_power_sample_age_seconds`, which will
   be hours younger.
5. **Facilities can 404 or be in the other network.** `COLLIE_BESS2` 404s, and
   ten of the battery facilities are WEM — querying one under `NEM` returns
   `404 No data available`, which is indistinguishable from a genuinely empty
   battery. The network is carried per row from `network_id` for exactly this
   reason. Both cases are counted, not raised as failures.

Plus one that is not about the data: **the API rejects the default
`Python-urllib` User-Agent with a bare HTTP 403** and no body. `USER_AGENT` is
explicit and must not be dropped. Verified 2026-09-30: `Python-urllib/3.12` →
403, `curl/8.5.0` → 200, `python-requests/2.31` → 200.

### Why the poll loop is not the scrape handler

The free Community plan allows 500 credits/day but rate-limits by bucket:
**8 requests / 5 min, 32 / 1 h, 366 / 1 day**. One request per battery per cycle
means scrape-driven polling is arithmetically impossible: at 288 scrapes/day and
even N=5, that is 1440 requests/day — 4x over the daily bucket.

So the exporter **polls on its own schedule** and serves the last completed
cycle to Prometheus. Prometheus can scrape as often as it likes — 5m here — at
zero cost to the API, and the API is only touched `--poll-interval` times per
cycle. Both upstream metrics ride in each data request, so the deployed
defaults (N=12, hourly) are 13 requests/cycle (14 on the daily fleet-metadata
refresh), 313/day, ~1 request per 4.6 min: inside every bucket with room to
spare.

Consequence worth knowing: a stopped poll loop is invisible in the SOC panels,
which happily show the last reading forever. That is what
`oe_last_poll_timestamp_seconds` and the `Since last poll` panel are for.

### Metrics

Per-battery, all carrying the same five labels —
`{facility, unit, name, region, status}` — so one Grafana variable selects the
same batteries everywhere and the table can join on `unit`:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_battery_soc_ratio` | gauge | SOC as 0–1, `energy / capacity` |
| `oe_battery_energy_stored_mwh` | gauge | Stored energy as published |
| `oe_battery_capacity_storage_mwh` | gauge | Registered capacity from metadata |
| `oe_battery_last_sample_timestamp_seconds` | gauge | When that value was observed |
| `oe_battery_sample_age_seconds` | gauge | Its age at export time |
| `oe_battery_scrape_success` | gauge | 1 if read, 0 if not |
| `oe_battery_capacity_rank` | gauge | 1 = largest in the monitored set |
| `oe_battery_idle_seconds` | gauge | Age of the newest reading seen for this unit |
| `oe_battery_power_mw` | gauge | Charge (negative) / discharge (positive) power |
| `oe_battery_power_sample_timestamp_seconds` | gauge | When that power value was observed |
| `oe_battery_power_sample_age_seconds` | gauge | Its age at export time |

With `--enable-inferred`, a separate synthetic family is exported for units with
no *fresh* measured reading. It is deliberately kept out of the table above so
it can never be mistaken for an upstream number:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_battery_soc_inferred_ratio` | gauge | SOC 0–1, dead-reckoned from power |
| `oe_battery_energy_inferred_mwh` | gauge | MWh behind that SOC, clamped to capacity |
| `oe_battery_inferred_timestamp_seconds` | gauge | Newest power point integrated |
| `oe_battery_inferred_age_seconds` | gauge | Its age at export time |
| `oe_battery_inferred_saturated` | gauge | 1 = the raw integral left `[0, capacity]` |
| `oe_battery_inferred_hold_hours` | gauge | hours the newest power rate has been carried forward to export time — the extrapolated share of the value above |
| `oe_battery_anchor_age_hours` | gauge | age of the measured reading inferred SOC integrates away from — the quantity `--max-infer-hours` bounds |
| `oe_battery_anchor_timestamp_seconds` | gauge | Unix time of that same anchor; never moves backwards, so a stalled feed reads as a frozen timestamp |

Measured always wins: while the measured reading is younger than
`--infer-fresh-hours` (default 2, deployed at 1) no inferred value is exported
at all, so the two families never both claim the same cycle.

**How accurate is it?** Measured against the live feed on 2026-10-01, integrating
from one measured reading to the next:

All figures below are with the deployed `--infer-charge-efficiency=0.9`.

| Window | n | Median error | Mean error |
| --- | --- | --- | --- |
| 1 hour (within an overnight block) | 140 | 0.25% of capacity | 0.81% |
| ~14 hours (the daytime gap) | 7 | 10.4% of capacity | 7.4% |

Error accumulates fastest in the first hours after the anchor, where most of the
movement is:

| Hours since anchor | Mean drift from anchor | Units saturated |
| --- | --- | --- |
| 1h | 0.9% | 0/7 |
| 2h | 1.9% | 0/7 |
| 3h | 2.1% | 0/7 |
| 4h | 1.1% | 0/7 |
| 5h | 3.7% | 1/7 |
| 6h | 10.3% | 1/7 |
| 8h | 31.8% | 1/7 |
| 10h | 43.0% | 3/7 |

This is the whole story of the feature. Because `storage_battery` leaves an
overnight gap, **integration cannot cover that gap for free**: by 10h the
estimate has run into the capacity bound on half the fleet, and clamping it to
100% would be a confident lie. Raising the cap buys coverage and costs accuracy
— it will not refuse to answer, only answer badly.

The deployment therefore runs `--max-infer-hours=48` with
`--infer-fresh-hours=1`. The 48 is **not** a claim that a 48h integral is
accurate; it is set above the worst anchor age actually observed so the estimate
stays visible during an upstream publication failure. The fit error above is
~5% of capacity by 14h and compounds well past that by 32h, so during an outage
the dashed line should be read as *"batteries moved this way since 04:00"*, not
as a state-of-charge reading.

Once `storage_battery` publishes again the anchor refreshes and 48 becomes
slack — the normal case is unaffected by the setting, so it can be dropped back
to 24 at leisure. What must not happen is the reverse: setting it below the
real anchor age does not degrade the estimate, it deletes it. That is exactly
what happened at 15h on 2026-10-03, and
[`oe_battery_anchor_age_hours`](#oe_battery_anchor_age_hours) now exists to make
that failure mode visible rather than silent.

### `--infer-charge-efficiency`

Grid-facing charging energy and stored energy are not the same number: some is
lost converting between AC and DC and never comes back. The coefficient is the
share that reaches the cells, applied to **charging only** — the discharge side
is already metered at the terminals, so there is nothing further to discount it
by. That makes it a round-trip figure: at `0.9`, 100 MWh into the grid
connection puts ~90 MWh in the cells and ~90 MWh comes back out.

A segment that crosses zero is split at the crossing, because charging and
discharging have to be weighted differently and averaging across the switch
point would apply the wrong factor to part of the interval.

Without it the integration is biased in one direction only — it consistently
**over**-predicts stored energy — which is the signature of an ignored loss
term. The default is `1.0`, i.e. no loss term at all, so the exporter assumes
nothing about hardware it was not told about; the deployment sets `0.9`
explicitly in `docker-compose.yml`.

| `--infer-charge-efficiency` | 1h median | 14h median | 14h mean |
| --- | --- | --- | --- |
| `1.0` (no loss term) | 0.3% | 14.3% | 11.3% |
| `0.9` (deployed) | 0.3% | 10.4% | **7.4%** |

It roughly halves the error over the long gap and leaves the one-hour case
untouched, which is what a genuine physical loss term should do. That is the
test a coefficient *fitted* to these seven samples failed: see `NOTES.md` for
why that version was rejected.

One coefficient for the whole fleet remains a simplification, and the obvious
next step — **a per-unit coefficient** — was tried and does not work. Fitted in
closed form per unit over five days and validated leave-one-day-out, the values
came out physically impossible (two units above 1.0, meaning they would store
more energy than they drew; two negative), and held-out validation improved the
fleet by 3% while being *worse* than a flat 0.9 on three units of seven. That is
the signature of a model whose residual is not a loss term; `NOTES.md` records
what the residual turns out to be. The lever that would actually move this error
is finding out what `power` measures, not another coefficient.

### `--infer-max-hold-hours`

How long the newest observed power rate is carried forward to export time,
scaled by the real elapsed time — a five-minute-old rate contributes 1/12th of
its hourly energy, an hour-old one a full hour. Default `6`, set to `0` to
disable.

It exists because of an asymmetry that is easy to miss: `power` arrives on a
fixed grid and Prometheus scrapes fall *between* those grid points. Integrating
only the samples therefore produces a value that is exactly right but changes
only when a new sample lands — hourly here. On a chart that is a staircase, and
a staircase is indistinguishable from a battery that stopped moving. The hold
turns it back into a line, and each real sample re-anchors it, so the error
introduced is bounded by the time to the next sample rather than by the hold.

Three bounds keep it an estimate rather than invention. `--max-infer-hours`
still kills the whole estimate at the anchor; `--infer-max-hold-hours` caps how
far a single unconfirmed rate is carried, so a poll that stops arriving
degrades into a frozen line with a growing age instead of an indefinite
extrapolation; and the capacity clamp still applies, so a held battery stops at
full and reports `oe_battery_inferred_saturated` rather than walking off the
panel. `oe_battery_inferred_hold_hours` publishes the held duration, because a
value that is partly extrapolated should not look like one that was measured.

### `oe_battery_anchor_age_hours`

Publishes the age of the measured `storage_battery` reading that inferred SOC
integrates away from, for every unit, whether or not an estimate exists.

It exists because `--max-infer-hours` is a hard cut-off rather than a
degradation: once the anchor is older than the setting, inference stops and
`oe_battery_soc_inferred_ratio` is simply *absent*. An absent series and a
battery with no data look identical on a dashboard, which is precisely when the
cause matters most. The anchor pair turns that silence into a number:

- `oe_battery_anchor_age_hours` — hours, so it reads directly against
  `--max-infer-hours`. Above the setting, no estimate is published; that is the
  whole rule.
- `oe_battery_anchor_timestamp_seconds` — the underlying instant. It only ever
  moves forward, so a stalled upstream feed is a flat line rather than a gap,
  which is the form that survives being pasted into a support ticket.

Both are emitted even when the poll that produced them returned nothing, and a
unit whose anchor is unknown is omitted rather than zeroed — a zero would read as
"published just now", inverting the metric's purpose.

Observed on 2026-10-03: `storage_battery` stopped publishing and the anchor
reached 31.8h, which at `--max-infer-hours=15` suppressed every inferred line
with no other symptom. The setting was raised to 48 to prefer a visibly-degrading
estimate over none; see the `--max-infer-hours` discussion below for what that
costs.

### `--power-history-file` and `--power-history-days`
### `--power-history-file` and `--power-history-days`

A local, self-hosted copy of the per-unit power series, held in memory and in a
plain JSON file (`power-history.json` beside `--fleet-cache-file` by default).

What it buys is **resilience, not accuracy**. Inferred SOC integrates the power
series between two measured energy readings, and until now that series came only
from the current cycle's API response — which quietly couples the estimator both
to how deep a lookback the exporter asked for and to whether the API was
reachable. A scrape that failed mid-window silently shortened the integration,
and the result was indistinguishable from a battery that had simply stopped.
Merging the cache with each fresh response turns that into lost freshness
rather than lost history.

It is deliberately **not** used to hold the inference anchor. `_last_measured_*`
still re-derives from the API on every start, because a persisted anchor would
let a restart serve a stale SOC as if it were current — the one failure mode the
exporter refuses everywhere else. Power is a bounded, self-correcting physical
signal; an energy anchor is a claim about the present, and a file on disk cannot
tell whether it is still true.

Bounded twice over: by `--power-history-days` (14 by default), and by keeping
one value per (unit, timestamp), so re-polling the same lookback replaces rather
than duplicates. At `--api-interval=5m` that is ~288 points per unit per day, so
14 days of a 12-unit fleet is roughly 1 MB — still trivial, but no longer
something to read with `cat`; `--power-history-days` is the knob if it ever
matters.

Storing it does **not** let you poll faster: the upstream power series is
already fully resolved at `--api-interval`, so re-fetching returns
byte-identical data for another request. `--api-interval=5m` is the knob that
*does* change resolution, and it is free, being a query parameter on the same
one request — over four days it roughly halves mean error at every horizon
(1.47% vs 2.12% at 6h) because the hourly value is only the arithmetic mean of
the twelve 5m samples under it. All verified, with numbers, in the sampling
section of `NOTES.md`.

`oe_battery_inferred_saturated` exists because a clamped value is otherwise
indistinguishable from a real one: in the live run Waratah read exactly `1.0`
because the integral ran ~97 MWh past its 1680 MWh capacity. A `1` there means
the integration and the anchor disagree by at least a full battery.

Flags: `--enable-inferred`, `--max-infer-hours` (default 24),
`--infer-fresh-hours` (default 2), `--infer-max-gap-hours` (default 2),
`--infer-max-hold-hours` (default 6), `--no-infer-clamp`. Inference costs no extra API requests: the full power series
comes back in the response already being read for `oe_battery_power_mw`.

Gaps are not bridged. `--infer-max-gap-hours` bounds how far apart two power
samples may be and still be integrated across, so one missing hourly point is
tolerated but a real outage ends the integration instead of inventing energy
across it.

Unlabelled fleet and poll gauges:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_batteries_enumerated` | gauge | Fleet units with capacity *and* upstream data |
| `oe_batteries_in_scope` | gauge | Units selected after `--top` and idle drops |
| `oe_batteries_demoted` | gauge | Candidates skipped for going idle |
| `oe_batteries_monitored` | gauge | Units that produced a usable reading |
| `oe_battery_fleet_capacity_mwh` | gauge | Capacity of the whole enumerated fleet |
| `oe_battery_monitored_capacity_mwh` | gauge | Capacity of the monitored subset |
| `oe_battery_series_without_capacity` | gauge | Series dropped, quirk 1 (~37, expected) |
| `oe_battery_series_too_stale` | gauge | Samples dropped for age (should be 0) |
| `oe_batteries_inferred` | gauge | Units carrying a synthetic SOC this cycle |
| `oe_poll_cycle_duration_seconds` | gauge | Wall time of the last cycle |
| `oe_last_poll_timestamp_seconds` | gauge | When the last cycle finished |
| `oe_last_fleet_refresh_timestamp_seconds` | gauge | When the metadata was last read |
| `oe_api_credits_remaining` | gauge | Daily balance, from the free `/me` |
| `oe_api_requests_total` | counter | Requests **issued**, failures included |

A battery that could not be read keeps its capacity, rank and `scrape_success 0`
— those are facts about the fleet — but its SOC, energy, timestamp and age series
are **omitted rather than zeroed**. On a battery, a zero is not "no reading", it
is "flat and empty", which is a different and much more alarming claim. Power
follows the same rule, so a gap in the power panel means *nothing was published
for that unit*, while a real `0` there is upstream genuinely reporting zero —
worth distinguishing, because commissioning units sit at zero all day.

### The rotation: idle slots refill, at no extra request

A strict top-N is only as good as its membership. Three of the original ten
units were Collie WEM batteries that return all-null energy series, so a third
of the request budget bought nothing, while larger publishing batteries further
down the fleet were never asked. The exporter therefore keeps the scope full:

- Each poll notes the newest usable reading per unit, keyed on the **sample's own
  timestamp**, not the poll time, and over *both* metrics. A unit that has not
  published since 04:00 is correctly measured as idle from 04:00; a unit
  dispatching on power alone is not idle, which is why the clock takes the newer
  of the two.
- A unit whose reading is older than `--drop-idle-hours` (default 36, matching
  the sample-age limit) is dropped from scope and the next-largest candidate
  takes its slot. `oe_batteries_demoted` counts batteries skipped, not slots
  lost; `oe_battery_idle_seconds` is exported for demoted units too, so "why did
  that battery stop appearing" is answerable from the metrics alone.
- **Cost is unchanged**: one request per in-scope battery per cycle, exactly as
  before. The rotation only changes *which* batteries those are.

The 36-hour default is deliberate and is the whole design. The feed publishes
overnight (~18:00–04:00), so a poll at 16:00 finds nothing newer than 04:00 and
the reading is 12 hours old. Anything tighter than a full publication cycle would
demote healthy batteries every afternoon and rotate the scope daily. A unit must
miss an entire night's publication to be judged idle. `--drop-idle-hours 0`
restores a strict capacity top-N.

Liveness is persisted so that a restart does not undo any of this. Without it,
the first sight of an unreadable unit sets its clock to *now*, so a container
restarted more often than the idle window hands every dead battery a fresh 36h
reprieve and the rotation never converges — the Collie units would hold their
slots forever, and every deploy would silently revert the demotions.
`--liveness-file` (defaulting to `liveness.json` beside `--fleet-cache-file`)
writes `{unit_code: epoch}` after every clean poll, atomically. The file is
**liveness only** — when each unit was last seen publishing. No reading, energy
value or SOC is ever written to it, so a restart still cannot serve a stale SOC
as current. That is the same refusal the exporter applies everywhere else, and
`test_only_liveness_is_persisted_never_a_reading` asserts it against the file's
actual bytes. (Energy readings *are* persisted elsewhere — see
`--reading-cache-file` below, which is a different file for a different reason.)
Future-dated stamps are dropped rather than trusted, so a skewed
clock cannot pin a dead battery in scope forever. An unreadable or corrupt file
logs a warning and starts cold, which is the first-ever-run behaviour.

The one thing rotation does *not* do for free is recovery: a demoted unit is
never asked again, so it can only return by outranking a failure or by a
restart clearing the in-memory state. `--watchlist-per-cycle N` re-asks N demoted
units per cycle, round-robin from the largest, so a battery that comes back is
back within `len(demoted)` cycles. It is off by default because it is the only
part that costs anything — at 1 per cycle it adds ~24 requests/day, taking the
total from 313 to 337. Probing and re-admission are separate steps, so the cycle
that probes a unit reports the scope it actually polled and the unit is back in
scope on the next one.

### Narrow request windows (`--reading-cache-file`, `--poll-window-hours`)

The exporter takes the newest non-null `storage_battery` sample *inside the
window it asked for*, and that reading is 6–14h old by poll time. So the window
has to be wide enough to reach back to it: 192h at `interval=5m`, which is not a
cheap way to fetch one number. Measured against the live API, one facility:

| window | response | points | fetch |
| --- | --- | --- | --- |
| 192h | 494 KB | 13,654 | 0.74s |
| 48h | 120 KB | 3,288 | 0.60s |
| 2h | 4.8 KB | 96 | 0.38s |

That 494 KB was being re-sent every hour, per facility — about 5.9MB per cycle
across the deployed 12 — mostly duplicating points already fetched.

`--reading-cache-file` keeps the newest reading per unit locally
(`readings.json` beside `--fleet-cache-file`), so a poll only has to cover the gap
since the last one. `--poll-window-hours` (default 2) sets that minimum; against a
1h poll interval that is 4x overlap, so a dropped poll cannot open a hole in the
power series. `infer_soc` *drops* segments wider than `--infer-max-gap-hours` instead of
integrating across them, so a hole is not a slightly-wrong number — it is the
line going flat at the last good point.

This does not change the API request count. The binding limit is 366
requests/**day**, and N facilities is N requests per cycle whether the window is
2h or 192h. What drops is bytes and server-side work per request, which is the
scrape time that actually grew.

The window returns to the full `--lookback-hours` automatically whenever it is not
safe to narrow: no cache configured, a cold cache, a missed poll (the window
tracks time since the last *attempt*, so a failed cycle cannot shrink the window
that would have repaired it), or a unit **known to publish** that is no longer
reachable. It stays wide on the first cycle after a restart even if the cache
survived — that process has not yet proven it can fetch.

A unit that has *never* published does not block narrowing. This matters in
practice: 5 of the deployed 12 batteries never publish `storage_battery`, and
requiring a reading from those would pin the window at 192h forever and make the
whole mechanism dead code. The cache records which units it has seen publish
(`seeded`), so "missing" means *regressed from an observed state*, not *absent of
evidence*.

**This does relax a safety invariant, deliberately.** Where the liveness file
stores no readings, this one does, and a cached reading can reach a *measured*
metric: if a narrow window returns power but no storage point inside it, the
cached reading is exported as the measured one rather than the metric going
blank. The bounds on that are explicit rather than assumed:

- it keeps its own timestamp, so `oe_battery_sample_age_seconds` and
  `oe_battery_anchor_age_hours` report its true age — verified to be
  identical to the age the wide path reports;
- `oe_battery_reading_from_cache` counts units in that state, so "the buffer has
  become the only source of truth" is visible rather than silent;
- it is **never** used for a facility that failed to answer — that stays
  `scrape_success=0`, because reporting a battery as monitored during an
  upstream outage is the one thing this exporter must not do;
- a live reading always supersedes it, so the cache can lag but never contradict
  what the API just said;
- it is bounded by `--max-sample-age` and `--max-infer-hours`, and
  future-dated stamps are dropped.

To disable the whole mechanism and keep every request at the full reach, omit
`--reading-cache-file`.

### Running

```bash
# one-shot exposition on stdout (exit 1 if the poll failed)
OPENEA_API_KEY=… ./oe_battery_exporter.py --once

# long-running exporter, top 15, every 30 minutes
OPENEA_API_KEY=… ./oe_battery_exporter.py \
    --listen-address 127.0.0.1 --port 9111 --top=15 --poll-interval=1800
curl -s localhost:9111/metrics
curl -s localhost:9111/healthz
```

Options: `--api-key-env` (default `OPENEA_API_KEY`) and `--api-key-file` (mutually
exclusive), `--top` (default 10, 0 for all), `--poll-interval` (default 3600s),
`--lookback-hours` (36), `--max-sample-age` (129600s = 36h),
`--drop-idle-hours` (36, 0 disables rotation), `--watchlist-per-cycle` (0),
`--fleet-refresh-interval` (86400s), `--fleet-cache-file`, `--liveness-file`,
`--reading-cache-file`, `--poll-window-hours` (2),
`--include-undispatched`,
`--listen-address`, `--port` (9111), `--timeout`, `--retries` (3),
`--retry-budget` (6s), `--once`, `-v`.

The key is read from the environment or a file, **never from argv**, so it cannot
land in the process table, in shell history, or in a `docker inspect` command
line. Do not add a `--api-key` flag to "make it easier".

Three files are written to `--fleet-cache-file`'s volume, and they are not the same
kind of thing. `fleet.json` is a *fallback*: it is only read when the API cannot
be reached. `liveness.json` is *state*, and is read on every start.
`readings.json` is an *anchor buffer* — it is read on every start and is what
makes a narrow request window safe (see above), so omitting
`--reading-cache-file` reverts every request to the full `--lookback-hours`.

The *fleet* disk cache is a fallback, not a second source of truth: it is only
read when the API cannot be reached, and its `cached_at` stamp is carried over as
the refresh time so a stale cache is replaced as soon as the API answers.
`liveness.json` deliberately holds no readings. `readings.json` does, and that is
the one place this exporter knowingly gives up the refusal the other two make —
bounded, timestamped and reported, as set out under `--reading-cache-file` above.

### Tests

```bash
python3 -m unittest discover -s scrapers -t scrapers    # all four, 684 tests
./scrapers/test_oe_battery_exporter.py                  # 335 tests, this one only
```

**No network access** — the fleet metadata, a real Eraring storage response, an
empty response and a PII-redacted `/me` are replayed from `scrapers/fixtures/`,
captured verbatim from the API on 2026-09-30.

The regressions worth naming, because each is a quiet failure:

- **The 403 User-Agent rejection** raising `AuthError` rather than a generic
  scrape error, and HTTP 401/403 never being retried.
- **`capacity_storage` being a *unit* field, not a facility field** — reading it
  off the facility yields no denominator at all and silently drops every SOC.
- **The three-series trap** — G1 and L1 counted, never summed into the SOC.
- **A committed/unreported facility counting as a fleet *observation***, which
  would push Eraring, Waratah and Orana out of the selected set to make room for
  batteries that publish nothing.
- **A stale cache pinning the fleet forever** — the cache is a fallback, and the
  daily metadata refresh has to still happen once a file exists.
- **A null newest sample being reported as a SOC of 0** rather than omitted.
- **WEM routed as NEM**, which returns a 404 that looks exactly like an empty
  battery.
- **One shared timestamp for two feeds.** Energy and power are hours apart; a
  single clock would date a 7-hour-old SOC as current.
- **The second metric costing a second request** — `power` has to ride along in
  the same call, not be fetched on its own.
- **A multi-unit facility double-sampled** when both units' series are present
  in one response: the sample must be attributed to the row's own unit, not
  concatenated across the block.
- **A power block arriving with a unit the metadata did not declare** — checked
  and warned, not silently accepted, so a change upstream is visible.
- **A sample older than the feed's daily gap being dropped**, so a dead feed
  loses its series instead of repeating a day-old reading as if it were current.
- **A 429/5xx transient being retried inside the time budget, and a 404 not
  being retried at all** — an absent facility is an answer, not a fault.
- **Demoting on a count of empty polls instead of the age of the last reading.**
  The feed only publishes ~18:00–04:00, so a poll in the late afternoon returns
  nothing *new* while the battery is perfectly healthy. Counting empty polls
  rotates the entire scope every afternoon; measuring reading age does not.
- **A unit that recovers only being re-admitted a cycle late** — the probe runs
  after the scope is chosen, so the exposition reports the scope that was
  actually polled. Correct, and asserted as such.

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
  interval) and `DispatchIS_Reports` (region-level `BDU_ENERGY_STORAGE`, every
  5-minute interval). These are the feeds that move a charge/discharge chart
  through the day. There is **no** public per-DUID live SOC: the daily report is
  the only per-unit storage, and AEMO's only live *measured* storage number is
  the region-level BDU figure.

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
   snapshot whose `UNIT_SCADA` table carries, per DUID, the *measured*
   `SCADAVALUE` in MW (negative = charging/absorbing). Verified live
   2026-10-08 15:20: ERB01 −120, WTAHB1 −34, LIMBESS1 +0.9. Rows are filtered to
   the units the daily report has learned are storage units (the battery set is
   seeded from the state file at startup so a restart does not wait for the
   daily poll); a DUID the daily file has never clocked as storage is ignored.
2. `https://nemweb.com.au/Reports/Current/DispatchIS_Reports/` lists
   `PUBLIC_DISPATCHIS_<YYYYMMDDHHMM>_<seq>.zip`, a ~23KB interim-solution
   snapshot whose `DISPATCH,REGIONSUM` table carries `BDU_INITIAL_ENERGY_STORAGE`
   (idx 129) and `BDU_ENERGY_STORAGE` (idx 124) — the reported aggregate stored
   MWh of all battery dispatch units per region (NSW1 5063.4 → 5084.6 MWh at
   15:20). Regions with no storage publish blanks (TAS1) and are skipped.

Both files are one interval each and advance every 5 minutes, so a cycle costs
two tiny listings plus ~30KB of zips; the reuse check absorbs a poll that lands
inside the same interval (the newest file is unchanged, so it is served without
a re-download). The two sides fail independently — `aemo_battery_scada_success`
and `aemo_battery_dispatchis_success` — and a failed side drops its own series
rather than repeating the last one, the same rule as the daily file.

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
(`charge_efficiency×|MW|×dt` while charging, `MW×dt` while discharging). The raw
integral is floored at zero (`--no-infer-clamp` serves the raw drift).
`aemo_battery_infer_success` reports whether the series is actually being served,
and `aemo_battery_inferred_anchor_age_seconds` the age of the daily anchor — let
them drop/rise and the estimate is stale.

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
| `aemo_battery_power_mw{duid}` | live measured output from `UNIT_SCADA` `SCADAVALUE`, MW (negative = charging) |
| `aemo_battery_power_timestamp_seconds{duid}` | the 5-minute interval behind the power reading |
| `aemo_battery_power_age_seconds{duid}` | its age at export time |
| `aemo_battery_power_report_info{file}` | the processed `PUBLIC_DISPATCHSCADA` file |
| `aemo_battery_power_interval_timestamp_seconds` | its interval |
| `aemo_battery_units_with_power` | storage units with a reading in the SCADA snapshot |
| `aemo_battery_region_stored_mwh{region}` | live region aggregate from `BDU_ENERGY_STORAGE`, MWh |
| `aemo_battery_region_initial_stored_mwh{region}` | from `BDU_INITIAL_ENERGY_STORAGE`, MWh |
| `aemo_battery_region_storage_timestamp_seconds{region}` | the 5-minute interval behind the region reading |
| `aemo_battery_region_storage_report_info{file}` | the processed `PUBLIC_DISPATCHIS` file |
| `aemo_battery_region_storage_interval_timestamp_seconds` | its interval |
| `aemo_battery_scada_success`, `aemo_battery_dispatchis_success` | 1 on success, 0 on failure, per intraday feed |
| `aemo_battery_intraday_poll_duration_seconds`, `aemo_battery_intraday_last_poll_timestamp_seconds` | intraday poll timing |
| `aemo_battery_inferred_stored_mwh{duid}` | live per-unit estimate: daily `ENERGY_STORAGE` anchor + integrated SCADA MW, MWh |
| `aemo_battery_inferred_timestamp_seconds{duid}` | the integrated SCADA interval behind the estimate |
| `aemo_battery_inferred_age_seconds{duid}` | its age at export time |
| `aemo_battery_inferred_clamped{duid}` | 1 when the raw integral fell below zero and is pinned at 0 |
| `aemo_battery_inferred_units` | inferred units being served |
| `aemo_battery_inferred_clamped_units` | ... of them pinned at the zero floor |
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
```

### Tests

110 tests in this file, **no network access** — upstream responses are synthetic
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
