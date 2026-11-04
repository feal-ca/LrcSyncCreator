# LRC Studio polish: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LRC Studio fast and obvious to use: new songs surface on their own, every library action is a right-click away and works on a selection, Soulseek search behaves like Nicotine+ (results stream in, Enter downloads the best copy), and lyrics come from more sources with stricter matching.

**Architecture:** Existing PyQt6 app in `lrc_studio/`. Qt-free logic lives in plain modules with unit tests (`library_index.py`, `postprocess.py`, `slsk_client.py`, `lyrics_fetch.py`/`lyrics_providers.py`); widgets stay thin. slskd (external daemon + REST) is replaced by an in-process Soulseek client built on `aioslsk`, running its asyncio loop in one background thread behind a synchronous facade with the same surface the download engine already uses.

**Tech Stack:** Python 3.14, PyQt6 6.10, mutagen, requests, rapidfuzz, python-vlc, aioslsk 1.7.x (new), pytest.

**Spec:** this conversation's agreed requirements, summarised here:
1. New songs are easy to find (user complaint 1).
2. Multi-select → right-click → Standardize names, and right-click everywhere; buttons must be intuitive (complaint 2).
3. Downloading from Soulseek must be easier than Nicotine+; use the in-process protocol like Nicotine+ does, rewrite allowed (complaint 3).
4. More/alternative lyrics providers to cross-check and fill gaps; Musixmatch/Spotify are out (decoy lyrics / ToS) (complaint 4).
5. Self-critique via subagents; ship a polished product.

## Global Constraints

- Never write to `~/Music` or the real QSettings (`~/.config/LrcStudio/Configs.conf`) from tests or scripts; tests use `QSettings(tmp_path/"x.ini", IniFormat)` and `QT_QPA_PLATFORM=offscreen`.
- The library is Syncthing-synced: renames, trash and conversion propagate. Every multi-file destructive action shows exactly what will happen and asks once.
- Never overwrite a song's existing synced `.lrc` automatically.
- UI copy follows the wording list in the UX critique (e.g. "Find lyrics…", "Shift timing…", "Rename to “Artist - Title”").
- Run the suite with `/home/georgiou/Documents/LrcSync/M/bin/python -m pytest -q` from the worktree root.

## Review Focus

- A song renamed or trashed while it is loaded in the editor/player: the editor must follow the rename, and trash must unload it first.
- Two selected songs whose standardized names collide (same tags): the second is skipped, nothing is overwritten.
- A library folder that changes on disk between scans (Syncthing): vanished files drop out, the index forgets them, nothing crashes.
- Soulseek credentials wrong, or the account logged in elsewhere: a clear message, no hang, the rest of the app keeps working.
- A lyrics provider that times out or returns garbage (credit lines, decoys, wrong song): the song stays "no lyrics" rather than getting wrong lyrics.

---

## Phase A: library and app shell

### Task A1: `library_index.py`, first-seen dates and "new" flags

**Files:** Create `lrc_studio/library_index.py`, `tests/test_library_index.py`.

**Interfaces (produces):**
- `class LibraryIndex(path: str)`; `load() -> None`; `save() -> None`
- `sync(paths: list[str], now: float | None = None) -> None`: the first sync ever (no file on disk) seeds every path with its mtime, marked seen; later syncs add unknown paths with `now`, unseen; paths not in *paths* are forgotten.
- `added_at(path) -> float`, `is_new(path) -> bool`, `mark_seen(paths: list[str]) -> None`, `rename(old, new) -> None`, `forget(path) -> None`, `add(paths, now=None)` (unseen, keeps existing entries).

Tests: first sync marks nothing new and uses mtimes; second sync with an extra file marks only it new with `added_at == now`; `rename` keeps date and flag; `mark_seen` clears the flag; save/load round-trip; a corrupt JSON file starts empty without raising.

### Task A2: rename planning in `postprocess.py`

**Interfaces (produces):** `@dataclass RenamePlan(old: str, new: str | None, reason: str)`; `plan_standard_names(paths: list[str]) -> list[RenamePlan]` (new is `None` with a reason when tags are missing, the name is already standard, or the target exists / collides with an earlier plan); `apply_renames(plans) -> list[tuple[str, str]]` moves audio + sidecar `.lrc`, returns the (old, new) pairs done.

