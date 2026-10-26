# NEM Monitoring

Registration and dispatch facts for the National Electricity Market, scraped
from AEMO and OpenElectricity, published as Prometheus metrics, and drawn as a
Leaflet map.

## The fleet

**Fleet**:
The registered generation and storage facilities of the NEM, as published by
OpenElectricity's `/facilities/` endpoint. Registration facts only — capacity,
region, fueltech, status, location — never measurements.
_Avoid_: the OpenNEM fleet (the upstream was renamed; the domain term is unchanged)

**Fleet metadata**:
The cached OpenElectricity facility list, held in `fleet.json`. Unfiltered by
fueltech, so it carries every unit's fueltech and region, not just batteries.
_Avoid_: fleet cache (that is the file; this is the content)

**Dispatch unit**:
AEMO's term for a single generating or storage unit in the NEM, identified by a
DUID. The unit of dispatch, and the key every AEMO series in this stack joins on.
_Avoid_: unit — say *dispatch unit* for AEMO's and *OE unit* for OpenElectricity's

**DUID**:
The dispatch unit identifier: `aemo_battery_power_mw{duid=…}`.

**Unit code**:
OpenElectricity's identifier for a unit. Usually the same string as the AEMO
DUID, but not always — the Tailem Bend 3 unit is `TBBESS3B1` in
OpenElectricity and `TB3B1` in AEMO.
_Avoid_: DUID (a different identifier, even when the strings match)

**Alias**:
A dispatch unit whose AEMO identifier differs from its unit code, mapped to
that code by the fleet module. Asserted, not derived: neither upstream payload
carries a bridge between the two, so the link can only be stated.

**Fueltech**:
OpenElectricity's generation-technology classification for a unit
(`battery`, `solar_utility`, `wind`, `coal_black`, `gas_ccgt`, `hydro`,
`distillate`, …).

**Category**:
The fleet taxonomy a unit falls into, derived from its fueltech: battery,
solar, wind, hydro, pumped hydro, coal, gas, distillate.

**Fossil**:
A unit whose fueltech is coal, gas or distillate — the set that rolls up into a
region's fossil generation. Wider than the map's coal and gas buckets, which
exclude distillate because the map draws it on its own.

## Interconnectors

**Link**:
An interconnector or MNSP connection joining two NEM regions, identified by its
AEMO interconnector id (`VIC1-NSW1`, `T-V-MNSP1`).
_Avoid_: line (that is the drawn geometry, and it also covers corridors that
are not links)

**Link registry**:
The single definition of a link's facts: its id, display name, the two regions
it joins, and its nominal rating in each direction. The exporter's
interconnector series and the map's displayed link names both derive from it.
_Avoid_: link table, interconnector table

**Nominal rating**:
A link's transfer capability in one direction under system-normal conditions,
in MW. Published separately for each direction, because a link can be
asymmetric — VIC1-NSW1 is 1700 MW one way and 1450 the other.
_Avoid_: capacity (that word is taken by registered capacity and registered
storage capacity, which are per unit, not per link and not per direction)

**Utilisation**:
A link's flow as a fraction of the nominal rating for the direction it is
travelling. Never taken against the dynamic dispatch limits.
_Avoid_: load factor

## Capacity

**Registered storage capacity**:
A storage unit's registered energy capacity, in MWh. The denominator for a
unit's state of charge.
_Avoid_: registered capacity

**Registered capacity**:
A unit's registered power capacity, in MW. Not usable as a state-of-charge
denominator — that is a units error that still produces a plausible number.
_Avoid_: registered storage capacity

## Storage

**Inferred stored MWh**:
The estimated energy stored in a storage unit now: the daily report's anchor
advanced by the integrated SCADA output each five-minute interval. An estimate,
never a measurement, and always labelled as such.
_Avoid_: state of charge (a ratio, not an energy)

**Anchor**:
The daily report's per-unit starting energy reading, which the inference
advances from. Never stamped forward — a gap is rebuilt from the archive, or
the series stays absent until the next daily report.

**SoC policy**:
The single definition of how state of charge is modelled and shown: the
bridge-gap limit, the fitted charging share, and the colour ramp the
dashboard's bar gauge and the map's battery markers both interpolate across.
Consumers read it (`scrapers/soc_policy.py`); none restates it beside them.
_Avoid_: policy (that is `inference.Policy`, the explicit input to one
estimator call)

## Exposition

**Exposition formatter**:
The single definition of how a value is written into the Prometheus text
format - label-value escaping and sample-value formatting at four decimal
places (`scrapers/exposition.py`). All four exporters write through it;
none carries a private copy.
_Avoid_: fmt (that is one function; this is the contract)
