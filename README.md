# LRC Studio

LRC Studio is a desktop app for keeping a music library with synced lyrics. It finds and downloads music from Soulseek, imports Spotify tracks, albums and playlists, finds lyrics online, and has an editor for syncing them by hand when nothing online fits.

![Demo](ExampleUseVideo.gif)

## What it does

- Your library folder loads on startup, newest songs first. Songs that arrived since you last looked are tagged "new" until you open them. Every song shows a dot: filled green for synced lyrics, filled sand for lyrics without timing, hollow for none. One filter with live counts shows only new songs, songs without lyrics or songs not synced yet, and the search box narrows the list by name.
- Everything you can do to songs is on the right-click menu, for one song or a selection (Shift- or Ctrl-click, Ctrl+A for everything shown): open in the editor, find lyrics, rename to "Artist - Title" from the tags (with a preview of every change), edit artist and title, convert lossless files to Opus, show in the folder, or move to the trash (Del). The Library menu has the same actions for the current selection.
- LRC Studio talks to Soulseek itself, like Nicotine+, so there is no separate program to install. Type the artist and title on the Search page (Ctrl+F) and results appear as people answer. Copies of the same file are grouped into one row per version, and the version you most likely want is on top and already selected. Versions with every word you typed in the file name come first; karaoke, remix, live and sped-up versions you didn't ask for go further down; after that, the most shared, the best quality and the soonest available win. Enter downloads the best copy, Shift+Enter the whole album. If a copy fails, the other copies of the same version are tried automatically. Each row shows its own download and says "In library" for songs you already have.
- The Downloads page (Ctrl+J) lists every download and what it's doing: starting, queued by the uploader (with its place in the queue), or percent, speed and time left. Up to six run at once. Right-click to try another copy, retry or cancel.
- Finished downloads are converted, renamed and filed. Lossless files (FLAC, WAV, AIFF, ALAC, APE, WavPack) become Opus, 192 kbps by default, with tags and cover art kept, and the original is deleted once the Opus file checks out. Lossy files are never re-encoded. Each file is renamed to "Artist - Title" from its tags, moved into the library, and its lyrics are looked up. Each of these steps can be switched off in Settings.
- Lyrics come from LRCLIB first, matched on artist, title, album and the track's real duration. A hit is checked against Megalobiz. When the two disagree, QQ Music and NetEase are asked too and the version most sources agree on wins, since LRCLIB entries are sometimes several seconds off. When LRCLIB has nothing, those sources fill in, and YouTube Music can be turned on in Settings for more Spanish and Catalan songs. Credit lines are stripped, and lyrics whose timing runs past the end of the song are rejected. Plain text without timing is the last resort, and the dot shows it. Lyrics you have synced are never overwritten automatically.
- Find lyrics (Ctrl+L) shows every source's results in one list, each with its length next to your file's, so a version that is 40 seconds longer stands out. Preview one and pick it. With several songs selected in the library, Ctrl+L looks them all up automatically.
- Pasting a Spotify link into the search box opens its review. Every track you don't already have is matched on Soulseek before anything downloads, and "Pick another copy…" shows the other results.
- In the editor, press S to stamp the current line, drag markers on the timeline, shift every timestamp at once (Ctrl+Shift+O), and save to a `.lrc` file next to the song (Ctrl+S). Ctrl+Z undoes the last timing change, including a whole shift. Saving keeps the file's tags such as `[ar:]` and `[ti:]`, and can also embed the lyrics in the audio file's tags (MP3, FLAC, Opus, OGG, M4A).
- Check Sync (optional) spot-checks a few lines against the audio with faster-whisper.

## Installation
Clone this repository, then install the dependencies:
```bash
pip install -r requirements.txt
```
You'll also need VLC installed at the system level, `ffmpeg` for converting to Opus, and `pactl` on Linux for the audio-device selector.

