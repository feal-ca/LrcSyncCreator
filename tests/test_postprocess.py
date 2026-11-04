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


# -- Standardize names (library right-click "Rename to “Artist - Title”") ----

def _fake_tags(monkeypatch, mapping):
    """Pretend *mapping* {basename: "Artist - Title"} are the files' tags."""
    import os
    monkeypatch.setattr(postprocess, "tag_based_stem",
                        lambda path: mapping.get(os.path.basename(path)))


def test_plan_standard_names_uses_tags(tmp_path, monkeypatch):
    song = tmp_path / "01 track.mp3"
    song.write_bytes(b"x")
    _fake_tags(monkeypatch, {"01 track.mp3": "Manu Chao - Clandestino"})

    [plan] = postprocess.plan_standard_names([str(song)])

    assert plan.old == str(song)
    assert plan.new == str(tmp_path / "Manu Chao - Clandestino.mp3")


def test_plan_skips_files_without_tags(tmp_path, monkeypatch):
    song = tmp_path / "track07.mp3"
    song.write_bytes(b"x")
    _fake_tags(monkeypatch, {})

    [plan] = postprocess.plan_standard_names([str(song)])

    assert plan.new is None
    assert plan.reason == "No artist/title tags"


def test_plan_skips_files_already_named_that_way(tmp_path, monkeypatch):
    song = tmp_path / "Estopa - Como Camarón.opus"
    song.write_bytes(b"x")
    _fake_tags(monkeypatch, {song.name: "Estopa - Como Camarón"})

    [plan] = postprocess.plan_standard_names([str(song)])

    assert plan.new is None
    assert plan.reason == "Already named that way"


def test_plan_skips_second_file_with_the_same_tags(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    _fake_tags(monkeypatch, {"a.mp3": "X - Y", "b.mp3": "X - Y"})

    first, second = postprocess.plan_standard_names([str(a), str(b)])

    assert first.new == str(tmp_path / "X - Y.mp3")
    assert second.new is None
    assert "already" in second.reason


def test_plan_skips_when_target_exists_on_disk(tmp_path, monkeypatch):
    song = tmp_path / "a.mp3"
    song.write_bytes(b"a")
    (tmp_path / "X - Y.mp3").write_bytes(b"other")
    _fake_tags(monkeypatch, {"a.mp3": "X - Y"})

    [plan] = postprocess.plan_standard_names([str(song)])

    assert plan.new is None


def test_apply_renames_moves_audio_and_lrc(tmp_path):
    song = tmp_path / "a.mp3"
    song.write_bytes(b"audio")
    (tmp_path / "a.lrc").write_text("[00:01.00] hi", encoding="utf-8")
    target = str(tmp_path / "X - Y.mp3")

    done = postprocess.apply_renames([postprocess.RenamePlan(str(song), target, "")])

    assert done == [(str(song), target)]
    assert (tmp_path / "X - Y.mp3").read_bytes() == b"audio"
    assert (tmp_path / "X - Y.lrc").read_text(encoding="utf-8") == "[00:01.00] hi"
    assert not song.exists()


def test_apply_renames_never_overwrites(tmp_path):
    song = tmp_path / "a.mp3"
    song.write_bytes(b"audio")
    (tmp_path / "X - Y.mp3").write_bytes(b"keep me")

    done = postprocess.apply_renames(
        [postprocess.RenamePlan(str(song), str(tmp_path / "X - Y.mp3"), "")]
    )

    assert done == []
    assert (tmp_path / "X - Y.mp3").read_bytes() == b"keep me"
    assert song.exists()
