"""Credentials must never reach a log line (11-compliance.md section 4).

The threat is not a developer logging a password on purpose. It is a library
we do not control raising an exception that carries the full proxy URL —
`httpx` and `curl_cffi` both do — and a call site logging that exception
without thinking about what is inside it.

Which is why these tests exercise the PROCESSOR rather than the call sites.
A test per call site tests the ones that exist today; a test on the processor
covers the ones written next year.
"""

from __future__ import annotations

import pytest

from engine.core.redaction import (
    contains_credentials,
    contains_email,
    mask_emails,
    redact,
)
from engine.logging_config import MASK, redaction_processor

PROXY_URL = "http://gooduser:supersecret@proxy.example.net:12321"


def process(**event: object) -> dict[str, object]:
    return redaction_processor(None, "info", dict(event))


# --------------------------------------------------------------------------
# Shape 1: a credentialed URL, recognisable anywhere in any string
# --------------------------------------------------------------------------


def test_a_proxy_url_in_a_message_is_redacted() -> None:
    out = process(event="fetch_failed", error=f"ConnectError: {PROXY_URL}")
    assert "supersecret" not in str(out)
    assert "gooduser" not in str(out)


def test_the_host_survives_redaction() -> None:
    """Redaction that destroys the diagnostic value gets removed by whoever is
    debugging at 3am. The host must still be readable."""
    out = process(event="fetch_failed", error=f"ConnectError: {PROXY_URL}")
    assert "proxy.example.net" in str(out["error"])


def test_a_traceback_string_is_redacted() -> None:
    """`format_exc_info` runs BEFORE redaction, so the formatted traceback is
    a plain string by the time the processor sees it. If the ordering is ever
    reversed, this fails."""
    traceback = f'  File "httpx/_transports.py", line 90\n    connect("{PROXY_URL}")\n'
    out = process(event="crash", exception=traceback)
    assert "supersecret" not in str(out)


def test_credentials_nested_in_a_dict_are_redacted() -> None:
    out = process(event="retry", context={"upstream": {"proxy": PROXY_URL}})
    assert "supersecret" not in str(out)


def test_credentials_in_a_list_are_redacted() -> None:
    out = process(event="pool", endpoints=[PROXY_URL, "http://clean.example.net"])
    assert "supersecret" not in str(out)


# --------------------------------------------------------------------------
# Shape 2: a bare secret, identifiable only by the key holding it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "proxy_password",
        "api_key",
        "apiKey",
        "Authorization",
        "x-api-key",
        "secret",
        "refresh_token",
        "Cookie",
    ],
)
def test_a_sensitive_key_is_masked_whatever_its_value(key: str) -> None:
    """A bare API key has no distinguishing shape — no pattern can find it.
    The name of the key is the only signal it is a secret."""
    out = process(event="auth", **{key: "sk-live-abcdef123456"})
    assert out[key] == MASK
    assert "abcdef123456" not in str(out)


def test_a_sensitive_key_inside_headers_is_masked() -> None:
    out = process(event="request", headers={"Authorization": "Bearer sk-live-xyz"})
    assert "sk-live-xyz" not in str(out)


def test_ordinary_fields_are_left_alone() -> None:
    """Over-redaction makes logs useless, which is its own kind of failure."""
    out = process(event="fetch", url="https://example.com/a", tier="http", status=200)
    assert out["url"] == "https://example.com/a"
    assert out["tier"] == "http"
    assert out["status"] == 200


def test_a_url_without_credentials_is_untouched() -> None:
    assert redact("https://example.com/path?q=1") == "https://example.com/path?q=1"


# --------------------------------------------------------------------------
# The guard itself
# --------------------------------------------------------------------------


def test_the_detector_agrees_with_the_redactor() -> None:
    """`contains_credentials` is what the leak assertions are built on. If it
    and `redact` ever disagree, every test above passes while leaking."""
    assert contains_credentials(PROXY_URL)
    assert not contains_credentials(redact(PROXY_URL))


def test_no_processed_event_reports_as_leaking() -> None:
    out = process(
        event="everything_at_once",
        error=f"failed via {PROXY_URL}",
        headers={"Authorization": "Bearer tok"},
        password="hunter2",
        url="https://example.com",
    )
    assert not contains_credentials(str(out))
    assert "hunter2" not in str(out)


