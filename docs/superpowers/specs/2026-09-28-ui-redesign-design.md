# LRC Studio: ground-up UI/UX redesign

Status: draft, awaiting review.

## Problem

The app's UI grew feature-by-feature rather than from a single design: a
"Library" tab and a "Download" tab that feel like two different programs,
a sidebar with three separate "add music" buttons that are really one
action wearing different hats, and two near-identical "fetch lyrics"
buttons (Web Search, Download Lyrics) in two different panels. None of
this is a single bug — it's the accumulated shape of features added one
at a time without anyone stepping back to ask what the app's actual job
is end to end.

An incremental pass already fixed concrete inconsistencies (unified
keyboard shortcuts, fixed color misuse, added a missing confirmation —
see the "UX revamp" commit). This spec goes further: a structural
redesign driven by the actual user journey, not a rearrangement of
existing panels.

## The user journey this is designed around

A song in this app is always in exactly one of four states: **not in
your library**, **in your library with no lyrics**, **has lyrics but
unsynced**, or **synced**. Everything the app does either moves a song
through that pipeline or helps you sync one by hand. That's the whole
product.

1. You land on your library — one list, every song, status visible at a
   glance.
2. Getting more music is one action *from* the library, not a
   destination you navigate away to. Regardless of source, results land
   in the same list, styled the same way.
3. Click a song that needs work and the editor is just there — the
   permanent main workspace, not something you switch to. If it has no
   lyrics, the first thing offered is one "Get Lyrics" action, not two
   similarly-named buttons in two different panels.
4. Syncing by hand is unchanged: play, stamp lines, drag the timeline.
5. Save, and the natural next thing to work on is obvious.

**Getting more music is not source-differentiated.** Soulseek is the
only actual download mechanism; a Spotify link is just a convenient way
to specify *many* songs at once instead of typing each one. "Find on
Soulseek" and "Import from Spotify" are the same action at different
scale, not two different sources — the menu should reflect that.

## Goals

- One continuous workspace: a persistent library (left) and a main
  workspace (right) that's always either the editor, an acquire/review
  flow, or a real empty state — never a disconnected tab.
- One "Add Music" entry point with exactly two choices: local files, or
  "Find Music" (a single smart input that searches Soulseek directly for
  typed text, or pulls a tracklist and batch-searches when given a
  Spotify link) — not three parallel buttons.
- One "Get Lyrics" action per loaded song, replacing the Web
  Search/Download Lyrics split. Bulk "fetch lyrics for everything
  missing" still exists, relocated to occasional-maintenance territory.
- Rarely-used housekeeping (Standardize Names, Clear Completed) moves
  out of primary button real estate into an overflow menu.
- Fix the bug the last pass introduced: "Get Lyrics" replacing a song's
  lyrics currently routes through `_clear_lyrics()`, which now pops the
  "can't be undone" confirmation meant only for the explicit Clear All
  button. Needs its own silent internal path.

## Non-goals

- No visual/token system pass (color/spacing/typography formalization) —
  that's a separate, already-identified follow-up, deliberately not
  bundled in here.
- No change to the sync-editor's core mechanics (playback, timeline
  drag, keyboard shortcuts for syncing) — that part already works and
  isn't part of the complaint.
- No change to the underlying Soulseek/Spotify/lyrics-fetch *logic*
  (already hardened in the reliability pass) — this is a UI
  restructuring around existing, working backends.

## Architecture

### Window shape

The top-level `QTabWidget` (Library / Download tabs) is removed.
`LrcSyncApp`'s central widget becomes a horizontal split:

```
┌─────────────┬──────────────────────────────────────────┐
│             │  Top bar (play/pause, seek, device, title)│
│  Library    │  Karaoke display                          │
│  panel      │  Timeline                                 │
│  (fixed     ├──────────────────────────────────────────┤
│  width,     │                                            │
│  like       │  Workspace (swaps by state):               │
│  today's    │   - Editor: verse list + action row        │
│  file       │   - Acquire/Review: search or batch table  │
│  panel)     │   - Empty: "Select a song, or Add Music"   │
│             │                                            │
└─────────────┴──────────────────────────────────────────┘
```

The menu bar (File/Edit/Tools/Help) stays — it's not part of the
complaint and keyboard-shortcut discoverability still needs it — but a
few entries retarget (see "Command table changes" below).

### Library panel (left)

- **"+ Add Music"** button at the top of the panel, opening a small
  popup menu with two items:
  - **Add Files** — today's file picker, unchanged.
  - **Find Music** — opens the Find Music flow (below) in the workspace.
- The song list, unchanged in behavior and status-color coding.
- A small overflow (**⋮**) button on the panel header opens a menu with:
  Standardize Names, Clear Completed, Fetch Lyrics for All Missing (the
  bulk version of Get Lyrics, previously "Download Lyrics").

### Find Music flow (replaces "Find on Soulseek" + "Import from Spotify")

One entry point, one input box: "Search Soulseek, or paste a Spotify
link…". What happens next depends on what's typed, reusing
`extract_spotify_ref()` (already in `spotify_playlist.py`) to detect a
Spotify link:

