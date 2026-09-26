from types import SimpleNamespace

import pytest
import requests

from src import sync_batch as batch
import resolve_stream as resolver


PLAYER = "videos.push({src: 'https://cdn.example/video.mp4', res: '1080', label: '1080p'});"


class Session:
    def __init__(self, outcomes):
        self.headers = {}
        self.outcomes = iter(outcomes)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status, body=""):
    return SimpleNamespace(status_code=status, ok=status == 200, text=body, headers={})


@pytest.fixture(autouse=True)
def proxy_setup(monkeypatch):
    monkeypatch.setattr(resolver, "_localhost_fetch_url", lambda url: None)
    monkeypatch.setattr(resolver, "_cz_proxy_fetch_urls", lambda url: [("cz_proxy_1", "https://proxy.example/")])
    monkeypatch.setattr(resolver, "RESOLVE_MIN_GAP", 0)
    monkeypatch.setattr(resolver.time, "sleep", lambda seconds: None)


def test_unreachable_proxy_exhausts_one_retry_budget_then_reports_infrastructure_failure():
    session = Session([requests.ConnectTimeout("unreachable")] * 3)
    with pytest.raises(resolver.ProxyUnavailableError) as caught:
        resolver.resolve("https://video.example/1", session=session, max_retries=2)
    assert len(session.urls) == 3
    assert not caught.value.permanent


def test_working_alternate_proxy_prevents_batch_abort(monkeypatch):
    monkeypatch.setattr(resolver, "_cz_proxy_fetch_urls", lambda url: [
        ("cz_proxy_1", "https://failed.example/"), ("cz_proxy_2", "https://working.example/")])
    session = Session([requests.ConnectTimeout(), response(200, PLAYER)])
    result = resolver.resolve("https://video.example/1", session=session, max_retries=0)
    assert result.fetch_via == "cz_proxy_2"
    assert result.videos[0].res == 1080


def test_recovered_proxy_keeps_normal_retry_behavior():
    session = Session([requests.ReadTimeout(), response(200, PLAYER)])
    result = resolver.resolve("https://video.example/1", session=session, max_retries=1)
    assert result.fetch_via == "cz_proxy_1"
    assert len(session.urls) == 2


def test_http_response_is_not_a_proxy_transport_outage():
    session = Session([response(503), requests.ConnectTimeout()])
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.resolve("https://video.example/1", session=session, max_retries=1)
    assert not isinstance(caught.value, resolver.ProxyUnavailableError)
    assert not caught.value.permanent


def test_missing_source_remains_a_permanent_source_failure():
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.resolve("https://video.example/1", session=Session([response(404)]), max_retries=2)
    assert caught.value.permanent
    assert not isinstance(caught.value, resolver.ProxyUnavailableError)


def test_direct_transport_error_does_not_claim_proxy_outage(monkeypatch):
    monkeypatch.setattr(resolver, "_cz_proxy_fetch_urls", lambda url: [])
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.resolve("https://video.example/1", session=Session([requests.ConnectTimeout()]), max_retries=0)
    assert not isinstance(caught.value, resolver.ProxyUnavailableError)


def test_batch_stops_without_rejecting_sources_or_trying_every_episode(monkeypatch, tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    backlog.touch()
    rows = [dict(episode_id=i, series_id=1, season=1, episode=i, display_name=f"Example S01E{i:02d}", candidates=[
        dict(source_id=i, url=f"https://video.example/{i}")]) for i in (1, 2)]
    previous_upload = dict(episode_id=99, prehrajto_video_id=100)
    state = dict(uploads=[previous_upload], failed_attempts=[])
    checked = []
    logs = []
    monkeypatch.setenv("PREHRAJTO_EMAIL", "test@example.invalid")
    monkeypatch.setenv("PREHRAJTO_PASSWORD", "test-password")
    monkeypatch.setattr(batch.sys, "argv", ["sync_batch.py", "--count", "2"])
    monkeypatch.setattr(batch, "BACKLOG", backlog)
    monkeypatch.setattr(batch, "load_backlog", lambda: rows)
    monkeypatch.setattr(batch, "load_state", lambda: state)
    monkeypatch.setattr(batch, "load_description_plans", lambda: {"series": {}, "episode": {}})
    monkeypatch.setattr(batch, "load_source_plans", lambda: {})
    monkeypatch.setattr(batch, "pick_next", lambda state, rows, attempted: next(
        (row for row in rows if row["episode_id"] not in attempted), None))
    monkeypatch.setattr(batch, "apply_source_plan", lambda episode, plans, **kwargs: episode)
    monkeypatch.setattr(batch, "login", lambda *args: object())
    monkeypatch.setattr(batch, "has_probable_czech", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(batch, "log", logs.append)

    def unavailable(url):
        checked.append(url)
        raise resolver.ProxyUnavailableError("proxy transport unavailable")

    monkeypatch.setattr(batch, "resolve_stream", unavailable)
    assert batch.main() == 1
    assert checked == ["https://video.example/1"]
    assert state == dict(uploads=[previous_upload], failed_attempts=[])
    assert any("infrastructure_failure=proxy_unavailable" in message for message in logs)
