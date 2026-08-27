"""Resumable crawl state: load/save the queue, visited set, and hit index
to/from the on-disk store, so a run can pick up where a previous one
stopped instead of starting over.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque

from . import config


class ResumableStateMixin:
    """Mixed into Crawler. Expects the state attributes assigned in
    Crawler.__init__ and a working `_enqueue`.
    """

    def _restore_state(self):
        state = self.store.load_state()
        if not state:
            print("No previous crawl state found — starting fresh.")
            return

        self.visited_keys = set(state.get("visited_keys", []))
        self.discovered_urls = set(state.get("discovered_urls", []))
        self.directly_fetched = set(state.get("directly_fetched", []))
        self.body_hashes = dict(state.get("body_hashes", {}))
        self.all_flags = set(state.get("all_flags", []))
        self.hits = {u: set(f) for u, f in (state.get("hits") or {}).items()}
        self.hit_notes = {u: list(n) for u, n in (state.get("hit_notes") or {}).items()}
        self.section_visit_count = defaultdict(int, state.get("section_visit_count", {}))
        self.section_pages_since_new_flag = defaultdict(
            int, state.get("section_pages_since_new_flag", {}))
        self.capped_sections = set(state.get("capped_sections", []))
        self.capped_pagination = set(state.get("capped_pagination", []))

        # rebuild the render queue, dropping anything already visited
        self.queue = deque()
        self.queued_keys = set()
        for url in state.get("queue", []):
            self._enqueue(url)
        # anything discovered but never rendered still deserves a render
        for url in sorted(self.discovered_urls):
            self._enqueue(url)

        self.resumed = True
        real = len(self.all_flags - {config.KNOWN_EXAMPLE_FLAG})
        print(f"Resumed: {len(self.visited_keys)} pages already rendered, "
              f"{len(self.directly_fetched)} URLs already fetched, "
              f"{real} password(s) already found, {len(self.queue)} queued.")

    def _state_snapshot(self) -> dict:
        return {
            "visited_keys": sorted(self.visited_keys),
            "queue": list(self.queue),
            "discovered_urls": sorted(self.discovered_urls),
            "directly_fetched": sorted(self.directly_fetched),
            "body_hashes": self.body_hashes,
            "all_flags": sorted(self.all_flags),
            "hits": {u: sorted(f) for u, f in self.hits.items()},
            "hit_notes": {u: sorted(set(n)) for u, n in self.hit_notes.items()},
            "section_visit_count": dict(self.section_visit_count),
            "section_pages_since_new_flag": dict(self.section_pages_since_new_flag),
            "capped_sections": sorted(self.capped_sections),
            "capped_pagination": sorted(self.capped_pagination),
        }

    def _save_state(self):
        if self.store is not None:
            self.store.save_state(self._state_snapshot())

    def _write_progress_file(self):
        """Small, fast-to-read file with just the current flag tally —
        updated the instant a new flag is found, so progress can be
        checked without scrolling through stdout or waiting for the
        next full results.json checkpoint.
        """
        real_flags = sorted(self.all_flags - {config.KNOWN_EXAMPLE_FLAG})
        try:
            with open("progress.json", "w") as fh:
                json.dump(
                    {
                        "found": len(real_flags),
                        "expected": config.EXPECTED_COUNT,
                        "flags": real_flags,
                        "pages_visited_so_far": len(self.visited_keys),
                    },
                    fh,
                    indent=2,
                )
        except Exception as e:
            print(f"  ! failed to write progress.json: {e}")
