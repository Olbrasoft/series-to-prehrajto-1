"""Paced OpenSubtitles development client with a shared, persistent quota."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import unicodedata
import re
from pathlib import Path

import requests

API = "https://api.opensubtitles.com/api/v1"


class ProviderStopped(RuntimeError):
    """Authentication, throttling, or download allowance prevents more work."""


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def normalized(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", value)


def identity_match(attributes: dict, episode: dict) -> bool:
    feature = attributes.get("feature_details") or {}
    try:
        same_episode = (int(feature["season_number"]) == int(episode["season"])
                        and int(feature["episode_number"]) == int(episode["episode"]))
    except (KeyError, ValueError, TypeError):
        return False
    if (not same_episode or attributes.get("language") != "cs"
            or any(attributes.get(key) for key in ("ai_translated", "machine_translated", "foreign_parts_only"))):
        return False
    imdb = str(episode.get("imdb_id") or "").removeprefix("tt")
    if imdb.isdigit():
        return str(feature.get("parent_imdb_id")) == str(int(imdb))
    names = {normalized(episode.get(key)) for key in ("series_title", "series_original_title")}
    names.discard("")
    return normalized(feature.get("parent_title")) in names


class OpenSubtitles:
    def __init__(self, root: Path, key: str, user_agent: str, daily_budget=90, reserve=10, session=None):
        if not key or not user_agent or not 1 <= daily_budget <= 90 or reserve < 10:
            raise ValueError("An API key, User-Agent, budget of 1–90, and reserve of at least ten are required")
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "provider.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"files": {}, "quota": {}}
        self.session = session or requests.Session()
        self.session.headers.update({"Api-Key": key, "User-Agent": user_agent, "Accept": "application/json"})
        self.user_agent = user_agent
        self.budget, self.reserve = daily_budget, reserve
        self.last_request = 0.0

    def save(self):
        write_json(self.path, self.data)

    def allowance(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        quota = self.data["quota"]
        try:
            reset = dt.datetime.fromisoformat(quota["reset_time_utc"].replace("Z", "+00:00"))
            if now >= reset:
                quota = self.data["quota"] = {}
        except (KeyError, ValueError, TypeError):
            # Uncertain request counts expire only at the next UTC day.
            if quota.get("date") and quota["date"] != now.date().isoformat():
                quota = self.data["quota"] = {}
        used = int(quota.get("requests", 0)) + int(quota.get("uncertain_requests", 0))
        remaining = int(quota.get("remaining", self.budget + self.reserve))
        return max(0, min(self.budget - used, remaining - self.reserve))

    def request(self, method, endpoint, **kwargs):
        time.sleep(max(0, 1.05 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.session.request(method, API + endpoint, timeout=30, **kwargs)
        if response.status_code in {401, 403, 406, 429}:
            if endpoint == "/download" and response.status_code == 406:
                value = response.json()
                self.record_quota(value)
            raise ProviderStopped(f"provider_http_{response.status_code}")
        if response.status_code != 200:
            raise ProviderStopped(f"provider_http_{response.status_code}")
        return response.json()

    def record_quota(self, value):
        previous = self.data["quota"]
        if "requests" in value and "remaining" in value:
            self.data["quota"] = {k: value[k] for k in ("requests", "remaining", "reset_time_utc") if k in value}
            self.data["quota"].update(checked_at=timestamp(), date=dt.datetime.now(dt.timezone.utc).date().isoformat())
        else:
            # Keep the reservation when the provider doesn't confirm its count.
            self.data["quota"] = previous
        self.save()

    def search(self, episode):
        base = {"languages": "cs", "type": "episode", "season_number": episode["season"], "episode_number": episode["episode"]}
        imdb = str(episode.get("imdb_id") or "").removeprefix("tt")
        queries = [{**base, "parent_imdb_id": int(imdb)}] if imdb.isdigit() else []
        names = list(dict.fromkeys(episode.get(key) for key in ("series_original_title", "series_title") if episode.get(key)))
        queries += [{**base, "query": name} for name in names]
        for params in queries:
            found = []
            for row in self.request("GET", "/subtitles", params=params).get("data", []):
                attrs = row.get("attributes") or {}
                if identity_match(attrs, episode) and len(attrs.get("files") or []) == 1 and int(attrs.get("nb_cd") or 1) == 1:
                    found.append(attrs)
            if found:
                return sorted(found, key=lambda row: (not row.get("from_trusted"), -int(row.get("download_count") or 0)))
        return []

    def download(self, file_id):
        file_id = int(file_id)
        path = self.root / f"{file_id}.srt"
        cached = self.data["files"].get(str(file_id))
        if cached and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == cached["sha256"]:
            return path, cached["sha256"]
        if self.allowance() <= 0:
            raise ProviderStopped("daily_download_budget_exhausted")
        # Reserve an uncertain attempt before the call. A timeout must not reset
        # the local counter or cause an immediate retry with the same identity.
        self.data["quota"].setdefault("date", dt.datetime.now(dt.timezone.utc).date().isoformat())
        self.data["quota"]["uncertain_requests"] = int(self.data["quota"].get("uncertain_requests", 0)) + 1
        self.save()
        try:
            receipt = self.request("POST", "/download", json={"file_id": file_id, "sub_format": "srt"})
            self.record_quota(receipt)
            # Never forward the API key to the signed file-serving URL, or log it.
            response = requests.get(receipt["link"], headers={"User-Agent": self.user_agent}, timeout=30)
            if response.status_code != 200:
                raise ProviderStopped(f"file_http_{response.status_code}")
            path.write_bytes(response.content)
            path.chmod(0o600)
            digest = hashlib.sha256(response.content).hexdigest()
            self.data["files"][str(file_id)] = {"sha256": digest, "downloaded_at": timestamp()}
            self.save()
            return path, digest
        except requests.RequestException as error:
            # Exception text may include credentials or signed download URLs.
            raise ProviderStopped(f"transport_{type(error).__name__}") from None
