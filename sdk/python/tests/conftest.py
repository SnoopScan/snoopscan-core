"""No test may ask PyPI whether a newer release exists."""

import pytest


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOOPSCAN_NO_UPDATE_CHECK", "1")
