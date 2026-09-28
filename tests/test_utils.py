"""Tests for lrc_studio/utils.py's pure timestamp/filename/LRC-parsing helpers.

Run with: pytest (from the repo root; conftest.py puts lrc_studio/ on
sys.path so these can use the same flat imports the app itself does).
"""

from constants import COLOR_MISSING, COLOR_SYNCED, COLOR_UNSYNCED
from utils import (
    analyze_lrc_status,
    clean_filename,
    format_ms,
    ms_to_lrc_timestamp,
    parse_lrc_file,
    parse_lrc_timestamp,
)


# ---------------------------------------------------------------------------
# clean_filename
# ---------------------------------------------------------------------------

def test_clean_filename_strips_blacklisted_chars():
    assert clean_filename('a:b?c"d/e\\f*g<h>i|j') == "abcdefghij"


def test_clean_filename_strips_surrounding_whitespace():
    assert clean_filename("  Track Name  ") == "Track Name"


def test_clean_filename_leaves_normal_names_untouched():
    assert clean_filename("Song (Remix) - Artist") == "Song (Remix) - Artist"


# ---------------------------------------------------------------------------
# format_ms
# ---------------------------------------------------------------------------

def test_format_ms_basic():
    assert format_ms(0) == "00:00"
    assert format_ms(65_000) == "01:05"
    assert format_ms(3_600_000) == "60:00"


def test_format_ms_clamps_negative_to_zero():
    assert format_ms(-500) == "00:00"


# ---------------------------------------------------------------------------
# ms_to_lrc_timestamp / parse_lrc_timestamp round-trip
# ---------------------------------------------------------------------------

def test_ms_to_lrc_timestamp_format():
    assert ms_to_lrc_timestamp(0) == "[00:00.00]"
    assert ms_to_lrc_timestamp(65_432) == "[01:05.43]"


def test_ms_to_lrc_timestamp_clamps_negative_to_zero():
    assert ms_to_lrc_timestamp(-100) == "[00:00.00]"


def test_parse_lrc_timestamp_centiseconds():
    assert parse_lrc_timestamp("[01:05.43]") == 65_430


def test_parse_lrc_timestamp_milliseconds_variant():
    assert parse_lrc_timestamp("[01:05.430]") == 65_430


def test_parse_lrc_timestamp_tenths_variant():
    assert parse_lrc_timestamp("[01:05.4]") == 65_400


def test_parse_lrc_timestamp_malformed_returns_zero():
    assert parse_lrc_timestamp("not a timestamp") == 0
    assert parse_lrc_timestamp("[]") == 0


def test_round_trip_ms_to_lrc_and_back():
    for ms in (0, 999, 1_000, 65_430, 3_599_990):
        assert parse_lrc_timestamp(ms_to_lrc_timestamp(ms)) == ms - (ms % 10)


# ---------------------------------------------------------------------------
# parse_lrc_file
# ---------------------------------------------------------------------------

def test_parse_lrc_file_basic_lines():
    text = "[00:01.00]First line\n[00:02.50]Second line\n"
    lines, offset = parse_lrc_file(text)
    assert offset == 0
    assert lines == [
        ("[00:01.00]", "First line"),
        ("[00:02.50]", "Second line"),
    ]


def test_parse_lrc_file_reads_offset_tag():
    text = "[offset:-250]\n[00:01.00]Line\n"
    _, offset = parse_lrc_file(text)
    assert offset == -250


def test_parse_lrc_file_skips_metadata_tags():
    text = "[ar:Some Artist]\n[ti:Some Title]\n[00:01.00]Line\n"
    lines, _ = parse_lrc_file(text)
    assert lines == [("[00:01.00]", "Line")]


def test_parse_lrc_file_expands_multiple_timestamps_on_one_line():
    text = "[00:01.00][00:05.00]Shared line\n"
    lines, _ = parse_lrc_file(text)
    assert lines == [
        ("[00:01.00]", "Shared line"),
        ("[00:05.00]", "Shared line"),
    ]


def test_parse_lrc_file_keeps_untimed_lines_with_default_stamp():
    text = "Plain lyric with no timestamp\n"
    lines, _ = parse_lrc_file(text)
    assert len(lines) == 1
    assert lines[0][1] == "Plain lyric with no timestamp"


def test_parse_lrc_file_ignores_blank_lines():
    text = "[00:01.00]Line one\n\n\n[00:02.00]Line two\n"
    lines, _ = parse_lrc_file(text)
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# analyze_lrc_status
# ---------------------------------------------------------------------------

def test_analyze_lrc_status_missing_file(tmp_path):
    assert analyze_lrc_status(str(tmp_path / "missing.lrc")) == COLOR_MISSING


def test_analyze_lrc_status_empty_file(tmp_path):
    path = tmp_path / "empty.lrc"
    path.write_text("")
    assert analyze_lrc_status(str(path)) == COLOR_MISSING


def test_analyze_lrc_status_unsynced_all_zero_timestamps(tmp_path):
    path = tmp_path / "unsynced.lrc"
    lines = "\n".join(f"[00:00.00]Line {i}" for i in range(10))
    path.write_text(lines)
    assert analyze_lrc_status(str(path)) == COLOR_UNSYNCED


def test_analyze_lrc_status_synced_varied_timestamps(tmp_path):
    path = tmp_path / "synced.lrc"
    lines = "\n".join(f"[00:{i:02d}.00]Line {i}" for i in range(1, 11))
    path.write_text(lines)
    assert analyze_lrc_status(str(path)) == COLOR_SYNCED


def test_analyze_lrc_status_no_timestamps_at_all(tmp_path):
    path = tmp_path / "notags.lrc"
    path.write_text("Just plain text, no LRC tags at all.")
    assert analyze_lrc_status(str(path)) == COLOR_UNSYNCED
