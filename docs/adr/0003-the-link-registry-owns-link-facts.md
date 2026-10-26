# The link registry owns link facts

A link's facts — its id, display name, the two regions it joins, and its
nominal rating in each direction — were held in three places: two tables in the
AEMO exporter (`INTERCONNECTOR_NOMINAL_MW`, `INTERCONNECTOR_REGIONS`), the
hand-authored `lines.geojson`, and hand-copied copies in the web map's test
fixtures. Adding or renaming a link meant editing all of them, and they had
already drifted: the exporter's nominal ratings were not what the map divided
by, and the geojson's coordinate order — which encodes the same FROM→TO
direction the region pairs assume — had shipped a reversed arrow.

We introduce `scrapers/links.py`, one module that states each link's facts once
(id, name, from/to regions, nominal rating in that direction and the other).
The exporter's nominal series and region interchange derive from it; the map's
displayed names are attached to the geometry when the server serves it, by id;
the geometry itself stays hand-drawn in `lines.geojson` as `kind`/`id` only.

## Considered Options

- **A JSON/YAML data file read by both sides.** Rejected: both readers are
  Python, so a shared module needs no parser, no schema and no load-order
  handling, and the provenance comments (AEMO's Interconnector Capabilities
  report, NER 3.13.3(p)) travel with the numbers as code comments rather than
  prose beside a file.
- **A generator that writes the names into `lines.geojson`.** Rejected: the
  file then still carries a copy of the name, and a regenerable artifact is
  only correct the moment someone remembers to regenerate it.
- **Names through `/data/live`.** Rejected: the front end would grow a second
  lookup path for a fact the geometry payload could carry, making
  `app.js` know about the registry at all.
- **The registry as a whitelist gating which links get series.** Rejected: AEMO
  can report a link the registry does not know. Absent facts stay absent —
  flow and limits still publish, nominal and interchange simply do not — which
  the tests already pin.

## Consequences

`scrapers/links.py` is bind-mounted read-only into the AEMO exporter and the
web map, the same arrangement `docs/adr/0002` blessed for `fleet.py`, so the
webmap container now depends on a second file in `scrapers/`. The raw
`lines.geojson` is no longer self-describing: opening it shows geometry and ids
with no names. The FROM→TO coordinate order remains hand-maintained, but a
test now pins it against the registry's regions, which is the seam that
previously shipped a bug. Adding a link is one registry edit; drawing its
geometry still is not.
