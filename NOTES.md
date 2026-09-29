# NOTES — monitoring stack

Working notes for whoever picks this up next (including future me).

## 1. Layout as of 2026-09-29

```
~/src/monitoring/                                    # this project
~/src/monitoring/docker-compose.yml                  # 4 services, project name "bone"
~/src/monitoring/prometheus.yml                      # scrape configs: tbm-exporter:9109, reservoir-exporter:9110
~/src/monitoring/grafana/provisioning/datasources/prometheus.yml
~/src/monitoring/scrapers/wht_tbm_exporter.py        # the WHT scraper (stdlib only, no image build)
~/src/monitoring/scrapers/snowy_tantangara_exporter.py  # the Snowy Hydro reservoir scraper
~/src/monitoring/scrapers/README.md                  # metrics + run instructions
~/src/monitoring/scrapers/NOTES.md                   # upstream endpoints
~/src/monitoring/scrapers/TASKS.md                   # original task list
~/.docker/cli-plugins/docker-compose                  # Compose v2 v5.5.1, installed by us
```

Everything lived directly in `~` before: `~/docker-compose.yml`,
`~/prometheus.yml`, `~/scrapers/`, `~/monitoring/grafana/provisioning/`. It is
all here now, and the redundant `monitoring/monitoring/` nesting is gone — the
Grafana provisioning is at `grafana/provisioning/`.

Docker objects (not files, survive any file move):

| Thing | Name |
| --- | --- |
| containers | `wht-tbm-exporter`, `snowy-tantangara-exporter`, `prometheus`, `grafana` |
| compose project | `bone` (pinned via `name:` in the compose file) |
| network | `bone_monitoring` (bridge) |
| volumes | `prometheus-data`, `grafana-storage` (external, pre-existing), `exporter-cache` |

`exporter-cache` belongs to `tbm-exporter` alone — it holds that exporter's
discovered ArcGIS layer config. `snowy-tantangara-exporter` deliberately mounts
**no** volume: it has no discovery step, and a persistent cache of reservoir
levels would be a way to serve a stale reading as a current one.

## 2. If files have moved

Re-locate before assuming:

```bash
docker compose ls                       # which compose file is actually running where
docker inspect wht-tbm-exporter --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect wht-tbm-exporter --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
grep -rl 'wht_tbm_exporter' ~ 2>/dev/null
```

Then fix, in this order:

1. **The three bind mounts in `docker-compose.yml`** — paths are resolved
   relative to the compose file, not the CWD:
   `./scrapers/wht_tbm_exporter.py`, `./prometheus.yml`,
   `./grafana/provisioning`.
2. **The project name.** It defaults to the compose file's directory name, so
   moving the file changes the project name and the network name — and the
   project-prefixed volume name (`bone_exporter-cache`). Either keep the
   directory name, or pin it with `name:` in the compose file / `-p bone` on the
   command line, and recreate: `docker compose up -d --force-recreate`.
3. **Container names are pinned** in the compose file (`container_name:`), so a
   second project with different names will collide on `prometheus` /
   `grafana`. Remove the old ones first (`docker rm -f prometheus grafana`).
4. Doc references to the compose file by absolute path — this file and
   `README.md` both assume you are running from `~/src/monitoring`.

Note that a `docker compose restart` reuses the *existing* container, so it keeps
the old bind mounts. Only `up -d` recreates containers and picks up new paths.

## 3. Problems hit, and their fixes

Each of these cost time; check they haven't regressed.

