# LRC Studio UI/UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LRC Studio's two-tab (Library/Download) layout with one continuous workspace: a persistent library panel on the left, and a main area on the right that swaps between the sync editor, an acquire/review flow, and an empty state — collapsing the current three-way "add music" button sprawl and the redundant lyrics-fetch buttons into single, obvious actions.

**Architecture:** Extract the file-list sidebar into a new `LibraryPanel(QWidget)` with a small, explicit public interface (signals for cross-cutting actions, plain methods for pure list operations). Replace `app.py`'s `QTabWidget` with a `QSplitter(LibraryPanel, QStackedWidget)`, where the stack holds three pages (Editor / Acquire-Review / Empty) that `LrcSyncApp` swaps between in response to `LibraryPanel` signals and `DownloadView` completion signals. `DownloadView`'s internal logic (search/download threading, match scoring, retry) is untouched — it's re-homed into the stack and restyled, not rewritten.

**Tech Stack:** Python 3, PyQt6, pytest (with `QT_QPA_PLATFORM=offscreen` for headless GUI tests, an isolated `QSettings` per test via `tmp_path`).

**Spec:** `docs/superpowers/specs/2026-09-28-ui-redesign-design.md`

## Global Constraints

- No visual/token system pass (color/spacing/typography formalization) — out of scope, a separate follow-up.
- No change to the sync-editor's core mechanics: playback, timeline drag-to-sync, keyboard shortcuts for syncing, undo.
- No change to backend logic: Soulseek search/scoring (`soulseek.py`), Spotify scraping (`spotify_playlist.py`), lyrics fetching (`workers.py`), Validate Sync (`lyrics_qa.py`), slskd lifecycle (`app.py`'s `_get_slskd_client`/`_restart_slskd`). This plan is a UI restructuring around existing, already-tested backends.
- Every test that constructs `LrcSyncApp` MUST redirect `QSettings` to a per-test temp file first (see `tests/test_app_commands.py`'s `window` fixture for the established pattern) — never touch the user's real persisted config.
- The existing 54 tests (as of the last commit) must stay green throughout. A test tied to removed behavior (e.g. tab-switching) gets replaced with an equivalent test of the new behavior, not just deleted.
- `QT_QPA_PLATFORM=offscreen` must be set (via `os.environ.setdefault(...)` before importing `PyQt6`) in any new test file that constructs real widgets, matching `tests/test_app_commands.py`.

## Review Focus

- **A malformed/partial Spotify link typed into Find Music** (e.g. `"spotify.com/track/"` with no ID, or plain text that happens to contain the word "spotify") must fall through to a literal Soulseek search, not crash or silently do nothing — Task 3's tests pin this.
- **Switching to a different song while a batch import is actively downloading in the Acquire/Review workspace** must not cancel or lose that download — `DownloadView` must be a persistent widget added once to the stack (never rebuilt per-visit), so hiding it via `QStackedWidget` doesn't touch its running threads. Task 6's tests assert instance identity survives a workspace swap.
- **Clicking "Get Lyrics" with no song loaded** must no-op safely, the same guard the current `_get_lyrics_online` already has (`if not self.song_filename: return`) — Task 8's tests confirm the guard survives the rename.
- **The empty state's own "Add Music" affordance** must route through the exact same signal-handling path as `LibraryPanel`'s own Add Music button, not a second, diverging implementation — Task 6's tests assert this by connecting a spy to the shared signal, not by testing two separate code paths.
- **"Fetch Lyrics for All Missing" on a library with zero missing-lyrics files** must show the existing "No missing lyrics to download" message and start no thread, matching today's `_batch_download_lyrics` guard — Task 2's tests cover the zero-file case explicitly, since it's an easy off-by-one to lose during the extraction.

---

## File Structure

- **Create `lrc_studio/library_panel.py`** — the new `LibraryPanel(QWidget)` class: file list widget, status-color logic, Add Music menu, overflow menu. Owns everything that's purely about the file list; signals out only what genuinely needs `LrcSyncApp` (playback state, slskd client, dialogs, progress bar).
- **Modify `lrc_studio/spotify_playlist.py`** — add `is_spotify_link(text: str) -> bool`, a thin wrapper around the existing `extract_spotify_ref()`.
- **Modify `lrc_studio/download_view.py`** — add `DownloadView.start_fetch(link: str) -> None` (programmatic trigger, reusing the existing fetch path) and a new `file_downloaded = pyqtSignal(str)` emitted per completed row (in addition to the existing end-of-batch `files_ready`), for live library updates.
- **Modify `lrc_studio/app.py`** — the bulk of the structural change: remove `QTabWidget`, build the `LibraryPanel` + `QStackedWidget` split, add the empty-state widget, implement `_find_music()`, split `_clear_lyrics()`/`_replace_all_verses()`, update `_build_commands()`, wire all the new signals.
- **Modify `README.md`** — the "User Interface" section currently describes the two-tab layout; update it to match (Task 10).
- **Test files:** `tests/test_library_panel.py` (new), `tests/test_spotify_playlist.py` (extend with `is_spotify_link` tests — check if this file exists first; create if not), `tests/test_app_commands.py` (extend), `tests/test_workspace.py` (new, for the stack/empty-state/routing behavior).

---

### Task 1: Extract `LibraryPanel` (behavior-preserving)

Move the file-list sidebar out of `app.py` into its own module, preserving current behavior for everything that's genuinely self-contained (the list itself, Add Files, Standardize Names, Clear Completed — still the old 6-button layout, the menu redesign is Task 2). The three buttons whose handlers need something outside `LibraryPanel` (slskd client, tab-switching, the progress bar — Find on Soulseek, Import from Spotify, Download Lyrics) become inert `lambda: None` stubs in this task, not working buttons — genuinely preserving their behavior here would mean wiring the exact cross-cutting signals that are Task 2/6's job, defeating the point of separating "did the extraction break anything" from "is the new design right" as two independently reviewable gates. This is intentional, not an oversight: those three buttons are replaced (not restored) by Task 2's Add Music/overflow menus a task later.

**Files:**
- Create: `lrc_studio/library_panel.py`
- Modify: `lrc_studio/app.py` (remove `_build_file_panel`, `_make_group_label`, `_refresh_file_list_placeholder`, `_real_file_items`, `_update_file_status_color`; replace `self.file_list` construction and all direct references with a `self.library_panel` instance)
- Test: `tests/test_library_panel.py`

**Interfaces:**
- Produces (used by every later task):
  ```python
  class LibraryPanel(QWidget):
      song_selected = pyqtSignal(str)              # emitted on double-click of a real item
      file_renamed = pyqtSignal(str, str)           # (old_path, new_path), from Standardize Names

      def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None: ...
      def add_files(self, paths: list[str]) -> None: ...
      def real_file_paths(self) -> list[str]: ...
      def paths_with_status(self, color: QColor) -> list[str]: ...
      def refresh_status(self, path: str) -> None: ...
      def select_path(self, path: str) -> None: ...
      def select_row(self, index: int) -> None: ...
      def current_row(self) -> int: ...
      def row_count(self) -> int: ...
  ```

- [ ] **Step 1: Write the failing test for basic construction and file adding**

```python
# tests/test_library_panel.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from library_panel import LibraryPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def panel(qapp, settings):
    return LibraryPanel(settings)


def test_starts_with_no_real_files(panel):
    assert panel.real_file_paths() == []


def test_add_files_appends_real_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f2 = tmp_path / "b.mp3"
    f1.write_bytes(b"")
    f2.write_bytes(b"")

    panel.add_files([str(f1), str(f2)])

    assert set(panel.real_file_paths()) == {str(f1), str(f2)}


def test_add_files_dedupes_already_present_paths(panel, tmp_path):
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"")

    panel.add_files([str(f1)])
    panel.add_files([str(f1)])

    assert panel.real_file_paths() == [str(f1)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_library_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'library_panel'`

- [ ] **Step 3: Create `library_panel.py` by moving the existing logic**

Read `lrc_studio/app.py`'s current `_build_file_panel`, `_make_group_label`, `_refresh_file_list_placeholder`, `_real_file_items`, `_update_file_status_color`, `_process_added_files`, `_remove_completed_files`, `_batch_rename_files`, and `_batch_download_lyrics`'s file-scanning loop (the `for i in range(self.file_list.count())` block only — the thread-starting part stays in `app.py`, see Task 2). Move their logic into this class, preserving the existing `_neutral_button_style()`-equivalent inline stylesheet strings verbatim (still today's 6 buttons — do not redesign the menu here):

```python
"""library_panel.py — LibraryPanel: the persistent file-list sidebar.

Owns everything that's purely about the file list (adding, removing,
renaming, status-coloring). Signals out only the two things it can't do
itself: loading a song into the editor (needs the VLC player) and a file
rename that might be the currently-loaded song (needs app-level state).
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

import mutagen

from constants import AUDIO_EXTENSIONS, COLOR_FG, COLOR_MISSING, COLOR_MUTED, COLOR_SYNCED, COLOR_TIMELINE_BG
from utils import analyze_lrc_status, clean_filename


class LibraryPanel(QWidget):
    song_selected = pyqtSignal(str)
    file_renamed = pyqtSignal(str, str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Audio Files")
        header.setStyleSheet(f"font-weight: bold; color: {COLOR_FG.name()}; margin-bottom: 5px;")
        layout.addWidget(header)

        self.file_list = QListWidget()
        self.file_list.setStyleSheet(
            f"background-color: {COLOR_TIMELINE_BG.name()};"
            " border: 1px solid #45475a; font-size: 13px; border-radius: 4px;"
        )
        self.file_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.file_list)

        btn_style = (
            f"QPushButton {{ background-color: #313244; color: {COLOR_FG.name()};"
            " border: 1px solid #45475a; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #45475a; }"
        )

        layout.addWidget(self._make_group_label("Add music"))
        for label, handler in [
            ("Add Files", self._add_files_dialog),
            ("Find on Soulseek", lambda: None),      # Task 2 rewires this
            ("Import from Spotify", lambda: None),   # Task 2 rewires this
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(btn_style)
            layout.addWidget(btn)

        layout.addWidget(self._make_group_label("Manage"))
        for label, handler in [
            ("Standardize Names", self._batch_rename_files),
            ("Download Lyrics", lambda: None),        # Task 2 rewires this
            ("Clear Completed", self._remove_completed_files),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            btn.setStyleSheet(btn_style)
            layout.addWidget(btn)

        self._refresh_placeholder()

    @staticmethod
    def _make_group_label(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"color: {COLOR_MUTED.name()}; font-size: 10px; font-weight: bold;"
            " letter-spacing: 1px; margin-top: 6px;"
        )
        return label

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.song_selected.emit(path)

    def _refresh_placeholder(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            if not self.file_list.item(i).data(Qt.ItemDataRole.UserRole):
                self.file_list.takeItem(i)
        if self.file_list.count() == 0:
            placeholder = QListWidgetItem('Drag audio files here, or click "Add Files" below')
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(COLOR_MUTED)
            self.file_list.addItem(placeholder)

    def real_file_paths(self) -> list[str]:
        return [
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
        ]

    def paths_with_status(self, color: QColor) -> list[str]:
        return [
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            and self.file_list.item(i).foreground().color() == color
        ]

    def refresh_status(self, path: str) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self._update_status_color(item, path)
                return

    @staticmethod
    def _update_status_color(item: QListWidgetItem, file_path: str) -> None:
        from constants import COLOR_UNSYNCED
        lrc_path = os.path.splitext(file_path)[0] + ".lrc"
        color = analyze_lrc_status(lrc_path)
        item.setForeground(color)
        tip = {
            COLOR_SYNCED.name(): "Synced",
            COLOR_UNSYNCED.name(): "Unsynced (timestamps may be [00:00])",
            COLOR_MISSING.name(): "No .lrc file",
        }.get(color.name(), "Unknown")
        item.setToolTip(tip)

    def select_path(self, path: str) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.file_list.setCurrentItem(item)
                return

    def select_row(self, index: int) -> None:
        if 0 <= index < self.file_list.count():
            self.file_list.setCurrentItem(self.file_list.item(index))

    def current_row(self) -> int:
        return self.file_list.currentRow()

    def row_count(self) -> int:
        return self.file_list.count()

    def add_files(self, file_paths: list[str]) -> None:
        existing = set(self.real_file_paths())
        for path in file_paths:
            if os.path.isfile(path) and path.lower().endswith(AUDIO_EXTENSIONS) and path not in existing:
                item = QListWidgetItem(os.path.basename(path))
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._update_status_color(item, path)
                self.file_list.addItem(item)
                existing.add(path)
        self._refresh_placeholder()

    def _add_files_dialog(self) -> None:
        last_dir = self._settings.value("last_dir", "")
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add Audio", last_dir, "Audio (*.mp3 *.flac *.wav *.ogg *.m4a *.opus)",
        )
        if files:
            self._settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_files(files)

    def _remove_completed_files(self) -> None:
        for i in range(self.file_list.count() - 1, -1, -1):
            item = self.file_list.item(i)
            if item.foreground().color() == COLOR_SYNCED:
                self.file_list.takeItem(i)
        self._refresh_placeholder()

    def _batch_rename_files(self) -> None:
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            old_path = item.data(Qt.ItemDataRole.UserRole)
            if not old_path:
                continue
            ext = os.path.splitext(old_path)[1]
            try:
                audio = mutagen.File(old_path, easy=True)
                if not audio or "artist" not in audio or "title" not in audio:
                    continue
                artist = clean_filename(audio["artist"][0])
                title = clean_filename(audio["title"][0])
                new_name = f"{artist} - {title}{ext}"
                new_path = os.path.join(os.path.dirname(old_path), new_name)
                if old_path == new_path:
                    continue
                os.rename(old_path, new_path)
                old_lrc = os.path.splitext(old_path)[0] + ".lrc"
                new_lrc = os.path.splitext(new_path)[0] + ".lrc"
                if os.path.exists(old_lrc):
                    os.rename(old_lrc, new_lrc)
                item.setText(new_name)
                item.setData(Qt.ItemDataRole.UserRole, new_path)
                self.file_renamed.emit(old_path, new_path)
            except Exception:
                continue
```

Add `conftest.py`-style import compatibility: since `library_panel.py` lives in `lrc_studio/`, it uses the same flat-import style (`from constants import ...`) as every other module there.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_library_panel.py -v`
Expected: PASS

- [ ] **Step 5: Rewire `app.py` to use `LibraryPanel` instead of `_build_file_panel`**

In `_build_library_tab` (or wherever the splitter is built), replace `self.splitter.addWidget(self._build_file_panel())` with:

```python
self.library_panel = LibraryPanel(self.settings)
self.library_panel.song_selected.connect(self._load_song)
self.library_panel.file_renamed.connect(self._on_file_renamed)
self.splitter.addWidget(self.library_panel)
```

Add `from library_panel import LibraryPanel` to `app.py`'s imports. Add the small handler:

```python
def _on_file_renamed(self, old_path: str, new_path: str) -> None:
    if self.current_file_path == old_path:
        self.current_file_path = new_path
        self.song_filename = os.path.basename(new_path)
```

Delete `_build_file_panel`, `_make_group_label`, `_refresh_file_list_placeholder`, `_real_file_items`, `_update_file_status_color`, `_add_files_dialog`, `_remove_completed_files`, `_batch_rename_files` from `app.py`. Update every remaining direct reference to `self.file_list` elsewhere in `app.py` (the auto-advance-to-next-track logic, `_open_in_library`, `_process_added_files`, `_batch_download_lyrics`, `_load_file_from_index`, `_save_lrc_file`'s status-color refresh) to call the equivalent `self.library_panel.*` method instead — e.g. `self.file_list.currentRow()` becomes `self.library_panel.current_row()`, `self._real_file_items()` becomes `self.library_panel.real_file_paths()`, `self._update_file_status_color(cur_item, path)` becomes `self.library_panel.refresh_status(path)`.

- [ ] **Step 6: Run the full suite to confirm nothing broke**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/python -m py_compile lrc_studio/*.py && M/bin/pytest tests/ -q`
Expected: all existing tests plus the new ones pass; compile succeeds.

- [ ] **Step 7: Headless screenshot check**

Run the same offscreen-palette screenshot script used in the last UX pass (construct `LrcSyncApp` with `QSettings` redirected to a tmp file, grab the Library tab) and view the PNG. Confirm the sidebar renders identically to before the extraction — same buttons, same groups, same placeholder text.

- [ ] **Step 8: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/library_panel.py lrc_studio/app.py tests/test_library_panel.py
git commit -m "Extract LibraryPanel from app.py (behavior-preserving)"
```

---

### Task 2: Redesign `LibraryPanel`'s action surface

Replace the six always-visible buttons with one "+ Add Music" button (opening a 2-item menu: Add Files, Find Music) and one overflow (⋮) button (Standardize Names, Clear Completed, Fetch Lyrics for All Missing).

**Files:**
- Modify: `lrc_studio/library_panel.py`
- Test: `tests/test_library_panel.py`

**Interfaces:**
- Consumes: `LibraryPanel` from Task 1.
- Produces (new signals used by Task 5/6/8):
  ```python
  find_music_requested = pyqtSignal()
  fetch_missing_lyrics_requested = pyqtSignal(list)   # list[str] of missing-lyrics paths
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_library_panel.py (append)

def test_add_music_menu_has_two_actions(panel):
    action_texts = [a.text() for a in panel.add_music_button.menu().actions()]
    assert action_texts == ["Add Files", "Find Music"]


def test_overflow_menu_has_three_actions(panel):
    action_texts = [a.text() for a in panel.overflow_button.menu().actions()]
    assert action_texts == ["Standardize Names", "Clear Completed", "Fetch Lyrics for All Missing"]


def test_find_music_action_emits_signal(panel, qtbot=None):
    received = []
    panel.find_music_requested.connect(lambda: received.append(True))
    panel.add_music_button.menu().actions()[1].trigger()
    assert received == [True]


def test_fetch_missing_lyrics_emits_paths_with_missing_status(panel, tmp_path):
    from constants import COLOR_MISSING
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"")
    panel.add_files([str(f1)])   # freshly added file with no .lrc -> COLOR_MISSING

    received = []
    panel.fetch_missing_lyrics_requested.connect(lambda paths: received.append(paths))
    panel.overflow_button.menu().actions()[2].trigger()

    assert received == [[str(f1)]]


def test_fetch_missing_lyrics_with_nothing_missing_emits_empty_list(panel):
    received = []
    panel.fetch_missing_lyrics_requested.connect(lambda paths: received.append(paths))
    panel.overflow_button.menu().actions()[2].trigger()

    assert received == [[]]
```

(Note: `qtbot` isn't used — `pytest-qt` isn't a project dependency; signal connections are tested directly via a list-append lambda, matching the pattern already used in `test_app_commands.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_library_panel.py -v -k "add_music_menu or overflow_menu or find_music_action or fetch_missing"`
Expected: FAIL — `panel.add_music_button` doesn't exist yet.

- [ ] **Step 3: Replace the button-group construction**

Three separate edits to `library_panel.py`:

**3a.** Add `QHBoxLayout, QMenu, QToolButton` to the existing `from PyQt6.QtWidgets import (...)` block at the top of the file (alongside `QFileDialog, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget` from Task 1).

**3b.** Add two new signals to the class body, alongside `song_selected`/`file_renamed` from Task 1:

```python
class LibraryPanel(QWidget):
    song_selected = pyqtSignal(str)
    file_renamed = pyqtSignal(str, str)
    find_music_requested = pyqtSignal()
    fetch_missing_lyrics_requested = pyqtSignal(list)
```

**3c.** In `_build_ui`, replace the body from `layout.addWidget(self._make_group_label("Add music"))` through the end of the "Manage" loop with:

```python
action_row = QHBoxLayout()

self.add_music_button = QToolButton()
self.add_music_button.setText("+ Add Music")
self.add_music_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
add_menu = QMenu(self.add_music_button)
add_menu.addAction("Add Files", self._add_files_dialog)
add_menu.addAction("Find Music", self.find_music_requested.emit)
self.add_music_button.setMenu(add_menu)
self.add_music_button.setStyleSheet(btn_style)
action_row.addWidget(self.add_music_button, stretch=1)

self.overflow_button = QToolButton()
self.overflow_button.setText("⋮")
self.overflow_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
overflow_menu = QMenu(self.overflow_button)
overflow_menu.addAction("Standardize Names", self._batch_rename_files)
overflow_menu.addAction("Clear Completed", self._remove_completed_files)
overflow_menu.addAction(
    "Fetch Lyrics for All Missing",
    lambda: self.fetch_missing_lyrics_requested.emit(self.paths_with_status(COLOR_MISSING)),
)
self.overflow_button.setMenu(overflow_menu)
self.overflow_button.setStyleSheet(btn_style)
action_row.addWidget(self.overflow_button)

layout.addLayout(action_row)
```

Remove the two `_make_group_label` calls and their loops (the "Add music"/"Manage" group headers are no longer needed — the menu structure now carries that grouping). `_make_group_label` itself can stay defined but unused, or be deleted; delete it since nothing calls it anymore.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_library_panel.py -v`
Expected: all PASS.

- [ ] **Step 5: Screenshot check**

Re-run the headless screenshot script. Confirm the sidebar now shows one "+ Add Music" split button and one "⋮" overflow button instead of six stacked buttons, and that the file list has noticeably more vertical space.

- [ ] **Step 6: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/library_panel.py tests/test_library_panel.py
git commit -m "Consolidate LibraryPanel's six buttons into Add Music + overflow menus"
```

---

### Task 3: Spotify-link detection helper

**Files:**
- Modify: `lrc_studio/spotify_playlist.py`
- Test: `tests/test_spotify_playlist.py` (create if it doesn't exist — check with `ls tests/` first)

**Interfaces:**
- Produces: `is_spotify_link(text: str) -> bool`, used by Task 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spotify_playlist.py
from spotify_playlist import is_spotify_link


def test_recognizes_a_track_link():
    assert is_spotify_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC") is True


def test_recognizes_a_playlist_uri():
    assert is_spotify_link("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") is True


def test_rejects_plain_search_text():
    assert is_spotify_link("Bohemian Rhapsody Queen") is False


def test_rejects_a_malformed_partial_link():
    assert is_spotify_link("spotify.com/track/") is False


def test_rejects_text_that_merely_mentions_spotify():
    assert is_spotify_link("I found this on spotify somewhere") is False


def test_rejects_empty_string():
    assert is_spotify_link("") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_spotify_playlist.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_spotify_link'`

- [ ] **Step 3: Implement it**

In `spotify_playlist.py`, near `extract_spotify_ref`:

```python
def is_spotify_link(text: str) -> bool:
    """True if *text* is a Spotify track/album/playlist link, URI, or bare
    ID — the same shapes extract_spotify_ref() accepts. Used to route the
    unified Find Music input: a recognized link means "look up this
    track list," anything else means "search Soulseek directly."
    """
    try:
        extract_spotify_ref(text)
        return True
    except SpotifyLinkError:
        return False
```

Check `extract_spotify_ref`'s existing behavior on a bare non-ID string (e.g. `"Bohemian Rhapsody Queen"`) — per its docstring, "bare IDs are assumed to be a playlist, for backwards compatibility," so confirm it actually raises `SpotifyLinkError` for arbitrary text rather than accepting it as a bare ID. Read the function body before assuming; if it currently accepts *any* non-empty string as a "bare ID," `is_spotify_link` will falsely return `True` for plain search text and this step needs a stricter check (e.g. requiring the bare-ID form to be exactly 22 alphanumeric characters, matching `_LINK_RE`'s own ID pattern) instead of delegating blindly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_spotify_playlist.py -v`
Expected: all PASS. If `test_rejects_plain_search_text` or `test_rejects_a_malformed_partial_link` fail because of the bare-ID fallback, fix `is_spotify_link` per Step 3's note, re-run.

- [ ] **Step 5: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/spotify_playlist.py tests/test_spotify_playlist.py
git commit -m "Add is_spotify_link() for routing the unified Find Music input"
```

---

### Task 4: `DownloadView.start_fetch()` and per-file live-update signal

**Files:**
- Modify: `lrc_studio/download_view.py`
- Test: `tests/test_download_view.py` (create — check `ls tests/` first)

**Interfaces:**
- Consumes: existing `DownloadView` from `download_view.py`.
- Produces: `DownloadView.start_fetch(link: str) -> None`; new signal `file_downloaded = pyqtSignal(str)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_download_view.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from download_view import DownloadView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeFetchThread:
    """Stands in for workers.PlaylistFetchThread — start_fetch() triggers
    a real fetch via _on_fetch_clicked(), and a test must never let that
    hit the network."""
    fetched = type("Signal", (), {"connect": lambda self, fn: None})()
    fetch_failed = type("Signal", (), {"connect": lambda self, fn: None})()

    def __init__(self, *a, **k) -> None: ...
    def start(self) -> None: ...


@pytest.fixture
def view(qapp, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)
    import download_view as download_view_module
    monkeypatch.setattr(download_view_module, "PlaylistFetchThread", _FakeFetchThread)
    return DownloadView(settings, get_client=lambda: None, collect_library_paths=lambda: [])


def test_start_fetch_populates_the_link_input(view):
    view.start_fetch("https://open.spotify.com/playlist/abc")
    assert view.link_input.text() == "https://open.spotify.com/playlist/abc"


def test_start_fetch_disables_the_button_like_a_manual_click_does(view):
    view.start_fetch("https://open.spotify.com/playlist/abc")
    assert view.fetch_button.isEnabled() is False


def test_file_downloaded_signal_exists():
    assert hasattr(DownloadView, "file_downloaded")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_download_view.py -v`
Expected: FAIL — `AttributeError: 'DownloadView' object has no attribute 'start_fetch'`

- [ ] **Step 3: Implement `start_fetch`**

In `download_view.py`, near `_on_fetch_clicked`:

```python
def start_fetch(self, link: str) -> None:
    """Programmatic equivalent of pasting *link* and clicking Fetch —
    used by the unified Find Music entry point in app.py."""
    self.link_input.setText(link)
    self._on_fetch_clicked()
```

- [ ] **Step 4: Add the `file_downloaded` signal and emit it per completed row**

Add `file_downloaded = pyqtSignal(str)` alongside the existing `files_ready`/`open_in_library` class-level signal declarations. Find where individual row completions are recorded (the `_newly_downloaded.append(local_path)` call, from the earlier `track_done` handler) and emit right there:

```python
self._newly_downloaded.append(local_path)
self.file_downloaded.emit(local_path)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_download_view.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/ -q`
Expected: all still passing.

- [ ] **Step 7: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/download_view.py tests/test_download_view.py
git commit -m "Add DownloadView.start_fetch() and per-file file_downloaded signal"
```

---

### Task 5: Unified `_find_music()` entry point in `app.py`

Replaces `_find_on_soulseek` and the "Import from Spotify" tab-switch with one routed entry point.

**Files:**
- Modify: `lrc_studio/app.py`
- Test: `tests/test_app_commands.py` (extend)

**Interfaces:**
- Consumes: `is_spotify_link` (Task 3), `DownloadView.start_fetch` (Task 4), `LibraryPanel.find_music_requested` (Task 2).
- Produces: `_find_music(self) -> None`, connected to `self.library_panel.find_music_requested` and replacing the old `Find on Soulseek…`/`Import from Spotify` command-table entries with one `Find Music…` entry (Ctrl+F).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_commands.py (append)

def test_find_music_routes_spotify_link_to_batch_import(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    calls = []
    monkeypatch.setattr(window, "_get_slskd_client", lambda: object())
    monkeypatch.setattr(window, "_open_batch_import", lambda link: calls.append(link))
    # dialog.exec() is modal and blocks in real use — run_search is wired
    # to the Search button *before* exec() is called, so replacing exec()
    # with a no-op that returns immediately (Rejected) lets _find_music()
    # return control to the test without ever blocking, while leaving the
    # live dialog object and its button wiring fully intact to drive by
    # hand afterward.
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)

    window._find_music()
    dialog = window._active_find_music_dialog
    dialog.query_input.setText("https://open.spotify.com/playlist/abc123")
    dialog.search_button.click()

    assert calls == ["https://open.spotify.com/playlist/abc123"]


def test_command_table_has_one_find_music_entry_not_two(window):
    labels = [cmd.label for cmd in window._commands]
    assert "Find Music…" in labels
    assert "Find on Soulseek…" not in labels
    assert "Import from Spotify" not in labels
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v -k find_music`
Expected: FAIL — `_find_music` doesn't exist yet.

- [ ] **Step 3: Implement `_find_music`**

Replace `_find_on_soulseek` in `app.py` with:

```python
def _find_music(self) -> None:
    if self._soulseek_download_thread is not None and self._soulseek_download_thread.isRunning():
        QMessageBox.information(
            self, "Soulseek Download In Progress",
            "A Soulseek download is already running — wait for it to "
            "finish before starting another one.",
        )
        return

    client = self._get_slskd_client()
    if client is None:
        return

    dialog = SoulseekSearchDialog("", self)
    dialog.query_input.setPlaceholderText("Search Soulseek, or paste a Spotify link…")
    self._active_find_music_dialog = dialog   # exposed for testability

    def run_search() -> None:
        query = dialog.query_input.text().strip()
        if not query:
            return
        if is_spotify_link(query):
            dialog.reject()
            self._open_batch_import(query)
            return
        dialog.set_searching(True)
        self._soulseek_search_thread = SoulseekSearchThread(client, query)
        self._soulseek_search_thread.results_ready.connect(
            lambda results: (dialog.set_searching(False), dialog.show_results(results))
        )
        self._soulseek_search_thread.search_failed.connect(
            lambda msg: (dialog.set_searching(False), dialog.show_error(msg))
        )
        self._soulseek_search_thread.start()

    dialog.search_button.clicked.connect(run_search)

    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_file:
        self._start_soulseek_download(client, dialog.selected_file, auto_fetch_lyrics=True)
```

Add `from spotify_playlist import is_spotify_link` to `app.py`'s imports (it's already importing other things from `spotify_playlist` indirectly via `workers.py`'s `TYPE_CHECKING` import — this is a new direct import). Add a stub `_open_batch_import` for now (fully implemented in Task 6):

```python
def _open_batch_import(self, link: str) -> None:
    raise NotImplementedError  # implemented in Task 6
```

Connect the new entry point where `_find_on_soulseek` used to be wired — both `self.library_panel.find_music_requested.connect(self._find_music)` (new, in the `LibraryPanel` construction from Task 1's Step 5) and update `_build_commands()`:

```python
_Command("Find Music…", "Ctrl+F", self._find_music, "tools"),
```

removing the old `"Find on Soulseek…"` and `"Import from Spotify"` entries. Delete `_go_to_download_tab` (no longer meaningful once tabs are gone — Task 6 removes its remaining reference).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v`
Expected: both tests PASS. `test_find_music_routes_spotify_link_to_batch_import` monkeypatches `window._open_batch_import` directly before calling `_find_music()`, so the real stub (still `raise NotImplementedError`) is never actually invoked at this point — the stub only needs to exist so `_find_music`'s reference to `self._open_batch_import` resolves at all. Task 6 replaces the stub with a real implementation; this test keeps passing unchanged since it never depended on the stub's body.

- [ ] **Step 5: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/app.py tests/test_app_commands.py
git commit -m "Add unified _find_music() entry point, routing by Spotify-link detection"
```

---

### Task 6: The workspace `QStackedWidget` — Editor / Acquire-Review / Empty

The largest task: removes the `QTabWidget`, builds the three-page stack, implements the empty state, wires `_open_batch_import`, and makes workspace swaps preserve `DownloadView`'s running threads.

**Files:**
- Modify: `lrc_studio/app.py`
- Test: `tests/test_workspace.py` (new)

**Interfaces:**
- Consumes: `LibraryPanel.song_selected` (Task 1), `DownloadView.file_downloaded`/`files_ready` (Task 4), `_open_batch_import` stub (Task 5).
- Produces: `self.workspace: QStackedWidget` with three pages at fixed indices (`WORKSPACE_EDITOR = 0`, `WORKSPACE_ACQUIRE = 1`, `WORKSPACE_EMPTY = 2`, module-level constants in `app.py`), and `self._open_batch_import(link: str) -> None` fully implemented.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QStackedWidget

import app as app_module
from app import LrcSyncApp, WORKSPACE_ACQUIRE, WORKSPACE_EDITOR, WORKSPACE_EMPTY


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeFetchThread:
    """Stands in for workers.PlaylistFetchThread so _open_batch_import
    (and anything that reaches DownloadView.start_fetch) never starts a
    real thread that hits the network in a test."""
    fetched = type("Signal", (), {"connect": lambda self, fn: None})()
    fetch_failed = type("Signal", (), {"connect": lambda self, fn: None})()

    def __init__(self, *a, **k) -> None: ...
    def start(self) -> None: ...


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    ini_path = str(tmp_path / "test_settings.ini")
    monkeypatch.setattr(
        app_module, "QSettings",
        lambda *a, **k: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    import download_view as download_view_module
    monkeypatch.setattr(download_view_module, "PlaylistFetchThread", _FakeFetchThread)

    win = LrcSyncApp()
    yield win
    win.close()


def test_workspace_is_a_stacked_widget_with_three_pages(window):
    assert isinstance(window.workspace, QStackedWidget)
    assert window.workspace.count() == 3


def test_starts_on_the_empty_page_with_no_song_loaded(window):
    assert window.workspace.currentIndex() == WORKSPACE_EMPTY


def test_selecting_a_song_swaps_to_the_editor_page(window, tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window.library_panel.add_files([str(f)])

    window.library_panel.song_selected.emit(str(f))

    assert window.workspace.currentIndex() == WORKSPACE_EDITOR


def test_open_batch_import_swaps_to_the_acquire_page(window):
    window._open_batch_import("https://open.spotify.com/playlist/abc")
    assert window.workspace.currentIndex() == WORKSPACE_ACQUIRE


def test_download_view_instance_survives_workspace_swaps(window, tmp_path):
    # A batch import must not be rebuilt (and its threads lost) just
    # because the workspace swaps away and back.
    view_before = window.download_view
    window._open_batch_import("https://open.spotify.com/playlist/abc")

    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    window.library_panel.add_files([str(f)])
    window.library_panel.song_selected.emit(str(f))   # swap to editor
    window._open_batch_import("https://open.spotify.com/playlist/abc")  # swap back

    assert window.download_view is view_before


def test_empty_state_add_music_button_reaches_the_same_signal_as_the_sidebar(window):
    received = []
    window.library_panel.find_music_requested.connect(lambda: received.append(True))

    window._empty_state_add_music_button.click()

    assert received == [True]


def test_file_downloaded_adds_to_library_panel_live(window, tmp_path):
    f = tmp_path / "new_song.mp3"
    f.write_bytes(b"")

    window.download_view.file_downloaded.emit(str(f))

    assert str(f) in window.library_panel.real_file_paths()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_workspace.py -v`
Expected: FAIL — `ImportError: cannot import name 'WORKSPACE_ACQUIRE' from 'app'`

- [ ] **Step 3: Build the stack**

Add module-level constants near the top of `app.py` (after the `_Command` dataclass):

```python
WORKSPACE_EDITOR = 0
WORKSPACE_ACQUIRE = 1
WORKSPACE_EMPTY = 2
```

Replace the `QTabWidget` construction in `_init_ui`:

```python
self.tabs = QTabWidget()
self.setCentralWidget(self.tabs)
self.tabs.addTab(self._build_library_tab(), "Library")

self.download_view = DownloadView(...)
self.download_view.files_ready.connect(self._on_download_files_ready)
self.download_view.open_in_library.connect(self._open_in_library)
self.tabs.addTab(self.download_view, "Download")
```

with:

```python
central = QWidget()
self.setCentralWidget(central)
root_layout = QVBoxLayout(central)
root_layout.setContentsMargins(15, 15, 15, 15)
root_layout.setSpacing(10)

root_layout.addLayout(self._build_top_bar())

self.karaoke_label = QLabel("Waiting for song…")
# ... (unchanged: karaoke_label construction, exactly as today)
root_layout.addWidget(self.karaoke_label)

self.timeline = TimelineWidget()
# ... (unchanged: timeline construction and signal wiring, exactly as today)
root_layout.addWidget(self.timeline)

self.splitter = QSplitter(Qt.Orientation.Horizontal)
self.library_panel = LibraryPanel(self.settings)
self.library_panel.song_selected.connect(self._load_song)
self.library_panel.file_renamed.connect(self._on_file_renamed)
self.library_panel.find_music_requested.connect(self._find_music)
# _batch_download_lyrics still takes no arguments at this point in the
# plan (it computes its own missing-files list internally, per Task 1) —
# Task 7 changes its signature to take the list directly and tightens
# this connection to a plain `self._batch_download_lyrics` once that's
# done. Wrapped in a lambda now purely so the signal (which emits a
# list) and the slot (which currently takes none) stay compatible in
# the meantime.
self.library_panel.fetch_missing_lyrics_requested.connect(lambda paths: self._batch_download_lyrics())
self.splitter.addWidget(self.library_panel)

self.workspace = QStackedWidget()
self.workspace.addWidget(self._build_lyrics_panel())      # WORKSPACE_EDITOR = 0

self.download_view = DownloadView(
    self.settings, self._get_slskd_client, self._collect_library_paths, self,
)
self.download_view.files_ready.connect(self._on_download_files_ready)
self.download_view.file_downloaded.connect(self.library_panel.add_files_single)
self.download_view.open_in_library.connect(self._open_in_library)
self.workspace.addWidget(self.download_view)               # WORKSPACE_ACQUIRE = 1

self.workspace.addWidget(self._build_empty_state())         # WORKSPACE_EMPTY = 2

self.splitter.addWidget(self.workspace)
self.splitter.setStretchFactor(0, 1)
self.splitter.setStretchFactor(1, 3)
root_layout.addWidget(self.splitter, stretch=1)

self.status_bar = QLabel("Ready")
# ... (unchanged: status bar / progress bar construction, exactly as today)
root_layout.addLayout(stat_row)

self.workspace.setCurrentIndex(WORKSPACE_EMPTY)
```

`download_view.file_downloaded.connect(self.library_panel.add_files_single)` needs a one-argument wrapper since `add_files` takes a list — add this tiny method to `LibraryPanel` (Task 1's `add_files` already exists; add `add_files_single`):

```python
# library_panel.py
def add_files_single(self, path: str) -> None:
    self.add_files([path])
```

This inlines `_build_library_tab`'s entire former body (top bar, karaoke label, timeline, splitter, status bar — all previously constructed inside it) directly into `_init_ui`. **Delete `_build_library_tab` as a separate method entirely** — its content now lives inline in `_init_ui` as shown above, and `_build_top_bar()`/`_build_lyrics_panel()` are called directly from `_init_ui` rather than from within it. Grep `grep -n "_build_library_tab" lrc_studio/app.py` afterward to confirm zero remaining references (the method definition and its single former call site should both be gone).

Build the empty state:

```python
def _build_empty_state(self) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    label = QLabel("Select a song from your library, or add music to get started.")
    label.setStyleSheet(f"color: {COLOR_MUTED.name()}; font-size: 14px;")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)

    self._empty_state_add_music_button = QPushButton("+ Add Music")
    self._empty_state_add_music_button.clicked.connect(self.library_panel.find_music_requested.emit)
    self._empty_state_add_music_button.setStyleSheet(self._primary_button_style())
    layout.addWidget(self._empty_state_add_music_button, alignment=Qt.AlignmentFlag.AlignCenter)

    return page
```

(This routes the empty state's button through `find_music_requested` specifically — the far more common zero-state need is "go find something," with "Add Files" still reachable via the always-visible sidebar button. This satisfies the Review Focus item about not diverging from the sidebar's own signal path.)

Update `_load_song` (or wherever a song finishes loading) to swap the workspace:

```python
self.workspace.setCurrentIndex(WORKSPACE_EDITOR)
```

— add this line at the point `_load_song` currently sets `self.current_file_path`/`self.song_filename` etc.

Implement `_open_batch_import` (replacing Task 5's stub):

```python
def _open_batch_import(self, link: str) -> None:
    self.workspace.setCurrentIndex(WORKSPACE_ACQUIRE)
    self.download_view.start_fetch(link)
```

Update `_on_download_files_ready` to return to the editor with the first newly downloaded song loaded, once a batch completes:

```python
def _on_download_files_ready(self, paths: list[str]) -> None:
    self.library_panel.add_files(paths)
    self._fetch_lyrics_for(paths)
    if paths:
        self._load_song(paths[0])
        self.library_panel.select_path(paths[0])
```

Remove `_go_to_download_tab`'s remaining reference (deleted in Task 5) and every remaining `self.tabs.*` call:

- `_open_in_library`'s `self.tabs.setCurrentWidget(...)` becomes `self.workspace.setCurrentIndex(WORKSPACE_EDITOR)`.
- `_restore_window_state` has a `last_tab` block (`last_tab = self.settings.value("last_tab", 0, type=int); if 0 <= last_tab < self.tabs.count(): self.tabs.setCurrentIndex(last_tab)`) — delete this block entirely; there's no equivalent to restore since the workspace always starts on `WORKSPACE_EMPTY` by design (loading a specific song on startup isn't part of this spec).
- `closeEvent`'s `self.settings.setValue("last_tab", self.tabs.currentIndex())` — delete this line for the same reason. Leave the rest of `closeEvent` (the dirty-check, geometry/splitter-state saving, `stop_thread(...)` calls, `self.download_view.shutdown()`) untouched — `self.download_view` still exists, just as a workspace page instead of a tab, so `closeEvent`'s existing `self.download_view.shutdown()` call keeps working with no change.

Run `grep -n "self\.tabs\b" lrc_studio/app.py` after these edits to confirm zero remaining hits.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_workspace.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite and compile check**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/python -m py_compile lrc_studio/*.py && M/bin/pytest tests/ -q`
Expected: everything passes. Fix any remaining `self.tabs`/`self.file_list` references the compiler or a runtime `AttributeError` surfaces — grep for `self\.tabs\b` and `self\.file_list\b` in `app.py` to confirm zero remaining hits outside comments.

- [ ] **Step 6: Screenshot all three workspace states**

Extend the headless screenshot script (real palette, `QSettings` redirected) to capture: the empty state on startup, the editor after loading a test song, and the acquire/review state after calling `_open_batch_import` with a fake link (the table will show no rows since no real network call happens, but confirm the layout renders without error). Save and view all three PNGs.

- [ ] **Step 7: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/app.py lrc_studio/library_panel.py tests/test_workspace.py
git commit -m "Replace QTabWidget with a 3-state workspace stack (Editor/Acquire/Empty)"
```

---

### Task 7: `_batch_download_lyrics` signature update

`LibraryPanel.fetch_missing_lyrics_requested` (Task 2) now passes the missing-paths list directly; `app.py`'s `_batch_download_lyrics` currently computes that list itself from `self.file_list` (already replaced in Task 1, but re-check the signature matches the new caller).

**Files:**
- Modify: `lrc_studio/app.py`
- Test: `tests/test_app_commands.py` (extend)

**Interfaces:**
- Consumes: `LibraryPanel.fetch_missing_lyrics_requested(list[str])` (Task 2).
- Produces: `_batch_download_lyrics(self, files: list[str]) -> None` (signature changed from no-arg to taking the paths directly).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_commands.py (append)

def test_batch_download_lyrics_accepts_paths_directly(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_downloader", None)

    class FakeThread:
        def __init__(self, files):
            started.append(files)
        progress = type("Signal", (), {"connect": lambda self, fn: None})()
        finished_batch = type("Signal", (), {"connect": lambda self, fn: None})()
        def start(self): pass

    monkeypatch.setattr(app_module, "LyricsDownloaderThread", FakeThread)

    window._batch_download_lyrics(["/fake/a.mp3", "/fake/b.mp3"])

    assert started == [["/fake/a.mp3", "/fake/b.mp3"]]


def test_batch_download_lyrics_with_empty_list_shows_info_and_starts_nothing(window, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "LyricsDownloaderThread", lambda files: started.append(files))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    window._batch_download_lyrics([])

    assert started == []
```

(`app_module` and `QMessageBox` are already imported at the top of `test_app_commands.py` from Task prior work — confirm the import line includes `import app as app_module` per the `test_workspace.py` pattern; add it if `test_app_commands.py` doesn't already have it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v -k batch_download_lyrics`
Expected: FAIL — `TypeError: _batch_download_lyrics() takes 1 positional argument but 2 were given` (it currently takes no args beyond `self`).

- [ ] **Step 3: Update the signature**

```python
def _batch_download_lyrics(self, files: list[str]) -> None:
    if not files:
        QMessageBox.information(self, "Info", "No missing lyrics to download.")
        return

    self.progress_bar.setRange(0, len(files))
    self.progress_bar.setValue(0)
    self.progress_bar.setVisible(True)

    self._downloader = LyricsDownloaderThread(files)
    self._downloader.progress.connect(self._on_download_progress)
    self._downloader.finished_batch.connect(self._on_batch_finished)
    self._downloader.start()
```

(This removes the old file-scanning loop — `LibraryPanel.paths_with_status(COLOR_MISSING)` already did that work before emitting the signal, per Task 2.)

Also tighten the connection Task 6 left as a discarding lambda: in `_init_ui`, replace

```python
self.library_panel.fetch_missing_lyrics_requested.connect(lambda paths: self._batch_download_lyrics())
```

with

```python
self.library_panel.fetch_missing_lyrics_requested.connect(self._batch_download_lyrics)
```

now that the signal's `list` argument and the slot's `files: list[str]` parameter match directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/app.py tests/test_app_commands.py
git commit -m "Update _batch_download_lyrics to take paths directly from LibraryPanel"
```

---

### Task 8: `Get Lyrics` action and the `_clear_lyrics`/`_replace_all_verses` split

Fixes the bug where fetching new lyrics currently pops the Clear-All confirmation as an unintended side effect.

**Files:**
- Modify: `lrc_studio/app.py`
- Test: `tests/test_app_commands.py` (extend)

**Interfaces:**
- Produces: `_replace_all_verses(self, lines: list[tuple[str, str]]) -> None` (no confirmation; the primitive). `_clear_lyrics()` keeps its existing confirmation-then-wipe behavior, now implemented in terms of the primitive. `_get_lyrics_online` (renamed to back the "Get Lyrics" button/label) uses the primitive, not `_clear_lyrics()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_app_commands.py (append)

def test_get_lyrics_does_not_trigger_the_clear_confirmation(window, monkeypatch):
    window._add_verse("[00:01.00]", "old line")
    window.current_file_path = "/fake/song.mp3"  # _get_lyrics_online guards on song_filename
    window.song_filename = "song.mp3"

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert asked == []
    assert len(window._verses) == 1
    assert window._verses[0].get_verse_text() == "new line"


def test_clear_lyrics_still_confirms_after_the_split(window, monkeypatch):
    window._add_verse("[00:01.00]", "line one")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)

    window._clear_lyrics()

    assert len(window._verses) == 1  # cancelled -> untouched, same as before the split


def test_replace_all_verses_clears_undo_stack_stale_entries(window):
    # Sanity check: replacing the verse list shouldn't leave undo entries
    # pointing at verse objects that no longer exist in self._verses.
    window._add_verse("[00:01.00]", "old line")
    old_verse = window._verses[0]
    window._undo_stack.append((old_verse, "[00:00.00]"))

    window._replace_all_verses([("[00:05.00]", "new line")])

    assert old_verse not in window._verses
    # The undo entry for the removed verse is harmless (already covered by
    # _undo_last_sync's "skip stale entries" logic from the earlier fix),
    # but confirm the new verse list is exactly the replacement.
    assert [v.get_verse_text() for v in window._verses] == ["new line"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v -k "replace_all_verses or get_lyrics_does_not"`
Expected: FAIL — `AttributeError: 'LrcSyncApp' object has no attribute '_replace_all_verses'`

- [ ] **Step 3: Split `_clear_lyrics`**

```python
def _replace_all_verses(self, lines: list[tuple[str, str]]) -> None:
    """Wipe the current verse list and replace it with *lines*
    ((timestamp_str, text) pairs) — no confirmation. The primitive
    behind both Get Lyrics (an intentional replace) and Clear All
    (which adds its own confirmation before calling this with [])."""
    for v in self._verses:
        self.lyrics_layout.removeWidget(v)
        v.deleteLater()
    self._verses.clear()
    self._playback_highlight_verse = None
    self.current_verse_index = 0
    for ts_str, content in lines:
        self._add_verse(ts_str, content)
    self._update_timeline()
    self._mark_dirty()

def _clear_lyrics(self) -> None:
    if self._verses and QMessageBox.question(
        self,
        "Clear All Lyrics",
        f"Remove all {len(self._verses)} line(s) for "
        f"'{self.song_filename}'? This can't be undone.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    ) != QMessageBox.StandardButton.Yes:
        return
    self._replace_all_verses([])
```

Update `_on_search_result` (the lyrics-search-thread completion handler, currently calling `self._clear_lyrics()` unconditionally) to call the primitive instead:

```python
def _on_search_result(self, result: str) -> None:
    if result:
        lines, offset_ms = parse_lrc_file(result)
        if offset_ms:
            adjusted = []
            for ts_str, content in lines:
                base_ms = parse_lrc_timestamp(ts_str)
                adjusted.append((ms_to_lrc_timestamp(max(0, base_ms + offset_ms)), content))
            lines = adjusted
            self.status_bar.setText(f"Applied LRC global offset: {offset_ms:+d} ms")
        self._replace_all_verses(lines)
        self._mark_dirty()
        self.status_bar.setText("Lyrics found!")
    else:
        QMessageBox.information(self, "Sorry", "No lyrics found.")
        self.status_bar.setText("Ready")
```

(This inlines what `_parse_and_add_lyrics` used to do — check whether `_parse_and_add_lyrics` is called anywhere else in `app.py` before deleting it; if it's only ever called from `_on_search_result`, delete it and keep the logic inlined as above for clarity. If something else still calls it, keep it as a helper and have `_on_search_result` call `self._replace_all_verses(...)` using its return value instead of reimplementing the offset math.)

Rename the button/label from "Web Search" to "Get Lyrics" in `_build_lyrics_panel`'s action row (the button constructed with `web_search_btn = QPushButton("Web Search")`):

```python
get_lyrics_btn = QPushButton("Get Lyrics")
get_lyrics_btn.setToolTip("Search online for synced lyrics for this song")
get_lyrics_btn.clicked.connect(self._get_lyrics_online)
get_lyrics_btn.setStyleSheet(self._neutral_button_style())
fetch_row.addWidget(get_lyrics_btn)
```

(the `"Fetch"` group label above it already reads correctly and doesn't need changing.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/test_app_commands.py -v`
Expected: all PASS, including the three pre-existing `test_clear_lyrics_*` tests from the previous UX pass (confirm they still pass unmodified — the public behavior of `_clear_lyrics()` hasn't changed, only its internals).

- [ ] **Step 5: Run the full suite**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/ -q`
Expected: all passing.

- [ ] **Step 6: Screenshot check**

Re-run the editor-state screenshot. Confirm the button reads "Get Lyrics" instead of "Web Search."

- [ ] **Step 7: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/app.py tests/test_app_commands.py
git commit -m "Split _clear_lyrics into a confirming wrapper and a silent _replace_all_verses primitive; rename Web Search to Get Lyrics"
```

---

### Task 9: Restyle the Acquire/Review table to match the library's visual language

The one part of the spec explicitly flagged as fuzzy — a visual judgment call, verified by looking, not by an automated assertion.

**Files:**
- Modify: `lrc_studio/download_view.py`

- [ ] **Step 1: Take a "before" screenshot**

Using the headless screenshot script from Task 6, capture the Acquire/Review workspace page as it looks going into this task.

- [ ] **Step 2: Align the table's visual language with `LibraryPanel`'s**

In `download_view.py`, update `self.table`'s stylesheet (currently a bespoke block with its own font-size/padding/border-radius values, and a `QHeaderView::section` using literal `#313244`/`white`) to match `LibraryPanel.file_list`'s stylesheet exactly in border color, border-radius, and font-size (`#45475a` border, `4px` radius, `13px` font — the same values `library_panel.py` already uses), and fix `color: white` in the header style to `COLOR_FG.name()` (an off-palette literal, same class of issue already fixed elsewhere in the app). Keep the table's status-badge coloring (per-row Match/Status columns) as-is — that's functional color-coding, not the inconsistency being fixed here.

- [ ] **Step 3: Take an "after" screenshot and compare side by side**

View both PNGs. Confirm: consistent border color/radius between the library list and the acquire table, no `white` literals remaining (`grep -n "color: white" lrc_studio/download_view.py` returns nothing), row height and font size read as part of the same app rather than a visually distinct table.

- [ ] **Step 4: Run the full suite**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/pytest tests/ -q`
Expected: all passing (this task changes only stylesheet strings, no logic).

- [ ] **Step 5: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add lrc_studio/download_view.py
git commit -m "Restyle the Acquire/Review table to match the library panel's visual language"
```

---

### Task 10: Final verification pass and README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Full headless smoke test through the whole new flow**

```python
# Run inline via: cd lrc_studio && QT_QPA_PLATFORM=offscreen ../M/bin/python -c "..."
# Construct the app with an isolated QSettings, verify:
#  - starts on WORKSPACE_EMPTY
#  - add a fake file via library_panel.add_files(), confirm it appears
#  - emit library_panel.song_selected, confirm workspace swaps to WORKSPACE_EDITOR
#  - call _open_batch_import with a fake link, confirm workspace swaps to WORKSPACE_ACQUIRE
#  - close the window, confirm no exception
```

Expected: no exceptions, all assertions hold.

- [ ] **Step 2: Run the complete test suite one final time**

Run: `cd /home/georgiou/Documents/LrcSync && M/bin/python -m py_compile lrc_studio/*.py && M/bin/pytest tests/ -q`
Expected: all tests passing, no compile errors.

- [ ] **Step 3: Confirm the real QSettings file was never touched**

Run: `stat -c '%Y %n' ~/.config/LrcStudio/*.conf 2>/dev/null` before and after the full suite run — the mtime must be identical, per the Global Constraints isolation requirement.

- [ ] **Step 4: Update README's "User Interface" section**

Read the current `## User Interface` section (describes "Two tabs: Download... and Library..."). Rewrite it to describe the new layout: persistent library panel on the left with an Add Music menu and overflow menu, workspace on the right that shows the editor, an acquire/review flow, or an empty state. Update any other README passages that reference "Download tab" or "Library tab" by name (search with `grep -n "Download tab\|Library tab" README.md`) to match the new structure.

- [ ] **Step 5: Take final screenshots of all three workspace states for the commit record**

Save to `/tmp/` as before; not committed to the repo, just for your own final visual confirmation before wrapping up.

- [ ] **Step 6: Commit**

```bash
cd /home/georgiou/Documents/LrcSync
git add README.md
git commit -m "Update README to describe the redesigned library/workspace layout"
```

---

## Self-Review Notes

**Spec coverage:** Window shape (Task 6) — covered. Library panel Add Music/overflow menus (Task 2) — covered. Find Music unified entry + Spotify-link routing (Tasks 3, 5) — covered. Workspace states including empty state (Task 6) — covered. `_clear_lyrics`/Get Lyrics bug fix (Task 8) — covered. Command table changes (Task 5) — covered. Live library updates during batch downloads (Tasks 4, 6) — covered. Visual restyling of the acquire table (Task 9) — covered, explicitly scoped as a manual/visual task per the spec's own admission this part is fuzzy. README update (Task 10) — covered.

**Placeholder scan:** no TBD/TODO markers; every step has real code or an explicit, concrete shell command.

**Type consistency:** `LibraryPanel.add_files(paths: list[str])` used consistently in Tasks 1, 6. `find_music_requested`/`fetch_missing_lyrics_requested` signal signatures match between their Task 2 definition and their Task 5/6/7 connection sites. `WORKSPACE_EDITOR`/`WORKSPACE_ACQUIRE`/`WORKSPACE_EMPTY` constants defined once in Task 6, referenced (not redefined) in later tests.

**Review Focus:** all five items from the header have an owning task and test, as listed inline in each task above.
