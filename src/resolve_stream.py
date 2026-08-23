#!/usr/bin/env python3
"""Resolve the best playable MP4 stream URL for a prehraj.to detail page.

Prehraj.to embeds the player config inline in the detail page HTML as JS
literals — no login or JS execution needed. Each upload exposes 1–N quality
variants (typically `1080p` + `720p`). The URLs are time-limited (token +
expires param), so callers must resolve immediately before downloading; do
NOT cache across batch boundaries.

Usage:
    from resolve_stream import resolve, pick_best

    resolved = resolve("https://prehraj.to/.../68e41ad579fd1")
    best = pick_best(resolved.videos)
    download_to(best.url, best.bytes_estimate, ...)
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests

GOOGLEBOT_SMARTPHONE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 "
    "Mobile Safari/537.36 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)
GOOGLEBOT_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
    "Googlebot/2.1; +http://www.google.com/bot.html) "
    "Chrome/145.0.0.0 Safari/537.36"
)
USER_AGENT = GOOGLEBOT_SMARTPHONE_USER_AGENT
GOOGLEBOT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# Throttle for the CZ proxy. chobotnice.aspfree.cz runs on shared ASP.NET
# hosting; ~50 HTML fetches in 5 min knock the app pool into 502 mode for
# tens of minutes. Force at least RESOLVE_MIN_GAP seconds between two
# proxy GETs within the same process. The download (~3 min) + upload
# (~2 min) phases that follow a successful resolve already create plenty
# of natural spacing — the gap matters most when candidates fail
# back-to-back (e.g. dead 404 uploads) and resolve calls would otherwise
# fire seconds apart.
RESOLVE_MIN_GAP = float(os.environ.get("CZ_PROXY_MIN_GAP_SECONDS", "5"))
_last_resolve_at: float = 0.0

# Matches every `videos.push({ src: "...", type: 'video/mp4', res: '1080', label: '1080p' [, default: true] });`
# in the inline JS. We accept either single or double quotes for each value
# and ignore the order of the inner properties.
_VIDEOS_PUSH_RE = re.compile(
    r"videos\.push\(\s*\{\s*(?P<body>[^}]*?)\s*\}\s*\)\s*;",
    re.DOTALL,
)
_PROP_RE = re.compile(
    r"""(?P<key>src|type|res|label|default)\s*:\s*
        (?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>true|false))""",
    re.VERBOSE,
)
_TRACK_RE = re.compile(
    r"src\s*:\s*\"(?P<src>https?://[^\"]+\.vtt[^\"]*)\"\s*,\s*"
    r"srclang\s*:\s*\"(?P<lang>[^\"]+)\"",
    re.DOTALL,
)
_VIDEO_ID_RE = re.compile(r"'videoId'\s*:\s*(\d+)")
_VIDEO_LENGTH_RE = re.compile(r"'videoLength'\s*:\s*(\d+)")
_VIDEO_NAME_RE = re.compile(r"'name'\s*:\s*\"([^\"]+)\"")


@dataclass
class StreamVariant:
    url: str
    res: int  # 1080, 720, …
    label: str  # "1080p", "720p", …
    is_default: bool


@dataclass
class SubtitleTrack:
    url: str
    lang: str  # ISO-ish, e.g. "cs"


@dataclass
class ResolvedUpload:
    upload_url: str
    video_id: Optional[int]
    name: Optional[str]
    duration_sec: Optional[int]
    videos: list[StreamVariant]
    tracks: list[SubtitleTrack]
    fetch_via: str = "direct"


class ResolveError(Exception):
    """Raised when we can't extract player config (dead upload, proxy hiccup, …).

    `permanent` distinguishes "give up on this upload_id forever" (404, no
    videos.push blocks in valid HTML) from "try again next batch" (5xx from
    proxy, network timeout). The orchestrator records the failure
    accordingly.
    """

    def __init__(self, message: str, *, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


def _parse_video_block(body: str) -> Optional[StreamVariant]:
    props: dict[str, str | bool] = {}
    for m in _PROP_RE.finditer(body):
        val: str | bool
        if m.group("dq") is not None:
            val = m.group("dq")
        elif m.group("sq") is not None:
            val = m.group("sq")
        else:
            val = m.group("bare") == "true"
        props[m.group("key")] = val

    src = props.get("src")
    res = props.get("res")
    if not isinstance(src, str) or not isinstance(res, str):
        return None
    try:
        res_int = int(res)
    except ValueError:
        return None
    label = props.get("label")
    return StreamVariant(
        url=src,
        res=res_int,
        label=label if isinstance(label, str) else f"{res_int}p",
        is_default=props.get("default") is True,
    )


def parse_html(html: str, upload_url: str, *, fetch_via: str = "direct") -> ResolvedUpload:
    videos: list[StreamVariant] = []
    for m in _VIDEOS_PUSH_RE.finditer(html):
        v = _parse_video_block(m.group("body"))
        if v is not None:
            videos.append(v)

    if not videos:
        raise ResolveError(f"no videos.push() blocks found at {upload_url}")

    tracks = [SubtitleTrack(url=m.group("src"), lang=m.group("lang")) for m in _TRACK_RE.finditer(html)]

    vid_id = int(_VIDEO_ID_RE.search(html).group(1)) if _VIDEO_ID_RE.search(html) else None
    name_m = _VIDEO_NAME_RE.search(html)
    name = name_m.group(1) if name_m else None
    dur_m = _VIDEO_LENGTH_RE.search(html)
    duration = int(dur_m.group(1)) if dur_m else None

    return ResolvedUpload(
        upload_url=upload_url,
        video_id=vid_id,
        name=name,
        duration_sec=duration,
        videos=sorted(videos, key=lambda v: -v.res),
        tracks=tracks,
        fetch_via=fetch_via,
    )


def _split_env_list(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _cz_proxy_fetch_urls(upload_url: str) -> list[tuple[str, str]]:
    """Build CZ-proxy URLs from configured proxy env vars.

    prehraj.to website geofences datacenter / non-CZ-residential ASNs (404).
    The shared `chobotnice.aspfree.cz/Proxy.ashx` handler runs on a Czech
    ASP host and transparently relays HTML — same pattern that cr-web's
    movies_api/prehrajto.rs uses for its stream resolver.
    """
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
                f"&url={urllib.parse.quote(upload_url, safe='')}",
            )
        )
    return urls


def _googlebot_headers() -> dict[str, str]:
    """Return request headers that identify the fetch as Googlebot-like.

    Only HTTP headers can be set here. The real Googlebot source IP and
    reverse DNS identity cannot be reproduced by a local script.
    """
    headers = dict(GOOGLEBOT_HEADERS)
    if os.environ.get("GOOGLEBOT_VARIANT", "smartphone").lower() == "desktop":
        headers["User-Agent"] = GOOGLEBOT_DESKTOP_USER_AGENT
    return headers


def _localhost_fetch_url(upload_url: str) -> str | None:
    """Optionally send the resolver request to a localhost page.

    Set GOOGLEBOT_LOCALHOST_URL to an exact URL, for example
    http://localhost:3000/. The original prehraj.to URL is still used as the
    logical upload_url for parsing and logs.
    """
    url = os.environ.get("GOOGLEBOT_LOCALHOST_URL", "").strip()
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ResolveError(
            "GOOGLEBOT_LOCALHOST_URL must point to localhost, 127.0.0.1, or ::1",
            permanent=False,
        )
    return url


def resolve(
    upload_url: str,
    *,
    timeout: float = 30.0,
    session: requests.Session | None = None,
    max_retries: int = 4,
    backoff_seconds: tuple[float, ...] = (3, 10, 30, 60),
) -> ResolvedUpload:
    """Fetch the prehraj.to detail page (via CZ proxy when configured) and
    parse out the player config. Retries 5xx / network errors with
    exponential backoff so an occasional proxy blip doesn't burn a
    candidate.

    The chobotnice proxy is a shared IIS handler; under burst load it
    sometimes returns 502 Bad Gateway for ~30-90s. With 4 retries up to
    ~103 s total we cover those windows; anything longer is treated as
    a real outage and surfaces as ResolveError(permanent=False), which
    the orchestrator records as a transient failed_attempt (retry-able
    on the next batch).
    """
    global _last_resolve_at
    sess = session or requests.Session()
    sess.headers.update(_googlebot_headers())
    localhost_url = _localhost_fetch_url(upload_url)
    if localhost_url:
        fetch_urls = [("localhost", localhost_url)]
    else:
        fetch_urls = _cz_proxy_fetch_urls(upload_url) or [("direct", upload_url)]

    last_err: str = "no attempts"
    for attempt in range(max_retries + 1):
        for fetch_label, fetch_url in fetch_urls:
            via_proxy = fetch_label.startswith("cz_proxy")
            # Pace consecutive proxy GETs. The first call in the batch hits
            # `wait <= 0` and goes through immediately; later calls wait out
            # the remaining gap. Retries DO honor the gap too — if proxy is
            # in 502 mode we want to be even gentler, not hammer it.
            if via_proxy:
                wait = RESOLVE_MIN_GAP - (time.monotonic() - _last_resolve_at)
                if wait > 0:
                    time.sleep(wait)
            try:
                resp = sess.get(fetch_url, timeout=timeout, allow_redirects=True)
                if via_proxy:
                    _last_resolve_at = time.monotonic()
            except requests.RequestException as e:
                last_err = f"{fetch_label} network error: {e}"
                continue
            if resp.status_code == 404:
                # Upload truly gone — give up immediately, burn the upload_id.
                raise ResolveError(f"upload not found (404): {upload_url}", permanent=True)
            if resp.ok:
                return parse_html(resp.text, upload_url, fetch_via=fetch_label)
            if 500 <= resp.status_code < 600:
                # Diagnostic: dump response so we can tell a real gateway
                # 502 (proxy / upstream truly down — retry helps) from the
                # CZ proxy remapping a per-URL upstream error into 502
                # (dead upload — retry will never help). The proxy wraps
                # WebException into a plain-text body like
                #   "WebException: ConnectionClosed - The request was
                #    aborted: The connection was closed unexpectedly."
                #   "Upstream NotFound: …"
                # When we see that signature, this upload_id is dead.
                body_preview = (resp.text or "")[:300].replace("\n", " ")
                ct = resp.headers.get("Content-Type", "")
                print(
                    f"[resolve] {fetch_label} HTTP {resp.status_code} ct={ct!r} body[0:300]={body_preview!r}",
                    flush=True,
                )
                permanent_markers = ("Upstream NotFound", "WebException:")
                if any(m in body_preview for m in permanent_markers):
                    raise ResolveError(
                        f"HTTP {resp.status_code} wrapping permanent upstream error: {upload_url}",
                        permanent=True,
                    )
                last_err = f"{fetch_label} HTTP {resp.status_code}"
                continue
            last_err = f"{fetch_label} HTTP {resp.status_code}"

        if attempt < max_retries:
            sleep_s = backoff_seconds[attempt] if attempt < len(backoff_seconds) else backoff_seconds[-1]
            print(f"[resolve] {last_err} on {upload_url[:80]}…, retry {attempt + 1}/{max_retries} in {sleep_s:.0f}s", flush=True)
            time.sleep(sleep_s)

    raise ResolveError(f"{last_err} after {max_retries + 1} attempts: {upload_url}",
                       permanent=False)


def pick_best(videos: list[StreamVariant], *, prefer: tuple[int, ...] = (1080, 720)) -> StreamVariant:
    """Return the preferred variant: highest in `prefer` that exists, else the highest available."""
    by_res = {v.res: v for v in videos}
    for r in prefer:
        if r in by_res:
            return by_res[r]
    # Fallback: largest resolution we got. `videos` is already sorted desc by parse_html.
    if not videos:
        raise ResolveError("no variants to pick from")
    return videos[0]


def _cli() -> int:
    if len(sys.argv) < 2:
        print("usage: resolve_stream.py <prehrajto-url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    info = resolve(url)
    print(f"upload : {info.upload_url}")
    print(f"id     : {info.video_id}")
    print(f"name   : {info.name}")
    print(f"length : {info.duration_sec}s")
    print(f"variants ({len(info.videos)}):")
    for v in info.videos:
        flag = " [default]" if v.is_default else ""
        print(f"  {v.label:>6}  res={v.res}  {v.url[:96]}…{flag}")
    print(f"tracks ({len(info.tracks)}):")
    for t in info.tracks:
        print(f"  {t.lang}  {t.url[:96]}…")
    best = pick_best(info.videos)
    print(f"\nbest pick: {best.label} → {best.url[:96]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
