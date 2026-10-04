"""Reusable subtitle files, carried between Actions runs as a bounded artifact."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ARTIFACT_NAME = "subtitle-source-cache"


def cache_key(upload: dict) -> str:
    account = upload.get("upload_account")
    if account not in {"primary", "serialy"}:
        raise ValueError("Unknown subtitle account")
    return f"{account}-{int(upload['episode_id'])}-{int(upload['prehrajto_video_id'])}"


class SubtitleCache:
    def __init__(self, directory: Path):
        self.directory = directory
        self.index = directory / "index.json"
        data = json.loads(self.index.read_text()) if self.index.exists() else {}
        self.entries = data.get("entries", {}) if data.get("version") == 1 else {}
        self.scheduler = data.get("scheduler", {})

    def available(self, upload: dict) -> bool:
        entry = self.entries.get(cache_key(upload), {})
        return self.ready(upload) or (entry.get("status") == "target_has_tracks"
                                      and entry.get("original_source_url") == upload.get("source_url"))

    def ready(self, upload: dict) -> bool:
        key = cache_key(upload)
        entry = self.entries.get(key, {})
        path = self.directory / f"{key}.srt"
        return (entry.get("status") == "ready"
                and entry.get("original_source_url") == upload.get("source_url")
                and path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == entry.get("sha256"))

    def get(self, upload: dict, target_duration: int | None) -> tuple[str, bytes] | None:
        if not self.ready(upload):
            return None
        key = cache_key(upload)
        entry = self.entries[key]
        duration = entry.get("target_duration")
        if not duration or not target_duration or abs(duration - target_duration) > 2:
            return None
        content = (self.directory / f"{key}.srt").read_bytes()
        if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
            return None
        return entry["source_url"], content

    def invalidate_duration(self, upload: dict, target_duration: int | None) -> None:
        entry = self.entries.get(cache_key(upload), {})
        stored = entry.get("target_duration")
        if entry.get("status") == "ready" and stored and target_duration and abs(stored - target_duration) > 2:
            self.record(upload, "stale", retry_hours=0)

    def due(self, upload: dict, now: dt.datetime) -> bool:
        entry = self.entries.get(cache_key(upload), {})
        if entry.get("original_source_url") != upload.get("source_url"):
            return True
        if self.ready(upload):
            return False
        if entry.get("status") == "ready":
            return True  # Recover missing/corrupted files immediately.
        try:
            return now >= dt.datetime.fromisoformat(entry["retry_after"])
        except (KeyError, ValueError, TypeError):
            return True

    def record(self, upload: dict, status: str, *, retry_hours: float = 6, **extra) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.entries[cache_key(upload)] = {
            "status": status, "episode_id": upload["episode_id"],
            "original_source_url": upload.get("source_url"),
            "checked_at": now.isoformat(),
            "retry_after": (now + dt.timedelta(hours=retry_hours)).isoformat(), **extra,
        }
        self.save()

    def put(self, upload: dict, source_url: str, target_duration: int, content: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{cache_key(upload)}.srt"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
        self.record(upload, "ready", source_url=source_url, target_duration=target_duration,
                    sha256=hashlib.sha256(content).hexdigest())

    def prune(self, uploads: list[dict]) -> None:
        keep = {cache_key(upload) for upload in uploads}
        self.entries = {key: value for key, value in self.entries.items() if key in keep}
        for path in self.directory.glob("*.srt"):
            if path.stem not in keep:
                path.unlink()
        self.save()

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": self.entries, "scheduler": self.scheduler}, ensure_ascii=False, sort_keys=True))
        tmp.replace(self.index)


def restore(directory: Path, repo: str) -> None:
    result = subprocess.check_output([
        "gh", "api", f"repos/{repo}/actions/artifacts?name={ARTIFACT_NAME}&per_page=20",
    ], text=True)
    artifacts = [a for a in json.loads(result)["artifacts"]
                 if not a["expired"] and a.get("workflow_run", {}).get("head_branch") == "main"]
    if not artifacts:
        print("No subtitle cache yet; starting empty.")
        return
    artifact = max(artifacts, key=lambda a: a["created_at"])
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run(["gh", "run", "download", str(artifact["workflow_run"]["id"]),
                        "--repo", repo, "--name", ARTIFACT_NAME, "--dir", temporary], check=True)
        directory.mkdir(parents=True, exist_ok=True)
        for path in Path(temporary).iterdir():
            if path.name == "index.json" or path.suffix == ".srt":
                shutil.copy2(path, directory / path.name)
    print(f"Restored subtitle cache from run {artifact['workflow_run']['id']}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    restore(args.directory, args.repo)
