#!/usr/bin/env python3
"""End-to-end self-test: serve a mock site that hides flags the same ways
the real target does, then run the real crawler against it and assert it
finds all of them.

This exists because a crawler that returns "4 of 8" gives you no signal
about WHICH capability is missing. Here every flag is planted via exactly
one mechanism, so a failure names the broken feature:

    python selftest.py

Run it after any change to the extractors, decoders, or discovery code.
"""
from __future__ import annotations

import base64
import gzip
import http.server
import io
import socketserver
import threading
import zlib

PORT = 8899
USER, PASSWORD = "selftest", "selftestpw"

# Each flag is reachable by exactly ONE mechanism, so a miss is diagnostic.
FLAGS = {
    "html-comment":      "VISUALPING{1111111111111111}",
    "js-file":           "VISUALPING{2222222222222222}",
    "response-header":   "VISUALPING{3333333333333333}",
    "deep-pagination":   "VISUALPING{4444444444444444}",
    "source-map":        "VISUALPING{5555555555555555}",
    "pdf-compressed":    "VISUALPING{6666666666666666}",
    "css-generated":     "VISUALPING{7777777777777777}",
    "split-by-markup":   "VISUALPING{8888888888888888}",
    "gzip-encoded":      "VISUALPING{9999999999999999}",
    "indexeddb":         "VISUALPING{aaaaaaaaaaaaaaaa}",
    "sse-stream":        "VISUALPING{bbbbbbbbbbbbbbbb}",
    "exif-utf16":        "VISUALPING{cccccccccccccccc}",
}
EXAMPLE_FLAG = "VISUALPING{0000deadbeef0000}"


def _pdf_with_flag(flag: str) -> bytes:
    """A minimal PDF whose text sits in a FlateDecode stream — the flag's
    ASCII never appears in the file bytes, so only decompression finds it.
    Also served as an attachment, so a headless browser won't give us a
    readable body and the direct-fetch tier has to do the work.
    """
    stream = zlib.compress(b"BT /F1 12 Tf (" + flag.encode() + b") Tj ET")
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
            b"4 0 obj<</Length " + str(len(stream)).encode() +
            b"/Filter/FlateDecode>>stream\n" + stream +
            b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF")


def _jpeg_with_utf16_exif(flag: str) -> bytes:
    """A JPEG whose EXIF UserComment holds the flag as UTF-16LE after the
    "UNICODE\0" charset prefix — exactly how the real site stored one.
    The flag's ASCII never appears contiguously in the file.
    """
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 200, 200)).save(buf, "JPEG")
    base = buf.getvalue()
    payload = b"UNICODE\x00" + flag.encode("utf-16-le")
    # minimal EXIF: APP1 + TIFF header + 1 IFD entry (UserComment, 0x9286)
    tiff = (b"II*\x00" + (8).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + b"\x86\x92" + (7).to_bytes(2, "little")
            + len(payload).to_bytes(4, "little")
            + (26).to_bytes(4, "little")
            + (0).to_bytes(4, "little") + payload)
    app1 = b"\xff\xe1" + (len(tiff) + 8).to_bytes(2, "big") + b"Exif\x00\x00" + tiff
    return base[:2] + app1 + base[2:]


