#!/usr/bin/env python3
"""Download a resolved prehraj.to MP4 stream URL into a local file.

Wraps `curl -fL` with prehraj.to-friendly headers (UA + Referer to avoid the
occasional 403). The CDN URLs returned by `resolve_stream` are time-limited
(~24 h), so callers should resolve immediately before invoking this.

Usage:
    from download import download_to

    size = download_to(stream_url, "/tmp/film.mp4")
"""

from __future__ import annotations

import shutil
import subprocess
import re
from pathlib import Path

import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)

# Anything smaller than this is almost certainly a CDN error page or empty
# response, not a real movie file.
MIN_FILE_SIZE = 10_000_000  # 10 MB

# GitHub-hosted runner has ~14 GB free disk. Practical experience: files
# above ~7 GB push the runner over the edge during the upload phase (file
# is kept on disk until upload completes; the multipart POST itself uses
# tmp buffers; OS + tooling already consume ~3 GB). cr_film_id=822
# (9.6 GB) crashed two runs in a row at the upload step. Cap at 6 GB so
# oversize candidates get marked permanent and we fall through to another
# candidate (most films have a 720p variant in the 1.5-3 GB range).
MAX_FILE_SIZE = 6_000_000_000  # 6 GB


class DownloadError(Exception):
    pass


def head_size(url: str, *, timeout_sec: int = 30) -> int | None:
    """Read actual stream size from headers, using one byte if HEAD omits it."""
    headers = {"User-Agent": USER_AGENT, "Referer": "https://prehraj.to/"}
    try:
        with requests.head(url, headers=headers, timeout=timeout_sec, allow_redirects=True) as response:
            response.raise_for_status()
            if response.status_code == 206:
                match = re.fullmatch(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
                if match and int(match.group(1)) > 0:
                    return int(match.group(1))
            elif response.status_code == 200:
                length = response.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > 0:
                    return int(length)
        # Never read the response body: close immediately even if the server
        # ignores Range and starts returning the entire file.
        with requests.get(url, headers={**headers, "Range": "bytes=0-0"},
                          stream=True, timeout=timeout_sec) as response:
            response.raise_for_status()
            if response.status_code == 206:
                match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
                return int(match.group(1)) if match and int(match.group(1)) > 0 else None
            if response.status_code == 200:
                length = response.headers.get("Content-Length", "")
                return int(length) if length.isdigit() and int(length) > 0 else None
    except requests.RequestException:
        pass
    return None


def download_to(
    url: str,
    dest: str | Path,
    *,
    timeout_sec: int = 3600,
    min_speed_bytes: int = 10_000,
    speed_time_sec: int = 60,
) -> int:
    """Download `url` to `dest`. Returns size in bytes. Raises DownloadError on failure.

    Uses a single curl call with HTTP Range (`bytes=0-`) — premiumcdn supports
    Range and some endpoints require it. The caller controls curl's low-speed
    guard so long-running batch jobs can skip unusably slow sources sooner.
    On non-zero exit, the partial file is removed.
    """
    if not shutil.which("curl"):
        raise DownloadError("curl not found in PATH")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "curl", "-fL", url,
        "-H", f"User-Agent: {USER_AGENT}",
        "-H", "Referer: https://prehraj.to/",
        "-H", "Range: bytes=0-",
        "--max-time", str(timeout_sec),
        "--speed-time", str(speed_time_sec), "--speed-limit", str(min_speed_bytes),
        "-s", "-S",
        "-o", str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"curl exit {proc.returncode}: {proc.stderr.strip()[:300]}")

    size = dest.stat().st_size
    if size < MIN_FILE_SIZE:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"file too small ({size} B), likely CDN error page")
    return size


def _cli() -> int:
    import sys
    if len(sys.argv) < 3:
        print("usage: download.py <url> <dest-path>", file=sys.stderr)
        return 2
    url, dest = sys.argv[1], sys.argv[2]
    size = download_to(url, dest)
    mb = size / 1_000_000
    print(f"downloaded {mb:,.1f} MB → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
