from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.pick_next_episode import cooling_down_source_ids, pick_next


def test_pick_next_skips_recent_transient_failure() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "uploads": [],
        "failed_attempts": [
            {
                "episode_id": 1,
                "source_id": 101,
                "reason": "download_failed: timeout",
                "permanent": False,
                "failed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ],
    }
    rows = [
        {
            "episode_id": 1,
            "series_id": 1,
            "season": 1,
            "episode": 1,
            "candidates": [{"source_id": 101}],
        },
        {
            "episode_id": 2,
            "series_id": 1,
            "season": 1,
            "episode": 2,
            "candidates": [{"source_id": 102}],
        },
    ]

    assert cooling_down_source_ids(state, now=now + timedelta(minutes=1)) == {101}
    assert pick_next(state, rows)["episode_id"] == 2


def test_expired_transient_failure_can_be_retried() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "uploads": [],
        "failed_attempts": [
            {
                "episode_id": 1,
                "source_id": 101,
                "reason": "download_failed: timeout",
                "permanent": False,
                "failed_at": (now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ],
    }
    rows = [
        {
            "episode_id": 1,
            "series_id": 1,
            "season": 1,
            "episode": 1,
            "candidates": [{"source_id": 101}],
        }
    ]

    assert cooling_down_source_ids(state, now=now) == set()
    assert pick_next(state, rows)["episode_id"] == 1