SPLIT = FLAGS["split-by-markup"]
PAGES: dict[str, tuple[bytes, str]] = {
    "/": (f"""<html><head>
<link rel="stylesheet" href="/static/css/site.css">
<script src="/static/js/app.js"></script>
<script src="/static/js/store.js"></script>
</head><body>
<h1>Mock site</h1>
<!-- planted here: {FLAGS['html-comment']} -->
<p>Worked example, not a real one: {EXAMPLE_FLAG}</p>
<a href="/products/gateway/">products</a>
<a href="/report/?page=1">report</a>
<a href="/docs/manual.pdf">manual (pdf)</a>
<a href="/split/">split</a>
<a href="/compressed/">compressed</a>
<img src="/static/img/photo.jpg">
<div class="badge"></div>
</body></html>""".encode(), "text/html"),

    # flag only in the CSS `content:` property — rendered, but in no
    # element's innerText and in no HTML attribute
    "/static/css/site.css": (
        f'.badge::before {{ content: "{FLAGS["css-generated"]}"; }}'.encode(),
        "text/css"),

    # flag in JS source, plus an UNQUOTED sourceMappingURL comment that a
    # quoted-string-literal regex cannot match and a browser won't fetch
    "/static/js/app.js": (
        f'var token = "{FLAGS["js-file"]}";\n'
        '//# sourceMappingURL=app.js.map\n'.encode(),
        "application/javascript"),

    "/static/js/app.js.map": (
        ('{"version":3,"sources":["app.src.js"],"sourcesContent":'
         f'["// original comment: {FLAGS["source-map"]}"]}}').encode(),
        "application/json"),

    # writes a flag into IndexedDB (nothing else on the page reveals it),
    # and opens an SSE stream that pushes another one
    "/static/js/store.js": (f"""
        try {{
          const rq = indexedDB.open('vpstore', 1);
          rq.onupgradeneeded = () => rq.result.createObjectStore('kv', {{keyPath:'k'}});
          rq.onsuccess = () => {{
            const tx = rq.result.transaction('kv','readwrite');
            tx.objectStore('kv').put({{k:'secret', v:'{FLAGS["indexeddb"]}'}});
          }};
        }} catch (e) {{}}
        try {{ new EventSource('/stream'); }} catch (e) {{}}
    """.encode(), "application/javascript"),

    "/split/": (
        f"<html><body><p>{SPLIT[:6]}<span>{SPLIT[6:12]}</span>"
        f"<b>{SPLIT[12:]}</b></p></body></html>".encode(), "text/html"),

    "/docs/manual.pdf": (_pdf_with_flag(FLAGS["pdf-compressed"]), "application/pdf"),
    "/static/img/photo.jpg": (_jpeg_with_utf16_exif(FLAGS["exif-utf16"]), "image/jpeg"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # keep test output readable

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="selftest"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        expected = "Basic " + base64.b64encode(
            f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            return self._unauthorized()

        path = self.path
        base = path.split("?")[0]

        # paginated series: the flag lives on a deep page, past where a
        # tight pagination cap would have stopped
        if base == "/report/":
            try:
                page = int(path.split("page=")[1].split("&")[0])
            except Exception:
                page = 1
            body = f"<html><body><h2>Report page {page}</h2>".encode()
            if page == 200:
                body += f"<p>{FLAGS['deep-pagination']}</p>".encode()
            if page < 200:
                body += f'<a href="/report/?page={page+1}">next</a>'.encode()
            body += b"</body></html>"
            return self._respond(body, "text/html")

        # flag in a custom response header only
        if base == "/products/gateway/":
            return self._respond(
                b"<html><body>gateway</body></html>", "text/html",
                extra={"X-Secret-Note": FLAGS["response-header"]})

        # body served gzipped with Content-Encoding: gzip. urllib does NOT
        # decompress automatically, so without decompress_content_encoding()
        # this page's bytes are garbage and its links vanish.
        if base == "/compressed/":
            raw = (f"<html><body><p>{FLAGS['gzip-encoded']}</p>"
                   f'<a href="/compressed/child/">child</a></body></html>').encode()
            return self._respond(gzip.compress(raw), "text/html",
                                 extra={"Content-Encoding": "gzip"})

        # reachable ONLY via the link inside the gzipped page above, so it
        # proves link extraction survived the decompression
        if base == "/compressed/child/":
            return self._respond(b"<html><body>child ok</body></html>", "text/html")

        # Server-Sent Events: this data never appears in a completed HTTP
        # response body, so only the stream tap sees it
        if base == "/stream":
            payload = f"data: {FLAGS['sse-stream']}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return

        if base in PAGES:
            body, ctype = PAGES[base]
            extra = {}
            # serve the PDF as a download so response.body() is unreadable
            # in-browser, forcing the direct-fetch tier to handle it
            if base.endswith(".pdf"):
                extra["Content-Disposition"] = 'attachment; filename="manual.pdf"'
            return self._respond(body, ctype, extra=extra)

        return self._respond(b"not found", "text/plain", status=404)

    def _respond(self, body: bytes, ctype: str, status: int = 200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    import os
    import tempfile

    from crawler import config
    from crawler.browser_crawler import Crawler

    # keep the self-test's own bounds identical to the real run
    config.KNOWN_EXAMPLE_FLAG = EXAMPLE_FLAG

    server = Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock site serving on http://127.0.0.1:{PORT}/\n")

    # The crawler writes results.json / progress.json into the CWD. Run from
    # a temp dir so a self-test never overwrites the real crawl's output.
    original_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp(prefix="vp-selftest-")
    try:
        os.chdir(tmpdir)
        crawler = Crawler(f"http://127.0.0.1:{PORT}/", USER, PASSWORD)
        crawler.crawl()
        found = crawler.all_flags
    finally:
        os.chdir(original_cwd)
        server.shutdown()

    print("\n" + "=" * 62)
    missing = {}
    for mechanism, flag in FLAGS.items():
        ok = flag in found
        print(f"  {'PASS' if ok else 'FAIL'}  {mechanism:18} {flag}")
        if not ok:
            missing[mechanism] = flag
    print("=" * 62)

    unexpected = found - set(FLAGS.values()) - {EXAMPLE_FLAG}
    if unexpected:
        print(f"  ! unexpected extra flags: {sorted(unexpected)}")

    if missing:
        print(f"\n{len(missing)} of {len(FLAGS)} mechanisms FAILED: "
              f"{sorted(missing)}")
        raise SystemExit(1)
    print(f"\nAll {len(FLAGS)} hiding mechanisms detected.")


if __name__ == "__main__":
    main()
