# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The installed version shows in the sidebar
footer; the Update banner names the version it is offering. Bump `version.py`
in the same commit as the change.

## 1.3.0 — 2026-08-14

### Removed
- **Collapsible sidebar.** It never worked reliably and the sidebar is the
  app's only navigation. Any saved "collapsed" state is cleared on load so a
  browser that stored one is not left with a permanently hidden sidebar.

### Added
- **Auto-merge, off by default** (`"auto_merge": true` in settings to enable).
  When a recording finishes processing, other parts of the same meeting from
  the same day are combined into one record automatically. The match rule is
  deliberately strict — same day AND either the same calendar event or an
  exact title — because silently merging two genuinely different meetings is
  far worse than leaving two parts separate.
  Because processing runs in a worker subprocess, a finishing part can wait
  for a sibling that is still transcribing without blocking the server, which
  is what stops a day being left split.
  Shipped disabled: it has been unit-tested on the match rule but the
  end-to-end test has not yet run against real recordings.

## 1.2.1 — 2026-08-13

### Fixed
- **Calendar debriefs threw on events whose description was only a previous
  debrief.** Introduced in 1.1.0: keeping backwards compatibility with the old
  marker added a second strip pass, and `text item 1 of ""` raises -1728 in
  AppleScript. Today's class silently got no debrief because of it.
- **AppleScript's own two-minute event timeout** was aborting the calendar
  query on large calendars, independently of the subprocess timeout raised
  earlier — reported to the user as a misleading "event not found". The
  script now declares a 900-second window explicitly. This is the real cause
  of debriefs having been intermittent rather than reliably broken.

## 1.2.0 — 2026-08-13

### Changed
- **Processing runs in its own process** (`worker.py`) instead of inside the
  web server. Diarization and speaker embeddings are CPU-bound Python that
  hold the GIL, so a long recording made the entire UI unresponsive while it
  processed — the app appeared to hang three times, twice during a live class.
  A separate process cannot starve the server no matter how long it runs.
- **A worker now survives a server restart.** Previously an update or crash
  during processing abandoned the work and the meeting had to be rescued at
  startup; the worker is started in its own session and keeps going. On
  startup the server checks whether the recorded worker pid is genuinely still
  running that meeting before restarting anything, so work is never duplicated
  and a dead worker is still rescued.
- A worker that dies unexpectedly marks its meeting `error` rather than
  leaving it stuck on `processing`, which used to block queued updates
  indefinitely.

## 1.1.0 — 2026-08-13

### Changed
- **Renamed to Baruch** (from LocalFellow, which derived from Fellow.ai).
  Baruch son of Neriah was Jeremiah's scribe — the man who wrote down every
  word from dictation — which is precisely this app's job, and it fits the
  machines it runs on (Caleb, Solomon, Esther).
- Calendar debriefs still recognize the old marker text, so re-running a
  debrief on an event written before the rename replaces the block instead of
  appending a second one.
- Google Doc exports keep using an existing Drive `LocalFellow` folder if one
  is there, so past exports are never orphaned; new setups get `Baruch`.
- Runtime identity is deliberately unchanged: the `/Applications` bundle, the
  LaunchAgent label, and the MCP registration keep their old names so macOS
  screen-recording and microphone permissions stay granted.

## 1.0.0 — 2026-08-13

First numbered release. Everything below already shipped and has been in daily
use for real classes and meetings; this release puts a version on it.

### Added — this week
- **Multiple calendars.** Settings hold a list of ICS URLs instead of one;
  today's events from every calendar merge into a single deduplicated list,
  and a broken calendar no longer breaks the rest. Existing single-calendar
  setups migrate automatically.
- **Version tracking.** `version.py` as the single source of truth, a version
  badge in the sidebar, `/api/version`, and an Update banner that names the
  version it will install.

### Fixed — this week
- **Hallucination loops.** Whisper feeds each 30-second window the previous
  window's text, so one bad window could repeat a single sentence through an
  entire recording (a 43-minute lecture collapsed to two alternating lines).
  Loops are now detected by unique-line ratio and the track is re-transcribed
  without cross-window context. Consecutive-line deduping alone could not
  catch alternating loops.
- **Calendar debriefs failing silently.** The Calendar.app search combined
  title and date conditions, which makes the query several times slower and
  blew the timeout — reported as "event not found". Now matches on title and
  filters dates in-script, with a longer timeout and honest error messages.
  Calendar.app is also launched in the background if it is not running
  (AppleScript error -600).
- **Confusing errors on merged meetings.** Opening a meeting that had been
  merged away returned a raw 500; the API now answers with a clear message and
  the UI no longer prints JSON-parse noise when a request fails.

## Earlier work (pre-versioning)

Reconstructed from the commit history and the project log.

### 2026-08-11
- Collapsible sidebar, persisted across sessions.
- Update banner triggers on server-code changes only (static files apply on
  refresh and need no restart).
- Zombie `processing`/`noting` meetings are rescued at startup instead of
  blocking the update system forever; stale update flags self-clear.

### 2026-08-06
- Published public: github.com/arvinnereus/localfellow.
- Self-update button that queues while recording or processing and applies
  itself when idle — never interrupts a recording.
- Template picker on the first Record press.
- Offline banner instead of a silently stale UI.
- Notes never invent speaker names.
- Fixed the 9-channel voice-processing capture that silently broke every mic
  conversion, producing empty transcripts and hallucinated notes; empty
  transcripts now raise an error instead of generating a note.
- Concat lists use absolute paths; startup sweep matches capture processes
  only, not legitimate conversions.

### Earlier
- Recording: botless system-audio capture (ScreenCaptureKit) for Zoom/Meet,
  echo-cancelled microphone capture, in-person raw capture, pause/resume,
  crash recovery, menu-bar control, `/Applications/LocalFellow.app`, always-on
  LaunchAgent.
- Transcription and speakers: whisper.cpp large-v3 with a calibrated recipe,
  sherpa-onnx diarization, voiceprint identification that names known people
  automatically.
- Notes: map-reduce generation over long meetings, four templates (general,
  minutes, lecture, discovery), agenda, copyable everything.
- Exports: Google Doc to Drive, Obsidian vault, calendar debrief written back
  onto the event.
- Library: full-text search, Ask (local LLM over your meetings), MCP server so
  Claude can read meetings, merge fragments of one meeting, 30-day audio
  retention.