- **Intermittent `EAI_AGAIN` from the ArcGIS endpoints, losing whole scrapes.**
  The interesting part is what the measurements *ruled out*, so don't "fix" this
  again by swapping the DNS.

  Shipped: transient-failure retries with exponential backoff **and jitter**,
  per-TBM error isolation, `wht_tbm_info` retained from a cache of the last good
  read, and `wht_tbm_config_stale` split out from scrape success (it used to go
  to 1 on *any* failure, which mislabelled an upstream outage as a config
  problem). `collect()` now returns `(samples, errors)`; `wht_tbm_scrape_success`
  is 0 if *any* TBM failed, so partial data is never mistaken for complete data.
  HTTP error responses are deliberately not retried — those are real answers.

  The retry budget is the load-bearing part. A failing lookup takes ~5s to give
  up, so the first version (3 retries, count-bounded) measured **23.7s for a
  single request** and 48s for a real scrape. Two TBMs would run to ~47s and blow
  through Prometheus's 30s `scrape_timeout`, which marks the target *down* — a
  worse outcome than the gap we were trying to close. `--retry-budget`
  (default 6s) caps total retry time per request, keeping scrapes at 1-10s.
  If you raise `scrape_timeout`, raise the budget with it; if you lower it,
  lower the budget.

  The resolver was the underlying culprit and is now overridden in compose
  (`dns: [1.1.1.1, 9.9.9.9]`). **The first measurement of this was wrong, and
  the way it was wrong is the useful part.** Comparing resolvers while DNS was
  healthy made the default look fine — 30/30 at 2ms median against 8-9ms for
  public resolvers — and the obvious conclusion was "don't touch it, the
  failures are just burst-correlated". Re-measuring *during a live failure
  window* reversed that: the default resolver's failures take **exactly
  5005ms** (a 5s upstream timeout, not a fast `EAI_AGAIN`), 2 of 130 lookups,
  while 1.1.1.1 and 9.9.9.9 returned 0/141 and 0/147. The 2ms median was
  measuring the healthy path and hiding a 5s cliff. That cliff is what ate the
  whole retry budget — two of them exhaust it, which is exactly the 21s scrape
  observed live. So: measure failure latency during an outage, not success
  latency during health, and do not let a good median average away a cliff.
  Post-change, 176 in-container lookups through the override: 0 failures, 9ms
  median, 166ms max.

  Both fixes are kept deliberately. The resolver removes the failure mode; the
  retry and budget are the backstop for when public resolvers blip too, and the
  budget is the only thing that guarantees a scrape stays inside
  `scrape_timeout` regardless of cause.

  Also noted: the exporter serves HTTP 200 on `/metrics` even when the upstream
  scrape fails, so the Prometheus target stays `up` and only the gauge gap plus
  `wht_tbm_scrape_success` reveal the failure. That is intentional (see
  section 7) but it means `up` is not a health signal here.

- **"Works on curl localhost, dead from the container."** The exporter defaulted
  to `--listen-address 127.0.0.1`. On a bridge network, `127.0.0.1` inside a
  container is that container. Fixed by binding `0.0.0.0` in compose while
  leaving 9109 unpublished (only Prometheus needs it).
- **`-p 9090:9090` silently ignored.** The old `start_prometheus.sh` combined
  `-p` with `--network host`, where Docker drops the publish flag; the UI was
  only reachable because host networking put Prometheus on every interface. The
  old `~/start_prometheus.sh` / `~/start_grafana.sh` have been deleted; do not
  recreate them alongside compose (second unmanaged exporter, same upstream).
- **Prometheus target stuck at `health: unknown` with no error.** Not a network
  problem — Prometheus jitters the *first* scrape across the whole interval, so
  a `scrape_interval: 60m` can leave a target unscraped for up to an hour after
  a restart. Was 60m, now 5m. Symptom to recognise: `lastScrape` is the zero
  time *and* `lastError` is empty.
- **`docker-compose` v1 is broken on this host** (`KeyError: 'ContainerConfig'`
  — v1.29.2 reads a field current Docker engines dropped from image inspect).
  It failed *after* removing the old container, so it destroyed Prometheus and
  then errored. Use `docker compose` (v2 plugin at
  `~/.docker/cli-plugins/docker-compose`). If it ever goes missing, reinstall
  from the docker/compose GitHub release, no root required.
