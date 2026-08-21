# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The installed version shows beside the
app name in the sidebar; the Update banner names the version it is offering. Bump `version.py`
in the same commit as the change.

## 1.8.1 — 2026-08-21

### Fixed
- **Recording no longer makes the speakers go quiet.** Echo cancellation runs
  on a macOS voice-processing unit, and that unit ducks all other output the
  way a phone call does — so while recording an online meeting, the meeting
  itself became almost inaudible even at full volume. Reported as "the speaker
  sound seems softer even at max".
  Measured on the system-audio tap, peak level while a sound played:

  | | level |
  |---|---|
  | not recording | −12.8 dB |
  | recording, before this fix | −42.8 dB |
  | recording, after | −20.8 dB |

  **30 dB of ducking cut to 8 dB.** The unit is now asked for minimum ducking
  (`voiceProcessingOtherAudioDuckingConfiguration`, macOS 14+); echo
  cancellation still works, since it uses the output as its reference rather
  than needing it quiet. The residual 8 dB is the unit's own behaviour and
  cannot be removed without giving up echo cancellation entirely.
  Only affects **Online (system + mic)** mode — in-person recording never used
  voice processing and never ducked.

## 1.8.0 — 2026-08-20

### Added
- **Speaker consolidation — a lecture is one speaker, not forty.** Diarization
  over-splits a single voice across a long recording, and merging a day's
  parts doubles the labels again because each part was diarized separately. A
  2 h 14 m class came out as **40 speakers** for one lecturer; another left
  **44** beside a correctly-named 92% speaker. Renaming them by hand was the
  most tedious thing in the app.
  Clusters whose voice centroids match are merged (cosine 0.58 — looser than
  the 0.70 identification threshold, because the question is "same voice as
  that cluster", not "is this the enrolled person"), then leftover slivers are
  absorbed into a dominant voice holding 55%+ of the speech. **Named speakers
  are never merged away or absorbed**, so a real Q&A participant survives.
  - Runs automatically after diarization and **before** voiceprint
    identification, which matters: on the 17 Aug class the fragments were each
    too short to match, but the consolidated cluster matched Rev Lynette at
    0.71 and was named with no renaming at all.
  - **Combine same voices** button on the Transcript tab applies it to any
    existing meeting.
  Measured on real classes: 40 → 1 speaker, and 44 → 4 (the lecturer, a named
  guest, and two genuine unnamed voices left intact).

## 1.7.3 — 2026-08-20

### Fixed
- **Recovery destroyed merged meetings.** A merged record holds one combined
  `meeting.wav` and no raw track files, so when a server restart triggered
  recovery on one, the raw pipeline found no tracks, failed, and stamped
  `error` on a complete record. The 17 Aug class — 134 minutes, fully
  transcribed, note written, calendar debrief done — was marked failed two
  days after it succeeded, which is why that day appeared unconsolidated.
  Every recovery path funnels through `worker.py`, so the guard lives there: a
  merged meeting is never re-run through the raw pipeline. If it already has a
  transcript and note it is simply marked ready; if only the note is missing,
  the note alone is regenerated. Verified by reproducing the exact failure and
  watching it repair the record instead of breaking it.

## 1.7.2 — 2026-08-15

### Fixed
- **README brought back in line with the code.** It still listed in-server
  processing as a known limit and called subprocess workers "the next planned
  change" — three days after they shipped — and still recommended hermes3:8b
  for Ask. A README that describes a version that no longer exists is worse
  than a short one, and this is the file strangers read first. Auto-merge, the
  health watchdog, the scoring harnesses and the note-model finding are now
  documented; the stale claims are gone.

## Benchmark result — notes stay on qwen2.5:7b-instruct (2026-08-15)

Scored on the two longest classes (192 min / 153 min, lecture template):

| | qwen2.5:7b-instruct | gemma4:12b |
|---|---|---|
| time per note | **5 min** | 43 min, then 10 hours |
| transcript-vocabulary coverage | 24% | **40%** |
| bullets per note | 103 | 158 |
| duplicate bullets | 0 | 0 |
| timestamps outside the recording | 0 | 0 |
| empty template sections | 0 | 2 |

**Decision: keep qwen2.5:7b-instruct for notes.** gemma4 writes a fuller note —
40% coverage against 24% — but took 43 minutes on the first class and **10
hours** on the second, against qwen's steady ~5 minutes. A note that arrives
the next day is not a note. Speed disqualifies it here regardless of quality,
which is the opposite conclusion to Ask, where 19 s versus 5 s per answer was
a trade worth making.

`note_model` in settings makes this a one-line change if a faster machine or a
smaller-but-better model appears.

Caveat, stated because the numbers are on record: this run used the pre-1.7.1
scorer, so its **timestamp-accuracy and invented-name columns are artifacts**
and were ignored. Coverage, bullet counts, duplicates and timings are sound,
and speed alone settles the decision.

## 1.7.1 — 2026-08-14

### Fixed
- **note_quality.py measured two things wrongly**, found on its first real run
  rather than by inspection:
  - Timestamp accuracy read "n/a" for every note. Bullets carry their
    timestamp in a separate `ts` field, not inline in the text, so the scan
    found none.
  - Invented-name detection flagged template headings ("Attendees",
    "Applications") and ordinary verbs opening a bullet ("Adapt your style").
    Template titles are now excluded, and only mid-sentence capitals count —
    which is what the original defect looked like ("led by Chin").

## 1.7.0 — 2026-08-14

### Added
- **The note model is now configurable** — `note_model` in
  `data/settings.json`, falling back to the old preference list. Ask has
  always been configurable; the model writing every class note was hardcoded,
  so it could neither be chosen nor compared.
- **`note_quality.py` — scores AI-note quality against real meetings.** Note
  quality sounds subjective, but every note failure this project has actually
  suffered is objectively checkable, and those are what it measures:
  - **invented names** — a note once credited a class to "Chin", a person
    never in the meeting, because prompt example names leaked into the output.
    Every capitalised word in the note must appear in the transcript.
  - **timestamps outside the recording** — a note citing (58:14) on a
    40-minute meeting is fabricated structure.
  - **timestamp accuracy** — a sampled bullet's timestamp must land near text
    that shares its wording. A plausible timestamp on the wrong moment is
    worse than none.
  - plus transcript-vocabulary coverage (catches fluent but empty notes),
    duplicate bullets, empty template sections, and time per note.

  It generates into a temp directory and never touches the stored note.
  Verified against a deliberately hallucinated note before use: it flags the
  invented name and the impossible timestamp, and passes a grounded one.

## 1.6.1 — 2026-08-14

### Changed
- **Ask now runs `gemma4:12b`.** Scored against hermes3:8b on the same graded
  set: **90% overall vs 70%** (grounded 86% vs 71%, honesty 100% vs 67%,
  cited a real source 6/7 vs 0/7). It is 3.5× slower — 19 s vs 5 s per answer
  — which is the right trade for a library of class material: a correct,
  cited answer beats a fast wrong one. Change `ask_model` in
  `data/settings.json` to switch back.
  Note generation still uses `qwen2.5:7b-instruct` and has NOT been
  benchmarked; that is the next open item.

## 1.6.0 — 2026-08-14

### Added
- **Health watchdog.** Two outages ran unnoticed on 2026-08-14: Ollama stopped
  (a class would have transcribed and produced no AI note, silently) and the
  server could not start at all, looping for hours. Neither surfaced anywhere
  — the offline banner only exists inside a browser tab, which is closed
  precisely when you are in the meeting being recorded.
  - `/api/health` reports what must be true for Baruch to work: Ollama
    answering, whisper-cli present, disk space, and meetings stuck
    mid-processing. Reachable from any device on the network.
  - `watchdog.py` runs every 5 minutes from its own LaunchAgent. It repairs
    what it can — restarts Ollama, restarts the server, bootstraps the
    LaunchAgent back if it was unloaded entirely, restarts a stalled worker —
    and raises a macOS notification for anything it cannot fix. Notifications
    are deduplicated so a persistent fault does not nag every five minutes.
    Verified by killing Ollama, killing the server, and unloading the
    LaunchAgent: all three were detected and repaired.
  - A banner in the app names the problem when something is wrong.

## 1.5.0 — 2026-08-14

### Changed
- **Auto-merge verified and now ON by default.** Parts of one meeting recorded
  on the same day are combined into a single record automatically once the
  last part finishes processing. Verified end to end against real audio: two
  parts merged into exactly one record, and a decoy meeting recorded the same
  day under a different title was left untouched. Set `"auto_merge": false`
  in `data/settings.json` to turn it off.

## 1.4.2 — 2026-08-14

### Fixed
- **The server could not start at all.** `.venv/bin/uvicorn` is a console
  script whose shebang hard-codes the absolute path the venv was created at —
  the pre-2026-08-04 location, reached through a symlink that has since been
  deleted. launchd restarted the server in a loop, each attempt dying with
  "cannot execute: No such file or directory", and the app was simply down.
  `run.sh` now launches through `.venv/bin/python -m uvicorn` (a relative
  symlink to the real interpreter), which survives any future relocation; pip
  is invoked the same way for the same reason.

## 1.4.1 — 2026-08-14

### Changed
- **Version badge moved beside the app name** in the sidebar header, where it
  is actually visible — it was buried in the footer. Its tooltip reports the
  RUNNING version and names a downloaded-but-not-yet-applied update
  separately, so the badge can never claim an update is installed when the
  server is still running the old code.

## 1.4.0 — 2026-08-14

### Changed
- **Search rewritten for Ask.** Scored retrieval recall — whether the answer
  is even present in the context the model receives — went from **43% to
  100%** on a graded question set, and 100% on a held-out set of questions
  never used while tuning. Four distinct faults, none of them the model's:
  - The model was handed an **18-token snippet**. A note section could match
    correctly and still pass on the summary line while the actual answer sat
    600 characters further down the same chunk. Notes now return a generous
    window around the match.
  - **Notes and utterances were ranked together.** A transcript has thousands
    of short utterances to a handful of note sections, and bm25 prefers short
    documents, so the AI note — the one place an answer is stated plainly —
    never even reached the ranking stage. The two are now queried separately
    and notes get reserved slots.
  - **Trivial matches outranked real evidence.** "I taught him." beat an
    entire teaching section. Utterance chunks under 60 characters are dropped.
  - **One meeting could take every slot**, so cross-meeting questions saw only
    part of the picture. Utterance hits are capped at three per meeting, and
    those hits now vote on whose notes to pull — the section defining a term
    is usually in the meeting the transcript hits already agree on.

### Added
- `retrieval_check.py` — scores retrieval on its own, with no LLM involved. A
  wrong answer can come from bad retrieval or from the model ignoring good
  context; those need opposite fixes, so they are now measured separately.

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
