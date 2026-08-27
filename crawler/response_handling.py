"""Browser response/console handlers: what happens to each network
response Playwright intercepts, and to console.log/warn/error output.
"""
from __future__ import annotations

from .extractors import (
    describe_image_metadata,
    describe_probable_text_image,
    find_flags_all_encodings,
    find_flags_in_bytes,
    find_flags_in_headers,
    find_flags_in_lsb_steganography,
    find_flags_in_text,
    looks_like_image,
)
from .link_discovery import extract_css_urls, extract_js_urls, extract_sourcemap_urls, same_origin


class ResponseHandlingMixin:
    """Mixed into Crawler. Expects `_record`, `_enqueue`, `_scan_payload`,
    `store`, `origin_netloc`, and the discovery/hit-tracking attributes.
    """

    def _handle_response(self, response):
        url = response.url
        try:
            headers = response.headers
        except Exception:
            headers = {}

        self._record(url, find_flags_in_headers(headers), "HTTP response headers")
        self._record_duplicate_headers(response, url)
        self._record_status_reason(response, url)

        body = self._read_browser_body(response, url)
        if body is None:
            return

        content_type = headers.get("content-type", "")
        self._scan_payload(url, body, f"response body ({content_type or 'unknown type'})")
        self._cache_browser_body(response, url, headers, body)

        if looks_like_image(content_type, url):
            self._scan_image_metadata_and_pixels(url, body)
        if "css" in content_type or url.endswith(".css"):
            self._enqueue_from(extract_css_urls, body, url)
        if "javascript" in content_type or url.endswith(".js"):
            self._enqueue_from(extract_js_urls, body, url)
        self._enqueue_source_maps(body, url)

    def _record_duplicate_headers(self, response, url: str):
        # headers_array() preserves DUPLICATE header names, which the
        # headers dict silently collapses — a flag planted in a repeated
        # custom header (or a second Set-Cookie) is only visible here.
        try:
            for h in response.headers_array():
                self._record(url, find_flags_in_text(f"{h['name']}: {h['value']}"),
                             "HTTP response headers (duplicate-preserving)")
        except Exception:
            pass

    def _record_status_reason(self, response, url: str):
        # the HTTP reason phrase is server-controlled and shows up nowhere
        # in the body or the headers dict
        try:
            if response.status_text:
                self._record(url, find_flags_in_text(response.status_text),
                             "HTTP status reason phrase")
        except Exception:
            pass

    def _read_browser_body(self, response, url: str):
        try:
            return response.body()
        except Exception:
            # Downloads, opaque redirects, and some cached responses raise
            # here. Don't drop it silently — queue it for the direct-fetch
            # tier over plain HTTP instead.
            if same_origin(url, self.origin_netloc):
                self.discovered_urls.add(url.split("#")[0])
                print(f"  [note] body unreadable in-browser, queued for "
                      f"direct fetch: {url}")
            return None

    def _cache_browser_body(self, response, url: str, headers: dict, body: bytes):
        # cache browser-observed bodies too, so --rescan covers resources
        # the direct tier might not reach (XHR/fetch responses, etc.)
        if self.store is None or not same_origin(url, self.origin_netloc):
            return
        if not self.store.has_body(url):
            status = response.status if hasattr(response, "status") else None
            self.store.save_body(url, status, headers, body)

    def _scan_image_metadata_and_pixels(self, url: str, body: bytes):
        meta = describe_image_metadata(body)
        if meta:
            meta_bytes = meta.encode("utf-8", errors="ignore")
            self._record(url, find_flags_in_bytes(meta_bytes), "image metadata")
            # EXIF UserComment declares a "UNICODE\0" charset and stores
            # UTF-16LE, so the flag's bytes look like V\x00I\x00S\x00...
            # and never match a contiguous-ASCII regex — the deep decoder
            # handles wide encodings.
            for how, hits in find_flags_all_encodings(meta_bytes).items():
                self._record(url, hits, f"image metadata, decoded via {how}")

        # flags hidden in the pixel data itself (LSB steganography), not
        # in any text/metadata field
        self._record(url, find_flags_in_lsb_steganography(body), "image LSB steganography")

        # A password can also be DRAWN into the pixels as visible text. No
        # byte-level technique can read that — flag it for manual review.
        hint = describe_probable_text_image(body)
        if hint:
            self.text_images[url] = hint
            print(f"  [look] {url}: {hint}")

    def _enqueue_from(self, extractor, body: bytes, url: str):
        try:
            text = body.decode("utf-8", errors="ignore")
        except Exception:
            return
        for u in extractor(text, url):
            self._enqueue(u)

    def _enqueue_source_maps(self, body: bytes, url: str):
        # sourceMappingURL comments can appear in JS *or* CSS, and are
        # unquoted so the literal sweep above never sees them. The browser
        # won't request the .map either (devtools-only), so discovering it
        # here is the only way it ever gets fetched — by the direct tier.
        try:
            text = body.decode("utf-8", errors="ignore")
        except Exception:
            return
        for u in extract_sourcemap_urls(text, url):
            if u not in self.discovered_urls:
                print(f"  [sourcemap] discovered {u}")
            self._enqueue(u)

    def _handle_console(self, msg):
        """console.log/warn/error calls never show up as network
        responses or in page.content() — this is the only way to see
        them.
        """
        try:
            text = msg.text
            page_url = msg.page.url
        except Exception:
            return
        flags = find_flags_in_text(text)
        if flags:
            self._record(page_url, flags, "browser console output")
        # keep console text for the rendered cache: WebSocket/SSE payloads
        # arrive here via the stream taps and exist nowhere else, so
        # without this a --rescan would silently lose them
        self._console_log[page_url].append(text)
