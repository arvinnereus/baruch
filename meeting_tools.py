"""Meeting-intelligence tool layer — the same functions back the in-app Ask
agent (local LLM) and the MCP server (Claude). All read-only over data/."""
import json
from pathlib import Path

import search_index

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"


def _fmt_ts(ms: int) -> str:
    s = int(ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def list_meetings(limit: int = 30) -> list[dict]:
    """Recent meetings: id, title, date, duration, status."""
    out = []
    for d in DATA.iterdir():
        f = d / "meeting.json"
        if f.exists():
            m = json.loads(f.read_text(encoding="utf-8"))
            out.append({"meeting_id": m["id"], "title": m.get("title", ""),
                        "created_at": m.get("created_at", 0),
                        "duration_s": m.get("duration_s", 0),
                        "status": m.get("status", "")})
    out.sort(key=lambda m: m["created_at"], reverse=True)
    return out[:limit]


def search_meetings(query: str, limit: int = 12) -> list[dict]:
    """Full-text search across all transcripts, notes and titles.
    Returns hits with meeting_id, timestamp and a snippet."""
    hits = search_index.search(query, limit)
    for h in hits:
        h["ts"] = _fmt_ts(h.pop("ts_ms", 0))
    return hits


def get_transcript(meeting_id: str, start_ts: str = "", end_ts: str = "",
                   max_chars: int = 9000) -> str:
    """Speaker-labelled transcript of one meeting, optionally limited to a
    [start_ts, end_ts] MM:SS window. Long transcripts are truncated."""
    import re
    if not re.fullmatch(r"[0-9a-f]{12}", meeting_id or "") or \
            not (DATA / meeting_id).exists():
        return (f"unknown meeting_id '{meeting_id}' — pass the 'id' value "
                f"from a search hit, e.g. get_transcript(meeting_id=\"abc123\", "
                f"start_ts=\"03:40\")")
    f = DATA / meeting_id / "transcript.json"
    if not f.exists():
        return "(no transcript for this meeting)"

    def to_ms(ts):
        if not ts:
            return None
        m, s = (ts.split(":") + ["0"])[:2]
        return (int(m) * 60 + int(s)) * 1000

    lo, hi = to_ms(start_ts), to_ms(end_ts)
    lines = []
    for seg in json.loads(f.read_text(encoding="utf-8")):
        if lo is not None and seg["end_ms"] < lo:
            continue
        if hi is not None and seg["start_ms"] > hi:
            continue
        lines.append(f"[{_fmt_ts(seg['start_ms'])}] "
                     f"{seg.get('speaker','?')}: {seg['text']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + \
            f"\n…(truncated — use start_ts/end_ts to read a specific window)"
    return text or "(window contains no speech)"


def get_note(meeting_id: str) -> str:
    """The AI note (Markdown) for one meeting."""
    f = DATA / meeting_id / "note.md"
    return f.read_text(encoding="utf-8") if f.exists() \
        else "(no AI note for this meeting)"


def get_agenda(meeting_id: str) -> str:
    """The agenda (talking points, action items, notepad) for one meeting."""
    f = DATA / meeting_id / "agenda.json"
    if not f.exists():
        return "(no agenda)"
    a = json.loads(f.read_text(encoding="utf-8"))
    return (f"Talking points:\n{a.get('talking_points','')}\n\n"
            f"Action items:\n{a.get('action_items','')}\n\n"
            f"Notepad:\n{a.get('notepad','')}")
