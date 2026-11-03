"""Convert lossless audio to Opus in place.

After a download (and from a one-off library tool) lossless files are
re-encoded to Opus 192 kbps next to the original, keeping the same folder and
filename stem so the sibling ``<stem>.lrc`` lyrics file stays paired. Tags and
cover art are carried over, and the original is deleted only once the output
has been verified. Lossy formats are never re-encoded. Qt-free.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

LOSSLESS_EXTENSIONS = (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv")

_LENGTH_TOLERANCE_S = 1.5
_ENCODE_TIMEOUT_S = 900

# ID3 frame / MP4 atom -> Vorbis comment key, for formats whose tags ffmpeg
# may not have mapped on its own.
_ID3_KEYS = {
    "TPE1": "artist", "TIT2": "title", "TALB": "album", "TPE2": "albumartist",
    "TRCK": "tracknumber", "TDRC": "date", "TCON": "genre",
}
_MP4_KEYS = {
    "\xa9ART": "artist", "\xa9nam": "title", "\xa9alb": "album",
    "aART": "albumartist", "\xa9day": "date", "\xa9gen": "genre",
    "\xa9lyr": "lyrics",
}
_FLAC_KEYS = (
    "artist", "title", "album", "albumartist", "tracknumber", "date",
    "genre", "lyrics",
)


class ConversionError(Exception):
    """Raised when a file cannot be converted; the source is left untouched."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def is_lossless(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in LOSSLESS_EXTENSIONS:
        return True
    if ext == ".m4a":
        try:
            return MP4(path).info.codec == "alac"
        except Exception:
            return False
    return False


def opus_target_path(src: str) -> str:
    return os.path.splitext(src)[0] + ".opus"


def _source_metadata(src: str) -> tuple[dict[str, list[str]], list[Picture]]:
    """Read text tags and cover art from ``src`` in a format-neutral shape."""
    audio = mutagen.File(src)
    tags: dict[str, list[str]] = {}
    pictures: list[Picture] = []
    if isinstance(audio, FLAC):
        for key in _FLAC_KEYS:
            values = [str(v) for v in audio.get(key, [])]
            if values:
                tags[key] = values
        pictures = list(audio.pictures)
    elif isinstance(audio, MP4):
        for atom, key in _MP4_KEYS.items():
            values = [str(v) for v in audio.get(atom, [])]
            if values:
                tags[key] = values
        trkn = audio.get("trkn")
        if trkn and trkn[0][0]:
            tags["tracknumber"] = [str(trkn[0][0])]
        for cover in audio.get("covr", []):
            pic = Picture()
            pic.type = 3
            pic.mime = ("image/png" if cover.imageformat == MP4Cover.FORMAT_PNG
                        else "image/jpeg")
            pic.data = bytes(cover)
            pictures.append(pic)
    elif audio is not None and audio.tags is not None and hasattr(audio.tags, "getall"):
        for frame_id, key in _ID3_KEYS.items():
            frames = audio.tags.getall(frame_id)
            if frames:
                tags[key] = [str(t) for t in frames[0].text]
        uslt = audio.tags.getall("USLT")
        if uslt:
            tags["lyrics"] = [str(uslt[0].text)]
        for frame in audio.tags.getall("APIC"):
            pic = Picture()
            pic.type = int(frame.type)
            pic.mime = frame.mime
            pic.desc = frame.desc
            pic.data = frame.data
            pictures.append(pic)
    return tags, pictures


def _carry_over_metadata(src: str, tmp: str) -> None:
    """Best-effort: add cover art and any text tag ffmpeg did not carry over."""
    try:
        tags, pictures = _source_metadata(src)
        opus = OggOpus(tmp)
        if opus.tags is None:
            opus.add_tags()
        for key, values in tags.items():
            if key not in opus.tags:
                opus.tags[key] = values
        if pictures:
            opus.tags["metadata_block_picture"] = [
                base64.b64encode(p.write()).decode("ascii") for p in pictures
            ]
        opus.save()
    except Exception:
        # Tags and art must never cost the user the conversion itself.
        pass


def _length(path: str, opus: bool = False) -> float | None:
    try:
        audio = OggOpus(path) if opus else mutagen.File(path)
        return float(audio.info.length)
    except Exception:
        return None


def convert_to_opus(src: str, bitrate_kbps: int = 192,
                    delete_source: bool = True) -> str:
    """Encode ``src`` to ``<stem>.opus`` beside it and return that path.

    Refuses to pick another name when the target exists, because the ``.lrc``
    pairing depends on the stem. The original is removed only after the new
    file has been verified and moved into place.
    """
    if not ffmpeg_available():
        raise ConversionError("ffmpeg is not installed")
    if not os.path.isfile(src):
        raise ConversionError(f"Source file not found: {src}")
    target = opus_target_path(src)
    if os.path.exists(target):
        raise ConversionError(f"Target already exists: {target}")

    folder, name = os.path.split(os.path.abspath(src))
    tmp = os.path.join(folder, f".{os.path.splitext(name)[0]}.converting.opus")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", os.path.join(folder, name),
        "-map", "0:a:0", "-map_metadata", "0",
        "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
        "-vbr", "on", "-compression_level", "10",
        tmp,
    ]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_ENCODE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConversionError(f"ffmpeg failed to run: {exc}") from exc
        if proc.returncode != 0:
            raise ConversionError(
                f"ffmpeg failed: {(proc.stderr or '').strip()[-300:]}")

        _carry_over_metadata(src, tmp)

        out_len = _length(tmp, opus=True)
        if out_len is None:
            raise ConversionError("Converted file could not be read back")
        src_len = _length(src)
        if src_len is not None and abs(out_len - src_len) > _LENGTH_TOLERANCE_S:
            raise ConversionError(
                f"Converted length {out_len:.1f}s differs from source "
                f"{src_len:.1f}s")

        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    if delete_source:
        try:
            os.remove(src)
        except OSError as exc:
            raise ConversionError(
                f"Converted to {target} but could not delete the original: {exc}"
            ) from exc
    return target


def convert_if_lossless(path: str, bitrate_kbps: int = 192) -> str:
    """Convert ``path`` when it is lossless and ffmpeg exists; else return it."""
    if is_lossless(path) and ffmpeg_available():
        return convert_to_opus(path, bitrate_kbps)
    return path


def find_lossless_files(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if is_lossless(full):
                found.append(full)
    return sorted(found)
