# LRC Studio

LRC Studio downloads songs from a Spotify track, album, or playlist link
(via Soulseek) and gets them synced lyrics — with a real editor underneath
for when you want to fine-tune the sync yourself.
![Demo](ExampleUseVideo.gif)
---

## Features

1. **Download tab**: paste a Spotify track, album, or playlist link and
   it's fetched, deduped against your library, and every missing track is
   auto-matched on Soulseek — nothing downloads until you've reviewed the
   matches (and overridden any you don't like) and click Download.
2. **Library tab**: the audio player, verse list, and timeline for
   syncing lyrics by hand — everything a downloaded (or manually added)
   track needs before it's "done."
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
1. Add audio files via "Add Files" or drag-and-drop.
2. Use "Web Search" or "Download Lyrics" (batch) to fetch existing synced lyrics, or type your own.
3. Play the track and press **S** to stamp each line as it starts, or drag verse blocks directly on the timeline to fine-tune.

### Finding & Downloading Music

**Download tab** is the main way to get music in:
1. Paste a Spotify track, album, or playlist link and click **Fetch**. Every track is fuzzy-matched (artist + title) against your library folder first — tracks you already have are marked "In Library" immediately. Spotify's public embed page (no login needed) caps album/playlist links at their first 50 tracks; longer ones are clearly flagged as truncated rather than silently dropping the rest.
2. Everything missing gets auto-searched on Soulseek right away and the table fills in with each track's best match (quality, size, upload-slot status) — but **nothing downloads yet**.
3. Review the **Match** column. Don't like a pick (wrong version, low bitrate, no free slot)? Click **Change Match** on that row to see every candidate and pick a different one, or re-run the search with an edited query.
4. Click **Download All Missing** (or a single row's **Download**) to actually fetch whatever's currently selected per row. Live per-row status, an overall progress bar, and a **Cancel** button for the whole batch.
5. Finished downloads get lyrics fetched automatically and show up in the Library tab's file list. Double-click a "Downloaded" row to jump straight to the Library tab with that song loaded and ready to sync.
6. Rows that failed get a **Retry Search** or **Retry** (download) action, whichever applies.

**Library tab → Find on Soulseek**: for a one-off track outside any playlist — search by artist/title, pick a result, and it downloads straight into your library folder and auto-fetches lyrics.

### Validate Sync
Tools → Validate Sync samples up to 5 lines spread across the song, extracts a short clip around each one's timestamp, and transcribes it locally with faster-whisper. Each sampled line is checked two ways:
- **Text**: does the transcription roughly match the lyric line? (fuzzy match, tolerant of ASR mishearing individual words)
- **Timing**: does detected speech line up with the stamped timestamp?

This is a spot-check, not an audit — it won't catch problems in lines it doesn't sample, and Whisper's own word-level timestamps have a measured ~1s wobble on a clip's very first word, so the timing check is deliberately tuned loose (flagging multi-second drift, not fine-grained accuracy). Treat flagged lines as "worth a listen," not certainties.

---

## User Interface
Two tabs: **Download** (Spotify link, track table, matches, progress) and
**Library** (shown below), which is where you land once a track's
downloaded and it's time to sync its lyrics:

![Screenshot of the working program](Exampleuse1.png)

- The top section features the play/pause button along with details about the currently playing track.
- Below that, there is an interactive playback bar.
- The bottom left section displays all the verses.
- Each verse includes a timestamp, the lyrics, and three buttons: "sync," "delete," and "add."
- On the right side, there are buttons for adding lyrics, saving the file, opening an MP3, and more.

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
