"""Tests for `soc_policy` - the one place the SoC policy is stated.

The policy has three facts, each quoted by consumers that used to restate it:
how long a gap the inference will bridge (the dashboard's panel descriptions),
the fitted charging share (the same descriptions, and the exporter's default),
and the state-of-charge colour ramp (the dashboard's bar gauge and the map's
battery markers). The seam under test is the *single definition* property: the
exporter's defaults are the module's values (identity, not equality - a copy
would pass `==` and drift), docker-compose sets no efficiency of its own, and
the ramp is structurally the shape both consumers need.

The numbers themselves are not re-asserted here: 6.0 and 0.91 are the module's
to state (0.91 is the fit recorded in scrapers/NOTES.md), and a test that
repeated them would be a second definition wearing a test's clothes.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_soc_policy.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aemo_battery_exporter as exporter  # noqa: E402
import soc_policy  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(REPO, "docker-compose.yml")


class SingleDefinitionTest(unittest.TestCase):
    def test_the_exporter_gap_limit_is_the_policy_modules_value(self):
        """The dashboard quotes the bridge-gap limit; the exporter enforces it.

        Identity rather than equality: `MAX_INFER_GAP_HOURS = 6.0` beside the
        module would satisfy `==` today and drift tomorrow, which is the bug
        this module exists to prevent.
        """
        self.assertIs(exporter.MAX_INFER_GAP_HOURS, soc_policy.MAX_GAP_HOURS)

    def test_the_exporter_charge_efficiency_default_is_the_fitted_value(self):
        """The deployed charging share has one home: the policy module.

        docker-compose used to pass `--infer-charge-efficiency=0.91` while the
        exporter's default said 1.0, so the fitted value was stated in compose
        and nowhere a Python consumer could read it. The default is the fitted
        value now, taken from the module by identity.
        """
        self.assertIs(
            exporter.DEFAULT_INFER_CHARGE_EFFICIENCY, soc_policy.CHARGE_EFFICIENCY
        )

    def test_compose_does_not_restate_the_efficiency(self):
        """A compose flag would be a second definition of the fitted value.

        The deployment may still override it - the flag exists - but compose
        must not set one by default, or regenerating the dashboard from the
        module and deploying from compose could disagree.
        """
        with open(COMPOSE, encoding="utf-8") as handle:
            lines = handle.readlines()
        restatements = [
            line for line in lines
            if line.strip().startswith("- --infer-charge-efficiency")
        ]
        self.assertEqual(restatements, [])


class RampTest(unittest.TestCase):
    def test_stops_climb_from_empty_to_full(self):
        """The ramp is what both consumers interpolate across: percent -> colour.

        Ascending, spanning the whole bar (a ramp that stops at 75 would leave
        the top quarter interpolating against nothing), five stops as both
        consumers have always drawn it.
        """
        stops = soc_policy.SOC_RAMP
        values = [value for value, _ in stops]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 100.0)
        self.assertEqual(len(stops), 5)

    def test_every_stop_is_a_rgb_triple(self):
        for value, rgb in soc_policy.SOC_RAMP:
            with self.subTest(value=value):
                self.assertEqual(len(rgb), 3)
                for channel in rgb:
                    self.assertIsInstance(channel, int)
                    self.assertGreaterEqual(channel, 0)
                    self.assertLessEqual(channel, 255)


if __name__ == "__main__":
    unittest.main()