- **Prometheus UI unreachable from other machines.** I had pinned it to
  `127.0.0.1:9090`; changed to `9090:9090` to match Grafana. Both UIs are meant
  to be LAN-visible on this host (`192.168.1.100`).
- **`Cannot create container for service grafana: Conflict` / `<id>_prometheus`
  already in use.** Aborted compose v1 runs leave stopped containers squatting
  on the name. `docker ps -a` then `docker rm <id>`. The home dir had ~7 of
  these; a `docker container prune` would clear them (not done — the user was
  not asked).
- **Grafana default password.** `GF_SECURITY_ADMIN_PASSWORD` is wired to
  `${GRAFANA_ADMIN_PASSWORD:-admin}` and **still defaults to `admin`** on a
  port published to the LAN. Set it in a `.env` next to the compose file.

## 4. Verify the stack

```bash
docker compose ps                                   # all four (healthy)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
curl -s --get --data-urlencode 'query=up{job="wht_tbm"}' localhost:9090/api/v1/query
curl -s --get --data-urlencode 'query=up{job="snowy_tantangara"}' localhost:9090/api/v1/query
docker compose logs --tail 20 tbm-exporter          # upstream errors land here
docker compose logs --tail 20 reservoir-exporter
```

The exporter's own timing gauge is the quickest check that the DNS retry path is
behaving — a durable scrape should sit around 1-10s, not tens of seconds:

```bash
curl -s --get --data-urlencode \
  'query=max_over_time(wht_tbm_scrape_duration_seconds[1h])' \
  localhost:9090/api/v1/query
```

Transient retry warnings in the exporter log are expected and self-clearing:

```
WARNING transient failure (attempt 1 of 4) on https://...: [Errno -3] Try again; retrying in 0.4s
```

### TODO 2026-09-29: confirm the DNS fix actually holds

**Check the 24h failure rate.** The retry + per-TBM isolation + DNS override went
live 2026-09-28 06:30 UTC. Four clean scrapes right after the change is
suggestive, not proof — the underlying `EAI_AGAIN` bursts are intermittent, so a
short clean window proves nothing either way. A full day of data does.

The `dns: [1.1.1.1, 9.9.9.9]` override is **not** WHT-specific — the fault is
in Docker's embedded resolver, not in the destination host, so
`snowy-tantangara-exporter` carries the same override for the same reason. The
*measurement* quoted below was taken against `utility.arcgis.com` only, so the
resolver comparison has not been re-run against `snowyhydro.com.au`. Worth doing
if a failure window opens up there, but the transfer of the fix is not in doubt
and the retry budget remains the backstop either way.

```bash
# 1. failed scrapes in 24h. NOTE: must be == bool 0 + sum_over_time.
#    count() over a range vector is a parse error in this Prometheus, and the
#    unfiltered `== 0` variant silently reports 0 because the series is empty.
curl -s --get --data-urlencode \
  'query=sum_over_time((wht_tbm_scrape_success == bool 0)[24h:5m])' \
  localhost:9090/api/v1/query

# 2. gauge gaps, per TBM. 288 is the full 24h at a 5m interval.
curl -s --get --data-urlencode \
  'query=count_over_time(wht_tbm_ring_number[24h])' \
  localhost:9090/api/v1/query

# 3. worst scrape duration. Must stay under the 30s scrape_timeout.
curl -s --get --data-urlencode \
  'query=max_over_time(wht_tbm_scrape_duration_seconds[24h])' \
  localhost:9090/api/v1/query
```

Compare against the pre-fix baseline (24h to 2026-09-28):

| Check | Baseline | Target |
| --- | --- | --- |
| failed scrapes / 24h | 20 (6.6%) | 0 |
| `wht_tbm_ring_number` samples | 268 / 269 of 288 | 288 / 288 |
| max scrape duration | 48.3s | well under 30s |

