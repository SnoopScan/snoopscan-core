"""Is this response a FILE no rung can make a page of?

Measured 25 Sep 2026: a scrape of an Ubuntu mirror's `.orig.tar.gz` climbed
http -> browser -> browser -> stealth, and the stealth rung pulled the archive
through a residential exit — 19 MB billed to the caller, ~5 MB on the proxy
invoice, for a page that never existed. The server labelled the archive
`text/html` with `Content-Encoding: x-gzip`, so the content-type check that
already refuses an honestly-labelled image never saw it.

So the answer comes from the BYTES — a file's magic number is the one label a
misconfigured server cannot get wrong — and, before anything is fetched, from
the URL: a path ending in an archive or video extension is capped to the plain
rungs, where the first chunk decides it and the download stops.

A refusal here is TERMINAL (FetchResult.refused): nothing above tier 1 renders
a tarball, and climbing is paying again for the same file.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# (offset, magic, label). Leading bytes only, bounded to the first 512.
_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x1f\x8b", "gzip"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (257, b"ustar", "tar"),
    (0, b"!<arch>\n", "deb"),
    (0, b"\xed\xab\xee\xdb", "rpm"),
    (0, b"\x7fELF", "executable"),
    (0, b"\xcf\xfa\xed\xfe", "executable"),
    (0, b"\xca\xfe\xba\xbe", "executable"),
    (0, b"\x00asm", "wasm"),
    (0, b"SQLite format 3\x00", "sqlite"),
    (4, b"ftyp", "video"),
    (0, b"\x1a\x45\xdf\xa3", "video"),
    (0, b"OggS", "audio"),
    (0, b"fLaC", "audio"),
    (0, b"ID3", "audio"),
    (0, b"\x89PNG\r\n\x1a\n", "image"),
    (0, b"\xff\xd8\xff", "image"),
    (0, b"GIF87a", "image"),
    (0, b"GIF89a", "image"),
)

# Paths that name a download rather than a page. Deliberately not .pdf or the
# office formats: those are documents the scrape path parses.
BINARY_EXTENSIONS = frozenset(
    {
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar",
        ".iso", ".img", ".dmg", ".exe", ".msi", ".deb", ".rpm", ".apk", ".jar",
        ".whl", ".bin",
        ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm",
        ".mp3", ".wav", ".flac", ".m4a", ".ogg",
    }
)  # fmt: skip

# Documents the scrape path reads. A .docx IS a zip; it must not be refused.
_DOCUMENT_EXTENSIONS = (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".epub")


def sniff(body: bytes | None) -> str | None:
    """The kind of file these leading bytes are, or None for anything else."""
    if not body:
        return None
    head = body[:512]
    for offset, magic, label in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return label
    # bzip2 is "BZh" plus a block-size digit plus the block magic — the three
    # letters alone could open a text file.
    if head[:3] == b"BZh" and head[3:4].isdigit() and head[4:10] == b"\x31\x41\x59\x26\x53\x59":
        return "bzip2"
    if head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI ", b"WEBP"):
        return "image" if head[8:12] == b"WEBP" else "media"
    return None


def is_document(url: str, content_type: str | None) -> bool:
    """A file the scrape path parses (PDF, office), which a zip magic must not refuse."""
    path = urlsplit(url or "").path.lower()
    if path.endswith((".pdf", *_DOCUMENT_EXTENSIONS)):
        return True
    ct = (content_type or "").lower()
    return "pdf" in ct or "officedocument" in ct or "opendocument" in ct


def refused_kind(url: str, content_type: str | None, body: bytes | None) -> str | None:
    """The binary kind to refuse this response for, or None to let it through."""
    kind = sniff(body)
    if kind is None or is_document(url, content_type):
        return None
    return kind


def names_a_download(url: str) -> bool:
    """Does this URL's path end in an archive, installer or media extension?"""
    path = urlsplit(url or "").path.lower()
    dot = path.rfind(".")
    return dot > path.rfind("/") and path[dot:] in BINARY_EXTENSIONS
