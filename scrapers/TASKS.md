1. examine https://caportal.com.au/rms/wht/tbm-tracker and write a prometheus scraper to get the names of the TBM, their current distance, and their total target distance
2. examine Snowy Hydro 2.0 for any live project status data. Audited 2026-09-29: there is no
   progress telemetry of any kind. The only live 2.0-adjacent feed is daily Snowy scheme
   reservoir levels, so the exporter is `snowy_tantangara_exporter.py` (Tantangara only, and
   explicitly *not* construction progress). See scrapers/NOTES.md.
3. a Grafana dashboard for state of charge of the largest N batteries, on a limited API
   credit budget, sampled at least hourly. Done 2026-09-30: `oe_battery_exporter.py` on 9111
   and dashboard `nembattery-soc`. Two findings changed the shape of the answer and are worth
   keeping in mind if it is revisited: OpenElectricity publishes **no** SOC metric at all, so
   the ratio is derived from stored energy over registered capacity; and the binding limit is
   the plan's 366 requests/day rate bucket rather than its 500 credits/day, which is why the
   exporter polls hourly on its own schedule and serves Prometheus from the last completed
   cycle. See scrapers/NOTES.md, "OpenElectricity NEM batteries — audited 2026-09-30", and
   NOTES.md section 9.