Caveat on the window: `[24h]` is only a clean post-fix measurement once the
whole 24h falls after 06:30 UTC on 2026-09-29. Checked earlier in the day it
still contains pre-fix scrapes and the failure count will look worse than it is
— use a shorter window, or a `query_range` with an explicit start of
2026-09-28T06:30Z.

Judgement call: a handful of failures is not a regression, it is upstream. What
*would* mean the fix failed is the max duration creeping back toward 30s, which
would say the 5s resolver cliff is back and the budget is again being eaten. If
duration is fine but gaps persist, the resolver override is working and the
remaining failures are the retry budget giving up correctly — in which case the
fix is to raise `--retry-budget` and `scrape_timeout` together, not to swap DNS
again.

First `/metrics` request after an exporter restart is cold: for `tbm-exporter` it
does full discovery and takes ~5-9 s. `scrape_timeout` is 30 s, so that is fine,
but don't panic if the first scrape is slow. `snowy-tantangara-exporter` has no
discovery step, so its cold start is a single ~190 KB fetch and lands around
0.3-1 s.

## 5. Not done / open

- **The exporter loses ~6.6% of scrapes to intermittent DNS failure.** 19 of
  289 scrapes failed in the 24h to 2026-09-28, and 20 of the logged failures
  were `[Errno -3] Try again` — `EAGAIN` out of `getaddrinfo` resolving
  `utility.arcgis.com`, not a timeout (the 20s timeout is not close to being
  reached). Reproduced in the container: 4 of 40 lookups failed, and 40 lookups
  took 24.5s, ~0.6s each against ~1-20ms for healthy DNS. Consequences:
  `collect()` had no retry and no per-machine `try`, so the first machine to
  fail aborted the whole scrape and both TBMs were lost for that interval; and
  because the gauges were then omitted (see section 7) the ring series got
  real holes, which breaks any PromQL that needs "the previous sample".
  **Fixed** — see the exporter section below for what shipped and what the
  measurements ruled out.
- `GRAFANA_ADMIN_PASSWORD` still unset.
- **No Grafana dashboard for the Snowy reservoir series yet.** The
  `snowy_tantangara` job is scraped and the metrics are documented, but the only
  provisioned dashboard is `Western Harbour TBM`. The metrics that a panel would
  want already exist: `snowy_tantangara_level_percent` (unit `percent`,
  0–100), `snowy_tantangara_level_change_7d_percentage_points` (unit
  `percentagepoint` — the level of care Grafana's unit handling needs, see
  section 7), and
  `time() - snowy_tantangara_last_sample_timestamp_seconds` as the staleness
  panel.
- No recording or alerting rules; `prometheus.yml` has scrape configs only.
- The `Western Harbour TBM` dashboard is provisioned from
  `grafana/provisioning/dashboards/` (see section 7), but there is no
  provisioning of Grafana *users* or folders.
- The exporter has a `web.enable-lifecycle`-style reload nowhere; changing
  `prometheus.yml` needs `docker compose restart prometheus`.
- Host is not 24/7-managed: nothing restarts Docker or the stack on reboot
  beyond `restart: unless-stopped`. No systemd unit for the stack.
- **`patyegarang` has not published a survey report since 2026-09-25**, ~44 h at
  the time of writing, and it is *not* finished: 441 m of a 1560 m target, ring
  170. `barangaroo` over the same period is at 229 m of 1567 m, ring 84, and
  reports every ~10 min. This is upstream, not us — `wht_tbm_scrape_success` is
  1 and `wht_tbm_config_stale` is 0, so the exporter is scraping and serving
  fresh layer config. Either that TBM has stopped, or the tracker stopped
  publishing survey rows for it. The *Report age* panel is what makes it
  obvious; it goes red at 6 h. Unresolved — do not "fix" it by touching the
  exporter.

## 6. After the move (2026-09-27)