def test_the_configured_logger_actually_emits() -> None:
    """The processor tests above all pass while the real configuration crashes.

    They did: `add_logger_name` reads `logger.name`, which only a stdlib logger
    has, and it was paired with a PrintLogger. Every log call raised
    AttributeError — including the ones reporting the incident you are trying
    to read about. Nothing above catches that, because nothing above builds the
    chain the process actually runs.
    """
    import io
    import logging

    import structlog

    from engine.logging_config import configure_logging

    buffer = io.StringIO()
    try:
        configure_logging(json_logs=True)
        logging.basicConfig(stream=buffer, format="%(message)s", level=logging.INFO, force=True)
        structlog.get_logger("engine.test").warning(
            "fetch_failed", error=f"ConnectError: {PROXY_URL}", api_key="sk-live-777"
        )
    finally:
        structlog.reset_defaults()
        logging.basicConfig(force=True)

    output = buffer.getvalue()
    assert output, "the configured logger emitted nothing"
    assert "supersecret" not in output
    assert "sk-live-777" not in output
    # Redaction that destroys diagnostics gets switched off by whoever is on call.
    assert "proxy.example.net" in output
    assert '"logger": "engine.test"' in output


# --------------------------------------------------------------------------
# Personal data — a compliance problem rather than a security one
# --------------------------------------------------------------------------


def test_an_email_address_is_masked() -> None:
    """11-compliance.md s2: a named individual's address is personal data.
    s6 requires deletion by domain and URL to service an erasure request, and
    a log aggregator on its own retention schedule is outside that entirely —
    deleting the `contacts` row does not reach it."""
    out = process(event="contact_discovered", email="sarah.jones@example.com")
    assert "sarah.jones" not in str(out)
    assert not contains_email(str(out))


def test_the_domain_survives_masking() -> None:
    """Which company, which directory, which target — that is what makes the
    line useful, and it is not what identifies a person."""
    out = process(event="contact_discovered", email="sarah.jones@example.com")
    assert "example.com" in str(out["email"])


def test_an_email_inside_free_text_is_masked() -> None:
    """The realistic case is not someone logging `email=`. It is an address
    arriving inside an exception message from a library."""
    out = process(event="verify_failed", error="SMTP 550 for bob@example.co.uk")
    assert "bob@" not in str(out)
    assert "example.co.uk" in str(out["error"])


def test_a_domain_only_field_is_not_touched() -> None:
    """`export.py` already logs `email_domain` rather than the address. Masking
    by KEY name would destroy that deliberate pattern — `email_domain` contains
    "email". Matching the email SHAPE instead leaves it alone."""
    out = process(event="contact_missing_source_url", email_domain="example.com")
    assert out["email_domain"] == "example.com"


def test_every_address_in_a_list_is_masked() -> None:
    out = process(event="export", recipients=["a@example.com", "b@example.net"])
    assert not contains_email(str(out))
    assert "example.net" in str(out)


def test_a_credential_and_an_address_in_one_string_are_both_cleaned() -> None:
    out = process(
        event="both",
        error=f"failed for bob@example.com via {PROXY_URL}",
    )
    assert not contains_email(str(out))
    assert not contains_credentials(str(out))
    assert "supersecret" not in str(out)


def test_the_email_detector_does_not_match_its_own_output() -> None:
    """The same trap as `contains_credentials` and `***:***@`: if the masked
    form still matches, every "no personal data in logs" assertion passes for
    the wrong reason."""
    raw = "sarah.jones@example.com"
    assert contains_email(raw)
    assert not contains_email(mask_emails(raw))


def test_ordinary_text_containing_an_at_sign_is_not_mangled() -> None:
    """Over-redaction is its own failure. A handle or a decorator is not an
    address, and mangling them makes logs harder to read for no gain."""
    for text in ("@example mentioned it", "cost is 5 @ £3", "use @property here"):
        assert mask_emails(text) == text


def test_deep_nesting_terminates() -> None:
    """An unbounded walk inside a log processor is a denial of service against
    our own logging."""
    deep: dict[str, object] = {"level": 0}
    node = deep
    for i in range(1, 40):
        child: dict[str, object] = {"level": i}
        node["child"] = child
        node = child
    process(event="deep", context=deep)  # must return rather than recurse away
