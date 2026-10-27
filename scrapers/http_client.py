#!/usr/bin/env python3
"""The one retrying HTTP client, shared by all four exporters.

Fetching an upstream had four private copies of the same loop - three
byte-identical stdlib copies plus an OpenElectricity variant with pacing and
credit counting bolted on - under an identical "HTTP helpers" banner. The
copies had already drifted: the TBM copy dropped the error body from its
message, so whatever the upstream said about a rejection never reached the
log, and the `Accept` header had been copied three different ways with nothing
to say which was right. The choices are settled here, deliberately, rather
than left to whichever file was copied last; see docs/adr/0008.

What the module owns (docs/adr/0008):

  * `ScrapeError` - the exception every failed fetch raises, so one
    `except ScrapeError` above a fetch catches it whatever went wrong;
  * `is_transient` - which faults a second attempt a moment later can
    plausibly clear, with the errno set stated beside it;
  * `describe_http_error` - an HTTP error rendered for a log line, response
    body folded in and trimmed;
  * `http_get` - the retry loop itself: attempt cap, retry budget, and
    exponential backoff with jitter, stated here once.

What it deliberately does *not* own: how many retries each exporter asks for
(`--retries` / `--retry-budget` stay per-exporter configuration and differ on
purpose - a 90 KB listing and a daily 8 MB zip are not the same risk), the
headers a given endpoint wants (callers override them per call), JSON
parsing (bodies are bytes here), or anything the request is *about*.
"""

from __future__ import annotations

import errno
import logging
import random
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

log = logging.getLogger("http_client")

# An error body is usually one short line - "Error: yearA and yearB must be
# between 1954 and 2026" - but the cap keeps a misrouted HTML error page from
# bloating every log line.
MAX_ERROR_BODY = 200

RETRY_BASE_DELAY = 0.5


class ScrapeError(Exception):
    """Raised when a required upstream request cannot be read or understood."""


# Errnos worth a second attempt. The one actually seen in these containers is
# EAI_AGAIN (-3, "Try again") out of getaddrinfo: about 10% of lookups fail
# inside the container (Docker's embedded resolver at 127.0.0.11 forwarding to
# systemd-resolved), and one failed lookup used to cost the whole scrape.
# HTTP errors are deliberately absent - the server answered, so an immediate
# retry is unlikely to differ.
TRANSIENT_ERRNOS = frozenset(
    {
        socket.EAI_AGAIN,  # -3, only in socket, not errno
        errno.EAGAIN,
        errno.ECONNRESET,
        errno.ECONNABORTED,
    }
)

# CloudFront rejects requests without a browser-like User-Agent, which is why
# the default request carries this one. The OE exporter overrides it with a
# truthful UA: its API 403s "Python-urllib" outright and gains nothing from
# pretending to be a browser.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# What a request that states no headers of its own sends. `identity` because
# every parser here reads the bytes it is given and none of them negotiates a
# compressed body; the Accept is the JSON-preferring, HTML-acceptable,
# `*/*`-floored set that every one of these four endpoints can answer, settled
# here rather than copied three ways (docs/adr/0008).
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Encoding": "identity",
    "Accept-Language": "en-AU,en;q=0.9",
}


def is_transient(reason: object) -> bool:
    """True for faults a second attempt a moment later can plausibly clear."""
    if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(reason, "errno", None) in TRANSIENT_ERRNOS


def describe_http_error(exc: urllib.error.HTTPError) -> str:
    """`GET url -> HTTP 400: Error: yearA and yearB must be between 1954 and 2026`

    HTTPError is file-like, so the body is read here and reported rather than
    discarded: on these endpoints the body is often the whole explanation - a
    plain-text complaint about a year range, a JSON message from a bad API key.
    A missing body is a normal outcome, not a failure (the OE API's 403
    carries none at all, which is why the status alone still has to be
    printable), and a body that will not read must not mask that status. The
    whitespace is collapsed so a multiline page stays one log line.
    """
    detail = ""
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001 - a broken error body must not mask the status
        body = b""
    finally:
        exc.close()
    if body:
        text = body.decode("utf-8", "replace").strip()
        if text:
            detail = ": " + " ".join(text.split())[:MAX_ERROR_BODY]
    return "GET %s -> HTTP %s%s" % (exc.url or "?", exc.code, detail)


def http_get(
    url: str,
    timeout: float,
    retries: int = 0,
    retry_budget: float = 0.0,
    *,
    headers: Mapping[str, str] | None = None,
    before_attempt: Callable[[], None] | None = None,
    http_error: Callable[[urllib.error.HTTPError], BaseException] | None = None,
) -> bytes:
    """Fetch a URL, retrying transient faults.

    `retries` caps the attempt count and `retry_budget` caps the total time
    spent retrying. The budget is the one that matters: a failing lookup inside
    a container takes ~5s to give up, so an uncapped retry loop can outlast
    Prometheus's `scrape_timeout` (30s) and turn a recoverable gap into a
    target marked down. Bounding by time keeps the failure local to the
    request.

    `headers` are merged over `DEFAULT_HEADERS`, so a caller states only what
    differs from the default request - a bearer token, a truthful User-Agent,
    an endpoint that only speaks JSON.

    `before_attempt` runs before each attempt, including the first: the OE
    exporter paces its burst limit and counts the request against its daily
    credit budget there, whether or not the attempt succeeds.

    `http_error` returns the exception to raise for a given HTTP error, for
    callers that classify a status (the OE exporter's 401/403 is an
    `AuthError`, not a generic scrape failure). HTTP errors are never
    retried: the server answered, so an immediate retry is unlikely to differ.
    """
    request = urllib.request.Request(
        url,
        headers={**DEFAULT_HEADERS, **(headers or {})},
    )
    started = time.monotonic()
    for attempt in range(retries + 1):
        if before_attempt is not None:
            before_attempt()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error = http_error(exc) if http_error else ScrapeError(describe_http_error(exc))
            raise error from exc
        except urllib.error.URLError as exc:
            if not is_transient(exc.reason):
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            if attempt >= retries:
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            spent = time.monotonic() - started
            if retry_budget and spent >= retry_budget:
                log.warning(
                    "giving up on %s after %.1fs of retry budget: %s",
                    url,
                    spent,
                    exc.reason,
                )
                raise ScrapeError("GET %s -> %s" % (url, exc.reason)) from exc
            # Exponential backoff with jitter. The failures arrive in
            # correlated bursts - a resolver blip takes every attempt inside
            # the same window - so a flat short delay just walks into the same
            # outage, and retrying in lockstep piles every machine onto the
            # resolver at once. Jitter de-synchronises them.
            delay = RETRY_BASE_DELAY * (2**attempt)
            delay *= 0.75 + 0.5 * random.random()
            log.warning(
                "transient failure (attempt %d of %d) on %s: %s; retrying in %.1fs",
                attempt + 1,
                retries + 1,
                url,
                exc.reason,
                delay,
            )
            time.sleep(delay)
            continue
    raise ScrapeError("GET %s -> gave up after %d attempts" % (url, retries + 1))
