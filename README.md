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
    - Prompts to save before you switch songs or close the app with unsaved changes.

---

## Installation
Clone this repository, then install the dependencies:
```bash
pip install -r requirements.txt
```
You'll also need VLC and Qt installed at the system level (the `vlc` binary/libraries, and `pactl` on Linux for the audio-device selector).

---

## Usage

### Starting the App
Run `./run-lrc.sh`, or directly: `python3 lrc_studio/main.py`.

### Syncing Lyrics
1. Add audio files via "Add Files" or drag-and-drop.
2. Use "Web Search" or "Download Lyrics" (batch) to fetch existing synced lyrics, or type your own.
3. Play the track and press **S** to stamp each line as it starts, or drag verse blocks directly on the timeline to fine-tune.

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
- Soulseek integration for finding/downloading missing tracks (planned — likely via a `slskd` companion daemon)
- Waveform rendering in the timeline

## License

MIT License.
