#!/usr/bin/env python3
"""Lightweight Czech audio/subtitle checks for episode source candidates."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

CZECH_AUDIO_LANGS = {"cs", "cz", "cze", "ces", "cs-cz", "cz-cz", "czech"}
CZECH_AUDIO_RE = re.compile(
    r"(^|[\W_])(cz|cs|cesky|česky|czech|dabing|czdab|cz-dab|cz dab|czech audio)([\W_]|$)",
    re.IGNORECASE,
)
CZECH_SUB_RE = re.compile(
    r"(^|[\W_])(cz\s*tit|cz titulky|cztit|tit[-_\s]*cz|ceske titulky|české titulky|cz sub|czech sub)([\W_]|$)",
    re.IGNORECASE,
)
_WHISPER_MODEL = None
_WHISPER_MODEL_KEY: tuple[str, str] | None = None


def normalize_lang(value: str | None) -> str:
    return (value or "").strip().lower().replace("_", "-")


def resolution_score(value: str | None) -> int:
    text = (value or "").lower()
    for number in re.findall(r"(?<!\d)(2160|1440|1080|720|576|480)(?!\d)", text):
        return int(number)
    if "4k" in text or "uhd" in text:
        return 2160
    if "fullhd" in text or "full hd" in text:
        return 1080
    if "hd" in text:
        return 720
    return 0


def title_language_hint(title: str | None) -> str | None:
    if not title:
        return None
    if CZECH_SUB_RE.search(title):
        return "cz_subtitle_title"
    if CZECH_AUDIO_RE.search(title):
        return "cz_audio_title"
    return None


def metadata_language_hint(candidate: dict) -> str | None:
    lang_class = (candidate.get("lang_class") or "").upper()
    audio_lang = normalize_lang(candidate.get("audio_lang"))
    if audio_lang in CZECH_AUDIO_LANGS:
        return "cz_audio_metadata"
    if lang_class in {"CZ_DUB", "CZ_NATIVE"}:
        return "cz_audio_lang_class"
    if lang_class == "CZ_SUB":
        return "cz_subtitle_lang_class"
    return None


def has_probable_czech(candidate: dict, *, allow_subtitles: bool = False) -> tuple[bool, str]:
    """Return whether candidate is worth trying before any download.

    Priority is Czech audio. Subtitle-only sources are accepted only when
    allow_subtitles=True, so the workflow can be strict by default while still
    having a fallback mode for scarce episodes.
    """
    hint = metadata_language_hint(candidate) or title_language_hint(candidate.get("title"))
    if hint in {"cz_audio_metadata", "cz_audio_lang_class", "cz_audio_title"}:
        return True, hint
    if allow_subtitles and hint in {"cz_subtitle_lang_class", "cz_subtitle_title"}:
        return True, hint
    return False, hint or "no_czech_hint"


def whisper_language(path: Path, *, seconds: int = 90) -> tuple[str | None, float | None, str]:
    """Optionally detect language with faster-whisper if it is installed.

    This intentionally runs after download and only when WHISPER_LANGUAGE_CHECK=1.
    The early pipeline can upload based on DB/title metadata; stricter batches can
    enable this without changing the orchestrator.
    """
    if os.environ.get("WHISPER_LANGUAGE_CHECK") != "1":
        return None, None, "disabled"
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:
        return None, None, f"unavailable: {type(exc).__name__}"

    model_name = os.environ.get("WHISPER_MODEL", "small")
    device = os.environ.get("WHISPER_DEVICE", "cpu")
    global _WHISPER_MODEL, _WHISPER_MODEL_KEY
    if _WHISPER_MODEL is None or _WHISPER_MODEL_KEY != (model_name, device):
        _WHISPER_MODEL = WhisperModel(model_name, device=device, compute_type="int8")
        _WHISPER_MODEL_KEY = (model_name, device)
    model = _WHISPER_MODEL
    if path.suffix.lower() in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
        _segments, info = model.transcribe(str(path), beam_size=1, vad_filter=True)
        return info.language, float(info.language_probability or 0.0), "ok"

    with tempfile.TemporaryDirectory() as td:
        sample = Path(td) / "sample.wav"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "180",
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(sample),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not sample.exists():
            return None, None, f"ffmpeg_failed: {proc.stderr.strip()[:200]}"
        _segments, info = model.transcribe(str(sample), beam_size=1, vad_filter=True)
        return info.language, float(info.language_probability or 0.0), "ok"
