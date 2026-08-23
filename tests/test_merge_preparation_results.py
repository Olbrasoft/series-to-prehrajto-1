import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import merge_preparation_results as preparation_merge  # noqa: E402


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_merge_keeps_newer_current_preparation_row(tmp_path):
    current = tmp_path / "current.jsonl"
    incoming = tmp_path / "incoming.jsonl"
    write_rows(
        current,
        [{"episode_id": 1, "prepared_at": "2026-06-30T12:00:00Z", "upload_ready": True}],
    )
    write_rows(
        incoming,
        [{"episode_id": 1, "prepared_at": "2026-06-30T11:00:00Z", "upload_ready": False}],
    )

    changed = preparation_merge.merge_by_key(
        current,
        incoming,
        key_fn=preparation_merge.prepared_key,
        winner_fn=preparation_merge.newer_timestamp_wins("prepared_at"),
    )

    assert changed == 0
    assert preparation_merge.load_jsonl(current)[0]["upload_ready"] is True


def test_merge_accepts_newer_incoming_preparation_row(tmp_path):
    current = tmp_path / "current.jsonl"
    incoming = tmp_path / "incoming.jsonl"
    write_rows(
        current,
        [{"episode_id": 1, "prepared_at": "2026-06-30T11:00:00Z", "upload_ready": False}],
    )
    write_rows(
        incoming,
        [{"episode_id": 1, "prepared_at": "2026-06-30T12:00:00Z", "upload_ready": True}],
    )

    changed = preparation_merge.merge_by_key(
        current,
        incoming,
        key_fn=preparation_merge.prepared_key,
        winner_fn=preparation_merge.newer_timestamp_wins("prepared_at"),
    )

    assert changed == 1
    assert preparation_merge.load_jsonl(current)[0]["upload_ready"] is True
