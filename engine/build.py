"""What code this process is actually running.

`__version__` answers "which release is this", which is a legal statement and
does not change between commits. This answers a different question — "is the
worker running the SAME code as the API" — and it changes whenever any line
does.

It exists because of a whole afternoon lost on 9 Sep 2026. The API was
restarted after every change; the worker daemon was not. It kept an older
`engine.core.models` in memory, so `credits.key_for` read a field that build
of `Cost` did not have, and EVERY crawl and EVERY batch died on its first
cached page with

    AttributeError: 'Cost' object has no attribute 'cache_own'

reported to the caller as `INTERNAL`. Nothing anywhere compared the two, so
the async half of the API was completely broken and the synchronous half was
perfect — which is exactly the shape that makes someone conclude the engine is
flaky rather than that a daemon is stale.

Content, not mtime: a checkout, a container rebuild and a `git stash pop` all
move timestamps without changing behaviour, and an rsync can leave the two
processes agreeing on a lie.
"""

from __future__ import annotations

import functools
import hashlib
import pathlib

# Enough to be unambiguous in a log line; the whole digest reads as noise.
_LENGTH = 12


@functools.lru_cache(maxsize=1)
def build_id() -> str:
    """A short digest of every .py file in the engine package.

    Cached: computed once per process, at first use rather than at import, so
    a tool that only wants a version does not pay for it.
    """
    root = pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            # An unreadable file is still a fact about this build.
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:_LENGTH]


__all__ = ["build_id"]
