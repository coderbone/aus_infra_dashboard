# monitoring

Docker stack that exposes the Transport for NSW Western Harbour Tunnel TBM
tracker as Prometheus metrics and charts it in Grafana.

- `docker-compose.yml` — the three services (`tbm-exporter`, `prometheus`, `grafana`)
- `prometheus.yml` — scrape config, target `tbm-exporter:9109`
- `grafana/provisioning/` — Grafana datasource provisioning
- `scrapers/` — the exporter itself; see `scrapers/README.md` for metrics and
  `scrapers/NOTES.md` for the upstream ArcGIS item ids
- `NOTES.md` — working notes: layout history, how to move the stack, a log of
  the problems hit here and their fixes, and what is still open

## Running

```bash
cd ~/src/monitoring
docker compose up -d        # exporter -> prometheus -> grafana, gated on healthchecks
docker compose ps
docker compose logs -f tbm-exporter
docker compose down         # volumes survive
```

Use `docker compose` (v2, space). The old python `docker-compose` v1 is broken
against current Docker engines and will remove containers and then error out.

The stack is on the `bone_monitoring` bridge network:

| Service | Reachable at |
| --- | --- |
| `tbm-exporter` | not published — only Prometheus needs it |
| `prometheus` | `http://192.168.1.100:9090` |
| `grafana` | `http://192.168.1.100:3000` |

## Design notes

- The exporter is a stdlib-only Python script, so there is no image to build. It
  runs in `python:3.12-alpine` with the script bind-mounted read-only from
  `./scrapers`, and its layer config cache lives in the `exporter-cache` volume.
- Prometheus scrapes `tbm-exporter:9109` over the compose network, so the
  exporter is started with `--listen-address 0.0.0.0`. A `127.0.0.1` bind only
  works while Prometheus shares the host network namespace.
- 9109 is deliberately **not** published.
- Grafana's Prometheus datasource is provisioned from
  `grafana/provisioning/`, so no manual datasource setup is needed.
- `prometheus-data` and `grafana-storage` are declared external, reusing the
  volumes `docker volume create` made for the pre-compose setup.
- `name: bone` at the top of the compose file is pinned on purpose. Without it
  the project name follows the directory name, so the move out of `~` would
  rename the network and the project-prefixed `bone_exporter-cache` volume. See
  `NOTES.md` section 2.

## Configuration

Set `GRAFANA_ADMIN_PASSWORD` in the environment (or a `.env` next to the compose
file) before exposing 3000 beyond the LAN — it currently defaults to `admin`.

## Verify

```bash
docker compose ps                                   # all three (healthy)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
curl -s --get --data-urlencode 'query=up{job="wht_tbm"}' localhost:9090/api/v1/query
docker compose logs --tail 20 tbm-exporter          # upstream errors land here
```

Alert on `wht_tbm_scrape_success == 0` and on
`time() - wht_tbm_last_report_timestamp_seconds`. Full metric list in
`scrapers/README.md`.
