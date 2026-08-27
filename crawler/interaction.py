"""Click/select discovery: exercise every clickable-looking element and
every <select> dropdown, in case navigation only happens via JS with no
href to scrape.
"""
from __future__ import annotations

from playwright.sync_api import TimeoutError as PWTimeout

from . import config
from .extractors import find_flags_in_text
from .link_discovery import CLICKABLE_SELECTOR, extract_resource_urls


class InteractionMixin:
    """Mixed into Crawler. Expects `_enqueue` and `_record` on the host."""

    def _click_discover(self, page, origin_url: str):
        try:
            handles = page.query_selector_all(CLICKABLE_SELECTOR)
        except Exception:
            return
        for handle in handles[: config.MAX_CLICK_TARGETS_PER_PAGE]:
            self._try_click(page, handle, origin_url)

    def _try_click(self, page, handle, origin_url: str):
        try:
            if not handle.is_visible():
                return
            with page.expect_navigation(timeout=3500):
                handle.click(timeout=3500)
            page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
            self._enqueue(page.url)
            if page.url != origin_url:
                page.go_back(wait_until="networkidle")
        except PWTimeout:
            # click didn't cause a navigation within the window — fine, it
            # either wasn't a nav link or opened something async that
            # _handle_response already captured.
            return
        except Exception:
            # element detached / covered / not clickable — best-effort
            # recovery, then move on to the next handle
            self._safe_go_back(page, origin_url)

    def _safe_go_back(self, page, origin_url: str):
        try:
            if page.url != origin_url:
                page.go_back(wait_until="networkidle")
        except Exception:
            pass

    def _select_discover(self, page, origin_url: str):
        """Exercise every <select> dropdown by choosing each of its
        options in turn. A dropdown (e.g. a region/category switcher)
        commonly fires navigation or an AJAX request on 'change', which a
        click-only sweep never triggers.
        """
        try:
            selects = page.query_selector_all("select")
        except Exception:
            return
        for select_handle in selects:
            self._exercise_select(page, select_handle, origin_url)

    def _exercise_select(self, page, select_handle, origin_url: str):
        try:
            if not select_handle.is_visible():
                return
            option_values = select_handle.eval_on_selector_all(
                "option", "opts => opts.map(o => o.value)")
        except Exception:
            return
        for value in option_values[: config.MAX_CLICK_TARGETS_PER_PAGE]:
            self._try_select_option(page, select_handle, value, origin_url)

    def _try_select_option(self, page, select_handle, value, origin_url: str):
        try:
            select_handle.select_option(value=value, timeout=3500)
        except Exception:
            return  # this option value didn't apply cleanly, skip it

        try:
            page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
        except Exception:
            pass

        if page.url != origin_url:
            self._enqueue(page.url)
            try:
                page.goto(origin_url, wait_until="networkidle")
            except Exception:
                pass
            return

        self._rescan_after_select(page, value, origin_url)

    def _rescan_after_select(self, page, value, origin_url: str):
        # same URL, but content may have been swapped in via AJAX (any
        # request that fired was already captured by _handle_response) —
        # re-scan the DOM for anything newly revealed
        try:
            html = page.content()
        except Exception:
            return
        flags = find_flags_in_text(html)
        if flags:
            self._record(origin_url, flags, f"page content after selecting '{value}'")
        for u in extract_resource_urls(html, origin_url):
            self._enqueue(u)
