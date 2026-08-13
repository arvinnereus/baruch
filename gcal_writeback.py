"""Write a post-meeting debrief into the Google Calendar event.

The ICS feed is read-only, so we go through the macOS Calendar app via
AppleScript: editing an event's description there syncs back to Google
Calendar (requires the Google account in Calendar.app). Best-effort:
failures are returned as strings and logged, never fatal.

Notes on Calendar.app AppleScript quirks:
- Recurring events expose only the series MASTER (start date = first
  occurrence), so we search a 62-day window and take the latest match.
  Editing the master's description applies to the series; the marker block
  keeps only the newest debrief while preserving the original description.
- `whose` filters are slow, so we query ONE calendar per osascript call,
  skip system calendars, and cache the calendar that matched last time.
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "data" / "settings.json"
MARKER = "— Baruch AI Note —"
# Events written before the rename carry the old marker; keep recognizing it or
# re-running a debrief would append a second block instead of replacing.
LEGACY_MARKERS = ["— LocalFellow AI Note —"]
SKIP_CALENDARS = {"Birthdays", "Siri Suggestions", "Scheduled Reminders",
                  "Holidays in Singapore", "Birthday", "clpd birthday"}

LIST_SCRIPT = '''
tell application "Calendar" to return name of every calendar
'''

_PRELUDE = '''
  set theCal to item 1 of argv
  set theTitle to item 2 of argv
  set y to (item 3 of argv) as integer
  set m to (item 4 of argv) as integer
  set d to (item 5 of argv) as integer
  set dStart to current date
  set year of dStart to y
  set month of dStart to m
  set day of dStart to d
  set time of dStart to 0
  copy dStart to dEnd
  set time of dEnd to 86340
  set dLow to dStart - (62 * days)
  tell application "Calendar"
    set c to first calendar whose name is theCal
    -- summary-only `whose` (compound date conditions make Calendar.app's
    -- query several times slower and blow the timeout on big calendars);
    -- the date window is filtered in-script below
    set evs to (every event of c whose summary is theTitle)
    if (count of evs) is 0 then return "___NOT_FOUND___"
    set best to missing value
    repeat with e in evs
      set sd to start date of e
      if sd is less than or equal to dEnd and sd is greater than or equal to dLow then
        if best is missing value then
          set best to e
        else if sd is greater than or equal to (start date of best) then
          set best to e
        end if
      end if
    end repeat
    if best is missing value then return "___NOT_FOUND___"
'''

# Single-pass upsert: the `whose` query on a large calendar takes ~40 s, so we
# must run it ONCE — read old description, strip any previous marker block,
# and write the merged text in the same AppleScript invocation.
UPSERT_SCRIPT = f'''
on run argv
{_PRELUDE}
    set newBlock to item 6 of argv
    set theMarker to item 7 of argv
    set oldMarker to item 8 of argv
    set oldDesc to ""
    try
      set oldDesc to description of best
    end try
    if oldDesc is missing value then set oldDesc to ""
    set kept to ""
    if oldDesc is not "" then
      set AppleScript's text item delimiters to theMarker
      set kept to text item 1 of oldDesc
      set AppleScript's text item delimiters to oldMarker
      set kept to text item 1 of kept
      set AppleScript's text item delimiters to ""
    end if
    set description of best to (kept & newBlock)
    return "ok"
  end tell
end run
'''


def _ensure_calendar_running():
    """AppleScript gets error -600 when Calendar.app isn't running (common
    right after a reboot or when the user quit it). Launch it hidden in the
    background and give it a moment to finish opening its database."""
    import time as _time
    r = subprocess.run(["pgrep", "-x", "Calendar"], capture_output=True)
    if r.returncode == 0:
        return
    subprocess.run(["open", "-gja", "Calendar"], capture_output=True)
    _time.sleep(8)


def _osascript(script: str, *args, timeout=60) -> str:
    _ensure_calendar_running()
    r = subprocess.run(["osascript", "-", *args], input=script,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and "-600" in r.stderr:
        import time as _time
        _time.sleep(8)  # Calendar still launching — one retry
        r = subprocess.run(["osascript", "-", *args], input=script,
                           capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200] or "osascript failed")
    return r.stdout.strip()


def _settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_setting(key: str, value: str):
    s = _settings()
    s[key] = value
    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")


def _candidate_calendars() -> list[str]:
    names = [n.strip() for n in
             _osascript(LIST_SCRIPT, timeout=30).split(",")]
    cached = _settings().get("gcal_calendar")
    ordered = ([cached] if cached in names else []) + \
        [n for n in names if n != cached and n not in SKIP_CALENDARS]
    return ordered


def _plain(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")


def build_debrief(note: dict, gdoc_path: str = "") -> str:
    lines = [MARKER, ""]
    summary = _plain(note.get("summary", ""))
    if len(summary) > 900:
        summary = summary[:900].rsplit(" ", 1)[0] + "…"
    lines.append(summary)
    if note.get("action_items"):
        lines += ["", "Action items:"] + \
            [f"• {_plain(a['text'])}" for a in note["action_items"][:8]]
    if note.get("topics"):
        lines += ["", "Topics covered:"] + \
            [f"• {_plain(t.get('title',''))}" for t in note["topics"][:12]]
    elif note.get("sections"):
        filled = [s for s in note["sections"] if s.get("bullets")]
        if filled:
            lines += ["", "Covered:"] + \
                [f"• {_plain(s['title'])} ({len(s['bullets'])} items)"
                 for s in filled[:12]]
    if gdoc_path:
        lines += ["", f"Full AI note (Google Drive): {gdoc_path}"]
    return "\n".join(lines)


def write_debrief(title: str, created_at: int, note: dict, gdoc_path: str = "") -> str:
    """Find the event (by title, on/near the meeting day) and set the debrief
    below any existing description. Returns 'ok' or an error string."""
    day = datetime.fromtimestamp(created_at)
    dargs = [title, str(day.year), str(day.month), str(day.day)]
    block = build_debrief(note, gdoc_path)
    try:
        calendars = _candidate_calendars()
    except Exception as e:
        return f"calendar access failed: {e}"
    failures = []
    for cal in calendars:
        try:
            res = _osascript(UPSERT_SCRIPT, cal, *dargs, block, MARKER,
                             LEGACY_MARKERS[0], timeout=240)
        except Exception as e:
            failures.append(f"{cal}: {e}")  # slow/broken calendar — move on
            continue
        if res == "ok":
            _save_setting("gcal_calendar", cal)
            return "ok"
    if failures:
        return f"calendar query failed on {len(failures)} calendar(s): {failures[0][:120]}"
    return "event not found in Calendar.app (checked all calendars)"
