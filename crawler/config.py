"""Configuration for the Visualping crawler challenge."""

# The task gives a Google "click-through" redirect wrapper URL. The real
# target is the `q=` query param. We resolve this automatically in
# run.py, but it's spelled out here for clarity:
#
#   https://www.google.com/url?q=http://54.214.7.161/&source=gmail&...
#                              ^^^^^^^^^^^^^^^^^^^^^^
#                              -> http://54.214.7.161/
REDIRECT_URL = (
    "https://www.google.com/url?q=http://54.214.7.161/"
    "&source=gmail&ust=1787342603249000&sa=E"
)
START_URL = "http://54.214.7.161/"

USERNAME = "shrouk.wally"
PASSWORD = "c248f3ed233997536c7d"  # HTTP Basic Auth password for the site (not one of the flags)

# Regex for the flags we're hunting: VISUALPING{ + 16 hex chars + }
import re  # noqa: E402

FLAG_RE = re.compile(rb"VISUALPING\{[0-9a-fA-F]{16}\}")

EXPECTED_COUNT = 8

# The task's own worked example. It is explicitly NOT one of the eight
# real flags, so we filter it out of results even though the crawler
# will legitimately find it printed somewhere on the site as a decoy.
KNOWN_EXAMPLE_FLAG = "VISUALPING{0000deadbeef0000}"

# Safety valves so a bug can't spin the crawler forever
# Soft warning threshold only — NOT a hard stop. The crawl terminates
# purely when the queue is empty (true BFS completion over a finite,
# deduplicated same-origin URL space, per canonical_key()). If the visited
# count ever passes this number we print a warning so you can notice and
# manually interrupt (Ctrl+C) if something is clearly generating unbounded
# URLs — but the script will not stop itself.
WARN_THRESHOLD_PAGES = 500
MAX_CLICK_TARGETS_PER_PAGE = 60
NAV_TIMEOUT_MS = 15_000

# --- direct-fetch tier -----------------------------------------------------
# Every discovered URL is also fetched verbatim over plain HTTP, outside the
# browser. This is cheap (no rendering) and is the only way to read:
#   - `.map` source maps (a browser only requests these with devtools open)
#   - PDFs / Content-Disposition: attachment responses (Playwright's
#     response.body() raises on downloads)
#   - query-param variants that canonical_key() collapsed for rendering
DIRECT_FETCH_TIMEOUT_S = 20
MAX_DIRECT_FETCH_BYTES = 16 * 1024 * 1024
# The direct-fetch tier loops until no new URLs appear. On a site with an
# unbounded ?page=N generator that never happens, so this caps the rounds.
# Hit on this target: /report/ was still climbing at page 2478.
MAX_DIRECT_FETCH_ROUNDS = 12

DIRECT_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# --- Deliberate, documented bounds on the two unbounded-generator shapes
# --- observed on this site. These are NOT arbitrary global page caps —
# --- each targets a specific structural pattern and is logged when it
# --- fires, so completeness claims stay honest (see README).
#
# NOTE: these bounds now only ever skip a *browser render*. Any URL a bound
# declines is still recorded in Crawler.discovered_urls and still fetched +
# deep-scanned by the direct-fetch tier, so a bound can no longer cost us a
# flag — only the JS-execution/click-discovery of that one page. The limits
# below are correspondingly much looser than before: the previous
# MAX_PAGINATION_DEPTH of 15 truncated the /report/ series, which is exactly
# the kind of silent coverage hole that made an 8-flag run come back with 4.

# 1) Numeric pagination-style query params (?page=N, ?p=N, ?offset=N,
#    ?start=N) that just keep incrementing.
PAGINATION_PARAM_NAMES = {"page", "p", "offset", "start"}
MAX_PAGINATION_DEPTH = 250

# 2) Per-top-level-section visit caps for sections whose slugs look
#    combinatorially generated (multi-word-hyphenated, ever-growing).
#    Once a section hits this many visited pages with no NEW flag found
#    in the most recent window, we stop enqueuing further pages from it.
MAX_PAGES_PER_SECTION = 1000
SECTION_STALL_WINDOW = 400  # pages with no new flag before we stop expanding

# How often (in pages visited) to flush results.json to disk, so an
# interrupted run still leaves usable output.
CHECKPOINT_EVERY = 5