The stack was moved from `~` into `~/src/monitoring` and recreated there with
`docker compose up -d`. All three containers now bind-mount from
`~/src/monitoring`, the project name is still `bone` (pinned in the compose
file), and the TSDB and Grafana state came through in the existing volumes.

The first scrape after that restart took ~2m45s to land, which is the normal
first-scrape jitter for a 5m interval — `/api/v1/targets` showed
`health: unknown` with a zero `lastScrape` and an empty `lastError` until it
arrived. That is the symptom in section 3, not a fault.

Grafana used to carry a second, hand-made datasource `prometheus-1` alongside
the provisioned `Prometheus` datasource. It lived in the `grafana-storage`
volume from the pre-compose setup and pointed at `http://localhost:9090`, which
can never work — Grafana runs in a container, so `localhost` there is the
Grafana container itself. The dashboard panels were moved onto the provisioned
`Prometheus` datasource and `prometheus-1` is now gone, leaving one datasource.
If it ever comes back, its URL has to be the service name, not `localhost`.

## 7. Grafana provisioning

`grafana/provisioning/` is the whole Grafana config, bind-mounted read-only
from the host and applied on every boot:

```
grafana/provisioning/datasources/prometheus.yml            # the Prometheus datasource
grafana/provisioning/dashboards/default.yml                # the dashboard file provider
grafana/provisioning/dashboards/western-harbour-tbm.json   # the dashboard itself
```

**The datasource URL is `http://prometheus:9090`, not
`http://localhost:9090`.** Grafana reaches Prometheus over the compose network
by service name, for the same reason the exporter has to bind `0.0.0.0`
(section 3). The port is not published for Grafana's benefit; `9090:9090` is
only there for the Prometheus UI on the LAN.

**The datasource `uid` is pinned to `PBFA97CFB590B2093` and must stay in sync
with the uid in the dashboard JSON.** Grafana invents a random uid when the
provisioning file omits one, and the dashboard refers to the datasource by uid,
so an unpinned uid means the dashboard only works on whichever volume happened
to be created first. It is an ugly uid, but pinning it means a fresh volume
reproduces the same identity with no manual step. If you would rather have a
readable uid, change it in both files, delete the old datasource in the UI, and
let provisioning recreate it.

**The dashboard uid `adr468z` must stay stable too.** Provisioning matches an
existing dashboard by uid, so keeping it updates the dashboard in place;
changing it creates a second copy of the same dashboard.

`allowUiUpdates: true` lets a dashboard be saved from the UI. **The file only
wins once the file changes** — provisioning pushes the file into the database
when the file itself is newer, and leaves a UI save alone while the file is
untouched. So you can work in the UI and commit the result (the round trip
below); you just have to pull it back before someone edits the file, because
that next file change overwrites whatever the UI holds.

Earlier notes here claimed the opposite — that a UI edit was reverted within
`updateIntervalSeconds` (30). That is not what happens. A dashboard edited in
the UI on 2026-09-28 was still intact 36 minutes later, through roughly 72 sync
cycles, with the provisioning file unchanged since the day before.

**To adopt UI changes into git, pull the dashboard back out of the API — no
manual export needed.** This is the whole round trip, and it is what produced
the 2026-09-28 commit:

```bash
uid=adr468z   # or: curl -s -u admin:admin 'localhost:3000/api/search?type=dash-db'
curl -s -u admin:admin localhost:3000/api/dashboards/uid/$uid \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["dashboard"]; \
      d.pop("version",None); d["id"]=None; \
      print(json.dumps(d,indent=2,sort_keys=True))' \
  > grafana/provisioning/dashboards/western-harbour-tbm.json
```

Three things to strip or pin before it is a valid provisioning file, all done
above:

- `version` — instance state, not part of the model. Absent from the committed
  file, and Grafana manages it.
