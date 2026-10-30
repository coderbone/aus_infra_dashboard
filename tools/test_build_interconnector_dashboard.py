"""Tests for the interconnector dashboard builder.

Same seams as the battery builder's suite (tools/test_build_battery_dashboard.py),
because the two builders are the same arrangement: generated board, committed
JSON, invariants that name what the board *claims*.

- importing the builder leaves
  `grafana/provisioning/dashboards/interconnectors.json` untouched - not a
  byte changed, not a rewrite of the same bytes;
- `build_dashboard()` returns exactly what is committed, so a hand edit (or a
  builder change that was never regenerated) fails the suite;
- `check_invariants()` rejects duplicate panel ids, overlapping panels and
  panels that run off the 24-column grid (shared with the battery builder);
- `check_semantic_invariants()` pins the board's claims: one panel per
  scrapers/links.py link in registry order, every query pinned to its own
  link, the five metric families with only the reverse rating negated, no
  rate() over held interval gauges, the pinned datasource, the megwatt unit
  (grafana's `megwatt`, not the unknown id `mw` - an unknown unit is printed
  verbatim on the axis) on a
  centred axis that never bridges a gap, prose quoting the registry's own
  ratings, and the metric families matching the web map's read contract
  (webmap/server.py LIVE_QUERIES).

From the repo root:

    python3 -m unittest discover -s tools -t tools -v
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_interconnector_dashboard as builder  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Deliberately built from REPO rather than `builder.OUT`: the file under test
# is the committed dashboard at its home in the repo, not wherever the builder
# currently points. A builder that started writing somewhere else should fail
# here, not have the test follow it out of the tree.
COMMITTED = os.path.join(
    REPO, "grafana", "provisioning", "dashboards", "interconnectors.json"
)


class ImportHasNoWriteTest(unittest.TestCase):
    def test_importing_the_builder_does_not_touch_the_committed_json(self):
        """Importing must be side-effect free, or the suite destroys the very
        bytes it is about to compare against - the failure that got the
        battery builder its seam. Both the content and the mtime are pinned:
        a rewrite of identical bytes still clobbers a hand edit's timestamp
        and hides when the file last changed.
        """
        with open(COMMITTED, "rb") as handle:
            before_bytes = handle.read()
        before_mtime = os.stat(COMMITTED).st_mtime_ns
        importlib.reload(builder)
        with open(COMMITTED, "rb") as handle:
            after_bytes = handle.read()
        after_mtime = os.stat(COMMITTED).st_mtime_ns
        self.assertEqual(after_bytes, before_bytes)
        self.assertEqual(after_mtime, before_mtime)


def _panel(panel_id, x, y, w=12, h=8):
    """A bare gridPos-shaped panel for the structural checker."""
    return {"id": panel_id, "gridPos": {"x": x, "y": y, "w": w, "h": h}}


class BuiltMatchesCommittedTest(unittest.TestCase):
    def test_build_dashboard_returns_exactly_the_committed_json(self):
        """The committed file is the builder's output, or the suite fails.

        Two ways to get here wrong, both caught: hand-edit the JSON (built
        != committed) or change the builder and forget to re-run it (same
        inequality, other direction). The comparison is the whole dashboard
        dict - panels, prose, styling - not a spot check, because the
        builder's job is the whole file.
        """
        with open(COMMITTED, encoding="utf-8") as handle:
            committed = json.load(handle)
        self.assertEqual(builder.build_dashboard(), committed)


class InvariantsTest(unittest.TestCase):
    """The structural invariants, shared with the battery builder."""

    def test_duplicate_panel_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            builder.check_invariants([_panel(1, 0, 0), _panel(1, 0, 8)])

    def test_overlapping_panels_are_rejected(self):
        """Same y and same x: the second panel sits on top of the first."""
        with self.assertRaises(ValueError):
            builder.check_invariants([_panel(1, 0, 0), _panel(2, 0, 0)])

    def test_a_panel_running_off_the_grid_is_rejected(self):
        with self.assertRaises(ValueError):
            builder.check_invariants(
                [_panel(1, 0, 0, w=24), _panel(2, 20, 8, w=12)]
            )

    def test_a_well_formed_board_passes(self):
        """The builder's own layout shape: two-up rows down the grid."""
        builder.check_invariants([_panel(1, 0, 0), _panel(2, 12, 0),
                                  _panel(3, 0, 8)])


