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
4. expose transmission line capacity and load on the AEMO exporter. Done 2026-10-11 for the
   interconnectors: `DISPATCH,INTERCONNECTORRES` in the DispatchIS zip already downloaded
   every 5 minutes yields `MWFLOW`/`METEREDMWFLOW` and `EXPORTLIMIT`/`IMPORTLIMIT` (carried
   verbatim, AEMO's sign convention) as `aemo_interconnector_*`. Intra-regional line loading
   is not published by AEMO at all and remains impossible; binding constraint IDs and the
   locally-constrained flags are still noted as future work. See scrapers/NOTES.md.


5. investigate the negative `aemo_interconnector_age_seconds`. Observed 2026-10-12: the gauge
   is ~-97s for all seven links, while the corresponding
   `aemo_interconnector_timestamp_seconds` is a *positive* ~91s old when re-read later. Cause
   is not clock skew (the exporter container's `date +%s` equals the host's). `age` is computed
   once per export as `now - ts` and cached (aemo_battery_exporter.py ~line 1440), and at export
   time `ts` is ~97s *ahead* of the exporter clock — i.e. DispatchIS `SETTLEMENTDATE` looks like
   the interval's **ending** stamp, so it is briefly in the future when the file is processed.
   Consequences: tooltips/stale checks read "-97s old"; any age<=0 treated as fresh is fine here
   but the sign is misleading. To close this out, confirm SETTLEMENTDATE semantics against the
   file's own publish time across several intervals, then either base `age` on the interval start
   (`SETTLEMENTDATE` minus the 5-minute dispatch interval, inferred from consecutive stamps) or
   clamp at 0 and document that SETTLEMENTDATE is the interval end. Verify the battery
   `*_age_seconds` (different tables) are unaffected — they read positive (~200s), so likely are.
