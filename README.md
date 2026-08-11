# LRC Sync Creator Tool

LRC Sync Tool is a powerful application for synchronizing audio files with their lyrics, designed to assist in creating LRC files by visually managing and syncing lyrics to audio files in real-time. 
![Demo](ExampleUseVideo.gif)
---

## Features

1. **Audio Playback**: Load and play `.mp3`/`.flac`/`.wav`/`.ogg`/`.m4a`/`.opus` files via VLC, with a seek bar and PipeWire/PulseAudio output-device selection (Linux).
2. **Lyric Synchronization**: Sync lyrics with audio by adding, adjusting, and editing timestamps — by keyboard, by dragging blocks on the timeline, or with a global ±ms offset for tracks that drift uniformly.
3. **Verse Management**: Add, delete, sort, or undo verse edits; fast keyboard-driven line entry (press Enter to jump to the next line).
4. **Keyboard Shortcuts** (see Help → Keyboard Shortcuts in-app for the full list):
    - **Space**: Toggle play/pause.
    - **S**: Sync the current line to playback and advance.
    - **Enter** (in a line): Move to the next line.
    - **↑ / ↓**: Select previous/next line. **← / →**: Seek -5s/+5s.
    - **Ctrl+S**: Save. **Ctrl+Z**: Undo last sync. **Ctrl+T**: Sort by time.
5. **Online Lyrics Fetching**: Search for and import synced lyrics online, individually or in a batch across your whole file list.
6. **File Operations**:
    - Load and save `.lrc` sidecar files.
    - Optionally embed the same synced lyrics into the audio file's own tags (ID3 USLT / Vorbis comment / MP4 atom).
    - Prompts to save before you switch songs or closing the app with unsaved changes.
7. **Soulseek Integration** (Tools → Find on Soulseek / Import Spotify Playlist): search and download tracks via a local [slskd](https://github.com/slskd/slskd) daemon, with lyrics fetched automatically once a download lands.
8. **Spotify Playlist Import** (Tools → Import Spotify Playlist): paste a playlist link, and every track not already in your library gets queued for Soulseek download — no Spotify developer account needed.
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
Runs locally on CPU; the app measured ~1.5s to load its model plus ~2.5s per sampled line on a 16-thread i7 laptop (first run also downloads a ~150MB model, needs internet once). It only samples a handful of short clips per song, not the whole track, specifically to stay light — see the "Validate Sync" section below for what it can and can't catch.

**Optional — Soulseek / Spotify import:**
```bash
./scripts/setup_slskd.sh   # downloads slskd, writes a starter config
```
Then edit `slskd-bin/slskd.yml`: replace the `soulseek.username`/`password` placeholders with a real Soulseek account (Soulseek auto-registers a new username the first time you log in with it — pick something real). You'll likely also want to add a `shares.directories` entry pointing at some real folder of yours — in testing, plenty of peers simply don't respond to searches from accounts sharing zero files. Then:
```bash
./scripts/run_slskd.sh
```
and in LRC Studio, go to Tools → Settings and fill in slskd's URL (`http://127.0.0.1:5030` by default) and the API key `setup_slskd.sh` printed.

---

## Usage

### Starting the App
Run `./run-lrc.sh`, or directly: `python3 lrc_studio/main.py`.

### Syncing Lyrics
1. Add audio files via "Add Files" or drag-and-drop.
2. Use "Web Search" or "Download Lyrics" (batch) to fetch existing synced lyrics, or type your own.
3. Play the track and press **S** to stamp each line as it starts, or drag verse blocks directly on the timeline to fine-tune.

### Finding & Downloading Music
- **Tools → Find on Soulseek**: search by artist/title, pick a result (sorted by free upload slot, queue length, then bitrate), and it downloads straight into your configured library folder and auto-fetches lyrics.
- **Tools → Import Spotify Playlist**: paste a playlist link. Every track is fuzzy-matched (artist + title) against your library folder first — only what's actually missing gets downloaded. Spotify's public embed page (no login needed) caps out at a playlist's first 50 tracks; longer playlists are clearly flagged as truncated rather than silently dropping the rest.

### Validate Sync
Tools → Validate Sync samples up to 5 lines spread across the song, extracts a short clip around each one's timestamp, and transcribes it locally with faster-whisper. Each sampled line is checked two ways:
- **Text**: does the transcription roughly match the lyric line? (fuzzy match, tolerant of ASR mishearing individual words)
- **Timing**: does detected speech line up with the stamped timestamp?

This is a spot-check, not an audit — it won't catch problems in lines it doesn't sample, and Whisper's own word-level timestamps have a measured ~1s wobble on a clip's very first word, so the timing check is deliberately tuned loose (flagging multi-second drift, not fine-grained accuracy). Treat flagged lines as "worth a listen," not certainties.

---

## User Interface
The interface is quite intuitive:


![Screenshot of the working program](Exampleuse1.png)

- The top section features the play/pause button along with details about the currently playing track.
- Below that, there is an interactive playback bar.
- The bottom left section displays all the verses.
- Each verse includes a timestamp, the lyrics, and three buttons: "sync," "delete," and "add."
- On the right side, there are buttons for adding lyrics, saving the file, opening an MP3, and more.

---

## Roadmap
- Waveform rendering in the timeline
- Official Spotify Web API support (for playlists over 50 tracks, if you're willing to register a developer app)

## License

MIT License.