Optional, for Check Sync and for YouTube Music lyrics:
```bash
pip install -r requirements-optional.txt
```
Check Sync also needs `ffmpeg` on your system `PATH` (used to extract the short audio clips it transcribes). Install it via your package manager (e.g. `apt install ffmpeg`, `dnf install ffmpeg`). Without it, every sampled line comes back unable to check.

Check Sync runs locally on CPU; the app measured ~1.5s to load its model plus ~2.5s per sampled line on a 16-thread i7 laptop (the first run also downloads a ~150MB model, so it needs internet once). It only samples a handful of short clips per song, not the whole track, to stay light. See the "Check Sync" section below for what it can and can't catch.

## Usage

Run `./run-lrc.sh`, or `python3 lrc_studio/main.py`.

Open File → Settings (Ctrl+,) once and set:

- Your music folder. The library fills itself from it on every start; drop files or folders on the window to add others.
- A Soulseek username and password. Soulseek registers a new name the first time you log in with it. If you used the old slskd setup, its account is picked up automatically. Don't use the same account in Nicotine+ at the same time: Soulseek logs the other session out.
- Whether to share your music folder. Many people only send files to users who share something, so sharing is on by default, with uploads limited to two at a time.

LRC Studio logs in to Soulseek in the background when it starts, and the connection state shows next to the tabs. It listens on port 2234 and asks your router to forward it (UPnP). Without a forwarded port, downloads from people who are also behind a firewall can fail. Downloads stay in `~/.local/share/LrcStudio/downloads` until they finish, then move into your music folder.

### Finding music

Ctrl+F opens the Search page. Type the artist and the title and press Enter; results keep arriving for up to a minute. The filter box narrows them (`-word` hides) and "Lossless only" hides everything else. Click the arrow on a row to see who has that version, and pick a particular copy if you prefer it.

Quality is shown per version. Anything below 192 kbps (128 kbps for Opus and OGG) is greyed out.

### Importing from Spotify

Paste a track, album or playlist link into the search box. Tracks already in your library are marked straight away. Spotify's public embed page only lists the first 50 tracks of an album or playlist, and longer ones are flagged as truncated. The rest are searched on Soulseek three at a time: artist and title first, then the title alone, then the artist alone. Each row shows how many peers have answered. The pick favours any file that is good enough (lossless, or lossy at 192 kbps or more, 128 for Opus/Vorbis) and then whichever will arrive soonest.

Nothing downloads until you click "Download all missing" or a row's "Download". "Download all missing" works as soon as one track is matched, and tracks still being searched then download as soon as they're matched. Each row shows its download live. If a source rejects the request, never answers, stops sending, or keeps the track in its queue for three minutes, the row moves on to its next candidate, up to three per track; "Try another copy" does that right away.

### Syncing lyrics

1. Double-click a song in the library, or select it and press Enter. If it has no lyrics, click Find lyrics, or type them in (Enter moves to the next line).
2. Play it and press S as each line starts, or drag the markers on the timeline.
3. Save with Ctrl+S.

Help → Keyboard Shortcuts lists every shortcut.

### Check Sync

Lyrics → Check Sync samples up to 5 lines spread across the song, extracts a short clip around each one's timestamp and transcribes it locally with faster-whisper. Each line is checked for text (a fuzzy match that tolerates misheard words) and for timing (does speech start near the timestamp?). It is a spot-check and won't catch problems in lines it doesn't sample. Whisper's word timestamps wobble by about a second on a clip's first word, so the timing check only flags multi-second drift.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run without a display (Qt's offscreen platform), never touch your real settings or library, and never go online: Soulseek and the lyrics sources are replaced with fakes. The format conversion tests need `ffmpeg`.

## Roadmap

- Waveform rendering in the timeline
- Official Spotify Web API support, for albums and playlists over 50 tracks

## License

LRC Studio's own code is under the MIT License. It uses aioslsk for Soulseek, which is GPL-3.0-or-later; if you distribute LRC Studio together with it, the GPL's terms apply to the combined work.
