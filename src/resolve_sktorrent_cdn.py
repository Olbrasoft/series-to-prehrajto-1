#!/usr/bin/env python3
"""Resolvuje živou SK Torrent CDN edge URL.

Vstup: URL ve tvaru
    https://online.sktorrent.eu/media/videos//h264/{id}_{quality}.mp4
        nebo libovolný online{N}.sktorrent.eu hostname.
Výstup: URL s první edge nodou, která vrátí 200/206 na HEAD probe.

CDN běží na hostech online1.sktorrent.eu .. online30.sktorrent.eu;
seznam ad-hoc rotuje, takže placeholder bez čísla nefunguje.

Spuštění:
    python3 resolve_sktorrent_cdn.py <placeholder_url>
"""
import re
import sys
import urllib.request
import urllib.error

EDGE_RANGE = range(1, 31)
TIMEOUT = 8.0
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://sktorrent.eu/",
}


def candidates(url: str):
    """První zkus URL jak je (možná to byla validní edge),
    pak rotuj přes online1..30.sktorrent.eu."""
    yield url
    base = re.sub(r"https?://(online\d*\.)?sktorrent\.eu", "", url)
    for n in EDGE_RANGE:
        candidate = f"https://online{n}.sktorrent.eu{base}"
        if candidate != url:
            yield candidate


def head_ok(url: str) -> bool:
    req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status in (200, 206) and int(r.headers.get("Content-Length", "0")) > 1_000_000
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return False


def resolve(url: str) -> str | None:
    for candidate in candidates(url):
        if head_ok(candidate):
            return candidate
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Použití: {sys.argv[0]} <sktorrent_placeholder_url>", file=sys.stderr)
        return 2
    placeholder = sys.argv[1]
    print(f"[cdn] resolving {placeholder}", file=sys.stderr)
    resolved = resolve(placeholder)
    if not resolved:
        print(f"[cdn] FAILED — žádná edge nezareagovala 200/206", file=sys.stderr)
        return 1
    print(f"[cdn] resolved → {resolved}", file=sys.stderr)
    print(resolved)  # stdout pro workflow consumption
    return 0


if __name__ == "__main__":
    sys.exit(main())
