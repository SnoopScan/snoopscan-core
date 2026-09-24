"""A fresh `pip install snoopscan` must work against the hosted API.

DEFAULT_BASE_URL used to be `http://localhost:8099` on the theory that the
engine is self-hosted and a published client cannot guess where a user's own
instance runs — true for someone self-hosting this open-core engine, false
for the overwhelming majority who just want the hosted service, whose very
first call failed with a connection error before getting anywhere.

DEFAULT_BASE_URL is computed once at import time from os.environ, so testing
it in-process risks reading whatever SNOOPSCAN_BASE_URL/SNOOP_BASE_URL the
surrounding shell happens to have set — a clean subprocess is the only way to
see the real default.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_default_base_url_is_not_localhost() -> None:
    unset = ("SNOOPSCAN_BASE_URL", "SNOOP_BASE_URL")
    env = {k: v for k, v in os.environ.items() if k not in unset}
    code = "from snoopscan.client import DEFAULT_BASE_URL; print(DEFAULT_BASE_URL)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    url = result.stdout.strip()
    assert "localhost" not in url
    assert "127.0.0.1" not in url
    assert url.startswith("https://")
