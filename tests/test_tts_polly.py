"""Amazon Polly TTS tests. Every Polly call is mocked - no real AWS traffic.

Covers: Indian voices filtered out (listings and default), engine fallback
order (generative -> long-form -> neural -> standard), sentence-boundary
chunking with the ~3000 character cap, NextToken paging, the 24h voice cache,
and the per-chat voice store.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from newsagent_v2.tts import polly
from newsagent_v2.tts.polly import (
    DEFAULT_VOICE_ID,
    PollyError,
    Voice,
    best_engine,
    chunk_text,
    clean_for_speech,
    default_voice_id,
    is_indian_voice,
    list_voices,
    synthesize,
)
from newsagent_v2.telegram import voice_picker


def _voice_row(voice_id, language_code, language_name, gender, engines):
    return {
        "Id": voice_id,
        "LanguageCode": language_code,
        "LanguageName": language_name,
        "Gender": gender,
        "SupportedEngines": list(engines),
    }


RUTH = _voice_row("Ruth", "en-US", "English (US)", "Female", ["generative", "neural", "standard"])
JOANNA = _voice_row("Joanna", "en-US", "English (US)", "Female", ["neural", "standard"])
MATTHEW = _voice_row("Matthew", "en-US", "English (US)", "Male", ["generative", "neural", "standard"])
BRIAN = _voice_row("Brian", "en-GB", "English (British)", "Male", ["neural", "standard"])
ADITI = _voice_row("Aditi", "en-IN", "English (Indian)", "Female", ["neural", "standard"])
KAJAL = _voice_row("Kajal", "hi-IN", "Hindi", "Female", ["generative", "neural", "standard"])
LUPE = _voice_row("Lupe", "es-US", "Spanish (US)", "Female", ["generative", "long-form", "neural", "standard"])
HANS = _voice_row("Hans", "de-DE", "German", "Male", ["standard"])


class FakePollyClient:
    """In-memory Polly stand-in. Records every call."""

    def __init__(self, pages, audio=b"\xff\xfbMP3"):
        # pages: list of (voice_rows, next_token_or_None)
        self._pages = list(pages)
        self._audio = audio
        self.describe_calls: list[dict] = []
        self.synth_calls: list[dict] = []

    def describe_voices(self, **kwargs):
        self.describe_calls.append(kwargs)
        token = kwargs.get("NextToken")
        if token is None:
            index = 0
        else:
            try:
                index = int(token)
            except ValueError:
                raise RuntimeError(f"bad NextToken {token!r}")
        rows, next_token = self._pages[index]
        payload = {"Voices": rows}
        if next_token is not None:
            payload["NextToken"] = next_token
        return payload

    def synthesize_speech(self, **kwargs):
        self.synth_calls.append(kwargs)
        # Echo the text inside the audio so order/length are assertable.
        return {"AudioStream": io.BytesIO(self._audio + kwargs["Text"].encode("utf-8"))}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Fresh voice cache per test, and any stray file writes land in tmp."""
    monkeypatch.chdir(tmp_path)
    with polly._cache_lock:
        polly._memory_cache.update({"fetched_at": 0.0, "voices": []})
    yield
    with polly._cache_lock:
        polly._memory_cache.update({"fetched_at": 0.0, "voices": []})


# ============ INDIAN VOICES EXCLUDED ============

def test_indian_voices_are_filtered_from_listings():
    client = FakePollyClient([([RUTH, ADITI, KAJAL, BRIAN], None)])
    voices = list_voices(client=client)
    codes = {voice.language_code for voice in voices}
    ids = {voice.id for voice in voices}
    assert ids == {"Ruth", "Brian"}
    assert all(not code.upper().endswith("-IN") for code in codes)


def test_indian_voice_detector():
    assert is_indian_voice("en-IN")
    assert is_indian_voice("hi-IN")
    assert is_indian_voice("ta-IN")
    assert not is_indian_voice("en-US")
    assert not is_indian_voice("")


def test_default_voice_is_not_indian():
    assert default_voice_id({}) == DEFAULT_VOICE_ID == "Ruth"  # en-US
    # An env override is honored; the built-in default is never Indian.
    assert default_voice_id({"NEWSAGENT_V2_TTS_VOICE": "Matthew"}) == "Matthew"


# ============ ENGINE FALLBACK ORDER ============

def test_best_engine_preference_order():
    assert best_engine(["standard", "neural", "generative", "long-form"]) == "generative"
    assert best_engine(["standard", "neural", "long-form"]) == "long-form"
    assert best_engine(["standard", "neural"]) == "neural"
    assert best_engine(["standard"]) == "standard"
    # Unknown/empty engine lists degrade to standard instead of guessing.
    assert best_engine([]) == "standard"


