#!/usr/bin/env python3
"""Check what country the target site thinks you're in — fast.

Use this to hop between VPN servers without re-running the whole probe.
It prints, in order:

  1. your current exit IP
  2. what public geo-IP databases say about it
  3. what the TARGET SITE says (the only opinion that actually matters)

A VPN's own label ("Germany Düsseldorf") is marketing; what counts is how
the site's geolocation database maps the exit IP. Those disagree often,
which is exactly the situation this tool is for.

Usage:
    python geocheck.py                                  # direct
    python geocheck.py --proxy socks5://10.64.0.1:1080  # via SOCKS
"""
from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from crawler import config

TARGET = urllib.parse.urljoin(config.START_URL, "/status/eu-region/")
AUTH = "Basic " + base64.b64encode(
    f"{config.USERNAME}:{config.PASSWORD}".encode()).decode()
FLAG = re.compile(r"VISUALPING\{[0-9a-fA-F]{16}\}")


def setup_proxy(proxy_url: str):
    parsed = urllib.parse.urlparse(proxy_url)
    if parsed.scheme.startswith("socks"):
        try:
            import socket
            import socks
        except ImportError:
            print("! SOCKS proxy needs PySocks:  pip install pysocks")
            raise SystemExit(2)
        socks.set_default_proxy(
            socks.SOCKS5 if "5" in parsed.scheme else socks.SOCKS4,
            parsed.hostname, parsed.port, rdns=True)
        socket.socket = socks.socksocket
    else:
        urllib.request.install_opener(urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url,
                                         "https": proxy_url})))
    print(f"routing through {proxy_url}")


def get(url: str, auth: bool = False, timeout: int = 20):
    headers = {"User-Agent": "curl/8"}
    if auth:
        headers["Authorization"] = AUTH
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}".encode()


def main():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--proxy" and i + 1 < len(argv):
            setup_proxy(argv[i + 1])
        elif a.startswith("--proxy="):
            setup_proxy(a.split("=", 1)[1])

    print("\n1. Your exit IP")
    ip = None
    for svc in ("http://checkip.amazonaws.com", "http://ifconfig.me/ip",
                "http://api.ipify.org"):
        status, body = get(svc, timeout=12)
        if status == 200 and body.strip():
            ip = body.decode(errors="replace").strip()
            print(f"   {ip}   (via {svc})")
            break
    if not ip:
        print("   ! could not determine exit IP — is the tunnel up?")

    print("\n2. What public geo-IP databases say")
    if ip:
        for svc, path in (("ip-api.com", f"http://ip-api.com/json/{ip}"),
                          ("ipwho.is", f"http://ipwho.is/{ip}")):
            status, body = get(path, timeout=12)
            if status == 200:
                try:
                    d = json.loads(body)
                    country = d.get("country") or d.get("country_name") or "?"
                    city = d.get("city") or "?"
                    isp = d.get("isp") or d.get("connection", {}).get("isp", "?")
                    print(f"   {svc:12} {country} / {city}   ({isp})")
                except Exception:
                    print(f"   {svc:12} (unparseable response)")
            else:
                print(f"   {svc:12} unreachable")

    print("\n3. What the TARGET SITE says  <-- the one that matters")
    status, body = get(TARGET, auth=True)
    text = body.decode("utf-8", errors="replace")
    print(f"   HTTP {status}")

    m = re.search(r"Your IP is from ([^.<]+)", text)
    if m:
        print(f"   site sees you in: {m.group(1).strip()}")
    only = re.search(r"only visible to ([^.<]+)", text)
    if only:
        print(f"   site requires:    {only.group(1).strip()}")

    found = FLAG.findall(text)
    print()
    if found:
        for f in sorted(set(found)):
            print(f"   *** PASSWORD: {f}")
        with open("eu-region.html", "w") as fh:
            fh.write(text)
        print("   full page saved to eu-region.html")
    elif status == 200:
        print("   HTTP 200 but no password string — dumping the page:")
        print("   " + "-" * 64)
        print(text[:2000])
    elif status == 403:
        print("   still blocked. Try another German server (Frankfurt is the")
        print("   most common German VPN location and is usually geolocated")
        print("   correctly), then re-run this.")
    else:
        print(f"   unexpected response: {text[:300]!r}")


if __name__ == "__main__":
    main()
