"""Tests for reading, judging and parsing .lrc files without losing or
mangling anything (utils.read_lrc_text, lrc_status_key, parse_lrc_file)."""

from utils import (
    lrc_metadata_lines, lrc_status_key, ms_to_lrc_timestamp, parse_lrc_file,
    parse_lrc_timestamp, read_lrc_text,
)


def _write(tmp_path, data: bytes, name="song.lrc"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# -- reading any encoding (a Latin-1 file used to abort the app on startup) --

def test_reads_latin1_files(tmp_path):
    path = _write(tmp_path, "[00:01.00] Café\n".encode("latin-1"))
    assert read_lrc_text(path) == "[00:01.00] Café\n"


def test_reads_utf16_files_with_a_bom(tmp_path):
    path = _write(tmp_path, "[00:01.00] Café\n".encode("utf-16"))
    assert read_lrc_text(path) == "[00:01.00] Café\n"


def test_strips_a_utf8_bom(tmp_path):
    path = _write(tmp_path, "﻿[ar:Rosalía]\n[00:01.00] x\n".encode("utf-8"))
    assert read_lrc_text(path).startswith("[ar:Rosalía]")


def test_status_of_a_latin1_file_does_not_raise(tmp_path):
    path = _write(tmp_path, "[00:01.00] Café\n[00:03.00] b\n[00:05.00] c\n".encode("latin-1"))
    assert lrc_status_key(path) == "synced"


# -- status: the blank starter row is not "synced" ---------------------------

def test_a_file_with_only_an_empty_stamp_counts_as_missing(tmp_path):
    assert lrc_status_key(_write(tmp_path, b"[00:00.00] \n")) == "missing"


def test_lines_all_at_zero_are_plain_not_synced(tmp_path):
    data = b"[00:00.00] one\n[00:00.00] two\n[00:00.00] three\n"
    assert lrc_status_key(_write(tmp_path, data)) == "plain"


def test_real_timing_is_synced(tmp_path):
    data = b"[00:00.00] intro\n[00:12.30] one\n[00:15.00] two\n"
    assert lrc_status_key(_write(tmp_path, data)) == "synced"


# -- parsing keeps what it doesn't edit ---------------------------------------

def test_stamps_without_a_fraction_are_read():
    lines, _ = parse_lrc_file("[01:02] text\n")
    assert lines == [("[01:02.00]", "text")]


def test_stamps_past_100_minutes_round_trip():
    lines, _ = parse_lrc_file("[100:05.00] late\n")
    assert lines == [("[100:05.00]", "late")]
    assert parse_lrc_timestamp("[100:05.00]") == 6_005_000
    assert ms_to_lrc_timestamp(6_005_000) == "[100:05.00]"


def test_section_labels_in_brackets_are_kept_as_lines():
    lines, _ = parse_lrc_file("[Chorus]\n[00:10.00] sung\n")
    assert lines[0][1] == "[Chorus]"


def test_metadata_is_kept_for_saving_but_offset_is_not():
    text = "﻿[ar:Rosalía]\n[ti:Malamente]\n[offset:500]\n[00:01.00] x\n"
    assert lrc_metadata_lines(text) == ["[ar:Rosalía]", "[ti:Malamente]"]
