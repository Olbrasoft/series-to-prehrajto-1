"""Validation helpers for generated Czech descriptions."""

from __future__ import annotations

import re

FORBIDDEN_DESCRIPTION_MARKERS = (
    "task:",
    "constraints:",
    "source material",
    "source text",
    "source description",
    "draft",
    "fact ",
    "plot ",
    "original?",
    "no copying",
    "checked",
    "episode:",
    "series/episode",
    "pokračuje v ději seriálu",
    "epizoda zapadá do širšího příběhu série",
    "text je připraven jako dočasný",
    "popis je připraven tak",
    "$\\rightarrow",
    "→",
)


def is_valid_generated_description(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if len(normalized) < 90 or len(normalized) > 900:
        return False
    lowered = normalized.lower()
    if any(marker in lowered for marker in FORBIDDEN_DESCRIPTION_MARKERS):
        return False
    if normalized.startswith(("*", "-", "#", "{", "[")):
        return False
    if "\n*" in text or "\n-" in text:
        return False
    if not re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", normalized):
        return False
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", normalized))
    return 1 <= sentence_count <= 5
