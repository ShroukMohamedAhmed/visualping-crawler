# Visualping crawler challenge

Finds the eight `VISUALPING{<16 hex chars>}` passwords hidden on
`http://54.214.7.161/`.

**Result: all 8 found.** See [FINDINGS.md](FINDINGS.md) for the passwords,
where each one was, and the evidence behind the completeness claim.

Six are extracted automatically by the crawler. Two cannot be, by
construction, and the crawler's job is to say so precisely rather than to
report a false negative:

| | Why no crawler can extract it | What the tool does instead |
| --- | --- | --- |
| `/static/img/whiteboard-scan.png` | the password is *drawn into the pixels* as visible text — it is not encoded anywhere in the bytes | flags the image as text-shaped: "open and read it" |
| `/status/eu-region/` | served only to a German IP; from elsewhere it is a 403 | `geocheck.py` reports what country the site thinks you are in |

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium        # ~150MB browser download

python selftest.py                 # verify every extraction mechanism works
python run.py                      # the crawl
python audit.py all                # census, header sweep, image dump
python geocheck.py                 # what country does the site see?
```

Credentials and the target are in `crawler/config.py`. The task's Google
redirect wrapper decodes to `http://54.214.7.161/`, which is used directly.

## Approach

### Two tiers: render, then fetch

An early version had only the first tier and returned 4 of 8. The split is
the most important design decision here.

**Tier 1 — browser render (Playwright/Chromium).** Executes JS, clicks every
clickable-looking element, exercises `<select>` dropdowns, reads shadow DOM,
storage and console output, and intercepts every network response. This is
what finds JS-only navigation and JS-injected content — the hint says *"not
everything a browser sees is an `<a>` tag"*, and on this site `main.js`
injects seven nav links that appear in no HTML source.

**Tier 2 — direct fetch (`fetch_all_discovered`).** Every same-origin URL
referenced *anywhere* is then fetched verbatim over plain HTTP and its raw
bytes deep-scanned. A headless browser is not a complete oracle:

- **Source maps are never requested.** A browser only fetches `app.js.map`
  with devtools open, and the reference is an *unquoted comment*
  (`//# sourceMappingURL=…`) that a quoted-string-literal regex cannot match.
- **Downloads have no readable body.** With `Content-Disposition: attachment`
  Playwright's `response.body()` raises.
- **Collapsed query-param variants.** `canonical_key()` treats `?ref=`/`?v=`
  as decoys so tier 1 doesn't render the same page fifty times; tier 2
  fetches every variant anyway, so a genuinely different one is still read.

Consequence: a crawl bound costs a page's *JS execution*, never its bytes.

### Deep decoding

`crawler/decoders.py` walks outward from the raw bytes, re-checking for a
password at every layer up to `MAX_DEPTH`, deduplicated by payload hash:

- compression: gzip, zlib, raw deflate, brotli, zip members, and
  zlib-inflating PDF `FlateDecode` streams
- transport: `Content-Encoding` (urllib does **not** auto-decompress, and
  without handling it a gzipped page yields garbage and contributes no links)
- encodings: base64 at **all four alignment offsets** plus the urlsafe
  alphabet, hex, percent-encoding
- browser-side escapes: HTML entities, `\xNN`/`\uNNNN`, `fromCharCode` arrays
- **wide text: UTF-16/32 both endiannesses, plus a null-stripping fallback**

That last one matters: EXIF `UserComment` declares a `UNICODE\0` charset and
stores UTF-16LE, so the bytes read `V\x00I\x00S\x00…` and never match a
contiguous-ASCII regex. It cost a password before it was added.

### Beyond the bytes

Some content exists only in a live browser, or only to a human:

- rendered `innerText` (plus a whitespace-stripped pass) — catches a password
  split across markup, which appears nowhere contiguously in the source
- computed `::before`/`::after` content — visible text that is in no
  element's `innerText` and no attribute
- IndexedDB, cookies/localStorage, shadow DOM
- WebSocket / SSE / `postMessage` taps installed via `add_init_script`, since
  a `text/event-stream` body never completes and network interception cannot
  see it
- **images whose pixels look like rendered text** — flagged for a human,
  never guessed at

## How I know the crawl was complete

