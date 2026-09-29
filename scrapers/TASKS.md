1. examine https://caportal.com.au/rms/wht/tbm-tracker and write a prometheus scraper to get the names of the TBM, their current distance, and their total target distance
2. examine Snowy Hydro 2.0 for any live project status data. Audited 2026-09-29: there is no
   progress telemetry of any kind. The only live 2.0-adjacent feed is daily Snowy scheme
   reservoir levels, so the exporter is `snowy_tantangara_exporter.py` (Tantangara only, and
   explicitly *not* construction progress). See scrapers/NOTES.md.

