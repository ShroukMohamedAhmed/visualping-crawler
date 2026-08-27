"""Census mode: browser-free threaded sweep of the whole site. Reports a
Content-Type census, unusual resources, and characterises the
`/report/?page=N` generator.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from crawler import config
from crawler.link_discovery import (
    extract_css_urls,
    extract_js_urls,
    extract_resource_urls,
    extract_sourcemap_urls,
    same_origin,
)

from . import audit_shared as shared

COMMON_TYPES = (
    "text/html", "text/css", "application/javascript", "text/javascript",
    "image/png", "image/jpeg", "image/svg+xml", "image/gif", "image/webp",
    "image/x-icon", "image/vnd.microsoft.icon",
)


def process(url: str) -> set[str]:
    """Fetch one URL, record it, return newly discovered same-origin URLs."""
    status, body, headers = shared.fetch(url)
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    digest = hashlib.sha1(body).hexdigest()

    shared.get_store().save_body(url, status, headers, body)
    with shared.lock:
        shared.records.append({
            "url": url, "status": status, "content_type": ctype,
            "length": len(body), "sha1": digest[:12],
            "content_disposition": headers.get("Content-Disposition", ""),
        })
        first_seen = shared.body_hashes.setdefault(digest, url)
    if first_seen != url:
        return set()  # byte-identical to something already scanned

    shared.scan(url, body, headers)
    return _extract_links(url, body, ctype)


def _extract_links(url: str, body: bytes, ctype: str) -> set[str]:
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        return set()

    found: set[str] = set()
    lowered = url.lower().split("?")[0]
    if "html" in ctype or "xml" in ctype:
        found |= extract_resource_urls(text, url)
    if "css" in ctype or lowered.endswith(".css"):
        found |= extract_css_urls(text, url)
    if ("javascript" in ctype or "json" in ctype
            or lowered.endswith((".js", ".json", ".map"))):
        found |= extract_js_urls(text, url)
    found |= extract_sourcemap_urls(text, url)
    return {u.split("#")[0] for u in found if same_origin(u, shared.HOST)}


def sweep(seeds: list[str]):
    """Continuously-fed BFS.

    A wave-synchronised BFS collapses to one request at a time whenever
    the frontier is a chain. This keeps the pool saturated instead:
    results are consumed as they complete and new URLs submitted
    immediately.
    """
    queue = collections.deque()
    for url in seeds:
        if url not in shared.seen:
            shared.seen.add(url)
            queue.append(url)

    pending: set = set()
    last_report = 0
    with ThreadPoolExecutor(max_workers=shared.WORKERS) as pool:
        while queue or pending:
            while (queue and len(pending) < shared.WORKERS * 4
                   and len(shared.seen) < shared.MAX_URLS):
                pending.add(pool.submit(process, queue.popleft()))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for url in _fresh_urls_from(done):
                queue.append(url)
            last_report = _maybe_report_progress(queue, pending, last_report)

    _report_bounded_series()


def _fresh_urls_from(done_futures) -> list[str]:
    fresh: list[str] = []
    for future in done_futures:
        try:
            found = future.result() or set()
        except Exception:
            continue
        for u in list(found):
            found |= shared.expand_pagination_series(u)
        with shared.lock:
            new = {u for u in found
                   if u not in shared.seen and shared.within_pagination_bound(u)}
            shared.seen.update(new)
        fresh.extend(sorted(new))
    return fresh


def _maybe_report_progress(queue, pending, last_report: int) -> int:
    if len(shared.records) - last_report < 200:
        return last_report
    print(f"  [sweep] {len(shared.records)} fetched, {len(shared.seen)} known, "
          f"{len(queue)} queued, {len(pending)} in flight")
    return len(shared.records)


def _report_bounded_series():
    if not shared.bounded_series:
        return
    print(f"\n  [bound] pagination sampled to page={shared.PAGINATION_SAMPLE} "
          f"rather than walked to the end:")
    for base, deepest in sorted(shared.bounded_series.items()):
        print(f"    {base} (saw a link as deep as page={deepest})")


def characterise_series(limit: int = shared.PAGINATION_SAMPLE):
    """Is /report/?page=N real content or a generator?

    Each body is normalised (digit runs -> `#`) and hashed. Many pages
    reducing to one shape means a template with a different number; many
    distinct shapes that never terminate means a content generator.
    """
    print(f"\nCharacterising /report/?page=N (sampling up to {limit})...")
    base = urllib.parse.urljoin(config.START_URL, "/report/")
    bodies: set[str] = set()
    shapes: set[str] = set()
    ended_at = None
    walked = 0

    for n in range(1, limit + 1):
        walked = n
        url = f"{base}?page={n}"
        status, body, headers = shared.fetch(url)
        bodies.add(hashlib.sha1(body).hexdigest())
        shapes.add(hashlib.sha1(re.sub(rb"\d+", b"#", body)).hexdigest())
        shared.scan(url, body, headers)
        if f"page={n + 1}" not in body.decode("utf-8", errors="replace"):
            ended_at = n
            break
        if n % 100 == 0:
            print(f"  ...page={n}: {len(bodies)} distinct bodies, "
                  f"{len(shapes)} distinct shapes")

    verdict = _series_verdict(ended_at, walked, len(shapes))
    print(f"  {verdict}")
    return {"pages_walked": walked, "ended_at": ended_at,
            "distinct_bodies": len(bodies), "distinct_shapes": len(shapes),
            "verdict": verdict}


def _series_verdict(ended_at, walked: int, shape_count: int) -> str:
    if ended_at:
        return f"finite: series ends at page={ended_at}"
    if shape_count <= 2:
        return f"GENERATOR: {walked} pages sampled, all reduce to {shape_count} template shape(s)"
    return (f"GENERATOR (unbounded, varying content): {walked} pages sampled, "
            f"{shape_count} distinct shapes, never terminates")


def mode_census():
    print(f"Sweeping {config.START_URL} with {shared.WORKERS} workers...\n")
    sweep([config.START_URL])
    series = characterise_series()
    _print_census_report(series)
    _write_census_json(series)


def _print_census_report(series: dict):
    census = collections.Counter(r["content_type"] or "(none)" for r in shared.records)
    print("\n" + "=" * 66)
    print("Content-Type census")
    print("=" * 66)
    for ctype, count in census.most_common():
        print(f"  {count:6d}  {ctype}")

    unusual = [r for r in shared.records
               if r["content_type"] and r["content_type"] not in COMMON_TYPES]
    print("\n" + "=" * 66)
    print(f"Unusual resources ({len(unusual)})")
    print("=" * 66)
    for r in sorted(unusual, key=lambda r: r["content_type"])[:80]:
        disp = "  [attachment]" if r["content_disposition"] else ""
        print(f"  {r['content_type']:26} {r['length']:8d}  {r['url']}{disp}")
    if not unusual:
        print("  (none — the site serves only html/js/css/png/jpg)")

    statuses = collections.Counter(r["status"] for r in shared.records)
    print(f"\nStatus codes: {dict(statuses)}")
    print(f"URLs fetched: {len(shared.records)}   distinct bodies: {len(shared.body_hashes)}")

    if shared.text_images:
        print("\n" + "=" * 66)
        print("Images that may BE the password (open and look at them)")
        print("=" * 66)
        for url, hint in shared.text_images.items():
            print(f"  {url}\n      {hint}")

    if shared.hex_candidates:
        print("\n" + "=" * 66)
        print("Bare 16-hex candidates (no VISUALPING{} wrapper — NOT counted)")
        print("=" * 66)
        for url, cands in sorted(shared.hex_candidates.items()):
            print(f"  {url}: {sorted(cands)}")

    found = sorted(set().union(*shared.flags.values())) if shared.flags else []
    print("\n" + "=" * 66)
    print(f"Passwords found by this sweep: {len(found)}")
    for f in found:
        print(f"  {f}")
    print("=" * 66)


def _write_census_json(series: dict):
    census = collections.Counter(r["content_type"] or "(none)" for r in shared.records)
    unusual = [r for r in shared.records
               if r["content_type"] and r["content_type"] not in COMMON_TYPES]
    statuses = collections.Counter(r["status"] for r in shared.records)
    found = sorted(set().union(*shared.flags.values())) if shared.flags else []

    out = {
        "census": dict(census),
        "unusual": unusual,
        "statuses": {str(k): v for k, v in statuses.items()},
        "report_series": series,
        "bounded_pagination_series": shared.bounded_series,
        "passwords": found,
        "passwords_by_url": {u: sorted(f) for u, f in shared.flags.items()},
        "bare_hex16_candidates": {u: sorted(c) for u, c in shared.hex_candidates.items()},
        "images_that_look_like_text": shared.text_images,
        "records": shared.records,
    }
    Path("audit-census.json").write_text(json.dumps(out, indent=2))
    print("Written to audit-census.json")
    print(f"Cached {shared.get_store().stats()['cached_bodies']} bodies to "
          f"{shared.get_store().root}/ — `python run.py --rescan` re-scans "
          f"them offline with any future decoder.")
