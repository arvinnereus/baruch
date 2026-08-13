# Baruch

**A fully local, botless AI meeting notetaker for macOS.** Records in-person
meetings and Zoom/Google Meet calls — no bot joins the call — then produces a
speaker-labelled transcript and a structured AI note with clickable timestamps.
**Everything runs and stays on your Mac**: whisper.cpp for transcription,
sherpa-onnx for speaker diarization and voice recognition, Ollama for notes and
chat. No cloud, no API tokens, no subscription.

*Named after Baruch son of Neriah, Jeremiah's scribe — the man who wrote down
every word from dictation.*

## Quick start

```bash
brew install whisper-cpp ffmpeg ollama
ollama pull qwen2.5:7b-instruct   # notes; hermes3:8b recommended for Ask
./run.sh                          # opens http://127.0.0.1:8377
```

First run downloads the whisper and speaker models (~1.7 GB, once), builds the
Swift capture tools, creates the Python environment, and starts the menu-bar
companion. macOS asks for Microphone and Screen Recording permission on the
first recording — approve both. Screen Recording is how system audio is
captured for online meetings; macOS re-asks roughly monthly.

## Use

1. **+ New** → pick a mode:
   - **In-person (mic)** — records the Mac microphone.
   - **Online (system + mic)** — botless Zoom/Meet capture: system audio
     becomes "Others", your mic becomes "Me", with echo cancellation applied
     so you can work without headphones.
2. Optionally fill **Context / vocabulary** (names, domain terms). It feeds
   whisper's domain prompt and the note model.
3. **● Record** → meet → **■ Stop**. Transcription and the AI note run
   automatically; pause and resume freely.
4. Tabs: **Agenda** (talking points, action items, notepad), **AI Note**, and
   **Transcript** (searchable, click a timestamp to seek the audio).
5. Every tab has a **Copy** button (Markdown). Click any speaker name in the
   transcript to rename them — one line or every line they spoke.
6. **⬆ Upload** any M4A/WAV/MP3/WEBM/MP4 for the same treatment.

Recordings can also be started from the menu bar or from a calendar event, and
several recordings of one meeting can be merged into a single note afterwards.

## What it does automatically

- **Names the speakers it knows.** Voiceprints are learned from any rename, so
  people you name once are recognized in every later recording.
- **Exports the note** as a formatted `.docx` to `My Drive/Baruch/` (opens
  directly in Google Docs) and as Markdown into an Obsidian vault.
- **Writes a debrief back onto the calendar event** — summary, action items,
  topics, and the document path — via Calendar.app, which syncs to Google
  Calendar.
- **Deletes audio after 30 days.** Transcripts, notes, and agendas are kept
  forever. Configure with `retention_days` in `data/settings.json` (0 keeps
  audio indefinitely).
- **Updates itself truthfully.** The banner appears only when there is a real
  update, names the version it will install, and queues rather than
  interrupting a recording in progress.

## Calendars

Sidebar → **Today → connect**, then paste a calendar's private ICS URL (Google
Calendar → Settings → that calendar → *Secret address in iCal format*). Add as
many calendars as you like with **＋ add**; their events merge into one
deduplicated list, and one broken calendar never breaks the others.

Baruch prefills the title and attendee names when you record from an event, and
pops up "*Meeting X is starting — record now?*" a few minutes ahead. Refreshes
every 5 minutes; daily and weekly recurrences are supported.

## Note templates

Pick the note format per meeting from the header dropdown; changing it offers
instant regeneration.

- **General Meeting** — summary, action items, decisions, topics (default).
- **Formal Minutes** — attendees, agenda items, decisions and resolutions,
  action items, matters arising. Minutes record *outcomes*; the general recap
  records *discussion*.
- **Lecture / Class** — key teachings with citations, illustrations, practical
  applications, Q&A. Never invents action items.
- **Client Discovery** — outcome, sentiment, needs, decision maker, budget,
  competitors, objections.

Add your own to `data/templates.json`; a template is just named sections with
extraction prompts (see `note_templates.py`).

## Ask, search, and MCP

- **Ask** — chat with your whole library using a local model through Ollama
  (`ask_model` in `data/settings.json`; `hermes3:8b` works best because it
  searches on its own initiative). Answers cite the meeting and timestamp, and
  the first search is auto-seeded so replies stay grounded.
- **Search** — full-text across every transcript, note, and title (SQLite FTS5).
- **MCP server** (`mcp_server.py`) — lets Claude Code or Claude Desktop on the
  same Mac list, search, and read your meetings, for synthesis work that a
  small local model handles poorly.

## Audio and transcription

Capture and playback run at 48 kHz; whisper receives a dedicated 16 kHz
downmix. Quiet or far-field sources get a static gain boost — dynamic
normalizers are deliberately avoided because they induce hallucination loops.
Transcription prefers whisper large-v3 when present and falls back to
large-v3-turbo.

Whisper feeds each 30-second window the previous window's text, so a single bad
window can repeat one sentence through an entire recording. Baruch detects that
by unique-line ratio and re-transcribes the affected track without cross-window
context.

Speakers are separated with sherpa-onnx (pyannote segmentation plus ERes2Net
embeddings) at roughly 11× real time, then matched against stored voiceprints.
Matching is deliberately conservative — an uncertain voice stays "Speaker N"
rather than risking a wrong name.

## Layout

- `server.py` — FastAPI: meetings, recording control, uploads, notes, settings.
- `recorder.py` — crash-safe capture; raw PCM for in-person, echo-cancelled
  `voicemic` for calls, `systemaudio` for system audio.
- `pipeline.py` — audio → whisper → diarization → voiceprints → templated note.
- `calendar_ics.py`, `gcal_writeback.py` — calendar reading and debriefs.
- `gdoc_export.py`, `obsidian_export.py` — exports.
- `ask.py`, `search_index.py`, `mcp_server.py` — library intelligence.
- `static/` — the single-page UI.
- `data/meetings/<id>/` — audio, transcript, note, agenda. Deleted and merged
  meetings move to `data/trash/`.
- `version.py` and `CHANGELOG.md` — release tracking.

## Known limits

- **Processing runs inside the web server**, so the UI can become unresponsive
  while a long recording is being transcribed. Moving the pipeline to
  subprocess workers is the next planned change and the highest-value one.
- Only daily and weekly calendar recurrences are handled; monthly and yearly
  events are skipped rather than guessed at.
- Note quality is bounded by the local model. A 7B model writes accurate but
  plainer prose than a cloud model would.

Requires macOS 13+ on Apple Silicon. See `CHANGELOG.md` for release history.