- **Plain text** → today's `SoulseekSearchDialog` single-result picker
  (kept as-is; it already does the right thing for "search, pick one").
- **Spotify link** → the batch review flow (today's `DownloadView`
  table), shown *in the workspace area*, not a separate tab. Visually
  restyled to read as "an extension of the library, temporarily in
  review mode" — same row height/status-color language as the library
  list — rather than a structurally different-looking table. As rows
  finish downloading, the corresponding entries appear/highlight in the
  library panel immediately (a direct signal connection, not "switch
  tabs to check").

This is a UI-level consolidation of the entry point, not a rewrite of
`DownloadView`'s internals (search/download threading, match-scoring,
retry logic all stay as they are — restyled and re-homed only).

### Workspace states

1. **Editor** (today's lyrics panel, mostly unchanged): top bar, karaoke
   display, timeline, verse list, bottom action row. The action row
   drops "Web Search" and gains **"Get Lyrics"**, which does what
   today's Web Search does (prompt for a query, fetch, apply) but is the
   one obvious lyrics action for the loaded song regardless of whether
   it already has some lyrics or none. Save/Sort Lines/Shift All/Clear
   All stay as the last pass left them.
2. **Acquire/Review** (the Find Music results — single dialog or batch
   table per above).
3. **Empty state**: shown when no song is loaded and no acquire flow is
   active. Replaces the current "Waiting for song…" karaoke label sitting
   above an otherwise-inert editor shell with copy that actually invites
   an action: "Select a song from your library, or Add Music to get
   started," plus the Add Music button repeated here for a zero-state
   affordance.

### The `_clear_lyrics()` bug fix

`_clear_lyrics()` currently does two jobs: it's the internal "wipe the
in-memory verse list" primitive *and* the user-facing Clear All handler
with its confirmation prompt. Split these:

- `_replace_all_verses(lines)` — the primitive, no confirmation, no
  prompt. Used internally by Get Lyrics (and anywhere else that needs to
  swap in a new lyric set).
- `_clear_lyrics()` — stays as the Clear All button's handler:
  confirmation prompt, then calls the primitive with an empty list.

### Command table changes

`_build_commands()` (the single source of truth added in the last pass)
updates to match:

- "Find on Soulseek…" and "Import from Spotify" collapse into one
  **"Find Music…"** Tools-menu entry, opening the same flow as the
  sidebar's Add Music → Find Music.
- "Validate Sync…" and "Settings…" are unaffected.
- No shortcut changes beyond dropping the now-merged item's duplicate
  binding (Ctrl+F stays, now pointing at Find Music).

## What stays exactly as-is

- Playback controls, karaoke display, timeline widget and its drag-sync
  interaction, verse row editing, undo, sort, shift-all.
- All backend logic: Soulseek search/scoring, Spotify scraping, lyrics
  fetching, Validate Sync, slskd lifecycle management.
- The color-coded status system for library entries.
- Settings dialog, About dialog, Keyboard Shortcuts dialog (content
  auto-derived from the command table, unaffected by this pass beyond
  the Find Music entry).

## Data flow notes

- `DownloadView` (or its restyled successor) keeps its existing
  `files_ready`/`open_in_library` signals; the receiving end changes
  from "add a tab" to "swap the workspace back to the editor and select
  the new song," and the library list gets a direct per-row update as
  each download completes rather than only refreshing on tab-switch.
- The workspace-swap mechanism is a `QStackedWidget` (or equivalent)
  holding the three states (editor / acquire-review / empty), swapped by
  index rather than the current `QTabWidget`.

## Testing plan

- Headless (offscreen-platform) construct/close smoke test, extended to
  also exercise: opening Find Music with plain text vs. a Spotify link
  and confirming the correct sub-flow triggers; workspace-state
  transitions (empty → editor → acquire → editor); the `_clear_lyrics()`
  / `_replace_all_verses()` split (Get Lyrics doesn't prompt, Clear All
  does — extending the existing confirmation tests).
- Manual verification via screenshot (as done in the last pass) since
  this is fundamentally a visual/layout change — automated tests cover
  logic and structure, not "does it look right."
- Existing test suite (54 tests as of the last pass) must stay green;
  new tests added, none removed unless the code they cover is deleted
  (e.g. tests tied to the old tab-switch behavior get replaced, not
  just left to rot).

## Risks / open questions

- Restyling `DownloadView`'s table to visually match the library list is
  the fuzziest part of this spec — exact visual treatment isn't
  prescribed here in pixel detail and will need judgment during
  implementation, verified with screenshots rather than guessed.
- Moving `DownloadView` out of a tab and into a stacked workspace state
  touches its parent-widget assumptions (sizing, layout margins) —
  needs verification it still renders correctly at various window
  sizes, not just the 1200x850 default.
- This is a large, single structural change to `app.py` (which is
  already the largest file in the package, ~1500 lines). Worth
  evaluating during implementation planning whether pieces of the new
  workspace/library-panel construction should land in their own module
  (e.g. `library_panel.py`, `workspace.py`) rather than growing
  `app.py` further — flagged here as a planning decision, not resolved
  in this spec.
