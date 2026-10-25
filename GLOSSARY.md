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
DUID, but not always — the Torrens BESS 3 unit is `TBBESS3B1` in
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
