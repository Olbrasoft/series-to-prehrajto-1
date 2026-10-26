"""Select the portal's complete original when the player URL is preview-limited."""
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, unquote_plus

import requests

from download import DownloadError, USER_AGENT, head_size

VIDEO_TYPES = {'.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
               '.mov': 'video/quicktime', '.webm': 'video/webm', '.m4v': 'video/mp4',
               '.ts': 'video/mp2t', '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg'}
PORTAL_HOSTS = {'prehraj.to', 'www.prehraj.to', 'prehrajto.cz', 'www.prehrajto.cz'}


@dataclass
class CompleteDownload:
    url: str
    size: int
    suffix: str


def player_download_limited(url: str, expected_size: int | None) -> bool:
    value = parse_qs(urlsplit(url).query).get('limitsize', [''])[0]
    return value.isdigit() and (expected_size is None or int(value) < expected_size)


def official_download(session, source_url: str, *, source_name: str = '') -> CompleteDownload:
    """Follow the ordinary download action; never modify signed CDN parameters."""
    parsed = urlsplit(source_url)
    if parsed.scheme != 'https' or parsed.hostname not in PORTAL_HOSTS:
        raise DownloadError('Official download requires a portal source URL')
    action = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({'do': 'download'}), ''))
    try:
        with session.get(action, allow_redirects=False, stream=True, timeout=30,
                         headers={'User-Agent': USER_AGENT, 'Referer': source_url}) as response:
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise DownloadError(f'Official download unavailable (HTTP {response.status_code})')
            url = urljoin(action, response.headers.get('Location', ''))
        cdn = urlsplit(url)
        if cdn.scheme != 'https' or not (cdn.hostname or '').endswith('.premiumcdn.net'):
            raise DownloadError('Official download did not return a trusted CDN URL')
        with requests.head(url, allow_redirects=True, timeout=30,
                           headers={'User-Agent': USER_AGENT, 'Referer': source_url}) as response:
            response.raise_for_status()
            disposition = Message()
            disposition['Content-Disposition'] = response.headers.get('Content-Disposition', '')
            filename = unquote_plus(disposition.get_filename() or source_name)
            length = response.headers.get('Content-Length', '')
            size = int(length) if response.status_code == 200 and length.isdigit() else None
        if size is None:
            size = head_size(url)
    except requests.RequestException as exc:
        # Exception strings can contain signed URLs; expose only their type.
        raise DownloadError(f'Official download lookup failed: {type(exc).__name__}') from exc
    if not size or player_download_limited(url, size):
        raise DownloadError('Official download does not provide a complete measurable file')
    suffix = Path(filename).suffix.lower()
    if suffix not in VIDEO_TYPES:
        raise DownloadError('Official download has an unsupported video format')
    return CompleteDownload(url, size, suffix)
