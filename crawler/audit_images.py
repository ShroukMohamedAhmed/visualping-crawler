"""Image mode: save every image, dump its metadata, run LSB-stego
extraction, and flag any image whose pixels look like rendered text.

Password #6 is a picture of itself: not encoded, not in metadata, not in
the LSB plane. No byte-level method can read it, so the honest output is
a pointer telling a human which file to open.
"""
from __future__ import annotations

import io
from pathlib import Path

from crawler.extractors import describe_image_metadata, find_flags_in_lsb_steganography

from . import audit_shared as shared


def _collect_image_urls() -> list[str]:
    urls = {r["url"] for r in shared.records if r["content_type"].startswith("image/")}
    if not urls:
        urls = {url for url, _, headers in shared.get_store().iter_bodies()
                if (headers or {}).get("Content-Type", "").startswith("image/")}
    return sorted(urls)


def _fetch_or_load(url: str):
    cached = shared.get_store().load_body(url)
    if cached:
        return cached
    status, body, headers = shared.fetch(url)
    return (body, headers) if status == 200 else (None, None)


def _analyse_one_image(url: str, out_dir: Path, image_cls):
    body, headers = _fetch_or_load(url)
    if body is None:
        return
    name = url.rsplit("/", 1)[-1]
    (out_dir / name).write_bytes(body)
    print(f"\n{name}: {len(body)} bytes -> {out_dir / name}")

    meta = describe_image_metadata(body)
    if meta:
        print(f"  metadata: {meta[:300]}")
    shared.scan(url, body, headers or {})
    for f in sorted(find_flags_in_lsb_steganography(body)):
        print(f"  [PASSWORD] (LSB stego) {f}")

    if image_cls is not None:
        try:
            img = image_cls.open(io.BytesIO(body))
            print(f"  {img.format} {img.size} mode={img.mode}")
        except Exception as exc:
            print(f"  ! could not read pixels: {exc}")


def mode_images():
    out_dir = Path("assets")
    out_dir.mkdir(exist_ok=True)

    urls = _collect_image_urls()
    if not urls:
        print("! No images cached — run `python audit.py census` first.")
        return

    try:
        from PIL import Image
    except ImportError:
        Image = None
        print("! Pillow missing — skipping pixel analysis")

    for url in urls:
        _analyse_one_image(url, out_dir, Image)

    print(f"\nAll images written to {out_dir.resolve()}")
    print("Open them and LOOK — a password rendered as pixels is invisible to "
          "every byte-level scan.")
