"""Tests for the battery dashboard builder.

The builder used to write the committed dashboard at module level, so merely
importing it overwrote the file: a hand edit died on the next import, and the
test suite could not compare the committed JSON with the builder without
destroying it first. The seams under test are the builder's public behaviour:

- importing the builder leaves
  `grafana/provisioning/dashboards/battery-state-of-charge.json` untouched -
  not a byte changed, not a rewrite of the same bytes;
- `build_dashboard()` returns exactly what is committed, so a hand edit (or a
  builder change that was never regenerated) fails the suite;
- `check_invariants()` rejects duplicate panel ids, overlapping panels and
  panels that run off the 24-column grid. Those invariants used to be bare
  asserts in the script body: stripped under `python -O`, and run only when
  someone remembered to run the script.
- `check_semantic_invariants()` pins the board's *claims*, not its geometry:
  every AEMO per-unit query joins `on(unit)` (docs/adr/0001), the bar gauge's
  threshold steps are the SoC policy ramp and its prose quotes the policy's
  gap limit (docs/adr/0005), and no query names a retired OpenElectricity
  series family. The golden-bytes comparison pins the bytes; these pin the
  semantics a regeneration could silently change.

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

import build_battery_dashboard as builder  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Deliberately built from REPO rather than `builder.OUT`: the file under test
# is the committed dashboard at its home in the repo, not wherever the builder
# currently points. A builder that started writing somewhere else should fail
# here, not have the test follow it out of the tree.
COMMITTED = os.path.join(
    REPO, "grafana", "provisioning", "dashboards", "battery-state-of-charge.json"
)


class ImportHasNoWriteTest(unittest.TestCase):
    def test_reloading_the_builder_leaves_the_committed_file_untouched(self):
        """A hand edit must survive an import.

        The write used to sit at module level, so any import - including one
        from a test that only wanted the panel set - regenerated the file and
        silently reverted a hand edit. Import is pure: reload the module and
        the file's bytes *and* its mtime are unchanged. A rewrite of identical
        bytes would still bump the mtime, which is why the timestamp is part
        of the assertion and not only the content.
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


def _panel(panel_id, x, y, w=12, h=4):
    return {"id": panel_id, "gridPos": {"x": x, "y": y, "w": w, "h": h}}


class BuiltMatchesCommittedTest(unittest.TestCase):
    def test_build_dashboard_returns_exactly_the_committed_json(self):
        """The committed file is the builder's output, or the suite fails.

        Two ways to get here wrong, both caught: hand-edit the JSON (built
        != committed) or change the builder and forget to re-run it (same
        inequality, other direction). The comparison is the whole dashboard
        dict - panels, templating, prose - not a spot check, because the
        builder's job is the whole file.
        """
        with open(COMMITTED, encoding="utf-8") as handle:
            committed = json.load(handle)
        self.assertEqual(builder.build_dashboard(), committed)


class InvariantsTest(unittest.TestCase):
    """The invariants that used to be bare asserts in the script body.

    Bare asserts vanish under `python -O` and only fire when the script runs.
    `check_invariants` raises `ValueError`, and the suite calls it directly -
    on the real panel set via `build_dashboard()`, and on the bad shapes below
    so a checker that silently accepts everything fails here too.
    """

    def test_duplicate_panel_ids_are_rejected(self):
        """Ids are the enumeration order, so a repeat means a broken board."""
        with self.assertRaises(ValueError):
            builder.check_invariants([_panel(1, 0, 0), _panel(1, 0, 4)])

    def test_overlapping_panels_are_rejected(self):
        """Same y and same x: the second panel sits on top of the first."""
        with self.assertRaises(ValueError):
            builder.check_invariants([_panel(1, 0, 0), _panel(2, 0, 0)])

    def test_a_panel_running_off_the_grid_is_rejected(self):
        """x + w past 24 is off the 24-column grid - here, the second panel."""
        with self.assertRaises(ValueError):
            builder.check_invariants(
                [_panel(1, 0, 0, w=24), _panel(2, 20, 9, w=12)]
            )

    def test_the_first_panel_is_held_to_the_grid_too(self):
        """The old assert loop started at the second panel.

        The invariant is *every* panel on the grid; a one-panel board that
        ran off the edge used to sail through.
        """
        with self.assertRaises(ValueError):
            builder.check_invariants([_panel(1, 20, 0, w=12)])

    def test_a_well_formed_board_passes(self):
        """Panels stacked top-to-bottom, each within the grid: no complaint."""
        builder.check_invariants([_panel(1, 0, 0, w=24), _panel(2, 0, 9, w=24)])


def _every_expr(dashboard):
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if "expr" in target:
                yield target


