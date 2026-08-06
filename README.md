# LocalFellow

**A fully local, botless AI meeting notetaker for macOS.** Records in-person
meetings and Zoom/Google Meet calls (no bot joins!), then generates a
speaker-labelled transcript and a structured AI note — summary, action items,
decisions, topics, all with clickable timestamps. **Everything runs and stays
on your Mac**: whisper.cpp transcription, sherpa-onnx speaker diarization +
voice recognition, and Ollama for notes and chat. Zero cloud, zero API tokens,
zero subscription.

## Quick start

```bash
brew install whisper-cpp ffmpeg ollama
ollama pull qwen2.5:7b-instruct   # notes; also try hermes3:8b for Ask
./run.sh                          # opens http://127.0.0.1:8377
```

First run downloads the whisper + speaker models (~1.7 GB one-time), builds
the Swift capture tools, creates the Python env, and launches the menu-bar
companion. macOS will ask for Microphone and Screen-Recording permission on
first recording — approve both (screen recording is how system audio is
captured for online meetings; note macOS re-asks monthly).

## Use

1. **+ New** → pick mode:
   - **In-person (mic)** — records the MacBook mic.
   - **Online (system + mic)** — botless Zoom/Meet capture: system audio =
     "Others", your mic = "Me". Wear headphones to avoid echo duplication.
2. Optional: fill **Context / vocabulary** (names, domain terms) — it is fed to
   whisper as the calibrated domain prompt and to the AI Note LLM.
3. **● Record** → meet → **■ Stop**. Transcription + AI Note run automatically.
4. Tabs: **Agenda** (talking points / action items / notepad, editable any time),
   **AI Note**, **Transcript** (search, click timestamps to seek audio).
5. **Copy** buttons on every tab (Markdown). Click a speaker name in the
   transcript to rename — one line or all lines by that speaker.
6. **⬆ Upload** any M4A/WAV/MP3/WEBM/MP4 for the same treatment.

## Architecture

- `server.py` — FastAPI: meetings CRUD, record start/stop, upload, rename, notes.
- `recorder.py` — ffmpeg mic capture (crash-safe raw PCM) + `systemaudio` child.
- `systemaudio.swift` — ScreenCaptureKit system-audio tap (16 kHz mono CAF).
- `pipeline.py` — whisper.cpp (calibrated: raw audio, beam 8, domain prompt,
  hallucination-loop dedupe) → per-track transcripts merged by timestamp →
  Ollama JSON note.
- `static/` — dark Fellow-style single-page UI.
- `data/meetings/<id>/` — audio, transcript.json, note.json/md, agenda.json.
  Deleted meetings go to `data/trash/`.

## Google Calendar

Sidebar → **📅 Today → connect** → paste your calendar's private ICS URL
(Google Calendar → Settings → your calendar → *Secret address in iCal format*).
LocalFellow then lists today's meetings, prefills title + attendee names when you
hit **● rec** on an event (attendees feed the speaker suggestions and whisper
vocabulary), and pops up "*Meeting X is starting — Record now?*" within 3 minutes
of the start time — Fellow-style. Refresh is every 5 min; simple daily/weekly
recurrences are supported.

## After the note is ready (auto, 2026-07-30)

- The AI Note is **auto-exported as a formatted .docx** to
  `My Drive/LocalFellow/` (opens directly in Google Docs; override folder with
  `gdoc_dir` in `data/settings.json`). Manual re-export: "📄 Save to Google Doc".
- If the meeting was started from a calendar event, LocalFellow writes a
  **debrief into the event's description** (summary, action items, topic list,
  Google Doc path) via macOS Calendar.app → syncs to Google Calendar.
  ONE-TIME SETUP: System Settings → Internet Accounts → add the Google account
  → enable Calendars. Existing description text is preserved above the marker.

## Pause / Resume

⏸ Pause during breaks, ▶ Resume after — each resume records a new segment;
segments are joined seamlessly at processing (pause time is simply absent).
The timer shows recorded time, not wall-clock.

## Audio quality (upgraded 2026-07-29)

- Everything records and plays back at **48 kHz** (previously 16 kHz — which
  sounded muffled). Whisper gets a dedicated 16 kHz downmix (soxr resampler).