def test_synthesize_picks_best_supported_engine():
    client = FakePollyClient([([RUTH, JOANNA], None)])
    synthesize("Hello there.", "Joanna", client=client)
    assert client.synth_calls[0]["Engine"] == "neural"  # no generative on Joanna
    synthesize("Hello there.", "Ruth", client=client)
    assert client.synth_calls[1]["Engine"] == "generative"


def test_synthesize_explicit_engine_wins():
    client = FakePollyClient([([RUTH], None)])
    synthesize("Hello there.", "Ruth", engine="standard", client=client)
    assert client.synth_calls[0]["Engine"] == "standard"


def test_synthesize_rejects_unsupported_engine_and_unknown_voice():
    client = FakePollyClient([([RUTH], None)])
    with pytest.raises(PollyError):
        synthesize("Hello there.", "Ruth", engine="crystal", client=client)
    with pytest.raises(PollyError):
        synthesize("Hello there.", "NoSuchVoice", client=client)


# ============ PAGING + CACHE ============

def test_describe_voices_follows_next_token():
    pages = [
        ([RUTH], "1"),
        ([JOANNA, BRIAN], "2"),
        ([HANS], None),
    ]
    client = FakePollyClient(pages)
    voices = list_voices(client=client)
    assert [voice.id for voice in voices] == ["Ruth", "Joanna", "Brian", "Hans"]
    assert client.describe_calls == [{}, {"NextToken": "1"}, {"NextToken": "2"}]


def test_voice_list_cached_for_24_hours(tmp_path):
    cache = tmp_path / "tts_voices.json"
    client = FakePollyClient([([RUTH], None)])
    now = time.time()
    first = list_voices(client=client, cache_path=cache, now=now)
    # Memory cache hit: no new describe call, same content.
    again = list_voices(client=client, cache_path=cache, now=now + 60)
    assert len(client.describe_calls) == 1
    assert [v.id for v in again] == [v.id for v in first]
    # Simulate a restart: memory cleared, disk cache still fresh.
    with polly._cache_lock:
        polly._memory_cache.update({"fetched_at": 0.0, "voices": []})
    from_disk = list_voices(client=client, cache_path=cache, now=now + 3600)
    assert len(client.describe_calls) == 1
    assert [v.id for v in from_disk] == ["Ruth"]
    # After the TTL, Polly is asked again.
    with polly._cache_lock:
        polly._memory_cache.update({"fetched_at": 0.0, "voices": []})
    list_voices(client=client, cache_path=cache, now=now + polly.VOICE_CACHE_TTL_SECONDS + 1)
    assert len(client.describe_calls) == 2


def test_disk_cache_excludes_indian_voices(tmp_path):
    cache = tmp_path / "tts_voices.json"
    client = FakePollyClient([([RUTH, KAJAL], None)])
    now = time.time()
    list_voices(client=client, cache_path=cache, now=now)
    with polly._cache_lock:
        polly._memory_cache.update({"fetched_at": 0.0, "voices": []})
    voices = list_voices(client=client, cache_path=cache, now=now + 60)
    assert [v.id for v in voices] == ["Ruth"]


# ============ CHUNKING ============

def _sentence(index: int, words: int = 40) -> str:
    return (" ".join(f"w{index}x{n}" for n in range(words))) + "."


