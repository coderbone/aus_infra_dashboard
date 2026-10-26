#!/usr/bin/env python3
"""The one Prometheus exposition formatter, shared by all four exporters.

Label escaping and sample-value formatting had four private copies - one per
exporter - under an identical "Prometheus exposition" banner. The copies
already disagreed: the OE battery exporter rounded fractional values to six
decimals while the other three rounded to four, in functions with the same
name. The choice is settled here, deliberately, rather than left to whichever
file was copied last; see docs/adr/0007.

What the module owns (docs/adr/0007):

  * `escape_label` - a label value escaped for the Prometheus text format
    (backslash, double quote, newline), accepting anything via `str()`;
  * `fmt` - a sample value rendered for the text format: `None` and the
    non-finite floats as `NaN` / `+Inf` / `-Inf`, integral values bare, other
    numbers at the four-decimal precision stated beside the function.

What it deliberately does *not* own: metric names, label *names*, or the line
layout of any exporter's exposition - only the two helpers every exporter
writes through. A consumer that does not know this module still works; it just
risks drifting from the other three, which is the bug this module ends.
"""

from __future__ import annotations

import math


def escape_label(value: str) -> str:
    """Escape a label value for the Prometheus text exposition format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt(value) -> str:
    """Format a sample value for the Prometheus text exposition format.

    Four decimal places, settled deliberately (docs/adr/0007): three of the
    four exporters already rounded to 4, and the two suites that pin a
    fractional result pin 4. Prometheus parses exposition values to float64
    regardless, so further digits would be display, not stored precision. The
    OE exporter used to round to 6; its fractional values carrying more than
    four decimals now emit four.
    """
    if value is None:
        return "NaN"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "NaN" if math.isnan(number) else ("+Inf" if number > 0 else "-Inf")
    if number.is_integer():
        return str(int(number))
    return repr(round(number, 4))
