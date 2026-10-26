"""The pure inferred stored-MWh estimator.

Turns a daily energy anchor plus five-minute measured power into a per-unit
inferred stored-energy figure. Everything here is a pure function of its
arguments: no poller, no thread, no state file. The poller (the AEMO exporter's
intraday thread) owns the impure parts - fetching the daily report, rebuilding
a gap from the SCADA archive, persisting state - and drives this module with
explicit policy.

Policy is an input, never a hidden default: how long a gap the estimator will
bridge (`max_gap_hours`), how it clamps at the top and floors at zero
(`clamp`), and how it discounts charging (`charge_efficiency`).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Policy:
    """The estimator's behaviour, stated explicitly by the caller."""

    # Charging (power < 0) stores only this fraction of the measured energy;
    # discharging (power > 0) deducts at full value.
    charge_efficiency: float
    # The longest gap - between anchor and first step, between steps, and from
    # the newest integrated step to `now` - the estimator will bridge. Beyond
    # it the series is absent: the anchor is never stamped forward.
    max_gap_hours: float
    # False serves the raw integral (still flagged when it left [0, capacity]);
    # True floors at zero and, when a registered capacity is known, clamps at it.
    clamp: bool


def clamp_stored(
    raw: float, capacity: float | None, policy: Policy
) -> tuple[float, float]:
    """Clamp the raw stored MWh into [0, capacity]; returns (mwh, flag).

    Stored energy cannot go below zero, and - when the unit's registered
    storage capacity is known - cannot exceed it either, so a raw value
    outside that envelope is pinned at the boundary. The flag reports the raw
    value leaving the envelope whether or not clamping is applied - the OE
    exporter's `saturated` convention - so a battery held at empty or full is
    distinguishable from one genuinely there. A unit with no known capacity is
    floored at zero only.
    """
    high = float(capacity) if capacity is not None and capacity > 0.0 else None
    flagged = 1.0 if (raw < 0.0 or (high is not None and raw > high)) else 0.0
    if not policy.clamp:
        return raw, flagged
    mwh = max(0.0, raw)
    if high is not None:
        mwh = min(mwh, high)
    return mwh, flagged


def advance(
    entry: dict,
    ts: float,
    power: float,
    capacity: float | None,
    policy: Policy,
) -> dict:
    """Integrate one measured power step into an inferred entry.

    A step that does not move the clock returns the entry unchanged, so
    re-sent or out-of-order rows never double-count. The returned flag is this
    step's clamp flag, not an accumulation: a clean step reads 0 even when the
    anchor itself was pinned.
    """
    delta_hours = (ts - entry["ts"]) / 3600.0
    if delta_hours <= 0:
        return entry
    if power > 0:
        delta = -power * delta_hours
    else:
        delta = -power * delta_hours * policy.charge_efficiency
    mwh, flagged = clamp_stored(entry["mwh"] + delta, capacity, policy)
    return {"ts": ts, "mwh": mwh, "clamped": flagged}


def estimate(
    anchor: tuple[float, float],
    series: list[tuple[float, float]],
    capacity: float | None,
    policy: Policy,
    now: float,
) -> dict | None:
    """The estimator: anchor + power series -> inferred stored MWh, or absent.

    `anchor` is (ts, raw MWh) from the daily report; `series` is (ts, MW)
    measured steps; `capacity` is the unit's registered storage capacity when
    known; `now` is the time the result would be served at. Returns the
    inferred entry {"ts", "mwh", "clamped"}, or None - absent - when a gap
    exceeds `policy.max_gap_hours` or the integrated result is already stale
    at `now`. The anchor is never stamped forward: an unbridgeable gap leaves
    the series absent rather than a stale value wearing a fresh timestamp.
    """
    ts, raw = anchor
    mwh, flagged = clamp_stored(float(raw), capacity, policy)
    entry: dict = {"ts": float(ts), "mwh": mwh, "clamped": flagged}
    for step_ts, power in series:
        step_ts = float(step_ts)
        if (step_ts - entry["ts"]) / 3600.0 > policy.max_gap_hours:
            return None
        entry = advance(entry, step_ts, float(power), capacity, policy)
    limit = now - policy.max_gap_hours * 3600.0
    if entry["ts"] <= limit:
        return None
    return entry
