# The inference module owns the estimator

The arithmetic that turns a daily energy anchor plus five-minute measured
power into a per-unit inferred stored-MWh figure lived inside the AEMO
exporter's intraday poller: `_clamp_inferred`, `_advance_entry` and the gap
checks in `_advance_inference`, reading their policy (how long a gap to
bridge, whether to clamp, the charging efficiency) from module constants and
poller attributes. The model and the poller were only ever exercised together,
so the real bugs hid in how they were called rather than in the arithmetic,
and a regression test needed a poll thread, a state file and a running
container to exist at all.

We introduce `scrapers/inference.py`, a pure module: `Policy` states the
estimator's behaviour explicitly (no hidden defaults), `clamp_stored` owns the
clamp rule, `advance` owns one integration step, and `estimate(anchor, series,
capacity, policy, now)` is the whole estimator — given an anchor, a power
series, a capacity and a time it returns the inferred entry, or absent
(`None`) when a gap exceeds the policy or the result is already stale. The
anchor is never stamped forward. The poller keeps the impure parts — fetching
the daily report, rebuilding a gap from the SCADA archive, persisting state,
the live-step guard — and drives the module with an explicit `Policy`.

## Considered Options

- **Leave the estimator in the poller.** Rejected: the issue it was filed
  against exists because the model cannot be driven from a test without the
  poller, and the poller's gap orchestration (backfill, `_backfilled_end`,
  the anchor-ts comparison) keeps re-expressing the arithmetic's rules.
- **A scrapers-and-webmap shared package/folder.** Rejected: the web map
  only reads the published Prometheus series — it never computes inference —
  so there is no second consumer to share a tree with. The flat-module
  convention `docs/adr/0002` and `docs/adr/0003` blessed for `fleet.py` and
  `links.py` already covers one-consumer shared modules.
- **Policy as module defaults in `inference.py`.** Rejected: the whole point
  is that the policy is an explicit input, so the defaults stay where they
  are configured — the exporter's constants and CLI flags — and the module
  takes a `Policy` every call.
- **A dataclass for the entry instead of the `{"ts", "mwh", "clamped"}`
  dict.** Rejected for now: the dict is the exporter's persisted state-file
  shape and the shape its existing tests pin; a conversion at the boundary
  would churn both for no behavioural gain.

## Consequences

`scrapers/inference.py` is bind-mounted read-only into the AEMO exporter, the
same arrangement as `fleet.py` and `links.py`. `charge_efficiency` travels in
`Policy` because a pure `advance` cannot read it off the poller. The flag the
clamp returns stays per-step, not accumulated — the OE exporter's
`saturated` convention — and with `clamp=False` the raw drift is served,
flag and all, which is the pre-extraction behaviour the existing inference
tests pin. The gap rule the exporter's live path uses
(`MAX_INFER_GAP_HOURS`) and the age gate at serve time both read the poller's
`Policy` now, so the "how long a gap it will bridge" number has one owner;
the stricter live-step/backfill guard remains poller orchestration, because
whether a hole can be rebuilt from the archive is a fact about fetching, not
about the arithmetic.
