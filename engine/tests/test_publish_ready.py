"""The pre-publication gate must be capable of failing.

Publishing is one-way: a secret pushed to a public repository is compromised
the moment it lands, and rewriting history does not help once it has been
cloned or indexed. So the gate's value is entirely in whether it CATCHES
something, and a check that silently matches nothing looks identical to a clean
repository.

These plant each class of problem and assert it is found.
"""

from __future__ import annotations

from pathlib import Path

from tools.check_publish_ready import (
    check_env_example_has_no_real_values,
    check_no_placeholders,
    check_no_real_addresses,
)

# A fixture written too short to be a real key. Assembled at run time so the
# literal never sits in the source: public-repo secret scanners match on the
# prefix and shape, and would block or report a push over a key that is fake.
SHORT_FIXTURE_KEY = "sk_" + "live_" + "51Qb7tmonV8cuP3uIfqyWZA93WHxCN"


def write(tmp_path: Path, name: str, body: str) -> list[Path]:
    path = tmp_path / name
    path.write_text(body)
    return [path]


# --------------------------------------------------------------------------
# C3 — a real individual's address must never be committed
# --------------------------------------------------------------------------


def test_a_scraped_looking_address_is_caught(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The case the check exists for: a real person at a real company, which is
    what a harvested address looks like."""
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    files = write(tmp_path, "leak.py", "OWNER_CONTACT = 'j.smith@realcorp.co.uk'\n")
    problems = check_no_real_addresses(files)
    assert problems, "a real-looking address was not caught"
    assert "j.smith@realcorp.co.uk" in problems[0]


def test_reserved_domains_are_allowed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """RFC 2606 reserves these for documentation. Flagging them would make the
    check cry wolf on every fixture in the suite, and a check nobody believes
    gets skipped."""
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    body = "\n".join(
        [
            "a@example.com",
            "b@example.org",
            "c@sub.example.co.uk",
            "d@thing.invalid",
            "e@acmeworks-fixture.io",
        ]
    )
    assert check_no_real_addresses(write(tmp_path, "fixtures.py", body)) == []


def test_a_generic_local_part_at_a_real_provider_is_allowed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The freemail classifier cannot be tested without naming real providers,
    and `someone@gmail.com` identifies nobody."""
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    body = "someone@gmail.com\nfounder@outlook.com\n"
    assert check_no_real_addresses(write(tmp_path, "classify.py", body)) == []


def test_a_named_person_at_a_freemail_provider_is_still_caught(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A generic local part is the exemption, not the domain. `gmail.com` does
    not make an address safe."""
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    problems = check_no_real_addresses(write(tmp_path, "leak.py", "r.patel1987@gmail.com\n"))
    assert problems, "a named individual at a freemail provider was not caught"


# --------------------------------------------------------------------------
# Placeholders — a funnel with a broken link is worse than no funnel
# --------------------------------------------------------------------------


def test_an_unfilled_placeholder_is_caught(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    files = write(tmp_path, "conf.toml", 'Source = "https://github.com/OWNER/scraping-engine"\n')
    assert check_no_placeholders(files)


def test_a_filled_url_passes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    files = write(tmp_path, "conf.toml", 'Source = "https://github.com/real-org/engine"\n')
    assert check_no_placeholders(files) == []


# --------------------------------------------------------------------------
# .env.example — the one env file that IS published
# --------------------------------------------------------------------------


def test_a_real_looking_secret_in_env_example_is_caught(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("ENGINE_PROXY_PASSWORD=hV8s2LqnZ4rTbW91xQ\n")
    assert check_env_example_has_no_real_values()


def test_templates_and_reserved_domains_pass(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A `{password}` template is a placeholder by construction, and a user
    agent naming example.invalid is documentation."""
    import tools.check_publish_ready as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "ENGINE_PROXY_PASSWORD_TEMPLATE={password}_country-{country}\n"
        "ENGINE_USER_AGENT=SnoopScan/0.1 (+https://example.invalid/bot-info)\n"
        "ENGINE_PROXY_PASSWORD=\n"
    )
    assert check_env_example_has_no_real_values() == []


def test_a_real_credential_in_env_example_is_still_caught(tmp_path, monkeypatch) -> None:
    """The carve-outs must not have opened a hole.

    Two were added because the rule "long value = secret" was wrong twice: a
    comma-separated provider ladder, and our own published User-Agent. Neither
    is opaque. An actual key still is, and still has to fail.
    """
    from tools import check_publish_ready as gate

    (tmp_path / ".env.example").write_text(
        "\n".join(
            [
                "ENGINE_SEARCH_PROVIDERS=searxng,duckduckgo",
                "ENGINE_USER_AGENT=SnoopScan (+https://snoopscan.com/bot)",
                "ENGINE_DATABASE_URL=postgresql://localhost:5432/scraping_engine",
                "ENGINE_STRIPE_KEY=" + SHORT_FIXTURE_KEY,
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    problems = gate.check_env_example_has_no_real_values()

    assert len(problems) == 1, problems
    assert "ENGINE_STRIPE_KEY" in problems[0] or "sk_live" in problems[0], problems


# --------------------------------------------------------------------------
# Telling a real key from our own fixtures
# --------------------------------------------------------------------------


def test_a_real_shaped_key_is_caught() -> None:
    from tools.check_publish_ready import looks_like_a_real_key

    assert looks_like_a_real_key("sk_live_" + "a" * 100) == "Stripe live secret key"
    assert looks_like_a_real_key("ghp_" + "b" * 36) == "GitHub personal access token"
    assert looks_like_a_real_key("AKIA" + "C" * 16) == "AWS access key id"


def test_our_own_fixtures_are_not_reported_as_leaks() -> None:
    """The 38-character `sk_live_...` in this very file is a stand-in, written
    short precisely so it cannot be used. Reported as a leak it sends whoever
    scans this repo hunting a secret that was never there — and towards
    deleting the repository to purge it."""
    from tools.check_publish_ready import looks_like_a_real_key

    assert looks_like_a_real_key(SHORT_FIXTURE_KEY) is None


def test_a_publishable_key_is_not_a_secret() -> None:
    """`pk_live_` keys are designed to sit in public web pages. Ours arrive
    inside captured fixtures of OTHER people's pages, which is exactly where
    they belong."""
    from tools.check_publish_ready import looks_like_a_real_key

    assert looks_like_a_real_key("pk_live_" + "d" * 99) is None


def test_the_check_reads_files_and_names_the_provider() -> None:
    import tempfile
    from pathlib import Path as P

    from tools.check_publish_ready import check_no_real_keys

    with tempfile.TemporaryDirectory() as tmp:
        leaky = P(tmp) / "config.py"
        leaky.write_text('KEY = "sk_live_' + "z" * 100 + '"\n')
        problems = check_no_real_keys([leaky])
        assert problems and "Stripe live secret key" in problems[0]

        clean = P(tmp) / "fine.py"
        clean.write_text(f'KEY = "{SHORT_FIXTURE_KEY}"\n')
        assert check_no_real_keys([clean]) == []
