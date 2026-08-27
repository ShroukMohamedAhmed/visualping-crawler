"""Direct-fetch tier: plain HTTP GET with Basic Auth, bypassing the
browser entirely, and looping over every discovered URL until none are
new. See README "Two tiers: render, then fetch".
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request

from . import config
from .decoders import decompress_content_encoding
from .extractors import (
    describe_image_metadata,
    find_bare_hex16_candidates,
    find_flags_all_encodings,
    find_flags_in_bytes,
    find_flags_in_headers,
    find_flags_in_lsb_steganography,
    find_flags_in_text,
    looks_like_image,
)
from .link_discovery import (
    extract_css_urls,
    extract_js_urls,
    extract_resource_urls,
    extract_sourcemap_urls,
)


class FetchingMixin:
    """Mixed into Crawler. Expects `_record`, `_enqueue`, `_auth_header`,
    `_checkpoint`, `store`, and the discovery/hit-tracking attributes set
    up in Crawler.__init__.
    """

    def _scan_payload(self, url: str, body: bytes, note_prefix: str):
        """Scan one response body for flags: literal bytes first, then
        every layered/compressed/escaped encoding.
        """
        self._record(url, find_flags_in_bytes(body), f"{note_prefix} (literal bytes)")
        for how, flags in find_flags_all_encodings(body).items():
            self._record(url, flags, f"{note_prefix}, decoded via {how}")
        # bare 16-hex runs with no wrapper — reported, never auto-promoted
        bare = find_bare_hex16_candidates(body)
        if bare:
            existing = set(self.hex_candidates.get(url, []))
            self.hex_candidates[url] = sorted(existing | bare)

    def _direct_fetch(self, url: str) -> tuple[bytes, dict] | None:
        """Plain HTTP GET with Basic Auth, bypassing the browser entirely.

        Needed because Chromium will not hand us the bytes of everything
        it touches: `.map` source maps are only fetched with devtools
        open, downloads (Content-Disposition: attachment) make
        response.body() raise, and canonical_key() collapses decoy-param
        duplicates before they're ever rendered.

        Read-through cached: if this URL's bytes are already on disk from
        an earlier run, use them instead of hitting the network again.
        """
        if self.store is not None:
            cached = self.store.load_body(url)
            if cached is not None:
                return cached

        req = urllib.request.Request(url, headers={
            "Authorization": self._auth_header(),
            "User-Agent": config.DIRECT_FETCH_USER_AGENT,
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=config.DIRECT_FETCH_TIMEOUT_S) as resp:
                return self._decode_and_cache(
                    url, resp.read(config.MAX_DIRECT_FETCH_BYTES), dict(resp.headers))
        except urllib.error.HTTPError as e:
            try:  # a 404/500 body can still carry a flag (custom error pages)
                return self._decode_and_cache(
                    url, e.read(config.MAX_DIRECT_FETCH_BYTES), dict(e.headers))
            except Exception:
                return None
        except Exception:
            return None

    def _decode_and_cache(self, url: str, raw: bytes, headers: dict) -> tuple[bytes, dict]:
        # urllib does not decompress automatically — without this, a
        # gzipped HTML page yields garbage on .decode() and contributes
        # zero links, silently losing a subtree.
        body = decompress_content_encoding(raw, headers.get("Content-Encoding", ""))
        if self.store is not None:
            # cache the DECODED bytes so a later --rescan sees usable
            # content without needing to redo the transfer decoding
            self.store.save_body(url, None, headers, body)
        return body, headers

    def fetch_all_discovered(self):
        """Second tier: fetch every discovered same-origin URL verbatim
        over plain HTTP and deep-scan the bytes. Runs after the browser
        crawl, looping until no NEW urls appear.
        """
        print("\n" + "-" * 60)
        print("Direct-fetch pass: retrieving every discovered URL verbatim")
        print("(source maps, PDFs, download-disposition files, param variants)")
        print("-" * 60)

        round_num = 0
        while True:
            pending = sorted(self.discovered_urls - self.directly_fetched)
            if not pending:
                break
            round_num += 1
            if round_num > config.MAX_DIRECT_FETCH_ROUNDS:
                self._stop_direct_fetch_rounds(pending)
                break
            print(f"  [direct] round {round_num}: {len(pending)} new URL(s)")
            for url in pending:
                self._direct_fetch_one(url)
            self._checkpoint()

    def _stop_direct_fetch_rounds(self, pending: list):
        # Safety net: a URL space that keeps producing new URLs every
        # round is a generator, not a site — without this the loop is
        # unbounded, since each fetched page reveals the next one forever.
        print(f"  [bound] stopping direct-fetch after "
              f"{config.MAX_DIRECT_FETCH_ROUNDS} rounds with "
              f"{len(pending)} URL(s) still pending — the frontier is not "
              f"converging, which means a generator is feeding it. "
              f"Pending sample: {pending[:3]}")
        self.unfetched_at_cutoff = pending

    def _direct_fetch_one(self, url: str):
        self.directly_fetched.add(url)
        fetched = self._direct_fetch(url)
        if fetched is None:
            return
        body, headers = fetched

        self._record(url, find_flags_in_headers(headers),
                     "HTTP response headers (direct fetch)")

        digest = hashlib.sha1(body).hexdigest()
        if digest in self.body_hashes:  # already scanned this exact content
            return
        self.body_hashes[digest] = url

        self._scan_payload(url, body, "direct fetch body")
        self._scan_image_if_looks_like_one(url, body, headers.get("Content-Type", ""),
                                           "direct fetch")
        self._reextract_links(url, body, headers)

    def _scan_image_if_looks_like_one(self, url: str, body: bytes, content_type: str,
                                      note_suffix: str):
        if not looks_like_image(content_type, url):
            return
        meta = describe_image_metadata(body)
        if meta:
            self._record(url, find_flags_in_text(meta), f"image metadata ({note_suffix})")
        self._record(url, find_flags_in_lsb_steganography(body),
                     f"image LSB steganography ({note_suffix})")

    def _reextract_links(self, url: str, body: bytes, headers: dict):
        # re-extract links from whatever we just pulled, so newly revealed
        # URLs get picked up on the next round
        try:
            text = body.decode("utf-8", errors="ignore")
        except Exception:
            return
        ctype = headers.get("Content-Type", "").lower()
        lowered = url.lower().split("?")[0]
        before = len(self.discovered_urls)
        if "html" in ctype or lowered.endswith((".html", "/")):
            for u in extract_resource_urls(text, url):
                self._enqueue(u)
        if "css" in ctype or lowered.endswith(".css"):
            for u in extract_css_urls(text, url):
                self._enqueue(u)
        if ("javascript" in ctype or "json" in ctype
                or lowered.endswith((".js", ".json", ".map"))):
            for u in extract_js_urls(text, url):
                self._enqueue(u)
        for u in extract_sourcemap_urls(text, url):
            self._enqueue(u)
        gained = len(self.discovered_urls) - before
        if gained:
            print(f"    +{gained} new URL(s) from {url}")
