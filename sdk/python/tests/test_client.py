def test_extract_takes_a_template_instead_of_a_schema() -> None:
    """The new parameter. Without it `extract` could not reach templates at
    all: `schema` was required, and sending both is a 400 by design."""
    from snoopscan.client import SnoopScan

    sent: dict = {}

    class FakePost(SnoopScan):
        def _post(self, path, body):  # type: ignore[override]
            sent.update({"path": path, "body": body})
            return []

    client = FakePost(api_key="sk_test", base_url="https://api.example.test")
    client.extract(["https://shop.test/p"], template="product")

    assert sent["path"] == "/v1/extract"
    assert sent["body"]["template"] == "product"
    assert "schema" not in sent["body"], "a template IS the schema"


def test_extract_still_takes_a_schema_the_way_it_always_did() -> None:
    from snoopscan.client import SnoopScan

    sent: dict = {}

    class FakePost(SnoopScan):
        def _post(self, path, body):  # type: ignore[override]
            sent.update(body)
            return []

    client = FakePost(api_key="sk_test", base_url="https://api.example.test")
    mine = {"type": "object", "properties": {"title": {"type": "string"}}}
    client.extract(["https://a.test/"], mine, prompt="the product")

    assert sent["schema"] == mine
    assert sent["prompt"] == "the product"
    assert "template" not in sent


def test_both_or_neither_is_refused_before_the_request_leaves() -> None:
    """The engine rejects both, and neither is a 400 too. Catching it here
    costs the caller a round trip and a clearer message."""
    import pytest

    from snoopscan.client import SnoopScan

    client = SnoopScan(api_key="sk_test", base_url="https://api.example.test")
    with pytest.raises(ValueError, match="either"):
        client.extract(["https://a.test/"], {"type": "object"}, template="product")
    with pytest.raises(ValueError, match="either"):
        client.extract(["https://a.test/"])
