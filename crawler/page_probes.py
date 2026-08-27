"""Browser-only probes: things a page RENDERS or stores that never show up
in a network response or in page.content() — storage, IndexedDB, shadow
DOM, CSS-generated content, and streamed WebSocket/SSE/postMessage data.
"""
from __future__ import annotations

from .extractors import (
    find_flags_all_encodings,
    find_flags_in_bytes,
    find_flags_in_text,
)

_STORAGE_JS = """() => {
    let ls = {}, ss = {};
    try { for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i); ls[k]=localStorage.getItem(k);} } catch(e) {}
    try { for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i); ss[k]=sessionStorage.getItem(k);} } catch(e) {}
    return {cookie: document.cookie, localStorage: ls, sessionStorage: ss};
}"""

_SHADOW_DOM_JS = """() => {
    let out = [];
    function walk(root) {
        const all = root.querySelectorAll('*');
        for (const el of all) {
            if (el.shadowRoot) {
                out.push(el.shadowRoot.innerHTML);
                walk(el.shadowRoot);
            }
        }
    }
    walk(document);
    return out.join('\\n');
}"""

_RENDERED_CONTENT_JS = """() => {
    const out = {text: '', pseudo: [], attrs: []};
    try { out.text = document.body ? document.body.innerText : ''; } catch(e) {}
    // strip zero-width chars often used to break up a string
    try { out.text = out.text.replace(/[\\u200B-\\u200D\\uFEFF\\u00AD]/g, ''); } catch(e) {}
    const els = document.querySelectorAll('*');
    for (const el of els) {
        for (const which of ['::before', '::after', '::marker']) {
            try {
                const c = getComputedStyle(el, which).content;
                if (c && c !== 'none' && c !== 'normal') out.pseudo.push(c);
            } catch(e) {}
        }
        for (const a of ['title','alt','aria-label','placeholder','value','data-content']) {
            try {
                const v = el.getAttribute(a);
                if (v) out.attrs.push(v);
            } catch(e) {}
        }
    }
    return out;
}"""

_INDEXEDDB_JS = """async () => {
    if (!window.indexedDB || !indexedDB.databases) return '';
    const out = [];
    let dbs = [];
    try { dbs = await indexedDB.databases(); } catch(e) { return ''; }
    for (const meta of dbs) {
        if (!meta.name) continue;
        try {
            const db = await new Promise((res, rej) => {
                const rq = indexedDB.open(meta.name);
                rq.onsuccess = () => res(rq.result);
                rq.onerror = () => rej(rq.error);
            });
            for (const storeName of Array.from(db.objectStoreNames)) {
                try {
                    const tx = db.transaction(storeName, 'readonly');
                    const rows = await new Promise((res, rej) => {
                        const rq = tx.objectStore(storeName).getAll();
                        rq.onsuccess = () => res(rq.result);
                        rq.onerror = () => rej(rq.error);
                    });
                    out.push(storeName + '=' + JSON.stringify(rows));
                } catch(e) {}
            }
            db.close();
        } catch(e) {}
    }
    return out.join('\\n');
}"""

# Patches WebSocket / EventSource / postMessage so streamed data is
# observable. `response.body()` on a `text/event-stream` never resolves,
# so network interception cannot see it — the cheapest reliable tap is
# wrapping the constructors and echoing every message to the console.
_STREAM_TAP_JS = """
    (() => {
      const tag = '[[VP-STREAM]] ';
      const shout = (what, data) => {
        try {
          console.log(tag + what + ' ' +
            (typeof data === 'string' ? data : JSON.stringify(data)));
        } catch (e) {}
      };
      try {
        const NativeWS = window.WebSocket;
        if (NativeWS) {
          window.WebSocket = function (...args) {
            const ws = new NativeWS(...args);
            ws.addEventListener('message', e => shout('ws', e.data));
            return ws;
          };
          window.WebSocket.prototype = NativeWS.prototype;
        }
      } catch (e) {}
      try {
        const NativeES = window.EventSource;
        if (NativeES) {
          window.EventSource = function (...args) {
            const es = new NativeES(...args);
            es.addEventListener('message', e => shout('sse', e.data));
            return es;
          };
          window.EventSource.prototype = NativeES.prototype;
        }
      } catch (e) {}
      try {
        window.addEventListener('message', e => shout('postMessage', e.data));
      } catch (e) {}
    })();
"""


