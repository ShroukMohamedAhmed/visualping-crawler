"""Shared plumbing for the audit tools: fetching, deep-scanning, and the
pagination-series handling that all three modes (census/headers/images)
rely on.
"""
from __future__ import annotations

import base64
import threading
import urllib.error
import urllib.parse
import urllib.request

from crawler import config
from crawler.decoders import decompress_content_encoding, find_flags_deep
from crawler.extractors import (
    describe_probable_text_image,
    find_bare_hex16_candidates,
    find_flags_in_bytes,
    find_flags_in_headers,
    looks_like_image,
)
from crawler.store import Store

HOST = urllib.parse.urlparse(config.START_URL).netloc
AUTH = "Basic " + base64.b64encode(
    f"{config.USERNAME}:{config.PASSWORD}".encode()).decode()

WORKERS = 8
MAX_URLS = 6000
# A `?page=N` chain reveals page N+1 only once page N is fetched, so
# following it is inherently serial — and if the series is a generator it
# never ends. Sample to this depth and record the bound rather than
# chasing it.
PAGINATION_SAMPLE = 300
PAGINATION_PARAMS = ("page", "p", "offset", "start")

# Re-entrant: sweep() holds this while calling helpers that also take it.
# A plain Lock deadlocks silently and looks exactly like the crawl hanging.
lock = threading.RLock()

_store: Store | None = None


def get_store() -> Store:
    """Lazily open the cache, so importing this module has no side effects."""
    global _store
    if _store is None:
        _store = Store()
    return _store


# --- shared mutable state, populated across a run of any mode --------------

seen: set[str] = set()
records: list[dict] = []
flags: dict[str, set[str]] = {}
body_hashes: dict[str, str] = {}
bounded_series: dict[str, int] = {}
expanded_series: set[str] = set()
hex_candidates: dict[str, set[str]] = {}
text_images: dict[str, str] = {}


# --- fetching ---------------------------------------------------------------

def fetch(url: str):
    req = urllib.request.Request(url, headers={
        "Authorization": AUTH,
        "User-Agent": config.DIRECT_FETCH_USER_AGENT,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "*/*",
    })

    def done(raw, headers):
        # urllib does not decompress; without this, link extraction from a
        # gzipped page silently yields nothing.
        return decompress_content_encoding(
            raw, headers.get("Content-Encoding", "")), headers

    try:
        with urllib.request.urlopen(req, timeout=config.DIRECT_FETCH_TIMEOUT_S) as r:
            body, headers = done(r.read(config.MAX_DIRECT_FETCH_BYTES), dict(r.headers))
            return r.status, body, headers
    except urllib.error.HTTPError as e:
        try:
            body, headers = done(e.read(config.MAX_DIRECT_FETCH_BYTES), dict(e.headers))
            return e.code, body, headers
        except Exception:
            return e.code, b"", {}
    except Exception as exc:
        return None, str(exc).encode(), {}


def scan(url: str, body: bytes, headers: dict) -> set[str]:
    """Deep-scan one response and record any leads that need a human."""
    found = set(find_flags_in_bytes(body)) | set(find_flags_in_headers(headers))
    for hits in find_flags_deep(body).values():
        found |= hits
    real = found - {config.KNOWN_EXAMPLE_FLAG}
    if real:
        _record_new_passwords(url, real)

    bare = find_bare_hex16_candidates(body)
    if bare:
        with lock:
            hex_candidates.setdefault(url, set()).update(bare)

    if looks_like_image(headers.get("Content-Type", ""), url):
        hint = describe_probable_text_image(body)
        if hint:
            with lock:
                text_images[url] = hint
            print(f"  [LOOK] {url}\n      {hint}")
    return real


def _record_new_passwords(url: str, real: set[str]):
    with lock:
        already = set().union(*flags.values()) if flags else set()
        flags.setdefault(url, set()).update(real)
        for f in sorted(real - already):
            print(f"  [PASSWORD] {f}  @ {url}")


# --- pagination handling -----------------------------------------------

def pagination_depth(url: str):
    parsed = urllib.parse.urlparse(url)
    for key, value in urllib.parse.parse_qsl(parsed.query):
        if key.lower() in PAGINATION_PARAMS:
            try:
                return parsed.path, int(value)
            except (TypeError, ValueError):
                continue
    return None, None


def _set_page(url: str, n: int) -> str:
    parsed = urllib.parse.urlparse(url)
    params = [(k, str(n) if k.lower() in PAGINATION_PARAMS else v)
              for k, v in urllib.parse.parse_qsl(parsed.query)]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))


def expand_pagination_series(url: str) -> set[str]:
    """Materialise a whole `?page=1..N` range at once.

    Not URL guessing: every one of these pages is reachable by clicking
    "next" repeatedly. We just don't pay for 300 sequential round trips.
    """
    base, depth = pagination_depth(url)
    if depth is None:
        return set()
    with lock:
        if base in expanded_series:
            return set()
        expanded_series.add(base)
    print(f"  [expand] {base} is a ?page=N series — fetching 1..{PAGINATION_SAMPLE} "
          f"in parallel instead of one at a time")
    return {_set_page(url, n) for n in range(1, PAGINATION_SAMPLE + 1)}


def within_pagination_bound(url: str) -> bool:
    """Callers hold `lock`; relies on it being an RLock."""
    base, depth = pagination_depth(url)
    if depth is None or depth <= PAGINATION_SAMPLE:
        return True
    with lock:
        if depth > bounded_series.get(base, 0):
            bounded_series[base] = depth
    return False