- `id` — the database row id of *this* Grafana instance. Reset to `null`, which
  is what the committed file carries. Otherwise a fresh volume tries to reuse
  the id.
- `uid` — deliberately **kept**, not stripped. It is what makes provisioning
  update the existing dashboard in place instead of creating a duplicate.

The API model is also not byte-identical to what a UI "Export JSON" gives you:
Grafana materialises defaults on save, so a round trip drops empty
`"mappings": []`, rewrites the first threshold step's `"value": null` to `0`,
and adds stat-panel keys like `showPercentChange`. That churn is normal and
harmless — it only means the first commit after a round trip touches panels
you did not change. Write it with `indent=2, sort_keys=True` to match the file
as it stands.

Check what you are about to commit before writing it, so you can tell your own
edits from Grafana's defaults:

```bash
curl -s -u admin:admin localhost:3000/api/dashboards/uid/adr468z \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["dashboard"],indent=2,sort_keys=True))' \
  > /tmp/live.json
diff -u grafana/provisioning/dashboards/western-harbour-tbm.json /tmp/live.json
```

The panel list itself carries a hazard: panels are ordered by `gridPos` `y`
then `x`, not by array position, so dragging a panel in the UI can leave the
JSON order looking shuffled while the layout is unchanged. Read `gridPos` when
diffing, not the array order.

Check a datasource from the host — the proxy is the real test, because it is
what Grafana itself uses:

```bash
curl -s -u admin:admin localhost:3000/api/datasources/uid/PBFA97CFB590B2093/health
curl -s -u admin:admin --get \
  'localhost:3000/api/datasources/proxy/uid/PBFA97CFB590B2093/api/v1/query' \
  --data-urlencode 'query=wht_tbm_progress_ratio'
```

To prove the files reproduce the stack from nothing, run a throwaway Grafana on
a fresh volume — it must come up with the same datasource uid and the same
dashboard, and no manual step:

```bash
docker run -d --name grafana-provision-test -p 127.0.0.1:3300:3000 \
  -v ~/src/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro \
  grafana/grafana
curl -s -u admin:admin localhost:3300/api/datasources
curl -s -u admin:admin 'localhost:3300/api/search?type=dash-db'
docker rm -f grafana-provision-test
```

Grafana 13 note: `PUT /api/dashboards/uid/<uid>` returns 404. Save dashboards
with `POST /api/dashboards/db` and the dashboard's numeric `id` instead.

The `Western Harbour TBM` dashboard has three timeseries panels: *Western
Harbour TBMs* in metres (`wht_tbm_distance_excavated_m`,
`wht_tbm_remaining_distance_m`, `wht_tbm_target_distance_m`), *TBM progress* as
a percentage (`wht_tbm_progress_ratio`), and *Rings*
(`wht_tbm_ring_number`). They are separate panels because the metres series,
the 0–1 ratio and the ring count do not share an axis sensibly.

*Western Harbour TBMs* and *TBM progress* are 13 rows tall in a 24-column grid,
*Rings* is 12 wide below the left one, and the two stats stack in the right-hand
column. *Western Harbour TBMs* hides the two `<tbm> excavated` series via a
`hideSeriesFrom` field override, so the excavated line is on its own axis
scale from the remaining and target distances.

*Rings* uses `lineInterpolation: stepAfter`, because the metric is a step
function: scraped every 5m but the ring only advances every few hours, so
24h of `barangaroo` is 270 samples carrying **3** distinct values (98.9%
duplicates). A stepped line shows each ring as its own step. This is a
rendering change only — it does not reduce the sample count, and at 270
points/series/day there is no performance reason to.

**There is no way to drop the duplicate samples in Prometheus or in a stock
Grafana transformation, and the obvious workaround is actively unsafe on this
data.** PromQL evaluates each timestamp independently, so it has no notion of
"the previous sample"; `offset` is a fixed *time* shift and only approximates
one. The approximation

