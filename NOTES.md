# NOTES — monitoring stack

Working notes for whoever picks this up next (including future me).

## 1. Layout as of 2026-10-08

```
~/src/monitoring/                                    # this project
~/src/monitoring/docker-compose.yml                  # 6 services, project name "aus_infra_dashboard"
~/src/monitoring/prometheus.yml                      # scrape configs: tbm-exporter:9109, reservoir-exporter:9110, battery-exporter:9111, aemobattery-exporter:9112
~/src/monitoring/.env.example                        # tracked template for the secrets below
~/src/monitoring/.env                                # UNTRACKED, mode 600, holds GRAFANA_ADMIN_PASSWORD + OPENEA_API_KEY
~/src/monitoring/grafana/provisioning/datasources/prometheus.yml
~/src/monitoring/grafana/provisioning/dashboards/battery-state-of-charge.json  # GENERATED, see tools/
~/src/monitoring/tools/build_battery_dashboard.py   # writes the file above
~/src/monitoring/scrapers/wht_tbm_exporter.py        # the WHT scraper (stdlib only, no image build)
~/src/monitoring/scrapers/snowy_tantangara_exporter.py  # the Snowy Hydro reservoir scraper
~/src/monitoring/scrapers/oe_battery_exporter.py     # the NEM battery scraper (OpenElectricity, bearer token)
~/src/monitoring/scrapers/aemo_battery_exporter.py   # the AEMO NEMWEB battery storage scraper
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
| containers | `wht-tbm-exporter`, `snowy-tantangara-exporter`, `openelectricity-battery-exporter`, `aemonemweb-battery-exporter`, `prometheus`, `grafana` |
| compose project | `aus_infra_dashboard` (pinned via `name:` in the compose file) |
| network | `aus_infra_dashboard_monitoring` (bridge) |
| volumes | `prometheus-data`, `grafana-storage` (external, pre-existing), `exporter-cache`, `battery-fleet-cache`, `aemo-state` (compose-created) |

`exporter-cache` belongs to `tbm-exporter` alone — it holds that exporter's
discovered ArcGIS layer config. `battery-fleet-cache` holds only the battery
exporter's fleet metadata, and is a *separate* volume rather than a second file
in `exporter-cache`: two exporters sharing a volume means one of them
clobbering the other's file, and there is nothing to gain from the coupling.
`aemo-state` is the same arrangement for `aemobattery-exporter`'s state file
(`/cache/state.json`), kept separate so the AEMO and OE exporters cannot
clobber each other. `snowy-tantangara-exporter` deliberately mounts **no**
volume: it has no discovery step, and a persistent cache of reservoir levels
would be a way to serve a stale reading as a current one.

Note the container/service naming asymmetry, now four deep: the service is
short (`battery-exporter`, `aemobattery-exporter`) and the container is spelled
out (`openelectricity-battery-exporter`, `aemonemweb-battery-exporter`). All
`container_name`s are pinned outright rather than derived from the project
name, which is why renaming the project leaves them alone — and why a second
stack cannot run alongside this one under any name.

## 2. If files have moved

Re-locate before assuming:

```bash
docker compose ls                       # which compose file is actually running where
docker inspect wht-tbm-exporter --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect wht-tbm-exporter --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
grep -rl 'wht_tbm_exporter' ~ 2>/dev/null
```

Then fix, in this order:

1. **The bind mounts in `docker-compose.yml`** — paths are resolved
   relative to the compose file, not the CWD: the four
   `./scrapers/*_exporter.py` scripts, `./prometheus.yml`,
   `./grafana/provisioning`.
2. **The project name.** It defaults to the compose file's directory name, so
   moving the file changes the project name and the network name — and the
   project-prefixed volume name (`aus_infra_dashboard_exporter-cache`). Either
   keep the directory name, or pin it with `name:` in the compose file /
   `-p aus_infra_dashboard` on the command line, and recreate:
   `docker compose up -d --force-recreate`.
3. **Container names are pinned** in the compose file (`container_name:`), so a
   second project with different names will collide on `prometheus` /
   `grafana`. Remove the old ones first (`docker rm -f prometheus grafana`).
4. Doc references to the compose file by absolute path — this file and
   `README.md` both assume you are running from `~/src/monitoring`.

Note that a `docker compose restart` reuses the *existing* container, so it keeps
the old bind mounts. Only `up -d` recreates containers and picks up new paths.

### Renaming the project

The same hazard, deliberately invoked: changing the `name:` in the compose file
renames the project, and with it the network and the project-prefixed
`exporter-cache` volume. Nothing else in the stack refers to the project name —
Prometheus and the Grafana datasource address the exporters by service name over
the compose network, and `prometheus-data` / `grafana-storage` are external
volumes with explicit names, so the TSDB and Grafana state are untouched.

The trap is that after the edit, `docker compose down` resolves to the *new*
project name and leaves the old one running, and the old containers still hold
the pinned `container_name` values. Bring the old project down first, naming it
explicitly:

```bash
docker compose -p <old-name> down       # before editing the file
# edit name: in docker-compose.yml
docker compose up -d
docker volume rm <old-name>_exporter-cache
```

`down` takes the old network with it and leaves the old project-prefixed volume
behind, since removing volumes is opt-in (`-v`). Nothing else is left over.

The only thing actually lost is the exporter's cached ArcGIS layer config, and
it re-discovers on the first cold scrape. That is worth a minute of upstream
load but nothing more — unless ArcGIS is down at that moment, in which case the
exporter starts with no config at all and has nothing to fall back to. Copy
`config.json` out of the old volume first if that matters.

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
- **An API key was parked in a loose `keys.txt` in the repo root.** On
  2026-09-30, having signed up for the free OpenElectricity Community plan, the
  `oe_…` key was dropped into `keys.txt` — untracked, but unignored, so one
  `git add .` away from being published. Fixed properly rather than by adding
  one ignore line:

  - moved the value into `.env` (gitignored, created with `O_CREAT` at mode
    `0600` rather than `chmod`ed afterwards, so there is no window in which the
    file is world-readable) and deleted `keys.txt`;
  - added `.env`, `.env.*` and a **negated** `!.env.example` to `.gitignore`.
    Order matters — `.env.*` matches `.env.example`, so the negation has to come
    after it or the template is ignored too and a cloner gets nothing;
  - added `.env.example`, the tracked half of the pair, so a cloner is told
    which variables to set without anyone shipping a real value;
  - **no rotation was needed.** `git log --all -- keys.txt` was empty and
    `git ls-files keys.txt` errored, so the key was never in an object. Check
    that *before* assuming a secret is burned — rewriting history to remove a
    key that was never committed is wasted work that loses history. Confirmed
    afterwards by searching the key against every object on every ref (115
    objects, 11 commits) and the 3 dangling blobs: no match.

  Two things that were over-engineered and have been removed rather than left
  to rot. An ignore rule for `keys.txt` by name: the file is gone, and
  ignoring a name that no longer exists only invites someone to wonder where it
  went. And a `secrets/` entry, added on the strength of a Compose `secrets:`
  mention that turned out to be an option being considered, not one that had
  been chosen — there is no such directory. Both were guesses dressed up as
  precautions. If a `.env`-based mechanism ever does turn out to be the wrong
  one, the correct time to add an ignore rule for the replacement is when the
  replacement exists, not in anticipation of it.

  The trap to remember: **`docker compose config` prints secrets in the clear.**
  Compose interpolates `.env` into the rendered model, so with
  `GRAFANA_ADMIN_PASSWORD` set that command emits
  `GF_SECURITY_ADMIN_PASSWORD: <value>` (verified on this host). Anything a
  service consumes through `environment:` is exposed by it. `OPENEA_API_KEY` is
  currently *not*, and the reason is not safety — it is simply that no service
  references it yet. The day one does, that exemption is gone. Same for anything
  that logs the rendered config; do not paste it into an issue.

  Not fixed, and the reason it is worth writing down: the key is a *personal*
  credential on a *shared-quota* free plan. Anyone who clones this repo has to
  bring their own, which is what `.env.example` is for, but nothing stops a
  future change from hardcoding a key in `docker-compose.yml` where it would be
  committed for real. If the NEM exporter is ever wired up, pass it through
  `environment:` from `.env`, or use a Compose `secrets:` entry read from a
  gitignored file — a bind-mounted secret never appears in `docker inspect`.

## 4. Verify the stack

```bash
docker compose ps                                   # all four (healthy)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
curl -s --get --data-urlencode 'query=up{job="wht_tbm"}' localhost:9090/api/v1/query
# Lookback-safe: snowy_tantangara is hourly, an instant query only looks back 5m.
curl -s --get --data-urlencode 'query=last_over_time(up{job="snowy_tantangara"}[2h])' \
  localhost:9090/api/v1/query
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
- `GRAFANA_ADMIN_PASSWORD` still unset. `.env` now exists to hold it (see
  section 3) but the variable is still blank in it, so Grafana is **still on the
  `admin`/`admin` default** on a LAN-published port. The one-line fix, and the
  reason it has stayed open for days is that it was never written down as a
  task.
- ~~**No NEM exporter.**~~ **Done 2026-09-30** — `battery-exporter`, serving
  state of charge for the largest 10 NEM batteries that publish data, with the
  `OPENEA_API_KEY` plumbing and a dashboard. Both of the concerns that item
  raised were the wrong shape and are worth recording as such: the limit that
  actually binds is not the 500 credits/day but the plan's **rate buckets**
  (8/5min, 32/hour, 366/day), and the answer is not "scrape less often" but
  "stop tying the poll to the scrape at all". See section 8. Generation, price
  and emissions are still not exported — only battery storage.
- **The battery dashboard is generated, so a UI edit to it will be lost.**
  `grafana/provisioning/dashboards/battery-state-of-charge.json` is written by
  `tools/build_battery_dashboard.py`; the provider has `allowUiUpdates: true`,
  so a save in the Grafana UI appears to work and is then overwritten the next
  time the file changes on disk. Edit the script and re-run it, exactly as
  section 7 describes for the other two.
- No recording or alerting rules; `prometheus.yml` has scrape configs only.
- The dashboards are provisioned from `grafana/provisioning/dashboards/` (see
  section 7), but there is no provisioning of Grafana *users* or folders.
- **Editing `prometheus.yml` needs `curl -XPOST localhost:9090/-/reload`, not a
  container restart.** `--web.enable-lifecycle` is in the Prometheus command in
  `docker-compose.yml` and the endpoint answers 200. This note previously said
  the reload endpoint did not exist here; that was wrong, and it mattered:
  Prometheus was found still running the old config with only the `wht_tbm` job
  loaded, minutes after a new job had been added to the bind-mounted file. If
  `/-/reload` ever stops answering, fall back to `docker compose restart
  prometheus`, and expect the first scrape after any restart to be offset.
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
file), and the TSDB and Grafana state came through in the existing volumes. The
pin is what made the move cheap: the network kept its name, so nothing had to be
re-plumbed. Two days later the name itself was changed — see section 8.

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
grafana/provisioning/dashboards/western-harbour-tbm.json   # WHT TBM progress dashboard
grafana/provisioning/dashboards/snowy-tantangara.json      # Snowy reservoir levels dashboard
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
changing it creates a second copy of the same dashboard. This applies to every
dashboard, and there are now two: `adr468z` for the TBM dashboard and
`snowy-tantangara` for the reservoir one. Neither uid may be reused for a
different dashboard either — `snowy-tantangara` is spelled out in full rather
than using a Grafana-style random uid precisely so it reads as what it is.

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
*Rings* is 12 wide below the left one, and four stats stack in the right-hand
column: *Report age* and *Last survey report* at y=13 and y=17, then *Advance
rate* and *Estimated completion* at y=21 and y=25. *Western Harbour TBMs* hides
the two `<tbm> excavated` series via a `hideSeriesFrom` field override, so the
excavated line is on its own axis scale from the remaining and target distances.

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
| *Advance rate* | `delta(wht_tbm_distance_excavated_m[$__range_s]) * 3600 / $__range_s` | `suffix:m/h` |
| *Estimated completion* | see below | `dateTimeAsIso` |

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

#### Advance rate and estimated completion

Two stats added 2026-09-29 at y=21 and y=25, continuing the right-hand column.
Both measure over **the dashboard's current time range**, not a fixed window, so
changing the time picker is how you ask for a different average. That is the
whole point of them: there is no single correct advance rate for a TBM that has
been up and down, and a fixed-window query would quietly pick one for you.

```
# Advance rate, metres per hour
delta(wht_tbm_distance_excavated_m[$__range_s]) * 3600 / $__range_s

# Estimated completion
(time() + wht_tbm_remaining_distance_m
          / (delta(wht_tbm_distance_excavated_m[$__range_s]) * 3600 / $__range_s)
          * 3600) * 1000
  and on(tbm) (delta(wht_tbm_distance_excavated_m[$__range_s]) * 3600 / $__range_s > 0)
```

Four things are load-bearing in that.

**`delta()`, not `rate()` or `increase()`.** `wht_tbm_distance_excavated_m` is a
gauge. `rate()`/`increase()` exist to turn a counter into a rate that survives
resets, and they *assume* a series only ever increases: a decrease is read as a
counter reset and the pre-dip value is added back into the total, so an upstream
survey correction would come out as an inflated advance rather than the dip that
actually happened. `delta()` is the function for "how much did this gauge move
across this window" and applies no reset logic at all.

**`$__range_s`, not `$__rate_interval`.** This is the inverse of the rule in the
Snowy section below, and worth stating side by side: `$__rate_interval` is what
Grafana derives from the panel's `Min interval` and the scrape interval, and the
datasource's `timeInterval: 5m` would clamp it to `5m` — turning both queries
into a constant 5-minute rate no matter what time range is selected. The range
has to come from the time picker, so it is `$__range_s`, an integer number of
seconds Grafana substitutes before the query is sent.

**The `* 3600` on the ETA is not a units detail, it is the whole calculation.**
`time()` is epoch **seconds**, while `remaining / rate` is in **hours**, because
the rate is metres per *hour*. Adding them as-is produces a timestamp about an
hour after now, which is a valid-looking date and completely wrong. Then the
`* 1000` on the outside converts to milliseconds for the `dateTimeAsIso` unit,
exactly as in *Last survey report* — same rule, same reason.

**The `and on(tbm) (... > 0)` guard suppresses an infinite ETA.** If the TBM has
not advanced inside the selected range the divisor is zero and the result is
`+Inf` (verified directly against Prometheus). Inf milliseconds is
`Invalid Date` in moment, so the panel would render that. Filtering on the rate
being positive returns no series instead, and the panel reads **No data** —
which is the truth: with no advance there is no rate to extrapolate from. `on(tbm)`
is explicit rather than relying on both sides carrying the same
`instance`/`job`/`tbm` labels, which they do.

Measured across ranges on 2026-09-29, to show how much the window matters:

| range | barangaroo rate | barangaroo ETA | patyegarang ETA |
| --- | --- | --- | --- |
| now-1h | 0.02 m/h | 2033-01-27 | 2026-12-15 |
| now-6h | 0.41 m/h | 2027-02-10 | 2026-11-24 |
| now-24h | 0.70 m/h | 2026-12-16 | 2026-12-13 |
| now-7d | 0.14 m/h | 2027-10-17 | 2027-06-22 |

That spread is the feature, not a bug, but it is why the panel description says
the estimate is only as good as the window. The 7d row is also bounded by the
data: Prometheus has only held this job for ~66 h, so `now-7d` and `now-30d` are
effectively "everything we have". That will change as the TSDB fills.

**A range shorter than the 5m scrape interval returns no data, not a zero.**
`delta()` needs two samples in the window; at `[300]` and `[60]` the query comes
back empty and the panel reads **No data**. That is the honest outcome — a real
zero would claim the TBM has stopped. `clamp_min($__range_s, 600)` is not an
option here, and neither is any other expression: a range selector duration has
to be a literal, so Prometheus rejects it with
`parse error: unexpected character in duration expression: 'c'` (the `c` of
`clamp_min`).

**`suffix:m/h` is a custom unit, not a built-in one, and it is worth knowing why
it parses.** There is no metres-per-hour id in the Grafana unit catalogue, so
`getValueFormat` falls through its index lookup to the custom-unit branch —
verified in the shipped bundle, `valueFormats.ts` in the container's
`grafana-data` source map:

```js
let idx = id.indexOf(':');
if (idx > 0) {
  const key = id.substring(0, idx);
  const sub = id.substring(idx + 1);
  if (key === 'prefix') { return toFixedUnit(sub, true); }
  if (key === 'suffix') { return toFixedUnit(sub, false); }
```

The split is on the **first** colon and `sub` is everything after it, so the `/`
in `m/h` is carried through untouched. `prefix:m/h` would be equally valid.

Neither panel is coloured (`colorMode: "none"`, one neutral `text` threshold
step), matching *Last survey report*. A rate is a measurement rather than a
status, and the stall signal already has a home in *Report age*.

They are **two panels, one query each**, for the same reason *Last survey report*
and *Report age* are not merged: a rate in `suffix:m/h` and a Date & time value
would need different unit families on one panel, and that is the override that
cannot be made to work reliably (see the two-frames-both-called-`Time` note
above).

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

**The two range-dependent queries cannot be checked that way, because
`$__range_s` is a Grafana variable and Prometheus rejects it** — so the file has
to be read, the variable substituted by hand, and the result run through
Grafana's own proxy (which is also the only way to prove the datasource path
works, and to prove the *No data* cases above are reachable rather than
hypothetical):

```bash
python3 - <<'PY'
import json, math, time, urllib.request, datetime

d = json.load(open("grafana/provisioning/dashboards/western-harbour-tbm.json"))
ds = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}
for panel in d["panels"]:
    if panel["id"] not in (6, 7):
        continue
    print("\n=== %s  (unit %s) ===" % (panel["title"],
          panel["fieldConfig"]["defaults"]["unit"]))
    for secs in (300, 3600, 21600, 86400, 604800):
        expr = panel["targets"][0]["expr"].replace("$__range_s", str(secs))
        body = json.dumps({
            "from": str(int((time.time() - secs) * 1000)),
            "to": str(int(time.time() * 1000)),
            "queries": [{"refId": "A", "datasource": ds, "instant": True,
                         "range": False, "format": "time_series",
                         "legendFormat": "{{tbm}}", "expr": expr}]}).encode()
        req = urllib.request.Request(
            "http://localhost:3000/api/ds/query", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Basic YWRtaW46YWRtaW4="})
        res = json.load(urllib.request.urlopen(req))
        # An empty result still comes back as one frame, with no fields.
        frames = [f for f in (res["results"]["A"].get("frames") or [])
                  if f["schema"]["fields"]]
        if not frames:
            print("  %-8s NO DATA" % secs); continue
        for f in frames:
            tbm = f["schema"]["fields"][1]["labels"]["tbm"]
            v = float(f["data"]["values"][1][0])
            if math.isinf(v) or math.isnan(v):
                out = str(v) + "   <-- would render as an invalid date"
            elif v > 1e9:
                out = datetime.datetime.fromtimestamp(v / 1000, datetime.UTC).isoformat()
            else:
                out = "%.3f m/h" % v
            print("  %-8d %-12s %s" % (secs, tbm, out))
PY
```

If that ever prints `+Inf`, the `and on(tbm) (... > 0)` guard has been dropped
from panel 7 and the panel will show **Invalid Date** in the browser.

### The `Snowy Hydro reservoir levels` dashboard

`grafana/provisioning/dashboards/snowy-tantangara.json`, uid `snowy-tantangara`,
tags `snowy-hydro`/`reservoir`. Second dashboard in this stack; the first was
hand-built in the UI and pulled back, this one was generated.

Its headline is the one thing a reader must not get wrong, so it is a `text`
panel in the top-left, above the data: **these are water levels, not Snowy Hydro
2.0 construction progress.** No 2.0 machine-readable status exists at all
(section 5, and the audit in `scrapers/NOTES.md`). It also states that the feed
publishes **once a day at about 07:00 Sydney**, which is what explains the flat
stretches in both charts.

Ten panels in a 24-column grid: a 5-row text header, a row of four 6-wide stats,
a full-width 11-row level chart, a row of three 8-wide stats, and a full-width
9-row weekly-change chart.

| Panel | Query | Unit |
| --- | --- | --- |
| *Tantangara level* (stat and chart) | `snowy_tantangara_level_percent` | `percent` |
| *Change, last 7 days* / *Weekly change* | `snowy_tantangara_level_change_7d_percentage_points` | `percentagepoint` |
| *YTD range* (2 queries, 1 panel) | `..._level_min_ytd_percent`, `..._level_max_ytd_percent` | `percent` |
| *Exporter scrape OK* | `snowy_tantangara_scrape_success` | `none` |
| *Last reading* | `snowy_tantangara_last_sample_timestamp_seconds * 1000` | `dateTimeAsIso` |
| *Reading age* | `time() - snowy_tantangara_last_sample_timestamp_seconds` | `dtdhms` |
| *Scrape duration* | `snowy_tantangara_scrape_duration_seconds` | `s` |

Unit notes, all of which cost something to get wrong:

- **`percent`, not `percentunit`.** These gauges are 0–100 already, and
  `percentunit` is the unit for a 0–1 ratio, which would render 11.33 as
  1133%. The TBM dashboard's *TBM progress* panel is the 0–1 case and uses
  `percentunit`; these are not.
- **`percentagepoint` for the weekly change**, and the axis label says
  `pp / 7 days`. Snowy's own copy quotes this figure as "-2.73 m/week", but the
  endpoint publishes no volume, so it is percentage points and **not metres**. The
  panel description repeats that, because the upstream wording invites the wrong
  reading.
- ***Last reading* and *Reading age* are separate panels** for exactly the reason
  set out above for *Last survey report* and *Report age*: one is an instant
  (Date & time unit, `* 1000`) and one is a duration (`dtdhms`, seconds). Same
  trap, same fix. `dateTimeFromNow` is not used anywhere on this dashboard.
- *YTD range* **is** two queries in one panel, and it is safe only because both
  series are percentages. The seconds-vs-milliseconds case above is the one that
  cannot be overridden, since `legendFormat` is applied in the frontend and both
  frames come back as an indistinguishable field named `Time`.

Both charts use `lineInterpolation: stepAfter`, for the same reason *Rings* does:
the level is a step function, taken once a day and then held, so a straight line
between points would draw intermediate levels that were never published.

Both charts set `spanNulls: true`, so a line is drawn across a missing sample.
This was **changed in the UI on 2026-09-29 and adopted into the file**; it was
originally written as `false` here on the argument that a gap *is* the signal,
since the exporter drops the gauges rather than serving a stale value, so a break
in the line means a failed scrape. That reasoning still holds, so be deliberate
about which you want: with `spanNulls: true` a failed scrape is bridged and the
line looks continuous, and the *Exporter scrape OK* stat becomes the only place
that failure is visible. With `false` the break is self-evident. The TBM
dashboard has always used `true`, so `true` here is at least consistent across
the two.

**Every query on this dashboard is wrapped in an `over_time` function, and that is
load-bearing, not decoration. Do not unwrap them.** Prometheus's instant-query
**lookback delta is 5 minutes**, and that 5 minutes applies *per evaluation step*
of a range query too — not just to instant queries. This job is scraped hourly,
so its samples are ~60 minutes apart, which is 12× the lookback. Measured on the
real data, bare selectors over the dashboard's default `now-7d` range:

| step | bare selector | `last_over_time(...[2h])` |
| --- | --- | --- |
| 300s | 5 points | 51 points |
| 600s | 5 points | 26 points |
| 1800s | **0 points** | 9 points |
| 3600s | **0 points** | 5 points |
| 7200s | **0 points** | 3 points |

The bare selector goes empty as soon as the step exceeds the lookback, because no
step window `[t-5m, t]` happens to contain a sample. Grafana picks the step from
the panel width and the range, so over `now-7d` it is comfortably over 600s and
**both charts rendered completely empty** until this was fixed. The stat panels
had the same defect for a different reason — an instant query with a 1 h
`scrape_interval` finds nothing unless it runs within 5 minutes of a scrape, so
all seven of them were blank for ~55 minutes of every hour.

This was nearly missed because the first verification happened to run *seconds*
after a scrape landed, which is the one moment a bare hourly selector does return
data. A one-off check right after provisioning is not evidence for an hourly job.

The wrappers are not all the same, deliberately:

- `last_over_time(...[2h])` for the level, change, YTD, scrape duration, last
  reading, and both charts. 2 h clears the 1 h interval plus its offset jitter.
  On the charts this also has the side benefit of drawing the reading as a held
  step across the gaps between hourly scrapes, which is what the value actually
  is. Staleness still shows: the exporter drops the gauges on a failure, so the
  line ends ~2 h after the feed stops rather than flatlining.
- `min_over_time(snowy_tantangara_scrape_success[2h])` for *Exporter scrape OK*.
  `min` rather than `last`, so **any** failed scrape in the window turns it red,
  and it goes empty if the target disappears altogether — a plain
  `last_over_time` would keep showing a stale `1` for 2 h after the exporter
  died, which is the one case that panel exists to catch.
- `time() - last_over_time(...[7d])` for *Reading age*, and **not** the 2 h the
  rest use. This panel has to keep answering when the feed goes quiet. The
  timestamp gauge is dropped on failure like the others, so a 2 h window would
  make it go blank exactly when its answer matters; 7 d lets the age keep
  counting up and cross the 36 h / 48 h thresholds.

If you ever add a metric here, wrap it on that basis. The general rule is
**scrape_interval > 5m needs the wrapper, scrape_interval ≤ 5m does not** — the
`wht_tbm` job is scraped every 5m, so a sample falls inside every 5m step window
and its bare selectors are fine at any step (verified at 300s through 3600s over
24h). That job's dashboard is deliberately left unwrapped. If you ever drop a
job's scrape interval below its publication frequency, this is what changes. If you want staleness
expressed as *line geometry* rather than a number — a threshold keyed to the
sampling frequency, so the line breaks only across genuinely long gaps — that is
expressible in PromQL and was verified: `last_over_time(level[2h]) and (time() -
last_over_time(ts[2h]) < 0)` returns **empty**, so `and` with a freshness
condition does suppress the series, and a recording rule would turn that into
real gaps in the TSDB. It is not used, because it needs recording rules (this
stack has none, section 5) and a blank line cannot distinguish "feed late" from
"exporter dead" from "no data yet", whereas the *Reading age* number is
unambiguous. Grafana has no distance-aware null handling of its own: `spanNulls`
is a boolean, Off or On, with no threshold to set.

**Every stat panel carries an explicit `{"mode": "absolute", "steps": [...]}`
thresholds object.** Writing `defaults["thresholds"] = steps` instead of
`{"mode": "absolute", "steps": steps}` — a natural thing to write — is accepted
by Grafana with **no error at all**. Tested on a throwaway instance: the save
returns `status: success` and the list is stored back verbatim, not normalised
into the object shape. The trap is that the stored model then *looks* correct,
because the colours and values are all there in the diff; it is only the missing
`{"mode", "steps"}` wrapper that gives the threshold logic nothing to read, so
the panel quietly loses its colouring. Everywhere else thresholds appear is that
object, including every panel of the TBM dashboard. The generator this dashboard
was built with made exactly this mistake first.

Only two panels have thresholds that mean anything:

- *Exporter scrape OK* — red below 1, green at 1 and above, `colorMode: value`.
- *Reading age* — green, orange at 36 h, red at 48 h. The feed is **daily**, so
  this value oscillates between roughly 0.5 h and 31.5 h by design. The TBM
  dashboard's thresholds are 1 h and 6 h and would go red every night.

The rest get a single neutral step (`{"color": "text", "value": null}`) with
`colorMode: none`, so no colour chip is drawn. This is not cosmetic: the
materialised defaults copied from the TBM dashboard carry **0 / 3600 / 21600**
steps, which are *seconds of report age*. Left in place they would sit on a
percent, on a date, and on a sub-30 s duration, where they can never fire —
harmless in the UI, and actively misleading to anyone reading the file.

`"refresh": "1h"`, not the TBM dashboard's `5m`. The upstream publishes once a
day, so a 5 m refresh only re-runs identical queries and invites the reader to
watch a number that is not moving. The default range is `now-7d`, which is about
seven data points on the level chart — the Prometheus history for this job only
begins 2026-09-29 even though the upstream feed itself goes back to 1954, and
that ceiling is stated in the chart's own description rather than left to be
discovered.

The `reservoir` template variable is
`label_values(snowy_tantangara_level_percent, reservoir)` and **every per-reservoir
query filters on `{reservoir="$reservoir"}`.** The exporter defaults to
Tantangara alone but supports `--all-reservoirs`, so a variable that filtered
nothing would silently show the first reservoir if that flag were ever set.

**The `timeInterval: 5m` on the datasource does not match this job's 1 h
`scrape_interval`,** and that is fine here: no query on this dashboard uses
`rate()` or `irate()`, so `$__rate_interval` never appears, and a plain selector
does not care about the step. The one place it would matter is `$__interval` on a
range query, which would ask for 5 m resolution against 1 h data. The range
queries therefore carry an explicit range. This is commented at the setting.

**The committed JSON is the source of truth; the generator is not kept.** It was
a throwaway script, and it only existed because Grafana materialises several
hundred default keys per panel, which are near-impossible to hand-write
correctly. It worked by deep-copying `fieldConfig.defaults`, `options` and the
annotation list out of `western-harbour-tbm.json` and overriding only what
differs, so the two dashboards stay structurally identical. Editing in the UI
and pulling back through the round trip above is the supported path from here.

**This file has since been round-tripped from the UI, so it is in the same
materialised form as `western-harbour-tbm.json` and should stay that way.** A
round trip on 2026-09-29 brought in Grafana's own keys: `showMiniMap` on the text
panel, an empty `targets` on it, `allowCustomValue` and `regexApplyTo` on the
variable, the variable's `current.selected` removed now it is resolved, empty
`overrides` pruned, and every first threshold step rewritten `null` → `0`. None
of that is a manual edit, and all of it is what Grafana writes on any save — so
writing the clean generated form instead would just guarantee the next UI save
produced a confusing diff. The `null` → `0` rewrite lands on the **first** step
only, which is harmless: a first step's `value` is the implicit base of the scale
and is never read, so `[{"color": "red", "value": 0}, {"color": "green",
"value": 1}]` means the same as the `null` it replaced. No later step in this
dashboard carries a `null`, and if one ever did, that would be the meaningful red
"below" bound and must be left alone.

The API reports `meta.provisioned: false` for **both** dashboards while
`meta.provisionedExternalId` is set to the filename. That is a quirk of this
Grafana version, not a sign the file was not provisioned — check
`provisionedExternalId`, not `provisioned`. Provisioning picked the file up with
no restart, inside the 30 s `updateIntervalSeconds` window, as intended.

Verify without a browser:

```bash
curl -s -u admin:admin localhost:3000/api/dashboards/uid/snowy-tantangara \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["meta"]["provisionedExternalId"], len(d["dashboard"]["panels"]))'
# Every expr in the file, run the way the panel runs it: stats as instant
# queries, charts as range queries over now-7d, and with $reservoir substituted.
# This prints OK/EMPTY, which is the check that would have caught the lookback
# bug -- a bare `status: success` proves nothing, since an empty result is also
# success.
python3 - <<'PY'
import json, pathlib, time, urllib.parse, urllib.request

d = json.loads(pathlib.Path("grafana/provisioning/dashboards/snowy-tantangara.json").read_text())
now = time.time()
bad = 0
for p in d["panels"]:
    for t in p.get("targets", []):
        q = t["expr"].replace("$reservoir", "Tantangara Reservoir")
        if p["type"] == "stat":
            url = "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": q})
        else:
            url = "http://localhost:9090/api/v1/query_range?" + urllib.parse.urlencode(
                {"query": q, "start": now - 7*86400, "end": now, "step": 1800})
        res = json.load(urllib.request.urlopen(url))
        r = res["data"]["result"]
        # An instant query returns "value" (singular); a range query "values".
        n = (1 if r and "value" in r[0] else len(r[0].get("values", []))) if r else 0
        bad += not n
        print("%-6s %-5s %s" % ("OK" if n else "EMPTY", p["type"][:5], t["expr"]))
print("empty:", bad)
PY
```

## 8. Project renamed `bone` -> `aus_infra_dashboard` (2026-09-29)

`name:` in the compose file was `bone`, inherited from the days when the stack
lived directly in `~` and the project name defaulted to the home directory. It
had outlived both: the stack moved to `~/src/monitoring` in section 6, and the
repo is `aus_infra_dashboard`, which the name matched nowhere. Renamed to match
the repo, using the procedure in section 2.

What moved and what did not:

| Object | Before | After |
| --- | --- | --- |
| project | `bone` | `aus_infra_dashboard` |
| network | `bone_monitoring` | `aus_infra_dashboard_monitoring` |
| exporter cache volume | `bone_exporter-cache` | `aus_infra_dashboard_exporter-cache` |
| TSDB | `prometheus-data` (external) | unchanged |
| Grafana state | `grafana-storage` (external) | unchanged |
| containers | pinned via `container_name:` | unchanged names, new project labels |

The only data at risk was the exporter's cached ArcGIS layer config, and it
came back byte-identical: the re-discovery resolved the same dashboard item
`973e08367a544c879edd7f345f9d6a15` and wrote a config differing only in
`discovered_at`. Worth remembering that this outcome was luck as much as
design — `config.json` is a fallback for when discovery fails, so a rename
taken while ArcGIS is unreachable leaves the exporter with no config at all
rather than a stale one. Copy the file out first if that matters.

Downtime was one `up -d`, about 15s. Historical samples survived: a query at
`now-2h` still returns `up{job="wht_tbm"} == 1` from before the rename.

Two things that looked like faults and were not:

- `/api/v1/targets` showed both jobs `health: unknown` with a zero `lastScrape`
  for ~3m20s. That is the first-scrape jitter already described in section 6,
  not a networking fault from the new project.
- `snowy_tantangara` was still `unknown` when `wht_tbm` had already come up. Its
  `scrape_interval` is 1h, so where its first scrape lands in that hour is
  arbitrary; here it was ~2m15s after the 5m job's, which is pure jitter and
  nothing to chase. The exporter itself was healthy the whole time.

The old network was removed by `down` itself and the old volume by an explicit
`docker volume rm`, which is the only cleanup step in the section 2 procedure.

## 9. NEM battery exporter added (2026-09-30)

`battery-exporter` on 9111, scraped as job `openelectricity_battery`, dashboard
`nembattery-soc` ("NEM battery state of charge"). Added as a third service rather
than a fourth exporter inside an existing one for the same reason the first two
are separate: different upstream, different failure semantics, separate blast
radius. It is also the first exporter here that needs a credential, and the
first that cannot afford to be scraped on the stack's normal cadence.

### The budget is a rate limit, not a credit limit

The assumption going in was "the Community plan gives 500 requests/day, so poll
hourly". It is 500 *credits*, not requests. `GET /v1/plans` is the authoritative
source and was read directly on 2026-09-30 (it rejects a default `urllib`
User-Agent with a 403, the same rejection quirk as the rest of the API — send
`USER_AGENT`). Plan `COMMUNITY` in full:

| field | value |
| --- | --- |
| `daily_credits` | 500 |
| `burst_rate_limit` | **2/s** |
| `bucket_limits` | 5m: 8, 1h: 32, 1d: 366, 7d: 366, 1M: 732, 3M: 1830, season: 1830, 1y: 3700 |

At one request per battery per cycle, scraping the exporter every 5 minutes — this
stack's normal interval — is 288 scrapes/day, and the *obvious* mistake is to let
each scrape be a poll: that is 2880 requests/day at `--top 5`, 8x over the bucket.

So the poll loop is not the scrape handler. The exporter polls on its own
`--poll-interval` (hourly) and serves the last completed cycle to Prometheus,
which means the 5m scrape costs the API nothing:

| | requests/day | vs 366/day |
| --- | --- | --- |
| scrape-driven, 5m, top 5 | 2880 | 8x over |
| own loop, 1h, top 10 | 265 | 101 spare |
| own loop, 1h, **top 12 (shipped)** | **313** | **53 spare** |
| own loop, 1h, top 20 | 504 | over |

The top-12 figure is 12 storage + 1 `/me` per hourly cycle, 24 cycles, plus one
`/facilities/` refresh a day: `13 x 24 + 1 = 313`. The idle rotation does not
change it — demotion keeps the scope full, it just changes which batteries are in
it. The credit balance is not the constraint and was never close: ~478 of 500
remaining at 16:00 local, having spent ~20.

**Two buckets are not satisfied, deliberately:**

- **`2/s` burst.** 13 sequential requests went out in 1.4–4.3 s, about 5 req/s.
  `--request-interval` (default 0.6 s) now paces them to ~1.7 req/s; the measured
  cycle duration went from ~2 s to 7.6 s. This one mattered: a 429 is **not
  retried** — it is raised as a `ScrapeError` on the first attempt, because a
  rate-limited request is treated as a hard answer — so overrunning the burst
  limit costs the entire cycle and blanks the dashboard, rather than costing a
  retry.
- **`8 / 5 min`.** 13 requests in one cycle exceeds it no matter the spacing;
  keeping inside it needs 37.5 s between requests, i.e. 8 minutes of a 60-minute
  cycle. This has been exceeded since the exporter shipped with no 429 in 18h of
  running, so the bucket is evidently enforced leniently or not at all. If the
  dashboard ever shows throttling, `--request-interval 38` fixes it with no other
  change.

**`7d: 366` is the one to watch.** If that is a literal sliding window — 366
requests per *seven days* — then 313/day exhausts it in a bit over a day and
throttles hard, and this whole table is optimistic. The 1M/3M/1y figures do not
scale as clean multiples of a daily rate either (732/1830/3700 against 30/90/365
days), which is why the semantics are not obvious from the response alone. This
is exactly what the multi-day dashboard check is for, and it is the single most
likely way the accounting above turns out to be wrong.

`scrape_interval: 5m` in `prometheus.yml` next to `--poll-interval=3600` in
`docker-compose.yml` looks like a contradiction and is not; the comment in both
files says so. The cost of the design is that **a stopped poll loop is
invisible in the SOC panels**, which go on showing the last reading indefinitely.
That is why `oe_last_poll_timestamp_seconds` and the `Since last poll` stat exist
— it goes orange at 90 min and red at 3 h, and it is the panel to check first
when the SOC looks plausible but stale.

Credits measured anywhere from 0 to 1 per narrow call and are not the constraint;
`oe_api_requests_total` is the number that predicts the bucket, and the
dashboard plots it against a `clamp_max(…, 366)` ceiling line.

### The dashboard is generated, not hand-written

`grafana/provisioning/dashboards/battery-state-of-charge.json` comes from
`tools/build_battery_dashboard.py`, so the sixteen panels' repeated boilerplate
stays consistent and a re-run leaves the rest of the file byte-identical. Re-run
it after any change:

```bash
python3 tools/build_battery_dashboard.py
```

This is a third pattern alongside the two in section 7, and it is worth knowing
which is which: the TBM and Snowy dashboards are hand-edited JSON (either place
is fine, git is the truth), this one is **only** editable through the script.
`allowUiUpdates: true` means a Grafana UI save will look like it worked and then
be overwritten. The generator asserts panel ids are unique, that no two panels
overlap, and that nothing runs off the 24-column grid, because those are the
mistakes worth making impossible in a generated file.

Every PromQL expression in it was validated against the running Prometheus, as
instant queries for the stat and table panels and as range queries over 7d for
the charts, with `$battery` substituted for `.*` — the same check section 7
describes, and for the same reason: `status: success` on an empty result proves
nothing, and the lookback bug in section 7 was exactly that.

Two design decisions in it worth keeping:

- **The battery variable is `label_values(oe_battery_capacity_storage_mwh, name)`,
  not the SOC metric.** The SOC series is *absent* for a battery that could not
  be read, so a variable built from it would make unread batteries unselectable
  — including the three Collie units that are the ones you would most want to
  look at. Capacity is exported for every in-scope unit regardless.
- **All eleven per-battery metrics share one label set**
  `{facility,unit,name,region,status}` — including the three `power` series
  added later, which is why they were given the same treatment rather than a
  reduced set. `oe_battery_scrape_success` originally
  carried a reduced `{facility,unit}`, which meant a `{name=~"$battery"}`
  selector matched nothing on that frame and the table had no `unit` field to
  join on. Changing the exporter was the right fix rather than working around
  it in the dashboard, and `test_every_per_battery_metric_shares_one_label_set`
  now holds it there.

### Verification actually performed

- `python3 -m unittest discover -s scrapers -t scrapers` — 345 tests, all pass,
  no network (88 of them for the new exporter).
- `docker compose up -d battery-exporter` from a clean service, then
  `curl -XPOST localhost:9090/-/reload`: the new target shows `health: unknown`
  with a zero `lastScrape` for ~4m30s before its first scrape lands. That is the
  first-scrape jitter already described in section 6 — the 5m job offset — not a
  networking fault. Do not go looking for one.
- All 26 dashboard queries return data, except the two `rate()`/`increase()`
  expressions, which need two scrapes to have anything to work with.
- Live one-shot (2026-09-30, pre-`power`): 74 enumerated, 10 in scope, 7
  monitored, 9 series without capacity, 12 requests, ~1.4–4.3 s per cycle.
- Live after adding `power` (2026-10-01): 74 enumerated, 12 in scope, 7
  monitored, 37 series without capacity, **14** requests, 7.5 s per cycle — the
  two extra requests being the larger `--top` and the daily fleet refresh, not
  the new metric. `count(oe_battery_power_mw)` is 12 against
  `count(oe_battery_soc_ratio)` 7, which is the two feeds behaving as documented
  rather than a gap.

### Still open

- The three Collie WEM units were in the original top 10 and have published
  **nothing** in a 30-day window, so that scope showed 7 batteries with a
  reading, not 10. That is upstream — and adding `power` does not fix it, since
  they publish no power either. The idle rotation handles it: each Collie slot
  refills from the next-largest candidate after 36h, at no extra request, so the
  scope is full and `oe_batteries_demoted` is 3 in the steady state.
- Recovery of a demoted unit is not automatic — it needs
  `--watchlist-per-cycle`, a restart, or a higher-ranked failure. Off by default
  because it is the only part of the rotation that costs a request.
- Liveness is now persisted to `liveness.json` in the cache volume, so restarts
  no longer reset the idle clock. Liveness only; readings are still never cached.

### Measured 2026-09-30: conditional requests exist, and do not help

Probed the live API for cache validators. `/data/facilities/{network}` (the hot
endpoint, 288 of the 313 requests/day) sends a **weak ETag** and
`cache-control: max-age=300`, and `If-None-Match` does return a real `304` with
an empty body. `/facilities/` sends an ETag and `max-age=900`; `/me` sends
neither.

It is still not worth using, and the reason is the budget's shape: a 304 saves
**bytes, not requests**. The binding limit is 366 requests/day, and a
revalidation is still a request against that bucket. So the validator cannot buy
frequency, and the only way to cut request *count* is to ask about fewer
batteries — which is a coverage decision, not an optimisation.

A second reason to leave it alone: the energy feed only carries values
~18:00–04:00, so a poll at 16:00 gets a 304 and learns nothing, while the same
poll without a validator costs one request and 1.0–4.6 kB. Nothing. (This is one
of the reasons the `power` metric mattered more than it looked: it is the half of
the data that is actually live during those hours.)

Credit accounting could not be settled this way either. `/me`'s balance moves
asynchronously — across a 200/304/200/304 sequence it went 479 → 479 → 480 → 479,
i.e. *up* after a billed request, so single-shot deltas measure nothing. Any
future claim about whether a 304 is billed needs a batched measurement over many
requests, not one probe.
- The `--top 12` scope is a guess, made for a request budget rather than from
  anything the user asked for. It is one flag in `docker-compose.yml`.

## 10. AEMO NEMWEB battery exporter added (2026-10-08)

`aemobattery-exporter` on 9112, scraped as job `aemo_battery` — the same per-DUID
energy storage the dispatch chart is built from, straight from AEMO's
`Next_Day_Dispatch` archive (no key, no request budget). Same decision as
section 9: its own service with its own volume (`aemo-state`) rather than sharing
the OE exporter's, because the two answer different questions (measured with a
trend vs. as-dispatched with one daily timestamp) and must not be able to clobber
each other's caches. Unlike the OE exporter it scales with *all* storage units
AEMO publishes (67 at the audit) rather than a `--top N`.

### Why a second battery feed

Worth being explicit, because both words start with "battery". The OE exporter
is the answer to "what did the batteries actually do" — stored energy from the
`storage_battery` series, with dead-reckoned SOC in between, limited to the
largest facilities that publish. The AEMO exporter is the answer to "what were
the batteries dispatched to hold today" — the whole storage fleet at its newest
reported interval per the daily dispatch file, which is the number the market
actually operated against. They agree on the same units, so a dashboard that
plots both is a consistency check across suppliers rather than a duplicate.

### The load problem is the same shape, and the answer is the same

A full day's `Next_Day_Dispatch` is ~126MB — no 30s scrape can carry it, so the
background poll loop returns, hourly by default, and the ~90KB directory listing
decides whether the big file has actually changed (lexicographic max name =
newest, same-day corrections included). The state file on `aemo-state` caches
the last parsed report; it is only trusted after the live listing re-verifies
the cached file is still the newest, and it is never overwritten by a failed
cycle. A failed poll publishes an empty report — series dropped, never stale —
just like the OE convention.

### What to watch

`aemo_battery_units_reporting` (67 at the audit — a dip is the file being
mid-upload), `aemo_battery_report_reused` (cache answering), and
`aemo_battery_report_generated_age_seconds` (a daily value, so it legitimately
reaches ~24-27h before today's file lands). See `scrapers/NOTES.md` for the
audit, the column layout, and the traps.
