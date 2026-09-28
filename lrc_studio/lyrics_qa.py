"""lyrics_qa.py — optional, local, no-network sync spot-check.

This is deliberately not a full re-transcription of the song: to keep it
fast enough to run on a laptop CPU without a long wait, it samples a
handful of short clips around existing verse timestamps and cross-checks
just those against faster-whisper's transcription — both for text (does
the lyric line match what's actually sung?) and timing (does the line
start where the .lrc says it does?).

It's a heuristic first pass, not a perfect oracle: background noise, an
instrumental intro bleeding into a clip, or a genuinely unusual vocal
delivery can all produce a false flag. Treat results as "worth a listen,"
not gospel.

faster-whisper is a soft dependency — it's only imported by app.py/
workers.py when the user actually runs Validate Sync, and importing
faster_whisper itself is deferred to is_available()/validate_sync() so
the base app runs fine without it installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from rapidfuzz import fuzz

DEFAULT_MODEL_SIZE = "base"
CLIP_DURATION_S = 8.0
MIN_CLIP_DURATION_S = 1.5         # floor, for lines packed very close together
CLIP_LEAD_IN_S = 1.0              # start each clip slightly before its stamped timestamp
MAX_SAMPLES = 5
# rapidfuzz partial_token_sort_ratio (0-100); below this -> text mismatch.
# Partial/substring-aligned rather than whole-string so a stray ad-lib or
# backing-vocal harmony Whisper picks up doesn't dilute an otherwise
# correct match the way a whole-string ratio would.
TEXT_MATCH_THRESHOLD = 60
# Measured against faster-whisper directly (see dev notes): the first word
# of a clip gets an unreliable timestamp — it's systematically anchored
# toward 0 rather than the true onset, by up to ~1s in testing, a known
# characteristic of Whisper's word-timestamp mechanism rather than a bug
# here. This threshold is deliberately loose so that bias doesn't produce
# false positives; it's tuned to catch genuinely wrong sync (several
# seconds off — wrong global offset, mismatched lyrics version), not to
# validate sub-second accuracy.
TIMING_DRIFT_THRESHOLD_MS = 2500


class WhisperUnavailable(Exception):
    """Raised when faster-whisper isn't installed."""


@dataclass
class LineCheck:
    verse_index: int
    expected_text: str
    expected_ms: int
    heard_text: str
    text_score: int                  # 0-100
    timing_drift_ms: int | None      # None if no speech was detected in the clip
    verdict: str                     # "ok" | "text_mismatch" | "timing_drift" | "no_audio_match"


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except (ImportError, OSError, RuntimeError):
        # OSError/RuntimeError: faster-whisper is installed but its native
        # ctranslate2 backend failed to load (e.g. a CPU/CUDA build
        # mismatch) — that should read as "unavailable," not crash.
        return False


def _pick_sample_indices(n_lines: int, max_samples: int = MAX_SAMPLES) -> list[int]:
    """Evenly spread up to *max_samples* indices across ``[0, n_lines)``,
    including the last line rather than stopping short of it."""
    if n_lines <= max_samples:
        return list(range(n_lines))
    if max_samples <= 1:
        return [n_lines // 2]
    step = (n_lines - 1) / (max_samples - 1)
    return sorted({round(i * step) for i in range(max_samples)})


def _extract_clip(
    audio_path: str, expected_ms: int, out_path: str, duration_s: float = CLIP_DURATION_S,
) -> None:
    start_s = max(0.0, expected_ms / 1000.0 - CLIP_LEAD_IN_S)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start_s:.2f}", "-t", f"{duration_s:.2f}",
            "-i", audio_path,
            "-ar", "16000", "-ac", "1",
            out_path,
        ],
        check=True,
        capture_output=True,
    )