```
wht_tbm_ring_number and (wht_tbm_ring_number != wht_tbm_ring_number offset 5m)
```

was measured on 24h of real data: it kept 1 of 270 samples and **silently lost
one of the two real ring changes**, because the series has holes (below) and
`and` drops any sample whose offset side is empty. The offset value also has to
be tuned to the panel's step grid — 5m/6m/10m/1h kept 1/1/3/22 samples. Of the
32 documented Grafana transformations, none collapses consecutive duplicates
(`Smoothing` does the reverse; `Filter data by values` compares against
constants, never the previous row).

**The holes were DNS failures, not an intrinsic property of the data.** 19 of
289 scrapes failed in 24h (6.6%), 20 of those failures logged
`[Errno -3] Try again` — `EAGAIN` from `getaddrinfo` on `utility.arcgis.com`.
Reproduced in the container: **4 of 40 lookups failed, and 40 lookups took
24.5s** (~0.6s each, against ~1-20ms for healthy DNS). `collect()` had no retry
and no per-machine error handling, so one machine's lookup failing aborted the
whole scrape and lost *both* TBMs. Fixed in the exporter: transient-failure
retries plus per-machine isolation, with `wht_tbm_info` retained from cache so
a failed poll still shows which TBM a hole belongs to. See section 5.

Two stat panels beside *Rings* show when each TBM last reported, in the viewer's
local timezone:

| Panel | Query | Unit |
| --- | --- | --- |
| *Last survey report* | `wht_tbm_last_report_timestamp_seconds * 1000` | `dateTimeAsIso` |
| *Report age* | `time() - wht_tbm_last_report_timestamp_seconds` | `dtdhms` |

**The two panels are formatted differently on purpose: one is an instant, one is
a duration, and Grafana has a different unit family for each. Do not
"simplify" them onto the same one.**

*Last survey report* shows an **instant** (when the report happened), so it uses
a Date & time unit. Those all take **milliseconds**, which is why the query is
multiplied by 1000 — the metric is Unix epoch *seconds* (the exporter divides
the ArcGIS millisecond value by 1000). Every Date & time formatter bottoms out
in one helper, and it is `moment().utc(value)`, so the input is milliseconds
exactly as with `moment(value)`; only `moment.unix()` would mean seconds:

```js
// grafana-data, date & time formatters
f  = (h,u) => { const P = n().utc(h); ... return P.local() }
LE = (h,u) => f(h, tz).format(...)   // dateTimeAsIso, dateTimeAsLocal, ...
fq = (h,u) => f(h, tz).fromNow()     // dateTimeFromNow -- see below, do not use here
```

*Report age* shows a **duration**, so it uses a duration unit. `dtdhms`
("duration (d hh:mm:ss)") takes **seconds**, so that query is left
unmultiplied and the staleness thresholds are plain seconds (1 h = `3600`,
6 h = `21600`). It renders `00:13:06` and `1 d 19:43:03`.

**Never use `dateTimeFromNow` for the age.** It is a Date & time unit, so it
reinterprets the number as an absolute instant, and it caused the second round
of wrong numbers here. It is a trap because it *nearly* works: an age in seconds
scaled to ms lands inside 1970, and moment's `fromNow()` buckets
(`m`/`mm`/`h`/`hh`/`d`/`dd`/`M`/`MM`/`y`/`yy`) read the 1970 instant back as a
plausible elapsed time — but only while it stays small. Past about a day the
reinterpretation crosses a bucket boundary and inverts:

| age | `* 1000` with `dateTimeFromNow` | correct, `dtdhms` |
| --- | --- | --- |
| 13 min | `1970-01-01 00:13` → "12 minutes ago" (right by luck) | `00:13:06` |
| 44 h | `1970-01-02 19:41` → **"57 years ago"** | `1 d 19:43:03` |

That is the shape of the bug: one TBM read correctly and the other said "57
years", which looks like a units error but is really a duration being formatted
as a date. A duration unit cannot be misread that way.

