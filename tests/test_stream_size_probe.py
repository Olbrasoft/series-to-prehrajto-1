import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from download import head_size


@pytest.mark.parametrize('mode,expected,expected_gets', [
    ('head', 900_000_000, 0),
    ('partial-head', 900_000_000, 0),
    ('redirect', 243_826_066, 1),
    ('range', 243_826_066, 1),
    ('unknown', None, 1),
    ('ignored', 800_000_000, 1),
    ('error', None, 0),
])
def test_size_probe_reads_headers_without_downloading_video(mode, expected, expected_gets):
    gets = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            if mode == 'redirect' and self.path == '/video':
                self.send_response(302)
                self.send_header('Content-Length', '0')
                self.send_header('Location', '/final')
            else:
                self.send_response(503 if mode == 'error' else 206 if mode == 'partial-head' else 200)
                if mode == 'head':
                    self.send_header('Content-Length', str(expected))
                elif mode == 'partial-head':
                    self.send_header('Content-Length', '1')
                    self.send_header('Content-Range', f'bytes 0-0/{expected}')
            self.end_headers()

        def do_GET(self):
            gets.append(dict(self.headers))
            self.send_response(200 if mode == 'ignored' else 206)
            if mode == 'ignored':
                self.send_header('Content-Length', str(expected))
            else:
                self.send_header('Content-Range', 'bytes 0-0/*' if mode == 'unknown' else 'bytes 0-0/243826066')
                self.send_header('Content-Length', '1')
            self.end_headers()
            # A correct client only needs headers, even if a full-file Content-
            # Length was returned. Reading that response body would fail here.
            self.wfile.write(b'x')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert head_size(f'http://127.0.0.1:{server.server_port}/video', timeout_sec=2) == expected
        assert len(gets) == expected_gets
        if gets:
            assert gets[0]['Range'] == 'bytes=0-0'
            assert gets[0]['Referer'] == 'https://prehraj.to/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