Not "we crawled a lot" — four independent arguments, each reproducible with
`audit.py`. **The evidence is in [FINDINGS.md](FINDINGS.md#how-i-know-the-crawl-was-complete);**
in summary:

1. **Reference closure** — every referenced resource was fetched; exactly one
   URL was left, the `/report/` generator's next link.
2. **Structural census** — one rare attribute, one HTML comment, one custom
   header site-wide. All three were passwords.
3. **Linguistic closure** — only three pages contain human-written English,
   measured by function-word density against a machine-generated corpus.
4. **Content-type census** — the site serves only html/js/css/png/jpg.

What the code contributes to that argument: `results.json` reports
`unique_pages_visited` vs `unique_urls_discovered` vs `urls_directly_fetched`,
so any gap between discovery and retrieval is visible rather than implicit,
and `capped_pagination_bases` names anything a bound skipped.

### The bounds, stated as assumptions

`/report/?page=N` is an unbounded generator — walked past page 575 (check
11,500), always exactly 20 rows, never terminating. It is sampled to
`MAX_PAGINATION_DEPTH` and every sampled page is deep-scanned.

Pagination past the cap is deliberately **not** recorded at all, unlike every
other bound. It has to be: page N's body links to page N+1, so fetching a
"bounded" page re-discovers the next one and tier 2 never converges. That bug
put a real run at `/report/?page=2478` before it was fixed, and
`MAX_DIRECT_FETCH_ROUNDS` is now a structural backstop that reports what it
dropped rather than grinding silently.

## Caching: iterate without re-crawling

Every response body and every per-page browser artifact (serialized DOM,
`innerText`, `::before` content, storage, IndexedDB, console) is written to
`.crawlcache/`.

```bash
python run.py                # resume where it stopped
python run.py --rescan       # re-scan cached data — no network, no browser
python run.py --fresh        # ignore the cache
python run.py --cache-stats
```

`--rescan` is the point of the cache. Measured against the self-test site: a
live crawl took 584s; the offline rescan took **0.1s** and reproduced every
password with the server shut down. It cannot discover *new* URLs, so it
measures extraction coverage rather than crawl coverage — and its
`crawl_complete_explanation` says exactly that instead of implying a clean
bill of health.

## `python selftest.py`

"Found 6 of 8" tells you nothing about *which* capability is missing. This
serves a local mock site on `127.0.0.1:8899` (Basic Auth, like the target)
hiding twelve passwords via twelve different mechanisms — one each — then
runs the real `Crawler` against it:

```
PASS  html-comment      PASS  source-map        PASS  gzip-encoded
PASS  js-file           PASS  pdf-compressed    PASS  indexeddb
PASS  response-header   PASS  css-generated     PASS  sse-stream
PASS  deep-pagination   PASS  split-by-markup   PASS  exif-utf16
```

A failure names the broken feature. It runs in a temp directory so it never
overwrites the real output. Run it after touching `extractors.py`,
`decoders.py`, or `link_discovery.py`.

## `python audit.py`

Browser-free diagnostics — this is where the completeness evidence above
comes from.

- **`census`** (default) — threaded sweep of the whole site. Content-Type
  census, unusual-resource list, `/report/` characterisation, deep scan of
  every body, and it populates `.crawlcache/`. Writes `audit-census.json`.
- **`headers`** — HEAD-sweeps every known path, unauthenticated *and*
  authenticated. Custom headers belong to the nginx *location*, so one shows
  up even on a pre-auth 401 — which is exactly how the header password
  behaves. Writes `audit-headers.json`.
- **`images`** — saves every image, dumps metadata, runs LSB extraction, and
  flags any image whose pixels look like rendered text.

## `python geocheck.py`

Reports, in order: your exit IP, what public geo-IP databases say about it,
and **what the target site says** — the only opinion that matters.

```bash
python geocheck.py
python geocheck.py --proxy socks5://127.0.0.1:1080
```

This exists because three separate attempts at the geo-blocked page failed
for three different reasons that all *looked* like "still blocked": Safari
force-upgrading to HTTPS on an HTTP-only host, a phone VPN refusing to carry
plain HTTP to a bare IP, and a German VPN whose exit geolocated to France.
**A failure to connect and a refusal to serve are different results**, and
they are indistinguishable if you only check whether you got the page. This
tool separates them.

## Layout

```
run.py               crawl / resume / rescan
audit.py             census, header sweep, image dump
geocheck.py          geo-IP checker for the region-locked page
selftest.py          12-mechanism regression test against a local mock
crawler/
  config.py          target, credentials, bounds
  browser_crawler.py the two-tier crawler
  decoders.py        recursive multi-layer decoding
  extractors.py      password extraction from bodies, headers, images
  link_discovery.py  URL discovery incl. source maps, srcset, meta refresh
  store.py           on-disk cache of bodies + rendered artifacts
```
