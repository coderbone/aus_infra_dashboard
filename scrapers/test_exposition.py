"""Tests for `exposition` - the one Prometheus exposition formatter.

The seam under test is the *single definition* property: each exporter's
`fmt` and `escape_label` are this module's functions by identity - a copy
would pass `==` and drift, which is exactly the bug (the OE exporter rounded
to six decimals while the other three rounded to four, in same-named
functions). The four-decimal precision is pinned here, at the module seam,
as the deliberate choice recorded in docs/adr/0007.

From the repo root:

    python3 -m unittest discover -s scrapers -t scrapers -v
    ./scrapers/test_exposition.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aemo_battery_exporter as aemo  # noqa: E402
import exposition  # noqa: E402
import oe_battery_exporter as oe  # noqa: E402
import snowy_tantangara_exporter as snowy  # noqa: E402
import wht_tbm_exporter as wht  # noqa: E402


class SingleDefinitionTest(unittest.TestCase):
    def test_every_exporter_writes_through_the_shared_helpers(self):
        """Identity, not equality: a re-copied helper would satisfy `==`."""
        for exporter in (aemo, snowy, wht, oe):
            with self.subTest(exporter=exporter.__name__):
                self.assertIs(exporter.fmt, exposition.fmt)
                self.assertIs(exporter.escape_label, exposition.escape_label)


class FmtTest(unittest.TestCase):
    def test_none_is_nan(self):
        self.assertEqual(exposition.fmt(None), "NaN")

    def test_integers_are_not_dressed_up(self):
        self.assertEqual(exposition.fmt(1997.0), "1997")
        self.assertEqual(exposition.fmt(1), "1")

    def test_specials(self):
        self.assertEqual(exposition.fmt(float("nan")), "NaN")
        self.assertEqual(exposition.fmt(float("inf")), "+Inf")
        self.assertEqual(exposition.fmt(float("-inf")), "-Inf")

    def test_fractional_values_round_to_four_decimals(self):
        """The deliberate precision (docs/adr/0007), pinned at the seam.

        These two cases are the ones the WHT and Snowy suites used to pin
        against their own private copies; they now pin the shared one.
        """
        self.assertEqual(exposition.fmt(0.15955555), "0.1596")
        self.assertEqual(exposition.fmt(11.333333), "11.3333")

    def test_shorter_values_keep_their_shape(self):
        self.assertEqual(exposition.fmt(-2.73), "-2.73")
        self.assertEqual(exposition.fmt(32085.47), "32085.47")

    def test_beyond_four_decimals_loses_the_tail(self):
        """What the OE exporter's six-decimal copy used to keep in full."""
        self.assertEqual(exposition.fmt(558.874425), "558.8744")


class EscapeLabelTest(unittest.TestCase):
    def test_backslash_quote_and_newline(self):
        self.assertEqual(
            exposition.escape_label('a\\b"c\nd'), 'a\\\\b\\"c\\nd'
        )

    def test_non_strings_are_strung(self):
        """The shared definition accepts anything, so no call site crashes."""
        self.assertEqual(exposition.escape_label(123), "123")


if __name__ == "__main__":
    unittest.main()
