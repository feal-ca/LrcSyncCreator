"""library_index.py — when each song joined the library, and whether it is
still "new" (arrived since the app last looked and not opened yet).

File mtimes can't answer that on their own: embedding lyrics in tags
rewrites the file, and a rename by hand keeps an old date. So the app keeps
a small JSON map of path -> {added, new}. The very first scan seeds it from
mtimes with nothing marked new, so an existing library doesn't light up;
after that, any file the app hasn't seen before counts as added "now".

Qt-free; the library panel owns one instance and saves it after changes.
"""

from __future__ import annotations

import json
import os
import time


class LibraryIndex:
    def __init__(self, path: str) -> None:
        self._path = path
        self._entries: dict[str, dict] = {}
        self._exists = False

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        self._entries = {}
        self._exists = os.path.exists(self._path)
        if not self._exists:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for path, entry in data.items():
                if isinstance(entry, dict) and isinstance(entry.get("added"), (int, float)):
                    self._entries[path] = {"added": float(entry["added"]), "new": bool(entry.get("new"))}

    def save(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, ensure_ascii=False)
        os.replace(tmp, self._path)
        self._exists = True

    # -- updates ------------------------------------------------------------

    def sync(self, paths: list[str], now: float | None = None) -> None:
        """Make the index match the files on disk: forget what's gone and
        add what's new (seeded from mtimes on the very first sync)."""
        now = time.time() if now is None else now
        current = set(paths)
        for gone in set(self._entries) - current:
            del self._entries[gone]
        first_time = not self._exists and not self._entries
        for path in paths:
            if path in self._entries:
                continue
            if first_time:
                try:
                    added = os.path.getmtime(path)
                except OSError:
                    added = now
                self._entries[path] = {"added": added, "new": False}
            else:
                self._entries[path] = {"added": now, "new": True}
        self._exists = True

    def add(self, paths: list[str], now: float | None = None) -> None:
        """Paths that just arrived (a download, a drop); known ones are kept."""
        now = time.time() if now is None else now
        for path in paths:
            self._entries.setdefault(path, {"added": now, "new": True})

    def rename(self, old: str, new: str) -> None:
        entry = self._entries.pop(old, None)
        if entry is not None:
            self._entries[new] = entry

    def forget(self, path: str) -> None:
        self._entries.pop(path, None)

    def mark_seen(self, paths: list[str]) -> None:
        for path in paths:
            entry = self._entries.get(path)
            if entry is not None:
                entry["new"] = False

    # -- queries --------------------------------------------------------------

    def added_at(self, path: str) -> float:
        entry = self._entries.get(path)
        return entry["added"] if entry else 0.0

    def is_new(self, path: str) -> bool:
        entry = self._entries.get(path)
        return bool(entry and entry["new"])

    def known_paths(self) -> set[str]:
        return set(self._entries)
