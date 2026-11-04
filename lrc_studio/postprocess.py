"""postprocess.py — what happens to a finished download before it joins
the library: optional lossless→Opus conversion, a rename to
"Artist - Title.ext" from its tags, then the move itself.

Qt-free so it runs inside any worker thread and can be tested directly.
Every step is best-effort except the move: a failed conversion keeps the
original file, a file without usable tags keeps a cleaned-up version of
the peer's filename, but the track always ends up in the library.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

import mutagen

from utils import clean_filename

# "01 - ", "05. ", "3_", "12) " — track numbers peers put in front of names.
_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[-._)]+\s*")


@dataclass
class FinalizeOptions:
    library_dir: str
    convert_lossless: bool = True
    opus_bitrate_kbps: int = 192
    rename: bool = True


def unique_path(path: str) -> str:
    """Return *path*, or ``name (2).ext``, ``name (3).ext``, … if it exists."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


def strip_track_number(stem: str) -> str:
    stripped = _TRACK_NUMBER_RE.sub("", stem)
    return stripped or stem


def tag_based_stem(path: str) -> str | None:
    """"Artist - Title" from *path*'s tags, or None if either is missing."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if not audio or not audio.tags:
        return None
    artist = (audio.get("artist") or [""])[0].strip()
    title = (audio.get("title") or [""])[0].strip()
    if not artist or not title:
        return None
    stem = clean_filename(f"{artist} - {title}").strip(" .")
    return stem or None


def library_stem(path: str) -> str:
    """The filename (without extension) a download should get in the library."""
    from_tags = tag_based_stem(path)
    if from_tags:
        return from_tags
    stem = os.path.splitext(os.path.basename(path))[0]
    return clean_filename(strip_track_number(stem)).strip(" .") or stem


@dataclass
class RenamePlan:
    """One file's standardized name: *new* is None when it stays as it is,
    and *reason* says why."""

    old: str
    new: str | None
    reason: str


def plan_standard_names(paths: list[str]) -> list[RenamePlan]:
    """What "Rename to “Artist - Title”" would do to each of *paths*, in
    order. Nothing is touched; a name that would collide with a file on
    disk, or with an earlier plan in the same batch, is left alone."""
    plans: list[RenamePlan] = []
    claimed: set[str] = set()
    for old in paths:
        stem = tag_based_stem(old)
        if not stem:
            plans.append(RenamePlan(old, None, "No artist/title tags"))
            continue
        new = os.path.join(os.path.dirname(old), stem + os.path.splitext(old)[1])
        if new == old:
            plans.append(RenamePlan(old, None, "Already named that way"))
        elif new in claimed or os.path.exists(new):
            plans.append(RenamePlan(old, None, f"“{os.path.basename(new)}” already exists"))
        else:
            claimed.add(new)
            plans.append(RenamePlan(old, new, ""))
    return plans


def apply_renames(plans: list[RenamePlan]) -> list[tuple[str, str]]:
    """Carry out *plans* (audio file plus its .lrc). Never overwrites;
    returns the (old, new) pairs that were actually renamed."""
    done: list[tuple[str, str]] = []
    for plan in plans:
        if not plan.new or os.path.exists(plan.new) or not os.path.exists(plan.old):
            continue
        try:
            os.rename(plan.old, plan.new)
        except OSError:
            continue
        old_lrc = os.path.splitext(plan.old)[0] + ".lrc"
        new_lrc = os.path.splitext(plan.new)[0] + ".lrc"
        if os.path.exists(old_lrc) and not os.path.exists(new_lrc):
            try:
                os.rename(old_lrc, new_lrc)
            except OSError:
                pass
        done.append((plan.old, plan.new))
    return done


def finalize_download(local_path: str, options: FinalizeOptions) -> str:
    """Convert (if enabled and lossless), rename, and move *local_path*
    into ``options.library_dir``. Returns the final path.

    Conversion happens before the move, in the downloads folder, so the
    library (which may be synced to other devices) never sees a FLAC
    appear and vanish a few seconds later.
    """
    path = local_path
    if options.convert_lossless:
        try:
            import convert
            path = convert.convert_if_lossless(path, options.opus_bitrate_kbps)
        except Exception as exc:  # noqa: BLE001 — keep the original rather than lose the track
            print(f"[warn] conversion failed for {path}: {exc}")

    ext = os.path.splitext(path)[1].lower()
    stem = library_stem(path) if options.rename else os.path.splitext(os.path.basename(path))[0]
    os.makedirs(options.library_dir, exist_ok=True)
    dest = unique_path(os.path.join(options.library_dir, stem + ext))
    shutil.move(path, dest)
    return dest


def default_downloads_dir() -> str:
    """Where Soulseek downloads land before they're finalized: the app's
    data folder, deliberately outside the (synced) library."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "LrcStudio", "downloads")


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def options_from_settings(settings) -> FinalizeOptions:
    """Build FinalizeOptions from a QSettings-like object (anything with
    ``.value(key, default)``). INI-backed QSettings hands bools back as
    the strings "true"/"false", hence ``_as_bool``."""
    library_dir = str(settings.value("library_dir", "") or "").strip() or os.getcwd()
    try:
        bitrate = int(settings.value("opus_bitrate", 192) or 192)
    except (TypeError, ValueError):
        bitrate = 192
    return FinalizeOptions(
        library_dir=library_dir,
        convert_lossless=_as_bool(settings.value("convert_lossless", True), True),
        opus_bitrate_kbps=bitrate,
        rename=_as_bool(settings.value("rename_downloads", True), True),
    )


def downloads_dir_from_settings(settings) -> str:
    return str(settings.value("downloads_dir", "") or "").strip() or default_downloads_dir()
