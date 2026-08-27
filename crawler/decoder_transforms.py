"""Individual byte-transform candidates for the deep decoder.

Each transform takes raw bytes and returns a list of (label, decoded_bytes)
candidates. They are deliberately permissive — a transform that doesn't
apply just returns []. `decoders.find_flags_deep` recurses through these to
find flags hidden behind layered, compressed, or escaped encodings:

1. **Compressed payloads.** A PDF stores its text in FlateDecode (zlib)
   streams; a `.gz` asset is gzip; a `.zip` holds deflated members. A
   byte-level regex over the raw file finds nothing, because the flag's
   ASCII never appears in the file — only its compressed form does.
2. **Misaligned base64.** Base64 only decodes correctly at a 4-char
   boundary. Grabbing a longer alphanumeric run and decoding it once
   yields garbage if the flag starts mid-run; trying all four offsets
   fixes it.
3. **Layered / escaped encodings.** base64-of-gzip, hex-of-base64, HTML
   entities, JS string escapes — none of which a one-pass decoder sees.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import gzip
import html
import io
import re
import zipfile
import zlib

try:
    # Optional: only needed for .woff2 fonts (internally brotli-compressed)
    # and `Content-Encoding: br` responses. Everything else works without it.
    import brotli
except ImportError:
    try:
        import brotlicffi as brotli  # type: ignore
    except ImportError:
        brotli = None

MAX_CANDIDATES_PER_LAYER = 60

B64_RUN_RE = re.compile(rb"[A-Za-z0-9+/_-]{16,}={0,2}")
HEX_RUN_RE = re.compile(rb"(?:0[xX])?[0-9a-fA-F]{32,}")
# \x56 / V style escapes, and decimal char-code arrays like
# [86,73,83,...] or String.fromCharCode(86,73,83,...)
JS_ESCAPE_RE = re.compile(rb"(?:\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}){6,}")
CHARCODE_RUN_RE = re.compile(rb"(?:\d{1,3}\s*,\s*){6,}\d{1,3}")


def _try_b64_decode(chunk: bytes) -> bytes | None:
    # Pad up to a 4-char boundary rather than truncating down to one:
    # truncating discards the final group, which loses the flag's closing
    # brace and leaves compressed payloads cut short so they won't inflate.
    chunk = chunk + b"=" * (-len(chunk) % 4)
    try:
        return base64.b64decode(chunk, validate=False)
    except (binascii.Error, ValueError):
        return None


def t_base64(data: bytes) -> list[tuple[str, bytes]]:
    """Base64, trying both alphabets and all four alignment offsets."""
    out = []
    for m in list(B64_RUN_RE.finditer(data))[:MAX_CANDIDATES_PER_LAYER]:
        run = m.group(0).rstrip(b"=")
        variants = {run}
        if b"-" in run or b"_" in run:  # urlsafe -> standard alphabet
            variants.add(run.replace(b"-", b"+").replace(b"_", b"/"))
        for v in variants:
            for offset in range(4):
                chunk = v[offset:]
                if len(chunk) < 16:
                    continue
                dec = _try_b64_decode(chunk)
                if dec:
                    out.append((f"base64(+{offset})", dec))
    return out


def t_hex(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    for m in list(HEX_RUN_RE.finditer(data))[:MAX_CANDIDATES_PER_LAYER]:
        run = m.group(0)
        if run[:2] in (b"0x", b"0X"):
            run = run[2:]
        if len(run) % 2:
            run = run[:-1]
        try:
            out.append(("hex", bytes.fromhex(run.decode("ascii"))))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def t_zlib_gzip(data: bytes) -> list[tuple[str, bytes]]:
    """Whole-payload decompression: gzip, zlib, raw deflate, and brotli."""
    out = []
    try:
        out.append(("gzip", gzip.decompress(data)))
    except Exception:
        pass
    # wbits=31 is gzip-with-header via decompressobj: unlike gzip.decompress
    # it tolerates a truncated or trailing-garbage stream and returns
    # whatever inflated cleanly, which matters when the blob came out of an
    # outer decode that guessed its boundaries.
    for label, wbits in (("gzip-partial", 31), ("zlib", 15), ("raw-deflate", -15)):
        try:
            dec = zlib.decompressobj(wbits).decompress(data)
            if dec:
                out.append((label, dec))
        except Exception:
            pass
    if brotli is not None:
        try:
            dec = brotli.decompress(data)
            if dec:
                out.append(("brotli", dec))
        except Exception:
            pass
    return out


def _inflate_pdf_stream(blob: bytes) -> bytes | None:
    for wbits in (15, -15):
        try:
            dec = zlib.decompress(blob, wbits)
            if dec:
                return dec
        except Exception:
            continue
    return None


def t_embedded_deflate(data: bytes) -> list[tuple[str, bytes]]:
    """Inflate zlib/deflate streams found *inside* a larger container.

    This is what makes PDFs work: a PDF's page text lives in
    `stream ... endstream` blocks that are FlateDecode-compressed, so the
    flag's ASCII is nowhere in the raw file. Also catches zlib blobs
    embedded in other binary formats.
    """
    out = []
    for m in list(re.finditer(rb"stream\r?\n", data))[:MAX_CANDIDATES_PER_LAYER]:
        start = m.end()
        end = data.find(b"endstream", start)
        blob = data[start:end if end != -1 else len(data)]
        dec = _inflate_pdf_stream(blob)
        if dec:
            out.append(("pdf-flate-stream", dec))
    # generic: any zlib header (0x78 0x01/0x9c/0xda) anywhere in the file
    for m in list(re.finditer(rb"\x78[\x01\x9c\xda]", data))[:MAX_CANDIDATES_PER_LAYER]:
        try:
            dec = zlib.decompressobj().decompress(data[m.start():])
            if dec:
                out.append(("embedded-zlib", dec))
        except Exception:
            continue
    return out


def _safe_zip_read(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except Exception:
        return None


def t_zip(data: bytes) -> list[tuple[str, bytes]]:
    """Extract every member of a zip archive (also covers .docx/.xlsx,
    which are just zips of XML)."""
    if not data.startswith(b"PK"):
        return []
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist()[:MAX_CANDIDATES_PER_LAYER]:
                content = _safe_zip_read(zf, name)
                if content is not None:
                    out.append((f"zip:{name}", content))
    except Exception:
        return []
    return out


def _t_wide_text(data: bytes) -> list[tuple[str, bytes]]:
    """UTF-16/32, both endiannesses, plus a null-stripping fallback.

    Every character is interleaved with null bytes, so a byte-level ASCII
    regex sees "V\\x00I\\x00S\\x00..." and matches nothing. This is how EXIF
    UserComment stores text after its "UNICODE\\0" prefix, and it cost a
    real password before this was added. The null-stripping fallback
    handles wide text that isn't cleanly 2-/4-byte aligned (e.g. because a
    binary header sits in front of it, as in EXIF).
    """
    if b"\x00" not in data:
        return []
    out = []
    for enc in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        try:
            decoded = data.decode(enc, errors="ignore")
        except Exception:
            continue
        if decoded:
            out.append((enc, decoded.encode("utf-8", "ignore")))
    out.append(("null-stripped", data.replace(b"\x00", b"")))
    return out


def _t_html_entities(text: str) -> list[tuple[str, bytes]]:
    if "&#" not in text:  # &#86;&#73;... or &#x56;&#x49;...
        return []
    try:
        return [("html-entities", html.unescape(text).encode("utf-8", "ignore"))]
    except Exception:
        return []


def _t_percent_encoding(text: str) -> list[tuple[str, bytes]]:
    if "%" not in text:
        return []
    try:
        from urllib.parse import unquote

        return [("url-decoded", unquote(text).encode("utf-8", "ignore"))]
    except Exception:
        return []


def _t_js_escapes(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    for m in list(JS_ESCAPE_RE.finditer(data))[:MAX_CANDIDATES_PER_LAYER]:
        try:
            dec = codecs.decode(m.group(0).decode("ascii"), "unicode_escape")
            out.append(("js-escape", dec.encode("utf-8", "ignore")))
        except Exception:
            continue
    return out


def _t_char_codes(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    for m in list(CHARCODE_RUN_RE.finditer(data))[:MAX_CANDIDATES_PER_LAYER]:
        try:
            nums = [int(n) for n in m.group(0).split(b",")]
        except Exception:
            continue
        if all(0 <= n < 256 for n in nums):
            out.append(("char-codes", bytes(nums)))
    return out


def _t_rot13_and_reverse(data: bytes, text: str) -> list[tuple[str, bytes]]:
    # cheap, and catch trivial obfuscation; skip once the flag is already
    # plainly visible, since these two are the least likely of the bunch
    if "VISUALPING" in text:
        return []
    out = [("reversed", data[::-1])]
    try:
        out.append(("rot13", codecs.encode(text, "rot13").encode("utf-8", "ignore")))
    except Exception:
        pass
    return out


def t_text_escapes(data: bytes) -> list[tuple[str, bytes]]:
    """HTML entities, JS string escapes, char-code arrays, percent-encoding,
    wide-character encodings, rot13, and whole-payload reversal.

    All of these render/eval to the flag in a browser (or in an image
    viewer's metadata pane) while the raw bytes never contain the literal
    ASCII string.
    """
    out = _t_wide_text(data)
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return out

    out += _t_html_entities(text)
    out += _t_percent_encoding(text)
    out += _t_js_escapes(data)
    out += _t_char_codes(data)
    out += _t_rot13_and_reverse(data, text)
    return out
