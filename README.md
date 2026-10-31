# LRC Studio

LRC Studio downloads songs from a Spotify track, album, or playlist link
(via Soulseek) and gets them synced lyrics — with a real editor underneath
for when you want to fine-tune the sync yourself.
![Demo](ExampleUseVideo.gif)
---

## Features

1. **Find Music**: one search box, reached from the library's "+ Add Music"
   menu — type a song to search Soulseek directly, or paste a Spotify
   track/album/playlist link to pull its whole tracklist. A pasted link
   fetches, dedupes against your library, and auto-matches every missing
   track on Soulseek — nothing downloads until you've reviewed the matches
   (and overridden any you don't like) and click Download.
2. **The editor**: the audio player, verse list, and timeline for syncing
   lyrics by hand — everything a downloaded (or manually added) track
   needs before it's "done." It's always there in the main workspace,
   ready as soon as you pick a song from the library.
3. **Audio Playback**: Load and play `.mp3`/`.flac`/`.wav`/`.ogg`/`.m4a`/`.opus` files via VLC, with a seek bar and PipeWire/PulseAudio output-device selection (Linux).
4. **Lyric Synchronization**: Sync lyrics with audio by adding, adjusting, and editing timestamps — by keyboard, by dragging blocks on the timeline, or with a global ±ms offset for tracks that drift uniformly.
5. **Verse Management**: Add, delete, sort, or undo verse edits; fast keyboard-driven line entry (press Enter to jump to the next line).
6. **Keyboard Shortcuts** (see Help → Keyboard Shortcuts in-app for the full list):
    - **Space**: Toggle play/pause.
    - **S**: Sync the current line to playback and advance.
    - **Enter** (in a line): Move to the next line.
    - **↑ / ↓**: Select previous/next line. **← / →**: Seek -5s/+5s.
    - **Ctrl+S**: Save. **Ctrl+Z**: Undo last sync. **Ctrl+T**: Sort by time.
7. **Online Lyrics Fetching**: Search for and import synced lyrics online, individually or in a batch across your whole file list — downloaded tracks get this automatically.
8. **File Operations**:
    - Load and save `.lrc` sidecar files.
    - Optionally embed the same synced lyrics into the audio file's own tags (ID3 USLT / Vorbis comment / MP4 atom).
    - Prompts to save before you switch songs or closing the app with unsaved changes.
9. **Validate Sync** (Tools → Validate Sync, optional): a local, offline spot-check of a handful of lines against the audio using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — flags lines whose text or timing look off. Not installed by default; see below.

---

## Installation
Clone this repository, then install the dependencies:
```bash
pip install -r requirements.txt
```
You'll also need VLC and Qt installed at the system level (the `vlc` binary/libraries, and `pactl` on Linux for the audio-device selector).

**Optional — Validate Sync:**
```bash
pip install -r requirements-optional.txt
```
Also needs `ffmpeg` on your system `PATH` (used to extract the short audio clips it transcribes) — install it via your package manager (e.g. `apt install ffmpeg`, `dnf install ffmpeg`). Without it, every sampled line comes back unable to check.

Runs locally on CPU; the app measured ~1.5s to load its model plus ~2.5s per sampled line on a 16-thread i7 laptop (first run also downloads a ~150MB model, needs internet once). It only samples a handful of short clips per song, not the whole track, specifically to stay light — see the "Validate Sync" section below for what it can and can't catch.

**Optional — Soulseek / Spotify import:**
```bash
./scripts/setup_slskd.sh   # downloads slskd, writes a starter config
```
Then edit `slskd-bin/slskd.yml`: replace the `soulseek.username`/`password` placeholders with a real Soulseek account (Soulseek auto-registers a new username the first time you log in with it — pick something real). You'll likely also want to add a `shares.directories` entry pointing at some real folder of yours — in testing, plenty of peers simply don't respond to searches from accounts sharing zero files.

In LRC Studio, go to Tools → Settings and fill in slskd's URL (`http://127.0.0.1:5030` by default) and the API key `setup_slskd.sh` printed. That's the last manual step — the first time a Soulseek feature needs slskd and it isn't already running, LRC Studio launches it itself (via `./scripts/run_slskd.sh`) and waits for it to come up; you can still run that script by hand if you'd rather watch its logs directly.

---

## Usage

### Starting the App
Run `./run-lrc.sh`, or directly: `python3 lrc_studio/main.py`.

### Syncing Lyrics
1. Add audio files via the library's "+ Add Music" → "Add Files," or drag-and-drop.
2. Use **Get Lyrics** to fetch existing synced lyrics for the loaded song, or type your own. To fetch lyrics for every file missing them at once, use the library panel's overflow (**⋮**) menu → "Fetch Lyrics for All Missing."
3. Play the track and press **S** to stamp each line as it starts, or drag verse blocks directly on the timeline to fine-tune.

### Finding & Downloading Music

**Find Music** (library "+ Add Music" menu, or Tools → Find Music, Ctrl+F) is the main way to get music in. It's one search box: type a song to search Soulseek directly, or paste a Spotify track/album/playlist link to pull its whole tracklist — the app figures out which from what you enter.

Pasting a link opens the review workspace, next to your library rather than replacing it:
1. Every track is fuzzy-matched (artist + title) against your library folder first — tracks you already have are marked "In Library" immediately. Spotify's public embed page (no login needed) caps album/playlist links at their first 50 tracks; longer ones are clearly flagged as truncated rather than silently dropping the rest.
2. Everything missing gets auto-searched on Soulseek right away and the table fills in with each track's best match (quality, size, upload-slot status) — but **nothing downloads yet**. The pick favors whichever good-enough file will arrive soonest: any lossless file or lossy one at 192 kbps or more (128 for Opus/Vorbis) counts as good enough, and among those a free upload slot, a short queue and a fast peer beat extra bitrate.
3. Review the **Match** column. Don't like a pick (wrong version, low bitrate, no free slot)? Click **Change Match** on that row to see every candidate and pick a different one, or re-run the search with an edited query.
4. Click **Download All Missing** (or a single row's **Download**) to actually fetch whatever's currently selected per row. Live per-row status, an overall progress bar, and a **Cancel** button for the whole batch. If a source rejects the request or stops sending for three minutes, the row moves on to its next candidate by itself (up to three sources per track); a slow transfer that keeps making progress is left to finish.
5. Finished downloads get lyrics fetched automatically and appear in the library immediately as each one lands — no need to switch views to see it happened. Once a batch finishes, the editor opens on the first newly-downloaded song, ready to sync.
6. Rows that failed get a **Retry Search** or **Retry** (download) action, whichever applies. A download retry starts from a source that hasn't failed yet; hover the status to see why each one failed.

Typing a plain search instead of pasting a link is for a one-off track outside any playlist: pick a result, and it downloads straight into your library folder and auto-fetches lyrics.

### Validate Sync
Tools → Validate Sync samples up to 5 lines spread across the song, extracts a short clip around each one's timestamp, and transcribes it locally with faster-whisper. Each sampled line is checked two ways:
- **Text**: does the transcription roughly match the lyric line? (fuzzy match, tolerant of ASR mishearing individual words)
- **Timing**: does detected speech line up with the stamped timestamp?

This is a spot-check, not an audit — it won't catch problems in lines it doesn't sample, and Whisper's own word-level timestamps have a measured ~1s wobble on a clip's very first word, so the timing check is deliberately tuned loose (flagging multi-second drift, not fine-grained accuracy). Treat flagged lines as "worth a listen," not certainties.

---

## User Interface
One window, not separate screens for browsing your library and finding new
music — a persistent **library panel** on the left, and a **workspace** on
the right that shows whichever of three things is relevant:

![Screenshot of the working program](Exampleuse1.png)

- **Library panel** (always visible): your song list, color-coded by sync
  status; a "+ Add Music" menu (Add Files, Find Music); and an overflow
  (**⋮**) menu for the occasional stuff — Standardize Names, Clear
  Completed, Fetch Lyrics for All Missing.
- **The editor**, once you pick a song: playback controls and the karaoke
  display up top, the timeline below that, then the verse list and its
  action row (Save, Get Lyrics, Sort Lines, Shift All, Clear All).
- **The review table**, while a Spotify link is being imported: track,
  status, and match columns, reviewed and downloaded without ever leaving
  the library behind.
- **An empty state**, when nothing's loaded yet, inviting you to select a
  song or add music rather than showing a blank editor shell.

---

## Development

The `utils.py` timestamp/LRC-parsing helpers have a test suite (Qt-free, so no display is needed to run it):
```bash
pip install -r requirements-dev.txt
pytest
```

---

## Roadmap
- Waveform rendering in the timeline
- Official Spotify Web API support (for albums/playlists over 50 tracks, if you're willing to register a developer app)

## License

MIT License.
