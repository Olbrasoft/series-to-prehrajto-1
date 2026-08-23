#!/usr/bin/env python3
"""Discover current prehraj.to uploads for a series episode."""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

import requests

BASE_URL = "https://prehraj.to"
SEARCH_URL = "https://prehraj.to/hledej/{query}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8,en-US;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Priority": "u=0, i",
    "Referer": "https://prehraj.to/",
    "Sec-Ch-Ua": '"Microsoft Edge";v="145", "Chromium";v="145", "Not A(Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_last_search_at = 0.0

_ITEM_RE = re.compile(
    r'<div class="video-wrapper">(?P<body>.*?)(?=<div class="video-wrapper">|</main>|$)',
    re.DOTALL,
)
_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*\bvideo--link\b[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]+title="(?P<title>[^"]+)"',
    re.DOTALL,
)
_DURATION_RE = re.compile(r'video__tag--time">\s*(?P<duration>\d{1,2}:\d{2}(?::\d{2})?)\s*<')
_SIZE_RE = re.compile(r'video__tag--size[^"]*">\s*(?P<size>[0-9.,]+)\s*(?P<unit>[MG]B)\s*<', re.IGNORECASE)
_FORMAT_RE = re.compile(r'format__text">\s*(?P<format>[^<]+)\s*<', re.IGNORECASE)


@dataclass(frozen=True)
class SearchResult:
    source_id: int
    external_id: str
    url: str
    title: str
    duration_sec: int | None
    resolution_hint: str | None
    filesize_bytes: int | None


def synthetic_source_id(external_id: str) -> int:
    digest = hashlib.sha256(f"prehrajto:{external_id}".encode()).digest()
    return -int.from_bytes(digest[:7], "big")


def duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def filesize_bytes(value: str | None, unit: str | None) -> int | None:
    if not value or not unit:
        return None
    number = float(value.replace(",", "."))
    multiplier = 1024**3 if unit.upper() == "GB" else 1024**2
    return int(number * multiplier)


def parse_search_html(page_html: str) -> list[SearchResult]:
    rows: list[SearchResult] = []
    seen: set[str] = set()
    for item in _ITEM_RE.finditer(page_html):
        body = item.group("body")
        link = _LINK_RE.search(body)
        if not link:
            continue
        href = html.unescape(link.group("href"))
        external_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[0-9a-fA-F]{12,32}", external_id) or external_id in seen:
            continue
        duration_match = _DURATION_RE.search(body)
        size_match = _SIZE_RE.search(body)
        format_match = _FORMAT_RE.search(body)
        rows.append(
            SearchResult(
                source_id=synthetic_source_id(external_id),
                external_id=external_id,
                url=urllib.parse.urljoin("https://prehraj.to", href),
                title=html.unescape(link.group("title")).strip(),
                duration_sec=duration_seconds(duration_match.group("duration") if duration_match else None),
                resolution_hint=(format_match.group("format").strip() if format_match else None),
                filesize_bytes=filesize_bytes(
                    size_match.group("size") if size_match else None,
                    size_match.group("unit") if size_match else None,
                ),
            )
        )
        seen.add(external_id)
    return rows


