"""Structured logging, configured once for the process.

structlog was being *used* throughout the engine but never *configured*, which
meant it fell back to its defaults: console output, no timestamps worth
parsing, and — the part that matters — no central point where anything could be
redacted.

That last point is the reason this module exists. Credential redaction was
being applied at individual call sites, which works exactly until someone adds
a call site and does not think about it. A library we do not control raising an
exception that carries a proxy URL is not a hypothetical: `httpx` and
`curl_cffi` both put the full connection string into transport errors, and the
proxy URL carries the password.

So redaction moved here, into a processor that every event passes through.
A call site can no longer forget, because there is nothing for it to remember.
Individual `redact()` calls in the fetchers are kept as defence in depth —
they also sanitise the string stored in `fetch_log`, which is not a log event.

11-compliance.md section 4: credentials must not appear in logs, stored errors
or crash reports.

The same processor masks email addresses, for a different reason. A credential
in a log is a security problem; an address is a COMPLIANCE one. Section 2 calls
a named individual's address personal data, and section 6 requires deletion by
domain and by URL to service an erasure request — which a log aggregator, on
its own retention schedule, is entirely outside. Deleting the `contacts` row
does not reach it, and the suppression list has the same hole: it means "never
contact this person" while a logged address is a copy nothing checks.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from engine.core.redaction import mask_emails, redact

# Values under these keys are replaced wholesale rather than pattern-matched.
# A bare API key or proxy password has no distinguishing shape — by the time it
# is a value in an event dict, the KEY is the only thing identifying it as a
# secret.
#
# Written WITHOUT separators, and the key is stripped of `-` and `_` before
# matching, so one entry covers `api_key`, `apiKey`, `x-api-key` and
# `API-KEY`. Listing the spellings individually is how `x-api-key` got missed
# the first time.
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "cookie",
)

MASK = "***redacted***"

# How deep to walk nested structures. Headers and payloads nest a little;
# nothing legitimate nests far, and an unbounded walk on a log processor is a
# denial of service against our own logging.
MAX_DEPTH = 4


def _is_sensitive(key: str) -> bool:
    normalised = key.lower().replace("-", "").replace("_", "")
    return any(part in normalised for part in SENSITIVE_KEY_PARTS)


def _clean(value: Any, depth: int = 0) -> Any:
    """Redact a single value, recursing into containers."""
    if depth > MAX_DEPTH:
        return value
    if isinstance(value, str):
        # Credentials first, then personal data. Both are pattern-based, so a
        # string carrying one of each is cleaned of both.
        return mask_emails(redact(value))
    if isinstance(value, dict):
        return {
            k: (MASK if isinstance(k, str) and _is_sensitive(k) else _clean(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        cleaned = [_clean(v, depth + 1) for v in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def redaction_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip credentials from every event, whatever produced it.

    Two mechanisms, because secrets arrive in two shapes. A credentialed URL
    has a recognisable form and is caught by pattern anywhere in any string.
    A bare secret does not, so it is caught by the name of the key holding it.
    """
    return {
        key: (MASK if _is_sensitive(key) else _clean(value)) for key, value in event_dict.items()
    }


def configure_logging(*, json_logs: bool | None = None, level: str = "INFO") -> None:
    """Configure structlog for the process. Safe to call more than once.

    JSON in production because logs are read by machines there; the console
    renderer in development because they are read by a person.
    """
    if json_logs is None:
        # A terminal means a human is watching.
        json_logs = not sys.stderr.isatty()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Exception formatting BEFORE redaction: it turns an exception object
        # into a string, and that string is exactly where a credentialed proxy
        # URL comes from. Redacting first would leave the traceback untouched.
        structlog.processors.format_exc_info,
        redaction_processor,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        # Must be the stdlib factory, not PrintLoggerFactory: `add_logger_name`
        # reads `logger.name`, which only a stdlib logger has. Pairing it with
        # a PrintLogger raises AttributeError on EVERY log call — including the
        # ones reporting the incident you are trying to read about.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # The standard library's loggers (uvicorn, httpx) should not bypass this.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