class SemanticInvariantsTest(unittest.TestCase):
    """The claims the board makes, checked against the built dict.

    The golden-bytes test pins the committed file to the builder's output, so
    a semantic change laundered through a regeneration - swap `on(unit)` back
    to `on(duid)`, hardcode a gap limit that no longer matches the policy,
    reintroduce a retired OE series - regenerates cleanly and passes. These
    predicates run inside `build_dashboard()` on the assembled dict, so that
    change fails here instead. Each test mutates a copy of the real board:
    a checker that silently accepts the regression must fail too.
    """

    def _mutated_dashboard(self, mutate):
        dashboard = copy.deepcopy(builder.build_dashboard())
        mutate(dashboard)
        return dashboard

    def _first_expr_matching(self, dashboard, predicate):
        for entry in _every_expr(dashboard):
            if predicate(entry["expr"]):
                return entry
        raise AssertionError("no expr in the built dashboard matches")

    def test_the_real_board_passes(self):
        """The committed board satisfies every semantic invariant."""
        builder.check_semantic_invariants(builder.build_dashboard())

    def test_a_duid_join_is_rejected(self):
        """ADR-0001: the fleet join is `on(unit)`, never `on(duid)`.

        The old `label_replace` bridge copied `unit` into `duid` so a `duid`
        join would match; it is gone, and a join that drifts back silently
        loses every unit whose AEMO code differs from its fleet code.
        """

        def mutate(dashboard):
            entry = self._first_expr_matching(
                dashboard, lambda expr: "on(unit)" in expr
            )
            entry["expr"] = entry["expr"].replace("on(unit)", "on(duid)", 1)

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_an_aemo_fleet_join_without_on_unit_is_rejected(self):
        """An AEMO per-unit metric paired with fleet metadata joins on(unit).

        Broader than the literal `on(duid)` ban: the join key can drift to
        another label (`on(name)`, `ignoring(...)`) or fall off entirely, and
        an AEMO metric divided by (or `and`-ed with) the OE fleet gauges
        without `on(unit)` is the ADR-0001 bug wearing a different shape.
        """

        def mutate(dashboard):
            entry = self._first_expr_matching(
                dashboard,
                lambda expr: "aemo_battery_" in expr
                and "oe_battery_fleet" in expr
                and "on(unit)" in expr,
            )
            entry["expr"] = entry["expr"].replace("on(unit)", "on(name)", 1)

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_a_bar_gauge_off_the_soc_ramp_is_rejected(self):
        """ADR-0005: the bar gauge's steps are the SoC policy's ramp.

        The ramp used to exist twice - threshold steps here, RGB stops in the
        map - free to diverge. It lives once in soc_policy now; a bar gauge
        whose steps no longer render that ramp is a restated copy.
        """

        def mutate(dashboard):
            bargauge = next(
                p for p in dashboard["panels"] if p["type"] == "bargauge"
            )
            steps = bargauge["fieldConfig"]["defaults"]["thresholds"]["steps"]
            steps[0]["value"] = 10

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_a_bar_gauge_whose_ramp_is_recoloured_is_rejected(self):
        """Same pin, other axis: values right, colours drifted."""

        def mutate(dashboard):
            bargauge = next(
                p for p in dashboard["panels"] if p["type"] == "bargauge"
            )
            steps = bargauge["fieldConfig"]["defaults"]["thresholds"]["steps"]
            steps[-1]["color"] = "#00FF00"

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_gap_limit_prose_off_the_policy_is_rejected(self):
        """ADR-0005: descriptions quoting the bridge limit quote the policy.

        The prose used to spell "6h" by hand, so regenerating after a policy
        change left the board describing the old model. The number in prose
        and MAX_GAP_HOURS are one fact; a hardcoded quote that disagrees is
        the drift this pins.
        """

        def mutate(dashboard):
            wrong = "%dh" % (int(builder.soc_policy.MAX_GAP_HOURS) + 6)
            for panel in dashboard["panels"]:
                description = panel.get("description", "")
                if "h bridge limit" in description:
                    panel["description"] = description.replace(
                        "%sh bridge limit" % builder.GAP_H, wrong + " bridge limit", 1
                    )
                    return
            raise AssertionError("no panel quotes the bridge limit")

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_a_board_that_stops_quoting_the_gap_limit_is_rejected(self):
        """The limit is a load-bearing claim: at least one quote must remain."""

        def mutate(dashboard):
            for panel in dashboard["panels"]:
                if "description" in panel:
                    panel["description"] = panel["description"].replace(
                        "%sh bridge limit" % builder.GAP_H, "bridge limit"
                    )
                content = panel.get("options", {}).get("content")
                if content:
                    panel["options"]["content"] = content.replace(
                        "%sh bridge limit" % builder.GAP_H, "bridge limit"
                    )

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_a_retired_oe_series_in_a_query_is_rejected(self):
        """The OE reading families are AEMO's now; a query cannot name one.

        RETIRED_METRICS lives in the exporter (its exposition contract); the
        exporter test pins /metrics against it and this pins the board's
        queries against the same list, so a stale duplicate cannot creep back
        onto the dashboard.
        """

        def mutate(dashboard):
            dashboard["panels"][2]["targets"][0]["expr"] = builder.exporter.RETIRED_METRICS[0]

        with self.assertRaises(ValueError):
            builder.check_semantic_invariants(self._mutated_dashboard(mutate))

    def test_a_retired_family_as_a_substring_of_a_live_metric_is_not_flagged(self):
        """The ban matches whole metric names, not substrings.

        `oe_battery_soc_ratio_total` contains a retired family's name; only
        an exact identifier counts as naming it, or the matcher would grow
        false positives the first time a live metric names collide.
        """

        def mutate(dashboard):
            dashboard["panels"][2]["targets"][0]["expr"] = (
                'label_values({__name__=~"oe_battery_soc_ratio_total"}, x)'
            )

        builder.check_semantic_invariants(self._mutated_dashboard(mutate))


if __name__ == "__main__":
    unittest.main()
