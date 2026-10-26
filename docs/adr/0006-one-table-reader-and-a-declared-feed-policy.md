# One table reader and a declared feed policy

The exporter reads five AEMO dispatch archives — the daily Next_Day_Dispatch,
Dispatch_SCADA, ROOFTOP_PV ACTUAL, and DispatchIS's REGIONSUM and
INTERCONNECTORRES tables — and each parser carried its own copy of the same
skeleton: open the zip, find the `.csv` member, stream the tagged `I`/`C`/`D`
rows, index the header's columns by name, keep the newest stamp per key and for
the file, and raise when the file yielded nothing. The copies drifted in small
ways: the daily feed read SETTLEMENTDATE and DUID positionally at columns 4 and
6 even though the header named them, so a column reorder upstream would have
silently mis-dated or mis-keyed every row while the other four feeds followed
their names.

Separately, `render_intraday` spelled the intraday failure policy — which
series survive which feed's failure — as ten interleaved `and scada_ok` /
`and region_ok` branches threaded between the series blocks, with the prose
version of the policy living in the function's docstring. A reader had to
follow every branch to know what a failed dispatch read drops; adding a feed
meant another flag, another parameter and more interleaving.

We introduce two declarations in `scrapers/aemo_battery_exporter.py`:

- **`read_aemo_table(...)`** — the archive skeleton, implemented once. Each
  feed is now a projection over it: a `table` tag tuple, a `columns` builder
  (by name, None = shape changed), a `_project_*` function that turns one data
  row into readings via `put(bucket, stamp, key, entry)`, the `results` maps the
  return always carries, and the `missing` message for a file that produced
  nothing. The newest-wins bookkeeping and the empty-file failure live in the
  reader, where they were identical five times over.
- **`INTRADAY_BLOCKS`** — the intraday failure policy as data: an ordered
  table of `(block, feed, emitter)`, where `render_intraday` emits a block only
  when its feed verified this cycle and `feed=None` marks a block with no feed
  gate (withheld only by its own data being absent, as
  `aemo_battery_fleet_metadata_missing` is today).

Every emission path in `render_intraday` is now one loop over that table; the
feed flags appear only in the `feeds` map the loop reads and in the always-on
success gauges.

## Considered Options

- **Extract only the zip open/find helpers, keep five loop bodies.** Rejected:
  the duplicated part is not the zip handling but the row discipline — header
  matching, name-indexed columns, newest-stamp keeping, empty-file failure —
  and that is where the five copies could drift apart.
- **The reader yields raw rows; each feed keeps its own newest stamps.**
  Rejected: "keep the newest stamp" is byte-identical in all five feeds (and
  runs twice in the DispatchIS storage feed), so leaving it in the projections
  would leave the skeleton split across the reader and its callers.
- **Keep the render's branches and replace the booleans with lookups**
  (`if regions and published("region_storage"):` over a block→feed map).
  Rejected: the policy would still be spelled out interleaved through the
  render, merely indirection away; walking one declared table makes the render
  itself branch-free in its feed handling, which is what "adding a feed does
  not add a branch" means.
- **Describe the failure matrix only in the render's docstring** (as before).
  Rejected: prose is not what the tests pin and not what a reader consults when
  adding a series; the declaration and the docstring now say the same thing
  with the table as the executable one.

## Consequences

Existing parser and render tests pass unchanged, and the refactor was verified
against a golden corpus: `render_intraday` output is byte-identical across all
16 feed-flag permutations plus empty-interconnector, cold-start-fleet,
held-state-after-failure and unknown-link inputs. Two behaviour edges are
deliberate and pinned by new tests: the daily feed now reads SETTLEMENTDATE and
DUID by name (the reorder test that fails on the positional read), and the
rooftop feed — previously the one feed with no tests at all — has
characterization tests at the parser and render seams. `INTRADAY_BLOCKS` is pinned by its own test — names, feeds and emission
order — so the declaration itself cannot drift without a failing suite. Two
edges on malformed
input differ deliberately: the reader applies one width guard — a data row must
be at least as wide as its header's widest indexed column — so a truncated row
is skipped whole instead of partially read (a short INTERCONNECTORRES row used
to publish whichever optional columns its length happened to reach, a short
REGIONSUM row its curtailment), and the `handled N rows` figure inside a "no
readings" error counts every table row past that guard rather than each feed's
earlier cutoff, which is message text only. Well-formed AEMO rows always carry
the full column width, so neither edge is reachable from a real file.

Adding a sixth feed is: its `columns` builder and `_project_*` projection over
`read_aemo_table`, its `parse_*` call, a line in `INTRADAY_BLOCKS`, a line in
the render's `feeds` map, and its always-on success gauge — no new branch in
the render and no new copy of the skeleton. The vocabulary the issue uses —
*feed*, *feed policy* — is not yet in `GLOSSARY.md`; that gap belongs to
`/domain-modeling`, not to this change.
