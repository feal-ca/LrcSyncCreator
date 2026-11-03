import base64
import os
import shutil
import subprocess

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TIT2
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE

import convert
from convert import (ConversionError, convert_if_lossless, convert_to_opus,
                     find_lossless_files, is_lossless, opus_target_path)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"),
                                  reason="ffmpeg not installed")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg==")


def make_audio(path, *codec_args):
    """Generate 3 s of stereo sine at ``path`` (format chosen by extension)."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", "-ac", "2", *codec_args,
         str(path)], check=True)
    return str(path)


def make_tagged_flac(path):
    make_audio(path)
    flac = FLAC(str(path))
    flac["artist"], flac["title"] = "The Artist", "The Title"
    flac["album"], flac["lyrics"] = "The Album", "la la la"
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/png", PNG
    pic.width = pic.height = 1
    pic.depth = 24
    flac.add_picture(pic)
    flac.save()
    return str(path)


def pictures_of(opus_path):
    blocks = OggOpus(opus_path).tags["metadata_block_picture"]
    return [Picture(base64.b64decode(b)) for b in blocks]


def leftovers(folder):
    return [n for n in os.listdir(folder) if "converting" in n]


@needs_ffmpeg
def test_flac_converts_in_place_with_tags_and_cover(tmp_path):
    src = make_tagged_flac(tmp_path / "x.flac")
    out = convert_to_opus(src)

    assert out == str(tmp_path / "x.opus")
    assert not os.path.exists(src)
    assert leftovers(tmp_path) == []
    opus = OggOpus(out)
    assert abs(opus.info.length - 3.0) < 0.2
    assert opus["artist"] == ["The Artist"]
    assert opus["title"] == ["The Title"]
    assert opus["album"] == ["The Album"]
    assert opus["lyrics"] == ["la la la"]
    (pic,) = pictures_of(out)
    assert pic.data == PNG and pic.mime == "image/png" and pic.type == 3


@needs_ffmpeg
def test_delete_source_false_keeps_original(tmp_path):
    src = make_tagged_flac(tmp_path / "x.flac")
    out = convert_to_opus(src, delete_source=False)
    assert os.path.exists(src) and os.path.exists(out)


@needs_ffmpeg
def test_existing_target_raises_and_changes_nothing(tmp_path):
    src = make_tagged_flac(tmp_path / "x.flac")
    (tmp_path / "x.opus").write_bytes(b"precious")
    with pytest.raises(ConversionError):
        convert_to_opus(src)
    assert os.path.exists(src)
    assert (tmp_path / "x.opus").read_bytes() == b"precious"
    assert leftovers(tmp_path) == []
    assert sorted(os.listdir(tmp_path)) == ["x.flac", "x.opus"]


@needs_ffmpeg
def test_corrupt_flac_raises_and_leaves_no_temp_file(tmp_path):
    src = tmp_path / "bad.flac"
    src.write_bytes(os.urandom(4096))
    with pytest.raises(ConversionError):
        convert_to_opus(str(src))
    assert src.exists()
    assert sorted(os.listdir(tmp_path)) == ["bad.flac"]


@needs_ffmpeg
def test_mp3_is_lossy_and_left_alone(tmp_path):
    mp3 = make_audio(tmp_path / "x.mp3", "-c:a", "libmp3lame")
    assert not is_lossless(mp3)
    assert convert_if_lossless(mp3) == mp3
    assert sorted(os.listdir(tmp_path)) == ["x.mp3"]


@needs_ffmpeg
def test_wav_converts_and_keeps_id3_title_and_cover(tmp_path):
    wav = make_audio(tmp_path / "w.wav")
    audio = WAVE(wav)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text="Wav Title"))
    audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=PNG))
    audio.save()

    assert is_lossless(wav)
    out = convert_if_lossless(wav)
    assert out == str(tmp_path / "w.opus")
    assert not os.path.exists(wav)
    assert OggOpus(out)["title"] == ["Wav Title"]
    assert pictures_of(out)[0].data == PNG


@needs_ffmpeg
def test_alac_m4a_is_lossless_aac_is_not_and_cover_survives(tmp_path):
    alac = make_audio(tmp_path / "a.m4a", "-c:a", "alac")
    aac = make_audio(tmp_path / "b.m4a", "-c:a", "aac")
    assert is_lossless(alac)
    assert not is_lossless(aac)

    mp4 = MP4(alac)
    mp4["\xa9nam"] = ["Alac Title"]
    mp4["covr"] = [MP4Cover(PNG, imageformat=MP4Cover.FORMAT_PNG)]
    mp4.save()
    out = convert_to_opus(alac)
    assert OggOpus(out)["title"] == ["Alac Title"]
    (pic,) = pictures_of(out)
    assert pic.data == PNG and pic.mime == "image/png"
    assert os.path.exists(aac)


@needs_ffmpeg
def test_find_lossless_files_skips_hidden_dirs_and_lossy(tmp_path):
    (tmp_path / "album").mkdir()
    (tmp_path / ".hidden").mkdir()
    for rel in ("a.flac", "album/b.WAV", "album/c.mp3", "album/d.opus",
                ".hidden/e.flac"):
        (tmp_path / rel).write_bytes(b"x")
    alac = make_audio(tmp_path / "album" / "f.m4a", "-c:a", "alac")

    found = find_lossless_files(str(tmp_path))
    assert found == sorted([str(tmp_path / "a.flac"),
                            str(tmp_path / "album" / "b.WAV"), alac])


def test_extension_helpers(tmp_path):
    assert opus_target_path("/m/Artist/Song.FLAC") == "/m/Artist/Song.opus"
    for ext in (".flac", ".WAV", ".aiff", ".aif", ".ape", ".wv"):
        assert is_lossless("x" + ext)
    for ext in (".mp3", ".opus", ".ogg", ".aac", ".txt"):
        assert not is_lossless("x" + ext)
    broken = tmp_path / "broken.m4a"
    broken.write_bytes(b"not an mp4")
    assert not is_lossless(str(broken))


def test_without_ffmpeg_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)
    src = tmp_path / "x.flac"
    src.write_bytes(b"x")
    assert convert_if_lossless(str(src)) == str(src)
    with pytest.raises(ConversionError):
        convert_to_opus(str(src))
    assert src.exists()
