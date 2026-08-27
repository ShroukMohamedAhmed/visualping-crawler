# Findings — Visualping crawler challenge

**All 8 passwords recovered.**

| # | Password | Location | How it was hidden |
| --- | --- | --- | --- |
| 1 | `VISUALPING{2dd5105a3fad0ef3}` | `/notes/diff-socket-socket/` | HTML comment |
| 2 | `VISUALPING{349a583fba34c301}` | `/static/js/analytics.js` | JS string literal (`ADMIN_PASSWORD`) |
| 3 | `VISUALPING{73c8f3073fdc5f74}` | `/wiki/detect-embed/` | `data-vp-archive` attribute on `<body>` |
| 4 | `VISUALPING{fb725e1f3d6728b1}` | `/static/js/theme-switcher.js` | char-code array → `String.fromCharCode` |
| 5 | `VISUALPING{db7e533a9cef7f72}` | `/static/img/field-visit.jpg` | EXIF `UserComment`, stored **UTF-16LE** |
| 6 | `VISUALPING{e1c2e40cf01c17cc}` | `/static/img/whiteboard-scan.png` | **drawn into the pixels as visible text** |
| 7 | `VISUALPING{64d26185a2f94e34}` | `/products/filter-gateway/` | `X-Provisioning-Note` response header |
| 8 | `VISUALPING{5488187886a5755a}` | `/status/eu-region/` | plain text, served **only to a German IP** |

Eight distinct mechanisms, no repeats. Six were extracted automatically; #6
and #8 cannot be, by construction.

## The two the crawler can't extract

**#6 is a picture of the answer.** Not encoded — not in the bytes, not in
metadata, not in the LSB plane. Only OCR or a human eye can read it. The
correct behaviour is to flag it rather than report a false negative, so
`describe_probable_text_image()` detects images that are mostly light with a
little dark ink and prints *"OPEN AND READ IT"*. It fires on synthetic
text-on-white and stays silent on this site's decoy gradients.

**#8 depends on who is asking.** From a US IP the page is a 403 (*"only
visible to Germany region"*); from a German IP it returns the password as
plain text in a `<pre><code>` block. Ruled out before resorting to egress
geography:

- **22 request variations** — `X-Forwarded-For`, `X-Real-IP`, `CF-IPCountry`,
  RFC 7239 `Forwarded`, `X-Geo-Country`, cookies, `Referer`,
  `?region=`/`?country=`/`?geo=`, and others. All 403.
- **Protocol level** — 5 Host headers, 12 path spellings (`//`, `/./`, `%2f`,
  `/a/../`, case, `index.html`), HTTPS. All 403 or 404.
- **403 body byte-identical across requests** → a static template, not a
  handler that might leak.

Retrieved via a Düsseldorf exit (`62.141.35.237`), confirmed as Germany by
`ip-api.com` and `ipwho.is`.

Worth recording: three attempts failed *before* the geo-check ever ran —
Safari force-upgrading to HTTPS on an HTTP-only host, a phone VPN refusing to
carry plain HTTP to a bare IP, and a German VPN whose exit geolocated to
France. **A failure to connect and a refusal to serve look identical if you
only check whether you got the page.** `geocheck.py` separates them by
reporting the exit IP, what geo-IP databases say, and what the site says.

## The trap

The homepage rules state that header passwords are *"staging placeholders...
not qualified — ignore them"*. An inline script deletes that rule from the DOM
on load:

```js
if (items.length >= 4) items[3].remove();   // exactly the header rule
```

So a **browser user never sees it; only someone reading the HTML source
does** — aimed squarely at the kind of tool this challenge asks you to build.
I fell for it and under-reported for several rounds.

**#7 is counted** because the homepage itself says *"the authoritative
instructions are in your invite email... the email wins"*, and the email
contains no such disqualification — and because the header is served on the
**pre-auth 401**, i.e. deliberately placed at the nginx location block rather
than left behind by accident.

The general lesson: a regex for `VISUALPING{...}` will never surface an
instruction *about* passwords.

## How I know the crawl was complete

Four independent arguments, all reproducible via `audit.py`.

**Reference closure.** Diffing every `href`/`src`/`srcset`/`url()`/`action`/
`poster` reference across all cached bodies against the fetch log leaves
exactly **one** unfetched URL: `/report/?page=301`, the generator's next link.
No `data:` URIs, no off-origin references.

