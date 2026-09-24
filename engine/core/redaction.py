"""Credential redaction for log output.

Lives in the public core deliberately. This is a general log-safety concern —
any transport that takes a URL with userinfo can put credentials into an
exception string — and a self-hosted deployment needs it as much as a hosted
one. What is proprietary is the proxy *intelligence*, not the good manners.

Defence in depth: call sites should already be logging a safe description,
but a credentialed URL can reach a log through an exception raised inside a
library we do not control.
"""

from __future__ import annotations

import re

# Any userinfo section in a URL — the shape credentials leak in.
_CREDENTIAL_URL = re.compile(r"(?P<scheme>\w+://)(?P<user>[^:@/\s]+):(?P<secret>[^@/\s]+)@")

# The replacement deliberately carries NO colon, so it cannot itself match
# _CREDENTIAL_URL. The obvious `***:***@` does match, which made
# `contains_credentials()` report already-redacted text as leaking — a leak
# detector that fires on its own output is worse than none, because every
# assertion built on it passes for the wrong reason.
PLACEHOLDER = "<redacted>"


def redact(text: str) -> str:
    """Strip credentials from anything about to be logged or stored.

    The host and port survive on purpose. Redaction that destroys the
    diagnostic value gets switched off by whoever is debugging at 3am.
    """
    return _CREDENTIAL_URL.sub(lambda m: f"{m.group('scheme')}{PLACEHOLDER}@", text)


def contains_credentials(text: str) -> bool:
    """True if the text carries a credential-shaped URL.

    Used by the leak test: assert no formatted log line matches.
    """
    return _CREDENTIAL_URL.search(text) is not None


# --------------------------------------------------------------------------
# Personal data
# --------------------------------------------------------------------------

# 11-compliance.md section 2: "a named individual's address (john@example.com)
# is personal data". Section 6 requires deletion by domain and by URL to
# service an erasure request — and an address sitting in application logs is
# outside that entirely. Logs ship to an aggregator, are retained on their own
# schedule, and are not reached by deleting the `contacts` row. The suppression
# list has the same hole: it means "never contact this person", but a logged
# address is a copy nothing checks.
#
# The lead-gen call sites are already careful — `export.py` logs `email_domain`
# rather than the address. That is a convention, and a convention is one edit
# from being wrong. This makes it structural.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


def mask_emails(text: str) -> str:
    """Replace the local part of any email, keeping the domain.

    Partial rather than wholesale, for the same reason the host survives
    credential redaction. The domain is what makes a log line useful — which
    directory, which company, which target — and it is not what identifies a
    person. `sarah.jones@example.com` becomes `<redacted>@example.com`.

    Note this is pattern-based, not key-based. A field called `email_domain`
    holding `example.com` is not email-shaped and passes through untouched,
    which is what keeps the existing lead-gen logging readable.
    """
    return _EMAIL.sub(lambda m: f"{PLACEHOLDER}@{m.group(1)}", text)


def contains_email(text: str) -> bool:
    """True if the text carries a full email address.

    As with credentials, this must NOT match its own masked output — otherwise
    every "no personal data in logs" assertion passes for the wrong reason.
    `<redacted>@example.com` contains `<` and `>`, which the local-part class
    excludes, so it cannot match.
    """
    return _EMAIL.search(text) is not None