def next_page_url(page_html: str, current_url: str) -> str | None:
    match = re.search(
        r'<div[^>]+id="snippet-videoListing-paginatorWrapper"[^>]*>(?P<body>.*?)</div>',
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    body = match.group("body")
    links: list[tuple[str, str, str]] = []
    for link in re.finditer(r'<a\b(?P<attrs>[^>]*)href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', body, re.IGNORECASE | re.DOTALL):
        attrs = link.group("attrs")
        href = html.unescape(link.group("href"))
        text = re.sub(r"<.*?>", "", link.group("text")).strip()
        links.append((attrs, text, href))
    for attrs, _text, href in links:
        if re.search(r'\brel=["\']?next\b', attrs, re.IGNORECASE):
            return urllib.parse.urljoin(BASE_URL, href)
    for _attrs, text, href in links:
        if text in {"2", "›", "»", "Další"}:
            return urllib.parse.urljoin(BASE_URL, href)
    current = urllib.parse.urlparse(current_url)
    for _attrs, _text, href in links:
        absolute = urllib.parse.urljoin(BASE_URL, href)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.path == current.path and absolute != current_url:
            return absolute
    return None


def _split_env_list(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _proxy_fetch_urls(search_url: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str, str]] = []
    base = os.environ.get("CZ_PROXY_URL", "").strip()
    key = os.environ.get("CZ_PROXY_KEY", "").strip()
    if base and key:
        pairs.append(("cz_proxy_1", base, key))
    base = os.environ.get("CZ_PROXY_URL_2", "").strip()
    key = os.environ.get("CZ_PROXY_KEY_2", "").strip()
    if base and key:
        pairs.append(("cz_proxy_2", base, key))
    for index, (base, key) in enumerate(zip(_split_env_list("CZ_PROXY_URLS"), _split_env_list("CZ_PROXY_KEYS")), start=1):
        if base and key:
            pairs.append((f"cz_proxy_list_{index}", base, key))

    seen: set[tuple[str, str]] = set()
    urls: list[tuple[str, str]] = []
    for label, base, key in pairs:
        pair_key = (base, key)
        if pair_key in seen:
            continue
        seen.add(pair_key)
        urls.append(
            (
                label,
                f"{base}?key={urllib.parse.quote(key, safe='')}"
                f"&url={urllib.parse.quote(search_url, safe='')}",
            )
        )
    return urls


def search(
    query: str,
    *,
    timeout: float = 30.0,
    min_interval: float = 3.0,
    retries: int = 3,
    session: requests.Session | None = None,
) -> list[SearchResult]:
    pages = search_pages(
        query,
        timeout=timeout,
        min_interval=min_interval,
        retries=retries,
        session=session,
        max_pages=1,
    )
    return pages[0] if pages else []


def search_pages(
    query: str,
    *,
    timeout: float = 30.0,
    min_interval: float = 3.0,
    retries: int = 3,
    session: requests.Session | None = None,
    max_pages: int = 2,
    should_fetch_next: Callable[[list[SearchResult]], bool] | None = None,
) -> list[list[SearchResult]]:
    """Fetch search results page by page.

    Callers can inspect page 1 first and request page 2 only when the first
    page has no usable candidate. The function itself never fetches more than
    max_pages.
    """
    global _last_search_at
    sess = session or requests.Session()
    min_interval = max(min_interval, float(os.environ.get("PREHRAJTO_SEARCH_MIN_INTERVAL", "0") or 0))
    search_url = SEARCH_URL.format(query=urllib.parse.quote(query))
    fetch_urls = _proxy_fetch_urls(search_url)
    if fetch_urls:
        min_interval = max(
            min_interval,
            float(os.environ.get("CZ_PROXY_MIN_GAP_SECONDS", "5")),
        )
    else:
        fetch_urls = [("direct", search_url)]
    pages: list[list[SearchResult]] = []
    next_url: str | None = search_url
    for page_index in range(max(max_pages, 1)):
        if not next_url:
            break
        page_fetch_urls = fetch_urls if page_index == 0 else (_proxy_fetch_urls(next_url) or [("direct", next_url)])
        response: requests.Response | None = None
        for attempt in range(max(retries, 1)):
            for _fetch_label, fetch_url in page_fetch_urls:
                wait = min_interval - (time.monotonic() - _last_search_at)
                if wait > 0:
                    time.sleep(wait)
                try:
                    response = sess.get(
                        fetch_url,
                        timeout=timeout,
                        headers=BROWSER_HEADERS,
                    )
                finally:
                    _last_search_at = time.monotonic()
                if response.ok:
                    results = parse_search_html(response.text)
                    pages.append(results)
                    candidate_next_url = next_page_url(response.text, response.url)
                    next_url = (
                        candidate_next_url
                        if candidate_next_url and (should_fetch_next is None or should_fetch_next(results))
                        else None
                    )
                    break
                if response.status_code != 429 or attempt + 1 >= retries:
                    continue
            if response is not None and response.ok:
                break
            retry_after = response.headers.get("Retry-After") if response is not None else None
            try:
                retry_seconds = float(retry_after) if retry_after else 0.0
            except ValueError:
                retry_seconds = 0.0
            time.sleep(max(retry_seconds, 10.0 * (attempt + 1)))
        else:
            if response is not None:
                response.raise_for_status()
            raise RuntimeError(f"Search failed without a response for {query!r}")
    return pages
