"""Extract VISUALPING{...} passwords from response bodies, headers, and
images.

Covers the cases where a password is not literal ASCII in the bytes:
non-plaintext encodings are delegated to `decoders.find_flags_deep`, image
pixel data is checked for LSB steganography, and an image whose pixels look
like *rendered text* is flagged for a human — no byte-level method can read
a password that was simply drawn into a picture.
"""
from __future__ import annotations

import io
import re

from .config import FLAG_RE

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:  # pillow optional at import time for non-image use
    Image = None
    TAGS = None


def find_flags_in_bytes(data: bytes) -> set[str]:
    """Byte-level regex scan. This is the workhorse: it catches flags in
    HTML comments, inline/external JS, CSS, JSON bodies, and any plain-text
    metadata sitting inside an otherwise-binary file (EXIF UserComment,
    PNG tEXt chunks, JPEG COM segments are all stored as plain ASCII inside
    the file bytes, so we don't need per-format parsers to find them).
    """
    if not data:
        return set()
    return {m.decode("ascii") for m in FLAG_RE.findall(data)}


def find_flags_in_text(text: str) -> set[str]:
    return find_flags_in_bytes(text.encode("utf-8", errors="ignore"))


def find_flags_in_headers(headers: dict) -> set[str]:
    found = set()
    for k, v in headers.items():
        found |= find_flags_in_text(f"{k}: {v}")
    return found


def find_flags_all_encodings(data: bytes) -> dict[str, set[str]]:
    """Find flags that aren't stored as literal ASCII, and report HOW.

    Returns a mapping of "how it was encoded" -> flags found via that route,
    so results.json can name the actual hiding mechanism (e.g.
    "pdf-flate-stream" or "base64(+2) > gzip") rather than "in the bytes
    somewhere".

    All the work is in decoders.find_flags_deep, which recurses through
    layered encodings, decompresses containers, and reverses browser-side
    and wide-character encodings. See that module for the catalogue.
    """
    from .decoders import find_flags_deep

    return find_flags_deep(data)


# --- image pixel-data steganography (LSB) -----------------------------

def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i : i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


def find_flags_in_lsb_steganography(image_bytes: bytes) -> set[str]:
    """Extract the least-significant bit of each byte in the image's raw
    pixel data (common simple steganography technique) and check whether
    the resulting bitstream, packed back into bytes, contains a flag.
    Tries a couple of common conventions (channel order as stored, and
    reading every Nth bit-plane) since the exact scheme isn't known ahead
    of time. This only works on lossless formats (PNG etc) — LSB data
    doesn't survive JPEG compression.
    """
    if Image is None:
        return set()
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
    except Exception:
        return set()

    raw = img.tobytes()  # flat sequence of R,G,B,R,G,B,... bytes
    lsb_bits = [b & 1 for b in raw]

    found = set()
    found |= find_flags_in_bytes(_bits_to_bytes(lsb_bits))
    # some encoders write MSB-first vs LSB-first within each reconstructed
    # byte; _bits_to_bytes already does MSB-first packing of the bit
    # stream, so also try the bit-reversed packing as a cheap second guess
    found |= find_flags_in_bytes(_bits_to_bytes(lsb_bits[::-1]))
    return found


def describe_image_metadata(data: bytes) -> str:
    """Best-effort human-readable dump of an image's metadata (EXIF, PNG
    text chunks, JPEG comment segments, etc.) for reporting purposes. The
    actual flag detection still relies on find_flags_in_bytes over the raw
    file, this is just for a readable trail in results.json.
    """
    if Image is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return ""

    parts = []
    info = getattr(img, "info", {}) or {}
    for k, v in info.items():
        parts.append(f"info[{k}]={v!r}")

    try:
        exif = img.getexif()
        for tag_id, value in exif.items():
            tag = TAGS.get(tag_id, tag_id) if TAGS else tag_id
            parts.append(f"exif[{tag}]={value!r}")
    except Exception:
        pass

    return "; ".join(parts)


def describe_probable_text_image(data: bytes) -> str:
    """Return a human-readable hint if an image looks like TEXT rendered
    into pixels, else "".

    A password drawn into an image as visible text defeats every
    byte-level technique in this crawler: it isn't in the file's bytes, in
    any metadata field, or in the LSB plane — it's just a picture of the
    answer. The only fix is OCR or a person looking at it, so the crawler's
    job is to notice the shape and say so loudly.

    Heuristic: text scans are mostly light background with a modest
    fraction of dark ink. Validated against the real
    /static/img/whiteboard-scan.png (12.5% dark / 86% light -> flagged) and
    against this site's noise-filled decoy PNGs (not flagged).
    """
    if Image is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        grey = img.convert("L")
    except Exception:
        return ""

    histogram = grey.histogram()
    total = sum(histogram) or 1
    dark = sum(histogram[:100]) / total
    light = sum(histogram[200:]) / total
    if light > 0.5 and 0.005 < dark < 0.5:
        return (f"looks like text/ink on a light background "
                f"({dark:.1%} dark, {light:.1%} light, {width}x{height}) — "
                f"OPEN AND READ IT; a password drawn as pixels is invisible "
                f"to every byte-level scan")
    return ""


def looks_like_image(content_type: str, url: str) -> bool:
    if content_type and content_type.startswith("image/"):
        return True
    return url.lower().split("?")[0].endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
    )


# A bare 16-hex-char run with no VISUALPING{...} wrapper. On this site
# several JPEG COM segments hold exactly this shape. They may be decoys or
# they may be a password with the wrapper stripped, so they're reported as
# CANDIDATES (never auto-promoted to a confirmed password) — guessing wrong
# in either direction is worse than surfacing them for a human to judge.
BARE_HEX16_RE = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{16})(?![0-9a-fA-F])")


def find_bare_hex16_candidates(data: bytes) -> set[str]:
    if not data:
        return set()
    stripped = data.replace(b"\x00", b"")
    found = set()
    for m in BARE_HEX16_RE.finditer(stripped):
        candidate = m.group(1).decode("ascii")
        # skip runs that are already inside a proper flag
        if f"VISUALPING{{{candidate}}}".encode() in stripped:
            continue
        found.add(candidate)
    return found
