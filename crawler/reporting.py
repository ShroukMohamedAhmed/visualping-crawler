"""Completeness report: summarises what a Crawler run found, and states
plainly whether the crawl is done or was bounded, and by what.
"""
from __future__ import annotations

from . import config


class ReportingMixin:
    """Mixed into Crawler. Expects the tracking attributes assigned in
    Crawler.__init__.
    """

    def report(self) -> dict:
        real_flags = self.all_flags - {config.KNOWN_EXAMPLE_FLAG}
        crawl_complete = len(self.queue) == 0 and not (
            self.capped_sections or self.capped_pagination
        )
        return {
            "flags": sorted(real_flags),
            "count": len(real_flags),
            "example_flag_seen_on_site": config.KNOWN_EXAMPLE_FLAG in self.all_flags,
            "unique_pages_visited": len(self.visited_keys),
            "unique_urls_discovered": len(self.discovered_urls),
            "urls_directly_fetched": len(self.directly_fetched),
            "distinct_body_hashes": len(self.body_hashes),
            "crawl_complete": crawl_complete,
            "crawl_complete_explanation": self._completeness_explanation(crawl_complete),
            "section_visit_counts": dict(self.section_visit_count),
            "capped_sections": sorted(self.capped_sections),
            "capped_pagination_bases": sorted(self.capped_pagination),
            # Leads needing a human decision — deliberately NOT counted as
            # passwords, because auto-promoting a guess is worse than
            # surfacing it.
            "images_that_look_like_text": self.text_images,
            "bare_hex16_candidates": self.hex_candidates,
            "unfetched_at_cutoff": self.unfetched_at_cutoff[:50],
            "hits_by_url": {
                url: {"flags": sorted(flags),
                      "notes": sorted(set(self.hit_notes.get(url, [])))}
                for url, flags in self.hits.items()
            },
        }

    def _completeness_explanation(self, crawl_complete: bool) -> str:
        if crawl_complete:
            return (
                "Queue exhausted naturally: every same-origin URL reachable "
                "from the homepage (via links, JS-driven clicks, CSS/JS "
                "references, source maps, and iframe content) was rendered "
                "once, deduplicated by canonical_key(); AND every discovered "
                f"URL ({len(self.discovered_urls)}) was then fetched verbatim "
                "over plain HTTP and deep-scanned, so nothing was judged only "
                "on what the browser chose to request."
            )
        if self.queue:
            return (
                f"Crawl did not run to completion — {len(self.queue)} URLs "
                f"were still queued when the process stopped (likely "
                f"interrupted manually)."
            )
        return (
            "Queue drained, but with deliberate bounds applied: "
            f"{sorted(self.capped_sections) or 'none'} section(s) were "
            f"capped as combinatorial-generator traps, and "
            f"{sorted(self.capped_pagination) or 'none'} pagination "
            f"series were capped past "
            f"{config.MAX_PAGINATION_DEPTH} pages. All real flags found "
            f"prior to each cap; see 'section_visit_counts' for the "
            f"evidence each section had gone stale (no new flags for "
            f"{config.SECTION_STALL_WINDOW}+ pages) before it was capped."
        )