**Structural census** over 1020 URLs / 678 distinct bodies (662 HTML):
exactly one rare attribute (`data-vp-archive` — #3), exactly one distinct HTML
comment site-wide (#1), and exactly one custom response header (#7) across a
**753-path HEAD sweep** run both authenticated and not. Everything else is
nginx boilerplate or uniform template text. All 7 JS-injected `MENU` paths
from `main.js` — which appear in no HTML source — were crawled.

**Linguistic closure.** The site's prose is machine-generated from a fixed
231-word vocabulary. By function-word density over each page's `<main>`, only
three pages contain human-written English: `/status/eu-region/` (0.375), and
`/` + `/index.html` (0.316). Everything else scores 0.000–0.012 — a **26× gap
with nothing in between**. This is what identified where #8 had to be, before
it was retrievable: a password must be placed by a human, and only two pages
have human writing.

**Content-type census.** Only html/js/css/png/jpg exist. No PDFs, fonts,
source maps, JSON or event streams — so those decoders are defensive
coverage, not evidence of a find. Also structurally absent: zero
`Set-Cookie`, zero `<form>`, one `<meta>` type, no SVG, no favicon.

## Dead ends

**The four 48×48 PNGs are algorithmic decoys.** They compress to 89% of raw
pixel size where a real gradient compresses to ~4%, which looks exactly like
steganography. It isn't — every pixel comes from a closed formula, verified
exact across all 2304 pixels with zero deviations:

```
R = (27 + 5x) mod 256    G = (34 + 5y) mod 256    B = (152 + 3*(x XOR y)) mod 256
```

The XOR term makes the blue channel incompressible while still looking like a
smooth gradient, which is why those two observations seemed to conflict. Also
brute-forced before that was understood: 24 LSB conventions, raw pixel bytes
as ASCII in 6 orderings, gradient residuals, pairwise and 4-way XOR, plus
visual inspection at 12×. All negative.

**Three bare 16-hex strings** sit in the JPEG COM segments
(`5a6b01d97bfffdc3`, `622ee9dfa76d54a6`, `e19cd3432599af6f`) with no
`VISUALPING{}` wrapper. `field-visit.jpg` carries both one of these *and* a
correctly-wrapped password, which is good evidence they're decoys. Reported
under `bare_hex16_candidates` rather than counted — auto-promoting a guess is
worse than surfacing it.

**`/report/?page=N` is an unbounded generator.** Walked past page 575 (check
11,500); always exactly 20 rows drawn from 90 recycled URLs. Sampled to
`MAX_PAGINATION_DEPTH`, every sampled page deep-scanned, no password. A
hypothesis that page 300 was the end — it reports "checks 5981–6000", and
6000/20 = exactly 300 — was tested and disproved.

**Nothing outside the link graph.** `/favicon.ico`, `robots.txt`,
`sitemap.xml`, `manifest.json`, feeds, `/.well-known/`, `/.git/HEAD` — all
404. Content negotiation (`Accept: json/plain/xml`, `X-Requested-With`)
returns byte-identical responses. Error and redirect bodies are stock nginx.

## Bugs this exposed in the crawler

1. **EXIF UTF-16.** The README claimed EXIF was covered because
   `UserComment` "is plain ASCII inside the file". False — the `UNICODE\0`
   charset means UTF-16LE, so the bytes read `V\x00I\x00S\x00…` and never
   matched a contiguous-ASCII regex. Cost password #5. Fixed with UTF-16/32
   both endiannesses plus a null-stripping fallback, since a binary EXIF
   header leaves the wide text unaligned.

2. **Pixels as text.** #6 was never encoded. The crawler now flags
   text-shaped images for human review instead of passing over them silently.

3. **Self-confirming bounds.** The crawler sampled `/report/` to depth 300 and
   concluded "no end in sight" from the last page *inside its own bound* —
   right answer, invalid reasoning. Bounds must be reported as assumptions,
   never as findings.

4. **A bound that defeated itself.** "A bound costs a render, never the bytes"
   holds everywhere except an unbounded chain: page N links to page N+1, so
   fetching a bounded page re-discovers the next one and the direct-fetch tier
   never converges. A real run reached `/report/?page=2478` before this was
   caught. Pagination past the cap is now not recorded at all, with
   `MAX_DIRECT_FETCH_ROUNDS` as a structural backstop.

5. **Reading bytes instead of prose.** The disqualification rule was in plain
   English on the homepage from the very first fetch.

All five are covered by `selftest.py`, which verifies 12 hiding mechanisms
end-to-end against a local mock site.
