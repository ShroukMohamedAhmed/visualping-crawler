#!/usr/bin/env python3
"""Site audit — the evidence behind the completeness claims in FINDINGS.md.

Three modes, each of which produced a real result on this target:

  census   (default) Browser-free sweep of the whole site. Reports a
           Content-Type census, flags unusual resources, characterises the
           /report/?page=N generator, deep-scans every body, and caches
           every response so `run.py --rescan` can re-examine them offline.

  headers  HEAD-sweep every known path, unauthenticated AND authenticated.
           This is how we established that `X-Provisioning-Note` is the only
           custom header on the site, and that it is attached at the nginx
           location block (it appears on the pre-auth 401).

  images   Download every image, dump its metadata, run LSB-stego
           extraction, and flag any image whose pixels look like rendered
           text. Password #6 is a picture of itself, so no byte-level
           method can read it — the tool's job is to say "look at this one".

Usage:
    python audit.py                # census
    python audit.py headers
    python audit.py images
    python audit.py all
"""
from __future__ import annotations

import sys

from crawler.audit_census import mode_census
from crawler.audit_headers import mode_headers
from crawler.audit_images import mode_images

MODES = {"census": mode_census, "headers": mode_headers, "images": mode_images}


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    # An unrecognised flag must be an error, not silently ignored — otherwise
    # a typo like `--headers` (instead of `headers`) quietly runs the census.
    bad_flags = [a for a in argv if a.startswith("-")]
    if bad_flags:
        print(f"Unknown option(s): {' '.join(bad_flags)}\n"
              f"Modes are positional: census | headers | images | all\n{__doc__}")
        return 2

    requested = argv or ["census"]
    if requested == ["all"]:
        requested = ["census", "headers", "images"]
    unknown = [m for m in requested if m not in MODES]
    if unknown:
        print(f"Unknown mode(s): {', '.join(unknown)}\n{__doc__}")
        return 2
    for mode in requested:
        print(f"\n{'#' * 66}\n# {mode}\n{'#' * 66}")
        MODES[mode]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
