"""Tests for lrc_studio/lyrics_qa.py's pure helpers: sample-index coverage,
per-line clip-duration capping, and text normalization/scoring. These don't
touch faster-whisper or ffmpeg (neither is installed in this environment) —
model.transcribe() itself isn't covered here, only the logic around it.
"""

from rapidfuzz import fuzz

from lyrics_qa import (
    CLIP_DURATION_S,
    CLIP_LEAD_IN_S,
    MIN_CLIP_DURATION_S,
    TEXT_MATCH_THRESHOLD,
    _clip_duration_for,
    _normalize,
    _pick_sample_indices,
    is_available,
)


# ---------------------------------------------------------------------------
# _pick_sample_indices
# ---------------------------------------------------------------------------

def test_pick_sample_indices_returns_everything_when_fewer_lines_than_samples():
    assert _pick_sample_indices(3, max_samples=5) == [0, 1, 2]


def test_pick_sample_indices_includes_the_last_line():
    indices = _pick_sample_indices(40, max_samples=5)
    assert indices[-1] == 39


def test_pick_sample_indices_includes_the_first_line():
    indices = _pick_sample_indices(40, max_samples=5)
    assert indices[0] == 0


def test_pick_sample_indices_spreads_evenly_and_has_no_duplicates():
    indices = _pick_sample_indices(100, max_samples=5)
    assert len(indices) == len(set(indices)) == 5
    assert indices == sorted(indices)


def test_pick_sample_indices_handles_single_sample():
    indices = _pick_sample_indices(40, max_samples=1)
    assert len(indices) == 1
    assert 0 <= indices[0] < 40


# ---------------------------------------------------------------------------
# _clip_duration_for
# ---------------------------------------------------------------------------

def test_clip_duration_uses_full_length_for_the_last_line():
    assert _clip_duration_for(expected_ms=60_000, next_ms=None) == CLIP_DURATION_S


def test_clip_duration_uses_full_length_when_next_line_is_far_away():
    # Next line is 30s later — far beyond CLIP_DURATION_S, so no capping needed.
    duration = _clip_duration_for(expected_ms=10_000, next_ms=40_000)
    assert duration == CLIP_DURATION_S


def test_clip_duration_caps_at_the_next_line_to_avoid_bleed():
    # Next line starts just 2s later (fast verse) — clip must not run past it.
    duration = _clip_duration_for(expected_ms=10_000, next_ms=12_000)
    expected = 2.0 + CLIP_LEAD_IN_S
    assert duration == expected
    assert duration < CLIP_DURATION_S


def test_clip_duration_never_drops_below_the_floor():
    # Lines only 0.2s apart — capping naively would produce a near-zero clip.
    duration = _clip_duration_for(expected_ms=10_000, next_ms=10_200)
    assert duration == MIN_CLIP_DURATION_S


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------

def test_normalize_lowercases_and_strips_punctuation():
    assert _normalize("Don't Stop Believin'!") == "dont stop believin"


def test_normalize_collapses_to_matching_forms():
    assert _normalize("Hey, you!") == _normalize("hey you")


# ---------------------------------------------------------------------------
# Scoring behavior (documents the threshold choice, not a specific song)
# ---------------------------------------------------------------------------

def test_partial_scoring_tolerates_trailing_adlib_bleed():
    expected = _normalize("I can't help falling in love")
    heard = _normalize("I can't help falling in love yeah")
    score = fuzz.partial_token_sort_ratio(expected, heard)
    assert score >= TEXT_MATCH_THRESHOLD


def test_partial_scoring_still_rejects_a_genuinely_different_line():
    expected = _normalize("I can't help falling in love")
    heard = _normalize("Take my hand take my whole life too")
    score = fuzz.partial_token_sort_ratio(expected, heard)
    assert score < TEXT_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

def test_is_available_returns_bool_without_raising():
    # faster-whisper isn't installed in this environment — just confirm
    # the broadened except clause doesn't let anything escape.
    assert is_available() in (True, False)
