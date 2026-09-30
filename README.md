# monitoring

Docker stack that exposes two public project feeds as Prometheus metrics and
charts them in Grafana:

- the Transport for NSW **Western Harbour Tunnel TBM tracker** (tunnel boring
  machine progress), and
- **Snowy Hydro reservoir levels**, focused on Tantangara Reservoir — the upper
  storage of the Snowy Hydro 2.0 pumped scheme.

> The Snowy series are **water levels, not construction progress.** Snowy Hydro
> 2.0 publishes no machine-readable project status of any kind; see
> `scrapers/NOTES.md` for the audit. Tantangara drawdown is the closest live
> signal that exists, because the 2.0 intake works are at the upper storage.

- `docker-compose.yml` — the four services (`tbm-exporter`,
  `reservoir-exporter`, `prometheus`, `grafana`)
- `prometheus.yml` — scrape configs, targets `tbm-exporter:9109` and
  `reservoir-exporter:9110`
- `grafana/provisioning/` — Grafana datasource and dashboard provisioning.
  Two dashboards, both loaded from files at boot: `Western Harbour TBM`
  (`adr468z`) and `Snowy Hydro reservoir levels` (`snowy-tantangara`). See
  `NOTES.md` §7 before changing the datasource uid or either dashboard uid.
- `scrapers/` — the exporters themselves; see `scrapers/README.md` for metrics
  and `scrapers/NOTES.md` for the upstream endpoints
- `NOTES.md` — working notes: layout history, how to move the stack, a log of
  the problems hit here and their fixes, and what is still open

## Running

```bash
cd ~/src/monitoring
docker compose up -d        # exporters -> prometheus -> grafana, gated on healthchecks
docker compose ps
docker compose logs -f tbm-exporter
docker compose down         # volumes survive
```

Use `docker compose` (v2, space). The old python `docker-compose` v1 is broken
against current Docker engines and will remove containers and then error out.

The stack is on the `aus_infra_dashboard_monitoring` bridge network:

| Service | Reachable at |
| --- | --- |
| `tbm-exporter` | not published — only Prometheus needs it |
| `reservoir-exporter` | not published — only Prometheus needs it |
| `prometheus` | `http://192.168.1.100:9090` |
| `grafana` | `http://192.168.1.100:3000` |

## Design notes

- The exporters are stdlib-only Python scripts, so there is no image to build.
  They run in `python:3.12-alpine` with each script bind-mounted read-only from
  `./scrapers`. The WHT exporter's layer config cache lives in the
  `exporter-cache` volume; the Snowy exporter mounts **no** volume, because it
  has no discovered configuration to cache and its level gauges must never be
  served from a stored copy.
- They are two separate services rather than one container running both: they
  have nothing to share — different upstreams, different retry and health
  semantics, and separate failure isolation.
- Prometheus scrapes them over the compose network, so each is started with
  `--listen-address 0.0.0.0`. A `127.0.0.1` bind only works while Prometheus
  shares the host network namespace.
- 9109 and 9110 are deliberately **not** published.
- Scrape intervals differ because the upstreams differ: 5m for the TBM tracker,
  which publishes survey lines every few hours, and 1h for the reservoir levels,
  which are published once a day around 07:00 Sydney time.
- Both containers override `dns:` to public resolvers. The measured failure mode
  is Docker's embedded resolver intermittently returning `EAI_AGAIN` after ~5s;
  see `NOTES.md` §3.
- Grafana's Prometheus datasource is provisioned from
  `grafana/provisioning/`, so no manual datasource setup is needed.
- Each dashboard gets its own refresh interval, matched to its upstream: 5m for
  the TBM tracker, 1h for the reservoir levels, which publish once a day. Grafana
  panels here are deliberately not generic: date-versus-duration units, percent
  versus `percentunit`, and step-versus-linear interpolation each have a specific
  reason, all written up in `NOTES.md` §7.
- `prometheus-data` and `grafana-storage` are declared external, reusing the
  volumes `docker volume create` made for the pre-compose setup.
- `name: aus_infra_dashboard` at the top of the compose file is pinned on
  purpose, so the project name is the repo name rather than following the
  directory (`monitoring`). It also keeps the network and the project-prefixed
  `exporter-cache` volume stable across moves. See `NOTES.md` section 2.

## Configuration

Secrets live in a `.env` next to the compose file, which is gitignored.
`.env.example` is the tracked template — it lists every variable the stack
wants and never holds a real value. Clone and run:

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Compose reads `.env` from the compose file's directory automatically, so no
`--env-file` flag and no `export` is needed. The `chmod 600` is worth doing:
the file is created by `cp` at whatever your umask allows, which on this host
was `664` — group- and world-readable.

| Variable | Used by | Notes |
| --- | --- | --- |
| `GRAFANA_ADMIN_PASSWORD` | `grafana` service | Defaults to `admin` if unset, and 3000 is published to the LAN. Set it. |
| `OPENEA_API_KEY` | nothing yet | OpenElectricity bearer token. The NEM exporter that needs it has not been written — see below. |

Do not commit `.env`, and do not paste its contents into an issue or a commit
message. Note also that **`docker compose config` prints interpolated secrets
in the clear** — verified here: with `GRAFANA_ADMIN_PASSWORD` set, that command
emits `GF_SECURITY_ADMIN_PASSWORD: <value>` in its rendered output. Anything
consumed by a service's `environment:` is fair game for it. `OPENEA_API_KEY` is
safe from this *only* because no service references it yet; the day one does,
it becomes exposed here too, along with anywhere the rendered config is logged.

### The OpenElectricity key

The NEM data comes from **OpenElectricity**, which was renamed from OpenNEM.
`opennem.com.au` and `api.opennem.com.au` are dead; the API is
`api.openelectricity.org.au`. Register at
`platform.openelectricity.org.au`, which issues a key shaped `oe_…`.

There is **no anonymous access** — `/data/network/NEM`, `/facilities` and
`/market/*` all return 401 without a bearer token, and only `/v1/plans` and
`/v4/social` are open. The free **Community** plan is 500 requests/day, 2/s
burst, 2 years of history, 1 key, non-commercial use. **Academic** (full
history, 2000/day, 5 keys) is also free but wants an accredited institution's
email domain.

Because a personal key is tied to one person's quota and account, a stack meant
to be cloned should read its own key from the cloner. That is what
`.env.example` is for. If you share this stack, do not ship your `.env`.

## Verify

```bash
docker compose ps                                   # all four (healthy)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
curl -s --get --data-urlencode 'query=up{job="wht_tbm"}' localhost:9090/api/v1/query
# snowy_tantangara is scraped hourly but an instant query only looks back 5
# minutes, so a bare `up{job="snowy_tantangara"}` is empty for most of the hour.
# Wrap it, or read the lookback-independent /api/v1/targets above.
curl -s --get --data-urlencode 'query=last_over_time(up{job="snowy_tantangara"}[2h])' \
  localhost:9090/api/v1/query
curl -s -u admin:admin 'localhost:3000/api/search?type=dash-db'   # both dashboards
docker compose logs --tail 20 tbm-exporter          # upstream errors land here
docker compose logs --tail 20 reservoir-exporter
```

Alert on `wht_tbm_scrape_success == 0`, on
`time() - wht_tbm_last_report_timestamp_seconds`, on
`snowy_tantangara_scrape_success == 0`, and on
`time() - snowy_tantangara_last_sample_timestamp_seconds` (the feed publishes
daily, so a threshold of ~36h catches a stopped feed without firing overnight).
Full metric lists in `scrapers/README.md`.
