"""Tests for postprocess: naming and moving a finished download."""

import shutil
import subprocess

import pytest

import postprocess
from postprocess import FinalizeOptions, finalize_download, options_from_settings, strip_track_number


class _Settings:
    def __init__(self, values):
        self._values = values

    def value(self, key, default=None):
        return self._values.get(key, default)


def test_strip_track_number():
    assert strip_track_number("01 - Manu Chao - Clandestino") == "Manu Chao - Clandestino"
    assert strip_track_number("05. Song") == "Song"
    assert strip_track_number("1999") == "1999"   # no separator, not a track number


def test_options_from_settings_parses_ini_strings(tmp_path):
    opts = options_from_settings(_Settings({
        "library_dir": str(tmp_path), "convert_lossless": "false",
        "opus_bitrate": "160", "rename_downloads": "true",
    }))
    assert opts == FinalizeOptions(str(tmp_path), False, 160, True)


def test_finalize_without_tags_strips_track_number_and_moves(tmp_path):
    downloads, library = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    src = downloads / "07 - Some Artist - Some Song.mp3"
    src.write_bytes(b"not really audio")

    dest = finalize_download(str(src), FinalizeOptions(str(library), convert_lossless=False))

    assert dest == str(library / "Some Artist - Some Song.mp3")
    assert not src.exists()


def test_finalize_never_overwrites_an_existing_library_file(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    (library / "Song.mp3").write_bytes(b"old")
    src = tmp_path / "Song.mp3"
    src.write_bytes(b"new")

    dest = finalize_download(str(src), FinalizeOptions(str(library), convert_lossless=False))

    assert dest == str(library / "Song (2).mp3")
    assert (library / "Song.mp3").read_bytes() == b"old"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_finalize_converts_lossless_and_renames_from_tags(tmp_path):
    from mutagen.flac import FLAC

    src = tmp_path / "03 - whatever.flac"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-ac", "2", str(src)],
        check=True,
    )
    audio = FLAC(str(src))
    audio["artist"] = "Rosalía"
    audio["title"] = "Malamente: Cap.1"
    audio.save()

    dest = finalize_download(str(src), FinalizeOptions(str(tmp_path / "lib")))

    assert dest == str(tmp_path / "lib" / "Rosalía - Malamente Cap.1.opus")
    assert not src.exists()
    assert postprocess.tag_based_stem(dest) == "Rosalía - Malamente Cap.1"
