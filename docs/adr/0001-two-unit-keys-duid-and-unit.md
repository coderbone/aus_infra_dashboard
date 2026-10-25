# Two unit keys: `duid` is AEMO's identifier, `unit` is the fleet unit code

A unit in the NEM has two identifiers, and this stack was publishing both under
one label. `duid` meant AEMO's dispatch unit id on the AEMO exporter's series
and OpenElectricity's unit code on `oe_battery_fleet_info`. The `on(duid)` join
between them worked only because the two strings happened to be equal for 67 of
the 68 dispatching units. They are not always equal: AEMO calls the Torrens
BESS 3 unit `TB3B1`, OpenElectricity calls it `TBBESS3B1`, so that unit
silently lost its region, its fueltech and its registered capacity.

We publish both keys explicitly: `duid` always means AEMO's dispatch unit
identifier, `unit` always means the fleet unit code, and the fleet join moves
to `on(unit)`. The fleet module owns the mapping between the two, so the one
known divergence is asserted as data rather than assumed away, and any future
divergence is published as a visible metric instead of a silent drop.

## Considered Options

- **One key — the fleet unit code, everywhere.** The join key is the same
  under every option, so the only question is what else rides along. Rejected:
  AEMO's dispatch unit id disappears from the metrics, so a series can no
  longer be traced back to the AEMO file it came from. Carrying both names
  costs a label; losing one costs the trail.
- **One key — the AEMO DUID.** Rejected: the fleet covers 122 battery units
  against AEMO's 68 dispatching ones, and the map and the capacity denominator
  both think in fleet units, so the fleet key has to exist anyway.
- **Keep one label meaning two things.** Rejected: it is the state this change
  exists to fix.

## Consequences

`oe_battery_fleet_info`'s `duid` label is renamed to `unit`, and the AEMO
series gain a resolved `unit`. A label rename and a recording-rule change are
not transactional in Prometheus — it reloads rules and scrapes on its own
schedule — so `oe_battery_fleet_info` carries both labels for one release: the
rule moves to `on(unit)` first, and `duid` is dropped from the fleet metric
afterwards.
