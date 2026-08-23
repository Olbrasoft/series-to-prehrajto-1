"""Shared source quality helpers for episode upload preparation."""

from __future__ import annotations

import re
from typing import Any

MIN_PREFERRED_FILESIZE_BYTES = 350 * 1024 * 1024


def resolution_score(value: str | None) -> int:
    text = (value or "").lower()
    if re.search(r"2160|4k|uhd", text):
        return 2160
    match = re.search(r"(?<!\d)(1080|720|576|480)(?!\d)", text)
    return int(match.group(1)) if match else 0


def source_quality_tier(source: dict[str, Any], *, resolved_resolution: int | None = None) -> str:
    resolution = int(resolved_resolution or 0) or resolution_score(
        " ".join(
            str(value or "")
            for value in (source.get("resolution_hint"), source.get("title"), source.get("source_title"))
        )
    )
    filesize = int(source.get("filesize_bytes") or 0)
    if resolution >= 1080 or filesize >= MIN_PREFERRED_FILESIZE_BYTES:
        return "preferred"
    return "acceptable"


def source_quality_score(source: dict[str, Any], *, resolved_resolution: int | None = None) -> tuple[int, int, int]:
    resolution = int(resolved_resolution or 0) or resolution_score(
        " ".join(
            str(value or "")
            for value in (source.get("resolution_hint"), source.get("title"), source.get("source_title"))
        )
    )
    filesize = int(source.get("filesize_bytes") or 0)
    preferred_bonus = 1 if source_quality_tier(source, resolved_resolution=resolution) == "preferred" else 0
    return preferred_bonus, resolution, filesize
