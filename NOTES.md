# NOTES — monitoring stack

Working notes for whoever picks this up next (including future me).

## 1. Layout as of 2026-09-27

```
~/src/monitoring/                                    # this project
~/src/monitoring/docker-compose.yml                  # 3 services, project name "bone"
~/src/monitoring/prometheus.yml                      # scrape config, target: tbm-exporter:9109
~/src/monitoring/grafana/provisioning/datasources/prometheus.yml
~/src/monitoring/scrapers/wht_tbm_exporter.py        # the scraper (stdlib only, no image build)
~/src/monitoring/scrapers/README.md                  # metrics + run instructions
~/src/monitoring/scrapers/NOTES.md                   # upstream ArcGIS item ids
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
| containers | `wht-tbm-exporter`, `prometheus`, `grafana` |
| compose project | `bone` (pinned via `name:` in the compose file) |
| network | `bone_monitoring` (bridge) |
| volumes | `prometheus-data`, `grafana-storage` (external, pre-existing), `exporter-cache` |

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
docker compose ps                                   # all three (healthy)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
curl -s --get --data-urlencode 'query=up{job="wht_tbm"}' localhost:9090/api/v1/query
docker compose logs --tail 20 tbm-exporter          # upstream errors land here
```

First `/metrics` request after an exporter restart is cold: it does full
discovery and takes ~5-9 s. `scrape_timeout` is 30 s, so that is fine, but
don't panic if the first scrape is slow.

## 5. Not done / open

- `GRAFANA_ADMIN_PASSWORD` still unset.
- No recording or alerting rules; `prometheus.yml` has scrape configs only.
- The `Western Harbour TBM` dashboard is provisioned from
  `grafana/provisioning/dashboards/` (see section 7), but there is no
  provisioning of Grafana *users* or folders.
- The exporter has a `web.enable-lifecycle`-style reload nowhere; changing
  `prometheus.yml` needs `docker compose restart prometheus`.
- Host is not 24/7-managed: nothing restarts Docker or the stack on reboot
  beyond `restart: unless-stopped`. No systemd unit for the stack.

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

`allowUiUpdates: true` means the file wins: an edit made in the Grafana UI is
overwritten by the file within `updateIntervalSeconds` (30). Edit the JSON in
git instead, or turn the flag off if you would rather work in the UI.

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

The `Western Harbour TBM` dashboard has two timeseries panels: *Western Harbour
TBMs* in metres (`wht_tbm_distance_excavated_m`, `wht_tbm_remaining_distance_m`,
`wht_tbm_target_distance_m`) and *TBM progress* as a percentage
(`wht_tbm_progress_ratio`). They are separate panels because the metres series
and the 0–1 ratio do not share an axis sensibly.
