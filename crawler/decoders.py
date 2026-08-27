"""Recursive, multi-layer decoding of arbitrary response bytes.

A single decode pass per encoding (one base64 attempt, one hex attempt)
misses layered encodings like base64-of-gzip or hex-of-base64. This module
walks *outward* from the raw bytes, applying every transform in
`decoder_transforms`, and re-checks for a flag at each layer up to
MAX_DEPTH. A hash set of already-seen payloads keeps the recursion from
exploding on self-similar data.
"""
from __future__ import annotations

import gzip
import hashlib
import zlib

from .config import FLAG_RE
from .decoder_transforms import (
    brotli,
    t_base64,
    t_embedded_deflate,
    t_hex,
    t_text_escapes,
    t_zip,
    t_zlib_gzip,
)

# Recursion / blowup guards. Depth 3 is enough for realistic layering
# (e.g. base64 -> gzip -> text) while keeping a full crawl fast.
MAX_DEPTH = 3
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

TRANSFORMS = (t_zlib_gzip, t_zip, t_embedded_deflate, t_base64, t_hex, t_text_escapes)


def _flags(data: bytes) -> set[str]:
    if not data:
        return set()
    return {m.decode("ascii") for m in FLAG_RE.findall(data)}


def decompress_content_encoding(body: bytes, content_encoding: str) -> bytes:
    """Undo a Content-Encoding transfer compression.

    urllib does NOT decompress automatically (unlike requests/browsers), so
    a server honouring our `Accept-Encoding: gzip, deflate, br` hands back
    compressed bytes. The flag regex would still fire via the deep decoder,
    but LINK EXTRACTION would silently break — `body.decode('utf-8')` on
    gzip bytes is garbage, so a compressed HTML page would contribute zero
    URLs and a whole subtree could vanish. Returns the original bytes if it
    isn't compressed or can't be decoded.
    """
    enc = (content_encoding or "").lower().strip()
    if not enc or enc == "identity":
        return body
    try:
        if "br" in enc:
            return body if brotli is None else brotli.decompress(body)
        if "gzip" in enc or "x-gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -15)
    except Exception:
        return body
    return body


def find_flags_deep(data: bytes) -> dict[str, set[str]]:
    """Scan `data` for flags at every decoding layer.

    Returns a mapping of "how it was encoded" -> flags found, so
    results.json can report the actual hiding mechanism (e.g.
    "pdf-flate-stream" or "base64(+2) > gzip") rather than just "in the
    bytes somewhere".
    """
    results: dict[str, set[str]] = {}
    seen: set[str] = set()
    # queue of (payload, human-readable provenance chain, depth)
    queue: list[tuple[bytes, str, int]] = [(data, "", 0)]

    while queue:
        payload, chain, depth = queue.pop(0)
        if not payload or len(payload) > MAX_PAYLOAD_BYTES:
            continue
        digest = hashlib.sha1(payload).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)

        found = _flags(payload)
        if found and chain:
            results.setdefault(chain, set()).update(found)

        if depth >= MAX_DEPTH:
            continue
        for transform in TRANSFORMS:
            try:
                candidates = transform(payload)
            except Exception:
                continue
            for label, decoded in candidates:
                next_chain = f"{chain} > {label}" if chain else label
                queue.append((decoded, next_chain, depth + 1))

    return results
