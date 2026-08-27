"""Find every URL a page references — not just <a href> — plus every
element that looks clickable but might only navigate via JS (onclick).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

# Any attribute across any tag that commonly carries a resource/navigation URL
URL_ATTRS = [
    "href", "src", "data-src", "action", "poster", "data-href",
    # less common but all legitimately navigable/fetchable
    "data-url", "data-link", "data", "cite", "formaction", "longdesc",
    "codebase", "background", "manifest", "ping",
]

CSS_URL_RE = re.compile(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)')
JS_LOCATION_RE = re.compile(
    r'''(?:location(?:\.href)?|window\.location(?:\.href)?)\s*=\s*['"]([^'"]+)['"]'''
)

# `//# sourceMappingURL=app.js.map` (or the older `//@`, or a /*...*/ block).
# Deliberately NOT quoted — which is exactly why JS_PATH_LITERAL_RE below
# can never match it. Browsers only fetch source maps when devtools is
# open, so a `.map` file is invisible to both static scraping AND to
# network interception in a headless browser: it has to be requested
# explicitly. A `.map` also embeds the ORIGINAL pre-minification sources
# in its `sourcesContent` array, so anything stripped out by a minifier
# (comments included) is still sitting in there.
SOURCEMAP_RE = re.compile(
    r'''(?://|/\*)[#@]\s*sourceMappingURL\s*=\s*([^\s'"*]+)'''
)

# <meta http-equiv="refresh" content="0; url=/somewhere">
META_REFRESH_RE = re.compile(r'''url\s*=\s*['"]?([^'";]+)''', re.I)

# Broad sweep for any quoted string in JS source that looks like a site
# path or a full URL on the target — catches links built dynamically
# (e.g. `fetch('/api/x')`, `el.href = '/page3'`, values pushed into an
# array and rendered later) that a narrower regex would miss.
JS_PATH_LITERAL_RE = re.compile(
    r'''['"](/[a-zA-Z0-9_\-./%]+|https?://[^\s'"]+)['"]'''
)


def extract_js_urls(js_text: str, base_url: str) -> set[str]:
    urls = set()
    for m in JS_PATH_LITERAL_RE.finditer(js_text):
        candidate = m.group(1)
        # skip obvious non-navigable noise (mime types, tiny fragments, etc.)
        if len(candidate) < 2:
            continue
        urls.add(urljoin(base_url, candidate))
    # source maps: unquoted, so the literal sweep above misses them entirely
    for m in SOURCEMAP_RE.finditer(js_text):
        urls.add(urljoin(base_url, m.group(1)))
    return urls


def extract_sourcemap_urls(text: str, base_url: str) -> set[str]:
    """Source-map references from any text body (JS *or* CSS — both
    support the `sourceMappingURL` comment convention)."""
    return {urljoin(base_url, m.group(1)) for m in SOURCEMAP_RE.finditer(text)}


def extract_srcset_urls(value: str, base_url: str) -> set[str]:
    """`srcset="a.png 1x, b@2x.png 2x"` holds multiple URLs each followed
    by a density/width descriptor, so it needs splitting rather than being
    treated as one URL. Responsive variants of an image are separate files
    and can hold separate metadata.
    """
    urls = set()
    for part in value.split(","):
        candidate = part.strip().split()
        if candidate:
            urls.add(urljoin(base_url, candidate[0]))
    return urls


def extract_resource_urls(html: str, base_url: str) -> set[str]:
    """All absolute URLs referenced by the page: links, scripts, images,
    stylesheets, iframes, forms, inline CSS url(...), and inline JS
    location redirects.
    """
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    for tag in soup.find_all(True):
        for attr in URL_ATTRS:
            val = tag.get(attr)
            if val:
                if isinstance(val, list):  # e.g. rel/ping can parse as a list
                    val = " ".join(val)
                urls.add(urljoin(base_url, val))
        # srcset / data-srcset hold several comma-separated URLs
        for attr in ("srcset", "data-srcset", "imagesrcset"):
            val = tag.get(attr)
            if val:
                urls |= extract_srcset_urls(val, base_url)
        # <meta http-equiv="refresh" content="0; url=...">  is a real
        # navigation a browser follows, with no <a> tag anywhere
        if (tag.get("http-equiv") or "").lower() == "refresh":
            content = tag.get("content") or ""
            for m in META_REFRESH_RE.finditer(content):
                urls.add(urljoin(base_url, m.group(1).strip()))
        # any other data-* attribute whose value looks like a site path
        for attr, val in (tag.attrs or {}).items():
            if not attr.startswith("data-") or not isinstance(val, str):
                continue
            if val.startswith(("/", "./", "../")) or val.startswith("http"):
                urls.add(urljoin(base_url, val))
        # inline style="...url(...)..."
        style = tag.get("style")
        if style:
            for m in CSS_URL_RE.finditer(style):
                urls.add(urljoin(base_url, m.group(1)))
        # inline onclick="location.href='...'" style navigation
        onclick = tag.get("onclick")
        if onclick:
            for m in JS_LOCATION_RE.finditer(onclick):
                urls.add(urljoin(base_url, m.group(1)))

    # <style>...</style> blocks
    for style_tag in soup.find_all("style"):
        for m in CSS_URL_RE.finditer(style_tag.get_text()):
            urls.add(urljoin(base_url, m.group(1)))

    # inline <script> bodies: path literals and sourceMappingURL comments
    for script_tag in soup.find_all("script"):
        body = script_tag.get_text() or ""
        if body.strip():
            urls |= extract_js_urls(body, base_url)

    return urls


def extract_css_urls(css_text: str, base_url: str) -> set[str]:
    urls = {urljoin(base_url, m.group(1)) for m in CSS_URL_RE.finditer(css_text)}
    urls |= extract_sourcemap_urls(css_text, base_url)
    return urls


def same_origin(url: str, origin_netloc: str) -> bool:
    return urlparse(url).netloc == origin_netloc


CLICKABLE_SELECTOR = (
    "a, button, [onclick], "
    "[role=button], [role=tab], [role=link], [role=menuitem], [role=option], "
    "input[type=button], input[type=submit], "
    ".btn, .link, .tab, [data-href], [data-tab], [aria-controls], [tabindex]"
)

# Query-string params observed to be decoy/tracking noise on the target
# (they generate endless URL variants of the exact same page content:
# ?hl=en, ?ref=nav, ?ref=related, ?utm_source=internal, ?utm_source=sidebar,
# ?v=1..9, etc.). We ignore these when deciding what to spend a full
# *browser render* on, so the crawl doesn't burn its budget on copies of
# the same page.
#
# IMPORTANT: collapsing these is only safe because every discovered URL is
# ALSO fetched verbatim — query string intact — by the cheap direct-fetch
# pass in browser_crawler.fetch_all_discovered(), which scans the exact
# bytes and re-extracts links. So if a `?v=2` variant genuinely differs, a
# flag in it is still found; we just don't pay to render it twice. Guessing
# which params are decoys is the kind of assumption that silently loses a
# page, so it must not be the only line of defence.
IGNORED_QUERY_PARAMS = {"hl", "ref", "utm_source", "utm_medium", "utm_campaign", "v"}


def canonical_key(url: str) -> str:
    """Stable dedup key for a URL: strips fragment, collapses
    /path, /path/, and /path/index.html to the same thing, and drops
    known decoy tracking query params while preserving order/values of
    any other (potentially meaningful) query params.
    """
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/"

    kept_params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in IGNORED_QUERY_PARAMS
    ]
    query = urlencode(kept_params)

    return f"{parsed.netloc}{path}" + (f"?{query}" if query else "")


def top_level_section(url: str) -> str:
    """First path segment, e.g. '/blog/foo/bar' -> 'blog'. Used to bucket
    pages for per-section trap-bounding.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0] if parts else ""


def pagination_param_value(url: str, param_names: set[str]):
    """If the URL has a numeric query param whose name looks like a
    pagination/offset control (page, p, offset, start), return its
    integer value. Otherwise None.
    """
    parsed = urlparse(url)
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in param_names:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None
