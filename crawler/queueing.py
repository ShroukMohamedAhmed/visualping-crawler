"""URL-queue bookkeeping: what gets rendered next, and the two trap bounds
(an unbounded ?page=N series, and a section that keeps generating new
slugs with no new password) that stop the crawl from running forever.
"""
from __future__ import annotations

from collections import deque
from urllib.parse import urlparse

from . import config
from .link_discovery import (
    canonical_key,
    pagination_param_value,
    same_origin,
    top_level_section,
)


class QueueingMixin:
    """Mixed into Crawler. Expects the queue/discovery attributes set up
    in Crawler.__init__.
    """

    def _enqueue(self, url: str) -> bool:
        """Add url to the browser-render queue unless it's a duplicate,
        off-origin, past the pagination bound, or in a capped section.

        Everywhere else a bound only skips the *render* — the URL is still
        recorded in discovered_urls so the direct-fetch tier retrieves it
        verbatim, so a bound can never cost a password. Pagination past
        the cap is the one exception; see `_is_pagination_beyond_cap`.
        """
        if not same_origin(url, self.origin_netloc):
            return False

        url = url.split("#")[0]  # fragments never change what the server returns
        if not url:
            return False

        if self._is_pagination_beyond_cap(url):
            return False

        self.discovered_urls.add(url)

        key = canonical_key(url)
        if key in self.visited_keys or key in self.queued_keys:
            return False
        if top_level_section(url) in self.capped_sections:
            return False

        self.queued_keys.add(key)
        self.queue.append(url)
        return True

    def _is_pagination_beyond_cap(self, url: str) -> bool:
        """Pagination past the cap is dropped entirely, not just skipped
        for rendering. It has to be: page N's body links to page N+1, so
        merely recording a "bounded" page for direct-fetch re-discovers
        the next one, and an unbounded generator never converges — a real
        run reached /report/?page=2478 before this was caught. Every
        sampled page below the cap is still deep-scanned regardless.
        """
        page_num = pagination_param_value(url, config.PAGINATION_PARAM_NAMES)
        if page_num is None or page_num <= config.MAX_PAGINATION_DEPTH:
            return False
        base = urlparse(url).path
        if base not in self.capped_pagination:
            self.capped_pagination.add(base)
            print(f"  [bound] {base} is a ?page=N series with no end; "
                  f"sampled to page {config.MAX_PAGINATION_DEPTH} and "
                  f"deep-scanned. Deeper pages are neither rendered nor "
                  f"fetched.")
        return True

    def _maybe_cap_stalled_section(self, section: str):
        """If this section has stalled (visited plenty, no new flag in the
        stall window), cap it and drop its already-queued siblings,
        rather than expanding it forever.
        """
        if section in self.capped_sections:
            return
        if self.section_visit_count[section] < config.MAX_PAGES_PER_SECTION:
            return
        if self.section_pages_since_new_flag[section] < config.SECTION_STALL_WINDOW:
            return

        self.capped_sections.add(section)
        print(f"\n  [bound] section '{section}' capped after "
              f"{self.section_visit_count[section]} visits with no new "
              f"flag in the last {config.SECTION_STALL_WINDOW} — treating "
              f"remaining slugs as a combinatorial generator.\n")
        self._drop_queued_section(section)

    def _drop_queued_section(self, section: str):
        remaining = deque()
        for q_url in self.queue:
            if top_level_section(q_url) == section:
                self.queued_keys.discard(canonical_key(q_url))
            else:
                remaining.append(q_url)
        self.queue = remaining
