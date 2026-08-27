"""On-disk cache of crawl state and every response body seen.

Motivation: a full run is ~628 rendered pages + ~1085 direct fetches. When
the extractors change (a new decoder, a new hiding mechanism), re-crawling
the whole site just to test that change is wasteful and slow — and it hits
the target site far more than necessary.

So everything the server ever gave us is written to disk once:

    .crawlcache/
        state.json          crawl progress: queue, visited, hits, counters
        bodies/<h>.body     raw response bytes, one file per URL
        bodies/<h>.meta     status + headers for that URL
        rendered/<h>.json   per-page browser-only artifacts (see below)

That enables two fast modes (see run.py):

  --resume   (default) skip pages already rendered and URLs already
             fetched; continue the queue where it stopped.
  --rescan   re-run every extractor over the CACHED bodies with no network
             and no browser at all. Seconds instead of an hour, and the
             right way to test a new decoder.

`rendered/` matters because some hiding places only exist in a live
browser — innerText with markup reassembled, CSS ::before content,
localStorage, IndexedDB. Caching those artifacts too means --rescan can
re-examine them without relaunching Chromium.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CACHE_DIR = ".crawlcache"


def _key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root: str = CACHE_DIR):
        self.root = Path(root)
        self.bodies = self.root / "bodies"
        self.rendered = self.root / "rendered"
        self.bodies.mkdir(parents=True, exist_ok=True)
        self.rendered.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"

    # --- response bodies ---------------------------------------------------

    def has_body(self, url: str) -> bool:
        return (self.bodies / f"{_key(url)}.body").exists()

    def save_body(self, url: str, status, headers: dict, body: bytes):
        h = _key(url)
        try:
            (self.bodies / f"{h}.body").write_bytes(body)
            (self.bodies / f"{h}.meta").write_text(json.dumps({
                "url": url, "status": status, "headers": dict(headers or {}),
            }))
        except Exception as e:
            print(f"  ! cache write failed for {url}: {e}")

    def load_body(self, url: str):
        """Return (body, headers) or None if not cached."""
        h = _key(url)
        body_path = self.bodies / f"{h}.body"
        meta_path = self.bodies / f"{h}.meta"
        if not body_path.exists():
            return None
        try:
            body = body_path.read_bytes()
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            return body, meta.get("headers", {})
        except Exception:
            return None

    def iter_bodies(self):
        """Yield (url, body, headers) for every cached response."""
        for meta_path in sorted(self.bodies.glob("*.meta")):
            body_path = meta_path.with_suffix(".body")
            if not body_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                yield meta.get("url", ""), body_path.read_bytes(), meta.get("headers", {})
            except Exception:
                continue

    # --- per-page browser artifacts ---------------------------------------

    def save_rendered(self, url: str, artifacts: dict):
        try:
            (self.rendered / f"{_key(url)}.json").write_text(
                json.dumps({"url": url, **artifacts}))
        except Exception as e:
            print(f"  ! rendered-cache write failed for {url}: {e}")

    def iter_rendered(self):
        for path in sorted(self.rendered.glob("*.json")):
            try:
                yield json.loads(path.read_text())
            except Exception:
                continue

    # --- crawl state -------------------------------------------------------

    def save_state(self, state: dict):
        try:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, self.state_path)  # atomic, so a kill can't corrupt it
        except Exception as e:
            print(f"  ! state write failed: {e}")

    def load_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text())
        except Exception as e:
            print(f"  ! state file unreadable ({e}) — starting fresh")
            return None

    def stats(self) -> dict:
        return {
            "cached_bodies": len(list(self.bodies.glob("*.body"))),
            "cached_rendered_pages": len(list(self.rendered.glob("*.json"))),
            "has_state": self.state_path.exists(),
        }
