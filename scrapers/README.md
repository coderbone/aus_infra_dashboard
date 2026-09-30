# Prometheus exporters

Three independent, stdlib-only exporters live here. All are single files that
`docker-compose.yml` bind-mounts into `python:3.12-alpine`; none needs an image
build.

| Exporter | Upstream | Port | Metrics prefix |
| --- | --- | --- | --- |
| [`wht_tbm_exporter.py`](#whtp2-tbm-tracker-exporter) | Transport for NSW Western Harbour Tunnel TBM tracker (ArcGIS) | 9109 | `wht_tbm_` |
| [`snowy_tantangara_exporter.py`](#snowy-hydro-reservoir-levels-exporter) | Snowy Hydro scheme reservoir levels (`getData.php`) | 9110 | `snowy_tantangara_` |
| [`oe_battery_exporter.py`](#nembattery-state-of-charge-exporter) | OpenElectricity NEM battery storage (`api.openelectricity.org.au`) | 9111 | `oe_battery_`, `oe_batteries_`, `oe_api_`, `oe_poll_`, `oe_last_` |

They share the retry-with-time-budget approach, the `--once` mode, the
`/metrics` + `/healthz` + index handler, and the "never serve a measurement
from a cache" rule — but they are separate processes with separate upstreams and
separate failure isolation. `NOTES.md` has the upstream details for all three.

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

136 tests in this file (345 for all three exporters), about 12 seconds, **no network
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
python3 -m unittest discover -s scrapers -t scrapers    # all three, 366 tests
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
> What it does publish is stored energy (`storage_battery`, MWh) and, in the
> facility metadata, registered capacity (`capacity_storage`, MWh). Every
> percentage in the dashboard is therefore **derived** as
> `energy / capacity` in the exporter, and inherits both of their error
> sources: a capacity correction upstream moves the reading. Do not describe
> `oe_battery_soc_ratio` as an upstream figure.

### How it gets the numbers

Two endpoints, one request per battery per cycle:

1. `GET /facilities/?fueltech_id=battery` — the fleet metadata: every battery
   unit, its `capacity_storage`, its `status_id`, and its `data_last_seen`. Read
   once per `--fleet-refresh-interval` (24h) and cached to disk as a fallback.
2. `GET /data/facilities/{NEM|WEM}?metrics=storage_battery&facility_code=…` — the
   data, **per facility**, with a `date_start`/`date_end` window and
   `interval=1h`. The response holds one series per *unit* code
   (`storage_battery_ERB01`), which is the only way to attribute a number to a
   battery.

The per-facility form is not optional. The same endpoint without
`facility_code` returns one flat array of timestamps with no unit attribution at
all, and summing it is meaningless.

### The four upstream quirks that shaped it

These are the whole reason the file is the shape it is; `../scrapers/NOTES.md`
has the full audit.

1. **Three series per facility, one with capacity.** Each battery facility
   returns `<CODE>1`, `<CODE>G1` and `<CODE>L1`. Only `<CODE>1` has a
   `capacity_storage` in the metadata, so only it can yield a SOC. Summing the
   three triples the apparent fleet size. The other two are counted in
   `oe_battery_series_without_capacity` (~9 for the default top 10) and dropped.
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
   three Collie WEM units in the default top 10 are all `data_last_seen` current
   and return 1151 points that are all null. Liveness has to be learned by
   polling, so the exporter tracks it — see the rotation below.
3. **The feed is sparse on purpose.** As of 2026-09-30 values exist only for
   roughly 18:00–04:00 Sydney time and are null for the rest of the day. So a
   daytime scrape legitimately finds nothing new. The exporter exports the newest
   *non-null* sample in the lookback window together with its real timestamp and
   age, and drops a series only once that sample is older than
   `--max-sample-age` (36h, comfortably past the feed's own daily gap). A
   daytime SOC reading is hours old *by design*, and `oe_battery_sample_age_seconds`
   is how you tell that from a dead feed.
4. **Facilities can 404 or be in the other network.** `COLLIE_BESS2` 404s, and
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
cycle. The defaults (N=10, hourly) are 11 requests/cycle (12 on the daily
fleet-metadata refresh), 265/day, ~1 request
per 5.5 min: inside every bucket with room to spare.

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

Unlabelled fleet and poll gauges:

| Metric | Type | Meaning |
| --- | --- | --- |
| `oe_batteries_enumerated` | gauge | Fleet units with capacity *and* upstream data |
| `oe_batteries_in_scope` | gauge | Units selected after `--top` and idle drops |
| `oe_batteries_demoted` | gauge | Candidates skipped for going idle |
| `oe_batteries_monitored` | gauge | Units that produced a usable reading |
| `oe_battery_fleet_capacity_mwh` | gauge | Capacity of the whole enumerated fleet |
| `oe_battery_monitored_capacity_mwh` | gauge | Capacity of the monitored subset |
| `oe_battery_series_without_capacity` | gauge | Series dropped, quirk 1 (~9, expected) |
| `oe_battery_series_too_stale` | gauge | Samples dropped for age (should be 0) |
| `oe_poll_cycle_duration_seconds` | gauge | Wall time of the last cycle |
| `oe_last_poll_timestamp_seconds` | gauge | When the last cycle finished |
| `oe_last_fleet_refresh_timestamp_seconds` | gauge | When the metadata was last read |
| `oe_api_credits_remaining` | gauge | Daily balance, from the free `/me` |
| `oe_api_requests_total` | counter | Requests **issued**, failures included |

A battery that could not be read keeps its capacity, rank and `scrape_success 0`
— those are facts about the fleet — but its SOC, energy, timestamp and age series
are **omitted rather than zeroed**. On a battery, a zero is not "no reading", it
is "flat and empty", which is a different and much more alarming claim.

### The rotation: idle slots refill, at no extra request

A strict top-N is only as good as its membership. Three of the ten default units
are Collie WEM batteries that return all-null series, so a third of the request
budget buys nothing, while larger publishing batteries further down the fleet are
never asked. The exporter therefore keeps the scope full:

- Each poll notes the newest usable reading per unit, keyed on the **sample's own
  timestamp**, not the poll time. A unit that has not published since 04:00 is
  correctly measured as idle from 04:00.
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
actual bytes. Future-dated stamps are dropped rather than trusted, so a skewed
clock cannot pin a dead battery in scope forever. An unreadable or corrupt file
logs a warning and starts cold, which is the first-ever-run behaviour.

The one thing rotation does *not* do for free is recovery: a demoted unit is
never asked again, so it can only return by outranking a failure or by a
restart clearing the in-memory state. `--watchlist-per-cycle N` re-asks N demoted
units per cycle, round-robin from the largest, so a battery that comes back is
back within `len(demoted)` cycles. It is off by default because it is the only
part that costs anything — at 1 per cycle it adds ~24 requests/day, taking the
total from 265 to 289. Probing and re-admission are separate steps, so the cycle
that probes a unit reports the scope it actually polled and the unit is back in
scope on the next one.

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
`--include-undispatched`,
`--listen-address`, `--port` (9111), `--timeout`, `--retries` (3),
`--retry-budget` (6s), `--once`, `-v`.

The key is read from the environment or a file, **never from argv**, so it cannot
land in the process table, in shell history, or in a `docker inspect` command
line. Do not add a `--api-key` flag to "make it easier".

Two files are written to `--fleet-cache-file`'s volume, and they are not the same
kind of thing. `fleet.json` is a *fallback*: it is only read when the API cannot
be reached. `liveness.json` is *state*, and is read on every start.

The disk cache is a *fallback*, not a second source of truth: it is only read
when the API cannot be reached, and its `cached_at` stamp is carried over as the
refresh time so a stale cache is replaced as soon as the API answers. There is
deliberately no cache of readings — a cached SOC served as a current one is the
same dishonest failure mode the other two exporters refuse.

### Tests

```bash
python3 -m unittest discover -s scrapers -t scrapers    # all three, 366 tests
./scrapers/test_oe_battery_exporter.py                  # 109 tests, this one only
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
  would push Eraring, Waratah and Orana out of the top 10 to make room for
  batteries that publish nothing.
- **A stale cache pinning the fleet forever** — the cache is a fallback, and the
  daily metadata refresh has to still happen once a file exists.
- **A null newest sample being reported as a SOC of 0** rather than omitted.
- **WEM routed as NEM**, which returns a 404 that looks exactly like an empty
  battery.
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
