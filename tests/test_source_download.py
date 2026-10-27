import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import source_download as source
from download import DownloadError


class Response:
    def __init__(self, status, headers):
        self.status_code, self.headers = status, headers
    def __enter__(self):return self
    def __exit__(self, *args):pass
    def raise_for_status(self):pass


class Session:
    def __init__(self, response):self.response, self.calls = response, []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs));return self.response


@pytest.mark.parametrize('query,size,limited', [('', 1000, False), ('limitsize=500', 1000, True),
    ('limitsize=1000', 1000, False), ('limitsize=2000', 1000, False),
    ('limitsize=500', None, True)])
def test_only_explicit_preview_limits_select_the_original(query, size, limited):
    assert source.player_download_limited('https://cdn.test/file?' + query, size) is limited


def test_official_download_preserves_server_signature_and_original_format(monkeypatch):
    url = 'https://pf-storage1.premiumcdn.net/original?token=untouched&signature=untouched'
    session = Session(Response(302, {'Location': url}))
    head_calls = []
    def head(link, **kwargs):
        head_calls.append(link)
        return Response(200, {'Content-Length': '736256384',
            'Content-Disposition': 'attachment; filename="Example+S01E02+CZ+Dabing.mkv"'})
    monkeypatch.setattr(source.requests, 'head', head)
    result = source.official_download(session, 'https://prehraj.to/example/abc?old=value', source_name='Example.mkv')
    assert result == source.CompleteDownload(url, 736256384, '.mkv')
    assert head_calls == [url]
    action, kwargs = session.calls[0]
    assert action == 'https://prehraj.to/example/abc?do=download'
    assert kwargs['allow_redirects'] is False
    assert kwargs['stream'] is True


@pytest.mark.parametrize('location', ['https://example.test/file', '/login',
                                     'https://premiumcdn.net.attacker.test/file', 'http://pf-storage1.premiumcdn.net/file'])
def test_official_download_rejects_untrusted_redirects(location):
    session = Session(Response(302, {'Location': location}))
    with pytest.raises(DownloadError, match='trusted CDN'):
        source.official_download(session, 'https://prehraj.to/example/abc')


def test_paywall_or_error_page_is_never_downloaded_as_video():
    session = Session(Response(200, {'Content-Type': 'text/html'}))
    with pytest.raises(DownloadError, match='unavailable'):
        source.official_download(session, 'https://prehraj.to/example/abc')


def test_portal_return_to_source_page_reports_missing_original(monkeypatch):
    session = Session(Response(302, {'Location': '/example/abc'}))
    monkeypatch.setattr(source.requests, 'head', lambda *a, **kw: pytest.fail('No media was returned'))
    with pytest.raises(DownloadError, match='unavailable.*returned to source page'):
        source.official_download(session, 'https://prehraj.to/example/abc')


@pytest.mark.parametrize('headers,query', [({'Content-Length': '1000'}, ''),
    ({'Content-Length': '1000', 'Content-Disposition': 'attachment; filename="file.txt"'}, ''),
    ({'Content-Length': '1000', 'Content-Disposition': 'attachment; filename="file.mkv"'}, '?limitsize=500')])
def test_unknown_format_or_limited_original_cannot_be_uploaded(monkeypatch, headers, query):
    session = Session(Response(302, {'Location': 'https://pf-storage1.premiumcdn.net/file' + query}))
    monkeypatch.setattr(source.requests, 'head', lambda *a, **kw: Response(200, headers))
    with pytest.raises(DownloadError):
        source.official_download(session, 'https://prehraj.to/example/abc')
