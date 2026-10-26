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

From the repo root:

    python3 -m unittest discover -s tools -t tools -v
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