class PageProbesMixin:
    """Mixed into Crawler. Expects `_record` and `_page_artifacts` on the
    host class.
    """

    def _check_storage_and_cookies(self, page, url: str):
        """document.cookie, localStorage, and sessionStorage are all
        invisible to network-response interception (they're set/read by
        JS, not sent as part of an HTTP response body).
        """
        try:
            snapshot = page.evaluate(_STORAGE_JS)
        except Exception:
            return
        blob = str(snapshot)
        self._page_artifacts["storage"] = blob
        self._record(url, find_flags_in_text(blob), "cookie/localStorage/sessionStorage")

    def _extract_shadow_dom_text(self, page) -> str:
        """page.content() serializes light DOM only — content rendered
        inside an open shadow root is invisible to it.
        """
        try:
            return page.evaluate(_SHADOW_DOM_JS) or ""
        except Exception:
            return ""

    def _scan_rendered_content(self, page, url: str):
        """Scan what the browser actually *renders*, not the HTML source.

        Three things live here that a source scan can never see: text
        split across markup (innerText joins it back together), CSS
        `::before`/`::after` generated content (in no innerText and no
        attribute), and attribute-only text (title/alt/aria-label).
        """
        try:
            payload = page.evaluate(_RENDERED_CONTENT_JS) or {}
        except Exception:
            return
        self._record_rendered_text(url, payload)
        self._record_pseudo_content(url, payload)
        self._record_attribute_text(url, payload)

    def _record_rendered_text(self, url: str, payload: dict):
        rendered = payload.get("text") or ""
        self._page_artifacts.update({
            "innerText": rendered,
            "pseudo": payload.get("pseudo") or [],
            "attrs": payload.get("attrs") or [],
        })
        self._record(url, find_flags_in_text(rendered), "rendered innerText")
        # also try with whitespace removed, in case the flag is split
        # across lines/elements and innerText inserted breaks inside it
        self._record(url, find_flags_in_text("".join(rendered.split())),
                     "rendered innerText, whitespace-stripped")

    def _record_pseudo_content(self, url: str, payload: dict):
        pseudo_blob = "\n".join(payload.get("pseudo") or [])
        if not pseudo_blob:
            return
        self._record(url, find_flags_in_text(pseudo_blob),
                     "CSS generated content (::before/::after)")
        for how, flags in find_flags_all_encodings(
                pseudo_blob.encode("utf-8", "ignore")).items():
            self._record(url, flags, f"CSS generated content, decoded via {how}")

    def _record_attribute_text(self, url: str, payload: dict):
        attr_blob = "\n".join(payload.get("attrs") or [])
        if attr_blob:
            self._record(url, find_flags_in_text(attr_blob),
                         "element attributes (title/alt/aria-label/...)")

    def _check_indexeddb(self, page, url: str):
        """Dump every IndexedDB object store on the page — a separate
        store nothing else touches, invisible to network interception,
        page.content(), and the storage snapshot above.
        """
        try:
            dump = page.evaluate(_INDEXEDDB_JS) or ""
        except Exception:
            return
        if not dump:
            return
        self._page_artifacts["indexeddb"] = dump
        self._record(url, find_flags_in_text(dump), "IndexedDB contents")
        for how, hits in find_flags_all_encodings(dump.encode("utf-8", "ignore")).items():
            self._record(url, hits, f"IndexedDB contents, decoded via {how}")

    def _install_stream_taps(self, context):
        context.add_init_script(_STREAM_TAP_JS)

    def _handle_websocket(self, ws):
        """Native Playwright websocket tap, belt-and-braces alongside the
        init-script patch (which a page could in principle bypass by
        grabbing a pristine WebSocket from an iframe)."""
        def on_frame(payload):
            try:
                data = payload if isinstance(payload, bytes) else str(payload).encode()
            except Exception:
                return
            self._record(ws.url, find_flags_in_bytes(data), "WebSocket frame")
            for how, hits in find_flags_all_encodings(data).items():
                self._record(ws.url, hits, f"WebSocket frame, decoded via {how}")

        try:
            ws.on("framereceived", on_frame)
            ws.on("framesent", on_frame)
        except Exception:
            pass
