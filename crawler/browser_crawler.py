"""The two-tier crawler: a headless-browser render pass (Playwright) that
executes JS and clicks/selects its way around the site, followed by a
plain-HTTP direct-fetch pass over every URL that was ever referenced.
See README "Two tiers: render, then fetch" for why both exist.
"""
from __future__ import annotations

import base64
import json
from collections import defaultdict, deque
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config
from .crawl_state import ResumableStateMixin
from .extractors import find_flags_in_text
from .fetching import FetchingMixin
from .interaction import InteractionMixin
from .link_discovery import canonical_key, extract_resource_urls, top_level_section
from .page_probes import PageProbesMixin
from .queueing import QueueingMixin
from .reporting import ReportingMixin
from .response_handling import ResponseHandlingMixin


class Crawler(ResumableStateMixin, PageProbesMixin, InteractionMixin,
              FetchingMixin, ResponseHandlingMixin, ReportingMixin, QueueingMixin):
    def __init__(self, start_url: str, username: str, password: str,
                 store=None, resume: bool = True):
        self.start_url = start_url
        self.origin_netloc = urlparse(start_url).netloc
        self.username = username
        self.password = password
        self.store = store
        self.resumed = False

        # canonical_key(url) -> have we dealt with this page already, so
        # ?utm_source=..., trailing slashes, and /index.html variants of
        # the same page are only ever visited once
        self.visited_keys: set[str] = set()
        self.queued_keys: set[str] = set()
        self.queue: deque[str] = deque()

        self.hits: dict[str, set[str]] = {}       # url -> flags found there
        self.hit_notes: dict[str, list[str]] = {}  # url -> where each was found
        self.all_flags: set[str] = set()

        # trap-bounding state (see queueing.py)
        self.section_visit_count: dict[str, int] = defaultdict(int)
        self.section_pages_since_new_flag: dict[str, int] = defaultdict(int)
        self.capped_sections: set[str] = set()
        self.capped_pagination: set[str] = set()

        # every same-origin URL ever referenced, exact query string intact.
        # The browser tier renders a canonically-deduplicated subset; the
        # direct-fetch tier then fetches every one verbatim, which is what
        # catches source maps, download-disposition files, and decoy
        # param variants a headless browser never requests on its own.
        self.discovered_urls: set[str] = set()
        self.directly_fetched: set[str] = set()

        # sha1(body) -> first URL that served it, so identical content
        # under many URLs is only scanned once
        self.body_hashes: dict[str, str] = {}

        # per-page browser-only artifacts, cached so --rescan can
        # re-examine them without relaunching Chromium
        self._page_artifacts: dict = {}
        # console output accrues DURING navigation, before _page_artifacts
        # is reset, so it's collected separately and merged in at save time
        self._console_log: dict[str, list[str]] = defaultdict(list)
        # leads that need a human decision, not auto-promoted to passwords
        self.text_images: dict[str, str] = {}
        self.hex_candidates: dict[str, list[str]] = {}
        # URLs left unfetched when the direct-fetch round cap fired
        self.unfetched_at_cutoff: list[str] = []

        self._enqueue(start_url)
        if resume and store is not None:
            self._restore_state()
        self._write_progress_file()

    def _auth_header(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _record(self, url: str, flags: set[str], note: str):
        if not flags:
            return
        new = flags - self.all_flags
        self.hits.setdefault(url, set()).update(flags)
        self.hit_notes.setdefault(url, []).append(note)
        self.all_flags |= flags
        if new:
            print(f"  [FOUND] {sorted(new)}  <-  {note} @ {url}")
            # a genuinely new flag proves the section still has real
            # content, so reset its stall counter
            section = top_level_section(url)
            self.section_pages_since_new_flag[section] = 0
            self._write_progress_file()

    def _checkpoint(self):
        try:
            with open("results.json", "w") as fh:
                json.dump(self.report(), fh, indent=2)
        except Exception as e:
            print(f"  ! failed to write checkpoint: {e}")
        self._save_state()

    # --- the browser crawl -----------------------------------------------

    def crawl(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = self._start_browser_session(browser)
            try:
                self._drain_queue(page)
            except KeyboardInterrupt:
                print("\n  ! Interrupted by user — writing partial results.json ...")
            finally:
                self._checkpoint()
                self._write_progress_file()
                browser.close()

        # Second tier, outside the browser: fetch every discovered URL
        # verbatim over plain HTTP. Runs after the browser is closed so a
        # browser-side failure can't skip it.
        try:
            self.fetch_all_discovered()
        except KeyboardInterrupt:
            print("\n  ! Interrupted during direct-fetch pass.")
        finally:
            self._checkpoint()
            self._write_progress_file()

    def _start_browser_session(self, browser):
        context = browser.new_context(
            http_credentials={"username": self.username, "password": self.password},
            ignore_https_errors=True,
        )
        context.set_default_timeout(config.NAV_TIMEOUT_MS)
        # must be installed BEFORE any page is created so the patched
        # constructors exist before page scripts run
        self._install_stream_taps(context)
        page = self._new_tracked_page(context)
        context.on("page", self._on_new_page)
        return page

    def _track_page(self, page):
        page.on("response", self._handle_response)
        page.on("console", self._handle_console)
        page.on("websocket", self._handle_websocket)

    def _new_tracked_page(self, context):
        page = context.new_page()
        self._track_page(page)
        return page

    def _on_new_page(self, new_page):
        """A click that opens a new tab/popup instead of navigating in
        place lands here, so we don't silently lose that branch."""
        self._track_page(new_page)
        try:
            new_page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
        except Exception:
            pass
        self._enqueue(new_page.url)
        try:
            new_page.close()
        except Exception:
            pass

    def _drain_queue(self, page):
        warned = False
        while self.queue:
            if not warned and len(self.visited_keys) >= config.WARN_THRESHOLD_PAGES:
                warned = True
                print(f"\n  ! Crawled {len(self.visited_keys)} unique pages so "
                      f"far with {len(self.queue)} still queued. This is just "
                      f"a heartbeat, not a stop — the crawl keeps going until "
                      f"the queue is empty or a section/pagination bound "
                      f"kicks in.\n")

            url = self.queue.popleft()
            key = canonical_key(url)
            self.queued_keys.discard(key)
            if key in self.visited_keys:
                continue  # became redundant while it sat in the queue
            self.visited_keys.add(key)

            section = top_level_section(url)
            self.section_visit_count[section] += 1
            self.section_pages_since_new_flag[section] += 1
            self._maybe_cap_stalled_section(section)

            self._visit_page(page, url)

            if len(self.visited_keys) % config.CHECKPOINT_EVERY == 0:
                self._checkpoint()

    def _visit_page(self, page, url: str):
        print(f"[visit] {url}  ({len(self.visited_keys)} unique so far)")
        if not self._goto(page, url):
            return
        self._scroll_for_lazy_content(page)

        html = page.content()
        self._page_artifacts = {"serializedDOM": html}
        # deep-scan the serialized DOM too: it includes comments and
        # post-JS mutations, and may hold encoded payloads
        self._scan_payload(url, html.encode("utf-8", "ignore"), "serialized DOM after JS")

        # what the browser RENDERS (innerText, CSS ::before content,
        # tooltip attributes) rather than what the source says
        self._scan_rendered_content(page, url)

        # storage/cookies and shadow DOM aren't captured by network
        # interception or page.content() at all — pull them separately
        self._check_storage_and_cookies(page, url)
        self._check_indexeddb(page, url)
        self._probe_shadow_dom(page, url)

        for u in extract_resource_urls(html, url):  # attribute-based discovery
            self._enqueue(u)
        self._discover_iframe_links(page, url)

        # click every element that *looks* interactive, in case navigation
        # only happens via JS with no href to scrape
        self._click_discover(page, url)
        # <select> dropdowns fire navigation/AJAX on 'change', never on
        # 'click' — a plain clickable-element sweep never triggers them
        self._select_discover(page, url)

        self._persist_rendered_artifacts(url)

    def _goto(self, page, url: str) -> bool:
        try:
            page.goto(url, wait_until="networkidle")
            return True
        except PWTimeout:
            try:
                page.goto(url, wait_until="load")
                return True
            except Exception as e:
                print(f"  ! failed to load {url}: {e}")
                return False
        except Exception as e:
            print(f"  ! failed to load {url}: {e}")
            return False

    def _scroll_for_lazy_content(self, page):
        # a couple of passes with pauses, since some content loads on a
        # delay after the scroll event fires
        try:
            for _ in range(3):
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(400)
        except Exception:
            pass

    def _probe_shadow_dom(self, page, url: str):
        shadow_html = self._extract_shadow_dom_text(page)
        if not shadow_html:
            return
        self._record(url, find_flags_in_text(shadow_html), "shadow DOM content")
        for u in extract_resource_urls(shadow_html, url):
            self._enqueue(u)

    def _discover_iframe_links(self, page, url: str):
        # a frame is a separate document the parent page's HTML won't reveal
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_html = frame.content()
            except Exception:
                continue
            for u in extract_resource_urls(frame_html, frame.url or url):
                self._enqueue(u)

    def _persist_rendered_artifacts(self, url: str):
        if self.store is None:
            return
        console = self._console_log.pop(url, [])
        if console:
            self._page_artifacts["console"] = "\n".join(console)
        self.store.save_rendered(url, self._page_artifacts)
