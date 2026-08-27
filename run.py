#!/usr/bin/env python3
"""Entrypoint for the Visualping crawler challenge.

Usage:
    python run.py                # resume from cache (default), then crawl on
    python run.py --rescan       # re-scan CACHED data only: no network, fast
    python run.py --fresh        # ignore cache, crawl from scratch
    python run.py --cache-stats  # what's currently cached

Why --rescan exists: a full crawl is ~600 rendered pages plus ~1000 direct
fetches. Every response body and every per-page browser artifact (innerText,
CSS ::before content, storage, IndexedDB) is cached to .crawlcache/, so after
changing a decoder you can re-examine everything the site ever served in
seconds instead of re-crawling. It can't discover NEW urls — for that, use a
plain (resuming) run.
"""
import sys

from crawler import config
from crawler.pipeline import rescan, run
from crawler.store import Store

USAGE = __doc__


def main(argv: list[str]) -> int:
    unknown = [a for a in argv if a not in
               ("--rescan", "--fresh", "--no-resume", "--cache-stats",
                "-h", "--help")]
    if unknown:
        print(f"Unknown option(s): {' '.join(unknown)}\n{USAGE}")
        return 2
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0

    store = Store()

    if "--cache-stats" in argv:
        stats = store.stats()
        print(f"Cache directory: {store.root}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        state = store.load_state()
        if state:
            found = len(set(state.get("all_flags", []))
                        - {config.KNOWN_EXAMPLE_FLAG})
            print(f"  pages rendered: {len(state.get('visited_keys', []))}")
            print(f"  urls fetched:   {len(state.get('directly_fetched', []))}")
            print(f"  queue pending:  {len(state.get('queue', []))}")
            print(f"  passwords found: {found} / {config.EXPECTED_COUNT}")
        return 0

    if "--rescan" in argv:
        print("Re-scanning cached data (no network, no browser)...\n")
        result = rescan(store, config.START_URL)
    else:
        resume = "--fresh" not in argv and "--no-resume" not in argv
        if not resume:
            print("Starting fresh — ignoring any cached crawl state.\n")
        result = run(config.START_URL, config.USERNAME, config.PASSWORD,
                     store=store, resume=resume)

    print(f"\nFound {result['count']} / {config.EXPECTED_COUNT} passwords "
          f"automatically.")
    if result["count"] < config.EXPECTED_COUNT:
        print("Two of the eight cannot be extracted by any crawler — see "
              "FINDINGS.md:")
        print("  /static/img/whiteboard-scan.png  password drawn as pixels; "
              "open and read it")
        print("  /status/eu-region/               served only to a German IP; "
              "use geocheck.py")
        print("Diagnostics:")
        print("  python audit.py all      # site census, header sweep, images")
        print("  python selftest.py       # which extraction mechanism broke")
    return 0


if __name__ == "__main__":
    # config.START_URL already points at the resolved target
    # (the task's Google redirect wrapper decodes to http://54.214.7.161/).
    sys.exit(main(sys.argv[1:]))
