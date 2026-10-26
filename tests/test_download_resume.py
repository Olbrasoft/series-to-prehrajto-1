import hashlib
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import download

PAYLOAD = bytes(range(256)) * 48_000


@pytest.fixture
def server(monkeypatch):
    requests = []
    mode = {'value': 'resume'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            raw = self.headers.get('Range')
            requests.append({'range': raw, 'if_range': self.headers.get('If-Range')})
            if mode['value'] == '404':
                self.send_error(404)
                return
            offset = int(re.fullmatch(r'bytes=(\d+)-', raw)[1])
            status = 200 if offset and mode['value'] == 'ignore' else 206
            start = offset + 1 if offset and mode['value'] == 'wrong_range' else offset
            total = len(PAYLOAD) + (1 if offset and mode['value'] == 'changed_length' else 0)
            self.send_response(status)
            self.send_header('Content-Length', str(len(PAYLOAD) - offset))
            self.send_header('Content-Range', f'bytes {start}-{len(PAYLOAD)-1}/{total}')
            self.send_header('ETag', '"changed"' if offset and mode['value'] == 'changed_etag' else '"stable"')
            self.end_headers()
            cut = len(requests) == 1 or mode['value'] == 'always_partial'
            data = PAYLOAD[offset:offset + 4096] if cut else PAYLOAD[offset:]
            try:
                self.wfile.write(data)
                self.wfile.flush()
                if cut:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(download.time, 'sleep', lambda seconds: None)
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/video', requests, mode
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_real_curl_resumes_eof_and_produces_exact_original_bytes(server, tmp_path):
    url, requests, _ = server
    dest = tmp_path / 'video.mp4'
    dest.write_bytes(b'unrelated old destination')
    size = download.download_to(url, dest, timeout_sec=10, expected_size=len(PAYLOAD))
    assert size == len(PAYLOAD)
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(PAYLOAD).digest()
    assert requests == [{'range': 'bytes=0-', 'if_range': None},
                        {'range': 'bytes=4096-', 'if_range': '"stable"'}]


@pytest.mark.parametrize('mode,reason', [('ignore', 'honor'), ('wrong_range', 'byte range'),
                                       ('changed_etag', 'changed'), ('changed_length', 'length changed')])
def test_invalid_resume_never_returns_partial_or_mixed_file(server, tmp_path, mode, reason):
    url, requests, state = server
    state['value'] = mode
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match=reason):
        download.download_to(url, dest, timeout_sec=10)
    assert len(requests) == 2
    assert not dest.exists()


def test_repeated_interruption_has_bounded_attempts_and_removes_partial_file(server, tmp_path):
    url, requests, state = server
    state['value'] = 'always_partial'
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match='curl exit 18'):
        download.download_to(url, dest, timeout_sec=10)
    assert [r['range'] for r in requests] == ['bytes=0-', 'bytes=4096-', 'bytes=8192-']
    assert not dest.exists()


def test_http_error_is_not_retried(server, tmp_path):
    url, requests, state = server
    state['value'] = '404'
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match='curl exit 22'):
        download.download_to(url, dest, timeout_sec=10)
    assert len(requests) == 1
    assert not dest.exists()


def test_head_length_must_match_downloaded_resource(server, tmp_path):
    url, requests, _ = server
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match='length changed'):
        download.download_to(url, dest, timeout_sec=10, expected_size=len(PAYLOAD) + 1)
    assert len(requests) == 1
    assert not dest.exists()


def test_retries_share_one_total_time_budget(monkeypatch, tmp_path):
    ticks = iter([0, 0, 9, 11])
    monkeypatch.setattr(download.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(download.time, 'sleep', lambda seconds: None)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index('--dump-header') + 1]).write_text(
            'HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-11999999/12000000\r\n\r\n')
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'partial')
        return SimpleNamespace(returncode=18, stderr='early EOF')
    monkeypatch.setattr(download.subprocess, 'run', run)
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match='time budget exhausted'):
        download.download_to('https://example.test/video', dest, timeout_sec=10)
    assert len(calls) == 1
    assert not dest.exists()


def test_headers_describe_final_response_after_redirect(tmp_path):
    path = tmp_path / 'headers'
    path.write_text('HTTP/1.1 200 Connection established\r\n\r\n'
                    'HTTP/1.1 302 Found\r\nETag: "redirect"\r\nLocation: /final\r\n\r\n'
                    'HTTP/2 206\r\nContent-Range: bytes 0-9/10\r\nETag: "final"\r\n\r\n')
    status, headers = download.transfer_headers(path)
    assert status == 206
    assert headers == {'content-range': 'bytes 0-9/10', 'etag': '"final"'}


def test_tls_eof_is_resumed_and_validated(monkeypatch, tmp_path):
    calls = []
    def run(cmd, **kwargs):
        dest = Path(cmd[cmd.index('-o') + 1])
        offset = int(cmd[cmd.index('--continue-at') + 1]) if '--continue-at' in cmd else 0
        calls.append(offset)
        Path(cmd[cmd.index('--dump-header') + 1]).write_text(
            f'HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}\r\nETag: "same"\r\n\r\n')
        with dest.open('ab' if offset else 'wb') as f:
            f.write(PAYLOAD[offset:] if offset else PAYLOAD[:8192])
        return SimpleNamespace(returncode=0 if offset else 56, stderr='unexpected EOF')
    monkeypatch.setattr(download.subprocess, 'run', run)
    monkeypatch.setattr(download.time, 'sleep', lambda seconds: None)
    dest = tmp_path / 'video.mp4'
    assert download.download_to('https://example.test/video', dest, expected_size=len(PAYLOAD)) == len(PAYLOAD)
    assert calls == [0, 8192]
    assert dest.read_bytes() == PAYLOAD


def test_low_speed_timeout_keeps_existing_skip_policy(monkeypatch, tmp_path):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index('--dump-header') + 1]).write_text('')
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'partial')
        return SimpleNamespace(returncode=28, stderr='Operation too slow')
    monkeypatch.setattr(download.subprocess, 'run', run)
    dest = tmp_path / 'video.mp4'
    with pytest.raises(download.DownloadError, match='curl exit 28'):
        download.download_to('https://example.test/video', dest)
    assert len(calls) == 1
    assert not dest.exists()