The general rule: **if the number is a point in time, scale seconds by 1000 and
use a Date & time unit; if it is an elapsed length, leave it in seconds and use
a duration unit.** The tells that you have picked wrong are a "seconds old"
figure that comes back in decades, and an age over ~1 day that reads as months
or years.

Other things that make *Last survey report* local time, all easy to break:

- The dashboard's `"timezone": "browser"` renders in the *viewer's* own zone.
  `f` above does `moment.tz.zone(tz)`, which is undefined for `browser`, so it
  falls through to `P.local()`. Set the dashboard timezone to `utc` and the
  panel silently switches to UTC.
- Both queries need `instant: true`. With a range query `time()` ramps across
  the range and a stat panel reduces it to an arbitrary point.

*Report age* is coloured by staleness — green under 1 h, orange under 6 h, red
beyond — so a TBM that has stopped publishing stands out. Thresholds compare
the raw field value, so they are in seconds to match the query.

**These two are separate panels on purpose. Do not merge them back into one
panel with two queries and a field override.** That was the first attempt: the
`dateTimeFromNow` unit landed on the *absolute timestamp* field and rendered
"57 years ago".

The override could not have worked anyway, because the two frames are
indistinguishable. Asking the datasource for both queries the way the panel does
returns `frame.name: null` and a field named `Time` for **both** of them —

```bash
python3 - <<'PY' > /tmp/two-queries.json
import json, time
ds = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}
q = [{"refId": r, "datasource": ds, "instant": True, "range": False,
      "format": "time_series", "legendFormat": "{{tbm}}", "expr": e}
     for r, e in (("A", "wht_tbm_last_report_timestamp_seconds"),
                  ("B", "time() - wht_tbm_last_report_timestamp_seconds"))]
print(json.dumps({"from": str(int((time.time() - 3600) * 1000)),
                  "to": str(int(time.time() * 1000)), "queries": q}))
PY
curl -s -u admin:admin -H 'Content-Type: application/json' -X POST \
  localhost:3000/api/ds/query --data-binary @/tmp/two-queries.json
# refId A | frame.name= None | field.name= 'Time'
# refId B | frame.name= None | field.name= 'Time'
```

`legendFormat` is applied in the frontend, not by the datasource, so neither
`byFrameRefID` nor `byName` has anything reliable to match on, and the override
silently resolved onto the first field instead of raising an error. One query
per panel means one field, so the panel's own default unit always applies and
there is no matcher to get wrong. *Last survey report* also uses
`colorMode: "none"`, which is what stops the raw 1.79e9 value from rendering as
a permanent red.

Note the metric name is `..._last_report_timestamp_seconds` (not
`..._timestamp`) — it is a gauge of epoch seconds, not a Prometheus timestamp
type.

The dashboard has `"refresh": "5m"`, so these panels re-query on that interval
rather than only on load. A 5 m refresh against a 5 m `scrape_interval` is fine
here — the report times only move when the tracker publishes. Note that refresh
re-runs the *queries* only: a tab that is already open keeps the dashboard model
it loaded, so a panel edit here needs a page reload before the browser picks it
up. Check `version` in the API when a panel "did not change":

```bash
curl -s -u admin:admin localhost:3000/api/dashboards/uid/adr468z \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["dashboard"]; print(d["version"], [(p["id"],p["title"],p["fieldConfig"]["defaults"].get("unit")) for p in d["panels"]])'
```

To check these panels without a browser, run the queries through Prometheus and
do the arithmetic yourself — they are plain PromQL:

```bash
curl -s --get --data-urlencode \
  'query=wht_tbm_last_report_timestamp_seconds * 1000' \
  localhost:9090/api/v1/query
curl -s --get --data-urlencode \
  'query=time() - wht_tbm_last_report_timestamp_seconds' \
  localhost:9090/api/v1/query
```
