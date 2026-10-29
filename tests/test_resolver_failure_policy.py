import datetime as dt
import sys
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import resolve_stream as resolver
from request_backoff import retry_after_seconds


@pytest.mark.parametrize('body,permanent', [
    ('WebException: ConnectionClosed - connection closed unexpectedly', False),
    ('WebException: Timeout - request timed out', False),
    ('Service Unavailable', False),
    ('Upstream NotFound: file is gone', True),
    ('WebException: ProtocolError - server returned (404) Not Found', True),
])
def test_only_explicit_missing_source_is_permanent(monkeypatch, body, permanent):
    monkeypatch.setattr(resolver, '_rate_limit_until', 0)
    monkeypatch.setattr(resolver, '_cz_proxy_fetch_urls', lambda _: [('cz_proxy_1', 'https://proxy.test/')])
    monkeypatch.setattr(resolver, 'RESOLVE_MIN_GAP', 0)
    response = SimpleNamespace(status_code=502, ok=False, text=body, headers={})
    session = SimpleNamespace(headers={}, get=lambda *a, **kw: response)
    with pytest.raises(resolver.ResolveError) as error:
        resolver.resolve('https://prehraj.to/example/abcdef123456', session=session, max_retries=0)
    assert error.value.permanent is permanent


def test_rate_limit_stops_requests_and_is_dependency_wide(monkeypatch):
    monkeypatch.setattr(resolver, '_rate_limit_until', 0)
    monkeypatch.setattr(resolver, '_cz_proxy_fetch_urls', lambda _: [])
    calls = []
    def get(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=429, ok=False, headers={'Retry-After': '120'})
    session = SimpleNamespace(headers={}, get=get)
    for path in ['first', 'second']:
        with pytest.raises(resolver.ProxyUnavailableError) as error:
            resolver.resolve('https://prehraj.to/' + path, session=session)
        assert not error.value.permanent
        assert error.value.retry_after_seconds >= 120
    assert len(calls) == 1


def test_retry_after_accepts_numeric_and_http_date():
    now = dt.datetime(2026, 10, 29, 16, tzinfo=dt.timezone.utc)
    assert retry_after_seconds('90', now=now.timestamp()) == 90
    assert retry_after_seconds(format_datetime(now + dt.timedelta(seconds=180)), now=now.timestamp()) == 180
    assert retry_after_seconds(None) == 300
    assert retry_after_seconds('not-a-date') == 300


def test_undersized_stream_never_starts_audio_download(monkeypatch):
    import audit_language_sources as audit
    monkeypatch.setenv('WHISPER_LANGUAGE_CHECK', '1')
    stream = SimpleNamespace(url='https://cdn.test/media', res=1080, label='1080p', is_default=True)
    resolved = SimpleNamespace(videos=[stream], tracks=[], video_id=1, name='Example', duration_sec=1200)
    monkeypatch.setattr(audit, 'resolve_prehrajto', lambda *a, **kw: resolved)
    monkeypatch.setattr(audit, 'head_size', lambda _: 100_000_000)
    monkeypatch.setattr(audit, 'sample_audio', lambda *a, **kw: pytest.fail('Undersized media must not download audio'))
    result = audit.audit_provider({'provider': 'prehrajto', 'source_url': 'https://prehraj.to/example'},
        sample_seconds=30, probe_stream=True, minimum_stream_bytes=300 * 1024 ** 2)
    assert result['whisper']['status'] == 'stream_undersized'
