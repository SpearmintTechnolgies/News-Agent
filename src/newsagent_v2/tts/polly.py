"""Amazon Polly text-to-speech for the News Agent.

The bot runs on the AWS hub (EC2, us-east-1). The instance IAM role already
allows polly:SynthesizeSpeech and polly:DescribeVoices, so boto3's default
credential chain is used as-is. Never put AWS keys in code or .env.

Indian voices are excluded everywhere: any voice whose LanguageCode ends in
"-IN" (en-IN, hi-IN, ...) is dropped from listings, and the default voice is
Ruth (en-US). No Indian voice can be picked or defaulted to.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGION = "us-east-1"
DEFAULT_VOICE_ID = "Ruth"  # en-US, supports the generative engine
VOICE_ENV = "NEWSAGENT_V2_TTS_VOICE"

# Best-sounding engine first; the first one a voice supports is used.
ENGINE_PREFERENCE = ("generative", "long-form", "neural", "standard")

# Polly caps one SynthesizeSpeech request at ~3000 characters. Stay under it.
MAX_REQUEST_CHARS = 3000

VOICE_CACHE_TTL_SECONDS = 24 * 60 * 60
VOICE_CACHE_PATH = Path("data/v5_state/tts_voices.json")


class PollyError(RuntimeError):
    """Polly is unavailable or refused the request. Message is safe to show."""


@dataclass(frozen=True)
class Voice:
    id: str
    language_code: str
    language_name: str
    gender: str
    engines: tuple[str, ...]

    @property
    def best_engine(self) -> str:
        return best_engine(self.engines)

    def label(self) -> str:
        """Picker label: 'Ruth · Female · generative'."""
        gender = self.gender or "Unknown"
        return f"{self.id} · {gender} · {self.best_engine}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "language_code": self.language_code,
            "language_name": self.language_name,
            "gender": self.gender,
            "engines": list(self.engines),
        }

    @staticmethod
    def from_dict(row: Any) -> "Voice | None":
        if not isinstance(row, dict):
            return None
        voice_id = str(row.get("id") or "").strip()
        language_code = str(row.get("language_code") or "").strip()
        if not voice_id or not language_code:
            return None
        engines = row.get("engines")
        if not isinstance(engines, (list, tuple)):
            engines = []
        return Voice(
            id=voice_id,
            language_code=language_code,
            language_name=str(row.get("language_name") or "").strip(),
            gender=str(row.get("gender") or "").strip(),
            engines=tuple(str(e) for e in engines if e),
        )


def default_voice_id(environ: dict[str, str] | None = None) -> str:
    """The configured default voice. Never Indian; Ruth when unset."""
    env = environ if environ is not None else os.environ
    chosen = str(env.get(VOICE_ENV) or "").strip()
    return chosen or DEFAULT_VOICE_ID


def best_engine(engines: Any) -> str:
    """First engine in ENGINE_PREFERENCE the voice supports."""
    supported = {str(engine).strip().lower() for engine in (engines or ()) if engine}
    for engine in ENGINE_PREFERENCE:
        if engine in supported:
            return engine
    # Voice supports nothing we prefer (unexpected): let Polly decide.
    return "standard"


def is_indian_voice(language_code: str) -> bool:
    """en-IN, hi-IN, ... are excluded from every listing and default."""
    return str(language_code or "").strip().upper().endswith("-IN")


def _voice_from_api(row: Any) -> Voice | None:
    if not isinstance(row, dict):
        return None
    voice_id = str(row.get("Id") or "").strip()
    language_code = str(row.get("LanguageCode") or "").strip()
    if not voice_id or not language_code:
        return None
    engines = row.get("SupportedEngines")
    if not isinstance(engines, (list, tuple)):
        engines = []
    return Voice(
        id=voice_id,
        language_code=language_code,
        language_name=str(row.get("LanguageName") or "").strip(),
        gender=str(row.get("Gender") or "").strip(),
        engines=tuple(str(e) for e in engines if e),
    )


def _default_client() -> Any:
    """boto3 default credential chain (the EC2 instance role). Lazy import so
    this module loads without boto3 installed (tests inject a mock client)."""
    try:
        import boto3
    except ImportError as exc:
        raise PollyError("boto3 is not installed on this host") from exc
    return boto3.client("polly", region_name=REGION)


def _describe_all_voices(client: Any) -> list[Voice]:
    """Every voice Polly knows about, following NextToken paging."""
    voices: list[Voice] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if next_token:
            kwargs["NextToken"] = next_token
        try:
            response = client.describe_voices(**kwargs)
        except Exception as exc:
            raise PollyError(f"describe_voices failed: {exc}") from exc
        rows = response.get("Voices") if isinstance(response, dict) else None
        for row in rows or []:
            voice = _voice_from_api(row)
            if voice is not None:
                voices.append(voice)
        next_token = response.get("NextToken") if isinstance(response, dict) else None
        if not next_token:
            break
    return voices


_cache_lock = threading.Lock()
_memory_cache: dict[str, Any] = {"fetched_at": 0.0, "voices": []}


def _read_file_cache(cache_path: Path, now: float) -> list[Voice] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        fetched_at = float(payload.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return None
    if fetched_at <= 0 or (now - fetched_at) >= VOICE_CACHE_TTL_SECONDS:
        return None
    rows = payload.get("voices")
    if not isinstance(rows, list):
        return None
    voices = [voice for voice in (Voice.from_dict(row) for row in rows) if voice]
    return voices or None


def _write_file_cache(cache_path: Path, voices: list[Voice], now: float) -> None:
    payload = {
        "fetched_at": now,
        "voices": [voice.to_dict() for voice in voices],
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def list_voices(
    *,
    client: Any | None = None,
    cache_path: Path | None = None,
    now: float | None = None,
) -> list[Voice]:
    """Every non-Indian Polly voice. Cached for 24 hours (memory + disk).

    ``client``/``cache_path``/``now`` are injectable so tests never touch AWS.
    """
    path = cache_path or VOICE_CACHE_PATH
    stamp = time.time() if now is None else float(now)
    with _cache_lock:
        cached_at = float(_memory_cache.get("fetched_at") or 0)
        memory_voices = _memory_cache.get("voices") or []
        if memory_voices and (stamp - cached_at) < VOICE_CACHE_TTL_SECONDS:
            return list(memory_voices)
        file_voices = _read_file_cache(path, stamp)
        if file_voices:
            _memory_cache.update({"fetched_at": stamp, "voices": file_voices})
            return list(file_voices)

    # Fetch outside the lock: describe_voices can take a moment.
    polly = client or _default_client()
    voices = [v for v in _describe_all_voices(polly) if not is_indian_voice(v.language_code)]
    if not voices:
        raise PollyError("Polly returned no usable voices")

    with _cache_lock:
        _memory_cache.update({"fetched_at": stamp, "voices": voices})
    _write_file_cache(path, voices, stamp)
    return list(voices)


def find_voice(voices: list[Voice], voice_id: str) -> Voice | None:
    wanted = str(voice_id or "").strip().lower()
    for voice in voices:
        if voice.id.lower() == wanted:
            return voice
    return None


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")


def chunk_text(text: str, max_chars: int = MAX_REQUEST_CHARS) -> list[str]:
    """Split on sentence boundaries into chunks Polly accepts in one request.

    A single sentence longer than the cap is hard-split on whitespace, then on
    the cap itself, so no chunk ever exceeds ``max_chars``.
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return []
    sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(cleaned) if part.strip()]
    pieces: list[str] = []
    for sentence in sentences:
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def synthesize(
    text: str,
    voice_id: str | None = None,
    engine: str | None = None,
    *,
    client: Any | None = None,
) -> bytes:
    """Synthesize speech to joined mp3 bytes.

    ``engine=None`` picks the best engine the voice supports: generative,
    then long-form, then neural, then standard. Long text is split on sentence
    boundaries (Polly caps a request at ~3000 characters) and the mp3 chunks
    are concatenated.
    """
    voice = str(voice_id or "").strip() or default_voice_id()
    chunks = chunk_text(text, MAX_REQUEST_CHARS)
    if not chunks:
        raise PollyError("nothing to synthesize")

    polly = client or _default_client()
    chosen_engine = str(engine or "").strip().lower()
    if not chosen_engine:
        voices = list_voices(client=polly)
        match = find_voice(voices, voice)
        if match is None:
            raise PollyError(f"unknown Polly voice: {voice}")
        chosen_engine = match.best_engine
    if chosen_engine not in ENGINE_PREFERENCE:
        raise PollyError(f"unsupported Polly engine: {chosen_engine}")

    parts: list[bytes] = []
    for chunk in chunks:
        try:
            response = polly.synthesize_speech(
                Text=chunk,
                TextType="text",
                OutputFormat="mp3",
                VoiceId=voice,
                Engine=chosen_engine,
            )
        except Exception as exc:
            raise PollyError(f"synthesize_speech failed: {exc}") from exc
        stream = response.get("AudioStream") if isinstance(response, dict) else None
        if stream is None:
            raise PollyError("Polly returned no AudioStream")
        data = stream.read()
        if not data:
            raise PollyError("Polly returned empty audio")
        parts.append(data)
    return b"".join(parts)


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_NOISE = re.compile(r"[*_`#>~]")


def clean_for_speech(text: str) -> str:
    """Strip markup so the narrator reads words, not punctuation names."""
    cleaned = _MARKDOWN_LINK.sub(r"\1", str(text or ""))
    cleaned = _HTML_TAG.sub(" ", cleaned)
    cleaned = _MARKDOWN_NOISE.sub("", cleaned)
    return " ".join(cleaned.split())
