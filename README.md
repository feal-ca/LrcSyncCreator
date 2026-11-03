# LRC Studio

LRC Studio is a desktop app for keeping a music library with synced
lyrics. It searches and downloads from Soulseek (through slskd), imports
Spotify tracks, albums and playlists, finds lyrics online, and has an
editor for syncing them by hand when nothing online fits.

![Demo](ExampleUseVideo.gif)

## What it does

- Your library folder loads on startup. Every song shows a dot: filled
  green for synced lyrics, filled sand for lyrics without timing, hollow
  for none. A search box and a status filter narrow the list, and the line
  under it counts what is left to do.
- The Search page works like Nicotine+. Search a title, then type the
  artist into the filter box (or the other way round). The filter matches
  the whole remote path and the uploader, ignores accents, and `-word`
  excludes. You can also filter by format, minimum bitrate and free upload
  slot, sort by any column, download several files at once, or grab a
  peer's whole folder (an album).
- The Downloads page lists every download, from Search and from playlist
  imports alike, and says what each one is doing: waiting for the peer to
  answer, its place in the peer's queue, or percent, speed and time left.
  Up to six run at once. A peer that rejects the request or never answers
  is dropped for another peer's copy of the same file when one exists, and
  "Try another source now" (right-click) does it on demand. You can cancel
  and retry, and the app asks before quitting with downloads running.
- Finished downloads are converted, renamed and filed. Lossless files
  (FLAC, WAV, ALAC) become Opus at 192 kbps with tags and cover art kept,
  and the original is deleted once the Opus file checks out. Lossy files
  are never re-encoded. Each file is renamed to "Artist - Title" from its
  tags and moved into the library, and lyrics are fetched for it. All of
  this can be switched off in Settings.
- Lyrics come from LRCLIB first, matched on artist, title, album and the
  track's real duration, so a live cut or an extended mix is not taken
  for the album version. Then other providers (Musixmatch, NetEase,
  Megalobiz) are tried, and their results are rejected if their timing
  runs past the end of the song. Plain text without timing is the last
  resort, and the dot shows it. Queries come from the file's tags, with
  track numbers, "(feat. …)" and "- Remastered" stripped.
- Get lyrics (Ctrl+L) lists the candidates with their length next to
  your file's, so a version that is 40 seconds longer stands out. Preview
  one and pick it.
- Pasting a Spotify link into the search box opens the Playlist page.
  Every track you don't already have is matched on Soulseek for review
  before anything downloads. Change match opens the same filterable
  results, with the artist already typed into the filter.
- In the editor, press S to stamp the current line, drag markers on the
  timeline, shift every timestamp at once, and save to a `.lrc` file next
  to the song. Saving can also embed the lyrics in the file's tags
  (MP3, FLAC, Opus, OGG, M4A).
- Tools → Convert Lossless Files to Opus converts the FLAC/WAV/ALAC
  files already in your library, in place, after asking. The `.lrc`
  files keep working because the file name only changes its extension.
- Validate Sync (optional) spot-checks a few lines against the audio
  with faster-whisper.

## Installation
Clone this repository, then install the dependencies:
```bash
pip install -r requirements.txt
```
You'll also need VLC installed at the system level, `ffmpeg` for converting downloads to Opus, and `pactl` on Linux for the audio-device selector.

Optional, for Validate Sync:
```bash
pip install -r requirements-optional.txt
```
Also needs `ffmpeg` on your system `PATH` (used to extract the short audio clips it transcribes). Install it via your package manager (e.g. `apt install ffmpeg`, `dnf install ffmpeg`). Without it, every sampled line comes back unable to check.

Runs locally on CPU; the app measured ~1.5s to load its model plus ~2.5s per sampled line on a 16-thread i7 laptop (first run also downloads a ~150MB model, needs internet once). It only samples a handful of short clips per song, not the whole track, specifically to stay light. See the "Validate Sync" section below for what it can and can't catch.

Optional, for Soulseek and Spotify import:
```bash
./scripts/setup_slskd.sh   # downloads slskd, writes a starter config
```
Then edit `slskd-bin/slskd.yml`: replace the `soulseek.username`/`password` placeholders with a real Soulseek account (Soulseek auto-registers a new username the first time you log in with it, so pick something real). You'll likely also want to add a `shares.directories` entry pointing at some real folder of yours: in testing, plenty of peers simply don't respond to searches from accounts sharing zero files.

In LRC Studio, go to Tools → Settings and fill in slskd's URL (`http://127.0.0.1:5030` by default) and the API key `setup_slskd.sh` printed. That's the last manual step. The first time a Soulseek feature needs slskd and it isn't already running, LRC Studio launches it itself (via `./scripts/run_slskd.sh`) and waits for it to come up; you can still run that script by hand if you'd rather watch its logs directly.

## Usage

Run `./run-lrc.sh`, or `python3 lrc_studio/main.py`.

Set your music folder and slskd's details once in Tools → Settings
(Ctrl+,). The library fills itself from that folder on every start;
drop files or folders on the window to add others. When LRC Studio starts
slskd itself, slskd's output goes to `slskd-bin/slskd-app.log`.

### Finding music

Ctrl+F opens the Search page. Results arrive once peers have answered,
which takes up to 25 seconds; the counter shows how many have answered so
far, and "Show results now" stops waiting. Double-click a result or press
Enter to download it. Right-click for the whole folder, or to show only
one uploader's files.

Quality is shown per file. Anything below 192 kbps (128 kbps for Opus and
OGG) is greyed out.

### Importing from Spotify

Paste a track, album or playlist link into the search box or the
Playlist page. Tracks already in your library are marked straight away.
Spotify's public embed page only lists the first 50 tracks of an album or
playlist, and longer ones are flagged as truncated. The rest are searched
on Soulseek three at a time, artist and title first, then the title
alone, then the artist alone, and each row shows how many peers have
answered. The pick favours any file that is good enough (lossless, or
lossy at 192 kbps or more, 128 for Opus/Vorbis) and then whichever will
arrive soonest.

Nothing downloads until you click Download All Missing or a row's
Download. Download All Missing works as soon as one track is matched, and
tracks still being searched then download as soon as they're matched.
Each row shows its download live. If a source rejects the request, never
answers, stops sending, or keeps the track in its queue for three
minutes, the row moves on to its next candidate, up to three per track;
"Other source" does that right away.

### Syncing lyrics

1. Double-click a song in the library. If it has no lyrics, use
   Get lyrics, or type them in (Enter moves to the next line).
2. Play it and press S as each line starts, or drag the markers on the
   timeline.
3. Save with Ctrl+S.

Help → Keyboard Shortcuts lists every shortcut.

### Validate Sync

Tools → Validate Sync samples up to 5 lines spread across the song,
extracts a short clip around each one's timestamp and transcribes it
locally with faster-whisper. Each line is checked for text (a fuzzy match
that tolerates misheard words) and for timing (does speech start near the
timestamp?). It is a spot-check and won't catch problems in lines it
doesn't sample. Whisper's word timestamps wobble by about a second on a
clip's first word, so the timing check only flags multi-second drift.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run without a display (Qt's offscreen platform) and never touch
your real settings or library. The format conversion tests need `ffmpeg`.

## Roadmap

- Waveform rendering in the timeline
- Official Spotify Web API support, for albums and playlists over 50 tracks

## License

MIT License.