def test_chunk_text_respects_cap_and_boundaries():
    text = " ".join(_sentence(i) for i in range(40))  # ~10k chars of sentences
    chunks = chunk_text(text, max_chars=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    # Sentences stay whole: every chunk (except possibly the last) ends at a boundary.
    for chunk in chunks[:-1]:
        assert chunk.endswith(".")
    # Nothing lost: the sentences reassemble in order.
    assert " ".join(chunks) == text


def test_chunk_text_hard_splits_a_giant_sentence():
    monster = " ".join(f"word{n}" for n in range(400))  # no sentence end, ~2400 chars
    chunks = chunk_text(monster, max_chars=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert " ".join(chunks).replace("  ", " ") == monster


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_synthesize_chunks_and_joins_in_order(monkeypatch):
    monkeypatch.setattr(polly, "MAX_REQUEST_CHARS", 200)
    client = FakePollyClient([([RUTH], None)])
    text = " ".join(_sentence(i, words=8) for i in range(12))  # several chunks at 200 cap
    audio = synthesize(text, "Ruth", engine="neural", client=client)
    assert len(client.synth_calls) > 1
    for call in client.synth_calls:
        assert len(call["Text"]) <= 200
        assert call["VoiceId"] == "Ruth"
        assert call["OutputFormat"] == "mp3"
    expected = b"".join(b"\xff\xfbMP3" + call["Text"].encode("utf-8") for call in client.synth_calls)
    assert audio == expected


def test_synthesize_empty_text_raises():
    client = FakePollyClient([([RUTH], None)])
    with pytest.raises(PollyError):
        synthesize("   ", "Ruth", client=client)


# ============ PER-CHAT VOICE STORE ============

def test_voice_store_is_per_chat(tmp_path):
    store = tmp_path / "tts_voice.json"
    voice_picker.set_voice("-100200", "Matthew", path=store)  # group
    voice_picker.set_voice("555", "Joanna", path=store)  # DM
    assert voice_picker.voice_for("-100200", path=store) == "Matthew"
    assert voice_picker.voice_for("555", path=store) == "Joanna"
    # A chat with no saved choice gets the env default / Ruth.
    assert voice_picker.voice_for("999", path=store) == "Ruth"
    assert voice_picker.voice_for("999", path=store, environ={"NEWSAGENT_V2_TTS_VOICE": "Brian"}) == "Brian"


# ============ PICKER KEYBOARDS ============

def _voices():
    return [
        Voice(id=row["Id"], language_code=row["LanguageCode"], language_name=row["LanguageName"],
              gender=row["Gender"], engines=tuple(row["SupportedEngines"]))
        for row in (RUTH, JOANNA, MATTHEW, BRIAN, LUPE, HANS)
    ]


def test_language_keyboard_english_first_then_alphabetical():
    keyboard = voice_picker.language_keyboard(_voices())
    labels = [row[0]["text"] for row in keyboard["inline_keyboard"] if row[0]["callback_data"].startswith("voice:lang:")]
    assert labels[0].startswith("English (US)")
    assert labels[1].startswith("English (British)")
    # German before Spanish, after English.
    assert labels[2].startswith("German")
    assert labels[3].startswith("Spanish")
    # No Indian language entries anywhere.
    assert all("Indian" not in label and "Hindi" not in label for label in labels)


def test_language_keyboard_paginates_after_8():
    voices = [
        Voice(id=f"V{i:02d}", language_code=f"x{i:02d}-ZZ", language_name=f"Lang{i:02d}", gender="Female", engines=("neural",))
        for i in range(20)
    ]
    page0 = voice_picker.language_keyboard(voices, page=0)
    buttons = [b["text"] for row in page0["inline_keyboard"] for b in row if b["callback_data"].startswith("voice:lang:")]
    assert len(buttons) == voice_picker.PAGE_SIZE
    nav = [b["callback_data"] for row in page0["inline_keyboard"] for b in row if b["callback_data"].startswith("voice:langs:")]
    assert "voice:langs:1" in nav
    page1 = voice_picker.language_keyboard(voices, page=1)
    nav1 = [b["callback_data"] for row in page1["inline_keyboard"] for b in row if b["callback_data"].startswith("voice:langs:")]
    assert "voice:langs:0" in nav1  # Prev


def test_voice_keyboard_labels_and_preview():
    keyboard = voice_picker.voice_keyboard(_voices(), "en-US", selected_id="Ruth")
    rows = keyboard["inline_keyboard"]
    first = rows[0]
    # Alphabetical by name; the saved voice carries the ✓, wherever it sits.
    assert first[0]["text"] == "Joanna · Female · neural"
    assert first[0]["callback_data"] == "voice:set:Joanna"
    assert first[1]["callback_data"] == "voice:play:Joanna"
    labels = [row[0]["text"] for row in rows if row[0]["callback_data"].startswith("voice:set:")]
    assert "✓ Ruth · Female · generative" in labels
    assert "Matthew · Male · generative" in labels
    ruth_row = next(row for row in rows if row[0]["callback_data"] == "voice:set:Ruth")
    assert ruth_row[1]["callback_data"] == "voice:play:Ruth"
    # Back to languages is always present.
    callbacks = [b["callback_data"] for row in rows for b in row]
    assert "voice:langs:0" in callbacks


def test_parse_voice_callback_round_trip():
    assert voice_picker.parse_voice_callback("voice:langs:2") == {"action": "langs", "page": "2"}
    assert voice_picker.parse_voice_callback("voice:lang:en-US:1") == {"action": "lang", "language_code": "en-US", "page": "1"}
    assert voice_picker.parse_voice_callback("voice:set:Ruth") == {"action": "set", "voice_id": "Ruth"}
    assert voice_picker.parse_voice_callback("voice:play:Joanna") == {"action": "play", "voice_id": "Joanna"}
    assert voice_picker.parse_voice_callback("approve:evt-1:v1") is None
    assert voice_picker.parse_voice_callback("") is None
    assert voice_picker.parse_voice_callback("voice:set:") is None


# ============ TEXT CLEANUP ============

def test_clean_for_speech_strips_markup():
    raw = "## Headline\n\n**Bitcoin** hits [a record](https://example.com/x). <b>Live</b> now."
    cleaned = clean_for_speech(raw)
    assert cleaned == "Headline Bitcoin hits a record. Live now."
