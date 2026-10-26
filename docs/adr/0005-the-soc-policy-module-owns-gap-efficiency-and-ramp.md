# The SoC policy module owns the gap limit, efficiency and ramp

Three facts about state of charge were stated beside the things they governed
rather than in one place. The AEMO exporter held `MAX_INFER_GAP_HOURS`, but the
fitted charging share existed only as `--infer-charge-efficiency=0.91` in
docker-compose while the exporter's own default said 1.0 — so the number the
deployment ran and the number a Python consumer could read were different
facts. The dashboard generator spelled "6h" and "fitted ~0.91" into its panel
descriptions by hand, so regenerating after a change left prose describing the
old model. And the colour ramp existed twice: Grafana threshold steps in the
bar gauge, RGB stops in the map's battery markers — similar enough to read as
one ramp and free to diverge.

We introduce `scrapers/soc_policy.py`: `MAX_GAP_HOURS`, `CHARGE_EFFICIENCY`
(the fitted value, with the fit's provenance) and `SOC_RAMP` (percent → RGB).
The exporter's `MAX_INFER_GAP_HOURS` and `DEFAULT_INFER_CHARGE_EFFICIENCY` take
the first two by identity, so `inference.Policy` construction and the CLI
surface are unchanged while the values have one home; the dashboard generator
interpolates them into its descriptions and renders the ramp as hex threshold
steps; the web map's server converts the ramp to the front end's 0..1 stops and
sends it with `/data/live`, because a browser cannot import a Python module.
docker-compose no longer passes `--infer-charge-efficiency` — the flag remains
for a deployment that fits its own value, it is just no longer the only place
the number exists. Published values are unchanged: the deployed share was
already 0.91.

The same change removes `oe_battery_capacity_storage_mwh` from the map's live
queries. The SoC denominator was already the fleet module's — `capacity_mwh`
on the payload's fleet entries, per docs/adr/0002 — and the Prometheus series
was a second reading of the same `fleet.json` reaching the same payload,
divided by nothing; it is gone so a future change cannot pick it up by
mistake.

## Considered Options

- **Read docker-compose.yml for the efficiency.** Rejected: compose would stay
  the definition while the exporter's default said 1.0, so the two would have
  to be kept in step by discipline, and the dashboard generator would grow a
  regex dependency on compose's layout.
- **Policy values as defaults in `inference.py`.** Rejected by docs/adr/0004:
  the estimator takes a `Policy` every call and holds no defaults. This module
  is configuration the *consumers* read; the exporter still builds the
  `Policy` explicitly from it, so the pure module's rule stands.
- **The dashboard imports the exporter for the two numbers.** Rejected: the
  exporter is an application, not a shared module — the flat-module convention
  docs/adr/0002 and 0003 blessed is for facts more than one program needs — and
  the ramp has no business living in an exporter.
- **Duplicate the ramp in both consumers and test that the copies match.**
  Rejected: two definitions with a drift alarm are still two definitions; the
  test would compare copies rather than pin one fact.

## Consequences

`scrapers/soc_policy.py` is bind-mounted read-only into the AEMO exporter and
the web map server, the same arrangement as `fleet.py`, `links.py` and
`inference.py` (the OE exporter reads neither this module nor the ramp: it
neither infers nor draws). The ramp reaches the browser only through
`/data/live`; `STOPS_SOC` in `app.js` is populated from each payload and is no
longer a literal. Unifying the ramp does change one pixel of the dashboard: the
bar gauge used to hold flat green from 75 to 100 while the map interpolated to
its own green, and it now interpolates that last quarter like the map always
did — the point of one definition. The four exporter tests whose worked
arithmetic assumed the
old 1.0 default now pin `charge_efficiency=1.0` explicitly — they test the
clamp and the archive reconstruction, not the fit, and the fit is pinned at the
`test_soc_policy` seam instead (exporter defaults are the module's values by
identity; compose restates nothing). The dashboard generator has no test seam
of its own yet — that is issue #8 — so its guarantee here is the regeneration
diff: change a value in the module, re-run `tools/build_battery_dashboard.py`,
and the panel prose changes with it.
