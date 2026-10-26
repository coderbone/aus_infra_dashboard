# The exposition module owns label escaping and sample formatting

All four exporters write Prometheus text exposition, and each carried its own
private copy of the two helpers every line goes through — a label-value
escaper and a sample-value formatter — under an identical "Prometheus
exposition" banner. The copies had already drifted: the OE battery exporter
rounded fractional values to six decimals while the other three rounded to
four, in functions with the same name. Nothing pinned which was right; the
winner was whichever file was copied last.

We introduce `scrapers/exposition.py`: `escape_label` (backslash, double
quote, newline, accepting anything via `str()`) and `fmt` (`None` and the
non-finite floats as `NaN` / `+Inf` / `-Inf`, integral values bare, other
numbers at four decimal places). All four exporters import both by name, so
call sites and the `exporter.fmt` / `exporter.escape_label` seams the test
suites exercise are unchanged. The four-decimal precision is stated beside
the function and pinned in `test_exposition.py`, which also pins each
exporter's helpers as the module's by identity — a re-copied helper would
satisfy `==` and drift, which is the bug this ends.

## Considered Options

- **Keep six decimals in the OE exporter.** Rejected: three of four exporters
  already round to four, the two suites that pin a fractional result pin four
  (`0.1596`, `11.3333`), and Prometheus parses exposition values to float64
  regardless — digits past four are display, not stored precision. Keeping
  six would silently change three exporters instead of one.
- **Per-exporter precision as a parameter.** Rejected: precision is exactly
  the sort of shared contract that drifted in the first place; a parameter
  would give each exporter a new place to disagree.
- **Pin the copies' equality with a test.** Rejected by the same argument as
  docs/adr/0005's ramp: two definitions with a drift alarm are still two
  definitions.

## Consequences

The OE exporter's formatter contract changes from six decimals to four, and
two of its metrics change their emitted text in production today:
`oe_last_poll_timestamp_seconds` and `oe_last_fleet_refresh_timestamp_seconds`
are `time.time()` floats, so they are fractional on every scrape and now
render four decimals where they used to render six (e.g. `…2017.123457`
becomes `…2017.1234`) — a loss of two exposition digits on a value Prometheus
stores as float64 anyway. The OE capacity figures do not change: every real
`capacity_storage` / `capacity_registered` value in the fixture set is
integral or carries at most two decimals, and the fleet total (`32085.47`) is
unchanged. The other three exporters' output is byte-identical — their own
timestamp metrics always rounded to four; their escaper now accepts
non-strings via `str()` where it previously required one, which no call site
exercises differently. The single-file bind-mount arrangement of
docs/adr/0002 is extended: `exposition.py` is mounted read-only beside all
four exporters in docker-compose.yml, and the web map, which formats nothing
for exposition, does not mount it.