def _clip_duration_for(expected_ms: int, next_ms: int | None) -> float:
    """How long a sampled line's clip should run.

    Capped at the next synced line's start (plus the lead-in, since the
    clip begins slightly before *expected_ms*) so the transcript doesn't
    bleed into the next line's words and dilute the match against this
    one — the main source of false "text mismatch" flags on lines that
    are actually synced correctly, especially in faster-paced verses.
    """
    if next_ms is None:
        return CLIP_DURATION_S
    available_s = (next_ms - expected_ms) / 1000.0 + CLIP_LEAD_IN_S
    return max(MIN_CLIP_DURATION_S, min(CLIP_DURATION_S, available_s))


def _normalize(text: str) -> str:
    """Lowercase and drop punctuation before fuzzy-matching, so quoting/
    punctuation differences between the .lrc text and Whisper's own
    transcription style don't affect the score."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def validate_sync(
    audio_path: str,
    lines: list[tuple[int, str, int]],
    model_size: str = DEFAULT_MODEL_SIZE,
    max_samples: int = MAX_SAMPLES,
    progress_callback: Callable[[str], None] | None = None,
) -> list[LineCheck]:
    """Spot-check a sample of *lines* against the audio.

    ``lines`` is ``[(verse_index, text, timestamp_ms), ...]`` — only lines
    with a real (already-synced) timestamp should be passed in; the caller
    is expected to filter those out, same as the karaoke display does.
    """
    if not is_available():
        raise WhisperUnavailable(
            "faster-whisper isn't installed. Run: pip install faster-whisper"
        )
    from faster_whisper import WhisperModel

    if not lines:
        return []

    sample_indices = _pick_sample_indices(len(lines), max_samples)

    if progress_callback:
        progress_callback(f"Loading Whisper model ({model_size})…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    results: list[LineCheck] = []
    with tempfile.TemporaryDirectory(prefix="lrcstudio_qa_") as tmp_dir:
        for n, idx in enumerate(sample_indices, 1):
            verse_index, expected_text, expected_ms = lines[idx]
            next_ms = lines[idx + 1][2] if idx + 1 < len(lines) else None

            if progress_callback:
                progress_callback(f"Checking line {n}/{len(sample_indices)}: \"{expected_text[:40]}\"")

            clip_path = os.path.join(tmp_dir, f"clip_{n}.wav")
            try:
                _extract_clip(
                    audio_path, expected_ms, clip_path,
                    duration_s=_clip_duration_for(expected_ms, next_ms),
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                results.append(LineCheck(
                    verse_index=verse_index, expected_text=expected_text,
                    expected_ms=expected_ms, heard_text="", text_score=0,
                    timing_drift_ms=None, verdict="no_audio_match",
                ))
                continue

            segments, _info = model.transcribe(
                clip_path, word_timestamps=True,
                vad_filter=True, condition_on_previous_text=False,
            )
            segments = list(segments)
            heard_text = " ".join(s.text.strip() for s in segments).strip()
            text_score = (
                int(fuzz.partial_token_sort_ratio(_normalize(expected_text), _normalize(heard_text)))
                if heard_text else 0
            )

            # Where did the model think speech actually started, relative to
            # where we expected it (CLIP_LEAD_IN_S into the clip)?
            words = [w for s in segments for w in (s.words or [])]
            timing_drift_ms = None
            if words:
                actual_ms = expected_ms - CLIP_LEAD_IN_S * 1000 + words[0].start * 1000
                timing_drift_ms = int(actual_ms - expected_ms)

            if not heard_text:
                verdict = "no_audio_match"
            elif text_score < TEXT_MATCH_THRESHOLD:
                verdict = "text_mismatch"
            elif timing_drift_ms is not None and abs(timing_drift_ms) > TIMING_DRIFT_THRESHOLD_MS:
                verdict = "timing_drift"
            else:
                verdict = "ok"

            results.append(LineCheck(
                verse_index=verse_index, expected_text=expected_text,
                expected_ms=expected_ms, heard_text=heard_text,
                text_score=text_score, timing_drift_ms=timing_drift_ms,
                verdict=verdict,
            ))

    return results
