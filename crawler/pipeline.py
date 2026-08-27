"""Top-level entry points: a full crawl (`run`) and an offline re-scan of
cached data only (`rescan`).
"""
from __future__ import annotations

import json

from . import config
from .browser_crawler import Crawler
from .extractors import (
    describe_image_metadata,
    find_flags_in_headers,
    find_flags_in_lsb_steganography,
    find_flags_in_text,
    looks_like_image,
)

# per-rendered-page fields that get simply deep-scanned as one text blob
_RENDERED_TEXT_FIELDS = ("serializedDOM", "innerText", "storage", "indexeddb", "console")
_RENDERED_LIST_FIELDS = ("pseudo", "attrs")


def _rescan_cached_bodies(crawler: Crawler, store) -> int:
    count = 0
    for url, body, headers in store.iter_bodies():
        count += 1
        crawler._record(url, find_flags_in_headers(headers), "HTTP response headers (cached)")
        crawler._scan_payload(url, body, "cached response body")
        ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
        if looks_like_image(ctype, url):
            meta = describe_image_metadata(body)
            if meta:
                crawler._record(url, find_flags_in_text(meta), "image metadata (cached)")
            crawler._record(url, find_flags_in_lsb_steganography(body),
                            "image LSB steganography (cached)")
    return count


def _rescan_one_rendered_page(crawler: Crawler, record: dict):
    url = record.get("url", "")
    for field in _RENDERED_TEXT_FIELDS:
        value = record.get(field)
        if not value:
            continue
        crawler._scan_payload(url, value.encode("utf-8", "ignore"), f"cached {field}")
        if field == "innerText":
            crawler._record(url, find_flags_in_text("".join(value.split())),
                            "cached innerText, whitespace-stripped")
    for field in _RENDERED_LIST_FIELDS:
        values = record.get(field) or []
        if values:
            crawler._scan_payload(url, "\n".join(values).encode("utf-8", "ignore"),
                                  f"cached {field}")


def _rescan_cached_rendered_pages(crawler: Crawler, store) -> int:
    count = 0
    for record in store.iter_rendered():
        count += 1
        _rescan_one_rendered_page(crawler, record)
    return count


def rescan(store, start_url: str) -> dict:
    """Re-run every extractor over CACHED data only. No network, no
    browser. Cannot discover new URLs, so this measures extraction
    coverage, not crawl coverage — a full `--resume` run still widens
    discovery. See README "Caching: iterate without re-crawling".
    """
    crawler = Crawler(start_url, config.USERNAME, config.PASSWORD, store=store, resume=False)
    crawler.store = store

    bodies = _rescan_cached_bodies(crawler, store)
    rendered_pages = _rescan_cached_rendered_pages(crawler, store)
    print(f"\nRescanned {bodies} cached bodies and {rendered_pages} "
          f"cached rendered pages — no network used.")

    result = crawler.report()
    result["crawl_complete"] = False
    result["crawl_complete_explanation"] = (
        f"RESCAN ONLY: re-ran extractors over {bodies} cached response bodies "
        f"and {rendered_pages} cached rendered pages. No network requests were "
        f"made, so no new URLs could be discovered — this measures extraction "
        f"coverage, not crawl coverage. Run `python run.py --resume` to widen "
        f"discovery."
    )
    with open("results.json", "w") as fh:
        json.dump(result, fh, indent=2)
    crawler._write_progress_file()
    return result


def run(start_url: str, username: str, password: str, store=None, resume: bool = True) -> dict:
    crawler = Crawler(start_url, username, password, store=store, resume=resume)
    crawler.crawl()
    result = crawler.report()

    print("\n" + "=" * 60)
    print(f"Found {result['count']} / {config.EXPECTED_COUNT} flags:")
    for f in result["flags"]:
        print(f"  {f}")
    print(f"Visited {result['unique_pages_visited']} unique pages")
    if result["capped_sections"] or result["capped_pagination_bases"]:
        print(f"Capped sections (trap-bounded): {result['capped_sections']}")
        print(f"Capped pagination series: {result['capped_pagination_bases']}")
    status = "COMPLETE" if result["crawl_complete"] else "INCOMPLETE"
    print(f"Crawl status: {status} — {result['crawl_complete_explanation']}")
    print("=" * 60)

    with open("results.json", "w") as fh:
        json.dump(result, fh, indent=2)
    print("Full details written to results.json")
    return result
