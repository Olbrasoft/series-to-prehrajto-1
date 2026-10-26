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
import tempfile
import time
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


def transfer_headers(path: Path) -> tuple[int, dict[str, str]]:
    """Read the last response, excluding redirects and proxy handshakes."""
    status, headers = 0, {}
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                raise DownloadError("invalid download response status")
            status, headers = int(parts[1]), {}
        elif ":" in line:
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
    return status, headers


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
    expected_size: int | None = None,
) -> int:
    """Download `url` to `dest`. Returns size in bytes. Raises DownloadError on failure.

    Resume premature EOF/network failures up to twice within one time budget.
    Validate resumed ranges, total length and available resource validators.
    HTTP errors, slow transfers and invalid resumes fail without uploading a
    partial file. The caller controls the low-speed guard.
    """
    if not shutil.which("curl"):
        raise DownloadError("curl not found in PATH")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout_sec
    validator = None
    total = expected_size
    try:
        with tempfile.TemporaryDirectory(prefix="prehrajto-download-") as directory:
            header_path = Path(directory) / "response.headers"
            for attempt in range(3):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DownloadError("download time budget exhausted")
                offset = dest.stat().st_size if dest.exists() else 0
                header_path.write_text("")
                cmd = ["curl", "-fL", url, "-H", f"User-Agent: {USER_AGENT}",
                       "-H", "Referer: https://prehraj.to/", "--max-time", str(remaining),
                       "--speed-time", str(speed_time_sec), "--speed-limit", str(min_speed_bytes),
                       "--dump-header", str(header_path), "-s", "-S", "-o", str(dest)]
                if offset:
                    cmd += ["--continue-at", str(offset)]
                    if validator:
                        cmd += ["-H", f"If-Range: {validator}"]
                else:
                    cmd += ["-H", "Range: bytes=0-"]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining + 5)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    raise DownloadError(f"download process failed: {type(exc).__name__}") from exc
                status, headers = transfer_headers(header_path)
                current_validator = headers.get("etag")
                if current_validator and current_validator.startswith("W/"):
                    current_validator = None
                current_validator = current_validator or headers.get("last-modified")
                if validator and current_validator and current_validator != validator:
                    raise DownloadError("resource changed during download")
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
                    if not match or int(match[1]) != offset or not offset <= int(match[2]) < int(match[3]):
                        raise DownloadError("invalid resumed byte range")
                    response_total = int(match[3])
                elif status == 200 and not offset:
                    length = headers.get("content-length", "")
                    response_total = int(length) if length.isdigit() else None
                elif offset and status:
                    raise DownloadError("server did not honor download resume")
                else:
                    response_total = None
                if response_total is not None:
                    if total is not None and response_total != total:
                        raise DownloadError("resource length changed during download")
                    total = response_total
                validator = validator or current_validator
                size = dest.stat().st_size if dest.exists() else 0
                if total is not None and size > total:
                    raise DownloadError("download exceeds expected resource length")
                if proc.returncode == 0:
                    if total is not None and size != total:
                        raise DownloadError("incomplete resource after successful transfer")
                    if size < MIN_FILE_SIZE:
                        raise DownloadError(f"file too small ({size} B), likely CDN error page")
                    return size
                if proc.returncode not in {7, 18, 35, 52, 55, 56} or attempt == 2:
                    message = re.sub(r"https?://\S+", "[redacted URL]", proc.stderr.strip())
                    raise DownloadError(f"curl exit {proc.returncode}: {message[:300]}")
                # Preserve received bytes; restarting a large stream repeats the
                # same failing prefix and wastes both bandwidth and queue slots.
                print(f"download retry={attempt + 1}/2 received_bytes={size} curl_exit={proc.returncode}", flush=True)
                time.sleep(min(2, max(0, deadline - time.monotonic())))
    except Exception:
        dest.unlink(missing_ok=True)
        raise


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
