"""Header-sweep mode: HEAD every known path, unauthenticated AND
authenticated. Custom headers are a property of the nginx location block,
not of a successful response — `X-Provisioning-Note` appears on the
pre-auth 401 for its path, so a HEAD without credentials is enough to
reveal one, and a path we only ever saw as 301/403/404 could still carry a
header we never read.
"""
from __future__ import annotations

import collections
import http.client
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from crawler import config
from crawler.extractors import find_flags_in_headers

from . import audit_shared as shared

STANDARD_HEADERS = {
    "server", "date", "content-type", "content-length", "connection",
    "last-modified", "etag", "accept-ranges", "location", "www-authenticate",
    "content-encoding", "vary", "cache-control", "expires",
}


def head(path: str, send_auth: bool):
    try:
        conn = http.client.HTTPConnection(shared.HOST, timeout=15)
        headers = {"Host": shared.HOST, "User-Agent": config.DIRECT_FETCH_USER_AGENT}
        if send_auth:
            headers["Authorization"] = shared.AUTH
        conn.request("HEAD", path, headers=headers)
        resp = conn.getresponse()
        resp.read()
        result = resp.status, dict(resp.getheaders())
        conn.close()
        return result
    except Exception:
        return None, {}


def _non_standard_headers(hdrs: dict) -> dict:
    return {k: v for k, v in hdrs.items() if k.lower() not in STANDARD_HEADERS}


def _collect_known_paths() -> set[str]:
    """Every path already fetched, plus a few worth checking even though
    they only ever came back as an error page."""
    paths = set()
    for url, _, _ in shared.get_store().iter_bodies():
        p = urllib.parse.urlparse(url)
        if p.netloc == shared.HOST:
            paths.add(p.path + (f"?{p.query}" if p.query else ""))
    if not paths:
        return paths
    # collapse the /report/ generator to a few samples
    report = sorted(p for p in paths if p.startswith("/report/"))
    paths -= set(report)
    paths |= set(report[:5])
    paths |= {"/", "/status/eu-region/", "/static/", "/static/img/"}
    return paths


def _check_path_headers(path: str, custom: dict, found: set):
    """One path's contribution to the sweep. A plain function (not a
    closure) because it's called via pool.map — and being outside
    mode_headers keeps that function's own nesting shallow.
    """
    for send_auth in (False, True):
        status, hdrs = head(path, send_auth)
        if not hdrs:
            continue
        extra = _non_standard_headers(hdrs)
        if not extra:
            continue
        with shared.lock:
            custom[path] = {"status": status, "headers": extra,
                            "authenticated": send_auth}
            for f in find_flags_in_headers(hdrs) - {config.KNOWN_EXAMPLE_FLAG}:
                found.add(f)
                print(f"\n  [PASSWORD] {f}")
                print(f"      {path}  (auth={send_auth}, HTTP {status})")
        return


def _report_header_sweep(custom: dict, found: set):
    print("\n" + "=" * 66)
    print(f"Paths with a NON-STANDARD header: {len(custom)}")
    print("=" * 66)
    names = collections.Counter()
    for path, info in sorted(custom.items()):
        for k, v in info["headers"].items():
            names[k] += 1
            print(f"  {path}")
            print(f"      HTTP {info['status']} "
                  f"(auth={info['authenticated']})  {k}: {v}")
    print(f"\nDistinct custom header names: {dict(names) or 'none'}")
    Path("audit-headers.json").write_text(json.dumps(
        {"custom_headers": custom, "passwords": sorted(found)}, indent=2))
    print("Written to audit-headers.json")


def mode_headers():
    custom: dict[str, dict] = {}
    found: set[str] = set()

    paths = _collect_known_paths()
    if not paths:
        print("! No cached URLs — run `python audit.py census` first.")
        return

    ordered = sorted(paths)
    print(f"HEAD-sweeping {len(ordered)} paths, unauthenticated then "
          f"authenticated...\n")
    with ThreadPoolExecutor(max_workers=shared.WORKERS) as pool:
        list(pool.map(partial(_check_path_headers, custom=custom, found=found), ordered))

    _report_header_sweep(custom, found)
