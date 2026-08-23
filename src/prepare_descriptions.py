#!/usr/bin/env python3
"""Prepare original Gemma/Gemini descriptions before upload."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from description_quality import is_valid_generated_description

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemma-4-31b-it")
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_THINKING_BUDGET = os.environ.get("GEMINI_THINKING_BUDGET", "0")
DEFAULT_RPM_PER_KEY = float(os.environ.get("GEMINI_RPM_PER_KEY", "1"))
DEFAULT_TPM_PER_KEY = int(os.environ.get("GEMINI_TPM_PER_KEY", "0"))
DEFAULT_RPD_PER_KEY = int(os.environ.get("GEMINI_RPD_PER_KEY", "1400"))
DEFAULT_DAILY_SAFETY_RESERVE = int(os.environ.get("GEMINI_DAILY_SAFETY_RESERVE", "50"))
DEFAULT_QUOTA_STATE = os.environ.get("GEMINI_QUOTA_STATE", "state/gemini-quota-state.json")
PACIFIC = ZoneInfo("America/Los_Angeles")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def api_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    keys = [key.strip() for key in raw.replace("\n", ",").split(",") if key.strip()]
    if not keys:
        raise RuntimeError("GEMINI_API_KEY or GEMINI_API_KEYS is required")
    return keys


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def existing_ok(path: Path, *, replace_fallback: bool = False, replace_non_gemma: bool = False) -> set[tuple[str, int, str]]:
    out: set[tuple[str, int, str]] = set()
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok":
                continue
            if replace_non_gemma and not str(row.get("model") or "").startswith("gemma-"):
                continue
            if replace_fallback and row.get("model") == "fallback-template-v1":
                continue
            if replace_fallback and not is_valid_generated_description(row.get("generated_description") or ""):
                continue
            kind = row.get("kind")
            entity_id = row.get("episode_id") if kind == "episode" else row.get("series_id")
            if kind and entity_id and row.get("source_hash"):
                out.add((kind, int(entity_id), row["source_hash"]))
    return out


def build_tasks(backlog: list[dict], done: set[tuple[str, int, str]], *, series_limit: int, episode_limit: int) -> list[dict]:
    tasks: list[dict] = []
    by_series: dict[int, dict] = {}
    for row in backlog:
        by_series.setdefault(int(row["series_id"]), row)

    for row in list(by_series.values())[:series_limit]:
        source = row.get("series_description") or row.get("series_overview_en") or row.get("source_description") or ""
        if not source.strip():
            continue
        h = source_hash(source)
        if ("series", int(row["series_id"]), h) in done:
            continue
        tasks.append({
            "kind": "series",
            "series_id": row["series_id"],
            "series_slug": row["series_slug"],
            "series_title": row["series_title"],
            "title": row["series_title"],
            "source_description": source,
            "source_hash": h,
        })

    for row in backlog[:episode_limit]:
        source = row.get("source_description") or row.get("description") or row.get("series_description") or ""
        if not source.strip():
            continue
        h = source_hash(source)
        if ("episode", int(row["episode_id"]), h) in done:
            continue
        title = f"{row['series_title']} {row['episode_code']}"
        if row.get("episode_name"):
            title += f" - {row['episode_name']}"
        tasks.append({
            "kind": "episode",
            "series_id": row["series_id"],
            "series_slug": row["series_slug"],
            "series_title": row["series_title"],
            "episode_id": row["episode_id"],
            "season": row["season"],
            "episode": row["episode"],
            "episode_code": row["episode_code"],
            "episode_name": row.get("episode_name"),
            "title": title,
            "source_description": source,
            "source_hash": h,
        })
    return tasks


def prompt_for(task: dict) -> str:
    title = task["title"]
    source = task["source_description"]
    if task["kind"] == "series":
        return (
            "Napiš finální popis seriálu pro videohosting.\n"
            "Výstup musí být jen samotný český popis v odstavci.\n"
            "Zakázáno: rozbor, odrážky, seznam, kontrola, varianty, Draft, Task, Constraints, vysvětlení, angličtina.\n"
            "Neopisuj zdrojové formulace, ale zachovej fakta a neprozrazuj zásadní zvraty.\n"
            "Délka: 3 až 5 vět. Začni rovnou první větou popisu.\n\n"
            f"Název seriálu: {title}\n"
            f"Zdrojový obsah:\n{source}\n\n"
            "Vrať pouze finální popis:"
        )
    return (
        "Napiš finální popis seriálové epizody pro videohosting.\n"
        "Výstup musí být jen samotný český popis v odstavci.\n"
        "Zakázáno: rozbor, odrážky, seznam, kontrola, varianty, Draft, Task, Constraints, vysvětlení, angličtina.\n"
        "Neopisuj zdrojové formulace, ale zachovej fakta a neprozrazuj pointu.\n"
        "Délka: 2 až 4 věty. Nepoužívej frázi 'v této epizodě uvidíte'. Začni rovnou první větou popisu.\n\n"
        f"Název epizody: {title}\n"
        f"Zdrojový obsah:\n{source}\n\n"
        "Vrať pouze finální popis:"
    )


def fallback_description(task: dict) -> str:
    if task["kind"] == "series":
        return (
            f"{task['title']} je seriál, který postupně rozvíjí osudy hlavních postav a jejich vzájemné vztahy. "
            "Příběh staví na napětí, charakterech a situacích, které se proměňují s každou další epizodou. "
            "Popis je připraven tak, aby nepřebíral původní text a neprozrazoval zásadní zvraty."
        )
    return (
        f"{task['title']} pokračuje v ději seriálu a soustředí se na další vývoj postav i jejich rozhodnutí. "
        "Epizoda zapadá do širšího příběhu série a drží prostor pro napětí bez prozrazení hlavních zvratů. "
        "Text je připraven jako dočasný originální popis pro upload."
    )


def generation_config(model: str) -> dict:
    config: dict = {
        "temperature": 0.25,
        "topP": 0.7,
        "maxOutputTokens": 180,
        "stopSequences": ["\n*", "\n-", "\nDraft", "\nTask", "\nConstraints"],
    }
    if DEFAULT_THINKING_BUDGET.strip() and not model.startswith("gemma-"):
        config["thinkingConfig"] = {"thinkingBudget": int(DEFAULT_THINKING_BUDGET)}
    return config


def response_error(resp: requests.Response) -> tuple[str, int | None, float | None, dict[str, str], list[dict]]:
    headers = {
        key: value
        for key, value in resp.headers.items()
        if key.lower() in {"retry-after", "date", "server"}
        or key.lower().startswith("x-ratelimit")
        or key.lower().startswith("x-goog")
    }
    retry_after = None
    if resp.headers.get("Retry-After"):
        try:
            retry_after = float(resp.headers["Retry-After"])
        except ValueError:
            retry_after = None
    try:
        body = resp.json()
    except ValueError:
        text = resp.text[:500]
        match = re.search(r"retry in ([0-9.]+)s", text, flags=re.IGNORECASE)
        if match:
            retry_after = float(match.group(1))
        return text, None, retry_after, headers, []

    error = body.get("error") or {}
    message = str(error.get("message") or resp.text[:500])
    details = error.get("details") or []
    match = re.search(r"retry in ([0-9.]+)s", message, flags=re.IGNORECASE)
    if match:
        retry_after = float(match.group(1))
    retry_delay = retry_after_from_details(details)
    if retry_delay is not None:
        retry_after = retry_delay
    return message[:500], int(error["code"]) if error.get("code") else None, retry_after, headers, details


def retry_after_from_details(details: list[dict]) -> float | None:
    for detail in details:
        retry_delay = detail.get("retryDelay")
        if not isinstance(retry_delay, str):
            continue
        match = re.fullmatch(r"([0-9.]+)s", retry_delay)
        if match:
            return float(match.group(1))
    return None


def quota_violations(details: list[dict]) -> list[dict]:
    violations: list[dict] = []
    for detail in details:
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict):
                violations.append(violation)
    return violations


def quota_names(details: list[dict]) -> list[str]:
    names: list[str] = []
    for violation in quota_violations(details):
        for key in ("quotaMetric", "quotaId", "subject", "description"):
            value = violation.get(key)
            if isinstance(value, str) and value not in names:
                names.append(value)
    return names


def is_daily_quota(details: list[dict], message: str, retry_after: float | None) -> bool:
    haystack = " ".join([message, *quota_names(details)]).lower()
    daily_markers = ("perday", "per_day", "requestsperday", "requests per day", "rpd", "daily")
    minute_markers = ("perminute", "per_minute", "requestsperminute", "tokensperminute", "requests per minute", "tokens per minute", "rpm", "tpm")
    if any(marker in haystack for marker in daily_markers):
        return True
    if any(marker in haystack for marker in minute_markers):
        return False
    # Generic RESOURCE_EXHAUSTED responses can indicate temporary model
    # capacity. Only explicit daily quota metadata is safe to persist until
    # the next Pacific-day reset.
    return False


def pacific_day() -> str:
    return dt.datetime.now(PACIFIC).date().isoformat()


def next_pacific_midnight_epoch() -> float:
    now = dt.datetime.now(PACIFIC)
    next_midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time.min, tzinfo=PACIFIC)
    return next_midnight.timestamp()


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class KeyQuotaLimiter:
    def __init__(
        self,
        keys: list[str],
        *,
        model: str,
        rpm_per_key: float,
        tpm_per_key: int,
        rpd_per_key: int,
        daily_safety_reserve: int,
        state_path: Path,
    ) -> None:
        self.keys = keys
        self.model = model
        self.rpm_per_key = max(0.0, rpm_per_key)
        self.tpm_per_key = max(0, tpm_per_key)
        self.rpd_per_key = max(0, rpd_per_key)
        self.daily_safety_reserve = max(0, daily_safety_reserve)
        self.state_path = state_path
        self.min_interval_seconds = 60.0 / self.rpm_per_key if self.rpm_per_key > 0 else 0.0
        self._locks = [threading.Lock() for _ in keys]
        self._next_allowed_at = [0.0 for _ in keys]
        self._minute_tokens: list[list[tuple[float, int]]] = [[] for _ in keys]
        self._state_lock = threading.RLock()
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"version": 1, "keys": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"version": 1, "keys": {}}

    def save(self) -> None:
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.state_path)

    def _state_row(self, key_index: int) -> dict:
        key = f"{self.model}:{key_id(self.keys[key_index])}"
        rows = self._state.setdefault("keys", {})
        row = rows.setdefault(key, {})
        day = pacific_day()
        if row.get("pacific_day") != day:
            row.clear()
            row["pacific_day"] = day
            row["requests_today"] = 0
        if (
            row.get("disabled_until_epoch")
            and not row.get("quota_names")
            and row.get("disabled_reason") == "Resource has been exhausted (e.g. check quota)."
        ):
            for field in ("disabled_until_epoch", "disabled_reason", "disabled_at", "quota_names"):
                row.pop(field, None)
        return row

    def key_available(self, key_index: int) -> bool:
        with self._state_lock:
            row = self._state_row(key_index)
            if float(row.get("disabled_until_epoch") or 0) > time.time():
                return False
            daily_limit = max(0, self.rpd_per_key - self.daily_safety_reserve)
            return not daily_limit or int(row.get("requests_today") or 0) < daily_limit

    def available_key_count(self) -> int:
        return sum(1 for index in range(len(self.keys)) if self.key_available(index))

    def pick_available_key(self, preferred_index: int) -> int | None:
        for offset in range(len(self.keys)):
            key_index = (preferred_index + offset) % len(self.keys)
            if self.key_available(key_index):
                return key_index
        return None

    def wait(self, key_index: int, estimated_tokens: int) -> bool:
        if not self.key_available(key_index):
            return False
        with self._locks[key_index]:
            while True:
                if not self.key_available(key_index):
                    return False
                now = time.monotonic()
                wait_seconds = max(0.0, self._next_allowed_at[key_index] - now)
                if self.tpm_per_key > 0:
                    window = [
                        item
                        for item in self._minute_tokens[key_index]
                        if now - item[0] < 60.0
                    ]
                    self._minute_tokens[key_index] = window
                    used_tokens = sum(tokens for _, tokens in window)
                    if used_tokens + estimated_tokens > self.tpm_per_key:
                        oldest = min((ts for ts, _ in window), default=now)
                        wait_seconds = max(wait_seconds, 60.0 - (now - oldest) + 0.1)
                if wait_seconds <= 0:
                    self._next_allowed_at[key_index] = now + self.min_interval_seconds
                    if self.tpm_per_key > 0:
                        self._minute_tokens[key_index].append((now, estimated_tokens))
                    with self._state_lock:
                        row = self._state_row(key_index)
                        row["requests_today"] = int(row.get("requests_today") or 0) + 1
                        row["last_request_at"] = now_iso()
                    self.save()
                    return True
                time.sleep(wait_seconds)

    def postpone(self, key_index: int, retry_after_seconds: float | None) -> None:
        if retry_after_seconds is None:
            return
        with self._locks[key_index]:
            self._next_allowed_at[key_index] = max(
                self._next_allowed_at[key_index],
                time.monotonic() + max(0.0, retry_after_seconds) + 1.0,
            )

    def disable_until_daily_reset(self, key_index: int, *, reason: str, details: list[dict]) -> None:
        with self._state_lock:
            row = self._state_row(key_index)
            row["disabled_until_epoch"] = next_pacific_midnight_epoch()
            row["disabled_reason"] = reason[:500]
            row["disabled_at"] = now_iso()
            row["quota_names"] = quota_names(details)
        self.save()


def estimate_tokens(text: str, max_output_tokens: int) -> int:
    return max(1, len(text) // 4 + max_output_tokens)


def generate(
    task: dict,
    key: str,
    model: str,
    *,
    key_index: int,
    limiter: KeyQuotaLimiter,
    retries: int = 3,
    fallback_on_error: bool = False,
) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    prompt = prompt_for(task)
    config = generation_config(model)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    last_error_status = None
    last_error_headers: dict[str, str] = {}
    last_retry_after = None
    last_quota_details: list[dict] = []
    estimated_tokens = estimate_tokens(prompt, int(config.get("maxOutputTokens") or 0))
    for attempt in range(retries):
        try:
            if not limiter.wait(key_index, estimated_tokens):
                raise RuntimeError("API key quota exhausted for current Pacific day")
            resp = requests.post(url, json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not is_valid_generated_description(text):
                raise ValueError("Generated description failed validation")
            return {**task, "status": "ok", "model": model, "generated_at": now_iso(), "generated_description": text}
        except Exception as exc:
            error_text = str(exc)
            retry_after = None
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                error_message, status_code, retry_after, headers, details = response_error(exc.response)
                last_error_status = status_code or exc.response.status_code
                last_error_headers = headers
                last_retry_after = retry_after
                last_quota_details = details
                error_text = f"HTTP {exc.response.status_code}: {error_message}"
                if exc.response.status_code == 429:
                    if is_daily_quota(details, error_message, retry_after):
                        limiter.disable_until_daily_reset(key_index, reason=error_message, details=details)
                    else:
                        limiter.postpone(key_index, retry_after or 65.0)
            if attempt == retries - 1:
                if fallback_on_error:
                    return {
                        **task,
                        "status": "ok",
                        "model": "fallback-template-v1",
                        "generated_at": now_iso(),
                        "generated_description": fallback_description(task),
                        "fallback_reason": type(exc).__name__,
                        "fallback_error": error_text[:500],
                    }
                return {
                    **task,
                    "status": "error",
                    "model": model,
                    "generated_at": now_iso(),
                    "error": type(exc).__name__,
                    "error_message": error_text[:500],
                    "error_status_code": last_error_status,
                    "error_retry_after_seconds": last_retry_after,
                    "error_headers": last_error_headers,
                    "error_quota_names": quota_names(last_quota_details),
                    "error_quota_details": last_quota_details,
                }
            time.sleep(max(DEFAULT_RETRY_SECONDS + attempt * 3, retry_after or 0))
    raise AssertionError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backlog", default="backlog/series-episodes.jsonl.gz")
    ap.add_argument("--out", default="plans/descriptions.jsonl")
    ap.add_argument("--series-limit", type=int, default=8)
    ap.add_argument("--episode-limit", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--rpm-per-key", type=float, default=DEFAULT_RPM_PER_KEY)
    ap.add_argument("--tpm-per-key", type=int, default=DEFAULT_TPM_PER_KEY)
    ap.add_argument("--rpd-per-key", type=int, default=DEFAULT_RPD_PER_KEY)
    ap.add_argument("--daily-safety-reserve", type=int, default=DEFAULT_DAILY_SAFETY_RESERVE)
    ap.add_argument("--quota-state", default=DEFAULT_QUOTA_STATE)
    ap.add_argument("--fallback-on-error", action="store_true")
    ap.add_argument(
        "--replace-fallback",
        action="store_true",
        help="Treat temporary fallback descriptions as missing so later runs can replace them with model output.",
    )
    ap.add_argument(
        "--replace-non-gemma",
        action="store_true",
        help="Treat older non-Gemma descriptions as missing so they can be replaced with Gemma output.",
    )
    args = ap.parse_args()

    rows = load_jsonl(Path(args.backlog))
    done = existing_ok(Path(args.out), replace_fallback=args.replace_fallback, replace_non_gemma=args.replace_non_gemma)
    tasks = build_tasks(rows, done, series_limit=args.series_limit, episode_limit=args.episode_limit)
    keys = api_keys()
    out_path = Path(args.out)
    limiter = KeyQuotaLimiter(
        keys,
        model=args.model,
        rpm_per_key=args.rpm_per_key,
        tpm_per_key=args.tpm_per_key,
        rpd_per_key=args.rpd_per_key,
        daily_safety_reserve=args.daily_safety_reserve,
        state_path=Path(args.quota_state),
    )
    ok = 0
    total = 0
    print(
        "Description generation config: "
        f"model={args.model} keys={len(keys)} workers={args.workers} "
        f"rpm_per_key={args.rpm_per_key} tpm_per_key={args.tpm_per_key or 'disabled'} "
        f"rpd_per_key={args.rpd_per_key} daily_safety_reserve={args.daily_safety_reserve} "
        f"available_keys={limiter.available_key_count()} quota_state={args.quota_state}",
        file=sys.stderr,
    )

    def task(index_task: tuple[int, dict]) -> dict:
        index, task_row = index_task
        key_index = limiter.pick_available_key(index % len(keys))
        if key_index is None:
            return {
                **task_row,
                "status": "error",
                "model": args.model,
                "generated_at": now_iso(),
                "error": "QuotaExhausted",
                "error_message": "All API keys exhausted for current Pacific day",
                "key_slot": None,
                "key_count": len(keys),
            }
        result = generate(
            task_row,
            keys[key_index],
            args.model,
            key_index=key_index,
            limiter=limiter,
            retries=args.retries,
            fallback_on_error=args.fallback_on_error,
        )
        result["key_slot"] = key_index + 1
        result["key_count"] = len(keys)
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(task, item)
            for item in enumerate(tasks)
        ]
        for fut in as_completed(futures):
            row = fut.result()
            append_jsonl(out_path, [row])
            total += 1
            if row.get("status") == "ok":
                ok += 1
            ident = row.get("episode_code") or row.get("series_slug")
            suffix = f" {row.get('error') or row.get('fallback_reason')}" if row.get("error") or row.get("fallback_reason") else ""
            key_suffix = f" key_slot={row.get('key_slot')}/{row.get('key_count')}"
            print(f"{row['status']} {row['kind']} {ident}{key_suffix}{suffix}", file=sys.stderr)
    print(f"Prepared descriptions: ok={ok} total={total} out={args.out}")
    return 0 if ok > 0 or not tasks else 1


if __name__ == "__main__":
    raise SystemExit(main())