- Quiet/far-field sources get a **static gain boost** before transcription
  (dynamic normalizers are deliberately avoided — they cause hallucination loops).
- Transcription auto-picks **large-v3** (full, most accurate) when present,
  falling back to large-v3-turbo. large-v3 is ~2–3× slower but noticeably better
  on distant or accented speech. Delete `models/ggml-large-v3.bin`
  to force turbo.

## Speaker diarization (added 2026-07-30)

In-person recordings and the online "Others" channel are automatically
separated into **Speaker 1..N** (sherpa-onnx: pyannote segmentation +
ERes2Net embeddings, ~11× real-time, models in `models/`). Click a speaker
name in the Transcript tab to rename — the dialog suggests previously used
names and calendar attendees (people directory in `data/people.json`), and
you choose one-line vs all-lines scope. Rename, then Regenerate the note to
get real names in summaries and calendar debriefs.

## Note templates (added 2026-07-31)

Pick the AI-note format per meeting (dropdown in the meeting header; changing
it offers instant regeneration):

- **General Meeting** — Summary · Action items · Decisions · Topics (default).
- **Formal Minutes** — official-record style: Attendees, Agenda Items
  Discussed, Decisions & Resolutions, Action Items, Matters Arising, Next
  Meeting. Minutes record *outcomes*; the recap records *discussion*.
- **Lecture / Class** — Key Teachings (with citations), Illustrations &
  Stories, Practical Applications, Q&A. No invented action items.
- **Client Discovery** — Fellow's sales template: Outcome, Sentiment, Needs,
  Decision Maker, Budget, Competitors, Objections.

Custom templates: add to `data/templates.json` (same shape as the built-ins
in `note_templates.py` — a template is just named sections with extraction
prompts).

## Storage & retention

- Recordings live ONLY at `data/meetings/<id>/` (audio never leaves your Mac);
  deleted/merged meetings go to `data/trash/`.
- **Retention policy (daily sweep): audio older than 30 days is deleted;
  transcripts, AI notes, and agendas are kept forever.** Trash entries older
  than 30 days are fully removed. Configure via `retention_days` in
  `data/settings.json` (0 = keep audio forever). Purged meetings show
  "audio removed by retention policy" and hide the player.

## Voice recognition (added 2026-07-30)

LocalFellow remembers voices and labels known people by name automatically:

- **👤 My name** (sidebar): your mic channel on calls is labelled with your
  name instead of "Me".
- **🎤 Enroll voice**: one-time ~20 s recording creates your voiceprint.
- **Renames teach it**: any all-lines rename to a real name harvests that
  person's voiceprint from the meeting audio — so everyone you name once is
  auto-recognized in future recordings.
- Matching is conservative (cosine ≥ 0.70; calibrated so a wrong name is
  near-impossible — uncertain voices stay "Speaker N"). Store:
  `data/voiceprints.json` (up to 6 prints/person, newest kept).

## Ask + search + MCP (upgrade #2, added 2026-07-30)

- **🔮 Ask** (sidebar button): chat with your whole meeting library. Runs a
  local model via Ollama (default `qwen2.5:7b-instruct`; change with
  `ask_model` in `data/settings.json` — `hermes3:8b` installed, `gemma4:12b`
  a candidate). Zero API tokens; answers cite meeting + [MM:SS]. First
  search is auto-seeded so answers are always grounded.
- **Search all meetings** box above the library list: full-text over every
  transcript, note, and title (SQLite FTS5, `data/index.db`, auto-refreshed).
- **MCP server for Claude** (`mcp_server.py`, registered as `localfellow`,
  user scope): Claude Code/Desktop on this Mac can list/search meetings and
  read transcripts/notes/agendas — use Claude for heavy synthesis (multi-
  meeting summaries, drafting emails) where the local model is too small.

## Known limits (next steps)

- Echo: without headphones, "Me" may duplicate "Others" lines on calls
  (voice-processing/AEC capture is the planned fix).
- Next upgrades queued: FTS search index + local Ask panel (hermes3:8b
  downloaded) + MCP server for Claude; note templates; menu-bar quick-record.
- See `../PRD-LocalNotetaker.md` for the full roadmap (Esther/Android Phase 2).
