"""Tests for `inference` - the pure inferred stored-MWh estimator.

The estimator turns a daily energy anchor plus five-minute measured power into
a per-unit inferred stored-energy figure. It is a pure function: no poller, no
thread, no state file. Its policy - how long a gap it will bridge, how it
clamps at the top and floors at zero - is an explicit input (a `Policy`), never
a hidden default.

The seam under test is `estimate` (the whole estimator) and `clamp_stored` (the
clamp primitive the poller also needs when it re-anchors or restores state).
The poller's wiring - archive backfill, state persistence, the age gate at
export time - stays covered by the exporter's own InferenceTest suites.
"""

from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inference  # noqa: E402


def policy(**over):
    base = {"charge_efficiency": 1.0, "max_gap_hours": 6.0, "clamp": True}
    base.update(over)
    return inference.Policy(**base)


class ClampStoredTest(unittest.TestCase):
    def test_a_negative_raw_value_is_floored_and_flagged(self):
        mwh, flagged = inference.clamp_stored(-270.0, None, policy())
        self.assertEqual(mwh, 0.0)
        self.assertEqual(flagged, 1.0)

    def test_a_value_above_a_known_capacity_is_clamped_and_flagged(self):
        mwh, flagged = inference.clamp_stored(230.0, 200.0, policy())
        self.assertEqual(mwh, 200.0)
        self.assertEqual(flagged, 1.0)

    def test_without_a_capacity_the_top_clamp_cannot_apply(self):
        mwh, flagged = inference.clamp_stored(230.0, None, policy())
        self.assertEqual(mwh, 230.0)
        self.assertEqual(flagged, 0.0)

    def test_clamp_off_serves_the_raw_value_but_keeps_the_flag(self):
        mwh, flagged = inference.clamp_stored(
            -270.0, None, policy(clamp=False)
        )
        self.assertEqual(mwh, -270.0)
        self.assertEqual(flagged, 1.0)


# An anchor at 04:00 AEST; the worked examples integrate half an hour.
T0 = 1791396000.0
T30 = T0 + 1800.0


class EstimateTest(unittest.TestCase):
    def test_discharge_deducts_full_and_charging_applies_efficiency(self):
        policy_ = policy(charge_efficiency=0.9)
        # 130 MWh discharging 10 MW for 0.5h -> 130 - 5 = 125.
        out = inference.estimate((T0, 130.0), [(T30, 10.0)], None, policy_, T30)
        self.assertAlmostEqual(out["mwh"], 125.0, places=4)
        # 50 MWh charging -10 MW for 0.5h -> 50 + 10*0.5*0.9 = 54.5.
        out = inference.estimate((T0, 50.0), [(T30, -10.0)], None, policy_, T30)
        self.assertAlmostEqual(out["mwh"], 54.5, places=4)
        self.assertEqual(out["ts"], T30)
        self.assertEqual(out["clamped"], 0.0)

    def test_a_gap_the_policy_will_not_bridge_leaves_the_series_absent(self):
        # The anchor is nine hours old and the policy bridges six: integrating
        # in one step would stamp a nine-hour-old reading as current.
        gap_ts = T0 + 9 * 3600.0
        self.assertIsNone(
            inference.estimate((T0, 130.0), [(gap_ts, -450.0)], None, policy(), gap_ts)
        )

    def test_a_gap_inside_a_longer_series_also_leaves_the_series_absent(self):
        out = inference.estimate(
            (T0, 130.0),
            [(T30, 10.0), (T30 + 7 * 3600.0, 0.0)],
            None,
            policy(),
            T30 + 7 * 3600.0,
        )
        self.assertIsNone(out)

    def test_a_result_stale_at_now_is_absent(self):
        # Integrated cleanly to T30, but served 7h later: beyond the policy.
        out = inference.estimate(
            (T0, 130.0), [(T30, 10.0)], None, policy(), T30 + 7 * 3600.0
        )
        self.assertIsNone(out)

    def test_a_result_exactly_at_the_age_limit_is_absent(self):
        # Serve time == limit: the poller serves only entries strictly newer.
        limit_ts = T30 + 6.0 * 3600.0
        out = inference.estimate((T0, 130.0), [(T30, 10.0)], None, policy(), limit_ts)
        self.assertIsNone(out)

    def test_a_result_within_the_age_limit_is_served(self):
        out = inference.estimate(
            (T0, 130.0), [(T30, 10.0)], None, policy(), T30 + 6.0 * 3600.0 - 1.0
        )
        self.assertAlmostEqual(out["mwh"], 125.0, places=4)

    def test_a_negative_anchor_is_floored_and_flagged(self):
        # 130 MWh anchor discharged 400 MW for 1h drives the raw integral to
        # -270; the floor pins it at zero and the flag reports the departure.
        out = inference.estimate(
            (T0, 130.0), [(T0 + 3600.0, 400.0)], None, policy(), T0 + 3600.0
        )
        self.assertEqual(out["mwh"], 0.0)
        self.assertEqual(out["clamped"], 1.0)

    def test_the_flag_is_per_step_not_accumulated(self):
        # A clean step after a pinned one reads 0: the flag reports the latest
        # clamp, so a battery that has recovered is no longer marked.
        out = inference.estimate(
            (T0, 130.0),
            [(T0 + 3600.0, 400.0), (T0 + 5400.0, 0.0)],
            None,
            policy(),
            T0 + 5400.0,
        )
        self.assertEqual(out["mwh"], 0.0)
        self.assertEqual(out["clamped"], 0.0)

    def test_charging_past_a_known_capacity_is_clamped_and_flagged(self):
        # 130 MWh charged 100 MW for 1h at efficiency 1.0 -> 230, ten percent
        # over the 200 MWh registered capacity: pinned at the capacity.
        out = inference.estimate(
            (T0, 130.0), [(T0 + 3600.0, -100.0)], 200.0, policy(), T0 + 3600.0
        )
        self.assertEqual(out["mwh"], 200.0)
        self.assertEqual(out["clamped"], 1.0)

    def test_without_a_capacity_the_top_clamp_cannot_hold(self):
        out = inference.estimate(
            (T0, 130.0), [(T0 + 3600.0, -100.0)], None, policy(), T0 + 3600.0
        )
        self.assertEqual(out["mwh"], 230.0)
        self.assertEqual(out["clamped"], 0.0)

    def test_steps_that_do_not_move_the_clock_never_double_count(self):
        # Re-sent or out-of-order rows: same ts, then an older one, then the
        # real next step. Only the advancing step integrates.
        out = inference.estimate(
            (T0, 130.0),
            [(T0, 10.0), (T0 - 300.0, 50.0), (T30, 10.0)],
            None,
            policy(),
            T30,
        )
        self.assertAlmostEqual(out["mwh"], 125.0, places=4)
        self.assertEqual(out["ts"], T30)


if __name__ == "__main__":
    unittest.main()
