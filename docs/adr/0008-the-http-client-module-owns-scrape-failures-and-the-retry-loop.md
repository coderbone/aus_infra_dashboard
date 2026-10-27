# The http_client module owns scrape failures and the retry loop

All four exporters fetch their upstream over the same retrying HTTP client,
and each carried its own copy under an identical "HTTP helpers" banner: three
byte-identical stdlib copies plus an OpenElectricity variant with pacing and
credit counting bolted on. The copies had already drifted. The TBM copy
dropped the error body from its message, so whatever ArcGIS said about a
rejection never reached the log - only `HTTP 403`, with the reason thrown
away. The `Accept` header had been copied three different ways, the retry
budget warning two ways, and the give-up message three ways, with nothing to
say which was right; the winner was whichever file was copied last. This is
the shared skeleton docs/adr/0007's pattern did not reach.

We introduce `scrapers/http_client.py`: `ScrapeError` (the exception every
failed fetch raises), `is_transient` (the transient errno set, stated beside
it, with HTTP errors deliberately absent because the server answered),
`describe_http_error` (an HTTP error rendered for a log line, response body
read, folded in and trimmed to `MAX_ERROR_BODY`), and `http_get` (the loop:
attempt cap, retry budget, exponential backoff with jitter at
`RETRY_BASE_DELAY`, plus the default request headers). All four exporters
import all four by name, so call sites and the seams the suites already
exercise - `exporter.http_get`, `exporter.ScrapeError`,
`exporter.is_transient`, `exporter.describe_http_error`, and the patched
`urlopen`/`time.sleep` - are unchanged. `test_http_client.py` pins each name
as the module's by identity, the way `test_exposition.py` does for the
exposition helpers: a re-copied definition would satisfy `==` and drift, which
is the bug this ends.

What stays with each exporter is what is genuinely its own: how many retries
and how much budget it asks for (`--retries` / `--retry-budget` defaults differ
on purpose - a 90 KB listing, a daily 8 MB zip and a metered JSON API are not
the same risk), the JSON or HTML shape on the other side of the fetch, and -
via two keyword-only hooks on `http_get` - the OpenElectricity exporter's
per-attempt pacing and credit counting (`before_attempt`) and its 401/403
classification into `AuthError` (`http_error`). The hooks carry facts about
that API; the retry policy they sit inside has one owner.

The default request headers are settled here too: the browser User-Agent
CloudFront requires, the JSON-preferring / HTML-acceptable / `*/*`-floored
`Accept` all four endpoints can answer, `identity` encoding (every parser
reads the bytes it is given), and `en-AU` language preference. A caller
overrides per call, which is how the OE exporter keeps its truthful User-Agent
(the API 403s `Python-urllib`), its JSON `Accept` and its bearer token.

## Considered Options

- **Keep the four copies and pin their equality with a test.** Rejected by the
  same argument as docs/adr/0005's ramp and docs/adr/0007: two definitions
  with a drift alarm are still two definitions.
- **Keep the OE exporter's private loop**, because pacing and credit counting
  look like a different client. Rejected: those are per-attempt facts, not a
  second policy, and the loop underneath them (backoff, jitter, budget,
  transient set) is already identical. Two hooks express the difference;
  a private copy expresses it as a fork.
- **Have each exporter pass its full header set per call**, making the client
  headerless. Rejected: the suites' fetch fakes are fixed at
  `(url, timeout, retries, retry_budget)` and must stay intact, and headers an
  endpoint needs on *every* request are defaults with declared overrides, not
  arguments at every one of a dozen call sites.
- **Fold `--retries` / `--retry-budget` defaults into the module** as well.
  Rejected: the loop is shared policy, but the allowance is configuration, and
  the four exporters' allowances are documented per exporter on purpose.

## Consequences

The TBM exporter's HTTP errors now carry the response body like the other
three, so a rejection is diagnosable from the log line alone. The AEMO
exporter's `Accept` unifies with the shared default (it was
`text/html,application/xhtml+xml;q=0.9,*/*;q=0.8`, and the default also
serves `*/*` and `text/html`, which is what NEMWEB's static listings answer);
no test pinned it. The OE exporter's requests gain the shared
`Accept-Language` header alongside the `identity` encoding it already sent,
and its exhausted-retry message becomes the shared
`GET url -> gave up after N attempts` (the reason it stopped is still in the
warning logged on the way out). Retry warnings are logged under the
`http_client` logger name now, which the exporters' log format
(`%(asctime)s %(levelname)s %(message)s`) does not print, so the emitted lines
are unchanged. The single-file bind-mount arrangement of docs/adr/0002 is
extended: `http_client.py` is mounted read-only beside all four exporters in
docker-compose.yml, and the web map, which fetches nothing, does not mount it.
