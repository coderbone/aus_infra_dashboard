#!/usr/bin/env python3
"""The SoC policy: the one place the inference's gap limit, the fitted
charging share and the state-of-charge colour ramp are stated.

Three facts, each quoted beside the thing it governs, and each had drifted or
was about to: the dashboard's panel descriptions spelled out "6h" and "fitted
~0.91" by hand, so regenerating the board after changing either one left the
prose describing the old model; and the colour ramp existed twice - once as
Grafana threshold steps in the dashboard generator, once as RGB stops in the
map - similar enough to look like one ramp and free to diverge. They live here
now; see docs/adr/0005.

What the module owns:

  * `MAX_GAP_HOURS` - the longest gap the inference will bridge (the
    estimator's `Policy.max_gap_hours`, and the "6h bridge limit" the panel
    descriptions quote);
  * `CHARGE_EFFICIENCY` - the share of charging energy counted as stored,
    fitted against AEMO's reported regional storage (the fit is stated with
    the value below; the calibration write-up is scrapers/NOTES.md);
  * `SOC_RAMP` - percent -> RGB stops the state-of-charge bar gauge and the
    map's battery markers both interpolate across.

What it deliberately does *not* own: how the estimator applies any of this.
`scrapers/inference.py` still takes a `Policy` on every call and holds no
defaults (docs/adr/0004); the exporter builds that Policy from these values,
and this module never reaches back into either. It also does not own the
denominator the ramp's percentages are taken against - registered storage
capacity is per-unit fleet data, and comes from the fleet module
(docs/adr/0002), not from here.

A consumer that does not know this module still works: the exporter's CLI
flags can override the efficiency for a deployment that fits its own, and a
map with no ramp payload colours batteries by status instead.
"""

from __future__ import annotations

# The longest gap - between anchor and first step, between steps, and from the
# newest integrated step to `now` - the inference will bridge. Beyond it the
# series is absent rather than a stale value wearing a fresh timestamp. This is
# the "6h bridge limit" the dashboard descriptions quote; the exporter's
# MAX_INFER_GAP_HOURS is this value, taken by identity.
MAX_GAP_HOURS = 6.0

# Share of charging energy that reaches the cells (discharging is deducted at
# full value, since it is metered at the terminals). Fitted 2026-10-19 over the
# eight complete days since the feed went live: at 18:00 AEST,
# sum(aemo_battery_inferred_stored_mwh) ran 1-21% (median ~11%) above
# sum(aemo_battery_region_stored_mwh), and the gap scaled with the day's
# charging energy. Solving gap = (1 - e) x charged per day gives 0.83-0.99,
# median 0.90, pooled 0.908 - so 0.91, within a rounding of the OE exporter's
# deployed 0.9 (same fleet, same conversion losses). At 1.0 the phantom charge
# also pushed up to 12 units/day onto the capacity ceiling and made the next
# morning's re-anchor a ~1.5 GWh drop. The exporter's default is this value;
# docker-compose.yml used to pass it as a flag, restating it, and no longer
# does.
CHARGE_EFFICIENCY = 0.91

# Percent -> (red, green, blue), empty to full. One definition consumed twice:
# the dashboard's bar gauge renders the RGB as hex threshold steps, and the web
# map's server converts the stops to the front end's {v: 0..1, c: [r,g,b]}
# shape when it builds the live payload. Red at empty, orange across the first
# quarter, yellow at half, green from three-quarters full - both consumers have
# always drawn that ramp; this is the first time they draw it from one list.
SOC_RAMP: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (217, 48, 70)),
    (25.0, (229, 119, 60)),
    (50.0, (224, 179, 67)),
    (75.0, (156, 200, 79)),
    (100.0, (94, 193, 107)),
)