Tests: tag-based name; missing tags skipped with reason "No artist/title tags"; already-standard skipped; two files with the same tags, second skipped; `.lrc` follows; target exists, skipped.

### Task A3: library panel rewrite

**Files:** Modify `lrc_studio/library_panel.py`, `tests/test_library_panel.py`.

- Header: "Library" heading; search box; one filter combo with live counts ("All songs (N)", "New (n)", "No lyrics (n)", "Not synced (n)"); sort combo "Newest first" / "A–Z" (persisted in settings key `library_sort`, default newest).
- List: `ExtendedSelection`; a path→item dict instead of linear scans; a delegate draws an accent "new" pill on new rows; tooltips show "Added <date>".
- Context menu, one song: Open in Editor · Find Lyrics… · Rename to “<actual new name>” (disabled with tooltip when no plan) · Edit Artist & Title… · Show in Folder · Mark as Seen (if new) · Move to Trash….
- Context menu, several: Find Lyrics for N Songs · Rename N to “Artist - Title”… · Convert N to Opus… (only if lossless among them) · Mark N as Seen · Move N to Trash….
- Keys on the list: Enter opens, Del trashes, F2 edits tags, Ctrl+R renames, Ctrl+A selects visible.
- Signals out: `song_selected(str)`, `file_renamed(str, str)`, `lyrics_requested(list)`, `lyrics_picker_requested(str)`, `convert_requested(list)`, `files_trashed(list)`.
- Opening a song marks it seen.

Tests: sort order newest/A–Z; "New" filter count; context-menu actions list for one vs several (via a `menu_actions_for(paths) -> list[str]` helper); rename via `apply_renames` updates rows and emits `file_renamed`; trash removes rows and emits `files_trashed` (trash function injected).

### Task A4: app shell

**Files:** Modify `lrc_studio/app.py`, `lrc_studio/dialogs.py`, tests.

- Menus: File (Settings… Ctrl+,, Rescan Library F5, Quit Ctrl+Q), Lyrics (Save Lyrics Ctrl+S, Undo Ctrl+Z, Find Lyrics… Ctrl+L, Shift Timing… Ctrl+Shift+O, Sort Lines by Time Ctrl+T, Check Sync…, Clear Lyrics…), Help. Ctrl+F (Search) and Ctrl+J (Downloads) stay as shortcuts. "Add Files…" and the Tools menu go.
- Tabs: Editor, Search, Downloads. The Spotify review page is shown when a link is pasted into Search and keeps the Search tab selected.
- Editor bar: Save (primary), Find lyrics…, Shift timing…, stretch, Clear all. The embed checkbox goes; Save honours the `embed_lyrics` setting (fixes the out-of-sync checkbox bug).
- Empty state: "Open a song to edit its lyrics", no button. A song without lyrics shows a "No lyrics yet" card with a primary "Find lyrics" button above the empty line.
- Trash flow: confirm, stop/unload the player if the current song is among them, `QFile.moveToTrash` audio + `.lrc`.

## Phase B: Soulseek without slskd

Detailed from the aioslsk research report. Outline:
- B1 `slsk_client.py`: facade with `check_connection`, `search`, `start_search(query, on_results, on_done) -> int`, `stop_search(id)`, `browse_folder`, `download`, `cancel_download`, `get_download_transfer` (slskd-shaped dicts), `close()`; credentials from settings, migrated once from `slskd-bin/slskd.yml`.
- B2 app wiring and Settings (username, password, share music folder, test connection); slskd process code and scripts removed.
- B3 search page: streaming, grouped by version, relevance ranking, Enter = best copy with fallbacks, Shift+Enter = album, per-row status, "In library" marks, text filter + "Lossless only".
- B4 downloads page: Song + Status columns, username in tooltip.

## Phase C: lyrics sources

Detailed from the lyrics-provider research report. Outline: providers module, NetEase credit-line stripping, cross-checking to pick the best candidate, picker searches every source in one pass, plain lyrics only upgrade to synced.

User ruling (2026-11-04): auto-fetch must NOT become stricter than today; the goal is MORE songs with lyrics. Extra providers raise coverage, cross-checking chooses among candidates, and only clearly broken results are dropped (credit-line junk, decoys, timing past the end of the song, a different song entirely). Plain lyrics are still saved when nothing synced exists, as today.

## Phase D: verified bugs and speed findings from the code critique

## Phase E: README, full suite, screenshots, whole-branch review subagent
