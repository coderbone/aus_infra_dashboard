# The fleet module owns the shared `fleet.json` contract

Three programs read the OpenElectricity facility cache: the OE exporter writes
it, the AEMO exporter reads each unit's capacity and fueltech from it, and the
web map reads coordinates and status from it. Each parsed the file on its own,
so the rules about it — which fields exist, which identifier a unit is known
by, whether a duplicated unit code is one unit or two — lived in prose in
`scrapers/README.md` and in three separate parsers that had already drifted
apart.

We introduce `scrapers/fleet.py`, a module that owns the file end to end: it
writes it, parses it, de-duplicates unit codes, classifies each unit's
category, and resolves a dispatch unit to a fleet unit. All three programs go
through it. The file's on-disk shape stays the raw, unfiltered OpenElectricity
payload, because that shape is itself a contract with anything else reading the
volume, and re-shaping it would need a migration.

## Considered Options

- **A normalised data file instead of shared code.** The OE exporter would
  write a versioned, deduplicated file and the others would read that.
  Rejected: two formats in flight, a version field and a migration, and it
  reverses the recorded decision that the raw shape is the shared interface.
- **A package directory rather than one file.** Rejected: `scrapers/` has no
  `__init__.py` on purpose, so that the exporter stays a single bind-mounted
  file, and `unittest discover -s scrapers -t scrapers` keeps working. A flat
  module mounted at `/app/fleet.py` is importable by `python /app/<script>.py`
  with no `PYTHONPATH` and no `__init__.py`.

## Consequences

The single-file bind-mount constraint is reopened: `scrapers/fleet.py` is
mounted read-only into both exporters and the web map, so the web map container
now depends on a file in `scrapers/`. The exporters remain separate processes
with separate retry and health semantics, and the shared file is read-only in
every container that does not write it.