def _dashboard():
    """A fresh deep copy of the built board - `build_dashboard()` returns a
    dict aliasing the module's panel list, so every mutation test works on a
    copy and leaves the builder's own state alone."""
    return copy.deepcopy(builder.build_dashboard())


def _first_panel(dashboard):
    return dashboard["panels"][0]


class SemanticInvariantsTest(unittest.TestCase):
    """The board's claims. Each test breaks exactly one and expects the
    checker to name it - a checker that accepts everything fails here."""

    def test_the_untouched_board_passes(self):
        builder.check_semantic_invariants(_dashboard())

    def test_a_panel_querying_another_link_is_rejected(self):
        """Cross-link leakage: one panel drawing a neighbour's series."""
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        own = panel["title"].split(" · ", 1)[0]
        other = next(key for key in builder.links.LINKS if key != own)
        panel["targets"][0]["expr"] = panel["targets"][0]["expr"].replace(
            'interconnector="%s"' % own, 'interconnector="%s"' % other
        )
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_rate_over_a_held_gauge_is_rejected(self):
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        panel["targets"][0]["expr"] = "rate(%s[5m])" % panel["targets"][0]["expr"]
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_missing_series_is_rejected(self):
        dashboard = _dashboard()
        _first_panel(dashboard)["targets"].pop()
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_an_unnegated_reverse_rating_is_rejected(self):
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        expr = panel["targets"][4]
        self.assertTrue(expr["expr"].startswith("- "))
        expr["expr"] = expr["expr"][2:]
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_negated_flow_is_rejected(self):
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        panel["targets"][0]["expr"] = "- " + panel["targets"][0]["expr"]
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_foreign_metric_family_is_rejected(self):
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        panel["targets"][0]["expr"] = 'up{interconnector="NSW1-QLD1"}'
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_dropped_link_is_rejected(self):
        """The panel set is the registry - one panel per link, in order."""
        dashboard = _dashboard()
        dashboard["panels"].pop()
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_an_extra_panel_is_rejected(self):
        dashboard = _dashboard()
        dashboard["panels"].append(copy.deepcopy(dashboard["panels"][0]))
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_panel_title_renamed_off_the_registry_is_rejected(self):
        dashboard = _dashboard()
        _first_panel(dashboard)["title"] = "QNI · New South Wales"
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_the_unpinned_datasource_is_rejected(self):
        dashboard = _dashboard()
        _first_panel(dashboard)["targets"][0]["datasource"] = {
            "type": "prometheus", "uid": "someone-elses-prom"
        }
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_non_megwatt_unit_is_rejected(self):
        dashboard = _dashboard()
        _first_panel(dashboard)["fieldConfig"]["defaults"]["unit"] = "short"
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_gap_bridging_is_rejected(self):
        """A gap is the exporter stopped, not zero flow."""
        dashboard = _dashboard()
        custom = _first_panel(dashboard)["fieldConfig"]["defaults"]["custom"]
        custom["spanNulls"] = True
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_an_off_centre_axis_is_rejected(self):
        """Flow and both limits cross zero; an uncentred axis hides the sign."""
        dashboard = _dashboard()
        custom = _first_panel(dashboard)["fieldConfig"]["defaults"]["custom"]
        custom["axisCenteredZero"] = False
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_prose_restating_a_rating_is_rejected(self):
        """The description quotes scrapers/links.py; a stale number fails."""
        dashboard = _dashboard()
        panel = _first_panel(dashboard)
        own = panel["title"].split(" · ", 1)[0]
        quoted = "%s MW" % builder._mw(builder.links.LINKS[own].nominal_forward_mw)
        panel["description"] = panel["description"].replace(quoted, "1 MW")
        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(dashboard)

    def test_a_metric_family_drifting_from_the_webmap_contract_is_rejected(self):
        """One contract, two consumers: the families must match
        webmap/server.py LIVE_QUERIES. The checker reads the module's table,
        so the table is what gets lied to here - a real rename on either side
        fails the same way."""
        original = builder._CONTRACT
        builder._CONTRACT = dict(original, flow="aemo_interconnector_mw")
        try:
            with self.assertRaises(ValueError):
                builder.check_semantic_invariants(_dashboard())
        finally:
            builder._CONTRACT = original


if __name__ == "__main__":
    unittest.main()
